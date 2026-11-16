"""N48: one replan per line, then the Planner; a split builds on passed work.

R8c line-0 was split into five sub-lines. Three ended ``no_new_commit`` and
one wrote its tests against stubs of a sibling's unmerged code, because every
child started from the dispatch base and had nothing of its dependency to
build on.

Two rules now. A line is replanned at most once: past that the machine stops
deciding and parks the line for the Planner, whose ruling -- resplit the task
or replace the developer -- is the only way on. And the children of the one
allowed replan start from the branch of a dependency that already passed, so
the dependency's code is in the worktree instead of stubbed. The
``depends_on`` edges decide which sibling: a line that waits on line-a needs
line-a's branch, not the branch of whichever sibling happened to pass last.
Every line of a dispatch starts from the same base, so two passed
dependencies sit on parallel branches and no commit the dispatch can name
carries both; such a line parks for the Planner instead of being split onto
a base that lacks half of what it waits on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.tools.auto_dispatch import LineStatus
from src.tools.auto_dispatch.machine import (
    REPLAN_PARKED,
    REPLAN_PARKED_MESSAGE,
    REPLAN_UNCOMBINED,
    REPLAN_UNCOMBINED_MESSAGE,
    apply_human_reassign,
)
from src.tools.auto_dispatch.types import Line, Verdict
from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line
from tests.unit.tools.auto_dispatch.test_n34_committed_retry import (
    BASE,
    H1,
    H2,
    ScriptedWorkers,
    _dispatch,
    _handoff,
)

pytestmark = pytest.mark.asyncio


class _ReplanPlanner:
    """Splits a replanned line into the given children; never plans a fresh goal."""

    def __init__(self, children: list[Line]) -> None:
        self._children = children
        self.calls: list[str] = []

    async def plan(self, goal: str) -> list[Line]:
        self.calls.append(goal)
        return list(self._children) if "Replan this line." in goal else []


class BlockThrice:
    """Blocks each line's first three reviews; the third sends it to replanning."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.reviewed: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.reviewed.append((line_id, model))
        self.counts[line_id] = self.counts.get(line_id, 0) + 1
        if self.counts[line_id] <= 3:
            return Verdict.blocked(["fix it"])
        return Verdict.ok(f"reviewed by {model}")


def _passed(line_id: str, dispatch) -> Line:
    line = _line(line_id, status=LineStatus.PASSED)
    line.branch = f"{dispatch.branch_prefix}/line-{line_id}"
    line.base_commit = BASE
    return line


def _git_heads(heads: dict[str, str]):
    """The gates' revision lookup: the branches git can name, and the rest it cannot."""
    return lambda line: heads.get(line.branch or "")


async def _replan_line(
    tmp_path: Path,
    dispatch,
    line: Line,
    planner: _ReplanPlanner,
    *,
    replans: int = 0,
    heads: dict[str, str] | None = None,
):
    """Drive one ``_replan`` on a claimed drive, as the review path does."""
    collaborators, machine, _commands, _trees = _bundle(tmp_path, [], planner=planner)
    gates = collaborators.gates
    await collaborators.store.create(dispatch)
    gates.prepare(dispatch)
    # The dispatch base a child falls back to when no sibling has passed: the
    # fake repo has no HEAD of its own to name.
    gates._ensure_root(dispatch)["base_commit"] = BASE
    gates._revisions = _git_heads(heads or {})
    snap = gates.line_snapshot(dispatch, line)
    snap["replans"] = replans
    snap["machine"].update(
        stage="replanning",
        replan_options=["resplit_task", "replace_developer"],
        author_provider="kimi",
        developer_provider="kimi",
        contributors=["kimi"],
    )
    await machine._claim_drive()
    try:
        await machine._replan(dispatch, line, ["blocker"], "two_failed_rounds")
    finally:
        machine._release_drive()
    return collaborators, machine


