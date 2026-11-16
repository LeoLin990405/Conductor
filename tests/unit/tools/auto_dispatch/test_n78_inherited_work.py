"""N78: a replan child's empty run delivers the inherited branch tip to review.

The prod case (2026-11-12, dispatches d3205b52, 7ddaa34a, 1302ef75): a replan
child ``line-N-r1-M`` is born on its own new branch. Its first author commits
the work, then the line is reassigned (operator reassign, replace_developer,
or a rate-limit move). The new author finds the work already complete,
verifies it and exits 0 with a clean tree and no new commit. N38 should send
that never-reviewed tip on to G2 and review, but the child's reviewed-heads
ledger was opened incomplete at its first launch: the replan count the child
carries from its parent read as "not fresh". N38 failed closed and the line
parked ``no_new_commit`` with nothing ever reviewing the work.

Now a child on its birth branch opens a complete ledger, a pre-N78 child
ledger (no recorded doubt) still vouches for an unlisted head, and the
delivery is recorded as ``delivered_inherited_tip``. A branch equal to its
base, a tip already reviewed and blocked, a nonzero exit and a dirty tree
still stop the line as before.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.tools.auto_dispatch import LineStatus
from src.tools.auto_dispatch.machine import (
    DELIVERED_INHERITED_TIP,
    LEDGER_DOUBT_HEAD_UNKNOWN,
    LEDGER_DOUBT_NOT_FRESH,
    REVIEWED_HEADS,
    apply_human_reassign,
)
from src.tools.auto_dispatch.protocols import NoNewCommitError
from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle
from tests.unit.tools.auto_dispatch.test_n34_committed_retry import (
    BASE,
    H1,
    NO_COMMIT_BLOCKER,
    BlockOnce,
    RecordingReviewer,
    ScriptedWorkers,
    _dispatch,
    _handoff,
)

A = "kimi"
B = "stepfun"


def _as_replan_child(gates, dispatch) -> None:
    """Make ``line-x`` a replan child of ``line-p`` on its birth branch."""
    line = dispatch.line("line-x")
    assert line is not None
    assert line.branch == f"{dispatch.branch_prefix}/line-{line.id}"
    gates.prepare(dispatch)
    snap = gates.line_snapshot(dispatch, line)
    snap["replans"] = 1
    snap["replanned_from"] = "line-p"


def _ledger(dispatch) -> dict:
    line = dispatch.line("line-x")
    return dispatch.gates[REVIEWED_HEADS]["line-x"][line.branch]


def _seed_handoff(gates, dispatch, ledger: dict, *, start: str = BASE) -> None:
    """A committed H1 on the child's branch, never submitted; then a human reassign to B."""
    line = dispatch.line("line-x")
    dispatch.gates["worker_recovery"] = {
        "line-x": {
            "attempts": {},
            "session_id": None,
            "session_mode": None,
            "runs": [
                {
                    "author": A,
                    "attempt": 0,
                    "branch": line.branch,
                    "start": start,
                    "head": H1,
                    "submitted": False,
                }
            ],
        }
    }
    dispatch.gates[REVIEWED_HEADS] = {"line-x": {line.branch: ledger}}
    snap = gates.line_snapshot(dispatch, line)
    # Parked where a human may reassign it (the N38 R4 checkpoint).
    snap["machine"].update(
        stage="replanning",
        replan_options=["resplit_task", "replace_developer"],
        author_provider=A,
        developer_provider=A,
        contributors=[A],
        fix_rounds=2,
    )
    line.status = LineStatus.BLOCKED
    line.failure_reason = "replan_exhausted"


