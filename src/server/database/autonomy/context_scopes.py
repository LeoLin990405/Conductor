"""Durable context scopes: the explicit shared/isolated boundary for Tickets.

A Ticket always names one scope. An ``isolated`` scope belongs to exactly one
Ticket; a ``shared`` scope is a deliberate grant that policies which data
classes may cross between its members and, optionally, which other users may
attach a Ticket. Scope membership is the Ticket's ``context_scope_id`` column,
so a query can prove a sharing decision without reconstructing history.
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection

_SCOPE_COLUMNS = """
    scope_id, user_id, name, kind, shared_with_user_ids, policy,
    created_at, updated_at
"""


class ScopeFullError(RuntimeError):
    """The shared scope already holds its policy's maximum participants."""


def _scope(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["scope_id"] = str(out["scope_id"])
    out["shared_with_user_ids"] = list(out.get("shared_with_user_ids") or [])
    out["policy"] = dict(out.get("policy") or {})
    return out


async def create_scope(
    *,
    user_id: str,
    name: str,
    kind: str,
    shared_with_user_ids: list[str] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if kind not in {"isolated", "shared"}:
        raise ValueError("scope kind must be isolated or shared")
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO autonomy_context_scopes (
                    user_id, name, kind, shared_with_user_ids, policy
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING {_SCOPE_COLUMNS}
                """,
                (
                    user_id,
                    name,
                    kind,
                    list(shared_with_user_ids or []),
                    Json(policy or {}),
                ),
            )
            return _scope(await cur.fetchone())  # type: ignore[return-value]


async def get_scope(scope_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_SCOPE_COLUMNS} FROM autonomy_context_scopes "
                "WHERE scope_id = %s",
                (scope_id,),
            )
            return _scope(await cur.fetchone())


async def list_scopes(user_id: str) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_SCOPE_COLUMNS}
                FROM autonomy_context_scopes
                WHERE user_id = %s OR %s = ANY(shared_with_user_ids)
                ORDER BY created_at DESC
                """,
                (user_id, user_id),
            )
            return [_scope(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


async def scope_for_ticket(ticket_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT s.scope_id, s.user_id, s.name, s.kind,
                       s.shared_with_user_ids, s.policy, s.created_at, s.updated_at
                FROM autonomy_context_scopes s
                JOIN autonomy_tickets t ON t.context_scope_id = s.scope_id
                WHERE t.ticket_id = %s
                """,
                (ticket_id,),
            )
            return _scope(await cur.fetchone())


async def create_isolated_scope(*, user_id: str, ticket_id: str) -> dict[str, Any]:
    """Create (or reuse) the closed scope for one Ticket and bind it.

    Older Tickets can predate the backfill, so this is idempotent and also
    repairs a NULL column. A Ticket already in a shared scope is left alone.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO autonomy_context_scopes (user_id, name, kind)
                VALUES (%s, %s, 'isolated')
                ON CONFLICT (user_id, name) DO UPDATE
                    SET updated_at = NOW()
                RETURNING {_SCOPE_COLUMNS}
                """,
                (user_id, f"isolated:{ticket_id}"),
            )
            scope = _scope(await cur.fetchone())
            assert scope is not None
            await cur.execute(
                """
                UPDATE autonomy_tickets
                SET context_scope_id = %s
                WHERE ticket_id = %s
                  AND context_scope_id IS NULL
                """,
                (scope["scope_id"], ticket_id),
            )
            return scope


async def set_ticket_scope(
    *, scope_id: str, ticket_id: str, max_participants: int | None = None
) -> dict[str, Any] | None:
    """Move a Ticket from its isolated scope into ``scope_id``.

    Re-binding a Ticket that is already a shared-scope member is refused so a
    caller cannot silently pull work out of one team boundary into another.
    The scope row is locked for the count and the attach, so concurrent joins
    cannot both pass a cap that only one of them fits. Returns the Ticket row,
    or None when the Ticket does not exist; raises ``ScopeFullError`` when the
    cap is already reached.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT scope_id FROM autonomy_context_scopes "
                    "WHERE scope_id = %s FOR UPDATE",
                    (scope_id,),
                )
                if await cur.fetchone() is None:
                    return None
                await cur.execute(
                    "SELECT ticket_id, user_id, context_scope_id "
                    "FROM autonomy_tickets WHERE ticket_id = %s",
                    (ticket_id,),
                )
                existing = await cur.fetchone()
                if existing is None:
                    return None
                current_scope = str(existing.get("context_scope_id") or "")
                if current_scope == scope_id:
                    return {
                        "ticket_id": str(existing["ticket_id"]),
                        "user_id": str(existing["user_id"]),
                        "context_scope_id": current_scope,
                    }
                if max_participants is not None:
                    await cur.execute(
                        "SELECT COUNT(*) AS members FROM autonomy_tickets "
                        "WHERE context_scope_id = %s",
                        (scope_id,),
                    )
                    members = int((await cur.fetchone() or {}).get("members") or 0)
                    if members >= max_participants:
                        raise ScopeFullError("context scope is full")
                await cur.execute(
                    """
                    UPDATE autonomy_tickets t
                    SET context_scope_id = %s
                    WHERE t.ticket_id = %s
                      AND (
                        t.context_scope_id IS NULL
                        OR EXISTS (
                            SELECT 1 FROM autonomy_context_scopes current
                            WHERE current.scope_id = t.context_scope_id
                              AND current.kind = 'isolated'
                        )
                      )
                    RETURNING ticket_id, user_id, context_scope_id
                    """,
                    (scope_id, ticket_id),
                )
                row = await cur.fetchone()
                if row is not None:
                    return {
                        "ticket_id": str(row["ticket_id"]),
                        "user_id": str(row["user_id"]),
                        "context_scope_id": str(row["context_scope_id"]),
                    }
                return {
                    "ticket_id": str(existing["ticket_id"]),
                    "user_id": str(existing["user_id"]),
                    "context_scope_id": current_scope,
                }


async def list_scope_ticket_ids(scope_id: str) -> list[str]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT ticket_id FROM autonomy_tickets "
                "WHERE context_scope_id = %s ORDER BY created_at",
                (scope_id,),
            )
            return [str(row["ticket_id"]) for row in await cur.fetchall()]


async def list_scope_tickets(scope_id: str) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT ticket_id, user_id, status, origin_thread_key
                FROM autonomy_tickets
                WHERE context_scope_id = %s
                ORDER BY created_at
                """,
                (scope_id,),
            )
            return [
                {
                    **dict(row),
                    "ticket_id": str(row["ticket_id"]),
                    "user_id": str(row["user_id"]),
                }
                for row in await cur.fetchall()
            ]


async def ticket_ids_for_thread(thread_key: str) -> list[str]:
    """Tickets currently subscribed to the Thread, for follow authorization.

    A reassignment keeps the ended subscription row as audit/handoff data, but
    an ended row grants nothing: the former owner must not keep following, or
    reading, Thread work the new owner holds. Only live rows count.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT ticket_id FROM autonomy_subscriptions "
                "WHERE thread_key = %s AND ended_at IS NULL ORDER BY ticket_id",
                (thread_key,),
            )
            return [str(row["ticket_id"]) for row in await cur.fetchall()]
