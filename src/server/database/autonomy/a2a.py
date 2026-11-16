"""Durable A2A agents and messages.

The bearer token is stored only as a digest. A message row, its context-only
Input and its delivery effect are written in one transaction, so a delivered
protocol message always has exactly one Input, one delivery effect, and a
replay can neither produce a second one nor lose the one it already has.
"""

from __future__ import annotations

from typing import Any, Iterable

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.autonomy.effects import insert_effect
from src.server.database.pool import get_db_connection

_AGENT_COLUMNS = """
    agent_id, user_id, ticket_id, name, revoked_at, last_seen_at, created_at
"""

_MESSAGE_COLUMNS = """
    message_id, user_id, sender_agent_id, sender_ticket_id,
    recipient_ticket_id, scope_id, message_key, payload_hash, intent,
    subject, body, allowed_actions, provenance, issued_at, expires_at,
    status, rejection, input_id, created_at
"""


def _agent(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("agent_id", "ticket_id"):
        out[key] = str(out[key])
    return out


def _message(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in (
        "message_id",
        "sender_agent_id",
        "sender_ticket_id",
        "recipient_ticket_id",
        "scope_id",
        "input_id",
    ):
        if out.get(key):
            out[key] = str(out[key])
    out["allowed_actions"] = list(out.get("allowed_actions") or [])
    out["provenance"] = dict(out.get("provenance") or {})
    return out


async def create_agent(
    *,
    user_id: str,
    ticket_id: str,
    name: str,
    token_hash: str,
) -> dict[str, Any]:
    """Register an agent for a Ticket, rotating the token if one exists."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO autonomy_a2a_agents (user_id, ticket_id, name, token_hash)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (ticket_id) DO UPDATE
                    SET token_hash = EXCLUDED.token_hash,
                        name = EXCLUDED.name,
                        revoked_at = NULL
                RETURNING {_AGENT_COLUMNS}
                """,
                (user_id, ticket_id, name, token_hash),
            )
            return _agent(await cur.fetchone())  # type: ignore[return-value]


async def get_agent(agent_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_AGENT_COLUMNS} FROM autonomy_a2a_agents WHERE agent_id = %s",
                (agent_id,),
            )
            return _agent(await cur.fetchone())


async def agent_for_token_hash(token_hash: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_AGENT_COLUMNS}
                FROM autonomy_a2a_agents
                WHERE token_hash = %s AND revoked_at IS NULL
                """,
                (token_hash,),
            )
            return _agent(await cur.fetchone())


async def touch_agent(agent_id: str) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE autonomy_a2a_agents SET last_seen_at = NOW() "
                "WHERE agent_id = %s",
                (agent_id,),
            )


async def revoke_agent(agent_id: str, *, user_id: str) -> bool:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_a2a_agents
                SET revoked_at = COALESCE(revoked_at, NOW())
                WHERE agent_id = %s AND user_id = %s
                """,
                (agent_id, user_id),
            )
            return cur.rowcount > 0


