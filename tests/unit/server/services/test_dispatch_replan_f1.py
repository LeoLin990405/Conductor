"""F1: a long G5 recovery is not cancelled by an outer timeout."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from src.server.services.autonomy.stage_gate import Stage
from src.server.services.dispatch import DispatchService, DispatchSettings
from src.server.services.dispatch_wakeup import DispatchWakeup, MemoryWakeupLease
from src.tools.auto_dispatch import (
    Dispatch,
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    LineStatus,
)
from src.tools.auto_dispatch.gates import CommandResult, GateSettings
from src.tools.auto_dispatch.machine import _load_state, _store_state
from tests.unit.tools.auto_dispatch.test_gate_recovery import FakeWorktrees, _bundle, _line

pytestmark = pytest.mark.asyncio


class SlowCommands:
    def __init__(self) -> None:
        self.started = 0
        self.completed = 0
        self.cancelled = 0
        self.release = asyncio.Event()

    async def run(self, argv, *, cwd, timeout, log_path):
        del argv, cwd, timeout
        self.started += 1
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_bytes(b"")
        try:
            await self.release.wait()
            self.completed += 1
            return CommandResult(exit_code=0, duration_ms=60)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


async def test_f1_a_long_g5_reaches_approval_without_an_outer_timeout(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = InMemoryDispatchStore()
    commands = SlowCommands()
    line = _line("line-0", assigned_model="kimi")
    line.status = LineStatus.PASSED
    settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=repo,
        g5_command="fake-g5",
        g5_timeout_seconds=3600,
    )
    _collaborators, machine, _commands, gates = _bundle(
        tmp_path,
        [line],
        settings=settings,
        commands=commands,
        trees=FakeWorktrees(),
        store=store,
    )
    row = Dispatch(
        id="d-slow-g5",
        goal="x",
        user_id="u",
        created_at="now",
        state=DispatchState.REVIEW,
        lines=[line],
        integrate_branch="integrate/auto-dispatch-d1",
    )
    gates.prepare(row)
    snap = gates.line_snapshot(row, line)
    _store_state(
        snap,
        replace(_load_state(snap), stage=Stage.INTEGRATION_CHECKS, primary_passed=True),
    )
    await store.create(row)
    service = DispatchService(
        DispatchSettings(
            proposed_dir=tmp_path / "p",
            runs_dir=tmp_path / "runs",
            repo_root=repo,
        ),
        store=store,
        gate=FileApprovalGate(tmp_path / "p"),
        machine_factory=lambda _dispatch: machine,
    )
    wake = DispatchWakeup(
        store=store,
        recover=service._drive,
        poll_interval_seconds=0,
        lease=MemoryWakeupLease(),
        recover_timeout_seconds=0.01,
    )
    sweep = asyncio.create_task(wake.sweep())
    await asyncio.sleep(0.05)
    assert commands.started == 1
    assert commands.cancelled == 0
    assert not sweep.done()
    commands.release.set()
    await sweep
    current = await store.get(row.id)
    assert current is not None
    assert commands.completed == 1
    assert commands.cancelled == 0
    assert current.state is DispatchState.APPROVAL
