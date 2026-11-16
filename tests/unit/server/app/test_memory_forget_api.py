"""Authenticated HTTP contract for the memory forget endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app

pytestmark = pytest.mark.asyncio

ENTRY_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def forget_service(monkeypatch):
    from src.server.app import memory
    from src.server.services.refactor import memory_forget
    from src.server.services.refactor.memory_forget import ForgetReason

    forget = AsyncMock(
        return_value={
            "entry_id": ENTRY_ID,
            "status": "completed",
            "carriers": {"store": "deleted", "projection": "deleted"},
        }
    )
    store = object()
    monkeypatch.setattr(memory, "_long_term_store", lambda: store)
    monkeypatch.setattr(memory_forget, "forget_memory_entry", forget)
    forget.test_store = store
    forget.test_reason_type = ForgetReason
    return forget


@pytest_asyncio.fixture
async def client():
    from src.server.app.memory import router

    async with AsyncClient(
        transport=ASGITransport(app=create_test_app(router)), base_url="http://test"
    ) as http:
        yield http


async def test_forget_calls_owner_scoped_service_and_returns_status(client, forget_service):
    response = await client.delete(f"/api/v1/memory/entries/{ENTRY_ID}")

    assert response.status_code == 200
    assert response.json() == {
        "entry_id": ENTRY_ID,
        "status": "completed",
        "carriers": {"store": "deleted", "projection": "deleted"},
    }
    forget_service.assert_awaited_once_with(
        user_id="test-user-123",
        entry_id=ENTRY_ID,
        store=forget_service.test_store,
        reason=forget_service.test_reason_type.USER_REQUEST,
    )


async def test_forget_accepts_known_reason_and_preserves_partial_status(
    client, forget_service
):
    forget_service.return_value = {
        "entry_id": ENTRY_ID,
        "status": "partial",
        "carriers": {"store": "deleted", "projection": "pending"},
    }

    response = await client.request(
        "DELETE",
        f"/api/v1/memory/entries/{ENTRY_ID}",
        json={"reason": "correction"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "partial"
    assert response.json()["carriers"]["projection"] == "pending"
    assert (
        forget_service.await_args.kwargs["reason"]
        is forget_service.test_reason_type.CORRECTION
    )


async def test_forget_returns_404_when_entry_does_not_exist(client, forget_service):
    forget_service.return_value = {
        "entry_id": ENTRY_ID,
        "status": "not_found",
        "carriers": {},
    }

    response = await client.delete(f"/api/v1/memory/entries/{ENTRY_ID}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Memory entry not found"}


async def test_forget_rejects_unknown_reason_before_service_call(client, forget_service):
    response = await client.request(
        "DELETE",
        f"/api/v1/memory/entries/{ENTRY_ID}",
        json={"reason": "typo"},
    )

    assert response.status_code == 422
    forget_service.assert_not_awaited()


async def test_forget_rejects_malformed_entry_id(client, forget_service):
    response = await client.delete("/api/v1/memory/entries/not-a-uuid")

    assert response.status_code == 422
    forget_service.assert_not_awaited()


async def test_list_proposals_is_owner_scoped_by_authenticated_route(client, monkeypatch):
    from src.server.services.refactor import proposals

    list_pending = AsyncMock(return_value=[{"proposal_id": ENTRY_ID, "status": "proposed"}])
    monkeypatch.setattr(proposals, "list_pending_proposals", list_pending)
    response = await client.get("/api/v1/memory/proposals?limit=5")

    assert response.status_code == 200
    assert response.json() == {"proposals": [{"proposal_id": ENTRY_ID, "status": "proposed"}]}
    list_pending.assert_awaited_once_with(user_id="test-user-123", limit=5)


async def test_manual_decision_uses_authenticated_owner_and_store(client, monkeypatch):
    from src.server.app import memory
    from src.server.services.refactor import proposals

    store = object()
    decide = AsyncMock(return_value={"proposal_id": ENTRY_ID, "status": "approved"})
    monkeypatch.setattr(memory, "_long_term_store", lambda: store)
    monkeypatch.setattr(proposals, "decide_proposal", decide)
    response = await client.post(
        f"/api/v1/memory/proposals/{ENTRY_ID}/decision", json={"approve": True}
    )

    assert response.status_code == 200
    assert response.json() == {"proposal_id": ENTRY_ID, "status": "approved"}
    decide.assert_awaited_once_with(
        proposal_id=ENTRY_ID,
        user_id="test-user-123",
        approve=True,
        approval_source="manual",
        store=store,
    )


async def test_manual_decision_returns_conflict_when_proposal_is_stale(client, monkeypatch):
    from src.server.contracts.refactor import ContractError
    from src.server.services.refactor import proposals

    decide = AsyncMock(side_effect=ContractError("proposal is already 'rejected'"))
    monkeypatch.setattr(proposals, "decide_proposal", decide)
    response = await client.post(
        f"/api/v1/memory/proposals/{ENTRY_ID}/decision", json={"approve": False}
    )

    assert response.status_code == 409
    decide.assert_awaited_once()
