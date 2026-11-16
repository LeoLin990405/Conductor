"""Authenticated HTTP contract for owner memory export pages."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client():
    from src.server.app.memory import router

    async with AsyncClient(
        transport=ASGITransport(app=create_test_app(router)), base_url="http://test"
    ) as http:
        yield http


async def test_export_route_uses_authenticated_owner_and_bounded_page(client, monkeypatch):
    from src.server.app import memory
    from src.server.services.refactor import memory_export

    store = object()
    export = AsyncMock(
        return_value={"items": [{"key": "fact.md", "content": "body"}], "next_cursor": None}
    )
    monkeypatch.setattr(memory, "_long_term_store", lambda: store)
    monkeypatch.setattr(memory_export, "export_memory_page", export)

    response = await client.get(
        "/api/v1/memory/export?limit=7&cursor=0%3A5&include_secret=true"
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [{"key": "fact.md", "content": "body"}],
        "next_cursor": None,
    }
    export.assert_awaited_once_with(
        store=store,
        user_id="test-user-123",
        limit=7,
        cursor="0:5",
        include_secret=True,
    )


async def test_export_route_rejects_invalid_page_size(client, monkeypatch):
    from src.server.services.refactor import memory_export

    export = AsyncMock()
    monkeypatch.setattr(memory_export, "export_memory_page", export)

    response = await client.get("/api/v1/memory/export?limit=201")

    assert response.status_code == 422
    export.assert_not_awaited()


async def test_export_route_classifies_invalid_cursor_as_client_error(client, monkeypatch):
    from src.server.app import memory

    monkeypatch.setattr(memory, "_long_term_store", lambda: object())

    response = await client.get("/api/v1/memory/export?cursor=invalid")

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid memory export cursor"}
