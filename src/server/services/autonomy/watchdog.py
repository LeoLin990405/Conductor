"""Deterministic Ticket watchdog.

The article's Watchdog is a separate model session that judges whether a
Worker is making progress. V1 starts with the deterministic half: surface
Tickets that have stopped moving long enough that a person should look, without
another model call. A stale Ticket is flagged through its attention slot, which
already drives the Inbox "needs you" count and row.

Thresholds are deliberately generous: a queued Ticket that never started, a
Task waiting for input, and a long-running Worker are the three states where
silence is ambiguous. Once flagged, ``attention`` is non-null and the sweep
skips the row, so this cannot spam.

The same pass emits a stuck-Ticket notification when the Ticket has been
quiet for ``CONDUCTOR_TICKET_STUCK_SECONDS`` (30 minutes unless that
variable says otherwise). The scan runs before the attention write, and
the write keeps the quiet ``updated_at``. That column is the stuck clock
for a queued, waiting, or blocked Ticket, and for a running Ticket that
has no ``started_at``. A write that moved it to now would make the next
scan miss the stall. The retain would then delete the alert, including a
banner the cap had not shown yet, and a later sweep would banner the same
stall again. The notification is de-duplicated and does not change the flag.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.server.database.autonomy import tickets as ticket_db
from src.server.database.autonomy.effects import enqueue_effect
from src.server.services.autonomy.models import EFFECT_WATCHDOG_REVIEW
from src.server.services.autonomy.silent_worker import sweep_silent_workers
from src.server.services.autonomy.worker_idle import sweep_worker_idle_wakes

logger = logging.getLogger(__name__)

# Seconds of no state change before a row is worth a person's glance.
QUEUED_STALL_SECONDS = 30 * 60
WAITING_STALL_SECONDS = 15 * 60
RUNNING_STALL_SECONDS = 2 * 60 * 60
# A containerized Worker has no LocalRunExecutor run to probe; its launch
# heartbeat is the durable liveness signal. Older than this and the run is
# treated as gone (the backend that renewed it is dead).
CLOUD_WORKER_HEARTBEAT_MAX_AGE_SECONDS = 10 * 60


def _quiet_timestamp(value: object) -> datetime | None:
    """The stuck clock as a timestamp, or None when this row has none."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _put_quiet_clock_back(
    row: dict[str, Any],
    updated: dict[str, Any] | None,
    *,
    quiet_at: object,
    quiet_age: object,
    quiet_stall: object,
) -> None:
    """Put the pre-write quiet clock back on the row the next scan reads.

    ``update_ticket`` moves ``updated_at`` to now unless the caller preserves
    it. A stub of that write, or a writer that applied now anyway, shares
    these dicts with the stuck scan. Restoring the values the sweep observed
    keeps an unresolved stall in the next result set, so retain does not
    drop its alert or a banner that is still waiting.
    """
    for target in (row, updated):
        if not isinstance(target, dict):
            continue
        target["updated_at"] = quiet_at
        if quiet_age is not None:
            target["age_seconds"] = quiet_age
        if quiet_stall is not None:
            target["stall_seconds"] = quiet_stall


