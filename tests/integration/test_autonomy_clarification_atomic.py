"""Clarification confirmation commits its release, answer, and launch together."""

import asyncio
from uuid import uuid4
from unittest.mock import AsyncMock, patch

import pytest

from src.server.database.autonomy import clarifications, tickets

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def _held_ticket(seed_user, seed_workspace):
    unique = uuid4().hex
    ticket = await tickets.create_ticket(
        user_id=seed_user["user_id"],
        workspace_id=str(seed_workspace["workspace_id"]),
        title="Help",
        scope="Help",
        mandate={},
        policy_version="",
        origin_trigger_id=f"clarify-atomic:{unique}",
        origin_thread_key=f"clarify-atomic:{unique}",
        initial_status="awaiting_clarification",
    )
    await clarifications.create_clarification(
        ticket_id=ticket["ticket_id"],
        user_id=seed_user["user_id"],
        original_prompt="Help",
        clarity_score=0.1,
        expanded_request="Help with the requested change.",
        assumptions=[],
        open_questions=["Which change?"],
    )
    return ticket


async def _delete_ticket(test_db_pool, ticket_id):
    async with test_db_pool.connection() as conn:
        await conn.execute("DELETE FROM autonomy_tickets WHERE ticket_id=%s", (ticket_id,))


async def test_concurrent_confirmation_writes_one_answer_and_launch(
    seed_user, seed_workspace, patched_get_db_connection, test_db_pool
):
    ticket = await _held_ticket(seed_user, seed_workspace)
    try:
        results = await asyncio.gather(
            *(
                clarifications.confirm_ticket_clarification(
                    ticket_id=ticket["ticket_id"],
                    text="Implement the requested change in module x.",
                    enqueue_launch=True,
                )
                for _ in range(2)
            )
        )
        assert sum(result is not None for result in results) == 1
        async with test_db_pool.connection() as conn:
            answer_count = await conn.execute(
                "SELECT count(*) FROM autonomy_ticket_inputs "
                "WHERE ticket_id=%s AND kind='answer'",
                (ticket["ticket_id"],),
            )
            effect_count = await conn.execute(
                "SELECT count(*) FROM autonomy_effects WHERE idempotency_key LIKE %s",
                (f"autonomy:launch:{ticket['ticket_id']}:clarified:%",),
            )
            assert (await answer_count.fetchone())["count"] == 1
            assert (await effect_count.fetchone())["count"] == 1
    finally:
        await _delete_ticket(test_db_pool, ticket["ticket_id"])


async def test_failed_launch_insert_rolls_back_the_confirmation(
    seed_user, seed_workspace, patched_get_db_connection, test_db_pool
):
    ticket = await _held_ticket(seed_user, seed_workspace)
    try:
        with patch(
            "src.server.database.autonomy.clarifications.effect_db.insert_effect",
            new=AsyncMock(side_effect=RuntimeError("injected effect insert failure")),
        ):
            with pytest.raises(RuntimeError, match="injected effect insert failure"):
                await clarifications.confirm_ticket_clarification(
                    ticket_id=ticket["ticket_id"],
                    text="Implement the requested change in module x.",
                    enqueue_launch=True,
                )
        updated = await tickets.get_ticket(ticket["ticket_id"])
        assert updated["status"] == "awaiting_clarification"
        assert await tickets.list_inputs(ticket["ticket_id"]) == []
        active = await clarifications.get_active_clarification(ticket["ticket_id"])
        assert active is not None
    finally:
        await _delete_ticket(test_db_pool, ticket["ticket_id"])
