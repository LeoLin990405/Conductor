"""Thread Evals: batch pending Triggers, ask the Router, record its Decision.

An Eval is the only place where a new Ticket is born. Its accept branch writes
the Ticket, its origin Subscription, and the Decision in one transaction, so a
crash can never leave a Ticket without the record that authorized it.
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.autonomy.tickets import is_open_accepted_speculative_plan
from src.server.database.pool import get_db_connection

_EVAL_COLUMNS = """
    eval_id, thread_key, trigger_ids, status, decision, reason,
    policy_version, started_at, delivered_at, deadline_at, decided_at
"""
_THREAD_COLUMNS = "thread_key, user_id, source_id, pending_trigger_ids, evaluating_eval_id, meta, updated_at"


def _eval(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["eval_id"] = str(out["eval_id"])
    out["trigger_ids"] = list(out.get("trigger_ids") or [])
    return out


async def create_eval(
    *,
    thread_key: str,
    trigger_ids: list[str],
    policy_version: str,
) -> dict[str, Any] | None:
    """Insert the Thread's active Eval; None if another one already holds it."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO autonomy_evals (
                    thread_key, trigger_ids, policy_version, deadline_at
                )
                VALUES (%s, %s, %s, NOW() + INTERVAL '5 minutes')
                ON CONFLICT DO NOTHING
                RETURNING {_EVAL_COLUMNS}
                """,
                (thread_key, list(trigger_ids), policy_version),
            )
            return _eval(await cur.fetchone())


async def start_thread_eval(
    *, thread_key: str, trigger_ids: list[str], policy_version: str
) -> dict[str, Any] | None:
    """Create the active Eval and clear its Trigger batch from the Thread queue."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    INSERT INTO autonomy_evals (
                        thread_key, trigger_ids, policy_version, deadline_at
                    )
                    VALUES (%s, %s, %s, NOW() + INTERVAL '5 minutes')
                    ON CONFLICT DO NOTHING
                    RETURNING {_EVAL_COLUMNS}
                    """,
                    (thread_key, list(trigger_ids), policy_version),
                )
                row = _eval(await cur.fetchone())
                if row is None:
                    return None
                await cur.execute(
                    """
                    UPDATE autonomy_threads
                    SET evaluating_eval_id = %s,
                        pending_trigger_ids = ARRAY(
                            SELECT DISTINCT unnest(pending_trigger_ids)
                            EXCEPT SELECT unnest(%s::TEXT[])
                        ),
                        updated_at = NOW()
                    WHERE thread_key = %s
                    """,
                    (row["eval_id"], list(trigger_ids), thread_key),
                )
                return row


async def get_eval(eval_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_EVAL_COLUMNS} FROM autonomy_evals WHERE eval_id = %s",
                (eval_id,),
            )
            return _eval(await cur.fetchone())


async def active_eval_for_thread(thread_key: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_EVAL_COLUMNS}
                FROM autonomy_evals
                WHERE thread_key = %s AND status IN ('queued', 'delivered')
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (thread_key,),
            )
            return _eval(await cur.fetchone())


async def active_eval_for_trigger(trigger_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_EVAL_COLUMNS}
                FROM autonomy_evals
                WHERE status IN ('queued', 'delivered')
                  AND %s = ANY(trigger_ids)
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (trigger_id,),
            )
            return _eval(await cur.fetchone())


async def list_pending_threads(*, limit: int = 50) -> list[dict[str, Any]]:
    """Threads with queued Triggers and no active Eval."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_THREAD_COLUMNS}
                FROM autonomy_threads
                WHERE cardinality(pending_trigger_ids) > 0
                  AND evaluating_eval_id IS NULL
                ORDER BY updated_at
                LIMIT %s
                """,
                (limit,),
            )
            return [
                {
                    **dict(row),
                    "source_id": str(row["source_id"]),
                    "pending_trigger_ids": list(row["pending_trigger_ids"] or []),
                }
                for row in await cur.fetchall()
            ]


async def list_queued_evals_without_effect(*, limit: int = 100) -> list[dict[str, str]]:
    """Recover the crash gap between Eval creation and durable effect insert."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT e.eval_id, t.user_id
                FROM autonomy_evals e
                JOIN autonomy_threads t ON t.thread_key = e.thread_key
                WHERE e.status = 'queued'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM autonomy_effects x
                      WHERE x.kind = 'router_turn'
                        AND (
                            x.payload ->> 'eval_id' = e.eval_id::text
                            OR COALESCE(x.payload -> 'eval_ids', '[]'::jsonb)
                                ? e.eval_id::text
                        )
                  )
                ORDER BY e.started_at
                LIMIT %s
                """,
                (limit,),
            )
            return [
                {"eval_id": str(row["eval_id"]), "user_id": str(row["user_id"])}
                for row in await cur.fetchall()
            ]


async def prune_pending_triggers(thread_key: str, trigger_ids: list[str]) -> int:
    """Remove policy-rejected queued rows without racing an active Eval."""
    if not trigger_ids:
        return 0
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_threads
                SET pending_trigger_ids = ARRAY(
                        SELECT x FROM unnest(pending_trigger_ids) AS x
                        WHERE NOT (x = ANY(%s::TEXT[]))
                    ),
                    updated_at = NOW()
                WHERE thread_key = %s AND evaluating_eval_id IS NULL
                """,
                (trigger_ids, thread_key),
            )
            return cur.rowcount


