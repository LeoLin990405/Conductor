"""Waker contract: one switchable source, one redacted wake event.

A waker turns an outside signal into a ``WakeEvent``. It does not park work
or open Tickets itself. Those decisions stay in scheduling, and the effect
queue is what makes a wake survive a restart. Payloads are scrubbed here, at
the boundary, so a later store never sees a credential that arrived in the
event.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from src.server.services.autonomy.security import (
    redact_external_data,
    redact_external_text,
)

# Effect kind for durable wakes. Kept here so models.py stays outside this line.
EFFECT_WAKE = "wake"

# Worker ids, not CodexBar's spellings. Daemon claims and ticket rows use the
# worker id, so zai/zcode and deepseek-dsh have to land on the same key.
_PROVIDER_ALIASES = {
    "arkcli": "doubao",
    "claude-code": "claude",
    "claudedev": "claude",
    "codexrev": "codex",
    "deepseek-dsh": "deepseek",
    "dsh": "deepseek",
    "minimax-mmx": "minimax",
    "mmx": "minimax",
    "moonshot": "kimi",
    "openai": "codex",
    "openai-codex": "codex",
    "step": "stepfun",
    "zai": "glm",
    "zcode": "glm",
    "zhipu": "glm",
}

_SECRET_KEY = re.compile(
    r"(?i)(authorization|cookie|set-cookie|token|secret|password|"
    r"api[_-]?key|credential|private[_-]?key|session)"
)
_MAX_TEXT = 500
_MAX_DEPTH = 6
_MAX_ITEMS = 40


def canonical_provider_id(provider: object) -> str:
    """One provider id for quota state, tickets, and daemon claims."""
    if not isinstance(provider, str):
        return ""
    text = provider.strip().lower()
    if not text or len(text) > 80:
        return ""
    return _PROVIDER_ALIASES.get(text, text)


def provider_spellings(provider: object) -> tuple[str, ...]:
    """Every stored spelling that canonicalizes to this provider."""
    canonical = canonical_provider_id(provider)
    if not canonical:
        return ()
    names = {canonical}
    if isinstance(provider, str) and provider.strip():
        names.add(provider.strip().lower())
    for alias, target in _PROVIDER_ALIASES.items():
        if target == canonical:
            names.add(alias)
    return tuple(sorted(names))


def wake_idempotency_key(source: str, kind: str, subject: str, external_id: str) -> str:
    """Stable effect key. A long or multiline id is hashed so the row stays bounded."""
    raw = f"waker:{source}:{kind}:{subject}:{external_id}"
    if len(raw) <= 240 and "\n" not in raw and "\r" not in raw:
        return raw
    digest = hashlib.sha256(raw.encode()).hexdigest()[:32]
    return f"waker:{source}:{kind}:{digest}"


def redact_wake_payload(value: object, *, _depth: int = 0) -> object:
    """Drop credential-shaped keys and scrub the strings that remain.

    Key redaction runs before the text scrubber: a JSON key named ``token``
    is not the ``token: value`` shape that scrubber looks for.
    """
    if _depth > _MAX_DEPTH:
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_external_text(value)[:_MAX_TEXT]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [
            redact_wake_payload(item, _depth=_depth + 1) for item in value[:_MAX_ITEMS]
        ]
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                break
            name = str(key)
            if _SECRET_KEY.search(name):
                cleaned[redact_external_text(name)[:_MAX_TEXT]] = "[REDACTED]"
                continue
            cleaned_key = redact_external_text(name)[:_MAX_TEXT]
            cleaned[cleaned_key] = redact_wake_payload(item, _depth=_depth + 1)
        return redact_external_data(cleaned)
    return redact_external_text(value)[:_MAX_TEXT]


@dataclass(frozen=True, slots=True)
class WakeEvent:
    """One redacted wake. The idempotency key is the effect's identity."""

    source: str
    kind: str
    idempotency_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    provider: str = ""
    repository: str = ""
    line_id: str = ""
    ticket_id: str = ""
    user_id: str = ""
    workspace_id: str = ""
    observed_at: str = ""
    # Ticket id the previous page of a provider-wide quota pass already
    # covered. The next effect reads strictly after it, so a full page
    # continues instead of scanning the same rows again.
    quota_after_ticket_id: str = ""
    # Origin wake whose observation rank this page inherits. Continuations
    # keep the first page's effect id so a later created_at cannot outrank
    # a same-timestamp reset that already beat the origin.
    quota_rank_effect_id: str = ""
    # Set from the claimed effect row at apply time. Not part of the
    # durable payload: it identifies this wake for observation ranking
    # when ``quota_rank_effect_id`` is empty.
    effect_id: str = ""

    def ranking_effect_id(self) -> str:
        """Effect id used for provider-history fences."""
        return str(self.quota_rank_effect_id or self.effect_id or "").strip()

    def as_payload(self) -> dict[str, Any]:
        body = {
            "source": self.source,
            "kind": self.kind,
            "provider": self.provider,
            "repository": self.repository,
            "line_id": self.line_id,
            "ticket_id": self.ticket_id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "observed_at": self.observed_at,
            "idempotency_key": self.idempotency_key,
            "quota_after_ticket_id": self.quota_after_ticket_id,
            "quota_rank_effect_id": self.quota_rank_effect_id,
            "payload": dict(self.payload),
        }
        redacted = redact_wake_payload(body)
        return dict(redacted) if isinstance(redacted, dict) else {}

    @classmethod
    def from_payload(cls, payload: object) -> "WakeEvent":
        raw = payload if isinstance(payload, dict) else {}
        inner = raw.get("payload")
        return cls(
            source=str(raw.get("source") or ""),
            kind=str(raw.get("kind") or ""),
            idempotency_key=str(raw.get("idempotency_key") or ""),
            payload=dict(inner) if isinstance(inner, dict) else {},
            provider=canonical_provider_id(raw.get("provider")),
            repository=str(raw.get("repository") or ""),
            line_id=str(raw.get("line_id") or ""),
            ticket_id=str(raw.get("ticket_id") or ""),
            user_id=str(raw.get("user_id") or ""),
            workspace_id=str(raw.get("workspace_id") or ""),
            observed_at=str(raw.get("observed_at") or ""),
            quota_after_ticket_id=str(raw.get("quota_after_ticket_id") or ""),
            quota_rank_effect_id=str(raw.get("quota_rank_effect_id") or ""),
        )


