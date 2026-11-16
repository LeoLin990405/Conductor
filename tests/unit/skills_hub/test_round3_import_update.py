"""Imported-copy updates refuse local edits unless --force snapshots them first."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src.skills_hub.core import SkillsError
from src.skills_hub.importer import import_source
from src.skills_hub.manager import rollback
from src.skills_hub.updates import update_skill


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _git(cwd: Path | None, *args: str) -> None:
    subprocess.run(
        ["git", *(() if cwd is None else ("-C", str(cwd))), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )


def _commit(repo: Path, message: str, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _imported(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Local seed, bare upstream, and a central repo whose copy has no .git."""
    bare = tmp_path / "upstream.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(None, "init", "-b", "main", str(seed))
    _commit(
        seed,
        "one",
        {
            "alpha/SKILL.md": "---\nname: alpha\ndescription: alpha\n---\n\nversion one\n"
        },
    )
    _git(None, "init", "--bare", "-b", "main", str(bare))
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-u", "origin", "main")
    imported = tmp_path / "central"
    import_source(imported, str(seed))
    assert not (imported / "alpha" / ".git").exists()
    return seed, bare, imported


def _push_v2(seed: Path) -> None:
    _commit(
        seed,
        "two",
        {
            "alpha/SKILL.md": "---\nname: alpha\ndescription: alpha\n---\n\nversion two\n"
        },
    )
    _git(seed, "push", "origin", "main")


@pytest.fixture
def git_ok() -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")


def test_imported_update_refuses_local_modified(git_ok: None, tmp_path: Path) -> None:
    seed, _bare, imported = _imported(tmp_path)
    _push_v2(seed)
    skill_md = imported / "alpha" / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8") + "local edit\n", encoding="utf-8"
    )
    edited = skill_md.read_text(encoding="utf-8")

    with pytest.raises(SkillsError, match="local_modified"):
        update_skill(imported, "alpha")

    assert skill_md.read_text(encoding="utf-8") == edited
    assert "version two" not in edited


def test_force_snapshots_local_edit_and_rollback_restores_it(
    git_ok: None, tmp_path: Path
) -> None:
    seed, _bare, imported = _imported(tmp_path)
    _push_v2(seed)
    skill_md = imported / "alpha" / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8") + "local edit\n", encoding="utf-8"
    )
    edited = skill_md.read_text(encoding="utf-8")

    updated = update_skill(imported, "alpha", force=True)

    assert "version two" in skill_md.read_text(encoding="utf-8")
    snapshot_id = updated["snapshot_id"]
    assert (imported / "snapshots" / f"{snapshot_id}.json").is_file()
    agent_dir = tmp_path / "agent-skills"
    agent_dir.mkdir()
    restored = rollback(imported, snapshot_id, allowed_dirs={agent_dir})

    assert "local edit" in skill_md.read_text(encoding="utf-8")
    assert skill_md.read_text(encoding="utf-8") == edited
    assert snapshot_id == restored["snapshot"]
