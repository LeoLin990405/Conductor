"""Cloud Worker launch rejects unisolated work without clobbering other routes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.server.services.autonomy import worker


def _ticket(**overrides):
    return {
        "ticket_id": "ticket-1",
        "status": "queued",
        "revision": 7,
        "worker_daemon_id": None,
        "worker_thread_id": None,
        "worker_run_id": None,
        "mandate": {},
        **overrides,
    }


def test_archived_ticket_is_not_an_unisolated_cloud_dispatch():
    # Archive is terminal and does not set closed_at, so the predicate names it.
    assert worker.is_unisolated_cloud_worker(_ticket(status="archived")) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ticket",
    [
        _ticket(),
        _ticket(
            status="running",
            worker_thread_id="legacy-thread",
            worker_run_id=None,
        ),
    ],
)
async def test_cloud_worker_launch_is_failed_closed_with_snapshot_guard(monkeypatch, ticket):
    get_ticket = AsyncMock(return_value=ticket)
    reject = AsyncMock(return_value={"ticket_id": "ticket-1"})
    monkeypatch.setattr(worker.ticket_db, "get_ticket", get_ticket)
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)

    await worker.launch_worker_effect(SimpleNamespace(payload={"ticket_id": "ticket-1"}))

    get_ticket.assert_awaited_once_with("ticket-1")
    reject.assert_awaited_once()
    passed_snapshot = reject.await_args.args[0]
    assert passed_snapshot is ticket
    assert reject.await_args.kwargs["error"].startswith(
        "Cloud Worker execution is disabled"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ticket",
    [
        None,
        _ticket(status="waiting"),
        _ticket(status="failed"),
        _ticket(status="completed"),
        _ticket(status="cancelled"),
        _ticket(status="archived"),
        _ticket(status="running", worker_thread_id="active-thread", worker_run_id="run-1"),
        _ticket(worker_daemon_id="daemon-1"),
        _ticket(mandate={"kind": "main_agent_root", "orchestration_only": True}),
        _ticket(mandate={"kind": "main_agent_child", "orchestration_child": True}),
    ],
)
async def test_cloud_worker_launch_leaves_other_states_and_routes_untouched(
    monkeypatch, ticket
):
    monkeypatch.setattr(worker.ticket_db, "get_ticket", AsyncMock(return_value=ticket))
    reject = AsyncMock()
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)

    await worker.launch_worker_effect(SimpleNamespace(payload={"ticket_id": "ticket-1"}))

    reject.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_worker_launch_without_revision_does_not_make_unfenced_update(monkeypatch):
    monkeypatch.setattr(
        worker.ticket_db, "get_ticket", AsyncMock(return_value=_ticket(revision=None))
    )
    reject = AsyncMock()
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)

    await worker.launch_worker_effect(SimpleNamespace(payload={"ticket_id": "ticket-1"}))

    reject.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_worker_launch_rereads_and_retries_when_revision_races(monkeypatch):
    # A concurrent input delivery bumps the ticket revision between the read
    # and the guarded rejection, so the first UPDATE misses. The handler must
    # re-read and retry while the ticket is still an eligible cloud launch.
    ticket = _ticket()
    get_ticket = AsyncMock(return_value=ticket)
    reject = AsyncMock(side_effect=[None, {"ticket_id": "ticket-1"}])
    monkeypatch.setattr(worker.ticket_db, "get_ticket", get_ticket)
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)

    await worker.launch_worker_effect(SimpleNamespace(payload={"ticket_id": "ticket-1"}))

    assert get_ticket.await_count == 2
    assert reject.await_count == 2
    assert reject.await_args_list[0].args[0] is ticket
    assert reject.await_args_list[1].args[0] is ticket


@pytest.mark.asyncio
async def test_cloud_worker_launch_stops_retrying_once_no_longer_eligible(monkeypatch):
    # After the raced miss, the re-read shows a daemon claimed the ticket: the
    # handler must stop quietly instead of rejecting someone else's launch.
    raced = _ticket()
    claimed = _ticket(worker_daemon_id="daemon-1")
    get_ticket = AsyncMock(side_effect=[raced, claimed])
    reject = AsyncMock(return_value=None)
    monkeypatch.setattr(worker.ticket_db, "get_ticket", get_ticket)
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)

    await worker.launch_worker_effect(SimpleNamespace(payload={"ticket_id": "ticket-1"}))

    assert get_ticket.await_count == 2
    assert reject.await_count == 1


@pytest.mark.asyncio
async def test_cloud_worker_launch_fails_effect_when_rejection_keeps_racing(monkeypatch):
    # Still contested on every fresh read: the handler must raise so the
    # effect runner nacks the row back to pending (it retries with backoff)
    # instead of acknowledging a launch that never settled.
    monkeypatch.setattr(
        worker.ticket_db, "get_ticket", AsyncMock(return_value=_ticket())
    )
    reject = AsyncMock(return_value=None)
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)

    with pytest.raises(RuntimeError, match="kept racing"):
        await worker.launch_worker_effect(
            SimpleNamespace(payload={"ticket_id": "ticket-1"})
        )

    assert reject.await_count == worker._MAX_REJECTION_READS


def test_native_speculative_prompt_keeps_observe_mode_read_only():
    prompt = worker.worker_prompt(
        {"ticket_id": "t1", "scope": "Review this"},
        {},
        native=True,
        speculative=True,
    )
    assert "Do not modify files" in prompt
    assert '"needs_review"' in prompt
    assert "AUTONOMY_STATE" not in prompt
