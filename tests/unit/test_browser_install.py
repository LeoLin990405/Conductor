from __future__ import annotations

import tempfile
import tomllib
from pathlib import Path

import pytest

from conductor_cli.browser.install import _default_source, install_skills

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def isolated_root():
    """Keep fake HOME, sources, and destinations inside this checkout."""
    with tempfile.TemporaryDirectory(prefix=".browser-install-test-", dir=REPO_ROOT) as root:
        yield Path(root)


def _make_source(root: Path) -> Path:
    source = root / "source" / "agent-browser"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: agent-browser\n---\n", encoding="utf-8")
    (source / "references" / "steps.md").write_text("bounded steps\n", encoding="utf-8")
    return source


def test_installs_missing_skill_to_explicit_targets_and_is_idempotent(isolated_root: Path):
    source = _make_source(isolated_root)
    target_a = isolated_root / "home" / ".claude" / "skills" / "agent-browser"
    target_b = isolated_root / "home" / ".agents" / "skills" / "agent-browser"

    result = install_skills(source, [target_a, target_b])

    assert result.installed == (str(target_a), str(target_b))
    assert not result.already_present
    assert not result.failed
    assert (target_a / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert (target_a / "references" / "steps.md").read_text(encoding="utf-8") == "bounded steps\n"
    assert (target_a / "SKILL.md").stat().st_mode & 0o777 == 0o600

    again = install_skills(source, [target_a, target_b])
    assert not again.installed
    assert again.already_present == (str(target_a), str(target_b))
    assert not again.failed


def test_existing_destination_is_skipped_without_overwrite(isolated_root: Path):
    source = _make_source(isolated_root)
    target = isolated_root / "home" / ".codex" / "skills" / "agent-browser"
    target.mkdir(parents=True)
    marker = target / "user-file.txt"
    marker.write_text("keep this", encoding="utf-8")

    result = install_skills(source, [target])

    assert result.already_present == (str(target),)
    assert marker.read_text(encoding="utf-8") == "keep this"
    assert not (target / "SKILL.md").exists()


def test_symlinked_destination_parent_is_rejected_without_following(isolated_root: Path):
    source = _make_source(isolated_root)
    outside = isolated_root / "outside"
    outside.mkdir()
    symlink_parent = isolated_root / "home" / ".cursor"
    symlink_parent.parent.mkdir()
    symlink_parent.symlink_to(outside, target_is_directory=True)
    target = symlink_parent / "skills" / "agent-browser"

    result = install_skills(source, [target])

    assert not result.installed
    assert not result.already_present
    assert len(result.failed) == 1
    assert not (outside / "skills").exists()


def test_symlinked_source_file_is_rejected(isolated_root: Path):
    source = _make_source(isolated_root)
    external = isolated_root / "private.txt"
    external.write_text("sensitive marker", encoding="utf-8")
    (source / "linked.txt").symlink_to(external)
    target = isolated_root / "home" / ".opencode" / "skills" / "agent-browser"

    result = install_skills(source, [target])

    assert not result.installed
    assert not target.exists()
    assert len(result.failed) == 1
    assert "sensitive marker" not in repr(result)


def test_defaults_cover_supported_agent_skill_roots(isolated_root: Path, monkeypatch: pytest.MonkeyPatch):
    source = _make_source(isolated_root)
    home = isolated_root / "fake-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    result = install_skills(source)

    expected = (
        home / ".claude/skills/agent-browser",
        home / ".codex/skills/agent-browser",
        home / ".cursor/skills/agent-browser",
        home / ".config/opencode/skills/agent-browser",
        home / ".agents/skills/agent-browser",
    )
    assert result.installed == tuple(str(path) for path in expected)
    assert all(path.is_dir() for path in expected)


def test_repository_skill_is_the_default_source(isolated_root: Path, monkeypatch: pytest.MonkeyPatch):
    home = isolated_root / "fake-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    assert (_default_source() / "SKILL.md").is_file()
    result = install_skills()

    assert len(result.installed) == 5
    assert not result.failed


def test_wheel_force_includes_the_single_repository_skill_source():
    with (REPO_ROOT / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)

    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"] == {
        "skills/agent-browser": "conductor_cli/browser/skill"
    }


def test_source_without_skill_manifest_is_rejected(isolated_root: Path):
    source = isolated_root / "incomplete"
    source.mkdir()
    (source / "notes.txt").write_text("not a skill", encoding="utf-8")
    target = isolated_root / "home" / ".agents/skills/agent-browser"

    result = install_skills(source, [target])

    assert not result.installed
    assert not target.exists()
    assert len(result.failed) == 1
