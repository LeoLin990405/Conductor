"""Lease persistence: compare-and-swap claim, heartbeat, release, expiry.

A lease is the durable evidence that one app currently owns one Ticket. The
partial unique index and the Ticket revision CAS are both enforced in SQL, so
two concurrent claims cannot both succeed even when they arrive on different
server workers.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row

from src.collab.types import AlreadyClaimedError
from src.collab.types import (
    ACTIVE_LEASE_STATES,
    LEASE_SECONDS,
    MAX_LEASE_SECONDS,
    normalize_app_id,
    normalize_agent,
    normalize_task_ref,
)
from src.server.database.pool import get_db_connection

_LEASE_COLUMNS = """
    lease_id, app_id, agent, target_ref, target_kind, ticket_id, line_id,
    revision, state, worktree_path, worktree_branch, worktree_base_sha,
    heartbeat_at, expires_at, max_lease_until, created_at, ended_at,
    end_reason
"""

_REPORTED_COLUMNS = ", ".join(
    f"l.{name.strip()}" for name in _LEASE_COLUMNS.split(",") if name.strip()
)

#: Ticket states a collaboration lease can leave behind while it is live:
#: ``running`` after a claim, and ``waiting`` / ``blocked`` after a non-done
#: ``task_report`` (``COMPLETION_STATUS_TO_TICKET_STATE``), which keeps the
#: lease. When the lease ends without completing, a Ticket in any of these
#: states goes back to ``queued``. ``queued``, ``ready`` and every terminal or
#: human-gated state (``completed``, ``cancelled``, ``failed``, ``archived``,
#: ``awaiting_clarification``) are never touched.
RECOVERABLE_TICKET_STATES = ("running", "waiting", "blocked")
_RECOVERABLE_SQL = ", ".join(f"'{state}'" for state in RECOVERABLE_TICKET_STATES)
#: Ticket states a live holder's report may write.
REPORT_TICKET_STATES = frozenset({"ready", "waiting", "blocked"})


def requeue_cte(source: str, alias: str) -> str:
    """The fenced ``UPDATE`` that returns an ended lease's Ticket to ``queued``.

    Every recovery path (release, revocation, expiry, sweep, handoff expiry
    and decline) uses this one fragment inside the statement that ends the
    lease, so the Ticket and the lease change in the same transaction. The
    fence: the lease is a Ticket lease, the Ticket is still in a recoverable
    state, its revision equals the lease's generation (``revision``, advanced
    only by the holder's own claim or report), and no daemon or cloud worker
    owns it. A lease that ended after a reassignment therefore cannot requeue
    the new owner's work, and a Ticket that is already queued, ready or
    terminal is never requeued twice.
    """
    return f"""
                    UPDATE autonomy_tickets t
                    SET status = 'queued',
                        revision = t.revision + 1,
                        updated_at = NOW()
                    FROM {source} {alias}
                    WHERE {alias}.ticket_id = t.ticket_id
                      AND {alias}.target_kind = 'ticket'
                      AND t.status IN ({_RECOVERABLE_SQL})
                      AND t.revision = {alias}.revision
                      AND t.worker_lease_id IS NULL
                      AND t.worker_run_id IS NULL
                    RETURNING t.ticket_id
    """


def _lease(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("lease_id", "ticket_id"):
        if out.get(key):
            out[key] = str(out[key])
    return out


async def insert_lease(
    *,
    app_id: str,
    agent: str = "",
    target_ref: str,
    target_kind: str,
    revision: int,
    expires_at: datetime,
    max_lease_until: datetime,
    ticket_id: str | None = None,
    line_id: str = "",
    worktree_path: str = "",
    worktree_branch: str = "",
    worktree_base_sha: str = "",
    state: str = "active",
    conn: Any = None,
) -> dict[str, Any] | None:
    """Insert one live lease; a conflicting live target returns ``None``.

    ``expires_at`` is clamped to ``max_lease_until`` so no lease outlives its
    ceiling, whatever clock the caller computed the deadline with.
    """
    if target_kind not in {"ticket", "line"}:
        raise ValueError("target_kind must be ticket or line")
    if state not in ACTIVE_LEASE_STATES:
        raise ValueError("a new lease must be active or handoff_pending")
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO collab_leases (
                    app_id, agent, target_ref, target_kind, ticket_id, line_id,
                    revision, state, worktree_path, worktree_branch,
                    worktree_base_sha, expires_at, max_lease_until
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        LEAST(%s::timestamptz, %s::timestamptz), %s)
                ON CONFLICT DO NOTHING
                RETURNING {_LEASE_COLUMNS}
                """,
                (
                    normalize_app_id(app_id),
                    normalize_agent(agent),
                    normalize_task_ref(target_ref),
                    target_kind,
                    ticket_id,
                    line_id,
                    int(revision),
                    state,
                    worktree_path,
                    worktree_branch,
                    worktree_base_sha,
                    expires_at,
                    max_lease_until,
                    max_lease_until,
                ),
            )
            return _lease(await cur.fetchone())


