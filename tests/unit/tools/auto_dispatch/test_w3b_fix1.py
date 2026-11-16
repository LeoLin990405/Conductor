"""W3b fix round 1: the eight Codex blockers, asserted as the corrected behavior."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest

from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch.gates import FIX_LAUNCH_FAILED, GateRunner, GateSettings
from src.tools.auto_dispatch.machine import MachineConfig
from src.tools.auto_dispatch.protocols import LaunchError, ModelRole
from src.tools.auto_dispatch.types import RunStatus, Verdict
from tests.unit.tools.auto_dispatch.test_g2_durable_pin import SerializedStore
from tests.unit.tools.auto_dispatch.test_gate_recovery import (
    WHEN,
    FakeWorktrees,
    ScriptedCommands,
    _bundle,
    _line,
)
from tests.unit.tools.auto_dispatch.test_machine_gates import (
    _bundle as _machine_bundle,
)
from tests.unit.tools.auto_dispatch.test_machine_gates import (
    _line as _machine_line,
)
from tests.unit.tools.auto_dispatch.test_machine_gates import (
    _repo_with_lines,
)

pytestmark = pytest.mark.asyncio


class FixedSelector:
    _pool = ("kimi", "stepfun", "glm")

    async def select(self, line, used_models, *, role=ModelRole.WORK):
        del line, role
        for name in ("stepfun", "glm", "kimi"):
            if name not in used_models:
                return name
        return None


class OnceBlock:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.calls.append((line_id, model))
        if len(self.calls) == 1:
            return Verdict.blocked(["needs fix"])
        return Verdict.ok("ok")


class FixFailLauncher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def launch(self, line, model, skills):
        del skills
        self.calls.append(
            {
                "id": line.id,
                "model": model,
                "is_fix": "Handoff:" in line.task,
                "task": line.task,
            }
        )
        if 2 <= len(self.calls) <= 4:
            raise LaunchError("injected unavailable original worker")
        return f"run-{len(self.calls)}"


class BlockLine:
    def __init__(self, line_id: str) -> None:
        self._line_id = line_id
        self.calls: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.calls.append((line_id, model))
        if line_id == self._line_id:
            return Verdict.blocked(["needs fix"])
        return Verdict.ok("ok")


class SplitPlanner:
    def __init__(self, lines) -> None:
        self._lines = lines

    async def plan(self, goal: str):
        if "Replan this line." in goal:
            return [_machine_line("x"), _machine_line("y")]
        return self._lines


class CaptureStore(SerializedStore):
    def __init__(self) -> None:
        super().__init__()
        self.saved: list[dict] = []

    async def save(self, dispatch: Dispatch) -> Dispatch:
        stored = await super().save(dispatch)
        self.saved.append(json.loads(json.dumps(dispatch.to_dict())))
        return stored

    async def list(self, *, user_id=None, states=None, limit=50):
        del user_id, states, limit
        return []


class Silent:
    def __init__(self) -> None:
        self.polls = 0

    async def poll(self, run_id: str) -> RunStatus:
        del run_id
        self.polls += 1
        return RunStatus(output="still running", complete=False)

    async def cancel(self, run_id: str) -> None:
        del run_id


class HoldMonitor:
    def __init__(self) -> None:
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def poll(self, run_id: str) -> RunStatus:
        del run_id
        self.blocked.set()
        await self.release.wait()
        return RunStatus(output="done", complete=True)

    async def cancel(self, run_id: str) -> None:
        del run_id
        self.release.set()


def _settings(tmp_path: Path, repo: Path, **extra) -> GateSettings:
    return GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=repo,
        g5_command="pytest -q",
        **extra,
    )


async def test_fix_launch_failures_park_the_original_author(tmp_path: Path) -> None:
    repo = _repo_with_lines(tmp_path / "repo", "a")
    launcher = FixFailLauncher()
    reviewer = OnceBlock()
    collaborators, machine, _commands, _trees = _machine_bundle(
        tmp_path,
        [_machine_line("a", assigned_model="kimi")],
        launcher=launcher,
        reviewer=reviewer,
        selector=FixedSelector(),
        settings=_settings(tmp_path, repo),
    )
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    result = await machine.run(dispatch)
    line = result.line("a")
    assert line is not None
    snap = result.gates["lines"]["a"]
    assert result.state is DispatchState.REVIEW
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == FIX_LAUNCH_FAILED
    assert line.assigned_model == "kimi"
    assert line.attempts == 0
    assert "Handoff:" not in line.task
    assert snap["machine"]["developer_provider"] == "kimi"
    assert snap["machine"]["contributors"] == ["kimi"]
    reviewers = {model for _line_id, model in reviewer.calls}
    assert line.assigned_model not in reviewers
    assert snap["machine"].get("blocking_reviewer") != "kimi"
    assert all(call["model"] == "kimi" for call in launcher.calls)
    assert launcher.calls[-1]["is_fix"] is True


async def test_a_split_retargets_every_downstream_line_in_the_same_save(tmp_path: Path) -> None:
    repo = _repo_with_lines(tmp_path / "repo", "a", "b", "c")
    initial = [
        _machine_line("a", assigned_model="kimi"),
        _machine_line("b", depends_on=("a",), assigned_model="glm"),
        _machine_line("c", depends_on=("b",), assigned_model="stepfun"),
    ]
    store = CaptureStore()
    collaborators, machine, _commands, _trees = _machine_bundle(
        tmp_path,
        initial,
        planner=SplitPlanner(initial),
        reviewer=BlockLine("b"),
        settings=_settings(tmp_path, repo),
    )
    collaborators.store = store
    dispatch = await store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    result = await machine.run(dispatch)
    children = [line.id for line in result.lines if line.id.startswith("b-r1-")]
    assert children == ["b-r1-1", "b-r1-2"]
    downstream = result.line("c")
    assert downstream is not None
    assert downstream.depends_on == ("b-r1-1", "b-r1-2")
    assert result.line("b") is not None
    assert result.line("b").failure_reason == "replanned"
    found = False
    for snap in store.saved:
        ids = [row["id"] for row in snap["lines"]]
        if "b-r1-1" not in ids:
            continue
        found = True
        saved_c = next(row for row in snap["lines"] if row["id"] == "c")
        assert saved_c["depends_on"] == ["b-r1-1", "b-r1-2"]
        break
    assert found
    raw = json.loads(json.dumps(result.to_dict()))
    restarted = Dispatch.from_dict(raw)
    assert restarted.line("c") is not None
    assert restarted.line("c").depends_on == ("b-r1-1", "b-r1-2")
    assert result.state is DispatchState.APPROVAL
    assert result.line("c").status is LineStatus.PASSED
    assert all(result.line(line_id).status is LineStatus.PASSED for line_id in children)


async def test_phase_is_implementing_while_the_worker_is_blocked(tmp_path: Path) -> None:
    store = SerializedStore()
    hold = HoldMonitor()
    collaborators, machine, _commands, _trees = _machine_bundle(
        tmp_path,
        [_machine_line("a", assigned_model="kimi")],
    )
    collaborators.store = store
    collaborators.monitor = hold
    dispatch = await store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    task = asyncio.create_task(machine.run(dispatch))
    await asyncio.wait_for(hold.blocked.wait(), timeout=5)
    saved = await store.get("d1")
    assert saved is not None
    snap = saved.gates["lines"]["a"]
    assert saved.state is DispatchState.MONITORING
    assert saved.line("a").status is LineStatus.RUNNING
    assert snap["phase"] == "implementing"
    assert snap["machine"]["stage"] == "implementing"
    reasons = [event["reason"] for event in snap["events"]]
    assert "implementation_started" in reasons
    assert "work_submitted" not in reasons
    restarted = Dispatch.from_dict(json.loads(json.dumps(saved.to_dict())))
    assert restarted.gates["lines"]["a"]["phase"] == "implementing"
    assert restarted.gates["lines"]["a"]["machine"]["stage"] == "implementing"
    hold.release.set()
    result = await asyncio.wait_for(task, timeout=5)
    done = result.gates["lines"]["a"]
    assert "work_submitted" in [event["reason"] for event in done["events"]]
    assert done["phase"] != "dag_ready"


async def test_a_positive_watchdog_leaves_the_line(tmp_path: Path) -> None:
    now = [WHEN]
    store = SerializedStore()
    settings = _settings(tmp_path, tmp_path / "repo", watchdog_seconds=30)
    collaborators, machine, _commands, gates = _bundle(
        tmp_path,
        [_line("line-0")],
        settings=settings,
        store=store,
    )
    collaborators.monitor = Silent()
    gates._clock = lambda: now[0]
    machine._config = MachineConfig(poll_interval_seconds=10)
    seen: list[str] = []

    async def tick(seconds: float) -> None:
        saved = await store.get("d1")
        assert saved is not None
        deadline = saved.gates["scheduler"]["deadline_at"]
        assert isinstance(deadline, str) and deadline
        seen.append(deadline)
        now[0] += timedelta(seconds=seconds)

    machine._sleep = tick
    dispatch = await store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    parked = await machine.run(dispatch)
    line = parked.line("line-0")
    assert line is not None
    assert line.status is not LineStatus.RUNNING
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "watchdog_timeout"
    assert parked.state is DispatchState.REVIEW
    assert seen
    assert len(set(seen)) == 1
    assert parked.gates["scheduler"]["audit"]


async def test_a_settled_run_survives_cancel_and_is_not_polled_again(tmp_path: Path) -> None:
    store = SerializedStore()
    settings = _settings(tmp_path, tmp_path / "repo")
    collaborators, machine, _commands, gates = _bundle(
        tmp_path,
        [_line("line-0")],
        settings=settings,
        store=store,
    )
    collaborators.monitor = Silent()

    async def stop(_seconds: float) -> None:
        raise AssertionError("stop")

    machine._sleep = stop
    dispatch = await store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    with pytest.raises(AssertionError, match="stop"):
        await machine.run(dispatch)
    paused = await store.get("d1")
    assert paused is not None
    paused_line = paused.line("line-0")
    assert paused_line is not None
    assert gates.push_wakeup(
        paused,
        "finished-run-1",
        "run_complete",
        "line-0",
        run_id=paused_line.current_run,
        attempt=paused_line.attempts,
    ) is True
    assert gates.push_wakeup(
        paused,
        "finished-run-1",
        "run_complete",
        "line-0",
        run_id=paused_line.current_run,
        attempt=paused_line.attempts,
    ) is False
    await store.save(paused)

    class KillCommand:
        async def run(self, argv, *, cwd, timeout, log_path):
            del argv, cwd, timeout
            log_path.parent.mkdir(parents=True, exist_ok=True)
            raise asyncio.CancelledError()

    _collaborators, resumed, _killed, _gates = _bundle(
        tmp_path,
        [],
        settings=settings,
        store=store,
        commands=KillCommand(),
    )
    _collaborators.monitor = Silent()
    with pytest.raises(asyncio.CancelledError):
        await resumed.run(await store.get("d1"))
    interrupted = await store.get("d1")
    assert interrupted is not None
    assert {
        "dispatch_id": "d1",
        "line_id": "line-0",
        "run_id": paused_line.current_run,
        "attempt": paused_line.attempts,
    } in interrupted.gates["scheduler"]["settled_lines"]
    assert "finished-run-1" in interrupted.gates["scheduler"]["handled"]
    assert interrupted.gates["lines"]["line-0"]["gates"]["G2"]["result"] == "running"

    _again, third, commands, replay_gates = _bundle(
        tmp_path,
        [],
        settings=settings,
        store=store,
    )
    monitor = Silent()
    _again.monitor = monitor

    async def fail_if_waiting(_seconds: float) -> None:
        raise AssertionError("the restarted machine waited on a dead monitor")

    third._sleep = fail_if_waiting
    result = await third.run(interrupted)
    assert monitor.polls == 0
    assert commands.calls
    assert result.line("line-0").status is LineStatus.PASSED
    assert replay_gates.push_wakeup(result, "finished-run-1", "run_complete", "line-0") is False


async def test_a_failed_persist_removes_the_worktree_and_starts_no_command(tmp_path: Path) -> None:
    trees = FakeWorktrees()
    commands = ScriptedCommands()
    gates = GateRunner(
        GateSettings(runs_dir=tmp_path / "runs", repo_root=tmp_path / "repo"),
        worktrees=trees,
        commands=commands,
        clock=lambda: WHEN,
    )
    line = _line("line-0")
    dispatch = Dispatch(id="d1", goal="ship it", user_id="u", created_at="now", lines=[line])
    path = tmp_path / "runs" / "gates" / "d1" / "_wt" / "line-0"

    async def cancel() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await gates.run_g2(dispatch, line, persist=cancel)
    assert not path.exists()
    assert commands.calls == []

    async def explode() -> None:
        raise RuntimeError("save failed")

    with pytest.raises(RuntimeError, match="save failed"):
        await gates.run_g2(dispatch, line, persist=explode)
    assert not path.exists()
    assert commands.calls == []


async def test_a_heartbeat_does_not_move_an_armed_deadline(tmp_path: Path) -> None:
    gates = GateRunner(
        _settings(tmp_path, tmp_path / "repo", watchdog_seconds=30),
        clock=lambda: WHEN,
    )
    dispatch = Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    assert gates.arm_watch(dispatch, 10) is True
    deadline = dispatch.gates["scheduler"]["deadline_at"]
    assert gates.arm_watch(dispatch, 10) is False
    gates.beat(dispatch)
    assert dispatch.gates["scheduler"]["deadline_at"] == deadline
    assert dispatch.gates["scheduler"]["last_seen"] == WHEN.isoformat().replace("+00:00", "+00:00")
    assert gates.watch_expired(dispatch) is False
