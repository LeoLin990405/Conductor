"""N25b: a BLOCKED line can be reassigned while its dispatch's drive runs.

The service queues the request in ``gates.human_reassigns`` without touching
the line or the epoch. The running drive applies it at its next loop step and
relaunches the line once. A request the drive never saw is applied by the
service before it releases the dispatch. RUNNING lines and other human calls
are still refused with ``drive_active``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.conductor_cli.dispatch import render_status
from src.server.services.dispatch import DispatchService, DispatchSettings, dispatch_summary
from src.tools.auto_dispatch import Dispatch, DispatchState, Line, LineStatus
from src.tools.auto_dispatch.gates import drive_epoch
from src.server.services.autonomy.stage_gate import Event, Stage, StageGateState, advance
from src.tools.auto_dispatch.machine import (
    HUMAN_REASSIGNS,
    REASSIGN_BRIEFS,
    pending_human_reassigns,
    reassign_brief,
)
from src.tools.auto_dispatch.types import RunStatus
from tests.unit.server.services.test_dispatch_r2_stale_result import Copies, _service
from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line


@pytest.fixture(autouse=True)
def _n73_human_g6(monkeypatch):
    """These tests pin the human G6; N73 S7 auto-approval is tested on its own."""
    monkeypatch.setenv("CONDUCTOR_DISPATCH_AUTO_APPROVE", "0")

pytestmark = pytest.mark.asyncio


def _settings(tmp_path: Path) -> DispatchSettings:
    return DispatchSettings(
        proposed_dir=tmp_path / "proposed",
        runs_dir=tmp_path / "runs",
        repo_root=tmp_path,
    )


def _handoff(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "branch": "feature/b",
        "commit": "abc123",
        "files": ("b.py",),
        "test_command": "pytest",
        "results": "fail",
        "known_limitations": "none",
    }


def _human_audit(row: Dispatch) -> list[dict[str, object]]:
    audit = ((row.gates or {}).get("scheduler") or {}).get("audit") or []
    return [item for item in audit if isinstance(item, dict) and item.get("kind") == "human_reassign"]


class HoldFirstPoll:
    """Holds the first poll, the running line's, until the test releases it."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.polls: list[str] = []

    async def poll(self, run_id: str) -> RunStatus:
        self.polls.append(run_id)
        if len(self.polls) == 1:
            self.entered.set()
            await self.release.wait()
        return RunStatus(output=f"done {run_id}", complete=True, tokens=8)

    async def cancel(self, run_id: str) -> None:
        del run_id


async def _no_schedule(dispatch: Dispatch) -> Dispatch:
    return dispatch


class PromptLauncher:
    """Records the model, the prompt task and the branch of every launch."""

    def __init__(self) -> None:
        self.launches: list[tuple[str, str, list[str]]] = []
        self.seen: list[tuple[str, str, str, str]] = []

    async def launch(self, line: Line, model: str, skills: list[str]) -> str:
        self.launches.append((line.id, model, list(skills)))
        self.seen.append((line.id, model, line.task, line.branch))
        return line.current_run or f"run-{line.id}-{len(self.seen)}"


B_BRANCH = "feature/auto-dispatch-handoff/line-b"
FINDING = "the parser still drops the last field"

# How line ``b`` waits: (line failure_reason, Stage-Gate machine fields).
BLOCKED_KINDS = {
    "review_blocked": (
        "review_blocked",
        {"stage": "fixing", "return_stage": "primary_review"},
    ),
    "replan_exhausted": (
        "replan_exhausted",
        {
            "stage": "replanning",
            "replan_options": ["resplit_task", "replace_developer"],
            "fix_rounds": 2,
        },
    ),
    # Paused by an operator during a fix round. A pause taken mid-
    # implementation is not a checkpoint; that refusal has its own test.
    "operator_blocked": (
        "operator_blocked",
        {"stage": "blocked", "blocked_from": "fixing", "return_stage": "primary_review"},
    ),
}


