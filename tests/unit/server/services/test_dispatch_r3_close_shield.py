"""R3: a second cancel does not abort poll cleanup that has already started."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from src.server.services.dispatch import DispatchService, DispatchSettings
from src.server.services.dispatch_wakeup import MemoryWakeupLease
from src.tools.auto_dispatch import (
    Dispatch,
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    LineStatus,
)
from src.tools.auto_dispatch.gates import GateRunner, GateSettings
from src.tools.auto_dispatch.types import RunStatus
from tests.unit.tools.auto_dispatch.test_gate_recovery import (
    WHEN,
    FakeWorktrees,
    ScriptedCommands,
    _bundle,
    _line,
)

pytestmark = pytest.mark.asyncio


class CleaningMonitor:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.finished = asyncio.Event()

    async def poll(self, run_id: str) -> RunStatus:
        del run_id
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleaning.set()
            await asyncio.sleep(0.05)
            self.finished.set()
        return RunStatus(output="done", complete=True)

    async def cancel(self, run_id: str) -> None:
        del run_id


async def test_r3_close_waits_for_poll_cleanup(tmp_path) -> None:
    store = InMemoryDispatchStore()
    monitor = CleaningMonitor()
    line = _line("line-0", assigned_model="kimi")
    line.status = LineStatus.RUNNING
    line.current_run = "run-1"
    dispatch = Dispatch(
        id="d-close-shield",
        goal="x",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        lines=[line],
        integrate_branch="integrate/x",
    )
    now = [WHEN]
    gate_runner = GateRunner(
        GateSettings(
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            g5_command="pytest -q",
            watchdog_seconds=0.04,
        ),
        commands=ScriptedCommands(),
        worktrees=FakeWorktrees(),
        clock=lambda: now[0],
    )
    gate_runner.prepare(dispatch)
    await store.create(dispatch)

    def factory(_dispatch: Dispatch):
        _collaborators, machine, _commands, _gates = _bundle(
            tmp_path,
            [],
            settings=gate_runner._settings,
            store=store,
            gates=gate_runner,
        )
        machine._c.monitor = monitor

        async def wait_deadline(_ignored: Dispatch) -> None:
            await monitor.entered.wait()
            now[0] = WHEN + timedelta(seconds=1)

        machine._wait_deadline = wait_deadline
        return machine

    service = DispatchService(
        DispatchSettings(
            proposed_dir=tmp_path / "proposed",
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            poll_interval_seconds=0.01,
        ),
        store=store,
        gate=FileApprovalGate(tmp_path / "proposed"),
        machine_factory=factory,
        wakeup_lease=MemoryWakeupLease(),
    )
    await service.open()
    await asyncio.wait_for(monitor.entered.wait(), timeout=2)
    await asyncio.wait_for(monitor.cleaning.wait(), timeout=2)
    await service.close()
    assert monitor.finished.is_set()
    stragglers = [
        task.get_name()
        for task in asyncio.all_tasks()
        if task.get_name() == "dispatch-poll" and not task.done()
    ]
    assert stragglers == []
