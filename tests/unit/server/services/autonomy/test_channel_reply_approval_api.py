"""The approval queue: a person reads a held reply before it is sent.

Article §4.2 asks the agent to draft a structured email for a person to
review and send. That is a durable state, not a message: the draft waits in
the outbox until somebody approves it, and a rejected draft is settled rather
than sent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app

USER_ID = "test-user-123"
OUTBOX_ID = "22222222-2222-4222-8222-222222222222"


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _outbox(status: str = "pending_approval", user_id: str = USER_ID) -> dict:
    return {
        "outbox_id": OUTBOX_ID,
        "user_id": user_id,
        "ticket_id": "33333333-3333-4333-8333-333333333333",
        "source_id": "44444444-4444-4444-8444-444444444444",
        "thread_key": "email:ops@example.com",
        "body": "Subject: Conductor update\n\nSummary:\nFixed the build.",
        "status": status,
        "channel": "email",
        "reply_kind": "result",
        "created_at": datetime.now(timezone.utc),
        "approved_at": None,
        "approved_by": "",
    }


@pytest.mark.asyncio
async def test_the_approval_queue_lists_the_replies_waiting_for_a_person(client):
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.outbox_db.list_outbox",
            new=AsyncMock(return_value=[_outbox()]),
        ) as listed,
    ):
        resp = await client.get("/api/v1/autonomy/outbox")

    assert resp.status_code == 200
    body = resp.json()
    assert body["replies"][0]["outbox_id"] == OUTBOX_ID
    assert body["replies"][0]["channel"] == "email"
    assert body["replies"][0]["body"].startswith("Subject:")
    listed.assert_awaited_once_with(USER_ID, status="pending_approval", limit=50)


@pytest.mark.asyncio
async def test_the_approval_queue_is_hidden_when_the_feature_is_off(client):
    with patch(
        "src.server.app.autonomy.features.user_feature_enabled",
        new=AsyncMock(return_value=False),
    ):
        resp = await client.get("/api/v1/autonomy/outbox")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_approving_a_reply_releases_it_for_delivery(client):
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.outbox_db.get_outbox",
            new=AsyncMock(return_value=_outbox()),
        ),
        patch(
            "src.server.app.autonomy.outbox_db.mark_approved",
            new=AsyncMock(return_value=True),
        ) as approved,
        patch(
            "src.server.database.autonomy.effects.enqueue_effect",
            new=AsyncMock(return_value=True),
        ) as enqueue,
    ):
        resp = await client.post(f"/api/v1/autonomy/outbox/{OUTBOX_ID}/approve")

    assert resp.status_code == 200
    approved.assert_awaited_once_with(OUTBOX_ID, USER_ID)
    # A distinct key: the held reply may already own an effect row.
    assert enqueue.await_args.kwargs["idempotency_key"] == (
        f"autonomy:reply-effect:{OUTBOX_ID}:approved"
    )
    assert enqueue.await_args.kwargs["payload"] == {"outbox_id": OUTBOX_ID}


@pytest.mark.asyncio
async def test_rejecting_a_reply_settles_it_instead_of_sending_it(client):
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.outbox_db.get_outbox",
            new=AsyncMock(return_value=_outbox()),
        ),
        patch(
            "src.server.app.autonomy.outbox_db.mark_rejected",
            new=AsyncMock(return_value=True),
        ) as rejected,
        patch(
            "src.server.database.autonomy.effects.enqueue_effect",
            new=AsyncMock(return_value=True),
        ) as enqueue,
    ):
        resp = await client.post(f"/api/v1/autonomy/outbox/{OUTBOX_ID}/reject")

    assert resp.status_code == 200
    rejected.assert_awaited_once_with(OUTBOX_ID, USER_ID)
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_reply_of_another_user_is_not_found(client):
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.outbox_db.get_outbox",
            new=AsyncMock(return_value=_outbox(user_id="someone-else")),
        ),
        patch(
            "src.server.app.autonomy.outbox_db.mark_approved",
            new=AsyncMock(return_value=True),
        ) as approved,
    ):
        resp = await client.post(f"/api/v1/autonomy/outbox/{OUTBOX_ID}/approve")

    assert resp.status_code == 404
    approved.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_reply_that_is_not_waiting_cannot_be_approved(client):
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.outbox_db.get_outbox",
            new=AsyncMock(return_value=_outbox(status="sent")),
        ),
        patch(
            "src.server.app.autonomy.outbox_db.mark_approved",
            new=AsyncMock(return_value=True),
        ) as approved,
    ):
        resp = await client.post(f"/api/v1/autonomy/outbox/{OUTBOX_ID}/approve")

    assert resp.status_code == 409
    approved.assert_not_awaited()