def _blocked_b(row: Dispatch, gates, kind: str) -> None:
    reason, machine_fields = BLOCKED_KINDS[kind]
    line = row.line("b")
    assert line is not None
    line.failure_reason = reason
    snap = gates.line_snapshot(row, line)
    snap["machine"].update(
        author_provider="deepseek",
        developer_provider="deepseek",
        **machine_fields,
    )
    if kind == "replan_exhausted":
        snap["replans"] = 1


def _assert_relaunch_prompt(launcher: PromptLauncher, reason: str) -> None:
    relaunched = [entry for entry in launcher.seen if entry[0] == "b"]
    assert len(relaunched) == 1
    _line_id, model, task, branch = relaunched[0]
    assert model == "stepfun"
    # The new model continues the existing branch and is told what to fix.
    assert branch == B_BRANCH
    assert task.startswith("do b\n\nHandoff:\n")
    assert "Blockers:\n" in task
    assert f"- {FINDING}" in task
    assert f"- Previous attempt stopped with: {reason}" in task


@pytest.mark.parametrize("kind", sorted(BLOCKED_KINDS))
async def test_a_blocked_line_is_relaunched_by_the_running_drive_once(
    tmp_path: Path, kind: str
) -> None:
    store = Copies()
    launcher = PromptLauncher()
    collaborators, machine, _commands, gates = _bundle(
        tmp_path, [], store=store, launcher=launcher
    )
    gates.handoff_facts = _handoff
    gates._revisions = lambda _line: None
    monitor = HoldFirstPoll()
    collaborators.monitor = monitor
    row = Dispatch(
        id="handoff",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.ASSIGNED,
        lines=[
            _line("a", status=LineStatus.ASSIGNED, assigned_model="kimi"),
            _line(
                "b",
                status=LineStatus.BLOCKED,
                assigned_model="deepseek",
                branch=B_BRANCH,
                blockers=[FINDING],
            ),
        ],
    )
    gates.prepare(row)
    _blocked_b(row, gates, kind)
    await store.create(row)
    service = _service(tmp_path, store, lambda _dispatch: machine)
    service._gate_runner = lambda: gates
    resumed: list[str] = []

    async def record_resume(dispatch: Dispatch) -> Dispatch:
        resumed.append(dispatch.id)
        return dispatch

    service._resume = record_resume
    drive = asyncio.create_task(service._drive(row.id))
    await asyncio.wait_for(monitor.entered.wait(), timeout=5)
    running = await store.get(row.id)
    assert running is not None
    assert running.line("a").status is LineStatus.RUNNING
    epoch = drive_epoch(running)

    # The RUNNING line belongs to the drive; that refusal is unchanged.
    with pytest.raises(ValueError, match="^drive_active$"):
        await service.reassign(row.id, "a", "stepfun")
    queued = await service.reassign(row.id, "b", "stepfun")

    # Queued only: the line, its attempt, and the epoch are as the drive left them.
    assert queued.line("b").status is LineStatus.BLOCKED
    assert queued.line("b").assigned_model == "deepseek"
    stored = await store.get(row.id)
    assert stored is not None
    assert drive_epoch(stored) == epoch
    assert stored.line("b").attempts == 0
    assert set(pending_human_reassigns(stored)) == {"b"}
    assert pending_human_reassigns(stored)["b"]["model"] == "stepfun"
    # The queue is visible to the API and the CLI.
    view = dispatch_summary(queued)["human_reassigns"]
    assert [(item["line_id"], item["model"]) for item in view["queued"]] == [("b", "stepfun")]
    assert view["dropped"] == []
    assert "queued: b → stepfun" in render_status(dispatch_summary(queued))
    assert resumed == []
    # A second request for the same line waits for the first.
    with pytest.raises(ValueError, match="^reassign_pending$"):
        await service.reassign(row.id, "b", "glm")

    monitor.release.set()
    await asyncio.wait_for(drive, timeout=10)

    after = await store.get(row.id)
    assert after is not None
    launched_b = [entry for entry in collaborators.launcher.launches if entry[0] == "b"]
    assert [model for _line_id, model, _skills in launched_b] == ["stepfun"]
    line_b = after.line("b")
    assert line_b is not None
    assert line_b.assigned_model == "stepfun"
    assert line_b.attempts == 1
    assert line_b.status is LineStatus.PASSED
    assert after.line("a").status is LineStatus.PASSED
    assert after.state is DispatchState.APPROVAL
    assert pending_human_reassigns(after) == {}
    assert dispatch_summary(after)["human_reassigns"]["queued"] == []
    # The brief is spent once the reassigned run has run.
    assert ((after.gates or {}).get(REASSIGN_BRIEFS) or {}).get("b") is None
    applied = _human_audit(after)
    assert len(applied) == 1
    assert applied[0]["applied_by"] == "drive"
    assert applied[0]["result"] == "applied"
    assert applied[0]["previous_model"] == "deepseek"
    assert applied[0]["previous_blockers"] == [FINDING]
    assert line_b.failure_reason is None
    assert line_b.branch == B_BRANCH
    _assert_relaunch_prompt(launcher, BLOCKED_KINDS[kind][0])
    assert not machine._abandoned
    assert row.id not in service.active_drives
    assert resumed == []


