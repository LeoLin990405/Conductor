"""``conductor skills --home``, preview-by-default writers, drift, fixed dirs."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from src.conductor_cli.cli import main
from src.skills_hub.adapters import AgentDirs
from src.skills_hub.config import load_config
from src.skills_hub.drift import drift_report

from ..mcp_hub.conftest import isolated_home  # noqa: F401 - fixture


@pytest.fixture
def homes(isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    monkeypatch.delenv("CONDUCTOR_URL", raising=False)
    monkeypatch.delenv("CONDUCTOR_SKILLS_REPO", raising=False)
    monkeypatch.delenv("CONDUCTOR_SKILLS_CONFIG", raising=False)
    rehearsal = tmp_path / "rehearsal"
    rehearsal.mkdir()
    return isolated_home, rehearsal


def run(argv: list[str]) -> tuple[int, object]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main(argv)
    text = out.getvalue().strip()
    try:
        return code, json.loads(text)
    except json.JSONDecodeError:
        return code, text


def make_skill(root: Path, name: str, body: str = "body") -> None:
    skill = root / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\n\n{body}\n", encoding="utf-8"
    )


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes() if path.is_file() else None)
        for path in sorted(root.rglob("*"))
    }


def test_dsh_skills_dir_is_dsh_home_with_legacy_detection(tmp_path: Path) -> None:
    dirs = AgentDirs(tmp_path)
    entry = dirs.get("dsh")
    # dsh scans $DSH_HOME/skills; the old row named a dir dsh never reads.
    assert entry.directory == tmp_path / ".dsh" / "skills"
    assert tmp_path / ".config" / "conductor" / "deepseek" / "skills" in entry.legacy
    assert dirs.dir_for("kimi") == tmp_path / ".kimi-code" / "skills"
    assert dirs.dir_for("minimax") == tmp_path / ".minimax" / "skills"
    assert dirs.dir_for("gemini") == tmp_path / ".gemini" / "skills"
    assert dirs.dir_for("antigravity") == tmp_path / ".gemini" / "antigravity" / "skills"
    assert dirs.dir_for("qwen") == tmp_path / ".qwen" / "skills"


def test_install_is_a_preview_without_apply_and_respects_home(homes) -> None:
    process_home, rehearsal = homes
    repo = rehearsal / ".conductor" / "skills"
    make_skill(repo, "alpha")
    before = _tree(rehearsal)
    code, out = run(
        ["skills", "install", "alpha", "--agent", "codex", "--home", str(rehearsal)]
    )
    assert code == 0
    assert out["dry_run"] is True
    assert out["agents"] == [
        {"agent": "codex", "dir": str(rehearsal / ".codex" / "skills")}
    ]
    assert _tree(rehearsal) == before
    code, out = run(
        [
            "skills",
            "install",
            "alpha",
            "--agent",
            "codex",
            "--home",
            str(rehearsal),
            "--apply",
        ]
    )
    assert code == 0
    assert (rehearsal / ".codex" / "skills" / "alpha" / "SKILL.md").exists()
    assert list(process_home.iterdir()) == []


def test_custom_repo_still_resolves_agent_dirs_under_home(homes) -> None:
    _process_home, rehearsal = homes
    config = rehearsal / ".config" / "conductor" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"repo": str(rehearsal / "elsewhere")}), encoding="utf-8")
    loaded = load_config(rehearsal, environ={})
    # Old code fell back to the process HOME for agent dirs whenever the
    # repo was not <home>/.conductor/skills.
    assert loaded.agent_dir("codex") == rehearsal / ".codex" / "skills"


def test_skills_drift_is_read_only(homes) -> None:
    _process_home, rehearsal = homes
    repo = rehearsal / ".conductor" / "skills"
    make_skill(repo, "alpha")
    make_skill(rehearsal / ".codex" / "skills", "manual-one")
    legacy = rehearsal / ".config" / "conductor" / "deepseek" / "skills"
    make_skill(legacy, "old")
    draft = rehearsal / "ws" / "draft-1234" / "s"
    make_skill(draft.parent, "s")
    (rehearsal / ".qwen" / "skills").mkdir(parents=True)
    (rehearsal / ".qwen" / "skills" / "s").symlink_to(draft)
    before = _tree(rehearsal)
    code, out = run(["skills", "drift", "--home", str(rehearsal), "--json"])
    assert code == 0, out
    data = out["data"] if "data" in out else out
    assert _tree(rehearsal) == before
    codex = data["agents"]["codex"]
    assert codex["extra"] == ["manual-one"]
    assert codex["not_installed"] == ["alpha"]
    assert data["agents"]["deepseek"]["legacy"] == [str(legacy)]
    assert data["agents"]["qwen"]["fragile"] == ["s"]


def test_drift_function_handles_missing_repo(tmp_path: Path) -> None:
    report = drift_report(tmp_path, tmp_path / "no-repo", [("codex", tmp_path / ".codex" / "skills")])
    assert report["central_skills"] == 0
    assert report["agents"]["codex"]["missing"] == []


def test_tilde_in_rehearsal_config_means_the_rehearsal_home(homes) -> None:
    process_home, rehearsal = homes
    config = rehearsal / ".config" / "conductor" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {"repo": "~/.conductor/skills", "agents": {"claude": "~/.claude/skills"}}
        ),
        encoding="utf-8",
    )
    make_skill(rehearsal / ".conductor" / "skills", "demo")
    code, out = run(
        [
            "skills",
            "install",
            "demo",
            "--agent",
            "claude",
            "--home",
            str(rehearsal),
            "--apply",
        ]
    )
    assert code == 0, out
    # Old code expanded ~ against the process HOME and linked the skill there.
    assert (rehearsal / ".claude" / "skills" / "demo").exists()
    assert list(process_home.iterdir()) == []


def test_rehearsal_config_pointing_outside_home_is_refused(homes, tmp_path: Path) -> None:
    _process_home, rehearsal = homes
    config = rehearsal / ".config" / "conductor" / "config.json"
    config.parent.mkdir(parents=True)
    outside = tmp_path / "outside" / "skills"
    config.write_text(json.dumps({"agents": {"claude": str(outside)}}), encoding="utf-8")
    code, _out = run(["skills", "status", "--home", str(rehearsal), "--json"])
    assert code != 0
    assert not outside.exists()
