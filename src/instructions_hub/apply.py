"""Apply rendered instructions to a HOME, with snapshots and rollback (I2).

Safety rules, in the order ``apply`` checks them:

- Only clean managed files are replaced: the ``source=<digest>`` in the
  managed header (after the hub's ``.mdc`` front matter) still matches the
  file's own bytes, or the file is exactly a Claude stub. A managed file
  someone edited is refused as ``modified``; any other file as ``unmanaged``.
  ``take_over`` overrides both.
- A symlinked file, a path that leaves the HOME, or a file that cannot be
  read is refused, and ``take_over`` does not override that.
- One refused file means nothing is written (all-or-nothing).
- Every write happens under an exclusive lock, after a snapshot of the prior
  bytes, and each file is compared-and-swapped against the bytes the plan saw.
- ``rollback`` restores the newest applied snapshot, and only when every
  file still holds the bytes the apply wrote (unless forced).
"""

from __future__ import annotations

import contextlib
import difflib
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import core
from .core import HubError, atomic_write, stays_inside
from .render import render
from .store import _select, resolve_library
from .targets import (
    CLI_EXTRA_FILES,
    RENDERED_DIR,
    Target,
    get_target,
    rendered_file,
    resolve_name,
)

# Called while the lock is held, before the re-plan. Tests use it to prove a
# second apply or rollback is refused while one is running.
hold_lock_hook: Callable[[], None] | None = None

_ORIGINAL_ATOMIC_WRITE = atomic_write
_MANAGED_PREFIX = "<!-- managed_by=conductor"
_HEADER_RE = re.compile(r"<!-- managed_by=conductor source=([0-9a-f]{12}) -->\n\n")
_MDC_WRAPPER = "---\nalwaysApply: true\n---\n\n"
_STUB_RE = re.compile(r"@~/" + re.escape(RENDERED_DIR) + r"/[A-Za-z0-9_.-]+\.md\n?")
_SNAPSHOT_RE = re.compile(r"\d{8}T\d{12}(?:-\d+)?")
_NEW_FILE_MODE = 0o644
_NEW_DIR_MODE = 0o755
_PRIVATE_DIR_MODE = 0o700


@dataclass
class _Entry:
    """One planned file, with the bytes the plan read and will write."""

    row: dict[str, Any]
    path: Path
    body: bytes
    current: bytes | None
    mode: int | None


# --------------------------------------------------------------------------
# Plan


def plan_apply(
    home: Path,
    *,
    profile: str = "operator",
    targets: list[str] | None = None,
    library: Path | None = None,
    take_over: Collection[str] = (),
    environ: dict[str, str] | None = None,
    operator: bool = False,
) -> dict[str, Any]:
    """What ``apply`` would write. Writes nothing."""
    base = core.resolve_home(home, operator=operator)
    plan, _entries = _plan(base, profile, targets, library, take_over, environ, operator)
    return plan


def _plan(
    base: Path,
    profile: str,
    targets: list[str] | None,
    library: Path | None,
    take_over: Collection[str],
    environ: dict[str, str] | None,
    operator: bool,
) -> tuple[dict[str, Any], list[_Entry]]:
    lib = resolve_library(base, library, operator=operator)
    try:
        selected = _select(targets)
        taken = {get_target(resolve_name(name)).name for name in take_over}
    except KeyError as exc:
        raise HubError(str(exc.args[0]) if exc.args else str(exc)) from exc

    # path -> (targets, role, body); a path shared by two targets appears once.
    seen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    failed: list[dict[str, Any]] = []
    for target in selected:
        try:
            rendered = render(
                profile,
                target.name,
                library=lib if lib.exists() else None,
                home=base,
                environ=environ,
            )
        except (HubError, ValueError, OSError) as exc:
            if "unknown profile" in str(exc):
                raise HubError(str(exc)) from exc
            failed.extend(_failed_rows(target, base, environ, profile, str(exc)))
            continue
        for planned in rendered.files:
            key = str(planned.path)
            if key not in seen:
                seen[key] = {
                    "targets": [target.name],
                    "role": planned.role,
                    "path": planned.path,
                    "body": planned.body,
                    "conflict": False,
                }
                order.append(key)
                continue
            slot = seen[key]
            slot["targets"].append(target.name)
            if slot["body"] != planned.body:
                slot["conflict"] = True

    entries: list[_Entry] = []
    rows: list[dict[str, Any]] = []
    for key in order:
        slot = seen[key]
        entry = _plan_file(base, slot, taken)
        entries.append(entry)
        rows.append(entry.row)
    rows.extend(failed)
    refused = sum(1 for row in rows if row["action"] == "refused")
    changes = sum(1 for row in rows if row["action"] in ("create", "update"))
    plan = {
        "home": str(base),
        "profile": profile,
        "dry_run": True,
        "files": rows,
        "refused": refused,
        "changes": changes,
    }
    return plan, entries


