"""Plan how a preset is delivered into a worker's isolated HOME.

``plan_delivery`` only describes the links, copies, and MCP document to
apply. It does not write, unlink, or change permissions, and it never reads
``Path.home()``: the real HOME is not a delivery target. Destinations are
lexical paths under ``isolated_home``. A symlink may be inspected only when
it is already under that home's parent (the test tmp); a link that points
out of the declared roots or out of the isolated home is rejected instead of
followed.

Skill files land in ``.conductor-delivered/skills/<name>`` unless the preset
sets ``agent_home_rel`` (for example ``.agents/skills``). MCP is one
document at ``.conductor-delivered/mcp/servers.toml``. Secret-shaped values
are refused before an action exists, so a secret never appears in the plan.
"""

from __future__ import annotations

import contextlib
import math
import os
import posixpath
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .core import SkillsError

KIND_LINK = "link_skill"
KIND_COPY = "copy_skill"
KIND_MCP = "write_mcp"

_SKILL_DIR = (".conductor-delivered", "skills")
_MCP_PARTS = (".conductor-delivered", "mcp", "servers.toml")

# Substring match on purpose: API_TOKEN, client_secret, and api_key are all
# secret-shaped keys. The value, not the key, is what must stay out of the plan.
_SECRET_KEY = re.compile(r"token|secret|key|password", re.IGNORECASE)
_ENV_REF = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_KEYCHAIN_REF = re.compile(r"^keychain:[^/\s]+/[^/\s]+$")
_UNSAFE_NAME = re.compile(r"[/\\\n\r\x00]|\.\.")


class DeliveryError(SkillsError):
    """The preset cannot be delivered safely.

    Messages name the key or the path rule that failed and never include a
    secret value.
    """


@dataclass(frozen=True)
class Action:
    """One filesystem change for the caller to apply later.

    ``source`` is an absolute skill path, or the MCP document text for
    ``write_mcp``. ``mode`` is ``symlink``, ``copy``, or ``600`` (the MCP
    file mode the caller should use). ``dest`` is absolute and stays inside
    the isolated home.
    """

    kind: str
    source: str
    dest: str
    mode: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "source": self.source,
            "dest": self.dest,
            "mode": self.mode,
        }


def plan_delivery(preset: Any, isolated_home: Any) -> list[Action]:
    """Return delivery actions for ``preset`` inside ``isolated_home``.

    The same preset and home always produce the same list. Nothing is
    created or modified. An unsafe name, an escaping path, or a plaintext
    secret raises :class:`DeliveryError` before any action is built.
    """
    home = _require_home(isolated_home)
    skills = _names(_field(preset, "skills"), what="skills")
    servers = _server_names(preset)
    sources = _field(preset, "skill_sources")
    documents = _field(preset, "mcp_documents")
    if _has(preset, "allowed_roots"):
        roots = _roots(_field(preset, "allowed_roots"))
    else:
        roots = None
    agent_rel = _field(preset, "agent_home_rel")

    # Validate the whole preset before any Action exists so a failure cannot
    # hand back a partial plan. An empty preset returns before link-mode and
    # destination checks that only matter when something will be delivered.
    _validate_names(skills, what="skill")
    _validate_names(servers, what="server")
    docs = _validate_documents(documents)
    if not skills and not servers:
        return []

    if skills or sources is not None:
        source_for = _validate_sources(sources, skills, roots, home)
    else:
        source_for = {}
    if skills:
        kind, mode = _skill_materialization(_field(preset, "link_mode"))
        skill_dir = _skill_dir(agent_rel)
    else:
        kind, mode = KIND_LINK, "symlink"
        skill_dir = _SKILL_DIR

    skill_rows = [
        (name, source_for.get(name, ""), _dest(home, *skill_dir, name))
        for name in _dedupe(skills)
    ]
    mcp_dest = _dest(home, *_MCP_PARTS) if servers else ""
    mcp_body = _render_servers(_dedupe(servers), docs) if servers else ""

    actions = [
        Action(kind=kind, source=source, dest=dest, mode=mode)
        for _name, source, dest in skill_rows
    ]
    if servers:
        actions.append(
            Action(kind=KIND_MCP, source=mcp_body, dest=mcp_dest, mode="600")
        )
    return actions