async def test_a_split_line_bases_its_children_on_a_passed_siblings_branch(
    tmp_path: Path,
) -> None:
    dispatch = _dispatch("d-n48-base")
    dispatch.lines.append(_passed("line-p", dispatch))
    line = dispatch.line("line-x")
    assert line is not None
    line.depends_on = ("line-p",)
    planner = _ReplanPlanner([_line("child-a"), _line("child-b")])
    collaborators, _machine = await _replan_line(
        tmp_path,
        dispatch,
        line,
        planner,
        heads={f"{dispatch.branch_prefix}/line-line-p": H1},
    )

    stored = await collaborators.store.get(dispatch.id)
    assert stored is not None
    children = [item for item in stored.lines if item.id.startswith("line-x-r1-")]
    assert [child.id for child in children] == ["line-x-r1-1", "line-x-r1-2"]
    # The dependency's code is the base, so no child stubs its interface.
    assert all(child.base_commit == H1 for child in children)
    assert all(child.depends_on == line.depends_on for child in children)
    assert children[0].branch == f"{dispatch.branch_prefix}/line-line-x-r1-1"
    assert stored.line("line-x").failure_reason == "replanned"


async def test_a_split_line_with_no_passed_sibling_keeps_the_dispatch_base(
    tmp_path: Path,
) -> None:
    """A blocked or still-running sibling is not a base: nothing has passed yet."""
    dispatch = _dispatch("d-n48-nosibling")
    blocked = _line("line-q")
    blocked.branch = f"{dispatch.branch_prefix}/line-line-q"
    blocked.status = LineStatus.BLOCKED
    dispatch.lines.append(blocked)
    line = dispatch.line("line-x")
    assert line is not None
    planner = _ReplanPlanner([_line("child-a")])
    collaborators, _machine = await _replan_line(tmp_path, dispatch, line, planner)

    stored = await collaborators.store.get(dispatch.id)
    child = stored.line("line-x-r1-1")
    assert child is not None
    assert child.base_commit == BASE


async def test_a_passed_sibling_whose_branch_git_cannot_name_is_not_a_base(
    tmp_path: Path,
) -> None:
    """A base no commit resolves to would fail the child's whole run."""
    dispatch = _dispatch("d-n48-nogit")
    dispatch.lines.append(_passed("line-p", dispatch))
    line = dispatch.line("line-x")
    assert line is not None
    planner = _ReplanPlanner([_line("child-a")])
    collaborators, _machine = await _replan_line(tmp_path, dispatch, line, planner)

    stored = await collaborators.store.get(dispatch.id)
    child = stored.line("line-x-r1-1")
    assert child is not None
    assert child.base_commit == BASE


async def test_a_second_replan_parks_the_line_for_the_planner(tmp_path: Path) -> None:
    """The cap is one replan: the line waits for the Planner, it is not retried."""
    dispatch = _dispatch("d-n48-cap")
    line = dispatch.line("line-x")
    assert line is not None
    planner = _ReplanPlanner([_line("child-a")])
    collaborators, _machine = await _replan_line(
        tmp_path, dispatch, line, planner, replans=1
    )
    gates = collaborators.gates
    assert planner.calls == []  # the planner is not asked again

    stored = await collaborators.store.get(dispatch.id)
    assert stored is not None
    parked = stored.line("line-x")
    assert parked is not None
    assert parked.status is LineStatus.BLOCKED
    assert parked.failure_reason == REPLAN_PARKED
    assert stored.line("line-x-r1-1") is None  # no split past the cap
    assert stored.message == REPLAN_PARKED_MESSAGE.format(dispatch_id=dispatch.id, line_id="line-x")
    assert "Planner" in parked.blockers[0]
    # Stage-Gate still holds the line in ``replanning`` with its options, so the
    # Planner's ruling is what moves it.
    state = gates.line_snapshot(stored, parked)["machine"]
    assert state["stage"] == "replanning"
    assert state["replan_options"] == ["resplit_task", "replace_developer"]


async def test_a_reassign_is_the_way_out_of_a_parked_line(tmp_path: Path) -> None:
    """The Planner's ``replace_developer`` ruling relaunches the parked line."""
    dispatch = _dispatch("d-n48-reassign")
    line = dispatch.line("line-x")
    assert line is not None
    planner = _ReplanPlanner([_line("child-a")])
    collaborators, _machine = await _replan_line(
        tmp_path, dispatch, line, planner, replans=1
    )
    gates = collaborators.gates
    gates.handoff_facts = _handoff
    stored = await collaborators.store.get(dispatch.id)
    assert stored is not None
    parked = stored.line("line-x")
    assert parked is not None

    assert await apply_human_reassign(stored, parked, "glm", None, gates) is None
    assert parked.status is LineStatus.ASSIGNED
    assert parked.assigned_model == "glm"
    assert parked.failure_reason is None
    assert gates.line_snapshot(stored, parked)["machine"]["stage"] == "assigned"


