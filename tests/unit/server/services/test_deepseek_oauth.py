"""DeepSeek account OAuth: PKCE auth_init, auth_exchange, token storage.

These tests pin the wire body shape and the invariants the adapter cares about
(state isolation, loopback-only redirect, local-delete-before-logout).
"""

import pytest

from src.server.services import deepseek_oauth as ds


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeClient:
    calls: list[tuple[str, dict, dict]] = []
    response: _FakeResponse = _FakeResponse()

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append((url, dict(json or {}), dict(headers or {})))
        return type(self).response


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.response = _FakeResponse(
        200, {"authorize_url": "https://platform.deepseek.com/dsh/authorize?x=1",
              "authorize_id": "a-1", "expires_in": 300}
    )
    monkeypatch.setattr(ds.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


@pytest.mark.asyncio
async def test_start_authorization_builds_the_documented_auth_init(fake_http):
    flow = await ds.start_authorization("http://localhost:5173/oauth/deepseek/callback")

    url, body, _ = fake_http.calls[0]
    assert url == ds.DEEPSEEK_AUTH_INIT_URL
    assert body["code_challenge_method"] == "S256"
    assert body["state"] == flow["state"]
    assert body["redirect_uri"] == "http://localhost:5173/oauth/deepseek/callback"
    assert body["login_source"] == "web"
    # The verifier is the PKCE secret held server-side; the challenge is its
    # SHA-256, never the verifier itself.
    assert len(body["code_challenge"]) == 43
    assert body["code_challenge"] != flow["verifier"]
    assert flow["authorize_url"].startswith("https://platform.deepseek.com/dsh/authorize")
    assert flow["expires_in"] == 300


@pytest.mark.asyncio
async def test_exchange_sends_the_account_identity_and_returns_the_token(fake_http, monkeypatch):
    async def fake_device_id():
        return "device-uuid"

    monkeypatch.setattr(ds, "device_id", fake_device_id)
    _FakeClient.response = _FakeResponse(200, {"token": "acct-token", "user": {"id": "u"}})

    token = await ds.exchange_code("code-1", "verifier-1", "http://127.0.0.1:5173/cb")

    url, body, _ = fake_http.calls[0]
    assert url == ds.DEEPSEEK_AUTH_EXCHANGE_URL
    assert body["code"] == "code-1"
    assert body["code_verifier"] == "verifier-1"
    assert body["device_id"] == "device-uuid"
    assert body["redirect_uri"] == "http://127.0.0.1:5173/cb"
    assert body["device_model"] and body["os_version"]
    assert token == "acct-token"


@pytest.mark.asyncio
async def test_exchange_rejects_a_missing_token(fake_http, monkeypatch):
    async def fake_device_id():
        return "device-uuid"

    monkeypatch.setattr(ds, "device_id", fake_device_id)
    _FakeClient.response = _FakeResponse(200, {"ok": True})

    with pytest.raises(ds.DeepSeekOAuthError):
        await ds.exchange_code("code-1", "verifier-1", "http://127.0.0.1:5173/cb")


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example/callback",
        "http://localhost:5173/cb?x=1",
        "http://user:pass@localhost:5173/cb",
        "file:///tmp/cb",
    ],
)
def test_redirect_uri_must_be_a_plain_loopback_callback(uri):
    with pytest.raises(ds.DeepSeekOAuthError):
        ds.validate_redirect_uri(uri)


def test_redirect_uri_accepts_loopback_http_and_https():
    assert ds.validate_redirect_uri("http://localhost:5173/oauth/cb")
    assert ds.validate_redirect_uri("https://127.0.0.1:8443/cb")


def test_flow_round_trips_only_complete_state():
    flow = {"state": "s", "verifier": "v", "redirect_uri": "http://localhost/cb"}
    assert ds.loads_flow(ds.dumps_flow(flow)) == flow
    assert ds.loads_flow(None) is None
    assert ds.loads_flow("not-json") is None
    assert ds.loads_flow('{"state": "only"}') is None


@pytest.mark.asyncio
async def test_store_token_persists_without_refresh_or_expiry(monkeypatch):
    recorded = {}

    async def fake_upsert(**kwargs):
        recorded.update(kwargs)

    async def fake_invalidate(user_id):
        return None

    monkeypatch.setattr(ds, "upsert_oauth_tokens", fake_upsert)
    monkeypatch.setattr(ds, "invalidate_oauth_active_cache", fake_invalidate)

    await ds.store_token("user-1", "acct-token")

    assert recorded["provider"] == ds.DEEPSEEK_PROVIDER
    assert recorded["access_token"] == "acct-token"
    assert recorded["refresh_token"] == ""
    assert recorded["expires_at"] == ds.NO_EXPIRY


