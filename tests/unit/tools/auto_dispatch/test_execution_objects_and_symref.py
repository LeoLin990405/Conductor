"""Launcher exit 79 ``objects_not_migrated`` and refusing a non-plain line ref.

A. dispatch_launch.sh exits 79 with one ``dispatch_launch: objects_not_migrated``
   stderr line when it could not move the Worker's git objects out of the
   per-launch quarantine. That is a launcher failure: class UNKNOWN, reason
   ``objects_not_migrated``, whatever the transcript's wording suggests.
B. A line branch ref that is a symlink or a symbolic ref is never read as the
   run's head (``_verify_submitted_worktree`` refuses, ``_branch_head`` is None).
"""

from __future__ import annotations

import os

import pytest

from src.llms.native_cli.failure import FailureClass
from src.tools.auto_dispatch.execution import (
    OBJECTS_NOT_MIGRATED_REASON,
    OBJECTS_NOT_MIGRATED_SWEEP_FAILED_REASON,
    LaunchError,
    WorkerLaunchFailure,
)
from tests.unit.tools.auto_dispatch.test_execution_exit_commit import (
    _git,
    _launcher,
    _line,
    _rev,
    _run,
    _state,
    _worktree,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def repo(tmp_path):
    """The same base repository as test_execution_exit_commit's fixture."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "dispatch@example.com")
    _git(tmp_path, "config", "user.name", "Dispatch Test")
    _git(tmp_path, "config", "core.hooksPath", "/dev/null")
    _git(tmp_path, "config", "core.fsmonitor", "false")
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


RATE_LIMIT_TEXT = "Error: 429 Too Many Requests: rate limit exceeded, retry after 30s\n"
MARKER = "dispatch_launch: objects_not_migrated quarantine=/tmp/q/objects-123\n"


def _exit_runner(text: str, code: int):
    async def runner(argv, cwd, log_path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(text, encoding="utf-8")
        return code

    return runner


async def _failure(repo, tmp_path, text: str, code: int = 79) -> WorkerLaunchFailure:
    launcher = _launcher(repo, _state(tmp_path), _exit_runner(text, code))
    with pytest.raises(WorkerLaunchFailure) as info:
        await _run(_line(repo), launcher)
    return info.value


async def test_exit_79_with_the_marker_is_unknown_objects_not_migrated(
    repo,
    tmp_path,
) -> None:
    failure = await _failure(repo, tmp_path, RATE_LIMIT_TEXT + MARKER)

    assert failure.exit_code == 79
    assert failure.failure_class is FailureClass.UNKNOWN
    assert failure.reason_code == OBJECTS_NOT_MIGRATED_REASON == "objects_not_migrated"
    assert failure.retry_after_seconds is None


async def test_exit_79_without_the_marker_keeps_the_transcript_class(
    repo,
    tmp_path,
) -> None:
    failure = await _failure(repo, tmp_path, RATE_LIMIT_TEXT)

    assert failure.failure_class is FailureClass.RATE_LIMITED
    assert failure.reason_code != OBJECTS_NOT_MIGRATED_REASON


async def test_the_marker_must_start_a_line(repo, tmp_path) -> None:
    failure = await _failure(
        repo,
        tmp_path,
        RATE_LIMIT_TEXT + "model said: dispatch_launch: objects_not_migrated\n",
    )

    assert failure.failure_class is FailureClass.RATE_LIMITED


async def test_the_marker_on_another_exit_code_is_ignored(
    repo,
    tmp_path,
) -> None:
    failure = await _failure(repo, tmp_path, RATE_LIMIT_TEXT + MARKER, code=75)

    assert failure.failure_class is FailureClass.RATE_LIMITED


# -- B: the line ref must be a plain loose or packed ref ---------------------


def _common_ref(repo, branch: str):
    return repo / ".git" / "refs" / "heads" / branch


def _commit(cwd) -> None:
    (cwd / "work.txt").write_text("the work\n", encoding="utf-8")
    _git(cwd, "add", "work.txt")
    _git(cwd, "commit", "-q", "-m", "the work")


def _action_runner(action):
    async def runner(argv, cwd, log_path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("worker done\n", encoding="utf-8")
        action(cwd)
        return 0

    return runner


def _symlink_ref(repo, line):
    def action(cwd) -> None:
        _commit(cwd)
        ref = _common_ref(repo, line.branch)
        other = _common_ref(repo, "other")
        other.write_text(ref.read_text(encoding="utf-8"), encoding="utf-8")
        ref.unlink()
        os.symlink(other, ref)

    return action


def _symbolic_ref(repo, line):
    def action(cwd) -> None:
        _commit(cwd)
        _common_ref(repo, line.branch).write_text(
            "ref: refs/heads/main\n", encoding="utf-8"
        )

    return action


def _symlinked_dir(repo, line):
    """A parent directory of the ref replaced by a symlink to a copy."""

    def action(cwd) -> None:
        _commit(cwd)
        ref = _common_ref(repo, line.branch)
        parent = ref.parent
        copy = repo / ".git" / "refs" / "elsewhere"
        parent.rename(copy)
        os.symlink(copy, parent)

    return action


@pytest.mark.parametrize("make", [_symlink_ref, _symbolic_ref, _symlinked_dir])
async def test_a_symlink_or_symbolic_line_ref_is_refused(
    repo,
    tmp_path,
    make,
) -> None:
    line = _line(repo)
    launcher = _launcher(repo, _state(tmp_path), _action_runner(make(repo, line)))

    with pytest.raises(LaunchError, match="branch ref is symbolic or a symlink"):
        await _run(line, launcher)

    assert await launcher._branch_head(line, _worktree(_state(tmp_path))) is None


async def test_a_plain_loose_ref_is_accepted(repo, tmp_path) -> None:
    line = _line(repo)
    launcher = _launcher(repo, _state(tmp_path), _action_runner(_commit))

    result = await _run(line, launcher)

    assert _common_ref(repo, line.branch).is_file()
    head = _rev(repo, line.branch)
    assert result.head == head
    assert await launcher._branch_head(line, _worktree(_state(tmp_path))) == head


async def test_a_packed_ref_is_accepted(repo, tmp_path) -> None:
    def commit_and_pack(cwd) -> None:
        _commit(cwd)
        _git(cwd, "pack-refs", "--all", "--prune")

    line = _line(repo)
    launcher = _launcher(repo, _state(tmp_path), _action_runner(commit_and_pack))

    result = await _run(line, launcher)

    assert not _common_ref(repo, line.branch).exists()
    head = _rev(repo, line.branch)
    assert result.head == head
    assert await launcher._branch_head(line, _worktree(_state(tmp_path))) == head


# -- A2: the Worker's own failure stays visible behind the marker ------------


def _marker(status: str) -> str:
    return f"dispatch_launch: objects_not_migrated worker_status={status} quarantine=/tmp/q/o\n"


@pytest.mark.parametrize(
    ("text", "status", "expected"),
    [
        (RATE_LIMIT_TEXT, "1", FailureClass.RATE_LIMITED),
        ("", "76", FailureClass.QUOTA_EXHAUSTED),
        ("", "77", FailureClass.AUTH_FAILED),
        ("", "124", FailureClass.TIMEOUT),
    ],
)
async def test_a_worker_failure_behind_the_marker_keeps_its_class(
    repo, tmp_path, text, status, expected
) -> None:
    failure = await _failure(repo, tmp_path, text + _marker(status))

    assert failure.failure_class is expected
    assert failure.reason_code == OBJECTS_NOT_MIGRATED_REASON


async def test_a_worker_that_exited_0_is_not_read_from_its_transcript(
    repo, tmp_path
) -> None:
    failure = await _failure(repo, tmp_path, RATE_LIMIT_TEXT + _marker("0"))

    assert failure.failure_class is FailureClass.UNKNOWN
    assert failure.reason_code == OBJECTS_NOT_MIGRATED_REASON
    assert failure.retry_after_seconds is None


async def test_a_plain_crash_behind_the_marker_is_unknown(repo, tmp_path) -> None:
    failure = await _failure(repo, tmp_path, "Segmentation fault\n" + _marker("139"))

    assert failure.failure_class is FailureClass.UNKNOWN
    assert failure.reason_code == OBJECTS_NOT_MIGRATED_REASON


async def test_a_failed_sweep_has_its_own_reason_and_stays_unknown(
    repo, tmp_path
) -> None:
    marker = (
        "dispatch_launch: objects_not_migrated worker_status=1 sweep=failed "
        "quarantine=/tmp/q with space/o\n"
    )
    failure = await _failure(repo, tmp_path, RATE_LIMIT_TEXT + marker)

    assert failure.failure_class is FailureClass.UNKNOWN
    assert failure.reason_code == OBJECTS_NOT_MIGRATED_SWEEP_FAILED_REASON
    assert failure.reason_code == "objects_not_migrated_sweep_failed"
    assert failure.retry_after_seconds is None


async def test_sweep_failed_in_the_quarantine_path_is_not_a_flag(
    repo, tmp_path
) -> None:
    marker = (
        "dispatch_launch: objects_not_migrated worker_status=0 "
        "quarantine=/tmp/sweep=failed/o\n"
    )
    failure = await _failure(repo, tmp_path, marker)

    assert failure.reason_code == OBJECTS_NOT_MIGRATED_REASON
