"""Waker switches. Env overrides; yaml fields are optional (no models.py edit)."""

from __future__ import annotations

import os

from src.config.core import get_infrastructure_config

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_DEFAULT_MAX_QUOTA_RETRIES = 3


def _waker_env_enabled(name: str) -> bool | None:
    """Environment override for one waker switch. None means the yaml/default."""
    value = os.getenv(name)
    if value is None:
        return None
    return value.strip().lower() in _TRUTHY


def get_autonomy_waker_codexbar_enabled() -> bool:
    """Whether CodexBar quota events may park work. Default off."""
    override = _waker_env_enabled("AUTONOMY_WAKER_CODEXBAR")
    if override is not None:
        return override
    return bool(
        getattr(get_infrastructure_config(), "autonomy_waker_codexbar_enabled", False)
    )


def get_autonomy_waker_repobar_enabled() -> bool:
    """Whether RepoBar and GitHub events are accepted. Default off."""
    override = _waker_env_enabled("AUTONOMY_WAKER_REPOBAR")
    if override is not None:
        return override
    return bool(
        getattr(get_infrastructure_config(), "autonomy_waker_repobar_enabled", False)
    )


def get_autonomy_waker_max_quota_retries() -> int:
    """Requeue budget for one parked line or Ticket. Invalid env falls back."""
    raw = os.getenv("AUTONOMY_WAKER_MAX_QUOTA_RETRIES")
    if raw is not None and raw.strip():
        try:
            value = int(raw.strip())
        except ValueError:
            value = -1
        if 0 <= value <= 20:
            return value
    cfg_value = getattr(
        get_infrastructure_config(),
        "autonomy_waker_max_quota_retries",
        _DEFAULT_MAX_QUOTA_RETRIES,
    )
    try:
        value = int(cfg_value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_QUOTA_RETRIES
    if 0 <= value <= 20:
        return value
    return _DEFAULT_MAX_QUOTA_RETRIES
