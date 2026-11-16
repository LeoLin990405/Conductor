"""Domain values shared by the autonomy services and their tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class EffectRecord:
    """One durable effect claimed from ``autonomy_effects``."""

    effect_id: str
    user_id: str
    kind: str
    idempotency_key: str
    payload: dict[str, Any]
    attempts: int
    due_at: datetime | None = None
    lease_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class EffectResult:
    """Outcome of executing one effect, returned by a handler."""

    ok: bool
    retryable: bool = True
    error: str = ""


@dataclass(frozen=True, slots=True)
class TriggerDraft:
    """Source-owned event before it becomes a stored Trigger."""

    external_id: str
    kind: str
    thread_key: str
    actor_login: str
    actor_is_bot: bool
    addressed: bool
    text: str
    url: str
    payload: dict[str, Any] = field(default_factory=dict)


# Stable kind names. Plain strings are used on the wire/SQL so an older worker
# can safely ignore a kind it does not know (the attempt is nacked, not lost).
EFFECT_POLL_SOURCE = "poll_source"
EFFECT_ROUTER_TURN = "router_turn"
EFFECT_LAUNCH_WORKER = "launch_worker"
EFFECT_DELIVER_INPUT = "deliver_input"
EFFECT_RELAY_REPORT = "relay_report"
EFFECT_REPLY = "reply"
EFFECT_TICKET_STATE = "ticket_state"
EFFECT_WATCHDOG_REVIEW = "watchdog_review"
EFFECT_MAIN_AGENT_ADVANCE = "main_agent_advance"
