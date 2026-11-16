"""Shared provider usage kinds and balance state rules."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

PROVIDER_KINDS = {
    "codex": "quota",
    "claude": "quota",
    "cursor": "quota",
    "grok": "quota",
    "kimi": "quota",
    "doubao": "quota",
    "minimax": "quota",
    "stepfun": "quota",
    "mimo": "quota",
    "zai": "quota",
    "zcode": "quota",
    "manus": "credits",
    "deepseek": "balance",
    "deepseek-dsh": "balance",
}
DEFAULT_LOW_BALANCE_THRESHOLDS = {
    "deepseek": {"CNY": 50.0},
}

_ALIASES = {
    "openai": "codex",
    "openai-codex": "codex",
    "claude-code": "claude",
    "moonshot": "kimi",
    "deepseek-dsh": "deepseek",
    "zhipu": "zai",
    "zcode": "zai",
}


def canonical_provider(provider: Any) -> str | None:
    """Normalize known routing aliases while retaining unknown provider IDs."""
    if not isinstance(provider, str) or not provider.strip() or len(provider) > 80:
        return None
    normalized = provider.strip().lower()
    return _ALIASES.get(normalized, normalized)


def provider_kind(provider: Any, declared_kind: Any = None) -> str:
    """Resolve quota, credits, and balance semantics for provider usage."""
    canonical = canonical_provider(provider)
    if canonical == "deepseek":
        return "balance"
    if isinstance(declared_kind, str) and declared_kind.strip().lower() == "balance":
        return "balance"
    if canonical is None:
        return "quota"
    return PROVIDER_KINDS.get(canonical, "quota")


def _finite_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def normalize_low_balance_thresholds(value: Any) -> dict[str, dict[str, int | float]]:
    """Canonicalize provider aliases and currency codes in threshold settings."""
    if not isinstance(value, Mapping):
        raise ValueError("balance thresholds must be a provider mapping")
    normalized: dict[str, dict[str, int | float]] = {}
    priorities: dict[tuple[str, str], int] = {}
    entries = sorted(
        value.items(),
        key=lambda item: (item[0].strip().lower() == canonical_provider(item[0]), item[0].strip().lower())
        if isinstance(item[0], str) else (False, ""),
    )
    for provider_key, currency_map in entries:
        canonical = canonical_provider(provider_key)
        if canonical is None or not isinstance(currency_map, Mapping):
            raise ValueError("balance thresholds must map providers to currency mappings")
        raw_provider = provider_key.strip().lower()
        priority = 1 if raw_provider == canonical else 0
        for currency_key, threshold in currency_map.items():
            if not isinstance(currency_key, str) or not currency_key.strip():
                raise ValueError("balance threshold currency must be a nonempty string")
            currency = currency_key.strip().upper()
            if not _finite_number(threshold) or threshold < 0:
                raise ValueError("balance threshold must be a finite nonnegative number")
            key = (canonical, currency)
            if priority > priorities.get(key, -1):
                normalized.setdefault(canonical, {})[currency] = threshold
                priorities[key] = priority
    return normalized


def _threshold_for(provider: str, currency: str, config: Any) -> float | int | None:
    try:
        normalized = normalize_low_balance_thresholds(config)
    except (ValueError, TypeError):
        return None
    canonical = canonical_provider(provider)
    provider_thresholds = normalized.get(canonical or provider)
    threshold = provider_thresholds.get(currency.strip().upper()) if provider_thresholds else None
    return threshold if _finite_number(threshold) and threshold >= 0 else None


def balance_state(
    provider: Any,
    balance: Any,
    low_balance_thresholds: Any = None,
) -> str:
    """Classify an explicit currency balance without conversion or coercion."""
    if not isinstance(balance, dict) or set(balance) != {"amount", "currency"}:
        return "unknown"
    amount = balance.get("amount")
    currency = balance.get("currency")
    if not _finite_number(amount) or not isinstance(currency, str) or not currency or len(currency) > 12:
        return "unknown"
    if amount <= 0:
        return "exhausted"
    if canonical_provider(provider) == "manus" and currency.strip().upper() == "CREDITS":
        return "available"
    thresholds = DEFAULT_LOW_BALANCE_THRESHOLDS if low_balance_thresholds is None else low_balance_thresholds
    threshold = _threshold_for(str(provider), currency, thresholds)
    if threshold is None:
        return "unknown"
    return "low" if amount < threshold else "available"
