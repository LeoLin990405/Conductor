"""Gemini OAuth service — Google account sign-in for Gemini Code Assist.

Connects Gemini models through a user's own Google account, using the same
installed-app OAuth client as Google's first-party Gemini CLI (a public client;
Google documents installed-app secrets as embeddable). The authorize endpoint
redirects to Google's hosted ``/authcode`` page, which prints a code the user
pastes back — the same hand-off the Claude connection uses, so no callback
server has to be reachable from the user's browser.

After the token exchange the account has to be set up for Code Assist: a
``loadCodeAssist`` call names the tier and managed project, and a first-time
free-tier account is onboarded through a long-running operation.
"""

import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse, parse_qs

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

GEMINI_PROVIDER = "gemini-oauth"

# OAuth client for the Code Assist sign-in. Its id and secret are not shipped:
# set CONDUCTOR_GEMINI_OAUTH_CLIENT_ID and CONDUCTOR_GEMINI_OAUTH_CLIENT_SECRET.
GEMINI_CLIENT_ID_ENV = "CONDUCTOR_GEMINI_OAUTH_CLIENT_ID"


def gemini_client_id() -> str:
    """Gemini OAuth client setting from ``CONDUCTOR_GEMINI_OAUTH_CLIENT_ID`` (not shipped; no default)."""
    from src.server.services.oauth_client_config import require

    return require(GEMINI_CLIENT_ID_ENV, "Gemini")


GEMINI_CLIENT_SECRET_ENV = "CONDUCTOR_GEMINI_OAUTH_CLIENT_SECRET"


def gemini_client_secret() -> str:
    """Gemini OAuth client setting from ``CONDUCTOR_GEMINI_OAUTH_CLIENT_SECRET`` (not shipped; no default)."""
    from src.server.services.oauth_client_config import require

    return require(GEMINI_CLIENT_SECRET_ENV, "Gemini")


GEMINI_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
    "https://www.googleapis.com/auth/aicode",
    "openid",
)
GEMINI_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/auth"
GEMINI_TOKEN_URL = "https://oauth2.googleapis.com/token"
GEMINI_REDIRECT_URI = "https://antigravity.google/oauth-callback"

CODE_ASSIST_ENDPOINT = "https://cloudcode-pa.googleapis.com"
CODE_ASSIST_VERSION = "v1internal"
# Google retired this flow for consumer accounts (including Gemini CLI) on
# 2026-06-18; Standard/Enterprise subscriptions follow a separate schedule.
GEMINI_CONSUMER_DEPRECATION_URL = (
    "https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals"
)
ANTIGRAVITY_MIGRATION_URL = "https://antigravity.google/docs/gcli-migration"
GEMINI_API_KEY_URL = "https://aistudio.google.com/apikey"
_CLIENT_METADATA = {
    "ideType": "ANTIGRAVITY",
    "platform": "PLATFORM_UNSPECIFIED",
    "pluginType": "GEMINI",
}
# Antigravity's fallback managed project, used when load/onboard returns a tier
# but no project (the same fallback its CLI uses).
ANTIGRAVITY_DEFAULT_PROJECT = "rising-fact-p41fc"


class GeminiCodeAssistIneligible(RuntimeError):
    """The account cannot use Code Assist with this OAuth client.

    Kept distinct from transport errors: the UI can tell the user that Google
    retired consumer Gemini Code Assist (or that a tier is unavailable) instead
    of suggesting another sign-in that would fail the same way.
    """

    def __init__(self, reason_code: str, reason_message: str) -> None:
        self.reason_code = reason_code
        self.reason_message = reason_message
        super().__init__(
            reason_message
            or "This Google account is not eligible for Gemini Code Assist."
        )


class GeminiProjectRequired(RuntimeError):
    """The account needs a Google Cloud project before Code Assist can run."""

    def __init__(self) -> None:
        super().__init__(
            "This Google account has no Gemini Code Assist project. "
            "Set GOOGLE_CLOUD_PROJECT (or GOOGLE_CLOUD_PROJECT_ID) to a Google "
            "Cloud project with Code Assist enabled, then sign in again."
        )


# --- PKCE ---------------------------------------------------------------

def generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(verifier: str, challenge: str) -> str:
    params = {
        "client_id": gemini_client_id(),
        "redirect_uri": GEMINI_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GEMINI_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": verifier,
    }
    return f"{GEMINI_AUTHORIZE_URL}?{urlencode(params)}"


def generate_authorize_url() -> tuple[str, str]:
    verifier, challenge = generate_pkce_pair()
    return build_authorize_url(verifier, challenge), verifier