async def list_agents(user_id: str) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_AGENT_COLUMNS}
                FROM autonomy_a2a_agents
                WHERE user_id = %s
                ORDER BY created_at DESC
                """,
                (user_id,),
            )
            return [_agent(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


async def _ensure_delivery_effect(
    cur: Any,
    *,
    user_id: str,
    ticket_id: str,
    input_id: str,
) -> bool:
    """Make sure exactly one delivery effect exists for an Input.

    The effect is keyed by the Input, not by the message id. Two senders may
    reuse one message id, so a key built from the message id alone was
    globally unique against the wrong thing: the second sender's effect was
    silently dropped and the message could never be delivered.
    """
    if not input_id:
        return False
    await cur.execute(
        """
        SELECT 1 FROM autonomy_effects
        WHERE kind = 'deliver_input'
          AND payload->>'input_id' = %s
        LIMIT 1
        """,
        (input_id,),
    )
    if await cur.fetchone() is not None:
        return False
    return await insert_effect(
        cur,
        user_id=user_id,
        kind="deliver_input",
        idempotency_key=f"autonomy:input:{input_id}",
        payload={
            "ticket_id": ticket_id,
            "input_id": input_id,
        },
    )


async def deliver_message(
    *,
    user_id: str,
    sender_agent_id: str,
    sender_ticket_id: str,
    recipient_ticket_id: str,
    scope_id: str,
    message_key: str,
    payload_hash: str,
    intent: str,
    subject: str,
    body: str,
    allowed_actions: list[str],
    provenance: dict[str, Any],
    issued_at: Any,
    expires_at: Any,
    input_text: str,
    delivery_user_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Store one message, its context Input and the delivery effect atomically.

    A replay of the same ``(sender, message_key)`` returns the existing row
    without a second Input or effect, and makes sure the stored message still
    has its delivery effect. The caller compares the hash first.
    ``delivery_user_id`` names the recipient whose Worker should be woken;
    omitting it stores an input for a Ticket that has no live backend Worker.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    INSERT INTO autonomy_a2a_messages (
                        user_id, sender_agent_id, sender_ticket_id,
                        recipient_ticket_id, scope_id, message_key,
                        payload_hash, intent, subject, body, allowed_actions,
                        provenance, issued_at, expires_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (sender_ticket_id, message_key) DO NOTHING
                    RETURNING {_MESSAGE_COLUMNS}
                    """,
                    (
                        user_id,
                        sender_agent_id,
                        sender_ticket_id,
                        recipient_ticket_id,
                        scope_id,
                        message_key,
                        payload_hash,
                        intent,
                        subject,
                        body,
                        list(allowed_actions),
                        Json(provenance),
                        issued_at,
                        expires_at,
                    ),
                )
                row = await cur.fetchone()
                if row is None:
                    await cur.execute(
                        f"""
                        SELECT {_MESSAGE_COLUMNS}
                        FROM autonomy_a2a_messages
                        WHERE sender_ticket_id = %s AND message_key = %s
                        """,
                        (sender_ticket_id, message_key),
                    )
                    existing = await cur.fetchone()
                    if existing is not None and delivery_user_id:
                        # A replay used to stop at the stored message, so a
                        # message whose delivery effect was lost could never
                        # be delivered. The effect is keyed by the Input, so a
                        # message that already has one is left alone.
                        await _ensure_delivery_effect(
                            cur,
                            user_id=delivery_user_id,
                            ticket_id=str(
                                existing.get("recipient_ticket_id")
                                or recipient_ticket_id
                            ),
                            input_id=str(existing.get("input_id") or ""),
                        )
                    return _message(existing), False  # type: ignore[return-value]

                await cur.execute(
                    """
                    INSERT INTO autonomy_ticket_inputs (ticket_id, trigger_id, kind, text)
                    VALUES (%s, NULL, 'context', %s)
                    RETURNING input_id
                    """,
                    (recipient_ticket_id, input_text),
                )
                input_row = await cur.fetchone()
                input_id = str(input_row["input_id"])
                await cur.execute(
                    "UPDATE autonomy_tickets SET revision = revision + 1 "
                    "WHERE ticket_id = %s",
                    (recipient_ticket_id,),
                )
                await cur.execute(
                    "UPDATE autonomy_a2a_messages SET input_id = %s "
                    "WHERE message_id = %s",
                    (input_id, row["message_id"]),
                )
                if delivery_user_id:
                    # The effect commits with the message. A crash between the
                    # two would otherwise leave a replayable message that can
                    # never be delivered because the replay short-circuits.
                    await _ensure_delivery_effect(
                        cur,
                        user_id=delivery_user_id,
                        ticket_id=recipient_ticket_id,
                        input_id=input_id,
                    )
                row = dict(row)
                row["input_id"] = input_id
                return _message(row), True  # type: ignore[return-value]


async def list_messages(
    ticket_id: str, *, limit: int = 50, visible_ticket_ids: Iterable[str]
) -> list[dict[str, Any]]:
    """List a Ticket's messages with the readability filter inside the query.

    A row is returned only when both endpoints sit in ``visible_ticket_ids``.
    Filtering before LIMIT keeps pages full: rows the caller may not read
    never claim a slot and shorten the page.
    """
    visible = list(visible_ticket_ids)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_MESSAGE_COLUMNS}
                FROM autonomy_a2a_messages
                WHERE (recipient_ticket_id = %s OR sender_ticket_id = %s)
                  AND sender_ticket_id = ANY(%s::uuid[])
                  AND recipient_ticket_id = ANY(%s::uuid[])
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (ticket_id, ticket_id, visible, visible, limit),
            )
            return [_message(row) for row in await cur.fetchall() if row]  # type: ignore[misc]