def _field(preset: Any, key: str) -> Any:
    if isinstance(preset, Mapping):
        return preset.get(key)
    return getattr(preset, key, None)


def _has(preset: Any, key: str) -> bool:
    if isinstance(preset, Mapping):
        return key in preset
    return hasattr(preset, key)


# macOS binds these names to ``/private/*``. The path string differs; the
# directory does not. Any other symlink component is an escape, and it is
# refused instead of resolved into a new sandbox root.
_BENIGN_PREFIX_LINKS = {
    "/var": "private/var",
    "/tmp": "private/tmp",
    "/etc": "private/etc",
}


def _require_home(isolated_home: Any) -> str:
    if isinstance(isolated_home, os.PathLike):
        raw = os.fspath(isolated_home)
    elif isinstance(isolated_home, str):
        raw = isolated_home
    else:
        raise DeliveryError("isolated_home must be a path")
    if not isinstance(raw, str) or raw == "" or _has_control(raw):
        raise DeliveryError("isolated_home must be a path")
    if not posixpath.isabs(raw):
        raise DeliveryError("isolated_home must be absolute")
    home = posixpath.normpath(raw)
    if home == "/":
        raise DeliveryError("isolated_home must be a dedicated directory")
    _reject_symlinked_home(home)
    return home


def _reject_symlinked_home(home: str) -> None:
    """Refuse a HOME path whose own components are symlinks.

    Resolving the link and delivering into the target would silently move
    the sandbox. A missing leaf is fine: nothing there is a link yet.
    """
    try:
        if os.path.islink(home):
            raise DeliveryError("isolated_home must not be a symlink")
    except DeliveryError:
        raise
    except OSError as exc:
        raise DeliveryError("isolated_home could not be checked") from exc
    current = home
    while True:
        parent = posixpath.dirname(current)
        if parent == current:
            return
        current = parent
        if _benign_prefix_link(current):
            continue
        try:
            linked = os.path.islink(current)
        except OSError as exc:
            raise DeliveryError("isolated_home could not be checked") from exc
        if linked:
            raise DeliveryError("isolated_home ancestor must not be a symlink")


def _benign_prefix_link(path: str) -> bool:
    expected = _BENIGN_PREFIX_LINKS.get(path)
    if expected is None or not os.path.islink(path):
        return False
    try:
        link = os.readlink(path)
    except OSError:
        return False
    return link == expected or link == "/" + expected


