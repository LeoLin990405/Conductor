"""Worker host: a scoped read of its own Ticket and its subscribed Threads."""

from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy.host import AutonomyWorkerHost
from src.tools.autonomy import tools as autonomy_tools

_MOD = "src.server.services.autonomy.host"


def _subs(*thread_keys: str) -> list[dict]:
    return [
        {"ticket_id": "ticket-1", "thread_key": key, "kind": "own"}
        for key in thread_keys
    ]


@pytest.mark.asyncio
async def test_worker_reads_own_ticket_with_inputs_reports_and_handoffs():
    inputs = AsyncMock(return_value=[{"input_id": "in-1"}])
    reports = AsyncMock(return_value=[{"report_id": "rep-1"}])
    handoffs = AsyncMock(return_value=[{"handoff_id": "h-1", "thread_key": "th-1"}])
    with (
        patch(
            f"{_MOD}.ticket_db.list_subscriptions",
            AsyncMock(return_value=_subs()),
        ),
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(return_value={"ticket_id": "ticket-1", "status": "running"}),
        ),
        patch(f"{_MOD}.ticket_db.list_inputs", inputs),
        patch(f"{_MOD}.ticket_db.list_reports", reports),
        patch(f"{_MOD}.ticket_db.list_handoffs_for_ticket", handoffs),
    ):
        result = await AutonomyWorkerHost(ticket_id="ticket-1", thread_id="thread-1").read(
            resource="ticket", resource_id="ticket-1", offset=2, limit=5
        )

    assert result["ticket_id"] == "ticket-1"
    assert result["inputs"] == [{"input_id": "in-1"}]
    assert result["reports"] == [{"report_id": "rep-1"}]
    assert result["handoffs"] == [{"handoff_id": "h-1", "thread_key": "th-1"}]
    assert result["offset"] == 2
    assert result["limit"] == 5
    inputs.assert_awaited_once_with("ticket-1", limit=5, offset=2)
    reports.assert_awaited_once_with("ticket-1", limit=5, offset=2)
    handoffs.assert_awaited_once_with("ticket-1", limit=5)


@pytest.mark.asyncio
async def test_worker_cannot_read_another_ticket():
    with patch(
        f"{_MOD}.ticket_db.list_subscriptions",
        AsyncMock(return_value=_subs()),
    ):
        with pytest.raises(ValueError, match="own Ticket"):
            await AutonomyWorkerHost(ticket_id="ticket-1", thread_id="thread-1").read(
                resource="ticket", resource_id="ticket-2"
            )


@pytest.mark.asyncio
async def test_worker_read_of_a_missing_ticket():
    with (
        patch(
            f"{_MOD}.ticket_db.list_subscriptions",
            AsyncMock(return_value=_subs()),
        ),
        patch(f"{_MOD}.ticket_db.get_ticket", AsyncMock(return_value=None)),
    ):
        with pytest.raises(ValueError, match="ticket not found"):
            await AutonomyWorkerHost(ticket_id="ticket-1", thread_id="thread-1").read(
                resource="ticket", resource_id="ticket-1"
            )


@pytest.mark.asyncio
async def test_worker_reads_a_context_subscribed_thread_with_shared_reports():
    thread_reports = AsyncMock(return_value=[{"report_id": "rep-9", "ticket_id": "ticket-1"}])
    with (
        patch(
            f"{_MOD}.ticket_db.list_subscriptions",
            AsyncMock(
                return_value=[
                    {
                        "ticket_id": "ticket-1",
                        "thread_key": "thread-1",
                        "kind": "own",
                    },
                    {
                        "ticket_id": "ticket-1",
                        "thread_key": "thread-2",
                        "kind": "context",
                    },
                ]
            ),
        ),
        patch(
            f"{_MOD}.trigger_db.list_triggers_for_thread",
            AsyncMock(return_value=[{"trigger_id": "trg-1"}]),
        ),
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(
                return_value={"ticket_id": "ticket-1", "status": "running"}
            ),
        ),
        patch(
            f"{_MOD}.context_scope.ensure_scope",
            AsyncMock(
                return_value={
                    "scope_id": "scope-1",
                    "kind": "shared",
                    "name": "team",
                }
            ),
        ),
        patch(
            f"{_MOD}.context_scope.assert_can_read_thread",
            AsyncMock(return_value={}),
        ),
        patch(
            f"{_MOD}.context_scope.readable_ticket_ids",
            AsyncMock(return_value={"ticket-1"}),
        ),
        patch(
            f"{_MOD}.ticket_db.list_tickets_for_thread",
            AsyncMock(return_value=[{"ticket_id": "ticket-1"}]),
        ),
        patch(f"{_MOD}.ticket_db.list_reports_for_thread", thread_reports),
    ):
        result = await AutonomyWorkerHost(ticket_id="ticket-1", thread_id="thread-1").read(
            resource="thread", resource_id="thread-2"
        )

    assert result["thread_key"] == "thread-2"
    assert result["triggers"] == [{"trigger_id": "trg-1"}]
    assert result["tickets"] == [{"ticket_id": "ticket-1"}]
    assert result["reports"] == [{"report_id": "rep-9", "ticket_id": "ticket-1"}]
    thread_reports.assert_awaited_once_with("thread-2", limit=20, offset=0)


