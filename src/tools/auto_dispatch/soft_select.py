"""Soft-weighted worker sampling (R6 L4, N5; mechanism after Fugu's soft labels).

Instead of always taking the top of a deterministic ranking, the selector can
sample among the already-eligible workers with weights

    w(m) = softmax(rate(m) / temperature) * headroom(m) * preference(m)

where ``rate`` is the smoothed success rate of model ``m`` on this task
category (a Beta prior, so an untried model is still tried -- the cold-start
fix), ``headroom`` is the remaining quota fraction and ``preference`` is a
gentle boost for the owner's registry order. Nothing here is learned or
trained; it is counting plus a fixed formula.

``CONDUCTOR_DISPATCH_SELECTOR_MODE`` = ``off`` | ``shadow`` | ``enforce``
(unset: ``shadow`` with a stored role registry, ``off`` without one):
``shadow`` computes and records the soft pick but the selector returns its
usual choice; ``enforce`` returns the soft pick.
"""

from __future__ import annotations

import math
import os
import random
from dataclasses import dataclass, field
from typing import Mapping, Sequence

SELECTOR_MODE_ENV = "CONDUCTOR_DISPATCH_SELECTOR_MODE"
TEMPERATURE_ENV = "CONDUCTOR_DISPATCH_SELECTOR_TEMPERATURE"
MODES = ("off", "shadow", "enforce")
DEFAULT_MODE = "shadow"
DEFAULT_TEMPERATURE = 0.15
# Beta(1, 1) prior: an untried model reads as 0.5, between a proven and a
# failing one, so it gets sampled sometimes.
PRIOR_PASSED = 1.0
PRIOR_RUNS = 2.0
PREFERENCE_STEP = 0.1
PREFERENCE_FLOOR = 0.5


def selector_mode(env: Mapping[str, str] | None = None) -> str:
    """The soft selector's mode.

    Unset (review fix 3): ``shadow`` only with a stored role registry, else
    ``off`` -- an install with no registry selects and reads stats exactly as
    main. An unknown value is ``shadow``; an explicit value is used as given.
    """
    source = os.environ if env is None else env
    raw = str(source.get(SELECTOR_MODE_ENV, "") or "").strip().lower()
    if not raw:
        from src.server.services.autonomy.role_types import stored_registry

        return DEFAULT_MODE if stored_registry() is not None else "off"
    return raw if raw in MODES else DEFAULT_MODE


def temperature(env: Mapping[str, str] | None = None) -> float:
    source = os.environ if env is None else env
    try:
        value = float(source.get(TEMPERATURE_ENV, "") or DEFAULT_TEMPERATURE)
    except ValueError:
        return DEFAULT_TEMPERATURE
    return value if value > 0 else DEFAULT_TEMPERATURE


@dataclass(frozen=True)
class Candidate:
    name: str
    passed: float = 0.0
    runs: float = 0.0
    headroom: float = 1.0


@dataclass
class SoftChoice:
    choice: str | None
    weights: dict[str, float] = field(default_factory=dict)
    rates: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "choice": self.choice,
            "weights": {k: round(v, 4) for k, v in self.weights.items()},
            "rates": {k: round(v, 4) for k, v in self.rates.items()},
        }


def smoothed_rate(passed: float, runs: float) -> float:
    return (max(passed, 0.0) + PRIOR_PASSED) / (max(runs, 0.0) + PRIOR_RUNS)


def preference_weight(name: str, preference: Sequence[str]) -> float:
    """1 + step for the first preferred name, falling to 1.0; unlisted names 1.0."""
    order = [str(item).strip().lower() for item in preference if str(item).strip()]
    if name not in order:
        return 1.0
    rank = order.index(name)
    return max(1.0 + PREFERENCE_STEP * (len(order) - rank) / max(len(order), 1), PREFERENCE_FLOOR)


def soft_weights(
    candidates: Sequence[Candidate],
    *,
    temp: float = DEFAULT_TEMPERATURE,
    preference: Sequence[str] = (),
) -> SoftChoice:
    """Normalised sampling weights over ``candidates`` (no pick yet)."""
    if not candidates:
        return SoftChoice(choice=None)
    rates = {c.name: smoothed_rate(c.passed, c.runs) for c in candidates}
    top = max(rates.values())
    raw: dict[str, float] = {}
    for c in candidates:
        logit = math.exp((rates[c.name] - top) / temp)
        raw[c.name] = logit * max(min(c.headroom, 1.0), 0.0) * preference_weight(c.name, preference)
    total = sum(raw.values())
    if total <= 0:
        # Every headroom read zero: fall back to the rate alone.
        raw = {c.name: math.exp((rates[c.name] - top) / temp) for c in candidates}
        total = sum(raw.values())
    weights = {name: value / total for name, value in raw.items()}
    return SoftChoice(choice=None, weights=weights, rates=rates)


def weighted_pick(
    candidates: Sequence[Candidate],
    *,
    rng: random.Random,
    temp: float = DEFAULT_TEMPERATURE,
    preference: Sequence[str] = (),
) -> SoftChoice:
    """Sample one candidate by :func:`soft_weights`; order of ``candidates`` breaks ties."""
    result = soft_weights(candidates, temp=temp, preference=preference)
    if not result.weights:
        return result
    roll = rng.random()
    acc = 0.0
    names = [c.name for c in candidates]
    for name in names:
        acc += result.weights[name]
        if roll < acc:
            result.choice = name
            return result
    result.choice = names[-1]
    return result
