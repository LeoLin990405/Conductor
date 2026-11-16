"""Diff, apply and rollback.

Apply is dry-run unless ``apply=True``. A real apply takes an exclusive lock,
snapshots the bytes it is about to change, then publishes each file with
``os.replace``. The journal is append-only and is the only fact. Before the
publish, a new event records state ``intent``. After the file lands a new
event records ``published``. Every ownership decision replays the journal
into memory for this command. ``managed.toml`` is written after that decision
for people to inspect. A failed sidecar write does not change the decision.
``recover`` is that replay.
An ``intent`` whose bytes match the post-hash is registered; any other
``intent`` appends ``abandoned``. The latest valid apply for an agent wins
even when the file bytes match an older apply. Manual rollback restores the
file first, then appends ``rolled_back``. Only that latest apply can be
rolled back. The registry is left as the caller set it.
An apply also releases ownership: a key the hub owns but the registry no
longer wants is deleted (server removed) or disabled (server off, agent has
an ``enabled`` field). That publish records a ``released`` event carrying the
owned set that stays. A key already gone from the file is a no-op release:
no snapshot and no write, but the stale ownership is still dropped so a key
the user recreates by hand is never taken back. The same holds when the whole
file is gone: the release is journalled against the digest of no bytes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Collection

from .agents import assert_no_escape, hub_dir
from .atomic import atomic_delete, atomic_write, file_mode
from .errors import McpHubError, PublishConflict
from .journal import (
    entry_for,
    later_apply_active,
    later_post_matches,
    mark_rolled_back,
    older_post_matches,
    ownership_from_journal,
    post_hash,
    snapshot_ids,
    write_entry,
)
from .model import AGENT_IDS
from .render import _home, render_agent
from .store import load_registry, registry_path, save_managed

# Tests block inside the lock so a second apply fails cleanly instead of racing.
hold_lock_hook: Callable[[], None] | None = None

# A config file the user deleted has no bytes to hash. This digest stands for
# "absent", so a release can still be journalled for that file.
_ABSENT_DIGEST = hashlib.sha256(b"").hexdigest()


def diff_all(
    home: Path | str,
    *,
    allow_inline_secret: Collection[str] = (),
) -> dict[str, dict[str, Any]]:
    """Per-agent path, diff, skipped servers, and orphaned keys. Writes nothing."""
    root = _home(home)
    registry = load_registry(root)
    out: dict[str, dict[str, Any]] = {}
    for agent in AGENT_IDS:
        view = render_agent(
            root, agent, registry, allow_inline_secret=allow_inline_secret
        )
        out[agent] = {
            "path": view["path"],
            "unified_diff": view["unified_diff"],
            "skipped": view["skipped"],
            "orphaned": view["orphaned"],
            "released": view["released"],
        }
    return out


def apply_all(
    home: Path | str,
    *,
    apply: bool,
    allow_inline_secret: Collection[str] = (),
) -> dict[str, Any]:
    """When ``apply`` is false, return :func:`diff_all` and write nothing.

    When true, snapshot then atomically update agent files that changed.
    Each successful publish rewrites the managed sidecar. A run that changes
    no agent file writes no snapshot. The return value carries ``snapshot_id``
    when a snapshot was written.
    """
    root = _home(home)
    if not apply:
        return diff_all(root, allow_inline_secret=allow_inline_secret)
    _require_registry(root)
    _acquire(root)
    try:
        hook = hold_lock_hook
        if hook is not None:
            hook()
        reconcile(root)
        managed, published = ownership_from_journal(root)
        registry = load_registry(root)
        views = [
            render_agent(
                root,
                agent,
                registry,
                allow_inline_secret=allow_inline_secret,
                owned=managed.get(agent, ()),
            )
            for agent in AGENT_IDS
        ]
        diffs = {
            view["agent"]: {
                "path": view["path"],
                "unified_diff": view["unified_diff"],
                "skipped": view["skipped"],
                "orphaned": view["orphaned"],
                "released": view["released"],
            }
            for view in views
        }
        files = [view for view in views if view["changed"]]
        managed_now = {agent: set(names) for agent, names in managed.items()}
        if not files:
            # No bytes change, but a key the user already deleted may still be
            # owned. Release that stale ownership so a recreated key survives.
            _release_noops(root, views, managed, managed_now, published)
            return {"applied": True, "snapshot_id": None, "diffs": diffs}
        conflicts = _conflicts(files)
        if conflicts:
            return _conflict_result(diffs, conflicts, snapshot_id=None)
        snapshot_id = _snapshot(root, files)
        conflicts = _conflicts(files)
        if conflicts:
            _discard_snapshot(root, snapshot_id)
            return _conflict_result(diffs, conflicts, snapshot_id=None)
        succeeded: list[str] = []
        for view in files:
            path = Path(view["path"])
            rendered = view["rendered"]
            if rendered is None:
                continue
            agent = str(view["agent"])
            # Compare-and-swap against the pre-image recorded in the snapshot.
            # A mismatch leaves that destination, and every earlier publish, as they are.
            if not _bytes_match(path, view):
                return _partial_result(
                    diffs,
                    snapshot_id=snapshot_id,
                    succeeded=succeeded,
                    failed=[_failure(view, "PublishConflict")],
                )
            mode = 0o600 if view["force_private"] else int(view["mode"])
            existed = bool(view["existed"])
            expected = (
                view["original"]
                if existed and isinstance(view["original"], bytes)
                else None
            )
            payload = rendered.encode("utf-8")
            post = hashlib.sha256(payload).hexdigest()
            pre_hash = (
                hashlib.sha256(expected).hexdigest()
                if isinstance(expected, bytes)
                else None
            )
            owned = sorted(str(name) for name in view["own"])
            released = sorted(str(item["key"]) for item in view["released"])
            record = {
                "snapshot_id": snapshot_id,
                "agent": agent,
                "target": str(view["relpath"]),
                "pre_hash": pre_hash,
                "post_hash": post,
                "owned": owned,
                "state": "intent",
            }
            if released:
                record["released"] = released
            try:
                write_entry(root, snapshot_id, record)
            except (OSError, McpHubError) as exc:
                return _partial_result(
                    diffs,
                    snapshot_id=snapshot_id,
                    succeeded=succeeded,
                    failed=[_failure(view, str(exc))],
                )
            try:
                atomic_write(
                    path,
                    payload,
                    mode=mode,
                    home=root,
                    expected=expected,
                    expect_absent=not existed,
                )
            except PublishConflict:
                return _partial_result(
                    diffs,
                    snapshot_id=snapshot_id,
                    succeeded=succeeded,
                    failed=[_failure(view, "PublishConflict")],
                )
            except OSError as exc:
                return _partial_result(
                    diffs,
                    snapshot_id=snapshot_id,
                    succeeded=succeeded,
                    failed=[_failure(view, str(exc))],
                )
            record["state"] = "published"
            try:
                write_entry(root, snapshot_id, record)
            except (OSError, McpHubError) as exc:
                succeeded.append(agent)
                return _partial_result(
                    diffs,
                    snapshot_id=snapshot_id,
                    succeeded=succeeded,
                    failed=[],
                    warnings=[_unregistered(view, str(exc))],
                )
            published[agent] = post
            managed_now[agent] = set(view["own"])
            try:
                save_managed(root, managed_now, published=published)
            except (PublishConflict, OSError, McpHubError) as exc:
                succeeded.append(agent)
                return _partial_result(
                    diffs,
                    snapshot_id=snapshot_id,
                    succeeded=succeeded,
                    failed=[],
                    warnings=[_unregistered(view, str(exc))],
                )
            record["state"] = "released" if released else "registered"
            try:
                write_entry(root, snapshot_id, record)
            except (OSError, McpHubError):
                # The file is already published. A missed ``registered`` event
                # does not unpublish it. The next replay appends that event.
                # The sidecar cache is not what keeps the claim.
                pass
            succeeded.append(agent)
        _release_noops(root, views, managed, managed_now, published)
        return {"applied": True, "snapshot_id": snapshot_id, "diffs": diffs}
    finally:
        _release(root)


def recover(home: Path | str) -> dict[str, Any]:
    """Replay the journal and rebuild the sidecar cache.

    An ``intent`` whose bytes equal the post-hash appends ``registered``.
    Any other ``intent`` appends ``abandoned``. A ``published`` entry whose
    bytes still match appends ``registered``. A newer valid apply stays the
    owner even when the file bytes match an older apply. A second call appends
    nothing once every accepted entry is ``registered``.
    """
    root = _home(home)
    _acquire(root)
    try:
        return reconcile(root)
    finally:
        _release(root)


def reconcile(root: Path) -> dict[str, Any]:
    """Append missing journal transitions and rewrite the sidecar cache.

    A sidecar write failure is ignored. The next command replays the journal
    again. This does not treat a file that matches an older apply as a reason
    to take that older apply back.
    """
    from .journal import mark_abandoned, mark_registered, mark_released

    recovered: list[dict[str, str]] = []
    for snapshot_id in snapshot_ids(root):
        for agent in AGENT_IDS:
            found = entry_for(root, snapshot_id, agent)
            if found is None:
                continue
            state = found.get("state")
            target = found.get("target")
            digest = found.get("post_hash")
            if not isinstance(target, str) or not target:
                continue
            if not isinstance(digest, str) or not digest:
                continue
            current_bytes = _file_bytes(root.joinpath(*PureSplit(target)))
            current = (
                hashlib.sha256(current_bytes).hexdigest()
                if current_bytes is not None
                else None
            )
            if state == "intent" and current != digest:
                mark_abandoned(root, [found])
                continue
            if state in {"intent", "published"} and current == digest:
                if later_apply_active(root, snapshot_id, agent):
                    continue
                if _has_release(found):
                    mark_released(root, [found])
                else:
                    mark_registered(root, [found])
                recovered.append(
                    {
                        "agent": agent,
                        "target": target,
                        "snapshot_id": snapshot_id,
                    }
                )
                continue
            # A restore that landed, whose journal append did not, leaves the
            # pre-image on disk. Fill ``rolled_back`` on replay. Bytes that
            # equal an older apply stay that apply's content and are not a
            # rollback of the newer one.
            if state in {"published", "registered", "released"} and current != digest:
                if later_apply_active(root, snapshot_id, agent):
                    continue
                if older_post_matches(root, snapshot_id, agent, current):
                    continue
                if _is_preimage(root, snapshot_id, target):
                    mark_rolled_back(root, [found])
    if snapshot_ids(root):
        managed, published = ownership_from_journal(root)
        try:
            save_managed(root, managed, published=published)
        except (OSError, McpHubError, PublishConflict):
            pass
    return {"recovered": recovered}


def rollback(
    home: Path | str,
    snapshot_id: str | None = None,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview a snapshot restore. Pass ``apply=True`` to write it back.

    ``snapshot_id`` defaults to the newest snapshot. Agent files the snapshot
    did not record are left alone. The default is a dry run and does not
    create the lock or change any config bytes.
    """
    root = _home(home)
    directory = _find_snapshot(root, snapshot_id)
    planned = _planned_restore(directory)
    if not apply:
        return {
            "dry_run": True,
            "snapshot_id": directory.name,
            "restored": [],
            "would_restore": [rel for rel, _entry in planned],
        }
    _acquire(root)
    try:
        reconcile(root)
        return _rollback_apply(root, directory, planned)
    finally:
        _release(root)


