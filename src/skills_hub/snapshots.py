"""Snapshots of agent skill directories, taken before every change.

A snapshot records, for each affected agent directory, exactly what
Conductor is about to touch: the names of its entries and whether each was a
symlink (and where it pointed), a real directory, or a plain file. Restoring
a snapshot puts missing entries back — including entries Conductor did not
install, because a rollback that silently dropped a user's own skills would
be worse than the change it undoes.

Snapshots are content-light on purpose: skill *bodies* (directories) are not
copied. A restored *copy-mode* entry comes back from the central repo when
the skill still exists there; a deleted repo skill restores as a small
tombstone marker, which is enough to tell "this slot was occupied" without
resurrecting stale content. Plain files are small, so their bytes are
recorded (base64, capped at ``MAX_RECORDED_FILE_BYTES``) and restored
verbatim; a file over the cap is preserved but not restorable.

Rollback never destroys an entry that is already on disk. An entry still
present belongs to the user now — wiping it to re-materialize the snapshot
would trade a reversible change for lost data — so restore only recreates
what is missing, plus the one cheap lossless repair: re-pointing a symlink
whose target drifted. That makes rolling back a snapshot of the current tree
a no-op, as it must be.

Nor does rollback delete an entry the snapshot does not record. Something
that appeared after the snapshot — a skill the user installed by hand, or one
a later install put there — is left alone and reported back as kept, because
the only honest answer to "what changed since the snapshot" is to say so, not
to undo it. Removal is ``sync --prune`` and ``uninstall``; those are the verbs
that exist to delete.

A snapshot file is a local operator artifact, not proof against a crafted
one: ``rollback_snapshot`` validates every recorded directory (absolute, and
inside ``allowed_dirs`` when the caller supplies its known agent
directories) before anything is touched, so a corrupt or planted JSON cannot
redirect the restore at an unrelated folder — a missing ``dir`` must never
fall back to the process working directory.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .core import (
    MAX_NAME_LEN,
    RESERVED_NAMES,
    SKILL_MD,
    SkillsError,
    atomic_write_json,
    load_manifest,
    valid_name,
)
from .importer import copy_skill_tree

SNAPSHOT_DIRNAME = "snapshots"
MAX_SNAPSHOTS = 50
# Loose files in a skill directory are small (notes, configs); anything over
# the cap is preserved on rollback but its bytes stay unrecorded.
MAX_RECORDED_FILE_BYTES = 64 * 1024
# A force-update snapshot of a central skill must keep the whole tree.
# Agent-directory snapshots above stay capped; this limit is only the
# refusal threshold for that lossless copy.
MAX_SKILL_TREE_BYTES = 200 * 1024 * 1024


@dataclass(frozen=True)
class EntryState:
    name: str
    kind: str  # "symlink" | "dir" | "file" | "missing"
    target: str | None = None  # symlink destination when kind == "symlink"
    content: str | None = None  # base64 bytes when kind == "file"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "target": self.target}
        if self.kind == "file":
            out["content"] = self.content
        return out


def snapshot_dir(repo: Path) -> Path:
    return repo / SNAPSHOT_DIRNAME


def list_snapshots(repo: Path) -> list[dict[str, Any]]:
    """Snapshot metadata, newest first. Body files are not read.

    Sorting is by (timestamp, suffix) with the suffix compared as an integer:
    a plain ``-``-less name is ``-0``. A plain string sort would rank
    ``...T005221-1`` *before* ``...T005221`` and answer with the oldest.
    """
    directory = snapshot_dir(repo)
    if not directory.is_dir():
        return []

    def sort_key(name: str) -> tuple[str, int]:
        stem = name[: -len(".json")]
        base, dash, suffix = stem.rpartition("-")
        if dash and suffix.isdigit():
            return base, int(suffix)
        return stem, 0

    found: list[dict[str, Any]] = []
    for child in sorted(
        directory.iterdir(), key=lambda p: sort_key(p.name), reverse=True
    ):
        if not child.is_file() or not child.name.endswith(".json"):
            continue
        stem = child.name[: -len(".json")]
        if not stem or len(stem.split("T")) != 2:
            continue
        found.append({"id": stem, "path": str(child)})
    return found


def create_snapshot(repo: Path, agents: list[tuple[str, Path]]) -> str:
    """Capture the current skill entries of ``agents`` and return the id.

    ``agents`` is ``(agent_id, directory)`` pairs; a directory that does not
    exist yet is recorded as empty so its creation is also reversible. Every
    directory must be absolute — a relative path recorded here would become
    whatever directory a later rollback happens to run in.
    """
    for agent, skill_dir in agents:
        if not Path(skill_dir).is_absolute():
            raise SkillsError(
                f"agent skill dir for {agent!r} must be absolute: {skill_dir}"
            )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    directory = snapshot_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stamp}.json"
    counter = 0
    while path.exists() and counter < 5:  # same-second collisions
        counter += 1
        path = directory / f"{stamp}-{counter}.json"
        stamp = path.stem
    body: dict[str, Any] = {"id": stamp, "created_at": datetime.now(UTC).isoformat()}
    states: dict[str, dict[str, Any]] = {}
    for agent, skill_dir in agents:
        states[agent] = {
            "dir": str(skill_dir),
            "entries": {e.name: e.to_dict() for e in _scan(skill_dir)},
        }
    body["agents"] = states
    atomic_write_json(path, body)
    _prune(directory)
    return stamp


def assert_skill_tree_restorable(skill_dir: Path) -> None:
    """Raise before a force replace when the tree cannot be snapshotted whole.

    Hidden files and directories, symlinks, permission bits, and files over
    64 KiB are part of the tree. A file that cannot be read, or a tree over
    200 MB, is refused here, before anything is replaced.
    """
    _inspect_skill_tree(skill_dir)


def snapshot_skill_content(repo: Path, name: str) -> str:
    """Record a central skill so rollback can put the whole tree back.

    Agent snapshots do not copy skill-directory bodies. A ``--force`` update
    needs a lossless copy: content, entry types, hidden entries, symlinks kept
    as links, the root directory mode, every subdirectory mode, empty
    directories, and large files. ``rollback_snapshot`` writes that tree back.
    """
    from .core import require_valid_name

    skill = require_valid_name(name)
    skill_dir = repo / skill
    if skill_dir.is_symlink() or not skill_dir.is_dir():
        raise SkillsError(f"skill {skill!r} is not a directory in the central repo")
    files = _inspect_skill_tree(skill_dir)
    root_mode = stat.S_IMODE(skill_dir.lstat().st_mode)
    directory = snapshot_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    path = directory / f"{stamp}.json"
    counter = 0
    while path.exists() and counter < 5:
        counter += 1
        stamp = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{counter}"
        path = directory / f"{stamp}.json"
    body: dict[str, Any] = {
        "id": stamp,
        "created_at": datetime.now(UTC).isoformat(),
        "kind": "skill-content",
        "skill": skill,
        "root_mode": root_mode,
        "files": files,
        "agents": {},
    }
    atomic_write_json(path, body)
    _prune(directory)
    return stamp


def _inspect_skill_tree(skill_dir: Path) -> list[dict[str, Any]]:
    """Lossless listing. ``.git`` is skipped; every other hidden name is kept.

    Size is counted from ``lstat`` and a too-large tree raises before the
    oversized file is read. Symlinks are recorded as links and are not followed.
    """
    if skill_dir.is_symlink() or not skill_dir.is_dir():
        raise SkillsError(f"skill {skill_dir.name!r} is not a directory")
    entries: list[dict[str, Any]] = []
    total = 0

    def walk(directory: Path) -> None:
        nonlocal total
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            where = _skill_rel(skill_dir, directory) or directory.name
            raise SkillsError(
                f"refusing to replace: could not read {where}: {exc.strerror or exc}"
            ) from exc
        for child in children:
            if child.name == ".git":
                continue
            rel = child.relative_to(skill_dir).as_posix()
            try:
                info = child.lstat()
            except OSError as exc:
                raise SkillsError(
                    f"refusing to replace: could not read {rel}: {exc.strerror or exc}"
                ) from exc
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                try:
                    target = os.readlink(child)
                except OSError as exc:
                    raise SkillsError(
                        f"refusing to replace: could not read {rel}: "
                        f"{exc.strerror or exc}"
                    ) from exc
                entries.append(
                    {"rel": rel, "kind": "symlink", "mode": mode, "target": target}
                )
                continue
            if stat.S_ISDIR(info.st_mode):
                entries.append({"rel": rel, "kind": "dir", "mode": mode})
                walk(child)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise SkillsError(
                    f"refusing to replace: {rel} is not a regular file, "
                    "directory, or symlink"
                )
            total += info.st_size
            if total > MAX_SKILL_TREE_BYTES:
                raise SkillsError(
                    "refusing to replace: skill tree is "
                    f"{total} bytes, over the 200 MB limit"
                )
            try:
                payload = child.read_bytes()
            except OSError as exc:
                raise SkillsError(
                    f"refusing to replace: could not read {rel}: {exc.strerror or exc}"
                ) from exc
            entries.append(
                {
                    "rel": rel,
                    "kind": "file",
                    "mode": mode,
                    "content": base64.b64encode(payload).decode("ascii"),
                }
            )

    walk(skill_dir)
    return entries


def _skill_rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def record_absent_symlink(
    repo: Path, snapshot_id: str, agent: str, name: str, target: str
) -> None:
    """Record a symlink sync is about to create when the snapshot missed it.

    The snapshot is taken before the write, so a skill that was already gone
    has no entry. Rollback of that snapshot must be able to put the agent's
    own central link back. An entry that is already recorded is left alone:
    that is the pre-change state, and a later rollback has to restore it.
    """
    if not valid_name(name) or name in RESERVED_NAMES:
        return
    if not isinstance(target, str) or not target or "\n" in target or "\x00" in target:
        return
    if target.startswith("/") or target.startswith("\\"):
        return
    body = _load_snapshot(repo, snapshot_id)
    state = body["agents"].get(agent)
    if not isinstance(state, dict):
        return
    entries = state.get("entries")
    if not isinstance(entries, dict) or name in entries:
        return
    entries[name] = {"kind": "symlink", "target": target}
    atomic_write_json(snapshot_dir(repo) / f"{snapshot_id}.json", body)


def _scan(skill_dir: Path) -> list[EntryState]:
    if not skill_dir.exists():
        return []
    if not skill_dir.is_dir():
        raise SkillsError(f"agent skill dir {skill_dir} is not a directory")
    out: list[EntryState] = []
    try:
        children = sorted(skill_dir.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        raise SkillsError(f"could not list {skill_dir}: {exc}") from exc
    for child in children:
        if child.is_symlink():
            try:
                target = os.readlink(child)
            except OSError:
                target = None
            out.append(EntryState(name=child.name, kind="symlink", target=target))
        elif child.is_dir():
            out.append(EntryState(name=child.name, kind="dir"))
        elif child.is_file():
            # Plain files must be part of the recorded state: a file deleted
            # after the snapshot is restored from these bytes, and there is no
            # other copy to restore it from.
            out.append(
                EntryState(name=child.name, kind="file", content=_record_file(child))
            )
    return out


def _record_file(path: Path) -> str | None:
    """Base64 of a small file's bytes, or None when unrecordable.

    Over-sized or unreadable files stay in the snapshot with ``content: None``
    so rollback preserves them but does not fabricate bytes it never captured.
    """
    try:
        if path.stat().st_size > MAX_RECORDED_FILE_BYTES:
            return None
        return base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None


def _prune(directory: Path) -> None:
    snapshots = sorted(
        (p for p in directory.iterdir() if p.name.endswith(".json")),
        key=lambda p: p.name,
    )
    for stale in snapshots[:-MAX_SNAPSHOTS]:
        with contextlib.suppress(OSError):
            stale.unlink()


def _load_snapshot(repo: Path, snapshot_id: str) -> dict[str, Any]:
    if (
        not snapshot_id
        or any(c in snapshot_id for c in "/\\.")
        or not snapshot_id.isascii()
    ):
        raise SkillsError("invalid snapshot id")
    path = snapshot_dir(repo) / f"{snapshot_id}.json"
    try:
        with path.open("r", encoding="utf-8") as stream:
            body = json.load(stream)
    except FileNotFoundError as exc:
        raise SkillsError(f"snapshot {snapshot_id} not found") from exc
    except json.JSONDecodeError as exc:
        raise SkillsError(f"snapshot {snapshot_id} is corrupt ({exc.msg})") from exc
    except OSError as exc:
        raise SkillsError(
            f"could not read snapshot {snapshot_id}: {exc.strerror}"
        ) from exc
    if not isinstance(body, dict) or not isinstance(body.get("agents"), dict):
        raise SkillsError(f"snapshot {snapshot_id} is malformed")
    return body


def rollback_snapshot(
    repo: Path,
    snapshot_id: str,
    *,
    skills_root: Path | None = None,
    allowed_dirs: set[Path] | None = None,
) -> dict[str, Any]:
    """Restore agent directories to the state captured in ``snapshot_id``.

    ``skills_root`` is where restored *copy-mode* content is read from (the
    central repo by default). Only the agents present in the snapshot are
    touched; everything else on the machine is left alone. When
    ``allowed_dirs`` is given, every directory the snapshot names must be in
    the set — the CLI passes its configured agent directories — so a crafted
    snapshot cannot point the restore at an unrelated folder. Every directory
    is validated before anything is touched.

    The result carries two maps, both keyed by agent id and both omitting
    agents that came out unchanged: ``restored`` lists what the rollback put
    back, ``kept`` lists the entries the snapshot never recorded (a skill the
    user added since, or one a later install placed) — reported, never
    deleted.
    """
    body = _load_snapshot(repo, snapshot_id)
    if body.get("kind") == "skill-content":
        return _rollback_skill_content(repo, snapshot_id, body)
    root = skills_root if skills_root is not None else repo
    manifest = load_manifest(repo)
    plans: list[tuple[str, Path, dict[str, Any]]] = []
    for agent, state in body["agents"].items():
        if not isinstance(state, dict):
            continue
        entries = state.get("entries", {})
        if not isinstance(entries, dict):
            continue
        skill_dir = _validated_dir(state, snapshot_id, agent)
        if allowed_dirs is not None and skill_dir not in allowed_dirs:
            raise SkillsError(
                f"snapshot {snapshot_id}: directory {skill_dir} for agent "
                f"{agent!r} is not a known agent skill directory; refusing to restore"
            )
        plans.append((agent, skill_dir, entries))
    changed: dict[str, list[str]] = {}
    kept: dict[str, list[str]] = {}
    for agent, skill_dir, entries in plans:
        before = _names(skill_dir)
        diff = _restore_agent(skill_dir, entries, root, agent=agent, manifest=manifest)
        after = _names(skill_dir)
        # Report the entries the restore actually changed, not merely
        # rewrote in place: a no-op rollback should say so.
        changed[agent] = [n for n in diff if before.get(n) != after.get(n)]
        if not changed[agent]:
            del changed[agent]
        unknown = sorted(set(after) - set(entries))
        if unknown:
            kept[agent] = unknown
    return {"snapshot": snapshot_id, "restored": changed, "kept": kept}


def _rollback_skill_content(
    repo: Path, snapshot_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Write a force-update snapshot's tree back into the central skill.

    Content and entry types are restored first. Directories stay writable
    during that pass. Directory modes, including the skill root, are applied
    last, deepest first. The directory is ``repo / skill``, taken from the
    validated skill name, not from a path stored in the snapshot. A path the
    snapshot still records as a directory keeps children the snapshot does
    not name. Replacing a node whose type changed does not follow a symlink.
    """
    from .core import require_valid_name

    skill = body.get("skill")
    if not isinstance(skill, str):
        raise SkillsError(f"snapshot {snapshot_id} records no skill")
    skill = require_valid_name(skill)
    skill_dir = repo / skill
    if skill_dir.is_symlink():
        raise SkillsError(f"refusing to roll back skill {skill!r} through a symlink")
    files = body.get("files")
    if not isinstance(files, list):
        raise SkillsError(f"snapshot {snapshot_id} has no skill files")
    original_root: int | None = None
    if skill_dir.is_dir() and not skill_dir.is_symlink():
        original_root = stat.S_IMODE(skill_dir.lstat().st_mode)
        if original_root & 0o200 == 0:
            _fchmod_path(skill_dir, original_root | 0o700)
    restored: list[str] = []
    refused: list[str] = []
    directories: list[dict[str, Any]] = []
    ordered = sorted(
        (item for item in files if isinstance(item, dict)),
        key=lambda item: (_entry_rank(item), str(item.get("rel") or "")),
    )
    for item in ordered:
        rel = item.get("rel")
        try:
            _restore_skill_entry(skill_dir, item)
        except SkillsError:
            if isinstance(rel, str):
                refused.append(rel)
            continue
        if isinstance(rel, str):
            restored.append(rel)
        if _kind_of(item) == "dir":
            directories.append(item)
    for item in sorted(
        directories,
        key=lambda entry: (
            str(entry.get("rel") or "").count("/"),
            str(entry.get("rel") or ""),
        ),
        reverse=True,
    ):
        rel = item.get("rel")
        try:
            _chmod_recorded_dir(skill_dir, item)
        except SkillsError:
            if isinstance(rel, str) and rel in restored:
                restored.remove(rel)
                refused.append(rel)
    root_mode = body.get("root_mode")
    if isinstance(root_mode, int) and skill_dir.is_dir() and not skill_dir.is_symlink():
        _fchmod_path(skill_dir, root_mode & 0o777)
    elif (
        original_root is not None
        and original_root & 0o200 == 0
        and skill_dir.is_dir()
        and not skill_dir.is_symlink()
    ):
        _fchmod_path(skill_dir, original_root)
    result: dict[str, Any] = {
        "snapshot": snapshot_id,
        "restored": {skill: restored},
        "kept": {},
    }
    if refused:
        result["refused"] = refused
    return result


