"""
OAuth Router — Connect external OAuth providers (ChatGPT Codex, Claude).

Codex — Device Code Flow (RFC 8628):
- POST   /api/v1/oauth/codex/device/initiate — Start device code flow
- POST   /api/v1/oauth/codex/device/poll     — Poll for user approval
- GET    /api/v1/oauth/codex/status           — Check connection status
- DELETE /api/v1/oauth/codex                  — Disconnect (delete tokens)

Claude — PKCE Authorization Code Flow:
- POST   /api/v1/oauth/claude/initiate        — Generate PKCE + authorize URL
- POST   /api/v1/oauth/claude/callback        — Exchange code#state for tokens
- GET    /api/v1/oauth/claude/status           — Check connection status
- DELETE /api/v1/oauth/claude                  — Disconnect (delete tokens)

Kimi Code — Device Code Flow:
- POST   /api/v1/oauth/kimi/device/initiate    — Start device code flow
- POST   /api/v1/oauth/kimi/device/poll        — Poll for user approval
- GET    /api/v1/oauth/kimi/status              — Check connection status
- DELETE /api/v1/oauth/kimi                     — Disconnect (delete tokens)

Zhipu (Z.ai / BigModel) — Server-mediated CLI flow + browser redirect:
- POST   /api/v1/oauth/zhipu/initiate           — Start a zai or bigmodel sign-in
- POST   /api/v1/oauth/zhipu/poll               — Advance / report the pending flow
- POST   /api/v1/oauth/zhipu/callback           — Exchange the BigModel callback code
- GET    /api/v1/oauth/zhipu/status              — Check connection status
- DELETE /api/v1/oauth/zhipu                     — Disconnect (delete both regions)

MiMo — Paste-code flow (platform X25519 + AES-GCM envelope):
- POST   /api/v1/oauth/mimo/initiate            — Start a login; returns the authorize URL
- POST   /api/v1/oauth/mimo/callback            — Decrypt the pasted code, store the minted key
- GET    /api/v1/oauth/mimo/status              — Check connection status (credential presence)
- DELETE /api/v1/oauth/mimo                     — Disconnect (delete the minted key)

Capability catalog:
- GET    /api/v1/oauth/providers                — Connection descriptors for the UI
"""

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.server.utils.api import CurrentUserId
from src.server.services.codex_oauth import (
    CODEX_PROVIDER,
    CODEX_DEVICE_VERIFY_URL,
    exchange_device_code,
    parse_jwt_claims,
    poll_device_authorization,
    request_device_code,
)
from src.server.services.claude_oauth import (
    CLAUDE_PROVIDER,
    exchange_code as claude_exchange_code,
    generate_authorize_url as claude_generate_authorize_url,
    parse_callback_input as claude_parse_callback_input,
)
from src.server.services import (
    deepseek_oauth,
    gemini_oauth,
    grok_oauth,
    kimi_oauth,
    mimo_oauth,
    oauth_catalog,
    zai_oauth,
)
from src.server.database.oauth_tokens import (
    delete_oauth_tokens,
    get_oauth_status,
    invalidate_oauth_active_cache,
    upsert_oauth_tokens,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/oauth", tags=["OAuth"])


# ─── Device Code: Initiate ────────────────────────────────────────────────────

@router.post("/codex/device/initiate")
async def codex_device_initiate(user_id: CurrentUserId):
    """Start device code flow. Returns user_code + verification URL.

    The frontend should:
    1. Display the user_code prominently
    2. Open verification_url in a new tab
    3. Start polling /device/poll every `interval` seconds
    """
    from src.server.services import oauth_client_config

    oauth_client_config.require_for_route("codex")
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise HTTPException(status_code=503, detail="Cache unavailable for OAuth")

    try:
        device = await request_device_code()
    except Exception as e:
        logger.error(f"[oauth] Device code request failed: {e}")
        raise HTTPException(status_code=502, detail="Failed to request device code from OpenAI")

    # Store device_auth_id + user_code in Redis (15-min TTL matching OpenAI's expiry)
    await cache.client.set(
        f"oauth:device:{user_id}",
        json.dumps({
            "device_auth_id": device["device_auth_id"],
            "user_code": device["user_code"],
        }),
        ex=900,
    )

    logger.info(f"[oauth] Device code initiated for user_id={user_id}")
    return {
        "user_code": device["user_code"],
        "verification_url": CODEX_DEVICE_VERIFY_URL,
        "interval": device["interval"],
    }


# ─── Device Code: Poll ────────────────────────────────────────────────────────

@router.post("/codex/device/poll")
async def codex_device_poll(user_id: CurrentUserId):
    """Poll for device authorization.

    Returns:
        {pending: true} if user hasn't approved yet
        {success: true, email, plan_type, account_id} on approval
    """
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise HTTPException(status_code=503, detail="Cache unavailable")

    raw = await cache.client.get(f"oauth:device:{user_id}")
    if not raw:
        raise HTTPException(status_code=400, detail="No pending device authorization. Please initiate again.")

    device = json.loads(raw)

    try:
        result = await poll_device_authorization(device["device_auth_id"], device["user_code"])
    except Exception as e:
        logger.error(f"[oauth] Device poll error for user_id={user_id}: {e}")
        raise HTTPException(status_code=502, detail="Failed to poll OpenAI")

    if result is None:
        return {"pending": True}

    # User approved — exchange authorization code for tokens
    try:
        tokens = await exchange_device_code(result["authorization_code"], result["code_verifier"])

        # Parse JWT claims
        claims = parse_jwt_claims(tokens.get("id_token", ""))
        if not claims.get("account_id"):
            at_claims = parse_jwt_claims(tokens.get("access_token", ""))
            if at_claims.get("account_id"):
                claims["account_id"] = at_claims["account_id"]

        exp_ts = claims.get("exp")
        expires_at = (
            datetime.fromtimestamp(exp_ts, tz=timezone.utc)
            if exp_ts
            else datetime.now(timezone.utc) + timedelta(hours=1)
        )

        await upsert_oauth_tokens(
            user_id=user_id,
            provider=CODEX_PROVIDER,
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            account_id=claims.get("account_id", ""),
            email=claims.get("email"),
            plan_type=claims.get("plan_type"),
            expires_at=expires_at,
        )

        try:
            await invalidate_oauth_active_cache(user_id)
        except Exception:
            pass

        # Clean up Redis
        await cache.client.delete(f"oauth:device:{user_id}")

        logger.info(
            f"[oauth] Codex connected for user_id={user_id} "
            f"plan={claims.get('plan_type')}"
        )
        return {
            "success": True,
            "email": claims.get("email"),
            "plan_type": claims.get("plan_type"),
            "account_id": claims.get("account_id", ""),
        }

    except Exception as e:
        logger.error(f"[oauth] Device code exchange failed for user_id={user_id}: {e}")
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {e}")


# ─── Status ──────────────────────────────────────────────────────────────────

@router.get("/codex/status")
async def codex_status(user_id: CurrentUserId):
    """Return connection status (no token decryption — fast check)."""
    status = await get_oauth_status(user_id, CODEX_PROVIDER)
    return status


# ─── Disconnect ──────────────────────────────────────────────────────────────

@router.delete("/codex")
async def codex_disconnect(user_id: CurrentUserId):
    """Delete stored OAuth tokens."""
    await delete_oauth_tokens(user_id, CODEX_PROVIDER)
    try:
        await invalidate_oauth_active_cache(user_id)
    except Exception:
        pass
    logger.info(f"[oauth] Codex disconnected for user_id={user_id}")
    return {"success": True}



# ═══════════════════════════════════════════════════════════════════════════════
# xAI Grok OAuth — Device Code Flow (SuperGrok / X Premium)
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/grok/device/initiate")
async def grok_device_initiate(user_id: CurrentUserId):
    """Start the xAI device-code flow; the user approves a code on accounts.x.ai."""
    from src.server.services import oauth_client_config

    oauth_client_config.require_for_route("grok")
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise HTTPException(status_code=503, detail="Cache unavailable for OAuth")

    try:
        device = await grok_oauth.request_device_code()
    except Exception as e:
        logger.error(f"[oauth] xAI device code request failed: {e}")
        raise HTTPException(status_code=502, detail="Failed to reach xAI sign-in")

    await cache.client.set(
        f"oauth:device:grok:{user_id}",
        json.dumps({"device_code": device["device_code"], "user_code": device["user_code"]}),
        ex=device.get("expires_in", 1800),
    )
    logger.info(f"[oauth] Grok device code initiated for user_id={user_id}")
    return {
        "user_code": device["user_code"],
        "verification_url": device["verification_url"],
        "interval": device["interval"],
        "expires_in": device.get("expires_in", 1800),
    }


@router.post("/grok/device/poll")
async def grok_device_poll(user_id: CurrentUserId):
    """Poll the xAI device authorization; stores tokens on approval."""
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise HTTPException(status_code=503, detail="Cache unavailable")

    raw = await cache.client.get(f"oauth:device:grok:{user_id}")
    if not raw:
        raise HTTPException(status_code=400, detail="No pending Grok authorization. Please initiate again.")
    if isinstance(raw, bytes):
        raw = raw.decode()
    device = json.loads(raw)

    try:
        tokens = await grok_oauth.poll_device_authorization(device["device_code"])
    except Exception as e:
        logger.error(f"[oauth] Grok device poll error for user_id={user_id}: {e}")
        raise HTTPException(status_code=502, detail="xAI sign-in failed; please try again")

    if tokens is None:
        return {"pending": True}

    claims = grok_oauth.parse_jwt_claims(tokens.get("id_token", ""))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=tokens["expires_in"])
    await upsert_oauth_tokens(
        user_id=user_id,
        provider=grok_oauth.GROK_PROVIDER,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        account_id=claims.get("account_id", ""),
        email=claims.get("email"),
        plan_type=claims.get("plan_type"),
        expires_at=expires_at,
    )
    try:
        await invalidate_oauth_active_cache(user_id)
    except Exception:
        pass
    await cache.client.delete(f"oauth:device:grok:{user_id}")
    logger.info(f"[oauth] Grok connected for user_id={user_id}")
    return {
        "success": True,
        "email": claims.get("email"),
        "account_id": claims.get("account_id", ""),
    }


