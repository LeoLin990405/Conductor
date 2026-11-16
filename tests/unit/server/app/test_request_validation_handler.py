"""A 422 names the field and the rule, never the value the caller sent.

FastAPI's default handler echoes each error's ``input`` and ``ctx``. On the
daemon output route that input is live Worker output, which the guard exists to
keep out of anything stored, logged or returned. The real app is driven because
``create_test_app`` installs no handlers and would test FastAPI's default.
"""

from __future__ import annotations

import logging

import pytest
import pytest_asyncio
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient

from src.server.app import setup

FAKE = "sk-" "ant-api03-FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0123456789"
TICKET = "66666666-6666-4666-8666-666666666666"
LEASE = "77777777-7777-4777-8777-777777777777"
URL = f"/api/v1/autonomy/daemon/tickets/{TICKET}/output"
GOOD = [{"kind": "text", "text": "ok"}]

CASES = {
    "overlong_text": ({"lease_id": LEASE, "events": [{"kind": "text", "text": FAKE + "x" * 4100}]}, ["body", "events", 0, "text"]),
    "too_many_events": ({"lease_id": LEASE, "events": [{"kind": "text", "text": FAKE}] * 65}, ["body", "events"]),
    "overlong_tool_name": ({"lease_id": LEASE, "events": [{"kind": "tool", "tools": [FAKE + "y" * 300]}]}, ["body", "events", 0, "tools", 0]),
    "wrong_type_text": ({"lease_id": LEASE, "events": [{"kind": "text", "text": [FAKE]}]}, ["body", "events", 0, "text"]),
    "bad_kind_beside_secret_text": ({"lease_id": LEASE, "events": [{"kind": "text", "text": FAKE}, {"kind": "nonsense", "text": FAKE}]}, ["body", "events", 1, "kind"]),
    "bad_lease_id": ({"lease_id": FAKE, "events": GOOD}, ["body", "lease_id"]),
    "bad_epoch": ({"lease_id": LEASE, "epoch": FAKE, "events": GOOD}, ["body", "epoch"]),
    "bad_run_id": ({"lease_id": LEASE, "run_id": FAKE * 10, "events": GOOD}, ["body", "run_id"]),
}


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=setup.app), base_url="http://test"
    ) as http:
        yield http


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(CASES))
async def test_a_422_names_the_field_and_never_echoes_the_value(client, caplog, name):
    body, loc = CASES[name]
    with caplog.at_level(logging.DEBUG):
        response = await client.post(
            URL, headers={"X-Daemon-Token": "a" * 43}, json=body
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail and all(set(entry) == {"type", "loc", "msg"} for entry in detail)
    assert loc in [entry["loc"] for entry in detail]
    assert FAKE[:20] not in response.text
    assert all(FAKE[:20] not in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_a_malformed_json_body_is_refused_without_its_context(client):
    response = await client.post(
        URL,
        headers={"X-Daemon-Token": "a" * 43, "Content-Type": "application/json"},
        content=b'{"lease_id": ' + FAKE.encode(),
    )
    assert response.status_code == 422
    assert all(set(entry) == {"type", "loc", "msg"} for entry in response.json()["detail"])
    assert FAKE[:20] not in response.text


def test_the_app_answers_validation_errors_with_the_public_handler():
    # Removing the registration would silently restore FastAPI's echoing default.
    handlers = setup.app.exception_handlers
    assert handlers[RequestValidationError] is setup._request_validation_handler