def _plan_file(base: Path, slot: dict[str, Any], taken: set[str]) -> _Entry:
    path: Path = slot["path"]
    body = slot["body"].encode("utf-8")
    row: dict[str, Any] = {
        "target": slot["targets"][0],
        "role": slot["role"],
        "path": str(path),
        "action": "unchanged",
        "current_digest": None,
        "rendered_digest": _sha(body),
        "bytes": len(body),
        "unified_diff": "",
    }
    if len(slot["targets"]) > 1:
        row["targets"] = list(slot["targets"])
    entry = _Entry(row, path, body, None, None)

    def refuse(reason: str) -> _Entry:
        row["action"] = "refused"
        row["reason"] = reason
        return entry

    if slot["conflict"]:
        return refuse("conflicting_targets")
    escape = _escape_reason(base, path)
    if escape:
        return refuse(escape)
    try:
        info = path.lstat()
    except FileNotFoundError:
        row["action"] = "create"
        row["unified_diff"] = _diff(None, body, path)
        return entry
    except OSError as exc:
        return refuse(f"unreadable: {exc.strerror or type(exc).__name__}")
    if stat.S_ISLNK(info.st_mode):
        return refuse("symlink")
    if not stat.S_ISREG(info.st_mode):
        return refuse("unreadable: not a regular file")
    try:
        current = path.read_bytes()
    except OSError as exc:
        return refuse(f"unreadable: {exc.strerror or type(exc).__name__}")
    entry.current = current
    entry.mode = stat.S_IMODE(info.st_mode)
    row["current_digest"] = _sha(current)
    try:
        text = current.decode("utf-8")
    except UnicodeDecodeError:
        return refuse("unreadable: not UTF-8")
    if current == body:
        return entry
    row["unified_diff"] = _diff(current, body, path)
    state = _managed_state(text, path, base, role=slot["role"])
    if state != "clean" and not (set(slot["targets"]) & taken):
        return refuse(state)
    row["action"] = "update"
    if state != "clean":
        row["take_over"] = True
    return entry


def _escape_reason(base: Path, path: Path) -> str | None:
    """``symlink`` / ``outside_home`` when the path or an ancestor escapes."""
    try:
        path.relative_to(base)
    except ValueError:
        return "outside_home"
    if not stays_inside(base, path):
        # An ancestor is a symlink that resolves out of the HOME.
        return "outside_home"
    return None


def _managed_state(text: str, path: Path, base: Path, *, role: str) -> str:
    """``clean``, ``modified`` or ``unmanaged`` for a file on disk.

    ``clean``: the hub wrote it and nobody changed it since. The managed
    header carries ``source=<digest>``, the first 12 hex of the sha256 of the
    document with the digest left out (see ``render._managed``), so the
    digest is recomputed from the file's own bytes. A header whose digest no
    longer matches the body is ``modified``: another tool edited a file the
    hub manages. A Claude stub has no header; it is clean when it is exactly
    a stub line naming a rendered body, and only for the stub role.
    """
    if role == "stub":
        return "clean" if _STUB_RE.fullmatch(text) else "unmanaged"
    rendered_dir = base.joinpath(*rendered_file("x")[:-1])
    hub_owned = path.parent == rendered_dir
    document = text
    if path.suffix == ".mdc":
        if text.startswith(_MDC_WRAPPER):
            document = text[len(_MDC_WRAPPER):]
        elif _mdc_header_after(text):
            # Hub-written body under front matter someone else changed.
            return "modified"
    if not document.startswith(_MANAGED_PREFIX):
        return "modified" if hub_owned else "unmanaged"
    match = _HEADER_RE.match(document)
    if match is None:
        return "modified"
    digest = match.group(1)
    unsigned = document[: match.start(1)] + document[match.end(1) :]
    recomputed = hashlib.sha256(unsigned.encode("utf-8")).hexdigest()[:12]
    return "clean" if recomputed == digest else "modified"