@pytest.mark.asyncio
async def test_disconnect_deletes_locally_before_remote_logout(monkeypatch):
    order: list[str] = []

    async def fake_get(user_id, provider):
        return {"access_token": "acct-token"}

    async def fake_delete(user_id, provider):
        order.append("delete")

    async def fake_logout(token):
        order.append(f"logout:{token}")

    async def fake_invalidate(user_id):
        return None

    monkeypatch.setattr(ds, "get_oauth_tokens", fake_get)
    monkeypatch.setattr(ds, "delete_oauth_tokens", fake_delete)
    monkeypatch.setattr(ds, "logout_account", fake_logout)
    monkeypatch.setattr(ds, "invalidate_oauth_active_cache", fake_invalidate)

    await ds.disconnect("user-1")
    assert order == ["delete", "logout:acct-token"]


@pytest.mark.asyncio
async def test_get_valid_token_requires_a_stored_access_token(monkeypatch):
    async def none_row(user_id, provider):
        return None

    monkeypatch.setattr(ds, "get_oauth_tokens", none_row)
    assert await ds.get_valid_token("user-1") is None

    async def row(user_id, provider):
        return {"access_token": "acct-token", "account_id": "", "expires_at": ds.NO_EXPIRY}

    monkeypatch.setattr(ds, "get_oauth_tokens", row)
    assert (await ds.get_valid_token("user-1"))["access_token"] == "acct-token"


@pytest.mark.asyncio
async def test_exchange_sends_the_platform_client_identity_headers(fake_http, monkeypatch):
    async def fake_device_id():
        return "device-uuid"

    monkeypatch.setattr(ds, "device_id", fake_device_id)
    _FakeClient.response = _FakeResponse(200, {"token": "acct-token"})

    await ds.exchange_code("code-1", "verifier-1", "http://localhost:5173/cb")

    _url, _body, headers = fake_http.calls[0]
    assert headers["x-client-bundle-id"] == ""
    assert headers["x-client-platform"] == "web"
    assert headers["x-client-version"]
    assert headers["x-client-locale"] == "zh_CN"
    timezone_offset = int(headers["x-client-timezone-offset"])
    assert -14 * 60 * 60 <= timezone_offset <= 14 * 60 * 60


@pytest.mark.asyncio
async def test_authorization_accepts_the_harness_callback_path(fake_http):
    flow = await ds.start_authorization("http://localhost:5173/oauth/callback")

    _url, body, _headers = fake_http.calls[0]
    assert body["redirect_uri"] == "http://localhost:5173/oauth/callback"
    assert flow["authorize_url"].startswith("https://platform.deepseek.com/dsh/authorize")


@pytest.mark.asyncio
async def test_start_authorization_reads_the_platform_business_envelope(fake_http):
    _FakeClient.response = _FakeResponse(
        200,
        {
            "code": 0,
            "msg": "",
            "data": {
                "biz_code": 0,
                "biz_msg": "",
                "biz_data": {
                    "authorize_url": "https://platform.deepseek.com/dsh/authorize?authorize_id=a",
                    "authorize_id": "a",
                    "expires_in": 600,
                },
            },
        },
    )

    flow = await ds.start_authorization("http://localhost:5173/oauth/deepseek")

    assert flow["authorize_url"].endswith("authorize_id=a")
    assert flow["expires_in"] == 600


@pytest.mark.asyncio
async def test_a_business_error_is_not_reported_as_a_missing_url(fake_http):
    _FakeClient.response = _FakeResponse(
        200,
        {"code": 0, "msg": "", "data": {"biz_code": 1001, "biz_msg": "login source rejected"}},
    )

    with pytest.raises(ds.DeepSeekOAuthError, match="login source rejected"):
        await ds.start_authorization("http://localhost:5173/oauth/deepseek")


@pytest.mark.asyncio
async def test_exchange_reads_a_nested_token(fake_http, monkeypatch):
    async def fake_device_id():
        return "device-uuid"

    monkeypatch.setattr(ds, "device_id", fake_device_id)
    _FakeClient.response = _FakeResponse(
        200,
        {"code": 0, "data": {"biz_code": 0, "biz_data": {"token": "nested-token"}}},
    )

    token = await ds.exchange_code("code-1", "verifier-1", "http://127.0.0.1:5173/cb")
    assert token == "nested-token"


def test_no_expiry_sentinel_decodes_inside_python_datetime_range():
    """The stored sentinel must survive psycopg decoding in positive-offset zones.

    Session timezone Asia/Shanghai turns 9999-12-31 23:59:59+00 into year 10000,
    which psycopg rejects with DataError; that made the DeepSeek account token
    unreadable. A 00:00 UTC sentinel leaves a full year of headroom for offsets
    up to +14.
    """
    from datetime import timedelta, timezone

    for offset_hours in (8, 14):
        zone = timezone(timedelta(hours=offset_hours))
        decoded = ds.NO_EXPIRY.astimezone(zone)
        assert decoded.year == 9999

    # The deployment's actual database session timezone.
    shanghai = ds.NO_EXPIRY.astimezone(timezone(timedelta(hours=8)))
    assert shanghai.isoformat().startswith("9999-12-31")
