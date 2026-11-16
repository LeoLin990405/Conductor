"""N38: an empty run sends a never-reviewed branch head on to review.

The real case (R4 line-0): grok's commit fixed the review findings, then the
line was parked replan_exhausted. A human reassigned it to cursor, which found
the fix already on the branch and made no commit. N34's "current author's
chain" rule blocked that empty run as ``no_new_commit``, although the head
had never been reviewed.

The rule now: an exit-0 run with no new commit goes on to G2 and review when
git says the branch is strictly ahead of base and that HEAD was never
submitted to G2 or review for the line. A reviewed (and rejected) HEAD still
blocks. Commits are not attributed. Instead, every model that ever held the
line is kept off its review. When that leaves no reviewer, the line parks
with ``no_eligible_reviewer`` rather than erroring the dispatch.
"""

from __future__ import annotations

import asyncio

import pytest
from pathlib import Path

from src.tools.auto_dispatch import DispatchState, LineStatus, RoundRobinSelector
from src.tools.auto_dispatch import machine as machine_module
from src.tools.auto_dispatch.machine import (
    NO_ELIGIBLE_REVIEWER,
    REVIEWED_HEADS,
    apply_human_reassign,
)
from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle
from tests.unit.tools.auto_dispatch.test_n34_committed_retry import (
    BASE,
    H1,
    H2,
    NO_COMMIT_BLOCKER,
    BlockOnce,
    RecordingReviewer,
    ScriptedWorkers,
    _dispatch,
    _handoff,
    _seed_runs,
)

H3 = "3" * 40
H0 = "0" * 40


def _ledger(result) -> dict:
    line = result.line("line-x")
    return result.gates[REVIEWED_HEADS]["line-x"][line.branch]


def _seed_ledger(dispatch, heads: list[str], *, complete: bool = True) -> None:
    """The reviewed-heads ledger a line opened before its branch's first run."""
    line = dispatch.line("line-x")
    assert line is not None
    dispatch.gates[REVIEWED_HEADS] = {
        "line-x": {line.branch: {"complete": complete, "heads": list(heads)}}
    }


def _first_reviewer(tmp_path: Path, author: str) -> str:
    """Who the selector would pick first to review ``author``'s work."""
    probe = RecordingReviewer()

    async def scenario() -> str:
        _c, machine, _cmd, _t = _bundle(
            tmp_path, [], launcher=ScriptedWorkers([("ok", BASE, H1)]), reviewer=probe
        )
        await machine.run(_dispatch("d-n38-probe", model=author))
        return probe.reviewed[0][1]

    return asyncio.run(scenario())


def test_the_r4_case_a_reassigned_empty_run_sends_the_fix_to_review(tmp_path: Path) -> None:
    """A commits the fix, the line is replan_exhausted, B's empty run goes to review."""
    b = "stepfun"
    a = _first_reviewer(tmp_path / "probe", b)
    assert a != b

    async def scenario() -> None:
        reviewer = RecordingReviewer()
        launcher = ScriptedWorkers([("empty", H1, True)])
        collaborators, machine, commands, _trees = _bundle(
            tmp_path / "r4", [], launcher=launcher, reviewer=reviewer
        )
        gates = collaborators.gates
        gates.handoff_facts = _handoff
        dispatch = _dispatch("d-n38-r4", model=a)
        line = dispatch.line("line-x")
        assert line is not None
        # A's first commit was reviewed and blocked; A's fix commit H1 never was.
        _seed_runs(
            gates,
            dispatch,
            [
                {"author": a, "start": BASE, "head": "0" * 40, "submitted": True},
                {"author": a, "start": "0" * 40, "head": H1, "submitted": False},
            ],
        )
        _seed_ledger(dispatch, [H0])
        snap = gates.line_snapshot(dispatch, line)
        snap["machine"].update(
            stage="replanning",
            replan_options=["resplit_task", "replace_developer"],
            author_provider=a,
            developer_provider=a,
            contributors=[a],
            fix_rounds=2,
        )
        line.status = LineStatus.BLOCKED
        line.failure_reason = "replan_exhausted"
        assert await apply_human_reassign(dispatch, line, b, None, gates) is None

        result = await machine.run(dispatch)

        out = result.line("line-x")
        assert out is not None
        assert [(model, kind) for _id, model, kind in launcher.calls] == [(b, "empty")]
        assert out.status is LineStatus.PASSED
        assert out.failure_reason != "no_new_commit"
        assert commands.calls  # G2 ran on H1
        assert reviewer.reviewed
        # Neither the holder who wrote the fix nor the one who sent it reviews it.
        assert all(model not in (a, b) for _id, model in reviewer.reviewed)

    asyncio.run(scenario())


