"""Every row of the Router's deterministic next-action table.

The table is a function of three independent signals: the completion status the
Worker claimed, the system-check verdict, and the attention reason (or a free
question). These tests pin one action per combination, the ready/open-question
guard, and the legacy adapter the host uses while its call sites move over.
"""

from __future__ import annotations

import ast
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from src.server.services.autonomy import router_decision
from src.server.services.autonomy.completion import (
    COMMIT_DELIVERY_VERDICTS,
    COMPLETION_NO_CHANGE,
    COMPLETION_REPORT_STATUSES,
    COMPLETION_UNVERIFIED,
    settle_native_completion,
)
from src.server.services.autonomy.router_decision import (
    ATTENTION_REASONS,
    COMPLETION_ATTENTION_REASONS,
    DECISION_TABLE,
    GIT_ATTENTION_REASONS,
    OPEN_QUESTION,
    ROUTER_DECISION_TABLE,
    Action,
    RouterDecision,
    decide_legacy_action,
    decide_next_action,
    legacy_next_action,
)

ATTENTION_INPUTS = (None, *sorted(ATTENTION_REASONS), OPEN_QUESTION)
#: The completion contract for the exhaustive matrix, kept independent of the
#: module's table: a cell's expected result comes from the claim's settled
#: state, not from ``decide_next_action``. This is what catches a cell routed to
#: the wrong-but-still-valid action.
_CLAIM_STATE = {
    "done": "ready",
    "partial": "waiting",
    "blocked": "blocked",
    "needs_review": "waiting",
    "needs_split": "waiting",
}
_STATE_ACTION = {
    "ready": Action.DELIVER_TO_USER,
    "waiting": Action.SEND_TO_REVIEW,
    "blocked": Action.ASK_HUMAN,
    "failed": Action.REDISPATCH,
    "dropped": Action.DELIVER_TO_USER,
}
_REFUTING_VERDICTS = frozenset({"missing", "unknown"})


def _expected_state(status: str, system_check: str, attention: str | None) -> str:
    state = _CLAIM_STATE[status]
    if state != "ready":
        return state
    if system_check in _REFUTING_VERDICTS:
        # A completion-check attention reports "review the run"; its missing
        # commit is the fact under review, not a contradicted delivery.
        state = (
            "waiting" if attention in COMPLETION_ATTENTION_REASONS else "blocked"
        )
    # ``unverified`` alone does not refute the claim: a done report that lists
    # commits settles ``ready`` with no attention reason. The bare-done case is
    # carried by the ``completion_unverified`` attention below.
    if attention in GIT_ATTENTION_REASONS and state in {"ready", "dropped"}:
        state = "blocked"
    elif attention in COMPLETION_ATTENTION_REASONS and state == "ready":
        state = "waiting"
    return state


def _expected_cell(
    status: str, system_check: str, attention: str | None
) -> tuple[Action, str, str]:
    """The one action, rule and settled state every matrix cell must produce."""
    state = _expected_state(status, system_check, attention)
    if status == "needs_split":
        return Action.OPEN_CHILD_TICKET, "needs_split", state
    if attention == OPEN_QUESTION:
        if state == "ready":
            raise ValueError("a ready settlement cannot carry an open question")
        return Action.ASK_HUMAN, "open_question", state
    return _STATE_ACTION[state], state, state


@pytest.mark.parametrize("status", sorted(COMPLETION_REPORT_STATUSES))
@pytest.mark.parametrize("system_check", COMMIT_DELIVERY_VERDICTS)
@pytest.mark.parametrize(
    "attention",
    ATTENTION_INPUTS,
    ids=lambda value: "none" if value is None else value,
)
def test_every_status_verdict_and_attention_row_has_one_action(
    status: str, system_check: str, attention: str | None
) -> None:
    try:
        expected_action, expected_rule, expected_state = _expected_cell(
            status, system_check, attention
        )
    except ValueError:
        with pytest.raises(ValueError, match="ready settlement cannot carry"):
            decide_next_action(
                status=status,
                system_check=system_check,
                attention=attention,
            )
        return

    decision = decide_next_action(
        status=status,
        system_check=system_check,
        attention=attention,
    )

    assert isinstance(decision, RouterDecision)
    assert decision.action is expected_action
    assert decision.rule == expected_rule
    assert decision.state == expected_state
    assert decision.status == status
    assert decision.system_check == system_check


