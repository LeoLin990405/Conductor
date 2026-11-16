"""Role, session and step on dispatch events (R6 L4, N5).

After dynamic assignment an event must say which role produced it, in which
session and at which step of the line, or the audit chain breaks (the same
model may be a Worker in one session and the Router in another).

Events R6 adds (registry decisions, debug-switch plans, the service-role
chain, Router handoffs) always carry the three keys. The events the machine
already recorded before R6 get them when ``CONDUCTOR_DISPATCH_EVENT_TAGS`` is
on; it is off by default so the saved rows keep main's exact shape until the
golden traces are re-recorded (N58: ship observable, switch on deliberately).
"""

from __future__ import annotations

import os
from typing import Any, Mapping, MutableMapping

ENV = "CONDUCTOR_DISPATCH_EVENT_TAGS"
ROLES = frozenset({"router", "planner", "reviewer", "worker", "system"})
STEP_KEY = "event_step"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return str(source.get(ENV, "") or "").strip().lower() in _TRUTHY


def tags(role: str, session: str | None, step: int) -> dict[str, Any]:
    role = role if role in ROLES else "system"
    return {"role": role, "session": str(session or ""), "step": int(step)}


def next_step(snapshot: MutableMapping[str, Any] | None) -> int:
    """A monotonic per-line step counter kept on the line snapshot."""
    if snapshot is None:
        return 0
    try:
        current = int(snapshot.get(STEP_KEY) or 0)
    except (TypeError, ValueError):
        current = 0
    snapshot[STEP_KEY] = current + 1
    return current + 1


def tag(
    event: MutableMapping[str, Any],
    *,
    role: str,
    session: str | None,
    snapshot: MutableMapping[str, Any] | None = None,
    force: bool = False,
    env: Mapping[str, str] | None = None,
) -> MutableMapping[str, Any]:
    """Add role/session/step to ``event`` (legacy events only when enabled)."""
    if not (force or enabled(env)):
        return event
    for key, value in tags(role, session, next_step(snapshot)).items():
        event.setdefault(key, value)
    return event
