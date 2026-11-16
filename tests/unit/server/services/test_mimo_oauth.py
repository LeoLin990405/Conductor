"""MiMo account login: X25519 + AES-256-GCM paste-code protocol and routes.

These tests build real codes with the same primitives the platform uses, so
they pin the wire format (ephemeral public key || nonce || ciphertext || tag,
SHA-256 of the X25519 shared secret) rather than mocking the crypto away. The
negative cases are the security contract: tampered payloads, replayed pending
state, bounded input, and no secret ever reaching a response, an exception
message, or a log line. All DB/Redis access is faked; there is no network.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.server.services import mimo_oauth
from src.utils.cache import redis_cache

PAYLOAD = {
    "uid": "mimo-user-1",
    "url": "https://api.xiaomimimo.com/v1",
    "sk": "sk-" "mimo-minted-secret-abc123",
}
PENDING_KEY = "oauth:mimo:user-1"


# ─── Protocol helpers (the platform's side of the envelope) ─────────────────


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _raw(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def build_code(private_key_b64: str, payload: dict | None = None) -> str:
    """Encrypt ``payload`` to our public key exactly like the platform does."""
    payload = PAYLOAD if payload is None else payload
    private_key = X25519PrivateKey.from_private_bytes(_raw(private_key_b64))
    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(private_key.public_key())
    digest = hashes.Hash(hashes.SHA256())
    digest.update(shared)
    nonce = os.urandom(12)
    ciphertext = AESGCM(digest.finalize()).encrypt(
        nonce, json.dumps(payload).encode(), None
    )
    ephemeral_public = ephemeral.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return _b64url(ephemeral_public + nonce + ciphertext)


# ─── create_login ──────────────────────────────────────────────────────────


def test_create_login_builds_the_authorize_url_and_keeps_the_private_half():
    from urllib.parse import parse_qs, urlparse

    login = mimo_oauth.create_login()
    parsed = urlparse(login["authorize_url"])
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "platform.xiaomimimo.com"
    assert parsed.path == "/authorize"
    assert set(query) == {"pk", "redirect_uri", "kn", "key_name"}
    assert query["kn"] == [mimo_oauth.MIMO_KN] == ["mimocode"]
    assert query["redirect_uri"] == [
        "https://platform.xiaomimimo.com/authorize/code/callback"
    ]
    assert query["key_name"][0].startswith("mimo-code-cli-key-")
    assert len(query["key_name"][0]) == len("mimo-code-cli-key-") + 8

    # The private half only exists locally; the URL carries the public half.
    private_raw = _raw(login["private_key"])
    public_raw = _raw(query["pk"][0])
    assert len(private_raw) == 32
    assert len(public_raw) == 32
    expected_public = (
        X25519PrivateKey.from_private_bytes(private_raw)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    assert public_raw == expected_public
    assert login["private_key"] not in login["authorize_url"]


def test_two_logins_use_fresh_keys_and_key_names():
    first = mimo_oauth.create_login()
    second = mimo_oauth.create_login()

    assert first["private_key"] != second["private_key"]
    assert first["key_name"] != second["key_name"]
    assert first["authorize_url"] != second["authorize_url"]


def test_operator_platform_override_drives_both_urls(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    monkeypatch.setenv("MIMO_PLATFORM_URL", "https://mimo.staging.example/")

    login = mimo_oauth.create_login()
    parsed = urlparse(login["authorize_url"])

    assert parsed.netloc == "mimo.staging.example"
    assert parse_qs(parsed.query)["redirect_uri"] == [
        "https://mimo.staging.example/authorize/code/callback"
    ]


def test_platform_override_is_read_per_call(monkeypatch):
    monkeypatch.setenv("MIMO_PLATFORM_URL", "https://staging.example")

    assert mimo_oauth.platform_base_url() == "https://staging.example"

    monkeypatch.delenv("MIMO_PLATFORM_URL")

    assert mimo_oauth.platform_base_url() == mimo_oauth.DEFAULT_PLATFORM_URL


# ─── decrypt_code: the happy path ──────────────────────────────────────────


def test_decrypt_code_returns_the_normalized_payload():
    login = mimo_oauth.create_login()
    code = build_code(login["private_key"])

    identity = mimo_oauth.decrypt_code(login["private_key"], code)

    assert identity == {
        "uid": PAYLOAD["uid"],
        "base_url": PAYLOAD["url"],
        "api_key": PAYLOAD["sk"],
    }


def test_decrypt_code_tolerates_pasted_whitespace_and_padding():
    login = mimo_oauth.create_login()
    code = build_code(login["private_key"])

    spaced = "  " + code[:20] + "\n\t" + code[20:] + "  "
    padded = code + "=" * (-len(code) % 4)

    assert mimo_oauth.decrypt_code(login["private_key"], spaced)["api_key"] == PAYLOAD["sk"]
    assert mimo_oauth.decrypt_code(login["private_key"], padded)["api_key"] == PAYLOAD["sk"]


# ─── decrypt_code: tampering and malformed input ───────────────────────────


def _assert_invalid(private_key_b64: str, code: str) -> None:
    with pytest.raises(mimo_oauth.MiMoOAuthError) as excinfo:
        mimo_oauth.decrypt_code(private_key_b64, code)

    message = str(excinfo.value)
    assert message == mimo_oauth.CODE_INVALID_MESSAGE
    # The failure must not echo any material an attacker could use.
    if code.strip():
        assert code[:24] not in message
    assert PAYLOAD["sk"] not in message


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda raw: raw.__setitem__(-1, raw[-1] ^ 0x01), id="ciphertext-tag"),
        pytest.param(lambda raw: raw.__setitem__(0, raw[0] ^ 0x01), id="ephemeral-key"),
        pytest.param(
            lambda raw: raw.__setitem__(32 + 11, raw[32 + 11] ^ 0x01), id="nonce"
        ),
    ],
)
def test_tampered_envelope_is_rejected_with_the_fixed_message(mutate):
    login = mimo_oauth.create_login()
    raw = bytearray(_raw(build_code(login["private_key"])))
    mutate(raw)

    _assert_invalid(login["private_key"], _b64url(bytes(raw)))


def test_code_for_a_different_pending_key_is_rejected():
    first = mimo_oauth.create_login()
    second = mimo_oauth.create_login()

    _assert_invalid(second["private_key"], build_code(first["private_key"]))


@pytest.mark.parametrize(
    "code",
    [
        "",
        "   ",
        "not*base64url",
        "A",  # length 1 mod 4 can never be valid base64
        _b64url(b"\x00" * 40),  # shorter than pub + nonce + tag
        _b64url(b"\x00" * (32 + 12 + 16)),  # right length, wrong content
    ],
)
def test_malformed_or_short_codes_are_rejected(code):
    login = mimo_oauth.create_login()

    _assert_invalid(login["private_key"], code)


def test_oversized_code_is_rejected_before_any_crypto():
    login = mimo_oauth.create_login()

    _assert_invalid(
        login["private_key"], "A" * (mimo_oauth.MAX_CODE_LENGTH + 1)
    )


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"url": PAYLOAD["url"], "sk": PAYLOAD["sk"]}, id="missing-uid"),
        pytest.param({"uid": PAYLOAD["uid"], "sk": PAYLOAD["sk"]}, id="missing-url"),
        pytest.param({"uid": PAYLOAD["uid"], "url": PAYLOAD["url"]}, id="missing-sk"),
        pytest.param(
            {"uid": "", "url": PAYLOAD["url"], "sk": PAYLOAD["sk"]}, id="empty-uid"
        ),
        pytest.param(
            {"uid": PAYLOAD["uid"], "url": PAYLOAD["url"], "sk": ""}, id="empty-sk"
        ),
        pytest.param(
            {"uid": PAYLOAD["uid"], "url": PAYLOAD["url"], "sk": 123}, id="numeric-sk"
        ),
        pytest.param(
            {"uid": PAYLOAD["uid"], "url": "file:///etc/passwd", "sk": PAYLOAD["sk"]},
            id="non-http-url",
        ),
        pytest.param(
            {"uid": PAYLOAD["uid"], "url": "not-a-url", "sk": PAYLOAD["sk"]},
            id="unparseable-url",
        ),
        pytest.param(
            {"uid": True, "url": PAYLOAD["url"], "sk": PAYLOAD["sk"]}, id="bool-uid"
        ),
        pytest.param(
            {
                "uid": PAYLOAD["uid"],
                "url": "https://api.xiaomimimo.com/" + "x" * mimo_oauth.MAX_URL_LENGTH,
                "sk": PAYLOAD["sk"],
            },
            id="oversized-url",
        ),
        pytest.param(
            {
                "uid": PAYLOAD["uid"],
                "url": PAYLOAD["url"],
                "sk": "sk-" + "x" * mimo_oauth.MAX_CREDENTIAL_LENGTH,
            },
            id="oversized-key",
        ),
    ],
)
def test_an_invalid_payload_inside_a_valid_envelope_is_rejected(payload):
    login = mimo_oauth.create_login()

    _assert_invalid(login["private_key"], build_code(login["private_key"], payload))


# ─── save_login / get_status / disconnect ──────────────────────────────────


def _patch_store(monkeypatch, *, enabled: bool = True, status_error: bool = False):
    state = SimpleNamespace(
        upserts=[],
        enable_calls=[],
        invalidate=AsyncMock(),
        personalization=AsyncMock(),
    )

    async def upsert(user_id, provider, api_key, base_url=None):
        state.upserts.append(
            {
                "user_id": user_id,
                "provider": provider,
                "api_key": api_key,
                "base_url": base_url,
            }
        )

    async def enable(user_id, value):
        state.enable_calls.append((user_id, value))

    monkeypatch.setattr(mimo_oauth, "upsert_api_key", upsert)
    if status_error:
        monkeypatch.setattr(
            mimo_oauth,
            "get_user_provider_credential_status",
            AsyncMock(side_effect=RuntimeError("database down")),
        )
    else:
        monkeypatch.setattr(
            mimo_oauth,
            "get_user_provider_credential_status",
            AsyncMock(return_value={"enabled": enabled, "providers": {}}),
        )
    monkeypatch.setattr(mimo_oauth, "set_byok_enabled", enable)
    monkeypatch.setattr(mimo_oauth, "invalidate_byok_cache", state.invalidate)
    monkeypatch.setattr(
        mimo_oauth, "maybe_complete_personalization", state.personalization
    )
    return state


def _identity() -> dict:
    return {
        "uid": PAYLOAD["uid"],
        "base_url": PAYLOAD["url"],
        "api_key": PAYLOAD["sk"],
    }


@pytest.mark.asyncio
async def test_save_login_stores_the_minted_key_with_its_base_url(monkeypatch):
    state = _patch_store(monkeypatch)

    result = await mimo_oauth.save_login("user-1", _identity())

    assert state.upserts == [
        {
            "user_id": "user-1",
            "provider": "mimo-oauth",
            "api_key": PAYLOAD["sk"],
            "base_url": PAYLOAD["url"],
        }
    ]
    assert result == {"uid": PAYLOAD["uid"], "base_url_host": "api.xiaomimimo.com"}
    assert PAYLOAD["sk"] not in json.dumps(result)
    state.invalidate.assert_awaited_once_with("user-1")
    state.personalization.assert_awaited_once_with("user-1")


@pytest.mark.asyncio
async def test_save_login_enables_byok_only_when_it_is_off(monkeypatch):
    disabled = _patch_store(monkeypatch, enabled=False)
    await mimo_oauth.save_login("user-1", _identity())
    assert disabled.enable_calls == [("user-1", True)]

    enabled = _patch_store(monkeypatch, enabled=True)
    await mimo_oauth.save_login("user-1", _identity())
    assert enabled.enable_calls == []


@pytest.mark.asyncio
async def test_save_login_keeps_the_entry_when_post_store_setup_fails(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    state = _patch_store(monkeypatch, status_error=True)

    result = await mimo_oauth.save_login("user-1", _identity())

    # The credential is stored; a transient post-store failure must not report
    # the login as failed (the pending state is already consumed).
    assert state.upserts[0]["api_key"] == PAYLOAD["sk"]
    assert result["uid"] == PAYLOAD["uid"]
    assert "Post-store setup failed" in caplog.text
    assert PAYLOAD["sk"] not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("providers", "connected", "plan_type"),
    [
        ({"mimo-oauth": True, "openai": True}, True, mimo_oauth.PLAN_TYPE),
        ({"openai": True}, False, None),
        ({}, False, None),
    ],
)
async def test_get_status_reports_credential_presence(
    monkeypatch, providers, connected, plan_type
):
    monkeypatch.setattr(
        mimo_oauth,
        "get_user_provider_credential_status",
        AsyncMock(return_value={"enabled": True, "providers": providers}),
    )

    assert await mimo_oauth.get_status("user-1") == {
        "connected": connected,
        "account_id": None,
        "email": None,
        "plan_type": plan_type,
    }


@pytest.mark.asyncio
async def test_disconnect_removes_only_the_mimo_key(monkeypatch):
    deleted = []

    async def delete(user_id, provider):
        deleted.append((user_id, provider))

    invalidate = AsyncMock()
    monkeypatch.setattr(mimo_oauth, "delete_api_key", delete)
    monkeypatch.setattr(mimo_oauth, "invalidate_byok_cache", invalidate)

    await mimo_oauth.disconnect("user-1")

    assert deleted == [("user-1", "mimo-oauth")]
    invalidate.assert_awaited_once_with("user-1")


# ─── Routes (thin orchestration over the service) ──────────────────────────


class FakeCacheClient:
    def __init__(self):
        self.values: dict = {}
        self.expirations: dict = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **kwargs):
        self.values[key] = value
        self.expirations[key] = kwargs.get("ex")
        return True

    async def delete(self, key):
        self.values.pop(key, None)
        return 1

    async def getdel(self, key):
        self.expirations.pop(key, None)
        return self.values.pop(key, None)


class FakeCacheClientWithoutGetdel:
    """Older/fake client: the route must fall back to GET+DELETE."""

    def __init__(self):
        self.values: dict = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **kwargs):
        self.values[key] = value
        return True

    async def delete(self, key):
        self.values.pop(key, None)
        return 1


class FakeCache:
    enabled = True

    def __init__(self, client=None):
        self.client = client or FakeCacheClient()


@pytest.fixture
def oauth_routes(monkeypatch):
    from src.server.app import oauth as routes

    cache = FakeCache()
    monkeypatch.setattr(redis_cache, "get_cache_client", lambda: cache)
    return routes, cache


def _use_cache(monkeypatch, cache) -> None:
    monkeypatch.setattr(redis_cache, "get_cache_client", lambda: cache)


@pytest.mark.asyncio
async def test_initiate_route_stores_a_one_time_pending_login(oauth_routes):
    routes, cache = oauth_routes

    result = await routes.mimo_initiate("user-1")

    assert set(result) == {"authorize_url"}
    pending = json.loads(cache.client.values[PENDING_KEY])
    assert set(pending) == {"private_key", "key_name"}
    assert len(_raw(pending["private_key"])) == 32
    assert pending["key_name"].startswith("mimo-code-cli-key-")
    assert cache.client.expirations[PENDING_KEY] == 600
    assert pending["private_key"] not in json.dumps(result)


@pytest.mark.asyncio
async def test_callback_route_stores_the_minted_key_and_never_returns_it(
    oauth_routes, monkeypatch, caplog
):
    routes, cache = oauth_routes
    caplog.set_level(logging.DEBUG)
    state = _patch_store(monkeypatch)

    await routes.mimo_initiate("user-1")
    pending = json.loads(cache.client.values[PENDING_KEY])
    code = build_code(pending["private_key"])

    result = await routes.mimo_callback(
        "user-1", routes.MimoCallbackRequest(code=code)
    )

    assert result == {
        "success": True,
        "uid": PAYLOAD["uid"],
        "base_url_host": "api.xiaomimimo.com",
    }
    assert state.upserts[0]["provider"] == "mimo-oauth"
    assert state.upserts[0]["api_key"] == PAYLOAD["sk"]
    assert state.upserts[0]["base_url"] == PAYLOAD["url"]
    # The pending private key is consumed, so the code cannot be replayed.
    assert PENDING_KEY not in cache.client.values
    # No secret in the response or in any log line.
    rendered = json.dumps(result)
    assert PAYLOAD["sk"] not in rendered
    assert pending["private_key"] not in rendered
    assert PAYLOAD["sk"] not in caplog.text
    assert pending["private_key"] not in caplog.text
    assert code not in caplog.text


@pytest.mark.asyncio
async def test_callback_route_rejects_a_replayed_code(oauth_routes, monkeypatch):
    routes, cache = oauth_routes
    _patch_store(monkeypatch)

    await routes.mimo_initiate("user-1")
    pending = json.loads(cache.client.values[PENDING_KEY])
    code = build_code(pending["private_key"])

    await routes.mimo_callback("user-1", routes.MimoCallbackRequest(code=code))

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await routes.mimo_callback("user-1", routes.MimoCallbackRequest(code=code))
    assert excinfo.value.status_code == 410
    assert PAYLOAD["sk"] not in str(excinfo.value.detail)


@pytest.mark.asyncio
async def test_callback_route_reports_missing_or_expired_pending_state(oauth_routes):
    routes, _cache = oauth_routes
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await routes.mimo_callback(
            "user-1", routes.MimoCallbackRequest(code="some-code")
        )

    assert excinfo.value.status_code == 410


@pytest.mark.asyncio
async def test_callback_route_returns_a_sanitized_failure_for_a_tampered_code(
    oauth_routes, monkeypatch, caplog
):
    routes, cache = oauth_routes
    caplog.set_level(logging.DEBUG)
    state = _patch_store(monkeypatch)

    await routes.mimo_initiate("user-1")
    pending = json.loads(cache.client.values[PENDING_KEY])
    raw = bytearray(_raw(build_code(pending["private_key"])))
    raw[-1] ^= 0x01
    tampered = _b64url(bytes(raw))

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await routes.mimo_callback(
            "user-1", routes.MimoCallbackRequest(code=tampered)
        )

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == mimo_oauth.CODE_INVALID_MESSAGE
    assert state.upserts == []
    # The pending state is consumed even on failure: a code is single-use.
    assert PENDING_KEY not in cache.client.values
    assert tampered not in caplog.text
    assert pending["private_key"] not in caplog.text
    assert PAYLOAD["sk"] not in caplog.text


@pytest.mark.asyncio
async def test_callback_route_rejects_an_oversized_code_without_consuming_pending(
    oauth_routes, monkeypatch
):
    routes, cache = oauth_routes
    state = _patch_store(monkeypatch)
    await routes.mimo_initiate("user-1")

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await routes.mimo_callback(
            "user-1",
            routes.MimoCallbackRequest(code="A" * (mimo_oauth.MAX_CODE_LENGTH + 1)),
        )
    assert excinfo.value.status_code == 400
    # Length is checked before the pending state is consumed, so a truncated
    # paste can be retried.
    assert PENDING_KEY in cache.client.values
    assert state.upserts == []


@pytest.mark.asyncio
async def test_callback_route_rejects_an_empty_code_without_consuming_pending(
    oauth_routes,
):
    routes, cache = oauth_routes
    await routes.mimo_initiate("user-1")

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await routes.mimo_callback("user-1", routes.MimoCallbackRequest(code="  "))
    assert excinfo.value.status_code == 400
    assert PENDING_KEY in cache.client.values


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", ["not-json", b"\xff\xfe", "[]", '{"key_name": "x"}'])
async def test_callback_route_treats_a_corrupt_pending_value_as_expired(
    oauth_routes, corrupt
):
    routes, cache = oauth_routes
    cache.client.values[PENDING_KEY] = corrupt

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await routes.mimo_callback(
            "user-1", routes.MimoCallbackRequest(code="some-code")
        )
    assert excinfo.value.status_code == 410


@pytest.mark.asyncio
async def test_callback_route_reports_a_store_failure_sanitized(
    oauth_routes, monkeypatch, caplog
):
    routes, cache = oauth_routes
    caplog.set_level(logging.DEBUG)
    _patch_store(monkeypatch)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(routes.mimo_oauth, "upsert_api_key", broken)
    await routes.mimo_initiate("user-1")
    pending = json.loads(cache.client.values[PENDING_KEY])
    code = build_code(pending["private_key"])

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await routes.mimo_callback("user-1", routes.MimoCallbackRequest(code=code))
    assert excinfo.value.status_code == 500
    detail = str(excinfo.value.detail)
    assert PAYLOAD["sk"] not in detail
    assert code not in detail
    assert PAYLOAD["sk"] not in caplog.text
    assert pending["private_key"] not in caplog.text
    assert code not in caplog.text


@pytest.mark.asyncio
async def test_callback_falls_back_when_the_cache_client_has_no_getdel(monkeypatch):
    from src.server.app import oauth as route_module

    _patch_store(monkeypatch)
    fallback_cache = FakeCache(client=FakeCacheClientWithoutGetdel())
    _use_cache(monkeypatch, fallback_cache)

    await route_module.mimo_initiate("user-1")
    pending = json.loads(fallback_cache.client.values[PENDING_KEY])
    code = build_code(pending["private_key"])

    result = await route_module.mimo_callback(
        "user-1", route_module.MimoCallbackRequest(code=code)
    )

    assert result["success"] is True
    assert PENDING_KEY not in fallback_cache.client.values


@pytest.mark.asyncio
async def test_mimo_routes_fail_503_when_the_cache_is_unavailable(monkeypatch):
    from fastapi import HTTPException

    from src.server.app import oauth as route_module

    monkeypatch.setattr(
        redis_cache,
        "get_cache_client",
        lambda: SimpleNamespace(enabled=False, client=None),
    )

    with pytest.raises(HTTPException) as init_excinfo:
        await route_module.mimo_initiate("user-1")
    assert init_excinfo.value.status_code == 503

    with pytest.raises(HTTPException) as callback_excinfo:
        await route_module.mimo_callback(
            "user-1", route_module.MimoCallbackRequest(code="some-code")
        )
    assert callback_excinfo.value.status_code == 503


@pytest.mark.asyncio
async def test_status_and_disconnect_routes_delegate(oauth_routes, monkeypatch):
    routes, _cache = oauth_routes
    status = {
        "connected": True,
        "account_id": None,
        "email": None,
        "plan_type": "coding_plan",
    }
    monkeypatch.setattr(routes.mimo_oauth, "get_status", AsyncMock(return_value=status))
    disconnect = AsyncMock()
    monkeypatch.setattr(routes.mimo_oauth, "disconnect", disconnect)

    assert await routes.mimo_status("user-1") == status
    assert await routes.mimo_disconnect("user-1") == {"success": True}
    disconnect.assert_awaited_once_with("user-1")
