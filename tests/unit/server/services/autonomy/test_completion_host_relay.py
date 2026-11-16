"""Completion reports settle through the existing Worker report host."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy import host
from src.server.services.autonomy.completion import (
    COMPLETION_REPORT_PROMPT,
    completion_report_from_text,
    reconcile_completion,
)
from src.server.services.autonomy.worker import worker_prompt
from src.server.utils.sanitize import REDACTED

_SECRET = "sk-" "ant-api03-ABCDEFGHIJKLMNOPQRSTUV"


def _report(**overrides: object) -> str:
    payload: dict[str, object] = {
        "status": "done",
        "summary": "Implemented and verified.",
        "artifacts": {
            "commits": ["abc123"],
            "files": ["src/example.py"],
            "pr": "https://example.test/pull/1",
        },
        "verification": [{"ran": "pytest tests/unit", "result": "passed"}],
        "next": "Review the result.",
        "handbacks": [],
    }
    payload.update(overrides)
    return json.dumps(payload)


def _host_patches(record: AsyncMock):
    ticket = {
        "ticket_id": "ticket-1",
        "user_id": "user-1",
        "status": "running",
        "worker_thread_id": "thread-1",
        "mandate": {"source_id": "source-1"},
    }
    return (
        patch.object(host.ticket_db, "get_ticket", AsyncMock(return_value=ticket)),
        patch.object(host.ticket_db, "record_worker_report", record),
        patch.object(
            host.source_db,
            "get_source_any_user",
            AsyncMock(return_value={"kind": "manual"}),
        ),
        patch.object(host, "enqueue_effect", AsyncMock()),
    )


@pytest.mark.asyncio
async def test_legacy_worker_report_keeps_its_explicit_values():
    record = AsyncMock(return_value={"report_id": "report-1"})
    get, add, source, relay = _host_patches(record)
    with get, add, source, relay:
        await host.apply_worker_report(
            ticket_id="ticket-1",
            state="ready",
            summary="Legacy summary.",
            question=None,
            expected_thread_id="thread-1",
        )

    assert record.await_args.kwargs["state"] == "ready"
    assert record.await_args.kwargs["summary"] == "Legacy summary."
    assert record.await_args.kwargs["body"] == "Legacy summary."


@pytest.mark.asyncio
async def test_completion_uses_reconciled_state_and_records_redacted_json():
    report = completion_report_from_text(_report(handbacks=[_SECRET]))
    assert report is not None
    completion = reconcile_completion(report, git_delivered=True, tests_passed=False)
    record = AsyncMock(return_value={"report_id": "report-1"})
    get, add, source, relay = _host_patches(record)
    with get, add, source, relay:
        await host.apply_worker_report(
            ticket_id="ticket-1",
            state="failed",
            summary="Worker summary.",
            question="Worker question.",
            expected_thread_id="thread-1",
            completion=completion,
            completion_report=report,
        )

    recorded = record.await_args.kwargs
    assert recorded["state"] == "blocked"
    assert recorded["summary"] == "Worker summary."
    assert recorded["question"] == "Worker question."
    assert recorded["ticket_fields"]["status"] == "blocked"
    assert "ended_at" not in recorded["ticket_fields"]
    assert _SECRET not in recorded["body"]
    assert json.loads(recorded["body"]) == {
        "status": "done",
        "summary": "Implemented and verified.",
        "artifacts": {
            "commits": ["abc123"],
            "files": ["src/example.py"],
            "pr": "https://example.test/pull/1",
        },
        "verification": [{"ran": "pytest tests/unit", "result": "passed"}],
        "next": "Review the result.",
        "handbacks": [REDACTED],
        "reconciled_state": "blocked",
        "disagreement": True,
        "disagreement_reason": "tests_failed",
    }


@pytest.mark.asyncio
async def test_a_caller_body_is_kept_and_sanitized():
    report = completion_report_from_text(_report())
    assert report is not None
    completion = reconcile_completion(report, git_delivered=True, tests_passed=None)
    record = AsyncMock(return_value={"report_id": "report-1"})
    get, add, source, relay = _host_patches(record)
    with get, add, source, relay:
        await host.apply_worker_report(
            ticket_id="ticket-1",
            state="ready",
            summary="Done.",
            body=f"Git context {_SECRET}",
            expected_thread_id="thread-1",
            completion=completion,
            completion_report=report,
        )

    body = record.await_args.kwargs["body"]
    assert body.startswith("Git context ")
    assert _SECRET not in body


@pytest.mark.asyncio
async def test_completion_needs_its_validated_report():
    report = completion_report_from_text(_report())
    assert report is not None
    completion = reconcile_completion(report, git_delivered=True, tests_passed=None)
    with pytest.raises(ValueError):
        await host.apply_worker_report(
            ticket_id="ticket-1",
            state="ready",
            summary="Done.",
            expected_thread_id="thread-1",
            completion=completion,
        )


@pytest.mark.parametrize(
    ("state", "status", "attention", "ended"),
    [
        ("ready", "completed", None, True),
        ("dropped", "completed", None, True),
        ("waiting", "waiting", None, False),
        ("blocked", "blocked", {"question": "Q?"}, False),
        ("failed", "failed", {"question": "Q?"}, False),
    ],
)
def test_reports_without_completion_keep_the_existing_state_mapping(
    state, status, attention, ended
):
    transition = host.worker_report_transition(
        ticket_id="ticket-1",
        ticket={"mandate": {"source_id": "source-1"}},
        state=state,
        summary="S.",
        question="Q?",
    )

    assert transition["status"] == status
    assert transition["attention"] == attention
    assert transition["ended"] is ended
    assert transition["relay"] is (state != "dropped")


@pytest.mark.asyncio
async def test_uncommitted_git_changes_cannot_complete_a_done_report():
    report = completion_report_from_text(_report())
    assert report is not None
    completion = reconcile_completion(report, git_delivered=False, tests_passed=True)
    record = AsyncMock(return_value={"report_id": "report-1"})
    get, add, source, relay = _host_patches(record)
    with get, add, source, relay:
        await host.apply_worker_report(
            ticket_id="ticket-1",
            state="ready",
            summary="Done, but not delivered.",
            expected_thread_id="thread-1",
            completion=completion,
            completion_report=report,
        )

    recorded = record.await_args.kwargs
    assert completion.state == "blocked"
    assert recorded["state"] == "blocked"
    assert recorded["ticket_fields"]["status"] == "blocked"
    assert "ended_at" not in recorded["ticket_fields"]
    body = json.loads(recorded["body"])
    assert body["disagreement"] is True
    assert body["disagreement_reason"] == "git_not_delivered"


@pytest.mark.asyncio
async def test_clean_completion_records_no_disagreement():
    report = completion_report_from_text(_report())
    assert report is not None
    completion = reconcile_completion(report, git_delivered=True, tests_passed=True)
    record = AsyncMock(return_value={"report_id": "report-1"})
    get, add, source, relay = _host_patches(record)
    with get, add, source, relay:
        await host.apply_worker_report(
            ticket_id="ticket-1",
            state="failed",
            summary="Done.",
            expected_thread_id="thread-1",
            completion=completion,
            completion_report=report,
        )

    recorded = record.await_args.kwargs
    assert recorded["state"] == "ready"
    assert recorded["ticket_fields"]["status"] == "completed"
    assert "ended_at" in recorded["ticket_fields"]
    body = json.loads(recorded["body"])
    assert body["disagreement"] is False
    assert body["disagreement_reason"] is None


def test_native_prompt_requests_one_json_completion_report():
    prompt = worker_prompt(
        {"ticket_id": "ticket-1", "title": "Fix", "scope": "Fix it"},
        {},
        native=True,
    )

    assert COMPLETION_REPORT_PROMPT in prompt
    assert "AUTONOMY_STATE" not in prompt
    assert "AUTONOMY_QUESTION" not in prompt


def test_native_prompt_allows_work_before_the_final_reporting_step():
    prompt = worker_prompt(
        {"ticket_id": "ticket-1", "title": "Fix", "scope": "Fix it"},
        {},
        native=True,
    )

    assert "Verify the change before reporting." in prompt
    assert "after completing and verifying the Ticket" in COMPLETION_REPORT_PROMPT
    assert "For that final reporting step" in COMPLETION_REPORT_PROMPT
    assert "Do not run tools or change files in this turn" not in prompt


def test_resumed_native_prompt_keeps_the_json_completion_contract():
    from src.server.services.autonomy.worker import append_followups

    prompt = worker_prompt(
        {"ticket_id": "ticket-1", "title": "Fix", "scope": "Fix it"},
        {},
        native=True,
    )
    resumed = append_followups(
        prompt, [{"input_id": "input-1", "kind": "answer", "text": "Continue."}]
    )

    assert COMPLETION_REPORT_PROMPT in resumed
    assert resumed.endswith("same completion report as instructed above.")
    assert "AUTONOMY_STATE" not in resumed
    assert "AUTONOMY_QUESTION" not in resumed


def test_missing_completion_attention_is_preserved_for_waiting_ticket():
    transition = host.worker_report_transition(
        ticket_id="ticket-1",
        ticket={"mandate": {}},
        state="waiting",
        summary="No valid completion report was received.",
        question="No valid completion report was received. Review the raw run.",
        attention_reason="completion_report_missing",
    )

    assert transition["status"] == "waiting"
    assert transition["attention"] == {
        "kind": "needs_action",
        "reason": "completion_report_missing",
        "question": "No valid completion report was received. Review the raw run.",
    }
