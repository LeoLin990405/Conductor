"""GET /api/v1/native-agents/routing/decisions is read-only telemetry."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.server.app import native_agents
from src.server.utils.api import get_current_user_id
from tests.conftest import create_test_app

USER = "routing-user"


def _app():
    app = create_test_app(native_agents.router)
    app.dependency_overrides[get_current_user_id] = lambda: USER
    return app


@pytest.mark.asyncio
async def test_recent_decisions_are_returned_with_their_reason():
    row = {
        "thread_id": "t-1",
        "title": "refactor",
        "decision": {
            "runtime": "codex",
            "reason": "coding affinity",
            "fallbacks": ["ptc"],
        },
        "native_provider": "codex",
        "created_at": datetime.now(timezone.utc),
    }
    with patch.object(
        native_agents, "list_runtime_decisions", new=AsyncMock(return_value=[row])
    ) as listed:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/native-agents/routing/decisions?limit=5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["decisions"][0]["decision"]["runtime"] == "codex"
    assert body["decisions"][0]["native_provider"] == "codex"
    listed.assert_awaited_once_with(USER, 5)
