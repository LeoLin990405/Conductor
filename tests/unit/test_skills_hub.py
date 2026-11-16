from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import pytest

from conductor_cli.skills_hub.catalog import AGENT_ADAPTERS, get_adapters, get_skill
from conductor_cli.skills_hub.commands import handle_install, register_parser
from conductor_cli import cli

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def isolated_root():
    with tempfile.TemporaryDirectory(prefix=".skills-hub-test-", dir=REPO_ROOT) as root:
        yield Path(root)


def _make_source(root: Path) -> Path:
    source = root / "source" / "agent-browser"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: agent-browser\n---\n", encoding="utf-8")
    return source


def test_catalog_and_adapters_are_allowlisted():
    assert get_skill("agent-browser").source_directory == "skills/agent-browser"
    assert tuple(adapter.name for adapter in get_adapters()) == (
        "claude",
        "codex",
        "cursor",
        "opencode",
        "agents",
    )
    assert get_adapters(("codex", "agents")) == (AGENT_ADAPTERS[1], AGENT_ADAPTERS[4])
    assert get_adapters(("codex", "codex")) == (AGENT_ADAPTERS[1],)
    with pytest.raises(ValueError, match="Unsupported agent adapter"):
        get_adapters(("unknown",))
    with pytest.raises(ValueError, match="Unknown skill"):
        get_skill("unlisted")


def test_hub_installs_selected_adapter_only_to_fake_home(isolated_root: Path, monkeypatch: pytest.MonkeyPatch):
    fake_home = isolated_root / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    source = _make_source(isolated_root)
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    register_parser(commands)
    args = parser.parse_args(
        ["skills-hub", "install", "agent-browser", "--agent", "codex", "--source", str(source)]
    )

    result = handle_install(args)

    expected = fake_home / ".codex/skills/agent-browser"
    assert result["installed"] == (str(expected),)
    assert not result["already_present"]
    assert not result["failed"]
    assert (expected / "SKILL.md").is_file()
    assert not (fake_home / ".claude/skills/agent-browser").exists()


def test_cli_routes_hub_locally_without_api_client(isolated_root: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    fake_home = isolated_root / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    def unexpected_client(*args, **kwargs):
        raise AssertionError("skills-hub must not construct the backend API client")

    monkeypatch.setattr(cli, "APIClient", unexpected_client)

    status = cli.main(["skills-hub", "install", "agent-browser", "--agent", "codex", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert status == 0
    assert output["ok"] is True
    assert output["data"]["installed"] == [str(fake_home / ".codex/skills/agent-browser")]
    assert (fake_home / ".codex/skills/agent-browser/SKILL.md").is_file()
