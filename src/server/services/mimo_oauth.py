"""MiMo coding-plan account login — the platform's paste-code envelope.

The official MiMo Code CLI asks the platform to mint an API key for it: the CLI
sends an ephemeral X25519 public key (``pk``) on the authorize URL, the user
approves in the browser, and the platform shows an opaque code (the CLI's ``u``
payload) carrying the minted key. The code is the platform's public key, a
nonce and the AES-256-GCM ciphertext, encrypted to the X25519 key whose private
half the CLI kept. This service implements that same protocol, with the
platform's own code callback as the paste-back page, so the app never asks for
a MiMo password and the minted key never travels through the browser in clear.

Invariants that make the flow safe:

- The X25519 private key exists only in Redis, only for the pending login, and
  the caller consumes that pending state before decrypting, so a pasted code
  cannot be replayed.
- No secret is ever logged or returned: not the code, the private key, the
  decrypted payload, or the minted key. Every decryption failure surfaces as
  one fixed message, so an attacker cannot use error text as an oracle.
- Both hosts are fixed — the platform base is an operator env override, never a
  request value — so no request can point the flow at another host (SSRF).

The minted key is stored as a BYOK credential for the manifest's
``mimo-oauth`` provider (with the base URL from the payload), which is exactly
what the runtime resolver reads; there is no separate OAuth token row to keep
in sync.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
from urllib.parse import urlencode, urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.server.database.api_keys import (
    delete_api_key,
    get_user_provider_credential_status,
    invalidate_byok_cache,
    set_byok_enabled,
    upsert_api_key,
)
from src.server.services.onboarding import maybe_complete_personalization

logger = logging.getLogger(__name__)

MIMO_PROVIDER = "mimo-oauth"
PLAN_TYPE = "coding_plan"

# The platform host is deployment configuration, not a user input. The env
# override exists so a staging deployment can exercise the flow end to end;
# it is read per call so tests never leave a real host configured.
DEFAULT_PLATFORM_URL = "https://platform.xiaomimimo.com"
PLATFORM_URL_ENV = "MIMO_PLATFORM_URL"

# The platform's own code page: the user copies the code it shows and pastes it
# into the app. This is also the redirect the official CLI registers.
MIMO_KN = "mimocode"
PENDING_TTL_SECONDS = 600

KEY_NAME_PREFIX = "mimo-code-cli-key-"
# A pasted code is a few hundred bytes of base64url; the cap bounds the work a
# malicious paste can cause and matches the route's request validation.
MAX_CODE_LENGTH = 8192
MAX_CREDENTIAL_LENGTH = 4096
MAX_URL_LENGTH = 2048

_PUBLIC_KEY_BYTES = 32
_NONCE_BYTES = 12
_TAG_BYTES = 16
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+={0,2}")

# One fixed message for every decryption failure: an oracle that distinguishes
# "wrong key" from "corrupt tag" from "bad JSON" would help an attacker holding
# a pasted code, and the user cannot act on the distinction anyway.
CODE_INVALID_MESSAGE = "MiMo authorization code could not be verified"


class MiMoOAuthError(RuntimeError):
    """A sanitized MiMo login failure. The message never carries a secret."""


# ─── Platform host / authorize URL ──────────────────────────────────────────


def platform_base_url() -> str:
    """The MiMo platform base, operator-overridable via ``MIMO_PLATFORM_URL``."""
    override = (os.environ.get(PLATFORM_URL_ENV) or "").strip()
    return (override or DEFAULT_PLATFORM_URL).rstrip("/")


def redirect_uri() -> str:
    """The platform page that shows the code the user pastes back."""
    return f"{platform_base_url()}/authorize/code/callback"


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def build_authorize_url(public_key_b64: str, key_name: str) -> str:
    """The MiMo authorize URL for an X25519 public key and a minted-key name."""
    query = urlencode(
        {
            "pk": public_key_b64,
            "redirect_uri": redirect_uri(),
            "kn": MIMO_KN,
            "key_name": key_name,
        }
    )
    return f"{platform_base_url()}/authorize?{query}"


def create_login() -> dict:
    """A fresh pending login: the private half we keep and the URL to open.

    ``private_key`` is base64url of the raw X25519 private bytes; it must go to
    Redis only. ``key_name`` names the minted platform key and is not secret.
    """
    private_key = X25519PrivateKey.generate()
    private_b64 = _b64url_encode(
        private_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    public_b64 = _b64url_encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    key_name = f"{KEY_NAME_PREFIX}{secrets.token_hex(4)}"
    return {
        "private_key": private_b64,
        "key_name": key_name,
        "authorize_url": build_authorize_url(public_b64, key_name),
    }


# ─── Decrypting the pasted code ─────────────────────────────────────────────


def _b64url_decode(value: str) -> bytes:
    """Strict base64url decode; accepts optional padding and pasted whitespace."""
    text = "".join(str(value or "").split())
    if not text or len(text) > MAX_CODE_LENGTH:
        raise ValueError("empty or oversized base64url value")
    if not _B64URL_RE.fullmatch(text):
        raise ValueError("not base64url")
    # A length of 1 mod 4 can never be valid; the padding rule below would
    # otherwise fabricate 3 '=' and let the decoder fail obscurely.
    if len(text) % 4 == 1:
        raise ValueError("invalid base64url length")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _payload_fields(payload: object) -> dict:
    """Validate and normalize the decrypted ``{uid, url, sk}`` payload."""
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
    uid = payload.get("uid")
    base_url = payload.get("url")
    api_key = payload.get("sk")

    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("payload has no api key")
    api_key = api_key.strip()
    if len(api_key) > MAX_CREDENTIAL_LENGTH:
        raise ValueError("api key too long")

    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("payload has no base url")
    base_url = base_url.strip()
    if len(base_url) > MAX_URL_LENGTH:
        raise ValueError("base url too long")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("base url is not http(s)")

    # ``uid`` is the platform account id: display/report only, never a key.
    if isinstance(uid, bool) or not isinstance(uid, (str, int)):
        raise ValueError("payload has no uid")
    uid_text = str(uid).strip()
    if not uid_text or len(uid_text) > 256:
        raise ValueError("payload uid is invalid")

    return {"uid": uid_text, "base_url": base_url, "api_key": api_key}


def decrypt_code(private_key_b64: str, code: str) -> dict:
    """Decrypt a pasted ``u`` payload into ``{uid, base_url, api_key}``.

    Wire format after base64url decoding: ``ephemeral_pub (32) || nonce (12) ||
    ciphertext || tag (16)``. The AES key is SHA-256 of the X25519 shared
    secret. Every failure raises the same sanitized :class:`MiMoOAuthError`, so
    no crypto detail (and no part of the code) can reach the wire or a log.
    """
    try:
        raw = _b64url_decode(code)
        if len(raw) < _PUBLIC_KEY_BYTES + _NONCE_BYTES + _TAG_BYTES:
            raise ValueError("code is too short")
        private_key = X25519PrivateKey.from_private_bytes(_b64url_decode(private_key_b64))
        ephemeral_public = X25519PublicKey.from_public_bytes(raw[:_PUBLIC_KEY_BYTES])
        shared = private_key.exchange(ephemeral_public)

        digest = hashes.Hash(hashes.SHA256())
        digest.update(shared)
        counter_start = _PUBLIC_KEY_BYTES + _NONCE_BYTES
        plaintext = AESGCM(digest.finalize()).decrypt(
            raw[_PUBLIC_KEY_BYTES:counter_start], raw[counter_start:], None
        )
        return _payload_fields(json.loads(plaintext))
    except MiMoOAuthError:
        raise
    except Exception:
        raise MiMoOAuthError(CODE_INVALID_MESSAGE) from None


# ─── Persistence: the minted key is a BYOK credential ──────────────────────


async def save_login(user_id: str, identity: dict) -> dict:
    """Store the minted key for ``mimo-oauth`` and enable it for the user.

    The key write is the only step allowed to fail the login: once it lands the
    account is connected. Enabling BYOK and completing personalization are
    best-effort so a transient failure there cannot report a failed login whose
    credential is already stored (the user could not retry: the pending state
    is consumed).
    """
    await upsert_api_key(
        user_id,
        MIMO_PROVIDER,
        identity["api_key"],
        base_url=identity["base_url"],
    )
    try:
        credentials = await get_user_provider_credential_status(user_id)
        if not credentials.get("enabled"):
            await set_byok_enabled(user_id, True)
        await invalidate_byok_cache(user_id)
        await maybe_complete_personalization(user_id)
    except Exception as exc:
        logger.warning(
            f"[mimo_oauth] Post-store setup failed for user_id={user_id}: "
            f"{type(exc).__name__}"
        )
    return {
        "uid": identity["uid"],
        "base_url_host": urlparse(identity["base_url"]).hostname,
    }


async def get_status(user_id: str) -> dict:
    """Connection status derived from the stored credential's presence.

    MiMo login has no OAuth token row: the minted key *is* the credential, so
    presence (not decrypted value) is the whole answer.
    """
    credentials = await get_user_provider_credential_status(user_id)
    connected = bool((credentials.get("providers") or {}).get(MIMO_PROVIDER))
    return {
        "connected": connected,
        "account_id": None,
        "email": None,
        "plan_type": PLAN_TYPE if connected else None,
    }


async def disconnect(user_id: str) -> None:
    """Remove the stored MiMo key; other providers' keys are untouched."""
    await delete_api_key(user_id, MIMO_PROVIDER)
    await invalidate_byok_cache(user_id)
