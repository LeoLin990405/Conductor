"""Normalization and redaction checks for differential fixtures.

Two jobs, both required before a comparison means anything:

1. **Normalize volatility** — UUIDs, ISO timestamps and captured-at metadata are
   replaced with stable markers, so a re-captured flow does not diff on ids. The
   replacement is deterministic (``<uuid>``), not a per-fixture token map: two
   fixtures are compared for *shape*, and a mapping would make an id swap
   invisible.
2. **Reject secrets** — a fixture is a file that gets committed. Capture-time
   redaction is the producer's job, but a validator here is the backstop that
   turns "someone pasted a token into a fixture" into a failing test instead of a
   permanent repo leak.

Volatile *numeric* values (token counts, latencies) are normalized to their
tolerance class rather than removed: ``compare`` needs the magnitude to decide
whether a delta is tolerated (see ``compare.py``).
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from scripts.refactor_diff.schema import DbSnapshot, Exchange, Fixture, SseEvent, SseStream

_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_ISO_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_UUID_MARKER = "<uuid>"
_TIMESTAMP_MARKER = "<ts>"
_REDACTED = "[REDACTED]"

#: ``(pattern, allow_redacted)`` — a match is allowed only when the matched value
#: is the redaction marker. Ordered from most specific to broadest.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic/openai-style key", re.compile(r"sk-(?:cp-|api-|ant-)?[A-Za-z0-9_-]{12,}")),
    ("bearer credential", re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{12,}")),
    ("long opaque token", re.compile(r"\b[A-Za-z0-9_-]{40,}\b")),
)

#: Field names that must hold ``[REDACTED]`` (or be absent) in a committed fixture.
_SECRET_FIELDS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "cookie",
        "password",
        "token",
    }
)


class RedactionError(ValueError):
    """A fixture carries something that looks like a live credential."""


def redact_for_capture(value: Any) -> Any:
    """Redact a freshly captured value before it is written into a fixture.

    Capture happens before anything is committed, so this is the producer side
    of the redaction contract (``assert_redacted`` is the backstop). Secret-named
    fields become ``[REDACTED]`` wholesale; secret-shaped substrings inside other
    strings are replaced in place.
    """
    if isinstance(value, str):
        text = value.replace("\x00", "")
        for _label, pattern in _SECRET_PATTERNS:
            text = pattern.sub(_REDACTED, text)
        return text
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key).replace("\x00", "")
            out[name] = (
                _REDACTED
                if name.lower() in _SECRET_FIELDS
                else redact_for_capture(item)
            )
        return out
    if isinstance(value, (list, tuple)):
        return [redact_for_capture(item) for item in value]
    return value


def normalize_text(value: str) -> str:
    """Replace volatile identifiers inside one string, idempotently."""
    text = _UUID.sub(_UUID_MARKER, value)
    return _ISO_TIMESTAMP.sub(_TIMESTAMP_MARKER, text)


def normalize_value(value: Any) -> Any:
    """Recursively normalize strings in a JSON-ish value (tuples become lists)."""
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, dict):
        return {key: normalize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_value(item) for item in value]
    return value


def normalize_fixture(fixture: Fixture) -> Fixture:
    """A comparison-ready copy: volatiles normalized, metadata cleared."""
    return replace(
        fixture,
        captured_at="",
        http=tuple(
            Exchange(
                method=exchange.method,
                path=normalize_text(exchange.path),
                status=exchange.status,
                request=normalize_value(exchange.request),
                response=normalize_value(exchange.response),
            )
            for exchange in fixture.http
        ),
        sse=tuple(
            SseStream(
                label=stream.label,
                events=tuple(
                    SseEvent(type=event.type, payload=normalize_value(event.payload))
                    for event in stream.events
                ),
            )
            for stream in fixture.sse
        ),
        db=tuple(
            DbSnapshot(
                table=snapshot.table,
                rows=tuple(normalize_value(row) for row in snapshot.rows),
                keys=snapshot.keys,
            )
            for snapshot in fixture.db
        ),
        ui=normalize_value(fixture.ui) if fixture.ui is not None else None,
    )


def _check_string(value: str, where: str) -> None:
    if value == _REDACTED:
        return
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(value):
            raise RedactionError(f"{where}: looks like a {label} ({value[:24]}…)")
    if value in {"Bearer", "Basic"}:
        raise RedactionError(f"{where}: bare auth scheme without a redaction marker")


def _walk(value: Any, where: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{where}.{key}"
            if str(key).lower() in _SECRET_FIELDS and isinstance(item, str):
                if item and item != _REDACTED:
                    raise RedactionError(
                        f"{child}: secret field must be '{_REDACTED}' or empty"
                    )
                continue
            _walk(item, child)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _walk(item, f"{where}[{index}]")
    elif isinstance(value, str):
        _check_string(value, where)


def assert_redacted(fixture: Fixture) -> None:
    """Raise ``RedactionError`` if any part of the fixture looks like a secret."""
    where = f"fixture<{fixture.flow}:{fixture.revision}>"
    for index, exchange in enumerate(fixture.http):
        _walk(exchange.request, f"{where}.http[{index}].request")
        _walk(exchange.response, f"{where}.http[{index}].response")
    for index, stream in enumerate(fixture.sse):
        for event_index, event in enumerate(stream.events):
            _walk(event.payload, f"{where}.sse[{index}].events[{event_index}].payload")
    for index, snapshot in enumerate(fixture.db):
        for row_index, row in enumerate(snapshot.rows):
            _walk(row, f"{where}.db[{index}].rows[{row_index}]")
    if fixture.ui is not None:
        _walk(fixture.ui, f"{where}.ui")
