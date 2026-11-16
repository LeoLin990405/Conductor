"""Native Workers have no report tool; their replay's last answer is the report."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy import executor
from src.server.services.projects.dispatch import last_assistant_text


@pytest.fixture(autouse=True)
def native_worker_uses_a_non_git_cwd():
    with patch(
        "src.server.database.conversation.native_runtime.get_thread_metadata",
        new=AsyncMock(return_value={"native_runtime": {"provider": "codex", "cwd": "/tmp"}}),
    ):
        yield


def test_takes_the_last_non_empty_assistant_chunk():
    sse = "\n".join(
        [
            'event: message_chunk',
            'data: {"role":"assistant","content":"first"}',
            "",
            'event: message_chunk',
            'data: {"role":"assistant","content":""}',
            "",
            'event: message_chunk',
            'data: {"role":"assistant","content":"  final answer  "}',
            "",
        ]
    )
    assert last_assistant_text(sse) == "final answer"


def test_ignores_user_and_tool_chunks():
    sse = "\n".join(
        [
            'event: message_chunk',
            'data: {"role":"user","content":"question"}',
            "",
            'event: message_chunk',
            'data: {"role":"assistant","content":"only answer"}',
            "",
        ]
    )
    assert last_assistant_text(sse) == "only answer"


def test_returns_none_for_no_answer_or_broken_rows():
    assert last_assistant_text("") is None
    assert last_assistant_text("event: message_chunk\ndata: not-json\n") is None
    assert last_assistant_text("event: message_chunk\ndata: {}\n") is None


class _Cursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, *args, **kwargs):
        return None

    async def fetchall(self):
        return [("ticket-1", "user-1", "thread-1")]


_BLOCKED_REPORT = json.dumps(
    {
        "status": "blocked",
        "summary": "Checked.",
        "artifacts": {"commits": [], "files": [], "pr": None},
        "verification": [],
        "next": "Which branch?",
        "handbacks": [],
    }
)


_FRESH_REPORT = json.dumps(
    {
        "status": "done",
        "summary": "Fresh result.",
        "artifacts": {"commits": [], "files": [], "pr": None},
        "verification": [],
        "next": "",
        "handbacks": [],
    }
)


class _Conn:
    def cursor(self):
        return _Cursor()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_a_completed_native_worker_report_becomes_a_ticket_report():
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_provider": "codex",
    }
    dispatcher = AsyncMock()
    dispatcher.thread_run_status = AsyncMock(return_value="completed")
    dispatcher.thread_last_assistant_text = AsyncMock(return_value="Implemented and verified.")
    with (
        patch("src.server.database.pool.get_db_connection", return_value=_Conn()),
        patch.object(executor, "ProjectDispatcher", return_value=dispatcher),
        patch.object(executor.ticket_db, "get_ticket", AsyncMock(return_value=ticket)),
        patch.object(executor.ticket_db, "latest_report", AsyncMock(return_value=None)),
        patch.object(executor, "apply_worker_report", AsyncMock()) as apply_report,
    ):
        await executor.poll_running_workers()

    # Prose is not a completion report: the Ticket waits for a person.
    apply_report.assert_awaited_once_with(
        ticket_id="ticket-1",
        state="waiting",
        summary="No valid completion report was received.",
        body="Implemented and verified.",
        question=(
            "No valid completion report was received. "
            "Review the raw run, then reply or retry."
        ),
        expected_thread_id="thread-1",
        attention_reason="completion_report_missing",
        expected_run_id=None,
    )


@pytest.mark.asyncio
async def test_a_failed_native_worker_also_gets_a_failed_report():
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_provider": "codex",
    }
    dispatcher = AsyncMock()
    dispatcher.thread_run_status = AsyncMock(return_value="error")
    dispatcher.thread_last_assistant_text = AsyncMock(return_value=None)
    with (
        patch("src.server.database.pool.get_db_connection", return_value=_Conn()),
        patch.object(executor, "ProjectDispatcher", return_value=dispatcher),
        patch.object(executor.ticket_db, "get_ticket", AsyncMock(return_value=ticket)),
        patch.object(executor.ticket_db, "latest_report", AsyncMock(return_value=None)),
        patch.object(executor, "apply_worker_report", AsyncMock()) as apply_report,
    ):
        await executor.poll_running_workers()

    apply_report.assert_awaited_once_with(
        ticket_id="ticket-1",
        state="failed",
        summary="Native Worker ended with error.",
        body=None,
        question=None,
        expected_thread_id="thread-1",
        attention_reason=None,
        expected_run_id=None,
    )


def test_the_marker_parser_is_gone():
    assert not hasattr(executor, "parse_native_report")


def test_the_native_prompt_asks_for_json_and_ptc_for_the_tool():
    from src.server.services.autonomy.completion import (
        COMPLETION_REPORT_PROMPT,
        completion_report_prompt,
    )
    from src.server.services.autonomy.worker import worker_prompt

    ticket = {"ticket_id": "ticket-1", "title": "Fix", "scope": "Fix it"}
    native = worker_prompt(ticket, {}, native=True)
    assert COMPLETION_REPORT_PROMPT in native
    assert "AUTONOMY_STATE" not in native
    speculative = worker_prompt(ticket, {}, native=True, speculative=True)
    assert completion_report_prompt(speculative=True) in speculative
    assert "AUTONOMY_STATE" not in speculative
    assert "AUTONOMY_QUESTION" not in speculative
    assert "autonomy_worker_report" not in native
    ptc = worker_prompt(ticket, {}, native=False)
    assert "autonomy_worker_report" in ptc
    assert "AUTONOMY_STATE" not in ptc


@pytest.mark.asyncio
async def test_a_blocked_report_parks_the_ticket_with_its_question():
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_provider": "codex",
    }
    dispatcher = AsyncMock()
    dispatcher.thread_run_status = AsyncMock(return_value="completed")
    dispatcher.thread_last_assistant_text = AsyncMock(return_value=_BLOCKED_REPORT)
    with (
        patch("src.server.database.pool.get_db_connection", return_value=_Conn()),
        patch.object(executor, "ProjectDispatcher", return_value=dispatcher),
        patch.object(executor.ticket_db, "get_ticket", AsyncMock(return_value=ticket)),
        patch.object(executor.ticket_db, "latest_report", AsyncMock(return_value=None)),
        patch.object(executor, "apply_worker_report", AsyncMock()) as apply_report,
    ):
        await executor.poll_running_workers()

    report = apply_report.await_args.kwargs
    assert report["state"] == "blocked"
    assert report["summary"] == "Checked."
    assert report["question"] == "Which branch?"
    assert report["attention_reason"] is None
    assert report["completion"].state == "blocked"
    assert report["completion_report"].status == "blocked"
    assert json.loads(report["body"])["next"] == "Which branch?"


@pytest.mark.asyncio
async def test_a_report_from_a_previous_attempt_does_not_settle_the_retry():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_provider": "codex",
        "updated_at": now,
    }
    dispatcher = AsyncMock()
    dispatcher.thread_run_status = AsyncMock(return_value="completed")
    dispatcher.thread_last_assistant_text = AsyncMock(return_value=_FRESH_REPORT)
    with (
        patch("src.server.database.pool.get_db_connection", return_value=_Conn()),
        patch.object(executor, "ProjectDispatcher", return_value=dispatcher),
        patch.object(executor.ticket_db, "get_ticket", AsyncMock(return_value=ticket)),
        patch.object(
            executor.ticket_db,
            "latest_report",
            AsyncMock(return_value={"created_at": now - timedelta(minutes=5)}),
        ),
        patch.object(executor, "apply_worker_report", AsyncMock()) as apply_report,
    ):
        await executor.poll_running_workers()

    apply_report.assert_awaited_once()
    report = apply_report.await_args.kwargs
    assert report["state"] == "ready"
    assert report["summary"] == "Fresh result."
    assert report["question"] is None
    assert report["expected_run_id"] is None


@pytest.mark.asyncio
async def test_ptc_without_report_fences_waiting_on_observed_thread():
    """A PTC turn that ends without the report tool must fence the waiting move."""
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_provider": None,
    }
    dispatcher = AsyncMock()
    dispatcher.thread_run_status = AsyncMock(return_value="completed")
    with (
        patch("src.server.database.pool.get_db_connection", return_value=_Conn()),
        patch.object(executor, "ProjectDispatcher", return_value=dispatcher),
        patch.object(executor.ticket_db, "get_ticket", AsyncMock(return_value=ticket)),
        patch.object(executor.ticket_db, "latest_report", AsyncMock(return_value=None)),
        patch.object(
            executor.ticket_db, "update_ticket", AsyncMock(return_value={"ticket_id": "ticket-1"})
        ) as update,
    ):
        await executor.poll_running_workers()

    update.assert_awaited_once_with(
        "ticket-1",
        expected_status="running",
        expected_thread_id="thread-1",
        status="waiting",
        summary="Worker ended without a structured report.",
        attention={
            "question": "The Worker ended without reporting. Review the raw run, then reply or retry."
        },
    )


@pytest.mark.asyncio
async def test_ptc_without_report_ignores_switched_away_line():
    """A switched-away PTC turn must not push the requeued Ticket to waiting."""
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_provider": "",
    }
    dispatcher = AsyncMock()
    dispatcher.thread_run_status = AsyncMock(return_value="completed")
    with (
        patch("src.server.database.pool.get_db_connection", return_value=_Conn()),
        patch.object(executor, "ProjectDispatcher", return_value=dispatcher),
        patch.object(executor.ticket_db, "get_ticket", AsyncMock(return_value=ticket)),
        patch.object(executor.ticket_db, "latest_report", AsyncMock(return_value=None)),
        patch.object(
            executor.ticket_db, "update_ticket", AsyncMock(return_value=None)
        ) as update,
    ):
        checked = await executor.poll_running_workers()

    assert checked == 1
    update.assert_awaited_once()
    assert update.await_args.kwargs["expected_thread_id"] == "thread-1"
    assert update.await_args.kwargs["expected_status"] == "running"
    assert update.await_args.kwargs["status"] == "waiting"
