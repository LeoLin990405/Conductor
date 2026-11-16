"""Payload redaction for the append-only event stream.

Event payloads are persisted, read back by the timeline and compared by the
differential harness. They must never carry a credential or a full tool result:
that is what ``provenance_records`` is for. Redaction happens **before** the
insert, once, so an emitted row cannot leak by a later reader forgetting to
redact.

The policy is deliberately blunt:
- keys whose name looks secret are replaced wholesale (not masked), because a
  partial mask on an unknown shape is how tokens survive;
- strings are truncated to a bounded length (long strings are usually tool
  output; the id/hash is kept alongside);
- nesting is depth-capped to stop a pathological payload from widening the row;
- NUL bytes are stripped because Postgres JSONB rejects them.
"""

from __future__ import annotations

import re
from typing import Any

MAX_STRING_LENGTH = 512
MAX_PAYLOAD_DEPTH = 4
REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"

_SECRET_KEY = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|authorization|cookie|credential|"
    r"private[_-]?key|session[_-]?id)",
    re.IGNORECASE,
)


def redact_event_payload(value: Any, *, _depth: int = 0) -> Any:
    """Return a JSON-safe copy with secrets replaced and long values bounded."""
    if _depth > MAX_PAYLOAD_DEPTH:
        return TRUNCATED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = value.replace("\x00", "")
        if len(text) <= MAX_STRING_LENGTH:
            return text
        return f"{text[:MAX_STRING_LENGTH]}…{TRUNCATED}<len={len(text)}>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key).replace("\x00", "")
            if _SECRET_KEY.search(name):
                out[name] = REDACTED
            else:
                out[name] = redact_event_payload(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_event_payload(item, _depth=_depth + 1) for item in value]
    # Unknown objects (datetimes, enums, custom models) become their string form;
    # letting json.dumps fail later would fail after redaction, not before.
    return redact_event_payload(str(value), _depth=_depth + 1)