def parse_callback_input(raw: str) -> str:
    """Accept a full redirect URL, ``code=...``, or a bare authorization code."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("No authorization code provided")
    if "code=" in text or text.startswith("http"):
        query = urlparse(text).query if text.startswith("http") else text
        params = parse_qs(query)
        codes = params.get("code")
        if codes:
            return codes[0]
        raise ValueError("The pasted URL does not contain an authorization code")
    return text


# --- Token exchange / refresh ------------------------------------------

def _token_bundle(data: dict) -> dict:
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "id_token": data.get("id_token", ""),
        "expires_in": int(data.get("expires_in") or 3600),
    }


async def exchange_code(code: str, verifier: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            GEMINI_TOKEN_URL,
            data={
                "code": code,
                "client_id": gemini_client_id(),
                "client_secret": gemini_client_secret(),
                "redirect_uri": GEMINI_REDIRECT_URI,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        resp.raise_for_status()
        return _token_bundle(resp.json())


async def refresh_tokens(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            GEMINI_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": gemini_client_id(),
                "client_secret": gemini_client_secret(),
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        resp.raise_for_status()
        return _token_bundle(resp.json())


def parse_jwt_claims(token: str) -> dict:
    if not token:
        return {"account_id": "", "email": None, "exp": None}
    parts = token.split(".")
    if len(parts) < 2:
        return {"account_id": "", "email": None, "exp": None}
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except Exception:
        return {"account_id": "", "email": None, "exp": None}
    return {
        "account_id": payload.get("sub") or "",
        "email": payload.get("email"),
        "exp": payload.get("exp"),
    }


# --- Code Assist project/tier setup ------------------------------------

def _ca_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "antigravity/1.0.0 (gzip)",
        "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
        "Client-Metadata": json.dumps(_CLIENT_METADATA),
    }


def _configured_project_id() -> str:
    """A project supplied by the deployment, as Gemini CLI also accepts it."""
    import os

    return (
        os.getenv("GEMINI_PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or ""
    ).strip()


def _project_id(value: object) -> str:
    """Normalize the several project shapes this API returns."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("id") or "").strip()
    return ""


async def _post_ca(method: str, access_token: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{CODE_ASSIST_ENDPOINT}/{CODE_ASSIST_VERSION}:{method}",
            json=body,
            headers=_ca_headers(access_token),
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Code Assist {method} failed ({resp.status_code}): {resp.text[:300]}"
            )
        return resp.json()


async def setup_code_assist(access_token: str, project_id: str | None = None) -> dict:
    """Resolve the account's tier and managed project, onboarding it if needed.

    ``project_id`` wins over the deployment's ``GEMINI_PROJECT_ID`` /
    ``GOOGLE_CLOUD_PROJECT`` fallback; it is the same escape hatch Gemini CLI
    documents for accounts whose tier requires a Google Cloud project.
    """
    configured_project = (project_id or _configured_project_id()).strip()
    load_body: dict = {"metadata": _CLIENT_METADATA}
    if configured_project:
        load_body["cloudaicompanionProject"] = configured_project
    load = await _post_ca("loadCodeAssist", access_token, load_body)
    project = _project_id(load.get("cloudaicompanionProject")) or configured_project
    current = load.get("currentTier") or {}
    paid = load.get("paidTier") or {}
    tier_id = paid.get("id") or current.get("id")
    tier_name = paid.get("name") or current.get("name")

    ineligible = [
        tier for tier in (load.get("ineligibleTiers") or []) if isinstance(tier, dict)
    ]
    if ineligible and not current:
        unsupported = next(
            (tier for tier in ineligible if tier.get("reasonCode") == "UNSUPPORTED_CLIENT"),
            None,
        )
        chosen = unsupported or ineligible[0]
        raise GeminiCodeAssistIneligible(
            str(chosen.get("reasonCode") or "INELIGIBLE_TIER"),
            str(chosen.get("reasonMessage") or ""),
        )

    if project:
        return {"project": project, "tier": tier_id or "standard", "tier_name": tier_name}
    if tier_id:
        # Antigravity reports a tier but sometimes omits the managed project;
        # its CLI falls back to this project rather than treating the account
        # as disconnected.
        return {
            "project": configured_project or ANTIGRAVITY_DEFAULT_PROJECT,
            "tier": tier_id,
            "tier_name": tier_name,
        }

    allowed = load.get("allowedTiers") or []
    onboard_tier = next((t for t in allowed if t.get("isDefault")), None) or (allowed[0] if allowed else None)
    tier_id = tier_id or (onboard_tier or {}).get("id") or "standard"
    tier_name = tier_name or (onboard_tier or {}).get("name")

    # Free tier uses a Google-managed project: sending a project makes onboarding fail.
    onboard_body: dict = {"tierId": tier_id, "metadata": _CLIENT_METADATA}
    operation = await _post_ca("onboardUser", access_token, onboard_body)

    deadline = datetime.now(timezone.utc) + timedelta(seconds=120)
    while not operation.get("done") and operation.get("name") and datetime.now(timezone.utc) < deadline:
        import asyncio

        await asyncio.sleep(5)
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                f"{CODE_ASSIST_ENDPOINT}/{CODE_ASSIST_VERSION}/{operation['name']}",
                headers=_ca_headers(access_token),
            )
            resp.raise_for_status()
            operation = resp.json()

    project = _project_id((operation.get("response") or {}).get("cloudaicompanionProject")) or configured_project
    if not project:
        # Antigravity hands consumer accounts a managed default project; keep
        # the same fallback instead of failing after a successful onboarding.
        project = ANTIGRAVITY_DEFAULT_PROJECT
    return {"project": project, "tier": tier_id, "tier_name": tier_name}


