"""N49: what a worker's exit means when the tree or the branch already has the work.

R8c's sub-lines reported passing tests and exited: some left the work in the
working tree, and some found nothing left to change on a branch that already
carried it. Both ended the line for a human -- ``uncommitted_changes`` and
``no_new_commit`` -- although nothing was actually wrong.

The rules now, both decided at the launcher's exit check:

- A tree with changes in it gets exactly one extra turn that asks the worker
  to commit them. A worker that will not commit still stops
  ``uncommitted_changes`` with its tree preserved for recovery, and the prompt
  is never repeated.
- A clean tree whose branch is already ahead of the line's base is a
  submission of the work that is there. Only a branch with nothing on it at
  all still stops ``no_new_commit``.

One exception, decided by the machine and not by git: a run started to clear
blockers a reviewer already raised must commit beyond its own starting head.
Such a run that leaves the branch where it found it has not answered the
blockers, so it stops ``no_new_commit`` exactly as N34 had it -- reading it
as a submission would resubmit the head the review already refused and burn
another review round on a worker that ignored the findings.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from src.tools.auto_dispatch import LineStatus
from src.tools.auto_dispatch.execution import (
    LaunchResult,
    ScriptLauncher,
    WorkerLaunchFailure,
)
from src.tools.auto_dispatch.protocols import NoNewCommitError, UncommittedChangesError
from src.tools.auto_dispatch.types import Line
from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle
from tests.unit.tools.auto_dispatch.test_n34_committed_retry import (
    BASE,
    H1,
    H2,
    BlockOnce,
    _dispatch,
    _handoff,
)

DISPATCH_ID = "d-n49"

pytestmark = pytest.mark.asyncio


def _git(cwd, *args: str) -> None:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _rev(repo, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "dispatch@example.com")
    _git(tmp_path, "config", "user.name", "Dispatch Test")
    _git(tmp_path, "config", "core.hooksPath", "/dev/null")
    _git(tmp_path, "config", "core.fsmonitor", "false")
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _state(tmp_path):
    """Run artifacts beside the repo, named for this test alone.

    ``tmp_path`` is the repo, and its parent is the whole session's temporary
    directory: a shared name there would let one test's worktree answer
    another's lookup.
    """
    state = tmp_path.parent / f"{tmp_path.name}-state"
    state.mkdir(parents=True, exist_ok=True)
    return state


def _worktree(state):
    return next((state / "dispatch-worktrees").glob("*/*/*/*"))


def _launcher(repo, state, runner) -> ScriptLauncher:
    script = state / "fake-worker"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    return ScriptLauncher(
        state / "runs",
        script=script,
        cwd=repo,
        worktrees_dir=state / "dispatch-worktrees",
        dispatch_id=DISPATCH_ID,
        runner=runner,
    )


def _line(repo) -> Line:
    return Line(
        id="line-n",
        name="the line",
        task="do the work",
        branch=f"feature/auto-dispatch-{DISPATCH_ID}/line-line-n",
        base_commit=_rev(repo, "HEAD"),
        current_run=f"{DISPATCH_ID}:line-n:0:run",
    )


def _run(line, launcher, *, fix_round: bool = False):
    kwargs = {} if not fix_round else {"fix_round": True}
    return launcher.launch_worker(
        line, "kimi", [], session_id=None, session_mode=None, handoff_path=None, **kwargs
    )


def _recording_runner(calls: list[list[str]], actions):
    """Record every argv, then run that call's action with ``(cwd, call number)``."""

    async def runner(argv, cwd, log_path):
        calls.append(list(argv))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("worker done\n", encoding="utf-8")
        return actions[len(calls) - 1](cwd, len(calls))

    return runner


def leave_dirty(cwd, call: int) -> int:
    (cwd / "work.txt").write_text("the work\n", encoding="utf-8")
    return 0


def commit_it(cwd, call: int) -> int:
    _git(cwd, "add", "work.txt")
    _git(cwd, "commit", "-q", "-m", "the work")
    return 0


def commit_more(cwd, call: int) -> int:
    (cwd / "more.txt").write_text("more work\n", encoding="utf-8")
    _git(cwd, "add", "more.txt")
    _git(cwd, "commit", "-q", "-m", "more work")
    return 0


def nothing(cwd, call: int) -> int:
    return 0


def refuse(cwd, call: int) -> int:
    return 1


def rate_limited(cwd, call: int) -> int:
    (cwd / "partial.txt").write_text("partial\n", encoding="utf-8")
    return 75


