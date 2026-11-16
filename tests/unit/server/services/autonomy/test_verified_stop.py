"""Stop service: durable cancellation plus the one runner dispatch it needs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.server.services.autonomy import stop as stop_service


def _ticket(**overrides):
    row = {
        "ticket_id": "t1",
        "user_id": "u1",
        "status": "cancelled",
        "worker_thread_id": None,
        "worker_run_id": None,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_a_ptc_stop_dispatches_cancellation_but_claims_no_acknowledgement():
    dispatcher = MagicMock()
    dispatcher.cancel_thread = AsyncMock()
    db_result = {
        "outcome": "ok",
        "ticket": _ticket(worker_thread_id="thread-1", worker_run_id="run-1"),
        "stop": {"mode": "backend", "verification": "not_applicable"},
    }
    with (
        patch(
            "src.server.database.autonomy.stops.request_stop",
            new=AsyncMock(return_value=db_result),
        ),
        patch(
            "src.server.services.projects.dispatch.ProjectDispatcher",
            return_value=dispatcher,
        ),
    ):
        result = await stop_service.request_ticket_stop(
            user_id="u1", ticket_id="t1", timeout_seconds=30
        )

    assert result["stop"]["verification"] == "not_applicable"
    dispatcher.cancel_thread.assert_awaited_once_with(
        user_id="u1", thread_id="thread-1", run_id="run-1"
    )


@pytest.mark.asyncio
async def test_a_daemon_stop_does_not_dispatch_a_backend_thread():
    db_result = {
        "outcome": "ok",
        "ticket": _ticket(worker_thread_id="thread-1", worker_run_id="run-1"),
        "stop": {"mode": "daemon", "verification": "pending"},
    }
    with (
        patch(
            "src.server.database.autonomy.stops.request_stop",
            new=AsyncMock(return_value=db_result),
        ),
        patch("src.server.services.projects.dispatch.ProjectDispatcher") as dispatcher,
    ):
        await stop_service.request_ticket_stop(
            user_id="u1", ticket_id="t1", timeout_seconds=30
        )

    dispatcher.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_and_finished_tickets_raise_domain_errors():
    with patch(
        "src.server.database.autonomy.stops.request_stop",
        new=AsyncMock(
            return_value={"outcome": "not_found", "ticket": None, "stop": None}
        ),
    ):
        with pytest.raises(stop_service.StopNotFound):
            await stop_service.request_ticket_stop(
                user_id="u1", ticket_id="t1", timeout_seconds=30
            )
    with patch(
        "src.server.database.autonomy.stops.request_stop",
        new=AsyncMock(return_value={"outcome": "ended", "ticket": None, "stop": None}),
    ):
        with pytest.raises(stop_service.StopAlreadyEnded):
            await stop_service.request_ticket_stop(
                user_id="u1", ticket_id="t1", timeout_seconds=30
            )


@pytest.mark.asyncio
async def test_acknowledgement_delegates_process_evidence():
    acknowledge = AsyncMock(return_value={"outcome": "ok", "stop": {"verified": True}})
    with patch("src.server.database.autonomy.stops.acknowledge_stop", new=acknowledge):
        result = await stop_service.acknowledge_ticket_stop(
            daemon_id="d1",
            ticket_id="t1",
            lease_id="l1",
            process_gone=True,
            evidence={"processes": [{"pid": 7, "alive": False}]},
        )

    assert result["stop"]["verified"] is True
    acknowledge.assert_awaited_once_with(
        daemon_id="d1",
        ticket_id="t1",
        lease_id="l1",
        process_gone=True,
        evidence={"processes": [{"pid": 7, "alive": False}]},
    )


@pytest.mark.asyncio
async def test_scheduler_pass_escalates_due_stops(monkeypatch):
    from src.server.services.autonomy import scheduler as scheduler_module

    expire = AsyncMock(return_value=1)
    monkeypatch.setattr(
        "src.server.database.autonomy.stops.expire_stop_requests", expire
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.daemons.recover_expired_daemon_leases",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.daemon_output.expire_old_logs",
        AsyncMock(return_value=0),
    )
    for name in (
        "enqueue_due_polls",
        "recover_stale_evals",
        "start_due_evals",
        "ensure_launch_effects",
        "poll_running_workers",
        "run_due_schedules",
        "watchdog_sweep",
    ):
        monkeypatch.setattr(scheduler_module, name, AsyncMock(return_value=0))

    await scheduler_module.AutonomyScheduler()._run_pass()

    expire.assert_awaited_once_with()
