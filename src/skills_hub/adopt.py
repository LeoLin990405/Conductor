"""Adopt agent-local skills into the central repo.

``plan_adopt`` only reads. ``apply_adopt`` snapshots the agent directories
with :func:`src.skills_hub.snapshots.create_snapshot` before it writes, then
replaces each real skill directory with a link (or a copy) at the single
central tree for that content hash.

Identical trees share one central directory. The same skill name with two
hashes keeps both: the first keeps the original name and the other is stored
as ``name--`` plus the first 8 hex chars of its hash. Those suffixed
directories are not manifest keys — :func:`require_valid_name` rejects ``--``
— so the manifest records them on the original name under ``variants``.
Nothing is deleted.

A symlink is refused, before it is read, when any hop lands outside the
agent skill directory and outside the repo.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import (
    SKILL_MD,
    SkillsError,
    load_manifest,
    require_valid_name,
    write_manifest,
)
from .snapshots import create_snapshot, rollback_snapshot

_TRAVERSAL_TOKENS = ("/", "\\", "..", "\n", "\r", "\0")
_MAX_LINK_HOPS = 16


@dataclass(frozen=True)
class AdoptItem:
    """One agent skill directory and the central name it will point at."""

    agent: str
    name: str
    central_name: str
    content_hash: str
    conflict: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "name": self.name,
            "central_name": self.central_name,
            "content_hash": self.content_hash,
            "conflict": self.conflict,
        }


@dataclass(frozen=True)
class AdoptPlan:
    """Read-only description of an adopt. Applying it is a separate call."""

    items: tuple[AdoptItem, ...]
    conflicts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "conflicts": list(self.conflicts),
        }


def _home(home: Path) -> Path:
    """Caller-supplied home. Relative paths anchor here, never ``Path.home()``."""
    if home is None:
        raise SkillsError("home is required; refusing to fall back to Path.home()")
    return Path(home)


def _abspath(home: Path, path: Path | str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = _home(home) / candidate
    return Path(os.path.abspath(candidate))


def _reject_traversal(name: str) -> None:
    """Refuse a path component that could leave the skill directory.

    Checked on the raw directory entry, before the name is joined onto
    anything, so ``../`` never becomes a read outside the agent dir.
    """
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or any(token in name for token in _TRAVERSAL_TOKENS)
    ):
        raise SkillsError(f"refusing skill name {name!r}")


def require_skill_name(name: str) -> str:
    """Validate a skill name for adopt: no traversal, then the hub charset."""
    _reject_traversal(name)
    return require_valid_name(name)


def _within(child: Path, root: Path) -> bool:
    """Lexical containment. Does not resolve symlinks."""
    try:
        Path(os.path.normpath(child)).relative_to(Path(os.path.normpath(root)))
    except ValueError:
        return False
    return True


def _within_any(child: Path, roots: Sequence[Path]) -> bool:
    return any(_within(child, root) for root in roots)


def _is_ancestor(parent: Path, child: Path) -> bool:
    """True when ``parent`` lexically contains ``child`` and is not ``child``."""
    parent_s = os.path.normpath(parent)
    child_s = os.path.normpath(child)
    if parent_s == child_s:
        return False
    try:
        Path(child_s).relative_to(parent_s)
    except ValueError:
        return False
    return True


def _stays_inside(candidate: Path, allowed: Sequence[Path]) -> bool:
    if _within_any(candidate, allowed):
        return True
    return any(_is_ancestor(candidate, root) for root in allowed)


def _resolve_components(path: Path, *, allowed: Sequence[Path] | None = None) -> Path:
    """Walk ``path`` one component at a time, following each symlink hop.

    ``normpath`` is not applied to a multi-component link target, so a symlink
    in the middle stays visible. When ``allowed`` is set, a hop that is neither
    inside an allowed root nor an ancestor of one is refused. ``/var`` on macOS
    is a symlink to ``/private/var``; both the path and the allowed roots are
    resolved the same way before the final containment check.
    """
    absolute = Path(os.path.abspath(path))
    current = Path(os.sep)
    pending = [part for part in absolute.parts if part not in {os.sep, ""}]
    seen: set[tuple[str, tuple[str, ...]]] = set()
    hops = 0
    while True:
        token = (str(current), tuple(pending))
        if token in seen:
            raise SkillsError(f"refusing symlink loop at {path}")
        seen.add(token)
        try:
            is_link = current.is_symlink()
        except OSError as exc:
            raise SkillsError(f"refusing unreadable symlink {path}") from exc
        if is_link:
            hops += 1
            if hops > _MAX_LINK_HOPS:
                raise SkillsError(
                    f"refusing symlink chain longer than {_MAX_LINK_HOPS} at {path}"
                )
            try:
                raw = os.readlink(current)
            except OSError as exc:
                raise SkillsError(f"refusing unreadable symlink {path}") from exc
            if os.path.isabs(raw):
                current = Path(os.sep)
                extra = [part for part in Path(raw).parts if part not in {os.sep, ""}]
            else:
                current = current.parent
                extra = [part for part in Path(raw).parts if part not in {"", "."}]
            pending = extra + pending
            if (
                allowed is not None
                and current != Path(os.sep)
                and not _stays_inside(current, allowed)
            ):
                raise SkillsError(
                    "refusing to follow symlink that resolves outside the agent "
                    f"skill dir and the repo: {path} -> {raw}"
                )
            continue
        if not pending:
            return current
        part = pending.pop(0)
        if part in {"", "."}:
            continue
        if part == "..":
            current = current.parent
            continue
        nxt = current / part
        if allowed is not None and not _stays_inside(nxt, allowed):
            try:
                linked = nxt.is_symlink()
            except OSError as exc:
                raise SkillsError(f"refusing unreadable symlink {path}") from exc
            # A directory symlink such as /var -> /private/var is not lexically
            # inside the resolved root. Follow it, then check the target.
            if not linked:
                raise SkillsError(
                    "refusing to follow symlink that resolves outside the agent "
                    f"skill dir and the repo: {path} -> {part}"
                )
        current = nxt


def _assert_symlink_stays(path: Path, allowed: Sequence[Path]) -> None:
    """Refuse a symlink whose real walk leaves every allowed root.

    Allowed roots are resolved first, so a directory symlink such as ``/var``
    does not look like an escape. A later hop through an intermediate symlink
    to a directory outside those roots is refused before any target is read.
    """
    allowed_real = tuple(_resolve_components(root) for root in allowed)
    resolved = _resolve_components(path, allowed=allowed_real)
    if not _within_any(resolved, allowed_real):
        raise SkillsError(
            "refusing to follow symlink that resolves outside the agent "
            f"skill dir and the repo: {path}"
        )


def _ignore_git(_directory: str, names: list[str]) -> list[str]:
    return [name for name in names if name == ".git"]


def content_hash(root: Path, *, allowed: Sequence[Path]) -> str:
    """sha256 of a skill tree: sorted relative paths, everything except ``.git``.

    Symlinks are hashed as the link text and are not followed. A link whose
    target leaves ``allowed`` raises :class:`SkillsError` instead of being read.
    """
    if root.is_symlink():
        _assert_symlink_stays(root, allowed)
    base = Path(os.path.abspath(root))
    digest = hashlib.sha256()
    files: list[tuple[str, bytes]] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        for name in list(dirnames):
            candidate = Path(dirpath) / name
            if not candidate.is_symlink():
                continue
            _assert_symlink_stays(candidate, allowed)
            rel = candidate.relative_to(base).as_posix()
            files.append(
                (
                    rel,
                    b"link\0"
                    + os.readlink(candidate).encode("utf-8", "surrogateescape"),
                )
            )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(base).as_posix()
            if ".git" in Path(rel).parts:
                continue
            if path.is_symlink():
                _assert_symlink_stays(path, allowed)
                payload = b"link\0" + os.readlink(path).encode(
                    "utf-8", "surrogateescape"
                )
            elif path.is_file():
                payload = b"file\0" + path.read_bytes()
            else:
                continue
            files.append((rel, payload))
    for rel, payload in sorted(files):
        digest.update(rel.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class _Found:
    agent: str
    agent_dir: Path
    name: str
    path: Path
    digest: str
    index: int


def _is_skill_dir(path: Path, allowed: Sequence[Path]) -> bool:
    """True when ``path`` is a skill directory that stays inside ``allowed``."""
    if path.is_symlink():
        _assert_symlink_stays(path, allowed)
        if not path.is_dir():
            return False
    elif not path.is_dir():
        return False
    skill_md = path / SKILL_MD
    if skill_md.is_symlink():
        _assert_symlink_stays(skill_md, allowed)
    return skill_md.is_file()


def _scan_agent(
    agent: str,
    directory: Path,
    repo: Path,
    found: list[_Found],
    start: int,
) -> int:
    if not directory.exists():
        return start
    if not directory.is_dir():
        raise SkillsError(f"agent skill dir {directory} is not a directory")
    allowed = (directory, repo)
    try:
        children = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        raise SkillsError(f"could not list {directory}: {exc}") from exc
    index = start
    for child in children:
        if not (child.is_symlink() or child.is_dir()):
            continue
        # Traversal components are refused even when the entry is not a skill,
        # because joining them would leave the agent directory.
        _reject_traversal(child.name)
        if not _is_skill_dir(child, allowed):
            continue
        require_skill_name(child.name)
        found.append(
            _Found(
                agent=agent,
                agent_dir=directory,
                name=child.name,
                path=child,
                digest=content_hash(child, allowed=allowed),
                index=index,
            )
        )
        index += 1
    return index


def _existing_central(repo: Path) -> dict[str, str]:
    """Map central directory names to content hashes. Does not create ``repo``."""
    if not repo.exists():
        return {}
    if not repo.is_dir():
        raise SkillsError(f"skills repo {repo} is not a directory")
    found: dict[str, str] = {}
    try:
        children = sorted(repo.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        raise SkillsError(f"could not list skills repo {repo}: {exc}") from exc
    for child in children:
        if child.name.startswith("."):
            continue
        if child.is_symlink() or child.is_dir():
            _reject_traversal(child.name)
        if not _is_skill_dir(child, (repo,)):
            continue
        found[child.name] = content_hash(child, allowed=(repo,))
    return found


def _conflict_dirname(base: str, digest: str, used: set[str]) -> str:
    """``base--<hash prefix>``, lengthening the prefix only to avoid a clash."""
    for length in (8, 16, 32, 64):
        candidate = f"{base}--{digest[:length]}"
        if candidate not in used:
            return candidate
    raise SkillsError(f"could not allocate a conflict name for {base!r}")


def _assign(found: list[_Found], existing: dict[str, str]) -> AdoptPlan:
    by_hash: dict[str, list[_Found]] = {}
    for item in found:
        by_hash.setdefault(item.digest, []).append(item)
    digests_by_name: dict[str, set[str]] = {}
    for item in found:
        digests_by_name.setdefault(item.name, set()).add(item.digest)

    conflicts: set[str] = set()
    for name, digests in digests_by_name.items():
        if len(digests) > 1:
            conflicts.add(name)
        elif name in existing and existing[name] not in digests:
            # The repo already holds a different body under this name. Keep it.
            conflicts.add(name)

    used = set(existing)
    central_for: dict[str, str] = {}
    ordered = sorted(
        by_hash.items(), key=lambda pair: min(item.index for item in pair[1])
    )
    for digest, group in ordered:
        reused = next((name for name, have in existing.items() if have == digest), None)
        if reused is not None:
            central_for[digest] = reused
            continue
        preferred: list[str] = []
        for item in sorted(group, key=lambda item: item.index):
            if item.name not in preferred:
                preferred.append(item.name)
        chosen: str | None = None
        for name in preferred:
            if name in used and existing.get(name) != digest:
                continue
            chosen = name
            break
        if chosen is None:
            chosen = _conflict_dirname(preferred[0], digest, used)
        used.add(chosen)
        central_for[digest] = chosen

    items = tuple(
        AdoptItem(
            agent=item.agent,
            name=item.name,
            central_name=central_for[item.digest],
            content_hash=item.digest,
            conflict=item.name in conflicts,
        )
        for item in found
    )
    return AdoptPlan(items=items, conflicts=tuple(sorted(conflicts)))


def plan_adopt(
    home: Path,
    repo: Path,
    agent_dirs: Sequence[tuple[str, Path]],
) -> AdoptPlan:
    """Describe adopting every agent skill into ``repo``. Does not write."""
    repo_path = _abspath(home, repo)
    found: list[_Found] = []
    index = 0
    for agent, directory in agent_dirs:
        index = _scan_agent(agent, _abspath(home, directory), repo_path, found, index)
    return _assign(found, _existing_central(repo_path))


def _already_placed(target: Path, source: Path, link_mode: str) -> bool:
    if link_mode == "symlink":
        if not target.is_symlink():
            return False
        try:
            return Path(os.path.abspath(os.path.realpath(target))) == Path(
                os.path.abspath(os.path.realpath(source))
            )
        except OSError:
            return False
    if target.is_symlink() or not target.is_dir():
        return False
    try:
        return content_hash(target, allowed=(target.parent,)) == content_hash(
            source, allowed=(source.parent, source)
        )
    except SkillsError:
        return False


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _backup_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.adoptbak-{os.getpid()}")


def _place(source: Path, target: Path, link_mode: str) -> Path | None:
    """Point ``target`` at ``source``. Return a backup path to drop on success.

    On failure the previous entry is moved back, so a raised ``IOError`` does
    not leave the agent directory half-replaced.
    """
    if _already_placed(target, source, link_mode):
        return None
    backup: Path | None = None
    if target.exists() or target.is_symlink():
        backup = _backup_path(target)
        _remove(backup)
        os.replace(target, backup)
    try:
        if link_mode == "symlink":
            os.symlink(
                os.path.relpath(source, target.parent), target, target_is_directory=True
            )
        else:
            shutil.copytree(source, target, symlinks=True, ignore=_ignore_git)
    except Exception:
        _remove(target)
        if backup is not None and backup.exists():
            os.replace(backup, target)
        raise
    return backup


def _materialize(repo: Path, item: AdoptItem, source: Path) -> None:
    target = repo / item.central_name
    if source.is_symlink():
        _assert_symlink_stays(source, (source.parent, repo))
    if target.exists() or target.is_symlink():
        if (
            _is_skill_dir(target, (repo,))
            and content_hash(target, allowed=(repo,)) == item.content_hash
        ):
            return
        raise SkillsError(
            f"refusing to overwrite central skill {item.central_name!r} with different content"
        )
    src = Path(os.path.abspath(source))
    repo.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, target, symlinks=True, ignore=_ignore_git)


def _write_adopt_manifest(repo: Path, items: Sequence[AdoptItem]) -> None:
    manifest = load_manifest(repo)
    grouped: dict[str, list[AdoptItem]] = {}
    for item in items:
        grouped.setdefault(item.name, []).append(item)
    for name, group in grouped.items():
        require_skill_name(name)
        record = dict(manifest.get(name) or {})
        agents = [
            agent for agent in record.get("agents") or [] if isinstance(agent, str)
        ]
        variants: list[dict[str, Any]] = []
        positions = {}
        for item in group:
            if item.agent not in agents:
                agents.append(item.agent)
            slot = positions.get(item.central_name)
            if slot is None:
                positions[item.central_name] = len(variants)
                variants.append(
                    {
                        "central_name": item.central_name,
                        "hash": item.content_hash,
                        "agents": [item.agent],
                    }
                )
            elif item.agent not in variants[slot]["agents"]:
                variants[slot]["agents"].append(item.agent)
        primary = next((v for v in variants if v["central_name"] == name), variants[0])
        record["agents"] = agents
        record["variants"] = variants
        record["conflict"] = any(item.conflict for item in group)
        record["central_name"] = primary["central_name"]
        record["content_hash"] = primary["hash"]
        manifest[name] = record
    write_manifest(repo, manifest)


def _sources_by_agent(
    home: Path, agent_dirs: Sequence[tuple[str, Path]]
) -> dict[tuple[str, str], Path]:
    """Re-read agent entries so apply uses the same paths the plan hashed."""
    found: dict[tuple[str, str], Path] = {}
    for agent, directory in agent_dirs:
        directory = _abspath(home, directory)
        if not directory.is_dir():
            continue
        for child in directory.iterdir():
            if child.is_symlink() or child.is_dir():
                found[(agent, child.name)] = child
    return found


def apply_adopt(
    home: Path,
    repo: Path,
    agent_dirs: Sequence[tuple[str, Path]],
    *,
    link_mode: str = "symlink",
) -> dict[str, Any]:
    """Copy new central skills, then replace agent copies with links or copies.

    The snapshot is taken before the first write. A failure while placing an
    agent entry moves that entry back from its sibling backup and then calls
    :func:`rollback_snapshot` for anything the snapshot can still repair.
    Snapshot rollback does not store skill bodies and will not overwrite an
    entry that is still on disk; the backup rename is what puts the original
    directory back.
    """
    if link_mode not in ("symlink", "copy"):
        raise SkillsError(f"unknown link mode {link_mode!r}")
    plan = plan_adopt(home, repo, agent_dirs)
    if not plan.items:
        return {"snapshot": None, "conflicts": [], "items": []}
    repo_path = _abspath(home, repo)
    agents = [(agent, _abspath(home, directory)) for agent, directory in agent_dirs]
    # Snapshot before any adopt write, including the central copy.
    stamp = create_snapshot(repo_path, agents)
    backups: list[tuple[Path, Path | None]] = []
    try:
        sources = _sources_by_agent(home, agents)
        seen_central: set[str] = set()
        for item in plan.items:
            if item.central_name in seen_central:
                continue
            source = sources.get((item.agent, item.name))
            if source is None:
                raise SkillsError(f"skill {item.name!r} disappeared before adopt")
            _materialize(repo_path, item, source)
            seen_central.add(item.central_name)
        for item in plan.items:
            source = repo_path / item.central_name
            target = sources[(item.agent, item.name)]
            backups.append((target, _place(source, target, link_mode)))
        _write_adopt_manifest(repo_path, plan.items)
    except Exception:
        for target, backup in reversed(backups):
            if backup is None or not backup.exists():
                continue
            _remove(target)
            os.replace(backup, target)
        try:
            rollback_snapshot(
                repo_path,
                stamp,
                skills_root=repo_path,
                allowed_dirs={directory for _, directory in agents},
            )
        except SkillsError:
            # The rename above already put original entries back. Rollback
            # refusing to overwrite them is the snapshot helper's contract.
            pass
        raise
    for _target, backup in backups:
        if backup is not None and backup.exists():
            _remove(backup)
    return {
        "snapshot": stamp,
        "conflicts": list(plan.conflicts),
        "items": [item.to_dict() for item in plan.items],
    }