@pytest.mark.asyncio
async def test_worker_read_hides_report_summary_when_reports_are_not_shared():
    with (
        patch(
            f"{_MOD}.ticket_db.list_subscriptions",
            AsyncMock(return_value=_subs("thread-1")),
        ),
        patch(
            f"{_MOD}.trigger_db.list_triggers_for_thread",
            AsyncMock(return_value=[]),
        ),
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(return_value={"ticket_id": "ticket-1", "status": "running"}),
        ),
        patch(
            f"{_MOD}.context_scope.ensure_scope",
            AsyncMock(
                return_value={
                    "scope_id": "scope-1",
                    "kind": "shared",
                    "name": "team",
                    "policy": {"data_classes": ["tickets"]},
                }
            ),
        ),
        patch(
            f"{_MOD}.context_scope.assert_can_read_thread",
            AsyncMock(return_value={}),
        ),
        patch(
            f"{_MOD}.context_scope.readable_ticket_ids",
            AsyncMock(return_value={"ticket-1", "ticket-2"}),
        ),
        patch(
            f"{_MOD}.ticket_db.list_tickets_for_thread",
            AsyncMock(
                return_value=[
                    {
                        "ticket_id": "ticket-2",
                        "title": "theirs",
                        "summary": "internal finding",
                    }
                ]
            ),
        ),
        patch(
            f"{_MOD}.ticket_db.list_reports_for_thread",
            AsyncMock(return_value=[]),
        ),
    ):
        result = await AutonomyWorkerHost(ticket_id="ticket-1", thread_id="thread-1").read(
            resource="thread", resource_id="thread-1"
        )

    assert result["tickets"] == [
        {"ticket_id": "ticket-2", "title": "theirs", "summary": ""}
    ]


@pytest.mark.asyncio
async def test_worker_read_hides_a_blocked_reports_attention_question():
    """A blocked report puts its own text in the peer's attention slot, so a
    Worker that may not read Reports must not read it there either."""
    with (
        patch(
            f"{_MOD}.ticket_db.list_subscriptions",
            AsyncMock(return_value=_subs("thread-1")),
        ),
        patch(
            f"{_MOD}.trigger_db.list_triggers_for_thread",
            AsyncMock(return_value=[]),
        ),
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(return_value={"ticket_id": "ticket-1", "status": "running"}),
        ),
        patch(
            f"{_MOD}.context_scope.ensure_scope",
            AsyncMock(
                return_value={
                    "scope_id": "scope-1",
                    "kind": "shared",
                    "name": "team",
                    "policy": {"data_classes": ["tickets"]},
                }
            ),
        ),
        patch(
            f"{_MOD}.context_scope.assert_can_read_thread",
            AsyncMock(return_value={}),
        ),
        patch(
            f"{_MOD}.context_scope.readable_ticket_ids",
            AsyncMock(return_value={"ticket-1", "ticket-2"}),
        ),
        patch(
            f"{_MOD}.ticket_db.list_tickets_for_thread",
            AsyncMock(
                return_value=[
                    {
                        "ticket_id": "ticket-2",
                        "title": "theirs",
                        "summary": "internal finding",
                        "attention": {"question": "internal finding"},
                    }
                ]
            ),
        ),
        patch(
            f"{_MOD}.ticket_db.list_reports_for_thread",
            AsyncMock(return_value=[]),
        ),
    ):
        result = await AutonomyWorkerHost(ticket_id="ticket-1", thread_id="thread-1").read(
            resource="thread", resource_id="thread-1"
        )

    assert result["tickets"] == [
        {
            "ticket_id": "ticket-2",
            "title": "theirs",
            "summary": "",
            "attention": {"question": ""},
        }
    ]
    assert "internal finding" not in str(result)


