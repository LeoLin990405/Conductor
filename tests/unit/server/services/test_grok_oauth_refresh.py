from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from src.server.services import grok_oauth


@pytest.fixture(autouse=True)
def _oauth_client_settings(monkeypatch):
    # The public build ships no OAuth client settings; tests supply test values.
    monkeypatch.setenv("CONDUCTOR_GROK_OAUTH_CLIENT_ID", "test-conductor-grok-oauth-client-id")


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, *args, **kwargs):
        return FakeResponse({
            "access_token": "new-access",
            "id_token": "new-id",
            "expires_in": 3600,
        })


@pytest.mark.asyncio
async def test_refresh_preserves_existing_refresh_token_when_provider_omits_rotation(monkeypatch):
    monkeypatch.setattr(grok_oauth.httpx, "AsyncClient", FakeClient)

    result = await grok_oauth.refresh_tokens("still-valid-refresh")

    assert result["access_token"] == "new-access"
    assert result["refresh_token"] == "still-valid-refresh"


@pytest.mark.asyncio
async def test_refresh_uses_rotated_refresh_token_when_provider_returns_one(monkeypatch):
    class RotatingClient(FakeClient):
        async def post(self, *args, **kwargs):
            return FakeResponse({
                "access_token": "new-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
            })

    monkeypatch.setattr(grok_oauth.httpx, "AsyncClient", RotatingClient)

    result = await grok_oauth.refresh_tokens("old-refresh")

    assert result["refresh_token"] == "rotated-refresh"


@pytest.mark.asyncio
async def test_lock_owner_rereads_tokens_before_refreshing(monkeypatch):
    from src.utils.cache import redis_cache

    old = {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "account_id": "acct",
        "expires_at": datetime.now(timezone.utc) - timedelta(minutes=10),
    }
    fresh = {
        "access_token": "fresh-access",
        "refresh_token": "rotated-refresh",
        "account_id": "acct",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    reads = iter([old, fresh])
    monkeypatch.setattr(grok_oauth, "get_oauth_tokens", AsyncMock(side_effect=lambda *args: next(reads)))

    class Cache:
        released = False

        async def acquire_lock(self, *args):
            return True

        async def release_lock(self, *args):
            self.released = True

    cache = Cache()
    monkeypatch.setattr(redis_cache, "get_cache_client", lambda: cache)
    refresh = AsyncMock(side_effect=AssertionError("must not reuse the stale refresh token"))
    monkeypatch.setattr(grok_oauth, "refresh_tokens", refresh)

    result = await grok_oauth.get_valid_token("user-id")

    assert result == {"access_token": "fresh-access", "account_id": "acct", "plan_type": None}
    refresh.assert_not_awaited()
    assert cache.released
