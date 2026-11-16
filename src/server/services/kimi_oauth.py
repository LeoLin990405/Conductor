"""Kimi Code OAuth service — device-code flow, token refresh with Redis lock.

Kimi Code (Moonshot's coding-plan subscription) publishes an OAuth device grant
for its first-party CLI. This service uses that same public client and the
standard device grant, so the app never asks for a Kimi password:

- device code:  https://auth.kimi.com/api/oauth/device_authorization
- token:        https://auth.kimi.com/api/oauth/token
- chat traffic: https://api.kimi.com/coding (Anthropic-compatible)

The auth server only answers a request that identifies the CLI caller, which is
what the ``X-Msh-*`` headers below are for. Three of them describe this machine;
the device id must be the same for the code request and every later poll, or an
approval would be bound to a different device. It is therefore persisted in
Redis once and reused by every worker — an ephemeral id is the fallback only
when Redis is unreachable, which can strand an in-flight approval but never
invalidates an already-stored token.

The coding endpoint accepts the OAuth access token as a bearer credential; the
Anthropic SDK is still handed the same value as its API key so that its
``x-api-key`` header carries it too (the endpoint accepts either). See
``resolve_oauth_llm_client`` for where the client is built.
"""

import base64
import json
import logging
import platform
import secrets
import socket
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from src.server.database.oauth_tokens import (
    get_oauth_tokens,
    invalidate_oauth_active_cache,
    upsert_oauth_tokens,
)
from src.server.services.oauth_refresh_wait import (
    REFRESH_BUFFER,
    REFRESH_LOCK_TTL_MS,
    await_refreshed_tokens,
    token_expiry,
)

logger = logging.getLogger(__name__)

KIMI_PROVIDER = "kimi-oauth"

# Public OAuth client used by Kimi's first-party CLI tooling.
KIMI_CLIENT_ID_ENV = "CONDUCTOR_KIMI_OAUTH_CLIENT_ID"


def kimi_client_id() -> str:
    """Kimi OAuth client setting from ``CONDUCTOR_KIMI_OAUTH_CLIENT_ID`` (not shipped; no default)."""
    from src.server.services.oauth_client_config import require

    return require(KIMI_CLIENT_ID_ENV, "Kimi")


KIMI_DEVICE_URL = "https://auth.kimi.com/api/oauth/device_authorization"
KIMI_TOKEN_URL = "https://auth.kimi.com/api/oauth/token"
KIMI_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# Shared by every `X-Msh-*` request the CLI makes; the auth server rejects
# requests from clients that do not identify themselves as the CLI.
KIMI_CLI_VERSION = "2.1.1"
KIMI_DEVICE_ID_KEY = "oauth:kimi:device_id"


def _header_value(value: object) -> str:
    """A header-safe rendering of a machine-supplied string.

    Hostnames and OS strings are user-machine data: a newline would split the
    header and a non-ASCII character would make httpx refuse the request, so
    keep only printable ASCII and never let the field go empty.
    """
    printable = "".join(ch for ch in str(value) if 32 <= ord(ch) < 127)
    return printable.strip() or "unknown"


async def _device_id() -> str:
    """The stable id of this installation, shared by all workers via Redis."""
    from src.utils.cache.redis_cache import get_cache_client

    try:
        cache = get_cache_client()
        if cache.enabled and cache.client:
            existing = await cache.client.get(KIMI_DEVICE_ID_KEY)
            if existing:
                return _header_value(existing.decode() if isinstance(existing, bytes) else existing)
            candidate = str(uuid.uuid4())
            # SETNX: two workers starting at once must converge on one id, so
            # whoever wins the write decides the id and everyone reads it back.
            await cache.client.set(KIMI_DEVICE_ID_KEY, candidate, nx=True)
            stored = await cache.client.get(KIMI_DEVICE_ID_KEY)
            if stored:
                return _header_value(stored.decode() if isinstance(stored, bytes) else stored)
            return candidate
    except Exception:
        pass
    # Redis down: a process-local id still completes the flow through this
    # worker; only a poll served by another worker would not recognise it.
    return str(uuid.uuid4())


async def _auth_headers() -> dict:
    """The CLI identity headers required on every Kimi auth request."""
    return {
        "Accept": "application/json",
        "X-Msh-Platform": "kimi_cli",
        "X-Msh-Version": KIMI_CLI_VERSION,
        "X-Msh-Device-Name": _header_value(socket.gethostname()),
        "X-Msh-Device-Model": _header_value(
            f"{platform.system()} {platform.release()} {platform.machine()}"
        ),
        "X-Msh-Os-Version": _header_value(platform.version()),
        "X-Msh-Device-Id": await _device_id(),
    }


def _form_headers(auth_headers: dict) -> dict:
    return {**auth_headers, "Content-Type": "application/x-www-form-urlencoded"}


async def request_device_code() -> dict:
    """Start a device-code authorization; returns the code the user approves."""
    headers = _form_headers(await _auth_headers())
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            KIMI_DEVICE_URL,
            data={"client_id": kimi_client_id()},
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
    return {
        "device_code": data["device_code"],
        "user_code": data["user_code"],
        "verification_url": (
            data.get("verification_uri_complete") or data.get("verification_uri") or ""
        ),
        "interval": int(data.get("interval") or 5),
        "expires_in": int(data.get("expires_in") or 1800),
    }


