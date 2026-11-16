"""Ticket ownership operations: stop, resume, reassign, archive.

Each operation is a guarded (compare-and-swap) write plus a row in
``autonomy_ticket_events``, so the exactly-one-owner invariant and its history
survive any interleaving of API workers, daemons, and the scheduler.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.server.database.autonomy import daemons as daemon_db
from src.server.database.autonomy import lines as line_db
from src.server.database.autonomy import tickets as ticket_db
from src.server.database.autonomy import triggers as trigger_db
from src.server.database.autonomy.effects import enqueue_effect
from src.server.services.autonomy import policy
from src.server.services.autonomy.models import EFFECT_LAUNCH_WORKER

TERMINAL_STATUSES = ("completed", "cancelled", "archived")
_PENDING_STOP = (
    "Daemon stop is pending; retry after the daemon confirms it stopped"
)
_PENDING_STOPPED = (
    "Daemon stop is pending; the ticket was stopped and will move "
    "after the daemon confirms it stopped"
)
_UNVERIFIED_STOP = (
    "Daemon stop is unverified. The ticket was not changed. "
    "Force-acknowledge the stop once the daemon is dead, then retry."
)
_FORCE_REASON = "operator accepted an unverified daemon stop"


class TicketOpError(Exception):
    """An ownership operation refused; ``status_code`` maps to the HTTP error."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def _owned_ticket(user_id: str, ticket_id: str) -> dict[str, Any]:
    row = await ticket_db.get_ticket(ticket_id)
    if row is None or row["user_id"] != user_id:
        raise TicketOpError(404, "Ticket not found")
    return row


async def revoked_mandate_reason(ticket: dict[str, Any]) -> str | None:
    """Why a retry/resume no longer holds the Ticket's Mandate, or None."""
    mandate = dict(ticket.get("mandate") or {})
    source_id = str(mandate.get("source_id") or "")
    trigger_id = str(ticket.get("origin_trigger_id") or mandate.get("trigger_id") or "")
    # Manual and scheduled Tickets have no external sender to revalidate.
    if not source_id or not trigger_id:
        return None
    from src.server.database.autonomy import sources as source_db

    source = await source_db.get_source_any_user(source_id)
    if source is None or not source.get("enabled"):
        return "the Source is no longer enabled"
    if str(source.get("kind") or "") == "manual":
        return None
    trigger = await trigger_db.get_trigger(trigger_id)
    if trigger is None:
        return "the originating Trigger no longer exists"
    current = await policy.current_policy(str(ticket["user_id"]))
    source_config = dict(source.get("config") or {})
    ok, reason = policy.revalidate(
        trigger,
        source_config,
        policy.effective_admission(current.get("admission"), source_config),
        strength=str(mandate.get("strength") or "explicit"),
        accept_mode=str(current.get("accept_mode") or "explicit"),
    )
    return None if ok else reason


async def _cancel_worker_thread(row: dict[str, Any]) -> bool:
    """Stop the cloud Worker before ownership leaves it.

    Returns True when nothing is left running (including "already finished").
    A False result means execution may still be live, so the caller must not
    publish a new owner over it.
    """
    if not row.get("worker_thread_id"):
        return True
    from src.server.services.projects.dispatch import ProjectDispatcher

    return await ProjectDispatcher().cancel_thread(
        user_id=str(row["user_id"]),
        thread_id=str(row["worker_thread_id"]),
        run_id=row.get("worker_run_id"),
    )


async def _cancel_cloud_worker_before_takeover(row: dict[str, Any]) -> None:
    """Fail the ownership change when the previous cloud Worker cannot stop."""
    if str(row.get("status") or "") != "running":
        return
    if not await _cancel_worker_thread(row):
        raise TicketOpError(
            409,
            "The current Worker could not be stopped; retry once it settles",
        )


async def _reenqueue_launch(row: dict[str, Any]) -> None:
    # Revision stays in this key. A requeue of the same attempt, such as a
    # reassign back to cloud, must still enqueue. The recovery sweep omits it
    # so a revision bump does not grow the effects table.
    await enqueue_effect(
        user_id=str(row["user_id"]),
        kind=EFFECT_LAUNCH_WORKER,
        idempotency_key=(
            f"autonomy:launch:{row['ticket_id']}:"
            f"attempt:{int(row.get('attempts') or 0) + 1}:"
            f"revision:{int(row.get('revision') or 0)}"
        ),
        payload={"ticket_id": str(row["ticket_id"])},
    )


