"""R2: a refused reassign is not saved, and a stale result cannot pass the new attempt."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from src.server.services.dispatch import DispatchService, DispatchSettings, merge_drive
from src.tools.auto_dispatch import (
    Dispatch,
    DispatchState,
    InMemoryDispatchStore,
    Line,
    LineStatus,
)
from src.tools.auto_dispatch.gates import GateRunner, GateSettings
from src.tools.auto_dispatch.types import Outcome, RunStatus
from tests.unit.tools.auto_dispatch.test_gate_recovery import (
    WHEN,
    FakeWorktrees,
    ScriptedCommands,
    _bundle,
    _line,
)

pytestmark = pytest.mark.asyncio


class Copies(InMemoryDispatchStore):
    async def create(self, dispatch: Dispatch) -> Dispatch:
        self.rows[dispatch.id] = copy.deepcopy(dispatch)
        return copy.deepcopy(dispatch)

    async def save(self, dispatch: Dispatch) -> Dispatch:
        self.rows[dispatch.id] = copy.deepcopy(dispatch)
        return copy.deepcopy(dispatch)

    async def get(self, dispatch_id: str) -> Dispatch | None:
        row = self.rows.get(dispatch_id)
        return None if row is None else copy.deepcopy(row)


def _service(tmp_path, store, factory) -> DispatchService:
    return DispatchService(
        DispatchSettings(
            proposed_dir=tmp_path / "proposed",
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path,
        ),
        store=store,
        machine_factory=factory,
    )


async def test_r2_an_old_poll_cannot_pass_the_reassigned_attempt(tmp_path) -> None:
    store = Copies()
    line = Line(
        id="a",
        name="a",
        task="old run",
        assigned_model="codex",
        status=LineStatus.RUNNING,
        current_run="run-old",
    )
    await store.create(
        Dispatch(
            id="same-line",
            goal="test",
            user_id="u",
            created_at="now",
            state=DispatchState.MONITORING,
            lines=[line],
        )
    )
    entered, release = asyncio.Event(), asyncio.Event()

    class Machine:
        async def run(self, dispatch: Dispatch) -> Dispatch:
            entered.set()
            await release.wait()
            current = dispatch.line("a")
            assert current is not None
            current.status = LineStatus.PASSED
            current.current_run = None
            dispatch.state = DispatchState.APPROVAL
            dispatch.outcomes["a"] = Outcome(
                line_id="a",
                model="codex",
                duration_secs=0,
                tokens=0,
                passed=True,
                reviewer_verdict="merge",
            )
            return dispatch

    service = _service(tmp_path, store, lambda _dispatch: Machine())

    async def no_schedule(dispatch: Dispatch) -> Dispatch:
        return dispatch

    service._resume = no_schedule
    drive = asyncio.create_task(service._drive("same-line"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    with pytest.raises(ValueError, match="^drive_active$"):
        await service.reassign("same-line", "a", "stepfun")
    from src.tools.auto_dispatch.gates import bump_epoch

    def mutation(row: Dispatch) -> Dispatch:
        line = row.line("a")
        assert line is not None
        line.assigned_model = "stepfun"
        line.attempts += 1
        line.status = LineStatus.ASSIGNED
        line.current_run = None
        bump_epoch(row)
        return row

    await service.apply_mutation("same-line", mutation)
    release.set()
    await asyncio.wait_for(drive, timeout=2)
    after = await store.get("same-line")
    assert after is not None
    landed = after.line("a")
    assert landed is not None
    assert landed.assigned_model == "stepfun"
    assert landed.attempts == 1
    assert landed.status is not LineStatus.PASSED
    assert after.state is not DispatchState.APPROVAL
    assert after.outcomes.get("a") is None
    gates = after.gates if isinstance(after.gates, dict) else {}
    scheduler = gates.get("scheduler") if isinstance(gates, dict) else None
    audit = scheduler.get("audit") if isinstance(scheduler, dict) else None
    assert isinstance(audit, list)
    assert any(isinstance(item, dict) and item.get("kind") == "stale_drive" for item in audit)


async def test_r2_a_later_gate_snapshot_stays_off_the_reassigned_line(tmp_path) -> None:
    """The drive's next save may touch only gates. That snapshot is still stale."""
    store = Copies()
    line = Line(
        id="a",
        name="a",
        task="old run",
        assigned_model="codex",
        status=LineStatus.RUNNING,
        current_run="run-old",
    )
    await store.create(
        Dispatch(
            id="gates-only",
            goal="test",
            user_id="u",
            created_at="now",
            state=DispatchState.MONITORING,
            lines=[line],
            gates={"lines": {"a": {"phase": "implementing"}}},
        )
    )
    entered, release = asyncio.Event(), asyncio.Event()

    class Collab:
        def __init__(self) -> None:
            self.store: Any = None

    class Machine:
        def __init__(self) -> None:
            self._c = Collab()

        async def run(self, dispatch: Dispatch) -> Dispatch:
            entered.set()
            await release.wait()
            current = dispatch.line("a")
            assert current is not None
            current.log_tail = "old-run-tail"
            await self._c.store.save(dispatch)
            gates = dict(dispatch.gates) if isinstance(dispatch.gates, dict) else {}
            snapshots = dict(gates.get("lines") or {})
            snapshots["a"] = {
                "phase": "primary_review",
                "G2": {"result": "pass"},
                "facts": ["focused_tests_passed"],
            }
            gates["lines"] = snapshots
            dispatch.gates = gates
            await self._c.store.save(dispatch)
            return dispatch

    service = _service(tmp_path, store, lambda _dispatch: Machine())

    async def no_schedule(dispatch: Dispatch) -> Dispatch:
        return dispatch

    service._resume = no_schedule
    drive = asyncio.create_task(service._drive("gates-only"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    with pytest.raises(ValueError, match="^drive_active$"):
        await service.reassign("gates-only", "a", "stepfun")
    from src.tools.auto_dispatch.gates import bump_epoch

    def mutation(row: Dispatch) -> Dispatch:
        line = row.line("a")
        assert line is not None
        line.assigned_model = "stepfun"
        line.attempts += 1
        line.status = LineStatus.ASSIGNED
        line.current_run = None
        bump_epoch(row)
        return row

    await service.apply_mutation("gates-only", mutation)
    release.set()
    await asyncio.wait_for(drive, timeout=2)
    after = await store.get("gates-only")
    assert after is not None
    landed = after.line("a")
    assert landed is not None
    assert landed.assigned_model == "stepfun"
    assert landed.attempts == 1
    assert landed.status is not LineStatus.PASSED
    assert after.state is not DispatchState.APPROVAL
    assert after.outcomes.get("a") is None
    gates = after.gates if isinstance(after.gates, dict) else {}
    snapshots = gates.get("lines") if isinstance(gates, dict) else None
    snap = snapshots.get("a") if isinstance(snapshots, dict) else None
    assert isinstance(snap, dict)
    assert snap.get("phase") != "primary_review"
    g2 = snap.get("G2")
    assert not (isinstance(g2, dict) and g2.get("result") == "pass")
    scheduler = gates.get("scheduler") if isinstance(gates, dict) else None
    audit = scheduler.get("audit") if isinstance(scheduler, dict) else None
    assert isinstance(audit, list)
    assert any(isinstance(item, dict) and item.get("kind") == "stale_drive" for item in audit)


async def test_r2_a_pure_heartbeat_still_merges_onto_a_reassigned_line() -> None:
    before = Dispatch(
        id="beat",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        lines=[
            Line(
                id="b",
                name="b",
                task="x",
                assigned_model="kimi",
                status=LineStatus.RUNNING,
            )
        ],
        gates={"scheduler": {"seq": 1}, "lines": {"b": {"phase": "implementing"}}},
    )
    after = copy.deepcopy(before)
    assert isinstance(after.gates, dict)
    after.gates["scheduler"]["seq"] = 2
    after.gates["scheduler"]["heartbeat_at"] = "t"
    fresh = copy.deepcopy(before)
    moved = fresh.line("b")
    assert moved is not None
    moved.assigned_model = "stepfun"
    moved.attempts = 1
    merged = merge_drive(fresh, before, after)
    landed = merged.line("b")
    assert landed is not None
    assert landed.assigned_model == "stepfun"
    assert landed.attempts == 1
    assert isinstance(merged.gates, dict)
    assert merged.gates["scheduler"]["seq"] == 2
    assert merged.gates["scheduler"]["heartbeat_at"] == "t"
    assert merged.gates["lines"]["b"]["phase"] == "implementing"
    audit = merged.gates["scheduler"].get("audit")
    assert not audit


async def test_r2_stage_gate_refusal_leaves_the_row_unchanged(tmp_path) -> None:
    store = InMemoryDispatchStore()
    line = _line("a", assigned_model="codex", status=LineStatus.RUNNING, current_run="run-old")
    dispatch = Dispatch(
        id="refused",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        lines=[line],
    )
    gates = GateRunner(
        GateSettings(
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            g5_command="pytest -q",
        ),
        commands=ScriptedCommands(),
        worktrees=FakeWorktrees(),
        clock=lambda: WHEN,
    )
    gates.prepare(dispatch)
    snap = gates.line_snapshot(dispatch, line)
    machine = snap.get("machine")
    assert isinstance(machine, dict)
    machine["stage"] = "implementing"
    gates.handoff_facts = lambda *_args, **_kwargs: {
        "branch": "feature/a",
        "commit": "abc123",
        "files": ("a.py",),
        "test_command": "pytest",
        "results": "pass",
        "known_limitations": "none",
    }
    await store.create(dispatch)
    before_attempts = line.attempts
    service = _service(tmp_path, store, lambda _dispatch: None)
    service._gate_runner = lambda: gates

    async def no_schedule(row: Dispatch) -> Dispatch:
        return row

    service._resume = no_schedule
    with pytest.raises(ValueError, match="^reassignment_not_at_checkpoint$"):
        await service.reassign("refused", "a", "stepfun")
    after = await store.get("refused")
    assert after is not None
    landed = after.line("a")
    assert landed is not None
    assert landed.assigned_model == "codex"
    assert landed.attempts == before_attempts
    assert landed.status is LineStatus.RUNNING
    assert landed.current_run == "run-old"
    assert after.state is DispatchState.MONITORING


async def test_r2_alias_poll_discards_a_reassigned_line(tmp_path) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Monitor:
        async def poll(self, run_id: str) -> RunStatus:
            del run_id
            entered.set()
            await release.wait()
            return RunStatus(complete=True, output="old completion")

        async def cancel(self, run_id: str) -> None:
            del run_id

    collaborators, machine, _commands, gates = _bundle(tmp_path, [])
    collaborators.monitor = Monitor()
    line = _line("a", assigned_model="codex", status=LineStatus.RUNNING, current_run="run-old")
    dispatch = Dispatch(
        id="alias",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        lines=[line],
    )
    gates.prepare(dispatch)
    await collaborators.store.create(dispatch)
    task = asyncio.create_task(machine._poll_line(dispatch, line))
    await asyncio.wait_for(entered.wait(), timeout=2)
    line.assigned_model = "stepfun"
    line.attempts = 1
    line.current_run = None
    line.status = LineStatus.ASSIGNED
    release.set()
    await asyncio.wait_for(task, timeout=2)
    assert line.status is LineStatus.ASSIGNED
    assert line.assigned_model == "stepfun"
    assert line.attempts == 1
    assert dispatch.outcomes.get("a") is None
    gates_body = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    scheduler = gates_body.get("scheduler")
    audit = scheduler.get("audit") if isinstance(scheduler, dict) else None
    assert isinstance(audit, list)
    assert any(isinstance(item, dict) and item.get("kind") == "stale_result" for item in audit)
