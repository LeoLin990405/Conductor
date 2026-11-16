"""A discriminated-union tag and an extra field name are the caller's text too.

Neither sits in pydantic's ``input``, so they came back in ``msg`` and ``loc``
after the handler dropped ``input``. Driven through the real app on routes whose
models carry a discriminated union and ``extra="forbid"``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.app import setup

FAKE = "sk-" "ant-api03-FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0123456789"

CASES = {
    "union_tag": ("/api/v1/mcp/oauth/start-pending", {"kind": FAKE}, "union_tag_invalid"),
    "extra_field": (
        "/api/v1/autonomy/a2a/agents",
        {"ticket_id": "t", FAKE: 1},
        "extra_forbidden",
    ),
}


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=setup.app), base_url="http://test"
    ) as http:
        yield http


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(CASES))
async def test_the_caller_chosen_tag_or_key_is_not_echoed(client, name):
    url, body, error_type = CASES[name]
    response = await client.post(url, headers={"X-User-Id": "u1"}, json=body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert error_type in [entry["type"] for entry in detail]
    assert all(set(entry) == {"type", "loc", "msg"} for entry in detail)
    assert FAKE[:20] not in response.text
