"""PRD 4.6 and §6.1: assignment mode, per-line sample, and rollback."""

from __future__ import annotations

import hashlib
import logging
import math

import pytest

from src.tools.auto_dispatch.superagent_assign.modes import (
    ASSIGN_MODE_ENV,
    ASSIGN_SAMPLE_ENV,
    DEFAULT_MODE,
    DEFAULT_SAMPLE,
    ENFORCE,
    OFF,
    SHADOW,
    SPEC_METRICS,
    adopts_superagent,
    assign_mode,
    assign_sample,
    line_sample_unit,
    line_sampled,
    rollback_due,
    rollback_reasons,
)

LINE = "line-0-r1-4"


@pytest.fixture(autouse=True)
def _clean_assign_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ASSIGN_MODE_ENV, raising=False)
    monkeypatch.delenv(ASSIGN_SAMPLE_ENV, raising=False)


def test_mode_defaults_to_shadow_and_sample_defaults_to_one() -> None:
    assert DEFAULT_MODE == SHADOW
    assert DEFAULT_SAMPLE == 1.0
    assert assign_mode({}) == SHADOW
    assert assign_mode({ASSIGN_MODE_ENV: "  "}) == SHADOW
    assert assign_sample({}) == 1.0
    assert assign_sample({ASSIGN_SAMPLE_ENV: "  "}) == 1.0
    assert adopts_superagent(LINE, environ={}) is False


@pytest.mark.parametrize(
    "raw, mode",
    [("off", OFF), ("Shadow", SHADOW), (" ENFORCE ", ENFORCE)],
)
def test_mode_is_read_case_insensitively(raw: str, mode: str) -> None:
    assert assign_mode({ASSIGN_MODE_ENV: raw}) == mode


def test_a_value_that_is_not_a_mode_falls_back_to_shadow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert assign_mode({ASSIGN_MODE_ENV: "enforcing"}) == SHADOW
    assert ASSIGN_MODE_ENV in caplog.text


@pytest.mark.parametrize(
    "raw, sample",
    [("0", 0.0), ("1", 1.0), ("0.5", 0.5), (" 0.25 ", 0.25)],
)
def test_sample_accepts_a_fraction(raw: str, sample: float) -> None:
    assert assign_sample({ASSIGN_SAMPLE_ENV: raw}) == sample


