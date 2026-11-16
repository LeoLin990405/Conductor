"""N42: a review whose reviewer cannot run is retried on the drive itself.

R8c line-0-r1-2 and F1 line-0 hit ``review_unavailable`` during a short codex
outage and stayed RUNNING with no worker for 90+ minutes: the retry waited for
a wakeup that never came because nothing else in the dispatch was active. The
drive now waits out 1, 5 and 15 minutes itself, asks the next eligible
reviewer after the second failure, and parks with a reason (resumed by
``dispatch unblock``) after the budget.
"""

from __future__ import annotations

import copy

import pytest

from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch.gates import REVIEW_UNAVAILABLE
from src.tools.auto_dispatch.machine import REVIEW_PARKED, REVIEW_RETRY_DELAYS
from src.tools.auto_dispatch.operator import apply_unblock
from src.tools.auto_dispatch.types import Verdict
from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line

pytestmark = pytest.mark.asyncio


class DownReviewer:
    """Fails for the named models (all of them when None) a number of times."""

    def __init__(self, fail_times: int, *, down: set[str] | None = None) -> None:
        self.fail_times = fail_times
        self.down = down
        self.calls: list[str] = []
        self.failures = 0

    async def review_line(self, line_id: str, model: str) -> Verdict:
        del line_id
        self.calls.append(model)
        if (self.down is None or model in self.down) and self.failures < self.fail_times:
            self.failures += 1
            raise RuntimeError("reviewer transport down")
        return Verdict.ok(f"ok from {model}")


async def _run(tmp_path, reviewer):
    collaborators, machine, _commands, _gates = _bundle(
        tmp_path, [_line("l")], reviewer=reviewer
    )
    waits: list[float] = []

    async def record(seconds: float) -> None:
        # The drive sleeps until retry_at: the delay less a few microseconds.
        waits.append(float(round(seconds)))

    machine._sleep = record
    dispatch = Dispatch(
        id="n42",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.SUBMITTED,
    )
    await collaborators.store.create(dispatch)
    result = await machine.run(dispatch)
    return collaborators, machine, result, waits


async def test_two_failures_then_success_with_nothing_else_active(tmp_path) -> None:
    reviewer = DownReviewer(2)
    _collab, _machine, result, waits = await _run(tmp_path, reviewer)
    line = result.line("l")
    assert line.status is LineStatus.PASSED
    assert len(reviewer.calls) == 3
    assert [w for w in waits if w in REVIEW_RETRY_DELAYS][:2] == [60.0, 300.0]
    assert result.state is not DispatchState.ERROR
    assert "review_attempts" not in result.gates["lines"]["l"]


async def test_the_second_failure_falls_back_to_another_reviewer(tmp_path) -> None:
    # Find the reviewer the drive picks first, then make that one be down.
    _collab, _machine, probe, _waits = await _run(tmp_path, DownReviewer(0))
    picked = probe.gates["lines"]["l"].get("primary_reviewer")
    assert picked
    reviewer = DownReviewer(99, down={picked})
    _collab, _machine, result, _waits = await _run(tmp_path / "again", reviewer)
    line = result.line("l")
    assert line.status is LineStatus.PASSED
    # Same reviewer for the first retry, another one from the second on.
    assert reviewer.calls[:2] == [picked, picked]
    assert reviewer.calls[2] != picked
    assert line.assigned_model not in reviewer.calls
    assert result.gates["lines"]["l"].get("primary_reviewer") == reviewer.calls[2]


async def test_the_budget_parks_the_line_and_unblock_resumes_the_review(tmp_path) -> None:
    reviewer = DownReviewer(99)
    collab, machine, result, waits = await _run(tmp_path, reviewer)
    line = result.line("l")
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == REVIEW_UNAVAILABLE
    assert len(reviewer.calls) == 1 + len(REVIEW_RETRY_DELAYS)
    assert "conductor dispatch unblock n42 l" in line.blockers[0]
    snap = result.gates["lines"]["l"]
    assert snap["machine"]["stage"] == "blocked"
    assert snap["machine"]["blocked_from"] == "primary_review"
    parked_run = snap[REVIEW_PARKED]["run_id"]
    assert parked_run
    assert result.state is DispatchState.REVIEW

    # The reviewer is back: unblock resumes the review on the finished run.
    stored = await collab.store.get("n42")
    assert apply_unblock(stored, stored.line("l"), note="codex is back") is None
    resumed = stored.line("l")
    assert resumed.status is LineStatus.RUNNING
    assert resumed.current_run == parked_run
    assert stored.gates["lines"]["l"]["machine"]["stage"] == "primary_review"
    await collab.store.save(stored)
    reviewer.fail_times = 0
    calls = len(reviewer.calls)
    done = await machine.run(stored)
    assert done.line("l").status is LineStatus.PASSED
    assert len(reviewer.calls) == calls + 1