def _rollback_apply(
    root: Path, directory: Path, planned: list[tuple[str, dict[str, Any]]]
) -> dict[str, Any]:
    """Restore a target only when its bytes still match the journal post-hash.

    The bytes written back are the snapshot's agent file, never the sidecar.
    A file the user changed after publish is left alone and reported as
    ``modified_since_apply``. A snapshot with no journal post-hash is left
    unrestored. The sidecar written at the end is a fresh journal replay.
    A failed sidecar write does not fail the rollback. A skipped file keeps
    the claim the journal still records.
    """
    restored: list[str] = []
    modified: list[dict[str, str]] = []
    superseded: list[dict[str, str]] = []
    # Newest target first. A later apply of another snapshot is skipped below.
    ordered = list(reversed(planned))
    for rel, entry in ordered:
        agent = _agent_of(entry)
        if agent is None:
            continue
        journal_row = entry_for(root, directory.name, agent)
        if journal_row is not None and journal_row.get("state") in {
            "rolled_back",
            "abandoned",
        }:
            continue
        if later_apply_active(root, directory.name, agent):
            superseded.append(_skip(agent, rel, "superseded_by_later_apply"))
            continue
        target = root.joinpath(*PureSplit(rel))
        current_bytes = _file_bytes(target)
        current = (
            hashlib.sha256(current_bytes).hexdigest()
            if current_bytes is not None
            else None
        )
        post = post_hash(root, directory.name, agent)
        # Do not invent a hash from the sidecar. Pre-journal snapshots
        # are unsupported and stay unrestored.
        if post is None:
            continue
        if current == post and current_bytes is not None:
            try:
                _restore_one(root, directory, rel, entry, expected=current_bytes)
            except PublishConflict:
                modified.append(_skip(agent, rel, "modified_since_apply"))
                continue
            restored.append(rel)
            if journal_row is not None and journal_row.get("state") in {
                "intent",
                "published",
                "registered",
                "released",
            }:
                mark_rolled_back(root, [journal_row])
            continue
        if current is not None and later_post_matches(
            root, directory.name, agent, current
        ):
            superseded.append(_skip(agent, rel, "superseded_by_later_apply"))
            continue
        if _matches_preimage(target, directory, entry):
            try:
                _restore_one(root, directory, rel, entry, expected=current_bytes)
            except PublishConflict:
                modified.append(_skip(agent, rel, "modified_since_apply"))
                continue
            restored.append(rel)
            if journal_row is not None and journal_row.get("state") in {
                "intent",
                "published",
                "registered",
                "released",
            }:
                mark_rolled_back(root, [journal_row])
            continue
        modified.append(_skip(agent, rel, "modified_since_apply"))
    managed, published = ownership_from_journal(root)
    try:
        save_managed(root, managed, published=published)
    except (OSError, McpHubError, PublishConflict):
        pass
    return {
        "dry_run": False,
        "snapshot_id": directory.name,
        "restored": restored,
        "modified_since_apply": modified,
        "superseded_by_later_apply": superseded,
    }