def test_an_unrecorded_commit_still_goes_to_review_with_every_holder_excluded(
    tmp_path: Path,
) -> None:
    """No record vouches for H2's commits; it was never reviewed, so it goes on."""
    first = _first_reviewer(tmp_path / "probe", "kimi")

    async def scenario() -> None:
        reviewer = RecordingReviewer()
        launcher = ScriptedWorkers([("empty", H2, True)])
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path / "gap", [], launcher=launcher, reviewer=reviewer
        )
        dispatch = _dispatch("d-n38-gap")
        # The first reviewer once held this line (its run left H1 unsubmitted).
        _seed_runs(
            collaborators.gates,
            dispatch,
            [{"author": first, "start": H1, "head": H1, "submitted": False}],
        )
        _seed_ledger(dispatch, [])

        result = await machine.run(dispatch)

        assert result.line("line-x").status is LineStatus.PASSED
        assert reviewer.reviewed
        assert all(model not in (first, "kimi") for _id, model in reviewer.reviewed)

    asyncio.run(scenario())


def test_a_rejected_head_and_an_empty_run_still_block(tmp_path: Path) -> None:
    async def scenario() -> None:
        launcher = ScriptedWorkers([("ok", BASE, H1), ("empty", H1, True)])
        reviewer = BlockOnce()
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, reviewer=reviewer
        )
        collaborators.gates.handoff_facts = _handoff

        result = await machine.run(_dispatch("d-n38-rejected"))

        line = result.line("line-x")
        assert line is not None
        assert line.status is LineStatus.BLOCKED
        assert line.failure_reason == "no_new_commit"
        assert line.blockers == [NO_COMMIT_BLOCKER]
        assert len(reviewer.reviewed) == 1

    asyncio.run(scenario())


def test_the_three_holder_sequence_reviews_and_excludes_every_holder(tmp_path: Path) -> None:
    """Z authors -> Y (commits unrecorded) -> X; X's rate-limited empty retry goes to
    review, and none of X, Y, Z reviews it."""
    x = "stepfun"
    y = _first_reviewer(tmp_path / "probe", x)
    z = "deepseek"
    assert len({x, y, z}) == 3

    async def scenario() -> None:
        reviewer = RecordingReviewer()
        launcher = ScriptedWorkers([("rate_limited", H1, H2), ("empty", H2, True)])
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path / "seq", [], launcher=launcher, reviewer=reviewer
        )
        gates = collaborators.gates
        gates.handoff_facts = _handoff
        dispatch = _dispatch("d-n38-seq", model=z)
        line = dispatch.line("line-x")
        assert line is not None
        _seed_runs(gates, dispatch, [{"author": z, "start": BASE, "head": "0" * 40, "submitted": True}])
        _seed_ledger(dispatch, [H0])
        snap = gates.line_snapshot(dispatch, line)
        snap["machine"].update(
            stage="fixing", author_provider=z, developer_provider=z,
            contributors=[z], return_stage="primary_review",
        )
        line.status = LineStatus.BLOCKED
        assert await apply_human_reassign(dispatch, line, y, None, gates) is None
        line.status = LineStatus.BLOCKED
        snap["machine"]["stage"] = "fixing"
        assert await apply_human_reassign(dispatch, line, x, None, gates) is None

        result = await machine.run(dispatch)

        assert result.line("line-x").status is LineStatus.PASSED
        assert reviewer.reviewed
        assert all(model not in (x, y, z) for _id, model in reviewer.reviewed)

    asyncio.run(scenario())


