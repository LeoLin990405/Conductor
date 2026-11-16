"""Policy changes reach open Tickets; only a running Worker gets delivery."""

from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy import policy


@pytest.mark.asyncio
async def test_policy_change_stores_a_notice_and_delivers_it_to_a_running_worker():
    tickets = [
        {
            "ticket_id": "running",
            "status": "running",
            "worker_thread_id": "thread-1",
        },
        {"ticket_id": "waiting", "status": "waiting", "worker_thread_id": None},
    ]
    add_input = AsyncMock(side_effect=[{"input_id": "in-1"}, {"input_id": "in-2"}])
    enqueue = AsyncMock(return_value=True)
    with (
        patch(
            "src.server.database.autonomy.tickets.list_open_for_user",
            new=AsyncMock(return_value=tickets),
        ),
        patch("src.server.database.autonomy.tickets.add_input", add_input),
        patch(
            "src.server.database.autonomy.effects.enqueue_effect",
            enqueue,
        ),
    ):
        count = await policy.notify_policy_change(
            "u1",
            before={"version": "v1"},
            after={"version": "v2", "accept_mode": "observe"},
        )

    assert count == 2
    assert add_input.await_count == 2
    enqueue.assert_awaited_once()
    assert enqueue.await_args.kwargs["kind"] == "deliver_input"
    assert enqueue.await_args.kwargs["payload"] == {
        "ticket_id": "running",
        "input_id": "in-1",
    }