async def test_a_request_the_drive_never_saw_is_applied_before_release(tmp_path: Path) -> None:
    store = Copies()
    await store.create(
        Dispatch(
            id="late",
            goal="test",
            user_id="u",
            created_at="now",
            state=DispatchState.MONITORING,
            lines=[
                Line(id="a", name="a", task="x", assigned_model="kimi", status=LineStatus.RUNNING, current_run="run-a"),
                Line(
                    id="b",
                    name="b",
                    task="y",
                    assigned_model="kimi",
                    status=LineStatus.BLOCKED,
                    failure_reason="operator_blocked",
                    blockers=["rate limited three times"],
                ),
            ],
        )
    )
    entered, release = asyncio.Event(), asyncio.Event()
    seen: list[tuple[str | None, int, LineStatus]] = []

    class Machine:
        """A drive that never reads the queue, like one that parks right after the request."""

        async def run(self, dispatch: Dispatch) -> Dispatch:
            line = dispatch.line("b")
            assert line is not None
            seen.append((line.assigned_model, line.attempts, line.status))
            if len(seen) == 1:
                entered.set()
                await release.wait()
                dispatch.state = DispatchState.REVIEW
            return dispatch

    service = DispatchService(
        _settings(tmp_path), store=store, machine_factory=lambda _row: Machine()
    )
    drive = asyncio.create_task(service._drive("late"))
    await asyncio.wait_for(entered.wait(), timeout=2)

    with pytest.raises(ValueError, match="^drive_active$"):
        await service.reassign("late", "a", "stepfun")
    for call in (service.approve("late", "note"), service.reject("late", "note")):
        with pytest.raises(ValueError, match="^drive_active$"):
            await call
    await service.reassign("late", "b", "stepfun")
    held = await store.get("late")
    assert held is not None
    assert held.line("b").status is LineStatus.BLOCKED
    assert drive_epoch(held) == 0

    release.set()
    await asyncio.wait_for(drive, timeout=5)

    # The body ran exactly twice: once as started, once for the reassigned line.
    assert seen == [
        ("kimi", 0, LineStatus.BLOCKED),
        ("stepfun", 1, LineStatus.ASSIGNED),
    ]
    after = await store.get("late")
    assert after is not None
    assert after.line("b").assigned_model == "stepfun"
    assert after.line("b").attempts == 1
    assert drive_epoch(after) == 1
    assert pending_human_reassigns(after) == {}
    assert isinstance((after.gates or {}).get(HUMAN_REASSIGNS), dict)
    audit = _human_audit(after)
    assert [(item["applied_by"], item["result"]) for item in audit] == [("service", "applied")]
    assert audit[0]["previous_blockers"] == ["rate limited three times"]
    assert "late" not in service.active_drives


