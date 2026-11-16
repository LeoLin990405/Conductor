"""Contracts for the deep refactor's new state (goals, plans, runs, memory).

The migration CHECK constraints spell the same vocabularies out in SQL (they
must run without the application stack). These tests pin the Python side and the
migration schemas together, matching the pattern ``test_status.py`` uses for the
run ledger: changing one end without the other fails the default unit suite.
"""

from __future__ import annotations

import pytest

from src.server.contracts import refactor
from src.server.contracts.refactor import (
    EVENT_TYPES,
    GOAL_STATUSES,
    GOAL_TERMINAL_STATUSES,
    GOAL_TRANSITIONS,
    MEMORY_ENTRY_STATUSES,
    MEMORY_PROPOSAL_TYPES,
    MEMORY_TIERS,
    PERMISSION_MODES,
    PLAN_STATUSES,
    PLAN_TRANSITIONS,
    SUBAGENT_CONTRACT_VERSION,
    VERIFIER_VERDICTS,
    ContractError,
    GoalSpec,
    ProposedMemory,
    SubagentResult,
    ensure_event_type,
    ensure_goal_transition,
    ensure_plan_transition,
)


class TestGoalTransitions:
    def test_every_status_has_a_transition_row(self):
        assert set(GOAL_TRANSITIONS) == set(GOAL_STATUSES)

    def test_active_can_pause_and_terminate(self):
        for target in ("paused", "completed", "cancelled", "blocked"):
            ensure_goal_transition("active", target)

    def test_terminal_goals_never_move_again(self):
        for terminal in GOAL_TERMINAL_STATUSES:
            assert GOAL_TRANSITIONS[terminal] == frozenset()
            with pytest.raises(ContractError, match="illegal goal transition"):
                ensure_goal_transition(terminal, "active")

    def test_paused_and_blocked_can_resume_or_cancel(self):
        for status in ("paused", "blocked"):
            assert GOAL_TRANSITIONS[status] == frozenset({"active", "cancelled"})

    def test_same_status_is_idempotent(self):
        # Event retries write the same status again; rejecting them would make
        # every writer read-before-write for no gain.
        for status in GOAL_STATUSES:
            ensure_goal_transition(status, status)

    def test_unknown_status_is_rejected(self):
        with pytest.raises(ContractError, match="unknown goal status"):
            ensure_goal_transition("active", "done")


class TestPlanTransitions:
    def test_every_status_has_a_transition_row(self):
        assert set(PLAN_TRANSITIONS) == set(PLAN_STATUSES)

    def test_review_path(self):
        ensure_plan_transition("draft", "awaiting_approval")
        ensure_plan_transition("awaiting_approval", "approved")
        ensure_plan_transition("awaiting_approval", "rejected")

    def test_supersede_is_terminal(self):
        ensure_plan_transition("approved", "superseded")
        with pytest.raises(ContractError):
            ensure_plan_transition("superseded", "draft")

    def test_approved_plan_cannot_be_rejected_later(self):
        with pytest.raises(ContractError, match="illegal plan transition"):
            ensure_plan_transition("approved", "rejected")


class TestGoalSpec:
    def test_round_trip(self):
        spec = GoalSpec(
            title="Research the Q3 roadmap",
            objective="Summarize the last four quarterly reports",
            stop_conditions=("each quarter has a citation",),
            budget={"max_turns": 8},
            verifier_config={"model": "background"},
        )
        record = spec.to_record()
        assert GoalSpec.from_mapping(record) == spec

    @pytest.mark.parametrize(
        "field, value, message",
        [
            ("title", "", "title"),
            ("objective", "  ", "objective"),
            ("stop_conditions", (), "stop condition"),
            ("stop_conditions", ("  ",), "non-empty strings"),
        ],
    )
    def test_required_fields(self, field, value, message):
        base = {
            "title": "t",
            "objective": "o",
            "stop_conditions": ("done",),
        }
        base[field] = value
        with pytest.raises(ContractError, match=message):
            GoalSpec.from_mapping(base)

    def test_budget_and_verifier_config_must_be_mappings(self):
        with pytest.raises(ContractError, match="budget"):
            GoalSpec.from_mapping(
                {"title": "t", "objective": "o", "stop_conditions": ["c"], "budget": []}
            )
        with pytest.raises(ContractError, match="verifier_config"):
            GoalSpec.from_mapping(
                {
                    "title": "t",
                    "objective": "o",
                    "stop_conditions": ["c"],
                    "verifier_config": [],
                }
            )


