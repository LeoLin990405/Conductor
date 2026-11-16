"""Stored Source events and their Thread rows.

A Trigger is immutable. Its stable id is supplied by the Source; the unique
``(source_id, external_id)`` is the second line of defence when a Source emits
the same event under a changed display id.
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection

_TRIGGER_COLUMNS = """
    trigger_id, source_id, user_id, external_id, kind, thread_key,
    actor_login, actor_is_bot, addressed, text, url, payload, received_at
"""
_THREAD_COLUMNS = """
    thread_key, user_id, source_id, pending_trigger_ids,
    evaluating_eval_id, meta, created_at, updated_at
"""


def _trigger(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["source_id"] = str(out["source_id"])
    out["payload"] = dict(out.get("payload") or {})
    return out


def _thread(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["source_id"] = str(out["source_id"])
    out["pending_trigger_ids"] = list(out.get("pending_trigger_ids") or [])
    out["evaluating_eval_id"] = (
        str(out["evaluating_eval_id"])
        if out.get("evaluating_eval_id")
        else None
    )
    out["meta"] = dict(out.get("meta") or {})
    return out


async def insert_trigger(
    *,
    trigger_id: str,
    source_id: str,
    user_id: str,
    external_id: str,
    kind: str,
    thread_key: str,
    actor_login: str,
    actor_is_bot: bool,
    addressed: bool,
    text: str,
    url: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Insert a Trigger, or return None when it was already stored.

    ``ON CONFLICT DO NOTHING`` covers both idempotency keys. The service can
    treat None as "already handled" and make no further state change.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO autonomy_triggers (
                    trigger_id, source_id, user_id, external_id, kind,
                    thread_key, actor_login, actor_is_bot, addressed,
                    text, url, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING {_TRIGGER_COLUMNS}
                """,
                (
                    trigger_id,
                    source_id,
                    user_id,
                    external_id,
                    kind,
                    thread_key,
                    actor_login,
                    bool(actor_is_bot),
                    bool(addressed),
                    text,
                    url,
                    Json(payload or {}),
                ),
            )
            return _trigger(await cur.fetchone())


async def get_trigger(trigger_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_TRIGGER_COLUMNS} FROM autonomy_triggers WHERE trigger_id = %s",
                (trigger_id,),
            )
            return _trigger(await cur.fetchone())


async def ensure_thread(
    *, thread_key: str, user_id: str, source_id: str, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Create the Thread row once; return the current row on every call."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO autonomy_threads (thread_key, user_id, source_id, meta)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (thread_key) DO UPDATE
                SET updated_at = NOW()
                RETURNING {_THREAD_COLUMNS}
                """,
                (thread_key, user_id, source_id, Json(meta or {})),
            )
            return _thread(await cur.fetchone())  # type: ignore[return-value]


async def append_pending_trigger(thread_key: str, trigger_id: str) -> list[str]:
    """Append a trigger to its Thread's Eval queue, returning the new queue."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE autonomy_threads
                SET pending_trigger_ids = (
                        SELECT ARRAY(
                            SELECT DISTINCT unnest(
                                pending_trigger_ids || ARRAY[%s]::TEXT[]
                            )
                        )
                    ),
                    eval_expiries = 0,
                    updated_at = NOW()
                WHERE thread_key = %s
                RETURNING pending_trigger_ids
                """,
                (trigger_id, thread_key),
            )
            row = await cur.fetchone()
            return list(row["pending_trigger_ids"] if row else [])


async def list_triggers_for_thread(
    thread_key: str, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_TRIGGER_COLUMNS}
                FROM autonomy_triggers
                WHERE thread_key = %s
                ORDER BY received_at DESC
                LIMIT %s OFFSET %s
                """,
                (thread_key, limit, offset),
            )
            return [_trigger(row) for row in await cur.fetchall() if row]  # type: ignore[misc]
