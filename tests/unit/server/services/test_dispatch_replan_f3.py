"""F3: close joins the poll task, including its finally block."""

from __future__ import annotations

import asyncio

import pytest

from src.tools.auto_dispatch import Dispatch, DispatchState, InMemoryDispatchStore, LineStatus
from src.tools.auto_dispatch.gates import GateRunner, GateSettings
from src.tools.auto_dispatch.types import RunStatus
from tests.unit.server.services.test_dispatch_w3b_fix2 import _factory, _service
from tests.unit.tools.auto_dispatch.test_gate_recovery import (
    WHEN,
    FakeWorktrees,
    ScriptedCommands,
    _line,
)

pytestmark = pytest.mark.asyncio


class SlowCleanupMonitor:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cleanup_finished = asyncio.Event()

    async def poll(self, run_id: str) -> RunStatus:
        del run_id
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            self.cleanup_finished.set()
        return RunStatus(output="done", complete=True)

    async def cancel(self, run_id: str) -> None:
        del run_id


async def test_f3_close_joins_the_poll_finally(tmp_path) -> None:
    store = InMemoryDispatchStore()
    monitor = SlowCleanupMonitor()
    line = _line("line-0", assigned_model="kimi")
    line.status = LineStatus.RUNNING
    line.current_run = "run-1"
    dispatch = Dispatch(
        id="d-close",
        goal="x",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        lines=[line],
        integrate_branch="integrate/x",
    )
    GateRunner(
        GateSettings(
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            g5_command="pytest -q",
        ),
        commands=ScriptedCommands(),
        worktrees=FakeWorktrees(),
        clock=lambda: WHEN,
    ).prepare(dispatch)
    await store.create(dispatch)
    service = _service(tmp_path, store, _factory(tmp_path, store, monitor=monitor))
    await service.open()
    await asyncio.wait_for(monitor.entered.wait(), timeout=2)
    await service.close()
    assert monitor.cleanup_finished.is_set()
    stragglers = [
        task.get_name()
        for task in asyncio.all_tasks()
        if task.get_name() == "dispatch-poll" and not task.done()
    ]
    assert stragglers == []
