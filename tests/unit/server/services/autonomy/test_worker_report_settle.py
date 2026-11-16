"""The one Worker-report settlement shared by PTC tools and native CLIs."""

from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy import host


@pytest.mark.parametrize(
    ("state", "expected_status", "expected_relay"),
    [
        ("ready", "completed", True),
        ("blocked", "blocked", True),
        ("failed", "failed", True),
        ("dropped", "completed", False),
        ("waiting", "waiting", True),
    ],
)
def test_canonical_transition_maps_all_normal_daemon_report_states(
    state, expected_status, expected_relay
):
    transition = host.worker_report_transition(
        ticket_id="t-1",
        ticket={"mandate": {"source_id": "s-1"}},
        state=state,
        summary="summary",
        question="question",
    )
    assert transition["status"] == expected_status
    assert transition["relay"] is expected_relay
    assert transition["ended"] is (expected_status == "completed")
    if state == "dropped":
        assert transition["useful"] is False


@pytest.mark.parametrize("state", ["ready", "dropped"])
@pytest.mark.parametrize("speculative", [False, True])
def test_uncommitted_success_needs_action_instead_of_completing(state, speculative):
    transition = host.worker_report_transition(
        ticket_id="t-1",
        ticket={"mandate": {"speculative": speculative}},
        state=state,
        summary="done with local changes",
        question="Commit or discard the changes.",
        attention_reason="uncommitted_changes",
    )
    if speculative:
        assert transition["status"] == "waiting"
    else:
        assert transition["status"] == "blocked"
    assert transition["attention"]["kind"] == "needs_action"
    assert transition["attention"]["reason"] == "uncommitted_changes"


def test_git_status_check_failure_is_attention_with_unknown_reason():
    transition = host.worker_report_transition(
        ticket_id="t-1",
        ticket={"mandate": {}},
        state="ready",
        summary="Git workspace status could not be checked safely.",
        question="Review workspace state.",
        attention_reason="git_status_unavailable",
    )
    assert transition["status"] == "blocked"
    assert transition["attention"]["reason"] == "git_status_unavailable"


@pytest.mark.parametrize(
    ("state", "expected_status"),
    [
        ("ready", "waiting"),
        ("blocked", "waiting"),
        ("failed", "failed"),
        ("dropped", "completed"),
        ("waiting", "waiting"),
    ],
)
def test_canonical_transition_maps_all_speculative_daemon_report_states(
    state, expected_status
):
    transition = host.worker_report_transition(
        ticket_id="t-1",
        ticket={"mandate": {"speculative": True, "source_id": "s-1"}},
        state=state,
        summary="summary",
        question="question",
    )
    assert transition["status"] == expected_status
    assert transition["relay"] is False
    assert transition["ended"] is (expected_status == "completed")
    assert transition["useful"] is (state != "dropped")
    if state == "dropped":
        assert transition["summary"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected_status"),
    [
        ("ready", "completed"),
        ("blocked", "blocked"),
        ("failed", "failed"),
        ("dropped", "completed"),
        ("waiting", "waiting"),
    ],
)
async def test_existing_ptc_report_mapping_stays_aligned_for_all_states(
    state, expected_status
):
    ticket = {
        "ticket_id": "t-1",
        "user_id": "u-1",
        "status": "running",
        "mandate": {"source_id": "s-1"},
    }
    get, add, update_ctx, source_ctx, relay_ctx, update, relay = _ctx(
        ticket, {"kind": "github"}
    )
    with get, add, update_ctx, source_ctx, relay_ctx:
        await host.apply_worker_report(
            ticket_id="t-1", state=state, summary="Summary", question="Question",
            expected_thread_id="thread-1",
        )
    assert update.call_args.kwargs["status"] == expected_status
    assert relay.await_count == (0 if state == "dropped" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected_status"),
    [
        ("ready", "waiting"),
        ("blocked", "waiting"),
        ("failed", "failed"),
        ("dropped", "completed"),
        ("waiting", "waiting"),
    ],
)
async def test_existing_ptc_speculative_mapping_stays_aligned_for_all_states(
    state, expected_status
):
    ticket = {
        "ticket_id": "t-1",
        "user_id": "u-1",
        "status": "running",
        "mandate": {"source_id": "s-1", "speculative": True},
    }
    get, add, update_ctx, source_ctx, relay_ctx, update, relay = _ctx(
        ticket, {"kind": "github"}
    )
    with get, add, update_ctx, source_ctx, relay_ctx:
        result = await host.apply_worker_report(
            ticket_id="t-1", state=state, summary="Summary", question="Question",
            expected_thread_id="thread-1",
        )
    assert update.call_args_list[0].kwargs["status"] == expected_status
    recorded = update.call_args.kwargs["mandate"]["speculative_outcome"]
    assert recorded["evidence"]["worker_state"] == state
    assert recorded["evidence"]["worker_summary"] == "Summary"
    assert recorded["evidence"]["report_id"] == "r-1"
    assert result["speculative_outcome"] == recorded["outcome"]
    if state == "dropped":
        assert update.call_args_list[0].kwargs["summary"] == ""
        assert result["useful"] is False
        assert recorded["outcome"] == "discarded"
    elif expected_status == "waiting":
        assert recorded["outcome"] == "accepted"
    else:
        assert recorded["outcome"] == "discarded"
        assert update.call_args_list[0].kwargs["status"] == "failed"
    relay.assert_not_awaited()


