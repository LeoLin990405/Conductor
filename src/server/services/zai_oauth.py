"""Zhipu (Z.ai / BigModel) coding-plan OAuth service.

The GLM coding plan is sold through two regional sign-in hosts:

- Z.ai (international):  a server-mediated CLI flow. ``POST /cli/init`` returns
  a login URL plus a flow id; the browser approves it, and the app polls
  ``/cli/poll/{flow_id}``. The poll token stays server-side.
- BigModel (CN):  a plain browser redirect to ``bigmodel.cn/login`` with a
  loopback callback. The user's browser comes back to the local callback page,
  which posts the auth code to us.

Neither login yields a durable bearer: the subscription grant expires. So, like
the official client, this service uses the account's OAuth token once to mint a
long-lived per-project API key named ``zcode-api-key`` and stores that key as
the OAuth credential. Runtime resolution then has no refresh path at all — the
minted key is the credential, and an expired row simply means sign in again.

Non-obvious invariants:

- Every Zhipu endpoint answers ``{code, msg, data}``; ``code == 0`` is success.
  Error text is sanitized before it can reach the wire or a log line.
- BigModel's callback URL is handed to the upstream login page, so it must be
  loopback-only: anything else would turn the endpoint into an open redirect.
- The minted key is ``apiKey + "." + secretKey``; BigModel reports the secret
  through the same ``copy`` call, and a key without one is used as-is.
"""

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlparse

import httpx

from src.server.database.oauth_tokens import (
    delete_oauth_tokens,
    get_oauth_status,
    get_oauth_tokens,
    invalidate_oauth_active_cache,
    upsert_oauth_tokens,
)
from src.server.services.oauth_refresh_wait import token_expiry

logger = logging.getLogger(__name__)

# OAuth credential providers (manifest `provider` ids); the flow kinds are the
# upstream names and are also what the API returns to the frontend.
ZAI_PROVIDER = "z-ai-oauth"
BIGMODEL_PROVIDER = "z-ai-cn-oauth"
ZAI_FLOW = "zai"
BIGMODEL_FLOW = "bigmodel"

ZCODE_BASE_URL = "https://zcode.z.ai"
ZCODE_CLI_INIT_URL = f"{ZCODE_BASE_URL}/api/v1/oauth/cli/init"
ZCODE_CLI_POLL_URL = f"{ZCODE_BASE_URL}/api/v1/oauth/cli/poll"
ZCODE_TOKEN_URL = f"{ZCODE_BASE_URL}/api/v1/oauth/token"

BIGMODEL_LOGIN_URL = "https://bigmodel.cn/login"
BIGMODEL_BUSINESS_BASE = "https://bigmodel.cn"
ZAI_BUSINESS_BASE = "https://api.z.ai"
ZAI_BIZ_LOGIN_URL = f"{ZAI_BUSINESS_BASE}/api/auth/z/login"

API_KEY_NAME = "zcode-api-key"
PLAN_TYPE = "coding_plan"
# The minted key is not refreshed; keep the row's expiry far enough out that it
# behaves as the long-lived credential it is.
CREDENTIAL_TTL_DAYS = 365

# The exchange endpoint can answer a transient 5xx while it validates a fresh
# code; the code is still exchangeable, so retry rather than fail the login.
_EXCHANGE_ATTEMPTS = 3

# Token-shaped substrings (JWTs, hex secrets, prefixed keys) are stripped from
# upstream error text before it is shown or logged.
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_\-\.]{32,}")


class ZhipuOAuthError(RuntimeError):
    """A sanitized Zhipu/Z.ai failure. The message never carries a token."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def sanitize_message(value: object) -> str:
    """A short, token-free rendering of upstream error text."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return _TOKEN_SHAPE.sub("[redacted]", text)[:200]


# ─── Envelope / request plumbing ─────────────────────────────────────────────


def unwrap_envelope(payload: object) -> object:
    """Return the business data from a Zhipu response envelope.

    Both zcode and the business APIs answer ``{code, msg, data}``; code 0 (or
    200) is success. A payload without an envelope is returned unchanged so a
    variant response still parses. Error text is sanitized here, because it is
    the only text that may leave the service.
    """
    if not isinstance(payload, dict) or "code" not in payload:
        return payload
    code = payload.get("code")
    try:
        ok = int(code) in (0, 200)
    except (TypeError, ValueError):
        ok = False
    if not ok:
        message = sanitize_message(payload.get("msg") or payload.get("message"))
        suffix = f": {message}" if message else ""
        raise ZhipuOAuthError(
            f"Zhipu sign-in failed (code {sanitize_message(code)}){suffix}"
        )
    data = payload.get("data")
    return payload if data is None else data


