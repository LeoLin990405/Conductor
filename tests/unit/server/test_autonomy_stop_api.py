"""Stop API: a native-daemon stop surfaces verified or unverified state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app

USER_ID = "test-user-123"
TICKET_ID = "11111111-1111-4111-8111-111111111111"
DAEMON_ID = "22222222-2222-4222-8222-222222222222"
LEASE_ID = "33333333-3333-4333-8333-333333333333"
STOP_ID = "44444444-4444-4444-8444-444444444444"


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _feature_on():
    return patch(
        "src.server.app.autonomy.features.user_feature_enabled",
        new=AsyncMock(return_value=True),
    )


def _daemon_authed():
    return patch(
        "src.server.app.autonomy._require_daemon",
        new=AsyncMock(return_value={"daemon_id": DAEMON_ID, "user_id": USER_ID}),
    )


def _ticket(status: str = "cancelled") -> dict:
    now = datetime.now(timezone.utc)
    return {
        "ticket_id": TICKET_ID,
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "Ship it",
        "scope": "Ship it",
        "status": status,
        "mandate": {"source_id": "s1"},
        "origin_trigger_id": None,
        "origin_thread_key": "manual:1",
        "router_thread_id": None,
        "worker_thread_id": None,
        "worker_run_id": None,
        "worker_provider": "codex",
        "worker_model": "",
        "worker_daemon_id": DAEMON_ID,
        "attempts": 1,
        "attention": None,
        "summary": "",
        "error": "",
        "created_at": now,
        "updated_at": now,
        "started_at": now,
        "ended_at": now,
    }


def _stop(verification: str = "pending", **overrides) -> dict:
    now = datetime.now(timezone.utc)
    row = {
        "stop_id": STOP_ID,
        "ticket_id": TICKET_ID,
        "mode": "daemon",
        "verification": verification,
        "verified": verification == "verified",
        "escalated": verification == "unverified",
        "requested_at": now - timedelta(seconds=5),
        "deadline_at": now + timedelta(seconds=25),
        "acknowledged_at": now if verification in {"verified", "unverified"} else None,
        "verified_at": now if verification == "verified" else None,
        "escalated_at": now if verification == "unverified" else None,
        "reason": "",
        "evidence": {"processes": [{"pid": 7, "alive": False}]},
        "attempts": 1,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_stop_ticket_routes_through_ownership_and_surfaces_verification(client):
    request = AsyncMock(
        return_value={"ticket": _ticket(), "stop": _stop("pending")}
    )
    with (
        _feature_on(),
        patch("src.server.app.autonomy.ownership.stop_ticket", new=request),
    ):
        resp = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/stop")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["stop"]["mode"] == "daemon"
    assert body["stop"]["verification"] == "pending"
    assert body["stop"]["verified"] is False
    request.assert_awaited_once_with(user_id=USER_ID, ticket_id=TICKET_ID)


@pytest.mark.asyncio
async def test_stop_ticket_maps_a_finished_ticket_to_409(client):
    from src.server.services.autonomy import ownership

    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.ownership.stop_ticket",
            new=AsyncMock(
                side_effect=ownership.TicketOpError(409, "Ticket already ended")
            ),
        ),
    ):
        resp = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/stop")

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Ticket already ended"


@pytest.mark.asyncio
async def test_stop_ticket_maps_an_unknown_ticket_to_404(client):
    from src.server.services.autonomy import ownership

    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.ownership.stop_ticket",
            new=AsyncMock(side_effect=ownership.TicketOpError(404, "Ticket not found")),
        ),
    ):
        resp = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/stop")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_acknowledge_unverified_stop_routes_through_ownership(client):
    request = AsyncMock(
        return_value={"ticket": _ticket(), "stop": _stop("verified")}
    )
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.ownership.acknowledge_unverified_stop",
            new=request,
        ),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/stop/acknowledge-unverified"
        )

    assert resp.status_code == 200
    assert resp.json()["stop"]["verification"] == "verified"
    request.assert_awaited_once_with(user_id=USER_ID, ticket_id=TICKET_ID)


@pytest.mark.asyncio
async def test_daemon_stop_ack_returns_the_verified_stop(client):
    ack = AsyncMock(return_value={"outcome": "ok", "stop": _stop("verified")})
    with (
        _daemon_authed(),
        patch("src.server.services.autonomy.stop.acknowledge_ticket_stop", new=ack),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/stop/ack",
            headers={"X-Daemon-Token": "a" * 43},
            json={
                "lease_id": LEASE_ID,
                "process_gone": True,
                "signal": "SIGKILL",
                "pid": 7,
                "pgid": 7,
                "processes": [{"pid": 7, "alive": False}],
            },
        )

    assert resp.status_code == 200
    assert resp.json()["stop"]["verified"] is True
    kwargs = ack.await_args.kwargs
    assert kwargs["daemon_id"] == DAEMON_ID
    assert kwargs["ticket_id"] == TICKET_ID
    assert kwargs["lease_id"] == LEASE_ID
    assert kwargs["process_gone"] is True
    assert kwargs["evidence"]["signal"] == "SIGKILL"
    assert kwargs["evidence"]["processes"] == [{"pid": 7, "alive": False}]


@pytest.mark.asyncio
async def test_daemon_stop_ack_maps_a_heartbeat_race_to_409(client):
    with (
        _daemon_authed(),
        patch(
            "src.server.services.autonomy.stop.acknowledge_ticket_stop",
            new=AsyncMock(
                return_value={"outcome": "heartbeat_active", "stop": _stop()}
            ),
        ),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/stop/ack",
            headers={"X-Daemon-Token": "a" * 43},
            json={"lease_id": LEASE_ID, "process_gone": True},
        )

    assert resp.status_code == 409
    assert "heartbeat" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_ticket_detail_carries_the_verified_stop(client):
    now = datetime.now(timezone.utc)
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.list_inputs",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.list_reports",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "src.server.app.autonomy.line_db.list_ticket_events",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "src.server.database.autonomy.stops.get_stop",
            new=AsyncMock(
                return_value=_stop("verified", verified_at=now, acknowledged_at=now)
            ),
        ),
    ):
        resp = await client.get(f"/api/v1/autonomy/tickets/{TICKET_ID}")

    assert resp.status_code == 200
    assert resp.json()["stop"]["verification"] == "verified"


@pytest.mark.asyncio
async def test_ticket_list_carries_an_unverified_stop(client):
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.list_tickets",
            new=AsyncMock(return_value=[_ticket()]),
        ),
        patch(
            "src.server.database.autonomy.stops.list_stops",
            new=AsyncMock(return_value=[_stop("unverified")]),
        ),
    ):
        resp = await client.get("/api/v1/autonomy/tickets")

    assert resp.status_code == 200
    stop = resp.json()["tickets"][0]["stop"]
    assert stop["verification"] == "unverified"
    assert stop["verified"] is False
    assert stop["escalated"] is True
