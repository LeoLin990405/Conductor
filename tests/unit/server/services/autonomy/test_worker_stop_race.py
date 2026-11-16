"""A backend Worker launch must lose a race with a Ticket stop.

The stop request verifies a Ticket with no runner as ``not_started``. That claim
is only true if a launch already in flight cannot come back afterwards and mark
the cancelled Ticket running, or start a run for it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from src.server.services.autonomy import worker
from src.server.services.projects.dispatch import DispatchError

TICKET_ID = "11111111-1111-4111-8111-111111111111"


def _queued_ticket() -> dict:
    return {
        "ticket_id": TICKET_ID,
        "user_id": "u1",
        "workspace_id": "w1",
        "title": "Ship it",
        "status": "queued",
        "worker_provider": "",
        "worker_model": "",
        "mandate": {},
        "attempts": 0,
    }


class _Store:
    """Ticket rows with the same guarded-update contract as the database."""

    def __init__(self, ticket: dict):
        self.ticket = dict(ticket)

    async def get(self, ticket_id: str):
        return dict(self.ticket) if ticket_id == self.ticket["ticket_id"] else None

    async def update(self, ticket_id: str, *, expected_status=None, **fields):
        if ticket_id != self.ticket["ticket_id"]:
            return None
        if expected_status is not None and self.ticket["status"] != expected_status:
            return None
        self.ticket.update(fields)
        return dict(self.ticket)


class _Dispatcher:
    def __init__(self, store: _Store, *, stop_at: str | None = None):
        self.store = store
        self.stop_at = stop_at
        self.dispatched: list[dict] = []
        self.cancelled: list[dict] = []

    async def create_thread(self, **kwargs):
        if self.stop_at == "create_thread":
            self.store.ticket["status"] = "cancelled"
        return "thread-1"

    async def dispatch_turn(self, **kwargs):
        self.dispatched.append(kwargs)
        if self.stop_at == "dispatch_turn":
            self.store.ticket["status"] = "cancelled"
        return SimpleNamespace(run_id="run-1")

    async def cancel_thread(self, **kwargs):
        self.cancelled.append(kwargs)
        return True


def _wire(monkeypatch, store: _Store, dispatcher: _Dispatcher):
    async def no_pending_instructions(ticket_id, **kwargs):
        return []

    @asynccontextmanager
    async def no_handoff_lock(ticket_id):
        yield

    monkeypatch.setattr(worker.ticket_db, "get_ticket", store.get)
    monkeypatch.setattr(worker.ticket_db, "update_ticket", store.update)
    monkeypatch.setattr(
        worker.ticket_db, "list_undelivered_instructions", no_pending_instructions
    )
    monkeypatch.setattr(worker, "_instruction_handoff_lock", no_handoff_lock)
    monkeypatch.setattr(worker, "ProjectDispatcher", lambda: dispatcher)


@pytest.mark.asyncio
async def test_a_stop_during_thread_creation_prevents_the_worker_run(monkeypatch):
    store = _Store(_queued_ticket())
    dispatcher = _Dispatcher(store, stop_at="create_thread")
    _wire(monkeypatch, store, dispatcher)

    await worker._launch_queued(dict(store.ticket), TICKET_ID, "prompt")

    assert store.ticket["status"] == "cancelled"
    assert store.ticket.get("worker_thread_id") is None
    assert dispatcher.dispatched == []


@pytest.mark.asyncio
async def test_a_stop_during_dispatch_cancels_the_run_it_started(monkeypatch):
    store = _Store(_queued_ticket())
    dispatcher = _Dispatcher(store, stop_at="dispatch_turn")
    _wire(monkeypatch, store, dispatcher)

    await worker._launch_queued(dict(store.ticket), TICKET_ID, "prompt")

    assert store.ticket["status"] == "cancelled"
    assert len(dispatcher.dispatched) == 1
    assert dispatcher.cancelled == [
        {"user_id": "u1", "thread_id": "thread-1", "run_id": "run-1"}
    ]


@pytest.mark.asyncio
async def test_a_dispatch_failure_does_not_overwrite_a_stop(monkeypatch):
    store = _Store(_queued_ticket())
    dispatcher = _Dispatcher(store)

    async def fail_turn(**kwargs):
        store.ticket["status"] = "cancelled"
        raise DispatchError("connection reset")

    dispatcher.dispatch_turn = fail_turn
    _wire(monkeypatch, store, dispatcher)

    with pytest.raises(DispatchError):
        await worker._launch_queued(dict(store.ticket), TICKET_ID, "prompt")

    assert store.ticket["status"] == "cancelled"
