from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.server.services.autonomy import scheduler


@pytest.mark.asyncio
async def test_scheduler_pass_invokes_opt_in_consolidation_with_shared_store(monkeypatch):
    async def noop(*args, **kwargs):
        return None

    for name in (
        "recover_stale_evals",
        "start_due_evals",
        "ensure_launch_effects",
        "poll_running_workers",
        "run_due_schedules",
        "watchdog_sweep",
        "enqueue_due_polls",
    ):
        monkeypatch.setattr(scheduler, name, noop)

    from src.server.database.autonomy import daemons, stops

    monkeypatch.setattr(daemons, "recover_expired_daemon_leases", noop)
    monkeypatch.setattr(stops, "expire_stop_requests", noop)
    monkeypatch.setattr(
        "src.server.database.autonomy.daemon_output.expire_old_logs",
        AsyncMock(return_value=0),
    )
    consolidate = AsyncMock(return_value={})
    monkeypatch.setattr(
        "src.server.services.refactor.memory_consolidation.run_due_memory_consolidations",
        consolidate,
    )

    instance = scheduler.AutonomyScheduler.__new__(scheduler.AutonomyScheduler)
    instance.memory_store = object()
    instance.main_agent_orchestrator = None

    await instance._run_pass()

    consolidate.assert_awaited_once_with(store=instance.memory_store)


@pytest.mark.asyncio
async def test_consolidation_failure_does_not_abort_scheduler_pass(monkeypatch):
    async def noop(*args, **kwargs):
        return None

    for name in (
        "recover_stale_evals",
        "start_due_evals",
        "ensure_launch_effects",
        "poll_running_workers",
        "run_due_schedules",
        "watchdog_sweep",
        "enqueue_due_polls",
    ):
        monkeypatch.setattr(scheduler, name, noop)

    from src.server.database.autonomy import daemons, stops

    monkeypatch.setattr(daemons, "recover_expired_daemon_leases", noop)
    monkeypatch.setattr(stops, "expire_stop_requests", noop)
    monkeypatch.setattr(
        "src.server.database.autonomy.daemon_output.expire_old_logs",
        AsyncMock(return_value=0),
    )

    async def broken(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(
        "src.server.services.refactor.memory_consolidation.run_due_memory_consolidations",
        broken,
    )
    instance = scheduler.AutonomyScheduler.__new__(scheduler.AutonomyScheduler)
    instance.memory_store = object()
    instance.main_agent_orchestrator = None

    await instance._run_pass()
