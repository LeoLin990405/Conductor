"""A refresh lock must protect the token read as well as its rotation."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.server.services import claude_oauth, codex_oauth
from src.utils.cache import redis_cache


@pytest.mark.asyncio
@pytest.mark.parametrize("service", [claude_oauth, codex_oauth])
async def test_prior_holder_rotated_before_lock_acquisition(service, monkeypatch):
    old = {
        "access_token": "old-access", "refresh_token": "consumed-refresh",
        "account_id": "account", "plan_type": "paid",
        "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
    }
    new = {
        **old, "access_token": "winner-access", "refresh_token": "winner-refresh",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    read = AsyncMock(side_effect=[old, new])
    refresh = AsyncMock(side_effect=AssertionError("must not consume an old refresh token"))
    cache = SimpleNamespace(acquire_lock=AsyncMock(return_value=True), release_lock=AsyncMock())
    monkeypatch.setattr(service, "get_oauth_tokens", read)
    monkeypatch.setattr(service, "refresh_tokens", refresh)
    monkeypatch.setattr(redis_cache, "get_cache_client", lambda: cache)

    result = await service.get_valid_token("user-1")

    assert result["access_token"] == "winner-access"
    assert read.await_count == 2
    refresh.assert_not_awaited()
    cache.release_lock.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("service", [claude_oauth, codex_oauth])
async def test_disconnected_while_waiting_for_lock_does_not_recreate_tokens(service, monkeypatch):
    old = {
        "access_token": "old-access", "refresh_token": "old-refresh",
        "account_id": "account", "expires_at": datetime.now(timezone.utc),
    }
    refresh, write = AsyncMock(), AsyncMock()
    cache = SimpleNamespace(acquire_lock=AsyncMock(return_value=True), release_lock=AsyncMock())
    monkeypatch.setattr(service, "get_oauth_tokens", AsyncMock(side_effect=[old, None]))
    monkeypatch.setattr(service, "refresh_tokens", refresh)
    monkeypatch.setattr(service, "upsert_oauth_tokens", write)
    monkeypatch.setattr(redis_cache, "get_cache_client", lambda: cache)

    assert await service.get_valid_token("user-1") is None
    refresh.assert_not_awaited()
    write.assert_not_awaited()
    cache.release_lock.assert_awaited_once()
