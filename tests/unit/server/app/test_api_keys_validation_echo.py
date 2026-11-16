"""A credential used as a BYOK provider key is not repeated by the 422 response.

Driven through the real app: the validators' sentences and the handler that
drops pydantic's ``input`` both have to hold for the body to be clean.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.app import setup

PROVIDERS = [
    "Basic dXNlcjpwYXNz",
    "ghp_" + "A" * 36,
    "session: 11111111-2222-3333-4444-555555555555",
    "sk-" + "b" * 40,
]
BODIES = {
    "api_keys_not_ascii": lambda p: {"api_keys": {p: "café"}},
    "api_keys_too_long": lambda p: {"api_keys": {p: "x" * 4097}},
    "base_urls_scheme": lambda p: {"base_urls": {p: "ftp://example.test"}},
    "base_urls_too_long": lambda p: {"base_urls": {p: "http://" + "x" * 600}},
}


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=setup.app), base_url="http://test"
    ) as http:
        yield http


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("name", sorted(BODIES))
async def test_the_provider_key_is_not_in_the_422_body(client, provider, name):
    response = await client.put(
        "/api/v1/users/me/api-keys",
        headers={"X-User-Id": "u1"},
        json=BODIES[name](provider),
    )
    assert response.status_code == 422
    assert provider not in response.text
    # The token part alone, in case the body was cut or re-quoted.
    assert provider.split()[-1] not in response.text
    assert all(set(entry) == {"type", "loc", "msg"} for entry in response.json()["detail"])


MAPPING_KEYS = [
    "Basic dXNlcjpwYXNz",
    "ghp_" + "A" * 36,
    "session: 11111111-2222-3333-4444-555555555555",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", MAPPING_KEYS)
@pytest.mark.parametrize("field", ["api_keys", "base_urls"])
async def test_a_key_whose_value_has_the_wrong_type_is_not_in_loc(client, key, field):
    """Pydantic rejects the value's type before any validator of ours runs, and
    its ``loc`` then ends in the key the caller chose."""
    response = await client.put(
        "/api/v1/users/me/api-keys",
        headers={"X-User-Id": "u1"},
        json={field: {key: {}}},
    )
    assert response.status_code == 422
    assert key not in response.text
    assert key.split()[-1] not in response.text
    (entry,) = response.json()["detail"]
    assert entry["loc"] == ["body", field, "<key>"]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", MAPPING_KEYS + ["ghp_" + "B" * 36])
@pytest.mark.parametrize(
    "value", [{}, "bad\nvalue", "x" * 5000, "$HOME_TOKEN", "${vault:}"]
)
async def test_a_header_name_is_not_echoed_whatever_rule_it_trips(client, key, value):
    response = await client.post(
        "/api/v1/mcp/servers/probe",
        headers={"X-User-Id": "u1"},
        json={"url": "https://example.com/mcp", "headers": {key: value}},
    )
    assert response.status_code == 422
    assert key not in response.text
    assert key.split()[-1] not in response.text


@pytest.mark.asyncio
async def test_the_fields_the_server_defines_are_still_named(client):
    response = await client.post(
        "/api/v1/mcp/servers/probe",
        headers={"X-User-Id": "u1"},
        json={"url": 5, "headers": {}},
    )
    assert response.status_code == 422
    assert [entry["loc"] for entry in response.json()["detail"]] == [["body", "url"]]
