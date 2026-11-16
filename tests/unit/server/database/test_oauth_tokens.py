"""
Tests for src/server/database/oauth_tokens.py

Verifies Redis cache behavior for has_any_oauth_token and invalidate_oauth_active_cache.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cache(enabled=True, get_return=None):
    """Create a mock RedisCacheClient with async get/set/delete."""
    cache = MagicMock()
    cache.enabled = enabled
    cache.client = AsyncMock()
    cache.client.get = AsyncMock(return_value=get_return)
    cache.client.set = AsyncMock()
    cache.client.delete = AsyncMock()
    return cache


def _make_db_fixtures(fetchone_return=None):
    """Create mock cursor, connection, and get_db_connection patch target."""
    cursor = AsyncMock()
    cursor.execute = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=fetchone_return)

    conn = AsyncMock()

    @asynccontextmanager
    async def _cursor_cm(**kwargs):
        yield cursor

    conn.cursor = _cursor_cm

    @asynccontextmanager
    async def _fake_db():
        yield conn

    return cursor, conn, _fake_db


# ===========================================================================
# Tests — has_any_oauth_token cache
# ===========================================================================


@pytest.mark.asyncio
async def test_has_any_oauth_token_cache_hit():
    """Cache returns b"1" -> True without any DB call."""
    cache = _make_cache(get_return=b"1")
    cursor, conn, fake_db = _make_db_fixtures()

    with patch(
        "src.utils.cache.redis_cache.get_cache_client", return_value=cache
    ), patch(
        "src.server.database.oauth_tokens.get_db_connection", new=fake_db
    ):
        from src.server.database.oauth_tokens import has_any_oauth_token

        result = await has_any_oauth_token("user-1")

    assert result is True
    cache.client.get.assert_awaited_once_with("oauth_active:user-1")
    # DB should NOT have been called
    cursor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_has_any_oauth_token_cache_miss():
    """Cache miss -> DB queried, cache.set called with b"1", returns True."""
    cache = _make_cache(get_return=None)
    # DB returns a row (token exists)
    cursor, conn, fake_db = _make_db_fixtures(fetchone_return={"?column?": 1})

    with patch(
        "src.utils.cache.redis_cache.get_cache_client", return_value=cache
    ), patch(
        "src.server.database.oauth_tokens.get_db_connection", new=fake_db
    ):
        from src.server.database.oauth_tokens import has_any_oauth_token

        result = await has_any_oauth_token("user-1")

    assert result is True
    # DB was queried
    cursor.execute.assert_awaited_once()
    # Cache was populated
    cache.client.set.assert_awaited_once_with(
        "oauth_active:user-1", b"1", ex=86400
    )


@pytest.mark.asyncio
async def test_invalidate_oauth_active_cache():
    """invalidate_oauth_active_cache deletes the correct cache key."""
    cache = _make_cache()

    with patch(
        "src.utils.cache.redis_cache.get_cache_client", return_value=cache
    ):
        from src.server.database.oauth_tokens import invalidate_oauth_active_cache

        await invalidate_oauth_active_cache("user1")

    cache.client.delete.assert_awaited_once_with("oauth_active:user1")


@pytest.mark.asyncio
async def test_get_oauth_tokens_clamps_a_legacy_far_future_expiry():
    """A 9999-12-31 23:59:59+00 sentinel decodes as year 10000 east of UTC.

    psycopg raises DataError while loading the row in the deployment's
    Asia/Shanghai session timezone, before Python can see it. The read clamps
    the instant in SQL instead; CASE keeps a real NULL as NULL.
    """
    row = {
        "access_token": "a",
        "refresh_token": "r",
        "account_id": "",
        "email": None,
        "plan_type": None,
        "expires_at": datetime(9999, 12, 31, tzinfo=timezone.utc),
    }
    cursor, _conn, fake_db = _make_db_fixtures(fetchone_return=row)

    with patch(
        "src.server.database.oauth_tokens.get_db_connection", new=fake_db
    ), patch(
        "src.server.database.oauth_tokens.get_encryption_key",
        return_value="encryption-key",
    ):
        from src.server.database.oauth_tokens import get_oauth_tokens

        result = await get_oauth_tokens("user-1", "deepseek-account")

    sql = cursor.execute.await_args.args[0]
    # Guard, clamped value and fallback in one shape: a replacement that keeps
    # the guard but drops the THEN or the ELSE would otherwise pass.
    assert "CASE" in sql
    assert "WHEN expires_at > '9999-12-31 00:00:00+00'::timestamptz" in sql
    assert "THEN '9999-12-31 00:00:00+00'::timestamptz" in sql
    assert "ELSE expires_at" in sql
    assert result is row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expires_at",
    [
        None,  # the separate "no expiry" spelling must stay NULL
        datetime(2026, 11, 4, 19, 0, tzinfo=timezone.utc),  # ordinary expiry
    ],
)
async def test_get_oauth_tokens_returns_null_and_ordinary_expiries_unchanged(
    expires_at,
):
    row = {
        "access_token": "a",
        "refresh_token": "r",
        "account_id": "",
        "email": None,
        "plan_type": None,
        "expires_at": expires_at,
    }
    _cursor, _conn, fake_db = _make_db_fixtures(fetchone_return=row)

    with patch(
        "src.server.database.oauth_tokens.get_db_connection", new=fake_db
    ), patch(
        "src.server.database.oauth_tokens.get_encryption_key",
        return_value="encryption-key",
    ):
        from src.server.database.oauth_tokens import get_oauth_tokens

        result = await get_oauth_tokens("user-1", "codex-oauth")

    assert result["expires_at"] is expires_at
