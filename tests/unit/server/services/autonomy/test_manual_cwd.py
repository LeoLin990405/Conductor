from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy.manual import create_manual_task


@pytest.mark.asyncio
@pytest.mark.parametrize("cwd", [None, "/worktrees/repo"])
async def test_manual_task_persists_only_explicit_cwd_in_ticket_mandate(cwd):
    ticket = {"ticket_id": "ticket-1"}
    with (
        patch(
            "src.server.services.autonomy.manual.source_db.list_sources",
            new=AsyncMock(return_value=[{"source_id": "source-1", "kind": "manual"}]),
        ),
        patch(
            "src.server.services.autonomy.manual.trigger_db.insert_trigger",
            new=AsyncMock(return_value={"trigger_id": "trigger-1"}),
        ),
        patch(
            "src.server.services.autonomy.manual.trigger_db.ensure_thread",
            new=AsyncMock(),
        ),
        patch(
            "src.server.services.autonomy.manual.policy.current_policy",
            new=AsyncMock(return_value={"version": "v1"}),
        ),
        patch(
            "src.server.services.autonomy.manual.clarify_enabled",
            return_value=True,
        ),
        patch(
            "src.server.services.autonomy.manual.gate",
            new=AsyncMock(return_value="parked"),
        ),
        patch(
            "src.server.services.autonomy.manual.ticket_db.create_ticket",
            new=AsyncMock(return_value=ticket),
        ) as create_ticket,
        patch(
            "src.server.services.autonomy.manual.ticket_db.add_subscription",
            new=AsyncMock(),
        ),
        patch(
            "src.server.services.autonomy.manual.ticket_db.get_ticket",
            new=AsyncMock(return_value=ticket),
        ),
    ):
        await create_manual_task(
            user_id="user-1",
            workspace_id="workspace-1",
            prompt="Do work",
            cwd=cwd,
        )

    mandate = create_ticket.await_args.kwargs["mandate"]
    assert mandate.get("cwd") == cwd
    assert ("cwd" in mandate) is (cwd is not None)