def test_a_replan_child_handoff_goes_to_review_and_passes(tmp_path: Path) -> None:
    """(1) A commits on the child's branch, is rate limited; B's empty run is reviewed."""

    async def scenario() -> None:
        launcher = ScriptedWorkers([("rate_limited", BASE, H1), ("empty", H1, True)])
        reviewer = RecordingReviewer()
        collaborators, machine, commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, reviewer=reviewer
        )
        gates = collaborators.gates
        gates.handoff_facts = _handoff
        dispatch = _dispatch("d-n78-child", model=A)
        _as_replan_child(gates, dispatch)

        result = await machine.run(dispatch)

        line = result.line("line-x")
        assert line is not None
        assert [kind for _id, _model, kind in launcher.calls] == ["rate_limited", "empty"]
        assert line.failure_reason != "no_new_commit"
        assert line.status is LineStatus.PASSED
        assert commands.calls  # G2 ran on the inherited tip
        assert reviewer.reviewed
        # The child's ledger opened complete; only this harness's review,
        # which git cannot name a head for, made it incomplete afterwards.
        assert _ledger(result).get("doubt") == LEDGER_DOUBT_HEAD_UNKNOWN
        assert H1 in _ledger(result)["heads"]
        snap = gates.line_snapshot(result, line)
        assert snap["gates"]["G2"]["result"] == "pass"
        assert snap["gates"]["primary_review"]["result"] == "pass"
        delivered = snap[DELIVERED_INHERITED_TIP]
        assert delivered["reason"] == DELIVERED_INHERITED_TIP
        assert delivered["head"] == H1
        assert delivered["base"] == BASE

    asyncio.run(scenario())


def test_a_stuck_pre_n78_child_is_reviewed_after_a_human_reassign(tmp_path: Path) -> None:
    """(1b) The prod state: a child ledger opened incomplete with no doubt recorded."""

    async def scenario() -> None:
        launcher = ScriptedWorkers([("empty", H1, True)])
        reviewer = RecordingReviewer()
        collaborators, machine, commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, reviewer=reviewer
        )
        gates = collaborators.gates
        gates.handoff_facts = _handoff
        dispatch = _dispatch("d-n78-legacy", model=A)
        _as_replan_child(gates, dispatch)
        _seed_handoff(gates, dispatch, {"complete": False, "heads": []})
        line = dispatch.line("line-x")
        assert await apply_human_reassign(dispatch, line, B, None, gates) is None

        result = await machine.run(dispatch)

        out = result.line("line-x")
        assert out is not None
        assert [(model, kind) for _id, model, kind in launcher.calls] == [(B, "empty")]
        assert out.status is LineStatus.PASSED
        assert commands.calls
        assert reviewer.reviewed
        assert all(model not in (A, B) for _id, model in reviewer.reviewed)
        snap = gates.line_snapshot(result, out)
        assert snap[DELIVERED_INHERITED_TIP]["head"] == H1

    asyncio.run(scenario())


def test_a_child_branch_equal_to_its_base_still_parks(tmp_path: Path) -> None:
    """(2) Nothing on the branch: no_new_commit, no G2, no review."""

    async def scenario() -> None:
        launcher = ScriptedWorkers([("empty", BASE, False)])
        reviewer = RecordingReviewer()
        collaborators, machine, commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, reviewer=reviewer
        )
        dispatch = _dispatch("d-n78-equal", model=A)
        _as_replan_child(collaborators.gates, dispatch)

        result = await machine.run(dispatch)

        line = result.line("line-x")
        assert line is not None
        assert line.status is LineStatus.BLOCKED
        assert line.failure_reason == "no_new_commit"
        assert line.blockers == [NO_COMMIT_BLOCKER]
        assert reviewer.reviewed == []
        assert commands.calls == []
        snap = collaborators.gates.line_snapshot(result, line)
        assert DELIVERED_INHERITED_TIP not in snap

    asyncio.run(scenario())


def test_a_tip_reviewed_and_blocked_at_the_same_sha_still_parks(tmp_path: Path) -> None:
    """(3) The child's tip was reviewed and blocked; an empty run cannot resend it."""

    async def scenario() -> None:
        launcher = ScriptedWorkers([("ok", BASE, H1), ("empty", H1, True)])
        reviewer = BlockOnce()
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, reviewer=reviewer
        )
        collaborators.gates.handoff_facts = _handoff
        dispatch = _dispatch("d-n78-rejected", model=A)
        _as_replan_child(collaborators.gates, dispatch)

        result = await machine.run(dispatch)

        line = result.line("line-x")
        assert line is not None
        assert line.status is LineStatus.BLOCKED
        assert line.failure_reason == "no_new_commit"
        assert len(reviewer.reviewed) == 1
        assert H1 in _ledger(result)["heads"]

    asyncio.run(scenario())


