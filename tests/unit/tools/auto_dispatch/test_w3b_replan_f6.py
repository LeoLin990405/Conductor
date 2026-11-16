"""F6: a fast poll does not sleep past the watchdog deadline."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch.gates import GateRunner, GateSettings
from src.tools.auto_dispatch.machine import MachineConfig
from src.tools.auto_dispatch.types import RunStatus
from tests.unit.tools.auto_dispatch.test_gate_recovery import (
    WHEN,
    FakeWorktrees,
    ScriptedCommands,
    _bundle,
    _line,
)

pytestmark = pytest.mark.asyncio


class ImmediatePoll:
    def __init__(self) -> None:
        self.polls = 0

    async def poll(self, run_id: str) -> RunStatus:
        del run_id
        self.polls += 1
        return RunStatus(output="", complete=False)

    async def cancel(self, run_id: str) -> None:
        del run_id


async def test_f6_a_fast_poll_leaves_running_at_the_deadline(tmp_path) -> None:
    now = [WHEN]
    settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=tmp_path / "repo",
        g5_command="pytest -q",
        watchdog_seconds=5,
        heartbeat_interval_seconds=1,
    )
    collaborators, machine, _commands, gates = _bundle(tmp_path, [], settings=settings)
    gates._clock = lambda: now[0]
    machine._config = MachineConfig(poll_interval_seconds=10)
    monitor = ImmediatePoll()
    collaborators.monitor = monitor
    line = _line("l", assigned_model="kimi", status=LineStatus.RUNNING, current_run="run-1")
    dispatch = Dispatch(
        id="deadline",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        lines=[line],
    )
    gates.prepare(dispatch)
    await collaborators.store.create(dispatch)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        task = asyncio.current_task()
        if task is not None and task.get_name() == "dispatch-deadline":
            await asyncio.Event().wait()
        sleeps.append(seconds)
        now[0] += timedelta(seconds=seconds)

    machine._sleep = sleep
    await machine._monitor(dispatch)
    elapsed = (now[0] - WHEN).total_seconds()
    assert elapsed == 5
    assert dispatch.lines[0].status is not LineStatus.RUNNING
    assert dispatch.lines[0].failure_reason == "watchdog_timeout"
    assert dispatch.state is not DispatchState.ERROR
    assert all(item <= 1 for item in sleeps)


async def test_f6_watch_expired_is_strictly_after_the_deadline(tmp_path) -> None:
    """Armed expiry is ``now > deadline``. Equality is not expired.

    The post-poll path uses remaining ``<= 0`` and is covered by the test above.
    A non-positive watchdog is expired immediately.
    """
    now = [WHEN]
    settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=tmp_path / "repo",
        g5_command="pytest -q",
        watchdog_seconds=5,
    )
    gates = GateRunner(
        settings,
        commands=ScriptedCommands(),
        worktrees=FakeWorktrees(),
        clock=lambda: now[0],
    )
    dispatch = Dispatch(
        id="boundary",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        lines=[_line("l", assigned_model="kimi")],
    )
    gates.prepare(dispatch)
    assert gates.arm_watch(dispatch, 10) is True
    assert gates.watch_expired(dispatch) is False
    now[0] = WHEN + timedelta(seconds=5)
    assert gates.watch_expired(dispatch) is False
    now[0] = WHEN + timedelta(seconds=5, microseconds=1)
    assert gates.watch_expired(dispatch) is True
    zero = GateRunner(
        GateSettings(
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            g5_command="pytest -q",
            watchdog_seconds=0,
        ),
        commands=ScriptedCommands(),
        worktrees=FakeWorktrees(),
        clock=lambda: now[0],
    )
    assert zero.watch_expired(dispatch) is True
