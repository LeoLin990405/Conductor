"""skills_hub.updates — local fetch, fast-forward, and a filtered backup."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src.skills_hub.core import SkillsError, load_manifest
from src.skills_hub.presets import get_preset, save_preset
from src.skills_hub.updates import (
    MAX_BACKUP_BYTES,
    backup_repo,
    check_updates,
    excluded_from_backup,
    update_skill,
)


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


def _skill_repo(path: Path, body: str) -> None:
    path.mkdir(parents=True)
    _git(None, "init", "-b", "main", str(path))
    _commit(
        path,
        "init",
        {"SKILL.md": f"---\nname: demo\ndescription: demo\n---\n\n{body}\n"},
    )


@pytest.fixture
def git_ok() -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")


def test_check_updates_and_fast_forward(
    git_ok: None, repo: Path, tmp_path: Path
) -> None:
    origin = tmp_path / "origin"
    _skill_repo(origin, "v1")
    skill = repo / "demo"
    _git(None, "clone", str(origin), str(skill))

    assert check_updates(repo) == [{"name": "demo", "behind": 0}]
    manifest = load_manifest(repo)
    assert Path(manifest["demo"]["upstream"]) == origin
    assert manifest["demo"]["commit"]

    _commit(
        origin, "v2", {"SKILL.md": "---\nname: demo\ndescription: demo\n---\n\nv2\n"}
    )
    assert check_updates(repo) == [{"name": "demo", "behind": 1}]

    updated = update_skill(repo, "demo")
    assert "v2" in (skill / "SKILL.md").read_text(encoding="utf-8")
    assert updated["commit"] == load_manifest(repo)["demo"]["commit"]
    assert check_updates(repo) == [{"name": "demo", "behind": 0}]


def test_update_refuses_dirty_worktree(
    git_ok: None, repo: Path, tmp_path: Path
) -> None:
    origin = tmp_path / "origin"
    _skill_repo(origin, "v1")
    skill = repo / "demo"
    _git(None, "clone", str(origin), str(skill))
    _commit(
        origin, "v2", {"SKILL.md": "---\nname: demo\ndescription: demo\n---\n\nv2\n"}
    )
    (skill / "SKILL.md").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(SkillsError, match="dirty"):
        update_skill(repo, "demo")

    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "dirty\n"


def test_imported_skill_without_git_dir_still_reports_local_upstream(
    git_ok: None, tmp_path: Path
) -> None:
    """Import drops ``.git``. A local bare upstream in the manifest is not skipped."""
    from src.skills_hub.core import load_manifest
    from src.skills_hub.importer import import_source

    seed = tmp_path / "seed"
    bare = tmp_path / "upstream.git"
    seed.mkdir()
    _git(None, "init", "-b", "main", str(seed))
    _commit(
        seed,
        "one",
        {"demo/SKILL.md": "---\nname: demo\ndescription: demo\n---\n\nversion-one\n"},
    )
    _git(None, "init", "--bare", "-b", "main", str(bare))
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-u", "origin", "main")
    first = subprocess.run(
        ["git", "-C", str(seed), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    ).stdout.strip()

    imported = tmp_path / "central"
    import_source(imported, str(seed))
    assert not (imported / "demo" / ".git").exists()
    recorded = load_manifest(imported)["demo"]
    assert Path(recorded["upstream"]) == bare
    assert recorded["commit"] == first

    _commit(
        seed,
        "v2",
        {"demo/SKILL.md": "---\nname: demo\ndescription: demo\n---\n\nversion-two\n"},
    )
    _git(seed, "push", "origin", "main")

    report = check_updates(imported)
    assert report == [{"name": "demo", "behind": 1}]


def test_imported_copy_updates_from_local_bare_without_parent_walk(
    git_ok: None, tmp_path: Path
) -> None:
    """A copy with no ``.git`` updates from its manifest upstream only.

    The central repo sits inside another checkout whose origin is not the
    skill. Update must not fetch that parent or walk above the import.
    """
    from src.skills_hub.importer import import_source

    outer = tmp_path / "outer"
    outer.mkdir()
    _git(None, "init", "-b", "main", str(outer))
    _git(outer, "remote", "add", "origin", str(tmp_path / "missing-parent.git"))
    _commit(outer, "outer", {"NOT_SKILL.txt": "do-not-copy\n"})
    outer_head = subprocess.run(
        ["git", "-C", str(outer), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    ).stdout.strip()

    bare = tmp_path / "upstream.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(None, "init", "-b", "main", str(seed))
    _commit(
        seed,
        "one",
        {"alpha/SKILL.md": "---\nname: alpha\ndescription: alpha\n---\n\nversion one\n"},
    )
    _git(None, "init", "--bare", "-b", "main", str(bare))
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-u", "origin", "main")

    imported = outer / "central"
    import_source(imported, str(seed))
    assert not (imported / "alpha" / ".git").exists()
    _commit(
        seed,
        "two",
        {"alpha/SKILL.md": "---\nname: alpha\ndescription: alpha\n---\n\nversion two\n"},
    )
    _git(seed, "push", "origin", "main")
    assert check_updates(imported) == [{"name": "alpha", "behind": 1}]

    updated = update_skill(imported, "alpha")

    body = (imported / "alpha" / "SKILL.md").read_text(encoding="utf-8")
    assert "version two" in body
    assert "version one" not in body
    assert not (imported / "alpha" / ".git").exists()
    assert not (imported / "alpha" / "NOT_SKILL.txt").exists()
    assert not (imported / "NOT_SKILL.txt").exists()
    assert os.path.realpath(updated["upstream"]) == os.path.realpath(bare)
    assert check_updates(imported) == [{"name": "alpha", "behind": 0}]
    assert not (outer / ".git" / "FETCH_HEAD").exists()
    still = subprocess.run(
        ["git", "-C", str(outer), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    ).stdout.strip()
    assert still == outer_head


def test_backup_pushes_to_existing_local_bare_without_moving_head(
    git_ok: None, repo: Path, tmp_path: Path
) -> None:
    kept = repo / "alpha"
    kept.mkdir(parents=True)
    (kept / "SKILL.md").write_text(
        "---\nname: alpha\n---\n\nbackup-body\n", encoding="utf-8"
    )
    (repo / "secret.env").write_text("synthetic-secret\n", encoding="utf-8")

    seed = tmp_path / "seed"
    bare = tmp_path / "upstream.git"
    _skill_repo(seed, "already-there")
    _git(None, "init", "--bare", "-b", "main", str(bare))
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-u", "origin", "main")
    head_before = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    ).stdout.strip()
    sym_before = subprocess.run(
        ["git", "--git-dir", str(bare), "symbolic-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    ).stdout.strip()

    result = backup_repo(repo, str(bare))

    assert result["pushed"] is True
    head_after = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    ).stdout.strip()
    sym_after = subprocess.run(
        ["git", "--git-dir", str(bare), "symbolic-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    ).stdout.strip()
    backup_ref = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "refs/heads/skills-backup"],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    ).stdout.strip()
    assert head_after == head_before
    assert sym_after == sym_before == "refs/heads/main"
    assert backup_ref != head_after
    restored = tmp_path / "restored"
    _git(None, "clone", "--branch", "skills-backup", str(bare), str(restored))
    text = (restored / "alpha" / "SKILL.md").read_text(encoding="utf-8")
    assert "backup-body" in text
    names = [path.name for path in restored.rglob("*")]
    assert "secret.env" not in names
    assert "synthetic-secret" not in text


def test_backup_points_empty_bare_head_at_backup_branch(
    git_ok: None, repo: Path, tmp_path: Path
) -> None:
    skill = repo / "alpha"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: alpha\n---\n\nonly\n", encoding="utf-8")
    bare = tmp_path / "empty.git"
    _git(None, "init", "--bare", "-b", "main", str(bare))

    result = backup_repo(repo, str(bare))

    assert result["pushed"] is True
    sym = subprocess.run(
        ["git", "--git-dir", str(bare), "symbolic-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    ).stdout.strip()
    assert sym == "refs/heads/skills-backup"


def test_network_upstream_is_not_fetched(
    git_ok: None, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network backup URL is pushed with git argv. It is not fetched or cloned."""
    skill = repo / "demo"
    _skill_repo(skill, "local")
    _git(skill, "remote", "add", "origin", "https://github.com/example/nope.git")
    assert check_updates(repo) == []

    import src.skills_hub.updates as updates

    real = updates._git
    pushed: list[tuple[str, ...]] = []

    def wrapped(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args and args[0] in {"fetch", "clone"}:
            raise AssertionError("backup must not fetch or clone")
        if args and args[0] == "push":
            pushed.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        return real(cwd, *args)

    monkeypatch.setattr(updates, "_git", wrapped)
    remote = "https://synthetic-user:synthetic-userinfo@github.com/example/nope.git"
    result = backup_repo(repo, remote)

    assert result["pushed"] is True
    assert pushed
    assert pushed[0][-1] == "+HEAD:refs/heads/skills-backup"
    host = str(result["remote"]).split("://", 1)[-1].split("/", 1)[0]
    push_host = pushed[0][1].split("://", 1)[-1].split("/", 1)[0]
    assert host == "github.com"
    assert push_host == "github.com"
    assert "@" not in host and "@" not in push_host
    blob = json.dumps(result) + " ".join(pushed[0])
    assert "synthetic-userinfo" not in blob


def test_backup_skips_secrets_and_sqlite(
    git_ok: None, repo: Path, tmp_path: Path
) -> None:
    kept = repo / "widget"
    kept.mkdir(parents=True)
    (kept / "SKILL.md").write_text("---\nname: widget\n---\n\nkeep\n", encoding="utf-8")
    (repo / "secret.env").write_text("nope", encoding="utf-8")
    (repo / ".env").write_text("nope", encoding="utf-8")
    (repo / "credential.txt").write_text("nope", encoding="utf-8")
    (repo / "data.sqlite").write_bytes(b"db")
    nested = repo / "nested"
    nested.mkdir()
    (nested / "cache.sqlite3").write_bytes(b"db")
    (nested / "token.txt").write_text("nope", encoding="utf-8")
    (nested / "notes.md").write_text("keep", encoding="utf-8")

    dest = tmp_path / "backup"
    result = backup_repo(repo, str(dest))

    assert Path(result["dest"]) == dest
    assert (dest / "widget" / "SKILL.md").is_file()
    assert (dest / "nested" / "notes.md").is_file()
    assert not (dest / "secret.env").exists()
    assert not (dest / ".env").exists()
    assert not (dest / "credential.txt").exists()
    assert not (dest / "data.sqlite").exists()
    assert not (dest / "nested" / "cache.sqlite3").exists()
    assert not (dest / "nested" / "token.txt").exists()
    names = [path.name for path in dest.rglob("*")]
    assert "secret.env" not in names
    assert not any(name.endswith((".sqlite", ".sqlite3")) for name in names)
    assert (dest / ".git").is_dir()


def test_size_predicate_uses_fake_stat() -> None:
    class Stat:
        def __init__(self, size: int) -> None:
            self.st_size = size

    assert excluded_from_backup(Path("blob.bin"), stat=Stat(MAX_BACKUP_BYTES + 1))
    assert not excluded_from_backup(Path("blob.bin"), stat=Stat(MAX_BACKUP_BYTES))
    assert excluded_from_backup(Path("secret.env"), stat=Stat(1))
    assert excluded_from_backup(Path("db.sqlite"), stat=Stat(1))
    assert excluded_from_backup(Path("db.sqlite3"), stat=Stat(1))
    assert not excluded_from_backup(Path("SKILL.md"), stat=Stat(10))


def test_preset_mcp_is_optional_and_old_json_loads(repo: Path) -> None:
    skill = repo / "alpha"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: alpha\n---\n\nbody\n", encoding="utf-8")
    legacy = repo / "presets" / "core.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"skills": ["alpha"], "description": "old"}\n', encoding="utf-8")

    loaded = get_preset(repo, "core")
    assert loaded is not None
    assert loaded.skills == ("alpha",)
    assert loaded.mcp == ()
    assert "mcp" not in loaded.to_dict()

    saved = save_preset(repo, "core", ["alpha"], description="old", mcp=["memory"])
    assert saved.mcp == ("memory",)
    raw = json.loads(legacy.read_text(encoding="utf-8"))
    assert raw["skills"] == ["alpha"]
    assert raw["mcp"] == ["memory"]

    plain = save_preset(repo, "other", ["alpha"])
    other = json.loads((repo / "presets" / "other.json").read_text(encoding="utf-8"))
    assert "mcp" not in other
    assert plain.skills == ("alpha",)