async def test_a_dirty_exit_gets_one_commit_prompt_and_then_verifies(
    repo, tmp_path
) -> None:
    calls: list[list[str]] = []
    launcher = _launcher(
        repo, _state(tmp_path), _recording_runner(calls, [leave_dirty, commit_it])
    )
    line = _line(repo)

    result = await _run(line, launcher)

    assert len(calls) == 2  # the run, then exactly one prompt
    # The prompt names the line and its branch, so the commit lands on the
    # right one, and it asks for the commit and nothing else.
    assert f"feature/auto-dispatch-{DISPATCH_ID}/line-line-n" in calls[1][2]
    assert "uncommitted changes" in calls[1][2]
    assert "do the work" not in calls[1][2]
    work = _worktree(_state(tmp_path)) / "work.txt"
    assert work.read_text(encoding="utf-8") == "the work\n"
    head = _rev(repo, line.branch)
    assert (result.start_head, result.head) == (line.base_commit, head)


async def test_the_commit_prompt_is_never_repeated(repo, tmp_path) -> None:
    calls: list[list[str]] = []
    launcher = _launcher(
        repo, _state(tmp_path), _recording_runner(calls, [leave_dirty, leave_dirty])
    )
    line = _line(repo)

    with pytest.raises(UncommittedChangesError, match="uncommitted_changes"):
        await _run(line, launcher)

    assert len(calls) == 2  # one prompt, then the run stops
    # The tree is preserved for recovery, exactly as before N49.
    work = _worktree(_state(tmp_path)) / "work.txt"
    assert work.read_text(encoding="utf-8") == "the work\n"


async def test_a_commit_prompt_that_fails_keeps_the_tree_recoverable(
    repo, tmp_path
) -> None:
    calls: list[list[str]] = []
    launcher = _launcher(
        repo, _state(tmp_path), _recording_runner(calls, [leave_dirty, refuse])
    )
    line = _line(repo)

    with pytest.raises(UncommittedChangesError, match="uncommitted_changes"):
        await _run(line, launcher)

    assert len(calls) == 2
    work = _worktree(_state(tmp_path)) / "work.txt"
    assert work.read_text(encoding="utf-8") == "the work\n"


async def test_a_dirty_tree_after_a_classified_exit_is_not_prompted(
    repo, tmp_path
) -> None:
    """A run that failed committed nothing: the failure decides, with no nudge."""
    calls: list[list[str]] = []
    launcher = _launcher(
        repo, _state(tmp_path), _recording_runner(calls, [rate_limited])
    )
    line = _line(repo)

    with pytest.raises(WorkerLaunchFailure):
        await _run(line, launcher)

    assert len(calls) == 1


async def test_a_clean_tree_whose_branch_already_has_the_work_is_submitted(
    repo, tmp_path
) -> None:
    calls: list[list[str]] = []
    launcher = _launcher(
        repo,
        _state(tmp_path),
        _recording_runner(calls, [leave_dirty, commit_it, nothing]),
    )
    line = _line(repo)
    first = await _run(line, launcher)
    head = _rev(repo, line.branch)
    assert first.head == head and head != line.base_commit

    # The retry finds the work already on the branch and the tree clean.
    line.current_run = f"{DISPATCH_ID}:line-n:0:retry"
    retry = await _run(line, launcher)

    assert (retry.start_head, retry.head) == (head, head)
    assert _rev(repo, line.branch) == head


async def test_a_branch_with_nothing_on_it_still_stops_no_new_commit(
    repo, tmp_path
) -> None:
    calls: list[list[str]] = []
    launcher = _launcher(repo, _state(tmp_path), _recording_runner(calls, [nothing]))
    line = _line(repo)

    with pytest.raises(NoNewCommitError, match="no_new_commit") as caught:
        await _run(line, launcher)

    assert len(calls) == 1
    assert caught.value.branch_has_commits is False


async def test_a_fix_round_that_commits_nothing_stops_no_new_commit(
    repo, tmp_path
) -> None:
    """A run meant to clear review blockers must commit beyond its start head.

    The first run committed, the review blocked it, and the fix round found
    the tree clean with nothing left to change. Reading that as a submission
    (N49) resubmitted the head the review had already refused, so the line
    went round the same review again; N34's guard stands for a fix round.
    """
    calls: list[list[str]] = []
    launcher = _launcher(
        repo,
        _state(tmp_path),
        _recording_runner(calls, [leave_dirty, commit_it, nothing]),
    )
    line = _line(repo)
    first = await _run(line, launcher)
    head = _rev(repo, line.branch)
    assert first.head == head and head != line.base_commit

    # The review blocked the line; the fix round changes nothing.
    line.current_run = f"{DISPATCH_ID}:line-n:0:fix"
    with pytest.raises(NoNewCommitError, match="no_new_commit") as caught:
        await _run(line, launcher, fix_round=True)

    assert len(calls) == 3  # no commit prompt: the tree was already clean
    assert caught.value.branch_has_commits is True
    assert caught.value.head == head
    # The branch is preserved, exactly as before N49.
    assert _rev(repo, line.branch) == head


