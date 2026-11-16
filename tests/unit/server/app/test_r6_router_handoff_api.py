"""R6 L3: the Router handoff API."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.app import router_handoff as api
from src.server.services.autonomy import role_registry as service_mod
from src.server.services.autonomy import role_types, router_handoff
from src.server.utils.api import get_current_user_id
from tests.conftest import create_test_app

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _service():
    service = service_mod.RoleRegistryService(service_mod.InMemoryRegistryStore())
    service_mod.set_service(service)
    await service.update_role(
        "router", {"candidates": ["a-router", "b-router"], "default": "a-router"},
        actor="u", reason="setup",
    )
    service.install()
    router_handoff._reset_for_tests()
    yield
    service_mod.set_service(None)
    role_types.set_registry_provider(None)


TOKEN = "test-service-token"
OPERATOR = {"X-Service-Token": TOKEN, "X-User-Id": "leo"}


@pytest.fixture(autouse=True)
def _service_token(monkeypatch):
    from src.server.utils import api as api_utils

    monkeypatch.setattr(api_utils, "_SERVICE_TOKEN", TOKEN)


def _client() -> AsyncClient:
    app = create_test_app(api.router)
    app.dependency_overrides[get_current_user_id] = lambda: "leo"
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_get_and_handoff() -> None:
    async with _client() as client:
        got = (await client.get("/api/v1/router")).json()
        assert got["active_model"] == "a-router" and got["candidates"] == ["a-router", "b-router"]
        denied = await client.post(
            "/api/v1/router/handoff", json={"to_model": "b-router", "reason": "quota"}
        )
        assert denied.status_code == 403
        posted = await client.post(
            "/api/v1/router/handoff", json={"to_model": "b-router", "reason": "quota"},
            headers=OPERATOR,
        )
        assert posted.status_code == 200, posted.text
        assert posted.json()["active_model"] == "b-router"
        assert "sessions" not in posted.json()["handoff"]
        again = (await client.get("/api/v1/router")).json()
        assert again["handoffs"][-1]["to_model"] == "b-router"
        bad = await client.post(
            "/api/v1/router/handoff", json={"to_model": "grok", "reason": "x"}, headers=OPERATOR
        )
        assert bad.status_code == 422
