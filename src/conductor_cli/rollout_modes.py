"""Rollout modes for a new check: ``off``, ``shadow`` or ``enforce`` (N58).

One normalisation for every check that ships in shadow first: the dispatch
gates (``src.tools.auto_dispatch.check_modes`` re-exports these names) and
the daemon's grant verifier (N44, ``daemon_capability.grant_verifier_mode``).
A value that is not a mode falls back to ``shadow`` with a warning: a typo
must neither silently enforce a check nor silently switch it off.

Stdlib only: ``conductor_cli`` may not import ``src.*``, and both the CLI side
and the server side read it.
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


def normalize_mode(value: object, *, source: str = "mode") -> str:
    """``value`` as a mode: trimmed, case-folded; ``shadow`` when empty or unknown."""
    raw = str(value or "").strip().casefold()
    if not raw:
        return DEFAULT_MODE
    if raw in MODES:
        return raw
    logger.warning(
        "%s=%r is not one of off|shadow|enforce; using %s", source, raw, DEFAULT_MODE
    )
    return DEFAULT_MODE


def check_mode(env_name: str, environ: Mapping[str, str] | None = None) -> str:
    """The mode ``env_name`` asks for; ``shadow`` when unset or not a mode."""
    env = os.environ if environ is None else environ
    return normalize_mode(env.get(env_name, ""), source=env_name)