async def claim_ticket_lease(
    *,
    ticket_id: str,
    app_id: str,
    agent: str = "",
    revision: int,
    lease_seconds: int = LEASE_SECONDS,
    max_lease_seconds: int = MAX_LEASE_SECONDS,
    worktree_path: str = "",
    worktree_branch: str = "",
    worktree_base_sha: str = "",
    conn: Any = None,
) -> dict[str, Any] | None:
    """Claim a queued Ticket in one transaction.

    Pass ``conn`` from :func:`collab_transaction` to join the caller's
    transaction; otherwise this function owns one. The Ticket status change and
    the lease insert commit or roll back together either way.

    The Ticket ``revision`` is the compare-and-swap token. A stale revision or
    a Ticket that is no longer queued (or still carries a daemon or cloud
    owner, which every recovery path fences on) returns ``None``; losing the
    lease insert race raises ``AlreadyClaimedError`` and rolls the Ticket
    write back.
    """
    if not 1 <= int(lease_seconds) <= int(max_lease_seconds):
        raise ValueError("lease_seconds must be positive and no longer than the ceiling")
    app = normalize_app_id(app_id)
    agent_label = normalize_agent(agent)
    async with get_db_connection(conn) as db:
        # A caller that passes ``conn`` owns the transaction (see
        # ``collab_transaction``); otherwise open one here so the status change
        # and the lease insert commit or roll back together.
        transaction = db.transaction() if conn is None else nullcontext()
        async with transaction:
            async with db.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    UPDATE autonomy_tickets
                    SET status = 'running', revision = revision + 1,
                        updated_at = NOW()
                    WHERE ticket_id = %s
                      AND revision = %s
                      AND status = 'queued'
                      AND worker_lease_id IS NULL
                      AND worker_run_id IS NULL
                    RETURNING ticket_id, revision
                    """,
                    (ticket_id, int(revision)),
                )
                ticket = await cur.fetchone()
                if ticket is None:
                    return None
                await cur.execute(
                    f"""
                    INSERT INTO collab_leases (
                        app_id, agent, target_ref, target_kind, ticket_id,
                        revision, state, worktree_path, worktree_branch,
                        worktree_base_sha, expires_at, max_lease_until
                    )
                    VALUES (
                        %s, %s, %s, 'ticket', %s, %s, 'active', %s, %s, %s,
                        NOW() + make_interval(secs => %s),
                        NOW() + make_interval(secs => %s)
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING {_LEASE_COLUMNS}
                    """,
                    (
                        app,
                        agent_label,
                        f"t:{ticket_id}",
                        ticket_id,
                        int(ticket["revision"]),
                        worktree_path,
                        worktree_branch,
                        worktree_base_sha,
                        int(lease_seconds),
                        int(max_lease_seconds),
                    ),
                )
                lease = _lease(await cur.fetchone())
                if lease is None:
                    # Another writer won the live-target race. Raising rolls
                    # the Ticket status write back with the insert.
                    raise AlreadyClaimedError(
                        "the task already has a live collaboration lease"
                    )
                return lease


async def get_lease(lease_id: str, *, conn: Any = None) -> dict[str, Any] | None:
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_LEASE_COLUMNS} FROM collab_leases WHERE lease_id = %s",
                (lease_id,),
            )
            return _lease(await cur.fetchone())


async def lease_for_target(
    target_ref: str, *, conn: Any = None
) -> dict[str, Any] | None:
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_LEASE_COLUMNS}
                FROM collab_leases
                WHERE target_ref = %s AND state IN ('active', 'handoff_pending')
                """,
                (normalize_task_ref(target_ref),),
            )
            return _lease(await cur.fetchone())


