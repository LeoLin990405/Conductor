"""Durable wakeup: a dead monitor, a late event, a duplicate, and the heartbeat.

Fakes only. The monitor is the protocol object the machine polls. Killing the
machine task is the monitor process going away. The event is pushed into the
serialized gates row while no machine is polling, then a new machine sweeps it.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.server.services.dispatch import dispatch_summary
from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch.gates import GateSettings
from src.tools.auto_dispatch.types import RunStatus
from tests.unit.tools.auto_dispatch.test_gate_recovery import (
    WHEN,
    ScriptedCommands,
    _bundle,
    _line,
)
from tests.unit.tools.auto_dispatch.test_g2_durable_pin import SerializedStore

pytestmark = pytest.mark.asyncio


class SilentMonitor:
    """Never reports completion. A sweep is the only way the line moves on."""

    def __init__(self) -> None:
        self.polls = 0

    async def poll(self, run_id: str) -> RunStatus:
        del run_id
        self.polls += 1
        return RunStatus(output="still running", complete=False)

    async def cancel(self, run_id: str) -> None:
        del run_id


async def test_a_killed_monitor_sweeps_an_event_that_arrived_while_it_was_down(
    tmp_path,
) -> None:
    store = SerializedStore()
    entered = asyncio.Event()

    async def _hold(_seconds: float) -> None:
        entered.set()
        await asyncio.Future()

    settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=tmp_path / "repo",
        g5_command="true",
    )
    waiting = SilentMonitor()
    collaborators, machine, _commands, gates = _bundle(
        tmp_path,
        [_line("line-0")],
        store=store,
        settings=settings,
    )
    collaborators.monitor = waiting
    machine._sleep = _hold
    dispatch = await store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now", spec_ref="spec-13")
    )
    task = asyncio.create_task(machine.run(dispatch))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert waiting.polls >= 1

    restored = await store.get("d1")
    assert restored is not None
    assert restored.state is DispatchState.MONITORING
    restored_line = restored.line("line-0")
    assert restored_line is not None
    assert restored_line.status is LineStatus.RUNNING
    event_id = "run-line-0-1"
    assert gates.push_wakeup(
        restored,
        event_id,
        "run_complete",
        "line-0",
        run_id=restored_line.current_run,
        attempt=restored_line.attempts,
    ) is True
    assert gates.push_wakeup(
        restored,
        event_id,
        "run_complete",
        "line-0",
        run_id=restored_line.current_run,
        attempt=restored_line.attempts,
    ) is False
    await store.save(restored)

    async def _fail_if_waiting(_seconds: float) -> None:
        raise AssertionError("the new machine waited on a dead monitor")

    replay_monitor = SilentMonitor()
    replay_commands = ScriptedCommands()
    _collaborators, resumed, replay_commands, replay_gates = _bundle(
        tmp_path,
        [],
        commands=replay_commands,
        settings=settings,
        store=store,
    )
    _collaborators.monitor = replay_monitor
    resumed._sleep = _fail_if_waiting
    result = await resumed.run(await store.get("d1"))
    assert replay_monitor.polls == 0
    assert result.line("line-0").status is LineStatus.PASSED
    assert result.state is DispatchState.APPROVAL
    assert replay_commands.calls.count(
        ["pytest", "-q", "tests/unit/server/app/test_health.py"]
    ) == 1
    saved = await store.get("d1")
    assert saved is not None
    assert event_id in saved.gates["scheduler"]["handled"]
    assert replay_gates.push_wakeup(saved, event_id, "run_complete", "line-0") is False
    assert replay_gates.pull_wakeups(saved) == []
    summary = dispatch_summary(saved)
    assert summary["spec_ref"] == "spec-13"
    assert summary["gates"]["scheduler"]["healthy"] is True
    assert summary["gates"]["scheduler"]["heartbeat_at"]
    stale = Dispatch.from_dict(json.loads(json.dumps(saved.to_dict())))
    stale.gates["scheduler"]["heartbeat_at"] = "2020-01-01T00:00:00+00:00"
    stale.gates["scheduler"]["threshold_seconds"] = 30
    unhealthy = dispatch_summary(stale)
    assert unhealthy["gates"]["scheduler"]["healthy"] is False
    del collaborators


async def test_a_watchdog_timeout_parks_and_leaves_an_audit_event(tmp_path) -> None:
    settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=tmp_path / "repo",
        g5_command="true",
        watchdog_seconds=0,
    )

    async def _fail_if_waiting(_seconds: float) -> None:
        raise AssertionError("watchdog returned to the poll loop")

    collaborators, machine, _commands, _gates = _bundle(
        tmp_path,
        [_line("line-0")],
        settings=settings,
    )
    collaborators.monitor = SilentMonitor()
    machine._sleep = _fail_if_waiting
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    parked = await machine.run(dispatch)
    assert parked.state is DispatchState.REVIEW
    line = parked.line("line-0")
    assert line is not None
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "watchdog_timeout"
    audit = parked.gates["scheduler"]["audit"]
    assert audit == [
        {
            "kind": "watchdog_timeout",
            "line_id": "line-0",
            "state": "REVIEW",
            "at": WHEN.isoformat(),
        }
    ]
    assert "Line line-0 blocked: watchdog_timeout" in (parked.message or "")