async def mark_delivered(eval_id: str) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_evals
                SET status = 'delivered',
                    delivered_at = COALESCE(delivered_at, NOW())
                WHERE eval_id = %s AND status = 'queued'
                """,
                (eval_id,),
            )


async def _settle_decision(cur, *, eval_id: str, decision: str, reason: str):
    """Mark one Eval decided and release its Thread. Caller owns the transaction."""
    await cur.execute(
        f"""
        UPDATE autonomy_evals
        SET status = 'decided', decision = %s, reason = %s,
            decided_at = NOW()
        WHERE eval_id = %s AND status IN ('queued', 'delivered')
        RETURNING {_EVAL_COLUMNS}
        """,
        (decision, reason[:2000], eval_id),
    )
    eval_row = _eval(await cur.fetchone())
    if eval_row is None:
        return None
    await cur.execute(
        """
        UPDATE autonomy_threads
        SET evaluating_eval_id = NULL,
            eval_expiries = 0,
            pending_trigger_ids = ARRAY(
                SELECT DISTINCT unnest(pending_trigger_ids)
                EXCEPT SELECT unnest(%s::TEXT[])
            ),
            updated_at = NOW()
        WHERE thread_key = %s
        """,
        (eval_row["trigger_ids"], eval_row["thread_key"]),
    )
    return eval_row


async def decide_eval(
    *,
    eval_id: str,
    decision: str,
    reason: str,
    ticket: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Record the Router's Decision; an accept also creates its Ticket.

    The caller supplies the fields for a new Ticket. This function owns the
    transaction because the Ticket and the Decision must share it.
    """
    if decision not in ("accept", "attach", "ignore", "stop"):
        raise ValueError(f"invalid decision {decision!r}")
    initial_status = (ticket or {}).get("initial_status") or "queued"
    if initial_status not in {"queued", "awaiting_clarification"}:
        raise ValueError("initial_status must be queued or awaiting_clarification")
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                eval_row = await _settle_decision(
                    cur, eval_id=eval_id, decision=decision, reason=reason
                )
                if eval_row is None:
                    return None
                if decision != "accept" or not ticket:
                    return eval_row
                context_scope_id = str(ticket.get("context_scope_id") or "")
                if not context_scope_id:
                    await cur.execute(
                        """
                        INSERT INTO autonomy_context_scopes (user_id, name, kind)
                        VALUES (%s, 'isolated:' || gen_random_uuid()::text, 'isolated')
                        RETURNING scope_id
                        """,
                        (ticket["user_id"],),
                    )
                    scope_row = await cur.fetchone()
                    context_scope_id = str(scope_row["scope_id"])
                await cur.execute(
                    """
                    INSERT INTO autonomy_tickets (
                        user_id, workspace_id, title, scope, mandate,
                        policy_version, origin_trigger_id, origin_thread_key,
                        router_thread_id, worker_provider, worker_model,
                        worker_daemon_id, status, context_scope_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING ticket_id
                    """,
                    (
                        ticket["user_id"],
                        ticket["workspace_id"],
                        ticket.get("title") or "",
                        ticket.get("scope") or "",
                        Json(ticket.get("mandate") or {}),
                        ticket.get("policy_version") or "",
                        ticket.get("origin_trigger_id"),
                        ticket.get("origin_thread_key") or eval_row["thread_key"],
                        ticket.get("router_thread_id"),
                        ticket.get("worker_provider") or "",
                        ticket.get("worker_model") or "",
                        ticket.get("worker_daemon_id") or None,
                        initial_status,
                        context_scope_id,
                    ),
                )
                ticket_row = await cur.fetchone()
                ticket_id = str(ticket_row["ticket_id"])
                await cur.execute(
                    """
                    INSERT INTO autonomy_subscriptions (ticket_id, thread_key, kind)
                    VALUES (%s, %s, 'own')
                    ON CONFLICT (ticket_id, thread_key) DO NOTHING
                    """,
                    (ticket_id, eval_row["thread_key"]),
                )
                return {**eval_row, "ticket_id": ticket_id}


