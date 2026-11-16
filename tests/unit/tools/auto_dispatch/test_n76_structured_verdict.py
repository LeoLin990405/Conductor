"""N76 L4 wiring: a structured review reply wins, prose is still parsed.

A structured-output CLI returns the verdict object itself (Codex's
``--output-schema`` file, StepFun's JSON final message). ``CrossReviewer``
validates that object against ``verdict.schema.json`` first and only then
falls back to the text parser that finds a JSON object in prose. The launch
admission helper counts lines waiting for review before starting a new one.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.tools.auto_dispatch import MachineConfig
from src.tools.auto_dispatch.execution import (
    CrossReviewer,
    decide_launch_admission,
    review_backlog_count,
)
from src.tools.auto_dispatch.liveness import record
from src.tools.auto_dispatch.review_admission import LAUNCH, QUEUE
from src.tools.auto_dispatch.types import DispatchState, Line, LineStatus

from .n76_fakes import (
    make_dispatch,
    make_gates,
    make_line,
    make_machine,
    park_line,
    stored,
)

pytestmark = pytest.mark.asyncio


class Backend:
    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def review(self, model: str, prompt: str):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def _reviewer(backend: Backend) -> CrossReviewer:
    return CrossReviewer(backend, lambda line_id: f"diff for {line_id}")


async def test_a_mapping_from_a_structured_adapter_is_used_directly() -> None:
    backend = Backend(
        {"passed": True, "blockers": [], "verdict": "looks good"}
    )

    verdict = await _reviewer(backend).review_line("line-0", "reviewer")

    assert verdict.passed is True
    assert verdict.verdict == "looks good"
    assert len(backend.prompts) == 1


async def test_a_schema_valid_json_string_wins_over_text_parsing() -> None:
    backend = Backend(
        '{"passed": false, "blockers": ["missing test"], "verdict": "blocked"}'
    )

    verdict = await _reviewer(backend).review_line("line-0", "reviewer")

    assert verdict.passed is False
    assert verdict.blockers == ["missing test"]
    assert len(backend.prompts) == 1


async def test_a_json_code_fence_is_still_structured_output() -> None:
    backend = Backend(
        '```json\n{"passed": true, "blockers": [], "verdict": "pass"}\n```'
    )

    verdict = await _reviewer(backend).review_line("line-0", "reviewer")

    assert verdict.passed is True
    assert len(backend.prompts) == 1


async def test_prose_still_falls_back_to_the_text_parser() -> None:
    backend = Backend(
        'I checked the diff.\n{"passed": false, "blockers": ["scope: touched auth"], '
        '"verdict": "blocked"}'
    )

    verdict = await _reviewer(backend).review_line("line-0", "reviewer")

    assert verdict.passed is False
    assert verdict.blockers == ["scope: touched auth"]


async def test_an_invalid_structured_reply_retries_and_then_accepts_json() -> None:
    backend = Backend(
        {"passed": "yes", "blockers": [], "verdict": "not a boolean"},
        '{"passed": true, "blockers": [], "verdict": "pass"}',
    )

    verdict = await _reviewer(backend).review_line("line-0", "reviewer")

    assert verdict.passed is True
    assert len(backend.prompts) == 2


async def test_both_invalid_replies_stay_unparseable() -> None:
    backend = Backend(
        {"passed": "yes", "blockers": [], "verdict": "bad"},
        {"passed": 1, "blockers": "none", "verdict": "still bad"},
    )

    verdict = await _reviewer(backend).review_line("line-0", "reviewer")

    assert verdict.passed is False
    assert verdict.blockers
    assert len(backend.prompts) == 2


class _Gates:
    def __init__(self, snapshots: dict[str, dict]) -> None:
        self._snapshots = snapshots

    def line_snapshot(self, dispatch, line):
        return self._snapshots.setdefault(line.id, {})


def _dispatch(*lines: Line):
    from src.tools.auto_dispatch.types import Dispatch

    return Dispatch(id="d1", goal="g", user_id="u", created_at="now", lines=list(lines))


async def test_review_backlog_counts_waiting_and_in_review_lines() -> None:
    review = Line(id="review", name="review", task="t", status=LineStatus.REVIEWING)
    running = Line(id="running", name="running", task="t", status=LineStatus.RUNNING)
    working = Line(id="working", name="working", task="t", status=LineStatus.RUNNING)
    parked = Line(id="parked", name="parked", task="t", status=LineStatus.BLOCKED)
    parked.failure_reason = "review_unavailable"
    gates = _Gates(
        {
            "running": {"machine": {"stage": "primary_review"}},
            "working": {"machine": {"stage": "implementing"}},
        }
    )
    dispatch = _dispatch(review, running, working, parked)

    assert review_backlog_count(dispatch, gates) == 3


async def test_launch_admission_queues_at_the_cap_and_launches_below_it() -> None:
    review = Line(id="review", name="review", task="t", status=LineStatus.REVIEWING)
    gates = _Gates({})
    dispatch = _dispatch(review)

    queued = decide_launch_admission(
        dispatch,
        primary_concurrency=1,
        gates=gates,
        env={"CONDUCTOR_DISPATCH_REVIEW_BACKLOG_MAX": "1"},
    )
    assert queued.decision == QUEUE
    assert queued.reason == "review_backlog"
    assert queued.limit == 1

    open_slot = decide_launch_admission(
        dispatch,
        primary_concurrency=2,
        gates=gates,
        env={},
    )
    assert open_slot.decision == LAUNCH
    assert open_slot.limit == 4


async def test_the_machine_holds_a_new_launch_behind_review_backlog(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CONDUCTOR_DISPATCH_REVIEW_BACKLOG_MAX", "1")
    gates = make_gates(tmp_path)
    machine, collaborators, recorder = make_machine(
        tmp_path,
        gates=gates,
        config=MachineConfig(
            liveness_mode="enforce", review_admission_wait_seconds=0
        ),
    )
    reviewing = make_line("line-0", status=LineStatus.REVIEWING)
    ready = make_line("line-1", status=LineStatus.ASSIGNED)
    dispatch = await stored(collaborators, make_dispatch(reviewing, ready))

    admitted = await machine._admit_launch(dispatch, ready)

    assert admitted is False
    assert ready.failure_reason == "review_backlog"
    assert "review_backlog" in (dispatch.message or "")
    assert recorder.waits == [0]


async def test_admission_is_a_no_op_outside_enforce(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CONDUCTOR_DISPATCH_REVIEW_BACKLOG_MAX", "1")
    for mode in ("off", "shadow"):
        gates = make_gates(tmp_path)
        machine, collaborators, recorder = make_machine(
            tmp_path,
            gates=gates,
            config=MachineConfig(
                liveness_mode=mode, review_admission_wait_seconds=0
            ),
        )
        reviewing = make_line("line-0", status=LineStatus.REVIEWING)
        ready = make_line("line-1", status=LineStatus.ASSIGNED)
        dispatch = await stored(collaborators, make_dispatch(reviewing, ready))

        assert await machine._admit_launch(dispatch, ready) is True
        assert ready.failure_reason is None
        assert recorder.waits == []


async def test_a_saturated_review_queue_parks_instead_of_looping(
    tmp_path, monkeypatch
) -> None:
    """A full queue is a recoverable park, never a bogus DAG error.

    Two review-parked lines hold the backlog cap. One independent line keeps
    trying to launch, is queued, and the old drive cleared its reason after
    every pause and tried again. That repeated until the loop guard raised
    ``The plan's line dependencies are not a DAG`` and turned a valid DAG into
    ERROR. A still-saturated queue must park in REVIEW and return.
    """
    monkeypatch.setenv("CONDUCTOR_DISPATCH_REVIEW_BACKLOG_MAX", "1")
    gates = make_gates(tmp_path)
    machine, collaborators, recorder = make_machine(
        tmp_path,
        gates=gates,
        config=MachineConfig(
            liveness_mode="enforce", review_admission_wait_seconds=0
        ),
    )
    parked = [make_line("line-0"), make_line("line-1")]
    ready = make_line("line-2", status=LineStatus.ASSIGNED)
    dispatch = await stored(collaborators, make_dispatch(*parked, ready))
    far = datetime(2099, 1, 1, tzinfo=timezone.utc)
    for line in parked:
        snapshot = park_line(gates, dispatch, line, "review_unavailable")
        waiting = record.new_record(
            "review_unavailable", "R-REVIEW", budget=4, at=far
        ).with_schedule(at=far, next_at=far, next_action="review")
        snapshot.update(record.store(snapshot, waiting))

    await machine._run_gated(dispatch)

    assert dispatch.state is DispatchState.REVIEW
    assert "line-2" in (dispatch.message or "")
    assert ready.failure_reason == "review_backlog"
    assert ready.status is LineStatus.ASSIGNED
    # One launch pause and one admission pause, then the recoverable park.
    assert 1 <= len(recorder.waits) <= 2
    assert [line.status for line in parked] == [
        LineStatus.BLOCKED,
        LineStatus.BLOCKED,
    ]