def _audit_event(
    *,
    ticket_id: str,
    user_id: str,
    action: str,
    from_owner: str = "",
    to_owner: str = "",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ticket_id": ticket_id,
        "user_id": user_id,
        "actor": "user",
        "action": action,
        "from_owner": from_owner,
        "to_owner": to_owner,
        "detail": detail or {},
    }


def _same_id(left: Any, right: Any) -> bool:
    return str(left or "") == str(right or "")


def _heartbeat_after_request(heartbeat_at: Any, requested_at: Any) -> bool:
    """True when the lease was renewed after this stop was requested.

    That heartbeat belongs to the current attempt. A receipt from before it
    does not prove this attempt stopped.
    """
    if heartbeat_at is None or requested_at is None:
        return False
    return heartbeat_at > requested_at


def _stop_verified(
    stop: dict[str, Any] | None,
    daemon_id: Any,
    *,
    lease_id: Any = None,
    heartbeat_at: Any = None,
    live: bool = False,
) -> bool:
    """Whether this receipt proves the current daemon attempt is gone.

    A live lease is a runner. Acknowledgement clears the lease, so a lease
    that is still live was not the attempt a verified row already closed —
    the caller must request a fresh stop. A verified row otherwise counts
    only for this daemon, and only for the lease it names when the ticket
    still carries one. ``requested_at`` has to be at least as new as that
    lease's last heartbeat; an older receipt is a previous attempt.
    """
    if not stop or live:
        return False
    if stop.get("verification") != "verified":
        return False
    if stop.get("mode") not in {"daemon", "not_started"}:
        return False
    if not _same_id(stop.get("daemon_id"), daemon_id):
        return False
    # A never-started receipt has no lease. A lease now means a later claim.
    if stop.get("mode") == "not_started":
        return not str(lease_id or "")
    if str(lease_id or "") and not _same_id(stop.get("lease_id"), lease_id):
        return False
    if _heartbeat_after_request(heartbeat_at, stop.get("requested_at")):
        return False
    return True


def _pending_for_lease(
    stop: dict[str, Any] | None, daemon_id: Any, lease_id: Any
) -> bool:
    if not stop or stop.get("verification") != "pending":
        return False
    return (
        stop.get("mode") == "daemon"
        and _same_id(stop.get("daemon_id"), daemon_id)
        and _same_id(stop.get("lease_id"), lease_id)
    )


def _release_fence(row: dict[str, Any]) -> dict[str, Any]:
    """The ownership snapshot an internal stop must still match under its lock."""
    return {
        "revision": int(row.get("revision") or 0),
        "worker_daemon_id": row.get("worker_daemon_id"),
        "worker_lease_id": row.get("worker_lease_id"),
    }


def _refuse_if_changed(result: dict[str, Any]) -> None:
    if result.get("outcome") == "changed":
        raise TicketOpError(409, "Ticket changed; retry")


async def _request_stop(
    *, user_id: str, ticket_id: str, expected: dict[str, Any] | None = None
) -> dict[str, Any]:
    from src.server.services.autonomy import stop as stop_service

    try:
        if expected is None:
            return await stop_service.request_ticket_stop(
                user_id=user_id, ticket_id=ticket_id
            )
        return await stop_service.request_ticket_stop(
            user_id=user_id, ticket_id=ticket_id, expected=expected
        )
    except stop_service.StopNotFound as exc:
        raise TicketOpError(404, "Ticket not found") from exc
    except stop_service.StopAlreadyEnded as exc:
        raise TicketOpError(409, "Ticket already ended") from exc


async def stop_ticket(*, user_id: str, ticket_id: str) -> dict[str, Any]:
    row = await _owned_ticket(user_id, ticket_id)
    if row["status"] in TERMINAL_STATUSES:
        raise TicketOpError(409, "Ticket already ended")
    # The stopped event is written in the stop transaction, not after it.
    result = await _request_stop(user_id=user_id, ticket_id=ticket_id)
    return {"ticket": result["ticket"], "stop": result.get("stop")}


