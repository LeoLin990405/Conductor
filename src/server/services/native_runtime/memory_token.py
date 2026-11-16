"""Short-lived capability token for the native memory MCP bridge.

The MCP server is a child process, not a browser session: it cannot use the
normal request auth. It gets an HMAC capability scoped to one user/thread and a
short expiry, and the write endpoint verifies that token itself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

_SECRET = (os.environ.get("NATIVE_MEMORY_SECRET") or os.environ.get("SECRET_KEY") or "").encode() or secrets.token_bytes(32)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def issue_memory_token(
    user_id: str,
    thread_id: str,
    *,
    workspace_id: str | None = None,
    ttl_seconds: int = 3600,
    now: int | None = None,
) -> str:
    payload = {
        "u": user_id,
        "t": thread_id,
        "e": int(now if now is not None else time.time()) + ttl_seconds,
    }
    if workspace_id:
        payload["w"] = workspace_id
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(_SECRET, raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(sig)}"


def verify_memory_token(token: str, *, now: int | None = None) -> dict[str, Any] | None:
    try:
        raw_part, sig_part = token.split(".", 1)
        raw = _unb64(raw_part)
        expected = hmac.new(_SECRET, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(sig_part), expected):
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        if int(payload.get("e") or 0) < int(now if now is not None else time.time()):
            return None
        if not str(payload.get("u") or "") or not str(payload.get("t") or ""):
            return None
        return payload
    except Exception:
        return None