def _skip(agent: str, rel: str, reason: str) -> dict[str, str]:
    return {"agent": agent, "path": rel, "reason": reason}


def _agent_of(entry: dict[str, Any]) -> str | None:
    blob = entry.get("blob")
    if isinstance(blob, str) and blob in AGENT_IDS:
        return blob
    return None


def _has_release(entry: dict[str, Any]) -> bool:
    """True when a journal row records an ownership release."""
    return bool(entry.get("released"))


def _file_bytes(path: Path) -> bytes | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _is_preimage(root: Path, snapshot_id: str, target: str) -> bool:
    """True when ``target`` is already the pre-image this snapshot recorded."""
    base = root / ".conductor" / "mcp" / "snapshots" / snapshot_id
    if not _safe_blob(snapshot_id) or base.is_symlink() or not base.is_dir():
        return False
    try:
        planned = _planned_restore(base)
    except McpHubError:
        return False
    for rel, entry in planned:
        if rel == target:
            return _matches_preimage(root.joinpath(*PureSplit(rel)), base, entry)
    return False


def _matches_preimage(path: Path, directory: Path, entry: dict[str, Any]) -> bool:
    """True when ``path`` is still the pre-image this snapshot recorded."""
    try:
        if path.is_symlink():
            return False
        if not entry.get("existed"):
            return not path.exists()
        blob = entry.get("blob")
        if not isinstance(blob, str) or not path.is_file():
            return False
        return path.read_bytes() == (directory / blob).read_bytes()
    except OSError:
        return False