async def test_the_cap_holds_across_a_whole_drive(tmp_path: Path) -> None:
    """The R8c shape end to end: one split, then the child parks for the Planner."""
    reviewer = BlockThrice()
    planner = _ReplanPlanner([_line("child-a")])
    launcher = ScriptedWorkers([("ok", BASE, H1)] * 8)
    collaborators, machine, _commands, _trees = _bundle(
        tmp_path, [], launcher=launcher, reviewer=reviewer, planner=planner
    )
    collaborators.gates.handoff_facts = _handoff
    collaborators.gates._revisions = _git_heads({})
    dispatch = _dispatch("d-n48-drive")
    await collaborators.store.create(dispatch)

    result = await machine.run(dispatch)

    # line-x was split once; its child was not.
    assert result.line("line-x-r1-1") is not None
    assert result.line("line-x-r1-1-r1-1") is None
    child = result.line("line-x-r1-1")
    assert child is not None
    assert child.status is LineStatus.BLOCKED
    assert child.failure_reason == REPLAN_PARKED
    assert result.state.value == "REVIEW"
    assert result.message == REPLAN_PARKED_MESSAGE.format(
        dispatch_id=dispatch.id, line_id="line-x-r1-1"
    )
    # One replan: the planner was asked once, for line-x.
    assert len([goal for goal in planner.calls if "Replan this line." in goal]) == 1


async def test_a_split_line_bases_its_children_on_the_dependency_it_waits_on(
    tmp_path: Path,
) -> None:
    """The dependency's branch, not the newest passed sibling's.

    line-x waits on line-a. line-b passed later and is unrelated, so its head
    carries nothing of line-a: basing the children there left the dependency
    stubbed exactly as it was before N48.
    """
    dispatch = _dispatch("d-n48-depends")
    dispatch.lines.append(_passed("line-a", dispatch))
    dispatch.lines.append(_passed("line-b", dispatch))
    line = dispatch.line("line-x")
    assert line is not None
    line.depends_on = ("line-a",)
    planner = _ReplanPlanner([_line("child-a")])
    collaborators, _machine = await _replan_line(
        tmp_path,
        dispatch,
        line,
        planner,
        heads={
            f"{dispatch.branch_prefix}/line-line-a": H1,
            f"{dispatch.branch_prefix}/line-line-b": H2,
        },
    )

    stored = await collaborators.store.get(dispatch.id)
    child = stored.line("line-x-r1-1")
    assert child is not None
    # line-b passed last, and its branch is not the base: line-a's is.
    assert child.base_commit == H1
    assert child.depends_on == ("line-a",)


async def test_a_split_line_with_no_dependency_bases_on_the_integrate_base(
    tmp_path: Path,
) -> None:
    """Nothing to wait on: the integrate base, not the newest passed sibling.

    A sibling the line does not wait on carries code the line never asked
    for, and its children's diffs would then include it.
    """
    dispatch = _dispatch("d-n48-twosiblings")
    dispatch.lines.append(_passed("line-p", dispatch))
    dispatch.lines.append(_passed("line-q", dispatch))
    line = dispatch.line("line-x")
    assert line is not None
    planner = _ReplanPlanner([_line("child-a")])
    collaborators, _machine = await _replan_line(
        tmp_path,
        dispatch,
        line,
        planner,
        heads={
            f"{dispatch.branch_prefix}/line-line-p": H1,
            f"{dispatch.branch_prefix}/line-line-q": H2,
        },
    )

    stored = await collaborators.store.get(dispatch.id)
    child = stored.line("line-x-r1-1")
    assert child is not None
    assert child.base_commit == BASE