@router.get("/grok/status")
async def grok_status(user_id: CurrentUserId):
    return await get_oauth_status(user_id, grok_oauth.GROK_PROVIDER)


@router.delete("/grok")
async def grok_disconnect(user_id: CurrentUserId):
    await delete_oauth_tokens(user_id, grok_oauth.GROK_PROVIDER)
    try:
        await invalidate_oauth_active_cache(user_id)
    except Exception:
        pass
    logger.info(f"[oauth] Grok disconnected for user_id={user_id}")
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════════════════
# Claude OAuth — PKCE Authorization Code Flow
# ═══════════════════════════════════════════════════════════════════════════════

class ClaudeCallbackRequest(BaseModel):
    callback_input: str


# ─── Initiate ────────────────────────────────────────────────────────────────

@router.post("/claude/initiate")
async def claude_initiate(user_id: CurrentUserId):
    """Generate PKCE pair and return authorize URL.

    Frontend should open the URL in a new tab. After the user authorizes,
    they'll see a code on Anthropic's callback page which they paste back
    via POST /claude/callback.
    """
    from src.server.services import oauth_client_config

    oauth_client_config.require_for_route("claude")
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise HTTPException(status_code=503, detail="Cache unavailable for OAuth")

    authorize_url, verifier = claude_generate_authorize_url()

    # Store verifier in Redis (10-min TTL)
    await cache.client.set(
        f"oauth:claude:{user_id}",
        verifier,
        ex=600,
    )

    logger.info(f"[oauth] Claude OAuth initiated for user_id={user_id}")
    return {"authorize_url": authorize_url}