def _conflicts(views: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"agent": str(view["agent"]), "path": str(view["path"])}
        for view in views
        if not _bytes_match(Path(view["path"]), view)
    ]


def _conflict_result(
    diffs: dict[str, Any],
    conflicts: list[dict[str, str]],
    *,
    snapshot_id: str | None,
) -> dict[str, Any]:
    return {
        "applied": False,
        "conflict": True,
        "conflicts": conflicts,
        "snapshot_id": snapshot_id,
        "diffs": diffs,
    }


def _failure(view: dict[str, Any], reason: str) -> dict[str, str]:
    return {"agent": str(view["agent"]), "path": str(view["path"]), "reason": reason}


def _unregistered(view: dict[str, Any], reason: str) -> dict[str, str]:
    return {
        "agent": str(view["agent"]),
        "path": str(view["path"]),
        "status": "published_unregistered",
        "reason": reason,
    }


def _partial_result(
    diffs: dict[str, Any],
    *,
    snapshot_id: str | None,
    succeeded: list[str],
    failed: list[dict[str, str]],
    warnings: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """A batch that stopped. Already published targets stay published."""
    conflict_rows = [
        {"agent": item["agent"], "path": item["path"]}
        for item in failed
        if item.get("reason") == "PublishConflict"
    ]
    command = f"conductor mcp rollback {snapshot_id} --apply" if snapshot_id else None
    result = {
        "applied": False,
        "partial": bool(succeeded),
        "conflict": bool(conflict_rows),
        "conflicts": conflict_rows,
        "succeeded": list(succeeded),
        "failed": failed,
        "snapshot_id": snapshot_id,
        "rollback_command": command,
        "diffs": diffs,
    }
    if warnings:
        result["warnings"] = warnings
    return result


def _discard_snapshot(home: Path, snapshot_id: str) -> None:
    directory = home / ".conductor" / "mcp" / "snapshots" / snapshot_id
    if not _safe_blob(snapshot_id):
        return
    try:
        assert_no_escape(home, directory)
    except McpHubError:
        return
    if directory.is_symlink() or not directory.is_dir():
        return
    for child in directory.iterdir():
        if child.is_symlink() or not child.is_file():
            continue
        with contextlib.suppress(OSError):
            child.unlink()
    with contextlib.suppress(OSError):
        directory.rmdir()


def _release_noops(
    root: Path,
    views: list[dict[str, Any]],
    managed: dict[str, set[str]],
    managed_now: dict[str, set[str]],
    published: dict[str, str],
) -> None:
    """Drop stale ownership for released keys already gone from the file.

    A user who deleted an owned key by hand leaves the file already correct,
    so render reports no change and no snapshot is taken. Ownership is still
    stale, and a later apply would delete a key the user recreates. Append a
    ``released`` event that records the owned set that stays, then refresh the
    sidecar. A file that is gone entirely is journalled the same way, against
    the digest of no bytes: the ownership is just as stale, and a key the user
    recreates in a fresh file must stay theirs. A failed sidecar write does not
    undo the journal event.
    """
    for view in views:
        if view["changed"]:
            continue
        agent = str(view["agent"])
        new_owned = {str(name) for name in view["own"]}
        if new_owned == set(managed.get(agent, ())):
            continue
        released = sorted(str(item["key"]) for item in view["released"])
        if not released:
            continue
        original = view["original"]
        digest = (
            hashlib.sha256(original).hexdigest()
            if isinstance(original, bytes)
            else _ABSENT_DIGEST
        )
        snapshot_id = _release_snapshot_id(root)
        write_entry(
            root,
            snapshot_id,
            {
                "snapshot_id": snapshot_id,
                "agent": agent,
                "target": str(view["relpath"]),
                "pre_hash": digest,
                "post_hash": digest,
                "owned": sorted(new_owned),
                "released": released,
                "state": "released",
            },
        )
        managed_now[agent] = new_owned
        published[agent] = digest
    try:
        save_managed(root, managed_now, published=published)
    except (OSError, McpHubError, PublishConflict):
        pass


def _release_snapshot_id(root: Path) -> str:
    """A fresh journal id for a release that writes no snapshot directory."""
    return _fresh_id(root, root / ".conductor" / "mcp" / "snapshots")


def _fresh_id(root: Path, base: Path) -> str:
    """An id no journal event and no snapshot directory has taken yet.

    A release writes journal events without a snapshot directory, so an id
    that is only in the journal is still taken: a later snapshot that reused
    it would append its own events to that release's story.
    """
    taken = set(snapshot_ids(root))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    candidate = stamp
    counter = 0
    while candidate in taken or (base / candidate).exists():
        counter += 1
        candidate = f"{stamp}-{counter}"
    return candidate


def _bytes_match(path: Path, view: dict[str, Any]) -> bool:
    """True when ``path`` is still the bytes render read. Never follows a link."""
    try:
        if path.is_symlink():
            return False
        existed = bool(view["existed"])
        if not existed:
            return not path.exists()
        if not path.is_file():
            return False
        expected = view["original"]
        if not isinstance(expected, bytes):
            return False
        return path.read_bytes() == expected
    except OSError:
        return False


def _read_manifest(directory: Path) -> dict[str, Any]:
    manifest_path = directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise McpHubError("snapshot manifest is unreadable") from exc
    if not isinstance(manifest, dict):
        raise McpHubError("snapshot manifest is invalid")
    return manifest


def _planned_restore(directory: Path) -> list[tuple[str, dict[str, Any]]]:
    manifest = _read_manifest(directory)
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise McpHubError("snapshot manifest is invalid")
    planned: list[tuple[str, dict[str, Any]]] = []
    for rel, entry in files.items():
        # Old snapshots stored the sidecar here. Skip it before any blob
        # check so a missing or corrupt managed blob cannot block restore.
        if rel == ".conductor/mcp/managed.toml":
            continue
        if not isinstance(rel, str) or not isinstance(entry, dict):
            raise McpHubError("snapshot manifest is invalid")
        if not _safe_rel(rel):
            raise McpHubError("snapshot path is invalid")
        blob = entry.get("blob")
        if not isinstance(blob, str) or not _safe_blob(blob):
            raise McpHubError("snapshot blob is invalid")
        if entry.get("existed"):
            blob_path = directory / blob
            if blob_path.is_symlink() or not blob_path.is_file():
                raise McpHubError("snapshot blob is missing")
        planned.append((rel, entry))
    return planned


def _restore_one(
    root: Path,
    directory: Path,
    rel: str,
    entry: dict[str, Any],
    *,
    expected: bytes | None,
) -> None:
    """Put one target back, or raise ``PublishConflict`` if the bytes moved.

    ``expected`` is the image checked before this call. ``atomic_write`` and
    ``atomic_delete`` compare it again after ``before_replace_hook``.
    """
    target = root.joinpath(*PureSplit(rel))
    assert_no_escape(root, target)
    existed = bool(entry.get("existed"))
    if not existed:
        if expected is None:
            if target.is_symlink() or target.exists():
                raise PublishConflict("config changed before publish")
            return
        atomic_delete(target, expected=expected, home=root)
        return
    if expected is None:
        raise PublishConflict("config changed before publish")
    blob = entry.get("blob")
    if not isinstance(blob, str):
        raise McpHubError("snapshot blob is invalid")
    blob_path = directory / blob
    try:
        data = blob_path.read_bytes()
    except OSError as exc:
        raise McpHubError("snapshot blob is missing") from exc
    mode = int(entry.get("mode", 0o600)) & 0o777
    atomic_write(target, data, mode=mode, home=root, expected=expected)


def _require_registry(home: Path) -> None:
    """Refuse a real apply when ``servers.toml`` is gone, before any write.

    A missing registry reads as empty, and an empty registry releases every
    key the hub owns. A deleted or moved file is not a request to remove every
    server, so apply stops instead. An empty file is still a deliberate
    registry with no servers.
    """
    path = registry_path(home)
    if not path.exists() and not path.is_symlink():
        raise McpHubError(
            "servers.toml is missing; refusing to apply (an apply would release "
            "every server the hub owns). Restore it, or write an empty "
            "servers.toml to remove them all"
        )


def _acquire(home: Path) -> None:
    directory = hub_dir(home)
    lock = directory / "apply.lock"
    assert_no_escape(home, lock)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise McpHubError("another apply is in progress") from exc
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)


