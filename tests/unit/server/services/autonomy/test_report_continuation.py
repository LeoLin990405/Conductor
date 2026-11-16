"""After a valid report, the next queued item is pushed to the Worker (R5b L2)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy import executor, report_continuation
from src.server.services.autonomy.worker_idle import IDLE_WAKE_NOTICE


class _Cursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, *args, **kwargs):
        return None

    async def fetchall(self):
        return [("ticket-1", "user-1", "thread-1")]


class _Connection:
    def cursor(self):
        return _Cursor()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _report(status: str = "done") -> str:
    return json.dumps(
        {
            "status": status,
            "summary": "Finished the first item.",
            "artifacts": {"commits": ["abc123"], "files": ["a.py"], "pr": None},
            "verification": [{"ran": "pytest", "result": "passed"}],
            "next": "",
            "handbacks": [],
        }
    )


def _ticket(**mandate) -> dict:
    return {
        "ticket_id": "ticket-1",
        "user_id": "user-1",
        "status": "running",
        "worker_provider": "codex",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-1",
        "attempts": 1,
        "mandate": {
            "git_delivery_baseline": {"version": 2},
            "git_delivery_result": {
                "version": 2,
                "thread_id": "thread-1",
                "run_id": "run-1",
                "attempt": 1,
                "summary": "",
                "commit_delivery": "delivered",
            },
            **mandate,
        },
    }


async def _poll(
    *,
    assistant: str,
    queued: list[str] | None,
    ticket: dict | None = None,
):
    dispatcher = AsyncMock()
    dispatcher.thread_run_status = AsyncMock(return_value="completed")
    dispatcher.thread_last_assistant_text = AsyncMock(return_value=assistant)
    queued_mock = AsyncMock(return_value=queued)
    push_mock = AsyncMock(return_value=bool(queued))
    release = AsyncMock()
    with (
        patch("src.server.database.pool.get_db_connection", return_value=_Connection()),
        patch.object(executor, "ProjectDispatcher", return_value=dispatcher),
        patch.object(
            executor.ticket_db, "get_ticket", AsyncMock(return_value=ticket or _ticket())
        ),
        patch.object(executor.ticket_db, "latest_report", AsyncMock(return_value=None)),
        patch.object(executor, "_release_settled_worktree", release),
        patch.object(executor, "queued_next_items", queued_mock),
        patch.object(executor, "push_queued_items", push_mock),
        patch.object(executor, "apply_worker_report", autospec=True) as apply_report,
    ):
        await executor.poll_running_workers()
    return apply_report, queued_mock, push_mock, release


@pytest.mark.asyncio
async def test_a_done_report_with_a_queued_item_waits_and_pushes_it():
    apply_report, _queued, push, release = await _poll(
        assistant=_report(), queued=["input-7"]
    )

    report = apply_report.await_args.kwargs
    # Done with this item, not with the Ticket: it must not complete.
    assert report["state"] == "waiting"
    assert report["completion"].state == "waiting"
    assert report["completion_report"].status == "done"
    push.assert_awaited_once()
    assert push.await_args.args[1] == ["input-7"]
    assert push.await_args.kwargs["run_id"] == "run-1"
    assert push.await_args.kwargs["report_text"] == _report()
    # The Ticket stays open, so its checkout is kept for the next item.
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_done_report_with_nothing_queued_completes_as_before():
    apply_report, queued, push, release = await _poll(
        assistant=_report(), queued=[]
    )

    assert apply_report.await_args.kwargs["state"] == "ready"
    queued.assert_awaited_once_with("ticket-1")
    push.assert_not_awaited()
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_unreadable_queue_holds_the_report_for_the_next_pass():
    apply_report, _queued, push, _release = await _poll(
        assistant=_report(), queued=None
    )

    apply_report.assert_not_awaited()
    push.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["blocked", "needs_review", "partial"])
async def test_a_report_that_is_not_done_keeps_its_outcome(status):
    apply_report, queued, push, _release = await _poll(
        assistant=_report(status), queued=["input-7"]
    )

    assert apply_report.await_args.kwargs["state"] != "ready"
    assert apply_report.await_args.kwargs["state"] == apply_report.await_args.kwargs[
        "completion"
    ].state
    queued.assert_not_awaited()
    push.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_turn_without_a_valid_report_pushes_nothing():
    apply_report, queued, push, _release = await _poll(
        assistant="I am done.", queued=["input-7"]
    )

    queued.assert_not_awaited()
    push.assert_not_awaited()
    assert apply_report.await_args.kwargs["state"] == "waiting"


@pytest.mark.asyncio
async def test_a_speculative_plan_pushes_nothing():
    apply_report, queued, push, _release = await _poll(
        assistant=_report(), queued=["input-7"], ticket=_ticket(speculative=True)
    )

    queued.assert_not_awaited()
    push.assert_not_awaited()


def test_only_a_clean_ready_report_may_continue():
    ok = dict(report_state="ready", has_completion_report=True, mandate={})
    assert report_continuation.report_may_continue(attention_reason=None, **ok)
    assert not report_continuation.report_may_continue(
        attention_reason="uncommitted_changes", **ok
    )
    assert not report_continuation.report_may_continue(
        attention_reason=None,
        report_state="ready",
        has_completion_report=False,
        mandate={},
    )
    assert not report_continuation.report_may_continue(
        attention_reason=None,
        report_state="ready",
        has_completion_report=True,
        mandate={"read_only": True},
    )


@pytest.mark.asyncio
async def test_queued_items_skip_and_discard_stale_wake_notices():
    rows = [
        {"input_id": "wake-1", "kind": "instruction", "text": IDLE_WAKE_NOTICE.format(minutes=30)},
        {"input_id": "input-7", "kind": "instruction", "text": "Also fix the README."},
        {"input_id": "answer-2", "kind": "answer", "text": "Use branch main."},
    ]
    marked = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.worker._pending_instruction_rows",
            AsyncMock(return_value=rows),
        ),
        patch.object(report_continuation.ticket_db, "mark_input_delivered", marked),
    ):
        queued = await report_continuation.queued_next_items("ticket-1")

    assert queued == ["input-7", "answer-2"]
    marked.assert_awaited_once_with("wake-1")


@pytest.mark.asyncio
async def test_a_failed_queue_read_is_not_an_empty_queue():
    with patch(
        "src.server.services.autonomy.worker._pending_instruction_rows",
        AsyncMock(side_effect=RuntimeError("db down")),
    ):
        assert await report_continuation.queued_next_items("ticket-1") is None


@pytest.mark.asyncio
async def test_the_push_sends_only_the_next_item_after_recording_the_continuation():
    order: list[str] = []

    async def record(ticket_id, key, **kwargs):
        order.append("record")
        assert key == report_continuation.CONTINUATION_MANDATE_KEY
        assert kwargs["expected_status"] == "waiting"
        assert kwargs["expected_thread_id"] == "thread-1"
        assert kwargs["set_fields"]["run_id"] == "run-1"
        assert kwargs["set_fields"]["input_id"] == "input-7"
        assert kwargs["set_fields"]["report_digest"] == (
            report_continuation.report_digest("the report")
        )
        return {"ticket_id": ticket_id}

    async def enqueue(**kwargs):
        order.append("enqueue")
        return True

    enqueue_mock = AsyncMock(side_effect=enqueue)
    with (
        patch.object(
            report_continuation.ticket_db,
            "update_mandate_record",
            AsyncMock(side_effect=record),
        ),
        patch("src.server.database.autonomy.effects.enqueue_effect", enqueue_mock),
    ):
        pushed = await report_continuation.push_queued_items(
            {
                "ticket_id": "ticket-1",
                "user_id": "user-1",
                "worker_thread_id": "thread-1",
                "attempts": 1,
            },
            ["input-7", "input-8"],
            run_id="run-1",
            report_text="the report",
        )

    assert pushed is True
    assert order == ["record", "enqueue"]
    enqueue_mock.assert_awaited_once()
    kwargs = enqueue_mock.await_args.kwargs
    assert kwargs["kind"] == "deliver_input"
    assert kwargs["payload"] == {"ticket_id": "ticket-1", "input_id": "input-7"}
    assert kwargs["idempotency_key"] == "autonomy:input:input-7:after-report:run-1"


@pytest.mark.asyncio
async def test_no_push_when_the_continuation_cannot_be_recorded():
    enqueue_mock = AsyncMock(return_value=True)
    with (
        patch.object(
            report_continuation.ticket_db,
            "update_mandate_record",
            AsyncMock(return_value=None),
        ),
        patch("src.server.database.autonomy.effects.enqueue_effect", enqueue_mock),
    ):
        pushed = await report_continuation.push_queued_items(
            {"ticket_id": "ticket-1", "user_id": "user-1", "worker_thread_id": "t"},
            ["input-7"],
            run_id="run-1",
            report_text="x",
        )

    assert pushed is False
    enqueue_mock.assert_not_awaited()


def _continued(**extra) -> dict:
    return _ticket(
        report_continuation={
            "run_id": "run-1",
            "attempt": 1,
            "input_id": "input-7",
            "report_digest": report_continuation.report_digest(_report()),
        },
        **extra,
    )


@pytest.mark.asyncio
async def test_the_filed_run_is_not_settled_again_before_the_pushed_item_starts():
    """Admitted back to running, the thread still shows the filed run's report."""
    apply_report, queued, push, release = await _poll(
        assistant=_report(), queued=[], ticket=_continued()
    )

    apply_report.assert_not_awaited()
    queued.assert_not_awaited()
    push.assert_not_awaited()
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_pushed_item_is_not_settled_on_the_previous_items_delivery():
    """The mandate's delivery verdict is the filed run's; it is not reused."""
    second = json.dumps(
        {
            "status": "done",
            "summary": "Finished the second item.",
            "artifacts": {"commits": [], "files": [], "pr": None},
            "verification": [],
            "next": "",
            "handbacks": [],
        }
    )
    apply_report, _queued, push, _release = await _poll(
        assistant=second, queued=[], ticket=_continued()
    )

    report = apply_report.await_args.kwargs
    # The recorded "delivered" verdict belongs to the first item.
    assert report["state"] != "ready"
    push.assert_not_awaited()


def test_a_marker_for_another_run_is_not_this_runs_continuation():
    mandate = {"report_continuation": {"run_id": "run-old"}}
    assert (
        report_continuation.continuation_marker(mandate, run_id="run-new") is None
    )
    assert report_continuation.continuation_marker(mandate, run_id="run-old")
