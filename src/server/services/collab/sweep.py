"""``collab_lease_sweep``: expire stale app leases and requeue their Tickets.

Every 30 seconds (N80 7):

* a lease past its ``expires_at`` or four-hour ceiling expires and its Ticket
  goes back to ``queued`` for the Router (``lease_expired``), in the same
  statement and transaction as the ownership event;
* a handoff nobody accepted in time releases its lease and requeues the
  Ticket (``released``, reason ``handoff_expired``);
* stale review requests expire;
* a lease the current rollout stage no longer allows (``modes.lease_allowed``:
  every lease in ``off`` / ``shadow``, a non-docs lease in ``docs``) is
  expired with reason ``mode_change``.

Branches and commits are kept, so the next holder (Worker or app) starts from
what the previous one left. In ``off`` the sweeper runs a single pass at
startup (to expire anything a previous stage left) and then stops: the mode
is read from the environment, which cannot change without a restart.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from src.collab import modes
from src.server.database.autonomy import tickets as ticket_db
from src.server.database.collab import handoffs as handoff_db
from src.server.database.collab import leases as lease_db
from src.server.database.collab import reviews as review_db
from src.server.database.collab.transactions import collab_transaction
from src.server.services.collab import tickets as task_svc

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 30.0
SWEEP_BATCH = 100


async def _event(
    conn: Any,
    *,
    ticket_id: str | None,
    app_id: str,
    collab_event: str,
    reason: str,
    lease_id: str,
) -> None:
    if not ticket_id:
        return
    ticket = await ticket_db.get_ticket(str(ticket_id), conn=conn)
    if ticket is None:
        return
    await task_svc.record_event(
        conn,
        ticket=ticket,
        actor=f"app:{app_id}",
        collab_event=collab_event,
        from_owner=f"app:{app_id}",
        to_owner="router",
        reason=reason,
        extra={"lease_id": lease_id, "by": "collab_lease_sweep"},
    )


async def _expire_expired() -> int:
    async with collab_transaction() as conn:
        rows = await lease_db.sweep_expired_leases(limit=SWEEP_BATCH, conn=conn)
        for row in rows:
            await _event(
                conn,
                ticket_id=row.get("ticket_id"),
                app_id=str(row.get("app_id") or ""),
                collab_event="lease_expired",
                reason="lease_expired",
                lease_id=str(row.get("lease_id") or ""),
            )
    return len(rows)


async def _expire_handoffs() -> int:
    async with collab_transaction() as conn:
        rows = await handoff_db.expire_handoffs(limit=SWEEP_BATCH, conn=conn)
        for row in rows:
            if not int(row.get("released") or 0):
                continue
            lease = await lease_db.get_lease(str(row.get("lease_id") or ""), conn=conn)
            if lease is None:
                continue
            await _event(
                conn,
                ticket_id=lease.get("ticket_id"),
                app_id=str(lease.get("app_id") or ""),
                collab_event="released",
                reason="handoff_expired",
                lease_id=str(lease.get("lease_id") or ""),
            )
    return len(rows)


async def _expire_for_mode(mode: str, environ: Mapping[str, str] | None) -> int:
    expired = 0
    for lease in await lease_db.list_active_leases():
        ticket_id = lease.get("ticket_id")
        ticket = await ticket_db.get_ticket(str(ticket_id)) if ticket_id else None
        facts = task_svc.task_facts(ticket) if ticket is not None else modes.TaskFacts(kind="line")
        if modes.lease_allowed(facts, mode=mode, environ=environ):
            continue
        async with collab_transaction() as conn:
            row = await lease_db.release_lease(
                str(lease["lease_id"]),
                app_id=str(lease["app_id"]),
                reason="mode_change",
                state="expired",
                conn=conn,
            )
            if row is None:
                continue
            await _event(
                conn,
                ticket_id=ticket_id,
                app_id=str(lease["app_id"]),
                collab_event="lease_expired",
                reason="mode_change",
                lease_id=str(lease["lease_id"]),
            )
            expired += 1
    return expired


async def sweep_once(*, environ: Mapping[str, str] | None = None) -> dict[str, int]:
    """One pass; each part is its own transaction so one failure stops only itself."""
    mode = modes.resolve_mode(None, environ=environ)
    result = {"expired": 0, "handoffs": 0, "reviews": 0, "mode_expired": 0}
    try:
        result["expired"] = await _expire_expired()
    except Exception:  # noqa: BLE001
        logger.warning("[collab] lease expiry pass failed", exc_info=True)
    try:
        result["handoffs"] = await _expire_handoffs()
    except Exception:  # noqa: BLE001
        logger.warning("[collab] handoff expiry pass failed", exc_info=True)
    try:
        result["reviews"] = len(await review_db.expire_reviews(limit=SWEEP_BATCH))
    except Exception:  # noqa: BLE001
        logger.warning("[collab] review expiry pass failed", exc_info=True)
    try:
        result["mode_expired"] = await _expire_for_mode(mode, environ)
    except Exception:  # noqa: BLE001
        logger.warning("[collab] mode expiry pass failed", exc_info=True)
    return result


class CollabLeaseSweeper:
    """Per-worker background loop; ``get_instance`` / ``start`` / ``stop``."""

    _instance: "CollabLeaseSweeper | None" = None

    def __init__(self, *, interval: float = SWEEP_INTERVAL_SECONDS) -> None:
        self.interval = interval
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def get_instance(cls) -> "CollabLeaseSweeper":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        while True:
            await sweep_once()
            if modes.current_mode() == modes.OFF:
                return
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.get_running_loop().create_task(
            self._run(), name="collab_lease_sweep"
        )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


collab_lease_sweep = sweep_once

__all__ = [
    "CollabLeaseSweeper",
    "SWEEP_INTERVAL_SECONDS",
    "collab_lease_sweep",
    "sweep_once",
]