async def _request(
    method: str,
    url: str,
    *,
    headers: dict,
    json_body: dict | None = None,
) -> object:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(method, url, headers=headers, json=json_body)
    if resp.status_code >= 400:
        # Never surface the body: an auth endpoint's error payload can echo
        # credential detail, and this message reaches an HTTP response.
        raise ZhipuOAuthError(
            f"Zhipu sign-in service returned HTTP {resp.status_code}",
            status_code=resp.status_code,
        )
    try:
        payload = resp.json()
    except Exception as exc:
        raise ZhipuOAuthError("Zhipu sign-in service returned an unreadable response") from exc
    return unwrap_envelope(payload)


async def _get_json(url: str, headers: dict) -> object:
    return await _request("GET", url, headers=headers)


async def _post_json(url: str, headers: dict, body: dict) -> object:
    return await _request("POST", url, headers=headers, json_body=body)


def _json_headers(token: str | None = None, *, bearer: bool = False) -> dict:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}" if bearer else token
    return headers


def _cli_headers(poll_token: str) -> dict:
    return _json_headers(poll_token, bearer=True)


# ─── Z.ai server-mediated CLI flow ──────────────────────────────────────────


async def initiate_cli_flow() -> dict:
    """Start a Z.ai CLI login; returns the flow the routes cache for polling.

    The poll token is generated locally and also returned in the envelope; the
    server's copy is authoritative when present, since it is the half the poll
    endpoint validates.
    """
    generated = secrets.token_hex(32)
    data = await _post_json(ZCODE_CLI_INIT_URL, _cli_headers(generated), {"provider": ZAI_FLOW})
    if not isinstance(data, dict):
        raise ZhipuOAuthError("Zhipu sign-in service returned an unexpected response")
    flow_id = data.get("flow_id")
    authorize_url = data.get("authorize_url")
    if not flow_id or not authorize_url:
        raise ZhipuOAuthError("Zhipu sign-in service did not return a login flow")
    try:
        expires_at = int(data["expires_at"]) if data.get("expires_at") is not None else None
    except (TypeError, ValueError):
        expires_at = None
    try:
        interval = int(data.get("poll_interval_sec") or 2)
    except (TypeError, ValueError):
        interval = 2
    return {
        "flow_id": str(flow_id),
        "poll_token": str(data.get("poll_token") or generated),
        "authorize_url": str(authorize_url),
        "expires_at": expires_at,
        "interval": interval,
    }


async def poll_cli_flow(flow_id: str, poll_token: str) -> dict:
    """One server-side poll of the Z.ai flow; the caller acts on ``status``."""
    url = f"{ZCODE_CLI_POLL_URL}/{quote(str(flow_id), safe='')}"
    data = await _get_json(url, _cli_headers(poll_token))
    if not isinstance(data, dict) or data.get("status") not in ("pending", "ready", "failed"):
        raise ZhipuOAuthError("Zhipu sign-in service returned an unexpected poll status")
    return data


# ─── BigModel browser redirect flow ─────────────────────────────────────────


