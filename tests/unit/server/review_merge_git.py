"""Real git worktree helpers for the review/merge tests (no network).

The merge tests must exercise actual commits, worktrees and a real merge, so
these helpers build a throwaway repository under pytest's ``tmp_path`` with an
identity configured per invocation. Nothing here touches a remote.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_GIT = [
    "git",
    "-c",
    "user.name=Conductor Test",
    "-c",
    "user.email=test@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "init.defaultBranch=main",
]


class GitCommandError(AssertionError):
    pass


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        [*_GIT, "-C", str(path), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise GitCommandError(
            f"git {' '.join(args)} failed in {path} (exit {result.returncode}):\n"
            f"{(result.stdout or '') + (result.stderr or '')}"
        )
    return result.stdout.strip()


def write(path: Path, relative: str, content: str) -> Path:
    target = path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def init_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    for name, content in files.items():
        write(path, name, content)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    return path


def commit_file(path: Path, name: str, content: str, message: str) -> str:
    write(path, name, content)
    git(path, "add", name)
    git(path, "commit", "-q", "-m", message)
    return head(path)


def add_worktree(
    repo: Path, dest: Path, branch: str, *, create_branch: bool = False
) -> Path:
    if create_branch:
        git(repo, "worktree", "add", "-q", "-b", branch, str(dest))
    else:
        git(repo, "worktree", "add", "-q", str(dest), branch)
    return dest


def head(path: Path) -> str:
    return git(path, "rev-parse", "HEAD")


def parents(path: Path) -> list[str]:
    return git(path, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]


def branch(path: Path) -> str:
    return git(path, "branch", "--show-current")


def has_merge_head(path: Path) -> bool:
    """True while a worktree is in a real (unfinished) merge."""
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def worktree_count(repo: Path) -> int:
    output = git(repo, "worktree", "list", "--porcelain")
    return sum(1 for line in output.splitlines() if line.startswith("worktree "))
