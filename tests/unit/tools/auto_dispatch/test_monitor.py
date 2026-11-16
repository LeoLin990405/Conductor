"""Monitor: stall detection, timeout and completion on a real log file."""

from __future__ import annotations

import pytest

from src.tools.auto_dispatch.execution import LogMonitor
from src.tools.auto_dispatch.types import RunStatus

from .fakes import Clock


def _write(runs_dir, run_id: str, text: str) -> None:
    (runs_dir / f"{run_id}.log").write_text(text, encoding="utf-8")


def _done(runs_dir, run_id: str) -> None:
    (runs_dir / f"{run_id}.exit").write_text("0", encoding="utf-8")


async def test_completion_is_reported_when_the_exit_marker_lands(tmp_path) -> None:
    clock = Clock()
    monitor = LogMonitor(tmp_path, clock=clock)
    _write(tmp_path, "run-1", "half a line")
    clock.advance(5)
    _done(tmp_path, "run-1")
    status = await monitor.poll("run-1")
    assert status.complete is True
    assert status.stalled is False
    assert "half a line" in status.output
    assert status.tokens == len("half a line") // 4


async def test_a_silent_log_stalls_after_the_stall_window(tmp_path) -> None:
    clock = Clock()
    monitor = LogMonitor(tmp_path, stall_seconds=60.0, clock=clock)
    _write(tmp_path, "run-1", "start")
    assert (await monitor.poll("run-1")).stalled is False
    clock.advance(59)
    assert (await monitor.poll("run-1")).stalled is False
    clock.advance(2)
    status = await monitor.poll("run-1")
    assert status.stalled is True
    assert status.complete is False


async def test_growth_resets_the_stall_window(tmp_path) -> None:
    clock = Clock()
    monitor = LogMonitor(tmp_path, stall_seconds=60.0, clock=clock)
    _write(tmp_path, "run-1", "a")
    clock.advance(50)
    _write(tmp_path, "run-1", "a" + "b" * 100)
    clock.advance(50)
    assert (await monitor.poll("run-1")).stalled is False


async def test_a_long_run_times_out(tmp_path) -> None:
    clock = Clock()
    monitor = LogMonitor(tmp_path, stall_seconds=600.0, timeout_seconds=600.0, clock=clock)
    _write(tmp_path, "run-1", "churning")
    assert (await monitor.poll("run-1")).timed_out is False
    clock.advance(599)
    assert (await monitor.poll("run-1")).timed_out is False
    clock.advance(2)
    status = await monitor.poll("run-1")
    assert status.timed_out is True
    assert status.stalled is False


async def test_cancel_forgets_the_run_and_calls_the_canceller(tmp_path) -> None:
    cancelled: list[str] = []

    async def canceller(run_id: str) -> None:
        cancelled.append(run_id)

    monitor = LogMonitor(tmp_path, canceller=canceller)
    _write(tmp_path, "run-1", "a")
    await monitor.poll("run-1")
    await monitor.cancel("run-1")
    assert cancelled == ["run-1"]
    # A fresh poll starts a new window rather than inheriting the old silence.
    clock_status = await monitor.poll("run-1")
    assert isinstance(clock_status, RunStatus)
    assert clock_status.stalled is False


async def test_a_missing_log_is_not_a_stall(tmp_path) -> None:
    monitor = LogMonitor(tmp_path, clock=Clock())
    status = await monitor.poll("never-started")
    assert status.stalled is False
    assert status.complete is False
    assert status.output == ""

pytestmark = pytest.mark.asyncio