# ─── Callback ────────────────────────────────────────────────────────────────

@router.post("/claude/callback")
async def claude_callback(user_id: CurrentUserId, body: ClaudeCallbackRequest):
    """Exchange callback code#state for tokens.

    Accepts various input formats:
    - Full URL: https://console.anthropic.com/oauth/code/callback?code=X&state=Y
    - code#state
    - code=X&state=Y
    """
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise HTTPException(status_code=503, detail="Cache unavailable")

    # Retrieve stored verifier
    verifier = await cache.client.get(f"oauth:claude:{user_id}")
    if not verifier:
        raise HTTPException(
            status_code=400,
            detail="No pending Claude authorization. Please initiate again.",
        )
    if isinstance(verifier, bytes):
        verifier = verifier.decode()

    # Parse callback input
    try:
        code, state = claude_parse_callback_input(body.callback_input)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Validate state = verifier (Anthropic's PKCE convention)
    if state != verifier:
        raise HTTPException(status_code=400, detail="State mismatch — possible CSRF. Please try again.")

    # Exchange code for tokens
    try:
        tokens = await claude_exchange_code(code, state, verifier)

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=tokens.get("expires_in", 3600))

        await upsert_oauth_tokens(
            user_id=user_id,
            provider=CLAUDE_PROVIDER,
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            account_id="",  # Anthropic tokens don't include account_id in JWT
            email=None,
            plan_type=None,
            expires_at=expires_at,
        )

        try:
            await invalidate_oauth_active_cache(user_id)
        except Exception:
            pass

        # Clean up Redis
        await cache.client.delete(f"oauth:claude:{user_id}")

        logger.info(f"[oauth] Claude connected for user_id={user_id}")
        return {"success": True}

    except Exception as e:
        logger.error(f"[oauth] Claude token exchange failed for user_id={user_id}: {e}")
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {e}")


# ─── Status ──────────────────────────────────────────────────────────────────

@router.get("/claude/status")
async def claude_status(user_id: CurrentUserId):
    """Return Claude connection status (no token decryption — fast check)."""
    status = await get_oauth_status(user_id, CLAUDE_PROVIDER)
    return status


# ─── Disconnect ──────────────────────────────────────────────────────────────

@router.delete("/claude")
async def claude_disconnect(user_id: CurrentUserId):
    """Delete stored Claude OAuth tokens."""
    await delete_oauth_tokens(user_id, CLAUDE_PROVIDER)
    try:
        await invalidate_oauth_active_cache(user_id)
    except Exception:
        pass
    logger.info(f"[oauth] Claude disconnected for user_id={user_id}")
    return {"success": True}

# ═══════════════════════════════════════════════════════════════════════════════
# Gemini OAuth — Google account sign-in (Gemini Code Assist)
# ═══════════════════════════════════════════════════════════════════════════════


class GeminiCallbackRequest(BaseModel):
    callback_input: str


@router.post("/gemini/initiate")
async def gemini_initiate(user_id: CurrentUserId):
    """Return the Google authorize URL; the user pastes back the code page."""
    from src.server.services import oauth_client_config

    oauth_client_config.require_for_route("gemini")
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise HTTPException(status_code=503, detail="Cache unavailable for OAuth")

    authorize_url, verifier = gemini_oauth.generate_authorize_url()
    await cache.client.set(f"oauth:gemini:{user_id}", verifier, ex=600)
    logger.info(f"[oauth] Gemini OAuth initiated for user_id={user_id}")
    return {"authorize_url": authorize_url}


