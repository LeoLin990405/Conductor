"""The production wakeup: a new service instance advances a saved dispatch."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.server.services.dispatch import DispatchService, DispatchSettings
from src.server.services.dispatch_wakeup import DispatchWakeup
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


@pytest.fixture(autouse=True)
def _n73_human_g6(monkeypatch):
    """These tests pin the human G6; N73 S7 auto-approval is tested on its own."""
    monkeypatch.setenv("CONDUCTOR_DISPATCH_AUTO_APPROVE", "0")

pytestmark = pytest.mark.asyncio


class SilentMonitor:
    def __init__(self) -> None:
        self.polls = 0

    async def poll(self, run_id: str) -> RunStatus:
        del run_id
        self.polls += 1
        return RunStatus(output="still running", complete=False)

    async def cancel(self, run_id: str) -> None:
        del run_id


async def test_a_new_service_sweeps_without_the_test_calling_run(tmp_path: Path) -> None:
    store = InMemoryDispatchStore()
    gate_settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=tmp_path / "repo",
        g5_command="pytest -q",
    )
    line = _line("line-0", assigned_model="kimi")
    line.status = LineStatus.RUNNING
    line.current_run = "run-1"
    dispatch = Dispatch(
        id="d1",
        goal="ship it",
        user_id="u",
        created_at="now",
        spec_ref="spec-13",
        state=DispatchState.MONITORING,
        lines=[line],
        integrate_branch="integrate/auto-dispatch-d1",
    )
    seeded = GateRunner(
        gate_settings,
        commands=ScriptedCommands(),
        worktrees=FakeWorktrees(),
        clock=lambda: WHEN,
    )
    seeded.prepare(dispatch)
    assert seeded.push_wakeup(
        dispatch,
        "run-line-0-1",
        "run_complete",
        "line-0",
        run_id="run-1",
        attempt=line.attempts,
    ) is True
    await store.create(dispatch)
    polls = SilentMonitor()

    def factory(_dispatch: Dispatch):
        collaborators, machine, _commands, _gates = _bundle(
            tmp_path,
            [],
            settings=gate_settings,
            store=store,
        )
        collaborators.monitor = polls

        async def _fail_if_waiting(_seconds: float) -> None:
            raise AssertionError("the sweep waited on a monitor")

        machine._sleep = _fail_if_waiting
        return machine

    service = DispatchService(
        DispatchSettings(
            proposed_dir=tmp_path / "proposed",
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            poll_interval_seconds=0.05,
        ),
        store=store,
        gate=FileApprovalGate(tmp_path / "proposed"),
        machine_factory=factory,
    )
    await service.open()
    try:
        saved = None
        for _ in range(50):
            saved = await store.get("d1")
            if saved is not None and saved.state is DispatchState.APPROVAL:
                break
            await asyncio.sleep(0.05)
        assert saved is not None
        assert saved.state is DispatchState.APPROVAL
        assert saved.line("line-0") is not None
        assert saved.line("line-0").status is LineStatus.PASSED
        assert polls.polls == 0
        assert "run-line-0-1" in saved.gates["scheduler"]["handled"]
    finally:
        await service.close()


async def test_the_consumer_loop_stays_up_after_a_recover_error() -> None:
    store = InMemoryDispatchStore()
    dispatch = Dispatch(
        id="d1",
        goal="ship it",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        gates={
            "scheduler": {
                "pending": [{"id": "e1", "kind": "run_complete", "line_id": "line-0"}],
                "handled": [],
            }
        },
    )
    await store.create(dispatch)
    calls = {"n": 0}

    async def recover(dispatch_id: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        row = await store.get(dispatch_id)
        assert row is not None
        row.gates["scheduler"]["pending"] = []
        await store.save(row)

    wakeup = DispatchWakeup(store=store, recover=recover, poll_interval_seconds=0.01)
    await wakeup.start()
    try:
        for _ in range(50):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0.02)
        assert calls["n"] >= 2
    finally:
        await wakeup.stop()
