"""Dual-write entry point for the refactor's append-only event stream.

``record_event`` is what call sites (turn lifecycle, permission checks, goal
moves) will call in Phase 1+: it validates the vocabulary, redacts the payload,
bounds its size, and writes through ``database/refactor/events.py`` when the
``events`` flag is on. Flag off means the call is a no-op returning ``None`` —
the old path keeps serving, exactly as the dual-write contract requires.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from src.server.contracts.refactor import ContractError, ensure_event_type
from src.server.database.refactor import events as db_events
from src.server.services.refactor.redaction import redact_event_payload
from src.server.services.refactor_flags import refactor_flag_enabled

#: A single event is not a place to carry tool output; anything larger is a bug
#: in the producer (use provenance ids instead), so fail loudly rather than
#: truncating silently.
MAX_PAYLOAD_BYTES = 16_384


def _encoded_size(payload: Mapping[str, Any]) -> int:
    """The size of an already-redacted payload, in bytes."""
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return len(encoded.encode("utf-8"))


def event_payload_bytes(payload: Mapping[str, Any] | None) -> int:
    """The size a payload has once it is written, in bytes.

    Redaction first, then compact JSON: this is the number the writer
    computes, so a producer that has to stay under the cap measures the same
    way the writer does. A producer that guessed would size its payload
    against a rule that is not the one applied.
    """
    return _encoded_size(redact_event_payload(dict(payload or {})))


async def record_event(
    *,
    user_id: str,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    thread_id: str | None = None,
    response_id: str | None = None,
    task_run_id: str | None = None,
    seq: int | None = None,
) -> dict[str, Any] | None:
    """Record one event, or ``None`` when the ``events`` flag is off."""
    if not refactor_flag_enabled("events"):
        return None
    ensure_event_type(event_type)
    redacted = redact_event_payload(dict(payload or {}))
    if _encoded_size(redacted) > MAX_PAYLOAD_BYTES:
        raise ContractError(
            f"event payload exceeds {MAX_PAYLOAD_BYTES} bytes after redaction"
        )
    return await db_events.append_event(
        user_id=user_id,
        event_type=event_type,
        payload=redacted,
        thread_id=thread_id,
        response_id=response_id,
        task_run_id=task_run_id,
        seq=seq,
    )