async def test_a_fix_round_that_commits_submits(repo, tmp_path) -> None:
    """The guard is on the run that did nothing, not on fix rounds as such."""
    calls: list[list[str]] = []
    launcher = _launcher(
        repo,
        _state(tmp_path),
        _recording_runner(calls, [leave_dirty, commit_it, commit_more]),
    )
    line = _line(repo)
    await _run(line, launcher)
    head = _rev(repo, line.branch)

    line.current_run = f"{DISPATCH_ID}:line-n:0:fix"
    fixed = await _run(line, launcher, fix_round=True)

    assert (fixed.start_head, fixed.head) == (head, _rev(repo, line.branch))
    assert fixed.head != head


class _RecordingWorkers:
    """Scripted launches that also record the keywords the machine passes."""

    def __init__(self, script: list[tuple[str, str]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, object]] = []

    async def launch(self, line, model, skills):
        raise AssertionError("the machine uses launch_worker")

    async def launch_worker(self, line, model, skills, **kwargs):
        start, head = self.script.pop(0)
        self.calls.append(dict(kwargs))
        return LaunchResult(
            line.current_run or f"run-{len(self.calls)}",
            None,
            "committed",
            start_head=start,
            head=head,
        )


async def test_the_machine_tells_the_launcher_which_run_is_a_fix_round(tmp_path) -> None:
    """The launcher cannot see the review, so the machine names the run."""
    launcher = _RecordingWorkers([(BASE, H1), (H1, H2)])
    reviewer = BlockOnce()
    collaborators, machine, _commands, _trees = _bundle(
        tmp_path, [], launcher=launcher, reviewer=reviewer
    )
    collaborators.gates.handoff_facts = _handoff

    result = await machine.run(_dispatch("d-n49-fixround"))

    # The first launch is the line's own work; the second clears the blockers
    # the review raised, and only that one is marked.
    assert [call.get("fix_round") for call in launcher.calls] == [None, True]
    assert result.line("line-x").status is LineStatus.PASSED


class _EmptyFixWorkers:
    """A worker that commits on its first run and nothing on the fix round.

    The launcher's contract restated: a run that leaves the branch where it
    found it is a submission of the work already there, unless the machine
    said the run is a fix round -- then it is ``no_new_commit``.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def launch(self, line, model, skills):
        raise AssertionError("the machine uses launch_worker")

    async def launch_worker(self, line, model, skills, **kwargs):
        self.calls.append(dict(kwargs))
        run_id = line.current_run or f"run-{len(self.calls)}"
        if len(self.calls) == 1:
            return LaunchResult(
                run_id, None, "committed", start_head=BASE, head=H1
            )
        if kwargs.get("fix_round"):
            error = NoNewCommitError(f"no_new_commit for line {line.id}", run_id=run_id)
            error.branch_has_commits = True
            error.head = H1
            raise error
        return LaunchResult(run_id, None, "committed", start_head=H1, head=H1)


async def test_an_empty_fix_round_stops_the_line_instead_of_a_second_review(
    tmp_path,
) -> None:
    """The R8c shape: a fix round that ignored the blockers is surfaced.

    Reading the unchanged head as a submission sent it back through G2 and
    the same review, which refused it again; the line now stops
    ``no_new_commit`` with its branch preserved, and the head the review
    refused is never resubmitted.
    """
    launcher = _EmptyFixWorkers()
    reviewer = BlockOnce()
    collaborators, machine, _commands, _trees = _bundle(
        tmp_path, [], launcher=launcher, reviewer=reviewer
    )
    collaborators.gates.handoff_facts = _handoff

    result = await machine.run(_dispatch("d-n49-empty-fix"))

    assert [call.get("fix_round") for call in launcher.calls] == [None, True]
    line = result.line("line-x")
    assert line is not None
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "no_new_commit"
    assert line.blockers == ["Worker exited without creating a commit; line branch preserved"]
    # Only the first submission was reviewed; H1 did not go back to review.
    assert len(reviewer.reviewed) == 1
