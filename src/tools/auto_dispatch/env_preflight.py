"""Environment preflight for a live Worker launch.

Environment-caused failures (no ``git`` on the Worker's PATH, an
unauthenticated ``gh``, two sessions sharing one worktree) used to surface
deep inside a gate or a Worker run. These checks run up front so the launch
fails with one clear reason instead.

``gh`` is only checked when ``CONDUCTOR_PREFLIGHT_REQUIRE_GH`` is ``1`` or
``true``: most lines never push, and ``gh auth status`` costs a process.
Its output is discarded; only the exit code is read.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .protocols import LaunchError

REQUIRE_GH_ENV = "CONDUCTOR_PREFLIGHT_REQUIRE_GH"

GhRunner = Callable[[Sequence[str], dict[str, str], float], int]


@dataclass(frozen=True)
class EnvProblem:
    code: str
    reason: str


#: Problems another attempt on this host may not hit again.
TRANSIENT_CODES = frozenset({"worktree_in_use"})


class EnvPreflightError(LaunchError):
    """The launch environment cannot run a Worker; nothing was started."""

    def __init__(self, problems: Sequence[EnvProblem]) -> None:
        self.problems = tuple(problems)
        detail = "; ".join(f"{item.code}: {item.reason}" for item in self.problems)
        super().__init__(f"environment preflight failed: {detail}")

    @property
    def retryable(self) -> bool:
        """False when this host cannot run any Worker (git/gh): retrying the
        launch here at once fails the same way. A busy worktree may free up."""
        return all(item.code in TRANSIENT_CODES for item in self.problems)

    @property
    def failure_reason(self) -> str:
        return f"env_preflight:{self.problems[0].code}" if self.problems else "env_preflight"


def require_gh_from_env() -> bool:
    return os.environ.get(REQUIRE_GH_ENV, "").strip().lower() in ("1", "true")


def check_git(path_env: str) -> EnvProblem | None:
    if shutil.which("git", path=path_env) is None:
        return EnvProblem("git_missing", "git is not on the worker PATH")
    return None


def _run_gh(argv: Sequence[str], env: dict[str, str], timeout: float) -> int:
    return subprocess.run(
        list(argv),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    ).returncode


def check_gh_auth(
    path_env: str, *, runner: GhRunner | None = None, timeout: float = 10
) -> EnvProblem | None:
    gh = shutil.which("gh", path=path_env)
    if gh is None:
        return EnvProblem("gh_missing", "gh is not on the worker PATH")
    env = dict(os.environ)
    env["PATH"] = path_env
    try:
        code = (runner or _run_gh)([gh, "auth", "status", "--hostname", "github.com"], env, timeout)
    except (OSError, subprocess.TimeoutExpired):
        return EnvProblem("gh_unauthenticated", "gh auth status could not run")
    if code != 0:
        return EnvProblem("gh_unauthenticated", "gh is not authenticated for github.com")
    return None


def preflight(
    path_env: str, *, require_gh: bool, gh_runner: GhRunner | None = None
) -> list[EnvProblem]:
    problems: list[EnvProblem] = []
    git = check_git(path_env)
    if git is not None:
        problems.append(git)
    if require_gh:
        gh = check_gh_auth(path_env, runner=gh_runner)
        if gh is not None:
            problems.append(gh)
    return problems


class WorktreeLease:
    """An exclusive, non-blocking ownership lock on one worktree path."""

    def __init__(self, fd: int, path: Path) -> None:
        self._fd = fd
        self.path = path

    def close(self) -> None:
        if self._fd < 0:
            return
        fd, self._fd = self._fd, -1
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> WorktreeLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def lease_path(worktree: Path, lock_dir: Path) -> Path:
    key = hashlib.sha256(os.path.realpath(worktree).encode()).hexdigest()[:24]
    return Path(lock_dir) / f"{key}.lock"


def acquire_worktree_lease(
    worktree: Path, lock_dir: Path, owner: str
) -> WorktreeLease | EnvProblem:
    """Lease ``worktree`` for ``owner`` or say which session already holds it."""
    lock_dir = Path(lock_dir)
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(lock_dir, 0o700)
    except OSError:
        pass
    path = lease_path(worktree, lock_dir)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            holder = os.pread(fd, 512, 0).decode("utf-8", "replace").strip() or "unknown owner"
        except OSError:
            holder = "unknown owner"
        os.close(fd)
        return EnvProblem(
            "worktree_in_use",
            f"worktree {worktree} is already in use by another session ({holder})",
        )
    except BaseException:
        os.close(fd)
        raise
    os.ftruncate(fd, 0)
    os.pwrite(fd, owner.encode("utf-8", "replace"), 0)
    return WorktreeLease(fd, path)