@pytest.mark.asyncio
async def test_worker_cannot_read_an_unsubscribed_thread():
    triggers = AsyncMock()
    with (
        patch(
            f"{_MOD}.ticket_db.list_subscriptions",
            AsyncMock(return_value=_subs("thread-1")),
        ),
        patch(f"{_MOD}.trigger_db.list_triggers_for_thread", triggers),
    ):
        with pytest.raises(ValueError, match="not subscribed"):
            await AutonomyWorkerHost(ticket_id="ticket-1", thread_id="thread-1").read(
                resource="thread", resource_id="thread-9"
            )

    triggers.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_reads_a_trigger_on_a_subscribed_thread():
    with (
        patch(
            f"{_MOD}.ticket_db.list_subscriptions",
            AsyncMock(return_value=_subs("thread-1")),
        ),
        patch(
            f"{_MOD}.trigger_db.get_trigger",
            AsyncMock(
                return_value={"trigger_id": "trg-1", "thread_key": "thread-1"}
            ),
        ),
    ):
        result = await AutonomyWorkerHost(ticket_id="ticket-1", thread_id="thread-1").read(
            resource="trigger", resource_id="trg-1"
        )

    assert result["trigger_id"] == "trg-1"


@pytest.mark.asyncio
async def test_worker_cannot_read_a_trigger_on_an_unsubscribed_thread():
    with (
        patch(
            f"{_MOD}.ticket_db.list_subscriptions",
            AsyncMock(return_value=_subs("thread-1")),
        ),
        patch(
            f"{_MOD}.trigger_db.get_trigger",
            AsyncMock(
                return_value={"trigger_id": "trg-9", "thread_key": "thread-9"}
            ),
        ),
    ):
        with pytest.raises(ValueError, match="not subscribed"):
            await AutonomyWorkerHost(ticket_id="ticket-1", thread_id="thread-1").read(
                resource="trigger", resource_id="trg-9"
            )


@pytest.mark.asyncio
async def test_worker_read_of_a_missing_trigger():
    with (
        patch(
            f"{_MOD}.ticket_db.list_subscriptions",
            AsyncMock(return_value=_subs("thread-1")),
        ),
        patch(f"{_MOD}.trigger_db.get_trigger", AsyncMock(return_value=None)),
    ):
        with pytest.raises(ValueError, match="trigger not found"):
            await AutonomyWorkerHost(ticket_id="ticket-1", thread_id="thread-1").read(
                resource="trigger", resource_id="trg-9"
            )


@pytest.mark.asyncio
async def test_worker_read_rejects_an_unknown_resource():
    with patch(
        f"{_MOD}.ticket_db.list_subscriptions",
        AsyncMock(return_value=_subs("thread-1")),
    ):
        with pytest.raises(ValueError, match="resource must be"):
            await AutonomyWorkerHost(ticket_id="ticket-1", thread_id="thread-1").read(
                resource="eval", resource_id="eval-1"
            )


@pytest.mark.asyncio
async def test_worker_read_clamps_offset_and_limit():
    inputs = AsyncMock(return_value=[])
    with (
        patch(
            f"{_MOD}.ticket_db.list_subscriptions",
            AsyncMock(return_value=_subs()),
        ),
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(return_value={"ticket_id": "ticket-1"}),
        ),
        patch(f"{_MOD}.ticket_db.list_inputs", inputs),
        patch(f"{_MOD}.ticket_db.list_reports", AsyncMock(return_value=[])),
        patch(f"{_MOD}.ticket_db.list_handoffs_for_ticket", AsyncMock(return_value=[])),
    ):
        result = await AutonomyWorkerHost(ticket_id="ticket-1", thread_id="thread-1").read(
            resource="ticket", resource_id="ticket-1", offset=-3, limit=100
        )

    assert result["offset"] == 0
    assert result["limit"] == 50
    inputs.assert_awaited_once_with("ticket-1", limit=50, offset=0)


def test_autonomy_read_is_exposed_to_workers():
    assert autonomy_tools.autonomy_read in autonomy_tools.AUTONOMY_WORKER_TOOLS
