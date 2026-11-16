"""Ticket replies stay durable without resuming an unisolated cloud Worker."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException

from src.server.app import autonomy
from src.server.models.autonomy import TicketReplyRequest
from src.server.services.autonomy import worker


@pytest.mark.asyncio
async def test_daemon_ticket_reply_is_queued_even_with_a_stale_worker_thread(monkeypatch):
    ticket = {
        "ticket_id": "ticket-1",
        "user_id": "user-1",
        "status": "waiting",
        "worker_daemon_id": "daemon-1",
        "worker_thread_id": "legacy-thread",
    }
    monkeypatch.setattr(autonomy, "_require_feature", AsyncMock())
    monkeypatch.setattr(autonomy.tickets_db, "get_ticket", AsyncMock(return_value=ticket))
    add_input = AsyncMock(return_value={"input_id": "input-1"})
    monkeypatch.setattr(autonomy.tickets_db, "add_input", add_input)
    mark = AsyncMock()
    monkeypatch.setattr(
        "src.server.services.autonomy.inputs.mark_cloud_input_for_review", mark
    )

    result = await autonomy.reply_ticket(
        UUID("00000000-0000-0000-0000-000000000001"),
        TicketReplyRequest(text="Continue safely"),
        "user-1",
    )

    assert result == {"input_id": "input-1", "status": "queued_for_daemon"}
    add_input.assert_awaited_once_with(
        ticket_id="00000000-0000-0000-0000-000000000001",
        trigger_id=None,
        kind="answer",
        text="Continue safely",
    )
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_cloud_reply_is_saved_for_review_without_dispatch(monkeypatch):
    ticket = {
        "ticket_id": "00000000-0000-0000-0000-000000000001",
        "user_id": "user-1",
        "status": "blocked",
        "worker_daemon_id": None,
        "worker_thread_id": "legacy-thread",
    }
    monkeypatch.setattr(autonomy, "_require_feature", AsyncMock())
    monkeypatch.setattr(autonomy.tickets_db, "get_ticket", AsyncMock(return_value=ticket))
    add_input = AsyncMock(return_value={"input_id": "input-1"})
    monkeypatch.setattr(autonomy.tickets_db, "add_input", add_input)
    mark = AsyncMock()
    monkeypatch.setattr(
        "src.server.services.autonomy.inputs.mark_cloud_input_for_review", mark
    )

    result = await autonomy.reply_ticket(
        UUID(ticket["ticket_id"]), TicketReplyRequest(text="Please continue"), "user-1"
    )

    assert result == {"input_id": "input-1", "status": "stored_for_review"}
    add_input.assert_awaited_once()
    mark.assert_awaited_once_with(ticket, "input-1")


@pytest.mark.asyncio
async def test_cloud_reply_without_a_worker_thread_queues_on_the_task(monkeypatch):
    """A reply before any worker session is an instruction, not a cloud launch."""
    ticket = {
        "ticket_id": "00000000-0000-0000-0000-000000000001",
        "user_id": "user-1",
        "status": "queued",
        "worker_daemon_id": None,
        "worker_thread_id": None,
    }
    monkeypatch.setattr(autonomy, "_require_feature", AsyncMock())
    monkeypatch.setattr(autonomy.tickets_db, "get_ticket", AsyncMock(return_value=ticket))
    add_input = AsyncMock()
    monkeypatch.setattr(autonomy.tickets_db, "add_input", add_input)

    @asynccontextmanager
    async def _noop_handoff(_ticket_id):
        yield

    stored = AsyncMock(return_value={"input_id": "input-1"})
    monkeypatch.setattr(worker, "_instruction_handoff_lock", _noop_handoff)
    monkeypatch.setattr(worker.ticket_db, "get_ticket", AsyncMock(return_value=ticket))
    monkeypatch.setattr(worker.ticket_db, "add_input", stored)

    result = await autonomy.reply_ticket(
        UUID(ticket["ticket_id"]), TicketReplyRequest(text="Continue"), "user-1"
    )

    assert result == {"input_id": "input-1", "status": "queued_on_task"}
    add_input.assert_not_awaited()
    stored.assert_awaited_once_with(
        ticket_id=ticket["ticket_id"],
        trigger_id=None,
        kind="instruction",
        text="Continue",
    )


@pytest.mark.asyncio
async def test_reply_reread_of_an_archived_ticket_does_not_store_a_review(monkeypatch):
    """Archive ends a ticket without closed_at, so the second read must see it.

    The first read can still be a live cloud worker. If archive lands before
    the re-read, the reply must not write a review input onto the archived row.
    """
    open_ticket = {
        "ticket_id": "00000000-0000-0000-0000-000000000001",
        "user_id": "user-1",
        "status": "running",
        "worker_daemon_id": None,
        "worker_thread_id": "legacy-thread",
    }
    archived = {**open_ticket, "status": "archived"}
    monkeypatch.setattr(autonomy, "_require_feature", AsyncMock())
    monkeypatch.setattr(
        autonomy.tickets_db, "get_ticket", AsyncMock(side_effect=[open_ticket, archived])
    )
    add_input = AsyncMock()
    monkeypatch.setattr(autonomy.tickets_db, "add_input", add_input)
    mark = AsyncMock()
    monkeypatch.setattr(
        "src.server.services.autonomy.inputs.mark_cloud_input_for_review", mark
    )

    with pytest.raises(HTTPException) as exc:
        await autonomy.reply_ticket(
            UUID(open_ticket["ticket_id"]),
            TicketReplyRequest(text="Please continue"),
            "user-1",
        )

    assert exc.value.status_code == 409
    add_input.assert_not_awaited()
    mark.assert_not_awaited()
