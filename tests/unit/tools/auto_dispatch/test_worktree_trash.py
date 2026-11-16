"""Gate worktree removal through the trash, and .DS_Store as dirty-tree noise."""

from __future__ import annotations

import asyncio
import errno
import os
import subprocess
import threading
from pathlib import Path

import pytest

import src.tools.auto_dispatch.gates as gates_module
import src.tools.auto_dispatch.worktree_trash as worktree_trash
from src.tools.auto_dispatch.gates import (
    GateRunner,
    GateSettings,
    GitWorktreeManager,
    git_status_paths,
)


def _env(home: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key != "HOME"
    }
    env.update(
        HOME=str(home),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return env


@pytest.fixture()
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    env = _env(home)
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )
    return done.stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / ".DS_Store").write_bytes(b"tracked")
    _git(repo, "add", "a.txt", ".DS_Store")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


class _PlainGit:
    """A runner that runs git directly and can make ``worktree remove`` fail."""

    def __init__(self, *, fail_remove: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail_remove = fail_remove

    async def __call__(self, argv: list[str], cwd: Path) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        if self.fail_remove and argv[:2] == ["worktree", "remove"]:
            # What the old 5 s timeout did: the worktree stays behind.
            return 124, "", "timed out"
        done = subprocess.run(
            ["git", *argv], cwd=str(cwd), capture_output=True, text=True
        )
        return done.returncode, done.stdout, done.stderr


def _listed(repo: Path) -> str:
    return _git(repo, "worktree", "list", "--porcelain")


@pytest.fixture()
def slow_delete(monkeypatch: pytest.MonkeyPatch):
    gate = threading.Event()
    real = worktree_trash.shutil.rmtree

    def _blocked(path, *args, **kwargs):
        gate.wait(10)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(worktree_trash.shutil, "rmtree", _blocked)
    yield gate
    gate.set()
    worktree_trash.wait_pending(10)


def test_remove_frees_path_at_once_and_add_reuses_it(
    tmp_path: Path, git_env, slow_delete
) -> None:
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    path = root / "key" / "gates" / "d1" / "line-0"
    runner = _PlainGit(fail_remove=True)
    manager = GitWorktreeManager(runner=runner, trash_root=root / ".conductor-trash")

    asyncio.run(manager.add_detached(repo, path, "HEAD"))
    (path / "node_modules").mkdir()
    (path / "node_modules" / "x.js").write_text("x", encoding="utf-8")
    asyncio.run(manager.remove(repo, path))

    # The delete is still blocked, yet the path and registration are gone.
    assert not path.exists()
    assert str(path.resolve()) not in _listed(repo)
    assert ["worktree", "prune"] in runner.calls
    assert ["worktree", "remove", "--force", str(path)] not in runner.calls
    trashed = list((root / ".conductor-trash").iterdir())
    assert len(trashed) == 1 and trashed[0].name.endswith("-line-0")

    _git(repo, "commit", "-q", "--allow-empty", "-m", "next")
    asyncio.run(manager.add_detached(repo, path, "HEAD"))
    assert (path / "a.txt").is_file()
    assert str(path.resolve()) in _listed(repo)

    slow_delete.set()
    assert worktree_trash.wait_pending(10)
    assert list((root / ".conductor-trash").iterdir()) == []


def test_rename_failure_falls_back_to_git_remove_with_long_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[list[str], float]] = []

    async def fake(argv, cwd, worktrees_root, *, timeout=5):
        seen.append((list(argv), timeout))
        return 0, "", ""

    monkeypatch.setattr(gates_module, "_isolated_git_mutation_exec", fake)

    def no_rename(src, dst):
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(worktree_trash.os, "rename", no_rename)
    path = tmp_path / "wt" / "line"
    path.mkdir(parents=True)
    asyncio.run(GitWorktreeManager().remove(tmp_path / "repo", path))
    assert seen == [(["worktree", "remove", "--force", str(path)], 120.0)]


def test_mutation_exec_passes_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    got: dict[str, object] = {}

    def fake_run(cwd, argv, **kwargs):
        got.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(gates_module, "run_isolated_git_mutation", fake_run)
    asyncio.run(gates_module._isolated_git_mutation_exec(["worktree", "prune"], tmp_path, tmp_path))
    assert got["timeout"] == 5
    asyncio.run(
        gates_module._isolated_git_mutation_exec(
            ["worktree", "prune"], tmp_path, tmp_path, timeout=120.0
        )
    )
    assert got["timeout"] == 120.0


