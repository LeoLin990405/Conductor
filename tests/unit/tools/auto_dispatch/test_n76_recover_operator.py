"""N76 L3: ``dispatch recover`` runs a parked line's next recovery step now.

``operator.apply_recover`` is a pure row write (a marker on the line's gate
cell, or the person latch for ``--human``); the drive's sweep performs the
step in every liveness mode, skipping its backoff. These tests drive the
operator write and the machine's request sweep directly with an injected clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.server.services.autonomy.stage_gate import Stage
from src.tools.auto_dispatch import MachineConfig
from src.tools.auto_dispatch import operator
from src.tools.auto_dispatch.liveness import modes, policy, record
from src.tools.auto_dispatch.machine import (
    _load_state,
    _snapshot_stage,
    _store_state,
    human_reassign_request_refusal,
    human_request,
)
from src.tools.auto_dispatch.types import DispatchState, LineStatus

from .n76_fakes import make_dispatch, make_gates, make_line, make_machine, park_line, stored
from .test_n76_reason_matrix import EXPECTED_GROUP, SECTION_3

pytestmark = pytest.mark.asyncio

WHEN = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


async def _parked(tmp_path, reason="no_new_commit", *, mode=modes.SHADOW, sibling=LineStatus.PASSED):
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=mode)
    )
    line = make_line("line-0", assigned_model="codex")
    other = make_line("line-1", assigned_model="kimi")
    other.status = sibling
    dispatch = await stored(collaborators, make_dispatch(line, other))
    dispatch.state = DispatchState.REVIEW
    cell = park_line(gates, dispatch, line, reason)
    if reason in ("review_unavailable", "reviewers_quota_exhausted", "review_unparseable"):
        from dataclasses import replace

        _store_state(cell, replace(_load_state(cell), blocked_from=Stage.PRIMARY_REVIEW.value))
    return machine, gates, dispatch, line


# -- refusals -------------------------------------------------------------------


async def test_recover_refuses_an_ungated_line(tmp_path) -> None:
    line = make_line()
    line.status = LineStatus.BLOCKED
    assert operator.recover_refusal(make_dispatch(line), line) == "not_gated"


async def test_recover_refuses_a_line_that_is_not_parked(tmp_path) -> None:
    machine, gates, dispatch, line = await _parked(tmp_path)
    line.status = LineStatus.RUNNING
    assert operator.recover_refusal(dispatch, line) == "line_not_parked"


async def test_recover_refuses_a_line_with_a_run_out(tmp_path) -> None:
    machine, gates, dispatch, line = await _parked(tmp_path)
    line.current_run = "run-line-0-1"
    assert operator.recover_refusal(dispatch, line) == "run_out"


async def test_recover_refuses_a_split_line_and_a_bad_mode(tmp_path) -> None:
    machine, gates, dispatch, line = await _parked(tmp_path)
    assert operator.recover_refusal(dispatch, line, "sideways") == "invalid_mode"
    line.status = LineStatus.FAILED
    line.failure_reason = "split_off"
    assert operator.recover_refusal(dispatch, line) == "already_split"


async def test_a_second_request_waits_for_the_first(tmp_path) -> None:
    machine, gates, dispatch, line = await _parked(tmp_path)
    assert operator.apply_recover(dispatch, line) is None
    assert operator.recover_refusal(dispatch, line) == "recover_pending"


@pytest.mark.parametrize(
    "reason", [reason for reason in SECTION_3 if EXPECTED_GROUP[reason] == policy.HUMAN]
)
async def test_a_reason_a_person_owns_answers_needs_human(tmp_path, reason) -> None:
    machine, gates, dispatch, line = await _parked(tmp_path, reason)
    assert operator.recover_refusal(dispatch, line) == "needs_human"
    assert operator.recover_plan(dispatch, line)["next_step"] == "human"
    # --human and --split are still open to the operator.
    assert operator.recover_refusal(dispatch, line, "human") is None
    assert operator.recover_refusal(dispatch, line, "split") is None


async def test_a_spent_budget_answers_needs_human(tmp_path) -> None:
    machine, gates, dispatch, line = await _parked(tmp_path)
    cell = gates.line_snapshot(dispatch, line)
    spent = record.new_record("no_new_commit", policy.R_EXIT, budget=1, at=WHEN)
    cell.update(record.store(cell, spent.with_step("reassign", at=WHEN, result=record.APPLIED)))
    assert operator.recover_plan(dispatch, line)["budget_left"] == 0
    assert operator.recover_refusal(dispatch, line) == "needs_human"


async def test_a_split_that_would_strand_running_lines_waits(tmp_path) -> None:
    machine, gates, dispatch, line = await _parked(tmp_path, sibling=LineStatus.RUNNING)
    assert operator.recover_refusal(dispatch, line, "split") == "split_waits"


async def test_the_queue_carries_recover_with_the_same_checks(tmp_path) -> None:
    machine, gates, dispatch, line = await _parked(tmp_path)
    request = human_request(line, {"kind": "recover", "mode": "next", "note": ""})
    assert human_reassign_request_refusal(dispatch, line, request) is None
    line.attempts += 1
    assert human_reassign_request_refusal(dispatch, line, request) == "line_moved"


# -- next ----------------------------------------------------------------------


@pytest.mark.parametrize("mode", [modes.OFF, modes.SHADOW, modes.ENFORCE])
async def test_recover_runs_the_next_step_now_in_every_mode(tmp_path, mode) -> None:
    machine, gates, dispatch, line = await _parked(tmp_path, mode=mode)
    assert operator.apply_recover(dispatch, line, note="go") is None
    assert isinstance(gates.line_snapshot(dispatch, line)[operator.RECOVER_MARKER], dict)

    await machine._sweep_liveness(dispatch, now=WHEN)

    assert line.status is LineStatus.ASSIGNED
    assert line.assigned_model not in (None, "codex")
    cell = gates.line_snapshot(dispatch, line)
    assert operator.RECOVER_MARKER not in cell
    done = record.load(cell)
    assert done is not None
    applied = [step for step in done.history if step.result == record.APPLIED]
    assert [step.action for step in applied] == ["reassign"]
    assert applied[0].detail.startswith("operator recover")
    assert done.spent == 1
    audit = dispatch.gates["scheduler"]["audit"]
    assert any(item.get("kind") == "operator_recover" for item in audit)


async def test_recover_skips_the_review_backoff(tmp_path) -> None:
    machine, gates, dispatch, line = await _parked(tmp_path, "review_unavailable", mode=modes.ENFORCE)
    # The sweep schedules R-REVIEW's first row 60s out and does not act now.
    await machine._sweep_liveness(dispatch, now=WHEN)
    waiting = record.load(gates.line_snapshot(dispatch, line))
    assert waiting is not None and waiting.next_at is not None
    assert record.parse_time(waiting.next_at) > WHEN
    assert line.status is LineStatus.BLOCKED

    assert operator.recover_plan(dispatch, line)["next_step"] == "review"
    assert operator.apply_recover(dispatch, line) is None
    await machine._sweep_liveness(dispatch, now=WHEN + timedelta(seconds=1))

    assert line.status is LineStatus.RUNNING
    assert _snapshot_stage(gates.line_snapshot(dispatch, line)) is Stage.PRIMARY_REVIEW


async def test_recover_skips_the_quota_wait(tmp_path) -> None:
    machine, gates, dispatch, line = await _parked(
        tmp_path, "reviewers_quota_exhausted", mode=modes.ENFORCE
    )
    assert operator.recover_plan(dispatch, line)["next_step"] == "wait"
    assert operator.apply_recover(dispatch, line) is None
    await machine._sweep_operator_requests(dispatch, now=WHEN)
    assert line.status is LineStatus.RUNNING
    done = record.load(gates.line_snapshot(dispatch, line))
    assert done is not None and done.history[-1].action == "review"


@pytest.mark.parametrize("reason", SECTION_3)
async def test_every_section_3_park_gets_a_step_or_a_clear_needs_human(tmp_path, reason) -> None:
    """Acceptance 7: recover executes the next step, or says a person is needed."""
    machine, gates, dispatch, line = await _parked(tmp_path, reason, mode=modes.SHADOW)
    refused = operator.apply_recover(dispatch, line)
    if EXPECTED_GROUP[reason] == policy.HUMAN:
        assert refused == "needs_human", reason
        return
    assert refused is None, (reason, refused)
    await machine._sweep_liveness(dispatch, now=WHEN)
    cell = gates.line_snapshot(dispatch, line)
    assert operator.RECOVER_MARKER not in cell
    done = record.load(cell)
    assert done is not None, reason
    operator_steps = [
        step for step in done.history if step.detail.startswith("operator recover")
    ]
    assert operator_steps, (reason, done.history)
    assert operator_steps[-1].result in (record.APPLIED, record.REFUSED), reason


# -- split ---------------------------------------------------------------------


async def test_recover_split_removes_the_line_and_writes_its_follow_up(tmp_path) -> None:
    machine, gates, dispatch, line = await _parked(tmp_path, "review_unavailable", mode=modes.OFF)
    assert operator.apply_recover(dispatch, line, "split") is None
    await machine._sweep_liveness(dispatch, now=WHEN)
    assert line.status is LineStatus.FAILED
    assert line.failure_reason == "split_off"
    assert (tmp_path / "followups" / "d1-line-0.md").exists()
    assert "line-0" in dispatch.outcomes["split_off"]
    done = record.load(gates.line_snapshot(dispatch, line))
    assert done is not None and done.outcome == record.SPLIT_OFF


async def test_recover_split_with_nothing_passed_hands_the_round_to_a_person(tmp_path) -> None:
    machine, gates, dispatch, line = await _parked(
        tmp_path, mode=modes.ENFORCE, sibling=LineStatus.BLOCKED
    )
    dispatch.lines[1].failure_reason = "no_new_commit"
    assert operator.apply_recover(dispatch, line, "split") is None
    await machine._sweep_operator_requests(dispatch, now=WHEN)
    assert line.status is LineStatus.BLOCKED
    latched = record.load(gates.line_snapshot(dispatch, line))
    assert latched is not None and latched.needs_human
    assert "R-SPLIT refused" in (dispatch.message or "")


# -- human ---------------------------------------------------------------------


async def test_recover_human_latches_a_person_and_stops_automatic_steps(tmp_path) -> None:
    machine, gates, dispatch, line = await _parked(tmp_path, mode=modes.ENFORCE)
    assert operator.apply_recover(dispatch, line, "human", note="mine") is None
    latched = record.load(gates.line_snapshot(dispatch, line))
    assert latched is not None and latched.needs_human
    assert latched.human_reason == operator.OPERATOR_HUMAN_REASON
    assert latched.notified_at  # the operator is the person; nobody is paged
    assert operator.recover_refusal(dispatch, line, "human") == "already_needs_human"

    for minutes in (0, 10, 60):
        await machine._sweep_liveness(dispatch, now=WHEN + timedelta(minutes=minutes))
    assert line.status is LineStatus.BLOCKED
    assert line.assigned_model == "codex"
    assert not (tmp_path / "inbox" / "needs-human.md").exists()
    view = operator.liveness_status(dispatch, now=WHEN)
    assert view["needs_human"] == ["line-0"]
    assert view["lines"]["line-0"]["owner"] == "needs_human"


# -- status view ---------------------------------------------------------------


async def test_status_names_owner_next_step_next_at_and_budget(tmp_path) -> None:
    machine, gates, dispatch, line = await _parked(tmp_path, "review_unavailable", mode=modes.ENFORCE)
    await machine._sweep_liveness(dispatch, now=WHEN)
    view = operator.liveness_status(dispatch, now=WHEN)
    entry = view["lines"]["line-0"]
    assert entry["owner"] == "recovery_timer"
    assert entry["next_step"] == "review"
    assert entry["next_at"]
    assert entry["budget_left"] == policy.DEFAULT_MAX_STEPS
    assert entry["needs_human"] is False
    assert "line-1" not in view["lines"]  # PASSED lines need no owner
    assert view["needs_human"] == []


async def test_next_whose_row_is_a_waiting_split_says_split_waits(tmp_path) -> None:
    """Review finding: ``next`` on an R-EXIT line whose two reassigns are spent
    was accepted and then silently rescheduled while a sibling still ran."""
    machine, gates, dispatch, line = await _parked(
        tmp_path, mode=modes.ENFORCE, sibling=LineStatus.RUNNING
    )
    cell = gates.line_snapshot(dispatch, line)
    spent = record.new_record("no_new_commit", policy.R_EXIT, budget=4, at=WHEN)
    for _ in range(2):
        spent = spent.with_step("reassign", at=WHEN, result=record.APPLIED)
    cell.update(record.store(cell, spent))
    assert operator.recover_plan(dispatch, line)["next_step"] == "split"
    assert operator.recover_refusal(dispatch, line) == "split_waits"
    assert operator.apply_recover(dispatch, line) == "split_waits"


async def test_a_person_who_acted_hands_the_line_back_to_the_sweep(tmp_path) -> None:
    """Review finding: the needs_human latch carried into every later park, so a
    line a person had fixed was never recovered or paged again."""
    from src.tools.auto_dispatch.machine import release_human_latch

    machine, gates, dispatch, line = await _parked(tmp_path, "handoff_incomplete", mode=modes.ENFORCE)
    await machine._sweep_liveness(dispatch, now=WHEN)
    inbox = tmp_path / "inbox" / "needs-human.md"
    assert record.load(gates.line_snapshot(dispatch, line)).needs_human
    assert inbox.read_text(encoding="utf-8").count("## d1 / line-0") == 1

    assert release_human_latch(dispatch, line) is True
    released = record.load(gates.line_snapshot(dispatch, line))
    assert not released.needs_human and released.outcome == record.RECOVERED
    assert release_human_latch(dispatch, line) is False

    park_line(gates, dispatch, line, "no_new_commit")
    await machine._sweep_liveness(dispatch, now=WHEN + timedelta(hours=2))
    # A new R-EXIT episode: its first row is scheduled, then taken.
    await machine._sweep_liveness(dispatch, now=WHEN + timedelta(hours=3))
    assert line.status is LineStatus.ASSIGNED  # the sweep owns it again
    again = record.load(gates.line_snapshot(dispatch, line))
    assert again.reason == "no_new_commit" and again.outcome == record.RECOVERED

    # A later hand-off to a person pages again: it is a new episode.
    park_line(gates, dispatch, line, "handoff_incomplete")
    await machine._sweep_liveness(dispatch, now=WHEN + timedelta(hours=4))
    await machine._sweep_liveness(dispatch, now=WHEN + timedelta(hours=5))
    assert record.load(gates.line_snapshot(dispatch, line)).needs_human
    assert inbox.read_text(encoding="utf-8").count("## d1 / line-0") == 2


async def test_an_applied_human_decision_releases_the_latch(tmp_path, monkeypatch) -> None:
    from src.tools.auto_dispatch import machine as machine_module

    machine, gates, dispatch, line = await _parked(tmp_path, "handoff_incomplete", mode=modes.ENFORCE)
    await machine._sweep_liveness(dispatch, now=WHEN)
    assert record.load(gates.line_snapshot(dispatch, line)).needs_human
    monkeypatch.setattr(operator, "apply_decision", lambda *_args, **_kw: None)
    request = human_request(line, {"kind": "unblock", "note": ""})
    assert await machine_module.apply_human_request(dispatch, line, request, gates) is None
    assert not record.load(gates.line_snapshot(dispatch, line)).needs_human

    # A refused decision, or a recover request, leaves the person in charge.
    _, gates2, dispatch2, line2 = await _parked(tmp_path / "b", "handoff_incomplete", mode=modes.ENFORCE)
    await machine._sweep_liveness(dispatch2, now=WHEN)
    monkeypatch.setattr(operator, "apply_decision", lambda *_args, **_kw: "line_not_wedged")
    assert await machine_module.apply_human_request(dispatch2, line2, request, gates2) == "line_not_wedged"