def _question(ticket: dict[str, Any]) -> str:
    status = str(ticket.get("status") or "")
    minutes = max(1, int((_age_seconds(ticket) or 0) // 60))
    summary = str(ticket.get("summary") or "").strip()
    tail = f" Latest summary: {summary[:300]}" if summary else ""
    if status == "queued":
        return (
            f"Queued for {minutes} minutes without a Worker start. "
            "Open the Ticket to retry or stop it."
        )
    if status == "waiting":
        return (
            f"Waiting for input for {minutes} minutes. "
            "Open the Ticket to answer or stop it."
            f"{tail}"
        )
    return (
        f"Worker has been running for {minutes} minutes without completing. "
        "Open the Ticket to inspect the run, or stop/retry it."
        f"{tail}"
    )


def _age_seconds(ticket: dict[str, Any]) -> float | None:
    # The DB supplies the age explicitly in the sweep query; tests can omit it.
    value = ticket.get("stall_seconds")
    return float(value) if value is not None else None


def _cloud_worker_heartbeat_live(ticket: dict[str, Any]) -> bool | None:
    """Liveness for a Docker-isolated Worker from its mandate heartbeat."""
    mandate = dict(ticket.get("mandate") or {})
    marker = mandate.get("cloud_worker")
    if not isinstance(marker, dict) or not marker.get("isolated"):
        return None
    heartbeat = marker.get("heartbeat_at")
    if not isinstance(heartbeat, str) or not heartbeat.strip():
        return False
    try:
        renewed = datetime.fromisoformat(heartbeat)
    except ValueError:
        return False
    if renewed.tzinfo is None:
        renewed = renewed.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - renewed).total_seconds()
    return age <= CLOUD_WORKER_HEARTBEAT_MAX_AGE_SECONDS


async def _run_is_live(ticket: dict[str, Any]) -> bool | None:
    """Whether the Worker run is still executing; None when unknowable."""
    thread_id = str(ticket.get("worker_thread_id") or "")
    run_id = str(ticket.get("worker_run_id") or "")
    if not thread_id or not run_id:
        return None
    heartbeated = _cloud_worker_heartbeat_live(ticket)
    if heartbeated is not None:
        return heartbeated
    try:
        from src.server.services.runs.executor import LocalRunExecutor

        return await LocalRunExecutor.get_instance().is_run_live(thread_id, run_id)
    except Exception:  # noqa: BLE001 - a read failure falls back to attention
        return None


def _dead_run_question(ticket: dict[str, Any]) -> str:
    minutes = max(1, int((_age_seconds(ticket) or 0) // 60))
    return (
        f"Worker run ended without a report after {minutes} minutes. "
        "The Ticket was marked failed; open it to retry or stop."
    )


async def watchdog_sweep(
    *, limit: int = 50, main_agent_orchestrator: Any | None = None
) -> int:
    """Flag stale queued/waiting/running Tickets for human attention."""
    # A silent Worker is woken first. The wake does not change what this
    # sweep does next: a dead run is still failed, and a Ticket that has
    # been running past the stall threshold is still flagged. A stored
    # notice is not a wake, so it cannot hide either path.
    await sweep_worker_idle_wakes()
    # A Worker that produced no output is a dispatch failure of its own
    # class (worker_no_output). Isolated: it must not break this pass.
    await _sweep_silent_workers()
    # Before any attention write, and the write below keeps updated_at.
    # Scanning first is not enough: the next sweep reads the clock again.
    await _notify_stuck_tickets()
    rows = await ticket_db.list_watchdog_candidates(
        queued_seconds=QUEUED_STALL_SECONDS,
        waiting_seconds=WAITING_STALL_SECONDS,
        running_seconds=RUNNING_STALL_SECONDS,
        limit=limit,
    )
    flagged = 0
    for row in rows:
        ticket_id = str(row.get("ticket_id") or "")
        if not ticket_id:
            continue
        mandate = dict(row.get("mandate") or {})
        if mandate.get("orchestration_only") or mandate.get("kind") == "main_agent_root":
            continue
        status = str(row.get("status") or "")
        live = await _run_is_live(row) if status == "running" else None
        if status == "running" and live is False:
            # A run that ended takes the Ticket with it. A wake in this same
            # pass does not keep a dead run in ``running``.
            updated = await ticket_db.update_ticket(
                ticket_id,
                status="failed",
                attention={"question": _dead_run_question(row)},
                error="Worker run ended without a report (watchdog)",
                ended_at=datetime.now(timezone.utc),
            )
        else:
            # Stall handling is the same whether or not this pass woke the
            # Worker. The wake already happened; skipping the flag would
            # hide a Ticket that has been running past the stall threshold.
            # The attention row is not progress. Keep the quiet clock so the
            # next scan still returns this stall and retain keeps its alert.
            # Fence both values to the scanned row: concurrent progress moves
            # the revision, so this write cannot restore an older timestamp.
            quiet_at = row.get("updated_at")
            quiet_age = row.get("age_seconds")
            quiet_stall = row.get("stall_seconds")
            preserve = _quiet_timestamp(quiet_at)
            write: dict[str, Any] = {
                "attention": {"question": _question(row)},
                "expected_status": status,
            }
            if row.get("revision") is not None:
                write["expected_revision"] = int(row["revision"])
            if preserve is not None:
                write["preserve_updated_at"] = preserve
            updated = await ticket_db.update_ticket(ticket_id, **write)
            if updated is not None and quiet_at is not None:
                _put_quiet_clock_back(
                    row,
                    updated,
                    quiet_at=quiet_at,
                    quiet_age=quiet_age,
                    quiet_stall=quiet_stall,
                )
        if updated is not None:
            flagged += 1
            if status == "running" and live is not False:
                await enqueue_effect(
                    user_id=str(row.get("user_id") or ""),
                    kind=EFFECT_WATCHDOG_REVIEW,
                    idempotency_key=(
                        f"autonomy:watchdog-review:{ticket_id}:"
                        f"{row.get('updated_at') or ''}"
                    ),
                    payload={"ticket_id": ticket_id},
                )
            logger.warning(
                "[autonomy] watchdog flagged ticket=%s status=%s age_s=%s live=%s",
                ticket_id,
                status,
                row.get("stall_seconds"),
                live,
            )
    if main_agent_orchestrator is not None:
        try:
            alerts = await main_agent_orchestrator.watchdog_sweep(user_id=None)
            if alerts:
                logger.warning(
                    "[main-agent] watchdog persisted stalled-task alerts count=%s",
                    len(alerts),
                )
        except Exception:  # noqa: BLE001 - main-agent alerting cannot block Ticket recovery
            logger.exception(
                "[main-agent] watchdog sweep failed; next scheduler pass will retry"
            )
    return flagged


async def _sweep_silent_workers() -> None:
    """Classify silent Worker runs; a failure is logged and retried next pass."""
    try:
        await sweep_silent_workers()
    except Exception:  # noqa: BLE001 - the silent-worker sweep is not this pass's result
        logger.exception("[autonomy] silent-worker sweep failed")


async def _notify_stuck_tickets() -> None:
    """Emit stuck-Ticket notifications without changing the flag count.

    The attention flag above is the Inbox row. This is the operator alert.
    A failure here is retried on the next pass and must not undo the flags.
    """
    try:
        from src.server.services.autonomy.notifications import check_stuck_tickets

        await check_stuck_tickets()
    except Exception:  # noqa: BLE001 - notifying is not the sweep's result
        logger.exception("[autonomy] stuck-ticket notification check failed")
