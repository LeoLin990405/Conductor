"""Daemon live-output API: token auth, live-lease fence, best-effort store."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app

DAEMON_ID = "55555555-5555-4555-8555-555555555555"
TICKET_ID = "66666666-6666-4666-8666-666666666666"
LEASE_ID = "77777777-7777-4777-8777-777777777777"
URL = f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/output"
TOKEN_HEADERS = {"X-Daemon-Token": "a" * 43}
STORE = "src.server.services.autonomy.daemon_output"


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _daemon_authed():
    return patch(
        "src.server.app.autonomy._require_daemon",
        new=AsyncMock(return_value={"daemon_id": DAEMON_ID, "user_id": "u1"}),
    )


def _payload(**overrides):
    body = {
        "lease_id": LEASE_ID,
        "run_id": "lease-generation-1",
        "events": [{"kind": "text", "text": "working on it"}],
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_output_requires_a_daemon_token(client):
    response = await client.post(URL, json=_payload())
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_output_accepts_and_stores_a_batch_with_the_fence_fields(client):
    from src.server.services.autonomy.daemon_output import AppendResult

    append = AsyncMock(
        return_value=AppendResult(stored=True, epoch=3, head="3.9", events=2)
    )
    with _daemon_authed(), patch(f"{STORE}.append_output", new=append):
        response = await client.post(
            URL,
            headers=TOKEN_HEADERS,
            json=_payload(
                epoch=3,
                batch_seq=4,
                events=[
                    {"kind": "text", "text": "\x1b[2Jvisible\n"},
                    {"kind": "tool", "tools": ["bash", ""]},
                    {"kind": "text", "text": ""},
                ],
            ),
        )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "stored": True,
        "events": 2,
        "epoch": 3,
        "head": "3.9",
        "tainted": False,
    }
    args = append.await_args.args
    assert args[:4] == (DAEMON_ID, TICKET_ID, LEASE_ID, "lease-generation-1")
    assert args[4:6] == (3, 4)
    # Text reaches the store raw: whether it may be stored is the persisted
    # scan state's decision (covered by the store tests), never a rewrite here.
    assert args[6] == [
        {"kind": "text", "text": "\x1b[2Jvisible\n"},
        {"kind": "tool", "tools": ["bash"]},
    ]


@pytest.mark.asyncio
async def test_output_first_batch_carries_no_epoch(client):
    from src.server.services.autonomy.daemon_output import AppendResult

    append = AsyncMock(return_value=AppendResult(stored=True, epoch=1, head="1.1"))
    with _daemon_authed(), patch(f"{STORE}.append_output", new=append):
        response = await client.post(URL, headers=TOKEN_HEADERS, json=_payload())
    assert response.status_code == 200
    assert append.await_args.args[4:6] == (None, 1)


@pytest.mark.asyncio
async def test_output_reports_a_taint_with_a_fixed_reason(client):
    from src.server.services.autonomy.daemon_output import AppendResult

    result = AppendResult(
        stored=False, epoch=2, tainted=True, reason="credential_key", events=1
    )
    with (
        _daemon_authed(),
        patch(f"{STORE}.append_output", new=AsyncMock(return_value=result)),
    ):
        response = await client.post(URL, headers=TOKEN_HEADERS, json=_payload(epoch=2))
    assert response.status_code == 200
    assert response.json()["stored"] is False
    assert response.json()["tainted"] is True
    assert response.json()["reason"] == "credential_key"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["stale_output_epoch", "duplicate_output_batch", "bad_output_batch"]
)
async def test_output_fence_violations_are_structured_409s(client, code):
    from src.server.services.autonomy.daemon_output import AppendResult

    result = AppendResult(stored=False, epoch=5, code=code)
    with (
        _daemon_authed(),
        patch(f"{STORE}.append_output", new=AsyncMock(return_value=result)),
    ):
        response = await client.post(URL, headers=TOKEN_HEADERS, json=_payload(epoch=4))
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": code, "epoch": 5}


@pytest.mark.asyncio
async def test_output_rejects_a_stale_lease(client):
    from src.server.services.autonomy.daemon_output import AppendResult

    result = AppendResult(stored=False, code="stale_lease")
    with (
        _daemon_authed(),
        patch(f"{STORE}.append_output", new=AsyncMock(return_value=result)),
    ):
        response = await client.post(URL, headers=TOKEN_HEADERS, json=_payload())
    assert response.status_code == 409
    assert "stale" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_output_succeeds_with_stored_false_when_the_store_is_unavailable(client):
    from src.server.services.autonomy.daemon_output import AppendResult

    result = AppendResult(stored=False, epoch=0, code="store_unavailable", events=1)
    with (
        _daemon_authed(),
        patch(f"{STORE}.append_output", new=AsyncMock(return_value=result)),
    ):
        response = await client.post(URL, headers=TOKEN_HEADERS, json=_payload())
    assert response.status_code == 200
    assert response.json() == {
        "stored": False,
        "events": 1,
        "epoch": 0,
        "head": None,
        "tainted": False,
    }


@pytest.mark.asyncio
async def test_empty_batch_never_touches_the_store(client):
    append = AsyncMock()
    with _daemon_authed(), patch(f"{STORE}.append_output", new=append):
        response = await client.post(
            URL, headers=TOKEN_HEADERS, json=_payload(events=[])
        )
    assert response.status_code == 200
    assert response.json() == {"stored": False, "events": 0, "epoch": 0}
    append.assert_not_awaited()


@pytest.mark.asyncio
async def test_output_end_to_end_through_the_real_store_enforces_the_fence(client):
    """Endpoint + service + (in-memory) database: epoch echo, replay, taint."""
    from tests.unit.server.fake_output_db import FakeOutputDb

    fake = FakeOutputDb()
    fake.allow(DAEMON_ID, TICKET_ID, LEASE_ID)
    with _daemon_authed(), patch(f"{STORE}.output_db", new=fake):
        first = await client.post(URL, headers=TOKEN_HEADERS, json=_payload())
        assert first.status_code == 200 and first.json()["epoch"] == 1
        # The payload text has no trailing whitespace, so the stream taints.
        assert first.json()["stored"] is False and first.json()["tainted"] is True
        fresh = await client.post(
            URL,
            headers=TOKEN_HEADERS,
            json=_payload(events=[{"kind": "text", "text": "working on it\n"}]),
        )
        assert fresh.json()["epoch"] == 2 and fresh.json()["stored"] is True
        ok = await client.post(
            URL,
            headers=TOKEN_HEADERS,
            json=_payload(
                epoch=2, batch_seq=2, events=[{"kind": "text", "text": "more\n"}]
            ),
        )
        assert ok.json()["head"] == "2.2"
        replay = await client.post(
            URL,
            headers=TOKEN_HEADERS,
            json=_payload(
                epoch=2, batch_seq=2, events=[{"kind": "text", "text": "more\n"}]
            ),
        )
        assert replay.status_code == 409
        assert replay.json()["detail"]["code"] == "duplicate_output_batch"
        wrong = await client.post(
            URL,
            headers=TOKEN_HEADERS,
            json=_payload(
                epoch=9, batch_seq=3, events=[{"kind": "text", "text": "x\n"}]
            ),
        )
        assert wrong.status_code == 409
        assert wrong.json()["detail"] == {"code": "stale_output_epoch", "epoch": 2}
    assert fake.stored_text(TICKET_ID) == "working on it\nmore\n"


@pytest.mark.asyncio
async def test_output_validates_the_payload_shape(client):
    with _daemon_authed():
        response = await client.post(
            URL,
            headers=TOKEN_HEADERS,
            json=_payload(events=[{"kind": "not-a-kind", "text": "x"}]),
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_output_rejects_an_oversized_batch(client):
    with _daemon_authed():
        response = await client.post(
            URL,
            headers=TOKEN_HEADERS,
            json=_payload(events=[{"kind": "text", "text": "x"} for _ in range(65)]),
        )
    assert response.status_code == 422


def test_daemon_output_event_contract_never_carries_arguments():
    """The API model has no field for tool arguments/results or prompts."""
    from src.server.models.autonomy import DaemonOutputEvent

    fields = set(DaemonOutputEvent.model_fields)
    assert fields == {"kind", "text", "tools", "join"}
    from src.server.models.autonomy import DaemonOutputBatch

    assert "join" in fields  # a flag, not a payload
    assert set(DaemonOutputBatch.model_fields) == {
        "lease_id",
        "run_id",
        "epoch",
        "batch_seq",
        "events",
    }
    encoded = DaemonOutputEvent(kind="tool", tools=["bash"]).model_dump_json()
    assert "args" not in json.loads(encoded)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("epoch", 0), ("epoch", -1), ("batch_seq", 0)])
async def test_output_rejects_out_of_range_fence_fields(client, field, value):
    with _daemon_authed():
        response = await client.post(
            URL, headers=TOKEN_HEADERS, json=_payload(**{field: value})
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_output_accepts_the_daemon_stop_marker_and_taints(client):
    from tests.unit.server.fake_output_db import FakeOutputDb

    fake = FakeOutputDb()
    fake.allow(DAEMON_ID, TICKET_ID, LEASE_ID)
    with _daemon_authed(), patch(f"{STORE}.output_db", new=fake):
        first = await client.post(
            URL,
            headers=TOKEN_HEADERS,
            json=_payload(events=[{"kind": "text", "text": "clean\n"}]),
        )
        epoch = first.json()["epoch"]
        stop = await client.post(
            URL,
            headers=TOKEN_HEADERS,
            json=_payload(epoch=epoch, batch_seq=2, events=[{"kind": "stop"}]),
        )
    assert stop.status_code == 200
    assert stop.json()["tainted"] is True
    assert fake.kinds(TICKET_ID) == ["text", "output_reset"]


@pytest.mark.asyncio
async def test_output_rejects_an_oversized_tool_name(client):
    with _daemon_authed():
        response = await client.post(
            URL,
            headers=TOKEN_HEADERS,
            json=_payload(events=[{"kind": "tool", "tools": ["x" * 257]}]),
        )
    assert response.status_code == 422
