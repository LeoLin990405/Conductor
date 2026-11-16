"""Outbox rows for Super-agent replies (one channel reply per row)."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

_COLUMNS = """
    outbox_id, user_id, ticket_id, source_id, thread_key, body,
    idempotency_key, status, attempts, due_at, sent_at, last_error,
    channel, reply_kind, approved_at, approved_by,
    created_at, updated_at
"""


def _row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("outbox_id", "ticket_id", "source_id"):
        if out.get(key):
            out[key] = str(out[key])
    return out


async def enqueue_outbox(
    *,
    user_id: str,
    ticket_id: str | None,
    source_id: str | None,
    thread_key: str,
    body: str,
    idempotency_key: str,
    status: str = "pending",
    channel: str = "",
    reply_kind: str = "result",
) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO autonomy_outbox (
                    user_id, ticket_id, source_id, thread_key, body, idempotency_key,
                    status, channel, reply_kind
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING {_COLUMNS}
                """,
                (
                    user_id,
                    ticket_id,
                    source_id,
                    thread_key,
                    body,
                    idempotency_key,
                    status,
                    channel,
                    reply_kind,
                ),
            )
            return _row(await cur.fetchone())


async def get_outbox(outbox_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM autonomy_outbox WHERE outbox_id = %s",
                (outbox_id,),
            )
            return _row(await cur.fetchone())


async def get_by_idempotency_key(key: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM autonomy_outbox WHERE idempotency_key=%s",
                (key,),
            )
            return _row(await cur.fetchone())


async def list_outbox(
    user_id: str, *, status: str = "", limit: int = 50
) -> list[dict[str, Any]]:
    """List one user's replies, newest first, optionally filtered by status."""
    bounded = max(1, min(int(limit or 50), 200))
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if status:
                await cur.execute(
                    f"""
                    SELECT {_COLUMNS} FROM autonomy_outbox
                    WHERE user_id = %s AND status = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (user_id, status, bounded),
                )
            else:
                await cur.execute(
                    f"""
                    SELECT {_COLUMNS} FROM autonomy_outbox
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (user_id, bounded),
                )
            rows = await cur.fetchall()
            return [_row(row) for row in rows if row is not None]


async def mark_sent(outbox_id: str) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_outbox
                SET status = 'sent', sent_at = NOW(), updated_at = NOW()
                WHERE outbox_id = %s AND status = 'pending'
                """,
                (outbox_id,),
            )


async def mark_dropped(outbox_id: str, error: str) -> bool:
    """Settle a reply that cannot be delivered because its Source is gone."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_outbox
                SET status = 'dropped', last_error = %s, updated_at = NOW()
                WHERE outbox_id = %s AND status = 'pending'
                """,
                ((error or "")[:2000], outbox_id),
            )
            return cur.rowcount > 0


async def mark_pending_approval(outbox_id: str, reason: str) -> bool:
    """Hold one reply for a person to read before it is delivered."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_outbox
                SET status = 'pending_approval', last_error = %s, updated_at = NOW()
                WHERE outbox_id = %s AND status = 'pending'
                """,
                ((reason or "")[:2000], outbox_id),
            )
            return cur.rowcount > 0


async def mark_approved(outbox_id: str, approved_by: str) -> bool:
    """Release one held reply for delivery."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_outbox
                SET status = 'pending', approved_at = NOW(), approved_by = %s,
                    last_error = '', updated_at = NOW()
                WHERE outbox_id = %s AND status = 'pending_approval'
                """,
                (str(approved_by or "")[:200], outbox_id),
            )
            return cur.rowcount > 0


async def mark_rejected(outbox_id: str, approved_by: str) -> bool:
    """Settle one held reply that its reader did not want to send."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_outbox
                SET status = 'rejected', approved_at = NOW(), approved_by = %s,
                    updated_at = NOW()
                WHERE outbox_id = %s AND status = 'pending_approval'
                """,
                (str(approved_by or "")[:200], outbox_id),
            )
            return cur.rowcount > 0


async def mark_failed(outbox_id: str, error: str) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_outbox
                SET attempts = attempts + 1,
                    last_error = %s,
                    due_at = NOW() + make_interval(secs => LEAST(300, 5 * POWER(2, attempts))),
                    updated_at = NOW()
                WHERE outbox_id = %s AND status = 'pending'
                """,
                (error[:2000], outbox_id),
            )
