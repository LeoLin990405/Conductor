"""A follow-up on a Thread goes to its owner before an attached subscriber."""

from contextlib import asynccontextmanager, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.server.services.autonomy import intake


@asynccontextmanager
async def _null_routing_lock(thread_key: str, *, timeout_seconds: float = 30.0):
    yield


@pytest.fixture(autouse=True)
def _routing_lock():
    """The advisory routing lock needs a real Postgres session; these tests
    patch the DB functions, so the lock becomes a no-op."""
    with patch(
        "src.server.services.autonomy.intake.ticket_db.thread_routing_lock",
        MagicMock(side_effect=_null_routing_lock),
    ):
        yield


def _source():
    return {
        "source_id": "s1",
        "user_id": "u1",
        "config": {"allowed_logins": ["alice"]},
    }


def _draft():
    return {
        "trigger_id": "trg1",
        "external_id": "m1",
        "kind": "comment",
        "thread_key": "thread-1",
        "actor_login": "alice",
        "actor_is_bot": False,
        "addressed": True,
        "text": "follow up",
        "url": "",
        "payload": {},
    }


@pytest.mark.asyncio
async def test_the_thread_owner_wins_over_an_attached_subscriber():
    attached = {
        "ticket_id": "attached",
        "status": "running",
        "origin_thread_key": "other-thread",
        "subscription_kind": "own",
    }
    owner = {
        "ticket_id": "owner",
        "status": "running",
        "origin_thread_key": "thread-1",
        "subscription_kind": "own",
    }
    add_input = AsyncMock(return_value={"input_id": "in1"})
    lock = MagicMock(side_effect=_null_routing_lock)
    with (
        patch(
            "src.server.services.autonomy.intake.trigger_db.insert_trigger",
            AsyncMock(return_value={"trigger_id": "trg1"}),
        ),
        patch(
            "src.server.services.autonomy.intake.trigger_db.ensure_thread",
            AsyncMock(),
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.thread_routing_lock",
            lock,
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.list_tickets_for_thread",
            AsyncMock(return_value=[attached, owner]),
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.add_input",
            add_input,
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.context_subscribers",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.server.services.autonomy.intake.enqueue_effect",
            AsyncMock(return_value=True),
        ) as effect,
    ):
        assert await intake.receive_trigger(source=_source(), draft=_draft())

    # Routing runs under the Thread lock, serialized against reassignment.
    lock.assert_called_once_with("thread-1")
    assert add_input.call_args.kwargs["ticket_id"] == "owner"
    assert add_input.call_args.kwargs["only_if_owning"] is True
    assert effect.call_args.kwargs["payload"]["ticket_id"] == "owner"


@pytest.mark.asyncio
async def test_without_an_owner_the_first_live_subscriber_gets_it():
    attached = {
        "ticket_id": "attached",
        "status": "waiting",
        "origin_thread_key": "other-thread",
    }
    add_input = AsyncMock(return_value={"input_id": "in1"})
    with (
        patch(
            "src.server.services.autonomy.intake.trigger_db.insert_trigger",
            AsyncMock(return_value={"trigger_id": "trg1"}),
        ),
        patch(
            "src.server.services.autonomy.intake.trigger_db.ensure_thread",
            AsyncMock(),
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.list_tickets_for_thread",
            AsyncMock(return_value=[attached]),
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.add_input",
            add_input,
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.context_subscribers",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.server.services.autonomy.intake.enqueue_effect",
            AsyncMock(return_value=True),
        ),
    ):
        assert await intake.receive_trigger(source=_source(), draft=_draft())
    assert add_input.call_args.kwargs["ticket_id"] == "attached"


