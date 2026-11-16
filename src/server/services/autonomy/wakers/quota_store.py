"""Quota park/unpark SQL and the provider observation fence.

Lives under wakers/ so the ticket/daemon SQL modules stay with their owners.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Iterable
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection, open_session_connection

# Reasons the quota waker itself writes. A question with any other reason
# belongs to a person and must survive a reset that was planned earlier.
_QUOTA_ATTENTION_REASONS = ("quota_exhausted", "quota_retries_exhausted")
# Session advisory-lock salt for one provider's quota park/unpark applies.
_QUOTA_PROVIDER_LOCK_SALT = 743
# Providers already held on this task. A nested acquire (apply holding the
# lock while continuation enqueue takes it again) must not open a second
# session: Postgres advisory locks are per-connection, so that waits forever.
_QUOTA_PROVIDER_LOCK_HELD: ContextVar[frozenset[str]] = ContextVar(
    "quota_provider_lock_held", default=frozenset()
)


@asynccontextmanager
async def quota_provider_lock(provider: str, *, timeout_seconds: float = 30.0):
    """Serialize quota park/unpark applies for one provider.

    The observation fence on ``apply_quota_schedule`` reads ``autonomy_effects``
    under the UPDATE's snapshot. Without this lock a newer reset can skip a
    still-queued Ticket (no row write) while an older exhaust's UPDATE still
    parks it. Holding the lock across load/plan/save makes the reset's effect
    row visible before the exhaust plans.

    Nested acquires for the same provider on this task are no-ops. Continuation
    enqueue runs under the apply lock and must not open another connection.
    """
    from src.server.services.autonomy.wakers.base import canonical_provider_id

    key = canonical_provider_id(provider)
    if not key:
        yield
        return
    held = _QUOTA_PROVIDER_LOCK_HELD.get()
    if key in held:
        yield
        return
    db = await open_session_connection()
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    token = _QUOTA_PROVIDER_LOCK_HELD.set(held | {key})
    try:
        acquired = False
        while not acquired:
            async with db.cursor() as cur:
                await cur.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, %s))",
                    (f"quota-waker:{key}", _QUOTA_PROVIDER_LOCK_SALT),
                )
                acquired = bool((await cur.fetchone() or (False,))[0])
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("quota provider lock is busy; retry the wake")
            await asyncio.sleep(0.05)
        try:
            yield
        finally:
            async with db.cursor() as cur:
                await cur.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, %s))",
                    (f"quota-waker:{key}", _QUOTA_PROVIDER_LOCK_SALT),
                )
    finally:
        _QUOTA_PROVIDER_LOCK_HELD.reset(token)
        await db.close()


def quota_observation_is_newer(latest_observed_at: str, observed_at: str) -> bool:
    """Whether a stored provider observation is strictly newer than this write.

    Compared as text, the same way the effect row orders ``observed_at``.
    A reset can skip a still-queued Ticket without bumping its revision, so
    the revision fence alone cannot stop the older exhaust.
    """
    latest = str(latest_observed_at or "")
    if not latest:
        return False
    return latest > str(observed_at or "")


def quota_observation_rank_beats(
    latest_rank: tuple[str, str, str],
    write_rank: tuple[str, str, str],
) -> bool:
    """Whether ``latest_rank`` outranks the write under provider history order.

    Order is ``observed_at``, then ``created_at``, then ``effect_id`` — the
    same tuple :func:`src.server.services.autonomy.wakers.quota_history._quota_rank` uses.
    """
    if not any(latest_rank):
        return False
    return tuple(str(part or "") for part in latest_rank) > tuple(
        str(part or "") for part in write_rank
    )


def quota_schedule_fence(
    *,
    status: str,
    revision: int,
    attention: dict[str, Any] | None,
    expected_status: str,
    expected_revision: int,
    replace_attention: bool,
    observed_at: str = "",
    latest_observed_at: str = "",
    write_rank: tuple[str, str, str] | None = None,
    latest_rank: tuple[str, str, str] | None = None,
) -> bool:
    """Whether a quota write still matches the row it was planned against.

    The SQL in :func:`apply_quota_schedule` is the fence that runs in the
    transaction. This predicate is the same rule, so a test can show a reset
    planned before a human question does not clear that question, and an
    older exhaust does not park a Ticket a newer reset already skipped —
    including when both wakes share ``observed_at`` and the reset wins on
    ``created_at`` / ``effect_id``.
    """
    if status != expected_status or revision != expected_revision:
        return False
    if write_rank is not None or latest_rank is not None:
        if quota_observation_rank_beats(
            latest_rank or ("", "", ""), write_rank or ("", "", "")
        ):
            return False
    elif quota_observation_is_newer(latest_observed_at, observed_at):
        return False
    if not replace_attention:
        return True
    if not isinstance(attention, dict):
        return True
    question = str(attention.get("question") or "").strip()
    reason = str(attention.get("reason") or "")
    if question and reason not in _QUOTA_ATTENTION_REASONS:
        return False
    return True


def _quota_uuid(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


async def list_quota_work_items(
    provider_names: Iterable[str],
    *,
    user_id: str = "",
    line_id: str | None = None,
    ticket_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
    after_ticket_id: str | None = None,
) -> list[dict[str, Any]]:
    """Queued, waiting, and blocked Tickets for one provider's quota plan.

    ``line_id`` is not part of the shared ticket column list, so it is
    selected here. An empty user id matches every owner: a local CodexBar
    snapshot is the machine's quota, not one user's. A bad uuid matches
    nothing instead of dropping the filter.

    Pages walk ``ticket_id``, not ``updated_at``. A park rewrites
    ``updated_at``, and a later reset ordered by that column would see the
    rows that were not parked first and never return to the ones that were.
    ``after_ticket_id`` is the last id of the previous page.
    """
    names = sorted({
        str(name).strip().lower()
        for name in provider_names
        if isinstance(name, str) and str(name).strip() and len(str(name).strip()) <= 80
    })
    if not names:
        return []
    if ticket_id is not None and _quota_uuid(ticket_id) is None:
        return []
    if line_id is not None and _quota_uuid(line_id) is None:
        return []
    if after_ticket_id is not None and _quota_uuid(after_ticket_id) is None:
        return []
    bounded = min(1001, max(1, int(limit)))
    start = max(0, int(offset))
    owner = str(user_id or "")
    after = _quota_uuid(after_ticket_id)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT ticket_id, user_id, status, mandate, attention,
                       worker_provider, line_id, revision
                FROM autonomy_tickets
                WHERE status IN ('queued', 'waiting', 'blocked')
                  AND lower(worker_provider) = ANY(%s)
                  AND (%s = '' OR user_id = %s)
                  AND (%s::uuid IS NULL OR line_id = %s::uuid)
                  AND (%s::uuid IS NULL OR ticket_id = %s::uuid)
                  AND (%s::uuid IS NULL OR ticket_id > %s::uuid)
                ORDER BY ticket_id ASC
                LIMIT %s OFFSET %s
                """,
                (
                    names,
                    owner,
                    owner,
                    _quota_uuid(line_id),
                    _quota_uuid(line_id),
                    _quota_uuid(ticket_id),
                    _quota_uuid(ticket_id),
                    after,
                    after,
                    bounded,
                    start,
                ),
            )
            rows = []
            for row in await cur.fetchall():
                item = dict(row)
                item["ticket_id"] = str(item.get("ticket_id") or "")
                item["line_id"] = str(item["line_id"]) if item.get("line_id") else ""
                mandate = item.get("mandate")
                item["mandate"] = dict(mandate) if isinstance(mandate, dict) else {}
                attention = item.get("attention")
                item["attention"] = dict(attention) if isinstance(attention, dict) else None
                rows.append(item)
            return rows


