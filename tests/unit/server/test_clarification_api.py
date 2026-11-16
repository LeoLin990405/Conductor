"""Clarification gate API: GET/POST on /tickets/{ticket_id}/clarification."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app

USER_ID = "test-user-123"
TICKET_ID = "22222222-2222-4222-8222-222222222222"
CLARIFICATION_ID = "33333333-3333-4333-8333-333333333333"


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _ticket(status: str = "awaiting_clarification", user_id: str = USER_ID) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "ticket_id": TICKET_ID,
        "user_id": user_id,
        "workspace_id": "w1",
        "title": "Fix the save button",
        "scope": "Change the label to Save",
        "status": status,
        "mandate": {"strength": "explicit", "source_id": "s1"},
        "origin_trigger_id": "trg_1",
        "origin_thread_key": "github:acme/widgets#12",
        "router_thread_id": None,
        "worker_thread_id": None,
        "worker_run_id": None,
        "attempts": 0,
        "attention": None,
        "summary": "",
        "error": "",
        "created_at": now,
        "updated_at": now,
    }


def _clarification() -> dict:
    return {
        "clarification_id": CLARIFICATION_ID,
        "ticket_id": TICKET_ID,
        "user_id": USER_ID,
        "original_prompt": "fix save",
        "clarity_score": 0.42,
        "expanded_request": "Change the button label to Save across the editor.",
        "assumptions": ["The button is in the toolbar"],
        "open_questions": ["Which toolbar?"],
        "status": "awaiting",
        "confirmed_text": None,
        "created_at": datetime.now(timezone.utc),
        "resolved_at": None,
    }


def _feature_on():
    return patch(
        "src.server.app.autonomy.features.user_feature_enabled",
        new=AsyncMock(return_value=True),
    )


@pytest.mark.asyncio
async def test_get_clarification_returns_active_row(client):
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.app.autonomy.clarification_db.get_active_clarification",
            new=AsyncMock(return_value=_clarification()),
        ),
    ):
        resp = await client.get(f"/api/v1/autonomy/tickets/{TICKET_ID}/clarification")
    assert resp.status_code == 200
    body = resp.json()
    assert body["clarification_id"] == CLARIFICATION_ID
    assert body["ticket_id"] == TICKET_ID
    assert body["status"] == "awaiting"
    assert body["clarity_score"] == 0.42
    assert body["original_prompt"] == "fix save"
    assert body["expanded_request"] == "Change the button label to Save across the editor."
    assert body["assumptions"] == ["The button is in the toolbar"]
    assert body["open_questions"] == ["Which toolbar?"]
    assert body["confirmed_text"] is None
    assert "created_at" in body


@pytest.mark.asyncio
async def test_get_clarification_404_when_none_active(client):
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.app.autonomy.clarification_db.get_active_clarification",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = await client.get(f"/api/v1/autonomy/tickets/{TICKET_ID}/clarification")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "No active clarification"


@pytest.mark.asyncio
async def test_get_clarification_404_for_unowned_ticket(client):
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_ticket(user_id="someone-else")),
        ),
        patch(
            "src.server.app.autonomy.clarification_db.get_active_clarification",
            new=AsyncMock(return_value=_clarification()),
        ),
    ):
        resp = await client.get(f"/api/v1/autonomy/tickets/{TICKET_ID}/clarification")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Ticket not found"


@pytest.mark.asyncio
async def test_confirm_clarification_with_explicit_text(client):
    confirm = AsyncMock(return_value=_ticket(status="queued"))
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.app.autonomy.clarification_db.get_active_clarification",
            new=AsyncMock(return_value=_clarification()),
        ),
        patch("src.server.app.autonomy.confirm_clarification", new=confirm),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/clarification/confirm",
            json={"text": "Use the main toolbar"},
        )
    assert resp.status_code == 200
    confirm.assert_awaited_once_with(ticket_id=TICKET_ID, text="Use the main toolbar")
    body = resp.json()
    assert body["ticket_id"] == TICKET_ID
    assert body["status"] == "queued"


@pytest.mark.asyncio
async def test_confirm_clarification_falls_back_to_expanded_request(client):
    confirm = AsyncMock(return_value=_ticket(status="queued"))
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.app.autonomy.clarification_db.get_active_clarification",
            new=AsyncMock(return_value=_clarification()),
        ),
        patch("src.server.app.autonomy.confirm_clarification", new=confirm),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/clarification/confirm",
            json={},
        )
    assert resp.status_code == 200
    confirm.assert_awaited_once_with(
        ticket_id=TICKET_ID,
        text="Change the button label to Save across the editor.",
    )


@pytest.mark.asyncio
async def test_confirm_clarification_409_when_not_awaiting(client):
    confirm = AsyncMock(return_value=_ticket(status="queued"))
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_ticket(status="queued")),
        ),
        patch(
            "src.server.app.autonomy.clarification_db.get_active_clarification",
            new=AsyncMock(return_value=_clarification()),
        ),
        patch("src.server.app.autonomy.confirm_clarification", new=confirm),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/clarification/confirm",
            json={"text": "hello"},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Ticket is not awaiting clarification"
    confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_clarification_404_for_unowned_ticket(client):
    confirm = AsyncMock(return_value=_ticket(status="queued"))
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_ticket(user_id="someone-else")),
        ),
        patch(
            "src.server.app.autonomy.clarification_db.get_active_clarification",
            new=AsyncMock(return_value=_clarification()),
        ),
        patch("src.server.app.autonomy.confirm_clarification", new=confirm),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/clarification/confirm",
            json={"text": "hello"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Ticket not found"
    confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_clarification_maps_value_error_to_409(client):
    confirm = AsyncMock(side_effect=ValueError("no active clarification"))
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.app.autonomy.clarification_db.get_active_clarification",
            new=AsyncMock(return_value=_clarification()),
        ),
        patch("src.server.app.autonomy.confirm_clarification", new=confirm),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/clarification/confirm",
            json={"text": "hello"},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "no active clarification"