def _names(value: Any, *, what: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        raise DeliveryError(f"{what} must be a list of names")
    if isinstance(value, (set, frozenset)):
        # Set order is not stable across processes; sort so the plan is.
        try:
            items = sorted(
                value,
                key=lambda item: item if isinstance(item, str) else "",
            )
        except TypeError as exc:
            raise DeliveryError(f"{what} must be a list of names") from exc
    else:
        try:
            items = list(value)
        except TypeError as exc:
            raise DeliveryError(f"{what} must be a list of names") from exc
    return items


def _server_names(preset: Any) -> list[str]:
    if _has(preset, "mcp"):
        return _names(_field(preset, "mcp"), what="mcp")
    documents = _field(preset, "mcp_documents")
    if isinstance(documents, Mapping):
        return sorted((key for key in documents.keys()), key=lambda item: str(item))
    return []


def _validate_names(names: list[Any], *, what: str) -> None:
    for name in names:
        _validate_name(name, what=what)


def _validate_name(name: Any, *, what: str) -> None:
    if not isinstance(name, str) or name == "" or name in {".", ".."}:
        raise DeliveryError(f"invalid {what} name")
    if _UNSAFE_NAME.search(name) or posixpath.isabs(name):
        raise DeliveryError(f"invalid {what} name {name!r}")
    if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
        raise DeliveryError(f"invalid {what} name {name!r}")


def _skill_materialization(link_mode: Any) -> tuple[str, str]:
    if link_mode is None or link_mode == "":
        return KIND_LINK, "symlink"
    if not isinstance(link_mode, str):
        raise DeliveryError("link_mode must be 'copy' or 'symlink'")
    text = link_mode.strip().lower()
    if text in {"symlink", "link"}:
        return KIND_LINK, "symlink"
    if text == "copy":
        return KIND_COPY, "copy"
    raise DeliveryError("link_mode must be 'copy' or 'symlink'")


def _skill_dir(agent_home_rel: Any) -> tuple[str, ...]:
    if agent_home_rel is None or agent_home_rel == "":
        return _SKILL_DIR
    if isinstance(agent_home_rel, os.PathLike):
        raw = os.fspath(agent_home_rel)
    elif isinstance(agent_home_rel, str):
        raw = agent_home_rel
    else:
        raise DeliveryError("agent_home_rel must be a relative path")
    if (
        not isinstance(raw, str)
        or raw == ""
        or posixpath.isabs(raw)
        or "\\" in raw
        or _has_control(raw)
        or ".." in PurePosixPath(raw).parts
        or ".." in raw
    ):
        raise DeliveryError("agent_home_rel must stay inside the isolated home")
    parts = tuple(part for part in PurePosixPath(raw).parts if part not in {"", "."})
    escaped = any(part in {"..", ""} or "/" in part or "\\" in part for part in parts)
    if not parts or escaped:
        raise DeliveryError("agent_home_rel must stay inside the isolated home")
    return parts


def _validate_sources(
    sources: Any,
    skills: list[str],
    roots: list[str] | None,
    home: str,
) -> dict[str, str]:
    if sources is None:
        return {}
    if not isinstance(sources, Mapping):
        raise DeliveryError("skill_sources must be a mapping of name to path")
    for key in sources:
        _validate_name(key, what="skill")
    checked: dict[str, str] = {}
    for key, value in sources.items():
        checked[key] = _check_source(_fspath(value), roots, home)
    missing = [name for name in skills if name not in checked]
    if missing:
        raise DeliveryError(f"no skill source for {missing[0]!r}")
    return checked


def _fspath(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, os.PathLike):
        converted = os.fspath(value)
        if isinstance(converted, str):
            return converted
    raise DeliveryError("skill source must be an absolute path")


def _roots(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, os.PathLike)):
        raise DeliveryError("allowed_roots must be a list of absolute paths")
    try:
        items = list(value)
    except TypeError as exc:
        raise DeliveryError("allowed_roots must be a list of absolute paths") from exc
    roots: list[str] = []
    for item in items:
        if isinstance(item, str):
            raw = item
        elif isinstance(item, os.PathLike):
            raw = os.fspath(item)
        else:
            raw = None
        if not isinstance(raw, str) or not posixpath.isabs(raw) or _has_control(raw):
            raise DeliveryError("allowed_roots entries must be absolute paths")
        roots.append(posixpath.normpath(raw))
    return roots


def _check_source(raw: str, roots: list[str] | None, home: str) -> str:
    if raw == "" or _has_control(raw) or "\\" in raw:
        raise DeliveryError("skill source path is not allowed")
    if not posixpath.isabs(raw):
        raise DeliveryError("skill source must be an absolute path")
    has_dotdot = ".." in PurePosixPath(raw).parts
    norm = posixpath.normpath(raw)
    if roots is not None:
        if not _within_any(norm, roots):
            raise DeliveryError("skill source leaves allowed_roots")
    elif has_dotdot:
        raise DeliveryError(
            "skill source contains '..' and no allowed_roots were declared"
        )
    stat_root = posixpath.dirname(home)
    _reject_source_symlink(norm, roots, stat_root)
    return norm


def _reject_source_symlink(path: str, roots: list[str] | None, stat_root: str) -> None:
    """Reject a source symlink that leaves ``roots`` without following it out.

    ``os.path.realpath`` would stat the link target. Targets outside the
    isolated home's parent are judged from the link text alone.
    """
    if not _within(path, stat_root):
        return
    _walk_source(path, roots, stat_root, seen=set())