def _mdc_header_after(text: str) -> bool:
    """A managed header after front matter that is not the hub's wrapper."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return False
    index = 1
    while index < len(lines) and lines[index].strip() != "---":
        index += 1
    index += 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    return index < len(lines) and lines[index].startswith(_MANAGED_PREFIX)


def _failed_rows(
    target: Target,
    base: Path,
    environ: dict[str, str] | None,
    profile: str,
    error: str,
) -> list[dict[str, Any]]:
    """Refused rows for a target that could not be rendered at all.

    ``render`` refuses a path whose ancestors leave the HOME before it plans
    any file, so the rows name the default paths of that target.
    """
    escaped = "inside the isolated HOME" in error
    paths = [_literal(base, target.relative, target.env, target.env_file,
                      target.env_config_only, environ)]
    if target.kind == "stub":
        paths.append(base.joinpath(*rendered_file(profile)))
    extra = CLI_EXTRA_FILES.get(target.name)
    if extra is not None:
        paths.append(_literal(base, extra.relative, extra.env, extra.env_file, False, environ))
    rows = []
    for path in paths:
        if path.is_symlink():
            reason = "symlink"
        elif escaped:
            reason = "outside_home"
        else:
            reason = error
        rows.append(
            {
                "target": target.name,
                "role": "",
                "path": str(path),
                "action": "refused",
                "reason": reason,
                "error": error,
                "current_digest": None,
                "rendered_digest": None,
                "bytes": 0,
                "unified_diff": "",
            }
        )
    return rows


def _literal(
    base: Path,
    relative: tuple[str, ...],
    env: str | None,
    env_file: str,
    config_only: bool,
    environ: dict[str, str] | None,
) -> Path:
    mapping = os.environ if environ is None else environ
    if env and not config_only:
        raw = (mapping.get(env) or "").strip()
        if raw:
            override = Path(raw).expanduser()
            if not override.is_absolute():
                override = base / override
            return override / env_file
    return base.joinpath(*relative)


def _diff(current: bytes | None, body: bytes, path: Path) -> str:
    old = [] if current is None else current.decode("utf-8", "replace").splitlines(True)
    new = body.decode("utf-8").splitlines(True)
    return "".join(
        difflib.unified_diff(
            old,
            new,
            fromfile="/dev/null" if current is None else str(path),
            tofile=str(path),
        )
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# Lock


def _hub_dir(base: Path) -> Path:
    hub = base / ".conductor" / "instructions"
    if not stays_inside(base, hub):
        raise HubError("the instructions directory must stay inside the HOME")
    return hub


def _mkdirs(base: Path, target: Path, mode: int) -> None:
    """Create every missing directory from ``base`` down to ``target``."""
    relative = target.relative_to(base)
    current = base
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=mode)
            os.chmod(current, mode)
            continue
        if stat.S_ISLNK(info.st_mode) and not stays_inside(base, current):
            raise HubError(f"{current} leaves the HOME")
        if not current.is_dir():
            raise HubError(f"{current} is not a directory")


@contextlib.contextmanager
def _locked(base: Path) -> Iterator[None]:
    hub = _hub_dir(base)
    _mkdirs(base, hub, _PRIVATE_DIR_MODE)
    lock = hub / ".lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock, flags, 0o600)
    except OSError as exc:
        raise HubError(f"cannot open the instructions lock: {exc.strerror}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise HubError("another instructions apply is running") from exc
            raise
        try:
            if hold_lock_hook is not None:
                hold_lock_hook()
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# Apply


def apply(
    home: Path,
    *,
    profile: str = "operator",
    targets: list[str] | None = None,
    library: Path | None = None,
    take_over: Collection[str] = (),
    environ: dict[str, str] | None = None,
    operator: bool = False,
) -> dict[str, Any]:
    """Write the plan. All-or-nothing; snapshot first; compare-and-swap."""
    base = core.resolve_home(home, operator=operator)
    with _locked(base):
        plan, entries = _plan(
            base, profile, targets, library, take_over, environ, operator
        )
        plan["dry_run"] = False
        if plan["refused"]:
            paths = [
                f"{row['path']} ({row.get('reason', '')})"
                for row in plan["files"]
                if row["action"] == "refused"
            ]
            raise HubError("refused, nothing written: " + "; ".join(paths))
        changed = [e for e in entries if e.row["action"] in ("create", "update")]
        if not changed:
            plan["snapshot_id"] = None
            return plan
        snap_dir, manifest = _write_snapshot(base, profile, operator, changed)
        pairs = list(zip(changed, manifest["files"]))
        try:
            for entry, _record in pairs:
                try:
                    now = _current_bytes(entry.path)
                except HubError:
                    now = b"\0<unreadable>"
                if now != entry.current:
                    raise HubError(f"{entry.path} changed during apply")
                try:
                    _mkdirs(base, entry.path.parent, _NEW_DIR_MODE)
                    mode = entry.mode if entry.mode is not None else _NEW_FILE_MODE
                    _writer()(entry.path, entry.body, mode=mode)
                except (OSError, HubError) as exc:
                    raise HubError(f"{entry.path}: write failed: {exc}") from exc
        except BaseException:
            # A CAS miss, a failed write, or an interrupt (KeyboardInterrupt,
            # SIGTERM turned into an exception): put back what this apply wrote
            # and mark the snapshot aborted, then re-raise.
            _abort(base, snap_dir, manifest, pairs)
            raise
        plan["snapshot_id"] = manifest["id"]
        return plan


def _writer() -> Callable[..., None]:
    """``atomic_write``, honouring a patch on this module or on ``core``."""
    local = globals()["atomic_write"]
    if local is not _ORIGINAL_ATOMIC_WRITE:
        return local
    return core.atomic_write


def _current_bytes(path: Path) -> bytes | None:
    """The bytes at ``path`` now, ``None`` when absent. Never follows a link."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise HubError(f"{path} is not a regular file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise HubError(f"{path}: {exc.strerror}") from exc


