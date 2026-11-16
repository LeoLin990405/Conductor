"""Line dashboard and ownership-operation routes: gating, mapping, and shapes."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app

USER_ID = "test-user-123"
TICKET_ID = "22222222-2222-4222-8222-222222222222"
LINE_ID = "33333333-3333-4333-8333-333333333333"
DAEMON_ID = "44444444-4444-4444-8444-444444444444"


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _feature(enabled: bool = True):
    return patch(
        "src.server.app.autonomy.features.user_feature_enabled",
        new=AsyncMock(return_value=enabled),
    )


def _ticket(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    row = {
        "ticket_id": TICKET_ID,
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "Fix the save button",
        "scope": "Change the label to Save",
        "status": "queued",
        "mandate": {"strength": "explicit"},
        "origin_trigger_id": None,
        "origin_thread_key": "github:acme/widgets#12",
        "router_thread_id": None,
        "worker_thread_id": None,
        "worker_run_id": None,
        "worker_provider": "",
        "worker_model": "",
        "worker_daemon_id": None,
        "attempts": 0,
        "attention": None,
        "summary": "",
        "error": "",
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "ended_at": None,
    }
    row.update(overrides)
    return row


def _line(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    row = {
        "line_id": LINE_ID,
        "user_id": USER_ID,
        "name": "ob-tickets",
        "branch": "feat/ob-tickets",
        "worktree_path": "/wt/ob-tickets",
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    row.update(overrides)
    return row


def _overview() -> dict:
    return {
        "lines": [
            {
                "line": _line(),
                "tickets": [
                    {
                        "ticket_id": TICKET_ID,
                        "title": "Fix the save button",
                        "status": "running",
                        "owner": "m5-daemon",
                        "worker_provider": "codex",
                        "attempts": 1,
                        "updated_at": datetime.now(timezone.utc),
                        "last_activity_at": datetime.now(timezone.utc),
                    }
                ],
                "ticket_count": 1,
                "open_count": 1,
                "last_activity_at": datetime.now(timezone.utc),
            }
        ],
        "ungrouped": None,
    }


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_lines_is_hidden_when_feature_is_off(client):
    with (
        _feature(False),
        patch(
            "src.server.app.autonomy.line_db.ticket_lines_overview",
            new=AsyncMock(),
        ) as overview,
    ):
        resp = await client.get("/api/v1/autonomy/tickets/lines")
    assert resp.status_code == 404
    overview.assert_not_awaited()


@pytest.mark.asyncio
async def test_ticket_lines_returns_the_grouped_dashboard(client):
    with (
        _feature(True),
        patch(
            "src.server.app.autonomy.line_db.ticket_lines_overview",
            new=AsyncMock(return_value=_overview()),
        ) as overview,
    ):
        resp = await client.get("/api/v1/autonomy/tickets/lines?include_archived=true")
    assert resp.status_code == 200
    overview.assert_awaited_once_with(USER_ID, include_archived=True)
    body = resp.json()
    (group,) = body["lines"]
    assert group["line"]["name"] == "ob-tickets"
    assert group["line"]["branch"] == "feat/ob-tickets"
    assert group["tickets"][0]["owner"] == "m5-daemon"
    assert group["tickets"][0]["status"] == "running"
    assert "last_activity_at" in group["tickets"][0]


# ---------------------------------------------------------------------------
# Ownership operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_delegates_to_the_ownership_service(client):
    with (
        _feature(True),
        patch(
            "src.server.app.autonomy.ownership.resume_ticket",
            new=AsyncMock(return_value=_ticket(status="queued")),
        ) as resume,
    ):
        resp = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/resume")
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    resume.assert_awaited_once_with(
        user_id=USER_ID, ticket_id=TICKET_ID, force=False
    )


@pytest.mark.asyncio
async def test_resume_maps_a_refusal_to_its_status_code(client):
    from src.server.services.autonomy import ownership

    with (
        _feature(True),
        patch(
            "src.server.app.autonomy.ownership.resume_ticket",
            new=AsyncMock(side_effect=ownership.TicketOpError(409, "Ticket is not stopped")),
        ),
    ):
        resp = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/resume")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Ticket is not stopped"


@pytest.mark.asyncio
async def test_reassign_passes_the_daemon_id_as_text(client):
    with (
        _feature(True),
        patch(
            "src.server.app.autonomy.ownership.reassign_ticket",
            new=AsyncMock(return_value=_ticket(worker_daemon_id=DAEMON_ID)),
        ) as reassign,
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/reassign",
            json={"daemon_id": DAEMON_ID},
        )
    assert resp.status_code == 200
    reassign.assert_awaited_once_with(
        user_id=USER_ID, ticket_id=TICKET_ID, daemon_id=DAEMON_ID, force=False
    )


@pytest.mark.asyncio
async def test_reassign_without_a_daemon_targets_cloud(client):
    with (
        _feature(True),
        patch(
            "src.server.app.autonomy.ownership.reassign_ticket",
            new=AsyncMock(return_value=_ticket()),
        ) as reassign,
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/reassign", json={}
        )
    assert resp.status_code == 200
    reassign.assert_awaited_once_with(
        user_id=USER_ID, ticket_id=TICKET_ID, daemon_id=None, force=False
    )


@pytest.mark.asyncio
async def test_archive_delegates_to_the_ownership_service(client):
    with (
        _feature(True),
        patch(
            "src.server.app.autonomy.ownership.archive_ticket",
            new=AsyncMock(return_value=_ticket(status="archived")),
        ) as archive,
    ):
        resp = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/archive")
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"
    archive.assert_awaited_once_with(user_id=USER_ID, ticket_id=TICKET_ID)


@pytest.mark.asyncio
async def test_set_ticket_line_validates_through_the_service(client):
    with (
        _feature(True),
        patch(
            "src.server.app.autonomy.ownership.assign_ticket_line",
            new=AsyncMock(return_value=_ticket()),
        ) as assign,
    ):
        resp = await client.put(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/line",
            json={"line_id": LINE_ID},
        )
    assert resp.status_code == 200
    assign.assert_awaited_once_with(
        user_id=USER_ID, ticket_id=TICKET_ID, line_id=LINE_ID
    )


# ---------------------------------------------------------------------------
# Line management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lines_round_trip(client):
    from psycopg.errors import UniqueViolation

    with (
        _feature(True),
        patch(
            "src.server.app.autonomy.line_db.list_lines",
            new=AsyncMock(return_value=[_line()]),
        ),
    ):
        resp = await client.get("/api/v1/autonomy/lines")
    assert resp.status_code == 200
    assert resp.json()["lines"][0]["name"] == "ob-tickets"

    created = _line()
    with (
        _feature(True),
        patch(
            "src.server.app.autonomy.line_db.create_line",
            new=AsyncMock(return_value=created),
        ) as create,
    ):
        resp = await client.post(
            "/api/v1/autonomy/lines",
            json={
                "name": "ob-tickets",
                "branch": "feat/ob-tickets",
                "worktree_path": "/wt/ob-tickets",
            },
        )
    assert resp.status_code == 201
    create.assert_awaited_once_with(
        user_id=USER_ID,
        name="ob-tickets",
        branch="feat/ob-tickets",
        worktree_path="/wt/ob-tickets",
    )

    with (
        _feature(True),
        patch(
            "src.server.app.autonomy.line_db.create_line",
            new=AsyncMock(side_effect=UniqueViolation("dup")),
        ),
    ):
        resp = await client.post("/api/v1/autonomy/lines", json={"name": "ob-tickets"})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_archive_line_404s_for_a_missing_line(client):
    with (
        _feature(True),
        patch(
            "src.server.app.autonomy.line_db.archive_line",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = await client.post(f"/api/v1/autonomy/lines/{LINE_ID}/archive")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ticket_detail_includes_the_audit_trail(client):
    events = [
        {
            "event_id": "55555555-5555-4555-8555-555555555555",
            "ticket_id": TICKET_ID,
            "actor": "user",
            "action": "reassigned",
            "from_owner": "cloud",
            "to_owner": DAEMON_ID,
            "detail": {"from_status": "queued"},
            "created_at": datetime.now(timezone.utc),
        }
    ]
    with (
        _feature(True),
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
            new=AsyncMock(return_value=events),
        ),
        patch(
            "src.server.database.autonomy.stops.get_stop",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = await client.get(f"/api/v1/autonomy/tickets/{TICKET_ID}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"][0]["action"] == "reassigned"
    assert body["events"][0]["from_owner"] == "cloud"


@pytest.mark.asyncio
async def test_lines_endpoints_are_hidden_when_feature_is_off(client):
    with _feature(False):
        resp = await client.get("/api/v1/autonomy/lines")
    assert resp.status_code == 404
    with _feature(False):
        resp = await client.post("/api/v1/autonomy/lines", json={"name": "x"})
    assert resp.status_code == 404