@router.post("/gemini/callback")
async def gemini_callback(user_id: CurrentUserId, body: GeminiCallbackRequest):
    """Exchange the pasted Google code, then set the account up for Code Assist."""
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise HTTPException(status_code=503, detail="Cache unavailable")

    verifier = await cache.client.get(f"oauth:gemini:{user_id}")
    if not verifier:
        raise HTTPException(status_code=400, detail="No pending Gemini authorization. Please initiate again.")
    if isinstance(verifier, bytes):
        verifier = verifier.decode()

    try:
        code = gemini_oauth.parse_callback_input(body.callback_input)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        tokens = await gemini_oauth.exchange_code(code, verifier)
        claims = gemini_oauth.parse_jwt_claims(tokens.get("id_token", ""))
        setup = await gemini_oauth.setup_code_assist(tokens["access_token"])
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=tokens["expires_in"])
        await upsert_oauth_tokens(
            user_id=user_id,
            provider=gemini_oauth.GEMINI_PROVIDER,
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            account_id=setup["project"],
            email=claims.get("email"),
            plan_type=setup["tier"],
            expires_at=expires_at,
        )
        try:
            await invalidate_oauth_active_cache(user_id)
        except Exception:
            pass
        await cache.client.delete(f"oauth:gemini:{user_id}")
        logger.info(f"[oauth] Gemini connected for user_id={user_id} tier={setup['tier']}")
        return {
            "success": True,
            "email": claims.get("email"),
            "plan_type": setup["tier"],
            "tier_name": setup.get("tier_name"),
        }
    except gemini_oauth.GeminiCodeAssistIneligible as e:
        logger.info(
            "[oauth] Gemini Code Assist account ineligible for user_id=%s: %s",
            user_id,
            e.reason_code,
        )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "gemini_ineligible",
                "reason_code": e.reason_code,
                "message": (
                    "This Google account is not eligible for Antigravity / Gemini "
                    f"Code Assist: {e.reason_message}. Check your Antigravity access "
                    "or use a Gemini API key."
                ),
                "docs_url": gemini_oauth.ANTIGRAVITY_MIGRATION_URL,
                "migration_url": gemini_oauth.ANTIGRAVITY_MIGRATION_URL,
                "api_key_url": gemini_oauth.GEMINI_API_KEY_URL,
            },
        )
    except gemini_oauth.GeminiProjectRequired as e:
        logger.info("[oauth] Gemini Code Assist project required for user_id=%s", user_id)
        raise HTTPException(
            status_code=400,
            detail={
                "code": "gemini_project_required",
                "message": str(e),
                "docs_url": gemini_oauth.GEMINI_CONSUMER_DEPRECATION_URL,
            },
        )
    except Exception as e:
        logger.error(f"[oauth] Gemini token exchange failed for user_id={user_id}: {e}")
        raise HTTPException(status_code=400, detail=f"Gemini sign-in failed: {e}")


@router.get("/gemini/status")
async def gemini_status(user_id: CurrentUserId):
    return await get_oauth_status(user_id, gemini_oauth.GEMINI_PROVIDER)


@router.delete("/gemini")
async def gemini_disconnect(user_id: CurrentUserId):
    await delete_oauth_tokens(user_id, gemini_oauth.GEMINI_PROVIDER)
    try:
        await invalidate_oauth_active_cache(user_id)
    except Exception:
        pass
    logger.info(f"[oauth] Gemini disconnected for user_id={user_id}")
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════════════════
# Kimi Code OAuth — device-code flow (subscription sign-in)
# ═══════════════════════════════════════════════════════════════════════════════

# Pending flows live in Redis so any worker can serve the next poll of a
# multi-worker deployment. Each route below checks cache availability first and
# fails with 503 rather than keeping per-process state that another worker
# would not see.
_KIMI_DEVICE_KEY_TTL = 1800


def _safe_error(exc: Exception) -> str:
    """Exception type and a short message for logs.

    Upstream bodies are never logged: an auth endpoint's error payload can echo
    credential detail, so only our own sanitized messages and the exception
    type are safe to write down.
    """
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text[:300]}"


async def _require_oauth_cache():
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise HTTPException(status_code=503, detail="Cache unavailable for OAuth")
    return cache


@router.post("/kimi/device/initiate")
async def kimi_device_initiate(user_id: CurrentUserId):
    """Start the Kimi Code device flow; the user approves a code at auth.kimi.com."""
    from src.server.services import oauth_client_config

    oauth_client_config.require_for_route("kimi")
    cache = await _require_oauth_cache()

    try:
        device = await kimi_oauth.request_device_code()
    except Exception as e:
        logger.error(f"[oauth] Kimi device code request failed: {_safe_error(e)}")
        raise HTTPException(status_code=502, detail="Failed to reach Kimi sign-in")

    await cache.client.set(
        f"oauth:device:kimi:{user_id}",
        json.dumps({"device_code": device["device_code"], "user_code": device["user_code"]}),
        ex=device.get("expires_in", _KIMI_DEVICE_KEY_TTL),
    )
    logger.info(f"[oauth] Kimi device code initiated for user_id={user_id}")
    return {
        "user_code": device["user_code"],
        "verification_url": device["verification_url"],
        "interval": device["interval"],
        "expires_in": device.get("expires_in", _KIMI_DEVICE_KEY_TTL),
    }


