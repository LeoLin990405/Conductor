"""Redact credential-shaped material at external Source boundaries."""

from __future__ import annotations

import re
from collections.abc import Iterable

_SECRET_PATTERNS = (
    # GitHub classic, fine-grained, and OAuth tokens.
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    # Common bearer/basic authorization values.
    re.compile(r"(?i)(\b(?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]{8,}"),
    # Explicitly named credentials in logs, config snippets, and URLs.
    re.compile(
        r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|api[_-]?key|client[_-]?secret|password|secret)\s*[=:]\s*)"
        r"([^\s,;\"']+)"
    ),
)


def redact_external_text(value: object, *, secrets: Iterable[str] = ()) -> str:
    """Remove known values and common credential formats before persistence or display."""
    text = str(value or "")
    known = sorted({str(secret) for secret in secrets if secret}, key=len, reverse=True)
    for secret in known:
        text = text.replace(secret, "[REDACTED]")
    text = _SECRET_PATTERNS[0].sub("[REDACTED]", text)
    text = _SECRET_PATTERNS[1].sub(r"\1[REDACTED]", text)
    return _SECRET_PATTERNS[2].sub(r"\1[REDACTED]", text)


def redact_external_data(value: object, *, secrets: Iterable[str] = ()) -> object:
    """Recursively scrub strings in provider payloads before callers can persist them."""
    if isinstance(value, str):
        return redact_external_text(value, secrets=secrets)
    if isinstance(value, list):
        return [redact_external_data(item, secrets=secrets) for item in value]
    if isinstance(value, dict):
        return {
            redact_external_text(key, secrets=secrets) if isinstance(key, str) else key:
            redact_external_data(item, secrets=secrets)
            for key, item in value.items()
        }
    return value
