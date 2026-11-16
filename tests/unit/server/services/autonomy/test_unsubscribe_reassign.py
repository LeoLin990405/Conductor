"""Unsubscribe and reassignment: the subscription lifecycle's two surfaces.

A Worker ends one of its Ticket's subscriptions; the Router moves a Thread's
ownership to another live Ticket and the handoff ledger records the state the
new owner inherits.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.server.services.autonomy import host
from src.server.services.autonomy.host import AutonomyToolHost, AutonomyWorkerHost
from src.tools.autonomy import tools as autonomy_tools

_MOD = "src.server.services.autonomy.host"


@asynccontextmanager
async def _null_routing_lock(thread_key: str, *, timeout_seconds: float = 30.0):
    yield


@pytest.fixture(autouse=True)
def _routing_lock():
    """The advisory routing lock needs a real Postgres session; these tests
    assert the transition's calls, so the lock becomes a no-op."""
    with patch(
        f"{_MOD}.ticket_db.thread_routing_lock",
        MagicMock(side_effect=_null_routing_lock),
    ):
        yield


def _ticket(ticket_id: str, *, user_id: str = "u-1", status: str = "running", **extra):
    return {
        "ticket_id": ticket_id,
        "user_id": user_id,
        "status": status,
        **extra,
    }


class _TxConnection:
    """Stand-in for a pooled connection: records the transaction boundary."""

    def __init__(self):
        self.transactions = 0
        self.committed = 0
        self.rolled_back = 0

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        try:
            yield
        except BaseException:
            self.rolled_back += 1
            raise
        else:
            self.committed += 1


def _tx_connection():
    """A fake pooled connection plus the patch that serves it to the host."""
    conn = _TxConnection()

    @asynccontextmanager
    async def _cm():
        yield conn

    return conn, patch(f"{_MOD}.get_db_connection", _cm)


@pytest.mark.asyncio
async def test_worker_unsubscribe_ends_the_subscription():
    end = AsyncMock(return_value={"ticket_id": "ticket-1", "thread_key": "thread-1"})
    with (
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket("ticket-1")),
        ),
        patch(f"{_MOD}.ticket_db.end_subscription", end),
    ):
        result = await AutonomyWorkerHost(ticket_id="ticket-1", thread_id="thread-1").unsubscribe(
            thread_key="thread-1", reason="out of scope"
        )

    assert result["status"] == "unsubscribed"
    assert result["thread_key"] == "thread-1"
    end.assert_awaited_once_with(
        "ticket-1", "thread-1", reason=host.ticket_db.END_UNSUBSCRIBED
    )


@pytest.mark.asyncio
async def test_worker_unsubscribe_is_idempotent_when_not_subscribed():
    with (
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket("ticket-1")),
        ),
        patch(f"{_MOD}.ticket_db.end_subscription", AsyncMock(return_value=None)),
    ):
        result = await AutonomyWorkerHost(ticket_id="ticket-1", thread_id="thread-1").unsubscribe(
            thread_key="thread-1"
        )

    assert result["status"] == "not_subscribed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "archived"])
async def test_worker_unsubscribe_refuses_an_ended_ticket(status):
    with patch(
        f"{_MOD}.ticket_db.get_ticket",
        AsyncMock(return_value=_ticket("ticket-1", status=status)),
    ):
        with pytest.raises(ValueError, match="already ended"):
            await AutonomyWorkerHost(ticket_id="ticket-1", thread_id="thread-1").unsubscribe(
                thread_key="thread-1"
            )


@pytest.mark.asyncio
async def test_worker_unsubscribe_requires_a_thread_key():
    with patch(
        f"{_MOD}.ticket_db.get_ticket",
        AsyncMock(return_value=_ticket("ticket-1")),
    ):
        with pytest.raises(ValueError, match="thread_key"):
            await AutonomyWorkerHost(ticket_id="ticket-1", thread_id="thread-1").unsubscribe(thread_key="  ")


