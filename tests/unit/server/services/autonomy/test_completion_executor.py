import json
from contextlib import nullcontext
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy import executor


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


def _completion_text(status="done", commits=("abc123",), summary=None):
    return json.dumps(
        {
            "status": status,
            "summary": summary or "Implemented and verified the executor change.",
            "artifacts": {"commits": list(commits), "files": ["executor.py"], "pr": None},
            "verification": [{"ran": "pytest focused", "result": "passed"}],
            "next": "",
            "handbacks": [],
        }
    )


async def _poll(
    *,
    status="completed",
    assistant=None,
    git_summary=None,
    commit_delivery=None,
    run_id="run-1",
    mock_host=True,
    speculative=False,
):
    mandate = {}
    if git_summary is not None:
        mandate = {
            "git_delivery_baseline": {"version": 2},
            "git_delivery_result": {
                "version": 2,
                "thread_id": "thread-1",
                "run_id": run_id,
                "attempt": 3,
                "summary": git_summary,
            },
        }
        if commit_delivery is not None:
            mandate["git_delivery_result"]["commit_delivery"] = commit_delivery
    if speculative:
        mandate["speculative"] = True
    ticket = {
        "ticket_id": "ticket-1",
        "user_id": "user-1",
        "status": "running",
        "worker_provider": "codex",
        "worker_thread_id": "thread-1",
        "worker_run_id": run_id,
        "attempts": 3,
        "mandate": mandate,
    }
    dispatcher = AsyncMock()
    dispatcher.thread_run_status = AsyncMock(return_value=status)
    dispatcher.thread_last_assistant_text = AsyncMock(return_value=assistant)
    host_patch = (
        patch.object(executor, "apply_worker_report", autospec=True)
        if mock_host
        else nullcontext()
    )
    with (
        patch("src.server.database.pool.get_db_connection", return_value=_Connection()),
        patch.object(executor, "ProjectDispatcher", return_value=dispatcher),
        patch.object(executor.ticket_db, "get_ticket", AsyncMock(return_value=ticket)),
        patch.object(executor.ticket_db, "latest_report", AsyncMock(return_value=None)),
        patch.object(
            executor.ticket_db,
            "update_ticket",
            AsyncMock(return_value={"ticket_id": "ticket-1"}),
        ) as update_ticket,
        patch.object(executor, "_release_settled_worktree", AsyncMock()),
        host_patch as apply_report,
    ):
        checked = await executor.poll_running_workers()

    assert checked == 1
    if apply_report is None:
        update_ticket.assert_not_awaited()
    else:
        assert apply_report.await_count + update_ticket.await_count == 1
    return apply_report, update_ticket


@pytest.mark.asyncio
async def test_completed_native_report_is_reconciled_with_git_delivery():
    git_summary = (
        "Uncommitted git changes detected.\ngit status --short:\n M executor.py"
    )

    apply_report, _ = await _poll(assistant=_completion_text(), git_summary=git_summary)
    report = apply_report.await_args.kwargs

    assert report["ticket_id"] == "ticket-1"
    assert report["state"] == "blocked"
    assert report["attention_reason"] == "uncommitted_changes"
    assert report["completion"].state == "blocked"
    assert report["completion"].disagreement is True
    assert report["completion"].disagreement_reason == "git_not_delivered"
    assert report["completion_report"].status == "done"
    assert report["body"].startswith(git_summary)
    assert report["expected_thread_id"] == "thread-1"
    assert report["expected_run_id"] == "run-1"


@pytest.mark.asyncio
async def test_completed_native_report_reconciles_clean_git_as_done():
    apply_report, _ = await _poll(
        assistant=_completion_text(), git_summary="", commit_delivery="delivered"
    )
    report = apply_report.await_args.kwargs

    assert report["state"] == "ready"
    assert report["question"] is None
    assert report["completion"].state == "ready"
    assert report["completion"].disagreement is False
    assert report["completion"].disagreement_reason is None
    assert report["completion_report"].status == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "assistant",
    [None, "Implemented and verified.", "AUTONOMY_STATE: ready", "{not valid json}"],
)
async def test_completed_native_turn_without_valid_report_waits_for_attention(
    assistant,
):
    apply_report, update_ticket = await _poll(assistant=assistant)

    update_ticket.assert_not_awaited()
    apply_report.assert_awaited_once_with(
        ticket_id="ticket-1",
        state="waiting",
        summary="No valid completion report was received.",
        body=assistant or "",
        question=(
            "No valid completion report was received. "
            "Review the raw run, then reply or retry."
        ),
        expected_thread_id="thread-1",
        attention_reason="completion_report_missing",
        expected_run_id="run-1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "assistant", ["Implemented and verified.", "AUTONOMY_STATE: ready"]
)
async def test_missing_report_without_run_id_or_mandate_still_waits(assistant):
    apply_report, update_ticket = await _poll(assistant=assistant, run_id=None)

    update_ticket.assert_not_awaited()
    report = apply_report.await_args.kwargs
    assert report["state"] == "waiting"
    assert report["attention_reason"] == "completion_report_missing"
    assert report["expected_run_id"] is None


