"""F4: a reviewer adapter failure retries, and the third parks."""

from __future__ import annotations

import pytest

from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch.gates import REVIEW_UNAVAILABLE
from src.tools.auto_dispatch.types import Verdict
from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line

pytestmark = pytest.mark.asyncio


class FlakyReview:
    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def review_line(self, line_id: str, model: str) -> Verdict:
        del line_id, model
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("temporary review transport failure")
        return Verdict.ok("ok")


def _attempts(dispatch: Dispatch, line_id: str) -> int:
    lines = dispatch.gates.get("lines") if isinstance(dispatch.gates, dict) else None
    snap = lines.get(line_id) if isinstance(lines, dict) else None
    if not isinstance(snap, dict):
        return 0
    return int(snap.get("review_attempts") or 0)


async def test_f4_one_review_failure_then_success_passes(tmp_path) -> None:
    """N42: the drive retries the review itself; no wakeup is needed."""
    reviewer = FlakyReview(1)
    collaborators, machine, _commands, _gates = _bundle(
        tmp_path, [_line("l")], reviewer=reviewer
    )
    dispatch = Dispatch(
        id="review-once",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.SUBMITTED,
    )
    await collaborators.store.create(dispatch)
    first = await machine.run(dispatch)
    assert first.state is not DispatchState.ERROR
    assert reviewer.calls == 2
    assert _attempts(first, "l") == 0
    assert first.line("l") is not None
    assert first.line("l").status is LineStatus.PASSED
    assert first.line("l").failure_reason != REVIEW_UNAVAILABLE


async def test_f4_review_failures_past_the_budget_park_unavailable(tmp_path) -> None:
    reviewer = FlakyReview(99)
    collaborators, machine, _commands, _gates = _bundle(
        tmp_path, [_line("l")], reviewer=reviewer
    )
    dispatch = Dispatch(
        id="review-park",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.SUBMITTED,
    )
    await collaborators.store.create(dispatch)
    current = await machine.run(dispatch)
    assert current.state is not DispatchState.ERROR
    # The first review and one retry after each of the three waits.
    assert reviewer.calls == 4
    line = current.line("l")
    assert line is not None
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == REVIEW_UNAVAILABLE
    assert current.state is DispatchState.REVIEW
