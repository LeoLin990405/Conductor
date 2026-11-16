"""Rollout switch for N73 assignment (PRD 4.6 and §6.1).

``CONDUCTOR_DISPATCH_ASSIGN_MODE`` is ``off``, ``shadow`` or ``enforce``,
default ``shadow`` (N58). A value that is not a mode falls back to ``shadow``:
a typo must neither enforce the chooser nor switch it off.

``CONDUCTOR_DISPATCH_ASSIGN_SAMPLE`` is a fraction in ``[0, 1]``, default
``1.0``. The sample is a sha256 of the line id, so the same line always falls
on the same side of a given rate. ``adopts_superagent`` is true only in
``enforce`` and only when the line is inside the sample. ``shadow`` still
records a choice, and ``off`` does not call the super-agent; neither adopts it.

``rollback_due`` reads the §6.2 metric mapping. It fires only when the
super-agent pass rate is more than 10 percentage points below the selector
group, or the cost per line is more than 50 percent higher. Review rounds,
time, feasibility, fallback and call overhead are recognized and never fire
this rule. The §6.2 figures (5 points, 0.3 rounds, 15 percent, 90 percent
feasible, 30 s) decide promotion, not rollback. A missing side, a non-finite
number, a zero selector cost, or an ``insufficient`` cell does not fire:
absent usage is not a cost of zero.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Mapping
from decimal import Decimal
from math import isfinite
from typing import Any

from src.conductor_cli.rollout_modes import (
    DEFAULT_MODE,
    ENFORCE,
    MODES,
    OFF,
    SHADOW,
    normalize_mode,
)

logger = logging.getLogger(__name__)

ASSIGN_MODE_ENV = "CONDUCTOR_DISPATCH_ASSIGN_MODE"
ASSIGN_SAMPLE_ENV = "CONDUCTOR_DISPATCH_ASSIGN_SAMPLE"
DEFAULT_SAMPLE = 1.0

# §6.1 C. These are not the §6.2 promotion thresholds.
PASS_RATE_DROP_POINTS = 10.0
COST_RISE_FRACTION = 0.50

REASON_PASS_RATE_DROP = "pass_rate_drop"
REASON_COST_RISE = "cost_per_line_rise"

# §6.2 rows, in table order. Aliases accept the short English name and the
# SPEC's Chinese header so a report can use either.
SPEC_METRICS = (
    "pass_rate",
    "review_rounds",
    "cost_per_line",
    "time_per_line",
    "feasibility_rate",
    "fallback_rate",
    "call_overhead",
)
_METRIC_ALIASES = {
    "pass_rate": "pass_rate",
    "通过率": "pass_rate",
    "review_rounds": "review_rounds",
    "审查轮数": "review_rounds",
    "cost_per_line": "cost_per_line",
    "cost": "cost_per_line",
    "成本": "cost_per_line",
    "time_per_line": "time_per_line",
    "每条线耗时": "time_per_line",
    "feasibility_rate": "feasibility_rate",
    "feasibility": "feasibility_rate",
    "可行率": "feasibility_rate",
    "fallback_rate": "fallback_rate",
    "fallback": "fallback_rate",
    "回退率": "fallback_rate",
    "call_overhead": "call_overhead",
    "调用开销": "call_overhead",
}
_SUPER_KEYS = ("superagent", "treatment", "candidate")
_SELECTOR_KEYS = ("selector", "control", "baseline")
_VALUE_KEYS = ("median", "value", "rate", "mean")
_PASS_RATE_LIMIT = Decimal(str(PASS_RATE_DROP_POINTS))
_COST_RISE_LIMIT = Decimal(str(COST_RISE_FRACTION))

__all__ = [
    "ASSIGN_MODE_ENV",
    "ASSIGN_SAMPLE_ENV",
    "COST_RISE_FRACTION",
    "DEFAULT_MODE",
    "DEFAULT_SAMPLE",
    "ENFORCE",
    "MODES",
    "OFF",
    "PASS_RATE_DROP_POINTS",
    "REASON_COST_RISE",
    "REASON_PASS_RATE_DROP",
    "SHADOW",
    "SPEC_METRICS",
    "adopts_superagent",
    "assign_mode",
    "assign_sample",
    "line_sample_unit",
    "line_sampled",
    "normalize_mode",
    "rollback_due",
    "rollback_reasons",
]


def assign_mode(environ: Mapping[str, str] | None = None) -> str:
    """The assignment mode. ``shadow`` when unset or not a mode."""
    env = os.environ if environ is None else environ
    return normalize_mode(env.get(ASSIGN_MODE_ENV, ""), source=ASSIGN_MODE_ENV)


def assign_sample(environ: Mapping[str, str] | None = None) -> float:
    """The sample fraction in ``[0, 1]``. Invalid values fall back to ``1.0``."""
    env = os.environ if environ is None else environ
    if ASSIGN_SAMPLE_ENV not in env:
        return DEFAULT_SAMPLE
    text = str(env.get(ASSIGN_SAMPLE_ENV, "")).strip()
    if not text:
        return DEFAULT_SAMPLE
    try:
        value = float(text)
    except ValueError:
        value = None
    if (
        value is None
        or isinstance(value, bool)
        or not isfinite(value)
        or value < 0.0
        or value > 1.0
    ):
        logger.warning(
            "%s=%r is not a fraction in [0, 1]; using %s",
            ASSIGN_SAMPLE_ENV,
            text,
            DEFAULT_SAMPLE,
        )
        return DEFAULT_SAMPLE
    return value


def line_sample_unit(line_id: str) -> float:
    """Uniform value in ``[0, 1)`` from sha256(line id), the same for every process."""
    text = _line_id(line_id)
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def line_sampled(line_id: str, sample: float) -> bool:
    """Whether ``line_id`` falls inside ``sample``. ``0`` is none, ``1`` is every line."""
    rate = _sample_rate(sample)
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    return line_sample_unit(line_id) < rate


def adopts_superagent(
    line_id: str,
    *,
    mode: str | None = None,
    sample: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """True when enforce mode keeps this line's super-agent choice.

    Shadow and off never adopt it. An unsampled enforce line stays with the
    selector (``source=selector``).
    """
    resolved = (
        normalize_mode(mode, source=ASSIGN_MODE_ENV)
        if mode is not None
        else assign_mode(environ)
    )
    if resolved != ENFORCE:
        return False
    rate = assign_sample(environ) if sample is None else sample
    return line_sampled(line_id, rate)


def rollback_due(metrics: Mapping[str, Any]) -> bool:
    """True only for a pass-rate drop over 10 points or a cost-per-line rise over 50%.

    The readings are compared as decimals. An exact 10-point gap and an exact
    50 percent rise do not fire. A gap only just past either threshold does.
    """
    return bool(rollback_reasons(metrics))


def rollback_reasons(metrics: Mapping[str, Any]) -> tuple[str, ...]:
    """The rollback arms that fired, pass rate first, then cost. Empty when none did."""
    if not isinstance(metrics, Mapping):
        raise TypeError("metrics must be a mapping")
    # The first spelling of a metric wins. The tuple order does not: pass rate
    # is reported before cost even when the cost key was listed first.
    pass_drop: bool | None = None
    cost_rise: bool | None = None
    for key, value in metrics.items():
        metric = _METRIC_ALIASES.get(str(key))
        if metric == "pass_rate" and pass_drop is None:
            pass_drop = _pass_rate_drop(value)
        elif metric == "cost_per_line" and cost_rise is None:
            cost_rise = _cost_rise(value)
    fired: list[str] = []
    if pass_drop:
        fired.append(REASON_PASS_RATE_DROP)
    if cost_rise:
        fired.append(REASON_COST_RISE)
    return tuple(fired)


def _line_id(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("line_id must be a string")
    if value.strip() == "":
        raise ValueError("line_id is required")
    return value


def _sample_rate(sample: object) -> float:
    if isinstance(sample, bool) or not isinstance(sample, (int, float)):
        raise TypeError("sample must be a float in [0, 1]")
    rate = float(sample)
    if not isfinite(rate) or rate < 0.0 or rate > 1.0:
        raise ValueError("sample must be in [0, 1]")
    return rate


def _pass_rate_drop(value: object) -> bool:
    pair = _cohort_pair(value)
    if pair is None:
        return False
    super_rate, selector_rate = pair
    super_fraction = _rate_fraction(super_rate)
    selector_fraction = _rate_fraction(selector_rate)
    if super_fraction is None or selector_fraction is None:
        return False
    # Decimal readings, not a rounded grid: binary ``0.8 - 0.7`` is slightly
    # over a tenth and would trip an exact 10-point gap, while rounding to
    # millipoints hides a gap of 10.0001 points.
    drop = (selector_fraction - super_fraction) * 100
    return drop > _PASS_RATE_LIMIT


def _cost_rise(value: object) -> bool:
    pair = _cohort_pair(value)
    if pair is None:
        return False
    super_cost, selector_cost = pair
    if (
        not isfinite(super_cost)
        or not isfinite(selector_cost)
        or selector_cost <= 0.0
        or super_cost < 0.0
    ):
        return False
    rise = (Decimal(str(super_cost)) - Decimal(str(selector_cost))) / Decimal(
        str(selector_cost)
    )
    return rise > _COST_RISE_LIMIT


def _cohort_pair(value: object) -> tuple[float, float] | None:
    if isinstance(value, str) and value.strip().casefold() == "insufficient":
        return None
    if not isinstance(value, Mapping):
        return None
    status = value.get("status")
    if isinstance(status, str) and status.strip().casefold() == "insufficient":
        return None
    super_value = _number(_cohort(value, _SUPER_KEYS))
    selector_value = _number(_cohort(value, _SELECTOR_KEYS))
    if super_value is None or selector_value is None:
        return None
    return super_value, selector_value


def _cohort(value: Mapping[object, object], names: tuple[str, ...]) -> object:
    folded = {str(key).casefold(): inner for key, inner in value.items()}
    for name in names:
        if name.casefold() in folded:
            return folded[name.casefold()]
    return None


def _number(value: object) -> float | None:
    if isinstance(value, str) and value.strip().casefold() == "insufficient":
        return None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if isfinite(number) else None
    if isinstance(value, Mapping):
        status = value.get("status")
        if isinstance(status, str) and status.strip().casefold() == "insufficient":
            return None
        for key in _VALUE_KEYS:
            if key in value:
                return _number(value[key])
    return None


def _rate_fraction(value: float) -> Decimal | None:
    """A fraction in ``[0, 1]``. Numbers above 1 are percentages (``80`` is ``0.80``)."""
    if not isfinite(value):
        return None
    number = Decimal(str(value))
    if number > 1:
        number = number / Decimal(100)
    if number < 0 or number > 1:
        return None
    return number