@pytest.mark.asyncio
async def test_without_an_owner_an_own_subscriber_wins_over_a_context_only_one():
    context_only = {
        "ticket_id": "context-only",
        "status": "running",
        "origin_thread_key": "other-thread",
        "subscription_kind": "context",
    }
    own_subscriber = {
        "ticket_id": "own-sub",
        "status": "running",
        "origin_thread_key": "another-thread",
        "subscription_kind": "own",
    }
    add_input = AsyncMock(return_value={"input_id": "in1"})
    with (
        patch(
            "src.server.services.autonomy.intake.trigger_db.insert_trigger",
            AsyncMock(return_value={"trigger_id": "trg1"}),
        ),
        patch(
            "src.server.services.autonomy.intake.trigger_db.ensure_thread",
            AsyncMock(),
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.list_tickets_for_thread",
            AsyncMock(return_value=[context_only, own_subscriber]),
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.add_input",
            add_input,
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.context_subscribers",
            AsyncMock(return_value=[context_only]),
        ),
        patch(
            "src.server.services.autonomy.intake.enqueue_effect",
            AsyncMock(return_value=True),
        ) as effect,
    ):
        assert await intake.receive_trigger(source=_source(), draft=_draft())

    assert add_input.await_args_list[0].kwargs["ticket_id"] == "own-sub"
    assert add_input.await_args_list[0].kwargs["kind"] == "trigger"
    assert add_input.await_args_list[0].kwargs["only_if_owning"] is True
    # The context-only subscriber still only gets a stored kind="context" input.
    assert add_input.await_args_list[1].kwargs["ticket_id"] == "context-only"
    assert add_input.await_args_list[1].kwargs["kind"] == "context"
    assert add_input.await_args_list[1].kwargs.get("only_if_owning") is not True
    assert effect.await_count == 1


@pytest.mark.asyncio
async def test_context_subscribers_get_a_stored_input_without_delivery():
    owner = {
        "ticket_id": "owner",
        "status": "running",
        "origin_thread_key": "thread-1",
    }
    add_input = AsyncMock(return_value={"input_id": "in-1"})
    with (
        patch(
            "src.server.services.autonomy.intake.trigger_db.insert_trigger",
            AsyncMock(return_value={"trigger_id": "trg1"}),
        ),
        patch(
            "src.server.services.autonomy.intake.trigger_db.ensure_thread",
            AsyncMock(),
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.list_tickets_for_thread",
            AsyncMock(return_value=[owner]),
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.context_subscribers",
            AsyncMock(return_value=[{"ticket_id": "ctx"}]),
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.add_input",
            add_input,
        ),
        patch(
            "src.server.services.autonomy.intake.enqueue_effect",
            AsyncMock(return_value=True),
        ) as effect,
    ):
        assert await intake.receive_trigger(source=_source(), draft=_draft())

    assert [call.kwargs["ticket_id"] for call in add_input.await_args_list] == [
        "owner",
        "ctx",
    ]
    assert add_input.await_args_list[0].kwargs["only_if_owning"] is True
    assert add_input.await_args_list[1].kwargs["kind"] == "context"
    assert add_input.await_args_list[1].kwargs.get("only_if_owning") is not True
    assert effect.await_count == 1


@contextmanager
def _patched(tickets, *, add_input, effect, pending):
    with (
        patch(
            "src.server.services.autonomy.intake.trigger_db.insert_trigger",
            AsyncMock(return_value={"trigger_id": "trg1"}),
        ),
        patch(
            "src.server.services.autonomy.intake.trigger_db.ensure_thread",
            AsyncMock(),
        ),
        patch(
            "src.server.services.autonomy.intake.trigger_db.append_pending_trigger",
            pending,
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.list_tickets_for_thread",
            AsyncMock(return_value=tickets),
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.add_input",
            add_input,
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.context_subscribers",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.server.services.autonomy.intake.enqueue_effect",
            effect,
        ),
    ):
        yield


def _discarded(**overrides):
    ticket = {
        "ticket_id": "discarded",
        "status": "completed",
        "origin_thread_key": "thread-1",
        "subscription_kind": "own",
        "mandate": {
            "speculative": True,
            "speculative_outcome": {"outcome": "discarded"},
        },
    }
    ticket.update(overrides)
    return ticket


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ticket",
    [
        _discarded(worker_thread_id="th-1"),
        _discarded(worker_daemon_id="d-1"),
    ],
)
async def test_a_discarded_plan_leaves_the_next_signal_pending(ticket):
    add_input = AsyncMock(return_value={"input_id": "in1"})
    effect = AsyncMock(return_value=True)
    pending = AsyncMock()
    with _patched([ticket], add_input=add_input, effect=effect, pending=pending):
        assert await intake.receive_trigger(source=_source(), draft=_draft())

    add_input.assert_not_awaited()
    effect.assert_not_awaited()
    pending.assert_awaited_once_with("thread-1", "trg1")


