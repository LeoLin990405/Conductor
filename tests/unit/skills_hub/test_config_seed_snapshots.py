"""skills_hub.config, .seed and .snapshots — resolution, seeding, history."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.skills_hub import seed, snapshots
from src.skills_hub.config import SkillsConfigError, load_config


# --- config -----------------------------------------------------------------


def test_default_repo_lives_under_home(home: Path) -> None:
    config = load_config(home=home)

    assert config.repo == home / ".conductor" / "skills"
    assert config.default_link_mode == "symlink"


def test_env_repo_overrides_config_file(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (home / ".config" / "conductor").mkdir(parents=True)
    (home / ".config" / "conductor" / "config.json").write_text(
        json.dumps({"repo": "/from/file"})
    )
    monkeypatch.setenv("CONDUCTOR_SKILLS_REPO", "/from/env")

    config = load_config(home=home)

    assert str(config.repo) == "/from/env"


def test_repo_from_config_file(home: Path) -> None:
    config_dir = home / ".config" / "conductor"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"repo": str(home / "custom-skills"), "default_link_mode": "copy"})
    )

    config = load_config(home=home)

    assert config.repo == home / "custom-skills"
    assert config.default_link_mode == "copy"


def test_agent_dir_overrides(home: Path) -> None:
    config_dir = home / ".config" / "conductor"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"agents": {"claude": str(home / "elsewhere")}})
    )

    config = load_config(home=home)

    assert config.agent_dir("claude") == home / "elsewhere"
    assert config.agent_dir("zcode") == home / ".zcode" / "skills"


def test_bad_link_mode_is_rejected(home: Path) -> None:
    config_dir = home / ".config" / "conductor"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(json.dumps({"default_link_mode": "hardlink"}))

    with pytest.raises(SkillsConfigError, match="link mode"):
        load_config(home=home)


def test_relative_repo_resolves_against_config_dir(home: Path) -> None:
    config_dir = home / ".config" / "conductor"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(json.dumps({"repo": "../../skills-here"}))

    config = load_config(home=home)

    assert config.repo == (home / "skills-here").resolve()


def test_relative_agent_dirs_resolve_against_home(home: Path) -> None:
    config_dir = home / ".config" / "conductor"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"agents": {"claude": "rel-skills"}})
    )

    config = load_config(home=home)

    assert config.agent_dir("claude") == home / "rel-skills"


def test_relative_agent_dirs_resolve_against_process_home_without_home(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CLI and the dashboard call load_config() with no home; a relative
    # override must anchor at HOME then, never at the caller's cwd — the
    # delete paths in uninstall/sync would otherwise resolve against wherever
    # the process happens to run.
    config_dir = home / ".config" / "conductor"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"agents": {"claude": "rel-skills"}})
    )
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.agent_dir("claude") == home / "rel-skills"


def test_relative_agent_dir_escaping_home_is_rejected(home: Path) -> None:
    # A relative override is anchored at the home; a '..' component must not
    # point uninstall/prune/rollback outside it. allowed_dirs compares the
    # same value, so it would not catch the escape either.
    config_dir = home / ".config" / "conductor"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"agents": {"claude": "../../escape"}})
    )

    with pytest.raises(SkillsConfigError, match="outside the home"):
        load_config(home=home)


def test_relative_agent_dir_dot_components_are_normalized(home: Path) -> None:
    config_dir = home / ".config" / "conductor"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"agents": {"claude": "sub/../rel-skills"}})
    )

    config = load_config(home=home)

    assert config.agent_dir("claude") == home / "rel-skills"


# --- seed -------------------------------------------------------------------


def test_seed_creates_fleet_rules(repo: Path) -> None:
    result = seed.seed_fleet_rules(repo)

    assert result.seeded is True
    body = (repo / "fleet-rules" / "SKILL.md").read_text()
    for phrase in ("No self-review", "Never touch secrets", "Worktree-only edits"):
        assert phrase in body


def test_seed_is_idempotent(repo: Path) -> None:
    seed.seed_fleet_rules(repo)

    again = seed.seed_fleet_rules(repo)

    assert again.seeded is False
    assert again.reason == "already seeded"


def test_seed_never_overwrites_foreign_skill(repo: Path) -> None:
    skill = repo / "fleet-rules"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: fleet-rules\n---\ncustom\n")

    result = seed.seed_fleet_rules(repo)

    assert result.seeded is False
    assert "custom" in (skill / "SKILL.md").read_text()


def test_seed_force_overwrites(repo: Path) -> None:
    skill = repo / "fleet-rules"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: fleet-rules\n---\ncustom\n")

    result = seed.seed_fleet_rules(repo, force=True)

    assert result.seeded is True
    assert "custom" not in (skill / "SKILL.md").read_text()


# --- snapshots --------------------------------------------------------------


def test_snapshots_list_newest_first(repo: Path, tmp_path: Path) -> None:
    directory = tmp_path / "agentdir"
    directory.mkdir()
    (directory / "one").mkdir()

    first = snapshots.create_snapshot(repo, [("claude", directory)])
    (directory / "two").mkdir()
    second = snapshots.create_snapshot(repo, [("claude", directory)])

    listed = [s["id"] for s in snapshots.list_snapshots(repo)]

    # Same-second collisions append -1, -2 ... and must still sort newest first.
    assert listed.index(second) < listed.index(first)


def test_rollback_unknown_snapshot_raises(repo: Path) -> None:
    with pytest.raises(snapshots.SkillsError, match="not found"):
        snapshots.rollback_snapshot(repo, "does-not-exist")


def test_rollback_restores_foreign_dir_from_repo(repo: Path, tmp_path: Path) -> None:
    # A copy-mode entry whose skill still exists in the repo is restored by
    # copying it back; the snapshot only records that a directory was there.
    skill = repo / "alpha"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: alpha\n---\nbody")
    directory = tmp_path / "agentdir" / "alpha"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("---\nname: alpha\n---\nold")
    snapshot_id = snapshots.create_snapshot(repo, [("claude", directory.parent)])

    import shutil

    shutil.rmtree(directory)
    result = snapshots.rollback_snapshot(repo, snapshot_id)

    assert result["restored"] == {"claude": ["alpha"]}
    assert (directory / "SKILL.md").read_text().endswith("body")


def test_rollback_tombstones_missing_repo_skill(repo: Path, tmp_path: Path) -> None:
    directory = tmp_path / "agentdir" / "gone-skill"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("---\nname: gone-skill\n---\nx")
    snapshot_id = snapshots.create_snapshot(repo, [("claude", directory.parent)])

    import shutil

    shutil.rmtree(directory)
    snapshots.rollback_snapshot(repo, snapshot_id)

    restored = directory.parent / "gone-skill" / "SKILL.md"
    assert restored.is_file()
    assert "no longer in the central repo" in restored.read_text()


def test_rollback_keeps_untracked_regular_files(
    repo: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file already in the agent dir must survive rollback (BLOCK finding)."""
    from src.skills_hub import manager
    from src.skills_hub.snapshots import create_snapshot, rollback_snapshot

    agent = home / ".claude" / "skills"
    agent.mkdir(parents=True)
    keep = agent / "notes.txt"
    keep.write_text("user data\n", encoding="utf-8")

    skill = repo / "alpha"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: alpha\n---\nbody\n", encoding="utf-8")

    stamp = create_snapshot(repo, [("claude", agent)])
    # Conductor installs (adds a symlink) then we roll back to the snapshot.
    manager.install(repo, [("claude", agent)], ["alpha"])
    assert (agent / "alpha").exists()

    result = rollback_snapshot(repo, stamp, skills_root=repo)

    assert keep.exists(), "rollback deleted a pre-existing regular file"
    assert keep.read_text(encoding="utf-8") == "user data\n"
    # Rollback never deletes an entry the snapshot did not record: the skill
    # installed after the snapshot is reported as kept, not removed.
    assert (agent / "alpha").exists()
    assert result["kept"] == {"claude": ["alpha"]}
    assert result["snapshot"] == stamp