async def test_a_dependency_that_has_not_passed_is_not_a_base(tmp_path: Path) -> None:
    """A blocked dependency has no branch to build on: the dispatch base stays."""
    dispatch = _dispatch("d-n48-unpassed-dep")
    blocked = _line("line-a")
    blocked.branch = f"{dispatch.branch_prefix}/line-line-a"
    blocked.status = LineStatus.BLOCKED
    dispatch.lines.append(blocked)
    dispatch.lines.append(_passed("line-b", dispatch))
    line = dispatch.line("line-x")
    assert line is not None
    line.depends_on = ("line-a",)
    planner = _ReplanPlanner([_line("child-a")])
    collaborators, _machine = await _replan_line(
        tmp_path,
        dispatch,
        line,
        planner,
        heads={f"{dispatch.branch_prefix}/line-line-b": H2},
    )

    stored = await collaborators.store.get(dispatch.id)
    child = stored.line("line-x-r1-1")
    assert child is not None
    assert child.base_commit == BASE


async def test_passed_dependencies_whose_branches_share_no_commit_park_the_line(
    tmp_path: Path,
) -> None:
    """Two passed dependencies, two parallel branches, no base that carries both.

    Every line of a dispatch starts from the same base, so line-a and line-b
    each hold only their own work and no commit the dispatch can name holds
    both. The machine does not pick one and leave the other stubbed: it stops
    deciding and parks the line for the Planner, as it does past the cap.
    """
    dispatch = _dispatch("d-n48-uncombined")
    dispatch.lines.append(_passed("line-a", dispatch))
    dispatch.lines.append(_passed("line-b", dispatch))
    line = dispatch.line("line-x")
    assert line is not None
    line.depends_on = ("line-a", "line-b")
    planner = _ReplanPlanner([_line("child-a")])
    collaborators, _machine = await _replan_line(
        tmp_path,
        dispatch,
        line,
        planner,
        heads={
            f"{dispatch.branch_prefix}/line-line-a": H1,
            f"{dispatch.branch_prefix}/line-line-b": H2,
        },
    )
    gates = collaborators.gates
    assert planner.calls == []  # no split is planned onto a base that is not there

    stored = await collaborators.store.get(dispatch.id)
    assert stored is not None
    parked = stored.line("line-x")
    assert parked is not None
    assert parked.status is LineStatus.BLOCKED
    assert parked.failure_reason == REPLAN_UNCOMBINED
    assert stored.line("line-x-r1-1") is None  # no children at all
    assert stored.message == REPLAN_UNCOMBINED_MESSAGE.format(dispatch_id=dispatch.id, line_id="line-x")
    assert "line-a" in parked.blockers[0] and "line-b" in parked.blockers[0]
    assert "Planner" in parked.blockers[0]
    # Stage-Gate still holds the line in ``replanning`` with its options, so
    # the Planner's ruling is what moves it.
    state = gates.line_snapshot(stored, parked)["machine"]
    assert state["stage"] == "replanning"
    assert state["replan_options"] == ["resplit_task", "replace_developer"]


async def test_a_reassign_is_the_way_out_of_an_uncombined_park(tmp_path: Path) -> None:
    """The same way out as the cap park: reassign relaunches the line."""
    dispatch = _dispatch("d-n48-uncombined-reassign")
    dispatch.lines.append(_passed("line-a", dispatch))
    dispatch.lines.append(_passed("line-b", dispatch))
    line = dispatch.line("line-x")
    assert line is not None
    line.depends_on = ("line-a", "line-b")
    planner = _ReplanPlanner([_line("child-a")])
    collaborators, _machine = await _replan_line(
        tmp_path,
        dispatch,
        line,
        planner,
        heads={
            f"{dispatch.branch_prefix}/line-line-a": H1,
            f"{dispatch.branch_prefix}/line-line-b": H2,
        },
    )
    gates = collaborators.gates
    gates.handoff_facts = _handoff
    stored = await collaborators.store.get(dispatch.id)
    assert stored is not None
    parked = stored.line("line-x")
    assert parked is not None

    assert await apply_human_reassign(stored, parked, "glm", None, gates) is None
    assert parked.status is LineStatus.ASSIGNED
    assert parked.assigned_model == "glm"
    assert parked.failure_reason is None
