"""Append-only record of apply, publish, rollback, import, and recover.

The journal is the only fact. Each event is a new file under the journal
directory; an existing file is never rewritten. The first event for a snapshot
stays ``<id>.json``. Later transitions are ``<id>.<n>.json``. Decisions replay
these events into memory. ``managed.toml`` is a cache written afterwards and
is never read to decide. ``published`` means the target file landed.
``registered`` means a later replay accepted that publish. ``imported`` means
``import_existing`` claimed the listed names. ``released`` means an apply gave
up ownership of the keys it lists, by deleting or disabling them, and records
the owned set that stays. A successful rollback appends ``rolled_back``. An
``intent`` whose file never landed appends ``abandoned``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .agents import assert_no_escape, hub_dir
from .atomic import atomic_write
from .errors import McpHubError
from .model import AGENT_IDS

# Active entries still name a publish recover or rollback may honor.
# ``imported`` claims names but is not a publish, so rollback does not treat
# its hash as a post-image. ``released`` is a publish that also gave up the
# keys it lists, so it is active for the same reasons ``registered`` is.
_ACTIVE = {"intent", "published", "registered", "released"}
_TERMINAL = {"rolled_back", "abandoned"}
_CLAIM = {"imported"}
_STATES = _ACTIVE | _TERMINAL | _CLAIM


def write_entry(home: Path, snapshot_id: str, entry: dict[str, Any]) -> None:
    """Append one event and fsync the journal directory.

    The first event for ``snapshot_id`` is ``<id>.json``. Later events are
    ``<id>.<n>.json``. Nothing in this directory is opened for replacement.
    """
    _check_entry(entry)
    directory = _directory(home, create=True)
    if directory is None:
        raise McpHubError("apply journal is unavailable")
    path = _next_event_path(directory, snapshot_id)
    assert_no_escape(home, path)
    document = {"snapshot_id": snapshot_id, "entries": [dict(entry)]}
    _write(home, path, document)


def entry_for(home: Path, snapshot_id: str, agent: str) -> dict[str, Any] | None:
    """Latest event for ``agent`` in ``snapshot_id``, or none."""
    found: dict[str, Any] | None = None
    for path in _event_paths(home, snapshot_id):
        document = _read(path)
        for existing in document.get("entries") or []:
            if isinstance(existing, dict) and existing.get("agent") == agent:
                found = dict(existing)
                found["snapshot_id"] = snapshot_id
    return found


def post_hash(home: Path, snapshot_id: str, agent: str) -> str | None:
    """Post-hash recorded for this snapshot, including an intent that did land.

    An ``intent`` entry already holds the intended post-hash. Rollback uses it
    only when the file's current hash matches, which is true after the publish
    and false when the publish never happened.
    """
    found = entry_for(home, snapshot_id, agent)
    if found is None or found.get("state") not in _ACTIVE:
        return None
    digest = found.get("post_hash")
    if not isinstance(digest, str) or not digest:
        return None
    return digest


def later_post_matches(
    home: Path, snapshot_id: str, agent: str, current_hash: str
) -> bool:
    """True when a newer still-valid apply recorded ``current_hash``."""
    for other in snapshot_ids(home):
        if other <= snapshot_id:
            continue
        found = entry_for(home, other, agent)
        if found is None or found.get("state") not in {"published", "registered", "released"}:
            continue
        if found.get("post_hash") == current_hash:
            return True
    return False


def later_apply_active(home: Path, snapshot_id: str, agent: str) -> bool:
    """True when a newer apply for ``agent`` is still published or registered.

    The hash does not matter. An older snapshot is not the one to roll back
    while this newer apply stands. A newer release also stands: it gave up the
    keys an older apply still names, so that older apply is not the owner.
    """
    for other in snapshot_ids(home):
        if other <= snapshot_id:
            continue
        found = entry_for(home, other, agent)
        if found is not None and found.get("state") in {"published", "registered", "released"}:
            return True
    return False


def snapshot_ids(home: Path) -> list[str]:
    """Snapshot ids that have at least one journal event, oldest first."""
    directory = _directory(home, create=False)
    if directory is None or not directory.is_dir() or directory.is_symlink():
        return []
    found: set[str] = set()
    for path in directory.glob("*.json"):
        if path.is_symlink() or not path.is_file():
            continue
        snapshot_id = _snapshot_id_from_name(path.name)
        if snapshot_id is not None:
            found.add(snapshot_id)
    return sorted(found)


def older_post_matches(
    home: Path, snapshot_id: str, agent: str, current_hash: str | None
) -> bool:
    """True when an older still-valid apply recorded ``current_hash``.

    Those bytes are the user's copy of that older apply. Replay must not
    treat them as the pre-image of a newer apply and roll the newer one back.
    """
    if not current_hash:
        return False
    for other in snapshot_ids(home):
        if other >= snapshot_id:
            continue
        found = entry_for(home, other, agent)
        if found is None or found.get("state") not in {"published", "registered", "released"}:
            continue
        if found.get("post_hash") == current_hash:
            return True
    return False


def ownership_from_journal(
    home: Path,
) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Owned server names and post-hash replayed for each agent.

    Walk events from oldest to newest. A ``registered``, ``imported`` or
    ``released`` event replaces that agent's owned set. ``imported`` stores the
    full set, not a delta. A newer ``published`` event that is not registered
    yet supersedes older claims, so those names are not returned until replay
    accepts it. ``rolled_back`` and ``abandoned`` do not claim. The publish
    hash moves only with ``registered`` or ``released``. A ``released`` event
    stores the owned set that stays after it gave up the keys it lists, so a
    released key drops out of ownership. This function does not read
    ``managed.toml``.
    """
    managed = {agent: set() for agent in AGENT_IDS}
    published: dict[str, str] = {}
    for snapshot_id in snapshot_ids(home):
        for agent in AGENT_IDS:
            found = entry_for(home, snapshot_id, agent)
            if found is None:
                continue
            state = found.get("state")
            if state in {"registered", "released", "imported"}:
                owned = found.get("owned")
                managed[agent] = (
                    {str(name) for name in owned} if isinstance(owned, list) else set()
                )
                if state in {"registered", "released"}:
                    digest = found.get("post_hash")
                    if isinstance(digest, str) and digest:
                        published[agent] = digest
            elif state == "published":
                managed[agent] = set()
                published.pop(agent, None)
    return managed, published