async def heartbeat_lease(
    lease_id: str,
    *,
    app_id: str,
    extend_seconds: int = LEASE_SECONDS,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Renew a live lease, never beyond its four-hour ceiling.

    The persisted ``expires_at`` is the authority: a heartbeat that arrives
    after it (but before the sweep has run) returns ``None`` instead of moving
    the deadline forward and resurrecting an expired lease.
    """
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE collab_leases
                SET heartbeat_at = NOW(),
                    expires_at = LEAST(
                        max_lease_until,
                        NOW() + make_interval(secs => %s)
                    )
                WHERE lease_id = %s
                  AND app_id = %s
                  AND state IN ('active', 'handoff_pending')
                  AND expires_at > NOW()
                  AND max_lease_until > NOW()
                RETURNING {_LEASE_COLUMNS}
                """,
                (int(extend_seconds), lease_id, normalize_app_id(app_id)),
            )
            return _lease(await cur.fetchone())


async def record_lease_report(
    lease_id: str,
    *,
    app_id: str,
    ticket_status: str,
    lease_seconds: int = LEASE_SECONDS,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Apply a holder's reconciled ``task_report`` to its Ticket and lease.

    ``ticket_status`` is the reconciled ``COMPLETION_STATUS_TO_TICKET_STATE``
    value. One statement moves the Ticket and advances the lease generation
    (``revision``) to the Ticket's new revision, so the recovery fence in
    :func:`requeue_cte` keeps matching after the report: a later expiry,
    release, revocation or handoff timeout returns the ``waiting`` or
    ``blocked`` Ticket to ``queued`` instead of stranding it. ``ready`` (a
    reconciled ``done``) completes the lease in the same statement; any other
    state keeps it live, counts as a heartbeat and renews the four-hour
    ceiling.

    Only a live, unexpired ``active`` lease held by ``app_id`` may report
    (``handoff_pending`` may not), and only on a Ticket that is still in a
    recoverable state with no daemon or cloud owner. Otherwise nothing changes
    and ``None`` is returned (``lease_lost`` / ``lease_expired`` for the
    caller). The returned row is the lease plus ``ticket_status``.
    """
    if ticket_status not in REPORT_TICKET_STATES:
        raise ValueError("ticket_status must be ready, waiting or blocked")
    if not 1 <= int(lease_seconds) <= MAX_LEASE_SECONDS:
        raise ValueError("lease_seconds must be positive and no longer than the ceiling")
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                WITH held AS (
                    SELECT lease_id, ticket_id, revision
                    FROM collab_leases
                    WHERE lease_id = %s
                      AND app_id = %s
                      AND state = 'active'
                      AND target_kind = 'ticket'
                      AND expires_at > NOW()
                      AND max_lease_until > NOW()
                    FOR UPDATE
                ),
                moved AS (
                    UPDATE autonomy_tickets t
                    SET status = %s,
                        revision = t.revision + 1,
                        updated_at = NOW()
                    FROM held h
                    WHERE t.ticket_id = h.ticket_id
                      AND t.status IN ({_RECOVERABLE_SQL})
                      AND t.revision >= h.revision
                      AND t.worker_lease_id IS NULL
                      AND t.worker_run_id IS NULL
                    RETURNING t.ticket_id, t.status, t.revision
                ),
                reported AS (
                    UPDATE collab_leases l
                    SET revision = m.revision,
                        heartbeat_at = NOW(),
                        state = CASE WHEN m.status = 'ready'
                                     THEN 'completed' ELSE l.state END,
                        ended_at = CASE WHEN m.status = 'ready'
                                        THEN NOW() ELSE l.ended_at END,
                        end_reason = CASE WHEN m.status = 'ready'
                                          THEN 'report_done' ELSE l.end_reason END,
                        max_lease_until = CASE WHEN m.status = 'ready'
                            THEN l.max_lease_until
                            ELSE GREATEST(
                                l.max_lease_until,
                                NOW() + make_interval(secs => %s)
                            ) END,
                        expires_at = CASE WHEN m.status = 'ready'
                            THEN l.expires_at
                            ELSE LEAST(
                                GREATEST(
                                    l.max_lease_until,
                                    NOW() + make_interval(secs => %s)
                                ),
                                NOW() + make_interval(secs => %s)
                            ) END
                    FROM held h
                    JOIN moved m ON m.ticket_id = h.ticket_id
                    WHERE l.lease_id = h.lease_id
                    RETURNING {_REPORTED_COLUMNS}
                )
                SELECT r.*, m.status AS ticket_status
                FROM reported r
                JOIN moved m ON m.ticket_id = r.ticket_id
                """,
                (
                    lease_id,
                    normalize_app_id(app_id),
                    ticket_status,
                    MAX_LEASE_SECONDS,
                    MAX_LEASE_SECONDS,
                    int(lease_seconds),
                ),
            )
            return _lease(await cur.fetchone())