def test_a_legacy_child_ledger_listing_the_tip_still_parks(tmp_path: Path) -> None:
    """(3b) A pre-N78 child ledger that lists the tip blocks, as does any recorded doubt."""

    async def scenario() -> None:
        for name, ledger, start in (
            ("listed", {"complete": False, "heads": [H1]}, BASE),
            (
                "doubt",
                {"complete": False, "heads": [], "doubt": LEDGER_DOUBT_HEAD_UNKNOWN},
                BASE,
            ),
            # The run records do not reach back to the branch's birth.
            ("unrooted", {"complete": False, "heads": []}, "0" * 40),
        ):
            launcher = ScriptedWorkers([("empty", H1, True)])
            reviewer = RecordingReviewer()
            collaborators, machine, commands, _trees = _bundle(
                tmp_path / name, [], launcher=launcher, reviewer=reviewer
            )
            gates = collaborators.gates
            gates.handoff_facts = _handoff
            dispatch = _dispatch(f"d-n78-{name}", model=A)
            _as_replan_child(gates, dispatch)
            _seed_handoff(gates, dispatch, ledger, start=start)
            line = dispatch.line("line-x")
            assert await apply_human_reassign(dispatch, line, B, None, gates) is None

            result = await machine.run(dispatch)

            out = result.line("line-x")
            assert out is not None
            assert out.status is LineStatus.BLOCKED, name
            assert out.failure_reason == "no_new_commit", name
            assert reviewer.reviewed == [], name
            assert commands.calls == [], name

    asyncio.run(scenario())


def test_a_nonzero_exit_without_a_commit_never_reaches_review(tmp_path: Path) -> None:
    """(4) Exit 1 on the inherited tip is a launch failure, not a delivery."""

    async def scenario() -> None:
        launcher = ScriptedWorkers([("exit1", H1, H1)] * 10)
        reviewer = RecordingReviewer()
        collaborators, machine, commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, reviewer=reviewer
        )
        gates = collaborators.gates
        gates.handoff_facts = _handoff
        dispatch = _dispatch("d-n78-exit1", model=A)
        _as_replan_child(gates, dispatch)

        result = await machine.run(dispatch)

        line = result.line("line-x")
        assert line is not None
        assert line.status is not LineStatus.PASSED
        assert reviewer.reviewed == []
        assert commands.calls == []
        assert DELIVERED_INHERITED_TIP not in gates.line_snapshot(result, line)
        runs = result.gates["worker_recovery"]["line-x"]["runs"]
        assert runs and not any(run["submitted"] for run in runs)

    asyncio.run(scenario())


def test_uncommitted_changes_are_unchanged(tmp_path: Path) -> None:
    """(5) A dirty tree on a child still parks uncommitted_changes."""

    async def scenario() -> None:
        launcher = ScriptedWorkers([("dirty", H1, H1)])
        reviewer = RecordingReviewer()
        collaborators, machine, commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, reviewer=reviewer
        )
        dispatch = _dispatch("d-n78-dirty", model=A)
        _as_replan_child(collaborators.gates, dispatch)

        result = await machine.run(dispatch)

        line = result.line("line-x")
        assert line is not None
        assert line.status is LineStatus.BLOCKED
        assert line.failure_reason == "uncommitted_changes"
        assert line.blockers == ["Worker worktree has uncommitted changes; preserved for recovery"]
        assert reviewer.reviewed == []
        assert commands.calls == []

    asyncio.run(scenario())


def test_only_a_child_on_its_birth_branch_is_fresh(tmp_path: Path) -> None:
    """A replanned line itself, or a child moved off its branch, is not fresh."""
    collaborators, machine, _commands, _trees = _bundle(tmp_path, [], launcher=ScriptedWorkers([]))
    gates = collaborators.gates
    dispatch = _dispatch("d-n78-fresh")
    line = dispatch.line("line-x")
    _as_replan_child(gates, dispatch)
    assert machine._branch_is_fresh(dispatch, line) is True
    snap = gates.line_snapshot(dispatch, line)
    snap["replanned_from"] = None  # the replanned parent carries the count too
    assert machine._branch_is_fresh(dispatch, line) is False
    snap["replanned_from"] = "line-p"
    line.branch = f"{dispatch.branch_prefix}/line-line-x-a1"
    assert machine._branch_is_fresh(dispatch, line) is False
    # A non-fresh child opens its ledger incomplete with a recorded doubt.
    machine._open_ledger_if_new_branch(dispatch, line)
    assert _ledger(dispatch) == {
        "complete": False,
        "heads": [],
        "doubt": LEDGER_DOUBT_NOT_FRESH,
    }
    error = NoNewCommitError("no_new_commit for line line-x", branch_has_commits=True)
    error.head = H1
    assert machine._unreviewed_head_continues(dispatch, line, error) is False
