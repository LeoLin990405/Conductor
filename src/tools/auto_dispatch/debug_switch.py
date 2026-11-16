"""Fresh eyes at debug points (R6 L4/N5; mechanism after Fugu's build->debug switch).

When a line is stuck at a debug point -- G2 (focused tests) failing again, a
review BLOCK again, a park -- the same author retrying the same session tends
to repeat itself. The policy here hands the line to a *different* model in a
*new* session on the second consecutive failure at the same point; the first
failure stays a same-author fix, because the author still has the context.

``CONDUCTOR_DISPATCH_DEBUG_SWITCH_MODE`` = ``off`` | ``shadow`` | ``enforce``
(default ``shadow``, N58). ``shadow`` only logs and keeps the plan in memory on
the machine (persisted rows keep main's shape); ``enforce`` reassigns through
the machine's normal reassign path, which already excludes the outgoing author
and starts a fresh run. The author-never-reviews rule is untouched: a switch
changes the author, and review still excludes whoever wrote the code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

MODE_ENV = "CONDUCTOR_DISPATCH_DEBUG_SWITCH_MODE"
MODES = ("off", "shadow", "enforce")
DEFAULT_MODE = "shadow"
G2_FAILED = "g2_failed"
REVIEW_BLOCKED = "review_blocked"
BLOCKED = "blocked"
DEBUG_POINTS = frozenset({G2_FAILED, REVIEW_BLOCKED, BLOCKED})
SWITCH_AFTER = 1  # prior failures at the same point before a switch
AUDIT_MAX = 32


def mode(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    value = str(source.get(MODE_ENV, "") or "").strip().lower()
    return value if value in MODES else DEFAULT_MODE


@dataclass(frozen=True)
class SwitchPlan:
    point: str
    line_id: str
    from_model: str
    prior_failures: int
    switch: bool
    reason: str
    role: str = "worker"
    session: str = ""
    step: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "point": self.point,
            "line_id": self.line_id,
            "from_model": self.from_model,
            "prior_failures": self.prior_failures,
            "switch": self.switch,
            "reason": self.reason,
            "role": self.role,
            "session": self.session,
            "step": self.step,
        }


def plan(
    point: str,
    line: Any,
    prior_failures: int,
    *,
    session: str = "",
    step: int = 0,
) -> SwitchPlan:
    """Whether this debug point should move the line to a different model."""
    if point not in DEBUG_POINTS:
        raise ValueError(f"unknown debug point: {point}")
    author = str(getattr(line, "assigned_model", "") or "")
    switch = bool(author) and prior_failures >= SWITCH_AFTER
    reason = (
        f"debug_switch:{point}:after_{prior_failures}"
        if switch
        else f"same_author_fix:{point}:after_{prior_failures}"
    )
    return SwitchPlan(
        point=point,
        line_id=str(getattr(line, "id", "") or ""),
        from_model=author,
        prior_failures=prior_failures,
        switch=switch,
        reason=reason,
        session=session,
        step=step,
    )
