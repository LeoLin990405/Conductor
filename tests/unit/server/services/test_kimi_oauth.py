"""Kimi Code subscription login: CLI headers, device poll, refresh-lock race.

The auth server identifies the caller by its ``X-Msh-*`` headers, so the device
id's stability is a protocol requirement rather than an implementation detail —
these tests pin it alongside the standard device-grant states and the shared
refresh path.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.server.services import kimi_oauth


@pytest.fixture(autouse=True)
def _oauth_client_settings(monkeypatch):
    # The public build ships no OAuth client settings; tests supply test values.
    monkeypatch.setenv("CONDUCTOR_KIMI_OAUTH_CLIENT_ID", "test-conductor-kimi-oauth-client-id")
from src.utils.cache import redis_cache


def _jwt(payload: dict) -> str:
    def segment(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{segment({'alg': 'none'})}.{segment(payload)}.signature"


class FakeResponse:
    def __init__(self, status_code: int = 200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class RecordingClient:
    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self._responses.pop(0)


def install_client(monkeypatch, responses: list[FakeResponse]) -> RecordingClient:
    client = RecordingClient(responses)
    monkeypatch.setattr(kimi_oauth.httpx, "AsyncClient", lambda *args, **kwargs: client)
    return client


def use_device_id(monkeypatch, device_id: str = "device-id-1") -> None:
    monkeypatch.setattr(kimi_oauth, "_device_id", AsyncMock(return_value=device_id))


# ─── Device authorization request ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_device_request_identifies_the_cli_and_returns_prompt(monkeypatch):
    use_device_id(monkeypatch)
    client = install_client(
        monkeypatch,
        [
            FakeResponse(
                200,
                {
                    "device_code": "device-code",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://auth.kimi.com/device",
                    "verification_uri_complete": "https://auth.kimi.com/device?code=ABCD-1234",
                    "interval": "5",
                    "expires_in": "1800",
                },
            )
        ],
    )

    result = await kimi_oauth.request_device_code()

    call = client.calls[0]
    assert call["url"] == kimi_oauth.KIMI_DEVICE_URL
    assert call["data"] == {"client_id": kimi_oauth.KIMI_CLIENT_ID}
    headers = {name.lower(): value for name, value in call["headers"].items()}
    assert headers["x-msh-platform"] == "kimi_cli"
    assert headers["x-msh-version"] == kimi_oauth.KIMI_CLI_VERSION
    assert headers["x-msh-device-id"] == "device-id-1"
    assert headers["x-msh-device-name"]
    assert headers["x-msh-device-model"]
    assert headers["x-msh-os-version"]
    assert headers["content-type"] == "application/x-www-form-urlencoded"
    assert result == {
        "device_code": "device-code",
        "user_code": "ABCD-1234",
        "verification_url": "https://auth.kimi.com/device?code=ABCD-1234",
        "interval": 5,
        "expires_in": 1800,
    }


@pytest.mark.asyncio
async def test_device_id_is_persisted_once_and_reused_across_workers(monkeypatch):
    stored: dict[str, bytes] = {}
    writes: list[str] = []

    class FakeRedis:
        async def get(self, key):
            return stored.get(key)

        async def set(self, key, value, *, nx=False, ex=None):
            writes.append(key)
            if nx and key in stored:
                return None
            stored[key] = value.encode() if isinstance(value, str) else value
            return True

    cache = SimpleNamespace(enabled=True, client=FakeRedis())
    monkeypatch.setattr(redis_cache, "get_cache_client", lambda: cache)
    monkeypatch.setattr(kimi_oauth.uuid, "uuid4", lambda: "generated-id")

    first = await kimi_oauth._device_id()
    second = await kimi_oauth._device_id()

    assert first == second == "generated-id"
    assert writes == [kimi_oauth.KIMI_DEVICE_ID_KEY]


@pytest.mark.asyncio
async def test_device_id_falls_back_to_ephemeral_when_cache_is_down(monkeypatch):
    monkeypatch.setattr(
        redis_cache, "get_cache_client", lambda: SimpleNamespace(enabled=False, client=None)
    )

    ids = {await kimi_oauth._device_id() for _ in range(3)}

    assert len(ids) == 3


# ─── Device authorization polling ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "error"),
    [(400, "authorization_pending"), (428, "slow_down")],
)
async def test_poll_reports_pending_while_the_user_has_not_approved(monkeypatch, status_code, error):
    use_device_id(monkeypatch)
    install_client(monkeypatch, [FakeResponse(status_code, {"error": error})])

    assert await kimi_oauth.poll_device_authorization("device-code") is None


@pytest.mark.asyncio
async def test_poll_returns_token_bundle_on_approval(monkeypatch):
    use_device_id(monkeypatch)
    client = install_client(
        monkeypatch,
        [
            FakeResponse(
                200,
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "id_token": _jwt({"email": "person@example.test"}),
                    "expires_in": "1800",
                },
            )
        ],
    )

    result = await kimi_oauth.poll_device_authorization("device-code")

    call = client.calls[0]
    assert call["url"] == kimi_oauth.KIMI_TOKEN_URL
    assert call["data"] == {
        "grant_type": kimi_oauth.KIMI_DEVICE_GRANT,
        "device_code": "device-code",
        "client_id": kimi_oauth.KIMI_CLIENT_ID,
    }
    assert call["headers"]["X-Msh-Platform"] == "kimi_cli"
    assert result["access_token"] == "access-token"
    assert result["refresh_token"] == "refresh-token"
    assert result["expires_in"] == 1800


@pytest.mark.asyncio
async def test_poll_failure_never_echoes_the_upstream_body(monkeypatch):
    use_device_id(monkeypatch)
    install_client(
        monkeypatch,
        [
            FakeResponse(
                400,
                {
                    "error": "invalid_grant",
                    "error_description": "secret-detail-that-must-not-surface",
                },
            )
        ],
    )

    with pytest.raises(RuntimeError) as excinfo:
        await kimi_oauth.poll_device_authorization("device-code")

    message = str(excinfo.value)
    assert "400" in message
    assert "invalid_grant" in message
    assert "secret-detail-that-must-not-surface" not in message


# ─── Refresh ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_preserves_existing_refresh_token_when_provider_omits_rotation(monkeypatch):
    use_device_id(monkeypatch)
    install_client(
        monkeypatch,
        [FakeResponse(200, {"access_token": "new-access", "expires_in": 3600})],
    )

    result = await kimi_oauth.refresh_tokens("still-valid-refresh")

    assert result["access_token"] == "new-access"
    assert result["refresh_token"] == "still-valid-refresh"


@pytest.mark.asyncio
async def test_refresh_uses_rotated_refresh_token_when_provider_returns_one(monkeypatch):
    use_device_id(monkeypatch)
    install_client(
        monkeypatch,
        [
            FakeResponse(
                200,
                {
                    "access_token": "new-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 3600,
                },
            )
        ],
    )

    result = await kimi_oauth.refresh_tokens("old-refresh")

    assert result["refresh_token"] == "rotated-refresh"


# ─── Token storage / claims ─────────────────────────────────────────────────


def test_parse_id_token_email_reads_the_claim():
    assert kimi_oauth.parse_id_token_email(_jwt({"email": "person@example.test"})) == (
        "person@example.test"
    )


def test_parse_id_token_email_tolerates_missing_or_malformed_tokens():
    assert kimi_oauth.parse_id_token_email("") is None
    assert kimi_oauth.parse_id_token_email("not-a-jwt") is None
    assert kimi_oauth.parse_id_token_email(_jwt({"sub": "no-email"})) is None


@pytest.mark.asyncio
async def test_save_device_tokens_writes_the_kimi_row(monkeypatch):
    writes: list[dict] = []
    monkeypatch.setattr(
        kimi_oauth, "upsert_oauth_tokens", AsyncMock(side_effect=lambda **kwargs: writes.append(kwargs))
    )
    invalidate = AsyncMock()
    monkeypatch.setattr(kimi_oauth, "invalidate_oauth_active_cache", invalidate)
    before = datetime.now(timezone.utc)

    result = await kimi_oauth.save_device_tokens(
        "user-1",
        {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": _jwt({"email": "person@example.test"}),
            "expires_in": 1800,
        },
    )

    assert result == {"email": "person@example.test", "account_id": ""}
    row = writes[0]
    assert row["provider"] == kimi_oauth.KIMI_PROVIDER
    assert row["access_token"] == "access-token"
    assert row["refresh_token"] == "refresh-token"
    assert row["account_id"] == ""
    assert row["plan_type"] is None
    assert row["expires_at"] >= before + timedelta(seconds=1799)
    invalidate.assert_awaited_once_with("user-1")


# ─── get_valid_token ────────────────────────────────────────────────────────


@pytest.fixture
def account(monkeypatch):
    state = SimpleNamespace(
        row={
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "account_id": "",
            "email": None,
            "plan_type": None,
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
    state.refresh = AsyncMock(
        return_value={
            "access_token": "new-access",
            "refresh_token": "rotated-refresh",
            "id_token": _jwt({"email": "person@example.test"}),
            "expires_in": 3600,
        }
    )
    state.invalidate = AsyncMock()
    state.cache = SimpleNamespace(
        acquire_lock=AsyncMock(return_value=True),
        release_lock=AsyncMock(),
    )
    monkeypatch.setattr(kimi_oauth, "get_oauth_tokens", state.read)
    monkeypatch.setattr(kimi_oauth, "upsert_oauth_tokens", write)
    monkeypatch.setattr(kimi_oauth, "invalidate_oauth_active_cache", state.invalidate)
    monkeypatch.setattr(kimi_oauth, "refresh_tokens", state.refresh)
    monkeypatch.setattr(redis_cache, "get_cache_client", lambda: state.cache)
    return state


@pytest.mark.asyncio
async def test_get_valid_token_returns_none_when_not_connected(account):
    account.row = None

    assert await kimi_oauth.get_valid_token("user-1") is None
    account.cache.acquire_lock.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_token_is_used_without_locking_or_refreshing(account):
    account.row["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)

    result = await kimi_oauth.get_valid_token("user-1")

    assert result == {"access_token": "old-access", "account_id": "", "plan_type": None}
    account.cache.acquire_lock.assert_not_awaited()
    account.refresh.assert_not_awaited()
    assert account.writes == []


@pytest.mark.asyncio
async def test_expired_token_is_refreshed_and_persisted(account):
    before = datetime.now(timezone.utc)

    result = await kimi_oauth.get_valid_token("user-1")

    assert result["access_token"] == "new-access"
    assert account.writes[0]["provider"] == kimi_oauth.KIMI_PROVIDER
    assert account.row["access_token"] == "new-access"
    assert account.row["refresh_token"] == "rotated-refresh"
    assert account.row["email"] == "person@example.test"
    assert account.row["expires_at"] >= before + timedelta(seconds=3599)
    account.invalidate.assert_awaited_once_with("user-1")
    account.cache.release_lock.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_without_rotation_keeps_the_stored_refresh_token(account):
    account.refresh.return_value["refresh_token"] = ""

    await kimi_oauth.get_valid_token("user-1")

    assert account.row["refresh_token"] == "old-refresh"


@pytest.mark.asyncio
async def test_lock_owner_rereads_tokens_after_acquiring_the_lock(account):
    fresh = {
        **account.row,
        "access_token": "fresh-access",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    account.read.side_effect = [dict(account.row), fresh]

    result = await kimi_oauth.get_valid_token("user-1")

    assert result["access_token"] == "fresh-access"
    account.refresh.assert_not_awaited()
    assert account.writes == []
    account.cache.release_lock.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_holder_returns_none_on_failure_and_releases_the_lock(account):
    account.refresh.side_effect = RuntimeError("provider down")

    assert await kimi_oauth.get_valid_token("user-1") is None
    assert account.writes == []
    account.cache.release_lock.assert_awaited_once()


@pytest.mark.asyncio
async def test_waiter_uses_the_tokens_the_lock_holder_stored(account, monkeypatch):
    account.cache.acquire_lock.return_value = False

    async def winner(*_args):
        return {
            **account.row,
            "access_token": "winner-access",
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        }

    waiter = AsyncMock(side_effect=winner)
    monkeypatch.setattr(kimi_oauth, "await_refreshed_tokens", waiter)

    result = await kimi_oauth.get_valid_token("user-1")

    assert result["access_token"] == "winner-access"
    waiter.assert_awaited_once_with("user-1", kimi_oauth.KIMI_PROVIDER)
    account.refresh.assert_not_awaited()
    account.cache.release_lock.assert_not_awaited()


# ─── Manifest / client dispatch ─────────────────────────────────────────────


def test_manifest_registers_the_kimi_subscription_model():
    from src.llms.llm import ModelConfig

    metadata = ModelConfig().get_model_metadata()["kimi-k3-oauth"]

    assert metadata["provider"] == kimi_oauth.KIMI_PROVIDER
    assert metadata["access_type"] == "oauth"


@pytest.mark.asyncio
async def test_resolver_dispatches_kimi_and_sends_the_oauth_bearer(monkeypatch):
    from src.llms import llm as llm_module
    from src.server.services.llm import clients

    class FakeManifest:
        def get_model_config(self, model_name):
            return {"provider": kimi_oauth.KIMI_PROVIDER}

        def get_provider_info(self, provider):
            return {"access_type": "oauth"}

    monkeypatch.setattr(
        llm_module.LLM, "get_model_config", classmethod(lambda cls: FakeManifest())
    )
    built: dict = {}

    def fake_create_llm(model, **kwargs):
        built["model"] = model
        built.update(kwargs)
        return "client"

    monkeypatch.setattr(llm_module, "create_llm", fake_create_llm)
    monkeypatch.setattr(
        kimi_oauth,
        "get_valid_token",
        AsyncMock(return_value={"access_token": "oauth-access", "account_id": "", "plan_type": None}),
    )

    client = await clients.resolve_oauth_llm_client("user-1", "kimi-k3-oauth")

    assert client == "client"
    assert built["api_key"] == "oauth-access"
    # The bearer is carried by the anthropic-oauth client's auth_token mode, not
    # by a pinned default header (a duplicate Authorization header would ship).
    assert not built.get("default_headers")


# ─── Routes (thin orchestration over the service) ──────────────────────────


class FakeCacheClient:
    def __init__(self):
        self.values: dict = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **kwargs):
        self.values[key] = value
        return True

    async def delete(self, key):
        self.values.pop(key, None)


class FakeCache:
    enabled = True

    def __init__(self):
        self.client = FakeCacheClient()


@pytest.fixture
def oauth_routes(monkeypatch):
    from src.server.app import oauth as routes

    cache = FakeCache()
    monkeypatch.setattr(redis_cache, "get_cache_client", lambda: cache)
    return routes, cache


@pytest.mark.asyncio
async def test_initiate_route_caches_the_device_code_and_returns_the_prompt(oauth_routes, monkeypatch):
    routes, cache = oauth_routes
    device = {
        "device_code": "device-code",
        "user_code": "ABCD-1234",
        "verification_url": "https://auth.kimi.com/device?code=ABCD-1234",
        "interval": 5,
        "expires_in": 1800,
    }
    monkeypatch.setattr(routes.kimi_oauth, "request_device_code", AsyncMock(return_value=device))

    result = await routes.kimi_device_initiate("user-1")

    assert result == {
        "user_code": "ABCD-1234",
        "verification_url": "https://auth.kimi.com/device?code=ABCD-1234",
        "interval": 5,
        "expires_in": 1800,
    }
    cached = json.loads(cache.client.values["oauth:device:kimi:user-1"])
    assert cached == {"device_code": "device-code", "user_code": "ABCD-1234"}


@pytest.mark.asyncio
async def test_poll_route_reports_pending_then_stores_the_connection(oauth_routes, monkeypatch):
    routes, cache = oauth_routes
    cache.client.values["oauth:device:kimi:user-1"] = json.dumps({"device_code": "device-code"})
    monkeypatch.setattr(
        routes.kimi_oauth, "poll_device_authorization", AsyncMock(return_value=None)
    )

    assert await routes.kimi_device_poll("user-1") == {"pending": True}

    tokens = {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "id_token": "",
        "expires_in": 1800,
    }
    monkeypatch.setattr(
        routes.kimi_oauth, "poll_device_authorization", AsyncMock(return_value=tokens)
    )
    save = AsyncMock(return_value={"email": "person@example.test", "account_id": ""})
    monkeypatch.setattr(routes.kimi_oauth, "save_device_tokens", save)

    assert await routes.kimi_device_poll("user-1") == {
        "success": True,
        "email": "person@example.test",
        "account_id": "",
    }
    save.assert_awaited_once_with("user-1", tokens)
    assert "oauth:device:kimi:user-1" not in cache.client.values


@pytest.mark.asyncio
async def test_routes_fail_503_when_the_cache_is_unavailable(monkeypatch):
    from fastapi import HTTPException

    from src.server.app import oauth as routes

    monkeypatch.setattr(
        redis_cache, "get_cache_client", lambda: SimpleNamespace(enabled=False, client=None)
    )

    for call in (
        lambda: routes.kimi_device_initiate("user-1"),
        lambda: routes.kimi_device_poll("user-1"),
    ):
        with pytest.raises(HTTPException) as excinfo:
            await call()
        assert excinfo.value.status_code == 503