def test_no_eligible_reviewer_parks_the_line_with_a_reason(tmp_path: Path) -> None:
    async def scenario() -> None:
        reviewer = RecordingReviewer()
        # The only reviewers in the pool both held the line.
        selector = RoundRobinSelector(pool=("kimi", "deepseek"))
        launcher = ScriptedWorkers([("empty", H1, True)])
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, reviewer=reviewer, selector=selector
        )
        dispatch = _dispatch("d-n38-none")
        _seed_runs(
            collaborators.gates,
            dispatch,
            [{"author": "deepseek", "start": BASE, "head": H1, "submitted": False}],
        )
        _seed_ledger(dispatch, [])
        collaborators.gates.handoff_facts = _handoff

        result = await machine.run(dispatch)

        line = result.line("line-x")
        assert line is not None
        assert result.state is DispatchState.REVIEW
        assert line.status is LineStatus.BLOCKED
        assert line.failure_reason == NO_ELIGIBLE_REVIEWER
        assert "deepseek" in line.blockers[0] and "kimi" in line.blockers[0]
        # The blocker names the action that works, and that action is accepted.
        assert f"conductor dispatch reassign {result.id} line-x <model>" in line.blockers[0]
        assert reviewer.reviewed == []
        snap = collaborators.gates.line_snapshot(result, line)
        assert snap["machine"]["stage"] == "blocked"
        assert await apply_human_reassign(result, line, "glm", None, collaborators.gates) is None
        assert line.status is LineStatus.ASSIGNED and line.assigned_model == "glm"

    asyncio.run(scenario())


def test_an_evicted_run_record_cannot_resubmit_a_rejected_head(tmp_path, monkeypatch) -> None:
    """Reviewer's probe: run records capped at 2, H1 rejected, two rate limits at H1."""
    monkeypatch.setattr(machine_module, "_MAX_RUN_RECORDS", 2)

    async def scenario() -> None:
        launcher = ScriptedWorkers(
            [
                ("ok", BASE, H1),
                ("rate_limited", H1, H1),
                ("rate_limited", H1, H1),
                ("empty", H1, True),
                ("empty", H1, True),
            ]
        )
        reviewer = BlockOnce()
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, reviewer=reviewer
        )
        collaborators.gates.handoff_facts = _handoff

        result = await machine.run(_dispatch("d-n38-evict"))

        line = result.line("line-x")
        assert line is not None
        runs = result.gates["worker_recovery"]["line-x"]["runs"]
        assert all(not run["submitted"] for run in runs)  # H1's submission was evicted
        assert H1 in _ledger(result)["heads"]
        assert len(reviewer.reviewed) == 1
        assert line.status is LineStatus.BLOCKED
        assert line.failure_reason == "no_new_commit"

    asyncio.run(scenario())


def test_a_restored_running_line_has_no_complete_ledger_and_fails_closed(tmp_path: Path) -> None:
    """A run finished via the monitor (restart) left no run record; its HEAD
    went to review, so an empty fix round must not send it again."""

    async def scenario() -> None:
        launcher = ScriptedWorkers([("empty", H1, True)])
        reviewer = BlockOnce()
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, reviewer=reviewer
        )
        gates = collaborators.gates
        gates.handoff_facts = _handoff
        dispatch = _dispatch("d-n38-restored")
        line = dispatch.line("line-x")
        assert line is not None
        line.status = LineStatus.RUNNING
        line.current_run = "run-before-restart"
        dispatch.state = DispatchState.MONITORING
        gates.prepare(dispatch)
        gates.line_snapshot(dispatch, line)["machine"].update(
            stage="implementing", author_provider="kimi", developer_provider="kimi",
            contributors=["kimi"],
        )

        result = await machine.run(dispatch)

        out = result.line("line-x")
        assert out is not None
        assert [kind for _id, _model, kind in launcher.calls] == ["empty"]
        assert len(reviewer.reviewed) == 1
        assert out.status is LineStatus.BLOCKED
        assert out.failure_reason == "no_new_commit"
        assert _ledger(result)["complete"] is False

    asyncio.run(scenario())