async def acknowledge_unverified_stop(
    *, user_id: str, ticket_id: str
) -> dict[str, Any]:
    """Accept an unverified stop once the daemon is dead, and audit that choice."""
    row = await _owned_ticket(user_id, ticket_id)
    if not row.get("worker_daemon_id"):
        raise TicketOpError(409, "Ticket is not owned by a daemon")
    from src.server.services.autonomy import stop as stop_service

    result = await stop_service.force_acknowledge_unverified(
        user_id=user_id, ticket_id=ticket_id, reason=_FORCE_REASON
    )
    outcome = result.get("outcome")
    if outcome == "not_found":
        raise TicketOpError(404, "Ticket not found")
    if outcome == "live_lease":
        raise TicketOpError(409, _PENDING_STOP)
    if outcome == "not_daemon":
        raise TicketOpError(409, "Ticket is not owned by a daemon")
    return result


async def _settle_daemon_release(
    row: dict[str, Any],
    *,
    user_id: str,
    ticket_id: str,
    require_verified: bool,
    force: bool,
) -> dict[str, Any]:
    """Decide whether a daemon-owned Ticket can leave its current owner.

    Archive does not need a verified stop. Reassign and resume do, unless the
    operator forces an unverified stop on a daemon that no longer holds a live
    lease. A Ticket that may still have a runner is not cancelled just to
    discover that the stop would be unverified.
    """
    daemon_id = row.get("worker_daemon_id")
    if not daemon_id:
        return row
    from src.server.services.autonomy import stop as stop_service

    snap = await stop_service.release_snapshot(user_id=user_id, ticket_id=ticket_id)
    if snap.get("outcome") == "not_found":
        raise TicketOpError(404, "Ticket not found")
    stop = snap.get("stop")
    live = bool(snap.get("live_lease"))
    launched = bool(snap.get("may_have_launched"))
    # get_ticket does not select lease columns. The snapshot does, and that
    # is the attempt a receipt has to match.
    snap_ticket = snap.get("ticket") or {}
    lease_id = snap_ticket.get("worker_lease_id", row.get("worker_lease_id"))
    heartbeat_at = snap_ticket.get(
        "worker_lease_heartbeat_at", row.get("worker_lease_heartbeat_at")
    )
    row = _with_lease(row, snap_ticket)
    fence = _release_fence(row)

    def covers(
        receipt: dict[str, Any] | None,
        *,
        lease: Any,
        heartbeat: Any,
        is_live: bool,
    ) -> bool:
        return _stop_verified(
            receipt,
            daemon_id,
            lease_id=lease,
            heartbeat_at=heartbeat,
            live=is_live,
        )

    if not covers(stop, lease=lease_id, heartbeat=heartbeat_at, is_live=live):
        # A running ticket that still holds a lease — live or merely expired —
        # needs a stop for this lease. An older verified row is not that stop.
        running = str(row.get("status") or "") == "running"
        if running and (live or lease_id):
            if require_verified and _pending_for_lease(stop, daemon_id, lease_id):
                raise TicketOpError(409, _PENDING_STOP)
            stopped = await _request_stop(
                user_id=user_id, ticket_id=ticket_id, expected=fence
            )
            _refuse_if_changed(stopped)
            fresh = stopped["ticket"]
            if not require_verified:
                # Archive keeps the lease so the daemon can still confirm.
                return fresh
            if not covers(
                stopped.get("stop"),
                lease=fresh.get("worker_lease_id"),
                heartbeat=fresh.get("worker_lease_heartbeat_at"),
                is_live=False,
            ):
                raise TicketOpError(409, _PENDING_STOPPED)
            return fresh

        if force:
            if live:
                raise TicketOpError(409, _PENDING_STOP)
            forced = await stop_service.force_acknowledge_unverified(
                user_id=user_id,
                ticket_id=ticket_id,
                reason=_FORCE_REASON,
                expected=fence,
            )
            outcome = forced.get("outcome")
            if outcome == "changed":
                raise TicketOpError(409, "Ticket changed; retry")
            if outcome == "live_lease":
                raise TicketOpError(409, _PENDING_STOP)
            if outcome == "not_found":
                raise TicketOpError(404, "Ticket not found")
            if outcome == "not_daemon":
                raise TicketOpError(409, "Ticket is not owned by a daemon")
            row = forced.get("ticket") or row
            stop = forced.get("stop") or stop
            # The acknowledgement bumped the revision; the following internal
            # stop must fence against the post-acknowledgement row, not the
            # pre-force snapshot.
            fence = _release_fence(row)
            lease_id = row.get("worker_lease_id")
            heartbeat_at = row.get("worker_lease_heartbeat_at")
            live = False

    if covers(stop, lease=lease_id, heartbeat=heartbeat_at, is_live=live):
        if str(row.get("status") or "") == "cancelled":
            refreshed = await _request_stop(
                user_id=user_id, ticket_id=ticket_id, expected=fence
            )
            _refuse_if_changed(refreshed)
            return refreshed["ticket"]
        return row

    if not require_verified:
        # Archiving never starts a runner. With no lease left to confirm,
        # there is no stop for the daemon to acknowledge.
        return row

    if stop and stop.get("verification") == "pending":
        raise TicketOpError(409, _PENDING_STOP)
    if stop and stop.get("verification") == "unverified":
        raise TicketOpError(409, _UNVERIFIED_STOP)
    if launched:
        raise TicketOpError(409, _UNVERIFIED_STOP)
    stopped = await _request_stop(
        user_id=user_id, ticket_id=ticket_id, expected=fence
    )
    _refuse_if_changed(stopped)
    fresh = stopped["ticket"]
    if not covers(
        stopped.get("stop"),
        lease=fresh.get("worker_lease_id"),
        heartbeat=fresh.get("worker_lease_heartbeat_at"),
        is_live=False,
    ):
        raise TicketOpError(409, _UNVERIFIED_STOP)
    return fresh