@pytest.mark.asyncio
async def test_missing_report_attention_is_persisted_through_real_host():
    record = AsyncMock(return_value={"report_id": "report-1"})
    with (
        patch.object(executor.ticket_db, "record_worker_report", record),
        patch("src.server.services.autonomy.host.enqueue_effect", AsyncMock()),
    ):
        await _poll(assistant="Implemented and verified.", mock_host=False)

    fields = record.await_args.kwargs["ticket_fields"]
    assert fields["status"] == "waiting"
    assert fields["summary"] == "No valid completion report was received."
    assert fields["attention"] == {
        "kind": "needs_action",
        "reason": "completion_report_missing",
        "question": (
            "No valid completion report was received. "
            "Review the raw run, then reply or retry."
        ),
    }


@pytest.mark.asyncio
async def test_failed_native_turn_does_not_accept_a_done_report():
    text = _completion_text()
    with patch.object(executor, "settle_native_completion") as settle:
        apply_report, _ = await _poll(status="error", assistant=text)

    settle.assert_not_called()
    report = apply_report.await_args.kwargs
    assert report["state"] == "failed"
    assert report["summary"] == text
    assert report["attention_reason"] is None
    assert "completion" not in report


@pytest.mark.asyncio
@pytest.mark.parametrize("commits", [(), ("abc123",)], ids=["no-commits", "commits"])
async def test_clean_workspace_without_a_new_commit_blocks_a_done_report(commits):
    """Regression: the no-delivery verdict applies whatever the Worker lists."""
    apply_report, _ = await _poll(
        assistant=_completion_text(commits=commits),
        git_summary="",
        commit_delivery="missing",
    )
    report = apply_report.await_args.kwargs

    assert report["state"] == "blocked"
    assert report["completion"].state == "blocked"
    assert report["completion"].disagreement is True
    assert report["completion"].disagreement_reason == "git_not_delivered"
    assert report["question"] == (
        "The system could not confirm a delivered commit for this Ticket. "
        "Commit the work, then retry."
    )
    assert json.loads(report["body"])["disagreement"] is True


@pytest.mark.asyncio
async def test_a_result_without_a_commit_verdict_keeps_workspace_only_behavior():
    apply_report, _ = await _poll(assistant=_completion_text(commits=()), git_summary="")
    report = apply_report.await_args.kwargs

    assert report["state"] == "ready"
    assert report["completion"].disagreement is False


@pytest.mark.asyncio
async def test_old_marker_text_no_longer_completes_a_ticket():
    apply_report, _ = await _poll(
        assistant="Implemented.\nAUTONOMY_STATE: ready",
        git_summary="",
        commit_delivery="delivered",
    )
    report = apply_report.await_args.kwargs

    assert report["state"] == "waiting"
    assert report["attention_reason"] == "completion_report_missing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "state"), [("needs_review", "waiting"), ("done", "dropped")]
)
async def test_speculative_reports_plan_or_drop_without_git_checks(status, state):
    apply_report, _ = await _poll(
        assistant=_completion_text(status=status, commits=(), summary="PLAN: fix it"),
        speculative=True,
    )
    report = apply_report.await_args.kwargs

    assert report["state"] == state
    assert report["attention_reason"] is None


@pytest.mark.asyncio
async def test_done_report_settles_completed_through_the_real_host():
    record = AsyncMock(return_value={"report_id": "report-1"})
    with (
        patch.object(executor.ticket_db, "record_worker_report", record),
        patch("src.server.services.autonomy.host.enqueue_effect", AsyncMock()),
        patch(
            "src.server.services.autonomy.host.source_db.get_source_any_user",
            AsyncMock(return_value={"kind": "manual"}),
        ),
    ):
        await _poll(
            assistant=_completion_text(),
            git_summary="",
            commit_delivery="delivered",
            mock_host=False,
        )

    recorded = record.await_args.kwargs
    assert recorded["state"] == "ready"
    assert recorded["ticket_fields"]["status"] == "completed"
    assert json.loads(recorded["body"])["reconciled_state"] == "ready"
