"""N34: an empty retry goes on to review only with its own author's unsubmitted commits.

The real case: author A's first run committed the work, then exited 75 (rate
limited), and A's automatic retry found nothing left to change. That retry
exits 0 with no new commit, and the branch head A committed goes on to G2 and
review.

A first run with no commit, a fix round that changed nothing after a BLOCK (a
rejected commit is never resubmitted), and a branch git does not call ahead
still block with ``no_new_commit``. N38 (test_n38_unreviewed_head.py) replaced
the "current author's chain" rule: any never-reviewed HEAD goes on, and every
model that ever held the line is kept off its review.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.llms.native_cli.failure import FailureClass
from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch.execution import LaunchResult, WorkerLaunchFailure
from src.tools.auto_dispatch.protocols import (
    LaunchError,
    NoNewCommitError,
    UncommittedChangesError,
)
from src.tools.auto_dispatch.types import Verdict
from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

BASE = "b" * 40
H1 = "1" * 40
H2 = "2" * 40
H9 = "9" * 40
NO_COMMIT_BLOCKER = "Worker exited without creating a commit; line branch preserved"


class ScriptedWorkers:
    """One launch outcome per call, each with the branch heads it reports.

    ``("ok", start, head)`` returns; ``("rate_limited", start, head)`` raises a
    rate-limit failure after the run left ``head``; ``("empty", head, ahead)``
    raises NoNewCommitError at ``head``; ``ahead`` is git's answer to "is the
    branch past base_commit".
    """

    def __init__(self, script: list[tuple[object, ...]]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str, str]] = []

    async def launch(self, line, model, skills):
        raise AssertionError("the recovery machine uses launch_worker")

    async def launch_worker(self, line, model, skills, *, session_id, session_mode, handoff_path):
        step = self.script.pop(0)
        kind = str(step[0])
        self.calls.append((line.id, model, kind))
        run_id = line.current_run or f"run-{len(self.calls)}"
        if kind == "ok":
            _kind, start, head = step
            return LaunchResult(run_id, session_id, "committed", start_head=start, head=head)
        if kind == "rate_limited":
            _kind, start, head = step
            failure = WorkerLaunchFailure(
                run_id=run_id,
                exit_code=75,
                output="HTTP 429 rate limited",
                session_id=None,
                retry_after_seconds=0,
                failure_class=FailureClass.RATE_LIMITED,
                reason_code="worker_rate_limited",
            )
            failure.start_head = start
            failure.head = head
            raise failure
        if kind == "exit1":
            # ("exit1", start, head): exit 1 after the run moved the branch.
            # start/head None models a run whose heads were never known.
            _kind, start, head = step
            error = LaunchError(f"{model} exited 1 for line {line.id}")
            error.worker_started = True
            error.run_id = run_id
            error.exit_code = 1
            error.start_head = start
            error.head = head
            raise error
        if kind == "dirty":
            _kind, start, head = step
            error = UncommittedChangesError(f"uncommitted_changes for line {line.id}", run_id=run_id)
            error.start_head = start
            error.head = head
            raise error
        if kind == "empty":
            _kind, head, ahead = step
            error = NoNewCommitError(f"no_new_commit for line {line.id}", run_id=run_id)
            error.branch_has_commits = ahead
            error.head = head
            raise error
        raise AssertionError(kind)


class BlockOnce:
    """The first review asks for a fix; every later review passes."""

    def __init__(self) -> None:
        self.reviewed: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.reviewed.append((line_id, model))
        if len(self.reviewed) == 1:
            return Verdict.blocked(["add a regression test"])
        return Verdict.ok(f"reviewed by {model}")


class RecordingReviewer:
    def __init__(self) -> None:
        self.reviewed: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.reviewed.append((line_id, model))
        return Verdict.ok(f"reviewed by {model}")


def _handoff(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "branch": "feature/line-x",
        "commit": "abc123",
        "files": ("a.py",),
        "test_command": "pytest",
        "results": "pass",
        "known_limitations": "none",
    }


def _dispatch(dispatch_id: str, *, model: str = "kimi") -> Dispatch:
    line = _line("line-x", assigned_model=model, status=LineStatus.ASSIGNED)
    line.branch = f"feature/auto-dispatch-{dispatch_id}/line-line-x"
    line.base_commit = BASE
    return Dispatch(
        id=dispatch_id,
        goal="ship the line",
        user_id="u",
        created_at="now",
        lines=[line],
        state=DispatchState.LAUNCHING,
    )


def _seed_runs(gates, dispatch: Dispatch, runs: list[dict[str, object]]) -> None:
    """Prepare the Stage-Gate snapshot first; a later prepare would wipe the seed."""
    line = dispatch.line("line-x")
    assert line is not None
    gates.prepare(dispatch)
    dispatch.gates["worker_recovery"] = {
        "line-x": {
            "attempts": {},
            "session_id": None,
            "session_mode": None,
            "runs": [{"branch": line.branch, "attempt": 0, **run} for run in runs],
        }
    }


def _recovery_events(result: Dispatch) -> list[tuple[object, object]]:
    events = result.gates["worker_recovery"]["line-x"]["events"]
    return [(event["failure_class"], event["action"]) for event in events]


def _seeded_authors(result: Dispatch) -> list[object]:
    runs = result.gates["worker_recovery"]["line-x"].get("runs") or []
    return [run.get("author") for run in runs]


def _assert_blocked(result: Dispatch, reviewer) -> None:
    line = result.line("line-x")
    assert line is not None
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "no_new_commit"
    assert line.blockers == [NO_COMMIT_BLOCKER]


def test_author_commits_then_is_rate_limited_then_an_empty_retry_goes_to_review(
    tmp_path: Path,
) -> None:
    """The R2b case."""

    async def scenario() -> None:
        launcher = ScriptedWorkers([("rate_limited", BASE, H1), ("empty", H1, True)])
        collaborators, machine, _commands, _trees = _bundle(tmp_path, [], launcher=launcher)

        result = await machine.run(_dispatch("d-n34-r2b"))

        line = result.line("line-x")
        assert line is not None
        assert [kind for _id, _model, kind in launcher.calls] == ["rate_limited", "empty"]
        assert _recovery_events(result) == [("rate_limited", "retry_same")]
        assert line.status is LineStatus.PASSED
        snap = collaborators.gates.line_snapshot(result, line)
        assert snap["gates"]["G2"]["result"] == "pass"
        assert snap["gates"]["primary_review"]["result"] == "pass"
        runs = result.gates["worker_recovery"]["line-x"]["runs"]
        assert [(run["author"], run["head"], run["submitted"]) for run in runs] == [
            ("kimi", H1, False),
            ("kimi", H1, True),
        ]

    asyncio.run(scenario())


def test_a_fix_round_that_commits_then_is_rate_limited_then_retries_empty_goes_to_review(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        launcher = ScriptedWorkers(
            [("ok", BASE, H1), ("rate_limited", H1, H2), ("empty", H2, True)]
        )
        reviewer = BlockOnce()
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, reviewer=reviewer
        )
        collaborators.gates.handoff_facts = _handoff
        # Git names the line branch's HEAD as it moves (BASE, H1, H2, H2): the
        # fake repo has no line branch, and a HEAD git cannot name would leave
        # the reviewed-heads ledger incomplete.
        heads = [BASE, H1, H2, H2]
        collaborators.gates._revision = lambda _line: heads[len(launcher.calls)]

        result = await machine.run(_dispatch("d-n34-fix"))

        line = result.line("line-x")
        assert line is not None
        assert [kind for _id, _model, kind in launcher.calls] == ["ok", "rate_limited", "empty"]
        assert line.status is LineStatus.PASSED
        assert len(reviewer.reviewed) == 2

    asyncio.run(scenario())


def test_an_empty_fix_round_after_a_block_never_resubmits_the_rejected_commit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        launcher = ScriptedWorkers([("ok", BASE, H1), ("empty", H1, True)])
        reviewer = BlockOnce()
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, reviewer=reviewer
        )
        collaborators.gates.handoff_facts = _handoff

        result = await machine.run(_dispatch("d-n34-rejected"))

        assert [kind for _id, _model, kind in launcher.calls] == ["ok", "empty"]
        _assert_blocked(result, reviewer)
        # Only the first submission was reviewed; H1 did not go back to review.
        assert len(reviewer.reviewed) == 1

    asyncio.run(scenario())


def test_git_saying_the_branch_is_not_ahead_blocks(tmp_path: Path) -> None:
    async def scenario() -> None:
        launcher = ScriptedWorkers([("empty", H1, False)])
        collaborators, machine, _commands, _trees = _bundle(tmp_path, [], launcher=launcher)
        dispatch = _dispatch("d-n34-behind")
        _seed_runs(collaborators.gates, dispatch, [{"author": "kimi", "start": BASE, "head": H1, "submitted": False}])

        result = await machine.run(dispatch)

        assert _seeded_authors(result) == ["kimi"]
        _assert_blocked(result, None)

    asyncio.run(scenario())


def test_a_first_run_with_no_commit_at_all_still_blocks(tmp_path: Path) -> None:
    async def scenario() -> None:
        launcher = ScriptedWorkers([("empty", BASE, False)])
        reviewer = RecordingReviewer()
        _collaborators, machine, commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, reviewer=reviewer
        )

        result = await machine.run(_dispatch("d-n34-never"))

        assert len(launcher.calls) == 1
        _assert_blocked(result, reviewer)
        assert reviewer.reviewed == []
        assert commands.calls == []

    asyncio.run(scenario())


def test_a_previous_author_of_branch_commits_is_never_the_reviewer(tmp_path: Path) -> None:
    async def scenario() -> None:
        # Find who the selector would pick first for this line.
        probe = RecordingReviewer()
        _c, probe_machine, _cmd, _t = _bundle(
            tmp_path / "probe", [], launcher=ScriptedWorkers([("ok", BASE, H1)]), reviewer=probe
        )
        await probe_machine.run(_dispatch("d-n34-probe"))
        first_choice = probe.reviewed[0][1]
        assert first_choice != "kimi"

        # That model committed on the branch earlier; it must not review it.
        reviewer = RecordingReviewer()
        c2, machine, _cmd2, _t2 = _bundle(
            tmp_path / "real", [], launcher=ScriptedWorkers([("ok", H1, H2)]), reviewer=reviewer
        )
        dispatch = _dispatch("d-n34-reviewer")
        _seed_runs(
            c2.gates,
            dispatch, [{"author": first_choice, "start": BASE, "head": H1, "submitted": True}]
        )

        result = await machine.run(dispatch)

        assert result.line("line-x").status is LineStatus.PASSED
        assert _seeded_authors(result)[0] == first_choice
        assert reviewer.reviewed
        assert all(model not in (first_choice, "kimi") for _id, model in reviewer.reviewed)

    asyncio.run(scenario())



def test_an_exit_one_run_and_a_dirty_run_record_their_heads(tmp_path: Path) -> None:
    """Failure paths with a known end HEAD keep the chain recorded."""

    async def scenario() -> None:
        # The machine retries after an exit 1; that retry commits again.
        launcher = ScriptedWorkers([("exit1", BASE, H1), ("ok", H1, H2)])
        _collaborators, machine, _commands, _trees = _bundle(tmp_path / "exit", [], launcher=launcher)
        result = await machine.run(_dispatch("d-n34-exit1"))
        runs = result.gates["worker_recovery"]["line-x"]["runs"]
        assert {"author": "kimi", "start": BASE, "head": H1, "submitted": False}.items() <= runs[0].items()

        dirty = ScriptedWorkers([("dirty", BASE, H1)])
        _c2, machine2, _cmd2, _t2 = _bundle(tmp_path / "dirty", [], launcher=dirty)
        blocked = await machine2.run(_dispatch("d-n34-dirty"))
        assert blocked.line("line-x").failure_reason == "uncommitted_changes"
        runs = blocked.gates["worker_recovery"]["line-x"]["runs"]
        assert [(run["start"], run["head"], run["submitted"]) for run in runs] == [(BASE, H1, False)]

    asyncio.run(scenario())