@router.post("/kimi/device/poll")
async def kimi_device_poll(user_id: CurrentUserId):
    """Poll the Kimi device authorization; stores tokens on approval."""
    cache = await _require_oauth_cache()

    raw = await cache.client.get(f"oauth:device:kimi:{user_id}")
    if not raw:
        raise HTTPException(
            status_code=400, detail="No pending Kimi authorization. Please initiate again."
        )
    if isinstance(raw, bytes):
        raw = raw.decode()
    device = json.loads(raw)

    try:
        tokens = await kimi_oauth.poll_device_authorization(device["device_code"])
    except Exception as e:
        logger.error(f"[oauth] Kimi device poll error for user_id={user_id}: {_safe_error(e)}")
        raise HTTPException(status_code=502, detail="Kimi sign-in failed; please try again")

    if tokens is None:
        return {"pending": True}

    try:
        result = await kimi_oauth.save_device_tokens(user_id, tokens)
    except Exception as e:
        logger.error(f"[oauth] Kimi token store failed for user_id={user_id}: {_safe_error(e)}")
        raise HTTPException(status_code=500, detail="Kimi sign-in could not be saved")

    await cache.client.delete(f"oauth:device:kimi:{user_id}")
    logger.info(f"[oauth] Kimi connected for user_id={user_id}")
    return {"success": True, "email": result["email"], "account_id": result["account_id"]}


@router.get("/kimi/status")
async def kimi_status(user_id: CurrentUserId):
    return await get_oauth_status(user_id, kimi_oauth.KIMI_PROVIDER)


@router.delete("/kimi")
async def kimi_disconnect(user_id: CurrentUserId):
    await delete_oauth_tokens(user_id, kimi_oauth.KIMI_PROVIDER)
    try:
        await invalidate_oauth_active_cache(user_id)
    except Exception:
        pass
    logger.info(f"[oauth] Kimi disconnected for user_id={user_id}")
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════════════════
# Zhipu OAuth — Z.ai server-mediated CLI flow / BigModel browser redirect
# ═══════════════════════════════════════════════════════════════════════════════

# The upstream flow itself lives 15 min; the cached flow state matches it.
_ZHIPU_FLOW_TTL = 900


def _zhipu_flow_key(user_id: str) -> str:
    return f"oauth:zhipu:{user_id}"


class ZhipuInitiateRequest(BaseModel):
    provider: str = zai_oauth.ZAI_FLOW
    redirect_uri: str | None = None


class ZhipuPollRequest(BaseModel):
    state: str | None = None


class ZhipuCallbackRequest(BaseModel):
    code: str
    state: str
    redirect_uri: str


@router.post("/zhipu/initiate")
async def zhipu_initiate(user_id: CurrentUserId, body: ZhipuInitiateRequest):
    """Start a Zhipu sign-in.

    ``zai`` is server-mediated: the flow and its poll token stay server-side
    and POST /zhipu/poll advances it. ``bigmodel`` requires a loopback
    ``redirect_uri``; the browser returns the auth code there and the callback
    page posts it back.
    """
    cache = await _require_oauth_cache()
    flow_kind = (body.provider or "").strip().lower()

    if flow_kind == zai_oauth.ZAI_FLOW:
        try:
            flow = await zai_oauth.initiate_cli_flow()
        except zai_oauth.ZhipuOAuthError as e:
            logger.error(f"[oauth] Z.ai flow init failed for user_id={user_id}: {e}")
            raise HTTPException(status_code=502, detail=str(e))
        state = secrets.token_hex(32)
        await cache.client.set(
            _zhipu_flow_key(user_id),
            json.dumps(
                {
                    "provider": zai_oauth.ZAI_FLOW,
                    "flow_id": flow["flow_id"],
                    "poll_token": flow["poll_token"],
                    "state": state,
                    "expires_at": flow.get("expires_at"),
                }
            ),
            ex=_ZHIPU_FLOW_TTL,
        )
        logger.info(f"[oauth] Z.ai flow initiated for user_id={user_id}")
        return {
            "authorize_url": flow["authorize_url"],
            "state": state,
            "interval": flow.get("interval", 2),
            "expires_in": _ZHIPU_FLOW_TTL,
        }

    if flow_kind == zai_oauth.BIGMODEL_FLOW:
        try:
            redirect_uri = zai_oauth.validate_loopback_redirect_uri(body.redirect_uri or "")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        state = secrets.token_hex(32)
        await cache.client.set(
            _zhipu_flow_key(user_id),
            json.dumps(
                {
                    "provider": zai_oauth.BIGMODEL_FLOW,
                    "redirect_uri": redirect_uri,
                    "state": state,
                }
            ),
            ex=_ZHIPU_FLOW_TTL,
        )
        logger.info(f"[oauth] BigModel flow initiated for user_id={user_id}")
        return {
            "authorize_url": zai_oauth.build_bigmodel_authorize_url(redirect_uri, state),
            "state": state,
            "expires_in": _ZHIPU_FLOW_TTL,
        }

    raise HTTPException(status_code=400, detail="Unsupported provider. Use 'zai' or 'bigmodel'.")


