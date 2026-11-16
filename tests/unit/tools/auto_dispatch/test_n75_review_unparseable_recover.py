"""N75: a line parked ``review_unparseable`` can be moved by the operator.

The reviewer's reply had no verdict object twice and nobody else was eligible
to review (d3205b52 line-0-r1-1, c18fb039 line-0-r1-5). The line parked
BLOCKED, but Stage-Gate was left at the review stage with no park record, so
``unblock`` said ``line_not_wedged`` and ``reassign`` said ``stage_gate_order``.

The park now writes the same ``review_parked`` record and Stage-Gate pause as
a ``review_unavailable`` park (N42): ``unblock`` reviews the same work again,
and ``reassign`` is a legal swap that carries the findings. Rows parked before
the fix (no record, Stage-Gate at the review stage) are handled too.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.server.services.autonomy.stage_gate import Stage
from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch.dev import RoundRobinSelector
from src.tools.auto_dispatch.gates import REVIEW_UNPARSEABLE
from src.tools.auto_dispatch.machine import (
    REVIEW_PARKED,
    _load_state,
    _store_state,
    apply_human_reassign,
    reassign_brief,
)
from src.tools.auto_dispatch.operator import (
    IN_SCOPE,
    adjudication_refusal,
    apply_unblock,
    unblock_refusal,
)

from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle as _machine_bundle
from tests.unit.tools.auto_dispatch.test_machine_gates import _line as _machine_line
from tests.unit.tools.auto_dispatch.test_machine_gates import _repo_with_lines
from tests.unit.tools.auto_dispatch.test_review_unparseable import (
    NOT_LAST,
    PASS,
    PROSE_AFTER,
    ReplyReviewer,
)
from tests.unit.tools.auto_dispatch.test_w3b_fix1 import _settings

pytestmark = pytest.mark.asyncio


async def _parked(tmp_path: Path):
    """One line by kimi; codex is the only other model and replies unusably."""
    repo = _repo_with_lines(tmp_path / "repo", "a")
    reviewer = ReplyReviewer(PROSE_AFTER)
    collaborators, machine, _commands, _trees = _machine_bundle(
        tmp_path,
        [_machine_line("a", assigned_model="kimi")],
        reviewer=reviewer,
        settings=_settings(tmp_path, repo),
        selector=RoundRobinSelector(("kimi", "codex")),
    )
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    parked = await machine.run(dispatch)
    line = parked.line("a")
    assert line is not None
    assert parked.state is DispatchState.REVIEW
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == REVIEW_UNPARSEABLE
    return collaborators, machine, reviewer, parked


def _as_legacy(dispatch: Dispatch) -> None:
    """Rewrite the park as the code before N75 left it: no record, no pause."""
    snap = dispatch.gates["lines"]["a"]
    snap.pop(REVIEW_PARKED, None)
    state = _load_state(snap)
    _store_state(snap, replace(state, stage=Stage(state.blocked_from), blocked_from=""))


async def test_the_park_records_a_review_pause_and_the_run_under_review(tmp_path) -> None:
    _collab, _machine, _reviewer, parked = await _parked(tmp_path)
    line = parked.line("a")
    snap = parked.gates["lines"]["a"]
    state = _load_state(snap)
    assert state.stage is Stage.BLOCKED
    assert state.blocked_from == Stage.PRIMARY_REVIEW.value
    record = snap[REVIEW_PARKED]
    assert record["run_id"]
    assert record["attempt"] == line.attempts
    assert record["author"] == "kimi"
    assert record["failed"] == ["codex"]
    # The findings are the reviewer's format, kept for the operator; no fix round.
    assert line.blockers == [NOT_LAST]
    assert "pending_fix" not in snap
    assert snap["machine"]["fix_rounds"] == 0


async def test_unblock_reviews_the_same_work_again(tmp_path) -> None:
    collab, machine, reviewer, parked = await _parked(tmp_path)
    parked_run = parked.gates["lines"]["a"][REVIEW_PARKED]["run_id"]

    stored = await collab.store.get("d1")
    line = stored.line("a")
    assert unblock_refusal(stored, line) is None
    assert apply_unblock(stored, line, note="codex answers now") is None
    assert line.status is LineStatus.RUNNING
    assert line.current_run == parked_run
    assert line.failure_reason is None
    assert line.blockers == []
    snap = stored.gates["lines"]["a"]
    assert _load_state(snap).stage is Stage.PRIMARY_REVIEW
    assert REVIEW_PARKED not in snap
    audit = stored.gates["scheduler"]["audit"][-1]
    assert audit["kind"] == "operator_unblock"
    assert audit["failure_reason"] == REVIEW_UNPARSEABLE

    await collab.store.save(stored)
    reviewer.replies = [PASS]
    calls = len(reviewer.calls)
    done = await machine.run(stored)
    assert done.line("a").status is LineStatus.PASSED
    assert len(reviewer.calls) == calls + 1
    # The author was launched once: the same work was reviewed, never fixed.
    assert [model for _l, model, _s in collab.launcher.launches] == ["kimi"]


async def test_reassign_is_accepted_and_carries_the_findings(tmp_path) -> None:
    collab, _machine, _reviewer, parked = await _parked(tmp_path)
    stored = await collab.store.get("d1")
    line = stored.line("a")
    attempts = line.attempts

    assert await apply_human_reassign(stored, line, "codex", None, collab.gates) is None
    assert line.status is LineStatus.ASSIGNED
    assert line.assigned_model == "codex"
    assert line.attempts == attempts + 1
    assert _load_state(stored.gates["lines"]["a"]).stage is Stage.ASSIGNED
    brief = reassign_brief(stored, line)
    assert brief is not None
    assert NOT_LAST in brief
    assert any(REVIEW_UNPARSEABLE in item for item in brief)


async def test_a_park_from_before_the_fix_unblocks_too(tmp_path) -> None:
    collab, machine, reviewer, _parked_row = await _parked(tmp_path)
    stored = await collab.store.get("d1")
    _as_legacy(stored)
    line = stored.line("a")
    assert _load_state(stored.gates["lines"]["a"]).stage is Stage.PRIMARY_REVIEW

    assert apply_unblock(stored, line) is None
    assert line.status is LineStatus.RUNNING
    assert line.current_run
    assert stored.gates["scheduler"]["audit"][-1]["legacy"] is True

    await collab.store.save(stored)
    reviewer.replies = [PASS]
    done = await machine.run(stored)
    assert done.line("a").status is LineStatus.PASSED


async def test_a_park_from_before_the_fix_reassigns_too(tmp_path) -> None:
    collab, _machine, _reviewer, _parked_row = await _parked(tmp_path)
    stored = await collab.store.get("d1")
    _as_legacy(stored)
    line = stored.line("a")

    assert await apply_human_reassign(stored, line, "codex", None, collab.gates) is None
    snap = stored.gates["lines"]["a"]
    assert _load_state(snap).stage is Stage.ASSIGNED
    moves = [
        (event.get("from_stage"), event.get("to_stage"), event.get("reason"))
        for event in snap.get("events") or []
    ]
    # The missing pause is recorded first, then the swap from it.
    assert moves[-2:] == [
        ("primary_review", "blocked", "operator_blocked"),
        ("blocked", "assigned", "developer_reassigned"),
    ]
    assert NOT_LAST in (reassign_brief(stored, line) or [])


async def test_other_refusals_are_unchanged(tmp_path) -> None:
    collab, _machine, _reviewer, _parked_row = await _parked(tmp_path)
    stored = await collab.store.get("d1")
    line = stored.line("a")

    # No adjudication is pending: the park is not a conflict.
    assert adjudication_refusal(stored, line, IN_SCOPE) == "not_awaiting_adjudication"
    # A run out still refuses the unblock.
    line.current_run = "someone-else"
    assert unblock_refusal(stored, line) == "run_out"
    line.current_run = None
    # Another author's attempt does not match the park record, and there is
    # no settled run to fall back on: refused as for ``review_unavailable``.
    line.assigned_model = "glm"
    stored.gates["scheduler"]["settled_lines"] = []
    assert unblock_refusal(stored, line) == "no_settled_run"
    line.assigned_model = "kimi"

    # A review-stage line parked for another reason is still not wedged, and
    # Stage-Gate still refuses to reassign it mid-review.
    _as_legacy(stored)
    line.failure_reason = "something_else"
    assert unblock_refusal(stored, line) == "line_not_wedged"
    assert (
        await apply_human_reassign(stored, line, "codex", None, collab.gates)
        == "stage_gate_order"
    )
    assert line.status is LineStatus.BLOCKED
    assert line.assigned_model == "kimi"


async def test_a_legacy_park_with_no_settled_run_is_not_resumed(tmp_path) -> None:
    collab, _machine, _reviewer, _parked_row = await _parked(tmp_path)
    stored = await collab.store.get("d1")
    _as_legacy(stored)
    stored.gates["scheduler"]["settled_lines"] = []
    assert unblock_refusal(stored, stored.line("a")) == "no_settled_run"