@pytest.mark.parametrize("speculative", [False, True])
@pytest.mark.parametrize("system_check", COMMIT_DELIVERY_VERDICTS)
@pytest.mark.parametrize(
    "attention",
    ATTENTION_INPUTS,
    ids=lambda value: "none" if value is None else value,
)
def test_needs_split_always_opens_a_child_ticket(
    speculative: bool, system_check: str, attention: str | None
) -> None:
    decision = decide_next_action(
        status="needs_split",
        system_check=system_check,
        attention=attention,
        speculative=speculative,
    )

    assert decision.action is Action.OPEN_CHILD_TICKET


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "ready", "system_check": "delivered", "attention": OPEN_QUESTION},
        {"status": "done", "system_check": "delivered", "attention": OPEN_QUESTION},
        {"status": "done", "system_check": "not_checked", "attention": OPEN_QUESTION},
        {"status": "done", "system_check": "not_checked", "open_question": True},
        {
            "completion_status": "done",
            "system_check": "delivered",
            "attention": OPEN_QUESTION,
        },
        {"completion_status": "ready", "attention": OPEN_QUESTION},
        {"reconciled_state": "ready", "attention": OPEN_QUESTION},
    ],
)
def test_ready_cannot_carry_an_open_question(kwargs: dict[str, object]) -> None:
    """Every spelling of a settled ready report refuses an open question."""
    with pytest.raises(ValueError, match="ready settlement cannot carry"):
        decide_next_action(**kwargs)


@pytest.mark.parametrize(
    ("status", "attention", "action", "state"),
    [
        ("ready", "uncommitted_changes", Action.ASK_HUMAN, "blocked"),
        ("ready", "git_status_unavailable", Action.ASK_HUMAN, "blocked"),
        ("done", "uncommitted_changes", Action.ASK_HUMAN, "blocked"),
        ("done", "completion_no_change", Action.SEND_TO_REVIEW, "waiting"),
        ("done", "completion_report_missing", Action.SEND_TO_REVIEW, "waiting"),
        ("done", "completion_unverified", Action.SEND_TO_REVIEW, "waiting"),
    ],
)
def test_attention_reasons_are_not_questions(
    status: str, attention: str, action: Action, state: str
) -> None:
    """A ready claim plus an attention fact is routed, never rejected."""
    decision = decide_next_action(status=status, attention=attention)

    assert decision.action is action
    assert decision.state == state
    assert decision.open_question is False


def test_confirmed_clean_no_change_waits_for_review() -> None:
    """The no-change outcome is a review state, not a delivery refusal."""
    decision = decide_next_action(
        status="done",
        system_check="missing",
        attention=COMPLETION_NO_CHANGE,
    )

    assert decision.action is Action.SEND_TO_REVIEW
    assert decision.state == "waiting"
    assert decision.rule == "waiting"


@pytest.mark.parametrize("attention", sorted(COMPLETION_ATTENTION_REASONS))
def test_a_completion_attention_is_a_review_outcome_not_a_delivery_refusal(
    attention: str,
) -> None:
    decision = decide_next_action(
        status="done",
        system_check="missing",
        attention=attention,
    )

    assert decision.action is Action.SEND_TO_REVIEW
    assert decision.state == "waiting"


def test_no_change_route_matches_settle_native_completion() -> None:
    report_text = json.dumps(
        {
            "status": "done",
            "summary": "Nothing needed changing.",
            "artifacts": {"commits": [], "files": [], "pr": None},
            "verification": [],
            "next": "",
            "handbacks": [],
        }
    )
    outcome = settle_native_completion(
        report_text,
        git_summary="",
        commit_delivery="missing",
        baseline_clean=True,
    )

    assert outcome["state"] == "waiting"
    assert outcome["attention_reason"] == COMPLETION_NO_CHANGE

    decision = decide_next_action(
        status=outcome["completion"].status,
        system_check="missing",
        attention=outcome["attention_reason"],
    )
    assert decision.action is Action.SEND_TO_REVIEW


def test_unverified_done_with_commits_stays_ready_and_delivers() -> None:
    """Regression: ``unverified`` alone must not discard a committed done claim.

    ``settle_native_completion`` settles a done report that lists commits as
    ``ready`` with no attention reason even when the delivery origin could not
    be verified. The Router must keep that settlement, not send it to review.
    """
    report_text = json.dumps(
        {
            "status": "done",
            "summary": "Implemented the change.",
            "artifacts": {
                "commits": ["abc1234"],
                "files": ["src/a.py"],
                "pr": None,
            },
            "verification": [],
            "next": "",
            "handbacks": [],
        }
    )
    outcome = settle_native_completion(
        report_text,
        git_summary="",
        commit_delivery="unverified",
        baseline_clean=True,
    )

    assert outcome["state"] == "ready"
    assert outcome["attention_reason"] is None

    decision = decide_next_action(
        status=outcome["completion"].status,
        system_check="unverified",
        attention=outcome["attention_reason"],
    )

    assert decision.action is Action.DELIVER_TO_USER
    assert decision.state == "ready"
    assert decision.rule == "ready"