def test_create_snapshot_requires_absolute_dirs(repo: Path) -> None:
    with pytest.raises(snapshots.SkillsError, match="absolute"):
        snapshots.create_snapshot(repo, [("claude", Path("relative/skills"))])


def _rewrite_snapshot_dir(repo: Path, snapshot_id: str, new_dir: object) -> None:
    path = snapshots.snapshot_dir(repo) / f"{snapshot_id}.json"
    body = json.loads(path.read_text())
    body["agents"]["claude"]["dir"] = new_dir
    path.write_text(json.dumps(body))


def test_rollback_refuses_snapshot_without_dir(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A snapshot missing "dir" must never fall back to the process cwd:
    # rollback deletes children of that directory.
    directory = tmp_path / "agentdir"
    directory.mkdir()
    snapshot_id = snapshots.create_snapshot(repo, [("claude", directory)])
    _rewrite_snapshot_dir(repo, snapshot_id, None)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "precious.txt").write_text("x")
    monkeypatch.chdir(cwd)

    with pytest.raises(snapshots.SkillsError, match="no directory"):
        snapshots.rollback_snapshot(repo, snapshot_id)

    assert (cwd / "precious.txt").exists()


def test_rollback_refuses_relative_dir(repo: Path, tmp_path: Path) -> None:
    directory = tmp_path / "agentdir"
    directory.mkdir()
    snapshot_id = snapshots.create_snapshot(repo, [("claude", directory)])
    _rewrite_snapshot_dir(repo, snapshot_id, "agentdir")

    with pytest.raises(snapshots.SkillsError, match="not absolute"):
        snapshots.rollback_snapshot(repo, snapshot_id)


