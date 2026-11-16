"""Durable handler for a Router Watchdog review turn."""

from __future__ import annotations

import logging

from src.server.database.autonomy import tickets as ticket_db

logger = logging.getLogger(__name__)


async def watchdog_review_effect(effect) -> None:
    """Dispatch one Router Watchdog turn for a running Ticket."""
    from src.server.services.autonomy.router import dispatch_watchdog_review

    ticket_id = str(effect.payload.get("ticket_id") or "")
    if not ticket_id:
        raise ValueError("watchdog effect has no ticket_id")
    ticket = await ticket_db.get_ticket(ticket_id)
    if ticket is None or ticket["status"] != "running":
        return
    reports = await ticket_db.list_reports(ticket_id)
    await dispatch_watchdog_review(ticket=ticket, reports=reports)
    logger.info("[autonomy] watchdog review dispatched ticket=%s", ticket_id)