def test_unverified_done_without_commits_still_routes_to_review() -> None:
    """The bare-done unverified case keeps its ``completion_unverified`` review."""
    report_text = json.dumps(
        {
            "status": "done",
            "summary": "Implemented the change.",
            "artifacts": {"commits": [], "files": ["src/a.py"], "pr": None},
            "verification": [],
            "next": "",
            "handbacks": [],
        }
    )
    outcome = settle_native_completion(
        report_text,
        git_summary="",
        commit_delivery="unverified",
        baseline_clean=True,
    )

    assert outcome["state"] == "waiting"
    assert outcome["attention_reason"] == COMPLETION_UNVERIFIED

    decision = decide_next_action(
        status=outcome["completion"].status,
        system_check="unverified",
        attention=outcome["attention_reason"],
    )

    assert decision.action is Action.SEND_TO_REVIEW
    assert decision.state == "waiting"
    assert decision.rule == "waiting"


def test_speculative_blocked_settlement_stays_on_review() -> None:
    """A settlement result must not undo speculative normalization.

    ``settle_native_completion`` records ``blocked`` for a speculative blocked
    plan. Passing that result as ``system_check`` used to replace the waiting
    normalization and ask a person. The same claim with a verdict, and the
    legacy adapter, both send the plan to review.
    """
    report_text = json.dumps(
        {
            "status": "blocked",
            "summary": "Need a decision before the plan can continue.",
            "artifacts": {"commits": [], "files": [], "pr": None},
            "verification": [],
            "next": "Which branch should I use?",
            "handbacks": [],
        }
    )
    outcome = settle_native_completion(report_text, speculative=True)

    assert outcome["state"] == "blocked"
    assert outcome["attention_reason"] is None
    assert outcome["completion"].status == "blocked"
    assert outcome["reconciled"].state == "blocked"

    routed = [
        decide_next_action(
            status=outcome["completion"].status,
            system_check=system_check,
            attention=outcome["attention_reason"],
            speculative=True,
        )
        for system_check in (
            outcome,
            outcome["reconciled"],
            outcome["state"],
            "not_checked",
        )
    ]
    routed.append(
        decide_next_action(system_check=outcome, speculative=True)
    )
    for decision in routed:
        assert decision.action is Action.SEND_TO_REVIEW
        assert decision.state == "waiting"

    legacy = decide_legacy_action(
        outcome["state"], outcome["attention_reason"], True
    )
    assert legacy.action is Action.SEND_TO_REVIEW


def test_attention_is_separate_from_status_and_changes_the_action() -> None:
    quiet = decide_next_action(status="done", system_check="delivered")
    dirty = decide_next_action(
        status="done",
        system_check="delivered",
        attention="uncommitted_changes",
    )
    unverified = decide_next_action(
        status="done",
        system_check="delivered",
        attention="completion_unverified",
    )

    assert quiet.action is Action.DELIVER_TO_USER
    assert dirty.action is Action.ASK_HUMAN
    assert unverified.action is Action.SEND_TO_REVIEW


@pytest.mark.parametrize(
    ("status", "action"),
    [
        ("done", Action.DELIVER_TO_USER),
        ("partial", Action.SEND_TO_REVIEW),
        ("blocked", Action.ASK_HUMAN),
        ("needs_review", Action.SEND_TO_REVIEW),
        ("needs_split", Action.OPEN_CHILD_TICKET),
    ],
)
def test_each_completion_status_has_a_claim_action(
    status: str, action: Action
) -> None:
    decision = decide_next_action(status=status, system_check="delivered")

    assert decision.action is action


@pytest.mark.parametrize(
    ("status", "action"),
    [
        ("done", Action.DELIVER_TO_USER),
        ("partial", Action.SEND_TO_REVIEW),
        ("blocked", Action.SEND_TO_REVIEW),
        ("needs_review", Action.SEND_TO_REVIEW),
        ("needs_split", Action.OPEN_CHILD_TICKET),
    ],
)
def test_speculative_plans_never_complete_work(
    status: str, action: Action
) -> None:
    decision = decide_next_action(
        status=status,
        system_check="not_checked",
        speculative=True,
    )

    assert decision.action is action


def test_attention_reasons_are_split_into_git_and_completion_facts() -> None:
    assert GIT_ATTENTION_REASONS == {
        "uncommitted_changes",
        "git_status_unavailable",
    }
    assert COMPLETION_ATTENTION_REASONS == {
        "completion_report_missing",
        "completion_no_change",
        "completion_unverified",
    }
    assert GIT_ATTENTION_REASONS.isdisjoint(COMPLETION_ATTENTION_REASONS)
    assert ATTENTION_REASONS == (
        GIT_ATTENTION_REASONS | COMPLETION_ATTENTION_REASONS
    )