@pytest.mark.asyncio
async def test_discard_between_selection_and_insert_leaves_the_signal_pending():
    """The plan looked live, then discard won the insert lock."""
    live = {
        "ticket_id": "plan",
        "status": "waiting",
        "origin_thread_key": "thread-1",
        "subscription_kind": "own",
        "worker_thread_id": "th-1",
        "mandate": {"speculative": True},
    }
    add_input = AsyncMock(return_value=None)
    effect = AsyncMock(return_value=True)
    pending = AsyncMock()
    with _patched([live], add_input=add_input, effect=effect, pending=pending):
        assert await intake.receive_trigger(source=_source(), draft=_draft())

    assert add_input.await_args.kwargs["only_if_owning"] is True
    effect.assert_not_awaited()
    pending.assert_awaited_once_with("thread-1", "trg1")


@pytest.mark.asyncio
async def test_a_lost_insert_fence_tries_the_next_live_owner():
    discarded = {
        "ticket_id": "soon",
        "status": "waiting",
        "origin_thread_key": "thread-1",
        "subscription_kind": "own",
        "worker_thread_id": "th-old",
        "mandate": {"speculative": True},
    }
    other = {
        "ticket_id": "other",
        "status": "running",
        "origin_thread_key": "thread-1",
        "subscription_kind": "own",
        "worker_thread_id": "th-live",
        "mandate": {},
    }

    effect = AsyncMock(return_value=True)
    pending = AsyncMock()
    with _patched(
        [discarded, other],
        add_input=AsyncMock(side_effect=[None, {"input_id": "in-2"}]),
        effect=effect,
        pending=pending,
    ):
        assert await intake.receive_trigger(source=_source(), draft=_draft())

    assert effect.await_args.kwargs["payload"]["ticket_id"] == "other"
    pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_discarded_plan_does_not_outrank_a_live_owner():
    live = {
        "ticket_id": "owner",
        "status": "running",
        "origin_thread_key": "thread-1",
        "subscription_kind": "own",
        "worker_thread_id": "th-live",
        "mandate": {},
    }
    add_input = AsyncMock(return_value={"input_id": "in1"})
    effect = AsyncMock(return_value=True)
    pending = AsyncMock()
    with _patched(
        [_discarded(worker_thread_id="th-old"), live],
        add_input=add_input,
        effect=effect,
        pending=pending,
    ):
        assert await intake.receive_trigger(source=_source(), draft=_draft())

    assert add_input.await_args.kwargs["ticket_id"] == "owner"
    assert effect.await_args.kwargs["payload"]["ticket_id"] == "owner"
    pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_ordinary_completed_ticket_leaves_the_signal_pending():
    """Under the lifecycle rule a terminal Ticket is not a live subscriber, so
    an ordinary completed Ticket no longer receives the follow-up either: the
    trigger stays pending for a new Eval."""
    finished = {
        "ticket_id": "done",
        "status": "completed",
        "origin_thread_key": "thread-1",
        "subscription_kind": "own",
        "worker_thread_id": "th-1",
        "mandate": {"source_id": "s1"},
    }
    add_input = AsyncMock(return_value={"input_id": "in1"})
    effect = AsyncMock(return_value=True)
    pending = AsyncMock()
    with _patched([finished], add_input=add_input, effect=effect, pending=pending):
        assert await intake.receive_trigger(source=_source(), draft=_draft())

    add_input.assert_not_awaited()
    effect.assert_not_awaited()
    pending.assert_awaited_once_with("thread-1", "trg1")


