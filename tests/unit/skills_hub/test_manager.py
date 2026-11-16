"""skills_hub.manager — install/uninstall/sync with the tracked manifest."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.skills_hub import importer, manager
from src.skills_hub.adapters import AgentDirs, UnknownAgentError
from src.skills_hub.core import SkillsError, load_manifest
from src.skills_hub.snapshots import list_snapshots


def make_skill(root: Path, name: str) -> None:
    skill = root / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\n\nbody\n", encoding="utf-8"
    )


@pytest.fixture
def populated(repo: Path, tmp_path: Path) -> Path:
    make_skill(repo, "alpha")
    make_skill(repo, "beta")
    return repo


@pytest.fixture
def dirs(home: Path) -> AgentDirs:
    return AgentDirs(home)


def test_install_symlinks_into_agent_dirs(
    populated: Path, dirs: AgentDirs
) -> None:
    agents = [("claude", dirs.dir_for("claude")), ("zcode", dirs.dir_for("zcode"))]

    result = manager.install(populated, agents, ["alpha", "beta"])

    assert set(result.installed) == {
        ("claude", "alpha"),
        ("claude", "beta"),
        ("zcode", "alpha"),
        ("zcode", "beta"),
    }
    link = dirs.dir_for("claude") / "alpha"
    assert link.is_symlink()
    assert (link / "SKILL.md").is_file()
    manifest = load_manifest(populated)
    assert sorted(manifest["alpha"]["agents"]) == ["claude", "zcode"]


def test_install_copy_mode_materializes_directories(
    populated: Path, dirs: AgentDirs
) -> None:
    agents = [("claude", dirs.dir_for("claude"))]

    manager.install(populated, agents, ["alpha"], mode="copy")

    entry = dirs.dir_for("claude") / "alpha"
    assert entry.is_dir() and not entry.is_symlink()
    assert (entry / "SKILL.md").is_file()


def test_install_unknown_skill_raises(populated: Path, dirs: AgentDirs) -> None:
    with pytest.raises(SkillsError, match="not found in the central repo"):
        manager.install(populated, [("claude", dirs.dir_for("claude"))], ["nope"])


def test_install_skips_entries_that_already_exist(
    populated: Path, dirs: AgentDirs
) -> None:
    agents = [("claude", dirs.dir_for("claude"))]
    manager.install(populated, agents, ["alpha"])

    result = manager.install(populated, agents, ["alpha"])

    assert result.installed == ()
    assert result.skipped == (("claude", "alpha", "already installed"),)


def test_uninstall_removes_only_tracked_entries(
    populated: Path, dirs: AgentDirs, home: Path
) -> None:
    agents = [("claude", dirs.dir_for("claude"))]
    manager.install(populated, agents, ["alpha"])
    foreign = dirs.dir_for("claude") / "handmade"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("---\nname: handmade\n---\nx")

    result = manager.uninstall(populated, agents, ["alpha", "handmade"])

    assert result.uninstalled == (("claude", "alpha"),)
    assert result.skipped == (("claude", "handmade", "not installed by conductor"),)
    assert not (dirs.dir_for("claude") / "alpha").exists()
    assert foreign.exists()  # a hand-installed skill is never touched
    assert "alpha" not in load_manifest(populated)


def test_uninstall_without_names_removes_all_tracked(
    populated: Path, dirs: AgentDirs
) -> None:
    agents = [("claude", dirs.dir_for("claude"))]
    manager.install(populated, agents, ["alpha", "beta"])

    result = manager.uninstall(populated, agents)

    assert set(result.uninstalled) == {("claude", "alpha"), ("claude", "beta")}
    assert not any(dirs.dir_for("claude").iterdir())


def test_install_refuses_git_worktree(repo: Path, dirs: AgentDirs, home: Path) -> None:
    make_skill(repo, "alpha")
    target = home / "projects" / "repo" / ".claude" / "skills"
    target.mkdir(parents=True)
    (target.parents[1] / ".git").mkdir()  # a bare .git dir marks a worktree

    with pytest.raises(SkillsError, match="git worktree"):
        manager.install(repo, [("claude", target)], ["alpha"])


def test_install_snapshots_first(populated: Path, dirs: AgentDirs) -> None:
    agents = [("claude", dirs.dir_for("claude"))]

    manager.install(populated, agents, ["alpha"])

    assert list_snapshots(populated), "install must snapshot before changing anything"


def test_rollback_restores_previous_state(
    populated: Path, dirs: AgentDirs
) -> None:
    agents = [("claude", dirs.dir_for("claude"))]
    manager.install(populated, agents, ["alpha"])
    # install of "beta" snapshots the state that had "alpha" in place
    manager.install(populated, agents, ["beta"])
    before = [s["id"] for s in list_snapshots(populated)][0]
    manager.uninstall(populated, agents, ["alpha", "beta"])
    assert not (dirs.dir_for("claude") / "alpha").exists()

    result = manager.rollback(populated, before)

    assert result["restored"] == {"claude": ["alpha"]}
    assert (dirs.dir_for("claude") / "alpha").is_symlink()
    # "beta" is not in this snapshot, so rollback neither restores it nor
    # reports it as kept: it is simply not this snapshot's business.
    assert result["kept"] == {}
    assert not (dirs.dir_for("claude") / "beta").exists()


def test_sync_installs_missing_and_reports_stale_without_deleting(
    populated: Path, dirs: AgentDirs
) -> None:
    agents = [("claude", dirs.dir_for("claude"))]
    manager.install(populated, agents, ["alpha", "beta"])
    importer.remove_skill(populated, "beta")

    result = manager.sync(populated, agents)

    # A skill that left the repo is reported, not deleted: a routine sync
    # must never remove what a later sync could have restored.
    assert result.stale == (("claude", "beta"),)
    assert result.uninstalled == ()
    assert (dirs.dir_for("claude") / "beta").is_symlink()  # entry left in place
    assert "beta" in load_manifest(populated)
    assert ("claude", "alpha") not in result.uninstalled
    assert (dirs.dir_for("claude") / "alpha").exists()


def test_sync_prune_removes_stale_entries(
    populated: Path, dirs: AgentDirs
) -> None:
    agents = [("claude", dirs.dir_for("claude"))]
    manager.install(populated, agents, ["alpha", "beta"])
    importer.remove_skill(populated, "beta")

    result = manager.sync(populated, agents, prune=True)

    assert result.stale == ()
    assert ("claude", "beta") in result.uninstalled
    assert not (dirs.dir_for("claude") / "beta").exists()
    assert "beta" not in load_manifest(populated)
    assert (dirs.dir_for("claude") / "alpha").exists()


def test_sync_needs_nothing_when_only_stale_entries(
    populated: Path, dirs: AgentDirs
) -> None:
    agents = [("claude", dirs.dir_for("claude"))]
    manager.install(populated, agents, ["alpha"])
    importer.remove_skill(populated, "alpha")

    result = manager.sync(populated, agents)

    assert result.snapshot is None
    assert result.stale == (("claude", "alpha"),)


def test_sync_leaves_foreign_entries_alone(
    populated: Path, dirs: AgentDirs
) -> None:
    agents = [("claude", dirs.dir_for("claude"))]
    foreign = dirs.dir_for("claude") / "handmade"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("---\nname: handmade\n---\nx")

    manager.sync(populated, agents)

    assert foreign.exists()


def test_list_installed_reports_managed_foreign_and_broken(
    populated: Path, dirs: AgentDirs
) -> None:
    agents = [("claude", dirs.dir_for("claude"))]
    manager.install(populated, agents, ["alpha"])
    broken = dirs.dir_for("claude") / "dangling"
    broken.symlink_to(str(populated / "gone"))
    foreign = dirs.dir_for("claude") / "handmade"
    foreign.mkdir()
    (foreign / "SKILL.md").write_text("---\nname: handmade\n---\nx")

    report = manager.list_installed(populated, agents)

    claude = report["claude"]
    by_name = {e["name"]: e for e in claude["entries"]}
    assert by_name["alpha"]["managed"] is True
    assert by_name["handmade"]["managed"] is False
    assert by_name["dangling"]["healthy"] is False
    assert "dangling" in claude["broken_symlinks"]
    assert "handmade" in claude["foreign"]


def test_list_installed_survives_unreadable_agent_dir(
    populated: Path, dirs: AgentDirs
) -> None:
    agents = [
        ("claude", dirs.dir_for("claude")),
        ("zcode", dirs.dir_for("zcode")),
    ]
    manager.install(populated, [("claude", dirs.dir_for("claude"))], ["alpha"])
    bad = dirs.dir_for("zcode")
    bad.mkdir(parents=True)
    (bad / "something").mkdir()
    bad.chmod(0)
    try:
        report = manager.list_installed(populated, agents)
    finally:
        bad.chmod(0o755)
    assert "error" in report["zcode"]
    assert [e["name"] for e in report["claude"]["entries"]] == ["alpha"]


def test_list_installed_marks_symlink_loops_broken(
    populated: Path, dirs: AgentDirs
) -> None:
    agents = [("claude", dirs.dir_for("claude"))]
    manager.install(populated, agents, ["alpha"])
    loop_a = dirs.dir_for("claude") / "loop-a"
    loop_b = dirs.dir_for("claude") / "loop-b"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)

    report = manager.list_installed(populated, agents)

    # The loop can never be resolved to a healthy skill; the inventory must
    # still complete and flag it rather than failing the whole agent.
    claude = report["claude"]
    by_name = {e["name"]: e for e in claude["entries"]}
    assert by_name["loop-a"]["healthy"] is False
    assert by_name["loop-b"]["healthy"] is False
    assert "loop-a" in claude["broken_symlinks"]
    assert by_name["alpha"]["healthy"] is True


def test_unknown_agent_is_rejected(dirs: AgentDirs) -> None:
    with pytest.raises(UnknownAgentError):
        dirs.get("no-such-agent")


def test_agent_aliases_resolve(dirs: AgentDirs) -> None:
    assert dirs.get("glm").agent == "zcode"
    assert dirs.get("cursor").agent == "cursor-agent"
    assert dirs.get("deepseek-harness").agent == "deepseek"
    assert dirs.get("minimax").agent == "mcode"
    assert dirs.get("stepfun").agent == "step"


def test_registry_covers_required_fleet_dirs(home: Path) -> None:
    expected = {
        "claude": home / ".claude" / "skills",
        "codex": home / ".codex" / "skills",
        "cursor-agent": home / ".cursor" / "skills",
        "grok": home / ".grok" / "skills",
        "kimi": home / ".kimi-code" / "skills",
        "mcode": home / ".minimax" / "skills",
        "step": home / ".stepcode" / "skills",
        "zcode": home / ".zcode" / "skills",
        "opencode": home / ".config" / "opencode" / "skills",
        "shared": home / ".agents" / "skills",
    }
    for agent, directory in expected.items():
        assert dirs_agent_dir(agent) == directory


def dirs_agent_dir(agent: str) -> Path:
    from src.skills_hub.adapters import AGENT_DIRS

    del AGENT_DIRS  # use a fresh registry bound to the patched HOME instead
    from src.skills_hub.adapters import AgentDirs

    return AgentDirs().dir_for(agent)


# ---------------------------------------------------------------------------
# Regression: uninstall/sync must refuse a git worktree (same as install)
# ---------------------------------------------------------------------------


def test_uninstall_refuses_git_worktree(repo: Path, dirs: AgentDirs, home: Path) -> None:
    make_skill(repo, "alpha")
    target = home / "projects" / "repo" / ".claude" / "skills"
    target.mkdir(parents=True)
    (target.parents[1] / ".git").mkdir()
    # Track the skill as if Conductor installed it, then try to uninstall.
    from src.skills_hub.core import load_manifest, write_manifest

    manifest = load_manifest(repo)
    manifest.setdefault("alpha", {"agents": []})["agents"].append("claude")
    write_manifest(repo, manifest)

    with pytest.raises(SkillsError, match="git worktree"):
        manager.uninstall(repo, [("claude", target)], ["alpha"])


def test_uninstall_preflights_every_target_before_removing_anything(
    repo: Path, dirs: AgentDirs, home: Path
) -> None:
    make_skill(repo, "alpha")
    first = dirs.dir_for("claude")
    manager.install(repo, [("claude", first)], ["alpha"])
    protected = home / "projects" / "repo" / ".claude" / "skills"
    protected.mkdir(parents=True)
    (protected.parents[1] / ".git").mkdir()

    with pytest.raises(SkillsError, match="git worktree"):
        manager.uninstall(repo, [("claude", first), ("zcode", protected)])

    assert (first / "alpha").is_symlink()  # nothing removed before the refusal
    assert "alpha" in load_manifest(repo)


def test_sync_refuses_git_worktree(repo: Path, dirs: AgentDirs, home: Path) -> None:
    make_skill(repo, "alpha")
    target = home / "projects" / "repo" / ".claude" / "skills"
    target.mkdir(parents=True)
    (target.parents[1] / ".git").mkdir()
    from src.skills_hub.core import load_manifest, write_manifest

    manifest = load_manifest(repo)
    # Tracked skill missing from the agent dir → sync would try to install/remove.
    manifest.setdefault("alpha", {"agents": []})["agents"].append("claude")
    write_manifest(repo, manifest)

    with pytest.raises(SkillsError, match="git worktree"):
        manager.sync(repo, [("claude", target)])


def test_copy_mode_does_not_follow_escaping_symlinks(
    populated: Path, dirs: AgentDirs, tmp_path: Path, home: Path
) -> None:
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE", encoding="utf-8")
    link = populated / "alpha" / "stolen"
    link.symlink_to(secret)

    manager.install(
        populated, [("claude", dirs.dir_for("claude"))], ["alpha"], mode="copy"
    )

    entry = dirs.dir_for("claude") / "alpha"
    assert (entry / "stolen").is_symlink()
    # The copy preserved the link itself; the secret's bytes were never read
    # into the installed tree.
    assert not (entry / "id_rsa").exists()