def test_rollback_allowed_dirs_restricts_targets(repo: Path, tmp_path: Path) -> None:
    directory = tmp_path / "agentdir"
    directory.mkdir()
    (directory / "one").mkdir()
    snapshot_id = snapshots.create_snapshot(repo, [("claude", directory)])

    with pytest.raises(snapshots.SkillsError, match="not a known agent"):
        snapshots.rollback_snapshot(repo, snapshot_id, allowed_dirs={tmp_path / "other"})

    result = snapshots.rollback_snapshot(repo, snapshot_id, allowed_dirs={directory})
    assert result["snapshot"] == snapshot_id
    assert (directory / "one").is_dir()


def test_rollback_validates_all_dirs_before_touching_any(
    repo: Path, tmp_path: Path
) -> None:
    # Two agents: the first dir is fine, the second is a crafted relative
    # path. Nothing may be deleted — including in the valid directory —
    # before the refusal.
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    stamp = snapshots.create_snapshot(repo, [("claude", first), ("zcode", second)])
    path = snapshots.snapshot_dir(repo) / f"{stamp}.json"
    body = json.loads(path.read_text())
    body["agents"]["zcode"]["dir"] = "../elsewhere"
    path.write_text(json.dumps(body))
    (first / "keep").mkdir()

    with pytest.raises(snapshots.SkillsError, match="not absolute"):
        snapshots.rollback_snapshot(repo, stamp)

    assert (first / "keep").exists()


def test_rollback_of_current_tree_is_a_noop(repo: Path, tmp_path: Path) -> None:
    # The invariant rollback lives by: restoring a snapshot of the state
    # still on disk must change nothing and report nothing.
    directory = tmp_path / "agentdir"
    directory.mkdir()
    (directory / "repo-skill").mkdir()
    (directory / "repo-skill" / "SKILL.md").write_text("---\nname: repo-skill\n---\n")
    handmade = directory / "handmade"
    handmade.mkdir()
    (handmade / "SKILL.md").write_text("UNIQUE-USER-CONTENT-EDITED")
    (directory / "loose.txt").write_text("loose bytes")
    snapshot_id = snapshots.create_snapshot(repo, [("claude", directory)])

    result = snapshots.rollback_snapshot(repo, snapshot_id)

    assert result["restored"] == {}
    assert (handmade / "SKILL.md").read_text() == "UNIQUE-USER-CONTENT-EDITED"
    assert (directory / "loose.txt").read_text() == "loose bytes"


def test_rollback_preserves_handmade_dir_missing_from_repo(
    repo: Path, tmp_path: Path
) -> None:
    # The reviewer's repro: a hand-installed directory that the central repo
    # never had must survive rollback with its bytes intact — not become the
    # tombstone placeholder.
    directory = tmp_path / "agentdir"
    handmade = directory / "handmade"
    handmade.mkdir(parents=True)
    (handmade / "SKILL.md").write_text("UNIQUE-USER-CONTENT-EDITED")
    snapshot_id = snapshots.create_snapshot(repo, [("claude", directory)])

    result = snapshots.rollback_snapshot(repo, snapshot_id)

    assert result["restored"] == {}
    assert (handmade / "SKILL.md").read_text() == "UNIQUE-USER-CONTENT-EDITED"


def test_rollback_preserves_loose_files(repo: Path, tmp_path: Path) -> None:
    # The reviewer's repro: rollback used to delete regular files in the
    # agent skill directory because _scan only recorded symlinks and dirs.
    directory = tmp_path / "agentdir"
    directory.mkdir()
    (directory / "loose.txt").write_text("loose bytes")
    snapshot_id = snapshots.create_snapshot(repo, [("claude", directory)])

    result = snapshots.rollback_snapshot(repo, snapshot_id)

    assert result["restored"] == {}
    assert (directory / "loose.txt").read_text() == "loose bytes"


def test_rollback_restores_deleted_loose_file(repo: Path, tmp_path: Path) -> None:
    # A file recorded at snapshot time comes back byte-for-byte when a later
    # change deleted it.
    directory = tmp_path / "agentdir"
    directory.mkdir()
    (directory / "loose.txt").write_text("loose bytes")
    snapshot_id = snapshots.create_snapshot(repo, [("claude", directory)])

    (directory / "loose.txt").unlink()
    result = snapshots.rollback_snapshot(repo, snapshot_id)

    assert result["restored"] == {"claude": ["loose.txt"]}
    assert (directory / "loose.txt").read_text() == "loose bytes"


