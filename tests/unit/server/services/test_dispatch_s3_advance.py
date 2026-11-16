"""S3: every gated reassign goes through Stage-Gate advance."""

from __future__ import annotations

import pytest

from src.server.app.background_starts import _start_tasks
from src.server.services.dispatch import DispatchService, DispatchSettings
from src.tools.auto_dispatch import Dispatch, DispatchState, InMemoryDispatchStore, LineStatus
from src.tools.auto_dispatch.gates import GateRunner, GateSettings, drive_epoch
from tests.unit.tools.auto_dispatch.test_gate_recovery import (
    WHEN,
    FakeWorktrees,
    ScriptedCommands,
    _line,
)

pytestmark = pytest.mark.asyncio


def _gates(tmp_path) -> GateRunner:
    return GateRunner(
        GateSettings(
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            g5_command="pytest -q",
        ),
        commands=ScriptedCommands(),
        worktrees=FakeWorktrees(),
        clock=lambda: WHEN,
    )


def _complete() -> dict[str, object]:
    return {
        "branch": "feature/a",
        "commit": "abc123",
        "files": ("a.py",),
        "test_command": "pytest",
        "results": "pass",
        "known_limitations": "none",
    }


async def _row(tmp_path, stage: str, facts: dict[str, object]):
    store = InMemoryDispatchStore()
    gates = _gates(tmp_path)
    line = _line("a", assigned_model="kimi", status=LineStatus.RUNNING, current_run="run-old")
    dispatch = Dispatch(
        id="gated",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        lines=[line],
    )
    gates.prepare(dispatch)
    snap = gates.line_snapshot(dispatch, line)
    machine = snap.get("machine")
    assert isinstance(machine, dict)
    machine["stage"] = stage
    gates.handoff_facts = lambda *_args, **_kwargs: facts
    await store.create(dispatch)
    service = DispatchService(
        DispatchSettings(
            proposed_dir=tmp_path / "proposed",
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path,
        ),
        store=store,
        machine_factory=lambda _dispatch: None,
    )
    service._gate_runner = lambda: gates

    async def no_schedule(row: Dispatch) -> Dispatch:
        return row

    service._resume = no_schedule
    return store, service, gates


async def test_s3_implementing_refuses_before_any_write(tmp_path) -> None:
    store, service, _gates_runner = await _row(tmp_path, "implementing", _complete())
    before = set(_start_tasks)
    with pytest.raises(ValueError, match="^reassignment_not_at_checkpoint$"):
        await service.reassign("gated", "a", "stepfun")
    after = await store.get("gated")
    assert after is not None
    landed = after.line("a")
    assert landed is not None
    assert landed.assigned_model == "kimi"
    assert landed.attempts == 0
    assert landed.current_run == "run-old"
    assert drive_epoch(after) == 0
    assert set(_start_tasks) == before


async def test_s3_a_legal_checkpoint_with_a_partial_handoff_refuses(tmp_path) -> None:
    store, service, _gates_runner = await _row(tmp_path, "fixing", {"branch": "feature/a"})
    before = set(_start_tasks)
    with pytest.raises(ValueError, match="^handoff_incomplete$"):
        await service.reassign("gated", "a", "stepfun")
    after = await store.get("gated")
    assert after is not None
    landed = after.line("a")
    assert landed is not None
    assert landed.assigned_model == "kimi"
    assert landed.attempts == 0
    assert landed.status is LineStatus.RUNNING
    assert drive_epoch(after) == 0
    assert set(_start_tasks) == before


async def test_s3_an_accepted_checkpoint_commits_the_transition_and_epoch(tmp_path) -> None:
    store, service, gates = await _row(tmp_path, "assigned", _complete())
    saved = await service.reassign("gated", "a", "stepfun")
    landed = saved.line("a")
    assert landed is not None
    assert landed.assigned_model == "stepfun"
    assert landed.attempts == 1
    assert landed.status is LineStatus.ASSIGNED
    assert drive_epoch(saved) == 1
    snap = gates.line_snapshot(saved, landed)
    machine = snap.get("machine")
    assert isinstance(machine, dict)
    assert machine.get("stage") == "assigned"
    stored = await store.get("gated")
    assert stored is not None
    assert drive_epoch(stored) == 1
    assert stored.line("a") is not None
    assert stored.line("a").assigned_model == "stepfun"
