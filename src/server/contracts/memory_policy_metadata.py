"""Closed metadata contract for memory conditions, evidence pointers, and sensitivity."""

from __future__ import annotations

from collections.abc import Mapping
import re
from datetime import datetime, timezone
from typing import Any, Literal

import yaml

MemorySensitivity = Literal["public", "private", "secret"]
MEMORY_SENSITIVITIES: tuple[MemorySensitivity, ...] = (
    "public",
    "private",
    "secret",
)

MAX_POLICY_TEXT_CHARS = 500
MAX_EVIDENCE_MESSAGE_ID_CHARS = 128
MAX_EVIDENCE_OFFSET = 1_000_000_000
_QUOTE_REF_FIELDS = frozenset({"message_id", "start", "end", "unit"})
_MEMORY_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?:(?P<body>.*?)\r?\n)?---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)


class MemoryPolicyMetadataError(ValueError):
    """Raised when policy metadata is malformed and cannot be used safely."""


def _bounded_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise MemoryPolicyMetadataError(f"{field} must be a string")
    normalized = " ".join(value.split())
    if len(normalized) > MAX_POLICY_TEXT_CHARS:
        raise MemoryPolicyMetadataError(f"{field} exceeds {MAX_POLICY_TEXT_CHARS} characters")
    return normalized


def _quote_ref(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _QUOTE_REF_FIELDS:
        raise MemoryPolicyMetadataError("evidence_quote_ref has an invalid shape")
    message_id = value.get("message_id")
    if (
        not isinstance(message_id, str)
        or not message_id.strip()
        or len(message_id.strip()) > MAX_EVIDENCE_MESSAGE_ID_CHARS
        or any(ord(char) < 32 or ord(char) == 127 for char in message_id)
    ):
        raise MemoryPolicyMetadataError("evidence_quote_ref has an invalid message_id")
    start = value.get("start")
    end = value.get("end")
    if (
        type(start) is not int
        or type(end) is not int
        or start < 0
        or end <= start
        or end > MAX_EVIDENCE_OFFSET
    ):
        raise MemoryPolicyMetadataError("evidence_quote_ref has invalid offsets")
    if value.get("unit") != "unicode_codepoint":
        raise MemoryPolicyMetadataError("evidence_quote_ref unit must be unicode_codepoint")
    return {
        "message_id": message_id.strip(),
        "start": start,
        "end": end,
        "unit": "unicode_codepoint",
    }


def normalize_memory_policy_metadata(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return bounded policy fields; malformed policy data fails closed.

    Missing sensitivity is treated as private for older memories. Invalid
    sensitivity and conditions map to secret; malformed quote pointers are
    dropped because they do not authorize or restrict memory use.
    """
    if metadata is not None and not isinstance(metadata, Mapping):
        raise MemoryPolicyMetadataError("memory metadata must be an object")
    source = metadata or {}
    sensitivity = source.get("sensitivity", "private")
    normalized_sensitivity = (
        sensitivity.strip().casefold() if isinstance(sensitivity, str) else "secret"
    )
    if normalized_sensitivity not in MEMORY_SENSITIVITIES:
        normalized_sensitivity = "secret"

    result: dict[str, Any] = {"sensitivity": normalized_sensitivity}
    for field in ("applies_when", "exceptions"):
        if field not in source:
            continue
        try:
            value = _bounded_text(source[field], field)
        except MemoryPolicyMetadataError:
            result["sensitivity"] = "secret"
            return result
        if value:
            result[field] = value
    if "evidence_quote_ref" in source:
        try:
            result["evidence_quote_ref"] = _quote_ref(source["evidence_quote_ref"])
        except MemoryPolicyMetadataError:
            pass
    return result


def normalize_memory_policy_metadata_layers(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge outer and wrapped metadata without letting an outer value unhide secret."""
    if metadata is not None and not isinstance(metadata, Mapping):
        return {"sensitivity": "secret"}
    malformed_wrapper = False
    layers: list[Mapping[str, Any]] = []
    pending = [(metadata, 0)] if isinstance(metadata, Mapping) else []
    seen: set[int] = set()
    while pending:
        current, depth = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        layers.append(current)
        for field in ("metadata", "memory_metadata"):
            if field not in current:
                continue
            wrapped = current[field]
            if not isinstance(wrapped, Mapping):
                malformed_wrapper = True
            elif depth >= 8:
                malformed_wrapper = True
            else:
                pending.append((wrapped, depth + 1))
    normalized = [normalize_memory_policy_metadata(layer) for layer in layers]
    result: dict[str, Any] = {"sensitivity": "private"}
    for layer in normalized:
        result.update(layer)
    if malformed_wrapper or any(
        layer["sensitivity"] == "secret" for layer in normalized
    ):
        result["sensitivity"] = "secret"
    return result


def memory_content_is_recallable(content: Any) -> bool:
    """Match projection recall policy before the legacy index can inject a detail."""
    if not isinstance(content, str):
        return False
    if not content.startswith("---"):
        return True
    match = _MEMORY_FRONTMATTER_RE.match(content)
    if match is None:
        return False
    try:
        metadata = yaml.safe_load(match.group("body") or "") or {}
    except yaml.YAMLError:
        return False
    if not isinstance(metadata, Mapping):
        return False
    if normalize_memory_policy_metadata_layers(metadata)["sensitivity"] == "secret":
        return False
    lifecycle = metadata.get("lifecycle")
    if lifecycle is not None and lifecycle != "active":
        return False
    current = datetime.now(timezone.utc)
    for field, expires in (("valid_from", False), ("valid_until", True)):
        raw = metadata.get(field)
        if not raw:
            continue
        try:
            boundary = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if boundary.tzinfo is None:
            boundary = boundary.replace(tzinfo=timezone.utc)
        if (expires and current >= boundary) or (not expires and current < boundary):
            return False
    return True
