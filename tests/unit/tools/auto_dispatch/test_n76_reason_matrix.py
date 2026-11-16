"""N76 acceptance 2: every park reason of PRD §3, one row each.

For each reason the line is parked the way its ``_block_line`` call site leaves
it (BLOCKED, Stage-Gate ``blocked``), with an independent PASSED sibling so an
R-SPLIT has something left to integrate. Then:

* after the sweep the line has an owner and passes the consistency table;
* ``shadow`` records the group's first row without spending budget;
* ``enforce`` takes that first row (once its backoff is due);
* a spent budget hands the line to a person exactly once.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.tools.auto_dispatch import MachineConfig
from src.tools.auto_dispatch.liveness import consistency, modes, owner, policy, record
from src.server.services.autonomy.stage_gate import Stage
from src.tools.auto_dispatch.machine import _load_state, _snapshot_stage, _store_state
from src.tools.auto_dispatch.types import DispatchState, LineStatus

from .n76_fakes import make_dispatch, make_gates, make_line, make_machine, park_line, stored

pytestmark = pytest.mark.asyncio

WHEN = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
LATER = WHEN + timedelta(hours=25)

#: PRD §3, every reason named in the table (the G5 row expands to its codes).
SECTION_3 = (
    "no_new_commit",
    "uncommitted_changes",
    "review_unavailable",
    "review_unparseable",
    "reviewers_quota_exhausted",
    "no_eligible_reviewer",
    "replan_parked",
    "replan_uncombined",
    "replan_base_unresolved",
    "replan_exhausted",
    "handoff_incomplete",
    "g2_no_commands_risk_bearing",
    "g2_revision_moved",
    "focused_tests_exhausted",
    "fix_launch_failed",
    "reviewer_conflict_scope",
    "reviewer_conflict_security",
    "watchdog_timeout",
    "author_quota_exhausted",
    "g5_not_configured",
    "g5_full_suite_failed",
    "no_secondary_reviewer",
    "no_eligible_worker",
    "git_evidence_unavailable",
)

#: The group the PRD assigns (§3 last column, §4.4 for the ex-ERROR reasons).
EXPECTED_GROUP = {
    "no_new_commit": policy.R_EXIT,
    "uncommitted_changes": policy.R_EXIT,
    "watchdog_timeout": policy.R_EXIT,
    "g2_no_commands_risk_bearing": policy.R_EXIT,
    "g2_revision_moved": policy.R_EXIT,
    "focused_tests_exhausted": policy.R_EXIT,
    "fix_launch_failed": policy.R_EXIT,
    "review_unavailable": policy.R_REVIEW,
    "review_unparseable": policy.R_REVIEW,
    "no_eligible_reviewer": policy.R_REVIEW,
    "reviewers_quota_exhausted": policy.R_QUOTA,
    "no_secondary_reviewer": policy.R_QUOTA,
    "no_eligible_worker": policy.R_QUOTA,
    "replan_parked": policy.R_REPLAN,
    "replan_uncombined": policy.R_REPLAN,
    "replan_base_unresolved": policy.R_REPLAN,
    "replan_exhausted": policy.R_REPLAN,
    "git_evidence_unavailable": policy.R_SPLIT,
    "handoff_incomplete": policy.HUMAN,
    "reviewer_conflict_scope": policy.HUMAN,
    "reviewer_conflict_security": policy.HUMAN,
    "g5_not_configured": policy.HUMAN,
    "g5_full_suite_failed": policy.HUMAN,
    # Kept on the worker-recovery Retry-After (PRD §3 "保持").
    "author_quota_exhausted": policy.HUMAN,
}


async def _parked(tmp_path, reason: str, mode: str):
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=mode)
    )
    line = make_line("line-0", assigned_model="codex")
    sibling = make_line("line-1", assigned_model="kimi")
    sibling.status = LineStatus.PASSED
    dispatch = await stored(collaborators, make_dispatch(line, sibling))
    dispatch.state = DispatchState.REVIEW
    snapshot = park_line(gates, dispatch, line, reason)
    if reason in _REVIEW_PARKS:
        # A review park blocks from the review stage it was in, so a resumed
        # review goes back to that stage (what Stage-Gate's block event keeps).
        state = _load_state(snapshot)
        _store_state(snapshot, replace(state, blocked_from=Stage.PRIMARY_REVIEW.value))
    return machine, gates, dispatch, line


_REVIEW_PARKS = frozenset(
    {
        "review_unavailable",
        "review_unparseable",
        "no_eligible_reviewer",
        "reviewers_quota_exhausted",
        "no_secondary_reviewer",
    }
)


def _cell(gates, dispatch, line):
    return gates.line_snapshot(dispatch, line)


def _owner(machine, gates, dispatch, line, moment):
    view = machine._liveness_view(dispatch, line, _cell(gates, dispatch, line))
    return owner.decide_owner(view, moment, env={})


def test_section_3_is_covered_by_the_policy_and_the_consistency_table() -> None:
    table = policy.default_policy()
    for reason in SECTION_3:
        assert table.group_for(reason) == EXPECTED_GROUP[reason], reason
        assert reason in consistency.REASON_STAGES, reason


@pytest.mark.parametrize("reason", SECTION_3)
async def test_shadow_records_the_first_row_and_spends_nothing(tmp_path, reason) -> None:
    machine, gates, dispatch, line = await _parked(tmp_path, reason, modes.SHADOW)
    before = (line.status, line.failure_reason, line.assigned_model, line.attempts)

    await machine._sweep_liveness(dispatch, now=WHEN)
    await machine._sweep_liveness(dispatch, now=LATER)

    assert (line.status, line.failure_reason, line.assigned_model, line.attempts) == before
    cell = _cell(gates, dispatch, line)
    stored_record = record.load(cell)
    assert stored_record is not None, reason
    assert stored_record.spent == 0
    assert stored_record.notified_at in (None, "")
    assert not (tmp_path / "inbox" / "needs-human.md").exists()
    plan = policy.default_policy().plan_for(reason)
    if not plan:
        # A person's reason: shadow notes the hand-off it would make and
        # neither latches nor notifies (enforce does, below).
        assert not stored_record.needs_human
        assert stored_record.next_action == "human"
        return
    assert stored_record.history, reason
    assert all(step.result == record.SHADOW for step in stored_record.history)
    first = stored_record.history[0].action
    expected = plan[0].action
    if reason == "no_eligible_worker" and expected == policy.REVIEW:
        expected = policy.REASSIGN
    assert first == expected, (reason, first, expected)
    assert _owner(machine, gates, dispatch, line, LATER).owner != owner.OWNER_NONE


@pytest.mark.parametrize("reason", SECTION_3)
async def test_enforce_takes_the_first_row_keeps_an_owner_and_stays_consistent(
    tmp_path, reason
) -> None:
    machine, gates, dispatch, line = await _parked(tmp_path, reason, modes.ENFORCE)
    await machine._sweep_liveness(dispatch, now=WHEN)
    await machine._sweep_liveness(dispatch, now=LATER)

    cell = _cell(gates, dispatch, line)
    stored_record = record.load(cell)
    group = EXPECTED_GROUP[reason]
    assert stored_record is not None, reason
    if group == policy.HUMAN:
        assert stored_record.needs_human, reason
        assert line.status is LineStatus.BLOCKED
        assert line.failure_reason == reason
        assert _owner(machine, gates, dispatch, line, LATER).owner == owner.OWNER_NEEDS_HUMAN
        return
    applied = [step for step in stored_record.history if step.result != record.SHADOW]
    assert applied, (reason, stored_record.history)
    first = applied[0].action
    if group == policy.R_EXIT or group == policy.R_REPLAN:
        assert first == policy.REASSIGN, (reason, first)
    elif group == policy.R_REVIEW:
        assert first == policy.REVIEW, (reason, first)
    elif group == policy.R_QUOTA:
        assert first in (policy.WAIT, policy.REVIEW, policy.REASSIGN), (reason, first)
    elif group == policy.R_SPLIT:
        assert first == policy.SPLIT, (reason, first)
        assert line.status is LineStatus.FAILED
        assert line.failure_reason == "split_off"
    assert stored_record.spent <= stored_record.budget
    if line.status is LineStatus.BLOCKED:
        # Still parked: a recovery timer (or a person) owns it.
        decision = _owner(machine, gates, dispatch, line, LATER)
        assert decision.owner in (owner.OWNER_RECOVERY_TIMER, owner.OWNER_NEEDS_HUMAN), (
            reason,
            decision,
        )
    elif line.status is not LineStatus.FAILED:
        # Recovered: handed back to the drive, which launches an ASSIGNED line
        # or reviews a RUNNING one right after the sweep (no run id yet).
        assert line.status in (LineStatus.ASSIGNED, LineStatus.RUNNING), (reason, line.status)
        assert stored_record.outcome == record.RECOVERED, reason
    if line.status is not LineStatus.FAILED:
        verdict = consistency.check(
            line.status, line.failure_reason, _snapshot_stage(_cell(gates, dispatch, line))
        )
        assert not isinstance(verdict, consistency.Inconsistent), (reason, verdict)


@pytest.mark.parametrize(
    "reason", [reason for reason in SECTION_3 if EXPECTED_GROUP[reason] != policy.HUMAN]
)
async def test_a_spent_budget_hands_the_line_to_a_person_once(tmp_path, reason) -> None:
    machine, gates, dispatch, line = await _parked(tmp_path, reason, modes.ENFORCE)
    cell = _cell(gates, dispatch, line)
    group = policy.default_policy().group_for(reason)
    spent = record.new_record(reason, group, budget=1, at=WHEN)
    spent = spent.with_step("reassign", at=WHEN, result=record.APPLIED)
    cell.update(record.store(cell, spent))

    await machine._sweep_liveness(dispatch, now=LATER)
    first = record.load(_cell(gates, dispatch, line))
    assert first is not None and first.needs_human, reason
    assert first.notified_at
    assert line.status is LineStatus.BLOCKED
    inbox = tmp_path / "inbox" / "needs-human.md"
    assert inbox.read_text(encoding="utf-8").count("## d1 / line-0") == 1

    await machine._sweep_liveness(dispatch, now=LATER + timedelta(hours=1))
    again = record.load(_cell(gates, dispatch, line))
    assert again is not None and again.notified_at == first.notified_at
    assert again.spent == first.spent
    assert inbox.read_text(encoding="utf-8").count("## d1 / line-0") == 1