async def _load_zhipu_flow(cache, user_id: str) -> dict:
    raw = await cache.client.get(_zhipu_flow_key(user_id))
    if not raw:
        raise HTTPException(
            status_code=400, detail="No pending Zhipu authorization. Please initiate again."
        )
    if isinstance(raw, bytes):
        raw = raw.decode()
    return json.loads(raw)


@router.post("/zhipu/poll")
async def zhipu_poll(user_id: CurrentUserId, body: ZhipuPollRequest | None = None):
    """Advance a pending Zhipu sign-in.

    For ``zai`` this performs one server-side poll of the zcode flow (the
    frontend polls about every 3 s). For ``bigmodel`` it only reports whether
    the callback for this state has landed.
    """
    cache = await _require_oauth_cache()
    flow = await _load_zhipu_flow(cache, user_id)
    state = (body.state if body else None) or None

    if state and flow.get("state") and state != flow["state"]:
        raise HTTPException(
            status_code=400, detail="State mismatch — possible CSRF. Please try again."
        )

    if flow.get("status") == "ready":
        return {
            "success": True,
            "provider": flow.get("provider"),
            "email": flow.get("email"),
        }

    if flow.get("provider") != zai_oauth.ZAI_FLOW:
        return {"pending": True}

    try:
        data = await zai_oauth.poll_cli_flow(flow["flow_id"], flow["poll_token"])
    except zai_oauth.ZhipuOAuthError as e:
        logger.error(f"[oauth] Z.ai poll failed for user_id={user_id}: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.error(f"[oauth] Z.ai poll error for user_id={user_id}: {_safe_error(e)}")
        raise HTTPException(status_code=502, detail="Z.ai sign-in failed; please try again")

    status = data.get("status")
    if status == "pending":
        return {"pending": True}
    if status == "failed":
        await cache.client.delete(_zhipu_flow_key(user_id))
        reason = zai_oauth.sanitize_message(data.get("message") or data.get("msg"))
        logger.info(f"[oauth] Z.ai flow failed for user_id={user_id}")
        raise HTTPException(
            status_code=400,
            detail=f"Z.ai sign-in was not completed{f': {reason}' if reason else ''}",
        )
    if status != "ready":
        raise HTTPException(status_code=502, detail="Z.ai sign-in returned an unexpected state")

    try:
        result = await zai_oauth.complete_zai_login(user_id, data)
    except zai_oauth.ZhipuOAuthError as e:
        logger.error(f"[oauth] Z.ai credential mint failed for user_id={user_id}: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.error(f"[oauth] Z.ai credential store failed for user_id={user_id}: {_safe_error(e)}")
        raise HTTPException(status_code=502, detail="Z.ai sign-in could not be completed")

    await cache.client.delete(_zhipu_flow_key(user_id))
    logger.info(f"[oauth] Zhipu connected for user_id={user_id} provider=zai")
    return {"success": True, "provider": "zai", "email": result["email"]}


@router.post("/zhipu/callback")
async def zhipu_callback(user_id: CurrentUserId, body: ZhipuCallbackRequest):
    """Exchange the BigModel browser callback code for a minted coding-plan key.

    The stored redirect_uri is authoritative for the exchange — it is the one
    BigModel was sent at authorize time, and it was validated loopback at
    initiate; the body's copy is re-validated only as defence in depth.
    """
    cache = await _require_oauth_cache()
    flow = await _load_zhipu_flow(cache, user_id)

    if flow.get("provider") != zai_oauth.BIGMODEL_FLOW:
        raise HTTPException(
            status_code=400, detail="No pending BigModel authorization. Please initiate again."
        )
    if flow.get("status") == "ready":
        return {"success": True, "provider": "bigmodel", "email": flow.get("email")}
    if flow.get("state") != body.state:
        raise HTTPException(
            status_code=400, detail="State mismatch — possible CSRF. Please try again."
        )
    try:
        zai_oauth.validate_loopback_redirect_uri(body.redirect_uri)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    redirect_uri = flow.get("redirect_uri") or body.redirect_uri

    try:
        exchange = await zai_oauth.exchange_bigmodel_code(body.code, body.state, redirect_uri)
        result = await zai_oauth.complete_bigmodel_login(user_id, exchange)
    except zai_oauth.ZhipuOAuthError as e:
        logger.error(f"[oauth] BigModel callback failed for user_id={user_id}: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.error(f"[oauth] BigModel callback error for user_id={user_id}: {_safe_error(e)}")
        raise HTTPException(status_code=500, detail="BigModel sign-in could not be completed")

    # The tab that started the flow is polling /zhipu/poll: leave the completed
    # result where that poll reads it.
    await cache.client.set(
        _zhipu_flow_key(user_id),
        json.dumps(
            {
                "provider": zai_oauth.BIGMODEL_FLOW,
                "status": "ready",
                "state": body.state,
                "email": result["email"],
            }
        ),
        ex=_ZHIPU_FLOW_TTL,
    )
    logger.info(f"[oauth] Zhipu connected for user_id={user_id} provider=bigmodel")
    return {"success": True, "provider": "bigmodel", "email": result["email"]}


@router.get("/zhipu/status")
async def zhipu_status(user_id: CurrentUserId):
    return await zai_oauth.get_status(user_id)


@router.delete("/zhipu")
async def zhipu_disconnect(user_id: CurrentUserId):
    await zai_oauth.disconnect(user_id)
    logger.info(f"[oauth] Zhipu disconnected for user_id={user_id}")
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════════════════
# Provider capability catalog — one source of truth for the connection UI
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/providers")
async def oauth_providers(user_id: CurrentUserId):
    """Every provider connection the UI can offer, with its login capability.

    The descriptors are static (``oauth_catalog``): login kind, the API-key
    provider to fall back to, the models a login unlocks, and — for providers
    with no account login — the reason. The UI renders from this list instead of
    hard-coding cards, and never has to invent the unavailable copy.
    """
    from src.server.services.provider_connect_defaults import connect_defaults
    from src.server.services.provider_connect_options import options_for

    providers = oauth_catalog.describe_providers()
    for descriptor in providers:
        provider = descriptor.get("api_key_provider")
        descriptor["connect_defaults"] = connect_defaults(provider)
        descriptor["connect_options"] = options_for(
            str(descriptor.get("brand") or ""),
            provider,
        )
    return {"providers": providers}


# ═══════════════════════════════════════════════════════════════════════════════
# MiMo account login — paste-code flow (the platform mints a coding-plan key)
# ═══════════════════════════════════════════════════════════════════════════════

# One pending login per user, in Redis so any worker can serve the callback.
# Ten minutes is the platform page's practical lifetime; the key is consumed on
# the first callback, so no additional cleanup path is needed.
_MIMO_PENDING_TTL = mimo_oauth.PENDING_TTL_SECONDS


class MimoCallbackRequest(BaseModel):
    code: str


def _mimo_pending_key(user_id: str) -> str:
    return f"oauth:mimo:{user_id}"


async def _consume_mimo_pending(cache, user_id: str) -> dict:
    """Take the user's one pending MiMo login; missing means expired or used.

    Redis GETDEL is atomic, so two concurrent callbacks cannot both decrypt
    against the same private key. A client without GETDEL degrades to
    GET+DELETE (the same guarantee for sequential calls).
    """
    key = _mimo_pending_key(user_id)
    getdel = getattr(cache.client, "getdel", None)
    if getdel is not None:
        raw = await getdel(key)
    else:
        raw = await cache.client.get(key)
        if raw is not None:
            await cache.client.delete(key)
    if not raw:
        raise HTTPException(
            status_code=410, detail="MiMo login expired. Please start again."
        )
    try:
        if isinstance(raw, bytes):
            raw = raw.decode()
        pending = json.loads(raw)
    except (TypeError, ValueError):
        pending = None
    if not isinstance(pending, dict) or not isinstance(pending.get("private_key"), str):
        raise HTTPException(
            status_code=410, detail="MiMo login expired. Please start again."
        )
    return pending


@router.post("/mimo/initiate")
async def mimo_initiate(user_id: CurrentUserId):
    """Start a MiMo login; returns the authorize URL the user opens.

    The response carries no key material: the X25519 private half stays in the
    pending Redis state until the callback consumes it.
    """
    cache = await _require_oauth_cache()

    try:
        login = mimo_oauth.create_login()
    except Exception as e:
        logger.error(
            f"[oauth] MiMo login setup failed for user_id={user_id}: {_safe_error(e)}"
        )
        raise HTTPException(status_code=500, detail="MiMo login could not be started")

    await cache.client.set(
        _mimo_pending_key(user_id),
        json.dumps(
            {"private_key": login["private_key"], "key_name": login["key_name"]}
        ),
        ex=_MIMO_PENDING_TTL,
    )
    logger.info(f"[oauth] MiMo login initiated for user_id={user_id}")
    return {"authorize_url": login["authorize_url"]}


@router.post("/mimo/callback")
async def mimo_callback(user_id: CurrentUserId, body: MimoCallbackRequest):
    """Decrypt the pasted code and store the minted key as the MiMo credential.

    The length check runs before the pending state is consumed, so a truncated
    paste can be retried; the pending state is consumed before decryption, so a
    code cannot be replayed. No failure response or log line contains the code,
    the private key, or the minted key.
    """
    cache = await _require_oauth_cache()

    code = str(body.code or "").strip()
    if not code or len(code) > mimo_oauth.MAX_CODE_LENGTH:
        logger.info(f"[oauth] MiMo code rejected for user_id={user_id} (invalid length)")
        raise HTTPException(status_code=400, detail=mimo_oauth.CODE_INVALID_MESSAGE)

    pending = await _consume_mimo_pending(cache, user_id)

    try:
        identity = mimo_oauth.decrypt_code(pending["private_key"], code)
    except mimo_oauth.MiMoOAuthError:
        logger.info(f"[oauth] MiMo code verification failed for user_id={user_id}")
        raise HTTPException(status_code=400, detail=mimo_oauth.CODE_INVALID_MESSAGE)
    except Exception as e:
        logger.error(
            f"[oauth] MiMo code verification error for user_id={user_id}: {_safe_error(e)}"
        )
        raise HTTPException(status_code=400, detail=mimo_oauth.CODE_INVALID_MESSAGE)

    try:
        result = await mimo_oauth.save_login(user_id, identity)
    except Exception as e:
        logger.error(
            f"[oauth] MiMo credential store failed for user_id={user_id}: {_safe_error(e)}"
        )
        raise HTTPException(status_code=500, detail="MiMo sign-in could not be saved")

    logger.info(f"[oauth] MiMo connected for user_id={user_id}")
    return {
        "success": True,
        "uid": result["uid"],
        "base_url_host": result["base_url_host"],
    }


@router.get("/mimo/status")
async def mimo_status(user_id: CurrentUserId):
    """Connection status from credential presence (no key is decrypted)."""
    return await mimo_oauth.get_status(user_id)


@router.delete("/mimo")
async def mimo_disconnect(user_id: CurrentUserId):
    await mimo_oauth.disconnect(user_id)
    logger.info(f"[oauth] MiMo disconnected for user_id={user_id}")
    return {"success": True}

# ═══════════════════════════════════════════════════════════════════════════════
# DeepSeek account — PKCE browser redirect, x-dsh-auth-token credential
# ═══════════════════════════════════════════════════════════════════════════════


class DeepSeekInitiateRequest(BaseModel):
    """The loopback page DeepSeek's authorize page returns the code to."""

    redirect_uri: str


class DeepSeekCallbackRequest(BaseModel):
    code: str
    state: str


@router.post("/deepseek/initiate")
async def deepseek_initiate(user_id: CurrentUserId, body: DeepSeekInitiateRequest):
    """Start a Platform sign-in; returns the URL the browser should open."""
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise HTTPException(status_code=503, detail="Cache unavailable for OAuth")

    try:
        flow = await deepseek_oauth.start_authorization(body.redirect_uri)
    except deepseek_oauth.DeepSeekOAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await cache.client.set(
        deepseek_oauth.flow_cache_key(user_id),
        deepseek_oauth.dumps_flow(flow),
        ex=flow["expires_in"],
    )
    logger.info("[oauth] DeepSeek account sign-in initiated user_id=%s", user_id)
    return {"authorize_url": flow["authorize_url"]}


@router.post("/deepseek/callback")
async def deepseek_callback(user_id: CurrentUserId, body: DeepSeekCallbackRequest):
    """Validate the returned state, exchange the code, and store the token."""
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise HTTPException(status_code=503, detail="Cache unavailable")

    flow = deepseek_oauth.loads_flow(
        await cache.client.get(deepseek_oauth.flow_cache_key(user_id))
    )
    if not flow:
        raise HTTPException(
            status_code=400,
            detail="No pending DeepSeek sign-in. Please initiate again.",
        )
    if not secrets.compare_digest(body.state, flow["state"]):
        raise HTTPException(status_code=400, detail="State mismatch — possible CSRF. Please try again.")
    if not body.code.strip():
        raise HTTPException(status_code=400, detail="Missing authorization code")

    try:
        token = await deepseek_oauth.exchange_code(
            body.code.strip(), flow["verifier"], flow["redirect_uri"]
        )
    except deepseek_oauth.DeepSeekOAuthError as exc:
        logger.warning("[oauth] DeepSeek exchange failed user_id=%s: %s", user_id, exc)
        raise HTTPException(status_code=400, detail=str(exc))

    await deepseek_oauth.store_token(user_id, token)
    await cache.client.delete(deepseek_oauth.flow_cache_key(user_id))
    logger.info("[oauth] DeepSeek account connected user_id=%s", user_id)
    return {"success": True}


@router.get("/deepseek/models")
async def deepseek_models(user_id: CurrentUserId):
    """The account's Platform model catalog (cached per token)."""
    rows = await deepseek_oauth.account_model_catalog(user_id)
    if rows is None:
        raise HTTPException(status_code=404, detail="No DeepSeek account is connected")
    return {"models": rows}


@router.get("/deepseek/status")
async def deepseek_status(user_id: CurrentUserId):
    return await get_oauth_status(user_id, deepseek_oauth.DEEPSEEK_PROVIDER)


@router.delete("/deepseek")
async def deepseek_disconnect(user_id: CurrentUserId):
    await deepseek_oauth.disconnect(user_id)
    logger.info("[oauth] DeepSeek account disconnected user_id=%s", user_id)
    return {"success": True}
