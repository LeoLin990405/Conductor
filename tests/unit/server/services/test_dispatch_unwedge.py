"""N36, N40, N41: the dispatch wedges an operator had no way out of.

Each wedge is reproduced with fakes through the real machine and service, and
then resolved by the new transition or command:

- N40: a run that ends with exit 0 and no new commit, or with a dirty tree,
  left the line in Stage-Gate ``implementing``. ``dispatch reassign`` answered
  reassignment_not_at_checkpoint, and every other ASSIGNED line waited in
  ``serial_wait``. Such a run now records the ``worker_exited`` checkpoint and
  a Stage-Gate pause; ``dispatch unblock`` does the same for a line parked
  before this change.
- N36: ``dispatch reject`` worked only in APPROVAL; a parked dispatch can now
  be rejected or cancelled while no line runs.
- N41: nothing sent ``adjudicate_conflict``, so a line in
  ``awaiting_adjudication`` waited forever; ``dispatch adjudicate`` sends it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus, Verdict
from src.tools.auto_dispatch.execution import LaunchResult
from src.tools.auto_dispatch.machine import (
    ADJUDICATION,
    FOLLOW_UPS,
    WORKER_EXITED,
)
from src.tools.auto_dispatch.protocols import NoNewCommitError, UncommittedChangesError
from tests.unit.server.services.test_dispatch_r2_stale_result import Copies, _service
from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line

pytestmark = pytest.mark.asyncio

BASE = "b" * 40
HEAD = "1" * 40


def _handoff(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "branch": "feature/x",
        "commit": "abc123",
        "files": ("x.py",),
        "test_command": "pytest",
        "results": "ok",
        "known_limitations": "none",
    }


class ScriptedLauncher:
    """Per line, a list of outcomes: "commit", "no_commit" or "dirty"."""

    def __init__(self, script: dict[str, list[str]]) -> None:
        self.script = {key: list(value) for key, value in script.items()}
        self.calls: list[tuple[str, str]] = []
        self.tasks: list[tuple[str, str]] = []
        self.dirty_allowed: list[tuple[str, bool]] = []

    async def launch(self, line, model, skills):
        raise AssertionError("the recovery machine uses launch_worker")

    async def launch_worker(
        self,
        line,
        model,
        skills,
        *,
        session_id,
        session_mode,
        handoff_path,
        allow_dirty_worktree=False,
    ):
        self.calls.append((line.id, model))
        self.tasks.append((line.id, line.task))
        self.dirty_allowed.append((line.id, allow_dirty_worktree))
        run_id = line.current_run or f"run-{len(self.calls)}"
        steps = self.script.get(line.id) or []
        outcome = steps.pop(0) if steps else "commit"
        if outcome == "no_commit":
            error = NoNewCommitError(
                f"no_new_commit for line {line.id}", run_id=run_id, branch_has_commits=False
            )
            error.head = BASE
            error.base = BASE
            raise error
        if outcome == "dirty":
            raise UncommittedChangesError(
                f"uncommitted_changes for line {line.id}", run_id=run_id
            )
        return LaunchResult(run_id, session_id, "committed", start_head=BASE, head=HEAD)

    async def uncommitted_diff(self, line, *, max_bytes=16_000):
        return "## git diff HEAD\n+half-done edit\n"


class ScriptedReviewer:
    """Verdicts in order, per line; then every review passes."""

    def __init__(self, script: dict[str, list[Verdict]] | None = None) -> None:
        self.script = {key: list(value) for key, value in (script or {}).items()}
        self.reviewed: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.reviewed.append((line_id, model))
        steps = self.script.get(line_id) or []
        if steps:
            return steps.pop(0)
        return Verdict.ok(f"reviewed by {model}")


async def _no_schedule(dispatch: Dispatch) -> Dispatch:
    return dispatch


async def _setup(
    tmp_path: Path,
    lines,
    *,
    launcher=None,
    reviewer=None,
    dispatch_id: str = "wedge",
):
    store = Copies()
    launcher = launcher or ScriptedLauncher({})
    overrides = {"store": store, "launcher": launcher}
    if reviewer is not None:
        overrides["reviewer"] = reviewer
    _collaborators, machine, _commands, gates = _bundle(tmp_path, [], **overrides)
    gates.handoff_facts = _handoff
    gates._revisions = lambda _line: None
    row = Dispatch(
        id=dispatch_id,
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.ASSIGNED,
        lines=lines,
    )
    gates.prepare(row)
    await store.create(row)
    service = _service(tmp_path, store, lambda _dispatch: machine)
    service._gate_runner = lambda: gates
    service._resume = _no_schedule
    return store, launcher, gates, service, row


async def _drive(service, dispatch_id: str) -> None:
    await asyncio.wait_for(service._drive(dispatch_id), timeout=10)


def _assigned(line_id: str, model: str, **extra):
    return _line(
        line_id,
        status=LineStatus.ASSIGNED,
        assigned_model=model,
        base_commit=BASE,
        **extra,
    )


# -- N40 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [("no_commit", "no_new_commit"), ("dirty", "uncommitted_changes")],
)
async def test_an_exited_run_is_a_checkpoint_and_independent_lines_keep_running(
    tmp_path: Path, outcome: str, reason: str
) -> None:
    launcher = ScriptedLauncher({"a": [outcome]})
    store, _launcher, _gates, service, row = await _setup(
        tmp_path,
        [_assigned("a", "deepseek"), _assigned("b", "kimi")],
        launcher=launcher,
    )
    await _drive(service, row.id)

    parked = await store.get(row.id)
    assert parked is not None
    line_a, line_b = parked.line("a"), parked.line("b")
    assert line_a.status is LineStatus.BLOCKED
    assert line_a.failure_reason == reason
    snap = parked.gates["lines"]["a"]
    # Stage-Gate recorded the pause: the line no longer holds the serial slot.
    assert snap["machine"]["stage"] == "blocked"
    assert snap["machine"]["blocked_from"] == "implementing"
    assert snap[WORKER_EXITED]["failure_class"] == reason
    assert snap[WORKER_EXITED]["author"] == "deepseek"
    # The independent line ran and passed instead of waiting in serial_wait.
    assert line_b.status is LineStatus.PASSED
    assert ("b", "kimi") in launcher.calls
    assert parked.state is DispatchState.REVIEW

    # A human reassign is now accepted and the new model continues the branch.
    reassigned = await service.reassign(row.id, "a", "stepfun")
    line = reassigned.line("a")
    assert line.status is LineStatus.ASSIGNED
    assert line.assigned_model == "stepfun"
    assert WORKER_EXITED not in reassigned.gates["lines"]["a"]
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert after.line("a").status is LineStatus.PASSED
    assert launcher.calls[-1] == ("a", "stepfun")
    relaunch_task = launcher.tasks[-1][1]
    assert f"Previous attempt stopped with: {reason}" in relaunch_task
    if outcome == "dirty":
        # The dirty tree stays in the worktree: the relaunch may start on it and
        # its prompt carries the diff as context.
        assert launcher.dirty_allowed[-1] == ("a", True)
        assert "+half-done edit" in relaunch_task
        assert "uncommitted changes" in relaunch_task
    else:
        assert launcher.dirty_allowed[-1] == ("a", False)


def _wedge_like_before(row: Dispatch, *, stage: str = "implementing") -> Dispatch:
    """A line parked by the old code: BLOCKED no_new_commit, still implementing."""
    line = row.line("a")
    assert line is not None
    line.status = LineStatus.BLOCKED
    line.failure_reason = "no_new_commit"
    line.current_run = None
    line.blockers = ["Worker exited without creating a commit; line branch preserved"]
    snap = row.gates["lines"]["a"]
    machine = dict(snap.get("machine") or {})
    machine.update(
        {
            "task_id": "a",
            "stage": stage,
            "author_provider": "deepseek",
            "developer_provider": "deepseek",
            "blocked_from": "implementing" if stage == "blocked" else "",
        }
    )
    snap["machine"] = machine
    snap.pop(WORKER_EXITED, None)
    other = row.line("b")
    assert other is not None
    other.failure_reason = "serial_wait"
    row.gates.setdefault("scheduler", {})["pending_relaunch"] = [
        {"line_id": "b", "reason": "serial_wait"}
    ]
    row.state = DispatchState.REVIEW
    return row


@pytest.mark.parametrize("stage", ["implementing", "blocked"])
async def test_unblock_moves_an_old_wedged_line_to_a_checkpoint(
    tmp_path: Path, stage: str
) -> None:
    store, launcher, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "deepseek"), _assigned("b", "kimi")]
    )
    await service.apply_mutation(row.id, lambda r: _wedge_like_before(r, stage=stage))

    with pytest.raises(ValueError, match="^reassignment_not_at_checkpoint$"):
        await service.reassign(row.id, "a", "stepfun")

    unblocked = await service.unblock(row.id, "a", note="worker gone since 09:00")
    snap = unblocked.gates["lines"]["a"]
    assert snap["machine"]["stage"] == "blocked"
    assert snap["machine"]["blocked_from"] == "implementing"
    assert snap[WORKER_EXITED]["failure_class"] == "operator_unblock"
    assert snap[WORKER_EXITED]["note"] == "worker gone since 09:00"
    assert unblocked.line("a").status is LineStatus.BLOCKED
    audit = unblocked.gates["scheduler"]["audit"]
    assert any(item.get("kind") == "operator_unblock" for item in audit)

    # The parked drive runs again: the line that waited in serial_wait goes.
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert after.line("b").status is LineStatus.PASSED
    assert launcher.calls == [("b", "kimi")]

    reassigned = await service.reassign(row.id, "a", "stepfun")
    assert reassigned.line("a").assigned_model == "stepfun"
    await _drive(service, row.id)
    done = await store.get(row.id)
    assert done.line("a").status is LineStatus.PASSED


async def test_unblock_refuses_while_a_run_of_the_line_is_out(tmp_path: Path) -> None:
    store, _launcher, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "deepseek"), _assigned("b", "kimi")]
    )
    await service.apply_mutation(row.id, _wedge_like_before)

    def run_out(r: Dispatch) -> Dispatch:
        r.line("a").current_run = "wedge:a:0:still"
        return r

    await service.apply_mutation(row.id, run_out)
    with pytest.raises(ValueError, match="^run_out$"):
        await service.unblock(row.id, "a")


async def test_unblock_refuses_a_live_launch_of_the_line(
    tmp_path: Path, monkeypatch
) -> None:
    store, _launcher, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "deepseek"), _assigned("b", "kimi")]
    )
    await service.apply_mutation(row.id, _wedge_like_before)
    import src.tools.auto_dispatch.execution as execution

    monkeypatch.setattr(
        execution,
        "live_runs_for_line",
        lambda dispatch_id, line_id: {f"{dispatch_id}:{line_id}:0:live"}
        if line_id == "a"
        else set(),
    )
    with pytest.raises(ValueError, match="^run_out$"):
        await service.unblock(row.id, "a")
    kept = await store.get(row.id)
    assert WORKER_EXITED not in kept.gates["lines"]["a"]


async def test_unblock_refuses_a_line_that_is_not_wedged(tmp_path: Path) -> None:
    _store, _launcher, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "deepseek"), _assigned("b", "kimi")]
    )
    # ASSIGNED, never launched: not parked.
    with pytest.raises(ValueError, match="^line_not_parked$"):
        await service.unblock(row.id, "a")

    def review_block(r: Dispatch) -> Dispatch:
        line = r.line("a")
        line.status = LineStatus.BLOCKED
        line.failure_reason = "review_blocked"
        r.gates["lines"]["a"]["machine"] = {
            **(r.gates["lines"]["a"].get("machine") or {}),
            "task_id": "a",
            "stage": "fixing",
        }
        return r

    await service.apply_mutation(row.id, review_block)
    with pytest.raises(ValueError, match="^line_not_wedged$"):
        await service.unblock(row.id, "a")
    with pytest.raises(ValueError, match="no such line"):
        await service.unblock(row.id, "zzz")


async def test_unblock_while_a_drive_runs_is_queued_and_applied_by_the_drive(
    tmp_path: Path,
) -> None:
    store, _launcher, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "deepseek"), _assigned("b", "kimi")]
    )
    await service.apply_mutation(row.id, _wedge_like_before)
    token = object()
    service.active_drives[row.id] = token
    try:
        queued = await service.unblock(row.id, "a")
    finally:
        service.active_drives.pop(row.id, None)
    assert queued.gates["human_reassigns"]["a"]["kind"] == "unblock"
    assert WORKER_EXITED not in queued.gates["lines"]["a"]
    # The drive applies it at its next loop step, then runs the waiting line.
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert after.gates["human_reassigns"] == {}
    assert after.gates["lines"]["a"][WORKER_EXITED]["failure_class"] == "operator_unblock"
    assert after.line("b").status is LineStatus.PASSED


# -- N36 ------------------------------------------------------------------------


async def _parked_review(tmp_path: Path):
    launcher = ScriptedLauncher({"a": ["no_commit"]})
    store, launcher, gates, service, row = await _setup(
        tmp_path,
        [_assigned("a", "deepseek"), _assigned("b", "kimi", depends_on=("a",))],
        launcher=launcher,
    )
    await _drive(service, row.id)
    parked = await store.get(row.id)
    assert parked.state is DispatchState.REVIEW
    return store, launcher, gates, service, parked


@pytest.mark.parametrize("verb", ["cancel", "reject"])
async def test_a_parked_dispatch_can_be_cancelled_or_rejected(
    tmp_path: Path, verb: str
) -> None:
    store, _launcher, _gates, service, parked = await _parked_review(tmp_path)
    ended = await getattr(service, verb)(parked.id, note="not worth it")
    assert ended.state is DispatchState.ABORT
    word = "rejected" if verb == "reject" else "cancelled"
    assert f"An operator {word} this dispatch while it was REVIEW" in ended.message
    assert "not worth it" in ended.message
    for line_id in ("a", "b"):
        line = ended.line(line_id)
        assert line.status is LineStatus.FAILED
        assert line.failure_reason == f"dispatch_{word}"
        assert line.current_run is None
        assert ended.gates["lines"][line_id]["machine"]["stage"] == "cancelled"
    # Branches are kept: the line keeps its branch name, nothing is set aside.
    assert ended.line("a").branch
    audit = ended.gates["scheduler"]["audit"]
    entry = next(item for item in audit if item.get("kind") == f"operator_{verb}")
    assert {item["line_id"] for item in entry["released"]} == {"a", "b"}
    # A second cancel is refused: the dispatch is terminal.
    with pytest.raises(ValueError, match="ABORT"):
        await service.cancel(parked.id)


async def test_cancel_refuses_a_running_line_unless_forced(
    tmp_path: Path, monkeypatch
) -> None:
    store, _launcher, _gates, service, parked = await _parked_review(tmp_path)

    def running(r: Dispatch) -> Dispatch:
        line = r.line("b")
        line.status = LineStatus.RUNNING
        line.current_run = "wedge:b:0:live"
        r.state = DispatchState.MONITORING
        return r

    await service.apply_mutation(parked.id, running)
    with pytest.raises(ValueError, match="^line_running: b"):
        await service.cancel(parked.id)
    with pytest.raises(ValueError, match="^line_running: b"):
        await service.reject(parked.id)
    kept = await store.get(parked.id)
    assert kept.state is DispatchState.MONITORING

    import src.tools.auto_dispatch.execution as execution

    stopped: list[str] = []

    async def fake_cancel(run_id: str) -> bool:
        stopped.append(run_id)
        return True

    monkeypatch.setattr(execution, "cancel_live_run", fake_cancel)
    ended = await service.cancel(parked.id, force=True)
    assert stopped == ["wedge:b:0:live"]
    assert ended.state is DispatchState.ABORT
    assert ended.line("b").current_run is None


async def test_cancel_refuses_the_approval_gate_and_a_live_drive(tmp_path: Path) -> None:
    store, _launcher, _gates, service, parked = await _parked_review(tmp_path)
    service.active_drives[parked.id] = object()
    try:
        with pytest.raises(ValueError, match="^drive_active"):
            await service.cancel(parked.id)
    finally:
        service.active_drives.pop(parked.id, None)

    def at_gate(r: Dispatch) -> Dispatch:
        r.state = DispatchState.APPROVAL
        return r

    await service.apply_mutation(parked.id, at_gate)
    with pytest.raises(ValueError, match="awaiting approval; reject it instead"):
        await service.cancel(parked.id)


# -- N41 ------------------------------------------------------------------------


def _conflict(kind: str) -> Verdict:
    return Verdict.blocked([f"{kind}: this change also rewrites the billing module"])


async def _awaiting(tmp_path: Path, kind: str = "scope"):
    reviewer = ScriptedReviewer(
        {"a": [Verdict.ok("primary ok"), _conflict(kind)]}
    )
    launcher = ScriptedLauncher({})
    store, launcher, gates, service, row = await _setup(
        tmp_path,
        [
            _assigned("a", "deepseek", risk_bearing=True),
            _assigned("b", "kimi"),
        ],
        launcher=launcher,
        reviewer=reviewer,
    )
    await _drive(service, row.id)
    parked = await store.get(row.id)
    return store, launcher, reviewer, service, parked


async def test_independent_lines_keep_running_while_a_line_awaits_adjudication(
    tmp_path: Path,
) -> None:
    store, launcher, _reviewer, service, parked = await _awaiting(tmp_path)
    line_a = parked.line("a")
    assert line_a.status is LineStatus.BLOCKED
    assert line_a.failure_reason == "reviewer_conflict_scope"
    snap = parked.gates["lines"]["a"]
    assert snap["machine"]["stage"] == "awaiting_adjudication"
    assert snap[ADJUDICATION]["conflict"] == "scope"
    assert snap[ADJUDICATION]["findings"] == [
        "scope: this change also rewrites the billing module"
    ]
    assert parked.line("b").status is LineStatus.PASSED
    assert parked.state is DispatchState.REVIEW
    # A reassign still cannot take the line out of adjudication.
    with pytest.raises(ValueError, match="^stage_gate_order$"):
        await service.reassign(parked.id, "a", "stepfun")


async def test_in_scope_gives_the_author_a_fix_round(tmp_path: Path) -> None:
    store, launcher, reviewer, service, parked = await _awaiting(tmp_path)
    ruled = await service.adjudicate(parked.id, "a", "in-scope", note="billing is part of it")
    line = ruled.line("a")
    assert line.status is LineStatus.ASSIGNED
    assert line.assigned_model == "deepseek"
    assert line.blockers == [
        "scope: this change also rewrites the billing module",
        "Planner ruling (in scope): billing is part of it",
    ]
    snap = ruled.gates["lines"]["a"]
    assert snap["machine"]["stage"] == "fixing"
    assert snap[ADJUDICATION]["ruling"] == "in-scope"

    await _drive(service, parked.id)
    after = await store.get(parked.id)
    assert launcher.calls[-1] == ("a", "deepseek")
    assert "billing is part of it" in launcher.tasks[-1][1]
    assert after.line("a").status is LineStatus.PASSED
    # The secondary review ran again on the fix.
    assert [item for item in reviewer.reviewed if item[0] == "a"][-1][0] == "a"
    assert len([item for item in reviewer.reviewed if item[0] == "a"]) == 3


async def test_out_of_scope_passes_the_review_and_keeps_a_follow_up(tmp_path: Path) -> None:
    store, launcher, _reviewer, service, parked = await _awaiting(tmp_path)
    calls = len(launcher.calls)
    ruled = await service.adjudicate(parked.id, "a", "out_of_scope", note="separate ticket")
    line = ruled.line("a")
    assert line.status is LineStatus.PASSED
    assert line.blockers == []
    assert ruled.gates["lines"]["a"]["machine"]["stage"] == "integration_checks"
    follow = ruled.gates[FOLLOW_UPS]
    assert follow[-1]["line_id"] == "a"
    assert follow[-1]["note"] == "separate ticket"
    assert follow[-1]["findings"] == ["scope: this change also rewrites the billing module"]
    await _drive(service, parked.id)
    after = await store.get(parked.id)
    # Nothing relaunched; the dispatch went on to G5 and the gate.
    assert len(launcher.calls) == calls
    assert after.state is DispatchState.APPROVAL


async def test_a_security_conflict_needs_a_note_to_be_ruled_out_of_scope(
    tmp_path: Path,
) -> None:
    store, _launcher, _reviewer, service, parked = await _awaiting(tmp_path, "security")
    assert parked.line("a").failure_reason == "reviewer_conflict_security"
    with pytest.raises(ValueError, match="^security_out_of_scope_needs_note$"):
        await service.adjudicate(parked.id, "a", "out-of-scope")
    ruled = await service.adjudicate(
        parked.id, "a", "out-of-scope", note="the token is a test fixture"
    )
    assert ruled.line("a").status is LineStatus.PASSED


async def test_adjudicate_refuses_a_line_not_awaiting_it(tmp_path: Path) -> None:
    store, _launcher, _reviewer, service, parked = await _awaiting(tmp_path)
    with pytest.raises(ValueError, match="^not_awaiting_adjudication$"):
        await service.adjudicate(parked.id, "b", "in-scope")
    with pytest.raises(ValueError, match="^invalid_ruling"):
        await service.adjudicate(parked.id, "a", "maybe")


async def test_adjudicate_while_a_drive_runs_is_queued(tmp_path: Path) -> None:
    store, launcher, _reviewer, service, parked = await _awaiting(tmp_path)
    service.active_drives[parked.id] = object()
    try:
        queued = await service.adjudicate(parked.id, "a", "in-scope")
        with pytest.raises(ValueError, match="^request_pending$"):
            await service.adjudicate(parked.id, "a", "in-scope")
    finally:
        service.active_drives.pop(parked.id, None)
    assert queued.gates["human_reassigns"]["a"]["kind"] == "adjudicate"
    assert queued.line("a").status is LineStatus.BLOCKED
    await _drive(service, parked.id)
    after = await store.get(parked.id)
    assert after.gates["human_reassigns"] == {}
    assert after.line("a").status is LineStatus.PASSED
    assert launcher.calls[-1] == ("a", "deepseek")


async def test_a_passed_line_with_its_last_run_id_does_not_block_cancel(
    tmp_path: Path,
) -> None:
    """Review fix 1: a PASSED line keeps its run id; it has no worker out."""
    store, _launcher, _reviewer, service, parked = await _awaiting(tmp_path)
    assert parked.line("b").status is LineStatus.PASSED
    assert parked.line("b").current_run
    assert parked.line("a").status is LineStatus.BLOCKED
    ended = await service.cancel(parked.id, note="mixed")
    assert ended.state is DispatchState.ABORT
    # The PASSED line is left as it was; the blocked one is released.
    assert ended.line("b").status is LineStatus.PASSED
    assert ended.line("a").failure_reason == "dispatch_cancelled"


async def test_a_running_line_whose_run_settled_is_not_a_running_worker(
    tmp_path: Path,
) -> None:
    from src.tools.auto_dispatch.operator import running_line_ids

    store, _launcher, _reviewer, service, parked = await _awaiting(tmp_path)
    row = await store.get(parked.id)
    line = row.line("a")
    line.status = LineStatus.RUNNING
    line.current_run = "wedge:a:0:done"
    assert running_line_ids(row) == ["a"]
    row.gates.setdefault("scheduler", {}).setdefault("settled_lines", []).append(
        {"line_id": "a", "run_id": "wedge:a:0:done", "attempt": line.attempts}
    )
    assert running_line_ids(row) == []


async def test_a_launch_never_spawns_after_the_epoch_moved(tmp_path: Path) -> None:
    """Review fix 2: the epoch is re-read immediately before the spawn.

    The legacy ``launch`` path has no session save in between, so only the
    spawn fence stops it.
    """
    from src.tools.auto_dispatch.gates import bump_epoch

    class LegacyLauncher:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def launch(self, line, model, skills):
            self.calls.append((line.id, model))
            return line.current_run

    store, launcher, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "deepseek")], launcher=LegacyLauncher()
    )
    machine = service._machine(row)
    reserve = machine._reserve_run

    async def reserve_then_cancel(dispatch, line):
        reserved = await reserve(dispatch, line)
        # An operator cancel --force lands between the reservation and the spawn.
        stored = store.rows[dispatch.id]
        bump_epoch(stored)
        return reserved

    machine._reserve_run = reserve_then_cancel
    await _drive(service, row.id)
    assert launcher.calls == []
    assert machine._abandoned


async def test_cancel_force_stops_a_launch_that_appears_after_the_bump(
    tmp_path: Path, monkeypatch
) -> None:
    import src.server.services.dispatch as service_module
    import src.tools.auto_dispatch.execution as execution

    store, _launcher, _gates, service, parked = await _parked_review(tmp_path)
    monkeypatch.setattr(service_module, "_LATE_LAUNCH_SCAN_SECONDS", 0)
    scans = {"n": 0}
    late = f"{parked.id}:b:0:late"

    def live(dispatch_id: str, line_id: str) -> set[str]:
        if line_id != "b":
            return set()
        scans["n"] += 1
        # Nothing at the check under the lock; the late spawn shows up after.
        return {late} if 2 <= scans["n"] <= 3 else set()

    stopped: list[str] = []

    async def fake_cancel(run_id: str) -> bool:
        stopped.append(run_id)
        return True

    monkeypatch.setattr(execution, "live_runs_for_line", live)
    monkeypatch.setattr(execution, "cancel_live_run", fake_cancel)
    ended = await service.cancel(parked.id, force=True)
    assert ended.state is DispatchState.ABORT
    assert late in stopped


async def _awaiting_mixed(tmp_path: Path):
    mixed = Verdict.blocked(
        [
            "scope: this change also rewrites the billing module",
            "the new test fails on an empty cart",
        ]
    )
    reviewer = ScriptedReviewer({"a": [Verdict.ok("primary ok"), mixed]})
    store, launcher, gates, service, row = await _setup(
        tmp_path,
        [_assigned("a", "deepseek", risk_bearing=True), _assigned("b", "kimi")],
        reviewer=reviewer,
    )
    await _drive(service, row.id)
    return store, launcher, reviewer, service, await store.get(row.id)


async def test_out_of_scope_on_a_mixed_verdict_keeps_the_other_blockers(
    tmp_path: Path,
) -> None:
    """Review fix 4: only the conflict finding is ruled out of scope."""
    store, launcher, reviewer, service, parked = await _awaiting_mixed(tmp_path)
    snap = parked.gates["lines"]["a"]
    assert snap[ADJUDICATION]["findings"] == [
        "scope: this change also rewrites the billing module"
    ]
    assert snap[ADJUDICATION]["other_blockers"] == ["the new test fails on an empty cart"]

    ruled = await service.adjudicate(parked.id, "a", "out-of-scope", note="ticket 42")
    line = ruled.line("a")
    # The review does not pass: the author gets a fix round for the rest.
    assert line.status is LineStatus.ASSIGNED
    assert ruled.gates["lines"]["a"]["machine"]["stage"] == "fixing"
    assert line.blockers[0] == "the new test fails on an empty cart"
    assert "filed as a follow-up" in line.blockers[1]
    assert ruled.gates[FOLLOW_UPS][-1]["findings"] == [
        "scope: this change also rewrites the billing module"
    ]
    await _drive(service, parked.id)
    after = await store.get(parked.id)
    assert launcher.calls[-1] == ("a", "deepseek")
    assert "empty cart" in launcher.tasks[-1][1]
    assert after.line("a").status is LineStatus.PASSED


async def test_in_scope_on_a_mixed_verdict_fixes_both(tmp_path: Path) -> None:
    store, _launcher, _reviewer, service, parked = await _awaiting_mixed(tmp_path)
    ruled = await service.adjudicate(parked.id, "a", "in-scope")
    assert ruled.line("a").blockers == [
        "scope: this change also rewrites the billing module",
        "the new test fails on an empty cart",
    ]
    assert FOLLOW_UPS not in ruled.gates


async def test_a_cancel_during_worktree_preparation_stops_the_spawn(tmp_path: Path) -> None:
    """Review fix (late launch): the launcher runs the epoch fence right before spawn."""
    from src.tools.auto_dispatch.gates import bump_epoch

    store = None

    class SlowPrepLauncher(ScriptedLauncher):
        def __init__(self) -> None:
            super().__init__({})
            self.spawned = 0

        async def launch_worker(self, line, model, skills, *, before_spawn=None, **kwargs):
            # Worktree preparation: an operator's cancel --force lands now.
            stored = store.rows["wedge"]
            bump_epoch(stored)
            assert before_spawn is not None
            await before_spawn()
            self.spawned += 1
            return await super().launch_worker(line, model, skills, **kwargs)

    launcher = SlowPrepLauncher()
    store, _launcher, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "deepseek")], launcher=launcher
    )
    machine = service._machine(row)
    await _drive(service, row.id)
    assert launcher.spawned == 0
    assert machine._abandoned


async def test_only_a_tagged_blocker_is_a_conflict_finding(tmp_path: Path) -> None:
    """Review fix 4: the word "scope" inside a real bug is not a conflict."""
    mixed = Verdict.blocked(
        [
            "scope: edits src/server/app.py which the task does not list",
            "Bug: the fixture is module-scoped and leaks state between tests",
        ]
    )
    reviewer = ScriptedReviewer({"a": [Verdict.ok("primary ok"), mixed]})
    store, launcher, _gates, service, row = await _setup(
        tmp_path,
        [_assigned("a", "deepseek", risk_bearing=True)],
        reviewer=reviewer,
    )
    await _drive(service, row.id)
    parked = await store.get(row.id)
    record = parked.gates["lines"]["a"][ADJUDICATION]
    assert record["findings"] == ["scope: edits src/server/app.py which the task does not list"]
    assert record["other_blockers"] == [
        "Bug: the fixture is module-scoped and leaks state between tests"
    ]
    ruled = await service.adjudicate(row.id, "a", "out-of-scope")
    # The module-scoped bug still has to be fixed: no pass.
    assert ruled.line("a").status is LineStatus.ASSIGNED
    assert ruled.gates["lines"]["a"]["machine"]["stage"] == "fixing"


def test_the_conflict_comes_only_from_the_tag() -> None:
    from src.tools.auto_dispatch.machine import _conflict_of, conflict_tag

    assert conflict_tag("scope: touches billing") == "scope"
    assert conflict_tag("[Security] token in a log") == "security"
    assert conflict_tag("the fixture is module-scoped") == ""
    assert conflict_tag("no security review was asked for") == ""
    assert _conflict_of(Verdict.blocked(["fixture is module-scoped"])) == ""
    assert _conflict_of(Verdict.blocked(["scope: x", "security: y"])) == "security"


async def test_other_blockers_past_the_cap_still_keep_the_block(tmp_path: Path) -> None:
    """Review fix 4: the "others remain" decision counts every blocker."""
    from src.tools.auto_dispatch.operator import _option, _other_count, RULINGS

    snap = {
        ADJUDICATION: {
            "conflict": "scope",
            "findings": ["scope: x"],
            "other_blockers": [],
            "other_blocker_count": 3,
        }
    }
    assert _other_count(snap) == 3
    assert _option("out-of-scope", _other_count(snap)) == RULINGS["in-scope"]

    many = Verdict.blocked(["scope: x", *[f"bug {n}" for n in range(30)]])
    reviewer = ScriptedReviewer({"a": [Verdict.ok("primary ok"), many]})
    store, _launcher, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "deepseek", risk_bearing=True)], reviewer=reviewer
    )
    await _drive(service, row.id)
    parked = await store.get(row.id)
    assert parked.gates["lines"]["a"][ADJUDICATION]["other_blocker_count"] == 30
