"""N87: ``dispatch recover <id> <line> --rereview`` for a replan-exhausted line.

N80 line-0 parked ``replan_exhausted`` twice; ``unblock`` answered
``line_not_wedged`` and ``recover`` only offered ``--split``/``--human``, so
there was no way to say "the operator fixed the branch, run the gates and the
review again". ``recover --rereview`` now does exactly that: Stage-Gate is
walked to G2 through legal events, the G2 result is reset, and the line is
RUNNING on a settled synthetic run that the drive adopts, so G2 runs on the
branch head and then the review, with no Worker launched.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.server.services.autonomy.stage_gate import REPLAN_OPTIONS, Stage
from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch import operator
from src.tools.auto_dispatch.gates import REPLAN_EXHAUSTED
from src.tools.auto_dispatch.machine import (
    _load_state,
    _store_state,
    human_reassign_request_refusal,
    human_request,
)
from src.tools.auto_dispatch.types import Verdict
from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line

pytestmark = pytest.mark.asyncio


class FlakyPlanner:
    """Plans once; every replan raises, so the line parks replan_exhausted."""

    def __init__(self, lines) -> None:
        self._lines = lines
        self.calls = 0

    async def plan(self, goal: str):
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("planner down")
        return self._lines


class SwitchReviewer:
    def __init__(self) -> None:
        self.passing = False
        self.calls: list[str] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.calls.append(line_id)
        if self.passing:
            return Verdict.ok("fixed by the operator")
        return Verdict.blocked(["the change is wrong"])


async def _exhausted(tmp_path):
    """A real drive parks the line on its review; the row then reads as the
    N80 park did: Stage-Gate in ``replanning`` (both options offered, as after
    two failed rounds) and the line BLOCKED ``replan_exhausted``."""
    lines = [_line("l")]
    reviewer = SwitchReviewer()
    collaborators, machine, commands, gates = _bundle(
        tmp_path, lines, reviewer=reviewer, planner=FlakyPlanner(lines)
    )
    dispatch = Dispatch(
        id="n87", goal="test", user_id="u", created_at="now", state=DispatchState.SUBMITTED
    )
    await collaborators.store.create(dispatch)
    result = await machine.run(dispatch)
    line = result.line("l")
    snap = result.gates["lines"]["l"]
    state = _load_state(snap)
    _store_state(
        snap,
        replace(
            state,
            stage=Stage.REPLANNING,
            blocked_from="",
            replan_options=REPLAN_OPTIONS,
            developer_provider=line.assigned_model or "codex",
        ),
    )
    line.status = LineStatus.BLOCKED
    line.failure_reason = REPLAN_EXHAUSTED
    line.current_run = None
    line.blockers = ["replan exhausted"]
    snap["reason"] = REPLAN_EXHAUSTED
    result.state = DispatchState.REVIEW
    await collaborators.store.save(result)
    result = await collaborators.store.get("n87")
    return collaborators, machine, commands, reviewer, result


async def test_a_replan_exhausted_line_has_no_other_operator_path(tmp_path) -> None:
    _c, _m, _cmd, _r, result = await _exhausted(tmp_path)
    line = result.line("l")
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == REPLAN_EXHAUSTED
    # The gap N87 closes: unblock refuses it.
    assert operator.unblock_refusal(result, line) == "line_not_wedged"
    assert operator.rereview_refusal(result, line) is None


async def test_rereview_reruns_g2_and_the_review_on_the_branch_head(tmp_path) -> None:
    collab, machine, commands, reviewer, result = await _exhausted(tmp_path)
    stored = await collab.store.get("n87")
    line = stored.line("l")
    launches_before = len(collab.launcher.launches)
    g2_before = len(commands.calls)
    reviews_before = len(reviewer.calls)

    assert operator.apply_recover(stored, line, "rereview", note="fixed by hand") is None

    assert line.status is LineStatus.RUNNING
    assert line.failure_reason is None
    assert line.blockers == []
    assert ":rereview-" in (line.current_run or "")
    snap = stored.gates["lines"]["l"]
    assert _load_state(snap).stage.value == "focused_tests"
    assert snap["gates"]["G2"]["result"] == "pending"
    audit = stored.gates["scheduler"]["audit"]
    assert audit[-1]["kind"] == "operator_rereview"
    assert audit[-1]["failure_reason"] == REPLAN_EXHAUSTED

    await collab.store.save(stored)
    reviewer.passing = True
    done = await machine.run(stored)

    assert done.line("l").status is LineStatus.PASSED
    # G2 ran again on the head, the review ran again, no Worker was launched.
    assert len(commands.calls) > g2_before
    assert len(reviewer.calls) == reviews_before + 1
    assert len(collab.launcher.launches) == launches_before


async def test_a_refused_rereview_leaves_the_line_parked(tmp_path) -> None:
    _c, _m, _cmd, _r, result = await _exhausted(tmp_path)
    line = result.line("l")
    line.current_run = "still-out"
    assert operator.apply_recover(result, line, "rereview") == "run_out"
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == REPLAN_EXHAUSTED


async def test_rereview_refuses_a_line_that_is_not_parked_or_has_no_branch(tmp_path) -> None:
    _c, _m, _cmd, _r, result = await _exhausted(tmp_path)
    line = result.line("l")
    branch = line.branch
    line.branch = ""
    assert operator.rereview_refusal(result, line) == "no_branch"
    line.branch = branch
    line.status = LineStatus.PASSED
    assert operator.rereview_refusal(result, line) == "line_not_parked"


async def test_rereview_refuses_a_replan_that_only_offered_a_resplit(tmp_path) -> None:
    _c, _m, _cmd, _r, result = await _exhausted(tmp_path)
    line = result.line("l")
    snap = result.gates["lines"]["l"]
    _store_state(snap, replace(_load_state(snap), replan_options=("resplit_task",)))
    assert operator.rereview_refusal(result, line) == "rereview_not_offered"


async def test_the_mode_is_known_and_the_queue_carries_it(tmp_path) -> None:
    assert operator.normalise_recover_mode("rereview") == "rereview"
    _c, _m, _cmd, _r, result = await _exhausted(tmp_path)
    line = result.line("l")
    request = human_request(line, {"kind": "recover", "mode": "rereview", "note": ""})
    assert human_reassign_request_refusal(result, line, request) is None
