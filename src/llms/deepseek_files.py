"""DeepSeek Files API transport for image inputs.

Large images are uploaded once to
``{base}/v1/files`` with ``anthropic-beta: files-api-2025-04-14`` and referenced
from Messages as ``{"type": "image", "source": {"type": "file", "file_id": ...}}``.
Inline base64 stays the fallback when the upload fails.

This module owns only the transport. Choosing between a file reference and an
inline block, the byte/count budgets, and the message rewrite live above it.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

FILES_BETA = "files-api-2025-04-14"
ANTHROPIC_VERSION = "2023-06-01"
MAX_UPLOAD_BYTES = 128 * 1024 * 1024
MIN_EXPIRY_SECONDS = 3600
MAX_EXPIRY_SECONDS = 2_592_000
DEFAULT_EXPIRY_SECONDS = 604_800  # 7 days, the official adapter's default

#: Every image-only model route we can attach a file block to.
IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


class DeepSeekFilesError(RuntimeError):
    """A sanitized Files API failure; the provider status is retained."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class DeepSeekFileRef:
    """One uploaded file as Messages references it."""

    file_id: str
    bytes: int
    filename: str

    def image_block(self) -> dict[str, Any]:
        return {"type": "image", "source": {"type": "file", "file_id": self.file_id}}


def files_api_root(base_url: str) -> str:
    """Resolve the API root without duplicating an explicit version segment."""
    base = (base_url or "").rstrip("/")
    if not base:
        raise ValueError("DeepSeek Files API requires a base URL")
    from urllib.parse import urlparse

    if urlparse(base).path.rstrip("/").endswith("/v1"):
        return base
    return f"{base}/v1"


def cache_key(base_url: str, auth_header: str, token: str, data: bytes) -> str:
    """Deterministic reuse key: endpoint + credential scope + exact bytes."""
    digest = hashlib.sha256(data).hexdigest()
    scope = hashlib.sha256(f"{auth_header}:{token}".encode()).hexdigest()[:16]
    return f"deepseek:files:{scope}:{hashlib.sha256(base_url.encode()).hexdigest()[:12]}:{digest}"


def _error_detail(payload: object) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            parts = [error.get("code"), error.get("type"), error.get("message")]
            return " ".join(str(p) for p in parts if isinstance(p, str))[:200]
        for key in ("message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
    return ""


async def upload_image(
    *,
    base_url: str,
    auth_header: str,
    token: str,
    data: bytes,
    media_type: str,
    filename: str,
    expires_after_seconds: int = DEFAULT_EXPIRY_SECONDS,
    timeout: float = 60.0,
) -> DeepSeekFileRef:
    """Upload one image and return its file reference.

    ``auth_header`` is the provider's credential header (``x-api-key`` for the
    API-key route, ``x-dsh-auth-token`` for the account route).
    """
    if media_type not in IMAGE_MEDIA_TYPES:
        raise DeepSeekFilesError(f"unsupported image media type {media_type!r}")
    if not data:
        raise DeepSeekFilesError("empty image upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise DeepSeekFilesError("image exceeds the 128 MiB Files API limit")
    if not MIN_EXPIRY_SECONDS <= expires_after_seconds <= MAX_EXPIRY_SECONDS:
        raise DeepSeekFilesError(
            "file expiry must be between one hour and thirty days"
        )

    headers = {
        auth_header: token,
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": FILES_BETA,
    }
    files = {
        "file": (filename or "image", data, media_type),
    }
    form = {
        "expires_after[anchor]": "created_at",
        "expires_after[seconds]": str(expires_after_seconds),
    }
    url = f"{files_api_root(base_url)}/files"
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        try:
            response = await client.post(url, headers=headers, data=form, files=files)
        except httpx.HTTPError as exc:
            raise DeepSeekFilesError("DeepSeek Files API is unreachable") from exc

    if response.status_code >= 400:
        try:
            payload = response.json()
        except Exception:
            payload = None
        detail = _error_detail(payload)
        suffix = f": {detail}" if detail else ""
        raise DeepSeekFilesError(
            f"DeepSeek Files API error (HTTP {response.status_code}){suffix}",
            response.status_code,
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise DeepSeekFilesError("DeepSeek Files API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise DeepSeekFilesError("DeepSeek Files API returned an invalid upload response")
    file_id = payload.get("id")
    size = payload.get("size_bytes")
    name = payload.get("filename")
    if (
        not isinstance(file_id, str)
        or not file_id
        or payload.get("type") != "file"
        or not isinstance(size, int)
        or not isinstance(name, str)
        or not name
    ):
        raise DeepSeekFilesError("DeepSeek Files API returned an invalid upload response")
    return DeepSeekFileRef(file_id=file_id, bytes=size, filename=name)