def _snapshots_dir(base: Path) -> Path:
    return _hub_dir(base) / "snapshots"


def _write_snapshot(
    base: Path, profile: str, operator: bool, changed: list[_Entry]
) -> tuple[Path, dict[str, Any]]:
    root = _snapshots_dir(base)
    _mkdirs(base, root, _PRIVATE_DIR_MODE)
    now = datetime.now(UTC)
    stamp = now.strftime("%Y%m%dT%H%M%S%f")
    snap_id = stamp
    suffix = 0
    while True:
        snap_dir = root / snap_id
        try:
            snap_dir.mkdir(mode=_PRIVATE_DIR_MODE)
            break
        except FileExistsError:
            suffix += 1
            snap_id = f"{stamp}-{suffix}"
    os.chmod(snap_dir, _PRIVATE_DIR_MODE)
    blobs = snap_dir / "files"
    blobs.mkdir(mode=_PRIVATE_DIR_MODE)
    os.chmod(blobs, _PRIVATE_DIR_MODE)
    records = []
    for index, entry in enumerate(changed):
        blob = None
        if entry.current is not None:
            blob = f"files/{index}"
            core.atomic_write(snap_dir / blob, entry.current, mode=0o600)
        records.append(
            {
                "path": str(entry.path),
                "existed": entry.current is not None,
                "mode": entry.mode,
                "pre_digest": entry.row["current_digest"],
                "post_digest": entry.row["rendered_digest"],
                "blob": blob,
            }
        )
    manifest = {
        "id": snap_id,
        "created_at": now.isoformat(),
        "profile": profile,
        "operator": bool(operator),
        "state": "applied",
        "files": records,
    }
    _save_manifest(snap_dir, manifest)
    return snap_dir, manifest


def _save_manifest(snap_dir: Path, manifest: dict[str, Any]) -> None:
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    core.atomic_write(snap_dir / "manifest.json", text, mode=0o600)


def _abort(
    base: Path,
    snap_dir: Path,
    manifest: dict[str, Any],
    pairs: list[tuple[_Entry, dict[str, Any]]],
) -> None:
    """Put back every file this apply wrote, then mark the snapshot aborted.

    A file this apply wrote holds exactly the planned body, so that is the
    test; it also covers an interrupt between the replace and the
    bookkeeping. A file someone else changed is left alone.
    """
    for entry, record in reversed(pairs):
        with contextlib.suppress(OSError, HubError):
            if _current_bytes(entry.path) == entry.body:
                _restore(base, snap_dir, record)
    manifest["state"] = "aborted"
    _save_manifest(snap_dir, manifest)


def _restore(base: Path, snap_dir: Path, record: dict[str, Any]) -> str:
    """Restore one file from a snapshot. ``restored`` or ``deleted``."""
    path = Path(record["path"])
    if not record.get("existed"):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return "deleted"
    blob = _blob_path(snap_dir, record)
    data = blob.read_bytes()
    _mkdirs(base, path.parent, _NEW_DIR_MODE)
    mode = record.get("mode")
    core.atomic_write(path, data, mode=_NEW_FILE_MODE if mode is None else int(mode))
    return "restored"


def _blob_path(snap_dir: Path, record: dict[str, Any]) -> Path:
    name = record.get("blob")
    if not isinstance(name, str) or not name:
        raise HubError(f"snapshot {snap_dir.name} has no copy of {record.get('path')}")
    blob = snap_dir / name
    if not stays_inside(snap_dir, blob) or blob.is_symlink() or not blob.is_file():
        raise HubError(f"snapshot {snap_dir.name} has a bad copy of {record.get('path')}")
    return blob


