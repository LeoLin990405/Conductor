"""Stop requests: cancel a Ticket and verify that its native runner stopped."""

from __future__ import annotations

import logging
from typing import Any

from src.config.settings import get_autonomy_stop_ack_timeout
from src.server.database.autonomy import stops as stop_db

logger = logging.getLogger(__name__)


class StopNotFound(Exception):
    """The Ticket does not exist for this user."""


class StopAlreadyEnded(Exception):
    """The Ticket completed before the stop request arrived."""


async def request_ticket_stop(
    *,
    user_id: str,
    ticket_id: str,
    timeout_seconds: float | None = None,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timeout = (
        get_autonomy_stop_ack_timeout() if timeout_seconds is None else timeout_seconds
    )
    result = await stop_db.request_stop(
        user_id=user_id,
        ticket_id=ticket_id,
        timeout_seconds=timeout,
        expected=expected,
    )
    if result["outcome"] == "not_found":
        raise StopNotFound(ticket_id)
    if result["outcome"] == "ended":
        raise StopAlreadyEnded(ticket_id)
    ticket = result.get("ticket") or {}
    stop = result.get("stop") or {}
    if stop.get("mode") == "backend" and ticket.get("worker_thread_id"):
        # PTC cancellation is a runner request without a native daemon receipt,
        # so the stop row says not_applicable rather than claiming verification.
        from src.server.services.projects.dispatch import ProjectDispatcher

        await ProjectDispatcher().cancel_thread(
            user_id=user_id,
            thread_id=str(ticket["worker_thread_id"]),
            run_id=ticket.get("worker_run_id"),
        )
    if result.get("outcome") == "ok" and stop.get("mode") in {"backend", "not_started"}:
        await _release_server_worktree(ticket)
    return result


async def _release_server_worktree(ticket: dict[str, Any]) -> None:
    """Release the server checkout bound in the stop's own Ticket snapshot.

    Fenced on that snapshot's path and generation, so a newer attempt bound
    after the stop is never touched, and removal still waits until the
    server runner is confirmed ended (otherwise it stays pending for the
    retry sweep). Daemon-host checkouts are skipped: the daemon removes them
    itself after its verified stop acknowledgement is accepted.
    """
    from src.server.services.autonomy.ticket_worktree import (
        release_server_ticket_worktree,
    )

    try:
        await release_server_ticket_worktree(ticket, reason="stop")
    except Exception:  # noqa: BLE001 - the stop itself is already durable
        logger.warning(
            "[autonomy] ticket worktree release failed after stop ticket=%s",
            ticket.get("ticket_id"),
            exc_info=True,
        )


async def release_snapshot(*, user_id: str, ticket_id: str) -> dict[str, Any]:
    """Read the stop and whether a daemon lease is still live. No writes."""
    return await stop_db.release_snapshot(user_id=user_id, ticket_id=ticket_id)


async def force_acknowledge_unverified(
    *,
    user_id: str,
    ticket_id: str,
    reason: str,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Operator override for a dead daemon whose stop can no longer be verified."""
    return await stop_db.force_acknowledge_unverified(
        user_id=user_id, ticket_id=ticket_id, reason=reason, expected=expected
    )


async def acknowledge_ticket_stop(
    *,
    daemon_id: str,
    ticket_id: str,
    lease_id: str,
    process_gone: bool,
    evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    # Deliberately no server-side checkout cleanup here: a daemon ack only
    # proves the daemon's runner is gone, and the daemon removes its own host
    # checkout (reporting through the fenced worktree/cleanup route). Loading
    # the current binding after an ack would let a replayed old ack delete a
    # newer attempt's checkout.
    return await stop_db.acknowledge_stop(
        daemon_id=daemon_id,
        ticket_id=ticket_id,
        lease_id=lease_id,
        process_gone=process_gone,
        evidence=evidence,
    )
