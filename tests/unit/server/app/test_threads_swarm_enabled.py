"""The per-thread Swarm switch: PATCH stores it and reads report it."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.server.utils.api import get_current_user_id
from tests.conftest import create_test_app
from tests.unit.server.app.test_threads_subagents_allowed import _pool_returning
from src.server.services.turn_runtime import TurnRoute

THREADS_MOD = "src.server.app.threads.crud"
AUTH_MOD = "src.server.utils.api"
USER = "test-user-123"


def _app(user=USER):
    from src.server.app.threads import router

    app = create_test_app(router)
    app.dependency_overrides[get_current_user_id] = lambda: user
    return app


def _row(**overrides):
    now = datetime.now(timezone.utc)
    row = {
        "conversation_thread_id": "t-1",
        "workspace_id": "ws-1",
        "current_status": "completed",
        "msg_type": "ptc",
        "thread_index": 0,
        "title": "Test Thread",
        "subagents_allowed": True,
        "swarm_enabled": False,
        "reasoning_effort": None,
        "fast_mode": None,
        "created_at": now,
        "updated_at": now,
    }
    row.update(overrides)
    return row


async def _request(method, path, body=None):
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as c:
        return await c.request(method, path, json=body)




@pytest.mark.asyncio
async def test_get_thread_reports_swarm_enabled():
    with (
        patch(f"{AUTH_MOD}.require_thread_owner", new=AsyncMock()),
        patch(
            f"{THREADS_MOD}.get_thread_by_id",
            new=AsyncMock(return_value=_row(swarm_enabled=True)),
        ),
    ):
        resp = await _request("GET", "/api/v1/threads/t-1")

    assert resp.status_code == 200
    assert resp.json()["swarm_enabled"] is True


@pytest.mark.asyncio
async def test_create_hands_swarm_to_the_insert():
    create_mod = "src.server.app.threads.create"
    with (
        patch(f"{create_mod}.HOST_MODE", "oss"),
        patch(
            f"{create_mod}.requested_workspace",
            new=AsyncMock(return_value={"workspace_id": "ws-1", "status": "active"}),
        ),
        patch(f"{create_mod}.turn_route", return_value=TurnRoute("ws-1", "generalist")),
        patch(
            f"{create_mod}.create_thread",
            new=AsyncMock(return_value=_row(swarm_enabled=True)),
        ) as create,
        patch(f"{create_mod}.schedule_title_generation", return_value=None),
    ):
        resp = await _request(
            "POST", "/api/v1/threads",
            {"workspace_id": "ws-1", "first_query": "hello", "swarm_enabled": True},
        )

    assert resp.status_code == 201
    assert create.await_args.kwargs["swarm_enabled"] is True
    assert resp.json()["swarm_enabled"] is True


@pytest.mark.asyncio
async def test_read_thread_swarm_enabled_reports_the_mode():
    from src.server.database.conversation import threads_read

    with patch.object(
        threads_read.pool, "get_db_connection",
        new=_pool_returning(row={"swarm_enabled": True}),
    ):
        assert await threads_read.read_thread_swarm_enabled(
            "123e4567-e89b-12d3-a456-426614174000"
        ) is True


@pytest.mark.asyncio
async def test_read_thread_swarm_enabled_degrades_on_error():
    from src.server.database.conversation import threads_read

    with patch.object(
        threads_read.pool, "get_db_connection",
        new=_pool_returning(error=RuntimeError("boom")),
    ):
        assert await threads_read.read_thread_swarm_enabled(
            "123e4567-e89b-12d3-a456-426614174000"
        ) is False