def published_entries(home: Path) -> list[dict[str, Any]]:
    """Entries whose file landed and whose replay has not accepted them yet."""
    return _entries_in(home, {"published"})


def pending_entries(home: Path) -> list[dict[str, Any]]:
    """``published`` rows, plus ``intent`` rows recover may still accept."""
    return _entries_in(home, {"published", "intent"})


def mark_registered(home: Path, rows: list[dict[str, Any]]) -> None:
    """Append ``registered`` when replay accepts a publish.

    The sidecar is not consulted. An ``intent`` row is included when recover
    has already checked that the current bytes equal the intended post-hash.
    ``rolled_back`` and ``abandoned`` stay terminal.
    """
    _mark(home, rows, sources={"published", "intent"}, dest="registered")


def mark_released(home: Path, rows: list[dict[str, Any]]) -> None:
    """Append ``released`` when replay accepts an ownership-release publish.

    The row already lists the keys it gave up in ``released`` and the owned
    set that stays in ``owned``; only the state moves. Used by recover for an
    ``intent`` or ``published`` release whose bytes match its post-hash.
    """
    _mark(home, rows, sources={"published", "intent"}, dest="released")


def mark_rolled_back(home: Path, rows: list[dict[str, Any]]) -> None:
    """Set entries to ``rolled_back`` after their bytes were restored."""
    _mark(home, rows, sources=_ACTIVE, dest="rolled_back")


def mark_abandoned(home: Path, rows: list[dict[str, Any]]) -> None:
    """Set an ``intent`` row to ``abandoned`` when the file never landed."""
    _mark(home, rows, sources={"intent"}, dest="abandoned")


