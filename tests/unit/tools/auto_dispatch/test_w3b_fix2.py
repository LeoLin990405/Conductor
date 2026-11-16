"""W3b fix round 2: one regression for each blocker B1 and B5–B7.

These tests call the shipped machine. The wakeup blockers live next to the
service, in ``test_w3b_fix2_wakeup.py``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch.dev import InMemoryDispatchStore
from src.tools.auto_dispatch.gates import RESUME_SPEC_MISSING, GateSettings
from src.tools.auto_dispatch.machine import MachineConfig
from src.tools.auto_dispatch.protocols import LaunchError, ModelRole
from src.tools.auto_dispatch.types import Line, RunStatus, Verdict
from tests.unit.tools.auto_dispatch.test_gate_recovery import (
    WHEN,
    _bundle,
    _line,
)

pytestmark = pytest.mark.asyncio


class RecordingMonitor:
    def __init__(self) -> None:
        self.polls: list[str] = []

    async def poll(self, run_id: str) -> RunStatus:
        self.polls.append(run_id)
        return RunStatus(output="still running", complete=False)

    async def cancel(self, run_id: str) -> None:
        del run_id


class StuckMonitor:
    async def poll(self, run_id: str) -> RunStatus:
        del run_id
        await asyncio.Event().wait()
        return RunStatus(output="done", complete=True)

    async def cancel(self, run_id: str) -> None:
        del run_id


class HeartbeatStore(InMemoryDispatchStore):
    def __init__(self) -> None:
        super().__init__()
        self.heartbeats: list[str] = []

    async def save(self, dispatch: Dispatch) -> Dispatch:
        saved = await super().save(dispatch)
        sched = dispatch.gates.get("scheduler") if isinstance(dispatch.gates, dict) else None
        stamp = sched.get("heartbeat_at") if isinstance(sched, dict) else None
        if isinstance(stamp, str):
            self.heartbeats.append(stamp)
        return saved


class SpecPlanner:
    def __init__(self, lines: list[Line]) -> None:
        self.seen: list[str] = []
        self._lines = lines

    async def plan(self, goal: str) -> list[Line]:
        self.seen.append(goal)
        return list(self._lines)


class FallbackSelector:
    async def select(self, line: Line, used_models: list[str], *, role: ModelRole = ModelRole.WORK):
        del line
        if role is ModelRole.REVIEW:
            if "kimi" not in used_models:
                return "kimi"
            return "glm"
        for name in ("kimi", "stepfun", "glm"):
            if name not in used_models:
                return name
        return None


class KimiLaunchFails:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def launch(self, line: Line, model: str, skills: list[str]) -> str:
        del line, skills
        self.calls.append(model)
        if model == "kimi":
            raise LaunchError("kimi unavailable")
        return f"run-{model}"


class AcceptingReviewer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.calls.append((line_id, model))
        return Verdict.ok("ok")


def _prepared(
    tmp_path,
    line: Line,
    *,
    state: DispatchState,
    spec_ref: str = "",
    settings: GateSettings | None = None,
):
    settings = settings or GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=tmp_path / "repo",
        g5_command="pytest -q",
    )
    collaborators, machine, _commands, gates = _bundle(
        tmp_path, [line], settings=settings
    )
    dispatch = Dispatch(
        id="d1",
        goal="ship it",
        user_id="u",
        created_at="now",
        state=state,
        spec_ref=spec_ref,
        lines=[line],
        integrate_branch="integrate/auto-dispatch-d1",
    )
    gates.prepare(dispatch)
    return collaborators, machine, gates, dispatch


async def test_b1_a_stale_completion_does_not_pass_the_new_run(tmp_path) -> None:
    line = _line("line-0", assigned_model="kimi")
    line.status = LineStatus.RUNNING
    line.current_run = "run-2"
    line.attempts = 1
    _collaborators, machine, gates, dispatch = _prepared(
        tmp_path, line, state=DispatchState.MONITORING
    )
    monitor = RecordingMonitor()
    _collaborators.monitor = monitor
    assert gates.push_wakeup(
        dispatch,
        "run-old",
        "run_complete",
        line.id,
        run_id="run-1",
        attempt=0,
    )
    await _collaborators.store.create(dispatch)

    async def stop(_seconds: float) -> None:
        raise AssertionError("stop")

    machine._sleep = stop
    with pytest.raises(AssertionError, match="stop"):
        await machine.run(dispatch)
    saved = await _collaborators.store.get("d1")
    assert saved is not None
    kept = saved.line("line-0")
    assert kept is not None
    assert kept.status is LineStatus.RUNNING
    assert kept.current_run == "run-2"
    assert saved.state is not DispatchState.APPROVAL
    assert monitor.polls == ["run-2"]
    audit = saved.gates["scheduler"]["audit"]
    assert {"kind": "stale_event"} in audit
    assert "run-old" in saved.gates["scheduler"]["handled"]


async def test_b5_restarts_do_not_become_error(tmp_path) -> None:
    missing = _line("line-0", assigned_model="kimi")
    missing.status = LineStatus.ASSIGNED
    collaborators, machine, _gates, dispatch = _prepared(
        tmp_path, missing, state=DispatchState.PLANNING, spec_ref=""
    )
    await collaborators.store.create(dispatch)
    parked = await machine.run(dispatch)
    assert parked.state is DispatchState.REVIEW
    assert parked.state is not DispatchState.ERROR
    assert parked.line("line-0") is not None
    assert parked.line("line-0").failure_reason == RESUME_SPEC_MISSING

    bare = Dispatch(
        id="d-bare",
        goal="ship it",
        user_id="u",
        created_at="now",
        state=DispatchState.PLANNING,
        spec_ref="",
    )
    await collaborators.store.create(bare)
    empty = await machine.run(bare)
    assert empty.state is DispatchState.REVIEW
    assert empty.message == RESUME_SPEC_MISSING
    assert empty.state is not DispatchState.ERROR

    planned = _line("from-spec")
    planner = SpecPlanner([planned])
    resumed_line = _line("old")
    collaborators, machine, _gates, dispatch = _prepared(
        tmp_path,
        resumed_line,
        state=DispatchState.PLANNING,
        spec_ref="spec-archived",
    )
    collaborators.planner = planner
    dispatch.lines = []
    await collaborators.store.create(dispatch)
    continued = await machine.run(dispatch)
    assert continued.state is not DispatchState.ERROR
    assert planner.seen == ["spec-archived"]

    assigned = _line("line-0", assigned_model="kimi")
    assigned.status = LineStatus.ASSIGNED
    collaborators, machine, _gates, dispatch = _prepared(
        tmp_path, assigned, state=DispatchState.ASSIGNED
    )
    await collaborators.store.create(dispatch)
    launched = await machine.run(dispatch)
    assert launched.state is not DispatchState.ERROR
    assert collaborators.launcher.launches

    launching = _line("line-0", assigned_model="kimi")
    launching.status = LineStatus.ASSIGNED
    collaborators, machine, _gates, dispatch = _prepared(
        tmp_path, launching, state=DispatchState.LAUNCHING
    )
    await collaborators.store.create(dispatch)
    relaunched = await machine.run(dispatch)
    assert relaunched.state is not DispatchState.ERROR
    assert collaborators.launcher.launches

    running = _line("line-0", assigned_model="kimi")
    running.status = LineStatus.RUNNING
    running.current_run = "run-line-0-1"
    collaborators, machine, _gates, dispatch = _prepared(
        tmp_path, running, state=DispatchState.MONITORING
    )
    await collaborators.store.create(dispatch)
    monitored = await machine.run(dispatch)
    assert monitored.state is not DispatchState.ERROR


async def test_b6_a_stuck_poll_parks_and_persists_heartbeats(tmp_path) -> None:
    now = [WHEN]
    store = HeartbeatStore()
    settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=tmp_path / "repo",
        g5_command="pytest -q",
        watchdog_seconds=5,
        heartbeat_interval_seconds=1,
    )
    collaborators, machine, gates, dispatch = _prepared(
        tmp_path,
        _line("line-0"),
        state=DispatchState.SUBMITTED,
        settings=settings,
    )
    collaborators.store = store
    collaborators.monitor = StuckMonitor()
    gates._clock = lambda: now[0]
    machine._config = MachineConfig(poll_interval_seconds=10)

    async def tick(seconds: float) -> None:
        now[0] += timedelta(seconds=seconds)

    machine._sleep = tick
    await store.create(dispatch)
    parked = await machine.run(dispatch)
    line = parked.line("line-0")
    assert line is not None
    assert line.status is not LineStatus.RUNNING
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "watchdog_timeout"
    assert len(set(store.heartbeats)) >= 2


async def test_b7_fallback_author_can_be_reviewed_by_the_original_model(tmp_path) -> None:
    selector = FallbackSelector()
    launcher = KimiLaunchFails()
    reviewer = AcceptingReviewer()
    collaborators, machine, _gates, dispatch = _prepared(
        tmp_path, _line("a"), state=DispatchState.SUBMITTED
    )
    collaborators.selector = selector
    collaborators.launcher = launcher
    collaborators.reviewer = reviewer
    machine._config = MachineConfig(launch_retries=0)
    await collaborators.store.create(dispatch)
    result = await machine.run(dispatch)
    snap = result.gates["lines"]["a"]["machine"]
    assert result.state is not DispatchState.ERROR
    assert snap["author_provider"] == "stepfun"
    assert snap["developer_provider"] == "stepfun"
    assert snap["contributors"] == ["stepfun"]
    assert launcher.calls[:2] == ["kimi", "stepfun"]
    assert ("a", "kimi") in reviewer.calls
    reasons = [event.get("reason") for event in result.gates["lines"]["a"]["events"]]
    assert "self_review_forbidden" not in reasons
