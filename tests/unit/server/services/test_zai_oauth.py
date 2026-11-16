"""Zhipu subscription login: zcode envelope, CLI flow, BigModel redirect, key minting.

The two regional logins converge on one durable credential — a minted
``zcode-api-key`` stored as an OAuth row — so the tests pin the envelope
unwrap, the flow parsing, the open-redirect guard, and the exact business-call
sequence that produces (or reuses) that key.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.server.services import zai_oauth
from src.utils.cache import redis_cache


class FakeResponse:
    def __init__(self, status_code: int = 200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class RecordingClient:
    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def request(self, method, url, headers=None, json=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers or {}, "json": json}
        )
        return self._responses.pop(0)


def install_client(monkeypatch, responses: list[FakeResponse]) -> RecordingClient:
    client = RecordingClient(responses)
    monkeypatch.setattr(zai_oauth.httpx, "AsyncClient", lambda *args, **kwargs: client)
    return client


def envelope(data) -> FakeResponse:
    return FakeResponse(200, {"code": 0, "msg": "ok", "data": data})


def customer_info() -> dict:
    return {
        "organizations": [
            {
                "organizationId": "org-default",
                "organizationName": "默认机构",
                "projects": [
                    {"projectId": "proj-default", "projectName": "默认项目"},
                ],
            },
            {
                "organizationId": "org-other",
                "organizationName": "Other Org",
                "projects": [{"projectId": "proj-other", "projectName": "Other"}],
            },
        ]
    }


# ─── Envelope ───────────────────────────────────────────────────────────────


def test_unwrap_returns_the_business_data():
    assert zai_oauth.unwrap_envelope({"code": 0, "msg": "ok", "data": {"a": 1}}) == {"a": 1}
    assert zai_oauth.unwrap_envelope({"code": 200, "data": [1, 2]}) == [1, 2]
    # A response without an envelope is passed through unchanged.
    assert zai_oauth.unwrap_envelope({"access_token": "x"}) == {"access_token": "x"}


def test_unwrap_raises_a_sanitized_business_error():
    secret = "s" * 64

    with pytest.raises(zai_oauth.ZhipuOAuthError) as excinfo:
        zai_oauth.unwrap_envelope({"code": 1001, "msg": f"token {secret} rejected"})

    message = str(excinfo.value)
    assert "1001" in message
    assert "token" in message
    assert secret not in message


def test_sanitize_message_redacts_token_shaped_text():
    sanitized = zai_oauth.sanitize_message("  bad   key " + "x" * 64)

    assert sanitized == "bad key [redacted]"


# ─── Z.ai CLI flow ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cli_init_parses_the_flow_and_sends_the_generated_poll_token(monkeypatch):
    monkeypatch.setattr(zai_oauth.secrets, "token_hex", lambda _n: "generated-poll-token")
    client = install_client(
        monkeypatch,
        [
            envelope(
                {
                    "flow_id": "flow-1",
                    "poll_token": "server-poll-token",
                    "authorize_url": "https://zcode.z.ai/authorize/flow-1",
                    "expires_at": 1_800_000_000,
                    "poll_interval_sec": 3,
                }
            )
        ],
    )

    flow = await zai_oauth.initiate_cli_flow()

    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == zai_oauth.ZCODE_CLI_INIT_URL
    assert call["headers"]["Authorization"] == "Bearer generated-poll-token"
    assert call["json"] == {"provider": "zai"}
    assert flow == {
        "flow_id": "flow-1",
        "poll_token": "server-poll-token",
        "authorize_url": "https://zcode.z.ai/authorize/flow-1",
        "expires_at": 1_800_000_000,
        "interval": 3,
    }


@pytest.mark.asyncio
async def test_cli_init_falls_back_to_the_generated_poll_token(monkeypatch):
    monkeypatch.setattr(zai_oauth.secrets, "token_hex", lambda _n: "generated-poll-token")
    install_client(
        monkeypatch,
        [
            envelope(
                {
                    "flow_id": "flow-1",
                    "authorize_url": "https://zcode.z.ai/authorize/flow-1",
                    "expires_at": 1_800_000_000,
                }
            )
        ],
    )

    flow = await zai_oauth.initiate_cli_flow()

    assert flow["poll_token"] == "generated-poll-token"
    assert flow["interval"] == 2


@pytest.mark.asyncio
async def test_cli_init_rejects_a_flow_without_a_login_url(monkeypatch):
    install_client(monkeypatch, [envelope({"flow_id": "flow-1"})])

    with pytest.raises(zai_oauth.ZhipuOAuthError):
        await zai_oauth.initiate_cli_flow()


@pytest.mark.asyncio
async def test_cli_poll_parses_pending_and_ready(monkeypatch):
    pending_client = install_client(monkeypatch, [envelope({"status": "pending"})])

    assert await zai_oauth.poll_cli_flow("flow-1", "poll-token") == {"status": "pending"}
    pending_call = pending_client.calls[0]
    assert pending_call["method"] == "GET"
    assert pending_call["url"] == f"{zai_oauth.ZCODE_CLI_POLL_URL}/flow-1"
    assert pending_call["headers"]["Authorization"] == "Bearer poll-token"

    install_client(
        monkeypatch,
        [
            envelope(
                {
                    "status": "ready",
                    "token": "minted-token",
                    "user": {"user_id": "u-1", "email": "person@example.test"},
                    "zai": {"access_token": "zai-access"},
                }
            )
        ],
    )

    ready = await zai_oauth.poll_cli_flow("flow-1", "poll-token")

    assert ready["status"] == "ready"
    assert ready["zai"]["access_token"] == "zai-access"


# ─── BigModel redirect / callback exchange ─────────────────────────────────


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://localhost:1455/oauth/zhipu/callback",
        "http://127.0.0.1:1455/cb",
        "https://localhost:8443/cb",
    ],
)
def test_loopback_redirect_uris_are_accepted(redirect_uri):
    assert zai_oauth.validate_loopback_redirect_uri(redirect_uri) == redirect_uri


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://evil.example/callback",
        "http://localhost.evil.example/callback",
        "http://127.0.0.2/callback",
        "ftp://localhost/callback",
        "",
        "not a url",
    ],
)
def test_non_loopback_redirect_uris_are_rejected(redirect_uri):
    with pytest.raises(ValueError):
        zai_oauth.validate_loopback_redirect_uri(redirect_uri)


def test_bigmodel_authorize_url_carries_the_redirect_and_state():
    from urllib.parse import parse_qs, urlparse

    url = zai_oauth.build_bigmodel_authorize_url("http://localhost:1455/cb", "state-1")
    query = parse_qs(urlparse(url).query)

    assert url.startswith("https://bigmodel.cn/login?")
    assert query["redirect"] == ["http://localhost:1455/cb"]
    assert query["appId"] == ["zcode"]
    assert query["state"] == ["state-1"]


@pytest.mark.asyncio
async def test_bigmodel_exchange_retries_transient_5xx(monkeypatch):
    client = install_client(
        monkeypatch,
        [
            FakeResponse(500),
            FakeResponse(502),
            envelope(
                {
                    "bigmodel": {"access_token": "bigmodel-access"},
                    "user": {"user_id": "u-1", "email": "person@example.test"},
                }
            ),
        ],
    )

    data = await zai_oauth.exchange_bigmodel_code(
        "code-1", "state-1", "http://localhost:1455/cb"
    )

    assert data["bigmodel"]["access_token"] == "bigmodel-access"
    assert len(client.calls) == 3
    assert client.calls[0]["json"] == {
        "provider": "bigmodel",
        "code": "code-1",
        "redirect_uri": "http://localhost:1455/cb",
        "state": "state-1",
    }


@pytest.mark.asyncio
async def test_bigmodel_exchange_gives_up_after_three_transient_failures(monkeypatch):
    client = install_client(monkeypatch, [FakeResponse(500) for _ in range(3)])

    with pytest.raises(zai_oauth.ZhipuOAuthError) as excinfo:
        await zai_oauth.exchange_bigmodel_code("code-1", "state-1", "http://localhost:1455/cb")

    assert "HTTP 500" in str(excinfo.value)
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_bigmodel_exchange_does_not_retry_a_client_error(monkeypatch):
    client = install_client(monkeypatch, [FakeResponse(400)])

    with pytest.raises(zai_oauth.ZhipuOAuthError):
        await zai_oauth.exchange_bigmodel_code("code-1", "state-1", "http://localhost:1455/cb")

    assert len(client.calls) == 1


# ─── Coding-plan key minting ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mint_reuses_the_named_key_and_copies_its_secret(monkeypatch):
    client = install_client(
        monkeypatch,
        [
            envelope(customer_info()),
            envelope(
                [
                    {"name": "other-key", "apiKey": "other-key-id"},
                    {"name": "zcode-api-key", "apiKey": "existing-key"},
                ]
            ),
            envelope({"secretKey": "secret-1"}),
        ],
    )

    key = await zai_oauth.mint_coding_plan_key(zai_oauth.BIGMODEL_FLOW, "bigmodel-access")

    assert key == "existing-key.secret-1"
    assert client.calls[0]["url"] == f"{zai_oauth.BIGMODEL_BUSINESS_BASE}/api/biz/customer/getCustomerInfo"
    # BigModel's business API takes the OAuth token raw, without a scheme.
    assert client.calls[0]["headers"]["Authorization"] == "bigmodel-access"
    keys_url = (
        f"{zai_oauth.BIGMODEL_BUSINESS_BASE}/api/biz/v1/organization/org-default"
        "/projects/proj-default/api_keys"
    )
    assert client.calls[1]["url"] == keys_url
    assert client.calls[2]["url"] == f"{keys_url}/copy/existing-key"


@pytest.mark.asyncio
async def test_mint_creates_the_named_key_when_it_does_not_exist(monkeypatch):
    client = install_client(
        monkeypatch,
        [
            envelope(customer_info()),
            envelope([]),
            envelope({"name": "zcode-api-key", "apiKey": "new-key"}),
            envelope({"secretKey": "secret-2"}),
        ],
    )

    key = await zai_oauth.mint_coding_plan_key(zai_oauth.BIGMODEL_FLOW, "bigmodel-access")

    assert key == "new-key.secret-2"
    create_call = client.calls[2]
    assert create_call["method"] == "POST"
    assert create_call["json"] == {"name": "zcode-api-key"}


@pytest.mark.asyncio
async def test_mint_falls_back_to_the_bare_key_without_a_secret(monkeypatch):
    install_client(
        monkeypatch,
        [
            envelope(customer_info()),
            envelope([{"name": "zcode-api-key", "apiKey": "bare-key"}]),
            envelope({}),
        ],
    )

    key = await zai_oauth.mint_coding_plan_key(zai_oauth.BIGMODEL_FLOW, "bigmodel-access")

    assert key == "bare-key"


@pytest.mark.asyncio
async def test_zai_mint_logs_into_the_business_api_with_the_oauth_token(monkeypatch):
    client = install_client(
        monkeypatch,
        [
            envelope({"access_token": "business-token"}),
            envelope(customer_info()),
            envelope([{"name": "zcode-api-key", "apiKey": "zai-key"}]),
            envelope({"secretKey": "zai-secret"}),
        ],
    )

    key = await zai_oauth.mint_coding_plan_key(zai_oauth.ZAI_FLOW, "zai-oauth-access")

    assert key == "zai-key.zai-secret"
    login = client.calls[0]
    assert login["url"] == zai_oauth.ZAI_BIZ_LOGIN_URL
    assert login["json"] == {"token": "zai-oauth-access"}
    assert client.calls[1]["url"] == f"{zai_oauth.ZAI_BUSINESS_BASE}/api/biz/customer/getCustomerInfo"
    assert client.calls[1]["headers"]["Authorization"] == "Bearer business-token"


@pytest.mark.asyncio
async def test_mint_prefers_the_default_organization_and_project(monkeypatch):
    client = install_client(
        monkeypatch,
        [
            envelope(customer_info()),
            envelope([{"name": "zcode-api-key", "apiKey": "key-1"}]),
            envelope({"secretKey": "s"}),
        ],
    )

    await zai_oauth.mint_coding_plan_key(zai_oauth.BIGMODEL_FLOW, "bigmodel-access")

    assert "/organization/org-default/projects/proj-default/" in client.calls[1]["url"]


# ─── Credential storage / resolution ───────────────────────────────────────


@pytest.fixture
def stored(monkeypatch):
    writes: list[dict] = []
    monkeypatch.setattr(
        zai_oauth,
        "upsert_oauth_tokens",
        AsyncMock(side_effect=lambda **kwargs: writes.append(kwargs)),
    )
    invalidate = AsyncMock()
    monkeypatch.setattr(zai_oauth, "invalidate_oauth_active_cache", invalidate)
    return writes, invalidate


@pytest.mark.asyncio
async def test_complete_bigmodel_login_stores_the_cn_provider_row(monkeypatch, stored):
    writes, invalidate = stored
    monkeypatch.setattr(
        zai_oauth, "mint_coding_plan_key", AsyncMock(return_value="api-key.secret")
    )
    before = datetime.now(timezone.utc)

    result = await zai_oauth.complete_bigmodel_login(
        "user-1",
        {
            "bigmodel": {"access_token": "bigmodel-access"},
            "user": {"user_id": "u-1", "email": "person@example.test"},
        },
    )

    assert result == {
        "provider": "bigmodel",
        "account_id": "u-1",
        "email": "person@example.test",
    }
    row = writes[0]
    assert row["provider"] == zai_oauth.BIGMODEL_PROVIDER
    assert row["access_token"] == "api-key.secret"
    assert row["refresh_token"] == ""
    assert row["account_id"] == "u-1"
    assert row["plan_type"] == zai_oauth.PLAN_TYPE
    assert row["expires_at"] >= before + timedelta(days=364)
    invalidate.assert_awaited_once_with("user-1")


@pytest.mark.asyncio
async def test_complete_zai_login_stores_the_intl_provider_row(monkeypatch, stored):
    writes, _invalidate = stored
    monkeypatch.setattr(
        zai_oauth, "mint_coding_plan_key", AsyncMock(return_value="intl-key.secret")
    )

    result = await zai_oauth.complete_zai_login(
        "user-1",
        {
            "status": "ready",
            "zai": {"access_token": "zai-access"},
            "user": {"user_id": "u-2", "email": "intl@example.test"},
        },
    )

    assert result["provider"] == "zai"
    assert writes[0]["provider"] == zai_oauth.ZAI_PROVIDER
    assert writes[0]["access_token"] == "intl-key.secret"


@pytest.mark.asyncio
async def test_complete_login_rejects_a_flow_without_an_access_token(monkeypatch, stored):
    with pytest.raises(zai_oauth.ZhipuOAuthError):
        await zai_oauth.complete_bigmodel_login("user-1", {"bigmodel": {}})


@pytest.mark.asyncio
async def test_get_valid_token_returns_the_unexpired_row(monkeypatch):
    row = {
        "access_token": "api-key.secret",
        "account_id": "u-1",
        "plan_type": "coding_plan",
        "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
    }
    read = AsyncMock(return_value=row)
    monkeypatch.setattr(zai_oauth, "get_oauth_tokens", read)

    result = await zai_oauth.get_valid_token("user-1", zai_oauth.ZAI_PROVIDER)

    assert result == {
        "access_token": "api-key.secret",
        "account_id": "u-1",
        "plan_type": "coding_plan",
    }
    read.assert_awaited_once_with("user-1", zai_oauth.ZAI_PROVIDER)


@pytest.mark.asyncio
async def test_get_valid_token_returns_none_when_missing_or_expired(monkeypatch):
    read = AsyncMock(return_value=None)
    monkeypatch.setattr(zai_oauth, "get_oauth_tokens", read)

    assert await zai_oauth.get_valid_token("user-1", zai_oauth.BIGMODEL_PROVIDER) is None

    read.return_value = {
        "access_token": "api-key.secret",
        "account_id": "u-1",
        "plan_type": "coding_plan",
        "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
    }
    assert await zai_oauth.get_valid_token("user-1", zai_oauth.BIGMODEL_PROVIDER) is None

    read.return_value = None
    assert await zai_oauth.get_valid_token("user-1", "made-up-oauth") is None
    assert read.await_count == 2


@pytest.mark.asyncio
async def test_get_valid_token_accepts_a_naive_db_expiry(monkeypatch):
    monkeypatch.setattr(
        zai_oauth,
        "get_oauth_tokens",
        AsyncMock(
            return_value={
                "access_token": "api-key.secret",
                "account_id": "u-1",
                "plan_type": "coding_plan",
                "expires_at": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
            }
        ),
    )

    result = await zai_oauth.get_valid_token("user-1", zai_oauth.ZAI_PROVIDER)

    assert result["access_token"] == "api-key.secret"


# ─── Status / disconnect ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_prefers_the_cn_row(monkeypatch):
    async def status(_user_id, provider):
        return {
            "connected": provider == zai_oauth.BIGMODEL_PROVIDER,
            "account_id": "u-1",
            "email": "person@example.test",
            "plan_type": "coding_plan",
        }

    read = AsyncMock(side_effect=status)
    monkeypatch.setattr(zai_oauth, "get_oauth_status", read)

    assert await zai_oauth.get_status("user-1") == {
        "connected": True,
        "provider": "bigmodel",
        "email": "person@example.test",
    }
    assert read.await_args_list[0].args[1] == zai_oauth.BIGMODEL_PROVIDER


@pytest.mark.asyncio
async def test_status_falls_back_to_the_intl_row(monkeypatch):
    async def status(_user_id, provider):
        return {
            "connected": provider == zai_oauth.ZAI_PROVIDER,
            "account_id": "u-1",
            "email": "intl@example.test",
            "plan_type": "coding_plan",
        }

    monkeypatch.setattr(zai_oauth, "get_oauth_status", AsyncMock(side_effect=status))

    assert await zai_oauth.get_status("user-1") == {
        "connected": True,
        "provider": "zai",
        "email": "intl@example.test",
    }


@pytest.mark.asyncio
async def test_status_reports_disconnected(monkeypatch):
    monkeypatch.setattr(
        zai_oauth,
        "get_oauth_status",
        AsyncMock(return_value={"connected": False, "account_id": None, "email": None}),
    )

    assert await zai_oauth.get_status("user-1") == {
        "connected": False,
        "provider": None,
        "email": None,
    }


@pytest.mark.asyncio
async def test_disconnect_deletes_both_regional_rows(monkeypatch):
    deleted: list[str] = []
    monkeypatch.setattr(
        zai_oauth, "delete_oauth_tokens", AsyncMock(side_effect=lambda _uid, provider: deleted.append(provider))
    )
    invalidate = AsyncMock()
    monkeypatch.setattr(zai_oauth, "invalidate_oauth_active_cache", invalidate)

    await zai_oauth.disconnect("user-1")

    assert deleted == [zai_oauth.BIGMODEL_PROVIDER, zai_oauth.ZAI_PROVIDER]
    invalidate.assert_awaited_once_with("user-1")


# ─── Manifest / client dispatch ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model_name", "provider"),
    [
        ("glm-5.3-oauth", zai_oauth.ZAI_PROVIDER),
        ("glm-5.3-cn-oauth", zai_oauth.BIGMODEL_PROVIDER),
    ],
)
def test_manifest_registers_the_zhipu_subscription_models(model_name, provider):
    from src.llms.llm import ModelConfig

    metadata = ModelConfig().get_model_metadata()[model_name]

    assert metadata["provider"] == provider
    assert metadata["access_type"] == "oauth"


@pytest.mark.asyncio
async def test_resolver_dispatches_zhipu_with_the_model_provider(monkeypatch):
    from src.llms import llm as llm_module
    from src.server.services.llm import clients

    class FakeManifest:
        def get_model_config(self, model_name):
            return {"provider": zai_oauth.BIGMODEL_PROVIDER}

        def get_provider_info(self, provider):
            return {"access_type": "oauth"}

    monkeypatch.setattr(
        llm_module.LLM, "get_model_config", classmethod(lambda cls: FakeManifest())
    )
    built: dict = {}

    def fake_create_llm(model, **kwargs):
        built.update(kwargs)
        return "client"

    monkeypatch.setattr(llm_module, "create_llm", fake_create_llm)
    read = AsyncMock(
        return_value={"access_token": "minted-key", "account_id": "u-1", "plan_type": "coding_plan"}
    )
    monkeypatch.setattr(zai_oauth, "get_valid_token", read)

    client = await clients.resolve_oauth_llm_client("user-1", "glm-5.3-cn-oauth")

    assert client == "client"
    read.assert_awaited_once_with("user-1", zai_oauth.BIGMODEL_PROVIDER)
    assert built["api_key"] == "minted-key"
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
async def test_initiate_route_rejects_a_non_loopback_redirect(oauth_routes):
    from fastapi import HTTPException

    routes, _cache = oauth_routes
    body = routes.ZhipuInitiateRequest(
        provider="bigmodel", redirect_uri="https://evil.example/callback"
    )

    with pytest.raises(HTTPException) as excinfo:
        await routes.zhipu_initiate("user-1", body)

    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_initiate_route_stores_the_bigmodel_flow(oauth_routes):
    routes, cache = oauth_routes
    body = routes.ZhipuInitiateRequest(
        provider="bigmodel", redirect_uri="http://localhost:1455/oauth/zhipu/callback"
    )

    result = await routes.zhipu_initiate("user-1", body)

    assert result["authorize_url"].startswith("https://bigmodel.cn/login?")
    assert result["expires_in"] == 900
    cached = json.loads(cache.client.values["oauth:zhipu:user-1"])
    assert cached == {
        "provider": "bigmodel",
        "redirect_uri": "http://localhost:1455/oauth/zhipu/callback",
        "state": result["state"],
    }


@pytest.mark.asyncio
async def test_initiate_route_caches_the_zai_flow_without_exposing_its_token(oauth_routes, monkeypatch):
    routes, cache = oauth_routes
    monkeypatch.setattr(
        routes.zai_oauth,
        "initiate_cli_flow",
        AsyncMock(
            return_value={
                "flow_id": "flow-1",
                "poll_token": "server-side-token",
                "authorize_url": "https://zcode.z.ai/authorize/flow-1",
                "expires_at": None,
                "interval": 2,
            }
        ),
    )

    result = await routes.zhipu_initiate(
        "user-1", routes.ZhipuInitiateRequest(provider="zai")
    )

    assert result == {
        "authorize_url": "https://zcode.z.ai/authorize/flow-1",
        "state": result["state"],
        "interval": 2,
        "expires_in": 900,
    }
    assert "server-side-token" not in json.dumps(result)
    cached = json.loads(cache.client.values["oauth:zhipu:user-1"])
    assert cached["poll_token"] == "server-side-token"


@pytest.mark.asyncio
async def test_initiate_route_rejects_an_unknown_provider(oauth_routes):
    from fastapi import HTTPException

    routes, _cache = oauth_routes

    with pytest.raises(HTTPException) as excinfo:
        await routes.zhipu_initiate(
            "user-1", routes.ZhipuInitiateRequest(provider="not-a-provider")
        )

    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_callback_route_mints_the_key_and_publishes_the_result_to_poll(oauth_routes, monkeypatch):
    routes, cache = oauth_routes
    cache.client.values["oauth:zhipu:user-1"] = json.dumps(
        {
            "provider": "bigmodel",
            "state": "state-1",
            "redirect_uri": "http://localhost:1455/oauth/zhipu/callback",
        }
    )
    exchange = AsyncMock(
        return_value={
            "bigmodel": {"access_token": "bigmodel-access"},
            "user": {"user_id": "u-1", "email": "person@example.test"},
        }
    )
    monkeypatch.setattr(routes.zai_oauth, "exchange_bigmodel_code", exchange)
    monkeypatch.setattr(
        routes.zai_oauth,
        "complete_bigmodel_login",
        AsyncMock(
            return_value={
                "provider": "bigmodel",
                "account_id": "u-1",
                "email": "person@example.test",
            }
        ),
    )

    result = await routes.zhipu_callback(
        "user-1",
        routes.ZhipuCallbackRequest(
            code="code-1",
            state="state-1",
            redirect_uri="http://localhost:1455/oauth/zhipu/callback",
        ),
    )

    assert result == {
        "success": True,
        "provider": "bigmodel",
        "email": "person@example.test",
    }
    exchange.assert_awaited_once_with(
        "code-1", "state-1", "http://localhost:1455/oauth/zhipu/callback"
    )
    assert await routes.zhipu_poll(
        "user-1", routes.ZhipuPollRequest(state="state-1")
    ) == {
        "success": True,
        "provider": "bigmodel",
        "email": "person@example.test",
    }


@pytest.mark.asyncio
async def test_poll_route_advances_the_zai_flow_and_stores_the_connection(oauth_routes, monkeypatch):
    routes, cache = oauth_routes
    cache.client.values["oauth:zhipu:user-1"] = json.dumps(
        {
            "provider": "zai",
            "flow_id": "flow-1",
            "poll_token": "server-side-token",
            "state": "state-1",
            "expires_at": None,
        }
    )
    ready = {
        "status": "ready",
        "zai": {"access_token": "zai-access"},
        "user": {"user_id": "u-1", "email": "person@example.test"},
    }
    monkeypatch.setattr(routes.zai_oauth, "poll_cli_flow", AsyncMock(return_value=ready))
    monkeypatch.setattr(
        routes.zai_oauth,
        "complete_zai_login",
        AsyncMock(
            return_value={
                "provider": "zai",
                "account_id": "u-1",
                "email": "person@example.test",
            }
        ),
    )

    result = await routes.zhipu_poll(
        "user-1", routes.ZhipuPollRequest(state="state-1")
    )

    assert result == {
        "success": True,
        "provider": "zai",
        "email": "person@example.test",
    }
    assert "oauth:zhipu:user-1" not in cache.client.values


@pytest.mark.asyncio
async def test_poll_route_reports_pending_for_a_bigmodel_callback(oauth_routes):
    routes, cache = oauth_routes
    cache.client.values["oauth:zhipu:user-1"] = json.dumps(
        {
            "provider": "bigmodel",
            "state": "state-1",
            "redirect_uri": "http://localhost:1455/cb",
        }
    )

    assert await routes.zhipu_poll(
        "user-1", routes.ZhipuPollRequest(state="state-1")
    ) == {"pending": True}


@pytest.mark.asyncio
async def test_poll_route_rejects_a_state_mismatch(oauth_routes):
    from fastapi import HTTPException

    routes, cache = oauth_routes
    cache.client.values["oauth:zhipu:user-1"] = json.dumps(
        {"provider": "bigmodel", "state": "expected", "redirect_uri": "http://localhost:1455/cb"}
    )

    with pytest.raises(HTTPException) as excinfo:
        await routes.zhipu_poll(
            "user-1", routes.ZhipuPollRequest(state="forged")
        )

    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_status_and_disconnect_routes_delegate(oauth_routes, monkeypatch):
    routes, _cache = oauth_routes
    monkeypatch.setattr(
        routes.zai_oauth,
        "get_status",
        AsyncMock(return_value={"connected": True, "provider": "bigmodel", "email": "e@x"}),
    )
    disconnect = AsyncMock()
    monkeypatch.setattr(routes.zai_oauth, "disconnect", disconnect)

    assert await routes.zhipu_status("user-1") == {
        "connected": True,
        "provider": "bigmodel",
        "email": "e@x",
    }
    assert await routes.zhipu_disconnect("user-1") == {"success": True}
    disconnect.assert_awaited_once_with("user-1")