def test_ungated_review_parks_no_eligible_reviewer_instead_of_error(tmp_path: Path) -> None:
    from src.tools.auto_dispatch import (
        Collaborators,
        Dispatch,
        DispatchMachine,
        FileApprovalGate,
        InMemoryDispatchStore,
        InMemoryOutcomeStore,
        NoopMerger,
        RecordingLauncher,
        ScriptedMonitor,
    )
    from tests.unit.tools.auto_dispatch.test_machine_gates import Planner, _line

    async def no_sleep(_seconds: float) -> None:
        return None

    async def scenario() -> None:
        line = _line("line-x", assigned_model="kimi", status=LineStatus.ASSIGNED)
        line.branch = "feature/auto-dispatch-d-n38-ungated/line-line-x"
        collaborators = Collaborators(
            planner=Planner([]),
            selector=RoundRobinSelector(pool=("kimi", "deepseek")),
            launcher=RecordingLauncher(),
            monitor=ScriptedMonitor(),
            reviewer=RecordingReviewer(),
            merger=NoopMerger(),
            gate=FileApprovalGate(tmp_path / "proposed"),
            outcomes=InMemoryOutcomeStore(),
            store=InMemoryDispatchStore(),
            gates=None,
        )
        machine = DispatchMachine(collaborators, sleep=no_sleep)
        dispatch = Dispatch(
            id="d-n38-ungated", goal="g", user_id="u", created_at="now",
            lines=[line], state=DispatchState.ASSIGNED,
            gates={"worker_recovery": {"line-x": {"attempts": {}, "runs": [
                {"author": "deepseek", "attempt": 0, "branch": line.branch, "start": "", "head": "",
                 "submitted": False},
            ]}}},
        )

        result = await machine.run(dispatch)

        out = result.line("line-x")
        assert out is not None
        assert result.state is not DispatchState.ERROR
        assert out.status is LineStatus.BLOCKED
        assert out.failure_reason == NO_ELIGIBLE_REVIEWER
        assert collaborators.reviewer.reviewed == []

    asyncio.run(scenario())


def test_the_availability_probe_neither_retries_nor_overwrites_the_refusal(tmp_path: Path) -> None:
    """Nobody can review at all: one probe call, no retry pause, and the real
    pick's refusal still decides the error."""
    from src.tools.auto_dispatch.selection import ModelRole

    class Selector:
        def __init__(self) -> None:
            self.review_refusal: str | None = None
            self.review_calls = 0

        async def select(self, line, used, *, role=None):
            if role is not ModelRole.REVIEW:
                return "kimi"
            self.review_calls += 1
            # The real pick (holders excluded) has no secondary reviewer; the
            # probe without holders reports something else. Both find nobody.
            self.review_refusal = "no_secondary_reviewer" if "deepseek" in used else "pool_empty"
            return None

    async def scenario() -> None:
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        selector = Selector()
        launcher = ScriptedWorkers([("empty", H1, True)])
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, selector=selector
        )
        machine._sleep = sleep
        collaborators.gates.handoff_facts = _handoff
        dispatch = _dispatch("d-n38-probe")
        _seed_runs(
            collaborators.gates,
            dispatch,
            [{"author": "deepseek", "start": BASE, "head": H1, "submitted": False}],
        )
        _seed_ledger(dispatch, [])

        result = await machine.run(dispatch)

        line = result.line("line-x")
        assert line is not None
        assert result.state is DispatchState.ERROR
        assert line.failure_reason == "no_secondary_reviewer"
        # Real pick: one try plus its retry. Probe: exactly one call.
        assert selector.review_calls == 3
        assert selector.review_refusal == "no_secondary_reviewer"

    asyncio.run(scenario())


