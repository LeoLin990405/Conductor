"""Stage-Gate state machine: lifecycle, failure policy, audit shape, purity."""

from __future__ import annotations

import ast
import re
from dataclasses import replace
from pathlib import Path

import pytest

from src.server.services.autonomy import stage_gate as sg
from src.server.services.autonomy.stage_gate import (
    EVENT_FIELDS,
    FAIL,
    PASS,
    REPLAN_OPTIONS,
    Event,
    GateResult,
    Handoff,
    Stage,
    StageGateState,
    advance,
    initial_state,
)

ROOT = Path(__file__).resolve().parents[5]
MODULE = ROOT / "src/server/services/autonomy/stage_gate.py"
DOCUMENT = ROOT / "docs/ports/stage-gate.md"

TASK = "task-1"
AUTHOR = "dev-a"
REVIEWER = "rev-codex"
PEER = "rev-kimi"

#: The purity gate from the PRD acceptance criteria, checked in-process.
FORBIDDEN = re.compile(r"import (os|time|asyncio|httpx|subprocess)|datetime\.now|open\(")


def _imported_modules() -> set[str]:
    tree = ast.parse(MODULE.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported



def _ts(second: int = 0) -> str:
    return f"2026-01-01T00:00:{second:02d}Z"


def _pass(reason: str = "criteria met", evidence: tuple[str, ...] = ()) -> GateResult:
    return GateResult(outcome=PASS, reason=reason, evidence=evidence)


def _fail(reason: str = "criteria unmet", evidence: tuple[str, ...] = ()) -> GateResult:
    return GateResult(outcome=FAIL, reason=reason, evidence=evidence)


def _handoff(**overrides: object) -> Handoff:
    values: dict[str, object] = {
        "branch": "feat/stage-gate-machine",
        "commit": "44f196c52",
        "files": ("src/server/services/autonomy/stage_gate.py",),
        "test_command": "pytest -q tests/unit/server/services/autonomy",
        "results": "72 passed",
        "known_limitations": "none",
    }
    values.update(overrides)
    return Handoff(**values)  # type: ignore[arg-type]


def _review(provider: str, result: GateResult, **overrides: object) -> Event:
    values: dict[str, object] = {
        "kind": "primary_review",
        "provider": provider,
        "result": result,
        "timestamp": _ts(7),
    }
    values.update(overrides)
    return Event(**values)  # type: ignore[arg-type]


def _at(
    stage: Stage,
    *,
    risk_bearing: bool = False,
    author: str = AUTHOR,
    developer: str = AUTHOR,
    primary: str = "",
    replan_options: tuple[str, ...] = (),
) -> StageGateState:
    """A state parked at ``stage`` with a plausible assignment."""
    return StageGateState(
        task_id=TASK,
        stage=stage,
        risk_bearing=risk_bearing,
        author_provider=author,
        developer_provider=developer,
        primary_reviewer=primary,
        # you only reach the secondary review with a primary that passed
        primary_passed=bool(primary),
        # a replan always offers something; the machine only accepts what was
        # offered for this replan
        replan_options=(
            replan_options or (REPLAN_OPTIONS if stage is Stage.REPLANNING else ())
        ),
    )


def _walk(state: StageGateState, events: tuple[Event, ...]) -> tuple[StageGateState, list[sg.Transition]]:
    seen: list[sg.Transition] = []
    for event in events:
        transition = advance(state, event)
        assert transition.accepted, f"{event.kind} refused: {transition.reason}"
        seen.append(transition)
        state = transition.state
    return state, seen


def _happy_events(risk_bearing: bool) -> tuple[Event, ...]:
    events = [
        Event(kind="spec_submitted", timestamp=_ts(1)),
        Event(kind="gate_result", gate="G0", result=_pass(), timestamp=_ts(2)),
        Event(kind="assign", provider=AUTHOR, timestamp=_ts(3)),
        Event(kind="start_implementation", timestamp=_ts(4)),
        Event(kind="submit_work", handoff=_handoff(), timestamp=_ts(5)),
        Event(kind="gate_result", gate="G2", result=_pass(), timestamp=_ts(6)),
        Event(
            kind="primary_review",
            provider=REVIEWER,
            result=_pass(),
            verdict="approve" if risk_bearing else "merge",
            timestamp=_ts(7),
        ),
    ]
    if risk_bearing:
        events.append(
            Event(kind="secondary_review", provider=PEER, result=_pass(), timestamp=_ts(8))
        )
    events.extend(
        [
            Event(kind="gate_result", gate="G5", result=_pass(), timestamp=_ts(9)),
            Event(kind="gate_result", gate="G6", result=_pass(), timestamp=_ts(10)),
        ]
    )
    return tuple(events)


class TestHappyPath:
    def test_planning_reaches_merged_through_every_gate(self):
        state, transitions = _walk(initial_state(TASK, risk_bearing=True), _happy_events(True))

        assert state.stage is Stage.MERGED
        assert [item.state.stage for item in transitions] == [
            Stage.SPEC_REVIEW,
            Stage.DAG_READY,
            Stage.ASSIGNED,
            Stage.IMPLEMENTING,
            Stage.FOCUSED_TESTS,
            Stage.PRIMARY_REVIEW,
            Stage.SECONDARY_REVIEW,
            Stage.INTEGRATION_CHECKS,
            Stage.AWAITING_HUMAN,
            Stage.MERGED,
        ]

    def test_a_primary_merge_skips_the_secondary_review_when_not_risk_bearing(self):
        state, transitions = _walk(initial_state(TASK, risk_bearing=False), _happy_events(False))

        assert state.stage is Stage.MERGED
        assert state.secondary_skipped is True
        assert state.secondary_passed is False
        merge = transitions[6]
        assert merge.reason == "primary_merge_no_risk"
        assert merge.event.to_dict()["to_stage"] == "integration_checks"
    def test_every_accepted_transition_records_an_event(self):
        _, transitions = _walk(initial_state(TASK, risk_bearing=True), _happy_events(True))

        for transition in transitions:
            record = transition.event
            assert record is not None
            assert record.from_stage != record.to_stage or record.reason in {
                "developer_reassigned",
                "developer_replaced",
            }
            assert record.task_id == TASK

    def test_the_gate_label_travels_with_the_transition(self):
        _, transitions = _walk(initial_state(TASK, risk_bearing=True), _happy_events(True))

        gates = [item.event.gate for item in transitions]  # type: ignore[union-attr]
        assert gates == ["G0", "G0", "D1", "", "G2", "G2", "", "G5", "G5", "G6"]


class TestGateFailures:
    def test_g0_failure_returns_to_the_planner(self):
        state = _at(Stage.SPEC_REVIEW)
        transition = advance(
            state, Event(kind="gate_result", gate="G0", result=_fail(), timestamp=_ts(2))
        )

        assert transition.accepted
        assert transition.reason == "spec_rejected"
        assert transition.state.stage is Stage.REPLANNING
        assert transition.options == ("resplit_task",)

    def test_g2_failure_sends_the_developer_back_to_fixing(self):
        state = _at(Stage.FOCUSED_TESTS)
        transition = advance(
            state, Event(kind="gate_result", gate="G2", result=_fail(), timestamp=_ts(6))
        )

        assert transition.reason == "focused_tests_failed"
        assert transition.state.stage is Stage.FIXING
        assert transition.state.return_stage == "focused_tests"
        # A failed gate is not a fix round; the round exists once the author
        # has fixed and resubmitted.
        assert transition.state.fix_rounds == 0

    def test_a_g2_fix_counts_one_round(self):
        blocked = advance(
            _at(Stage.FOCUSED_TESTS),
            Event(kind="gate_result", gate="G2", result=_fail(), timestamp=_ts(6)),
        )
        fixed = advance(blocked.state, Event(kind="submit_work", timestamp=_ts(7)))

        assert fixed.state.stage is Stage.FOCUSED_TESTS
        assert fixed.state.fix_rounds == 1

    def test_g5_failure_blocks_the_task_for_an_operator(self):
        state = _at(Stage.INTEGRATION_CHECKS)
        transition = advance(
            state, Event(kind="gate_result", gate="G5", result=_fail(), timestamp=_ts(9))
        )

        assert transition.reason == "integration_checks_failed"
        assert transition.state.stage is Stage.BLOCKED
        assert transition.state.blocked_from == "integration_checks"

    def test_a_blocked_task_resumes_where_it_stopped(self):
        blocked = advance(
            _at(Stage.INTEGRATION_CHECKS),
            Event(kind="gate_result", gate="G5", result=_fail(), timestamp=_ts(9)),
        )
        resumed = advance(blocked.state, Event(kind="resume", timestamp=_ts(10)))

        assert resumed.reason == "resumed"
        assert resumed.state.stage is Stage.INTEGRATION_CHECKS
        assert resumed.state.blocked_from == ""

    def test_g6_rejection_returns_to_the_planner(self):
        state = _at(Stage.AWAITING_HUMAN)
        transition = advance(
            state, Event(kind="gate_result", gate="G6", result=_fail(), timestamp=_ts(11))
        )

        assert transition.reason == "human_rejected"
        assert transition.state.stage is Stage.REPLANNING
        assert transition.options == ("resplit_task",)

    def test_a_gate_result_for_another_gate_is_refused(self):
        transition = advance(
            _at(Stage.FOCUSED_TESTS),
            Event(kind="gate_result", gate="G0", result=_pass(), timestamp=_ts(6)),
        )

        assert not transition.accepted
        assert transition.reason == "gate_mismatch"


class TestFailurePolicy:
    def test_the_initial_block_is_not_a_fix_round(self):
        blocked = advance(_at(Stage.PRIMARY_REVIEW), _review(REVIEWER, _fail()))

        assert blocked.reason == "review_blocked"
        assert blocked.state.stage is Stage.FIXING
        assert blocked.state.blocking_rounds == 0
        assert blocked.state.fix_rounds == 0
        assert blocked.state.blocking_reviewer == REVIEWER

    def test_block_fix_block_does_not_escalate(self):
        blocked, resubmitted = _block_and_fix(_at(Stage.PRIMARY_REVIEW), REVIEWER)
        second = advance(resubmitted.state, _review(REVIEWER, _fail()))

        assert blocked.state.blocking_rounds == 0
        assert second.reason == "review_blocked"
        assert second.state.stage is Stage.FIXING
        assert second.state.blocking_rounds == 1
        assert second.state.fix_rounds == 1

    def test_three_blocks_from_the_same_reviewer_replan(self):
        replanning = _reach_replanning(REVIEWER)

        assert replanning.reason == "two_failed_rounds"
        assert replanning.state.stage is Stage.REPLANNING
        assert replanning.state.blocking_rounds == 2
        assert replanning.state.fix_rounds == 2
        assert replanning.options == REPLAN_OPTIONS
        assert replanning.state.replan_options == REPLAN_OPTIONS

    def test_a_block_from_another_reviewer_starts_a_new_round(self):
        blocked, resubmitted = _block_and_fix(_at(Stage.PRIMARY_REVIEW), REVIEWER)
        second = advance(resubmitted.state, _review(PEER, _fail()))

        assert second.reason == "review_blocked"
        assert second.state.stage is Stage.FIXING
        # a changed reviewer starts again at zero: their first BLOCK is initial
        assert second.state.blocking_rounds == 0
        assert second.state.fix_rounds == 0
        assert second.state.blocking_reviewer == PEER

    def test_a_merge_mid_sequence_clears_the_count(self):
        blocked, resubmitted = _block_and_fix(
            _at(Stage.PRIMARY_REVIEW, risk_bearing=False), REVIEWER
        )
        merged = advance(resubmitted.state, _review(REVIEWER, _pass(), verdict="merge"))

        assert merged.reason == "primary_merge_no_risk"
        assert merged.state.stage is Stage.INTEGRATION_CHECKS
        assert merged.state.blocking_reviewer == ""
        assert merged.state.blocking_rounds == 0

    def test_a_block_after_a_full_round_trip_starts_from_zero(self):
        # Explicit event sequence: BLOCK -> fix -> MERGE -> G5 -> G6 reject ->
        # replan -> resplit -> a fresh cycle, and the same reviewer BLOCKs again.
        merged = advance(
            advance(
                _block_and_fix(_at(Stage.PRIMARY_REVIEW, risk_bearing=False), REVIEWER)[1].state,
                _review(REVIEWER, _pass(), verdict="merge"),
            ).state,
            Event(kind="gate_result", gate="G5", result=_pass(), timestamp=_ts(9)),
        )
        g6_reject = advance(
            merged.state,
            Event(kind="gate_result", gate="G6", result=_fail(), timestamp=_ts(12)),
        )
        assert g6_reject.state.stage is Stage.REPLANNING

        state = advance(
            g6_reject.state,
            Event(kind="replan_decision", option="resplit_task", timestamp=_ts(13)),
        ).state
        for event in (
            Event(kind="spec_submitted", timestamp=_ts(14)),
            Event(kind="gate_result", gate="G0", result=_pass(), timestamp=_ts(15)),
            Event(kind="assign", provider=AUTHOR, timestamp=_ts(16)),
            Event(kind="start_implementation", timestamp=_ts(17)),
            Event(kind="submit_work", timestamp=_ts(18)),
            Event(kind="gate_result", gate="G2", result=_pass(), timestamp=_ts(19)),
        ):
            state = advance(state, event).state

        again = advance(state, _review(REVIEWER, _fail()))

        assert again.reason == "review_blocked"
        assert again.state.blocking_rounds == 0
        assert again.state.fix_rounds == 0

    def test_an_approve_opens_the_next_review_stage_from_zero(self):
        state = _at(Stage.PRIMARY_REVIEW)
        for _ in range(2):  # two failed fix rounds under the same reviewer
            _, resubmitted = _block_and_fix(state, REVIEWER)
            state = resubmitted.state
        assert state.blocking_rounds == 1

        approved = advance(
            state, _review(REVIEWER, _pass(), verdict="approve")
        )

        assert approved.reason == "primary_review_passed"
        assert approved.state.stage is Stage.SECONDARY_REVIEW
        assert approved.state.blocking_reviewer == ""
        assert approved.state.blocking_rounds == 0
        assert approved.state.fix_rounds == 0

    def test_a_secondary_approve_enters_g5_from_zero(self):
        approved = advance(
            _at(Stage.PRIMARY_REVIEW), _review(REVIEWER, _pass(), verdict="approve")
        )
        assert approved.state.stage is Stage.SECONDARY_REVIEW

        state = approved.state
        for _ in range(2):  # two failed fix rounds under the secondary reviewer
            blocked = advance(state, _review(PEER, _fail(), kind="secondary_review"))
            state = advance(
                blocked.state, Event(kind="submit_work", timestamp=_ts(9))
            ).state
        assert state.blocking_rounds == 1

        passed = advance(state, _review(PEER, _pass(), kind="secondary_review"))

        assert passed.reason == "secondary_review_passed"
        assert passed.state.stage is Stage.INTEGRATION_CHECKS
        assert passed.state.blocking_reviewer == ""
        assert passed.state.blocking_rounds == 0
        assert passed.state.fix_rounds == 0

    def test_a_secondary_block_returns_to_the_secondary_review(self):
        state = _at(Stage.SECONDARY_REVIEW, primary=REVIEWER)
        blocked = advance(state, _review(PEER, _fail(), kind="secondary_review"))
        resubmitted = advance(blocked.state, Event(kind="submit_work", timestamp=_ts(9)))

        assert blocked.state.stage is Stage.FIXING
        assert blocked.state.return_stage == "secondary_review"
        assert resubmitted.state.stage is Stage.SECONDARY_REVIEW

    def test_an_ordinary_reviewer_conflict_goes_to_the_planner(self):
        state = _at(Stage.SECONDARY_REVIEW, primary=REVIEWER)
        transition = advance(
            state,
            _review(PEER, _fail(), kind="secondary_review", conflict="correctness"),
        )

        assert transition.reason == "reviewer_conflict_planner"
        assert transition.state.stage is Stage.REPLANNING
        assert transition.options == REPLAN_OPTIONS

    @pytest.mark.parametrize("category", ["scope", "security"])
    def test_a_scope_or_security_conflict_awaits_adjudication(self, category):
        state = _at(Stage.SECONDARY_REVIEW, primary=REVIEWER)
        transition = advance(
            state,
            _review(PEER, _fail(), kind="secondary_review", conflict=category),
        )

        assert transition.reason == f"reviewer_conflict_{category}"
        assert transition.state.stage is Stage.AWAITING_ADJUDICATION
        assert transition.state.conflict_reviewer == PEER


class TestConflictAdjudication:
    """B1: a reviewer conflict is adjudicated, never approved as a merge.

    ``awaiting_adjudication`` and ``awaiting_human`` are separate states: G6 is
    only ever entered from a passing G5, so no adjudication outcome -- and no
    hand-built state -- can reach ``merged`` without the dual review and G5.
    """

    @pytest.mark.parametrize("category", ["scope", "security"])
    @pytest.mark.parametrize("risk_bearing", [True, False])
    def test_g6_is_not_available_while_a_conflict_is_open(self, category, risk_bearing):
        state = _at(Stage.SECONDARY_REVIEW, primary=REVIEWER, risk_bearing=risk_bearing)
        conflict = advance(
            state, _review(PEER, _fail(), kind="secondary_review", conflict=category)
        )

        for kind in ("gate_result", "replan_decision", "secondary_review"):
            refused = advance(
                conflict.state,
                Event(
                    kind=kind,
                    gate="G6",
                    result=_pass(),
                    provider=PEER,
                    timestamp=_ts(12),
                ),
            )
            assert not refused.accepted, kind
            assert refused.reason == "stage_gate_order", kind
            assert refused.state is conflict.state

    @pytest.mark.parametrize("category", ["scope", "security"])
    @pytest.mark.parametrize("risk_bearing", [True, False])
    def test_upholding_the_block_returns_to_fixing(self, category, risk_bearing):
        conflict = advance(
            _at(Stage.SECONDARY_REVIEW, primary=REVIEWER, risk_bearing=risk_bearing),
            _review(PEER, _fail(), kind="secondary_review", conflict=category),
        )
        upheld = advance(
            conflict.state,
            Event(
                kind="adjudicate_conflict", option="maintain_block", timestamp=_ts(12)
            ),
        )

        assert upheld.reason == "review_blocked"
        assert upheld.state.stage is Stage.FIXING
        assert upheld.state.blocking_reviewer == PEER
        assert upheld.state.return_stage == "secondary_review"
        # the fix goes back to the review it failed, not straight to a gate
        resubmitted = advance(upheld.state, Event(kind="submit_work", timestamp=_ts(13)))
        assert resubmitted.state.stage is Stage.SECONDARY_REVIEW

    @pytest.mark.parametrize("category", ["scope", "security"])
    @pytest.mark.parametrize("risk_bearing", [True, False])
    def test_overturning_the_block_runs_the_next_required_gate(self, category, risk_bearing):
        conflict = advance(
            _at(Stage.SECONDARY_REVIEW, primary=REVIEWER, risk_bearing=risk_bearing),
            _review(PEER, _fail(), kind="secondary_review", conflict=category),
        )
        overturned = advance(
            conflict.state,
            Event(
                kind="adjudicate_conflict", option="overturn_block", timestamp=_ts(12)
            ),
        )

        # the overturned secondary review counts as passed, so the next
        # required gate is G5 -- never a straight merge
        assert overturned.reason == "conflict_overturned"
        assert overturned.state.stage is Stage.INTEGRATION_CHECKS
        assert overturned.state.secondary_passed is True
        assert overturned.state.conflict_reviewer == ""

        g5 = advance(
            overturned.state,
            Event(kind="gate_result", gate="G5", result=_pass(), timestamp=_ts(13)),
        )
        assert g5.state.stage is Stage.AWAITING_HUMAN
        merged = advance(
            g5.state,
            Event(kind="gate_result", gate="G6", result=_pass(), timestamp=_ts(14)),
        )
        assert merged.state.stage is Stage.MERGED
        assert merged.state.secondary_passed is True

    def test_an_overturn_cannot_wave_through_a_review_that_never_passed(self):
        # v1 raises conflicts only at the secondary review, so this needs a
        # restored state: the primary review never passed, and an adjudication
        # must not manufacture a secondary pass on top of that.
        state = StageGateState.from_dict(
            {
                "task_id": TASK,
                "stage": "awaiting_adjudication",
                "risk_bearing": True,
                "author_provider": AUTHOR,
                "developer_provider": AUTHOR,
                "primary_reviewer": REVIEWER,
                "primary_passed": False,
                "conflict_reviewer": PEER,
            }
        )
        overturned = advance(
            state,
            Event(kind="adjudicate_conflict", option="overturn_block", timestamp=_ts(12)),
        )

        assert overturned.reason == "conflict_overturned"
        assert overturned.state.stage is Stage.SECONDARY_REVIEW
        assert overturned.state.primary_passed is False
        assert overturned.state.secondary_passed is False

    def test_an_upheld_conflict_counts_as_a_round_for_that_reviewer(self):
        state = _at(Stage.SECONDARY_REVIEW, primary=REVIEWER)
        conflict = advance(
            state, _review(PEER, _fail(), kind="secondary_review", conflict="scope")
        )
        upheld = advance(
            conflict.state,
            Event(kind="adjudicate_conflict", option="maintain_block", timestamp=_ts(12)),
        )
        assert upheld.state.blocking_rounds == 0

        resubmitted = advance(upheld.state, Event(kind="submit_work", timestamp=_ts(13)))
        second = advance(
            resubmitted.state, _review(PEER, _fail(), kind="secondary_review")
        )
        assert second.state.blocking_rounds == 1

    def test_an_unknown_adjudication_is_refused(self):
        conflict = advance(
            _at(Stage.SECONDARY_REVIEW, primary=REVIEWER),
            _review(PEER, _fail(), kind="secondary_review", conflict="security"),
        )
        transition = advance(
            conflict.state,
            Event(kind="adjudicate_conflict", option="waive", timestamp=_ts(12)),
        )

        assert not transition.accepted
        assert transition.reason == "adjudication_option_unknown"
        assert transition.state is conflict.state

    def test_an_adjudication_without_a_conflict_reviewer_is_refused(self):
        state = _at(Stage.AWAITING_ADJUDICATION)
        transition = advance(
            state,
            Event(kind="adjudicate_conflict", option="maintain_block", timestamp=_ts(12)),
        )

        assert not transition.accepted
        assert transition.reason == "provider_required"

    def test_awaiting_human_is_only_reachable_from_a_passing_g5(self):
        for stage in Stage:
            transition = advance(
                _at(stage),
                Event(kind="gate_result", gate="G6", result=_pass(), timestamp=_ts(12)),
            )
            reached_merged = transition.accepted and transition.state.stage is Stage.MERGED
            assert reached_merged == (stage is Stage.AWAITING_HUMAN), stage

    def test_a_primary_merge_cannot_replace_the_secondary_review(self):
        transition = advance(
            _at(Stage.PRIMARY_REVIEW, risk_bearing=True),
            _review(REVIEWER, _pass(), verdict="merge"),
        )

        assert not transition.accepted
        assert transition.reason == "secondary_review_required"
        assert transition.event is None

    def test_risk_bearing_work_cannot_enter_g5_without_the_secondary_review(self):
        approved = advance(
            _at(Stage.PRIMARY_REVIEW, risk_bearing=True),
            _review(REVIEWER, _pass(), verdict="approve"),
        )
        assert approved.state.stage is Stage.SECONDARY_REVIEW

        skipped = advance(
            approved.state,
            Event(kind="gate_result", gate="G5", result=_pass(), timestamp=_ts(9)),
        )
        assert not skipped.accepted
        assert skipped.reason == "stage_gate_order"

    @pytest.mark.parametrize("missing", ["primary_passed", "secondary_passed"])
    def test_a_restored_state_missing_a_review_is_refused_at_g5_and_g6(self, missing):
        # v2 may restore a state from the database; the guards still apply.
        data = {
            "task_id": TASK,
            "stage": "integration_checks",
            "risk_bearing": True,
            "author_provider": AUTHOR,
            "developer_provider": AUTHOR,
            "primary_reviewer": REVIEWER,
            "primary_passed": True,
            "secondary_reviewer": PEER,
            "secondary_passed": True,
        }
        data[missing] = False
        state = StageGateState.from_dict(data)

        for gate in ("G5", "G6"):
            at_gate = state if gate == "G5" else replace(state, stage=Stage.AWAITING_HUMAN)
            transition = advance(
                at_gate,
                Event(kind="gate_result", gate=gate, result=_pass(), timestamp=_ts(12)),
            )
            assert not transition.accepted, gate
            assert transition.reason == "secondary_review_required", gate
            assert transition.state is at_gate

    def test_a_state_that_lost_its_secondary_review_is_refused_at_g5(self):
        # v2 may restore a state from the database; the guard still applies.
        state = StageGateState.from_dict(
            {
                "task_id": TASK,
                "stage": "integration_checks",
                "risk_bearing": True,
                "author_provider": AUTHOR,
                "developer_provider": AUTHOR,
                "primary_reviewer": REVIEWER,
                "primary_passed": True,
            }
        )
        transition = advance(
            state, Event(kind="gate_result", gate="G5", result=_pass(), timestamp=_ts(9))
        )

        assert not transition.accepted
        assert transition.reason == "secondary_review_required"

    def test_the_author_cannot_take_the_secondary_review(self):
        state = _at(Stage.SECONDARY_REVIEW, primary=REVIEWER)
        transition = advance(
            state, _review(AUTHOR, _pass(), kind="secondary_review")
        )

        assert not transition.accepted
        assert transition.reason == "self_review_forbidden"

    def test_the_primary_reviewer_cannot_review_their_own_code(self):
        transition = advance(
            _at(Stage.PRIMARY_REVIEW), _review(AUTHOR, _pass(), verdict="approve")
        )

        assert not transition.accepted
        assert transition.reason == "self_review_forbidden"

    def test_the_original_author_stays_barred_after_a_handoff(self):
        state = StageGateState(
            task_id=TASK,
            stage=Stage.PRIMARY_REVIEW,
            author_provider=AUTHOR,
            developer_provider="dev-b",
        )
        transition = advance(state, _review(AUTHOR, _pass(), verdict="approve"))

        assert not transition.accepted
        assert transition.reason == "self_review_forbidden"

    def test_the_secondary_review_needs_another_model(self):
        state = _at(Stage.SECONDARY_REVIEW, primary=REVIEWER)
        transition = advance(
            state, _review(REVIEWER, _pass(), kind="secondary_review")
        )

        assert not transition.accepted
        assert transition.reason == "reviewer_is_primary"

    def test_a_swap_during_implementation_is_refused(self):
        transition = advance(
            _at(Stage.IMPLEMENTING),
            Event(
                kind="reassign",
                provider="dev-b",
                handoff=_handoff(),
                timestamp=_ts(5),
            ),
        )

        assert not transition.accepted
        assert transition.reason == "reassignment_not_at_checkpoint"
        assert transition.state.stage is Stage.IMPLEMENTING

    def test_a_checkpoint_swap_before_review_needs_a_handoff(self):
        transition = advance(
            _at(Stage.ASSIGNED),
            Event(kind="reassign", provider="dev-b", timestamp=_ts(5)),
        )

        assert not transition.accepted
        assert transition.reason == "handoff_required"

    @pytest.mark.parametrize(
        "missing",
        ("branch", "commit", "files", "test_command", "results", "known_limitations"),
    )
    def test_an_incomplete_handoff_is_refused(self, missing):
        transition = advance(
            _at(Stage.ASSIGNED),
            Event(
                kind="reassign",
                provider="dev-b",
                handoff=_handoff(**{missing: "" if missing != "files" else ()}),
                timestamp=_ts(5),
            ),
        )

        assert not transition.accepted
        assert transition.reason == "handoff_incomplete"
        assert missing in transition.detail

    def test_a_checkpoint_swap_after_a_block_is_allowed(self):
        blocked = advance(_at(Stage.PRIMARY_REVIEW), _review(REVIEWER, _fail()))
        swapped = advance(
            blocked.state,
            Event(
                kind="reassign",
                provider="dev-b",
                handoff=_handoff(),
                timestamp=_ts(8),
            ),
        )

        assert swapped.accepted
        assert swapped.reason == "developer_reassigned"
        assert swapped.state.developer_provider == "dev-b"
        assert swapped.state.author_provider == AUTHOR
        assert swapped.event.handoff_ref == _handoff().reference()

    def test_a_checkpoint_swap_keeps_the_reviewers_count(self):
        # Swapping the developer does not reset the reviewer's round count: the
        # same reviewer still has to be convinced.
        _, resubmitted = _block_and_fix(_at(Stage.PRIMARY_REVIEW), REVIEWER)
        second = advance(resubmitted.state, _review(REVIEWER, _fail()))
        assert second.state.stage is Stage.FIXING

        swapped = advance(
            second.state,
            Event(
                kind="reassign",
                provider="dev-b",
                handoff=_handoff(),
                timestamp=_ts(9),
            ),
        )

        assert swapped.state.blocking_reviewer == REVIEWER
        assert swapped.state.blocking_rounds == 1
        assert swapped.state.fix_rounds == 1

    def test_the_reviewers_count_survives_g2_and_a_checkpoint_swap(self):
        """B3: X BLOCK -> fix -> X BLOCK -> swap to B -> B submits -> G2 pass.

        No MERGE and no reviewer change happened, so the count must survive the
        tests and the developer swap, and X's third BLOCK must escalate.
        """
        _, resubmitted = _block_and_fix(_at(Stage.PRIMARY_REVIEW), REVIEWER)
        second = advance(resubmitted.state, _review(REVIEWER, _fail()))
        assert second.state.stage is Stage.FIXING
        assert second.state.blocking_rounds == 1

        state = advance(
            second.state,
            Event(
                kind="reassign",
                provider="dev-b",
                handoff=_handoff(),
                timestamp=_ts(9),
            ),
        ).state
        for event in (
            Event(kind="start_implementation", timestamp=_ts(10)),
            Event(kind="submit_work", timestamp=_ts(11)),
            Event(kind="gate_result", gate="G2", result=_pass(), timestamp=_ts(12)),
        ):
            state = advance(state, event).state

        assert state.stage is Stage.PRIMARY_REVIEW
        assert state.blocking_reviewer == REVIEWER
        assert state.blocking_rounds == 1
        # the swapped-in developer's submission counts as a fix round too
        assert state.fix_rounds == 2

        third = advance(state, _review(REVIEWER, _fail()))
        assert third.reason == "two_failed_rounds"
        assert third.state.stage is Stage.REPLANNING
        assert third.state.blocking_rounds == 2
        assert third.state.fix_rounds == 2
        assert third.options == REPLAN_OPTIONS

    def test_a_g2_failure_does_not_reset_the_count(self):
        _, resubmitted = _block_and_fix(_at(Stage.PRIMARY_REVIEW), REVIEWER)
        second = advance(resubmitted.state, _review(REVIEWER, _fail()))
        state = advance(
            second.state,
            Event(
                kind="reassign",
                provider="dev-b",
                handoff=_handoff(),
                timestamp=_ts(9),
            ),
        ).state
        for event in (
            Event(kind="start_implementation", timestamp=_ts(10)),
            Event(kind="submit_work", timestamp=_ts(11)),
            Event(kind="gate_result", gate="G2", result=_fail(), timestamp=_ts(12)),
        ):
            state = advance(state, event).state

        assert state.stage is Stage.FIXING
        assert state.blocking_rounds == 1
        assert state.blocking_reviewer == REVIEWER


class TestContributors:
    """B2: everyone who actually implemented is barred from reviewing."""

    def test_a_submission_records_its_developer_once(self):
        state = _at(Stage.IMPLEMENTING, developer="dev-a", author="dev-a")
        first = advance(state, Event(kind="submit_work", timestamp=_ts(5)))
        assert first.state.contributors == ("dev-a",)

        g2_failed = advance(
            first.state,
            Event(kind="gate_result", gate="G2", result=_fail(), timestamp=_ts(6)),
        )
        resubmitted = advance(
            g2_failed.state, Event(kind="submit_work", timestamp=_ts(7))
        )
        assert resubmitted.state.contributors == ("dev-a",)

    def test_an_assignment_alone_is_not_a_contribution(self):
        swapped = advance(
            _at(Stage.ASSIGNED, developer="dev-a", author="dev-a"),
            Event(
                kind="reassign",
                provider="dev-b",
                handoff=_handoff(),
                timestamp=_ts(6),
            ),
        )

        assert swapped.state.contributors == ()
        assert swapped.state.developer_provider == "dev-b"

    def test_a_replaced_developer_cannot_review(self):
        state = _three_implementers()

        assert state.contributors == ("dev-a", "dev-b", "dev-c")
        assert state.developer_provider == "dev-c"

        primary = advance(state, _review("dev-b", _pass(), verdict="approve"))
        assert primary.reason == "self_review_forbidden"
        assert primary.state is state

        approved = advance(state, _review(REVIEWER, _pass(), verdict="approve"))
        assert approved.state.stage is Stage.SECONDARY_REVIEW
        secondary = advance(
            approved.state, _review("dev-b", _pass(), kind="secondary_review")
        )
        assert secondary.reason == "self_review_forbidden"

    def test_the_contributor_set_survives_serialization(self):
        state = StageGateState.from_dict(_three_implementers().to_dict())

        assert state.contributors == ("dev-a", "dev-b", "dev-c")
        assert advance(state, _review("dev-b", _pass(), verdict="approve")).reason == (
            "self_review_forbidden"
        )
        approved = advance(state, _review(REVIEWER, _pass(), verdict="approve"))
        assert advance(
            approved.state, _review("dev-b", _pass(), kind="secondary_review")
        ).reason == "self_review_forbidden"

    def test_the_original_author_stays_barred_after_two_handoffs(self):
        state = _three_implementers()

        assert advance(state, _review("dev-a", _pass(), verdict="approve")).reason == (
            "self_review_forbidden"
        )


def _three_implementers() -> StageGateState:
    """A implements, is blocked, B implements, is blocked, C implements.

    Each round uses a different reviewer so the fix-round counter never
    escalates and the chain stays about contributors, not counting.
    """
    events = (
        Event(kind="spec_submitted", timestamp=_ts(1)),
        Event(kind="gate_result", gate="G0", result=_pass(), timestamp=_ts(2)),
        Event(kind="assign", provider="dev-a", timestamp=_ts(3)),
        Event(kind="start_implementation", timestamp=_ts(4)),
        Event(kind="submit_work", timestamp=_ts(5)),
        Event(kind="gate_result", gate="G2", result=_pass(), timestamp=_ts(6)),
        Event(
            kind="primary_review", provider=REVIEWER, result=_fail(), timestamp=_ts(7)
        ),
        Event(
            kind="reassign", provider="dev-b", handoff=_handoff(), timestamp=_ts(8)
        ),
        Event(kind="start_implementation", timestamp=_ts(9)),
        Event(kind="submit_work", timestamp=_ts(10)),
        Event(kind="gate_result", gate="G2", result=_pass(), timestamp=_ts(11)),
        Event(
            kind="primary_review", provider=PEER, result=_fail(), timestamp=_ts(12)
        ),
        Event(
            kind="reassign", provider="dev-c", handoff=_handoff(), timestamp=_ts(13)
        ),
        Event(kind="start_implementation", timestamp=_ts(14)),
        Event(kind="submit_work", timestamp=_ts(15)),
        Event(kind="gate_result", gate="G2", result=_pass(), timestamp=_ts(16)),
    )
    state, _ = _walk(initial_state(TASK), events)
    return state


class TestDagMilestone:
    """D1 is a milestone, not a pass/fail gate."""

    def test_an_invalid_dag_is_a_planning_failure(self):
        transition = advance(
            _at(Stage.DAG_READY),
            Event(kind="reject_dag", reason="cycle", timestamp=_ts(3)),
        )

        assert transition.reason == "dag_invalid"
        assert transition.state.stage is Stage.REPLANNING
        assert transition.options == REPLAN_OPTIONS

    def test_a_valid_dag_is_assigned_normally(self):
        transition = advance(
            _at(Stage.DAG_READY),
            Event(kind="assign", provider=AUTHOR, timestamp=_ts(3)),
        )

        assert transition.reason == "assigned"
        assert transition.state.stage is Stage.ASSIGNED

    def test_no_gate_result_carries_the_d1_label(self):
        # D1 has no pass/fail GateResult; a gate_result for it is refused
        transition = advance(
            _at(Stage.DAG_READY),
            Event(kind="gate_result", gate="D1", result=_pass(), timestamp=_ts(3)),
        )

        assert not transition.accepted
        assert transition.reason == "stage_gate_order"


class TestHandoffNormalization:
    @pytest.mark.parametrize("blank", ["", " ", "\t", "\n", "  \n "])
    def test_a_whitespace_only_field_is_missing(self, blank):
        transition = advance(
            _at(Stage.ASSIGNED),
            Event(
                kind="reassign",
                provider="dev-b",
                handoff=_handoff(branch=blank),
                timestamp=_ts(5),
            ),
        )

        assert not transition.accepted
        assert transition.reason == "handoff_incomplete"
        assert "branch" in transition.detail

    def test_a_whitespace_only_file_entry_is_missing(self):
        transition = advance(
            _at(Stage.ASSIGNED),
            Event(
                kind="reassign",
                provider="dev-b",
                handoff=_handoff(files=("   ",)),
                timestamp=_ts(5),
            ),
        )

        assert not transition.accepted
        assert transition.reason == "handoff_incomplete"
        assert "files" in transition.detail

    def test_a_real_value_with_surrounding_space_is_complete(self):
        transition = advance(
            _at(Stage.ASSIGNED),
            Event(
                kind="reassign",
                provider="dev-b",
                handoff=_handoff(branch="  feat/x  "),
                timestamp=_ts(5),
            ),
        )

        assert transition.accepted
        assert transition.reason == "developer_reassigned"

    def test_the_same_developer_cannot_be_reassigned(self):
        transition = advance(
            _at(Stage.ASSIGNED),
            Event(
                kind="reassign",
                provider=AUTHOR,
                handoff=_handoff(),
                timestamp=_ts(5),
            ),
        )

        assert not transition.accepted
        assert transition.reason == "reassignment_same_provider"

    def test_replanning_offers_both_ways_out(self):
        replanning = _reach_replanning(REVIEWER)
        assert replanning.state.stage is Stage.REPLANNING

        resplit = advance(
            replanning.state, Event(kind="replan_decision", option="resplit_task", timestamp=_ts(9))
        )
        assert resplit.reason == "replanned_split"
        assert resplit.state.stage is Stage.PLANNING
        assert resplit.state.fix_rounds == 0
        assert resplit.state.blocking_rounds == 0
        assert resplit.state.primary_reviewer == ""
        assert resplit.state.replan_options == ()

        replaced = advance(
            replanning.state,
            Event(
                kind="replan_decision",
                option="replace_developer",
                provider="dev-b",
                handoff=_handoff(),
                timestamp=_ts(9),
            ),
        )
        assert replaced.reason == "developer_replaced"
        assert replaced.state.stage is Stage.ASSIGNED
        assert replaced.state.developer_provider == "dev-b"
        assert replaced.state.author_provider == AUTHOR
        # a replaced developer starts a fresh cycle, not the old reviewer's count
        assert replaced.state.fix_rounds == 0
        assert replaced.state.blocking_rounds == 0
        assert replaced.state.blocking_reviewer == ""
        assert replaced.state.primary_reviewer == ""
        assert replaced.state.replan_options == ()

    def test_a_replan_option_that_was_not_offered_is_refused(self):
        state = _at(Stage.REPLANNING, replan_options=("resplit_task",))
        transition = advance(
            state,
            Event(
                kind="replan_decision",
                option="replace_developer",
                provider="dev-b",
                handoff=_handoff(),
                timestamp=_ts(9),
            ),
        )

        assert not transition.accepted
        assert transition.reason == "replan_option_unknown"
        assert "replace_developer" in transition.detail
        assert "resplit_task" in transition.detail

    def test_replacing_the_developer_after_a_replan_needs_a_handoff(self):
        replanning = _reach_replanning(REVIEWER)

        transition = advance(
            replanning.state,
            Event(
                kind="replan_decision",
                option="replace_developer",
                provider="dev-b",
                handoff=_handoff(known_limitations=""),
                timestamp=_ts(9)),
        )
        assert not transition.accepted
        assert transition.reason == "handoff_incomplete"
        assert "known_limitations" in transition.detail

    def test_an_unknown_replan_option_is_refused(self):
        transition = advance(
            _at(Stage.REPLANNING),
            Event(kind="replan_decision", option="escalate", timestamp=_ts(9)),
        )

        assert not transition.accepted
        assert transition.reason == "replan_option_unknown"

    def test_a_verdict_that_contradicts_the_result_is_refused(self):
        transition = advance(
            _at(Stage.PRIMARY_REVIEW),
            _review(REVIEWER, _fail(), verdict="approve"),
        )

        assert not transition.accepted
        assert transition.reason == "verdict_result_mismatch"

    def test_a_passing_review_cannot_carry_the_block_verdict(self):
        transition = advance(
            _at(Stage.PRIMARY_REVIEW), _review(REVIEWER, _pass(), verdict="block")
        )

        assert not transition.accepted
        assert transition.reason == "verdict_result_mismatch"

    def test_a_reassignment_without_a_provider_is_refused(self):
        transition = advance(
            _at(Stage.ASSIGNED),
            Event(kind="reassign", handoff=_handoff(), timestamp=_ts(5)),
        )

        assert not transition.accepted
        assert transition.reason == "provider_required"

    def test_a_primary_review_ignores_a_conflict_category(self):
        # A conflict is a secondary-review concept; the primary keeps the BLOCK.
        transition = advance(
            _at(Stage.PRIMARY_REVIEW),
            _review(REVIEWER, _fail(), conflict="security"),
        )

        assert transition.reason == "review_blocked"
        assert transition.state.stage is Stage.FIXING

    def test_an_empty_gate_defaults_to_the_stages_own_gate(self):
        transition = advance(
            _at(Stage.SPEC_REVIEW),
            Event(kind="gate_result", result=_pass(), timestamp=_ts(2)),
        )

        assert transition.accepted
        assert transition.event.gate == "G0"  # type: ignore[union-attr]

    def test_an_unknown_verdict_is_refused(self):
        transition = advance(
            _at(Stage.PRIMARY_REVIEW),
            _review(REVIEWER, _pass(), verdict="lgtm"),
        )

        assert not transition.accepted
        assert transition.reason == "invalid_verdict"
    def test_a_review_without_a_result_is_refused(self):
        transition = advance(
            _at(Stage.PRIMARY_REVIEW),
            Event(kind="primary_review", provider=REVIEWER, timestamp=_ts(7)),
        )

        assert not transition.accepted
        assert transition.reason == "result_required"

    def test_a_review_without_a_provider_is_refused(self):
        transition = advance(
            _at(Stage.PRIMARY_REVIEW), _review("", _pass(), verdict="approve")
        )

        assert not transition.accepted
        assert transition.reason == "provider_required"

    def test_an_assignment_without_a_provider_is_refused(self):
        transition = advance(_at(Stage.DAG_READY), Event(kind="assign", timestamp=_ts(3)))

        assert not transition.accepted
        assert transition.reason == "provider_required"


def _block_and_fix(
    state: StageGateState, reviewer: str
) -> tuple[sg.Transition, sg.Transition]:
    """One BLOCK, then the author fixes and resubmits to the same review."""
    blocked = advance(state, _review(reviewer, _fail()))
    resubmitted = advance(blocked.state, Event(kind="submit_work", timestamp=_ts(8)))
    return blocked, resubmitted


def _reach_replanning(reviewer: str = REVIEWER) -> sg.Transition:
    """Three consecutive BLOCKs from one reviewer; the Planner takes over."""
    state = _at(Stage.PRIMARY_REVIEW)
    for _ in range(2):  # two failed fix rounds
        _, resubmitted = _block_and_fix(state, reviewer)
        state = resubmitted.state
    return advance(state, _review(reviewer, _fail()))


def _canonical(kind: str) -> Event:
    """The most permissive event for ``kind``; legality only, not semantics."""
    values: dict[str, object] = {"kind": kind, "timestamp": _ts(30)}
    if kind == "gate_result":
        values.update(result=_pass())
    elif kind in {"primary_review", "secondary_review"}:
        values.update(provider="rev-x", result=_pass())
    elif kind in {"assign", "reassign"}:
        values.update(provider="dev-b", handoff=_handoff())
    elif kind == "replan_decision":
        values.update(option="resplit_task")
    return Event(**values)  # type: ignore[arg-type]


class TestIllegalTransitions:
    def test_every_undeclared_pair_is_refused_with_a_reason_code(self):
        declared = {rule.key for rule in sg.RULES}
        for stage in Stage:
            for kind in sg.EventKind:
                state = _at(stage)
                transition = advance(state, _canonical(kind.value))
                if stage.terminal:
                    assert transition.reason == "already_terminal", (stage, kind)
                    continue
                if (stage, kind) in declared:
                    assert transition.reason != "stage_gate_order", (stage, kind)
                else:
                    assert transition.reason == "stage_gate_order", (stage, kind)
                    assert transition.state is state
                    assert transition.event is None

    def test_a_refusal_names_the_legal_events_for_the_stage(self):
        state = _at(Stage.IMPLEMENTING)
        transition = advance(
            state,
            Event(
                kind="primary_review",
                provider=REVIEWER,
                result=_pass(),
                timestamp=_ts(7),
            ),
        )

        assert not transition.accepted
        assert transition.reason == "stage_gate_order"
        assert transition.state is state
        assert transition.event is None
        assert "submit_work" in transition.detail

    def test_a_terminal_stage_refuses_every_event_kind(self):
        for kind in sg.EventKind:
            assert advance(_at(Stage.MERGED), _canonical(kind.value)).reason == "already_terminal"
            assert advance(_at(Stage.CANCELLED), _canonical(kind.value)).reason == "already_terminal"

    def test_an_unknown_event_kind_is_refused(self):
        transition = advance(
            _at(Stage.PLANNING), Event(kind="land_the_plane", timestamp=_ts(1))
        )

        assert not transition.accepted
        assert transition.reason == "unknown_event_kind"

    def test_a_missing_timestamp_is_refused(self):
        transition = advance(_at(Stage.PLANNING), Event(kind="spec_submitted"))

        assert not transition.accepted
        assert transition.reason == "timestamp_missing"
        assert transition.event is None

    def test_a_result_that_is_neither_pass_nor_fail_is_refused(self):
        transition = advance(
            _at(Stage.SPEC_REVIEW),
            Event(
                kind="gate_result",
                gate="G0",
                result=GateResult(outcome="maybe"),
                timestamp=_ts(2),
            ),
        )

        assert not transition.accepted
        assert transition.reason == "invalid_result"

    def test_a_gate_result_without_a_result_is_refused(self):
        transition = advance(
            _at(Stage.SPEC_REVIEW),
            Event(kind="gate_result", gate="G0", timestamp=_ts(2)),
        )

        assert not transition.accepted
        assert transition.reason == "result_required"


class TestGlobalTransitions:
    def test_block_and_cancel_are_available_from_every_non_terminal_stage(self):
        for stage in Stage:
            if stage.terminal or stage is Stage.BLOCKED:
                continue
            state = _at(stage)
            blocked = advance(state, Event(kind="block", reason="waiting", timestamp=_ts(20)))
            assert blocked.reason == "operator_blocked", stage
            assert blocked.state.stage is Stage.BLOCKED
            assert blocked.state.blocked_from == stage.value

            cancelled = advance(state, Event(kind="cancel", reason="superseded", timestamp=_ts(21)))
            assert cancelled.reason == "cancelled", stage
            assert cancelled.state.stage is Stage.CANCELLED

    def test_an_already_blocked_task_refuses_a_second_block(self):
        blocked = advance(_at(Stage.ASSIGNED), Event(kind="block", timestamp=_ts(20)))

        transition = advance(blocked.state, Event(kind="block", timestamp=_ts(21)))

        assert not transition.accepted
        assert transition.reason == "already_blocked"

    def test_resume_without_a_recorded_origin_returns_to_planning(self):
        resumed = advance(_at(Stage.BLOCKED), Event(kind="resume", timestamp=_ts(22)))

        assert resumed.reason == "resumed"
        assert resumed.state.stage is Stage.PLANNING

    def test_a_blocked_task_can_still_be_cancelled(self):
        blocked = advance(_at(Stage.ASSIGNED), Event(kind="block", timestamp=_ts(20)))
        cancelled = advance(blocked.state, Event(kind="cancel", timestamp=_ts(21)))

        assert cancelled.reason == "cancelled"
        assert cancelled.state.stage is Stage.CANCELLED


class TestAuditEvent:
    def test_the_event_dict_is_the_field_whitelist(self):
        _, transitions = _walk(initial_state(TASK), _happy_events(True))

        for transition in transitions:
            data = transition.event.to_dict()  # type: ignore[union-attr]
            assert tuple(data) == EVENT_FIELDS
            assert set(data) == set(EVENT_FIELDS)

    def test_the_event_keeps_no_prompt_token_or_diff_content(self):
        secret = "PROMPT: rewrite the acceptance criteria"
        diff = "diff --git a/secrets.py b/secrets.py"
        transition = advance(
            _at(Stage.SPEC_REVIEW),
            Event(
                kind="gate_result",
                gate="G0",
                result=GateResult(outcome=FAIL, reason=secret, evidence=(diff, "token=abc")),
                timestamp=_ts(2),
            ),
        )

        assert transition.accepted
        assert transition.event.reason == "spec_rejected"  # stable code, not the caller's text
        payload = repr(transition.event.to_dict())
        assert secret not in payload
        assert diff not in payload
        assert "token" not in payload
        assert secret not in repr(transition.state.to_dict())

    def test_the_handoff_is_referenced_not_embedded(self):
        transition = advance(
            _at(Stage.ASSIGNED),
            Event(
                kind="reassign",
                provider="dev-b",
                handoff=_handoff(),
                timestamp=_ts(5),
            ),
        )

        data = transition.event.to_dict()
        assert data["handoff_ref"] == _handoff().reference()
        assert _handoff().branch not in repr(data)
        assert _handoff().commit not in repr(data)
        assert _handoff().results not in repr(data)

    def test_a_caller_supplied_reference_wins(self):
        transition = advance(
            _at(Stage.ASSIGNED),
            Event(
                kind="reassign",
                provider="dev-b",
                handoff=_handoff(),
                handoff_ref="ticket-event-7",
                timestamp=_ts(5),
            ),
        )

        assert transition.event.handoff_ref == "ticket-event-7"  # type: ignore[union-attr]

    def test_the_event_carries_the_result_and_the_provider(self):
        _, transitions = _walk(initial_state(TASK), _happy_events(True))
        for transition in transitions:
            data = transition.event.to_dict()  # type: ignore[union-attr]
            assert data["result"] in ("", PASS, FAIL)
            assert isinstance(data["provider"], str)

        failed = advance(
            _at(Stage.SPEC_REVIEW),
            Event(kind="gate_result", gate="G0", result=_fail(), timestamp=_ts(2)),
        )
        assert failed.event.result == FAIL  # type: ignore[union-attr]
        assert failed.event.provider == ""  # type: ignore[union-attr]

        reviewed = advance(
            _at(Stage.PRIMARY_REVIEW), _review(REVIEWER, _pass(), verdict="approve")
        )
        assert reviewed.event.result == PASS  # type: ignore[union-attr]
        assert reviewed.event.provider == REVIEWER  # type: ignore[union-attr]

    def test_the_event_carries_the_timestamp_the_caller_supplied(self):
        transition = advance(
            _at(Stage.PLANNING), Event(kind="spec_submitted", timestamp="2026-02-03T04:05:06Z")
        )

        assert transition.event.timestamp == "2026-02-03T04:05:06Z"  # type: ignore[union-attr]

    def test_a_refusal_produces_no_event(self):
        transition = advance(
            _at(Stage.PRIMARY_REVIEW, risk_bearing=True),
            _review(REVIEWER, _pass(), verdict="merge"),
        )

        assert not transition.accepted
        assert transition.event is None


class TestSerialization:
    @pytest.mark.parametrize("stage", list(Stage))
    def test_the_state_roundtrips_at_every_stage(self, stage):
        state = replace(
            _at(stage),
            handoff=_handoff(),
            handoff_ref="handoff:deadbeef",
            replan_options=REPLAN_OPTIONS,
            fix_rounds=2,
            blocking_rounds=1,
            return_stage="primary_review",
            blocked_from="integration_checks",
            primary_verdict="approve",
            primary_passed=True,
            secondary_passed=True,
            secondary_skipped=False,
        )

        restored = StageGateState.from_dict(state.to_dict())

        assert restored == state
        assert restored.to_dict() == state.to_dict()

    def test_the_roundtrip_survives_a_full_walk(self):
        state, _ = _walk(initial_state(TASK, risk_bearing=True), _happy_events(True))

        assert StageGateState.from_dict(state.to_dict()) == state

    def test_from_dict_rejects_an_unknown_stage(self):
        with pytest.raises(ValueError):
            StageGateState.from_dict({"task_id": TASK, "stage": "limbo"})

    def test_a_restored_state_keeps_deciding(self):
        state, _ = _walk(initial_state(TASK, risk_bearing=True), _happy_events(True)[:6])
        restored = StageGateState.from_dict(state.to_dict())

        transition = advance(
            restored,
            Event(
                kind="primary_review",
                provider=REVIEWER,
                result=_pass(),
                verdict="approve",
                timestamp=_ts(7),
            ),
        )

        assert transition.accepted
        assert transition.state.stage is Stage.SECONDARY_REVIEW


class TestDeterminism:
    def test_the_same_event_sequence_gives_the_same_answer(self):
        first = _walk(initial_state(TASK), _happy_events(True))
        second = _walk(initial_state(TASK), _happy_events(True))

        assert [item.event.to_dict() for item in first[1]] == [  # type: ignore[union-attr]
            item.event.to_dict() for item in second[1]  # type: ignore[union-attr]
        ]
        assert first[0].to_dict() == second[0].to_dict()

    def test_refusals_are_reproducible(self):
        state = _at(Stage.IMPLEMENTING)
        event = Event(
            kind="reassign", provider="dev-b", handoff=_handoff(), timestamp=_ts(5)
        )

        first = advance(state, event)
        second = advance(state, event)

        assert (first.accepted, first.reason, first.detail) == (
            second.accepted,
            second.reason,
            second.detail,
        )
        assert first.state is second.state

    def test_a_refusal_in_the_middle_does_not_change_the_outcome(self):
        events = list(_happy_events(True))
        events.insert(
            4,
            Event(
                kind="primary_review",
                provider=REVIEWER,
                result=_pass(),
                timestamp=_ts(45),
            ),
        )
        state = initial_state(TASK)
        for event in events:
            transition = advance(state, event)
            if not transition.accepted:
                assert transition.reason == "stage_gate_order"
                assert transition.state is state
            else:
                state = transition.state

        assert state.stage is Stage.MERGED


class TestDocumentation:
    def test_the_transition_table_matches_the_document(self):
        document = DOCUMENT.read_text()

        for line in sg.render_transition_table().splitlines():
            assert line in document, line

    def test_the_reason_table_matches_the_document(self):
        document = DOCUMENT.read_text()

        for line in sg.render_reason_table().splitlines():
            assert line in document, line

    def test_the_document_carries_the_required_sections(self):
        text = DOCUMENT.read_text()

        assert "```mermaid" in text
        for heading in ("Transition table", "Reason codes", "v2 integration"):
            assert heading in text

    def test_the_document_describes_the_v2_hooks(self):
        text = DOCUMENT.read_text()

        # where the scheduler calls advance, and where the events are written
        assert "Where the scheduler calls `advance`" in text
        assert "Where events are written" in text
        assert "def advance(state: StageGateState, event: Event) -> Transition" in text
        assert "GateEvent.to_dict()" in text
        # the fix-round counting rule is documented, not just implemented
        assert "BLOCK #1" in text
        assert "state.replan_options" in text
        # the mermaid diagram covers the resume edge from blocked
        assert "note right of blocked" in text
        # the adjudication channel is documented as separate from G6
        assert "awaiting_adjudication" in text
        assert "D1 is a milestone" in text
        assert "contributors" in text


class TestPurity:
    def test_the_module_reads_no_clock_and_does_no_io(self):
        assert FORBIDDEN.search(MODULE.read_text()) is None

    def test_the_module_does_not_import_the_role_policy(self):
        imported = _imported_modules()
        assert imported <= {
            "__future__",
            "dataclasses",
            "enum",
            "hashlib",
            "json",
            "typing",
        }

    def test_the_rules_and_handlers_are_in_parity(self):
        assert set(sg._RULES_BY_KEY) == set(sg._HANDLERS)

    def test_every_documented_reason_code_is_emitted_by_a_handler(self):
        source = MODULE.read_text()
        # strip the documentation table so the code must appear in a handler
        table = source.index("REASON_CODES: tuple")
        handlers = source[:table] + source[source.index("def render_reason_table") :]
        codes = [code for code, _ in sg.REASON_CODES]

        assert len(codes) == len(set(codes))
        for code in codes:
            assert f'"{code}"' in handlers, code

    def test_the_stage_vocabulary_is_fixed(self):
        assert [stage.value for stage in Stage] == [
            "planning",
            "spec_review",
            "dag_ready",
            "assigned",
            "implementing",
            "focused_tests",
            "primary_review",
            "fixing",
            "secondary_review",
            "integration_checks",
            "awaiting_human",
            "awaiting_adjudication",
            "merged",
            "replanning",
            "blocked",
            "cancelled",
        ]

    def test_the_handoff_requires_the_six_documented_fields(self):
        assert Handoff.REQUIRED == (
            "branch",
            "commit",
            "files",
            "test_command",
            "results",
            "known_limitations",
        )
