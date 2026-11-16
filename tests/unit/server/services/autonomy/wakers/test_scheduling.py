"""Quota parks, requeues, and the recorded retry budget."""

from src.server.services.autonomy.wakers.base import WakeEvent
from src.server.services.autonomy.wakers.scheduling import (
    WorkItem,
    item_after,
    plan_quota,
    work_item_from_ticket,
)


def _event(kind, key, **kwargs):
    return WakeEvent(
        source="codexbar",
        kind=kind,
        idempotency_key=key,
        provider=kwargs.get("provider", "glm"),
        observed_at="2026-11-10T00:00:00+00:00",
        ticket_id=kwargs.get("ticket_id", ""),
        line_id=kwargs.get("line_id", ""),
    )


def _ticket(status="queued", **kwargs):
    return WorkItem(
        target_id=kwargs.get("target_id", "t1"),
        provider=kwargs.get("provider", "glm"),
        status=status,
        line_id=kwargs.get("line_id", "line-1"),
        retries=kwargs.get("retries", 0),
        parked_reason=kwargs.get("parked_reason", ""),
        last_event_key=kwargs.get("last_event_key", ""),
        has_human_question=kwargs.get("has_human_question", False),
        log=tuple(kwargs.get("log", ())),
        last_observed_at=kwargs.get("last_observed_at", ""),
        user_id=kwargs.get("user_id", ""),
    )


def _tickets(actions):
    return [action for action in actions if action.target_kind == "ticket"]


def _lines(actions):
    return [action for action in actions if action.target_kind == "line"]


def test_parks_a_queued_ticket_and_records_the_line():
    actions = plan_quota(
        [_ticket()], _event("quota_exhausted", "k-park"), max_retries=3
    )
    parked = _tickets(actions)[0]
    assert parked.action == "park"
    assert parked.writes is True
    assert parked.to_status == "blocked"
    assert parked.attention["reason"] == "quota_exhausted"
    assert parked.retries == 0
    assert parked.ledger["last_event_key"] == "k-park"
    assert parked.ledger["state"] == "parked"
    line = _lines(actions)
    assert len(line) == 1
    assert line[0].writes is False
    assert line[0].action == "park"
    assert line[0].from_status == "active"
    assert line[0].to_status == "active"
    assert line[0].record["tickets"] == ["t1"]


def test_a_line_rolls_up_only_the_tickets_that_moved():
    items = [
        _ticket(target_id="a", line_id="L"),
        _ticket(target_id="b", line_id="L"),
        _ticket(target_id="c", line_id=""),
    ]
    actions = plan_quota(
        items, _event("quota_exhausted", "k-line", line_id="L"), max_retries=3
    )
    assert {action.target_id for action in _tickets(actions)} == {"a", "b"}
    lines = _lines(actions)
    assert len(lines) == 1
    assert lines[0].target_id == "L"
    assert lines[0].writes is False
    assert set(lines[0].record["tickets"]) == {"a", "b"}


def test_does_not_park_a_running_lease_or_a_human_question():
    running = plan_quota(
        [_ticket(status="running", target_id="run", line_id="")],
        _event("quota_exhausted", "k-run", ticket_id="run"),
    )
    assert running[0].action == "skipped"
    assert running[0].record["reason"] == "live_lease"
    assert running[0].writes is False

    waiting = plan_quota(
        [_ticket(status="waiting", has_human_question=True)],
        _event("quota_exhausted", "k-wait"),
    )
    assert waiting[0].record["reason"] == "not_parkable"
    assert waiting[0].writes is False

    targeted = plan_quota(
        [
            _ticket(
                status="waiting", has_human_question=True, target_id="ask", line_id=""
            )
        ],
        _event("quota_exhausted", "k-ask", ticket_id="ask"),
    )
    assert targeted[0].record["reason"] == "needs_attention"
    assert targeted[0].replace_attention is False


def test_requeue_records_the_retry_and_clears_attention():
    parked = _ticket(
        status="blocked",
        parked_reason="quota_exhausted",
        retries=1,
        last_event_key="old",
    )
    actions = plan_quota([parked], _event("quota_reset", "k-reset"), max_retries=3)
    action = _tickets(actions)[0]
    assert action.action == "requeue"
    assert action.writes is True
    assert action.to_status == "queued"
    assert action.attention is None
    assert action.replace_attention is True
    assert action.retries == 2
    assert action.ledger["retries"] == 2
    assert action.record["retries"] == 2
    assert action.launch_key.startswith("autonomy:launch:t1:quota:")
    assert action.launch_user_id == ""
    assert _lines(actions)[0].action == "requeue"
    assert _lines(actions)[0].writes is False


