"""Durable operator notifications, stored as finished effect rows.

There is no notification table. A row in ``autonomy_effects`` with kind
``user_notification`` and status ``done`` is an inbox entry: the effect runner
only claims ``pending`` and expired ``claimed`` rows, and the pipeline metrics
ignore ``done``. The unique ``idempotency_key`` is the de-duplication key, so
two workers cannot insert the same alert twice.

These rows are the inbox. A sweep that deletes finished work effects must
leave ``user_notification`` rows alone.
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database import pool

# Not an effect the runner executes. Status ``done`` keeps it out of the claim.
EFFECT_KIND = "user_notification"

_TICKET_COLUMNS = """
    ticket_id::text AS ticket_id,
    user_id,
    title,
    status,
    COALESCE(attention, '{}'::jsonb) AS attention,
    COALESCE(error, '') AS error,
    COALESCE(worker_provider, '') AS worker_provider,
    EXTRACT(EPOCH FROM (NOW() - CASE
        WHEN status = 'running' THEN COALESCE(started_at, updated_at)
        ELSE updated_at
    END))::int AS age_seconds
"""

# The root Ticket is the operator's own session. It is not an alert.
_NOT_ROOT = """
    AND COALESCE(mandate->>'kind', '') <> 'main_agent_root'
    AND mandate->>'orchestration_only' IS DISTINCT FROM 'true'
"""


def _rows(fetched: list[Any] | None) -> list[dict[str, Any]]:
    return [dict(row) for row in fetched or [] if isinstance(row, dict)]


async def insert_notification(
    *,
    user_id: str,
    dedup_key: str,
    payload: dict[str, Any],
) -> bool:
    """Insert one unread notification. False when the dedup key already exists."""
    if not str(user_id or "").strip() or not str(dedup_key or "").strip():
        return False
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO autonomy_effects (
                    user_id, kind, idempotency_key, payload, status
                )
                VALUES (%s, %s, %s, %s, 'done')
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING effect_id
                """,
                (user_id, EFFECT_KIND, dedup_key, Json(payload)),
            )
            return await cur.fetchone() is not None


async def count_unread(user_id: str) -> int:
    """Unread inbox rows for one user. A missing read flag still counts."""
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*)
                FROM autonomy_effects
                WHERE user_id = %s
                  AND kind = %s
                  AND COALESCE(payload->>'read', 'false') <> 'true'
                """,
                (user_id, EFFECT_KIND),
            )
            row = await cur.fetchone()
    if not row:
        return 0
    try:
        return int(row[0] or 0)
    except (TypeError, ValueError):
        return 0


async def retain_notifications(notification_kind: str, active_keys: list[str]) -> int:
    """Drop inbox rows of this kind whose condition is no longer active.

    An empty ``active_keys`` clears the kind: nothing is in that condition
    anymore, so the badge must fall. Keys are the idempotency keys.
    """
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM autonomy_effects
                WHERE kind = %s
                  AND payload->>'notification_kind' = %s
                  AND NOT (idempotency_key = ANY(%s))
                """,
                (EFFECT_KIND, notification_kind, list(active_keys)),
            )
            return int(cur.rowcount or 0)


async def list_stuck_tickets(
    *,
    min_seconds: int,
    limit: int,
    offset: int = 0,
    running_seconds: int | None = None,
) -> list[dict[str, Any]]:
    """One page of open Tickets that have not moved within their threshold.

    A queued, waiting, or blocked Ticket is stuck after ``min_seconds`` of no
    write. A running Ticket's clock is ``started_at``, so it is stuck only
    after ``running_seconds`` (a run that is still working is not quiet just
    because no row changed); ``None`` leaves running Tickets out. A Ticket
    parked on an exhausted quota is the worker-quota alert, not a stall.

    ``ticket_id`` is the tie-break so a later page cannot repeat or skip a row
    that shares ``updated_at`` with the page before it.
    """
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_TICKET_COLUMNS}
                FROM autonomy_tickets
                WHERE status IN ('queued', 'waiting', 'running', 'blocked')
                  {_NOT_ROOT}
                  AND COALESCE(attention->>'reason', '') NOT IN (
                      'quota_exhausted', 'quota_retries_exhausted'
                  )
                  AND (
                    (status <> 'running'
                     AND updated_at < NOW() - (%s::int * INTERVAL '1 second'))
                    OR (status = 'running'
                     AND COALESCE(started_at, updated_at)
                         < NOW() - (%s::int * INTERVAL '1 second'))
                  )
                ORDER BY updated_at ASC, ticket_id ASC
                LIMIT %s OFFSET %s
                """,
                (
                    int(min_seconds),
                    None if running_seconds is None else int(running_seconds),
                    int(limit),
                    int(offset),
                ),
            )
            return _rows(await cur.fetchall())


async def list_needs_user(
    *, limit: int, offset: int = 0
) -> list[dict[str, Any]]:
    """One page of open Tickets whose attention is a person, not a quota park.

    ``awaiting_clarification`` is included. The clarification gate and the
    prompt refiner store the confirmation question on that status, so a
    filter that stops at queued, waiting, running, and blocked never sees
    those Tickets and they get no inbox row.
    """
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_TICKET_COLUMNS}
                FROM autonomy_tickets
                WHERE attention IS NOT NULL
                  AND status IN (
                      'queued', 'waiting', 'running', 'blocked',
                      'awaiting_clarification'
                  )
                  AND COALESCE(attention->>'reason', '') NOT IN (
                      'quota_exhausted', 'quota_retries_exhausted'
                  )
                  {_NOT_ROOT}
                ORDER BY updated_at DESC, ticket_id ASC
                LIMIT %s OFFSET %s
                """,
                (int(limit), int(offset)),
            )
            return _rows(await cur.fetchall())


