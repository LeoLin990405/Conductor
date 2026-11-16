"""Peer Worker messages: one bounded, cross-worker channel per receiver."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

# One advisory lock namespace for every peer inbox; the hash of the receiver
# ticket id keeps concurrent senders of different inboxes off each other.
_PEER_LOCK_NAMESPACE = "autonomy_peer:"


def _row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["message_id"] = str(out["message_id"])
    return out


def _rows(rows: list[Any]) -> list[dict[str, Any]]:
    return [item for item in (_row(row) for row in rows) if item is not None]


async def pending_count(receiver_ticket_id: str) -> int:
    """Count a receiver's unconsumed messages (the channel's fill level)."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT COUNT(*) AS n FROM autonomy_peer_messages "
                "WHERE receiver_ticket_id = %s AND read_at IS NULL",
                (receiver_ticket_id,),
            )
            row = await cur.fetchone()
    return int((row or {}).get("n") or 0)


async def insert_message(
    *,
    user_id: str,
    sender_ticket_id: str,
    receiver_ticket_id: str,
    text: str,
    capacity: int,
) -> dict[str, Any] | None:
    """Append one message unless the receiver's pending channel is full.

    The count-and-insert runs under a transaction-scoped advisory lock so
    concurrent senders on different backend workers cannot both slip past a
    capacity that is already reached. ``None`` means the channel is full.
    """
    async with get_db_connection() as conn:
        # Connections are autocommit, so the xact-scoped lock only spans the
        # count and insert when they share an explicit transaction.
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (_PEER_LOCK_NAMESPACE + receiver_ticket_id,),
                )
                await cur.execute(
                    "SELECT COUNT(*) AS n FROM autonomy_peer_messages "
                    "WHERE receiver_ticket_id = %s AND read_at IS NULL",
                    (receiver_ticket_id,),
                )
                row = await cur.fetchone()
                if int((row or {}).get("n") or 0) >= max(1, capacity):
                    return None
                await cur.execute(
                    """
                    INSERT INTO autonomy_peer_messages
                        (user_id, sender_ticket_id, receiver_ticket_id, text)
                    VALUES (%s, %s, %s, %s)
                    RETURNING message_id, user_id, sender_ticket_id,
                              receiver_ticket_id, text, created_at, read_at
                    """,
                    (user_id, sender_ticket_id, receiver_ticket_id, text),
                )
                return _row(await cur.fetchone())


async def claim_pending(receiver_ticket_id: str, limit: int) -> list[dict[str, Any]]:
    """Atomically consume up to ``limit`` pending messages, oldest first.

    ``SKIP LOCKED`` keeps concurrent readers from claiming the same rows, so
    each message is returned to exactly one reader.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE autonomy_peer_messages SET read_at = NOW()
                WHERE message_id IN (
                    SELECT message_id FROM autonomy_peer_messages
                    WHERE receiver_ticket_id = %s AND read_at IS NULL
                    ORDER BY created_at, message_id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING message_id, user_id, sender_ticket_id,
                          receiver_ticket_id, text, created_at, read_at
                """,
                (receiver_ticket_id, limit),
            )
            rows = _rows(await cur.fetchall())
    return sorted(rows, key=lambda r: (r["created_at"], r["message_id"]))