def _release(home: Path) -> None:
    lock = home / ".conductor" / "mcp" / "apply.lock"
    with contextlib.suppress(OSError):
        if lock.is_symlink():
            return
        lock.unlink()


def _snapshot(home: Path, views: list[dict[str, Any]]) -> str:
    base = hub_dir(home) / "snapshots"
    assert_no_escape(home, base)
    base.mkdir(exist_ok=True)
    os.chmod(base, 0o700)
    snap_id = _new_id(home, base)
    directory = base / snap_id
    directory.mkdir()
    os.chmod(directory, 0o700)
    manifest: dict[str, Any] = {"id": snap_id, "files": {}}
    for view in views:
        rel = str(view["relpath"])
        blob = str(view["agent"])
        entry: dict[str, Any] = {
            "existed": bool(view["existed"]),
            "mode": int(view["mode"]) & 0o777,
            "blob": blob,
        }
        # The mode we store is the pre-write mode. force_private changes the
        # written file, not the snapshot of what was there.
        if view["existed"] and not view["force_private"]:
            entry["mode"] = int(view["mode"]) & 0o777
        elif view["existed"]:
            path = Path(view["path"])
            entry["mode"] = file_mode(path, force_private=False)
        original = view["original"]
        if view["existed"] and isinstance(original, bytes):
            entry["pre_hash"] = hashlib.sha256(original).hexdigest()
            atomic_write(directory / blob, original, mode=0o600, home=home)
        else:
            entry["pre_hash"] = None
        manifest["files"][rel] = entry
    raw = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    atomic_write(directory / "manifest.json", raw, mode=0o600, home=home)
    return snap_id


def _new_id(home: Path, base: Path) -> str:
    return _fresh_id(home, base)


def _find_snapshot(home: Path, snapshot_id: str | None) -> Path:
    base = home / ".conductor" / "mcp" / "snapshots"
    if snapshot_id is None:
        if not base.is_dir():
            raise McpHubError("no snapshots to roll back")
        names = sorted(
            child.name
            for child in base.iterdir()
            if child.is_dir()
            and _safe_blob(child.name)
            and (child / "manifest.json").is_file()
        )
        if not names:
            raise McpHubError("no snapshots to roll back")
        snapshot_id = names[-1]
    if not _safe_blob(snapshot_id):
        raise McpHubError("invalid snapshot id")
    directory = base / snapshot_id
    assert_no_escape(home, directory)
    if not directory.is_dir():
        raise McpHubError(f"unknown snapshot {snapshot_id}")
    return directory


def _safe_rel(rel: str) -> bool:
    if not rel or rel.startswith(("/", "\\")) or "\\" in rel:
        return False
    parts = rel.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _safe_blob(name: str) -> bool:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return False
    return True


def PureSplit(rel: str) -> tuple[str, ...]:
    return tuple(rel.split("/"))
