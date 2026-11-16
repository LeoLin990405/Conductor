"""Dynamic worker assignment inside the role registry (R6 L2, N2).

The registry's worker entry says who may take a line: its candidates (in the
owner's preference order) and its constraints. This module turns that into a
per-line decision the machine hands the selector:

* ``never`` -- roster names the registry excludes for this line: not a worker
  candidate, listed in ``constraints.excluded``, or lacking a capability the
  constraints require (``requires_session_resume``; a write line needs a
  write-capable worker, which ``can_take_line`` already enforces);
* ``preferred`` -- the candidate order, which the selector uses as a soft
  weight (owner preference). Headroom and recorded success are the
  selector's own inputs, so they are not duplicated here.

A legacy registry (``source == "fallback"``) is today's behaviour: no extra
exclusions, no preference. The Router role is per deployment or session, never
per line, so it is not chosen here. The same model may serve the Router and a
Worker only in different sessions (:func:`assert_distinct_sessions`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from src.server.services.autonomy.role_types import (
    ROLE_WORKER,
    SOURCE_FALLBACK,
    RoleRegistry,
    stored_registry,
)

NOT_A_CANDIDATE = "registry_not_candidate"
EXCLUDED = "registry_excluded"
NO_SESSION_RESUME = "registry_needs_session_resume"
SAME_SESSION = "router_worker_same_session"
DECISIONS_MAX = 8


class SessionConflict(RuntimeError):
    """A Worker launch would reuse the Router's session."""


@dataclass(frozen=True)
class AssignmentDecision:
    line_id: str
    source: str
    never: tuple[str, ...] = ()
    preferred: tuple[str, ...] = ()
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.source != SOURCE_FALLBACK

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id,
            "source": self.source,
            "never": list(self.never),
            "preferred": list(self.preferred),
            "reasons": dict(self.reasons),
        }


def _supports_resume(name: str) -> bool:
    try:
        from src.server.services.autonomy.roles import runtime_provider_id
        from src.server.services.native_runtime.registry import capabilities_for

        return bool(capabilities_for(runtime_provider_id(name)).session_resume)
    except Exception:
        # Unknown capability is not evidence of support.
        return False


def decide(
    line: Any,
    roster: Iterable[str],
    *,
    registry: RoleRegistry | None = None,
    resume_supported=_supports_resume,
) -> AssignmentDecision:
    """The registry's exclusions and preference for one work line."""
    from src.server.services.autonomy.roles import canonical_provider

    line_id = str(getattr(line, "id", "") or "")
    reg = registry or stored_registry()
    if reg is None or reg.source == SOURCE_FALLBACK:
        return AssignmentDecision(line_id=line_id, source=SOURCE_FALLBACK)
    entry = reg.entry(ROLE_WORKER)
    candidates = [canonical_provider(name) for name in entry.candidate_names]
    excluded = {canonical_provider(name) for name in entry.constraints.excluded}
    never: list[str] = []
    reasons: dict[str, str] = {}
    for raw in roster:
        name = canonical_provider(str(raw))
        if not name or name in reasons:
            continue
        if name not in candidates:
            reasons[name] = NOT_A_CANDIDATE
        elif name in excluded:
            reasons[name] = EXCLUDED
        elif entry.constraints.requires_session_resume and not resume_supported(name):
            reasons[name] = NO_SESSION_RESUME
        else:
            continue
        never.append(name)
    preferred = tuple(name for name in candidates if name not in reasons and name not in excluded)
    return AssignmentDecision(
        line_id=line_id,
        source=reg.source,
        never=tuple(never),
        preferred=preferred,
        reasons=reasons,
    )


def ticket_worker_allowed(
    provider: str, *, registry: RoleRegistry | None = None
) -> tuple[bool, str | None]:
    """Whether a Ticket may launch on ``provider`` under the registry."""
    from src.server.services.autonomy.roles import canonical_provider

    reg = registry or stored_registry()
    if reg is None or reg.source == SOURCE_FALLBACK or not str(provider or "").strip():
        return True, None
    entry = reg.entry(ROLE_WORKER)
    name = canonical_provider(provider)
    if name not in {canonical_provider(n) for n in entry.candidate_names}:
        return False, NOT_A_CANDIDATE
    if name in {canonical_provider(n) for n in entry.constraints.excluded}:
        return False, EXCLUDED
    return True, None


def assert_distinct_sessions(router_session: str | None, worker_session: str | None) -> None:
    """The Router and a Worker may share a model, never a session."""
    a = str(router_session or "").strip()
    b = str(worker_session or "").strip()
    if a and b and a == b:
        raise SessionConflict(SAME_SESSION)


def record(snapshot: dict[str, Any] | None, decision: AssignmentDecision) -> None:
    """Keep the last few decisions on the line snapshot (only when active)."""
    if snapshot is None or not decision.active:
        return
    entries = snapshot.setdefault("registry_assignment", [])
    if not isinstance(entries, list):
        entries = []
        snapshot["registry_assignment"] = entries
    entries.append(decision.to_dict())
    del entries[:-DECISIONS_MAX]
