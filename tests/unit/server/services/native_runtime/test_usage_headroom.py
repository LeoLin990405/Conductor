"""Quota snapshots are bounded, local inputs to automatic runtime routing."""

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.server.services.native_runtime.usage_headroom import (
    load_usage_headroom,
    read_usage_headroom,
)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
NOW_TEXT = "2026-11-02T12:00:00Z"


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_widget_snapshot_maps_aliases_and_uses_latest_valid_window(tmp_path):
    path = tmp_path / "widget.json"
    _write(
        path,
        {
            "generatedAt": "2099-01-01T00:00:00Z",
            "entries": [
                {
                    "provider": "openai-codex",
                    "updatedAt": NOW_TEXT,
                    "primary": {"usedPercent": 100, "resetsAt": "2026-11-02T11:59:00Z"},
                    "secondary": {"usedPercent": 35, "resetsAt": "2026-11-02T13:00:00Z"},
                },
                {
                    "provider": "moonshot",
                    "updatedAt": NOW_TEXT,
                    "primary": {"usedPercent": 100},
                    "secondary": {"usedPercent": 100},
                },
                {
                    "provider": "not-a-native-provider",
                    "updatedAt": NOW_TEXT,
                    "primary": {"usedPercent": 0},
                },
            ],
        },
    )

    assert read_usage_headroom(str(path), now=NOW) == {
        "codex": "available",
        "kimi": "exhausted",
    }