async def release_lease(
    lease_id: str,
    *,
    app_id: str,
    reason: str = "",
    state: str = "released",
    conn: Any = None,
) -> dict[str, Any] | None:
    """End a lease and put a Ticket it still holds back in ``queued``.

    Requeuing shares the statement (and therefore the transaction) with the
    lease update. A released or expired lease can otherwise leave a running,
    waiting or blocked Ticket with no owner: the expiry sweep only sees live
    leases. The requeue uses :func:`requeue_cte`, so it is fenced on the
    lease's generation and on the absence of a daemon or cloud owner. A
    ``completed`` lease normally ends with its Ticket in ``ready`` (see
    :func:`record_lease_report`), which is not recoverable and stays put; a
    ``completed`` lease whose Ticket never reached ``ready`` is requeued rather
    than stranded.
    """
    if state not in {"released", "completed", "expired"}:
        raise ValueError("release state must be released, completed or expired")
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                WITH released AS (
                    UPDATE collab_leases
                    SET state = %s, ended_at = NOW(), end_reason = %s
                    WHERE lease_id = %s
                      AND app_id = %s
                      AND state IN ('active', 'handoff_pending')
                    RETURNING {_LEASE_COLUMNS}
                ),
                requeued AS ({requeue_cte('released', 'r')})
                SELECT r.*, (SELECT COUNT(*) FROM requeued) AS requeued
                FROM released r
                """,
                (
                    state,
                    reason[:500],
                    lease_id,
                    normalize_app_id(app_id),
                ),
            )
            return _lease(await cur.fetchone())


async def expire_lease(
    lease_id: str, *, reason: str = "lease_expired", conn: Any = None
) -> dict[str, Any] | None:
    """Expire one lease and return its Ticket to ``queued`` atomically.

    The persisted deadline decides, not the caller's clock: a lease whose
    ``expires_at`` and ceiling are both still in the future (for example one a
    heartbeat renewed after the caller read it) is left alone and ``None`` is
    returned. Ending a live lease early is :func:`release_lease`.
    """
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                WITH expired AS (
                    UPDATE collab_leases
                    SET state = 'expired', ended_at = NOW(), end_reason = %s
                    WHERE lease_id = %s
                      AND state IN ('active', 'handoff_pending')
                      AND (expires_at <= NOW() OR max_lease_until <= NOW())
                    RETURNING {_LEASE_COLUMNS}
                ),
                requeued AS ({requeue_cte('expired', 'e')})
                SELECT e.*, (SELECT COUNT(*) FROM requeued) AS requeued
                FROM expired e
                """,
                (reason[:500], lease_id),
            )
            return _lease(await cur.fetchone())