def _walk_source(
    path: str,
    roots: list[str] | None,
    stat_root: str,
    seen: set[str],
) -> None:
    if path in seen:
        raise DeliveryError("skill source symlink loop")
    parts = PurePosixPath(path).parts
    if not parts or parts[0] != "/":
        return
    # Ancestors of the tmp parent are not ours to stat. Start checking only
    # once the walked path is inside that parent.
    current = "/"
    for index, part in enumerate(parts[1:], start=1):
        current = posixpath.join(current, part)
        if not _within(current, stat_root):
            continue
        try:
            is_link = os.path.islink(current)
        except OSError:
            return
        if not is_link:
            continue
        try:
            link = os.readlink(current)
        except OSError as exc:
            raise DeliveryError("skill source symlink could not be read") from exc
        if _has_control(link) or "\\" in link:
            raise DeliveryError("skill source symlink target is not allowed")
        if posixpath.isabs(link):
            target = posixpath.normpath(link)
        else:
            parent = posixpath.dirname(current)
            target = posixpath.normpath(posixpath.join(parent, link))
        rest = parts[index + 1 :]
        followed = posixpath.normpath(posixpath.join(target, *rest)) if rest else target
        allowed = roots if roots is not None else [stat_root]
        if not _within_any(target, allowed) or not _within_any(followed, allowed):
            raise DeliveryError("skill source symlink leaves allowed_roots")
        # Follow the next hop even when this link is the whole source.
        # A chain l1 -> l2 -> outside must fail on the hop that leaves
        # the roots, not only on a link with a leftover path suffix.
        if _within(followed, stat_root):
            if len(seen) >= 8:
                raise DeliveryError("skill source symlink chain is too long")
            _walk_source(followed, roots, stat_root, seen | {path})
        return


def _validate_documents(documents: Any) -> dict[str, Mapping[str, Any]]:
    if documents is None:
        return {}
    if not isinstance(documents, Mapping):
        raise DeliveryError("mcp_documents must be a mapping")
    checked: dict[str, Mapping[str, Any]] = {}
    for name, body in documents.items():
        _validate_name(name, what="server")
        if not isinstance(body, Mapping):
            raise DeliveryError(f"mcp document for {name!r} must be a mapping")
        _reject_secrets(body, server=name)
        checked[name] = body
    return checked


def _reject_secrets(node: Any, *, server: str, key: str | None = None) -> None:
    if isinstance(node, Mapping):
        for child_key, value in node.items():
            if not isinstance(child_key, str):
                raise DeliveryError(f"mcp document for {server!r} has a non-string key")
            leaked = (
                _SECRET_KEY.search(child_key) is not None
                and isinstance(value, str)
                and not _safe_ref(value)
            )
            if leaked:
                raise DeliveryError(
                    f"mcp config {server!r} key {child_key!r} must be a "
                    "${VAR} or keychain:service/account reference"
                )
            _reject_secrets(value, server=server, key=child_key)
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            if (
                key is not None
                and _SECRET_KEY.search(key)
                and isinstance(item, str)
                and not _safe_ref(item)
            ):
                raise DeliveryError(
                    f"mcp config {server!r} key {key!r} must be a "
                    "${VAR} or keychain:service/account reference"
                )
            _reject_secrets(item, server=server, key=key)


def _safe_ref(value: str) -> bool:
    if _ENV_REF.fullmatch(value) is not None:
        return True
    return _KEYCHAIN_REF.fullmatch(value) is not None


def _dest(home: str, *parts: str) -> str:
    stat_root = posixpath.dirname(home)
    spelled = home
    for index, part in enumerate(parts):
        if (
            not isinstance(part, str)
            or part in {"", ".", ".."}
            or "/" in part
            or "\\" in part
            or _has_control(part)
            or ".." in part
        ):
            raise DeliveryError("dest escapes isolated home")
        spelled = posixpath.join(spelled, part)
        _refuse_dest_symlink(spelled, home, stat_root, parts[index + 1 :])
    final = posixpath.normpath(spelled)
    if final == home or not _within(final, home):
        raise DeliveryError("dest escapes isolated home")
    return final