@pytest.mark.asyncio
async def test_speculative_evidence_keeps_a_bounded_quiet_reason():
    ticket = {
        "ticket_id": "t-1",
        "user_id": "u-1",
        "status": "running",
        "mandate": {"speculative": True, "source_id": "s-1"},
    }
    get, add, update_ctx, source_ctx, relay_ctx, update, relay = _ctx(
        ticket, {"kind": "github"}
    )
    summary = "n" * (host.SPECULATIVE_EVIDENCE_MAX_CHARS + 50)
    with get, add, update_ctx, source_ctx, relay_ctx:
        await host.apply_worker_report(
            ticket_id="t-1", state="dropped", summary=summary, question="q" * 600,
            expected_thread_id="thread-1",
        )
    evidence = update.call_args.kwargs["mandate"]["speculative_outcome"]["evidence"]
    assert len(evidence["worker_summary"]) == host.SPECULATIVE_EVIDENCE_MAX_CHARS
    assert len(evidence["worker_question"]) == 500
    assert evidence["worker_summary"] == "n" * host.SPECULATIVE_EVIDENCE_MAX_CHARS
    relay.assert_not_awaited()


def _ctx(ticket, source):
    ticket = {**ticket, "worker_thread_id": "thread-1"}
    update = AsyncMock()
    relay = AsyncMock()

    async def record(**kw):
        await update(kw["ticket_id"], **kw["ticket_fields"])
        return {"report_id": "r-1"}

    return (
        patch.object(host.ticket_db, "get_ticket", AsyncMock(return_value=ticket)),
        patch.object(host.ticket_db, "record_worker_report", record),
        patch.object(host.ticket_db, "update_ticket", update),
        patch.object(
            host.source_db, "get_source_any_user", AsyncMock(return_value=source)
        ),
        patch.object(host, "enqueue_effect", relay),
        update,
        relay,
    )


@pytest.mark.asyncio
async def test_ready_completes_and_relays_a_non_manual_source():
    ticket = {
        "ticket_id": "t-1",
        "user_id": "u-1",
        "status": "running",
        "mandate": {"source_id": "s-1"},
    }
    get, add, update_ctx, source_ctx, relay_ctx, update, relay = _ctx(
        ticket, {"kind": "github"}
    )
    with get, add, update_ctx, source_ctx, relay_ctx:
        await host.apply_worker_report(
            ticket_id="t-1", state="ready", summary="Done and verified.",
            expected_thread_id="thread-1",
        )
    assert update.call_args.kwargs["status"] == "completed"
    relay.assert_awaited_once()
    assert relay.call_args.kwargs["payload"] == {"ticket_id": "t-1", "state": "ready"}


@pytest.mark.asyncio
async def test_isolated_report_preserves_structured_git_check(monkeypatch):
    from src.server.services.autonomy import host, worker

    apply = AsyncMock(return_value={"ticket_id": "t-1"})
    monkeypatch.setattr(host, "apply_worker_report", apply)

    await worker._apply_isolated_worker_report(
        ticket_id="t-1",
        thread_id="thread-1",
        state="ready",
        summary="done",
        git_check="unsupported_runtime",
    )

    assert apply.await_args.kwargs["git_check"] == "unsupported_runtime"


def test_ticket_report_view_exposes_structured_git_check():
    from datetime import datetime, timezone

    from src.server.models.autonomy import TicketReportView

    view = TicketReportView.model_validate(
        {
            "report_id": "r-1",
            "ticket_id": "t-1",
            "state": "ready",
            "summary": "done",
            "git_check": "unsupported_runtime",
            "created_at": datetime.now(timezone.utc),
        }
    )
    assert view.git_check == "unsupported_runtime"


@pytest.mark.asyncio
async def test_blocked_keeps_state_and_question_without_relaying_manual():
    ticket = {
        "ticket_id": "t-1",
        "user_id": "u-1",
        "status": "running",
        "mandate": {"source_id": "s-1"},
    }
    get, add, update_ctx, source_ctx, relay_ctx, update, relay = _ctx(
        ticket, {"kind": "manual"}
    )
    with get, add, update_ctx, source_ctx, relay_ctx:
        await host.apply_worker_report(
            ticket_id="t-1",
            state="blocked",
            summary="Need input.",
            question="Which branch?",
            expected_thread_id="thread-1",
        )
    assert update.call_args.kwargs["status"] == "blocked"
    assert update.call_args.kwargs["attention"] == {"question": "Which branch?"}
    relay.assert_not_awaited()