async def reuse_open_speculative_plan(
    *,
    eval_id: str,
    ticket_id: str,
    user_id: str,
    trigger_id: str,
    text: str,
    reason: str,
) -> dict[str, Any] | None:
    """Attach a signal to an open accepted speculative plan, or attach nothing.

    The Ticket lock, the passive context insert, and the Eval decision share
    one transaction. The plan must still be waiting, speculative, and
    accepted for this user at lock time. A discarded or otherwise settled
    Ticket returns None with no input and no decision, so the caller can
    open a new observe. The context row is stored already delivered: a daemon
    resume claims undelivered inputs, and this record must not restart the
    plan-only Worker or clear its review attention.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT ticket_id, user_id, status, mandate
                    FROM autonomy_tickets
                    WHERE ticket_id = %s
                    FOR UPDATE
                    """,
                    (ticket_id,),
                )
                locked = await cur.fetchone()
                if locked is None:
                    return None
                ticket = dict(locked)
                mandate = ticket.get("mandate")
                ticket["mandate"] = dict(mandate) if isinstance(mandate, dict) else {}
                if not is_open_accepted_speculative_plan(ticket, user_id=user_id):
                    return None
                eval_row = await _settle_decision(
                    cur, eval_id=eval_id, decision="attach", reason=reason
                )
                if eval_row is None:
                    raise ValueError("evaluation already decided")
                await cur.execute(
                    """
                    INSERT INTO autonomy_ticket_inputs
                        (ticket_id, trigger_id, kind, text, delivered_at)
                    VALUES (%s, %s, 'context', %s, NOW())
                    ON CONFLICT (ticket_id, trigger_id) WHERE trigger_id IS NOT NULL
                    DO UPDATE SET delivered_at = COALESCE(
                        autonomy_ticket_inputs.delivered_at, NOW()
                    )
                    RETURNING input_id, ticket_id, delivered_at
                    """,
                    (ticket_id, trigger_id, text),
                )
                inserted = await cur.fetchone()
                if inserted is None:
                    raise ValueError("passive context was not stored")
                await cur.execute(
                    """
                    UPDATE autonomy_tickets
                    SET revision = revision + 1
                    WHERE ticket_id = %s
                    """,
                    (ticket_id,),
                )
                return {
                    "ticket_id": str(ticket["ticket_id"]),
                    "mandate": ticket["mandate"],
                    "input_id": str(inserted["input_id"]),
                    "delivered_at": inserted.get("delivered_at"),
                    "eval_id": eval_row["eval_id"],
                }


# Same five-minute bound as ROUTER_EVAL_DEADLINE_SECONDS. Inlined so this
# module does not import the service layer.
_NULL_DEADLINE_STALE_AFTER = "5 minutes"

_EXPIRE_RETURNING = "RETURNING eval_id, thread_key, trigger_ids"

