"""Authenticated HTTP contract for exact memory sources and explicit revert."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app

pytestmark = pytest.mark.asyncio

ENTRY_ID = "11111111-1111-4111-8111-111111111111"


@pytest_asyncio.fixture
async def client():
    from src.server.app.memory import router

    async with AsyncClient(
        transport=ASGITransport(app=create_test_app(router)), base_url="http://test"
    ) as http:
        yield http


async def test_source_route_returns_exact_source_or_unavailable(client, monkeypatch):
    from src.server.services.refactor import memory_sources

    lookup = AsyncMock(
        return_value={
            "status": "available",
            "entry_id": ENTRY_ID,
            "source": {"message_id": "m1", "quote": "exact text"},
        }
    )
    monkeypatch.setattr(memory_sources, "lookup_memory_source", lookup)
    response = await client.get(f"/api/v1/memory/entries/{ENTRY_ID}/source")

    assert response.status_code == 200
    assert response.json()["source"]["quote"] == "exact text"
    lookup.assert_awaited_once_with(user_id="test-user-123", entry_id=ENTRY_ID)

    lookup.return_value = {
        "status": "unavailable",
        "entry_id": ENTRY_ID,
        "reason": "source_response_unavailable",
    }
    response = await client.get(f"/api/v1/memory/entries/{ENTRY_ID}/source")
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"


async def test_source_route_hides_unowned_or_missing_entry(client, monkeypatch):
    from src.server.services.refactor import memory_sources

    lookup = AsyncMock(return_value={"status": "not_found", "entry_id": ENTRY_ID})
    monkeypatch.setattr(memory_sources, "lookup_memory_source", lookup)
    response = await client.get(f"/api/v1/memory/entries/{ENTRY_ID}/source")

    assert response.status_code == 404
    assert response.json() == {"detail": "Memory entry not found"}


async def test_revert_route_passes_authenticated_owner_and_reason(client, monkeypatch):
    from src.server.app import memory
    from src.server.services.refactor import memory_sources

    store = object()
    revert = AsyncMock(
        return_value={"status": "complete", "new_entry_key": "restored--revision.md"}
    )
    monkeypatch.setattr(memory, "_long_term_store", lambda: store)
    monkeypatch.setattr(memory_sources, "revert_memory_revision", revert)
    response = await client.post(
        f"/api/v1/memory/entries/{ENTRY_ID}/revert", json={"reason": "Restore wording"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "complete",
        "new_entry_key": "restored--revision.md",
    }
    revert.assert_awaited_once_with(
        user_id="test-user-123",
        entry_id=ENTRY_ID,
        store=store,
        reason="Restore wording",
    )


async def test_revert_route_maps_missing_and_stale_revision(client, monkeypatch):
    from src.server.app import memory
    from src.server.services.refactor import memory_sources

    monkeypatch.setattr(memory, "_long_term_store", lambda: object())
    revert = AsyncMock(
        side_effect=memory_sources.MemoryRevisionError("entry_not_found")
    )
    monkeypatch.setattr(memory_sources, "revert_memory_revision", revert)
    response = await client.post(
        f"/api/v1/memory/entries/{ENTRY_ID}/revert", json={"reason": "undo"}
    )
    assert response.status_code == 404

    revert.side_effect = memory_sources.MemoryRevisionError("active_index_changed")
    response = await client.post(
        f"/api/v1/memory/entries/{ENTRY_ID}/revert", json={"reason": "undo"}
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "active_index_changed"}