@pytest.mark.asyncio
async def test_reassign_hands_the_thread_off_with_state():
    end = AsyncMock(return_value={"ticket_id": "from-1", "thread_key": "thread-1"})
    add_sub = AsyncMock()
    move = AsyncMock(
        return_value=[{"input_id": "in-1", "trigger_id": "trg-1", "kind": "trigger", "text": "ping"}]
    )
    latest = AsyncMock(return_value={"report_id": "rep-9", "state": "progress", "summary": "half done"})
    record = AsyncMock(return_value={"handoff_id": "handoff-1"})
    enqueue = AsyncMock(return_value=True)
    refuse = AsyncMock(return_value={"ticket_id": "to-1", "status": "failed"})
    tx, tx_patch = _tx_connection()
    get_ticket = AsyncMock(
        side_effect=[
            _ticket("from-1"),
            _ticket("to-1", worker_thread_id="wt-1"),
            _ticket("to-1", worker_thread_id="wt-1"),
        ]
    )
    with (
        tx_patch,
        patch(f"{_MOD}.ticket_db.get_ticket", get_ticket),
        patch(f"{_MOD}.ticket_db.end_subscription", end),
        patch(f"{_MOD}.ticket_db.add_subscription", add_sub),
        patch(f"{_MOD}.ticket_db.move_undelivered_inputs", move),
        patch(f"{_MOD}.ticket_db.latest_report", latest),
        patch(f"{_MOD}.ticket_db.record_handoff", record),
        patch(f"{_MOD}.enqueue_effect", enqueue),
        patch(
            "src.server.services.autonomy.worker.refuse_unisolated_cloud_dispatch",
            refuse,
        ),
    ):
        result = await AutonomyToolHost(user_id="u-1", thread_id="router").reassign(
            thread_key="thread-1",
            from_ticket_id="from-1",
            to_ticket_id="to-1",
            reason="to-1 owns the fix",
        )

    assert result["decision"] == "reassigned"
    assert result["handoff_id"] == "handoff-1"
    assert result["moved_inputs"] == 1
    assert tx.transactions == 1
    assert tx.committed == 1
    # The target is re-read under its row lock inside the transition, so a
    # completion that lands after the early validation cannot be overwritten.
    assert get_ticket.await_args_list[2].kwargs == {"conn": tx, "for_update": True}
    end.assert_awaited_once_with(
        "from-1", "thread-1", reason=host.ticket_db.END_REASSIGNED, conn=tx
    )
    add_sub.assert_awaited_once_with("to-1", "thread-1", kind="own", conn=tx)
    move.assert_awaited_once_with(
        from_ticket_id="from-1", to_ticket_id="to-1", thread_key="thread-1", conn=tx
    )
    latest.assert_awaited_once_with("from-1", conn=tx)
    state = record.await_args.kwargs["state"]
    assert state["latest_report"]["summary"] == "half done"
    assert state["moved_input_ids"] == ["in-1"]
    assert state["moved_trigger_ids"] == ["trg-1"]
    assert record.await_args.kwargs["conn"] is tx
    # The moved input is not delivered on a cloud thread.
    enqueue.assert_not_awaited()
    refuse.assert_awaited_once()
    assert refuse.await_args.args[0]["ticket_id"] == "to-1"


@pytest.mark.asyncio
async def test_reassign_revalidates_the_target_under_its_row_lock():
    """The early read says the target is live, but a completion lands before
    the transition: the locked re-read inside the transaction must catch it,
    or the reactivated subscription and a later delivery would resurrect a
    completed ticket to running."""
    end = AsyncMock(return_value={"ticket_id": "from-1", "thread_key": "thread-1"})
    add_sub = AsyncMock()
    tx, tx_patch = _tx_connection()
    with (
        tx_patch,
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(
                side_effect=[
                    _ticket("from-1"),
                    _ticket("to-1", status="running", worker_thread_id="wt-1"),
                    _ticket("to-1", status="completed", worker_thread_id="wt-1"),
                ]
            ),
        ),
        patch(f"{_MOD}.ticket_db.end_subscription", end),
        patch(f"{_MOD}.ticket_db.add_subscription", add_sub),
    ):
        with pytest.raises(ValueError, match="already ended"):
            await AutonomyToolHost(user_id="u-1", thread_id="router").reassign(
                thread_key="thread-1",
                from_ticket_id="from-1",
                to_ticket_id="to-1",
            )

    assert tx.rolled_back == 1
    assert tx.committed == 0
    end.assert_not_awaited()
    add_sub.assert_not_awaited()