@pytest.mark.parametrize("raw", ["2", "-0.1", "nope", "nan", "inf"])
def test_a_sample_outside_0_to_1_falls_back_to_one(
    raw: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert assign_sample({ASSIGN_SAMPLE_ENV: raw}) == 1.0
    assert ASSIGN_SAMPLE_ENV in caplog.text


def test_the_sample_is_a_deterministic_per_line_hash() -> None:
    digest = hashlib.sha256(LINE.encode("utf-8")).digest()
    unit = int.from_bytes(digest[:8], "big") / 2**64
    assert line_sample_unit(LINE) == unit
    assert line_sample_unit(LINE) == line_sample_unit(LINE)
    assert line_sampled(LINE, 0.0) is False
    assert line_sampled(LINE, 1.0) is True
    assert line_sampled(LINE, unit) is False
    assert line_sampled(LINE, math.nextafter(unit, 2.0)) is True
    assert line_sample_unit("a") != line_sample_unit("b")


def test_only_an_enforce_sample_adopts_the_superagent() -> None:
    unit = line_sample_unit(LINE)
    assert adopts_superagent(LINE, mode="off", sample=1.0) is False
    assert adopts_superagent(LINE, mode="shadow", sample=1.0) is False
    assert adopts_superagent(LINE, mode="enforce", sample=0.0) is False
    assert adopts_superagent(LINE, mode="enforce", sample=1.0) is True
    assert adopts_superagent(LINE, mode="enforce", sample=unit) is False
    assert (
        adopts_superagent(LINE, mode="enforce", sample=math.nextafter(unit, 2.0))
        is True
    )
    assert (
        adopts_superagent(
            LINE, environ={ASSIGN_MODE_ENV: "enforce", ASSIGN_SAMPLE_ENV: "0"}
        )
        is False
    )
    assert (
        adopts_superagent(
            LINE, environ={ASSIGN_MODE_ENV: "enforce", ASSIGN_SAMPLE_ENV: "1"}
        )
        is True
    )


def test_a_pass_rate_drop_of_ten_points_does_not_roll_back() -> None:
    assert not rollback_due({"pass_rate": {"superagent": 0.70, "selector": 0.80}})
    assert not rollback_due({"通过率": {"superagent": 70, "selector": 80}})


def test_a_pass_rate_drop_over_ten_points_rolls_back() -> None:
    metrics = {"pass_rate": {"superagent": 0.70, "selector": 0.81}}
    assert rollback_due(metrics) is True
    assert rollback_reasons(metrics) == ("pass_rate_drop",)
    assert rollback_due({"通过率": {"treatment": 69, "control": 80}}) is True


def test_a_cost_rise_of_fifty_percent_does_not_roll_back() -> None:
    assert not rollback_due({"cost_per_line": {"superagent": 15, "selector": 10}})
    assert not rollback_due({"成本": {"superagent": 3, "selector": 2}})


def test_a_gap_just_past_either_threshold_rolls_back() -> None:
    assert (
        rollback_due({"pass_rate": {"superagent": 0.699999, "selector": 0.8}}) is True
    )
    assert (
        rollback_due({"cost_per_line": {"superagent": 15.0000001, "selector": 10}})
        is True
    )


def test_a_cost_rise_over_fifty_percent_rolls_back() -> None:
    metrics = {"cost": {"superagent": 15.01, "selector": 10}}
    assert rollback_due(metrics) is True
    assert rollback_reasons(metrics) == ("cost_per_line_rise",)
    assert rollback_due(
        {"cost_per_line": {"superagent": {"median": 16}, "selector": {"median": 10}}}
    )


def test_rollback_fires_only_for_those_two_spec_rows() -> None:
    noisy = {
        "pass_rate": {"superagent": 0.80, "selector": 0.80},
        "review_rounds": {"superagent": 9, "selector": 0.1},
        "cost_per_line": {"superagent": 14, "selector": 10},
        "time_per_line": {"superagent": 100, "selector": 1},
        "feasibility_rate": {"superagent": 0.1, "selector": 1},
        "fallback_rate": {"superagent": 0.9, "selector": 0.01},
        "call_overhead": {"superagent": 120, "selector": 1},
    }
    assert not rollback_due(noisy)
    for name in SPEC_METRICS:
        if name in {"pass_rate", "cost_per_line"}:
            continue
        assert not rollback_due({name: {"superagent": 1000, "selector": 1}})


def test_missing_zero_and_insufficient_metrics_do_not_roll_back() -> None:
    assert not rollback_due({})
    assert not rollback_due({"pass_rate": {"superagent": 0.5}})
    assert not rollback_due({"cost_per_line": {"superagent": 5, "selector": 0}})
    assert not rollback_due({"cost_per_line": {"superagent": 5}})
    assert not rollback_due({"pass_rate": "insufficient"})
    assert not rollback_due(
        {"pass_rate": {"status": "insufficient", "superagent": 0.1, "selector": 0.9}}
    )
    assert not rollback_due({"pass_rate": {"superagent": 0.95, "selector": 0.70}})
    assert not rollback_due({"cost_per_line": {"superagent": 1, "selector": 10}})


def test_rollback_reasons_list_pass_rate_before_cost() -> None:
    reasons = rollback_reasons(
        {
            "cost_per_line": {"superagent": 20, "selector": 10},
            "pass_rate": {"superagent": 0.50, "selector": 0.70},
        }
    )
    assert reasons == ("pass_rate_drop", "cost_per_line_rise")


def test_both_rollback_arms_are_reported_together() -> None:
    reasons = rollback_reasons(
        {
            "review_rounds": {"superagent": 4, "selector": 0},
            "pass_rate": {"superagent": 0.50, "selector": 0.70},
            "cost_per_line": {"superagent": 20, "selector": 10},
        }
    )
    assert reasons == ("pass_rate_drop", "cost_per_line_rise")
    assert rollback_due(
        {
            "pass_rate": {"superagent": 0.50, "selector": 0.70},
            "cost_per_line": {"superagent": 20, "selector": 10},
        }
    )


def test_metrics_must_be_a_mapping() -> None:
    with pytest.raises(TypeError):
        rollback_due([{"pass_rate": 1}])  # type: ignore[arg-type]
