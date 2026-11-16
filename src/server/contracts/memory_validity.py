"""Pure validity and confidence rules shared by memory retrieval layers."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


def is_current(metadata: Mapping[str, Any] | None, *, now: datetime | None = None) -> bool:
    """Whether optional validity bounds contain ``now``; expiry is exclusive."""
    if not isinstance(metadata, Mapping) or not metadata:
        return True
    lifecycle = metadata.get("lifecycle")
    if lifecycle is not None and lifecycle != "active":
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    for field, expires in (
        ("valid_from", False),
        ("valid_until", True),
        ("valid_to", True),
    ):
        raw = metadata.get(field)
        if not raw:
            continue
        try:
            boundary = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if boundary.tzinfo is None:
            boundary = boundary.replace(tzinfo=timezone.utc)
        if expires and current >= boundary:
            return False
        if not expires and current < boundary:
            return False
    return True


def confidence_value(metadata: Mapping[str, Any] | None) -> float | None:
    """Return finite numeric confidence; bools and malformed values are unknown."""
    if not isinstance(metadata, Mapping) or "confidence" not in metadata:
        return None
    value = metadata.get("confidence")
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 1 else None


def is_low_confidence(metadata: Mapping[str, Any] | None) -> bool:
    value = confidence_value(metadata)
    return value is not None and value <= 0.5