def _entries_in(home: Path, states: set[str]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for snapshot_id in snapshot_ids(home):
        agents: set[str] = set()
        for path in _event_paths(home, snapshot_id):
            document = _read(path)
            for existing in document.get("entries") or []:
                if isinstance(existing, dict) and isinstance(existing.get("agent"), str):
                    agents.add(existing["agent"])
        for agent in sorted(agents):
            latest = entry_for(home, snapshot_id, agent)
            if latest is None or latest.get("state") not in states:
                continue
            key = (snapshot_id, agent)
            if key in seen:
                continue
            seen.add(key)
            found.append(latest)
    return found


def _mark(
    home: Path,
    rows: list[dict[str, Any]],
    *,
    sources: set[str],
    dest: str,
) -> None:
    """Append a new event. The previous file stays as it was."""
    for row in rows:
        snapshot_id = str(row.get("snapshot_id") or "")
        agent = row.get("agent")
        if not isinstance(agent, str) or not snapshot_id:
            continue
        current = entry_for(home, snapshot_id, agent)
        if current is None or current.get("state") not in sources:
            continue
        updated = dict(current)
        updated["state"] = dest
        if dest == "rolled_back":
            updated["rollback_of"] = snapshot_id
        write_entry(home, snapshot_id, updated)


def _directory(home: Path, *, create: bool) -> Path | None:
    if create:
        root = hub_dir(home)
    else:
        root = home / ".conductor" / "mcp"
        if not root.is_dir() or root.is_symlink():
            return None
    directory = root / "journal"
    if create:
        assert_no_escape(home, directory)
        directory.mkdir(exist_ok=True)
        os.chmod(directory, 0o700)
        return directory
    return directory


def _path(home: Path, snapshot_id: str, *, create: bool) -> Path | None:
    if not _safe_id(snapshot_id):
        raise McpHubError("invalid snapshot id")
    directory = _directory(home, create=create)
    if directory is None:
        return None
    path = directory / f"{snapshot_id}.json"
    if create:
        assert_no_escape(home, path)
    return path


def _event_paths(home: Path, snapshot_id: str) -> list[Path]:
    directory = _directory(home, create=False)
    if directory is None or not directory.is_dir() or directory.is_symlink():
        return []
    if not _safe_id(snapshot_id):
        return []
    ranked: list[tuple[int, Path]] = []
    for path in directory.glob("*.json"):
        if path.is_symlink() or not path.is_file():
            continue
        parsed = _event_seq(path.name, snapshot_id)
        if parsed is None:
            continue
        ranked.append((parsed, path))
    ranked.sort(key=lambda item: item[0])
    return [path for _seq, path in ranked]


def _next_event_path(directory: Path, snapshot_id: str) -> Path:
    if not _safe_id(snapshot_id):
        raise McpHubError("invalid snapshot id")
    primary = directory / f"{snapshot_id}.json"
    if not primary.exists():
        return primary
    seq = 1
    while True:
        candidate = directory / f"{snapshot_id}.{seq}.json"
        if not candidate.exists():
            return candidate
        seq += 1


def _event_seq(name: str, snapshot_id: str) -> int | None:
    if name == f"{snapshot_id}.json":
        return 0
    prefix = f"{snapshot_id}."
    if not name.startswith(prefix) or not name.endswith(".json"):
        return None
    seq = name[len(prefix) : -len(".json")]
    if not seq.isdigit():
        return None
    return int(seq)


def _snapshot_id_from_name(name: str) -> str | None:
    if not name.endswith(".json"):
        return None
    stem = name[: -len(".json")]
    if "." in stem:
        base, seq = stem.rsplit(".", 1)
        if seq.isdigit() and _safe_id(base):
            return base
        return None
    if _safe_id(stem):
        return stem
    return None


def _write(home: Path, path: Path, document: dict[str, Any]) -> None:
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write(path, payload, mode=0o600, home=home)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        return {"entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise McpHubError("apply journal is unreadable") from exc
    if not isinstance(data, dict):
        raise McpHubError("apply journal is invalid")
    return data


def _check_entry(entry: dict[str, Any]) -> None:
    if entry.get("state") not in _STATES:
        raise McpHubError("apply journal state is invalid")
    target = entry.get("target")
    agent = entry.get("agent")
    post = entry.get("post_hash")
    if not isinstance(target, str) or not target:
        raise McpHubError("apply journal target is invalid")
    if not isinstance(agent, str) or not agent:
        raise McpHubError("apply journal agent is invalid")
    if not isinstance(post, str) or not post:
        raise McpHubError("apply journal post-hash is invalid")


def _safe_id(name: str) -> bool:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return False
    return True
