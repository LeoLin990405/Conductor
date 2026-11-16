"""Unread operator-notification badge."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from src.server.app.readiness import router as readiness_router
from src.server.services.autonomy.notifications import (
    KIND_FAILURE,
    KIND_STUCK,
    MemoryNotificationStore,
    NotificationEvent,
    override_store,
)
from tests.conftest import create_test_app


@pytest.mark.asyncio
async def test_badge_counts_the_signed_in_user_and_is_not_cached():
    store = MemoryNotificationStore()
    await store.add(
        NotificationEvent(
            kind=KIND_STUCK,
            dedup_key="notify:stuck:mine:running",
            user_id="test-user-123",
            title="Ticket is stuck",
            body="mine",
            subject_id="mine",
        )
    )
    await store.add(
        NotificationEvent(
            kind=KIND_FAILURE,
            dedup_key="notify:failure:other:abcd",
            user_id="other-user",
            title="Ticket failed",
            body="other",
            subject_id="other",
        )
    )
    app = create_test_app(readiness_router)
    with override_store(store):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/system/notifications/badge")
    assert response.status_code == 200
    assert response.json() == {"unread": 1}
    assert response.headers["cache-control"] == "private, no-store"
