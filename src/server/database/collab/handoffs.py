"""Durable handoffs: a pending lease transfer plus its untrusted self-report.

The ``system`` section of the pack is generated from current Ticket state by
the service, never stored here as authority. The only conversation material
this table holds is ``{app, session_id}``; bodies stay in memfw.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.collab.types import (
    MAX_LEASE_SECONDS,
    normalize_agent,
    normalize_app_id,
)
from src.server.database.collab.leases import requeue_cte
from src.server.database.pool import get_db_connection

_HANDOFF_COLUMNS = """
    handoff_id, lease_id, from_app_id, to_target, reason, note, self_report,
    conversation_ref, state, expires_at, accepted_by, accepted_at, created_at
"""
_REASONS = frozenset(
    {"quota_exhausted", "context_full", "stuck", "end_of_session", "other"}
)
_CONVERSATION_KEYS = frozenset({"app", "session_id"})


def _handoff(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("handoff_id", "lease_id", "accepted_by"):
        if out.get(key):
            out[key] = str(out[key])
    out["self_report"] = dict(out.get("self_report") or {})
    ref = out.get("conversation_ref")
    out["conversation_ref"] = dict(ref) if isinstance(ref, dict) else None
    return out


def _optional_app(app_id: str | None) -> str | None:
    return None if app_id is None else normalize_app_id(app_id)


def _conversation_ref(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _CONVERSATION_KEYS:
        raise ValueError("conversation_ref must contain exactly app and session_id")
    app = str(value.get("app") or "").strip()
    session_id = str(value.get("session_id") or "").strip()
    if not app or not session_id or len(session_id) > 200:
        raise ValueError("conversation_ref needs a non-empty app and session_id")
    return {"app": app, "session_id": session_id}


async def create_handoff(
    *,
    lease_id: str,
    from_app_id: str,
    reason: str,
    expires_at: datetime | None = None,
    to_target: str = "router",
    note: str = "",
    self_report: dict[str, Any] | None = None,
    conversation_ref: dict[str, Any] | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Record the pending handoff for a lease already in ``handoff_pending``.

    Call it after :func:`~src.server.database.collab.leases.set_handoff_pending`
    in the same transaction. The row is only written while ``from_app_id``
    still holds that lease in ``handoff_pending`` and no other handoff for it
    is pending (a partial unique index backs this up), so a stale or duplicate
    request cannot later be accepted, expired or declined against a lease the
    requester no longer controls. A refused request returns ``None``.
    """
    if reason not in _REASONS:
        raise ValueError("unknown handoff reason")
    if len(note) > 1000:
        raise ValueError("handoff note is longer than 1000 characters")
    ref = _conversation_ref(conversation_ref)
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO collab_handoffs (
                    lease_id, from_app_id, to_target, reason, note,
                    self_report, conversation_ref, expires_at
                )
                SELECT l.lease_id, l.app_id, %s, %s, %s, %s, %s,
                       COALESCE(%s, NOW() + INTERVAL '30 minutes')
                FROM collab_leases l
                WHERE l.lease_id = %s
                  AND l.app_id = %s
                  AND l.state = 'handoff_pending'
                  AND NOT EXISTS (
                      SELECT 1 FROM collab_handoffs p
                      WHERE p.lease_id = l.lease_id AND p.state = 'pending'
                  )
                ON CONFLICT DO NOTHING
                RETURNING {_HANDOFF_COLUMNS}
                """,
                (
                    str(to_target or "router")[:200],
                    reason,
                    note,
                    Json(dict(self_report or {})),
                    Json(ref) if ref is not None else None,
                    expires_at,
                    lease_id,
                    normalize_app_id(from_app_id),
                ),
            )
            return _handoff(await cur.fetchone())


async def get_handoff(
    handoff_id: str, *, conn: Any = None
) -> dict[str, Any] | None:
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_HANDOFF_COLUMNS} FROM collab_handoffs WHERE handoff_id = %s",
                (handoff_id,),
            )
            return _handoff(await cur.fetchone())


async def list_handoffs(
    *,
    to_target: str | None = None,
    app_id: str | None = None,
    state: str = "pending",
    limit: int = 20,
    conn: Any = None,
) -> list[dict[str, Any]]:
    conditions = ["state = %s"]
    params: list[Any] = [state]
    if to_target is not None:
        conditions.append("to_target = %s")
        params.append(str(to_target))
    if app_id is not None:
        conditions.append(
            "(to_target = 'any' OR to_target = %s OR to_target = %s)"
        )
        params.extend([normalize_app_id(app_id), f"app:{normalize_app_id(app_id)}"])
    params.append(max(1, min(50, int(limit))))
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_HANDOFF_COLUMNS}
                FROM collab_handoffs
                WHERE {' AND '.join(conditions)}
                ORDER BY created_at
                LIMIT %s
                """,
                tuple(params),
            )
            return [_handoff(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


async def accept_handoff(
    *,
    handoff_id: str,
    app_id: str,
    agent: str = "",
    lease_seconds: int = 900,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Accept a pending handoff and transfer its lease in one transaction.

    Pass ``conn`` from :func:`collab_transaction` to join the caller's
    transaction (for example so the ownership event and audit row commit with
    the transfer).

    The lease must still be held by the app that requested the handoff: a
    stale handoff row cannot move a lease that has since changed hands.
    """
    acceptor = normalize_app_id(app_id)
    agent_label = normalize_agent(agent)
    if not 1 <= int(lease_seconds) <= MAX_LEASE_SECONDS:
        raise ValueError("lease_seconds must be positive and no longer than the ceiling")
    async with get_db_connection(conn) as db:
        transaction = db.transaction() if conn is None else nullcontext()
        async with transaction:
            async with db.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT h.handoff_id, h.lease_id, h.expires_at
                    FROM collab_handoffs h
                    JOIN collab_leases l ON l.lease_id = h.lease_id
                    WHERE h.handoff_id = %s
                      AND h.state = 'pending'
                      AND h.expires_at > NOW()
                      AND (h.to_target IN ('any', %s, %s))
                      AND l.state = 'handoff_pending'
                      AND l.app_id = h.from_app_id
                      AND l.expires_at > NOW()
                      AND l.max_lease_until > NOW()
                    FOR UPDATE OF h, l
                    """,
                    (handoff_id, f"app:{acceptor}", acceptor),
                )
                pending = await cur.fetchone()
                if pending is None:
                    return None
                await cur.execute(
                    f"""
                    UPDATE collab_handoffs
                    SET state = 'accepted', accepted_by = %s,
                        accepted_at = NOW()
                    WHERE handoff_id = %s
                    RETURNING {_HANDOFF_COLUMNS}
                    """,
                    (acceptor, handoff_id),
                )
                handoff = _handoff(await cur.fetchone())
                await cur.execute(
                    """
                    UPDATE collab_leases
                    SET app_id = %s,
                        agent = %s,
                        state = 'active',
                        heartbeat_at = NOW(),
                        expires_at = LEAST(
                            max_lease_until,
                            NOW() + make_interval(secs => %s)
                        )
                    WHERE lease_id = %s AND state = 'handoff_pending'
                      AND expires_at > NOW()
                      AND max_lease_until > NOW()
                    RETURNING lease_id, app_id, agent, target_ref, target_kind,
                              ticket_id, line_id, revision, state, worktree_path,
                              worktree_branch, worktree_base_sha, heartbeat_at,
                              expires_at, max_lease_until, created_at, ended_at,
                              end_reason
                    """,
                    (acceptor, agent_label, int(lease_seconds), pending["lease_id"]),
                )
                lease = await cur.fetchone()
                return {
                    "handoff": handoff,
                    "lease": dict(lease) if lease is not None else None,
                }


async def cancel_handoff(
    handoff_id: str, *, app_id: str | None = None, conn: Any = None
) -> dict[str, Any] | None:
    """Withdraw a pending handoff and give the lease back to its holder.

    One statement cancels the handoff and returns its ``handoff_pending``
    lease to ``active`` for the requesting app, so the holder can report
    again; otherwise the lease would sit in ``handoff_pending`` with nobody
    able to accept it. A lease whose persisted deadline has already passed is
    left for the expiry sweep, which requeues its Ticket. ``app_id``, when
    given, must be the requesting app.
    """
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                WITH cancelled AS (
                    UPDATE collab_handoffs
                    SET state = 'cancelled'
                    WHERE handoff_id = %s AND state = 'pending'
                      AND (%s::text IS NULL OR from_app_id = %s::text)
                    RETURNING {_HANDOFF_COLUMNS}
                ),
                restored AS (
                    UPDATE collab_leases l
                    SET state = 'active'
                    FROM cancelled c
                    WHERE l.lease_id = c.lease_id
                      AND l.state = 'handoff_pending'
                      AND l.app_id = c.from_app_id
                      AND l.expires_at > NOW()
                      AND l.max_lease_until > NOW()
                    RETURNING l.lease_id
                )
                SELECT c.*, (SELECT COUNT(*) FROM restored) AS restored
                FROM cancelled c
                """,
                (handoff_id, _optional_app(app_id), _optional_app(app_id)),
            )
            return _handoff(await cur.fetchone())


async def decline_handoff(
    handoff_id: str, *, app_id: str, conn: Any = None
) -> dict[str, Any] | None:
    """Refuse a handoff addressed to ``app_id`` and send the task to the Router.

    Only a handoff aimed at this app (``app:<id>`` or ``<id>``) can be
    declined; ``any`` and ``router`` handoffs simply wait for a taker or
    expire. Declining cancels the handoff, releases its ``handoff_pending``
    lease (``handoff_declined``) and requeues the Ticket through the same
    fence as every other recovery path, all in one statement, so a decline
    cannot leave a waiting, blocked or running Ticket without an owner.
    """
    target = normalize_app_id(app_id)
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                WITH declined AS (
                    UPDATE collab_handoffs
                    SET state = 'cancelled'
                    WHERE handoff_id = %s AND state = 'pending'
                      AND to_target IN (%s, %s)
                    RETURNING {_HANDOFF_COLUMNS}
                ),
                released AS (
                    UPDATE collab_leases l
                    SET state = 'released',
                        ended_at = NOW(),
                        end_reason = 'handoff_declined'
                    FROM declined d
                    WHERE l.lease_id = d.lease_id
                      AND l.state = 'handoff_pending'
                      AND l.app_id = d.from_app_id
                    RETURNING l.lease_id, l.ticket_id, l.target_kind, l.revision
                ),
                requeued AS ({requeue_cte('released', 'r')})
                SELECT d.*,
                       (SELECT COUNT(*) FROM released) AS released,
                       (SELECT COUNT(*) FROM requeued) AS requeued
                FROM declined d
                """,
                (handoff_id, f"app:{target}", target),
            )
            return _handoff(await cur.fetchone())


async def expire_handoffs(
    *, limit: int = 100, conn: Any = None
) -> list[dict[str, Any]]:
    """Expire pending handoffs, release their leases, and requeue their Tickets.

    A handoff that expires without a taker must not leave a ``handoff_pending``
    lease (and therefore a running, waiting or blocked Ticket) with no owner.
    The handoff, lease and Ticket changes are one statement. The lease is only
    released while the requesting app still holds it, and the requeue uses
    :func:`~src.server.database.collab.leases.requeue_cte` (lease generation
    plus no daemon or cloud owner), so a stale handoff cannot requeue a Ticket
    that was reassigned or handed on after it was requested.
    """
    bounded = max(1, min(1000, int(limit)))
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                WITH expired AS (
                    UPDATE collab_handoffs
                    SET state = 'expired'
                    WHERE handoff_id IN (
                        SELECT handoff_id
                        FROM collab_handoffs
                        WHERE state = 'pending' AND expires_at <= NOW()
                        ORDER BY expires_at
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING handoff_id, lease_id, from_app_id
                ),
                released AS (
                    UPDATE collab_leases l
                    SET state = 'released',
                        ended_at = NOW(),
                        end_reason = 'handoff_expired'
                    FROM expired e
                    WHERE l.lease_id = e.lease_id
                      AND l.state = 'handoff_pending'
                      AND l.app_id = e.from_app_id
                    RETURNING l.lease_id, l.ticket_id, l.target_kind, l.revision
                ),
                requeued AS ({requeue_cte('released', 'r')})
                SELECT e.handoff_id, e.lease_id,
                       (SELECT COUNT(*) FROM released) AS released,
                       (SELECT COUNT(*) FROM requeued) AS requeued
                FROM expired e
                """,
                (bounded,),
            )
            return [dict(row) for row in await cur.fetchall()]


__all__ = [
    "accept_handoff",
    "cancel_handoff",
    "create_handoff",
    "decline_handoff",
    "expire_handoffs",
    "get_handoff",
    "list_handoffs",
]