# --- Orchestrator ------------------------------------------------------

async def get_valid_token(user_id: str) -> dict | None:
    """A valid access token plus the Code Assist project/tier for this account."""
    tokens = await get_oauth_tokens(user_id, GEMINI_PROVIDER)
    if not tokens:
        return None

    now = datetime.now(timezone.utc)
    async def _store(stored: dict) -> None:
        await upsert_oauth_tokens(
            user_id=user_id,
            provider=GEMINI_PROVIDER,
            access_token=stored["access_token"],
            refresh_token=stored["refresh_token"],
            account_id=stored.get("account_id") or "",
            email=stored.get("email"),
            plan_type=stored.get("plan_type"),
            expires_at=token_expiry(stored),
        )
        try:
            await invalidate_oauth_active_cache(user_id)
        except Exception:
            pass

    async def _ensure_setup(stored: dict) -> dict:
        if not stored.get("account_id"):
            setup = await setup_code_assist(stored["access_token"])
            stored = {**stored, "account_id": setup["project"], "plan_type": setup["tier"]}
            await _store(stored)
        return {
            "access_token": stored["access_token"],
            "project": stored["account_id"],
            "tier": stored.get("plan_type"),
            "email": stored.get("email"),
        }

    if token_expiry(tokens) > now + REFRESH_BUFFER:
        try:
            return await _ensure_setup(tokens)
        except Exception as e:
            logger.error(f"[gemini_oauth] Code Assist setup failed for user_id={user_id}: {e}")
            return None

    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    lock_key = f"oauth:refresh:{user_id}:{GEMINI_PROVIDER}"
    lock_token = secrets.token_hex(16)
    acquired = await cache.acquire_lock(lock_key, lock_token, REFRESH_LOCK_TTL_MS)
    if acquired is False:
        refreshed = await await_refreshed_tokens(user_id, GEMINI_PROVIDER)
        if not refreshed:
            return None
        try:
            return await _ensure_setup(refreshed)
        except Exception as e:
            logger.error(f"[gemini_oauth] Code Assist setup after refresh wait failed: {e}")
            return None

    try:
        if acquired:
            # A preceding holder can commit between our first read and SET NX.
            # Re-read under the lock before consuming a rotated refresh token.
            tokens = await get_oauth_tokens(user_id, GEMINI_PROVIDER)
            if not tokens:
                return None
            if token_expiry(tokens) > datetime.now(timezone.utc) + REFRESH_BUFFER:
                return await _ensure_setup(tokens)
        new = await refresh_tokens(tokens["refresh_token"])
        claims = parse_jwt_claims(new["id_token"])
        stored = {
            **tokens,
            "access_token": new["access_token"],
            "refresh_token": new["refresh_token"] or tokens["refresh_token"],
            "email": claims.get("email") or tokens.get("email"),
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=new["expires_in"]),
        }
        # Setup can fail or outlive the refresh lock. Preserve rotated secrets
        # before it starts so later requests can retry with the new credentials.
        await _store(stored)
        try:
            return await _ensure_setup(stored)
        except Exception as e:
            logger.error(f"[gemini_oauth] Code Assist setup after refresh failed: {e}")
            return None
    except Exception as e:
        logger.error(f"[gemini_oauth] Token refresh failed for user_id={user_id}: {e}")
        return None
    finally:
        if acquired:
            await cache.release_lock(lock_key, lock_token)


def __getattr__(name: str):
    if name == "GEMINI_CLIENT_ID":
        return gemini_client_id()
    if name == "GEMINI_CLIENT_SECRET":
        return gemini_client_secret()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