# --------------------------------------------------------------------------
# Snapshots and rollback


def _load_snapshots(base: Path) -> list[dict[str, Any]]:
    root = _snapshots_dir(base)
    if not stays_inside(base, root) or root.is_symlink() or not root.is_dir():
        return []
    found = []
    for child in root.iterdir():
        if not _SNAPSHOT_RE.fullmatch(child.name) or child.is_symlink() or not child.is_dir():
            continue
        manifest_path = child / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict) or manifest.get("id") != child.name:
            continue
        if not isinstance(manifest.get("files"), list):
            continue
        found.append(manifest)
    found.sort(key=lambda item: _sort_key(item["id"]), reverse=True)
    return found


def _sort_key(snap_id: str) -> tuple[str, int]:
    stamp, _sep, suffix = snap_id.partition("-")
    return stamp, int(suffix or 0)


def list_snapshots(home: Path, *, operator: bool = False) -> list[dict[str, Any]]:
    """Snapshots under ``home``, newest first."""
    base = core.resolve_home(home, operator=operator)
    return [
        {
            "id": manifest["id"],
            "created_at": manifest.get("created_at"),
            "profile": manifest.get("profile"),
            "state": manifest.get("state"),
            "files": len(manifest["files"]),
        }
        for manifest in _load_snapshots(base)
    ]


def _choose(snapshots: list[dict[str, Any]], snapshot_id: str | None) -> dict[str, Any]:
    """The snapshot ``rollback`` may restore, or a HubError saying why not."""
    applied = [item for item in snapshots if item.get("state") == "applied"]
    if snapshot_id is None:
        if not applied:
            raise HubError("no applied snapshot to roll back")
        return applied[0]
    manifest = next((item for item in snapshots if item["id"] == snapshot_id), None)
    if manifest is None:
        raise HubError(f"unknown snapshot {snapshot_id!r}")
    if manifest.get("state") != "applied":
        raise HubError(f"snapshot {snapshot_id} is {manifest.get('state')}, not applied")
    if applied and applied[0]["id"] != snapshot_id:
        raise HubError(f"roll back {applied[0]['id']} first")
    return manifest


def _already_restored(record: dict[str, Any], now: bytes | None) -> bool:
    """The file already holds its pre-apply state (an interrupted apply)."""
    if not record.get("existed"):
        return now is None
    return now is not None and _sha(now) == record.get("pre_digest")


def rollback(
    home: Path,
    snapshot_id: str | None = None,
    *,
    library: Path | None = None,
    force: bool = False,
    operator: bool = False,
) -> dict[str, Any]:
    """Restore the newest applied snapshot. ``library`` is accepted, unused.

    A file that already holds its pre-apply bytes (or is absent when it did
    not exist before) is skipped, not refused: an apply that was killed
    before it wrote that file leaves it so.
    """
    del library
    base = core.resolve_home(home, operator=operator)
    # Fail before the lock (which creates the hub directories) when there is
    # nothing to roll back; the choice is re-checked under the lock.
    _choose(_load_snapshots(base), snapshot_id)
    with _locked(base):
        manifest = _choose(_load_snapshots(base), snapshot_id)
        snap_dir = _snapshots_dir(base) / manifest["id"]
        refused: list[str] = []
        todo: list[dict[str, Any]] = []
        skipped: list[str] = []
        for record in manifest["files"]:
            path = Path(str(record.get("path", "")))
            if not path.is_absolute() or _escape_reason(base, path):
                refused.append(f"{path} (outside_home)")
                continue
            if path.is_symlink():
                refused.append(f"{path} (symlink)")
                continue
            try:
                now = _current_bytes(path)
            except HubError:
                refused.append(f"{path} (unreadable)")
                continue
            if _already_restored(record, now):
                skipped.append(str(path))
                continue
            if record.get("existed"):
                _blob_path(snap_dir, record)
            digest = None if now is None else _sha(now)
            if digest != record.get("post_digest") and not force:
                refused.append(f"{path} (changed_since_apply)")
                continue
            todo.append(record)
        if refused:
            raise HubError("refused, nothing restored: " + "; ".join(refused))
        restored: list[str] = []
        deleted: list[str] = []
        for record in todo:
            outcome = _restore(base, snap_dir, record)
            (restored if outcome == "restored" else deleted).append(record["path"])
        manifest["state"] = "rolled_back"
        _save_manifest(snap_dir, manifest)
        return {
            "snapshot_id": manifest["id"],
            "restored": restored,
            "deleted": deleted,
            "skipped": skipped,
        }