def _token_bundle(data: dict) -> dict:
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "id_token": data.get("id_token", ""),
        "expires_in": int(data.get("expires_in") or 3600),
    }


async def poll_device_authorization(device_code: str) -> dict | None:
    """One poll attempt.

    Returns the token bundle once the user approves, None while the login is
    still pending (``authorization_pending`` / ``slow_down``). Other failures
    raise; only the OAuth error code is surfaced — an auth server's error body
    can echo request detail and must never reach the wire or a log line.
    """
    headers = _form_headers(await _auth_headers())
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            KIMI_TOKEN_URL,
            data={
                "grant_type": KIMI_DEVICE_GRANT,
                "device_code": device_code,
                "client_id": kimi_client_id(),
            },
            headers=headers,
        )
    if resp.status_code == 200:
        return _token_bundle(resp.json())
    try:
        error = (resp.json() or {}).get("error")
    except Exception:
        error = None
    if resp.status_code in (400, 428) and error in ("authorization_pending", "slow_down"):
        return None
    raise RuntimeError(
        f"Kimi device authorization failed ({resp.status_code}): {error or 'unknown error'}"
    )


async def refresh_tokens(refresh_token: str) -> dict:
    """Refresh an expired access token."""
    headers = _form_headers(await _auth_headers())
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            KIMI_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": kimi_client_id(),
            },
            headers=headers,
        )
        resp.raise_for_status()
        bundle = _token_bundle(resp.json())
    # OAuth providers may rotate refresh tokens, but some successful refresh
    # responses omit the field. Preserve the still-valid prior token in that
    # case; persisting an empty value strands the account.
    bundle["refresh_token"] = bundle["refresh_token"] or refresh_token
    return bundle


def parse_id_token_email(token: str) -> str | None:
    """The email claim of an id_token, when the provider sent one.

    No signature check: the token arrived directly from the token endpoint
    over TLS, and the claim is display-only.
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except Exception:
        return None
    email = payload.get("email")
    return email if isinstance(email, str) else None


async def save_device_tokens(user_id: str, tokens: dict) -> dict:
    """Persist a just-approved device login; returns non-secret account fields.

    Kimi's device grant carries no account claim we can rely on, so
    ``account_id`` stays empty and only the display email is kept.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=tokens["expires_in"])
    email = parse_id_token_email(tokens.get("id_token", ""))
    await upsert_oauth_tokens(
        user_id=user_id,
        provider=KIMI_PROVIDER,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        account_id="",
        email=email,
        plan_type=None,
        expires_at=expires_at,
    )
    try:
        await invalidate_oauth_active_cache(user_id)
    except Exception:
        pass
    return {"email": email, "account_id": ""}


def _token_view(tokens: dict) -> dict:
    return {
        "access_token": tokens["access_token"],
        "account_id": tokens["account_id"],
        "plan_type": tokens.get("plan_type"),
    }


async def get_valid_token(user_id: str) -> dict | None:
    """Valid access token for the user's connected Kimi account (None if not connected)."""
    tokens = await get_oauth_tokens(user_id, KIMI_PROVIDER)
    if not tokens:
        return None

    now = datetime.now(timezone.utc)
    if token_expiry(tokens) > now + REFRESH_BUFFER:
        return _token_view(tokens)

    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    lock_key = f"oauth:refresh:{user_id}:{KIMI_PROVIDER}"
    lock_token = secrets.token_hex(16)
    acquired = await cache.acquire_lock(lock_key, lock_token, REFRESH_LOCK_TTL_MS)
    if acquired is False:
        refreshed = await await_refreshed_tokens(user_id, KIMI_PROVIDER)
        return _token_view(refreshed) if refreshed else None

    try:
        # The snapshot above predates the distributed lock. Another worker may
        # have rotated it and released the lock while this request was waiting
        # to acquire it; always re-read under ownership.
        latest = await get_oauth_tokens(user_id, KIMI_PROVIDER)
        if not latest:
            return None
        tokens = latest
        now = datetime.now(timezone.utc)
        if token_expiry(tokens) > now + REFRESH_BUFFER:
            return _token_view(tokens)

        new = await refresh_tokens(tokens["refresh_token"])
        email = parse_id_token_email(new["id_token"]) or tokens.get("email")
        expires_at = now + timedelta(seconds=new["expires_in"])

        await upsert_oauth_tokens(
            user_id=user_id,
            provider=KIMI_PROVIDER,
            access_token=new["access_token"],
            # ``refresh_tokens`` already preserves a missing rotation, but the
            # row is the last copy: keep the old token rather than persist "".
            refresh_token=new["refresh_token"] or tokens["refresh_token"],
            account_id=tokens.get("account_id") or "",
            email=email,
            plan_type=tokens.get("plan_type"),
            expires_at=expires_at,
        )
        try:
            await invalidate_oauth_active_cache(user_id)
        except Exception:
            pass
        logger.debug(f"[kimi_oauth] Refreshed tokens for user_id={user_id}")
        return {
            "access_token": new["access_token"],
            "account_id": tokens.get("account_id") or "",
            "plan_type": tokens.get("plan_type"),
        }
    except Exception as e:
        logger.error(f"[kimi_oauth] Token refresh failed for user_id={user_id}: {e}")
        return None
    finally:
        if acquired:
            await cache.release_lock(lock_key, lock_token)


def __getattr__(name: str):
    if name == "KIMI_CLIENT_ID":
        return kimi_client_id()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