# Release only this eval's thread. Clear the pointer when it still names the
# expired eval, when it is already null (triggers were removed and never put
# back), or when it names an eval that is no longer queued/delivered. Do not
# touch a pointer that still names some other queued/delivered eval, and do
# not requeue once the thread has already expired three batches.
_RELEASE_EXPIRED_THREAD = """
UPDATE autonomy_threads
SET eval_expiries = eval_expiries + 1,
    evaluating_eval_id = NULL,
    pending_trigger_ids = CASE
        WHEN eval_expiries + 1 < 3 THEN ARRAY(
            SELECT DISTINCT x
            FROM unnest(pending_trigger_ids || %s::TEXT[]) x
        )
        ELSE pending_trigger_ids
    END,
    updated_at = NOW()
WHERE thread_key = %s
  AND (
    evaluating_eval_id = %s
    OR evaluating_eval_id IS NULL
    OR (
      evaluating_eval_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1
        FROM autonomy_evals pointed
        WHERE pointed.eval_id = autonomy_threads.evaluating_eval_id
          AND pointed.status IN ('queued', 'delivered')
      )
    )
  )
RETURNING eval_expiries
"""


async def _release_expired_thread(cur, row: dict[str, Any]) -> None:
    eval_id = str(row["eval_id"])
    thread_key = str(row["thread_key"])
    trigger_ids = list(row["trigger_ids"] or [])
    await cur.execute(
        _RELEASE_EXPIRED_THREAD,
        (trigger_ids, thread_key, eval_id),
    )
    updated = await cur.fetchone()
    if updated and int(updated["eval_expiries"] or 0) >= 3:
        await cur.execute(
            "UPDATE autonomy_evals SET reason = %s WHERE eval_id = %s",
            ("router unavailable after 3 attempts", eval_id),
        )


async def expire_stale_evals() -> list[str]:
    """Expire undecided Evals; retry twice, then leave the batch unavailable.

    Returns the expired eval ids so the caller can cancel each eval's own
    Router run. A queued or delivered eval that still has time left is not
    included.

    The first two expiries requeue the Trigger batch and let the next pass start
    a fresh Eval. The third records the Eval unavailable without requeueing, so
    a broken Router cannot spin on the same batch forever. A Decision resets the
    Thread's counter.

    A queued or delivered eval with ``deadline_at`` still in the future is left
    alone. A row whose deadline was never stamped expires once ``started_at``
    is older than five minutes, which is the restart case for evals created
    before deadlines were written.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    UPDATE autonomy_evals
                    SET status = 'unavailable',
                        reason = 'router deadline expired',
                        decided_at = NOW()
                    WHERE status IN ('queued', 'delivered')
                      AND (
                        (deadline_at IS NOT NULL AND deadline_at < NOW())
                        OR (
                          deadline_at IS NULL
                          AND started_at < NOW() - INTERVAL '{_NULL_DEADLINE_STALE_AFTER}'
                        )
                      )
                    {_EXPIRE_RETURNING}
                    """
                )
                rows = await cur.fetchall()
                expired: list[str] = []
                for row in rows:
                    await _release_expired_thread(cur, row)
                    expired.append(str(row["eval_id"]))
                return expired


