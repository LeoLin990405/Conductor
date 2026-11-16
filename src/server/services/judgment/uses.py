"""Registry of judgment uses: mode, thresholds, version and fallback (N61 4.1).

The thresholds are placeholders. The LLM backend is uncalibrated, so they
stay placeholders until ``scripts/judgment_eval.py`` has shadow samples.
Changing a threshold or a question means bumping the use's ``version``.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from types import MappingProxyType

from src.conductor_cli.rollout_modes import check_mode, normalize_mode

WATCHDOG_PROGRESS = "watchdog_progress"  # U1
COMPLETION_EVIDENCE = "completion_evidence"  # U2
ROUTER_PRESELECT = "router_preselect"  # U3


class Fallback(StrEnum):
    """What a flow does when the judgment is ERROR."""

    KEEP_PATH = "keep_path"
    FAIL_CLOSED = "fail_closed"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class UseSpec:
    name: str
    version: str
    lo: float
    hi: float
    margin: float
    fallback: Fallback
    default_mode: str = "shadow"


# Every use samples this share of its keys unless
# ``CONDUCTOR_JUDGMENT_<USE>_SAMPLE`` says otherwise: shadow judgments cost
# model calls, so v1 judges one subject in ten.
DEFAULT_SAMPLE = 0.1
# ``off`` here turns every use off, whatever its own mode says.
GLOBAL_MODE_ENV = "CONDUCTOR_JUDGMENT_MODE"

# Placeholder thresholds: uncalibrated until evaluated (PRD 4.4, risk 7).
_LO, _HI, _MARGIN = 0.2, 0.8, 0.15

USES: Mapping[str, UseSpec] = MappingProxyType(
    {
        spec.name: spec
        for spec in (
            UseSpec(WATCHDOG_PROGRESS, "1", _LO, _HI, _MARGIN, Fallback.ESCALATE),
            UseSpec(COMPLETION_EVIDENCE, "1", _LO, _HI, _MARGIN, Fallback.KEEP_PATH),
            UseSpec(ROUTER_PRESELECT, "1", _LO, _HI, _MARGIN, Fallback.KEEP_PATH),
        )
    }
)


def get_use(name: str) -> UseSpec:
    """The registered use; ``KeyError`` for an unknown name."""
    return USES[name]


def _env_name(name: str, suffix: str) -> str:
    return f"CONDUCTOR_JUDGMENT_{name.upper()}_{suffix}"


def use_mode(name: str, environ: Mapping[str, str] | None = None) -> str:
    """``off|shadow|enforce`` for ``name``; unknown values read as ``shadow``.

    ``CONDUCTOR_JUDGMENT_MODE=off`` is the global kill switch: it wins over
    every per-use mode. Any other value of the global variable is ignored.
    Otherwise ``CONDUCTOR_JUDGMENT_<USE>_MODE`` decides; unset means the
    use's ``default_mode`` (``shadow`` for every v1 use).
    """
    env = os.environ if environ is None else environ
    if str(env.get(GLOBAL_MODE_ENV, "") or "").strip().lower() == "off":
        return "off"
    env_name = _env_name(name, "MODE")
    if not str(env.get(env_name, "") or "").strip():
        spec = USES.get(name)
        return normalize_mode(spec.default_mode if spec else "", source=env_name)
    return check_mode(env_name, env)


def use_sample(name: str, environ: Mapping[str, str] | None = None) -> float:
    """Sample rate in [0, 1] from ``CONDUCTOR_JUDGMENT_<USE>_SAMPLE``.

    Unset or unreadable means ``DEFAULT_SAMPLE`` (0.1); values are clamped.
    """
    env = os.environ if environ is None else environ
    raw = str(env.get(_env_name(name, "SAMPLE"), "") or "").strip()
    if not raw:
        return DEFAULT_SAMPLE
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SAMPLE
    if not isfinite(value):
        return DEFAULT_SAMPLE
    return max(0.0, min(1.0, value))


def in_sample(name: str, key: str, environ: Mapping[str, str] | None = None) -> bool:
    """Deterministic: the same (use, key) is always in or always out."""
    rate = use_sample(name, environ)
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    digest = hashlib.sha256(f"{name}:{key}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return bucket < rate