def test_the_same_event_does_not_count_twice():
    item = _ticket(
        status="blocked",
        parked_reason="quota_exhausted",
        retries=1,
        last_event_key="k-reset",
    )
    action = plan_quota([item], _event("quota_reset", "k-reset"), max_retries=3)[0]
    assert action.action == "already"
    assert action.writes is False
    assert action.retries == 1


def test_a_second_pass_of_the_saved_requeue_is_already():
    item = _ticket(status="blocked", parked_reason="quota_exhausted", line_id="")
    event = _event("quota_reset", "k-reset")
    first = plan_quota([item], event, max_retries=3)[0]
    again = plan_quota([item_after(item, first)], event, max_retries=3)[0]
    assert first.action == "requeue"
    assert again.action == "already"
    assert again.retries == first.retries


def test_a_second_exhaust_while_parked_does_not_spend_a_retry():
    item = _ticket(
        status="blocked",
        parked_reason="quota_exhausted",
        retries=2,
        last_event_key="old",
    )
    action = plan_quota([item], _event("quota_exhausted", "k-again"), max_retries=3)[0]
    assert action.action == "already"
    assert action.writes is False
    assert action.retries == 2


def test_the_retry_budget_stays_blocked():
    item = _ticket(
        status="blocked", parked_reason="quota_exhausted", retries=3, line_id=""
    )
    action = plan_quota([item], _event("quota_reset", "k-spent"), max_retries=3)[0]
    assert action.action == "budget_spent"
    assert action.writes is True
    assert action.to_status == "blocked"
    assert action.retries == 3
    assert action.ledger["reason"] == "quota_retries_exhausted"
    assert action.attention["reason"] == "quota_retries_exhausted"
    assert action.launch_key == ""
    again = plan_quota(
        [item_after(item, action)],
        _event("quota_reset", "k-later"),
        max_retries=3,
    )[0]
    assert again.action == "budget_spent"
    assert again.writes is False
    assert again.retries == 3


def test_a_reset_does_not_clear_a_human_question():
    item = _ticket(
        status="blocked",
        parked_reason="quota_exhausted",
        has_human_question=True,
        line_id="",
    )
    action = plan_quota([item], _event("quota_reset", "k-human"), max_retries=3)[0]
    assert action.action == "skipped"
    assert action.record["reason"] == "needs_attention"
    assert action.writes is False
    assert action.replace_attention is False


def test_the_retry_log_keeps_the_latest_eight():
    log = tuple(
        {"action": "requeue", "idempotency_key": str(index)} for index in range(8)
    )
    item = _ticket(
        status="blocked",
        parked_reason="quota_exhausted",
        log=log,
        line_id="",
    )
    action = plan_quota([item], _event("quota_reset", "k-new"), max_retries=5)[0]
    assert len(action.ledger["log"]) == 8
    assert action.ledger["log"][-1]["idempotency_key"] == "k-new"
    assert action.ledger["log"][0]["idempotency_key"] == "1"


def test_quota_low_does_not_park():
    assert plan_quota([_ticket()], _event("quota_low", "k-low")) == []


def test_ledger_and_attention_decide_the_human_question():
    parked = work_item_from_ticket(
        {
            "ticket_id": "t",
            "worker_provider": "zai",
            "status": "blocked",
            "line_id": "L",
            "mandate": {
                "quota_waker": {
                    "retries": 1,
                    "reason": "quota_exhausted",
                    "last_event_key": "k",
                    "log": [{"action": "park"}],
                }
            },
            "attention": {
                "question": "Parked because the glm quota is exhausted.",
                "reason": "quota_exhausted",
            },
        }
    )
    assert parked.provider == "glm"
    assert parked.retries == 1
    assert parked.has_human_question is False

    asked = work_item_from_ticket(
        {
            "ticket_id": "t",
            "worker_provider": "glm",
            "status": "blocked",
            "mandate": {"quota_waker": {"reason": "quota_exhausted", "retries": 1}},
            "attention": {"question": "Which branch?", "reason": "clarification"},
        }
    )
    assert asked.has_human_question is True
    assert asked.parked_reason == "quota_exhausted"


def test_a_stale_exhaust_does_not_park_after_a_reset():
    parked = _ticket(
        status="queued",
        line_id="",
        last_observed_at="2026-11-10T02:00:00+00:00",
    )
    action = plan_quota(
        [parked], _event("quota_exhausted", "k-late"), max_retries=3
    )[0]
    assert action.action == "skipped"
    assert action.writes is False
    assert action.record["reason"] == "stale_quota_event"
    assert action.to_status == "queued"

    same = _ticket(line_id="", last_observed_at="2026-11-10T00:00:00Z")
    current = plan_quota([same], _event("quota_exhausted", "k-now"), max_retries=3)[0]
    assert current.action == "park"
    assert current.writes is True
