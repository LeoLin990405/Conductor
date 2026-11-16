"""Handoff rows: manifests of files one agent hands to another."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection

_HANDOFF_COLUMNS = """
    handoff_id, ticket_id, user_id, producer, consumer, purpose, status,
    files, verify_error, created_at, updated_at, verified_at
"""


def _handoff(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("handoff_id", "ticket_id"):
        if out.get(key):
            out[key] = str(out[key])
    out["files"] = list(out.get("files") or [])
    return out


async def insert_handoff(
    cur: Any,
    *,
    ticket_id: str,
    user_id: str,
    producer: str,
    consumer: str,
    purpose: str,
    files: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Insert one handoff on the caller's cursor/transaction.

    Kept cursor-local so a ticket transition and the handoff it owes can commit
    atomically, mirroring :func:`autonomy.effects.insert_effect`.
    """
    await cur.execute(
        f"""
        INSERT INTO autonomy_ticket_handoffs (
            ticket_id, user_id, producer, consumer, purpose, files
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING {_HANDOFF_COLUMNS}
        """,
        (ticket_id, user_id, producer, consumer, purpose, Json(files)),
    )
    return _handoff(await cur.fetchone())


async def create_handoff(
    *,
    ticket_id: str,
    user_id: str,
    producer: str,
    consumer: str,
    purpose: str,
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            row = await insert_handoff(
                cur,
                ticket_id=ticket_id,
                user_id=user_id,
                producer=producer,
                consumer=consumer,
                purpose=purpose,
                files=files,
            )
            return row  # type: ignore[return-value]


async def get_handoff(handoff_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_HANDOFF_COLUMNS} FROM autonomy_ticket_handoffs "
                "WHERE handoff_id = %s",
                (handoff_id,),
            )
            return _handoff(await cur.fetchone())


async def list_handoffs(
    ticket_id: str, *, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_HANDOFF_COLUMNS}
                FROM autonomy_ticket_handoffs
                WHERE ticket_id = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (ticket_id, limit, offset),
            )
            return [_handoff(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


async def settle_handoff(
    handoff_id: str,
    *,
    status: str,
    verify_error: str = "",
    expected_status: str = "registered",
) -> dict[str, Any] | None:
    """Settle one handoff, fenced by its current status.

    Only a ``registered`` row can settle, so a retry of the same verify (or a
    stale owner racing a newer one) is a no-op rather than a flip.
    """
    if status not in {"verified", "rejected"}:
        raise ValueError("handoff status must settle to verified or rejected")
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE autonomy_ticket_handoffs
                SET status = %s,
                    verify_error = %s,
                    verified_at = CASE WHEN %s = 'verified' THEN NOW() ELSE NULL END,
                    updated_at = NOW()
                WHERE handoff_id = %s AND status = %s
                RETURNING {_HANDOFF_COLUMNS}
                """,
                (
                    status,
                    (verify_error or "")[:2000],
                    status,
                    handoff_id,
                    expected_status,
                ),
            )
            return _handoff(await cur.fetchone())
