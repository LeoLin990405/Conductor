"""Pin the host's Worker-report transition to the Router decision table.

``worker_report_transition`` is shared by the PTC and daemon report paths, so
every state/attention/speculative combination must keep the exact transition
dict it returned before the action moved into ``router_decision``. The matrix
below is the contract; the delegation test proves the host asks the table
instead of re-deriving the action from the state itself.
"""

from __future__ import annotations

import json

import pytest

from src.server.services.autonomy import host, router_decision
from src.server.services.autonomy.completion import (
    COMPLETION_NO_CHANGE,
    COMPLETION_REPORT_MISSING,
    COMPLETION_UNVERIFIED,
)
from src.server.services.autonomy.router_decision import Action

STATES = ("ready", "blocked", "failed", "dropped", "waiting")
REASONS = (
    None,
    "uncommitted_changes",
    "git_status_unavailable",
    COMPLETION_REPORT_MISSING,
    COMPLETION_NO_CHANGE,
    COMPLETION_UNVERIFIED,
)
GIT_REASONS = frozenset({"uncommitted_changes", "git_status_unavailable"})
COMPLETION_REASONS = frozenset(
    {COMPLETION_REPORT_MISSING, COMPLETION_NO_CHANGE, COMPLETION_UNVERIFIED}
)
SPECULATIVE_PLAN_QUESTION = (
    "Speculative plan ready. Review it, then Retry to execute the work."
)
SUMMARY = "Worker summary"
QUESTION = "Worker question?"


def _reason_id(reason: str | None) -> str:
    return "none" if reason is None else reason


def _status(state: str, reason: str | None, *, speculative: bool) -> str:
    """The settled status the old host mapping produced, independent of it."""
    if speculative:
        if state == "dropped":
            return "waiting" if reason in GIT_REASONS else "completed"
        return "failed" if state == "failed" else "waiting"
    if state in {"ready", "dropped"}:
        return "blocked" if reason in GIT_REASONS else "completed"
    if state in {"blocked", "failed"}:
        return state
    return "waiting"


def _attention(
    reason: str | None,
    status: str,
    *,
    speculative: bool,
    question: str | None,
) -> dict[str, object] | None:
    if status == "completed":
        return None
    needs_action = {
        "kind": "needs_action",
        "reason": reason,
        "question": question or SUMMARY,
    }
    if speculative:
        if reason is not None:
            return needs_action
        if status == "waiting":
            return {"question": SPECULATIVE_PLAN_QUESTION}
        return {"question": question or SUMMARY}
    if status == "blocked":
        return (
            needs_action if reason in GIT_REASONS else {"question": question or SUMMARY}
        )
    if status == "waiting":
        return needs_action if reason in COMPLETION_REASONS else None
    return {"question": question or SUMMARY}


def _expected_transition(
    state: str,
    reason: str | None,
    *,
    speculative: bool,
    question: str | None,
) -> dict[str, object]:
    status = _status(state, reason, speculative=speculative)
    attention = _attention(reason, status, speculative=speculative, question=question)
    if speculative:
        transition: dict[str, object] = {
            "ticket_id": "t-1",
            "state": state,
            "speculative": True,
        }
        if state == "dropped":
            transition["summary"] = "" if status == "completed" else SUMMARY
        transition["status"] = status
        transition["attention"] = attention
        transition["ended"] = status == "completed"
        transition["relay"] = False
        # A workspace attention keeps a dropped plan as accepted evidence; only
        # the quiet drop discards it.
        transition["useful"] = status != "completed"
        return transition
    transition = {
        "ticket_id": "t-1",
        "state": state,
        "status": status,
        "attention": attention,
        "ended": status == "completed",
        "relay": state != "dropped",
        "source_id": "source-1",
    }
    if state == "dropped":
        transition["useful"] = False
    return transition


@pytest.mark.parametrize(
    "speculative", [False, True], ids=["non_speculative", "speculative"]
)
@pytest.mark.parametrize("reason", REASONS, ids=_reason_id)
@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize(
    "question", [None, QUESTION], ids=["summary_fallback", "question"]
)
def test_full_transition_is_pinned(
    state: str,
    reason: str | None,
    speculative: bool,
    question: str | None,
) -> None:
    """Every report combination keeps its exact dict and key order."""
    transition = host.worker_report_transition(
        ticket_id="t-1",
        ticket={"mandate": {"source_id": "source-1", "speculative": speculative}},
        state=state,
        summary=SUMMARY,
        question=question,
        attention_reason=reason,
    )
    expected = _expected_transition(
        state, reason, speculative=speculative, question=question
    )

    assert transition == expected
    # ``==`` ignores key order; the report is serialized for the wire, so pin
    # the insertion order as well as the values.
    assert json.dumps(transition) == json.dumps(expected)


def test_unknown_report_state_still_raises() -> None:
    with pytest.raises(ValueError, match="state must be ready, blocked"):
        host.worker_report_transition(
            ticket_id="t-1",
            ticket={"mandate": {}},
            state="almost_ready",
            summary=SUMMARY,
            question=QUESTION,
        )


def test_host_calls_the_decision_entry_point(monkeypatch) -> None:
    """The host must ask ``decide_next_action``, not a private copy of the rules.

    Patching the entry point (rather than the legacy adapter the host calls
    directly) proves the adapter itself delegates to the table. On the old
    adapter this spy is never called and the ready report still completes.
    """
    calls: list[dict[str, object]] = []

    def spy(**kwargs: object) -> router_decision.RouterDecision:
        calls.append(kwargs)
        return router_decision.RouterDecision(
            action=Action.ASK_HUMAN,
            rule="forced_by_test",
            state=str(kwargs.get("status")),
            attention=kwargs.get("attention"),  # type: ignore[arg-type]
            speculative=bool(kwargs.get("speculative")),
        )

    monkeypatch.setattr(router_decision, "decide_next_action", spy)

    transition = host.worker_report_transition(
        ticket_id="t-1",
        ticket={"mandate": {"source_id": "source-1"}},
        state="ready",
        summary=SUMMARY,
        question=QUESTION,
    )

    assert calls, "host must reach the router decision table entry point"
    assert transition["status"] == "blocked"
    assert transition["attention"] == {"question": QUESTION}
    assert transition["ended"] is False


def test_replacing_the_table_changes_the_host_transition(monkeypatch) -> None:
    """Regression for the blocker: the table itself must choose the action.

    An unconditional ``ASK_HUMAN`` table must turn a non-speculative ready
    report into a blocked one. The old adapter never read ``DECISION_TABLE``,
    so it still returned ``completed`` here.
    """
    monkeypatch.setattr(
        router_decision,
        "DECISION_TABLE",
        (router_decision.DecisionRule("forced_by_test", Action.ASK_HUMAN),),
    )

    transition = host.worker_report_transition(
        ticket_id="t-1",
        ticket={"mandate": {"source_id": "source-1"}},
        state="ready",
        summary=SUMMARY,
        question=QUESTION,
    )

    assert transition["status"] == "blocked"
    assert transition["attention"] == {"question": QUESTION}
    assert transition["ended"] is False
    assert transition["relay"] is True
