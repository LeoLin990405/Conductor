"""R6 L1: the role registry API reads, edits with a reason, and lists the audit."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from src.server.app import role_registry as api
from src.server.services.autonomy import role_registry as service_mod
from src.server.services.autonomy import role_types
from src.server.utils.api import get_current_user_id
from tests.conftest import create_test_app

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _service():
    service_mod.set_service(service_mod.RoleRegistryService(service_mod.InMemoryRegistryStore()))
    role_types.set_registry_provider(None)
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


async def test_get_put_and_audit() -> None:
    async with _client() as client:
        got = (await client.get("/api/v1/roles")).json()
        assert got["source"] == "fallback" and "router_routes_never_executes" in got["hard_rules"]
        put = await client.put(
            "/api/v1/roles/worker",
            json={"candidates": ["stepfun", "kimi"], "default": "stepfun", "reason": "web"},
            headers=OPERATOR,
        )
        assert put.status_code == 200, put.text
        assert put.json()["roles"]["worker"]["default"] == "stepfun"
        assert role_types.current_registry().source == "store"
        audit = (await client.get("/api/v1/roles/audit")).json()["audit"]
        assert audit[0]["actor"] == "operator:leo" and audit[0]["reason"] == "web"


async def test_refusals() -> None:
    async with _client() as client:
        bad = await client.put(
            "/api/v1/roles/router", json={"constraints": {"may_execute": True}, "reason": "x"},
            headers=OPERATOR,
        )
        assert bad.status_code == 422 and "router_routes_never_executes" in bad.text
        assert (await client.put("/api/v1/roles/boss", json={"reason": "x"}, headers=OPERATOR)).status_code == 404
        assert (await client.put("/api/v1/roles/worker", json={"reason": "x"}, headers=OPERATOR)).status_code == 422
        assert (
            await client.put("/api/v1/roles/worker", json={"default": "kimi"}, headers=OPERATOR)
        ).status_code == 422
        stale = await client.put(
            "/api/v1/roles/worker", json={"default": "kimi", "reason": "x", "expected_revision": 5},
            headers=OPERATOR,
        )
        assert stale.status_code == 409


async def test_a_signed_in_user_without_the_operator_credential_cannot_edit() -> None:
    """Review fix 2 (fails on 3025c108e): PUT was open to any user."""
    async with _client() as client:
        denied = await client.put(
            "/api/v1/roles/worker", json={"default": "kimi", "reason": "x"}
        )
        assert denied.status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {"extra": {"handoff": {"sessions": {}}}, "reason": "x"},
        {"extra": {"notes": "x"}, "reason": "x"},
        {"extra": {"avoid": {"kimi": "x" * 5000}}, "reason": "x"},
        {"candidates": ["kimi", "not-a-worker"], "reason": "x"},
        {"constraints": {"max_parallel": "lots"}, "reason": "x"},
    ],
)
async def test_bad_edits_are_422_not_500(body) -> None:
    async with _client() as client:
        response = await client.put("/api/v1/roles/worker", json=body, headers=OPERATOR)
    assert response.status_code == 422, response.text


async def test_reviewers_must_be_read_only_capable() -> None:
    async with _client() as client:
        response = await client.put(
            "/api/v1/roles/reviewer",
            json={"candidates": ["codex", "grok"], "reason": "x"},
            headers=OPERATOR,
        )
    assert response.status_code == 422 and "read_only_capable" in response.text