@pytest.mark.asyncio
async def test_reassign_routes_through_the_thread_lock():
    """Intake inserts follow-ups under the same per-Thread lock; reassignment
    must hold it across the whole transition or the two can interleave."""
    calls: list[str] = []

    @asynccontextmanager
    async def recording_lock(thread_key: str, *, timeout_seconds: float = 30.0):
        assert thread_key == "thread-1"
        calls.append("lock-enter")
        yield
        calls.append("lock-exit")

    tx, tx_patch = _tx_connection()
    real_transaction = tx.transaction

    @asynccontextmanager
    async def recording_transaction():
        calls.append("tx-begin")
        async with real_transaction():
            try:
                yield
            finally:
                calls.append("tx-end")

    tx.transaction = recording_transaction
    with (
        tx_patch,
        patch(
            f"{_MOD}.ticket_db.thread_routing_lock",
            MagicMock(side_effect=recording_lock),
        ),
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(
                side_effect=[
                    _ticket("from-1"),
                    _ticket("to-1", status="waiting"),
                    _ticket("to-1", status="waiting"),
                ]
            ),
        ),
        patch(
            f"{_MOD}.ticket_db.end_subscription",
            AsyncMock(return_value={"ticket_id": "from-1"}),
        ),
        patch(f"{_MOD}.ticket_db.add_subscription", AsyncMock()),
        patch(f"{_MOD}.ticket_db.move_undelivered_inputs", AsyncMock(return_value=[])),
        patch(f"{_MOD}.ticket_db.latest_report", AsyncMock(return_value=None)),
        patch(
            f"{_MOD}.ticket_db.record_handoff",
            AsyncMock(return_value={"handoff_id": "h-1"}),
        ),
        patch(f"{_MOD}.enqueue_effect", AsyncMock(return_value=True)),
    ):
        await AutonomyToolHost(user_id="u-1", thread_id="router").reassign(
            thread_key="thread-1", from_ticket_id="from-1", to_ticket_id="to-1"
        )

    assert calls == ["lock-enter", "tx-begin", "tx-end", "lock-exit"]