async def test_unblock_leaves_the_line_parked_when_stage_gate_refuses_the_resume(
    tmp_path, monkeypatch
) -> None:
    """Review fix 5: the line goes RUNNING only after Stage-Gate accepts."""
    import src.tools.auto_dispatch.operator as operator
    from src.server.services.autonomy.stage_gate import Transition

    collab, _machine, result, _waits = await _run(tmp_path, DownReviewer(99))
    stored = await collab.store.get("n42")
    line = stored.line("l")
    assert line.failure_reason == REVIEW_UNAVAILABLE

    def refuse(state, event):
        return Transition(accepted=False, state=state, reason="stage_gate_order")

    monkeypatch.setattr(operator, "advance", refuse)
    assert apply_unblock(stored, line) == "stage_gate_order"
    assert line.status is LineStatus.BLOCKED
    assert line.current_run is None
    assert REVIEW_PARKED in stored.gates["lines"]["l"]


class LineDown:
    """Line "a"'s reviewer is down; every other line's review passes."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.calls.append(line_id)
        if line_id == "a":
            raise RuntimeError("down")
        return Verdict.ok("ok")


async def test_other_lines_keep_moving_while_a_review_retry_waits(tmp_path) -> None:
    """Review fix 2: a retry is scheduled on the line, not slept inline."""
    reviewer = LineDown()
    collab, machine, _c, _g = _bundle(tmp_path, [_line("a"), _line("b")], reviewer=reviewer)
    reviewed_b_at_wait: list[int] = []

    async def sleep(seconds: float) -> None:
        if round(seconds) in REVIEW_RETRY_DELAYS:
            reviewed_b_at_wait.append(reviewer.calls.count("b"))

    machine._sleep = sleep
    row = Dispatch(id="p", goal="g", user_id="u", created_at="now", state=DispatchState.SUBMITTED)
    await collab.store.create(row)
    result = await machine.run(row)
    assert result.line("b").status is LineStatus.PASSED
    assert result.line("a").failure_reason == REVIEW_UNAVAILABLE
    # b ran and was reviewed before the drive waited for a's first retry.
    assert reviewed_b_at_wait and all(n >= 1 for n in reviewed_b_at_wait)


async def test_a_scheduled_retry_survives_a_restart(tmp_path) -> None:
    """The failed reviewer and retry_at are persisted; a new drive honours them."""
    reviewer = DownReviewer(1)
    collab, machine, _c, gates = _bundle(tmp_path, [_line("l")], reviewer=reviewer)

    from src.tools.auto_dispatch.machine import StaleDrive

    async def restart(_dispatch, _waits) -> None:
        # The process goes away while it waits for the retry.
        raise StaleDrive

    machine._wait_for_review_retry = restart
    row = Dispatch(id="r", goal="g", user_id="u", created_at="now", state=DispatchState.SUBMITTED)
    await collab.store.create(row)
    await machine.run(row)
    stored = copy.deepcopy(await collab.store.get("r"))
    retry = stored.gates["lines"]["l"]["review_retry"]
    assert retry["attempt"] == 1
    assert retry["failed"] == [reviewer.calls[0]]
    assert retry["retry_at"] > 0

    # A new drive (after a restart) waits only for what is left, then retries
    # the same reviewer.
    _collab2, machine2, _c2, _g2 = _bundle(
        tmp_path / "second", [], reviewer=reviewer, store=collab.store, gates=gates
    )
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    machine2._sleep = sleep
    due = float(retry["retry_at"])
    machine2._wall = lambda: due - 10.0
    collab.store.rows["r"] = stored
    done = await machine2.run(stored)
    assert done.line("l").status is LineStatus.PASSED
    assert any(9.0 <= value <= 10.0 for value in slept)
    assert reviewer.calls[-1] == reviewer.calls[0]
    assert "review_retry" not in done.gates["lines"]["l"]


async def test_a_retry_after_the_epoch_moved_is_not_sent(tmp_path) -> None:
    from src.tools.auto_dispatch.gates import bump_epoch

    reviewer = DownReviewer(99)
    collab, machine, _c, _g = _bundle(tmp_path, [_line("l")], reviewer=reviewer)

    async def operator_acts(seconds: float) -> None:
        if round(seconds) in REVIEW_RETRY_DELAYS:
            # An operator acts on the row while the drive waits for the retry.
            stored = copy.deepcopy(collab.store.rows["n42e"])
            bump_epoch(stored)
            collab.store.rows["n42e"] = stored

    machine._sleep = operator_acts
    row = Dispatch(id="n42e", goal="g", user_id="u", created_at="now", state=DispatchState.SUBMITTED)
    await collab.store.create(row)
    await machine.run(row)
    assert len(reviewer.calls) == 1
    assert machine._abandoned
