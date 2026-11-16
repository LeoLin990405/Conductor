"""Rollout modes for the N76 liveness sweep: ``off``, ``shadow`` or ``enforce``.

The sweep runs from the drive that already re-drives a dispatch. It ships in
``shadow`` first: it records the recovery step it *would* take on the line's
``liveness`` cell and changes nothing. ``enforce`` takes that step; ``off``
neither records nor acts, so behaviour stays byte-for-byte what main does.

Only the exact spellings ``off``, ``shadow`` and ``enforce`` are recognized.
Any other value, including a differently-cased or padded spelling such as
``" ENFORCE "``, falls back to ``shadow``: enforcement is an explicit opt-in,
so a typo must not silently start reassigning lines, and must not silently turn
the sweep off either.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

logger = logging.getLogger(__name__)

OFF = "off"
SHADOW = "shadow"
ENFORCE = "enforce"
MODES = frozenset({OFF, SHADOW, ENFORCE})
DEFAULT_MODE = SHADOW

#: The one environment variable that switches the sweep.
MODE_ENV = "CONDUCTOR_DISPATCH_LIVENESS_MODE"


def normalize_mode(value: object, *, source: str = MODE_ENV) -> str:
    """``value`` as a mode; ``shadow`` when empty or not an exact spelling.

    Matching is deliberately exact. ``enforce`` has real side effects, so a
    value that merely resembles it must not enable them.
    """
    raw = str(value or "")
    if not raw:
        return DEFAULT_MODE
    if raw in MODES:
        return raw
    logger.warning(
        "%s=%r is not one of off|shadow|enforce; using %s", source, raw, DEFAULT_MODE
    )
    return DEFAULT_MODE


def liveness_mode(environ: Mapping[str, str] | None = None) -> str:
    """The configured mode; ``shadow`` when the variable is unset or invalid."""
    env = os.environ if environ is None else environ
    return normalize_mode(env.get(MODE_ENV, ""), source=MODE_ENV)


def should_record(
    mode: str | None = None, *, environ: Mapping[str, str] | None = None
) -> bool:
    """Whether the sweep may write a ``liveness`` record for a parked line.

    Both ``shadow`` and ``enforce`` record; ``off`` is the main-identical mode.
    """
    active = liveness_mode(environ) if mode is None else normalize_mode(mode)
    return active != OFF


def should_act(
    mode: str | None = None, *, environ: Mapping[str, str] | None = None
) -> bool:
    """Whether the sweep may perform the step it chose (``enforce`` only)."""
    active = liveness_mode(environ) if mode is None else normalize_mode(mode)
    return active == ENFORCE


__all__ = [
    "DEFAULT_MODE",
    "ENFORCE",
    "MODE_ENV",
    "MODES",
    "OFF",
    "SHADOW",
    "liveness_mode",
    "normalize_mode",
    "should_act",
    "should_record",
]
