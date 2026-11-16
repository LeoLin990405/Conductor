"""Gemini refresh credentials must survive setup failures and reach other workers."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.server.services import gemini_oauth as oauth
from src.utils.cache import redis_cache


@pytest.fixture(autouse=True)
def _oauth_client_settings(monkeypatch):
    # The public build ships no OAuth client settings; tests supply test values.
    monkeypatch.setenv("CONDUCTOR_GEMINI_OAUTH_CLIENT_ID", "test-conductor-gemini-oauth-client-id")
    monkeypatch.setenv("CONDUCTOR_GEMINI_OAUTH_CLIENT_SECRET", "test-conductor-gemini-oauth-client-secret")


@pytest.fixture
def tokens(monkeypatch):
    state = SimpleNamespace(
        row={
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "account_id": "existing-project",
            "email": "person@example.test",
            "plan_type": "standard-tier",
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        },
        writes=[],
    )

    async def read(*_args):
        return dict(state.row) if state.row is not None else None

    async def write(**row):
        state.writes.append(dict(row))
        state.row = dict(row)

    state.read = AsyncMock(side_effect=read)
    state.refresh = AsyncMock(return_value={
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "id_token": "",
        "expires_in": 3600,
    })
    state.setup = AsyncMock(return_value={"project": "new-project", "tier": "free-tier"})
    state.cache = SimpleNamespace(
        acquire_lock=AsyncMock(return_value=True), release_lock=AsyncMock(),
    )
    monkeypatch.setattr(oauth, "get_oauth_tokens", state.read)
    monkeypatch.setattr(oauth, "upsert_oauth_tokens", write)
    monkeypatch.setattr(oauth, "invalidate_oauth_active_cache", AsyncMock())
    monkeypatch.setattr(oauth, "refresh_tokens", state.refresh)
    monkeypatch.setattr(oauth, "setup_code_assist", state.setup)
    monkeypatch.setattr(redis_cache, "get_cache_client", lambda: state.cache)
    return state


@pytest.mark.asyncio
async def test_refresh_is_persisted_before_return_and_reused(tokens):
    before = datetime.now(timezone.utc)
    result = await oauth.get_valid_token("user-1")

    assert result["access_token"] == "new-access"
    assert result["project"] == "existing-project"
    assert tokens.row["access_token"] == "new-access"
    assert tokens.row["refresh_token"] == "new-refresh"
    assert tokens.row["expires_at"] >= before + timedelta(seconds=3599)
    assert tokens.row["email"] == "person@example.test"
    tokens.setup.assert_not_awaited()

    assert await oauth.get_valid_token("user-1") == result
    tokens.refresh.assert_awaited_once_with("old-refresh")
    tokens.cache.release_lock.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_without_rotation_keeps_existing_refresh_token(tokens):
    tokens.refresh.return_value["refresh_token"] = ""

    assert (await oauth.get_valid_token("user-1"))["access_token"] == "new-access"
    assert tokens.row["refresh_token"] == "old-refresh"


@pytest.mark.asyncio
async def test_missing_project_setup_preserves_new_expiry_and_credentials(tokens):
    tokens.row["account_id"] = ""

    result = await oauth.get_valid_token("user-1")

    assert result["project"] == "new-project"
    assert tokens.row["account_id"] == "new-project"
    assert tokens.row["plan_type"] == "free-tier"
    assert tokens.row["refresh_token"] == "new-refresh"
    assert tokens.row["expires_at"] > datetime.now(timezone.utc) + timedelta(minutes=55)


@pytest.mark.asyncio
async def test_setup_failure_does_not_discard_rotated_refresh_token(tokens):
    tokens.row["account_id"] = ""
    tokens.setup.side_effect = RuntimeError("temporary setup failure")

    assert await oauth.get_valid_token("user-1") is None
    assert tokens.row["access_token"] == "new-access"
    assert tokens.row["refresh_token"] == "new-refresh"
    assert tokens.row["expires_at"] > datetime.now(timezone.utc)

    tokens.setup.side_effect = None
    assert (await oauth.get_valid_token("user-1"))["project"] == "new-project"
    tokens.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_fresh_token_does_not_lock_or_write(tokens):
    tokens.row["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)

    assert (await oauth.get_valid_token("user-1"))["access_token"] == "old-access"
    tokens.cache.acquire_lock.assert_not_awaited()
    tokens.refresh.assert_not_awaited()
    assert tokens.writes == []


@pytest.mark.asyncio
async def test_rechecks_winners_token_after_acquiring_refresh_lock(tokens):
    async def acquired(*_args):
        tokens.row.update(
            access_token="other-worker-access", refresh_token="other-worker-refresh",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        return True

    tokens.cache.acquire_lock.side_effect = acquired

    result = await oauth.get_valid_token("user-1")

    assert result["access_token"] == "other-worker-access"
    tokens.refresh.assert_not_awaited()
    assert tokens.writes == []
    tokens.cache.release_lock.assert_awaited_once()


@pytest.mark.asyncio
async def test_waiter_reads_persisted_tokens_without_refreshing(tokens, monkeypatch):
    tokens.cache.acquire_lock.return_value = False
    winner = {
        **tokens.row, "access_token": "winner-access",
        "refresh_token": "winner-refresh",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    waiter = AsyncMock(return_value=winner)
    monkeypatch.setattr(oauth, "await_refreshed_tokens", waiter)

    result = await oauth.get_valid_token("user-1")

    assert result["access_token"] == "winner-access"
    waiter.assert_awaited_once_with("user-1", oauth.GEMINI_PROVIDER)
    tokens.refresh.assert_not_awaited()
    tokens.cache.release_lock.assert_not_awaited()


@pytest.mark.parametrize("value", [
    "https://codeassist.google.com/authcode?code=auth%2Bcode&state=nonce",
    "code=auth%2Bcode&state=nonce",
    "auth+code",
])
def test_callback_accepts_documented_formats(value):
    assert oauth.parse_callback_input(value) == "auth+code"


# --- Code Assist project/tier setup ------------------------------------


@pytest.mark.asyncio
async def test_unsupported_consumer_client_is_reported_as_ineligible(monkeypatch):
    async def fake_post(method, _token, _body):
        assert method == "loadCodeAssist"
        return {
            "ineligibleTiers": [
                {
                    "reasonCode": "UNSUPPORTED_CLIENT",
                    "reasonMessage": "This client is no longer supported.",
                }
            ],
            "allowedTiers": [{"id": "standard-tier", "isDefault": True}],
        }

    monkeypatch.setattr(oauth, "_post_ca", fake_post)
    with pytest.raises(oauth.GeminiCodeAssistIneligible) as excinfo:
        await oauth.setup_code_assist("token")
    assert excinfo.value.reason_code == "UNSUPPORTED_CLIENT"


@pytest.mark.asyncio
async def test_missing_project_uses_antigravity_default_project(monkeypatch):
    async def fake_post(method, _token, _body):
        if method == "loadCodeAssist":
            return {
                "currentTier": {"id": "standard-tier", "name": "Standard"},
                "allowedTiers": [{"id": "standard-tier", "isDefault": True}],
            }
        return {"done": True, "response": {}}

    monkeypatch.setattr(oauth, "_post_ca", fake_post)
    setup = await oauth.setup_code_assist("token")
    assert setup["project"] == oauth.ANTIGRAVITY_DEFAULT_PROJECT
    assert setup["tier"] == "standard-tier"


@pytest.mark.asyncio
async def test_configured_project_is_used_when_google_returns_none(monkeypatch):
    async def fake_post(method, _token, body):
        assert method == "loadCodeAssist"
        assert body["cloudaicompanionProject"] == "my-project"
        return {
            "currentTier": {"id": "standard-tier", "name": "Standard"},
            "allowedTiers": [{"id": "standard-tier", "isDefault": True}],
        }

    monkeypatch.setattr(oauth, "_post_ca", fake_post)
    setup = await oauth.setup_code_assist("token", project_id="my-project")
    assert setup == {
        "project": "my-project",
        "tier": "standard-tier",
        "tier_name": "Standard",
    }


def test_antigravity_oauth_client_and_scopes():
    assert oauth.GEMINI_CLIENT_ID_ENV == "CONDUCTOR_GEMINI_OAUTH_CLIENT_ID"
    assert oauth.GEMINI_CLIENT_SECRET_ENV == "CONDUCTOR_GEMINI_OAUTH_CLIENT_SECRET"
    assert oauth.GEMINI_REDIRECT_URI == "https://antigravity.google/oauth-callback"
    assert "https://www.googleapis.com/auth/cclog" in oauth.GEMINI_SCOPES
    assert "https://www.googleapis.com/auth/experimentsandconfigs" in oauth.GEMINI_SCOPES


def test_authorize_url_uses_antigravity_client_and_callback():
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(oauth.build_authorize_url("verifier", "challenge")).query)
    assert query["client_id"] == [oauth.GEMINI_CLIENT_ID]
    assert query["redirect_uri"] == ["https://antigravity.google/oauth-callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert "https://www.googleapis.com/auth/cclog" in query["scope"][0]
    assert "https://www.googleapis.com/auth/aicode" in query["scope"][0]
