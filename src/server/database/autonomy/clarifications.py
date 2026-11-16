"""Clarification rows: the gate's stored draft for an unclear Ticket."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.autonomy import effects as effect_db
from src.server.database.autonomy import tickets as ticket_db
from src.server.database.pool import get_db_connection

_COLUMNS = """
    clarification_id, ticket_id, user_id, original_prompt, clarity_score,
    expanded_request, assumptions, open_questions, status, confirmed_text,
    created_at, resolved_at
"""


def _row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("clarification_id", "ticket_id"):
        if out.get(key):
            out[key] = str(out[key])
    out["assumptions"] = list(out.get("assumptions") or [])
    out["open_questions"] = list(out.get("open_questions") or [])
    return out


async def create_clarification(
    *,
    ticket_id: str,
    user_id: str,
    original_prompt: str,
    clarity_score: float,
    expanded_request: str,
    assumptions: list[str],
    open_questions: list[str],
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO autonomy_ticket_clarifications (
                    ticket_id, user_id, original_prompt, clarity_score,
                    expanded_request, assumptions, open_questions
                )
                SELECT %s, %s, %s, %s, %s, %s, %s
                FROM autonomy_tickets
                WHERE ticket_id = %s
                  AND status = 'awaiting_clarification'
                  AND COALESCE(mandate->>'clarified', 'false') <> 'true'
                ON CONFLICT (ticket_id) WHERE status = 'awaiting' DO NOTHING
                RETURNING {_COLUMNS}
                """,
                (
                    ticket_id,
                    user_id,
                    original_prompt,
                    clarity_score,
                    expanded_request,
                    Json(list(assumptions)),
                    Json(list(open_questions)),
                    ticket_id,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                await cur.execute(
                    f"""
                    SELECT {_COLUMNS}
                    FROM autonomy_ticket_clarifications
                    WHERE ticket_id = %s AND status = 'awaiting'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (ticket_id,),
                )
                row = await cur.fetchone()
            return _row(row)  # type: ignore[return-value]


async def get_clarification(clarification_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM autonomy_ticket_clarifications
                WHERE clarification_id = %s
                """,
                (clarification_id,),
            )
            return _row(await cur.fetchone())


async def get_active_clarification(ticket_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM autonomy_ticket_clarifications
                WHERE ticket_id = %s AND status = 'awaiting'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (ticket_id,),
            )
            return _row(await cur.fetchone())


async def resolve_clarification(
    clarification_id: str, *, status: str, confirmed_text: str = ""
) -> dict[str, Any] | None:
    if status not in {"confirmed", "dismissed"}:
        raise ValueError("status must be confirmed or dismissed")
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE autonomy_ticket_clarifications
                SET status = %s,
                    confirmed_text = %s,
                    resolved_at = NOW()
                WHERE clarification_id = %s AND status = 'awaiting'
                RETURNING {_COLUMNS}
                """,
                (status, confirmed_text, clarification_id),
            )
            return _row(await cur.fetchone())


async def confirm_ticket_clarification(
    *, ticket_id: str, text: str, enqueue_launch: bool
) -> dict[str, Any] | None:
    """Commit the answer, Ticket release, draft resolution, and owed launch."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT ticket_id, user_id, status, mandate FROM autonomy_tickets "
                    "WHERE ticket_id = %s FOR UPDATE",
                    (ticket_id,),
                )
                ticket = await cur.fetchone()
                if ticket is None or ticket["status"] != "awaiting_clarification":
                    return None
                await cur.execute(
                    f"""SELECT {_COLUMNS}
                    FROM autonomy_ticket_clarifications
                    WHERE ticket_id = %s AND status = 'awaiting'
                    FOR UPDATE""",
                    (ticket_id,),
                )
                clarification = await cur.fetchone()
                if clarification is None:
                    return None
                mandate = dict(ticket.get("mandate") or {})
                mandate["goal"] = text[:1500]
                mandate["clarified"] = True
                await cur.execute(
                    "INSERT INTO autonomy_ticket_inputs (ticket_id, trigger_id, kind, text) "
                    "VALUES (%s, NULL, 'answer', %s)",
                    (ticket_id, text),
                )
                await cur.execute(
                    """UPDATE autonomy_tickets
                    SET scope = %s, mandate = %s, status = 'queued', attention = NULL,
                        updated_at = NOW(), revision = revision + 1
                    WHERE ticket_id = %s AND status = 'awaiting_clarification'
                    RETURNING ticket_id, user_id, workspace_id, title, scope, status,
                              mandate, policy_version, origin_trigger_id,
                              origin_thread_key, router_thread_id, worker_thread_id,
                              worker_run_id, worker_provider, worker_model,
                              worker_daemon_id, attempts, attention, summary, error,
                              revision, created_at, updated_at, started_at, ended_at""",
                    (text, Json(mandate), ticket_id),
                )
                updated_ticket = await cur.fetchone()
                if updated_ticket is None:
                    return None
                await cur.execute(
                    """UPDATE autonomy_ticket_clarifications
                    SET status = 'confirmed', confirmed_text = %s, resolved_at = NOW()
                    WHERE clarification_id = %s AND status = 'awaiting'""",
                    (text, clarification["clarification_id"]),
                )
                if cur.rowcount != 1:
                    return None
                if enqueue_launch:
                    await effect_db.insert_effect(
                        cur,
                        user_id=str(ticket["user_id"]),
                        kind="launch_worker",
                        idempotency_key=(
                            f"autonomy:launch:{ticket_id}:clarified:"
                            f"{clarification['clarification_id']}"
                        ),
                        payload={"ticket_id": ticket_id},
                    )
                return {
                    "ticket": ticket_db._ticket(updated_ticket),
                    "clarification": _row(clarification),
                }


