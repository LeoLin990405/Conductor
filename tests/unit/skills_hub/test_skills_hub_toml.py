"""skills-hub.toml overlay and the Kimi Code skill directory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.skills_hub.adapters import AgentDirs
from src.skills_hub.config import TOML_CONFIG_FILENAME, SkillsConfigError, load_config


def _write_toml(home: Path, text: str) -> None:
    path = home / ".conductor" / TOML_CONFIG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_toml_filename_is_skills_hub_toml() -> None:
    assert TOML_CONFIG_FILENAME == "skills-hub.toml"


def test_kimi_dir_is_kimi_code_skills(home: Path) -> None:
    assert AgentDirs(home).dir_for("kimi") == home / ".kimi-code" / "skills"
    assert load_config(home=home).agent_dir("kimi") == home / ".kimi-code" / "skills"


def test_toml_override_adds_custom_agent_path(home: Path) -> None:
    _write_toml(home, '[agents]\ncustom = ".customagent/skills"\n')

    config = load_config(home=home)

    assert config.agent_dir("custom") == home / ".customagent" / "skills"
    assert config.agent_dir("claude") == home / ".claude" / "skills"


def test_toml_escape_is_rejected(home: Path) -> None:
    _write_toml(home, '[agents]\ncustom = "../escape"\n')

    with pytest.raises(SkillsConfigError, match="outside the home"):
        load_config(home=home)


def test_missing_toml_is_fine(home: Path) -> None:
    assert not (home / ".conductor" / TOML_CONFIG_FILENAME).exists()

    config = load_config(home=home)

    assert config.repo == home / ".conductor" / "skills"
    assert config.agent_dir("kimi") == home / ".kimi-code" / "skills"


def test_toml_wins_when_json_sets_the_same_agent(home: Path) -> None:
    config_dir = home / ".config" / "conductor"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "repo": str(home / "json-repo"),
                "default_link_mode": "copy",
                "agents": {"claude": ".from-json", "zcode": ".json-zcode"},
            }
        ),
        encoding="utf-8",
    )
    _write_toml(
        home,
        "\n".join(
            (
                'repo = "/from/toml"',
                'note = "ignored"',
                "",
                "[agents]",
                'claude = ".from-toml"',
                'custom = ".customagent/skills"',
                "",
            )
        ),
    )

    config = load_config(home=home)

    assert config.repo == home / "json-repo"
    assert config.default_link_mode == "copy"
    assert config.agent_dir("claude") == home / ".from-toml"
    assert config.agent_dir("zcode") == home / ".json-zcode"
    assert config.agent_dir("custom") == home / ".customagent" / "skills"


def test_invalid_toml_raises(home: Path) -> None:
    _write_toml(home, "[\n")

    with pytest.raises(SkillsConfigError, match="not valid TOML"):
        load_config(home=home)


def test_toml_absolute_agent_path_is_allowed(home: Path, tmp_path: Path) -> None:
    target = tmp_path / "abs-skills"
    _write_toml(home, f'[agents]\ncustom = "{target}"\n')

    config = load_config(home=home)

    assert config.agent_dir("custom") == target
