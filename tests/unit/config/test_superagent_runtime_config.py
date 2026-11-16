"""Per-role Super-agent runtime switch (R8c 4.6).

Default ``ptc`` keeps production unchanged; the environment wins over
config.yaml so a role can be cut over or rolled back with a restart; any
unrecognised value is ``ptc`` with a warning, never a crash or a silent ``p``.
"""

import logging
from unittest.mock import patch

import pytest

from src.config.core import load_infrastructure_config
from src.config.models import InfrastructureConfig, SuperagentRuntimeConfig
from src.config.settings import get_superagent_runtime

ROLES = ("supervisor", "watchdog")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.setattr("src.config.settings._SUPERAGENT_RUNTIME_WARNED", set())
    for role in ROLES:
        monkeypatch.delenv(f"CONDUCTOR_SUPERAGENT_RUNTIME_{role.upper()}", raising=False)


def _with_config(**runtime):
    config = InfrastructureConfig(superagent_runtime=runtime)
    return patch("src.config.settings.get_infrastructure_config", return_value=config)


def test_defaults_are_ptc():
    assert SuperagentRuntimeConfig().supervisor == "ptc"
    assert SuperagentRuntimeConfig().watchdog == "ptc"
    assert InfrastructureConfig().superagent_runtime == SuperagentRuntimeConfig()
    with _with_config():
        for role in ROLES:
            assert get_superagent_runtime(role) == "ptc"


def test_committed_config_keeps_both_roles_on_ptc():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    load_infrastructure_config.cache_clear()
    try:
        config = load_infrastructure_config(str(root / "config.yaml"))
    finally:
        load_infrastructure_config.cache_clear()
    assert config.superagent_runtime.supervisor == "ptc"
    assert config.superagent_runtime.watchdog == "ptc"


def test_config_yaml_values_are_honored(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("superagent_runtime:\n  supervisor: p\n  watchdog: ' P '\n")
    load_infrastructure_config.cache_clear()
    try:
        config = load_infrastructure_config(str(path))
    finally:
        load_infrastructure_config.cache_clear()
    assert config.superagent_runtime.supervisor == "p"
    assert config.superagent_runtime.watchdog == "p"
    with patch("src.config.settings.get_infrastructure_config", return_value=config):
        assert get_superagent_runtime("supervisor") == "p"
        assert get_superagent_runtime("watchdog") == "p"


@pytest.mark.parametrize("role", ROLES)
def test_env_overrides_config_each_role(monkeypatch, role):
    other = "watchdog" if role == "supervisor" else "supervisor"
    env = f"CONDUCTOR_SUPERAGENT_RUNTIME_{role.upper()}"
    with _with_config(supervisor="ptc", watchdog="ptc"):
        monkeypatch.setenv(env, "  P ")
        assert get_superagent_runtime(role) == "p"
        assert get_superagent_runtime(other) == "ptc"
    with _with_config(supervisor="p", watchdog="p"):
        monkeypatch.setenv(env, "ptc")
        assert get_superagent_runtime(role) == "ptc"
        assert get_superagent_runtime(other) == "p"


@pytest.mark.parametrize("role", ROLES)
def test_set_but_empty_env_means_ptc(monkeypatch, role):
    monkeypatch.setenv(f"CONDUCTOR_SUPERAGENT_RUNTIME_{role.upper()}", "  ")
    with _with_config(supervisor="p", watchdog="p"):
        assert get_superagent_runtime(role) == "ptc"


def test_unknown_env_value_is_ptc_with_warning(monkeypatch, caplog):
    monkeypatch.setenv("CONDUCTOR_SUPERAGENT_RUNTIME_WATCHDOG", "native")
    with _with_config(watchdog="p"), caplog.at_level(logging.WARNING):
        assert get_superagent_runtime("watchdog") == "ptc"
    assert "CONDUCTOR_SUPERAGENT_RUNTIME_WATCHDOG" in caplog.text
    assert "native" in caplog.text


def test_unknown_config_value_is_ptc_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        config = SuperagentRuntimeConfig(supervisor="pp", watchdog="")
    assert config.supervisor == "ptc"
    assert config.watchdog == "ptc"
    assert "superagent_runtime.supervisor" in caplog.text
    with patch(
        "src.config.settings.get_infrastructure_config",
        return_value=InfrastructureConfig(superagent_runtime=config),
    ):
        assert get_superagent_runtime("supervisor") == "ptc"


@pytest.mark.parametrize("role", ["worker", "generalist", "", "SUPERVISOR"])
def test_unknown_role_is_ptc(monkeypatch, role):
    monkeypatch.setenv("CONDUCTOR_SUPERAGENT_RUNTIME_WORKER", "p")
    with _with_config(supervisor="p", watchdog="p"):
        assert get_superagent_runtime(role) == "ptc"


def test_router_snapshot_is_off_while_supervisor_runs_on_p(monkeypatch):
    # Planner ruling 2: snapshot cache hits drop the channel notes the P
    # history window may no longer hold.
    from src.config.settings import get_autonomy_router_snapshot_enabled

    monkeypatch.setenv("AUTONOMY_ROUTER_SNAPSHOT_ENABLED", "true")
    assert get_autonomy_router_snapshot_enabled() is True
    monkeypatch.setenv("CONDUCTOR_SUPERAGENT_RUNTIME_WATCHDOG", "p")
    assert get_autonomy_router_snapshot_enabled() is True
    monkeypatch.setenv("CONDUCTOR_SUPERAGENT_RUNTIME_SUPERVISOR", "p")
    assert get_autonomy_router_snapshot_enabled() is False


def test_an_unknown_env_value_warns_once_not_every_admission(monkeypatch, caplog):
    monkeypatch.setenv("CONDUCTOR_SUPERAGENT_RUNTIME_SUPERVISOR", "native")
    with _with_config(), caplog.at_level(logging.WARNING):
        for _ in range(5):
            assert get_superagent_runtime("supervisor") == "ptc"
    assert caplog.text.count("CONDUCTOR_SUPERAGENT_RUNTIME_SUPERVISOR") == 1