def _with_lease(row: dict[str, Any], snap_ticket: dict[str, Any]) -> dict[str, Any]:
    """Copy lease columns onto a ticket read that did not select them.

    Reassign fences on ``worker_lease_id``. A snapshot that saw a lease has
    to ride the row the fence uses, or a stale read would compare against NULL
    and miss the live row.
    """
    if not snap_ticket:
        return row
    merged = dict(row)
    for key in (
        "worker_lease_id",
        "worker_lease_claim_id",
        "worker_lease_expires_at",
        "worker_lease_heartbeat_at",
    ):
        if key in snap_ticket:
            merged[key] = snap_ticket[key]
    return merged


async def _refuse_if_mandate_revoked(row: dict[str, Any], ticket_id: str) -> None:
    revoked = await revoked_mandate_reason(row)
    if not revoked:
        return
    # Same transaction as the attention stamp: a revision miss writes neither
    # the stamp nor the audit row, and the caller retries against the new row.
    updated = await ticket_db.update_ticket(
        ticket_id,
        expected_status="cancelled",
        expected_revision=int(row.get("revision") or 0),
        attention={"question": f"Mandate revoked: {revoked}"},
        error=f"Mandate revoked: {revoked}",
        event=_audit_event(
            ticket_id=ticket_id,
            user_id=str(row["user_id"]),
            action="mandate_revoked",
            from_owner=line_db.owner_ref(row.get("worker_daemon_id")),
            detail={"from_status": "cancelled", "reason": revoked},
        ),
    )
    if updated is None:
        raise TicketOpError(409, "Ticket changed; retry")
    raise TicketOpError(409, f"Mandate revoked: {revoked}")


