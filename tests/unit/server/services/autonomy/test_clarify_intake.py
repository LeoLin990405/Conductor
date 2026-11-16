"""A channel follow-up on a parked Ticket confirms the clarification."""

from contextlib import ExitStack, asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.server.services.autonomy import intake

_MOD = "src.server.services.autonomy.intake"


@asynccontextmanager
async def _null_routing_lock(thread_key: str, *, timeout_seconds: float = 30.0):
    yield


@pytest.fixture(autouse=True)
def _routing_lock():
    """The advisory routing lock needs a real Postgres session; these tests
    patch the DB functions, so the lock becomes a no-op."""
    with patch(
        f"{_MOD}.ticket_db.thread_routing_lock",
        MagicMock(side_effect=_null_routing_lock),
    ):
        yield


def _source():
    return {
        "source_id": "s1",
        "user_id": "u1",
        "config": {"allowed_logins": ["alice"]},
    }


def _draft(text: str = "follow up"):
    return {
        "trigger_id": "trg1",
        "external_id": "m1",
        "kind": "comment",
        "thread_key": "thread-1",
        "actor_login": "alice",
        "actor_is_bot": False,
        "addressed": True,
        "text": text,
        "url": "",
        "payload": {},
    }


def _ticket(**overrides):
    ticket = {
        "ticket_id": "t-1",
        "status": "awaiting_clarification",
        "origin_thread_key": "thread-1",
    }
    ticket.update(overrides)
    return ticket


def _enter_common(stack: ExitStack, tickets: list) -> dict:
    return {
        "insert": stack.enter_context(
            patch(
                f"{_MOD}.trigger_db.insert_trigger",
                AsyncMock(return_value={"trigger_id": "trg1"}),
            )
        ),
        "ensure_thread": stack.enter_context(
            patch(f"{_MOD}.trigger_db.ensure_thread", AsyncMock())
        ),
        "list_for_thread": stack.enter_context(
            patch(
                f"{_MOD}.ticket_db.list_tickets_for_thread",
                AsyncMock(return_value=tickets),
            )
        ),
        "add_input": stack.enter_context(
            patch(
                f"{_MOD}.ticket_db.add_input",
                AsyncMock(return_value={"input_id": "in1"}),
            )
        ),
        "context_subscribers": stack.enter_context(
            patch(
                f"{_MOD}.ticket_db.context_subscribers",
                AsyncMock(return_value=[]),
            )
        ),
        "answer": stack.enter_context(
            patch(f"{_MOD}.answer_via_channel", AsyncMock(return_value={"ticket_id": "t-1"}))
        ),
        "effect": stack.enter_context(
            patch(f"{_MOD}.enqueue_effect", AsyncMock(return_value=True))
        ),
    }


@pytest.mark.asyncio
async def test_a_reply_to_a_parked_ticket_confirms_the_clarification():
    with ExitStack() as stack:
        mocks = _enter_common(stack, [_ticket()])
        assert await intake.receive_trigger(
            source=_source(), draft=_draft("Yes, fix the auth bug.")
        )
        mocks["answer"].assert_awaited_once_with(
            ticket=_ticket(), text="Yes, fix the auth bug."
        )
        mocks["effect"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_parked_ticket_with_a_worker_still_gets_the_input_delivered():
    with ExitStack() as stack:
        mocks = _enter_common(
            stack, [_ticket(status="awaiting_clarification", worker_thread_id="w-1")]
        )
        assert await intake.receive_trigger(
            source=_source(), draft=_draft("Yes, fix the auth bug.")
        )
        mocks["answer"].assert_not_awaited()
        mocks["effect"].assert_awaited_once()
        assert mocks["effect"].await_args.kwargs["kind"] == "deliver_input"


@pytest.mark.asyncio
async def test_a_queued_ticket_still_launches_a_worker():
    with ExitStack() as stack:
        mocks = _enter_common(stack, [_ticket(status="queued")])
        assert await intake.receive_trigger(
            source=_source(), draft=_draft("more context")
        )
        mocks["answer"].assert_not_awaited()
        mocks["effect"].assert_awaited_once()
        assert mocks["effect"].await_args.kwargs["kind"] == "launch_worker"
        assert mocks["effect"].await_args.kwargs["idempotency_key"] == (
            "autonomy:launch:t-1"
        )