@pytest.mark.asyncio
async def test_daemon_report_uses_live_context_and_canonical_transition():
    from src.server.database.autonomy import daemons as daemon_db

    ticket = {
        "ticket_id": "t-1",
        "user_id": "u-1",
        "status": "running",
        "mandate": {"source_id": "s-1"},
    }
    result = {"ticket_id": "t-1", "state": "ready"}
    with (
        patch.object(
            daemon_db, "daemon_report_context", AsyncMock(return_value=ticket)
        ),
        patch.object(
            daemon_db, "commit_daemon_report", AsyncMock(return_value=result)
        ) as commit,
    ):
        assert (
            await host.apply_daemon_worker_report(
                daemon_id="d-1",
                ticket_id="t-1",
                lease_id="lease-1",
                state="ready",
                summary="Done.",
            )
            == result
        )
    assert commit.await_args.kwargs["transition"]["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("speculative", [False, True])
async def test_daemon_dropped_report_is_discarded_without_relay(speculative):
    from src.server.database.autonomy import daemons as daemon_db

    ticket = {
        "ticket_id": "t-1",
        "user_id": "u-1",
        "status": "running",
        "mandate": {"speculative": speculative, "source_id": "s-1"},
    }
    with (
        patch.object(daemon_db, "daemon_report_context", AsyncMock(return_value=ticket)),
        patch.object(daemon_db, "commit_daemon_report", AsyncMock(return_value={})) as commit,
    ):
        await host.apply_daemon_worker_report(
            daemon_id="d-1",
            ticket_id="t-1",
            lease_id="lease-1",
            state="dropped",
            summary="No confirmed issue",
        )
    transition = commit.await_args.kwargs["transition"]
    assert transition["status"] == "completed"
    assert transition["relay"] is False
    assert transition["useful"] is False
    mandate_patch = commit.await_args.kwargs["mandate_patch"]
    if speculative:
        assert transition["summary"] == ""
        recorded = mandate_patch["speculative_outcome"]
        assert recorded["outcome"] == "discarded"
        assert recorded["evidence"]["worker_summary"] == "No confirmed issue"
        assert recorded["evidence"]["worker_state"] == "dropped"
    else:
        assert mandate_patch is None


@pytest.mark.asyncio
async def test_daemon_report_replays_a_persisted_attempt_result():
    from src.server.database.autonomy import daemons as daemon_db

    accepted = {"ticket_id": "t-1", "state": "ready"}
    with (
        patch.object(
            daemon_db,
            "daemon_report_context",
            AsyncMock(return_value={"report_result": accepted}),
        ),
        patch.object(daemon_db, "commit_daemon_report", AsyncMock()) as commit,
    ):
        result = await host.apply_daemon_worker_report(
            daemon_id="d-1",
            ticket_id="t-1",
            lease_id="old-lease",
            state="ready",
            summary="duplicate",
        )
    assert result == accepted
    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_report_rejects_an_older_runs_result_on_a_reused_thread():
    ticket = {
        "ticket_id": "t-1",
        "user_id": "u-1",
        "status": "running",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-2",
        "mandate": {"source_id": "s-1"},
    }
    with patch.object(host.ticket_db, "get_ticket", AsyncMock(return_value=ticket)):
        with pytest.raises(host.StaleWorkerReport):
            await host.apply_worker_report(
                ticket_id="t-1",
                state="ready",
                summary="older attempt",
                expected_thread_id="thread-1",
                expected_run_id="run-1",
            )


@pytest.mark.asyncio
async def test_worker_report_passes_the_run_fence_to_settlement():
    ticket = {
        "ticket_id": "t-1",
        "user_id": "u-1",
        "status": "running",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-1",
        "mandate": {"source_id": "s-1"},
    }
    record = AsyncMock(return_value={"report_id": "r-1"})
    with (
        patch.object(host.ticket_db, "get_ticket", AsyncMock(return_value=ticket)),
        patch.object(host.ticket_db, "record_worker_report", record),
        patch.object(host.source_db, "get_source_any_user", AsyncMock(return_value=None)),
        patch.object(host, "enqueue_effect", AsyncMock()),
    ):
        await host.apply_worker_report(
            ticket_id="t-1",
            state="ready",
            summary="done",
            expected_thread_id="thread-1",
            expected_run_id="run-1",
        )

    assert record.await_args.kwargs["expected_run_id"] == "run-1"