async def resume_ticket(
    *, user_id: str, ticket_id: str, force: bool = False
) -> dict[str, Any]:
    row = await _owned_ticket(user_id, ticket_id)
    if row.get("closed_at"):
        raise TicketOpError(409, "Ticket was closed")
    if row["status"] != "cancelled":
        raise TicketOpError(409, "Ticket is not stopped")
    if row.get("worker_daemon_id"):
        row = await _settle_daemon_release(
            row,
            user_id=user_id,
            ticket_id=ticket_id,
            require_verified=True,
            force=force,
        )
    await _refuse_if_mandate_revoked(row, ticket_id)
    from src.server.services.autonomy import launch_dedup

    # A resume is a deliberate re-run: the next claim bypasses launch de-dup.
    # The write is fenced on the revision, so replacing the mandate is safe.
    resume_fields: dict[str, Any] = {}
    if (
        row.get("worker_daemon_id")
        and isinstance(row.get("mandate"), dict)
        and launch_dedup.dedup_mode() != launch_dedup.MODE_OFF
    ):
        resume_fields["mandate"] = launch_dedup.with_force_next(row.get("mandate"))
    updated = await ticket_db.update_ticket(
        ticket_id,
        expected_status="cancelled",
        # Fence every resume: closing a cancelled Ticket keeps its status, so a
        # stale read must not overwrite the close stamp and re-enqueue a launch.
        expected_revision=int(row.get("revision") or 0),
        **resume_fields,
        status="queued",
        attention=None,
        error="",
        ended_at=None,
        event=_audit_event(
            ticket_id=ticket_id,
            user_id=user_id,
            action="resumed",
            to_owner=line_db.owner_ref(row.get("worker_daemon_id")),
            detail={"from_status": "cancelled"},
        ),
    )
    if updated is None:
        raise TicketOpError(409, "Ticket changed; retry")
    # Safe for daemon-owned Tickets too: the launch effect skips them and the
    # daemon's own claim path picks the Ticket up.
    await _reenqueue_launch(updated)
    return updated


async def reassign_ticket(
    *,
    user_id: str,
    ticket_id: str,
    daemon_id: str | None,
    force: bool = False,
) -> dict[str, Any]:
    original = await _owned_ticket(user_id, ticket_id)
    if original.get("closed_at"):
        raise TicketOpError(409, "Ticket was closed")
    if original["status"] in {"completed", "archived"}:
        raise TicketOpError(409, "Ticket already ended")
    target = None
    if daemon_id is not None:
        target = await daemon_db.get_daemon(user_id, daemon_id)
        if target is None:
            raise TicketOpError(404, "Daemon not found")
    current = original.get("worker_daemon_id") or None
    if current == (daemon_id or None):
        return original
    row = original
    if current:
        row = await _settle_daemon_release(
            original,
            user_id=user_id,
            ticket_id=ticket_id,
            require_verified=True,
            force=force,
        )
    else:
        await _cancel_cloud_worker_before_takeover(original)
    # Cloud and daemon use the same rule: a stopped Ticket restarts only while
    # its mandate still holds.
    if row["status"] == "cancelled":
        await _refuse_if_mandate_revoked(row, ticket_id)
    # The Ticket follows its new daemon's provider; a cloud reassignment keeps
    # the provider configuration the Ticket already carries.
    provider = (
        str(target.get("provider") or "") if target else str(row.get("worker_provider") or "")
    )
    model = "" if target else str(row.get("worker_model") or "")
    restart = row["status"] == "cancelled"
    new_status = "queued" if row["status"] in {"running", "cancelled"} else row["status"]
    updated = await line_db.reassign_ticket(
        ticket_id,
        user_id=user_id,
        from_daemon_id=current,
        worker_daemon_id=daemon_id,
        worker_provider=provider,
        worker_model=model,
        expected_revision=int(row.get("revision") or 0),
        expected_status=str(row["status"]),
        expected_lease_id=row.get("worker_lease_id"),
        restart_cancelled=restart,
        event=_audit_event(
            ticket_id=ticket_id,
            user_id=user_id,
            action="reassigned",
            from_owner=line_db.owner_ref(current),
            to_owner=line_db.owner_ref(daemon_id),
            detail={
                "from_status": row["status"],
                "status": new_status,
                **({"daemon_name": str(target.get("name") or "")} if target else {}),
                # The reassignment clears worker_thread_id; the server checkout
                # it orphans is removed only after this old server run ends,
                # possibly by a later retry that needs the thread from here.
                **(
                    {"from_worker_thread_id": str(row["worker_thread_id"])}
                    if current is None and row.get("worker_thread_id")
                    else {}
                ),
            },
        ),
    )
    if updated is None:
        raise TicketOpError(409, "Ticket changed; retry")
    updated = await _hand_over_ticket_worktree(
        updated,
        from_daemon_id=current,
        from_lease_id=row.get("worker_lease_id"),
        from_thread_id=row.get("worker_thread_id"),
    )
    if updated.get("status") == "queued" and daemon_id is None:
        await _reenqueue_launch(updated)
    return updated