def test_rollback_repoints_drifted_symlink(repo: Path, tmp_path: Path) -> None:
    # A symlink whose target changed after the snapshot is the one case
    # rollback repairs in place: the recorded state is complete and
    # re-pointing loses nothing.
    directory = tmp_path / "agentdir"
    target_a = tmp_path / "target-a"
    target_b = tmp_path / "target-b"
    target_a.mkdir()
    target_b.mkdir()
    link = directory / "linked"
    link.parent.mkdir(parents=True)
    link.symlink_to(target_a)
    snapshot_id = snapshots.create_snapshot(repo, [("claude", directory)])

    link.unlink()
    link.symlink_to(target_b)
    result = snapshots.rollback_snapshot(repo, snapshot_id)

    assert result["restored"] == {"claude": ["linked"]}
    assert os.readlink(link) == str(target_a)


def test_rollback_replaces_file_with_dir_only_when_missing(
    repo: Path, tmp_path: Path
) -> None:
    # A recorded file that a later change replaced with a directory is left
    # alone: the directory is real data the snapshot never held.
    directory = tmp_path / "agentdir"
    directory.mkdir()
    (directory / "slot").write_text("file bytes")
    snapshot_id = snapshots.create_snapshot(repo, [("claude", directory)])

    (directory / "slot").unlink()
    (directory / "slot").mkdir()
    result = snapshots.rollback_snapshot(repo, snapshot_id)

    assert result["restored"] == {}
    assert (directory / "slot").is_dir()


def test_rollback_keeps_and_reports_user_added_skills(
    repo: Path, tmp_path: Path
) -> None:
    # The reviewer's repro: a skill the user installed by hand after the
    # snapshot is not rollback's to delete. Rollback reports what it left
    # alone and leaves it byte-for-byte intact.
    directory = tmp_path / "agentdir"
    (directory / "repo-skill").mkdir(parents=True)
    (directory / "repo-skill" / "SKILL.md").write_text("---\nname: repo-skill\n---\n")
    snapshot_id = snapshots.create_snapshot(repo, [("claude", directory)])

    mine = directory / "mine"
    mine.mkdir()
    (mine / "SKILL.md").write_text("UNIQUE-USER-CONTENT")
    (directory / "notes.txt").write_text("loose bytes")

    result = snapshots.rollback_snapshot(repo, snapshot_id)

    assert result["kept"] == {"claude": ["mine", "notes.txt"]}
    assert result["restored"] == {}
    assert (mine / "SKILL.md").read_text() == "UNIQUE-USER-CONTENT"
    assert (directory / "notes.txt").read_text() == "loose bytes"


def test_rollback_restores_what_it_lost_and_keeps_what_it_never_knew(
    repo: Path, tmp_path: Path
) -> None:
    # Restoring and keeping are not in tension: the entry the snapshot
    # recorded and a later change removed comes back, while the one that
    # appeared afterwards stays where the user put it.
    directory = tmp_path / "agentdir"
    directory.mkdir()
    (directory / "alpha").symlink_to(repo / "alpha")
    (repo / "alpha").mkdir(parents=True)
    (repo / "alpha" / "SKILL.md").write_text("---\nname: alpha\n---\n")
    snapshot_id = snapshots.create_snapshot(repo, [("claude", directory)])

    (directory / "alpha").unlink()
    mine = directory / "mine"
    mine.mkdir()
    (mine / "SKILL.md").write_text("UNIQUE-USER-CONTENT")

    result = snapshots.rollback_snapshot(repo, snapshot_id)

    assert result["restored"] == {"claude": ["alpha"]}
    assert result["kept"] == {"claude": ["mine"]}
    assert (directory / "alpha").is_symlink()
    assert (mine / "SKILL.md").read_text() == "UNIQUE-USER-CONTENT"


def test_rollback_reports_nothing_kept_when_the_tree_is_unchanged(
    repo: Path, tmp_path: Path
) -> None:
    # A no-op rollback reports nothing in either map: there is nothing it
    # restored and nothing it had to leave behind.
    directory = tmp_path / "agentdir"
    directory.mkdir()
    (directory / "alpha").mkdir()
    (directory / "alpha" / "SKILL.md").write_text("---\nname: alpha\n---\n")
    snapshot_id = snapshots.create_snapshot(repo, [("claude", directory)])

    result = snapshots.rollback_snapshot(repo, snapshot_id)

    assert result == {"snapshot": snapshot_id, "restored": {}, "kept": {}}
