import pytest

from src.utils.provider_usage import (
    DEFAULT_LOW_BALANCE_THRESHOLDS,
    PROVIDER_KINDS,
    balance_state,
    canonical_provider,
    normalize_low_balance_thresholds,
    provider_kind,
)


def test_provider_kind_keeps_deepseek_balance_only_even_if_declared_quota():
    assert PROVIDER_KINDS["deepseek"] == "balance"
    assert provider_kind("deepseek", "quota") == "balance"
    assert provider_kind("deepseek-dsh", "quota") == "balance"
    assert provider_kind("codex", "balance") == "balance"
    assert provider_kind("codex", "quota") == "quota"
    assert provider_kind("unknown") == "quota"
    assert canonical_provider("deepseek-dsh") == "deepseek"


def test_manus_credits_kind_preserves_explicit_api_balance_support():
    assert PROVIDER_KINDS["manus"] == "credits"
    assert provider_kind("manus") == "credits"
    assert provider_kind("manus", "quota") == "credits"
    assert provider_kind("manus", "balance") == "balance"
    assert balance_state("manus", {"amount": 1, "currency": "CREDITS"}) == "available"
    assert balance_state("manus", {"amount": 0, "currency": "CREDITS"}) == "exhausted"


def test_provider_kinds_cover_all_enabled_codexbar_providers():
    enabled = {
        "codex", "claude", "cursor", "zai", "minimax", "manus",
        "kimi", "mimo", "doubao", "deepseek", "stepfun", "grok",
    }
    assert enabled <= set(PROVIDER_KINDS)
    assert provider_kind("cursor") == "quota"
    assert provider_kind("cursor", "credits") == "quota"


@pytest.mark.parametrize(
    "balance,expected",
    [
        ({"amount": 0, "currency": "CNY"}, "exhausted"),
        ({"amount": -1, "currency": "CNY"}, "exhausted"),
        ({"amount": 49.99, "currency": "CNY"}, "low"),
        ({"amount": 50, "currency": "CNY"}, "available"),
        ({"amount": 50.01, "currency": "CNY"}, "available"),
        ({"amount": 5, "currency": "USD"}, "unknown"),
    ],
)
def test_balance_state_thresholds(balance, expected):
    assert balance_state("deepseek-dsh", balance) == expected


@pytest.mark.parametrize(
    "balance",
    [
        None,
        {"amount": True, "currency": "CNY"},
        {"amount": "10", "currency": "CNY"},
        {"amount": float("nan"), "currency": "CNY"},
        {"amount": float("inf"), "currency": "CNY"},
        {"amount": 10**1000, "currency": "CNY"},
        {"amount": 10},
        {"amount": 10, "currency": ""},
        {"amount": 10, "currency": "CNY", "other": 1},
    ],
)
def test_malformed_balances_are_unknown(balance):
    assert balance_state("deepseek", balance) == "unknown"


def test_threshold_configuration_is_provider_and_currency_specific():
    thresholds = {"deepseek-dsh": {"CNY": 25, "USD": 3}}
    assert balance_state("deepseek", {"amount": 25, "currency": "CNY"}, thresholds) == "available"
    assert balance_state("deepseek", {"amount": 20, "currency": "CNY"}, thresholds) == "low"
    assert balance_state("deepseek-dsh", {"amount": 30, "currency": "CNY"}, {
        "deepseek": {"CNY": 20}, "deepseek-dsh": {"CNY": 40}
    }) == "available"
    assert balance_state("deepseek-dsh", {"amount": 4, "currency": "USD"}, thresholds) == "available"
    assert balance_state("deepseek", {"amount": 4, "currency": "EUR"}, thresholds) == "unknown"
    assert DEFAULT_LOW_BALANCE_THRESHOLDS == {"deepseek": {"CNY": 50.0}}


def test_threshold_normalization_is_stable_across_alias_and_currency_spelling():
    config = normalize_low_balance_thresholds({
        "deepseek-dsh": {" cny ": 40},
        "deepseek": {"CNY": 25},
    })
    assert config == {"deepseek": {"CNY": 25}}
    assert balance_state("deepseek-dsh", {"amount": 30, "currency": "cny"}, config) == "available"


@pytest.mark.parametrize(
    "config",
    [
        [],
        {"deepseek": []},
        {"deepseek": {"": 1}},
        {"deepseek": {"CNY": True}},
        {"deepseek": {"CNY": "10"}},
        {"deepseek": {"CNY": float("nan")}},
        {"deepseek": {"CNY": -1}},
        {"deepseek": {"CNY": 10**1000}},
    ],
)
def test_threshold_normalization_rejects_invalid_configuration(config):
    with pytest.raises(ValueError):
        normalize_low_balance_thresholds(config)
