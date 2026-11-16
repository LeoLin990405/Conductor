"""Typed access to the deep-refactor deployment flags.

The catalog and truthiness rule live in ``src/config/settings.py`` (the
canonical tunables path); this service adds the domain error the refactor code
paths import, so a mistyped flag fails loudly instead of reading as "off".
"""

from __future__ import annotations

from src.config import settings


class UnknownRefactorFlagError(KeyError):
    """The flag is not in the code catalog (typo or removed flag)."""


def refactor_flag_enabled(flag: str) -> bool:
    """True iff ``flag`` is explicitly enabled in the current environment."""
    try:
        return settings.get_refactor_flag(flag)
    except KeyError:
        raise UnknownRefactorFlagError(
            f"unknown refactor flag {flag!r}; known: {sorted(settings.get_refactor_flags())}"
        ) from None


def enabled_refactor_flags() -> dict[str, bool]:
    """Every cataloged flag and its current state, for diagnostics/tests."""
    return settings.get_refactor_flags()
