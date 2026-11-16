"""Durable effect queue for the autonomy layer.

Rows are written inside the same transaction as the state change that owes the
work, then leased by any backend worker with ``FOR UPDATE SKIP LOCKED``. The
lease is the crash recovery path: doing the effect is idempotent and the row is
offered again once its lease expires. No process-memory registry is truth.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection

# A handler is expected to finish in seconds; a route/turn handler should only
# enqueue the next durable step, never wait for a whole agent turn.
DEFAULT_LEASE_SECONDS = 60.0
DEFAULT_MAX_ATTEMPTS = 6



async def enqueue_effect(
    *,
    user_id: str,
    kind: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    due_at: datetime | None = None,
    conn=None,
) -> bool:
    """Insert one effect, returning False when its idempotency key exists.

    A retry of the same request must not enqueue a second effect, so callers
    derive ``idempotency_key`` from the durable identity (trigger id, ticket id
    plus input id, and so on), never from a fresh uuid. Pass ``conn`` to write
    on a caller's transaction — a state change and the effect it owes commit
    atomically.
    """
    async with get_db_connection(conn) as db:
        async with db.cursor() as cur:
            return await insert_effect(
                cur,
                user_id=user_id,
                kind=kind,
                idempotency_key=idempotency_key,
                payload=payload,
                due_at=due_at,
            )


async def insert_effect(
    cur: Any,
    *,
    user_id: str,
    kind: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    due_at: datetime | None = None,
) -> bool:
    """Insert an effect on the caller's cursor/transaction.

    Kept separate from :func:`enqueue_effect` because a ticket transition and
    the effect it owes must commit atomically.
    """
    await cur.execute(
        """
        INSERT INTO autonomy_effects (
            user_id, kind, idempotency_key, payload, due_at
        )
        VALUES (%s, %s, %s, %s, COALESCE(%s, NOW()))
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING effect_id
        """,
        (user_id, kind, idempotency_key, Json(payload or {}), due_at),
    )
    return await cur.fetchone() is not None


async def park_exhausted_effects(
    *, max_attempts: int = DEFAULT_MAX_ATTEMPTS
) -> list[dict[str, Any]]:
    """Mark expired leases that already burned their attempts as dead.

    A worker that crashes never nacks its row. Without this pass the row would
    stay claimed forever and never be offered again nor become visible as a
    problem. Executors must be idempotent, so a lease expiring just after the
    effect committed simply runs it again; only the attempt ceiling parks it.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE autonomy_effects
                SET status = 'dead',
                    lease_until = NULL,
                    updated_at = NOW()
                WHERE status = 'claimed'
                  AND lease_until <= NOW()
                  AND attempts >= %s
                RETURNING effect_id, kind, idempotency_key, attempts, last_error
                """,
                (max_attempts,),
            )
            return [dict(row) for row in await cur.fetchall()]


async def claim_due_effects(
    *,
    limit: int = 20,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[dict[str, Any]]:
    """Lease due or expired effects for one worker.

    ``clock_timestamp()`` is intentional: a transaction that waited on a hot
    row must not commit a lease whose runway was consumed before it started.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    WITH due AS (
                        SELECT effect_id
                        FROM autonomy_effects
                        WHERE (
                                (status = 'pending' AND due_at <= clock_timestamp())
                                OR (
                                    status = 'claimed'
                                    AND lease_until <= clock_timestamp()
                                    AND attempts < %s
                                )
                            )
                        ORDER BY due_at, created_at, effect_id
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE autonomy_effects e
                    SET status = 'claimed',
                        attempts = e.attempts + 1,
                        lease_until = clock_timestamp()
                            + make_interval(secs => %s),
                        updated_at = clock_timestamp()
                    FROM due
                    WHERE e.effect_id = due.effect_id
                    RETURNING e.effect_id, e.user_id, e.kind,
                              e.idempotency_key, e.payload, e.attempts,
                              e.due_at, e.lease_until
                    """,
                    (max_attempts, limit, float(lease_seconds)),
                )
                return [dict(row) for row in await cur.fetchall()]


async def ack_effect(*, effect_id: str, attempts: int) -> bool:
    """Settle a claimed effect. ``attempts`` fences a stale former owner."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_effects
                SET status = 'done',
                    lease_until = NULL,
                    updated_at = NOW()
                WHERE effect_id = %s
                  AND status = 'claimed'
                  AND attempts = %s
                """,
                (effect_id, attempts),
            )
            return cur.rowcount > 0


async def nack_effect(
    *,
    effect_id: str,
    attempts: int,
    error: str,
    retry_delay_seconds: float = 5.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> bool:
    """Retry a claimed effect with backoff, or park it dead at the ceiling."""
    delay = max(0.0, float(retry_delay_seconds))
    next_status = "dead" if attempts >= max_attempts else "pending"
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_effects
                SET status = %s,
                    lease_until = NULL,
                    due_at = CASE
                        WHEN %s = 'pending' THEN clock_timestamp()
                            + make_interval(secs => %s)
                        ELSE due_at
                    END,
                    last_error = %s,
                    updated_at = NOW()
                WHERE effect_id = %s
                  AND status = 'claimed'
                  AND attempts = %s
                """,
                (
                    next_status,
                    next_status,
                    delay,
                    (error or "")[:2000],
                    effect_id,
                    attempts,
                ),
            )
            return cur.rowcount > 0


