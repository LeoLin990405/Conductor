"""Environment preflight: git/gh on the Worker PATH, one session per worktree,
``.DS_Store`` is not a dirty tree, and the G2 evidence header."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import src.tools.auto_dispatch.execution as execution_module
from src.tools.auto_dispatch import env_preflight
from src.tools.auto_dispatch.env_preflight import (
    EnvPreflightError,
    EnvProblem,
    WorktreeLease,
    acquire_worktree_lease,
    check_gh_auth,
    check_git,
    preflight,
)
from src.tools.auto_dispatch.execution import ScriptLauncher, line_evidence
from src.tools.auto_dispatch.gates import SCHEMA_VERSION
from src.tools.auto_dispatch.protocols import LaunchError, UncommittedChangesError
from src.tools.auto_dispatch.types import Dispatch, Line, LineStatus


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.delenv(env_preflight.REQUIRE_GH_ENV, raising=False)


def _bin(tmp_path: Path, name: str, body: str) -> Path:
    directory = tmp_path / f"bin-{name}"
    directory.mkdir(exist_ok=True)
    script = directory / name
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return directory


# -- git / gh -----------------------------------------------------------------


def test_git_missing_and_present(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    problem = check_git(str(empty))
    assert problem is not None and problem.code == "git_missing"
    assert check_git(str(_bin(tmp_path, "git", "exit 0"))) is None


def test_gh_unauthenticated_authenticated_and_missing(tmp_path: Path) -> None:
    bad = _bin(tmp_path, "gh", 'echo "token SECRET" >&2; exit 1')
    problem = check_gh_auth(str(bad))
    assert problem == EnvProblem("gh_unauthenticated", "gh is not authenticated for github.com")
    assert "SECRET" not in problem.reason
    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    (ok_dir / "gh").write_text(
        '#!/bin/sh\n[ "$1 $2 $3 $4" = "auth status --hostname github.com" ] || exit 9\nexit 0\n',
        encoding="utf-8",
    )
    (ok_dir / "gh").chmod(0o755)
    assert check_gh_auth(str(ok_dir)) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    missing = check_gh_auth(str(empty))
    assert missing is not None and missing.code == "gh_missing"


def test_preflight_only_runs_gh_when_required(tmp_path: Path) -> None:
    path_env = str(_bin(tmp_path, "git", "exit 0"))
    calls: list[list[str]] = []

    def runner(argv, env, timeout):
        calls.append(list(argv))
        return 1

    assert preflight(path_env, require_gh=False, gh_runner=runner) == []
    assert calls == []
    problems = preflight(path_env, require_gh=True, gh_runner=runner)
    assert [item.code for item in problems] == ["gh_missing"]  # no gh on that PATH
    assert calls == []
    both = path_env + os.pathsep + str(_bin(tmp_path, "gh", "exit 0"))
    assert [item.code for item in preflight(both, require_gh=True, gh_runner=runner)] == [
        "gh_unauthenticated"
    ]
    assert calls and calls[0][1:] == ["auth", "status", "--hostname", "github.com"]


def test_require_gh_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    assert env_preflight.require_gh_from_env() is False
    for value in ("1", "true", "TRUE"):
        monkeypatch.setenv(env_preflight.REQUIRE_GH_ENV, value)
        assert env_preflight.require_gh_from_env() is True
    monkeypatch.setenv(env_preflight.REQUIRE_GH_ENV, "0")
    assert env_preflight.require_gh_from_env() is False


def test_error_message_and_failure_reason() -> None:
    error = EnvPreflightError([EnvProblem("git_missing", "a"), EnvProblem("gh_missing", "b")])
    assert isinstance(error, LaunchError)
    assert str(error) == "environment preflight failed: git_missing: a; gh_missing: b"
    assert error.failure_reason == "env_preflight:git_missing"


# -- worktree lease -----------------------------------------------------------


def test_lease_is_exclusive_across_path_spellings(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(worktree)
    locks = tmp_path / "runs" / "worktree-locks"
    first = acquire_worktree_lease(worktree, locks, "pid 1 run first")
    assert isinstance(first, WorktreeLease)
    assert (locks.stat().st_mode & 0o777) == 0o700
    for spelling in (worktree, alias, tmp_path / "wt" / ".." / "wt"):
        second = acquire_worktree_lease(spelling, locks, "pid 2 run second")
        assert isinstance(second, EnvProblem)
        assert second.code == "worktree_in_use"
        assert "pid 1 run first" in second.reason
    first.close()
    with acquire_worktree_lease(alias, locks, "pid 3 run third") as third:
        assert isinstance(third, WorktreeLease)
    again = acquire_worktree_lease(worktree, locks, "pid 4")
    assert isinstance(again, WorktreeLease)
    again.close()


# -- ScriptLauncher hook ------------------------------------------------------


def _line(branch: str = "") -> Line:
    return Line(id="L", name="L", task="t", branch=branch, current_run="d:L:0:run")


@pytest.mark.asyncio
async def test_execute_worker_fails_fast_when_git_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[object] = []

    async def fake_run(*args, **kwargs):
        spawned.append(args)
        return 0

    monkeypatch.setattr(execution_module, "_run_command", fake_run)
    launcher = ScriptLauncher(tmp_path / "runs", cwd=tmp_path, dispatch_id="d")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(launcher, "_launch_env", lambda *a, **k: {"PATH": str(empty)})

    async def no_worktree(*a, **k):
        raise AssertionError("worktree work before preflight")

    monkeypatch.setattr(launcher, "_prepare_line_worktree", no_worktree)
    line = _line(branch="feature/auto-dispatch-d/line-L")
    with pytest.raises(EnvPreflightError, match="git_missing") as info:
        await launcher._execute_worker(["s", "codex"], line, "run-1", allow_dirty=False)
    assert info.value.failure_reason == "env_preflight:git_missing"
    # The machine records the line's reason when it handles the LaunchError.
    assert line.failure_reason is None
    assert spawned == []


@pytest.mark.asyncio
async def test_fake_runner_skips_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner(argv, cwd, log_path):
        return 0

    launcher = ScriptLauncher(tmp_path / "runs", cwd=tmp_path, runner=runner)
    monkeypatch.setattr(launcher, "_launch_env", lambda *a, **k: {"PATH": "/nonexistent"})
    code, *_ = await launcher._execute_worker(["s", "codex"], _line(), "run-1", allow_dirty=False)
    assert code == 0


@pytest.mark.asyncio
async def test_second_session_on_same_worktree_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = tmp_path / "runs"
    seen_during: list[object] = []

    async def fake_run(argv, cwd, log_path, **kwargs):
        seen_during.append(acquire_worktree_lease(worktree, runs / "worktree-locks", "probe"))
        return 0

    monkeypatch.setattr(execution_module, "_run_command", fake_run)
    repo = tmp_path / "repo"
    repo.mkdir()
    launcher = ScriptLauncher(runs, cwd=repo, dispatch_id="d")
    monkeypatch.setattr(launcher, "_launch_env", lambda *a, **k: {})
    line = _line(branch="feature/auto-dispatch-d/line-L")
    # The lease is on the path _prepare_line_worktree will use.
    worktree = launcher._expected_line_worktree(line)
    prepares: list[int] = []

    async def prepared(*a, **k):
        prepares.append(1)
        worktree.mkdir(parents=True, exist_ok=True)
        return worktree

    async def head(repo, cwd, argv):
        return 0, "a" * 40, ""

    monkeypatch.setattr(launcher, "_prepare_line_worktree", prepared)
    monkeypatch.setattr(launcher, "_line_git", head)

    # Held while the Worker runs, released when it exits.
    code, *_ = await launcher._execute_worker(["s", "codex"], line, "run-1", allow_dirty=False)
    assert code == 0
    assert isinstance(seen_during[0], EnvProblem) and seen_during[0].code == "worktree_in_use"
    assert "run run-1" in seen_during[0].reason

    other = acquire_worktree_lease(worktree, runs / "worktree-locks", "pid 99 run other-session")
    assert isinstance(other, WorktreeLease)
    try:
        with pytest.raises(EnvPreflightError, match="worktree_in_use") as info:
            await launcher._execute_worker(["s", "codex"], line, "run-2", allow_dirty=False)
        assert "other-session" in str(info.value)
        assert info.value.failure_reason == "env_preflight:worktree_in_use"
        assert line.failure_reason is None
        assert len(seen_during) == 1
        # Refused before the worktree is touched, so a busy tree is never
        # misread as uncommitted_changes.
        assert prepares == [1]
    finally:
        other.close()


# -- .DS_Store is not a dirty tree --------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "T")
    _git(path, "config", "user.email", "t@example.invalid")
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path


async def _fake_runner(argv, cwd, log_path):
    return 0


@pytest.mark.asyncio
async def test_ds_store_does_not_make_a_line_worktree_dirty(tmp_path: Path, repo: Path) -> None:
    state = tmp_path / "state"
    launcher = ScriptLauncher(
        state / "runs", cwd=repo, worktrees_dir=state / "wts", dispatch_id="d",
        runner=_fake_runner,
    )
    line = Line(
        id="L", name="L", task="t", branch="feature/auto-dispatch-d/line-L",
        base_commit=_git(repo, "rev-parse", "HEAD"),
    )
    worktree = await launcher._prepare_line_worktree(line)
    (worktree / ".DS_Store").write_bytes(b"\0finder")
    (worktree / "sub").mkdir()
    (worktree / "sub" / ".DS_Store").write_bytes(b"\0finder")
    assert await launcher._prepare_line_worktree(line) == worktree
    (worktree / "other.txt").write_text("x", encoding="utf-8")
    with pytest.raises(UncommittedChangesError):
        await launcher._prepare_line_worktree(line)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "dirty"),
    [("?? .DS_Store\n?? sub/.DS_Store\n", False), ("?? .DS_Store\n?? other.txt\n", True)],
)
async def test_app_branch_facts_ignores_ds_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, dirty: bool
) -> None:
    launcher = ScriptLauncher(tmp_path / "runs", cwd=tmp_path, dispatch_id="d", runner=_fake_runner)
    monkeypatch.setattr(launcher, "line_worktree", lambda line: tmp_path)
    monkeypatch.setattr(launcher, "_line_ref_is_plain", lambda repo, branch: True)

    async def fake_git(repo, cwd, argv):
        if argv[0] == "rev-parse":
            return 0, "b" * 40 + "\n", ""
        if argv[0] == "diff":
            return 0, "x.py\n", ""
        return 0, status, ""

    monkeypatch.setattr(launcher, "_line_git", fake_git)
    facts = await launcher.app_branch_facts(_line(branch="feature/x"), "a" * 40)
    assert facts is not None and facts["dirty"] is dirty


@pytest.mark.asyncio
async def test_cleanup_removes_ds_store_only_tree_without_archiving(
    tmp_path: Path, repo: Path
) -> None:
    state = tmp_path / "state"
    launcher = ScriptLauncher(
        state / "runs", cwd=repo, worktrees_dir=state / "wts", dispatch_id="d",
        runner=_fake_runner,
    )
    line = Line(
        id="L", name="L", task="t", branch="feature/auto-dispatch-d/line-L",
        base_commit=_git(repo, "rev-parse", "HEAD"),
    )
    worktree = await launcher._prepare_line_worktree(line)
    (worktree / ".DS_Store").write_bytes(b"\0finder")
    dispatch = Dispatch(id="d", goal="g", user_id="u", created_at="now")
    dispatch.lines = [line]
    assert await launcher.cleanup_worktrees(dispatch) == []
    assert not worktree.exists()
    assert not (state / "wts" / "recovery-artifacts").exists()


# -- G2 evidence header -------------------------------------------------------


def test_g2_header_says_host_run_evidence() -> None:
    line = Line(id="l0", name="l", task="t", acceptance=("cmd: pytest -q",), status=LineStatus.PASSED)
    dispatch = Dispatch(id="d1", goal="g", user_id="u", created_at="now")
    dispatch.lines = [line]
    dispatch.gates = {
        "schema_version": SCHEMA_VERSION,
        "lines": {"l0": {"gates": {"G2": {
            "result": "pass", "reason": "focused_tests_passed",
            "commands": [{"index": 0, "exit_code": 0, "duration_ms": 1, "log_ref": "x"}],
        }}}},
    }
    text = line_evidence(dispatch, {})("l0")
    assert (
        "G2 results (host-run on the line's commit; treat as the test evidence):\n"
        "G2 pass focused_tests_passed\ncommand 0 exit 0"
    ) in text


def test_host_problems_are_not_retryable_but_a_busy_worktree_is() -> None:
    assert EnvPreflightError([EnvProblem("git_missing", "x")]).retryable is False
    assert EnvPreflightError([EnvProblem("gh_unauthenticated", "x")]).retryable is False
    assert EnvPreflightError([EnvProblem("worktree_in_use", "x")]).retryable is True