async def list_failures(
    *, limit: int, offset: int = 0, within_seconds: int | None = None
) -> list[dict[str, Any]]:
    """One page of Tickets that failed within ``within_seconds``.

    The window keeps the whole failure history out of the inbox: without it
    the first pass after a deploy would banner every Ticket that ever failed
    and the badge would never fall. ``None`` lists every failed Ticket.
    The caller de-duplicates; this only lists.
    """
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_TICKET_COLUMNS}
                FROM autonomy_tickets
                WHERE status = 'failed'
                  {_NOT_ROOT}
                  AND (
                    %s::int IS NULL
                    OR updated_at >= NOW() - (%s::int * INTERVAL '1 second')
                  )
                ORDER BY updated_at DESC, ticket_id ASC
                LIMIT %s OFFSET %s
                """,
                (
                    None if within_seconds is None else int(within_seconds),
                    None if within_seconds is None else int(within_seconds),
                    int(limit),
                    int(offset),
                ),
            )
            return _rows(await cur.fetchall())


async def list_quota_blocked(
    *, limit: int, offset: int = 0
) -> list[dict[str, Any]]:
    """One page of Tickets parked because a worker provider is out of quota."""
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_TICKET_COLUMNS}
                FROM autonomy_tickets
                WHERE status NOT IN ('completed', 'cancelled', 'archived', 'failed')
                  AND COALESCE(attention->>'reason', '') IN (
                      'quota_exhausted', 'quota_retries_exhausted'
                  )
                  {_NOT_ROOT}
                ORDER BY updated_at DESC, ticket_id ASC
                LIMIT %s OFFSET %s
                """,
                (int(limit), int(offset)),
            )
            return _rows(await cur.fetchall())


async def list_dead_daemons(
    *, min_seconds: int, limit: int, offset: int = 0
) -> list[dict[str, Any]]:
    """One page of daemons quiet longer than the threshold.

    A daemon that has never heartbeated is quiet from ``created_at``, so a
    registration that is still inside the window is not dead. ``daemon_id``
    keeps the page boundary stable when two rows share a timestamp.
    """
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT daemon_id::text AS daemon_id,
                       user_id,
                       name,
                       provider,
                       last_seen_at,
                       created_at,
                       EXTRACT(EPOCH FROM (
                           NOW() - COALESCE(last_seen_at, created_at)
                       ))::int AS age_seconds
                FROM autonomy_daemons
                WHERE COALESCE(last_seen_at, created_at)
                      < NOW() - (%s * INTERVAL '1 second')
                ORDER BY COALESCE(last_seen_at, created_at) ASC, daemon_id ASC
                LIMIT %s OFFSET %s
                """,
                (int(min_seconds), int(limit), int(offset)),
            )
            return _rows(await cur.fetchall())


async def list_pending_banners(
    notification_kind: str, limit: int
) -> list[dict[str, Any]]:
    """Oldest inbox rows of this kind that have not been shown as a banner."""
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT idempotency_key AS dedup_key,
                       user_id,
                       payload->>'notification_kind' AS notification_kind,
                       payload->>'title' AS title,
                       payload->>'body' AS body,
                       payload->>'subject_id' AS subject_id
                FROM autonomy_effects
                WHERE kind = %s
                  AND payload->>'notification_kind' = %s
                  AND COALESCE(payload->>'bannered', 'false') <> 'true'
                ORDER BY created_at ASC, effect_id ASC
                LIMIT %s
                """,
                (EFFECT_KIND, notification_kind, int(limit)),
            )
            return _rows(await cur.fetchall())


async def mark_notification_bannered(dedup_key: str) -> None:
    """Remember that this inbox row was shown, so a later pass skips it."""
    if not str(dedup_key or "").strip():
        return
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_effects
                SET payload = jsonb_set(
                        payload, '{bannered}', 'true'::jsonb, true
                    ),
                    updated_at = NOW()
                WHERE kind = %s
                  AND idempotency_key = %s
                """,
                (EFFECT_KIND, dedup_key),
            )