async def defer_effect(
    *, effect_id: str, attempts: int, delay_seconds: float
) -> bool:
    """Put a claimed effect back to pending, due later, with its attempt unspent.

    For work that cannot be done yet but has not failed, such as a launch that
    found every Worker slot taken. A nack would count each look against the
    effect's few attempts and dead-letter a launch that merely waited behind a
    long Worker, so the claim's attempt is given back and waiting can last as
    long as it has to. ``attempts`` fences a stale former owner, as in ack.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_effects
                SET status = 'pending',
                    lease_until = NULL,
                    attempts = GREATEST(attempts - 1, 0),
                    due_at = clock_timestamp() + make_interval(secs => %s),
                    updated_at = clock_timestamp()
                WHERE effect_id = %s
                  AND status = 'claimed'
                  AND attempts = %s
                """,
                (max(0.0, float(delay_seconds)), effect_id, attempts),
            )
            return cur.rowcount > 0


async def has_newer_live_effect(*, effect_id: str) -> bool:
    """Whether a newer effect of this kind for the same Ticket is pending or claimed.

    A Ticket can carry several effects of one kind (the one made with it, the
    recovery sweep's, one per owner requeue), and each of them would otherwise
    wait on its own. Ordering by creation, then id, leaves exactly one with no
    newer sibling, so every other one can end and that one keeps waiting. The
    newest is the one that stays because it is the one that was made for the
    Ticket's latest state; an older sibling still tearing down a previous run
    must not be mistaken for the one that serves it. Claimed siblings count,
    since effects made together are leased together and would otherwise never
    see each other pending.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM autonomy_effects me
                    JOIN autonomy_effects newer
                      ON newer.kind = me.kind
                     AND newer.payload->>'ticket_id' = me.payload->>'ticket_id'
                     AND newer.status IN ('pending', 'claimed')
                     AND (newer.created_at, newer.effect_id)
                         > (me.created_at, me.effect_id)
                    WHERE me.effect_id = %s
                )
                """,
                (effect_id,),
            )
            return bool((await cur.fetchone())[0])


async def dead_letter_effect(*, effect_id: str, attempts: int, error: str) -> bool:
    """Park a permanently invalid effect, fenced against a stale lease owner."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_effects
                SET status = 'dead', lease_until = NULL, last_error = %s,
                    updated_at = clock_timestamp()
                WHERE effect_id = %s AND status = 'claimed' AND attempts = %s
                """,
                ((error or "")[:2000], effect_id, attempts),
            )
            return cur.rowcount > 0


async def extend_effect_lease(
    *, effect_id: str, attempts: int, lease_seconds: float = DEFAULT_LEASE_SECONDS
) -> bool:
    """Extend a claimed row while a short follow-up step still runs.

    The queue does not expect long handlers; this exists for a handler that must
    make one more bounded call (for example a source's pagination) before it can
    settle the row.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_effects
                SET lease_until = clock_timestamp()
                        + make_interval(secs => %s),
                    updated_at = clock_timestamp()
                WHERE effect_id = %s
                  AND status = 'claimed'
                  AND attempts = %s
                """,
                (float(lease_seconds), effect_id, attempts),
            )
            return cur.rowcount > 0


def backoff_seconds(attempts: int) -> float:
    """Bounded exponential backoff for a failed effect."""
    return min(300.0, 5.0 * (2 ** max(0, attempts - 1)))
