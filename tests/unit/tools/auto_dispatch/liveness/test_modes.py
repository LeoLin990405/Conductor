"""The liveness sweep defaults to shadow, and off means main-identical."""

from __future__ import annotations

import logging

import pytest

from src.tools.auto_dispatch.liveness.modes import (
    DEFAULT_MODE,
    ENFORCE,
    MODE_ENV,
    MODES,
    OFF,
    SHADOW,
    liveness_mode,
    normalize_mode,
    should_act,
    should_record,
)


def test_the_default_is_shadow() -> None:
    assert DEFAULT_MODE == SHADOW
    assert MODES == {OFF, SHADOW, ENFORCE}


@pytest.mark.parametrize(
    "raw,mode", [("off", OFF), ("shadow", SHADOW), ("enforce", ENFORCE)]
)
def test_a_mode_is_the_exact_spelling(raw: str, mode: str) -> None:
    assert liveness_mode({MODE_ENV: raw}) == mode


def test_a_mode_that_only_resembles_enforce_falls_back_to_shadow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Enforcement is an explicit opt-in: padding or different casing must not
    # turn it on.
    assert normalize_mode(" ENFORCE ") == SHADOW
    assert normalize_mode("Enforce") == SHADOW
    assert not should_act(" ENFORCE ")
    with caplog.at_level(logging.WARNING):
        assert liveness_mode({MODE_ENV: " ENFORCE "}) == SHADOW
    assert "not one of off|shadow|enforce" in caplog.text


def test_an_unset_empty_or_invalid_value_falls_back_to_shadow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert liveness_mode({}) == SHADOW
    assert liveness_mode({MODE_ENV: ""}) == SHADOW
    with caplog.at_level(logging.WARNING):
        assert normalize_mode("enforced") == SHADOW
    assert "not one of off|shadow|enforce" in caplog.text


def test_only_enforce_acts_and_off_records_nothing() -> None:
    assert not should_record(OFF)
    assert should_record(SHADOW)
    assert should_record(ENFORCE)

    assert not should_act(OFF)
    assert not should_act(SHADOW)
    assert should_act(ENFORCE)
    # Anything that is not the enforce spelling falls back to shadow, which
    # never takes an action.
    assert not should_act("enforced")


def test_the_helpers_read_the_environment_when_no_mode_is_passed() -> None:
    assert should_record(environ={MODE_ENV: SHADOW})
    assert should_act(environ={MODE_ENV: ENFORCE})
    assert not should_act(environ={})
    assert liveness_mode({MODE_ENV: "off"}) == OFF
