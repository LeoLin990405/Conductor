"""Refactor deployment flags: default off, env-driven, loud on typos."""

from __future__ import annotations

import pytest

from src.config import settings
from src.server.services import refactor_flags

ENV_NAMES = tuple(settings._REFACTOR_FLAG_ENV.values())


@pytest.fixture(autouse=True)
def _clear_flags(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    yield


def test_defaults_are_off():
    assert all(settings.get_refactor_flags().values()) is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "On"])
def test_truthy_spellings_enable_a_flag(monkeypatch, raw):
    monkeypatch.setenv("REFACTOR_GOALS_ENABLED", raw)
    assert settings.get_refactor_flag("goals") is True
    assert refactor_flags.refactor_flag_enabled("goals") is True


@pytest.mark.parametrize("raw", ["0", "false", "off", "", "enabled"])
def test_other_values_leave_a_flag_off(monkeypatch, raw):
    monkeypatch.setenv("REFACTOR_GOALS_ENABLED", raw)
    assert settings.get_refactor_flag("goals") is False


def test_flags_are_independent(monkeypatch):
    monkeypatch.setenv("REFACTOR_EVENTS_ENABLED", "1")
    flags = refactor_flags.enabled_refactor_flags()
    assert flags["events"] is True
    assert flags["goals"] is False


def test_unknown_flag_is_loud():
    with pytest.raises(refactor_flags.UnknownRefactorFlagError, match="unknown refactor flag"):
        refactor_flags.refactor_flag_enabled("goalz")


def test_unknown_flag_from_settings_raises_key_error():
    with pytest.raises(KeyError):
        settings.get_refactor_flag("goalz")


def test_catalog_is_stable():
    # The differential harness and the refactor runbook pin these names.
    assert refactor_flags.enabled_refactor_flags().keys() == {
        "goals",
        "events",
        "memory_index",
        "task_tree",
        "subagent_contract",
        "permissions",
        "approval_chain",
        "compaction_events",
    }