def test_a_home_relative_path_is_expanded_to_the_machine_that_runs_it(tmp_path, monkeypatch):
    """The tracked config carries "~"; the home directory decides the path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    snapshot = tmp_path / ".conductor" / "usage" / "codexbar-snapshot.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    _write(
        snapshot,
        {
            "generatedAt": "2099-01-01T00:00:00Z",
            "entries": [
                {
                    "provider": "openai-codex",
                    "updatedAt": NOW_TEXT,
                    "primary": {"usedPercent": 0},
                },
            ],
        },
    )

    assert read_usage_headroom("~/.conductor/usage/codexbar-snapshot.json", now=NOW) == {
        "codex": "available",
    }


def test_a_home_relative_path_that_does_not_exist_fails_open(tmp_path, monkeypatch):
    """A "~" path with no file behind it is no reading, not an error."""
    monkeypatch.setenv("HOME", str(tmp_path))

    assert read_usage_headroom("~/.conductor/usage/absent.json", now=NOW) == {}


def test_any_current_exhausted_window_blocks_even_if_another_has_headroom(tmp_path):
    path = tmp_path / "mixed-windows.json"
    _write(
        path,
        {
            "entries": [
                {
                    "provider": "codex",
                    "updatedAt": NOW_TEXT,
                    "primary": {"usedPercent": 100, "resetsAt": "2026-11-02T13:00:00Z"},
                    "secondary": {"usedPercent": 10, "resetsAt": "2026-11-02T14:00:00Z"},
                    "tertiary": {"usedPercent": 100, "resetsAt": "2026-11-02T11:00:00Z"},
                }
            ]
        },
    )

    # The expired tertiary window is ignored; the active primary limit still
    # blocks routing despite headroom in the secondary window.
    assert read_usage_headroom(str(path), now=NOW) == {"codex": "exhausted"}


@pytest.mark.parametrize(
    "windows,expected",
    [
        ([{"usedPercent": 20}, {"usedPercent": 101}], {}),
        ([{"usedPercent": 20}, {"usedPercent": 10, "resetsAt": "bad"}], {}),
        ([{"usedPercent": 100}, {"usedPercent": 101}], {"codex": "exhausted"}),
        ([{"usedPercent": 20}] * 41, {}),
    ],
    ids=["out-of-range-companion", "bad-reset-companion", "known-full-window-wins", "too-many-windows"],
)
def test_malformed_companion_window_cannot_create_false_headroom(tmp_path, windows, expected):
    path = tmp_path / "window-set.json"
    _write(
        path,
        {
            "providers": [
                {"provider": "codex", "updated_at": NOW_TEXT, "windows": windows}
            ]
        },
    )

    assert read_usage_headroom(str(path), now=NOW) == expected


def test_sanitized_usage_and_legacy_normalized_metrics_are_supported(tmp_path):
    path = tmp_path / "sanitized.json"
    _write(
        path,
        {
            "providers": [
                {
                    "provider": "deepseek",
                    "updated_at": NOW_TEXT,
                    "windows": [{"used_percent": 72, "resets_at_utc": "2026-11-02T13:00:00Z"}],
                },
                {
                    "provider_id": "zai",
                    "observed_at_utc": NOW_TEXT,
                    "status": "ok",
                    "metrics": [
                        {
                            "kind": "quota_used_percent",
                            "unit": "percent",
                            "value": 100,
                            "resets_at_utc": None,
                        }
                    ],
                },
            ]
        },
    )

    assert read_usage_headroom(str(path), now=NOW) == {
        "deepseek-dsh": "unknown",
        "zcode": "exhausted",
    }


@pytest.mark.parametrize(
    "amount,expected",
    [(50, "available"), (49, "low"), (0, "exhausted"), (-1, "exhausted")],
)
def test_balance_rows_use_configured_currency_threshold(tmp_path, amount, expected):
    path = tmp_path / "balance.json"
    _write(path, {"providers": [{
        "provider": "deepseek", "kind": "balance", "status": "ok",
        "updated_at": NOW_TEXT, "balance": {"amount": amount, "currency": "CNY"},
    }]})

    assert read_usage_headroom(str(path), now=NOW) == {"deepseek-dsh": expected}


@pytest.mark.parametrize("amount,expected", [(1, "available"), (0, "exhausted")])
def test_manus_credit_balance_requires_a_fresh_explicit_balance_row(
    tmp_path, amount, expected
):
    from src.utils.provider_usage import provider_kind

    assert provider_kind("manus", "quota") == "credits"
    path = tmp_path / "manus-balance.json"
    _write(
        path,
        {
            "providers": [
                {
                    "provider": "manus",
                    "kind": "balance",
                    "status": "ok",
                    "updated_at": NOW_TEXT,
                    "balance": {"amount": amount, "currency": "CREDITS"},
                    "windows": [{"used_percent": 100}],
                }
            ]
        },
    )
    assert read_usage_headroom(str(path), now=NOW) == {"manus": expected}


def test_manus_credit_windows_drive_headroom_without_an_api_balance(tmp_path):
    path = tmp_path / "manus-credit-windows.json"
    _write(path, {"providers": [{
        "provider": "manus", "updated_at": NOW_TEXT,
        "windows": [{"used_percent": 0}],
    }]})
    assert read_usage_headroom(str(path), now=NOW) == {"manus": "available"}


def test_manus_credit_allowance_without_windows_remains_unknown(tmp_path):
    path = tmp_path / "manus-credit-allowance-only.json"
    _write(path, {"providers": [{
        "provider": "manus", "kind": "credits", "status": "ok",
        "updated_at": NOW_TEXT, "credits": {"total": 1000},
    }]})
    assert read_usage_headroom(str(path), now=NOW) == {}


@pytest.mark.parametrize("balance", [None, {"amount": "51", "currency": "CNY"},
                                      {"amount": float("nan"), "currency": "CNY"},
                                      {"amount": 51, "currency": "USD"}])
def test_balance_rows_with_invalid_or_unconfigured_amount_are_unknown(tmp_path, balance):
    path = tmp_path / "balance.json"
    _write(path, {"providers": [{
        "provider": "deepseek-dsh", "status": "ok", "updated_at": NOW_TEXT,
        "balance": balance,
    }]})

    assert read_usage_headroom(str(path), now=NOW) == {"deepseek-dsh": "unknown"}


def test_stale_balance_is_unknown_and_unknown_duplicate_cannot_become_available(tmp_path):
    path = tmp_path / "balance.json"
    _write(path, {"providers": [
        {"provider": "deepseek", "updated_at": "2026-11-02T11:00:00Z",
         "balance": {"amount": 100, "currency": "CNY"}},
        {"provider": "deepseek-dsh", "updated_at": NOW_TEXT,
         "balance": {"amount": 100, "currency": "CNY"}},
    ]})

    assert read_usage_headroom(str(path), now=NOW) == {"deepseek-dsh": "unknown"}


def test_duplicate_low_balance_is_more_conservative_than_unknown(tmp_path):
    path = tmp_path / "balance.json"
    _write(path, {"providers": [
        {"provider": "deepseek", "updated_at": NOW_TEXT,
         "balance": {"amount": 100, "currency": "CNY"}},
        {"provider": "deepseek-dsh", "updated_at": NOW_TEXT,
         "balance": {"amount": 49, "currency": "CNY"}},
    ]})

    assert read_usage_headroom(str(path), now=NOW) == {"deepseek-dsh": "low"}


def test_non_deepseek_balance_kind_is_supported_without_reclassifying_known_quota_provider(tmp_path):
    path = tmp_path / "balance.json"
    _write(path, {"providers": [
        {"provider": "minimax", "kind": "balance", "updated_at": NOW_TEXT,
         "balance": {"amount": 100, "currency": "CNY"}},
        {"provider": "codex", "kind": "balance", "updated_at": NOW_TEXT,
         "balance": {"amount": 100, "currency": "CNY"}},
    ]})

    assert read_usage_headroom(
        str(path), now=NOW,
        low_balance_thresholds={"minimax": {"CNY": 50}, "codex": {"CNY": 50}},
    ) == {"minimax": "available", "codex": "available"}


def test_provider_kind_table_extension_is_accepted_without_reader_alias_edit(tmp_path, monkeypatch):
    from src.utils import provider_usage

    monkeypatch.setitem(provider_usage.PROVIDER_KINDS, "fake-api", "balance")
    path = tmp_path / "balance.json"
    _write(path, {"providers": [{
        "provider": "fake-api", "updated_at": NOW_TEXT,
        "balance": {"amount": 20, "currency": "USD"},
    }]})

    assert read_usage_headroom(
        str(path), now=NOW, low_balance_thresholds={"fake-api": {"USD": 10}},
    ) == {"fake-api": "available"}


@pytest.mark.parametrize(
    "payload,kwargs",
    [
        ({"entries": [{"provider": "codex", "updatedAt": "2026-11-02T11:00:00Z", "primary": {"usedPercent": 2}}]}, {}),
        ({"entries": [{"provider": "codex", "updatedAt": NOW_TEXT, "primary": {"usedPercent": float("nan")}}]}, {}),
        ({"entries": [{"provider": "codex", "updatedAt": NOW_TEXT, "primary": {"usedPercent": 101}}]}, {}),
        ({"entries": [{"provider": "codex", "updatedAt": NOW_TEXT, "primary": {"usedPercent": 10**1000}}]}, {}),
        ({"entries": [{"provider": "codex", "updatedAt": NOW_TEXT, "primary": {"usedPercent": 2, "resetsAt": "bad"}}]}, {}),
        ({"entries": [{"provider": "codex", "updatedAt": "2026-11-02T12:02:00Z", "primary": {"usedPercent": 2}}]}, {}),
        ({"entries": [{"provider": "codex", "updatedAt": "0001-01-01T00:00:00+01:00", "primary": {"usedPercent": 2}}]}, {}),
        ({"entries": [{"provider": "codex", "updatedAt": "9999-12-31T23:59:59-01:00", "primary": {"usedPercent": 2}}]}, {}),
        ({"providers": [{"provider": "codex", "updated_at": NOW_TEXT, "status": "error", "windows": [{"used_percent": 2}]}]}, {}),
    ],
    ids=["stale", "nan", "out-of-range", "huge-integer", "bad-reset", "future", "old-offset-overflow", "future-offset-overflow", "error-status"],
)
def test_invalid_or_stale_rows_do_not_create_headroom(tmp_path, payload, kwargs):
    path = tmp_path / "invalid.json"
    _write(path, payload)

    assert read_usage_headroom(str(path), now=NOW, **kwargs) == {}


def test_bridge_snapshot_keeps_readable_providers_and_never_claims_an_unreadable_one(tmp_path):
    """The bridge reports a provider it could not read as ``unknown``; that row
    must not turn into headroom, and must not cost its neighbours theirs."""
    path = tmp_path / "bridge.json"
    _write(
        path,
        {
            "providers": [
                {
                    "provider": "zai",
                    "status": "ok",
                    "updated_at": NOW_TEXT,
                    "windows": [{"usedPercent": 33, "resetsAt": "2026-11-02T13:00:00Z"}],
                },
                {
                    "provider": "claude",
                    "status": "unknown",
                    "updated_at": None,
                    "windows": [],
                    "error": "No available fetch strategy for claude.",
                },
            ]
        },
    )

    states = read_usage_headroom(str(path), now=NOW)

    assert states["zcode"] == "available"
    # The unreadable provider carries no state of its own; routing reads an
    # absent provider as unknown, never as headroom.
    assert "claude" not in states


def test_oversized_missing_and_malformed_files_fail_open(tmp_path):
    oversized = tmp_path / "large.json"
    oversized.write_text(" " * 4096, encoding="utf-8")
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")

    assert read_usage_headroom(str(oversized), now=NOW, max_bytes=100) == {}
    assert read_usage_headroom(str(malformed), now=NOW) == {}
    assert read_usage_headroom(str(tmp_path / "missing.json"), now=NOW) == {}


def test_fifo_is_rejected_without_waiting_for_a_writer(tmp_path):
    path = tmp_path / "snapshot.pipe"
    os.mkfifo(path)

    assert read_usage_headroom(str(path), now=NOW) == {}


def test_deeply_nested_json_fails_open(tmp_path):
    path = tmp_path / "deep.json"
    path.write_text('{"entries":' + "[" * 1500 + "0" + "]" * 1500 + "}", encoding="utf-8")

    assert read_usage_headroom(str(path), now=NOW) == {}


@pytest.mark.asyncio
async def test_disabled_loader_never_starts_file_reader(monkeypatch):
    settings = SimpleNamespace(enabled=False, snapshot_path="/must/not/read", max_age_seconds=10, max_bytes=100)
    config = settings
    to_thread = AsyncMock(side_effect=AssertionError("disabled headroom attempted file I/O"))
    monkeypatch.setattr(
        "src.server.services.native_runtime.usage_headroom.asyncio.to_thread", to_thread
    )

    assert await load_usage_headroom(config, now=NOW) == {}
    to_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_enabled_loader_passes_explicit_bounds_to_reader(monkeypatch):
    settings = SimpleNamespace(enabled=True, snapshot_path="/configured/snapshot.json", max_age_seconds=120, max_bytes=8192)
    config = settings
    to_thread = AsyncMock(return_value={"codex": "available"})
    monkeypatch.setattr(
        "src.server.services.native_runtime.usage_headroom.asyncio.to_thread", to_thread
    )

    result = await load_usage_headroom(config, now=NOW)

    assert result == {"codex": "available"}
    to_thread.assert_awaited_once_with(
        read_usage_headroom,
        "/configured/snapshot.json",
        now=NOW,
        max_age_seconds=120,
        max_bytes=8192,
        low_balance_thresholds={"deepseek": {"CNY": 50.0}},
    )


def test_usage_headroom_config_is_disabled_and_pathless_by_default():
    from src.config.models import UsageHeadroomConfig

    config = UsageHeadroomConfig()

    assert config.enabled is False
    assert config.snapshot_path is None
    assert config.max_age_seconds == 900
    assert config.max_bytes == 262144
    assert config.low_balance_thresholds == {"deepseek": {"CNY": 50.0}}


@pytest.mark.parametrize(
    "values",
    [
        {"max_age_seconds": 0},
        {"max_age_seconds": 604801},
        {"max_bytes": 255},
        {"max_bytes": 1048577},
    ],
)
def test_usage_headroom_config_rejects_unbounded_limits(values):
    from src.config.models import UsageHeadroomConfig

    with pytest.raises(ValidationError):
        UsageHeadroomConfig(**values)


@pytest.mark.parametrize("thresholds", [
    {"deepseek": {"CNY": -1}},
    {"deepseek": {"CNY": float("inf")}},
    {"deepseek": {"CNY": 10**1000}},
    {"deepseek": {"CNY": True}},
    {"deepseek": {"CNY": "50"}},
])
def test_usage_headroom_config_rejects_invalid_thresholds(thresholds):
    from src.config.models import UsageHeadroomConfig

    with pytest.raises(ValidationError):
        UsageHeadroomConfig(low_balance_thresholds=thresholds)


def test_usage_headroom_config_normalizes_aliases_with_canonical_precedence():
    from src.config.models import UsageHeadroomConfig

    config = UsageHeadroomConfig(low_balance_thresholds={
        "deepseek-dsh": {"cny": 25}, "deepseek": {"CNY": 60},
    })

    assert config.low_balance_thresholds == {"deepseek": {"CNY": 60.0}}