@pytest.mark.parametrize("g2_commands", [True, False])
def test_a_head_reviewed_after_a_restart_is_never_resubmitted(
    tmp_path: Path, monkeypatch, g2_commands: bool
) -> None:
    """Reviewer's repro: the line opened its ledger under this code, the process
    restarted mid-run, the run finished via the monitor (no run record), G2 ran
    or was skipped, review rejected H1, and an empty fix run sits at H1."""
    import src.tools.auto_dispatch.gates as gates_module

    monkeypatch.setattr(gates_module, "git_head", lambda _worktree: H1)

    async def scenario() -> None:
        launcher = ScriptedWorkers([("empty", H1, True), ("empty", H1, True)])
        reviewer = BlockOnce()
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, reviewer=reviewer
        )
        gates = collaborators.gates
        gates.handoff_facts = _handoff
        gates._revision = lambda _line: H1
        dispatch = _dispatch(f"d-n38-restart-{g2_commands}")
        line = dispatch.line("line-x")
        assert line is not None
        if not g2_commands:
            line.acceptance = ()  # no G2 commands, not risk-bearing: G2 is skipped
        line.status = LineStatus.RUNNING
        line.current_run = "run-before-restart"
        dispatch.state = DispatchState.MONITORING
        gates.prepare(dispatch)
        gates.line_snapshot(dispatch, line)["machine"].update(
            stage="implementing", author_provider="kimi", developer_provider="kimi",
            contributors=["kimi"],
        )
        _seed_ledger(dispatch, [])

        result = await machine.run(dispatch)

        out = result.line("line-x")
        assert out is not None
        assert len(reviewer.reviewed) == 1, reviewer.reviewed
        assert H1 in _ledger(result)["heads"]
        assert out.status is LineStatus.BLOCKED
        assert out.failure_reason == "no_new_commit"

    asyncio.run(scenario())


def test_a_reused_branch_name_keeps_its_reviewed_heads(tmp_path: Path) -> None:
    from src.tools.auto_dispatch.machine import _open_reviewed_ledger
    from src.tools.auto_dispatch.protocols import NoNewCommitError

    collaborators, machine, _commands, _trees = _bundle(tmp_path, [], launcher=ScriptedWorkers([]))
    dispatch = _dispatch("d-n38-reuse")
    line = dispatch.line("line-x")
    assert line is not None
    collaborators.gates.prepare(dispatch)
    reused = f"{dispatch.branch_prefix}/line-line-x-a1"
    line.branch = reused
    _open_reviewed_ledger(dispatch, line, complete=True)
    machine._note_reviewed_head(dispatch, line, H1)
    # The line moves on, then an automatic reassign lands on the same name.
    line.branch = f"{dispatch.branch_prefix}/line-line-x-a2"
    _open_reviewed_ledger(dispatch, line, complete=True)
    line.branch = reused
    _open_reviewed_ledger(dispatch, line, complete=True)

    error = NoNewCommitError("no_new_commit for line line-x", branch_has_commits=True)
    error.head = H1
    assert H1 in _ledger(dispatch)["heads"]
    assert machine._unreviewed_head_continues(dispatch, line, error) is False


def test_a_reassigned_old_line_with_cleared_cells_is_not_fresh(tmp_path: Path) -> None:
    collaborators, machine, _commands, _trees = _bundle(tmp_path, [], launcher=ScriptedWorkers([]))
    dispatch = _dispatch("d-n38-old")
    line = dispatch.line("line-x")
    assert line is not None
    collaborators.gates.prepare(dispatch)
    snap = collaborators.gates.line_snapshot(dispatch, line)
    snap["machine"]["stage"] = "assigned"  # after the human swap
    snap["gates"]["G2"] = {"result": "pending", "reason": None, "at": None, "commands": []}
    snap["gates"]["primary_review"] = None
    snap["gates"]["secondary_review"] = None
    dispatch.outcomes.clear()
    line.attempts = 1  # every reassign raises the attempt
    assert machine._branch_is_fresh(dispatch, line) is False
    line.attempts = 0
    assert machine._branch_is_fresh(dispatch, line) is True


def test_a_review_head_git_cannot_name_makes_the_ledger_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.tools.auto_dispatch.machine import _open_reviewed_ledger

    collaborators, machine, _commands, _trees = _bundle(tmp_path, [], launcher=ScriptedWorkers([]))
    dispatch = _dispatch("d-n38-nogit")
    line = dispatch.line("line-x")
    assert line is not None
    collaborators.gates.prepare(dispatch)
    _open_reviewed_ledger(dispatch, line, complete=True)
    # Git cannot name the HEAD under review; the newest run record may be an
    # older run's, so it is written but never keeps the ledger complete.
    monkeypatch.setattr(collaborators.gates, "_revision", lambda _line: None)
    monkeypatch.setattr(machine, "_latest_run_head", lambda _d, _l: H3)
    asyncio.run(machine._note_head_under_review(dispatch, line))
    ledger = _ledger(dispatch)
    assert H3 in ledger["heads"]
    assert ledger["complete"] is False