def validate_loopback_redirect_uri(redirect_uri: str) -> str:
    """Accept only a loopback http(s) callback URL.

    The value is handed to BigModel's login page as the post-login redirect, so
    a non-loopback one would turn this app into an open redirect: the callback
    page has to be something this machine serves.
    """
    value = (redirect_uri or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in (
        "localhost",
        "127.0.0.1",
    ):
        raise ValueError(
            "redirect_uri must be a loopback http(s) URL (localhost or 127.0.0.1)"
        )
    return value


def build_bigmodel_authorize_url(redirect_uri: str, state: str) -> str:
    params = {"redirect": redirect_uri, "appId": "zcode", "state": state}
    return f"{BIGMODEL_LOGIN_URL}?{urlencode(params)}"


async def exchange_bigmodel_code(code: str, state: str, redirect_uri: str) -> dict:
    """Exchange the browser callback code; retries transient 5xx.

    A 5xx while the upstream validates a fresh code does not consume the code,
    so a couple of immediate retries turn a spurious failure into a login.
    """
    body = {
        "provider": BIGMODEL_FLOW,
        "code": code,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    headers = _json_headers()
    last_error: ZhipuOAuthError | None = None
    for attempt in range(_EXCHANGE_ATTEMPTS):
        try:
            data = await _post_json(ZCODE_TOKEN_URL, headers, body)
        except ZhipuOAuthError as exc:
            last_error = exc
            transient = exc.status_code is not None and 500 <= exc.status_code < 600
            if transient and attempt + 1 < _EXCHANGE_ATTEMPTS:
                continue
            raise
        if not isinstance(data, dict):
            raise ZhipuOAuthError("Zhipu sign-in service returned an unexpected response")
        return data
    raise last_error or ZhipuOAuthError("BigModel sign-in failed")


# ─── Coding-plan key minting ────────────────────────────────────────────────

_LIST_FIELDS = (
    "list",
    "items",
    "data",
    "organizations",
    "projects",
    "keys",
    "apiKeys",
    "api_keys",
    "records",
)


def _as_dict_list(value: object) -> list[dict]:
    """Rows from a list field, whichever container shape the endpoint used."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for field in _LIST_FIELDS:
            rows = value.get(field)
            if isinstance(rows, list):
                return [item for item in rows if isinstance(item, dict)]
    return []


def _pick_org_and_project(customer: object) -> tuple[str, str]:
    """The organization/project a coding-plan key belongs to.

    Prefer the explicitly defaulted ones; an account with several projects
    otherwise gets its first organization that actually has projects.
    """
    organizations = _as_dict_list(customer)
    if not organizations:
        raise ZhipuOAuthError("Zhipu account has no organization to mint a coding-plan key")
    organization = next(
        (org for org in organizations if "默认机构" in str(org.get("organizationName") or "")),
        None,
    )
    if organization is None:
        organization = next((org for org in organizations if org.get("projects")), organizations[0])
    projects = _as_dict_list(organization.get("projects"))
    if not projects:
        raise ZhipuOAuthError("Zhipu account has no project to mint a coding-plan key")
    project = next(
        (proj for proj in projects if "默认项目" in str(proj.get("projectName") or "")),
        projects[0],
    )
    organization_id = organization.get("organizationId")
    project_id = project.get("projectId")
    if not organization_id or not project_id:
        raise ZhipuOAuthError("Zhipu account returned an incomplete organization/project")
    return str(organization_id), str(project_id)


def _find_named_key(value: object) -> dict | None:
    return next(
        (row for row in _as_dict_list(value) if row.get("name") == API_KEY_NAME),
        None,
    )


async def _zai_business_token(zai_access_token: str) -> str:
    """Exchange the Z.ai OAuth token for the business-API bearer."""
    data = await _post_json(ZAI_BIZ_LOGIN_URL, _json_headers(), {"token": zai_access_token})
    if not isinstance(data, dict) or not data.get("access_token"):
        raise ZhipuOAuthError("Z.ai sign-in did not return a business token")
    return str(data["access_token"])


async def mint_coding_plan_key(flow: str, access_token: str) -> str:
    """Mint (or reuse) the account's long-lived ``zcode-api-key``.

    Repeated logins reuse the named key so they do not pile up API keys; a key
    that already exists is accepted as-is. The final credential is
    ``apiKey.secretKey``, or the bare apiKey when the upstream reports no
    secret for it.
    """
    if flow == BIGMODEL_FLOW:
        # BigModel's business API takes its OAuth token raw, without a scheme.
        base = BIGMODEL_BUSINESS_BASE
        headers = _json_headers(access_token)
    elif flow == ZAI_FLOW:
        base = ZAI_BUSINESS_BASE
        headers = _json_headers(await _zai_business_token(access_token), bearer=True)
    else:
        raise ZhipuOAuthError("Unsupported Zhipu sign-in provider")

    customer = await _get_json(f"{base}/api/biz/customer/getCustomerInfo", headers)
    organization, project = _pick_org_and_project(customer)
    keys_url = (
        f"{base}/api/biz/v1/organization/{quote(organization, safe='')}"
        f"/projects/{quote(project, safe='')}/api_keys"
    )

    entry = _find_named_key(await _get_json(keys_url, headers))
    if entry is None:
        created = await _post_json(keys_url, headers, {"name": API_KEY_NAME})
        entry = created if isinstance(created, dict) and created.get("name") == API_KEY_NAME else None
        if entry is None:
            # The create endpoint's payload is not contractually a list row;
            # re-read the list instead of guessing at its fields.
            entry = _find_named_key(await _get_json(keys_url, headers))
        if entry is None:
            raise ZhipuOAuthError("Zhipu did not return the new coding-plan API key")

    api_key = entry.get("apiKey") or entry.get("id") or entry.get("keyId")
    if not api_key:
        raise ZhipuOAuthError("Zhipu returned an API key without an id")
    copied = await _get_json(
        f"{keys_url}/copy/{quote(str(api_key), safe='')}",
        headers,
    )
    secret_key = copied.get("secretKey") if isinstance(copied, dict) else None
    return f"{api_key}.{secret_key}" if secret_key else str(api_key)


def _user_fields(flow_data: dict) -> tuple[str, str | None]:
    user = flow_data.get("user")
    if not isinstance(user, dict):
        return "", None
    account_id = str(user.get("user_id") or user.get("userId") or "")
    email = user.get("email")
    return account_id, email if isinstance(email, str) else None


async def _store_credential(
    user_id: str, provider: str, access_token: str, flow_data: dict
) -> dict:
    account_id, email = _user_fields(flow_data)
    await upsert_oauth_tokens(
        user_id=user_id,
        provider=provider,
        access_token=access_token,
        refresh_token="",
        account_id=account_id,
        email=email,
        plan_type=PLAN_TYPE,
        expires_at=datetime.now(timezone.utc) + timedelta(days=CREDENTIAL_TTL_DAYS),
    )
    try:
        await invalidate_oauth_active_cache(user_id)
    except Exception:
        pass
    return {
        "provider": BIGMODEL_FLOW if provider == BIGMODEL_PROVIDER else ZAI_FLOW,
        "account_id": account_id,
        "email": email,
    }


async def complete_zai_login(user_id: str, flow_data: dict) -> dict:
    """Mint and store the credential for a ready Z.ai CLI flow."""
    zai = flow_data.get("zai")
    zai_access_token = zai.get("access_token") if isinstance(zai, dict) else None
    if not zai_access_token:
        raise ZhipuOAuthError("Z.ai sign-in did not return an access token")
    key = await mint_coding_plan_key(ZAI_FLOW, str(zai_access_token))
    return await _store_credential(user_id, ZAI_PROVIDER, key, flow_data)


async def complete_bigmodel_login(user_id: str, exchange_data: dict) -> dict:
    """Mint and store the credential for a completed BigModel callback."""
    bigmodel = exchange_data.get("bigmodel")
    access_token = bigmodel.get("access_token") if isinstance(bigmodel, dict) else None
    if not access_token:
        raise ZhipuOAuthError("BigModel sign-in did not return an access token")
    key = await mint_coding_plan_key(BIGMODEL_FLOW, str(access_token))
    return await _store_credential(user_id, BIGMODEL_PROVIDER, key, exchange_data)


# ─── Resolution / status / disconnect ──────────────────────────────────────


async def get_valid_token(user_id: str, provider: str = ZAI_PROVIDER) -> dict | None:
    """The stored coding-plan key while its row is unexpired (else None).

    A minted key has no refresh grant: the row *is* the credential, and an
    expired one means the user has to sign in again.
    """
    if provider not in (ZAI_PROVIDER, BIGMODEL_PROVIDER):
        return None
    tokens = await get_oauth_tokens(user_id, provider)
    if not tokens or not tokens.get("expires_at"):
        return None
    if token_expiry(tokens) <= datetime.now(timezone.utc):
        return None
    return {
        "access_token": tokens["access_token"],
        "account_id": tokens["account_id"],
        "plan_type": tokens.get("plan_type"),
    }


async def get_status(user_id: str) -> dict:
    """Connection status, preferring the CN (BigModel) row."""
    for provider, flow in (
        (BIGMODEL_PROVIDER, BIGMODEL_FLOW),
        (ZAI_PROVIDER, ZAI_FLOW),
    ):
        status = await get_oauth_status(user_id, provider)
        if status.get("connected"):
            return {"connected": True, "provider": flow, "email": status.get("email")}
    return {"connected": False, "provider": None, "email": None}


async def disconnect(user_id: str) -> None:
    """Drop both regional credentials; a user can only be connected to one."""
    for provider in (BIGMODEL_PROVIDER, ZAI_PROVIDER):
        await delete_oauth_tokens(user_id, provider)
    try:
        await invalidate_oauth_active_cache(user_id)
    except Exception:
        pass