def _refuse_dest_symlink(
    path: str,
    home: str,
    stat_root: str,
    rest: tuple[str, ...],
) -> None:
    """Follow every hop, including a link whose target is itself a link.

    A directory that points at another name inside the home is not safe until
    that name is followed too. A later hop that leaves and even one that
    comes back is still an escape: the intermediate path was outside.
    """
    if not _within(path, stat_root):
        if not _within(path, home):
            raise DeliveryError("dest symlink escapes isolated home")
        return
    current = path
    pending = rest
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for _ in range(32):
        key = (current, pending)
        if key in seen:
            raise DeliveryError("dest symlink escapes isolated home")
        seen.add(key)
        if not _within(current, stat_root):
            # An absolute target is spelled from ``/``. Do not lstat the
            # ancestors above the tmp parent; a sibling still fails below.
            if not _is_ancestor(current, home) or not pending:
                raise DeliveryError("dest symlink escapes isolated home")
            part = pending[0]
            if part in {"", ".", ".."} or ".." in part:
                raise DeliveryError("dest symlink escapes isolated home")
            current = posixpath.join(current, part)
            pending = pending[1:]
            continue
        if not _within(current, home) and not _is_ancestor(current, home):
            raise DeliveryError("dest symlink escapes isolated home")
        try:
            is_link = os.path.islink(current)
        except OSError as exc:
            raise DeliveryError("dest symlink could not be read") from exc
        if is_link:
            try:
                link = os.readlink(current)
            except OSError as exc:
                raise DeliveryError("dest symlink could not be read") from exc
            base, parts = _link_spell(current, link)
            # Walk the link text one component at a time. normpath would
            # delete ``via/..`` before the intermediate symlink is seen.
            current = base
            pending = parts + pending
            continue
        if pending:
            part = pending[0]
            if part in {"", ".", ".."} or ".." in part:
                raise DeliveryError("dest symlink escapes isolated home")
            current = posixpath.join(current, part)
            if not _within(current, home) and not _is_ancestor(current, home):
                raise DeliveryError("dest symlink escapes isolated home")
            pending = pending[1:]
            continue
        if not _within(current, home):
            raise DeliveryError("dest symlink escapes isolated home")
        return
    raise DeliveryError("dest symlink escapes isolated home")


def _link_spell(current: str, link: str) -> tuple[str, tuple[str, ...]]:
    """Split a symlink target without collapsing ``..`` or intermediate hops."""
    if _has_control(link) or "\\" in link:
        raise DeliveryError("dest symlink escapes isolated home")
    parts: list[str] = []
    for piece in link.split("/"):
        if piece in {"", "."}:
            continue
        if piece == ".." or ".." in piece:
            raise DeliveryError("dest symlink escapes isolated home")
        parts.append(piece)
    if not parts:
        raise DeliveryError("dest symlink escapes isolated home")
    base = "/" if link.startswith("/") else posixpath.dirname(current)
    return base, tuple(parts)


def _is_ancestor(path: str, root: str) -> bool:
    """True when ``path`` is a strict lexical ancestor of ``root``.

    An absolute link into the home is spelled through ``/`` and the tmp
    parent. Those prefixes are not the destination. A sibling of the home
    is not an ancestor.
    """
    path_n = posixpath.normpath(path)
    root_n = posixpath.normpath(root)
    if path_n == root_n:
        return False
    return root_n.startswith(path_n.rstrip("/") + "/")


def apply_delivery(actions: list[Action], isolated_home: Any) -> list[str]:
    """Recheck every destination, then write. A symlink planted after planning is refused.

    The check runs once for the whole plan and again immediately before each
    write, so a link created between those two moments cannot be followed.
    """
    home = _require_home(isolated_home)
    for action in actions:
        _recheck_dest(action.dest, home)
    written: list[str] = []
    for action in actions:
        _recheck_dest(action.dest, home)
        _write_action(action)
        written.append(action.dest)
    return written


