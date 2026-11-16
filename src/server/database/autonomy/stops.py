"""Durable stop requests and native-daemon acknowledgement evidence.

A Ticket cancellation is a state change; a verified stop is a claim about the
runner. Keeping both in Postgres lets any worker decide whether a stopped
Ticket's process actually went away, and lets a timeout escalate rather than
silently assume it did.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.autonomy.tickets import TICKET_COLUMNS, ticket_from_row
from src.server.database.pool import get_db_connection

_STOP_FIELDS = """
    stop_id, ticket_id, user_id, daemon_id, lease_id, mode, verification,
    requested_at, deadline_at, acknowledged_at, verified_at, escalated_at,
    reason, evidence, attempts, updated_at
"""

_STOP_KEYS = (
    "stop_id",
    "ticket_id",
    "user_id",
    "daemon_id",
    "lease_id",
    "mode",
    "verification",
    "requested_at",
    "deadline_at",
    "acknowledged_at",
    "verified_at",
    "escalated_at",
    "reason",
    "evidence",
    "attempts",
    "updated_at",
)

_LEASE_FIELDS = """
    worker_lease_id, worker_lease_claim_id, worker_lease_expires_at,
    worker_lease_heartbeat_at
"""


def _stop(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = {key: row.get(key) for key in _STOP_KEYS}
    for key in ("stop_id", "ticket_id", "daemon_id", "lease_id"):
        if out.get(key):
            out[key] = str(out[key])
    out["evidence"] = dict(out.get("evidence") or {})
    out["verified"] = out.get("verification") == "verified"
    out["escalated"] = out.get("escalated_at") is not None
    return out


def _ticket_with_lease(row: dict[str, Any] | None) -> dict[str, Any] | None:
    ticket = ticket_from_row(row)
    return ticket


def _may_have_launched(ticket: dict[str, Any], current: dict[str, Any]) -> bool:
    """Whether a runner may exist even though its lease metadata is gone.

    ``attempts`` and ``started_at`` survive lease recovery, while the lease and
    claim ids do not. A stop must not read that absence as proof the process is
    gone: a dead daemon's native CLI can outlive its lease.
    """
    return bool(
        ticket.get("started_at")
        or ticket.get("worker_thread_id")
        or ticket.get("worker_run_id")
        or current.get("worker_lease_claim_id")
        or int(ticket.get("attempts") or 0) > 0
    )


def _fence_matches(
    ticket: dict[str, Any], current: dict[str, Any], expected: dict[str, Any] | None
) -> bool:
    """Whether the locked row still matches the snapshot the caller planned on."""
    if not expected:
        return True
    if "revision" in expected and int(ticket.get("revision") or 0) != int(
        expected.get("revision") or 0
    ):
        return False
    if "worker_daemon_id" in expected and str(
        ticket.get("worker_daemon_id") or ""
    ) != str(expected.get("worker_daemon_id") or ""):
        return False
    if "worker_lease_id" in expected and str(
        current.get("worker_lease_id") or ""
    ) != str(expected.get("worker_lease_id") or ""):
        return False
    return True


def _stop_attention(reason: str) -> Json:
    return Json(
        {
            "kind": "stop_unverified",
            "question": f"Stop was not verified: {reason}.",
        }
    )


async def request_stop(
    *,
    user_id: str,
    ticket_id: str,
    timeout_seconds: float,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cancel one Ticket and record what must be verified for its runner.

    ``expected`` fences the call to a snapshot (revision, owner, lease) read by
    the caller. An internal ownership transition passes it so a reassignment
    that lands while it waits cannot have its new owner's run stopped instead.
    """
    timeout = max(1.0, float(timeout_seconds))
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""SELECT {TICKET_COLUMNS}, {_LEASE_FIELDS}
                    FROM autonomy_tickets
                    WHERE ticket_id = %s AND user_id = %s FOR UPDATE""",
                    (ticket_id, user_id),
                )
                current = await cur.fetchone()
                if current is None:
                    return {"outcome": "not_found", "ticket": None, "stop": None}
                ticket = _ticket_with_lease(current)
                if not _fence_matches(ticket, current, expected):
                    return {"outcome": "changed", "ticket": ticket, "stop": None}
                status = str(ticket.get("status") or "")
                await cur.execute(
                    f"SELECT {_STOP_FIELDS} FROM autonomy_stop_requests "
                    "WHERE ticket_id = %s",
                    (ticket_id,),
                )
                existing = _stop(await cur.fetchone())
                if status in {"completed", "archived"} or ticket.get("closed_at"):
                    return {
                        "outcome": "ended",
                        "ticket": ticket,
                        "stop": existing,
                    }
                if existing is not None and status == "cancelled":
                    return {"outcome": "ok", "ticket": ticket, "stop": existing}

                daemon_id = ticket.get("worker_daemon_id")
                lease_id = current.get("worker_lease_id")
                if status == "running" and daemon_id and lease_id:
                    mode, verification = "daemon", "pending"
                elif daemon_id and _may_have_launched(ticket, current):
                    # Lease recovery clears the lease while the daemon's native
                    # CLI can still be running. With no live lease there is no
                    # runner left to acknowledge, so report this as unverified
                    # instead of treating missing metadata as termination.
                    mode, verification = "daemon", "unverified"
                elif ticket.get("worker_thread_id"):
                    mode, verification = "backend", "not_applicable"
                elif _may_have_launched(ticket, current):
                    # A backend launch may be between creating its thread and
                    # persisting the id; never claim verification for it.
                    mode, verification = "backend", "not_applicable"
                else:
                    mode, verification = "not_started", "verified"
                keep_lease = mode == "daemon" and verification == "pending"
                if mode == "daemon" and verification == "unverified":
                    reason = (
                        "The worker lease was lost before the stop; its native "
                        "process could not be verified"
                    )
                elif mode == "backend":
                    reason = (
                        "PTC worker cancellation has no native daemon acknowledgement"
                    )
                else:
                    reason = "No active Worker process to stop"
                attention = (
                    _stop_attention(reason) if verification == "unverified" else None
                )
                evidence = {"never_started": True} if mode == "not_started" else {}

                await cur.execute(
                    f"""UPDATE autonomy_tickets SET
                        status = 'cancelled',
                        attention = CASE WHEN %s THEN %s::jsonb ELSE NULL END,
                        ended_at = COALESCE(ended_at, NOW()),
                        worker_lease_id = CASE WHEN %s THEN worker_lease_id ELSE NULL END,
                        worker_lease_claim_id = CASE WHEN %s THEN worker_lease_claim_id ELSE NULL END,
                        worker_lease_expires_at = CASE WHEN %s THEN worker_lease_expires_at ELSE NULL END,
                        worker_lease_heartbeat_at = CASE WHEN %s THEN worker_lease_heartbeat_at ELSE NULL END,
                        revision = revision + 1,
                        updated_at = NOW()
                    WHERE ticket_id = %s
                    RETURNING {TICKET_COLUMNS}, {_LEASE_FIELDS}""",
                    (
                        attention is not None,
                        attention,
                        keep_lease,
                        keep_lease,
                        keep_lease,
                        keep_lease,
                        ticket_id,
                    ),
                )
                updated = _ticket_with_lease(await cur.fetchone())

                await cur.execute(
                    f"""INSERT INTO autonomy_stop_requests (
                        ticket_id, user_id, daemon_id, lease_id, mode,
                        verification, requested_at, deadline_at, verified_at,
                        escalated_at, reason, evidence
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        clock_timestamp(),
                        clock_timestamp() + make_interval(secs => %s),
                        CASE WHEN %s = 'verified' THEN clock_timestamp() ELSE NULL END,
                        CASE WHEN %s = 'unverified' THEN clock_timestamp() ELSE NULL END,
                        %s, %s::jsonb
                    )
                    ON CONFLICT (ticket_id) DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        daemon_id = EXCLUDED.daemon_id,
                        lease_id = EXCLUDED.lease_id,
                        mode = EXCLUDED.mode,
                        verification = EXCLUDED.verification,
                        requested_at = EXCLUDED.requested_at,
                        deadline_at = EXCLUDED.deadline_at,
                        acknowledged_at = NULL,
                        verified_at = EXCLUDED.verified_at,
                        escalated_at = EXCLUDED.escalated_at,
                        reason = EXCLUDED.reason,
                        evidence = EXCLUDED.evidence,
                        attempts = 0,
                        updated_at = NOW()
                    RETURNING {_STOP_FIELDS}""",
                    (
                        ticket_id,
                        user_id,
                        daemon_id,
                        lease_id,
                        mode,
                        verification,
                        timeout,
                        verification,
                        verification,
                        reason,
                        Json(evidence),
                    ),
                )
                stop = _stop(await cur.fetchone())
                if stop is None:
                    await cur.execute(
                        f"SELECT {_STOP_FIELDS} FROM autonomy_stop_requests "
                        "WHERE ticket_id = %s",
                        (ticket_id,),
                    )
                    stop = _stop(await cur.fetchone())
                # The status change and its audit commit together. A crash
                # between them would cancel a Ticket with no stopped row.
                if status != "cancelled":
                    from src.server.database.autonomy.lines import (
                        insert_ticket_event,
                        owner_ref,
                    )

                    await insert_ticket_event(
                        cur,
                        ticket_id=ticket_id,
                        user_id=user_id,
                        actor="user",
                        action="stopped",
                        from_owner=owner_ref(daemon_id),
                        detail={
                            "from_status": status,
                            "verification": verification,
                        },
                    )
                return {"outcome": "ok", "ticket": updated, "stop": stop}


async def acknowledge_stop(
    *,
    daemon_id: str,
    ticket_id: str,
    lease_id: str,
    process_gone: bool,
    evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    """Verify a daemon's stop acknowledgement against durable server state."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """SELECT s.*, t.status AS ticket_status, t.ended_at,
                        t.worker_daemon_id, t.worker_lease_id,
                        t.worker_lease_heartbeat_at
                    FROM autonomy_stop_requests s
                    JOIN autonomy_tickets t ON t.ticket_id = s.ticket_id
                    WHERE s.ticket_id = %s
                    FOR UPDATE OF s, t""",
                    (ticket_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return {"outcome": "not_found", "stop": None}
                stop = _stop(row)
                if stop["daemon_id"] != str(daemon_id):
                    return {"outcome": "not_owner", "stop": stop}
                if stop["verification"] != "pending":
                    return {"outcome": "ok", "stop": stop}
                if stop["lease_id"] != str(lease_id):
                    return {"outcome": "stale_lease", "stop": stop}
                # Archive asks the daemon to stop and then leaves the ticket
                # archived, so a "process gone" reply has to land there too.
                # Anything still running has not persisted the cancellation.
                if row["ticket_status"] not in {"cancelled", "archived"} or row["ended_at"] is None:
                    return {"outcome": "state_not_persisted", "stop": stop}
                requested_at = row.get("requested_at")
                heartbeat_at = row.get("worker_lease_heartbeat_at")
                if (
                    heartbeat_at is not None
                    and requested_at is not None
                    and heartbeat_at > requested_at
                ):
                    return {"outcome": "heartbeat_active", "stop": stop}

                now = datetime.now(timezone.utc)
                deadline = row.get("deadline_at")
                late = deadline is not None and now > deadline
                verified = bool(process_gone) and not late
                if verified:
                    verification = "verified"
                    reason = ""
                elif not process_gone:
                    verification = "unverified"
                    reason = "Daemon reported the native CLI process is still running"
                else:
                    verification = "unverified"
                    reason = "Daemon acknowledged the stop after its deadline"
                audit_evidence = {
                    **dict(evidence or {}),
                    "process_gone": bool(process_gone),
                    "late_ack": late,
                }
                await cur.execute(
                    f"""UPDATE autonomy_stop_requests SET
                        verification = %s,
                        acknowledged_at = COALESCE(acknowledged_at, NOW()),
                        verified_at = CASE WHEN %s THEN NOW() ELSE verified_at END,
                        escalated_at = CASE WHEN %s THEN NOW() ELSE escalated_at END,
                        reason = %s,
                        evidence = evidence || %s::jsonb,
                        attempts = attempts + 1,
                        updated_at = NOW()
                    WHERE stop_id = %s
                    RETURNING {_STOP_FIELDS}""",
                    (
                        verification,
                        verified,
                        not verified,
                        reason,
                        Json(audit_evidence),
                        stop["stop_id"],
                    ),
                )
                stop = _stop(await cur.fetchone())
                attention = (
                    Json(
                        {
                            "kind": "stop_unverified",
                            "question": f"Stop was not verified: {reason}.",
                        }
                    )
                    if not verified
                    else None
                )
                await cur.execute(
                    """UPDATE autonomy_tickets SET
                        worker_lease_id = NULL,
                        worker_lease_claim_id = NULL,
                        worker_lease_expires_at = NULL,
                        worker_lease_heartbeat_at = NULL,
                        attention = CASE WHEN %s THEN %s ELSE attention END,
                        revision = revision + 1,
                        updated_at = NOW()
                    WHERE ticket_id = %s""",
                    (not verified, attention, ticket_id),
                )
                return {"outcome": "ok", "stop": stop}


async def expire_stop_requests(*, limit: int = 100) -> int:
    """Escalate pending stops whose daemon never acknowledged in time."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""SELECT {_STOP_FIELDS}
                    FROM autonomy_stop_requests
                    WHERE verification = 'pending' AND deadline_at <= NOW()
                    ORDER BY deadline_at
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED""",
                    (max(1, int(limit)),),
                )
                rows = [_stop(row) for row in await cur.fetchall()]
                for stop in rows:
                    reason = "Daemon did not acknowledge the stop before its deadline"
                    await cur.execute(
                        """UPDATE autonomy_stop_requests SET
                            verification = 'unverified',
                            escalated_at = NOW(),
                            reason = %s,
                            evidence = evidence || %s::jsonb,
                            updated_at = NOW()
                        WHERE stop_id = %s""",
                        (
                            reason,
                            Json({"timeout": True}),
                            stop["stop_id"],
                        ),
                    )
                    await cur.execute(
                        """UPDATE autonomy_tickets SET
                            worker_lease_id = NULL,
                            worker_lease_claim_id = NULL,
                            worker_lease_expires_at = NULL,
                            worker_lease_heartbeat_at = NULL,
                            attention = %s,
                            revision = revision + 1,
                            updated_at = NOW()
                        WHERE ticket_id = %s AND status IN ('cancelled', 'archived')""",
                        (
                            Json(
                                {
                                    "kind": "stop_unverified",
                                    "question": f"Stop was not verified: {reason}.",
                                }
                            ),
                            stop["ticket_id"],
                        ),
                    )
                return len(rows)


def _live_lease_sql() -> str:
    # A normal stop flips the status to cancelled while preserving the lease so
    # the daemon can still acknowledge. That lease is still a live runner: force
    # acknowledgement must wait for the daemon or for the lease to expire.
    return """
        (worker_lease_id IS NOT NULL
         AND worker_lease_expires_at > clock_timestamp()) AS live_lease
    """


async def release_snapshot(*, user_id: str, ticket_id: str) -> dict[str, Any]:
    """The stop row and whether the daemon lease is still alive. Read only."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""SELECT {TICKET_COLUMNS}, {_LEASE_FIELDS}, {_live_lease_sql()}
                FROM autonomy_tickets
                WHERE ticket_id = %s AND user_id = %s""",
                (ticket_id, user_id),
            )
            current = await cur.fetchone()
            if current is None:
                return {
                    "outcome": "not_found",
                    "ticket": None,
                    "stop": None,
                    "live_lease": False,
                    "may_have_launched": False,
                }
            await cur.execute(
                f"SELECT {_STOP_FIELDS} FROM autonomy_stop_requests "
                "WHERE ticket_id = %s",
                (ticket_id,),
            )
            stop = _stop(await cur.fetchone())
    ticket = _ticket_with_lease(current)
    return {
        "outcome": "ok",
        "ticket": ticket,
        "stop": stop,
        "live_lease": bool(current.get("live_lease")),
        "may_have_launched": _may_have_launched(ticket or {}, current),
    }


def _force_receipt_is_current(
    receipt: dict[str, Any] | None, current: dict[str, Any]
) -> bool:
    """A verified stop that already names this daemon's cleared attempt.

    Mirrors ``ownership._stop_verified`` for the no-lease case. A live lease
    is refused before this runs. A lease that is still stored, or a receipt
    for another daemon, is not current and must be rewritten.
    """
    if receipt is None or current.get("worker_lease_id") or current.get("live_lease"):
        return False
    if receipt.get("verification") != "verified":
        return False
    if receipt.get("mode") not in {"daemon", "not_started"}:
        return False
    if str(receipt.get("daemon_id") or "") != str(current.get("worker_daemon_id") or ""):
        return False
    if receipt.get("mode") == "not_started":
        return True
    heartbeat = current.get("worker_lease_heartbeat_at")
    requested = receipt.get("requested_at")
    if heartbeat is not None and requested is not None and heartbeat > requested:
        return False
    return True


async def force_acknowledge_unverified(
    *,
    user_id: str,
    ticket_id: str,
    reason: str,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark an unverified stop verified because the operator says the daemon is dead.

    A live lease is refused: the runner can still acknowledge. The stop row,
    the lease clear, and the audit event commit together.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""SELECT {TICKET_COLUMNS}, {_LEASE_FIELDS}, {_live_lease_sql()}
                    FROM autonomy_tickets
                    WHERE ticket_id = %s AND user_id = %s
                    FOR UPDATE""",
                    (ticket_id, user_id),
                )
                current = await cur.fetchone()
                if current is None:
                    return {"outcome": "not_found", "ticket": None, "stop": None}
                ticket = _ticket_with_lease(current)
                if not _fence_matches(ticket, current, expected):
                    return {"outcome": "changed", "ticket": ticket, "stop": None}
                if not current.get("worker_daemon_id"):
                    return {"outcome": "not_daemon", "ticket": ticket, "stop": None}
                if current.get("live_lease"):
                    return {"outcome": "live_lease", "ticket": ticket, "stop": None}
                await cur.execute(
                    f"""SELECT {_STOP_FIELDS} FROM autonomy_stop_requests
                    WHERE ticket_id = %s FOR UPDATE""",
                    (ticket_id,),
                )
                existing = await cur.fetchone()
                prior = str((existing or {}).get("verification") or "")
                receipt = _stop(existing) if existing is not None else None
                # Idempotent only when this receipt is already the current
                # attempt and the lease is clear. An older verified row used
                # to return here, so force did nothing. The upsert rebinds
                # daemon, lease, and mode; leaving those stale made the
                # caller's coverage check fail after this audit write.
                if _force_receipt_is_current(receipt, current):
                    return {"outcome": "ok", "ticket": ticket, "stop": receipt}
                evidence = {
                    "forced": True,
                    "override": "acknowledge_unverified",
                    "prior_verification": prior or "absent",
                }
                await cur.execute(
                    f"""INSERT INTO autonomy_stop_requests (
                        ticket_id, user_id, daemon_id, lease_id, mode,
                        verification, requested_at, deadline_at,
                        acknowledged_at, verified_at, reason, evidence
                    )
                    VALUES (
                        %s, %s, %s, %s, 'daemon',
                        'verified', clock_timestamp(), clock_timestamp(),
                        clock_timestamp(), clock_timestamp(), %s, %s::jsonb
                    )
                    ON CONFLICT (ticket_id) DO UPDATE SET
                        daemon_id = EXCLUDED.daemon_id,
                        lease_id = EXCLUDED.lease_id,
                        mode = EXCLUDED.mode,
                        verification = 'verified',
                        requested_at = EXCLUDED.requested_at,
                        deadline_at = EXCLUDED.deadline_at,
                        acknowledged_at = COALESCE(
                            autonomy_stop_requests.acknowledged_at, clock_timestamp()
                        ),
                        verified_at = clock_timestamp(),
                        reason = EXCLUDED.reason,
                        evidence = autonomy_stop_requests.evidence || EXCLUDED.evidence,
                        attempts = autonomy_stop_requests.attempts + 1,
                        updated_at = NOW()
                    RETURNING {_STOP_FIELDS}""",
                    (
                        ticket_id,
                        user_id,
                        current.get("worker_daemon_id"),
                        current.get("worker_lease_id"),
                        reason,
                        Json(evidence),
                    ),
                )
                stop = _stop(await cur.fetchone())
                await cur.execute(
                    f"""UPDATE autonomy_tickets SET
                        attention = CASE
                            WHEN attention->>'kind' = 'stop_unverified' THEN NULL
                            ELSE attention
                        END,
                        worker_lease_id = NULL,
                        worker_lease_claim_id = NULL,
                        worker_lease_expires_at = NULL,
                        worker_lease_heartbeat_at = NULL,
                        revision = revision + 1,
                        updated_at = NOW()
                    WHERE ticket_id = %s AND user_id = %s
                    RETURNING {TICKET_COLUMNS}, {_LEASE_FIELDS}""",
                    (ticket_id, user_id),
                )
                updated = _ticket_with_lease(await cur.fetchone())
                from src.server.database.autonomy.lines import (
                    insert_ticket_event,
                    owner_ref,
                )

                await insert_ticket_event(
                    cur,
                    ticket_id=ticket_id,
                    user_id=user_id,
                    actor="user",
                    action="stop_acknowledged",
                    from_owner=owner_ref(current.get("worker_daemon_id")),
                    detail={
                        "override": "acknowledge_unverified",
                        "forced": True,
                        "prior_verification": prior or "absent",
                        "from_status": str(current.get("status") or ""),
                    },
                )
                return {"outcome": "ok", "ticket": updated, "stop": stop}


async def get_stop(ticket_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_STOP_FIELDS} FROM autonomy_stop_requests "
                "WHERE ticket_id = %s",
                (ticket_id,),
            )
            return _stop(await cur.fetchone())


async def list_stops(ticket_ids: list[str]) -> list[dict[str, Any]]:
    if not ticket_ids:
        return []
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_STOP_FIELDS} FROM autonomy_stop_requests "
                "WHERE ticket_id::text = ANY(%s)",
                (list(ticket_ids),),
            )
            return [_stop(row) for row in await cur.fetchall() if row]
