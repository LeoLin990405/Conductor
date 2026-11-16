"""Force-update snapshots keep the whole tree, or refuse before replacing it."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from src.skills_hub.core import SkillsError
from src.skills_hub.importer import import_source
from src.skills_hub.manager import rollback
from src.skills_hub.snapshots import rollback_snapshot, snapshot_skill_content
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


def _imported(tmp_path: Path) -> tuple[Path, Path]:
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
    return seed, imported


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


def test_force_update_restores_large_hidden_symlink_and_mode(
    git_ok: None, tmp_path: Path
) -> None:
    """Local Git import, local edit, force update, rollback restores the tree."""
    seed, imported = _imported(tmp_path)
    _push_v2(seed)
    skill = imported / "alpha"
    skill_md = skill / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8") + "local edit\n",
        encoding="utf-8",
    )
    edited = skill_md.read_text(encoding="utf-8")
    large = skill / "large-local.txt"
    large.write_bytes(b"L" * 65537)
    os.chmod(large, 0o640)
    (skill / ".assets").mkdir()
    (skill / ".assets" / "local.txt").write_text("hidden local body", encoding="utf-8")
    (skill / "local-link").symlink_to("SKILL.md")

    updated = update_skill(imported, "alpha", force=True)

    assert "version two" in skill_md.read_text(encoding="utf-8")
    assert not large.exists()
    assert not (skill / ".assets").exists()
    assert not (skill / "local-link").exists()
    restored = rollback(imported, updated["snapshot_id"])

    assert skill_md.read_text(encoding="utf-8") == edited
    assert large.read_bytes() == b"L" * 65537
    assert stat.S_IMODE(large.stat().st_mode) == 0o640
    assert (skill / ".assets" / "local.txt").read_text(encoding="utf-8") == (
        "hidden local body"
    )
    assert (skill / "local-link").is_symlink()
    assert os.readlink(skill / "local-link") == "SKILL.md"
    assert "large-local.txt" in restored["restored"]["alpha"]


def test_force_update_refuses_an_oversized_tree_before_replace(
    git_ok: None, tmp_path: Path
) -> None:
    seed, imported = _imported(tmp_path)
    _push_v2(seed)
    skill = imported / "alpha"
    skill_md = skill / "SKILL.md"
    before = skill_md.read_text(encoding="utf-8")
    big = skill / "big.bin"
    fd = os.open(big, os.O_CREAT | os.O_WRONLY, 0o644)
    os.truncate(fd, 200 * 1024 * 1024 + 1)
    os.close(fd)

    with pytest.raises(SkillsError, match="200 MB"):
        update_skill(imported, "alpha", force=True)

    assert skill_md.read_text(encoding="utf-8") == before
    assert "version two" not in before
    assert big.stat().st_size == 200 * 1024 * 1024 + 1


def test_force_update_refuses_an_unreadable_tree_before_replace(
    git_ok: None, tmp_path: Path
) -> None:
    seed, imported = _imported(tmp_path)
    _push_v2(seed)
    skill = imported / "alpha"
    skill_md = skill / "SKILL.md"
    before = skill_md.read_text(encoding="utf-8")
    blocked = skill / "locked"
    blocked.mkdir()
    (blocked / "note.txt").write_text("secret", encoding="utf-8")
    os.chmod(blocked, 0o000)
    try:
        with pytest.raises(SkillsError, match="refusing to replace"):
            update_skill(imported, "alpha", force=True)
    finally:
        os.chmod(blocked, 0o700)

    assert skill_md.read_text(encoding="utf-8") == before
    assert "version two" not in before
    assert (blocked / "note.txt").read_text(encoding="utf-8") == "secret"


def test_skill_rollback_does_not_write_through_an_intermediate_symlink(
    tmp_path: Path,
) -> None:
    """``link/new`` is missing and ``link`` points outside. Nothing is created there."""
    repo = tmp_path / "central"
    skill = repo / "alpha"
    (skill / "link" / "new").mkdir(parents=True)
    (skill / "SKILL.md").write_text("body", encoding="utf-8")
    (skill / "link" / "new" / "file.txt").write_text(
        "snapshot content", encoding="utf-8"
    )
    snapshot_id = snapshot_skill_content(repo, "alpha")
    shutil.rmtree(skill / "link")
    outside = tmp_path / "outside"
    outside.mkdir()
    (skill / "link").symlink_to(outside, target_is_directory=True)

    restored = rollback_snapshot(repo, snapshot_id)

    assert not (outside / "new" / "file.txt").exists()
    assert not (outside / "new").exists()
    assert "link/new/file.txt" in restored["refused"]
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "body"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _plant(path: Path, kind: str, label: str) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
    if kind == "file":
        path.write_text(label, encoding="utf-8")
    elif kind == "dir":
        path.mkdir()
        (path / f"{label}.txt").write_text(label, encoding="utf-8")
    else:
        path.symlink_to("SKILL.md")


def _kind(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "dir"
    return "file"


def test_force_update_round_trips_permission_bits_only(
    git_ok: None, tmp_path: Path
) -> None:
    """A chmod that does not change the content hash is still snapshotted."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(None, "init", "-b", "main", str(seed))
    _commit(
        seed,
        "one",
        {
            "alpha/SKILL.md": "---\nname: alpha\ndescription: alpha\n---\n\nversion one\n",
            "alpha/notes/keep.txt": "keep",
        },
    )
    bare = tmp_path / "upstream.git"
    _git(None, "init", "--bare", "-b", "main", str(bare))
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-u", "origin", "main")
    imported = tmp_path / "central"
    import_source(imported, str(seed))
    skill = imported / "alpha"
    nested = skill / "notes"
    os.chmod(skill, 0o700)
    os.chmod(skill / "SKILL.md", 0o600)
    os.chmod(nested, 0o700)
    _push_v2(seed)

    updated = update_skill(imported, "alpha", force=True)

    assert "snapshot_id" in updated
    assert "version two" in (skill / "SKILL.md").read_text(encoding="utf-8")
    assert _mode(skill) != 0o700
    assert _mode(skill / "SKILL.md") != 0o600
    assert _mode(nested) != 0o700
    rollback(imported, updated["snapshot_id"])

    assert _mode(skill) == 0o700
    assert _mode(skill / "SKILL.md") == 0o600
    assert nested.is_dir()
    assert _mode(nested) == 0o700
    assert (nested / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert "version two" not in (skill / "SKILL.md").read_text(encoding="utf-8")


def test_force_update_round_trips_an_empty_directory_only(
    git_ok: None, tmp_path: Path
) -> None:
    """An empty directory does not change the content hash and still comes back."""
    seed, imported = _imported(tmp_path)
    skill = imported / "alpha"
    empty = skill / "local-empty"
    empty.mkdir()
    os.chmod(empty, 0o700)
    _push_v2(seed)

    updated = update_skill(imported, "alpha", force=True)

    assert "snapshot_id" in updated
    assert not empty.exists()
    assert "version two" in (skill / "SKILL.md").read_text(encoding="utf-8")
    rollback(imported, updated["snapshot_id"])

    assert empty.is_dir()
    assert not any(empty.iterdir())
    assert _mode(empty) == 0o700
    assert "version two" not in (skill / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("file", "symlink"),
        ("symlink", "dir"),
        ("dir", "file"),
        ("file", "dir"),
        ("dir", "symlink"),
    ],
)
def test_force_update_restores_entry_type_changes(
    git_ok: None, tmp_path: Path, old: str, new: str
) -> None:
    """Upstream type changes round-trip through a real Git import and rollback."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(None, "init", "-b", "main", str(seed))
    source = seed / "alpha"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: alpha\n---\n\nversion one\n",
        encoding="utf-8",
    )
    _plant(source / "switch", old, "old")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "one")
    bare = tmp_path / "upstream.git"
    _git(None, "init", "--bare", "-b", "main", str(bare))
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-u", "origin", "main")
    imported = tmp_path / "central"
    import_source(imported, str(seed))
    _plant(source / "switch", new, "new")
    (source / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: alpha\n---\n\nversion two\n",
        encoding="utf-8",
    )
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "two")
    _git(seed, "push", "origin", "main")

    updated = update_skill(imported, "alpha", force=True)

    assert "snapshot_id" in updated
    skill = imported / "alpha"
    assert _kind(skill / "switch") == new
    rollback(imported, updated["snapshot_id"])

    restored = skill / "switch"
    assert _kind(restored) == old
    if old == "file":
        assert restored.read_text(encoding="utf-8") == "old"
    elif old == "symlink":
        assert os.readlink(restored) == "SKILL.md"
    else:
        assert (restored / "old.txt").read_text(encoding="utf-8") == "old"
    refused = rollback(imported, updated["snapshot_id"]).get("refused") or []
    assert "switch" not in refused
    assert _kind(restored) == old


def test_rollback_replaces_a_live_readonly_directory_with_the_file(
    tmp_path: Path,
) -> None:
    """A 0555 directory left on disk is removed without following links."""
    repo = tmp_path / "central"
    skill = repo / "alpha"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("body", encoding="utf-8")
    switch = skill / "switch"
    switch.write_text("old bytes", encoding="utf-8")
    snapshot_id = snapshot_skill_content(repo, "alpha")
    switch.unlink()
    switch.mkdir()
    (switch / "new.txt").write_text("new bytes", encoding="utf-8")
    nested = switch / "nested"
    nested.mkdir()
    (nested / "inner.txt").write_text("inner", encoding="utf-8")
    os.chmod(nested, 0o555)
    os.chmod(switch, 0o555)
    try:
        restored = rollback_snapshot(repo, snapshot_id)
    finally:
        if nested.exists():
            os.chmod(nested, 0o755)
        if switch.is_dir():
            os.chmod(switch, 0o755)

    assert switch.is_file()
    assert not switch.is_symlink()
    assert switch.read_text(encoding="utf-8") == "old bytes"
    assert "switch" in restored["restored"]["alpha"]
    assert "switch" not in (restored.get("refused") or [])


def test_rollback_restores_a_readonly_directory_after_its_child(tmp_path: Path) -> None:
    """A 0555 directory is writable while its child is written, then mode returns."""
    repo = tmp_path / "central"
    skill = repo / "alpha"
    child = skill / "readonly"
    child.mkdir(parents=True)
    (skill / "SKILL.md").write_text("body", encoding="utf-8")
    (child / "local.txt").write_text("saved bytes", encoding="utf-8")
    os.chmod(child, 0o555)
    snapshot_id = snapshot_skill_content(repo, "alpha")
    os.chmod(child, 0o755)
    shutil.rmtree(child)

    restored = rollback_snapshot(repo, snapshot_id)

    assert (child / "local.txt").read_text(encoding="utf-8") == "saved bytes"
    assert _mode(child) == 0o555
    assert "readonly/local.txt" in restored["restored"]["alpha"]
    assert "readonly/local.txt" not in restored.get("refused", [])
    os.chmod(child, 0o755)


def test_skill_rollback_keeps_files_inside_a_directory_it_still_records(
    tmp_path: Path,
) -> None:
    """A directory that stays a directory keeps children the snapshot did not name."""
    repo = tmp_path / "central"
    skill = repo / "alpha"
    keep = skill / "keep"
    keep.mkdir(parents=True)
    (skill / "SKILL.md").write_text("body", encoding="utf-8")
    (keep / "old.txt").write_text("old", encoding="utf-8")
    snapshot_id = snapshot_skill_content(repo, "alpha")
    (keep / "extra.txt").write_text("user", encoding="utf-8")
    (keep / "old.txt").write_text("changed", encoding="utf-8")

    rollback_snapshot(repo, snapshot_id)

    assert (keep / "old.txt").read_text(encoding="utf-8") == "old"
    assert (keep / "extra.txt").read_text(encoding="utf-8") == "user"
