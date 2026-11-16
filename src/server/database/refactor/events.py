"""Append-only agent event stream (SQL layer, migration 088).

The stream is what the Phase-3 timeline and the differential harness read; it is
not a second source of truth for run state (the run ledger owns that). Rows are
append-only by convention — this module exposes no update/delete.

``seq`` is optional: a caller that already numbers its SSE frames (the stream
producer) passes the same number. Without one, the insert reads the current max
for that response and adds one; the partial unique index
``uq_agent_events_seq`` rejects a concurrent duplicate so the caller can retry
rather than silently interleaving. That is why the index is on
``(response_id, seq)`` and not a global sequence: replay fixtures compare one
response's frames, and a global sequence would renumber every deployment's first
event.
"""

from __future__ import annotations

from typing import Any, Mapping

from psycopg.rows import dict_row

from src.server.database import pool
from src.server.utils.pg_sanitize import SafeJson, normalize_uuid, refactor_owner_uuid

_EVENT_COLUMNS = (
    "event_id, user_id, thread_id, response_id, task_run_id, seq, type, payload,"
    " created_at"
)


def _normalized_id(value: object, field: str) -> str | None:
    if value is None:
        return None
    normalized = normalize_uuid(value)
    if normalized is None:
        raise ValueError(f"{field} is not a UUID")
    return normalized


async def append_event(
    *,
    user_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    thread_id: str | None = None,
    response_id: str | None = None,
    task_run_id: str | None = None,
    seq: int | None = None,
) -> dict[str, Any]:
    """Append one event. Raises ``ValueError`` for an invalid id.

    When ``response_id`` is set and ``seq`` is omitted, the next per-response
    sequence is computed and inserted in one connection; a concurrent writer can
    make the insert fail on the unique index, and the caller retries with the
    next value (the stream producer passes its own seq, so this path is for
    out-of-band writers such as permission decisions).
    """
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        raise ValueError("user_id is required")
    thread = _normalized_id(thread_id, "thread_id")
    response = _normalized_id(response_id, "response_id")
    task_run = _normalized_id(task_run_id, "task_run_id")
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if response is not None and seq is None:
                await cur.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq"
                    " FROM agent_events WHERE response_id = %s",
                    (response,),
                )
                row = await cur.fetchone()
                seq = int(row["next_seq"]) if row else 1
            await cur.execute(
                f"""
                INSERT INTO agent_events
                    (user_id, thread_id, response_id, task_run_id, seq, type, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING {_EVENT_COLUMNS}
                """,
                (
                    owner,
                    thread,
                    response,
                    task_run,
                    int(seq if seq is not None else 0),
                    event_type,
                    SafeJson(dict(payload)),
                ),
            )
            return dict(await cur.fetchone())


async def list_events(
    *,
    thread_id: str,
    after_event_id: int = 0,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """A thread's events in append order, after a cursor, bounded by ``limit``."""
    if limit < 1 or limit > 1000:
        raise ValueError("event page size must be between 1 and 1000")
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_EVENT_COLUMNS} FROM agent_events"
                " WHERE thread_id = %s AND event_id > %s"
                " ORDER BY event_id LIMIT %s",
                (_normalized_id(thread_id, "thread_id"), int(after_event_id), limit),
            )
            return [dict(row) for row in await cur.fetchall()]
