"""N73 soft gates: allow-or-not checks open, Reviewer + G5 are the last defence.

Owner decision (2026-11-12): the Super-agent picks every Worker's model, effort and
runtime, and "allow or not" gates go away; the final Reviewer defends. Hard
runtime boundaries stay (sandbox writes only in the worktree, no secrets or
keychain, nothing sent out, Workers never push, an author never reviews its
own code) -- nothing in this module touches them.

* S1 paid credits: review may spend paid credits once a plan is full, by
  default; ``CONDUCTOR_DISPATCH_ALLOW_PAID_CREDITS=0`` turns it off.
* S2/S3 developer pool and DAG quota: advisory. The pool order is the
  ``preferred`` input and policy ``avoid`` entries are inputs too; quota no
  longer removes a DAG place -- a line whose developer is out of quota is
  left for ``_assign`` (selector or Super-agent, both quota-aware).
  ``CONDUCTOR_DISPATCH_QUOTA_ADVISORY=0`` restores the old placement.
* S4-S6 N46/N47/N52: record-only. ``enforce`` logs a warning and acts as
  ``shadow``; the findings go into the review evidence instead.
  ``CONDUCTOR_DISPATCH_RECORD_ONLY_CHECKS=0`` restores enforcement.
"""

from __future__ import annotations

import logging
import os
from typing import Mapping

from .check_modes import ENFORCE, SHADOW

logger = logging.getLogger(__name__)

QUOTA_ADVISORY_ENV = "CONDUCTOR_DISPATCH_QUOTA_ADVISORY"
#: S4-S6 rollback switch: ``0`` lets an ``enforce`` structural check refuse again.
RECORD_ONLY_ENV = "CONDUCTOR_DISPATCH_RECORD_ONLY_CHECKS"
_FALSY = frozenset({"0", "false", "no", "off"})
_warned: set[str] = set()


def quota_advisory(environ: Mapping[str, str] | None = None) -> bool:
    """S3: quota is an input, not a DAG placement gate (default on)."""
    source = os.environ if environ is None else environ
    return str(source.get(QUOTA_ADVISORY_ENV, "") or "").strip().lower() not in _FALSY


def record_only(
    mode: str, *, check: str = "", environ: Mapping[str, str] | None = None
) -> str:
    """S4-S6: an ``enforce`` structural check records like ``shadow``.

    ``CONDUCTOR_DISPATCH_RECORD_ONLY_CHECKS=0`` is the rollback switch: the
    check enforces again, exactly as before N73.
    """
    if mode != ENFORCE:
        return mode
    source = os.environ if environ is None else environ
    if str(source.get(RECORD_ONLY_ENV, "") or "").strip().lower() in _FALSY:
        return mode
    if check not in _warned:
        _warned.add(check)
        logger.warning(
            "N73: %s is record-only; enforce acts as shadow and the finding goes to review",
            check or "check",
        )
    return SHADOW