@dataclass(frozen=True, slots=True)
class WakerFailure:
    """A waker that raised. The message is already redacted."""

    waker: str
    error: str


@dataclass(frozen=True, slots=True)
class DispatchReport:
    """What one pass over the wakers produced. A failure does not drop the rest."""

    events: tuple[WakeEvent, ...]
    failures: tuple[WakerFailure, ...]
    skipped: tuple[str, ...]


@runtime_checkable
class Waker(Protocol):
    """One wake source. ``enabled`` is the per-waker switch."""

    name: str

    def enabled(self) -> bool: ...

    def collect(self, raw: object) -> list[WakeEvent]: ...


def dispatch_wakers(
    wakers: list[Waker], raw_by_name: dict[str, object] | None = None
) -> DispatchReport:
    """Run each enabled waker. One failure is recorded and the next waker still runs."""
    events: list[WakeEvent] = []
    failures: list[WakerFailure] = []
    skipped: list[str] = []
    raw = raw_by_name or {}
    for waker in wakers:
        if not waker.enabled():
            skipped.append(waker.name)
            continue
        try:
            found = waker.collect(raw.get(waker.name))
        except Exception as exc:  # noqa: BLE001 - isolation is the contract
            failures.append(
                WakerFailure(waker.name, redact_external_text(str(exc))[:_MAX_TEXT])
            )
            continue
        events.extend(found)
    return DispatchReport(
        events=tuple(events), failures=tuple(failures), skipped=tuple(skipped)
    )