async def expire_one(eval_id: str) -> bool:
    """Expire one eval even when its deadline is still a moment in the future.

    The dispatch timeout uses this so a single eval can be closed without
    waiting for ``deadline_at < NOW()``. Already decided or unavailable rows
    are left untouched, including their thread. The thread release and the
    three-attempt requeue rule match ``expire_stale_evals``.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    UPDATE autonomy_evals
                    SET status = 'unavailable',
                        reason = 'router deadline expired',
                        decided_at = NOW()
                    WHERE eval_id = %s
                      AND status IN ('queued', 'delivered')
                    {_EXPIRE_RETURNING}
                    """,
                    (eval_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return False
                await _release_expired_thread(cur, row)
                return True


async def router_effect_batches(eval_ids: list[str]) -> list[dict[str, Any]]:
    """Find durable Router request batches containing any of the given Evals."""
    if not eval_ids:
        return []
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT idempotency_key, payload->'eval_ids' AS eval_ids
                FROM autonomy_effects
                WHERE kind = 'router_turn'
                  AND idempotency_key LIKE 'autonomy:router-batch:%%'
                  AND EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(
                        COALESCE(payload->'eval_ids', '[]'::jsonb)
                    ) AS member(eval_id)
                    WHERE member.eval_id = ANY(%s::text[])
                  )
                ORDER BY created_at""",
                (eval_ids,),
            )
            return [
                {
                    "idempotency_key": str(row["idempotency_key"]),
                    "eval_ids": [str(item) for item in row.get("eval_ids") or []],
                }
                for row in await cur.fetchall()
            ]


async def discard_evals_for_user(user_id: str, *, reason: str) -> int:
    """Discard active Evals after a Policy change and requeue their Triggers.

    The next pass starts a fresh Eval under the new Policy version. Threads stay
    otherwise untouched, so a running Worker is not interrupted by the change.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    WITH affected AS (
                        SELECT e.eval_id, e.thread_key, e.trigger_ids
                        FROM autonomy_evals e
                        JOIN autonomy_threads t ON t.thread_key = e.thread_key
                        WHERE t.user_id = %s
                          AND e.status IN ('queued', 'delivered')
                    ), expired AS (
                        UPDATE autonomy_evals e
                        SET status = 'unavailable',
                            reason = %s,
                            decided_at = NOW()
                        FROM affected a
                        WHERE e.eval_id = a.eval_id
                        RETURNING a.thread_key, a.trigger_ids
                    ), released AS (
                        UPDATE autonomy_threads t
                        SET evaluating_eval_id = NULL,
                            eval_expiries = 0,
                            pending_trigger_ids = ARRAY(
                                SELECT DISTINCT x
                                FROM unnest(t.pending_trigger_ids || e.trigger_ids) x
                            ),
                            updated_at = NOW()
                        FROM expired e
                        WHERE t.thread_key = e.thread_key
                        RETURNING 1
                    )
                    SELECT COUNT(*) FROM expired
                    """,
                    (user_id, reason[:2000]),
                )
                row = await cur.fetchone()
                return int((row or (0,))[0] or 0)


async def list_unavailable_for_user(
    user_id: str, *, limit: int = 20
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT e.eval_id, e.thread_key, e.trigger_ids, e.reason,
                       e.started_at, e.decided_at
                FROM autonomy_evals e
                JOIN autonomy_threads t ON t.thread_key = e.thread_key
                WHERE t.user_id = %s AND e.status = 'unavailable'
                ORDER BY e.decided_at DESC NULLS LAST
                LIMIT %s
                """,
                (user_id, limit),
            )
            return [
                {
                    **dict(row),
                    "eval_id": str(row["eval_id"]),
                    "trigger_ids": list(row.get("trigger_ids") or []),
                }
                for row in await cur.fetchall()
            ]


async def requeue_unavailable(eval_id: str, user_id: str) -> bool:
    """Put an unavailable Eval's Triggers back on its Thread queue."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT e.trigger_ids, e.thread_key
                    FROM autonomy_evals e
                    JOIN autonomy_threads t ON t.thread_key = e.thread_key
                    WHERE e.eval_id = %s AND t.user_id = %s
                      AND e.status = 'unavailable'
                    """,
                    (eval_id, user_id),
                )
                row = await cur.fetchone()
                if row is None:
                    return False
                trigger_ids = list(row["trigger_ids"] or [])
                await cur.execute(
                    """
                    UPDATE autonomy_threads
                    SET pending_trigger_ids = ARRAY(
                            SELECT DISTINCT x
                            FROM unnest(pending_trigger_ids || %s::TEXT[]) x
                        ),
                        eval_expiries = 0,
                        updated_at = NOW()
                    WHERE thread_key = %s
                    """,
                    (trigger_ids, row["thread_key"]),
                )
                await cur.execute(
                    "UPDATE autonomy_evals SET reason = %s WHERE eval_id = %s",
                    ("requeued manually", eval_id),
                )
                return True
