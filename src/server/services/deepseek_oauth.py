"""DeepSeek account OAuth service — browser PKCE flow, account token storage.

Lets a user sign in with a DeepSeek Platform account instead of an API key.
The account route differs from the API-key route only in auth: the same Messages endpoint at
``https://api.deepseek.com/anthropic`` accepts a Platform account token in the
``x-dsh-auth-token`` header instead of ``x-api-key``.

Flow:
- ``POST {platform}/auth-api/v0/dsh/auth_init`` with PKCE S256, state,
  redirect_uri, locale and login_source; returns ``authorize_url``,
  ``authorize_id``, ``expires_in``.
- The browser completes the same-origin authorize page and returns ``code`` and
  ``state`` to the app's redirect URI.
- ``POST {platform}/auth-api/v0/dsh/auth_exchange`` with the code, verifier,
  redirect_uri and a stable device identity; returns the account token.
- Inference and the account model catalog use ``x-dsh-auth-token``; sign-out
  deletes locally first, then asks ``/auth-api/v0/users/logout`` to revoke.

There is no refresh grant: DeepSeek account tokens are long-lived and a 401
means sign in again, so the stored row has no refresh token and no expiry.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import platform
import secrets
import socket
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from src.server.database.oauth_tokens import (
    delete_oauth_tokens,
    get_oauth_tokens,
    invalidate_oauth_active_cache,
    upsert_oauth_tokens,
)

logger = logging.getLogger(__name__)

DEEPSEEK_PROVIDER = "deepseek-account"
DEEPSEEK_PLATFORM_ORIGIN = "https://platform.deepseek.com"
DEEPSEEK_AUTH_INIT_URL = f"{DEEPSEEK_PLATFORM_ORIGIN}/auth-api/v0/dsh/auth_init"
DEEPSEEK_AUTH_EXCHANGE_URL = f"{DEEPSEEK_PLATFORM_ORIGIN}/auth-api/v0/dsh/auth_exchange"
DEEPSEEK_LOGOUT_URL = f"{DEEPSEEK_PLATFORM_ORIGIN}/auth-api/v0/users/logout"

DEEPSEEK_DEVICE_ID_KEY = "oauth:deepseek:device_id"
AUTH_TIMEOUT_SECONDS = 30.0
# The authorize attempt expires server-side; this is the local cache bound.
FLOW_TTL_SECONDS = 600

# Platform's auth exchange validates the caller identity. The client sends
# five headers derived from the requesting UI (the web shell in OSS
# Conductor): an intentionally empty bundle id, "web" platform, build version,
# wire locale, and the east-positive timezone offset in seconds.
CLIENT_BUNDLE_ID = ""
CLIENT_PLATFORM = "web"
CLIENT_VERSION = "0.1.0"
CLIENT_WIRE_LOCALE = "zh_CN"
# DeepSeek account tokens are long-lived and have no refresh grant, but the
# shared credential table requires a non-null expires_at. A far-future sentinel
# records "no expiry" without a schema change. Keep it at 00:00 UTC: psycopg
# decodes a timestamptz in the session timezone and the app's database runs in
# Asia/Shanghai, so 23:59:59 UTC would come back as year 10000 and raise
# DataError. 00:00 UTC stays inside Python's year-9999 range in every timezone.
NO_EXPIRY = datetime(9999, 12, 31, tzinfo=timezone.utc)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


class DeepSeekOAuthError(RuntimeError):
    """A sanitized DeepSeek sign-in failure (never carries the token)."""


def generate_pkce_pair() -> tuple[str, str]:
    """Return (verifier, S256 challenge), per the platform's documented flow."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def validate_redirect_uri(raw: str) -> str:
    """Only a loopback app callback is accepted as the OAuth redirect target.

    The value is handed to DeepSeek's authorize page, so anything but a
    loopback HTTP(S) origin with a plain path would make this an open redirect.
    """
    value = (raw or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise DeepSeekOAuthError("redirect_uri must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DeepSeekOAuthError("redirect_uri must be a plain loopback callback URL")
    if parsed.hostname not in _LOOPBACK_HOSTS:
        raise DeepSeekOAuthError("redirect_uri must point at this machine")
    return value


async def device_id() -> str:
    """Return the stable installation id for DeepSeek account requests."""
    from src.utils.cache.redis_cache import get_cache_client

    try:
        cache = get_cache_client()
        if cache.enabled and cache.client:
            existing = await cache.client.get(DEEPSEEK_DEVICE_ID_KEY)
            if existing:
                return existing.decode() if isinstance(existing, bytes) else str(existing)
            candidate = str(uuid.uuid4())
            await cache.client.set(DEEPSEEK_DEVICE_ID_KEY, candidate, nx=True)
            stored = await cache.client.get(DEEPSEEK_DEVICE_ID_KEY)
            if stored:
                return stored.decode() if isinstance(stored, bytes) else str(stored)
            return candidate
    except Exception:
        pass
    return str(uuid.uuid4())


def _device_model() -> str:
    return f"{platform.system().lower()}-{platform.machine().lower()}"


def _os_version() -> str:
    return platform.version() or socket.gethostname()


def _timezone_offset_seconds() -> int:
    offset = datetime.now().astimezone().utcoffset()
    return int(offset.total_seconds()) if offset is not None else 0


def _headers() -> dict[str, str]:
    return {
        "content-type": "application/json",
        "accept": "application/json",
        "x-client-bundle-id": CLIENT_BUNDLE_ID,
        "x-client-platform": CLIENT_PLATFORM,
        "x-client-version": CLIENT_VERSION,
        "x-client-locale": CLIENT_WIRE_LOCALE,
        "x-client-timezone-offset": str(_timezone_offset_seconds()),
    }


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        for key in ("message", "msg", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
    return f"HTTP {response.status_code}"


def _unwrap_payload(payload: object) -> dict:
    """Normalize the Platform's outer envelope to its business object.

    The auth routes answer ``{code, msg, data: {biz_code, biz_msg, biz_data}}``
    (and older responses were bare objects). Parsing both keeps the adapter
    tolerant without trusting the envelope's zero code.
    """
    if not isinstance(payload, dict):
        raise DeepSeekOAuthError("DeepSeek sign-in returned an invalid response")
    code = payload.get("code")
    if code not in (None, 0, "0"):
        raise DeepSeekOAuthError(
            f"DeepSeek sign-in failed: {str(payload.get('msg') or code)[:200]}"
        )
    data = payload.get("data")
    if isinstance(data, dict):
        biz_code = data.get("biz_code")
        if biz_code not in (None, 0, "0"):
            raise DeepSeekOAuthError(
                f"DeepSeek sign-in failed: {str(data.get('biz_msg') or biz_code)[:200]}"
            )
        biz_data = data.get("biz_data")
        return biz_data if isinstance(biz_data, dict) else data
    return payload


async def start_authorization(redirect_uri: str) -> dict:
    """Begin a sign-in: returns the browser URL plus the server-side flow state."""
    redirect_uri = validate_redirect_uri(redirect_uri)
    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(32)
    body = {
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "redirect_uri": redirect_uri,
        "locale": "zh_CN",
        "login_source": "web",
    }
    async with httpx.AsyncClient(timeout=AUTH_TIMEOUT_SECONDS, follow_redirects=False) as client:
        try:
            response = await client.post(DEEPSEEK_AUTH_INIT_URL, json=body, headers=_headers())
        except httpx.HTTPError as exc:
            raise DeepSeekOAuthError("DeepSeek sign-in is unreachable; check the network") from exc
    if response.status_code >= 400:
        raise DeepSeekOAuthError(f"DeepSeek sign-in failed: {_error_detail(response)}")
    try:
        payload = _unwrap_payload(response.json())
    except DeepSeekOAuthError:
        raise
    except Exception as exc:
        raise DeepSeekOAuthError("DeepSeek sign-in returned an invalid response") from exc
    authorize_url = payload.get("authorize_url")
    if not isinstance(authorize_url, str) or not authorize_url:
        raise DeepSeekOAuthError("DeepSeek sign-in returned no authorize URL")
    expires_in = payload.get("expires_in")
    ttl = int(expires_in) if isinstance(expires_in, (int, float)) and expires_in > 0 else FLOW_TTL_SECONDS
    return {
        "authorize_url": authorize_url,
        "authorize_id": payload.get("authorize_id"),
        "state": state,
        "verifier": verifier,
        "redirect_uri": redirect_uri,
        "expires_in": ttl,
    }


async def exchange_code(code: str, verifier: str, redirect_uri: str) -> str:
    """Exchange the browser code for the Platform account token."""
    redirect_uri = validate_redirect_uri(redirect_uri)
    body = {
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "device_id": await device_id(),
        "device_model": _device_model(),
        "os_version": _os_version(),
    }
    async with httpx.AsyncClient(timeout=AUTH_TIMEOUT_SECONDS, follow_redirects=False) as client:
        try:
            response = await client.post(DEEPSEEK_AUTH_EXCHANGE_URL, json=body, headers=_headers())
        except httpx.HTTPError as exc:
            raise DeepSeekOAuthError("DeepSeek sign-in exchange is unreachable") from exc
    if response.status_code >= 400:
        raise DeepSeekOAuthError(f"DeepSeek sign-in exchange failed: {_error_detail(response)}")
    try:
        payload = _unwrap_payload(response.json())
    except DeepSeekOAuthError:
        raise
    except Exception as exc:
        raise DeepSeekOAuthError("DeepSeek sign-in exchange returned an invalid response") from exc
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise DeepSeekOAuthError("DeepSeek sign-in exchange returned no token")
    return token


async def store_token(user_id: str, token: str) -> None:
    """Persist the account token; the route and the test both go through here."""
    await upsert_oauth_tokens(
        user_id=user_id,
        provider=DEEPSEEK_PROVIDER,
        access_token=token,
        refresh_token="",
        account_id="",
        email=None,
        plan_type=None,
        expires_at=NO_EXPIRY,
    )
    try:
        await invalidate_oauth_active_cache(user_id)
    except Exception:
        pass


async def get_valid_token(user_id: str) -> dict | None:
    """The stored token for this user, or None when they are not signed in."""
    row = await get_oauth_tokens(user_id, DEEPSEEK_PROVIDER)
    if not row or not row.get("access_token"):
        return None
    return {
        "access_token": row["access_token"],
        "account_id": row.get("account_id") or "",
        "expires_at": row.get("expires_at"),
    }


async def logout_account(token: str) -> None:
    """Best-effort remote sign-out; the local row is deleted by the caller first."""
    async with httpx.AsyncClient(timeout=AUTH_TIMEOUT_SECONDS, follow_redirects=False) as client:
        try:
            await client.post(
                DEEPSEEK_LOGOUT_URL,
                headers={**_headers(), "x-dsh-auth-token": token},
            )
        except httpx.HTTPError as exc:
            logger.info("[deepseek-oauth] remote logout failed: %s", exc)


async def invalidate_token(user_id: str) -> bool:
    """Drop a token the Platform rejected with 401; no remote logout attempt."""
    row = await get_oauth_tokens(user_id, DEEPSEEK_PROVIDER)
    if not row:
        return False
    await delete_oauth_tokens(user_id, DEEPSEEK_PROVIDER)
    try:
        await invalidate_oauth_active_cache(user_id)
    except Exception:
        pass
    return True


async def disconnect(user_id: str) -> None:
    """Delete local credentials first, then ask the platform to revoke."""
    row = await get_oauth_tokens(user_id, DEEPSEEK_PROVIDER)
    await delete_oauth_tokens(user_id, DEEPSEEK_PROVIDER)
    try:
        await invalidate_oauth_active_cache(user_id)
    except Exception:
        pass
    token = (row or {}).get("access_token")
    if isinstance(token, str) and token:
        await logout_account(token)


def flow_cache_key(user_id: str) -> str:
    return f"oauth:deepseek:{user_id}"


def dumps_flow(flow: dict) -> str:
    return json.dumps(
        {
            "state": flow["state"],
            "verifier": flow["verifier"],
            "redirect_uri": flow["redirect_uri"],
        }
    )


def loads_flow(raw: str | bytes | None) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        return None
    if not isinstance(value, dict) or not all(
        isinstance(value.get(key), str) and value[key] for key in ("state", "verifier", "redirect_uri")
    ):
        return None
    return value


def expiry_from_now(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# ─── Account model catalog ──────────────────────────────────────────────────
#
# The account route's model list comes from the Platform (`GET /models` at the
# inference origin) rather than from our manifest. It is read once per
# credential, cached for ten minutes, and the built-in catalog is used when
# the request fails.

DEEPSEEK_MODELS_URL = "https://api.deepseek.com/models"
MODELS_CACHE_TTL_SECONDS = 600
MODELS_TIMEOUT_SECONDS = 5.0


def _models_cache_key(token: str) -> str:
    import hashlib

    digest = hashlib.sha256(token.encode()).hexdigest()
    return f"oauth:deepseek:models:{digest}"


async def fetch_account_models(token: str) -> list[dict] | None:
    """The account's model catalog; None when the platform could not answer.

    The response is cached per token so a model page refresh never waits on the
    platform. A 401 is returned as None (not an exception) because a rejected
    token is a sign-in problem, not a startup problem; the caller keeps the
    built-in catalog in that case, like the official harness.
    """
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    key = _models_cache_key(token)
    if cache.enabled and cache.client:
        try:
            cached = await cache.client.get(key)
            if cached:
                value = json.loads(cached.decode() if isinstance(cached, bytes) else cached)
                if isinstance(value, list):
                    return value
        except Exception:
            pass

    async with httpx.AsyncClient(timeout=MODELS_TIMEOUT_SECONDS, follow_redirects=False) as client:
        try:
            response = await client.get(
                DEEPSEEK_MODELS_URL,
                headers={**_headers(), "x-dsh-auth-token": token},
            )
        except httpx.HTTPError as exc:
            logger.info("[deepseek-oauth] model catalog unreachable: %s", exc)
            return None
    if response.status_code >= 400:
        logger.info("[deepseek-oauth] model catalog refused: HTTP %s", response.status_code)
        return None
    try:
        payload = response.json()
    except Exception:
        return None
    rows = payload.get("data") if isinstance(payload, dict) else payload
    # The Platform's auth envelope nests business data; the public `/models`
    # route is normally a bare list under `data`, so accept both.
    if isinstance(rows, dict):
        rows = rows.get("biz_data") or rows.get("data")
    if not isinstance(rows, list):
        return None
    models = [row for row in rows if isinstance(row, dict)]
    if cache.enabled and cache.client:
        try:
            await cache.client.set(key, json.dumps(models), ex=MODELS_CACHE_TTL_SECONDS)
        except Exception:
            pass
    return models


async def account_model_catalog(user_id: str) -> list[dict] | None:
    """The signed-in user's catalog, or None when there is no usable account."""
    token_data = await get_valid_token(user_id)
    if not token_data:
        return None
    return await fetch_account_models(token_data["access_token"])
