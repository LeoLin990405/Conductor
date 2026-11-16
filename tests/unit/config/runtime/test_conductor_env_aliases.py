"""``CONDUCTOR_*`` is the name; ``PTC_*`` stays a deprecated alias.

The rename must be safe to land variable by variable: the new spelling wins
when both are set, a deployment that still exports only the old spelling keeps
working, and an empty new value does not mask an old one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from src.config.runtime.file_utils import find_config_file, get_env, legacy_env_names


def test_the_new_name_wins_when_both_are_set(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_PROBE_SETTING", "new")
    monkeypatch.setenv("PTC_PROBE_SETTING", "old")

    assert get_env("CONDUCTOR_PROBE_SETTING") == "new"


def test_the_legacy_name_still_works(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_PROBE_SETTING", raising=False)
    monkeypatch.setenv("PTC_PROBE_SETTING", "old")

    assert get_env("CONDUCTOR_PROBE_SETTING") == "old"


def test_an_empty_new_value_falls_back(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_PROBE_SETTING", "")
    monkeypatch.setenv("PTC_PROBE_SETTING", "old")

    assert get_env("CONDUCTOR_PROBE_SETTING") == "old"


def test_an_unset_pair_returns_the_default(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_PROBE_SETTING", raising=False)
    monkeypatch.delenv("PTC_PROBE_SETTING", raising=False)

    assert get_env("CONDUCTOR_PROBE_SETTING", "fallback") == "fallback"


def test_only_conductor_names_have_an_alias():
    assert legacy_env_names("CONDUCTOR_CONFIG_FILE") == ("PTC_CONFIG_FILE",)
    assert legacy_env_names("PTC_CONFIG_FILE") == ()
    assert legacy_env_names("PATH") == ()


@pytest.mark.parametrize("env_name", ["CONDUCTOR_CONFIG_FILE", "PTC_CONFIG_FILE"])
def test_find_config_file_honours_both_spellings(monkeypatch, tmp_path, env_name):
    config = tmp_path / "agent_config.yaml"
    config.write_text("llm: {}\n", encoding="utf-8")
    monkeypatch.delenv("CONDUCTOR_CONFIG_FILE", raising=False)
    monkeypatch.delenv("PTC_CONFIG_FILE", raising=False)
    monkeypatch.setenv(env_name, str(config))

    assert find_config_file("agent_config.yaml", env_var="CONDUCTOR_CONFIG_FILE") == config


def test_sandbox_constants_use_the_new_spelling_and_keep_the_alias():
    from ptc_agent.core.sandbox._shared import TURN_CWD_ENV, TURN_CWD_LEGACY_ENV

    assert TURN_CWD_ENV == "CONDUCTOR_TURN_CWD"
    assert TURN_CWD_LEGACY_ENV == "PTC_TURN_CWD"


def test_the_sandbox_sitecustomize_reads_both_spellings():
    source = (
        Path(__file__).resolve().parents[4]
        / "src/ptc_agent/core/sandbox/turn_cwd_sitecustomize.py"
    ).read_text(encoding="utf-8")

    assert "CONDUCTOR_TURN_CWD" in source
    assert "PTC_TURN_CWD" in source


def _load_env_helper(module_name: str, relative_path: str):
    """Load a CLI source file without importing the optional ``ptc_cli`` extra.

    ``conductor-cli`` is an optional dependency, so the default test venv does
    not have it installed. Loading the two files directly still exercises the
    shipped source, without dragging in the TUI packages that ``ptc_cli``
    itself imports.
    """
    path = Path(__file__).resolve().parents[4] / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module._env


def test_the_cli_helpers_read_both_spellings(monkeypatch):
    theme_env = _load_env_helper("_ticket_theme_env", "libs/ptc-cli/ptc_cli/core/theme.py")
    main_env = _load_env_helper("_ticket_main_env", "libs/ptc-cli/ptc_cli/main.py")

    monkeypatch.setenv("CONDUCTOR_SERVER_URL", "http://new")
    monkeypatch.setenv("PTC_SERVER_URL", "http://old")
    assert main_env("CONDUCTOR_SERVER_URL") == "http://new"

    monkeypatch.delenv("CONDUCTOR_SERVER_URL", raising=False)
    assert main_env("CONDUCTOR_SERVER_URL") == "http://old"

    monkeypatch.setenv("CONDUCTOR_THEME", "light")
    monkeypatch.setenv("PTC_THEME", "dark")
    assert theme_env("CONDUCTOR_THEME") == "light"

    monkeypatch.delenv("CONDUCTOR_THEME", raising=False)
    assert theme_env("CONDUCTOR_THEME") == "dark"
    assert theme_env("CONDUCTOR_NOT_SET") is None


def test_no_target_source_reads_a_bare_ptc_env_name():
    """Every read in the renamed scopes goes through the alias-aware helper.

    Comments and the compatibility helpers themselves still spell the old
    names; this asserts no ``os.environ.get("PTC_...")`` call remains.
    """
    root = Path(__file__).resolve().parents[4]
    scopes = (
        "src/config/runtime",
        "src/ptc_agent/core/sandbox",
        "libs/ptc-cli/ptc_cli/main.py",
        "libs/ptc-cli/ptc_cli/core/theme.py",
    )
    offenders: list[str] = []
    for scope in scopes:
        base = root / scope
        paths = [base] if base.is_file() else list(base.rglob("*.py"))
        for path in paths:
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if "os.environ.get(\"PTC_" in line or "os.getenv(\"PTC_" in line:
                    # The alias helper itself builds the legacy name.
                    if "\"PTC_\" +" in line:
                        continue
                    offenders.append(f"{path.relative_to(root)}:{lineno}: {line.strip()}")
    assert not offenders, "bare legacy env reads:\n" + "\n".join(offenders)


def test_a_legacy_name_that_supplies_a_value_is_reported_once(monkeypatch, caplog):
    import logging

    from src.config.runtime import file_utils

    monkeypatch.setattr(file_utils, "_WARNED_LEGACY_ENV", set())
    monkeypatch.setenv("CONDUCTOR_ALIAS_PROBE", "")
    monkeypatch.setenv("PTC_ALIAS_PROBE", "old")
    with caplog.at_level(logging.WARNING, logger=file_utils.__name__):
        assert file_utils.get_env("CONDUCTOR_ALIAS_PROBE") == "old"
        assert file_utils.get_env("CONDUCTOR_ALIAS_PROBE") == "old"
    warnings = [r for r in caplog.records if "PTC_ALIAS_PROBE" in r.getMessage()]
    assert len(warnings) == 1
    assert "CONDUCTOR_ALIAS_PROBE" in warnings[0].getMessage()

    caplog.clear()
    monkeypatch.setenv("CONDUCTOR_ALIAS_PROBE", "new")
    with caplog.at_level(logging.WARNING, logger=file_utils.__name__):
        assert file_utils.get_env("CONDUCTOR_ALIAS_PROBE") == "new"
    assert not caplog.records


def test_migrations_find_the_config_under_the_new_name():
    """046/055 mirror the runtime config search, so they read the new name
    first and keep the old one as its alias."""
    root = Path(__file__).resolve().parents[4]
    for rel in (
        "migrations/versions/046_computers.py",
        "migrations/versions/055_user_level_mcp_and_vault.py",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        assert 'os.getenv("CONDUCTOR_CONFIG_FILE") or os.getenv("PTC_CONFIG_FILE")' in text, rel