async def _hand_over_ticket_worktree(
    row: dict[str, Any],
    *,
    from_daemon_id: str | None,
    from_lease_id: Any = None,
    from_thread_id: Any = None,
) -> dict[str, Any]:
    """Release the checkout the previous owner bound, so the new one can bind.

    A server checkout is removed once the cancelled server runner is gone
    (its old Worker thread, which the reassignment cleared from the row, is
    checked too), or kept as durable pending cleanup. A daemon checkout lives
    on that daemon's host, which the server cannot reach: when the released
    attempt's lease is known the binding is dropped fenced on host, daemon and
    that lease, so a newer bind (another lease) is never cleared. Otherwise
    the old daemon still owes its cleanup: it is recorded pending for that
    daemon, which removes its copy and reports through the cleanup route.
    Neither step may fail the reassignment that already happened.
    """
    from src.server.services.autonomy.ticket_worktree import (
        HOST_DAEMON,
        orphaned_server_checkout,
        release_server_ticket_worktree,
    )

    ticket_id = str(row.get("ticket_id") or "")
    path = str(row.get("worktree_path") or "")
    try:
        if orphaned_server_checkout(row):
            await release_server_ticket_worktree(
                row,
                reason="reassign",
                runner_thread_id=str(from_thread_id or "") or None,
            )
        elif (
            from_daemon_id
            and path
            and str(row.get("worktree_host") or "") == HOST_DAEMON
            and _same_id(row.get("worktree_daemon_id"), from_daemon_id)
            and not _same_id(row.get("worker_daemon_id"), from_daemon_id)
        ):
            generation = int(row.get("worktree_generation") or 0)
            lease_id = str(from_lease_id or "") or None
            cleared = False
            if lease_id is not None:
                cleared = await ticket_db.clear_ticket_worktree_binding(
                    ticket_id,
                    worktree_path=path,
                    worktree_generation=generation,
                    lease_id=lease_id,
                    host=HOST_DAEMON,
                    daemon_id=str(from_daemon_id),
                )
            if not cleared:
                await ticket_db.record_worktree_cleanup_pending(
                    ticket_id,
                    worktree_path=path,
                    worktree_generation=generation,
                    host=HOST_DAEMON,
                    daemon_id=str(from_daemon_id),
                )
        else:
            return row
    except Exception:  # noqa: BLE001 - the reassignment itself stands
        import logging

        logging.getLogger(__name__).warning(
            "[autonomy] could not hand over the Ticket worktree ticket=%s",
            ticket_id,
            exc_info=True,
        )
        return row
    return await ticket_db.get_ticket(ticket_id) or row


async def archive_ticket(*, user_id: str, ticket_id: str) -> dict[str, Any]:
    original = await _owned_ticket(user_id, ticket_id)
    if original["status"] == "archived":
        raise TicketOpError(409, "Ticket already archived")
    row = original
    if original.get("worker_daemon_id") and original["status"] not in {
        "completed",
        "archived",
    }:
        row = await _settle_daemon_release(
            original,
            user_id=user_id,
            ticket_id=ticket_id,
            require_verified=False,
            force=False,
        )
    else:
        await _cancel_cloud_worker_before_takeover(original)
    updated = await ticket_db.update_ticket(
        ticket_id,
        expected_status=row["status"],
        # Always fence the archive: while the cloud Worker cancellation runs, a
        # reassignment and a fresh daemon claim can advance the revision and put
        # the Ticket back to running. A stale archive must not end that new run.
        expected_revision=int(row.get("revision") or 0),
        status="archived",
        attention=None,
        ended_at=row.get("ended_at") or datetime.now(timezone.utc),
        event=_audit_event(
            ticket_id=ticket_id,
            user_id=user_id,
            action="archived",
            from_owner=line_db.owner_ref(row.get("worker_daemon_id")),
            detail={"from_status": row["status"]},
        ),
    )
    if updated is None:
        raise TicketOpError(409, "Ticket changed; retry")
    return updated


async def assign_ticket_line(
    *, user_id: str, ticket_id: str, line_id: str | None
) -> dict[str, Any]:
    row = await _owned_ticket(user_id, ticket_id)
    if line_id is not None:
        line = await line_db.get_line(user_id, line_id)
        if line is None:
            raise TicketOpError(404, "Line not found")
    await line_db.set_ticket_line(ticket_id, line_id)
    updated = await ticket_db.get_ticket(ticket_id)
    return updated if updated is not None else row
