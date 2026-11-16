from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app


@pytest_asyncio.fixture
async def metrics_client():
    from src.server.app.router_metrics import router

    async with AsyncClient(
        transport=ASGITransport(app=create_test_app(router)),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_router_metrics_endpoint_uses_authenticated_user_and_bounded_limit(
    metrics_client, monkeypatch
):
    from src.server.app import router_metrics as app_router

    seen = []

    async def feature_enabled(user_id: str, key: str) -> bool:
        assert user_id == "test-user-123"
        assert key == "super_agent"
        return True

    async def fake_metrics(user_id: str, *, limit: int):
        seen.append((user_id, limit))
        return {"enabled": True, "available": True, "items": []}

    monkeypatch.setattr(app_router.features, "user_feature_enabled", feature_enabled)
    monkeypatch.setattr(app_router, "router_metrics_for_user", fake_metrics)
    response = await metrics_client.get(
        "/api/v1/metrics/router?user_id=attacker&limit=12"
    )
    assert response.status_code == 200
    assert seen == [("test-user-123", 12)]

    default = await metrics_client.get("/api/v1/metrics/router")
    assert default.status_code == 200
    assert seen[-1] == ("test-user-123", 50)

    assert (await metrics_client.get("/api/v1/metrics/router?limit=0")).status_code == 422
    assert (await metrics_client.get("/api/v1/metrics/router?limit=101")).status_code == 422


@pytest.mark.asyncio
async def test_router_metrics_endpoint_is_hidden_when_feature_is_disabled(
    metrics_client, monkeypatch
):
    from src.server.app import router_metrics as app_router

    checks = []

    async def feature_disabled(user_id: str, key: str) -> bool:
        checks.append((user_id, key))
        return False

    async def unexpected_metrics_call(user_id: str, *, limit: int):
        pytest.fail("metrics service must not run when super_agent is disabled")

    monkeypatch.setattr(app_router.features, "user_feature_enabled", feature_disabled)
    monkeypatch.setattr(app_router, "router_metrics_for_user", unexpected_metrics_call)

    response = await metrics_client.get("/api/v1/metrics/router")

    assert response.status_code == 404
    assert response.json()["detail"] == "Feature not enabled"
    assert checks == [("test-user-123", "super_agent")]
