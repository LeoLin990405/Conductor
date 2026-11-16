"""Count the archived worker sessions shown by Super-agent Ticket views."""

from __future__ import annotations

from collections.abc import Mapping


def count_rotations(ticket: object) -> int:
    """Return the number of distinct provider sessions archived by a Ticket."""
    if not isinstance(ticket, Mapping):
        return 0
    worker_session = ticket.get("worker_session")
    archived = worker_session.get("archived") if isinstance(worker_session, Mapping) else None
    if not archived:
        archived = ticket.get("session_archive")
    if not isinstance(archived, list):
        return 0

    session_ids: set[str] = set()
    for entry in archived:
        if not isinstance(entry, dict):
            continue
        session_id = entry.get("session_id")
        if not isinstance(session_id, str):
            continue
        session_id = session_id.strip()
        if session_id and not session_id.startswith("rot-"):
            session_ids.add(session_id)
    return len(session_ids)


__all__ = ["count_rotations"]
