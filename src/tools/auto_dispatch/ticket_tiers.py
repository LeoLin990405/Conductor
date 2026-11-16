"""Fast and deep Ticket tiers with automatic downgrade (R6 L4, N5).

* ``fast``: one worker, no planner split, primary review only.
* ``deep``: planner split, several workers, secondary review on risk.

A ``deep`` request is downgraded to ``fast`` when quota is low: the tightest
remaining-quota reading among the providers the Ticket would use is under
``CONDUCTOR_TICKET_DEEP_MIN_HEADROOM`` (default 0.25, the usage-plan "low"
tier), or the usage-plan tier says ``low``/``exhausted``. A downgrade is
never silent: the result carries the reason. ``fast`` is never upgraded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Mapping

FAST = "fast"
DEEP = "deep"
TIERS = (FAST, DEEP)
DEEP_MIN_HEADROOM_ENV = "CONDUCTOR_TICKET_DEEP_MIN_HEADROOM"
DEFAULT_DEEP_MIN_HEADROOM = 0.25
LOW_USAGE_TIERS = frozenset({"low", "exhausted", "critical"})


@dataclass(frozen=True)
class TierPlan:
    tier: str
    requested: str
    downgraded: bool = False
    reason: str = ""
    single_worker: bool = True
    planner_split: bool = False
    secondary_review: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "requested": self.requested,
            "downgraded": self.downgraded,
            "reason": self.reason,
            "single_worker": self.single_worker,
            "planner_split": self.planner_split,
            "secondary_review": self.secondary_review,
        }


def _min_headroom(env: Mapping[str, str] | None) -> float:
    source = os.environ if env is None else env
    try:
        value = float(source.get(DEEP_MIN_HEADROOM_ENV, "") or DEFAULT_DEEP_MIN_HEADROOM)
    except ValueError:
        return DEFAULT_DEEP_MIN_HEADROOM
    return min(max(value, 0.0), 1.0)


def normalise_tier(value: str | None) -> str:
    text = str(value or "").strip().lower()
    return text if text in TIERS else DEEP if text in {"ultra", "max"} else FAST


def _plan(tier: str, requested: str, *, downgraded: bool = False, reason: str = "") -> TierPlan:
    deep = tier == DEEP
    return TierPlan(
        tier=tier,
        requested=requested,
        downgraded=downgraded,
        reason=reason,
        single_worker=not deep,
        planner_split=deep,
        secondary_review=deep,
    )


def choose_tier(
    requested: str | None,
    *,
    headroom: Mapping[str, float] | None = None,
    providers: Iterable[str] = (),
    usage_tier: str | None = None,
    env: Mapping[str, str] | None = None,
) -> TierPlan:
    """The tier to run, downgrading ``deep`` to ``fast`` on low quota."""
    asked = normalise_tier(requested)
    if asked == FAST:
        return _plan(FAST, asked)
    tier_name = str(usage_tier or "").strip().lower()
    if tier_name in LOW_USAGE_TIERS:
        return _plan(FAST, asked, downgraded=True, reason=f"usage_tier_{tier_name}")
    readings = dict(headroom or {})
    names = [str(p).strip().lower() for p in providers if str(p).strip()] or list(readings)
    values = [float(readings[name]) for name in names if name in readings]
    floor = _min_headroom(env)
    if values and min(values) < floor:
        tightest = min(names, key=lambda n: readings.get(n, 1.0))
        return _plan(
            FAST,
            asked,
            downgraded=True,
            reason=f"low_headroom:{tightest}={readings[tightest]:.2f}<{floor:.2f}",
        )
    return _plan(DEEP, asked)