async def test_a_queued_request_for_a_line_that_moved_is_dropped(tmp_path: Path) -> None:
    store = Copies()
    await store.create(
        Dispatch(
            id="moved",
            goal="test",
            user_id="u",
            created_at="now",
            state=DispatchState.MONITORING,
            lines=[
                Line(id="b", name="b", task="y", assigned_model="kimi", status=LineStatus.BLOCKED),
            ],
        )
    )
    entered, release = asyncio.Event(), asyncio.Event()
    runs = {"n": 0}

    class Machine:
        async def run(self, dispatch: Dispatch) -> Dispatch:
            runs["n"] += 1
            entered.set()
            await release.wait()
            return dispatch

    service = DispatchService(
        _settings(tmp_path), store=store, machine_factory=lambda _row: Machine()
    )
    drive = asyncio.create_task(service._drive("moved"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    await service.reassign("moved", "b", "stepfun")

    # The line takes another attempt before the drive ends, so the request is stale.
    def bump_attempt(row: Dispatch) -> Dispatch:
        line = row.line("b")
        assert line is not None
        line.attempts += 1
        return row

    await service.apply_mutation("moved", bump_attempt)
    release.set()
    await asyncio.wait_for(drive, timeout=5)

    after = await store.get("moved")
    assert after is not None
    assert runs["n"] == 1
    assert after.line("b").assigned_model == "kimi"
    assert after.line("b").status is LineStatus.BLOCKED
    assert pending_human_reassigns(after) == {}
    audit = _human_audit(after)
    assert [(item["result"], item.get("reason")) for item in audit] == [("refused", "line_moved")]
    view = dispatch_summary(after)["human_reassigns"]
    assert view["queued"] == []
    assert [(item["line_id"], item["model"], item["reason"]) for item in view["dropped"]] == [
        ("b", "stepfun", "line_moved")
    ]
    assert "dropped: b → stepfun (line_moved)" in render_status(dispatch_summary(after))
    assert "moved" not in service.active_drives


@pytest.mark.parametrize("kind", sorted(BLOCKED_KINDS))
async def test_a_parked_reassign_relaunches_on_the_branch_with_the_blockers(
    tmp_path: Path, kind: str
) -> None:
    """No drive: the reassign is applied at once, and the next drive relaunches."""
    store = Copies()
    launcher = PromptLauncher()
    _collaborators, machine, _commands, gates = _bundle(
        tmp_path, [], store=store, launcher=launcher
    )
    gates.handoff_facts = _handoff
    gates._revisions = lambda _line: None
    row = Dispatch(
        id="handoff",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.REVIEW,
        lines=[
            _line(
                "b",
                status=LineStatus.BLOCKED,
                assigned_model="deepseek",
                branch=B_BRANCH,
                blockers=[FINDING],
            ),
        ],
    )
    gates.prepare(row)
    _blocked_b(row, gates, kind)
    await store.create(row)
    service = _service(tmp_path, store, lambda _dispatch: machine)
    service._gate_runner = lambda: gates
    service._resume = _no_schedule

    reassigned = await service.reassign(row.id, "b", "stepfun")
    line = reassigned.line("b")
    assert line is not None
    assert line.status is LineStatus.ASSIGNED
    assert line.assigned_model == "stepfun"
    assert line.failure_reason is None
    assert line.branch == B_BRANCH
    assert drive_epoch(reassigned) == 1

    await asyncio.wait_for(service._drive(row.id), timeout=10)

    after = await store.get(row.id)
    assert after is not None
    assert after.line("b").status is LineStatus.PASSED
    assert after.line("b").assigned_model == "stepfun"
    _assert_relaunch_prompt(launcher, BLOCKED_KINDS[kind][0])
    assert ((after.gates or {}).get(REASSIGN_BRIEFS) or {}).get("b") is None


async def test_a_running_line_is_still_refused_when_parked(tmp_path: Path) -> None:
    store = Copies()
    _collaborators, machine, _commands, gates = _bundle(tmp_path, [], store=store)
    gates.handoff_facts = _handoff
    row = Dispatch(
        id="still-running",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        lines=[_line("a", status=LineStatus.RUNNING, assigned_model="kimi", current_run="run-a")],
    )
    gates.prepare(row)
    gates.line_snapshot(row, row.line("a"))["machine"].update(
        stage="implementing", author_provider="kimi", developer_provider="kimi"
    )
    await store.create(row)
    service = _service(tmp_path, store, lambda _dispatch: machine)
    service._gate_runner = lambda: gates
    service._resume = _no_schedule

    with pytest.raises(ValueError, match="^reassignment_not_at_checkpoint$"):
        await service.reassign(row.id, "a", "stepfun")
    kept = await store.get(row.id)
    assert kept is not None
    assert kept.line("a").assigned_model == "kimi"
    assert kept.line("a").status is LineStatus.RUNNING


def _paused(blocked_from: str) -> StageGateState:
    return StageGateState(
        task_id="b",
        stage=Stage.BLOCKED,
        author_provider="deepseek",
        developer_provider="deepseek",
        contributors=("deepseek",),
        blocked_from=blocked_from,
    )


async def test_stage_gate_refuses_a_swap_of_a_task_paused_mid_implementation() -> None:
    from src.server.services.autonomy.stage_gate import Handoff

    handoff = Handoff(
        branch="feature/b",
        commit="abc123",
        files=("b.py",),
        test_command="pytest",
        results="fail",
        known_limitations="none",
    )
    event = Event(kind="reassign", timestamp="2026-11-10T00:00:00+00:00", provider="stepfun", handoff=handoff)
    refused = advance(_paused("implementing"), event)
    assert not refused.accepted
    assert refused.reason == "reassignment_not_at_checkpoint"
    accepted = advance(_paused("fixing"), event)
    assert accepted.accepted
    assert accepted.state.stage is Stage.ASSIGNED
    assert accepted.state.developer_provider == "stepfun"
    assert accepted.state.blocked_from == ""


@pytest.mark.parametrize("driving", [False, True])
async def test_a_line_paused_mid_implementation_is_refused(tmp_path: Path, driving: bool) -> None:
    store = Copies()
    _collaborators, machine, _commands, gates = _bundle(tmp_path, [], store=store)
    gates.handoff_facts = _handoff
    row = Dispatch(
        id="paused-impl",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.REVIEW,
        lines=[
            _line(
                "b",
                status=LineStatus.BLOCKED,
                assigned_model="deepseek",
                failure_reason="worker_rate_limited_retries_exhausted",
            )
        ],
    )
    gates.prepare(row)
    gates.line_snapshot(row, row.line("b"))["machine"].update(
        stage="blocked",
        blocked_from="implementing",
        author_provider="deepseek",
        developer_provider="deepseek",
    )
    await store.create(row)
    service = _service(tmp_path, store, lambda _dispatch: machine)
    service._gate_runner = lambda: gates
    service._resume = _no_schedule
    if driving:
        service.active_drives[row.id] = object()

    with pytest.raises(ValueError, match="^reassignment_not_at_checkpoint$"):
        await service.reassign(row.id, "b", "stepfun")
    kept = await store.get(row.id)
    assert kept is not None
    assert kept.line("b").assigned_model == "deepseek"
    assert kept.line("b").status is LineStatus.BLOCKED
    assert pending_human_reassigns(kept) == {}


async def test_a_spent_or_other_attempt_brief_is_not_used() -> None:
    row = Dispatch(
        id="brief",
        goal="test",
        user_id="u",
        created_at="now",
        lines=[Line(id="b", name="b", task="t", assigned_model="stepfun", attempts=2)],
        gates={REASSIGN_BRIEFS: {"b": {"author": "stepfun", "attempt": 1, "blockers": ["x"]}}},
    )
    line = row.line("b")
    assert line is not None
    assert reassign_brief(row, line) is None
    line.attempts = 1
    assert reassign_brief(row, line) == ["x"]