@pytest.mark.asyncio
async def test_reassign_does_not_enqueue_a_cloud_delivery_for_a_moved_input():
    """A cloud target used to enqueue deliver_input under a fresh key.

    That effect dispatched on the shared runtime. The refusal replaces it,
    so no delivery effect is written for an unisolated cloud worker.
    """
    enqueue = AsyncMock(return_value=True)
    tx, tx_patch = _tx_connection()
    with (
        tx_patch,
        patch(f"{_MOD}.ticket_db.get_ticket", AsyncMock(side_effect=[_ticket("from-1"), _ticket("to-1", worker_thread_id="wt-1"), _ticket("to-1", worker_thread_id="wt-1")])),
        patch(f"{_MOD}.ticket_db.end_subscription", AsyncMock(return_value={"ticket_id": "from-1"})),
        patch(f"{_MOD}.ticket_db.add_subscription", AsyncMock()),
        patch(
            f"{_MOD}.ticket_db.move_undelivered_inputs",
            AsyncMock(return_value=[{"input_id": "in-1", "trigger_id": "trg-1"}]),
        ),
        patch(f"{_MOD}.ticket_db.latest_report", AsyncMock(return_value=None)),
        patch(f"{_MOD}.ticket_db.record_handoff", AsyncMock(return_value={"handoff_id": "h-1"})),
        patch(f"{_MOD}.enqueue_effect", enqueue),
        patch(
            "src.server.services.autonomy.worker.refuse_unisolated_cloud_dispatch",
            AsyncMock(return_value={"ticket_id": "to-1", "status": "failed"}),
        ),
    ):
        await AutonomyToolHost(user_id="u-1", thread_id="router").reassign(
            thread_key="thread-1", from_ticket_id="from-1", to_ticket_id="to-1"
        )

    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_reassign_rolls_back_when_a_step_fails():
    """A mid-transition failure must not leave ownership half-transferred:
    the transaction rolls back, so a retried reassign still sees the source
    subscription active."""
    enqueue = AsyncMock(return_value=True)
    record = AsyncMock(side_effect=TypeError("Object of type datetime is not JSON serializable"))
    tx, tx_patch = _tx_connection()
    with (
        tx_patch,
        patch(f"{_MOD}.ticket_db.get_ticket", AsyncMock(side_effect=[_ticket("from-1"), _ticket("to-1", worker_thread_id="wt-1"), _ticket("to-1", worker_thread_id="wt-1")])),
        patch(f"{_MOD}.ticket_db.end_subscription", AsyncMock(return_value={"ticket_id": "from-1"})),
        patch(f"{_MOD}.ticket_db.add_subscription", AsyncMock()),
        patch(
            f"{_MOD}.ticket_db.move_undelivered_inputs",
            AsyncMock(return_value=[{"input_id": "in-1", "trigger_id": "trg-1"}]),
        ),
        patch(f"{_MOD}.ticket_db.latest_report", AsyncMock(return_value=None)),
        patch(f"{_MOD}.ticket_db.record_handoff", record),
        patch(f"{_MOD}.enqueue_effect", enqueue),
    ):
        with pytest.raises(TypeError, match="not JSON serializable"):
            await AutonomyToolHost(user_id="u-1", thread_id="router").reassign(
                thread_key="thread-1", from_ticket_id="from-1", to_ticket_id="to-1"
            )

    assert tx.transactions == 1
    assert tx.rolled_back == 1
    assert tx.committed == 0
    # The delivery effect is part of the transition: it must not survive a
    # rollback of everything that preceded it.
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_reassign_does_not_launch_a_cloud_target_without_a_worker_thread():
    enqueue = AsyncMock(return_value=True)
    refuse = AsyncMock(return_value=None)
    tx, tx_patch = _tx_connection()
    with (
        tx_patch,
        patch(f"{_MOD}.ticket_db.get_ticket", AsyncMock(side_effect=[_ticket("from-1"), _ticket("to-1", status="waiting"), _ticket("to-1", status="waiting")])),
        patch(f"{_MOD}.ticket_db.end_subscription", AsyncMock(return_value={"ticket_id": "from-1"})),
        patch(f"{_MOD}.ticket_db.add_subscription", AsyncMock()),
        patch(
            f"{_MOD}.ticket_db.move_undelivered_inputs",
            AsyncMock(return_value=[{"input_id": "in-1", "trigger_id": "trg-1"}]),
        ),
        patch(f"{_MOD}.ticket_db.latest_report", AsyncMock(return_value=None)),
        patch(f"{_MOD}.ticket_db.record_handoff", AsyncMock(return_value={"handoff_id": "h-1"})),
        patch(f"{_MOD}.enqueue_effect", enqueue),
        patch(
            "src.server.services.autonomy.worker.refuse_unisolated_cloud_dispatch",
            refuse,
        ),
        patch(
            "src.server.services.autonomy.inputs.mark_cloud_input_for_review",
            AsyncMock(),
        ),
    ):
        result = await AutonomyToolHost(user_id="u-1", thread_id="router").reassign(
            thread_key="thread-1", from_ticket_id="from-1", to_ticket_id="to-1"
        )

    enqueue.assert_not_awaited()
    refuse.assert_awaited_once()
    assert result["input_delivery"] == "stored_for_review"
    assert tx.committed == 1


@pytest.mark.asyncio
async def test_reassign_leaves_a_daemon_target_to_pull_its_inputs():
    enqueue = AsyncMock(return_value=True)
    tx, tx_patch = _tx_connection()
    with (
        tx_patch,
        patch(f"{_MOD}.ticket_db.get_ticket", AsyncMock(side_effect=[_ticket("from-1"), _ticket("to-1", worker_daemon_id="daemon-1"), _ticket("to-1", worker_daemon_id="daemon-1")])),
        patch(f"{_MOD}.ticket_db.end_subscription", AsyncMock(return_value={"ticket_id": "from-1"})),
        patch(f"{_MOD}.ticket_db.add_subscription", AsyncMock()),
        patch(
            f"{_MOD}.ticket_db.move_undelivered_inputs",
            AsyncMock(return_value=[{"input_id": "in-1", "trigger_id": "trg-1"}]),
        ),
        patch(f"{_MOD}.ticket_db.latest_report", AsyncMock(return_value=None)),
        patch(f"{_MOD}.ticket_db.record_handoff", AsyncMock(return_value={"handoff_id": "h-1"})),
        patch(f"{_MOD}.enqueue_effect", enqueue),
    ):
        await AutonomyToolHost(user_id="u-1", thread_id="router").reassign(
            thread_key="thread-1", from_ticket_id="from-1", to_ticket_id="to-1"
        )

    enqueue.assert_not_awaited()
    assert tx.committed == 1


