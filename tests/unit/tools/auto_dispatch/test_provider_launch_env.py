"""Dispatch Workers get the provider keys cond gives daemons (backlog N26).

A dispatch Worker is a child of the backend, so DeepSeek DSH failed with
MISSING_CREDENTIAL: its key lives in ~/.config/deepseek/env, which cond reads
only for daemons. provider_launch_env applies the same allow-list and file rules.
"""

from __future__ import annotations

from pathlib import Path

from src.tools.auto_dispatch.execution import provider_launch_env


def _home(tmp_path: Path, text: str, *, mode: int = 0o600) -> dict[str, str]:
    env_file = tmp_path / ".config" / "deepseek" / "env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(text, encoding="utf-8")
    env_file.chmod(mode)
    return {"HOME": str(tmp_path), "CONDUCTOR_HOME": str(tmp_path / ".conductor")}


def test_deepseek_gets_only_allow_listed_names(tmp_path):
    environ = _home(tmp_path, 'export DEEPSEEK_API_KEY="sk-test"\nOTHER_SECRET=no\nDSH_HOME=/x\n')
    assert provider_launch_env("deepseek", environ=environ) == {"DEEPSEEK_API_KEY": "sk-test", "DSH_HOME": "/x"}


def test_a_name_already_set_is_not_overridden(tmp_path):
    environ = _home(tmp_path, "DEEPSEEK_API_KEY=from-file\n")
    environ["DEEPSEEK_API_KEY"] = "from-env"
    assert "DEEPSEEK_API_KEY" not in provider_launch_env("deepseek", environ=environ)


def test_a_readable_file_is_ignored(tmp_path):
    environ = _home(tmp_path, "DEEPSEEK_API_KEY=sk-test\n", mode=0o644)
    assert provider_launch_env("deepseek", environ=environ) == {}


def test_secrets_env_is_read_for_the_providers_names(tmp_path):
    secrets = tmp_path / ".conductor" / "secrets.env"
    secrets.parent.mkdir(parents=True)
    secrets.write_text("CURSOR_API_KEY=cur-test\nDEEPSEEK_API_KEY=ds-test\n", encoding="utf-8")
    secrets.chmod(0o600)
    environ = {"HOME": str(tmp_path), "CONDUCTOR_HOME": str(tmp_path / ".conductor")}
    assert provider_launch_env("cursor", environ=environ) == {"CURSOR_API_KEY": "cur-test"}


def test_an_unknown_provider_or_a_broken_file_adds_nothing(tmp_path):
    environ = _home(tmp_path, "not a valid line\n")
    assert provider_launch_env("deepseek", environ=environ) == {}
    assert provider_launch_env("nobody", environ=environ) == {}