async def reject_ticket_clarification(
    *, ticket_id: str, enqueue_launch: bool
) -> dict[str, Any] | None:
    """Keep the original prompt as the goal; release the parked Ticket.

    The mirror of ``confirm_ticket_clarification`` for a declined rewrite: the
    stored draft is dismissed, the request runs as first written, and the
    launch the hold withheld is owed again.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT ticket_id, user_id, status, mandate FROM autonomy_tickets "
                    "WHERE ticket_id = %s FOR UPDATE",
                    (ticket_id,),
                )
                ticket = await cur.fetchone()
                if ticket is None or ticket["status"] != "awaiting_clarification":
                    return None
                await cur.execute(
                    f"""SELECT {_COLUMNS}
                    FROM autonomy_ticket_clarifications
                    WHERE ticket_id = %s AND status = 'awaiting'
                    FOR UPDATE""",
                    (ticket_id,),
                )
                clarification = await cur.fetchone()
                if clarification is None:
                    return None
                text = str(clarification["original_prompt"] or "")
                if not text.strip():
                    return None
                mandate = dict(ticket.get("mandate") or {})
                mandate["goal"] = text[:1500]
                mandate["clarified"] = True
                await cur.execute(
                    "INSERT INTO autonomy_ticket_inputs (ticket_id, trigger_id, kind, text) "
                    "VALUES (%s, NULL, 'answer', %s)",
                    (ticket_id, text),
                )
                await cur.execute(
                    """UPDATE autonomy_tickets
                    SET scope = %s, mandate = %s, status = 'queued', attention = NULL,
                        updated_at = NOW(), revision = revision + 1
                    WHERE ticket_id = %s AND status = 'awaiting_clarification'
                    RETURNING ticket_id, user_id, workspace_id, title, scope, status,
                              mandate, policy_version, origin_trigger_id,
                              origin_thread_key, router_thread_id, worker_thread_id,
                              worker_run_id, worker_provider, worker_model,
                              worker_daemon_id, attempts, attention, summary, error,
                              revision, created_at, updated_at, started_at, ended_at""",
                    (text, Json(mandate), ticket_id),
                )
                updated_ticket = await cur.fetchone()
                if updated_ticket is None:
                    return None
                await cur.execute(
                    """UPDATE autonomy_ticket_clarifications
                    SET status = 'dismissed', confirmed_text = %s, resolved_at = NOW()
                    WHERE clarification_id = %s AND status = 'awaiting'""",
                    (text, clarification["clarification_id"]),
                )
                if cur.rowcount != 1:
                    return None
                if enqueue_launch:
                    await effect_db.insert_effect(
                        cur,
                        user_id=str(ticket["user_id"]),
                        kind="launch_worker",
                        idempotency_key=(
                            f"autonomy:launch:{ticket_id}:rejected:"
                            f"{clarification['clarification_id']}"
                        ),
                        payload={"ticket_id": ticket_id},
                    )
                return {
                    "ticket": ticket_db._ticket(updated_ticket),
                    "clarification": _row(clarification),
                }