def test_action_values_are_the_wire_vocabulary() -> None:
    assert [action.value for action in Action] == [
        "deliver_to_user",
        "send_to_review",
        "open_child_ticket",
        "redispatch",
        "ask_human",
    ]
    assert Action.deliver_to_user is Action.DELIVER_TO_USER
    assert Action.ask_human is Action.ASK_HUMAN
    assert DECISION_TABLE[-1].name == "fallback"
    assert DECISION_TABLE[-1].action is Action.ASK_HUMAN
    assert ROUTER_DECISION_TABLE is DECISION_TABLE


def test_every_action_appears_in_the_table() -> None:
    assert {rule.action for rule in DECISION_TABLE} == set(Action)


def test_decision_is_frozen_and_repeatable() -> None:
    first = decide_next_action(
        status="needs_review",
        system_check="unverified",
        attention="completion_unverified",
    )
    second = decide_next_action(
        status="needs_review",
        system_check="unverified",
        attention="completion_unverified",
    )

    assert first == second
    assert first.action is Action.SEND_TO_REVIEW
    with pytest.raises(FrozenInstanceError):
        first.action = Action.ASK_HUMAN  # type: ignore[misc]


@pytest.mark.parametrize(
    ("state", "attention", "speculative", "action"),
    [
        ("ready", None, False, Action.DELIVER_TO_USER),
        ("dropped", None, False, Action.DELIVER_TO_USER),
        ("dropped", "completion_no_change", False, Action.DELIVER_TO_USER),
        ("dropped", "uncommitted_changes", False, Action.ASK_HUMAN),
        ("waiting", "completion_unverified", False, Action.SEND_TO_REVIEW),
        ("waiting", "git_status_unavailable", False, Action.SEND_TO_REVIEW),
        ("blocked", None, False, Action.ASK_HUMAN),
        ("failed", None, False, Action.REDISPATCH),
        ("failed", "completion_report_missing", False, Action.REDISPATCH),
        ("ready", None, True, Action.SEND_TO_REVIEW),
        ("dropped", "uncommitted_changes", True, Action.SEND_TO_REVIEW),
        ("blocked", None, True, Action.SEND_TO_REVIEW),
        ("failed", None, True, Action.REDISPATCH),
    ],
)
def test_legacy_adapter_maps_the_old_report_tuple(
    state: str, attention: str | None, speculative: bool, action: Action
) -> None:
    decision = decide_legacy_action(state, attention, speculative)

    assert decision.action is action
    assert decision.state == state
    assert decision.speculative is speculative
    assert legacy_next_action(state, attention, speculative) is action


def test_legacy_adapter_accepts_the_tuple_spelling() -> None:
    decision = decide_legacy_action(("waiting", "completion_unverified", False))

    assert decision.action is Action.SEND_TO_REVIEW
    assert decision.state == "waiting"
    assert decision.attention == "completion_unverified"


def test_legacy_adapter_refuses_unknown_vocabulary() -> None:
    with pytest.raises(ValueError, match="unknown reconciled state"):
        decide_legacy_action("almost_ready")
    with pytest.raises(ValueError, match="unknown attention reason"):
        decide_legacy_action("waiting", "needs_a_coffee")


def test_module_has_no_io_or_database_imports() -> None:
    tree = ast.parse(Path(router_decision.__file__).read_text())
    allowed = {
        "__future__",
        "dataclasses",
        "enum",
        "typing",
        "src.server.services.autonomy.completion",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert imported <= allowed, imported - allowed


@pytest.mark.parametrize("attention", sorted(GIT_ATTENTION_REASONS))
def test_a_git_attention_without_a_settled_state_asks_a_person(attention) -> None:
    """No status and no settled verdict: a Git fact still stops delivery."""
    decision = decide_next_action(attention=attention)

    assert decision.rule == "git_attention_unsettled"
    assert decision.action is Action.ASK_HUMAN
    assert decision.state is None


@pytest.mark.parametrize("attention", sorted(COMPLETION_ATTENTION_REASONS))
def test_a_completion_attention_without_a_settled_state_goes_to_review(
    attention,
) -> None:
    decision = decide_next_action(system_check="not_checked", attention=attention)

    assert decision.rule == "completion_attention_unsettled"
    assert decision.action is Action.SEND_TO_REVIEW
    assert decision.state is None


def test_nothing_to_decide_on_falls_back_to_a_person() -> None:
    decision = decide_next_action()

    assert decision.rule == "fallback"
    assert decision.action is Action.ASK_HUMAN


def test_every_table_row_is_reachable_and_named_by_a_test() -> None:
    """A row nobody can reach, or nobody tests, is a rule nobody checks."""
    source = Path(__file__).read_text(encoding="utf-8")
    for rule in DECISION_TABLE:
        assert f'"{rule.name}"' in source, rule.name