async def apply_quota_schedule(
    ticket_id: str,
    *,
    expected_status: str,
    status: str | None,
    attention: dict[str, Any] | None,
    replace_attention: bool,
    ledger: dict[str, Any],
    expected_revision: int | None = None,
    launch_key: str = "",
    launch_user_id: str = "",
    observed_at: str = "",
    provider: str = "",
    effect_id: str = "",
) -> bool:
    """Write the quota ledger and, when asked, the status and attention.

    The status fence makes a lost race a no-op rather than an effect failure.
    The revision fence and the attention predicate make a reset that was
    planned before another writer added a human question a no-op too: status
    alone would still match ``blocked``. The observation fence covers the
    case revision does not: a newer reset skips a still-queued Ticket, so
    its revision stays put and an older exhaust would otherwise park it
    after the reset is already the latest snapshot. When ``effect_id`` is
    set, the fence uses the full provider-history rank (observed_at,
    created_at, effect_id) so a later-created reset with the same timestamp
    still blocks the older exhaust. Callers serialize one provider's
    enqueue and apply with :func:`quota_provider_lock` so a concurrent
    reset cannot publish during this UPDATE's snapshot. ``attempts`` and
    the worker lease stay as they are. A requeue inserts its launch effect
    on this transaction, because the attempt-scoped launch key may already
    have been acknowledged while the Ticket was parked.
    """
    target = _quota_uuid(ticket_id)
    if target is None or not expected_status:
        return False
    if expected_revision is None or isinstance(expected_revision, bool):
        return False
    from src.server.services.autonomy.wakers.quota_history import QUOTA_GATE_KINDS
    from src.server.services.autonomy.wakers.base import (
        canonical_provider_id,
        provider_spellings,
    )

    canonical = canonical_provider_id(provider)
    spellings = list(provider_spellings(canonical))
    # An empty gate skips the observation check. A known provider always
    # has at least its canonical spelling, so a real quota write is fenced.
    observation_gate = canonical if spellings else ""
    wake_effect = _quota_uuid(effect_id) or ""
    attention_value = Json(attention) if attention is not None else None
    key = str(launch_key or "").strip()
    observed = str(observed_at or "")
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE autonomy_tickets
                    SET mandate = jsonb_set(
                            COALESCE(mandate, '{}'::jsonb),
                            '{quota_waker}',
                            %s,
                            true
                        ),
                        status = COALESCE(%s, status),
                        attention = CASE WHEN %s THEN %s ELSE attention END,
                        updated_at = NOW(),
                        revision = revision + 1
                    WHERE ticket_id = %s
                      AND status = %s
                      AND revision = %s
                      AND (
                        NOT %s
                        OR attention IS NULL
                        OR btrim(COALESCE(attention->>'question', '')) = ''
                        OR COALESCE(attention->>'reason', '') = ANY(%s)
                      )
                      AND (
                        %s = ''
                        OR (
                          %s <> ''
                          AND NOT EXISTS (
                            SELECT 1
                            FROM autonomy_effects newer
                            JOIN autonomy_effects me ON me.effect_id = %s::uuid
                            WHERE newer.kind = 'wake'
                              AND newer.payload->>'source' = 'codexbar'
                              AND newer.payload->>'kind' = ANY(%s)
                              AND lower(COALESCE(newer.payload->>'provider', ''))
                                  = ANY(%s)
                              AND COALESCE(newer.payload->>'quota_after_ticket_id', '') = ''
                              AND (
                                COALESCE(newer.payload->>'observed_at', ''),
                                newer.created_at,
                                newer.effect_id::text
                              ) > (
                                COALESCE(me.payload->>'observed_at', ''),
                                me.created_at,
                                me.effect_id::text
                              )
                          )
                        )
                        OR (
                          %s = ''
                          AND NOT EXISTS (
                            SELECT 1
                            FROM autonomy_effects
                            WHERE kind = 'wake'
                              AND payload->>'source' = 'codexbar'
                              AND payload->>'kind' = ANY(%s)
                              AND lower(COALESCE(payload->>'provider', ''))
                                  = ANY(%s)
                              AND COALESCE(payload->>'observed_at', '') > %s
                          )
                        )
                      )
                    """,
                    (
                        Json(dict(ledger)),
                        status,
                        bool(replace_attention),
                        attention_value,
                        target,
                        expected_status,
                        int(expected_revision),
                        bool(replace_attention),
                        list(_QUOTA_ATTENTION_REASONS),
                        observation_gate,
                        wake_effect,
                        wake_effect,
                        list(QUOTA_GATE_KINDS),
                        spellings,
                        wake_effect,
                        list(QUOTA_GATE_KINDS),
                        spellings,
                        observed,
                    ),
                )
                wrote = cur.rowcount > 0
                if wrote and key:
                    from src.server.database.autonomy.effects import insert_effect

                    await insert_effect(
                        cur,
                        user_id=str(launch_user_id or ""),
                        kind="launch_worker",
                        idempotency_key=key,
                        payload={"ticket_id": target},
                    )
                return wrote
