"""Rollout modes for new dispatch checks: ``off``, ``shadow`` or ``enforce`` (N58).

The device policy went straight to enforcement and refused all 8 worker
Tickets (N44), so a new check ships in ``shadow`` first and changes nothing:
G2 records what it would have refused on the line's G2 cell
(``gates.G2.shadow``) and as a ``shadow_refusal`` entry on the scheduler
audit, and ``conductor dispatch status`` shows both. The N46 plan rule runs
before a line exists, so plan validation only logs its shadow refusal; G2
re-runs the same check on the line and records it there. A check is
promoted per gate to ``enforce`` once its shadow log shows acceptable false
blocks on real lines. ``off`` skips the check entirely.

Each check reads its own environment variable, so one can be promoted without
the others. A value that is not a mode falls back to ``shadow``: a typo must
neither silently enforce a gate nor silently switch it off.
"""

from __future__ import annotations

from src.conductor_cli.rollout_modes import (
    DEFAULT_MODE,
    ENFORCE,
    MODES,
    OFF,
    SHADOW,
    check_mode,
    normalize_mode,
)

__all__ = [
    "DEFAULT_MODE",
    "ENFORCE",
    "MODES",
    "OFF",
    "REQUIRE_TESTS_CHECK",
    "REQUIRE_TESTS_ENV",
    "SCOPE_CHECK",
    "SCOPE_CHECK_ENV",
    "SHADOW",
    "SHADOW_REFUSAL",
    "WORKTREE_CHECK",
    "WORKTREE_CHECK_ENV",
    "check_mode",
    "normalize_mode",
]

#: N46: plan validation refuses a code line whose acceptance runs no test of its own.
REQUIRE_TESTS_ENV = "CONDUCTOR_DISPATCH_G2_REQUIRE_TESTS_MODE"
#: N47: G2 fails when the test run left changes outside tmp.
WORKTREE_CHECK_ENV = "CONDUCTOR_DISPATCH_G2_WORKTREE_CHECK_MODE"
#: N52: G2 fails when the line's diff leaves its write scope.
SCOPE_CHECK_ENV = "CONDUCTOR_DISPATCH_G2_SCOPE_CHECK_MODE"

#: The names a shadow record carries, one per check.
REQUIRE_TESTS_CHECK = "require_tests"
WORKTREE_CHECK = "worktree_check"
SCOPE_CHECK = "scope_check"

#: The scheduler audit kind of a refusal a shadow-mode check did not make.
SHADOW_REFUSAL = "shadow_refusal"