def _kind_of(item: dict[str, Any]) -> str | None:
    kind = item.get("kind")
    if kind is None and isinstance(item.get("content"), str):
        kind = "file"
    if kind in {"file", "dir", "symlink"}:
        return str(kind)
    return None


def _entry_rank(item: dict[str, Any]) -> int:
    return {"dir": 0, "file": 1, "symlink": 2}.get(str(_kind_of(item)), 9)


def _rel_parts(rel: str) -> tuple[str, ...] | None:
    if not rel or rel.startswith(("/", "\\")) or "\\" in rel:
        return None
    parts = tuple(rel.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return parts


def _restore_skill_entry(skill_dir: Path, item: dict[str, Any]) -> None:
    """Write one snapshot entry without following a symlink out of ``skill_dir``.

    Existing ancestors are opened with ``O_NOFOLLOW``. A missing suffix is
    created under that directory fd, so ``link/new/file.txt`` cannot appear
    outside the skill when ``link`` is an outside symlink and ``new`` is absent.
    """
    rel = item.get("rel")
    if not isinstance(rel, str):
        raise SkillsError("refusing to restore an unnamed skill file")
    parts = _rel_parts(rel)
    if parts is None:
        raise SkillsError(f"refusing to restore {rel!r}")
    kind = _kind_of(item)
    if kind is None:
        raise SkillsError(f"refusing to restore {rel!r}")
    try:
        parent_fd = os.open(skill_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise SkillsError("refusing to roll back through a symlink") from exc
    try:
        for part in parts[:-1]:
            child_fd = _open_or_make_dir(parent_fd, part)
            os.close(parent_fd)
            parent_fd = child_fd
        name = parts[-1]
        # A symlink that points outside the skill is not replaced. One that
        # points inside is the recorded node, so it can become a directory.
        # Opening a path through the link still uses O_NOFOLLOW.
        _clear_for_kind(parent_fd, name, kind, skill_dir)
        if kind == "dir":
            child_fd = _open_or_make_dir(parent_fd, name)
            os.close(child_fd)
            return
        if kind == "symlink":
            target = item.get("target")
            if not isinstance(target, str) or not target or "\x00" in target:
                raise SkillsError(f"refusing to restore symlink {rel!r}")
            _put_symlink(parent_fd, name, target)
            return
        content = item.get("content")
        if not isinstance(content, str):
            raise SkillsError(f"refusing to restore {rel!r}")
        try:
            data = base64.b64decode(content.encode("ascii"), validate=True)
        except (ValueError, UnicodeError) as exc:
            raise SkillsError(f"refusing to restore {rel!r}") from exc
        _put_file(parent_fd, name, data, _mode_of(item, 0o644))
    finally:
        os.close(parent_fd)


def _mode_of(item: dict[str, Any], default: int) -> int:
    raw = item.get("mode")
    if isinstance(raw, int):
        return raw & 0o777
    return default


def _node_kind(parent_fd: int, name: str) -> str | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SkillsError(f"refusing to restore {name!r}") from exc
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    if stat.S_ISREG(info.st_mode):
        return "file"
    return "other"


def _clear_for_kind(parent_fd: int, name: str, kind: str, root: Path) -> None:
    """Make ``name`` ready for ``kind`` without following a symlink.

    A directory the snapshot still records as a directory is left in place,
    including children the snapshot does not name. A symlink that points
    outside ``root`` is not replaced by a directory. A symlink that points
    inside ``root`` is the node being restored, so it is unlinked first.
    A directory that exists only because the recorded type is now a file or
    a symlink is removed without following links. A read-only directory is
    given ``u+w`` for that removal.
    """
    current = _node_kind(parent_fd, name)
    if current is None or current == kind:
        return
    if current == "symlink" and kind == "dir":
        if not _symlink_points_inside(parent_fd, name, root):
            raise SkillsError(f"refusing to follow symlink {name!r}")
        try:
            os.unlink(name, dir_fd=parent_fd)
        except OSError as exc:
            raise SkillsError(f"refusing to replace {name!r}") from exc
        return
    if current == "dir":
        _remove_dir_nofollow(parent_fd, name)
        return
    try:
        os.unlink(name, dir_fd=parent_fd)
    except OSError as exc:
        raise SkillsError(f"refusing to replace {name!r}") from exc


def _directory_fd_path(fd: int) -> str:
    """Path of an open directory fd. A child symlink is not followed.

    macOS ``/dev/fd`` does not resolve to that directory. ``F_GETPATH`` names
    the directory that was opened. Linux names it through ``/proc/self/fd``.
    """
    proc = f"/proc/self/fd/{fd}"
    try:
        found = os.readlink(proc)
    except OSError:
        found = ""
    if found:
        return found
    if not hasattr(fcntl, "F_GETPATH"):
        raise OSError("directory path is unavailable")
    raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
    if isinstance(raw, bytes):
        text = raw.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
    else:
        text = str(raw).split("\0", 1)[0]
    if not text:
        raise OSError("directory path is unavailable")
    return text


def _symlink_points_inside(parent_fd: int, name: str, root: Path) -> bool:
    """True when the link text stays inside ``root`` without following it."""
    try:
        target = os.readlink(name, dir_fd=parent_fd)
    except OSError:
        return False
    if not target or "\x00" in target:
        return False
    try:
        parent = Path(_directory_fd_path(parent_fd)).resolve()
        root_norm = Path(os.path.normpath(str(root.resolve())))
    except OSError:
        return False
    if os.path.isabs(target):
        candidate = Path(os.path.normpath(target))
    else:
        candidate = Path(os.path.normpath(os.path.join(str(parent), target)))
    try:
        candidate.relative_to(root_norm)
    except ValueError:
        return False
    return True


def _remove_dir_nofollow(parent_fd: int, name: str) -> None:
    """Remove a directory tree. A symlink is never followed."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        child = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise SkillsError(f"refusing to follow symlink {name!r}") from exc
    try:
        mode = stat.S_IMODE(os.fstat(child).st_mode)
        if mode & 0o200 == 0:
            os.fchmod(child, mode | 0o200)
        for entry in os.listdir(child):
            info = os.stat(entry, dir_fd=child, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                _remove_dir_nofollow(child, entry)
            else:
                os.unlink(entry, dir_fd=child)
    finally:
        os.close(child)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError as exc:
        raise SkillsError(f"refusing to replace {name!r}") from exc


def _open_or_make_dir(parent_fd: int, name: str) -> int:
    """Return a directory fd for ``name`` under ``parent_fd``. Never follows a link.

    The recorded mode is applied later. A directory that is not writable by
    the user is made writable so its children can be restored first.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        child = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        os.mkdir(name, 0o755, dir_fd=parent_fd)
        child = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise SkillsError(f"refusing to follow symlink {name!r}") from exc
    try:
        mode = stat.S_IMODE(os.fstat(child).st_mode)
        if mode & 0o200 == 0:
            os.fchmod(child, mode | 0o700)
    except OSError as exc:
        os.close(child)
        raise SkillsError(f"could not set mode on {name!r}") from exc
    return child


def _chmod_recorded_dir(skill_dir: Path, item: dict[str, Any]) -> None:
    rel = item.get("rel")
    if not isinstance(rel, str):
        raise SkillsError("refusing to restore an unnamed skill file")
    parts = _rel_parts(rel)
    if parts is None:
        raise SkillsError(f"refusing to restore {rel!r}")
    mode = _mode_of(item, 0o755)
    try:
        parent_fd = os.open(skill_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise SkillsError("refusing to roll back through a symlink") from exc
    try:
        for part in parts[:-1]:
            child_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = child_fd
        _fchmod_fd(parent_fd, parts[-1], mode)
    except OSError as exc:
        raise SkillsError(f"could not set mode on {rel!r}") from exc
    finally:
        os.close(parent_fd)


def _fchmod_fd(parent_fd: int, name: str, mode: int) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _fchmod_path(path: Path, mode: int) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _put_file(parent_fd: int, name: str, data: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, mode, dir_fd=parent_fd)
    except OSError as exc:
        raise SkillsError(f"refusing to follow symlink {name!r}") from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _put_symlink(parent_fd: int, name: str, target: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SkillsError(f"refusing to replace {name!r}") from exc
    try:
        os.symlink(target, name, dir_fd=parent_fd)
    except TypeError:
        os.symlink(target, Path(f"/dev/fd/{parent_fd}") / name)


def _validated_dir(state: dict[str, Any], snapshot_id: str, agent: str) -> Path:
    """The snapshot's directory for ``agent``, or raise before anything is touched.

    A snapshot missing ``dir`` (``Path("")`` would be the process cwd) or
    naming a relative path is refused: rollback rewrites children of this
    directory, so guessing is never acceptable.
    """
    raw = state.get("dir")
    if not isinstance(raw, str) or not raw:
        raise SkillsError(
            f"snapshot {snapshot_id}: agent {agent!r} records no directory; refusing to restore"
        )
    path = Path(raw)
    if not path.is_absolute():
        raise SkillsError(
            f"snapshot {snapshot_id}: directory {raw!r} for agent {agent!r} "
            "is not absolute; refusing to restore"
        )
    return path


def _names(directory: Path) -> dict[str, tuple[str, str | None]]:
    """name -> (kind, symlink target or None) for one agent directory."""
    if not directory.is_dir():
        return {}
    out: dict[str, tuple[str, str | None]] = {}
    for child in directory.iterdir():
        if child.is_symlink():
            try:
                out[child.name] = ("symlink", os.readlink(child))
            except OSError:
                out[child.name] = ("symlink", None)
        elif child.is_dir():
            out[child.name] = ("dir", None)
        elif child.is_file():
            out[child.name] = ("file", None)
    return out


def _safe_component(name: str) -> bool:
    return (
        bool(name)
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and ".." not in name
    )


def _copy_source(root: Path, manifest: dict[str, Any], agent: str, name: str) -> Path:
    """Central directory for a copy-mode restore.

    A dedup alias is stored under another name (``beta`` lives in ``alpha``).
    Reading ``root / name`` misses that tree and the restore writes a
    placeholder over the original body.
    """
    record = manifest.get(name)
    chosen = name
    if isinstance(record, dict):
        variants = record.get("variants") or []
        if isinstance(variants, list):
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                agents = variant.get("agents") or []
                central = variant.get("central_name")
                if (
                    isinstance(agents, list)
                    and agent in agents
                    and isinstance(central, str)
                    and _safe_component(central)
                ):
                    chosen = central
                    break
        if chosen == name:
            central = record.get("central_name")
            agents = record.get("agents") or []
            if (
                isinstance(central, str)
                and _safe_component(central)
                and isinstance(agents, list)
                and (not agents or agent in agents)
                and (root / central / SKILL_MD).is_file()
            ):
                chosen = central
    if not _safe_component(chosen):
        chosen = name
    candidate = root / chosen
    if (candidate / SKILL_MD).is_file():
        return candidate
    return root / name


def _restore_agent(
    skill_dir: Path,
    entries: dict[str, Any],
    root: Path,
    *,
    agent: str = "",
    manifest: dict[str, Any] | None = None,
) -> list[str]:
    """Recreate what the snapshot recorded and this directory has lost.

    Only missing entries are recreated. An entry the snapshot does not name is
    left exactly as it is: it appeared after the snapshot, so it is the user's
    (or a later install's), and ``rollback_snapshot`` reports it back. There is
    no deletion path here at all — that is the point.
    """
    if not str(skill_dir):
        return []
    skill_dir.mkdir(parents=True, exist_ok=True)
    touched: list[str] = []
    for name, state in sorted(entries.items()):
        if not isinstance(state, dict):
            continue
        if state.get("kind") == "file":
            # Real file names carry dots (``notes.md``) that the skill-name
            # pattern forbids; only the traversal rules apply to them.
            if not _safe_file_name(name):
                continue
        elif not valid_name(name) or name in RESERVED_NAMES:
            continue
        source = _copy_source(root, manifest or {}, agent, name)
        if _restore_entry(skill_dir / name, state, source):
            touched.append(name)
    return touched


def _safe_file_name(name: str) -> bool:
    """A recorded file name must stay one component inside the skill dir."""
    return (
        bool(name)
        and name not in RESERVED_NAMES
        and len(name) <= MAX_NAME_LEN
        and "/" not in name
        and "\\" not in name
        and name not in (".", "..")
    )


def _restore_entry(path: Path, state: dict[str, Any], source: Path) -> bool:
    """Bring one recorded entry back. Returns True when the tree changed.

    An entry that is still on disk is never wiped: it belongs to the user
    now, and re-materializing the snapshot over it would trade a reversible
    change for lost data (a hand-edited skill, a directory the repo never
    had). Restore only recreates what is missing, plus the one cheap lossless
    repair — a symlink whose target drifted gets re-pointed.
    """
    kind = state.get("kind")
    if kind == "symlink" and isinstance(state.get("target"), str):
        target = state["target"]
        if path.is_symlink():
            try:
                if os.readlink(path) == target:
                    return False
            except OSError:
                pass
            path.unlink()
            _symlink(Path(target), path)
            return True
        if not path.exists():
            _symlink(Path(target), path)
            return True
        return False  # something real replaced the link; keep it
    if kind == "dir":
        if path.exists() or path.is_symlink():
            return False
        if (source / SKILL_MD).is_file():
            copy_skill_tree(source, path)
        else:
            # The repo no longer has the content; leave an explicit marker
            # instead of resurrecting stale bytes from elsewhere.
            path.mkdir(parents=True, exist_ok=True)
            (path / SKILL_MD).write_text(
                "---\nname: placeholder\n---\n\n"
                "Removed by a Conductor skills rollback; the source skill "
                "is no longer in the central repo.\n",
                encoding="utf-8",
            )
        return True
    if kind == "file":
        if path.exists() or path.is_symlink():
            return False
        content = state.get("content")
        if not isinstance(content, str):
            return False  # over the recording cap when the snapshot was taken
        try:
            path.write_bytes(base64.b64decode(content.encode("ascii")))
        except (ValueError, OSError):
            return False
        return True
    return False


def _symlink(target: Path, link: Path) -> None:
    import os

    link.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        link.unlink()
    os.symlink(str(target), link)