def _recheck_dest(dest: str, home: str) -> None:
    if not isinstance(dest, str) or not dest:
        raise DeliveryError("dest escapes isolated home")
    final = posixpath.normpath(dest)
    if not _within(final, home):
        raise DeliveryError("dest escapes isolated home")
    # Walk every existing ancestor. A missing tail is not a link yet.
    cursor = home
    rest = posixpath.relpath(final, home)
    parts = tuple(part for part in rest.split("/") if part not in {"", "."})
    for index, part in enumerate(parts):
        cursor = posixpath.join(cursor, part)
        _refuse_dest_symlink(cursor, home, posixpath.dirname(home), parts[index + 1 :])


def _write_action(action: Action) -> None:
    dest = Path(action.dest)
    if action.kind == KIND_MCP:
        _write_mcp_file(dest, action.source.encode("utf-8"))
        return
    if action.kind == KIND_LINK:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or dest.is_symlink():
            return
        os.symlink(action.source, dest)
        return
    if action.kind == KIND_COPY:
        raise DeliveryError("copy delivery is refused until the source is rechecked")
    raise DeliveryError("dest escapes isolated home")


def _write_mcp_file(dest: Path, payload: bytes) -> None:
    """Write via an exclusive temp file in the validated parent, then rename.

    A predictable ``<dest>.tmp`` is not used: a symlink planted under that
    name would be followed by ``write_bytes`` and ``chmod``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent
        )
        tmp_path = Path(tmp_name)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise DeliveryError("could not write mcp document")
            view = view[written:]
        os.fchmod(fd, 0o600)
        os.close(fd)
        fd = -1
        os.replace(tmp_path, dest)
        tmp_path = None
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()


def _render_servers(servers: list[str], docs: Mapping[str, Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for name in servers:
        body = docs.get(name, {})
        if lines:
            lines.append("")
        _emit_table(lines, ("servers", name), body)
    return "\n".join(lines) + ("\n" if lines else "")


def _emit_table(
    lines: list[str],
    path: tuple[str, ...],
    body: Mapping[str, Any],
) -> None:
    scalars: list[tuple[str, Any]] = []
    tables: list[tuple[str, Mapping[str, Any]]] = []
    for key in sorted(body.keys(), key=str):
        if not isinstance(key, str):
            raise DeliveryError("mcp document has a non-string key")
        value = body[key]
        if isinstance(value, Mapping):
            tables.append((key, value))
        elif value is None:
            raise DeliveryError("mcp document has an empty value")
        else:
            scalars.append((key, value))
    header = ".".join(_toml_string(part) for part in path)
    lines.append(f"[{header}]")
    for key, value in scalars:
        lines.append(f"{_toml_string(key)} = {_toml_value(value)}")
    for key, value in tables:
        if lines and lines[-1] != "":
            lines.append("")
        _emit_table(lines, path + (key,), value)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DeliveryError("mcp document has a non-finite number")
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise DeliveryError("mcp document has an unsupported value")


def _toml_string(value: str) -> str:
    escaped: list[str] = []
    for char in value:
        if char == "\\":
            escaped.append("\\\\")
        elif char == '"':
            escaped.append('\\"')
        elif char == "\n":
            escaped.append("\\n")
        elif char == "\r":
            escaped.append("\\r")
        elif char == "\t":
            escaped.append("\\t")
        elif ord(char) < 0x20:
            escaped.append(f"\\u{ord(char):04x}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _within(path: str, root: str) -> bool:
    path_n = posixpath.normpath(path)
    root_n = posixpath.normpath(root)
    if path_n == root_n:
        return True
    return path_n.startswith(root_n.rstrip("/") + "/")


def _within_any(path: str, roots: list[str]) -> bool:
    return any(_within(path, root) for root in roots)


def _has_control(value: str) -> bool:
    return any(ord(char) < 0x20 for char in value)
