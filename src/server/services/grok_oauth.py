"""xAI Grok OAuth service — device-code flow, token refresh with Redis lock.

Connects Grok models through a user's own SuperGrok / X Premium subscription.
xAI publishes an OAuth client for its first-party CLI tooling; this service uses
that same public client and the standard device-code grant, so the app never
asks for an X password:

- device code:  https://auth.x.ai/oauth2/device/code
- token:        https://auth.x.ai/oauth2/token
- subscription: https://cli-chat-proxy.grok.com/v1 (Responses API)

The token is a bearer credential; the connected account's plan is not carried in
a verifiable claim, so plan gating is left to xAI's own entitlement checks.
"""

import base64
import json
import logging
import secrets
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

GROK_PROVIDER = "grok-oauth"

# Public OAuth client used by xAI's first-party Grok CLI tooling.
GROK_CLIENT_ID_ENV = "CONDUCTOR_GROK_OAUTH_CLIENT_ID"


def grok_client_id() -> str:
    """Grok OAuth client setting from ``CONDUCTOR_GROK_OAUTH_CLIENT_ID`` (not shipped; no default)."""
    from src.server.services.oauth_client_config import require

    return require(GROK_CLIENT_ID_ENV, "Grok")


GROK_SCOPE = (
    "openid profile email offline_access grok-cli:access api:access "
    "conversations:read conversations:write"
)
GROK_DEVICE_URL = "https://auth.x.ai/oauth2/device/code"
GROK_TOKEN_URL = "https://auth.x.ai/oauth2/token"
GROK_VERIFY_URL = "https://accounts.x.ai/oauth2/device"
GROK_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

_FORM_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
    "User-Agent": "conductor/0.1.0",
}


async def request_device_code() -> dict:
    """Start a device-code authorization; returns the code the user must approve."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            GROK_DEVICE_URL,
            data={"client_id": grok_client_id(), "scope": GROK_SCOPE},
            headers=_FORM_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "device_code": data["device_code"],
            "user_code": data["user_code"],
            "verification_url": (
                data.get("verification_uri_complete")
                or data.get("verification_uri")
                or GROK_VERIFY_URL
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

    Returns the token bundle on approval, None while the user has not approved
    yet (``authorization_pending`` / ``slow_down``). Other failures raise.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            GROK_TOKEN_URL,
            data={
                "grant_type": GROK_DEVICE_GRANT,
                "device_code": device_code,
                "client_id": grok_client_id(),
            },
            headers=_FORM_HEADERS,
        )
        if resp.status_code == 200:
            return _token_bundle(resp.json())
        try:
            error = (resp.json() or {}).get("error")
        except Exception:
            error = None
        if resp.status_code in (400, 428) and error in ("authorization_pending", "slow_down"):
            return None
        raise RuntimeError(f"xAI device authorization failed ({resp.status_code}): {error or 'unknown error'}")


async def refresh_tokens(refresh_token: str) -> dict:
    """Refresh an expired access token."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            GROK_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": grok_client_id(),
                "refresh_token": refresh_token,
            },
            headers=_FORM_HEADERS,
        )
        resp.raise_for_status()
        bundle = _token_bundle(resp.json())
        # OAuth providers may rotate refresh tokens, but some successful
        # refresh responses omit the field. Preserve the still-valid prior
        # token in that case; persisting an empty value strands the account.
        bundle["refresh_token"] = bundle["refresh_token"] or refresh_token
        return bundle


def parse_jwt_claims(token: str) -> dict:
    """Read an id_token payload (no signature check — it came from the token endpoint)."""
    if not token:
        return {"account_id": "", "email": None, "plan_type": None, "exp": None}
    parts = token.split(".")
    if len(parts) < 2:
        return {"account_id": "", "email": None, "plan_type": None, "exp": None}
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {"account_id": "", "email": None, "plan_type": None, "exp": None}
    return {
        "account_id": payload.get("sub") or payload.get("user_id") or "",
        "email": payload.get("email"),
        # xAI has no documented plan claim; entitlement stays with xAI.
        "plan_type": None,
        "exp": payload.get("exp"),
    }


async def get_valid_token(user_id: str) -> dict | None:
    """Valid access token for the user's connected Grok account (None if not connected)."""
    tokens = await get_oauth_tokens(user_id, GROK_PROVIDER)
    if not tokens:
        return None

    now = datetime.now(timezone.utc)
    if token_expiry(tokens) > now + REFRESH_BUFFER:
        return {
            "access_token": tokens["access_token"],
            "account_id": tokens["account_id"],
            "plan_type": tokens.get("plan_type"),
        }

    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    lock_key = f"oauth:refresh:{user_id}:{GROK_PROVIDER}"
    lock_token = secrets.token_hex(16)
    acquired = await cache.acquire_lock(lock_key, lock_token, REFRESH_LOCK_TTL_MS)
    if acquired is False:
        tokens = await await_refreshed_tokens(user_id, GROK_PROVIDER)
        if tokens:
            return {
                "access_token": tokens["access_token"],
                "account_id": tokens["account_id"],
                "plan_type": tokens.get("plan_type"),
            }
        return None

    try:
        # The token snapshot above predates the distributed lock. Another
        # worker may have rotated it and released the lock while this request
        # was waiting to acquire it; always re-read under ownership.
        latest = await get_oauth_tokens(user_id, GROK_PROVIDER)
        if not latest:
            return None
        tokens = latest
        now = datetime.now(timezone.utc)
        if token_expiry(tokens) > now + REFRESH_BUFFER:
            return {
                "access_token": tokens["access_token"],
                "account_id": tokens["account_id"],
                "plan_type": tokens.get("plan_type"),
            }

        new = await refresh_tokens(tokens["refresh_token"])
        claims = parse_jwt_claims(new["id_token"])
        expires_at = now + timedelta(seconds=new["expires_in"])
        account_id = claims["account_id"] or tokens["account_id"]

        await upsert_oauth_tokens(
            user_id=user_id,
            provider=GROK_PROVIDER,
            access_token=new["access_token"],
            refresh_token=new["refresh_token"],
            account_id=account_id,
            email=claims.get("email") or tokens.get("email"),
            plan_type=tokens.get("plan_type"),
            expires_at=expires_at,
        )
        try:
            await invalidate_oauth_active_cache(user_id)
        except Exception:
            pass
        logger.debug(f"[grok_oauth] Refreshed tokens for user_id={user_id}")
        return {
            "access_token": new["access_token"],
            "account_id": account_id,
            "plan_type": tokens.get("plan_type"),
        }
    except Exception as e:
        logger.error(f"[grok_oauth] Token refresh failed for user_id={user_id}: {e}")
        return None
    finally:
        if acquired:
            await cache.release_lock(lock_key, lock_token)


def __getattr__(name: str):
    if name == "GROK_CLIENT_ID":
        return grok_client_id()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
