"""Unit tests for the pure line-status x Stage-Gate consistency table."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.server.services.autonomy.stage_gate import Stage
from src.tools.auto_dispatch.liveness import consistency
from src.tools.auto_dispatch.liveness.consistency import (
    BLOCK_REASONS,
    CONSISTENCY_TABLE,
    FAILED_REASON_STAGES,
    KNOWN_REASONS,
    LINE_STATUS_STAGES,
    REASON_STAGES,
    STATUS_STAGES,
    Consistency,
    Inconsistent,
    Ok,
    Repair,
    allowed_stages,
    check,
    check_snapshot,
    stages_for,
)
from src.tools.auto_dispatch.types import Line, LineStatus

CONSISTENCY_SOURCE = Path(consistency.__file__).resolve()

#: Every reason named in PRD n76 §3 (the parking-reason table).  Keeping this
#: list explicit means a table edit that drops one of them fails here before the
#: scan test's call-site coverage can hide it.
PRD_SECTION_3_REASONS = frozenset(
    {
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
        "liveness_inconsistent",
    }
)


def test_prd_section_3_reasons_are_all_in_the_table() -> None:
    missing = sorted(PRD_SECTION_3_REASONS - KNOWN_REASONS)
    assert not missing, "consistency.py has no row for: " + ", ".join(missing)


def test_every_prd_reason_is_a_string_literal_in_the_module() -> None:
    """The table keys must be literal, not imported constants the scan cannot read."""
    tree = ast.parse(CONSISTENCY_SOURCE.read_text(encoding="utf-8"))
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    missing = sorted(PRD_SECTION_3_REASONS - literals)
    assert not missing, "these reasons are not string literals in consistency.py: " + ", ".join(
        missing
    )


def test_every_block_reason_has_stages_and_the_blocked_stage_is_allowed() -> None:
    for reason in sorted(BLOCK_REASONS):
        allowed = allowed_stages(LineStatus.BLOCKED, reason)
        assert allowed, f"{reason} has no allowed stage"
        assert Stage.BLOCKED in allowed, f"{reason} must allow Stage.BLOCKED"
        assert all(isinstance(stage, Stage) for stage in allowed)


def test_every_failed_reason_has_stages() -> None:
    for reason, allowed in FAILED_REASON_STAGES.items():
        assert allowed, f"{reason} has no allowed stage"
        assert all(isinstance(stage, Stage) for stage in allowed)


@pytest.mark.parametrize("reason", sorted(BLOCK_REASONS))
def test_a_reason_at_its_allowed_stage_is_ok(reason: str) -> None:
    result = check(LineStatus.BLOCKED, reason, Stage.BLOCKED)
    assert isinstance(result, Ok)
    assert isinstance(result, Consistency)
    assert result.consistent
    assert result.repair is None
    assert not result.needs_human
    assert result.repair_event() is None


@pytest.mark.parametrize(
    ("status", "stage"),
    [
        (LineStatus.PENDING, Stage.PLANNING),
        (LineStatus.PENDING, Stage.SPEC_REVIEW),
        (LineStatus.PENDING, Stage.DAG_READY),
        (LineStatus.ASSIGNED, Stage.DAG_READY),
        (LineStatus.ASSIGNED, Stage.ASSIGNED),
        (LineStatus.ASSIGNED, Stage.IMPLEMENTING),
        (LineStatus.ASSIGNED, Stage.FIXING),
        (LineStatus.ASSIGNED, Stage.BLOCKED),
        (LineStatus.RUNNING, Stage.ASSIGNED),
        (LineStatus.RUNNING, Stage.IMPLEMENTING),
        (LineStatus.RUNNING, Stage.FOCUSED_TESTS),
        (LineStatus.RUNNING, Stage.FIXING),
        (LineStatus.RUNNING, Stage.PRIMARY_REVIEW),
        (LineStatus.RUNNING, Stage.SECONDARY_REVIEW),
        (LineStatus.RUNNING, Stage.INTEGRATION_CHECKS),
        (LineStatus.REVIEWING, Stage.FOCUSED_TESTS),
        (LineStatus.REVIEWING, Stage.PRIMARY_REVIEW),
        (LineStatus.REVIEWING, Stage.SECONDARY_REVIEW),
        (LineStatus.REVIEWING, Stage.AWAITING_ADJUDICATION),
        (LineStatus.REVIEWING, Stage.FIXING),
        (LineStatus.PASSED, Stage.INTEGRATION_CHECKS),
        (LineStatus.PASSED, Stage.AWAITING_HUMAN),
        (LineStatus.PASSED, Stage.MERGED),
        (LineStatus.FAILED, Stage.BLOCKED),
        (LineStatus.FAILED, Stage.CANCELLED),
    ],
)
def test_non_blocked_statuses_have_their_own_allowed_stages(
    status: LineStatus, stage: Stage
) -> None:
    result = check(status, None, stage)
    assert isinstance(result, Ok)
    assert result.consistent
    assert not result.needs_human


def test_a_fix_deferred_behind_a_sibling_is_consistent_in_fixing() -> None:
    """A queued review/G2 fix persists as ASSIGNED/serial_wait while its gate is fixing.

    The line turned ASSIGNED before ``_launch_guarded`` deferred it behind a
    sibling, but Stage-Gate had already recorded the review/G2 failure by
    moving to ``fixing``.  That is a valid queued fix, not drift: escalating it
    to a person would abandon work the drive resumes on its own.
    """
    result = check("ASSIGNED", "serial_wait", "fixing")

    assert isinstance(result, Ok)
    assert result.consistent
    assert not result.needs_human
    assert result.repair is None
    assert Stage.FIXING in allowed_stages(LineStatus.ASSIGNED, "serial_wait")


def test_a_review_stage_park_is_repaired_by_appending_block() -> None:
    result = check(LineStatus.BLOCKED, "review_unavailable", Stage.PRIMARY_REVIEW)

    assert isinstance(result, Repair)
    assert isinstance(result, Consistency)
    assert not result.consistent
    assert result.repair == "block"
    assert result.repair_event() == {"kind": "block"}
    assert not result.needs_human


def test_the_same_park_is_consistent_once_block_landed() -> None:
    result = check(LineStatus.BLOCKED, "review_unavailable", Stage.BLOCKED)

    assert isinstance(result, Ok)
    assert result.consistent
    assert result.repair is None


def test_a_replan_park_may_stay_in_replanning() -> None:
    for reason in ("replan_parked", "replan_uncombined", "replan_base_unresolved"):
        result = check(LineStatus.BLOCKED, reason, Stage.REPLANNING)
        assert isinstance(result, Ok)
        assert result.consistent


def test_a_g5_park_may_stay_in_integration_checks() -> None:
    result = check(LineStatus.BLOCKED, "g5_not_configured", Stage.INTEGRATION_CHECKS)

    assert isinstance(result, Ok)
    assert result.consistent


def test_a_conflict_park_may_stay_awaiting_adjudication() -> None:
    result = check(
        LineStatus.BLOCKED, "reviewer_conflict_scope", Stage.AWAITING_ADJUDICATION
    )

    assert isinstance(result, Ok)
    assert result.consistent


def test_a_conflict_park_in_a_random_stage_goes_to_a_person_instead_of_block() -> None:
    result = check(LineStatus.BLOCKED, "reviewer_conflict_scope", Stage.FIXING)

    assert isinstance(result, Inconsistent)
    assert not result.consistent
    assert result.repair is None
    assert result.needs_human
    assert result.repair_event() is None


@pytest.mark.parametrize("stage", [Stage.PRIMARY_REVIEW, Stage.SECONDARY_REVIEW])
@pytest.mark.parametrize("reason", sorted(BLOCK_REASONS))
def test_every_park_in_a_stale_review_stage_is_repaired_by_block(
    reason: str, stage: Stage
) -> None:
    """A line parked during review may leave the gate in either review stage.

    The acceptance rule is uniform: every parking reason is repaired with the
    missed ``block`` event, including the human-adjudication reasons and the
    sweep's own marker.
    """
    result = check(LineStatus.BLOCKED, reason, stage)

    assert isinstance(result, Repair)
    assert result.repair == "block"
    assert result.repair_event() == {"kind": "block"}
    assert not result.needs_human


@pytest.mark.parametrize("stage", [Stage.PRIMARY_REVIEW, Stage.SECONDARY_REVIEW])
@pytest.mark.parametrize(
    "reason",
    ["reviewer_conflict_scope", "reviewer_conflict_security", "liveness_inconsistent"],
)
def test_manual_reasons_still_repair_a_stale_review_stage(
    reason: str, stage: Stage
) -> None:
    result = check(LineStatus.BLOCKED, reason, stage)

    assert isinstance(result, Repair)
    assert result.repair == "block"
    assert not result.needs_human


@pytest.mark.parametrize(
    "reason",
    ["stage_gate_order", "self_review_forbidden", "reviewer_is_primary"],
)
def test_stage_gate_refusal_codes_are_parkable_reasons(reason: str) -> None:
    """``_apply_review_result`` parks the raw refusal code, not just ``review_blocked``."""
    assert reason in REASON_STAGES
    assert check(LineStatus.BLOCKED, reason, Stage.BLOCKED).consistent
    result = check(LineStatus.BLOCKED, reason, Stage.PRIMARY_REVIEW)
    assert isinstance(result, Repair)
    assert result.repair == "block"


def test_a_blocked_line_without_a_reason_is_inconsistent() -> None:
    result = check(LineStatus.BLOCKED, None, Stage.BLOCKED)

    assert isinstance(result, Inconsistent)
    assert not result.consistent
    assert result.needs_human


def test_an_unknown_reason_in_a_review_stage_needs_a_person() -> None:
    result = check(LineStatus.BLOCKED, "new_reason_from_a_future_patch", Stage.FIXING)

    assert isinstance(result, Inconsistent)
    assert not result.consistent
    assert result.needs_human
    assert result.repair is None
    assert "no consistency row" in result.detail


def test_an_unknown_reason_already_blocked_is_stage_consistent() -> None:
    # The static scan should catch a new reason first; an old stored row that
    # is already in ``blocked`` is not something the sweep can repair.
    result = check(LineStatus.BLOCKED, "new_reason_from_a_future_patch", Stage.BLOCKED)

    assert isinstance(result, Ok)
    assert result.consistent


def test_a_malformed_stage_is_escalated_not_treated_as_ungated() -> None:
    result = check(LineStatus.RUNNING, None, "garbled")

    assert isinstance(result, Inconsistent)
    assert result.applicable
    assert not result.consistent
    assert result.needs_human
    assert result.repair is None
    assert "unparseable" in result.detail


def test_a_nonterminal_mismatch_goes_to_a_person() -> None:
    result = check(LineStatus.RUNNING, None, Stage.PLANNING)

    assert isinstance(result, Inconsistent)
    assert not result.consistent
    assert result.repair is None
    assert result.needs_human
    assert "RUNNING" in result.detail


def test_a_passed_line_in_a_planning_stage_is_inconsistent() -> None:
    result = check(LineStatus.PASSED, None, Stage.PLANNING)

    assert isinstance(result, Inconsistent)
    assert not result.consistent
    assert result.needs_human


def test_split_off_is_excluded_from_integration_whatever_its_gate_stage() -> None:
    for stage in (Stage.PRIMARY_REVIEW, Stage.BLOCKED, Stage.REPLANNING, Stage.MERGED):
        result = check(LineStatus.FAILED, "split_off", stage)
        assert isinstance(result, Ok)
        assert result.consistent


def test_a_replanned_line_is_consistent_once_its_gate_resets_to_planning() -> None:
    assert check(LineStatus.FAILED, "replanned", Stage.PLANNING).consistent
    assert not check(LineStatus.FAILED, "replanned", Stage.REPLANNING).consistent


def test_an_ungated_dispatch_has_no_consistency_opinion() -> None:
    result = check(LineStatus.BLOCKED, "no_new_commit", None)

    assert isinstance(result, Ok)
    assert result.consistent
    assert not result.applicable
    assert not result.needs_human
    assert result.repair_event() is None


def test_allowed_stages_defaults_to_the_status_row() -> None:
    assert allowed_stages(LineStatus.PASSED) == STATUS_STAGES[LineStatus.PASSED]
    assert allowed_stages(LineStatus.BLOCKED) == {Stage.BLOCKED}
    assert allowed_stages(LineStatus.BLOCKED, "review_unavailable") == {Stage.BLOCKED}
    assert Stage.REPLANNING in allowed_stages(LineStatus.BLOCKED, "replan_parked")
    assert Stage.PRIMARY_REVIEW not in allowed_stages(LineStatus.BLOCKED, "no_new_commit")


def test_stages_for_is_the_old_spelling_of_the_same_table() -> None:
    assert stages_for is allowed_stages
    assert stages_for(LineStatus.BLOCKED, "review_unavailable") == {Stage.BLOCKED}
    assert LINE_STATUS_STAGES is STATUS_STAGES


def test_the_consistency_table_accepts_a_status_reason_pair() -> None:
    assert CONSISTENCY_TABLE == REASON_STAGES
    assert CONSISTENCY_TABLE[LineStatus.BLOCKED, "review_unavailable"] == {
        Stage.BLOCKED
    }
    assert Stage.REPLANNING in CONSISTENCY_TABLE[LineStatus.BLOCKED, "replan_parked"]


@pytest.mark.parametrize("status", list(LineStatus))
def test_string_statuses_are_accepted(status: LineStatus) -> None:
    assert check(status.value, None, Stage.BLOCKED).status is status


def test_check_snapshot_reads_the_machine_stage() -> None:
    line = Line(
        id="line-0",
        name="Line 0",
        task="work",
        status=LineStatus.BLOCKED,
        failure_reason="review_unparseable",
    )
    result = check_snapshot(line, {"machine": {"stage": "primary_review"}})

    assert isinstance(result, Repair)
    assert result.stage is Stage.PRIMARY_REVIEW
    assert result.repair == "block"


def test_check_snapshot_falls_back_to_phase() -> None:
    line = Line(
        id="line-0",
        name="Line 0",
        task="work",
        status=LineStatus.BLOCKED,
        failure_reason="review_unparseable",
    )
    result = check_snapshot(line, {"phase": "secondary_review"})

    assert isinstance(result, Repair)
    assert result.stage is Stage.SECONDARY_REVIEW
    assert result.repair == "block"


def test_check_snapshot_without_gates_is_not_applicable() -> None:
    line = Line(
        id="line-0",
        name="Line 0",
        task="work",
        status=LineStatus.BLOCKED,
        failure_reason="review_unparseable",
    )
    result = check_snapshot(line, None)

    assert isinstance(result, Ok)
    assert not result.applicable


def test_reason_table_covers_the_known_failed_reasons() -> None:
    assert set(REASON_STAGES) == set(BLOCK_REASONS)
    assert KNOWN_REASONS == BLOCK_REASONS | frozenset(FAILED_REASON_STAGES)


def test_two_failed_rounds_may_wait_in_replanning() -> None:
    """``operator.adjudicate`` parks the second failed fix round for a replan."""
    result = check(LineStatus.BLOCKED, "two_failed_rounds", Stage.REPLANNING)

    assert isinstance(result, Ok)
    assert check(LineStatus.BLOCKED, "two_failed_rounds", Stage.BLOCKED).consistent

