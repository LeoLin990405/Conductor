"""CodexBar quota wakes: exhausted parks, reset requeues, low is only recorded.

The snapshot reader already classifies a provider as available, low, exhausted,
or unknown. This module only decides which of those changes is a wake. An
available reading does not requeue anything unless that provider is known
exhausted, so a routine poll cannot put parked work back just because the file
was refreshed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.server.services.autonomy.wakers.base import (
    WakeEvent,
    canonical_provider_id,
    wake_idempotency_key,
)

_STATE_KIND = {
    "exhausted": "quota_exhausted",
    "low": "quota_low",
    "available": "quota_reset",
    "quota_exhausted": "quota_exhausted",
    "quota_low": "quota_low",
    "quota_reset": "quota_reset",
}
_EXPLICIT_KINDS = frozenset({"quota_exhausted", "quota_low", "quota_reset"})


class CodexBarWaker:
    """Switchable CodexBar source. Disabled collects nothing, including bad input."""

    name = "codexbar"

    def __init__(self, *, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def enabled(self) -> bool:
        return self._enabled

    def collect(self, raw: object) -> list[WakeEvent]:
        if not self._enabled or raw is None:
            return []
        if isinstance(raw, list):
            events: list[WakeEvent] = []
            for item in raw[:50]:
                events.extend(self.collect(item))
            return events
        if not isinstance(raw, Mapping):
            return []
        explicit = explicit_quota_event(raw)
        if explicit is not None:
            return [explicit]
        return quota_transitions(
            _kind_map(raw.get("previous")),
            _state_map(raw.get("states", raw.get("providers"))),
            exhausted=_provider_set(raw.get("exhausted")),
            observed_at=str(raw.get("observed_at") or ""),
            user_id=str(raw.get("user_id") or ""),
            line_id=str(raw.get("line_id") or ""),
            ticket_id=str(raw.get("ticket_id") or ""),
        )


def explicit_quota_event(raw: Mapping[str, Any]) -> WakeEvent | None:
    """One named quota event. A snapshot-shaped mapping returns None."""
    kind = _STATE_KIND.get(str(raw.get("kind") or "").strip().lower(), "")
    if kind not in _EXPLICIT_KINDS:
        return None
    provider = canonical_provider_id(raw.get("provider"))
    if not provider:
        return None
    observed_at = str(raw.get("observed_at") or "")
    external = str(raw.get("event_id") or raw.get("id") or observed_at or "unspecified")
    return _event(
        kind=kind,
        provider=provider,
        external_id=external,
        observed_at=observed_at,
        user_id=str(raw.get("user_id") or ""),
        line_id=str(raw.get("line_id") or ""),
        ticket_id=str(raw.get("ticket_id") or ""),
        extra={"resets_at": str(raw.get("resets_at") or "")},
    )


def quota_transitions(
    previous: Mapping[str, str],
    current: Mapping[str, str],
    *,
    exhausted: set[str],
    observed_at: str,
    user_id: str = "",
    line_id: str = "",
    ticket_id: str = "",
) -> list[WakeEvent]:
    """Emit a wake only when the classified state changed in a way that matters."""
    events: list[WakeEvent] = []
    known_exhausted = {canonical_provider_id(item) for item in exhausted}
    known_exhausted.discard("")
    for provider, state in list(current.items())[:50]:
        canonical = canonical_provider_id(provider)
        kind = _STATE_KIND.get(str(state or "").strip().lower(), "")
        if not canonical or not kind:
            continue
        old = _STATE_KIND.get(str(previous.get(canonical) or "").strip().lower(), "")
        if not old:
            old = _STATE_KIND.get(str(previous.get(provider) or "").strip().lower(), "")
        if kind == old:
            continue
        if (
            kind == "quota_reset"
            and canonical not in known_exhausted
            and old != "quota_exhausted"
        ):
            continue
        external = f"{old or 'none'}:{observed_at or 'unspecified'}"
        events.append(
            _event(
                kind=kind,
                provider=canonical,
                external_id=external,
                observed_at=observed_at,
                user_id=user_id,
                line_id=line_id,
                ticket_id=ticket_id,
            )
        )
    return events


def _event(
    *,
    kind: str,
    provider: str,
    external_id: str,
    observed_at: str,
    user_id: str,
    line_id: str,
    ticket_id: str,
    extra: dict[str, str] | None = None,
) -> WakeEvent:
    payload: dict[str, Any] = {"provider": provider, "kind": kind}
    if extra:
        payload.update(extra)
    return WakeEvent(
        source="codexbar",
        kind=kind,
        idempotency_key=wake_idempotency_key("codexbar", kind, provider, external_id),
        payload=payload,
        provider=provider,
        line_id=line_id,
        ticket_id=ticket_id,
        user_id=user_id,
        observed_at=observed_at,
    )


def _kind_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, item in list(value.items())[:50]:
        canonical = canonical_provider_id(key)
        if canonical:
            out[canonical] = str(item or "")
    return out


def _state_map(value: object) -> dict[str, str]:
    return _kind_map(value)


def _provider_set(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, (set, list, tuple)):
        return {str(item) for item in list(value)[:50]}
    return set()