@pytest.mark.asyncio
async def test_reassign_without_moved_inputs_enqueues_nothing():
    enqueue = AsyncMock(return_value=True)
    tx, tx_patch = _tx_connection()
    with (
        tx_patch,
        patch(f"{_MOD}.ticket_db.get_ticket", AsyncMock(side_effect=[_ticket("from-1"), _ticket("to-1"), _ticket("to-1")])),
        patch(f"{_MOD}.ticket_db.end_subscription", AsyncMock(return_value={"ticket_id": "from-1"})),
        patch(f"{_MOD}.ticket_db.add_subscription", AsyncMock()),
        patch(f"{_MOD}.ticket_db.move_undelivered_inputs", AsyncMock(return_value=[])),
        patch(f"{_MOD}.ticket_db.latest_report", AsyncMock(return_value=None)),
        patch(f"{_MOD}.ticket_db.record_handoff", AsyncMock(return_value={"handoff_id": "h-1"})),
        patch(f"{_MOD}.enqueue_effect", enqueue),
    ):
        result = await AutonomyToolHost(user_id="u-1", thread_id="router").reassign(
            thread_key="thread-1", from_ticket_id="from-1", to_ticket_id="to-1"
        )

    assert result["moved_inputs"] == 0
    enqueue.assert_not_awaited()
    assert tx.committed == 1


@pytest.mark.asyncio
async def test_reassign_refuses_a_cross_user_target():
    with (
        patch(f"{_MOD}.ticket_db.get_ticket", AsyncMock(side_effect=[_ticket("from-1"), _ticket("to-1", user_id="u-2")])),
    ):
        with pytest.raises(ValueError, match="target ticket not found"):
            await AutonomyToolHost(user_id="u-1", thread_id="router").reassign(
                thread_key="thread-1", from_ticket_id="from-1", to_ticket_id="to-1"
            )


@pytest.mark.asyncio
async def test_reassign_refuses_a_cross_user_source():
    with patch(
        f"{_MOD}.ticket_db.get_ticket",
        AsyncMock(return_value=_ticket("from-1", user_id="u-2")),
    ):
        with pytest.raises(ValueError, match="source ticket not found"):
            await AutonomyToolHost(user_id="u-1", thread_id="router").reassign(
                thread_key="thread-1", from_ticket_id="from-1", to_ticket_id="to-1"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "archived"])
async def test_reassign_refuses_a_terminal_target(status):
    with patch(
        f"{_MOD}.ticket_db.get_ticket",
        AsyncMock(side_effect=[_ticket("from-1"), _ticket("to-1", status=status)]),
    ):
        with pytest.raises(ValueError, match="already ended"):
            await AutonomyToolHost(user_id="u-1", thread_id="router").reassign(
                thread_key="thread-1", from_ticket_id="from-1", to_ticket_id="to-1"
            )


@pytest.mark.asyncio
async def test_reassign_refuses_a_source_without_an_active_subscription():
    tx, tx_patch = _tx_connection()
    with (
        tx_patch,
        patch(f"{_MOD}.ticket_db.get_ticket", AsyncMock(side_effect=[_ticket("from-1"), _ticket("to-1"), _ticket("to-1")])),
        patch(f"{_MOD}.ticket_db.end_subscription", AsyncMock(return_value=None)),
    ):
        with pytest.raises(ValueError, match="not subscribed"):
            await AutonomyToolHost(user_id="u-1", thread_id="router").reassign(
                thread_key="thread-1", from_ticket_id="from-1", to_ticket_id="to-1"
            )

    assert tx.rolled_back == 1
    assert tx.committed == 0


@pytest.mark.asyncio
async def test_reassign_refuses_to_move_a_thread_to_its_current_ticket():
    with pytest.raises(ValueError, match="current Ticket"):
        await AutonomyToolHost(user_id="u-1", thread_id="router").reassign(
            thread_key="thread-1", from_ticket_id="t-1", to_ticket_id="t-1"
        )


def test_unsubscribe_and_reassign_tools_are_registered():
    assert autonomy_tools.autonomy_unsubscribe in autonomy_tools.AUTONOMY_WORKER_TOOLS
    assert autonomy_tools.autonomy_reassign in autonomy_tools.AUTONOMY_ROUTER_TOOLS