class TestSubagentResult:
    def _result(self, **overrides) -> SubagentResult:
        values = {
            "contract_version": SUBAGENT_CONTRACT_VERSION,
            "task_id": "task-1",
            "status": "completed",
            "summary": "Found three reports and two charts.",
            "findings": ("report 2025Q3 mentions X",),
            "artifact_ids": ("artifact-1",),
            "evidence": ("thread://turn-3",),
            "proposed_memories": (
                ProposedMemory(type="project", text="Reports live under reports/"),
            ),
            "open_questions": ("confirm the accounting standard",),
        }
        values.update(overrides)
        return SubagentResult(**values)

    def test_round_trip(self):
        result = self._result()
        assert SubagentResult.from_mapping(result.to_record()) == result

    def test_contract_version_must_match(self):
        with pytest.raises(ContractError, match="contract version"):
            self._result(contract_version=0)

    def test_status_must_be_terminal(self):
        with pytest.raises(ContractError, match="unknown subagent run status"):
            self._result(status="in_progress")

    def test_summary_is_required(self):
        with pytest.raises(ContractError, match="summary"):
            self._result(summary="   ")

    def test_task_id_is_required(self):
        with pytest.raises(ContractError, match="task id"):
            self._result(task_id="")

    def test_entries_must_be_non_empty_strings(self):
        with pytest.raises(ContractError, match="findings"):
            self._result(findings=("",))

    def test_unknown_proposal_type_rejected(self):
        with pytest.raises(ContractError, match="proposal type"):
            ProposedMemory(type="secret", text="x")  # type: ignore[arg-type]

    def test_proposal_text_bounded(self):
        with pytest.raises(ContractError, match="too long"):
            ProposedMemory(type="user", text="x" * 4001)

    def test_correction_requires_target_and_reason(self):
        with pytest.raises(ContractError, match="replacement key and reason"):
            ProposedMemory(type="user", text="fact", change_kind="correction")

    def test_once_is_dropped_when_reading_a_durable_contract(self):
        result = self._result()
        record = result.to_record()
        record["proposed_memories"].append(
            {"type": "feedback", "text": "only for this turn", "change_kind": "once"}
        )
        restored = SubagentResult.from_mapping(record)
        assert len(restored.proposed_memories) == 1

    def test_proposal_types_match_subagent_prompt_vocabulary(self):
        # prompts/templates/subagents/memory.md.j2 asks for user/feedback/
        # project/reference; the contract is the machine-readable copy.
        assert MEMORY_PROPOSAL_TYPES == ("user", "feedback", "project", "reference")

    def test_policy_metadata_round_trips_and_nested_secret_is_preserved(self):
        proposal = ProposedMemory(
            type="user",
            text="Keep responses concise.",
            metadata={
                "sensitivity": "public",
                "metadata": {"sensitivity": "secret"},
                "applies_when": "writing a response",
            },
        )
        record = proposal.to_record()
        assert record["metadata"] == {
            "sensitivity": "secret",
            "applies_when": "writing a response",
        }
        restored = SubagentResult.from_mapping(
            {
                "contract_version": 1,
                "task_id": "task-1",
                "status": "completed",
                "summary": "Done.",
                "proposed_memories": [record],
            }
        )
        assert restored.proposed_memories[0].metadata["sensitivity"] == "secret"

    def test_typed_constraint_metadata_survives_contract_round_trip(self):
        proposal = ProposedMemory(
            type="user",
            text="Do not use shell execution.",
            metadata={
                "kind": "constraint",
                "denied_tools": [{"type": "tool", "name": "Bash"}],
                "valid_from": "2026-11-01",
            },
        )
        record = proposal.to_record()
        assert record["metadata"]["denied_tools"] == [
            {"type": "tool", "name": "Bash"}
        ]
        restored = SubagentResult.from_mapping(
            {
                "contract_version": 1,
                "task_id": "task-1",
                "status": "completed",
                "summary": "Done.",
                "proposed_memories": [record],
            }
        )
        assert restored.proposed_memories[0].metadata["kind"] == "constraint"


class TestEventTypes:
    def test_vocabulary_is_closed(self):
        with pytest.raises(ContractError, match="unknown event type"):
            ensure_event_type("goal.exploded")

    def test_known_types_pass(self):
        for event_type in EVENT_TYPES:
            ensure_event_type(event_type)
        assert len(EVENT_TYPES) == len(set(EVENT_TYPES))


class TestPinnedVocabularies:
    def test_goal_statuses(self):
        assert GOAL_STATUSES == ("active", "paused", "completed", "cancelled", "blocked")

    def test_plan_statuses(self):
        assert PLAN_STATUSES == (
            "draft",
            "awaiting_approval",
            "approved",
            "rejected",
            "superseded",
        )

    def test_permission_modes(self):
        assert PERMISSION_MODES == ("manual", "auto", "skip")

    def test_verifier_verdicts(self):
        assert VERIFIER_VERDICTS == ("satisfied", "unsatisfied", "unknown")

    def test_memory_tiers(self):
        assert MEMORY_TIERS == ("user", "project", "workspace", "session", "task")

    def test_memory_entry_statuses(self):
        assert MEMORY_ENTRY_STATUSES == ("active", "superseded", "proposed", "rejected")

    def test_terminal_goals_are_a_subset(self):
        assert GOAL_TERMINAL_STATUSES <= set(GOAL_STATUSES)

    def test_task_statuses(self):
        assert refactor.TASK_STATUSES == (
            "pending",
            "in_progress",
            "completed",
            "blocked",
            "cancelled",
        )

    def test_task_sources(self):
        assert refactor.TASK_SOURCES == ("todo", "subagent")


def test_module_exports_the_contract_version():
    assert refactor.SUBAGENT_CONTRACT_VERSION == 1