@pytest.mark.asyncio
async def test_a_cancelled_ticket_leaves_the_signal_pending():
    cancelled = {
        "ticket_id": "gone",
        "status": "cancelled",
        "origin_thread_key": "thread-1",
        "subscription_kind": "own",
        "worker_thread_id": "th-1",
        "mandate": {},
    }
    add_input = AsyncMock(return_value={"input_id": "in1"})
    effect = AsyncMock(return_value=True)
    pending = AsyncMock()
    with _patched([cancelled], add_input=add_input, effect=effect, pending=pending):
        assert await intake.receive_trigger(source=_source(), draft=_draft())

    add_input.assert_not_awaited()
    effect.assert_not_awaited()
    pending.assert_awaited_once_with("thread-1", "trg1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("routing_mode", "addressed"),
    [("ignore", True), ("respond-only-explicit", False)],
)
async def test_source_mode_blocks_ignored_or_non_explicit_followups(
    routing_mode: str, addressed: bool
):
    source = _source()
    source["config"]["routing_mode"] = routing_mode
    draft = {**_draft(), "addressed": addressed}
    with patch(
        "src.server.services.autonomy.intake.trigger_db.insert_trigger",
        AsyncMock(),
    ) as insert:
        assert not await intake.receive_trigger(source=source, draft=draft)
    insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_completed_ticket_no_longer_receives_followups():
    """A terminal Ticket is not a live subscriber; the trigger returns to Eval."""
    completed = {
        "ticket_id": "done-ticket",
        "status": "completed",
        "origin_thread_key": "thread-1",
    }
    with (
        patch(
            "src.server.services.autonomy.intake.trigger_db.insert_trigger",
            AsyncMock(return_value={"trigger_id": "trg1"}),
        ),
        patch(
            "src.server.services.autonomy.intake.trigger_db.ensure_thread",
            AsyncMock(),
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.list_tickets_for_thread",
            AsyncMock(return_value=[completed]),
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.add_input",
            AsyncMock(),
        ) as add_input,
        patch(
            "src.server.services.autonomy.intake.enqueue_effect",
            AsyncMock(),
        ),
        patch(
            "src.server.services.autonomy.intake.trigger_db.append_pending_trigger",
            AsyncMock(),
        ) as append,
    ):
        assert await intake.receive_trigger(source=_source(), draft=_draft())

    add_input.assert_not_awaited()
    append.assert_awaited_once_with("thread-1", "trg1")


@pytest.mark.asyncio
async def test_an_archived_ticket_no_longer_receives_followups():
    """Archived is terminal, same as completed: the trigger returns to Eval."""
    archived = {
        "ticket_id": "done-ticket",
        "status": "archived",
        "origin_thread_key": "thread-1",
    }
    with (
        patch(
            "src.server.services.autonomy.intake.trigger_db.insert_trigger",
            AsyncMock(return_value={"trigger_id": "trg1"}),
        ),
        patch(
            "src.server.services.autonomy.intake.trigger_db.ensure_thread",
            AsyncMock(),
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.list_tickets_for_thread",
            AsyncMock(return_value=[archived]),
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.add_input",
            AsyncMock(),
        ) as add_input,
        patch(
            "src.server.services.autonomy.intake.enqueue_effect",
            AsyncMock(),
        ),
        patch(
            "src.server.services.autonomy.intake.trigger_db.append_pending_trigger",
            AsyncMock(),
        ) as append,
    ):
        assert await intake.receive_trigger(source=_source(), draft=_draft())

    add_input.assert_not_awaited()
    append.assert_awaited_once_with("thread-1", "trg1")


@pytest.mark.asyncio
async def test_a_context_only_refollow_does_not_reclaim_routing_after_a_handoff():
    """After A->B reassignment, A's context-only re-follow keeps context
    semantics: A opened the Thread, but its subscription is 'context' now, so
    the follow-up routes to the Ticket that took the Thread over."""
    former_owner = {
        "ticket_id": "former-owner",
        "status": "running",
        "origin_thread_key": "thread-1",
        "subscription_kind": "context",
    }
    new_owner = {
        "ticket_id": "new-owner",
        "status": "running",
        "origin_thread_key": "other-thread",
        "subscription_kind": "own",
    }
    add_input = AsyncMock(return_value={"input_id": "in1"})
    with (
        patch(
            "src.server.services.autonomy.intake.trigger_db.insert_trigger",
            AsyncMock(return_value={"trigger_id": "trg1"}),
        ),
        patch(
            "src.server.services.autonomy.intake.trigger_db.ensure_thread",
            AsyncMock(),
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.list_tickets_for_thread",
            AsyncMock(return_value=[former_owner, new_owner]),
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.add_input",
            add_input,
        ),
        patch(
            "src.server.services.autonomy.intake.ticket_db.context_subscribers",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.server.services.autonomy.intake.enqueue_effect",
            AsyncMock(return_value=True),
        ) as effect,
    ):
        assert await intake.receive_trigger(source=_source(), draft=_draft())

    assert add_input.call_args.kwargs["ticket_id"] == "new-owner"
    assert effect.call_args.kwargs["payload"]["ticket_id"] == "new-owner"
