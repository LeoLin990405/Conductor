"""The scheduler pass collects expired daemon output logs, a bounded batch at a time,
ahead of the steps that can fail."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.server.database.autonomy import daemon_output as output_db
from src.server.services.autonomy import scheduler


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    # The module's own handle only: the event loop shares the real time.monotonic.
    monkeypatch.setattr(
        scheduler, "time", SimpleNamespace(monotonic=lambda: now["t"])
    )
    return now


@pytest.fixture
def expire(monkeypatch):
    mock = AsyncMock(return_value=0)
    monkeypatch.setattr(output_db, "expire_old_logs", mock)
    return mock


def _scheduler() -> scheduler.AutonomyScheduler:
    return scheduler.AutonomyScheduler.__new__(scheduler.AutonomyScheduler)


@pytest.mark.asyncio
async def test_the_first_pass_collects_and_the_interval_then_throttles(clock, expire):
    instance = _scheduler()
    await instance._collect_expired_output()
    assert expire.await_count == 1
    clock["t"] += scheduler.OUTPUT_GC_INTERVAL_SECONDS - 1
    await instance._collect_expired_output()
    assert expire.await_count == 1
    clock["t"] += 1
    await instance._collect_expired_output()
    assert expire.await_count == 2


@pytest.mark.asyncio
async def test_a_full_batch_continues_on_the_next_pass(clock, expire):
    expire.return_value = output_db.EXPIRE_BATCH
    instance = _scheduler()
    await instance._collect_expired_output()
    clock["t"] += scheduler.PASS_INTERVAL_SECONDS
    await instance._collect_expired_output()
    assert expire.await_count == 2
    expire.return_value = output_db.EXPIRE_BATCH - 1  # the backlog is drained
    await instance._collect_expired_output()
    clock["t"] += scheduler.PASS_INTERVAL_SECONDS
    await instance._collect_expired_output()
    assert expire.await_count == 3


@pytest.mark.asyncio
async def test_a_failure_is_swallowed_and_waits_out_the_interval(clock, expire, caplog):
    expire.side_effect = RuntimeError("db down")
    instance = _scheduler()
    await instance._collect_expired_output()  # must not raise
    assert "daemon output expiry failed" in caplog.text
    clock["t"] += scheduler.PASS_INTERVAL_SECONDS
    await instance._collect_expired_output()
    assert expire.await_count == 1
    clock["t"] += scheduler.OUTPUT_GC_INTERVAL_SECONDS
    await instance._collect_expired_output()
    assert expire.await_count == 2


def _stub_the_pass(monkeypatch, *, poisoned: bool = False) -> scheduler.AutonomyScheduler:
    async def noop(*args, **kwargs):
        return None

    async def poison(*args, **kwargs):
        raise RuntimeError("poison row")

    for name in (
        "recover_stale_evals",
        "start_due_evals",
        "ensure_launch_effects",
        "poll_running_workers",
        "run_due_schedules",
        "enqueue_due_polls",
    ):
        monkeypatch.setattr(scheduler, name, noop)
    monkeypatch.setattr(scheduler, "watchdog_sweep", poison if poisoned else noop)
    from src.server.database.autonomy import daemons, stops

    monkeypatch.setattr(daemons, "recover_expired_daemon_leases", noop)
    monkeypatch.setattr(stops, "expire_stop_requests", noop)
    monkeypatch.setattr(
        "src.server.services.refactor.memory_consolidation.run_due_memory_consolidations",
        noop,
    )
    instance = _scheduler()
    instance.memory_store = None
    instance.main_agent_orchestrator = None
    return instance


@pytest.mark.asyncio
async def test_the_pass_runs_the_collection(monkeypatch, expire):
    await _stub_the_pass(monkeypatch)._run_pass()

    expire.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_a_step_that_fails_every_pass_does_not_starve_the_collection(
    monkeypatch, expire, clock
):
    instance = _stub_the_pass(monkeypatch, poisoned=True)

    with pytest.raises(RuntimeError, match="poison row"):
        await instance._run_pass()
    assert expire.await_count == 1

    # The interval still applies when the pass keeps failing, and the
    # collection comes due again on schedule.
    clock["t"] += scheduler.PASS_INTERVAL_SECONDS
    with pytest.raises(RuntimeError, match="poison row"):
        await instance._run_pass()
    assert expire.await_count == 1
    clock["t"] += scheduler.OUTPUT_GC_INTERVAL_SECONDS
    with pytest.raises(RuntimeError, match="poison row"):
        await instance._run_pass()
    assert expire.await_count == 2