async def set_handoff_pending(
    lease_id: str,
    *,
    app_id: str,
    expires_at: datetime,
    conn: Any = None,
) -> dict[str, Any] | None:
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE collab_leases
                SET state = 'handoff_pending',
                    expires_at = LEAST(max_lease_until, %s)
                WHERE lease_id = %s AND app_id = %s AND state = 'active'
                  AND expires_at > NOW()
                  AND max_lease_until > NOW()
                RETURNING {_LEASE_COLUMNS}
                """,
                (expires_at, lease_id, normalize_app_id(app_id)),
            )
            return _lease(await cur.fetchone())


async def transfer_lease(
    lease_id: str,
    *,
    from_app_id: str,
    to_app_id: str,
    agent: str = "",
    lease_seconds: int = LEASE_SECONDS,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Move a handoff-pending lease to the accepting app in one statement.

    The persisted deadlines fence the transfer: a handoff on a lease whose
    ``expires_at`` or four-hour ceiling has passed before the sweep runs is
    refused, and the same conditions are repeated in the guarded UPDATE.
    """
    if not 1 <= int(lease_seconds) <= MAX_LEASE_SECONDS:
        raise ValueError("lease_seconds must be positive and no longer than the ceiling")
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE collab_leases
                SET app_id = %s,
                    agent = %s,
                    state = 'active',
                    heartbeat_at = NOW(),
                    expires_at = LEAST(
                        max_lease_until,
                        NOW() + make_interval(secs => %s)
                    )
                WHERE lease_id = %s
                  AND app_id = %s
                  AND state = 'handoff_pending'
                  AND expires_at > NOW()
                  AND max_lease_until > NOW()
                RETURNING {_LEASE_COLUMNS}
                """,
                (
                    normalize_app_id(to_app_id),
                    normalize_agent(agent),
                    int(lease_seconds),
                    lease_id,
                    normalize_app_id(from_app_id),
                ),
            )
            return _lease(await cur.fetchone())


async def list_active_leases(
    app_id: str | None = None, *, conn: Any = None
) -> list[dict[str, Any]]:
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            if app_id is None:
                await cur.execute(
                    f"""
                    SELECT {_LEASE_COLUMNS}
                    FROM collab_leases
                    WHERE state IN ('active', 'handoff_pending')
                    ORDER BY created_at
                    """
                )
            else:
                await cur.execute(
                    f"""
                    SELECT {_LEASE_COLUMNS}
                    FROM collab_leases
                    WHERE app_id = %s AND state IN ('active', 'handoff_pending')
                    ORDER BY created_at
                    """,
                    (normalize_app_id(app_id),),
                )
            return [_lease(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


async def release_app_leases(
    app_id: str, *, reason: str = "revoked", conn: Any = None
) -> list[dict[str, Any]]:
    """Release every live lease an app holds and requeue their Tickets.

    App revocation and mode changes both take this path. The lease rows and the
    Ticket status changes are one statement, so a revoked app cannot leave a
    Ticket running with no holder. The same generation/ownership fence as
    :func:`release_lease` applies to every Ticket this app used to hold.
    """
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                WITH released AS (
                    UPDATE collab_leases
                    SET state = 'released',
                        ended_at = NOW(),
                        end_reason = %s
                    WHERE app_id = %s
                      AND state IN ('active', 'handoff_pending')
                    RETURNING {_LEASE_COLUMNS}
                ),
                requeued AS ({requeue_cte('released', 'r')})
                SELECT r.*, (SELECT COUNT(*) FROM requeued) AS requeued
                FROM released r
                ORDER BY r.created_at
                """,
                (reason[:500], normalize_app_id(app_id)),
            )
            return [_lease(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


async def sweep_expired_leases(
    *, limit: int = 100, conn: Any = None
) -> list[dict[str, Any]]:
    """Expire stale leases and return their Tickets to ``queued``.

    A lease is stale once its ``expires_at`` or its four-hour ceiling has
    passed. The data-modifying CTE updates the Ticket in the same statement as
    the lease, so a crash cannot leave a running, waiting or blocked Ticket
    with no live owner. The
    returned rows are the expired lease rows plus how many Tickets moved. The
    requeue matches the lease's Ticket revision and refuses to touch a Ticket
    with a live daemon or cloud owner, so a sweep racing a reassignment cannot
    requeue the new owner's work.
    """
    bounded = max(1, min(1000, int(limit)))
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                WITH expired AS (
                    UPDATE collab_leases
                    SET state = 'expired',
                        ended_at = NOW(),
                        end_reason = 'lease_expired'
                    WHERE lease_id IN (
                        SELECT lease_id
                        FROM collab_leases
                        WHERE state IN ('active', 'handoff_pending')
                          AND (expires_at <= NOW() OR max_lease_until <= NOW())
                        ORDER BY expires_at
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING lease_id, app_id, ticket_id, target_ref,
                              target_kind, revision
                ),
                requeued AS ({requeue_cte('expired', 'e')})
                SELECT e.lease_id, e.app_id, e.ticket_id, e.target_ref,
                       e.target_kind, e.revision,
                       (SELECT COUNT(*) FROM requeued) AS requeued
                FROM expired e
                ORDER BY e.lease_id
                """,
                (bounded,),
            )
            return [dict(row) for row in await cur.fetchall()]


__all__ = [
    "RECOVERABLE_TICKET_STATES",
    "REPORT_TICKET_STATES",
    "claim_ticket_lease",
    "expire_lease",
    "get_lease",
    "heartbeat_lease",
    "insert_lease",
    "lease_for_target",
    "list_active_leases",
    "record_lease_report",
    "release_app_leases",
    "release_lease",
    "requeue_cte",
    "set_handoff_pending",
    "sweep_expired_leases",
    "transfer_lease",
]
