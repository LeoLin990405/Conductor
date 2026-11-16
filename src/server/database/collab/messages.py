"""App-to-app messages: bounded, untrusted and consumed on read.

The peer channel's limits are reused rather than re-invented: 2000 characters
per message and 32 unread messages per recipient. Both ends are app ids, so a
message can never be addressed to a Ticket's Worker through this path.
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from psycopg.rows import dict_row

from src.collab.types import ChannelFullError
from src.collab.types import (
    MESSAGE_CHANNEL_CAPACITY,
    MESSAGE_MAX_CHARS,
    normalize_agent,
    normalize_app_id,
    normalize_task_ref,
)
from src.server.database.pool import get_db_connection

_MESSAGE_COLUMNS = """
    message_id, from_app_id, from_agent, to_target, text, about_ref,
    reply_to, read_at, created_at
"""


def _message(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    if out.get("message_id"):
        out["message_id"] = str(out["message_id"])
    out["untrusted"] = True
    return out


def _target(value: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 200:
        raise ValueError("message target must be a non-empty app or task reference")
    if text.startswith("app:"):
        normalize_app_id(text[4:])
    elif text.startswith("t:"):
        normalize_task_ref(text)
    return text


async def send_message(
    *,
    from_app_id: str,
    to_target: str,
    text: str,
    from_agent: str = "",
    about_ref: str = "",
    reply_to: str = "",
    channel_capacity: int = MESSAGE_CHANNEL_CAPACITY,
    conn: Any = None,
) -> dict[str, Any]:
    """Insert one message after checking the recipient's unread capacity.

    The advisory transaction lock serializes two senders filling the last slot;
    the count and insert cannot interleave on different server workers. Pass
    ``conn`` from :func:`collab_transaction` to join the caller's transaction,
    so a failed audit insert rolls the message back too.
    """
    if not isinstance(text, str) or not 1 <= len(text) <= MESSAGE_MAX_CHARS:
        raise ValueError(
            f"message text must be between 1 and {MESSAGE_MAX_CHARS} characters"
        )
    target = _target(to_target)
    about = normalize_task_ref(about_ref) if about_ref else ""
    capacity = max(1, int(channel_capacity))
    async with get_db_connection(conn) as db:
        transaction = db.transaction() if conn is None else nullcontext()
        async with transaction:
            async with db.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))", (target,)
                )
                await cur.execute(
                    """
                    SELECT COUNT(*) AS unread
                    FROM collab_messages
                    WHERE to_target = %s AND read_at IS NULL
                    """,
                    (target,),
                )
                count_row = await cur.fetchone() or {}
                if int(count_row.get("unread") or 0) >= capacity:
                    raise ChannelFullError(
                        "the recipient's collaboration inbox is full"
                    )
                await cur.execute(
                    f"""
                    INSERT INTO collab_messages (
                        from_app_id, from_agent, to_target, text, about_ref,
                        reply_to
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING {_MESSAGE_COLUMNS}
                    """,
                    (
                        normalize_app_id(from_app_id),
                        normalize_agent(from_agent),
                        target,
                        text,
                        about,
                        str(reply_to or "")[:200],
                    ),
                )
                return _message(await cur.fetchone())  # type: ignore[return-value]


async def inbox(
    *,
    to_target: str,
    limit: int = 20,
    peek: bool = False,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Read unread messages, consuming them unless ``peek`` is true."""
    target = _target(to_target)
    bounded = max(1, min(50, int(limit)))
    async with get_db_connection(conn) as db:
        transaction = db.transaction() if conn is None else nullcontext()
        async with transaction:
            async with db.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT {_MESSAGE_COLUMNS}
                    FROM collab_messages
                    WHERE to_target = %s AND read_at IS NULL
                    ORDER BY created_at, message_id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (target, bounded),
                )
                rows = await cur.fetchall()
                messages = [_message(row) for row in rows if row]
                if messages and not peek:
                    ids = [str(row["message_id"]) for row in rows if row]
                    await cur.execute(
                        """
                        UPDATE collab_messages
                        SET read_at = NOW()
                        WHERE message_id = ANY(%s) AND read_at IS NULL
                        """,
                        (ids,),
                    )
                return messages  # type: ignore[return-value]


async def message_count(
    *, to_target: str, unread_only: bool = True, conn: Any = None
) -> int:
    target = _target(to_target)
    condition = " AND read_at IS NULL" if unread_only else ""
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT COUNT(*) AS count FROM collab_messages WHERE to_target = %s{condition}",
                (target,),
            )
            row = await cur.fetchone() or {}
            return int(row.get("count") or 0)


async def mark_read(message_id: str, *, conn: Any = None) -> bool:
    async with get_db_connection(conn) as db:
        async with db.cursor() as cur:
            await cur.execute(
                "UPDATE collab_messages SET read_at = COALESCE(read_at, NOW()) "
                "WHERE message_id = %s",
                (message_id,),
            )
            return cur.rowcount > 0


__all__ = ["inbox", "mark_read", "message_count", "send_message"]