def test_remove_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(argv, cwd):
        if argv[:2] == ["worktree", "list"]:
            return 0, "", ""
        raise RuntimeError("git exploded")

    def bad_rmtree(path, *args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(worktree_trash.shutil, "rmtree", bad_rmtree)
    path = tmp_path / "wt" / "line"
    path.mkdir(parents=True)
    manager = GitWorktreeManager(runner=boom, trash_root=tmp_path / "trash")
    asyncio.run(manager.remove(tmp_path, path))  # prune raises: swallowed
    assert not path.exists()
    assert worktree_trash.wait_pending(10)
    # Rename impossible and the git fallback raises too.
    other = tmp_path / "wt" / "other"
    other.mkdir()
    monkeypatch.setattr(worktree_trash, "move_to_trash", lambda p, r: None)
    asyncio.run(manager.remove(tmp_path, other))


def test_unreadable_worktree_list_keeps_the_tree(tmp_path: Path) -> None:
    async def no_list(argv, cwd):
        return 128, "", "fatal"

    path = tmp_path / "wt" / "line"
    path.mkdir(parents=True)
    manager = GitWorktreeManager(runner=no_list, trash_root=tmp_path / "trash")
    asyncio.run(manager.remove(tmp_path, path))
    # Lock state unknown: not renamed away, Git's own remove was tried.
    assert path.is_dir()


def test_move_to_trash_refuses_symlink_and_missing(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    assert worktree_trash.move_to_trash(link, tmp_path / "trash") is None
    assert worktree_trash.move_to_trash(tmp_path / "missing", tmp_path / "trash") is None
    assert target.is_dir()


def test_trash_dir_for_defaults_beside_worktree(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b"
    assert worktree_trash.trash_dir_for(path) == tmp_path / "a" / ".conductor-trash"
    assert worktree_trash.trash_dir_for(path, tmp_path / "t") == tmp_path / "t"


def test_sweep_trash_cleans_and_tolerates_missing(tmp_path: Path) -> None:
    assert worktree_trash.sweep_trash(tmp_path / "absent") == 0
    trash = tmp_path / "trash"
    (trash / "x-line" / "deep").mkdir(parents=True)
    (trash / "x-line" / "deep" / "f").write_text("f", encoding="utf-8")
    (trash / "stray").write_text("s", encoding="utf-8")
    assert worktree_trash.sweep_trash(trash) == 2
    assert list(trash.iterdir()) == []


def test_gate_runner_sweeps_trash_once_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    worktrees = tmp_path / "state" / "worktrees"
    leftover = worktrees / ".conductor-trash" / "abc-line-0"
    leftover.mkdir(parents=True)
    (leftover / "f").write_text("f", encoding="utf-8")
    gate = threading.Event()
    deleting: list[str] = []
    real = worktree_trash.shutil.rmtree

    def _blocked(path, *args, **kwargs):
        deleting.append(threading.current_thread().name)
        gate.wait(10)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(worktree_trash.shutil, "rmtree", _blocked)
    monkeypatch.setattr(worktree_trash, "_swept", set())
    settings = GateSettings(
        runs_dir=tmp_path / "state" / "runs", repo_root=repo, worktrees_dir=worktrees
    )
    # The constructor returns while the delete is still blocked: it never
    # runs rmtree on the calling (event loop) thread.
    runner = GateRunner(settings)
    assert runner._worktrees.trash_root == worktrees / ".conductor-trash"
    main = threading.current_thread().name
    assert main not in deleting
    gate.set()
    for _ in range(100):
        if not leftover.exists():
            break
        worktree_trash.wait_pending(0.1)
    assert worktree_trash.wait_pending(10)
    assert not leftover.exists()
    # A second GateRunner in the same process does not sweep again.
    again = worktrees / ".conductor-trash" / "def-line-1"
    again.mkdir()
    GateRunner(settings)
    worktree_trash.wait_pending(1)
    assert again.exists()
    # No trash directory at all is fine too.
    GateRunner(
        GateSettings(
            runs_dir=tmp_path / "s2" / "runs",
            repo_root=repo,
            worktrees_dir=tmp_path / "s2" / "worktrees",
        )
    )


def test_locked_worktree_is_not_trashed(tmp_path: Path, git_env) -> None:
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    path = root / "key" / "gates" / "d1" / "line-0"
    runner = _PlainGit()
    manager = GitWorktreeManager(runner=runner, trash_root=root / ".conductor-trash")
    asyncio.run(manager.add_detached(repo, path, "HEAD"))
    _git(repo, "worktree", "lock", str(path))
    asyncio.run(manager.remove(repo, path))
    # Not renamed away: the locked registration still has its files, and the
    # old Git remove path was used (Git refuses a locked tree without -f -f).
    assert path.is_dir()
    assert ["worktree", "remove", "--force", str(path)] in runner.calls
    assert not (root / ".conductor-trash").exists() or not list(
        (root / ".conductor-trash").iterdir()
    )
    _git(repo, "worktree", "unlock", str(path))
    asyncio.run(manager.remove(repo, path))
    assert not path.exists()


def test_is_locked_reads_porcelain(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    text = f"worktree {a}\nHEAD 1\ndetached\nlocked reason\n\nworktree {b}\nHEAD 2\ndetached\n\n"
    assert worktree_trash.is_locked(text, a) is True
    assert worktree_trash.is_locked(text, b) is False


def test_clean_ignores_untracked_ds_store_only(tmp_path: Path, git_env) -> None:
    repo = _repo(tmp_path)
    manager = GitWorktreeManager(runner=_PlainGit())
    (repo / "sub").mkdir()
    (repo / "sub" / ".DS_Store").write_bytes(b"finder")
    assert asyncio.run(manager._clean(repo)) is True

    (repo / "other.txt").write_text("o", encoding="utf-8")
    assert asyncio.run(manager._clean(repo)) is False
    (repo / "other.txt").unlink()

    (repo / ".DS_Store").write_bytes(b"changed")
    assert asyncio.run(manager._clean(repo)) is False


def test_status_paths_drop_untracked_ds_store_only(tmp_path: Path, git_env) -> None:
    repo = _repo(tmp_path)
    (repo / "sub").mkdir()
    (repo / "sub" / ".DS_Store").write_bytes(b"finder")
    assert git_status_paths(repo) == ()

    (repo / "new.txt").write_text("n", encoding="utf-8")
    (repo / ".DS_Store").write_bytes(b"changed")
    assert set(git_status_paths(repo) or ()) == {"new.txt", ".DS_Store"}


def test_porcelain_paths_keep_renames_around_noise() -> None:
    raw = b"?? .DS_Store\0R  new.py\0old.py\0?? a/.DS_Store\0 M .DS_Store\0?? x.py\0"
    assert gates_module._porcelain_v1_paths(raw) == ("new.py", "old.py", ".DS_Store", "x.py")
