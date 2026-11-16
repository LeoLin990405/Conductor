"""Deterministic stall watchdog: which Tickets get attention, and why."""

from unittest.mock import AsyncMock

import pytest

from src.server.database.autonomy import tickets as ticket_db
from src.server.services.autonomy import watchdog


@pytest.mark.asyncio
async def test_watchdog_flags_stale_queued_waiting_and_running_tickets(monkeypatch):
    rows = [
        {"ticket_id": "t-queued", "status": "queued", "stall_seconds": 2400},
        {
            "ticket_id": "t-waiting",
            "status": "waiting",
            "stall_seconds": 1200,
            "summary": "Need the API key",
        },
        {"ticket_id": "t-running", "status": "running", "stall_seconds": 8000},
    ]
    update = AsyncMock(return_value={"ticket_id": "updated"})
    monkeypatch.setattr(
        ticket_db, "list_watchdog_candidates", AsyncMock(return_value=rows)
    )
    monkeypatch.setattr(ticket_db, "update_ticket", update)
    monkeypatch.setattr(watchdog, "_run_is_live", AsyncMock(return_value=True))
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(watchdog, "enqueue_effect", enqueue)

    assert await watchdog.watchdog_sweep() == 3
    enqueue.assert_awaited_once()
    assert enqueue.await_args.kwargs["kind"] == "watchdog_review"
    assert update.await_count == 3
    by_id = {
        call.args[0]: call.kwargs["attention"]["question"]
        for call in update.await_args_list
    }
    assert "Queued for 40 minutes" in by_id["t-queued"]
    assert "Waiting for input for 20 minutes" in by_id["t-waiting"]
    assert "Need the API key" in by_id["t-waiting"]
    assert "running for 133 minutes" in by_id["t-running"]


@pytest.mark.asyncio
async def test_watchdog_does_nothing_when_no_ticket_is_stale(monkeypatch):
    monkeypatch.setattr(
        ticket_db, "list_watchdog_candidates", AsyncMock(return_value=[])
    )
    update = AsyncMock()
    monkeypatch.setattr(ticket_db, "update_ticket", update)

    assert await watchdog.watchdog_sweep() == 0
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_watchdog_never_marks_main_agent_root_ticket_stale(monkeypatch):
    monkeypatch.setattr(
        ticket_db,
        "list_watchdog_candidates",
        AsyncMock(
            return_value=[
                {
                    "ticket_id": "root-1",
                    "status": "running",
                    "mandate": {"kind": "main_agent_root", "orchestration_only": True},
                }
            ]
        ),
    )
    update = AsyncMock()
    monkeypatch.setattr(ticket_db, "update_ticket", update)

    assert await watchdog.watchdog_sweep() == 0
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_watchdog_fails_a_running_ticket_whose_run_is_not_live(monkeypatch):
    monkeypatch.setattr(
        ticket_db,
        "list_watchdog_candidates",
        AsyncMock(
            return_value=[
                {
                    "ticket_id": "t-dead",
                    "status": "running",
                    "stall_seconds": 8000,
                    "worker_thread_id": "thread-1",
                    "worker_run_id": "run-1",
                }
            ]
        ),
    )
    update = AsyncMock(return_value={"ticket_id": "t-dead"})
    monkeypatch.setattr(ticket_db, "update_ticket", update)
    monkeypatch.setattr(watchdog, "_run_is_live", AsyncMock(return_value=False))
    enqueue = AsyncMock()
    monkeypatch.setattr(watchdog, "enqueue_effect", enqueue)

    assert await watchdog.watchdog_sweep() == 1
    enqueue.assert_not_awaited()
    kwargs = update.call_args.kwargs
    assert kwargs["status"] == "failed"
    assert "ended without a report" in kwargs["error"]
    assert "ended without a report" in kwargs["attention"]["question"]
    assert kwargs["ended_at"] is not None


@pytest.mark.asyncio
async def test_watchdog_reviews_a_running_ticket_despite_recent_progress(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(
        ticket_db,
        "list_watchdog_candidates",
        AsyncMock(
            return_value=[
                {
                    "ticket_id": "t-progress",
                    "status": "running",
                    "stall_seconds": 8000,
                    "progress_at": datetime.now(timezone.utc),
                }
            ]
        ),
    )
    update = AsyncMock()
    monkeypatch.setattr(ticket_db, "update_ticket", update)
    monkeypatch.setattr(watchdog, "_run_is_live", AsyncMock(return_value=True))
    enqueue = AsyncMock()
    monkeypatch.setattr(watchdog, "enqueue_effect", enqueue)

    assert await watchdog.watchdog_sweep() == 1
    update.assert_awaited_once()
    enqueue.assert_awaited_once()


def _running_ticket(**overrides):
    ticket = {
        "ticket_id": "t-1",
        "status": "running",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-1",
    }
    ticket.update(overrides)
    return ticket


def test_isolated_cloud_worker_liveness_uses_the_mandate_heartbeat():
    from datetime import datetime, timedelta, timezone

    fresh = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    live = _running_ticket(
        mandate={"cloud_worker": {"isolated": True, "heartbeat_at": fresh}}
    )
    dead = _running_ticket(
        mandate={"cloud_worker": {"isolated": True, "heartbeat_at": stale}}
    )
    missing = _running_ticket(mandate={"cloud_worker": {"isolated": True}})
    not_isolated = _running_ticket(mandate={"cloud_worker": {"heartbeat_at": fresh}})

    assert watchdog._cloud_worker_heartbeat_live(live) is True
    assert watchdog._cloud_worker_heartbeat_live(dead) is False
    assert watchdog._cloud_worker_heartbeat_live(missing) is False
    # An ordinary PTC/native run keeps using the LocalRunExecutor probe.
    assert watchdog._cloud_worker_heartbeat_live(not_isolated) is None


@pytest.mark.asyncio
async def test_isolated_cloud_worker_liveness_skips_the_ptc_probe(monkeypatch):
    from datetime import datetime, timezone

    registry = AsyncMock()
    monkeypatch.setattr(
        "src.server.services.runs.executor.LocalRunExecutor.get_instance",
        lambda: registry,
    )
    ticket = _running_ticket(
        mandate={
            "cloud_worker": {
                "isolated": True,
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        }
    )

    assert await watchdog._run_is_live(ticket) is True
    registry.is_run_live.assert_not_called()
