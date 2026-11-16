"""N79: a reassign after a park still tells the new author what review rejected.

The prod case (2026-11-12, dispatch c18fb039, line-0-r1-1 and line-0-r1-6):
review blocked a replan child twice, its one replan was used, and the line
parked ``replan_parked`` with ``line.blockers`` replaced by the operator's
"the Planner decides" message. The operator reassigned it, and the new
author's brief held only that message: the reviewer's five findings were
gone. The author saw code that looked complete, verified it and exited with
no commit. N38 rightly refuses to resubmit a tip review already rejected, so
the line parked ``no_new_commit`` -- and every later reassign looped the same
way.

Now the last rejecting review's findings stay on the gate snapshot
(``last_review_blockers``) until a review passes, and every reassign brief on
that branch opens with an instruction that the tip was rejected and must
change, followed by the reviewer's own text, bounded and sanitized like any
brief item. The operator-facing park message is unchanged.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.tools.auto_dispatch import LineStatus
from src.tools.auto_dispatch.machine import (
    LAST_REVIEW_BLOCKERS,
    MAX_LINE_TASK_BYTES,
    REPLAN_PARKED,
    REPLAN_PARKED_MESSAGE,
    _BRIEF_MAX_BLOCKERS,
    _BRIEF_MAX_CHARS,
    apply_human_reassign,
    reassign_brief,
)
from src.tools.auto_dispatch.types import Verdict
from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle
from tests.unit.tools.auto_dispatch.test_n34_committed_retry import (
    BASE,
    H1,
    H2,
    NO_COMMIT_BLOCKER,
    ScriptedWorkers,
    _dispatch,
    _handoff,
)

A = "kimi"
B = "stepfun"
C = "glm"
H3 = "3" * 40

FINDINGS = [
    "src/a.py: the retry loop never backs off; add the exponential delay",
    "src/a.py: the timeout is swallowed; raise TimeoutError to the caller",
    "tests/test_a.py: no test covers the second retry",
    "src/b.py: the cache key ignores the user id",
    "docs: the new flag is not documented",
]
TIP_REJECTED = "rejected this line's current branch tip"


class PromptedWorkers(ScriptedWorkers):
    """ScriptedWorkers that also keep the task each launch was given."""

    def __init__(self, script: list[tuple[object, ...]]) -> None:
        super().__init__(script)
        self.tasks: list[str] = []

    async def launch_worker(self, line, model, skills, *, session_id, session_mode, handoff_path):
        self.tasks.append(line.task)
        return await super().launch_worker(
            line,
            model,
            skills,
            session_id=session_id,
            session_mode=session_mode,
            handoff_path=handoff_path,
        )


class BlockWith:
    """Every review blocks with the given findings."""

    def __init__(self, findings: list[str]) -> None:
        self.findings = list(findings)
        self.reviewed: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.reviewed.append((line_id, model))
        return Verdict.blocked(list(self.findings))


def _as_spent_child(gates, dispatch) -> None:
    """``line-x`` is a replan child: its one replan is already used."""
    line = dispatch.line("line-x")
    assert line is not None
    gates.prepare(dispatch)
    snap = gates.line_snapshot(dispatch, line)
    snap["replans"] = 1
    snap["replanned_from"] = "line-p"


async def _parked_after_two_blocks(tmp_path: Path, findings: list[str], dispatch_id: str):
    """Blocked review rounds on a spent child until the line parks for the Planner.

    Stage-Gate sends the line to ``replanning`` (two_failed_rounds) on the
    third BLOCK; its replan is already used, so it parks ``replan_parked``.
    """
    launcher = PromptedWorkers([("ok", BASE, H1), ("ok", H1, H2), ("ok", H2, H3)])
    reviewer = BlockWith(findings)
    collaborators, machine, _commands, _trees = _bundle(
        tmp_path, [], launcher=launcher, reviewer=reviewer
    )
    gates = collaborators.gates
    gates.handoff_facts = _handoff
    dispatch = _dispatch(dispatch_id, model=A)
    _as_spent_child(gates, dispatch)
    result = await machine.run(dispatch)
    return collaborators, machine, launcher, reviewer, result


def _brief_text(brief: list[str] | None) -> str:
    assert brief is not None
    return "\n".join(brief)


def test_a_replan_park_reassign_brief_carries_the_reviewers_findings(tmp_path: Path) -> None:
    """(1) and (3): failed review rounds, replan park, reassign -- the findings reach the author."""

    async def scenario() -> None:
        collaborators, _machine, launcher, reviewer, result = await _parked_after_two_blocks(
            tmp_path, FINDINGS, "d-n79-park"
        )
        gates = collaborators.gates
        line = result.line("line-x")
        assert line is not None
        assert len(reviewer.reviewed) == 3
        assert line.status is LineStatus.BLOCKED
        assert line.failure_reason == REPLAN_PARKED
        # (3) The operator-facing park message is still what the line shows.
        assert len(line.blockers) == 1
        assert line.blockers[0].startswith("This line used its one replan; the Planner decides")
        assert result.message == REPLAN_PARKED_MESSAGE.format(
            dispatch_id=result.id, line_id="line-x"
        )
        kept = gates.line_snapshot(result, line)[LAST_REVIEW_BLOCKERS]
        assert kept["blockers"] == FINDINGS
        assert kept["head"] == H3
        assert kept["branch"] == line.branch

        assert await apply_human_reassign(result, line, B, None, gates) is None
        text = _brief_text(reassign_brief(result, line))
        assert TIP_REJECTED in text
        assert "no_new_commit" in text
        for finding in FINDINGS:
            assert finding in text
        # The instruction comes first, then the findings.
        brief = reassign_brief(result, line)
        assert brief is not None and TIP_REJECTED in brief[0]
        assert brief[1] == "Review finding: " + FINDINGS[0]

        # The new author's prompt is built from that brief.
        launcher.script.append(("empty", H3, True))
        await _run_again(_machine, result)
        prompt = launcher.tasks[-1]
        assert TIP_REJECTED in prompt
        for finding in FINDINGS:
            assert finding in prompt

    asyncio.run(scenario())


async def _run_again(machine, dispatch):
    """Drive the reassigned dispatch again, as the operator's reassign does."""
    return await machine.run(dispatch)


def test_a_no_new_commit_park_after_that_reassign_keeps_the_findings(tmp_path: Path) -> None:
    """(2) The author only verified the rejected tip; the next reassign still has the findings."""

    async def scenario() -> None:
        collaborators, _machine, launcher, _reviewer, result = await _parked_after_two_blocks(
            tmp_path, FINDINGS, "d-n79-nnc"
        )
        gates = collaborators.gates
        line = result.line("line-x")
        assert line is not None
        assert await apply_human_reassign(result, line, B, None, gates) is None

        # B verifies the tip and exits with no commit: N38 still parks it.
        launcher.script.append(("empty", H3, True))
        after = await _run_again(_machine, result)
        parked = after.line("line-x")
        assert parked is not None
        assert parked.status is LineStatus.BLOCKED
        assert parked.failure_reason == "no_new_commit"
        assert parked.blockers == [NO_COMMIT_BLOCKER]
        assert gates.line_snapshot(after, parked)[LAST_REVIEW_BLOCKERS]["blockers"] == FINDINGS

        assert await apply_human_reassign(after, parked, C, None, gates) is None
        brief = reassign_brief(after, parked)
        text = _brief_text(brief)
        assert TIP_REJECTED in text
        for finding in FINDINGS:
            assert finding in text
        # The operator's no-commit message and the stop reason follow the findings.
        assert NO_COMMIT_BLOCKER in text
        assert brief is not None and brief[-1] == "Previous attempt stopped with: no_new_commit"

        launcher.script.append(("empty", H3, True))
        await _run_again(_machine, after)
        prompt = launcher.tasks[-1]
        assert TIP_REJECTED in prompt
        for finding in FINDINGS:
            assert finding in prompt

    asyncio.run(scenario())


def test_the_brief_is_bounded_and_sanitized(tmp_path: Path) -> None:
    """(4) Many long findings, a credential and terminal controls: capped and cleaned."""
    secret = "sk-" + "a1B2c3D4e5" * 4
    findings = [
        f"finding {index}: \x1b[31mred\x1b[0m api_key={secret} " + "x" * 3_000
        for index in range(40)
    ]

    async def scenario() -> None:
        collaborators, _machine, launcher, _reviewer, result = await _parked_after_two_blocks(
            tmp_path, findings, "d-n79-bound"
        )
        gates = collaborators.gates
        line = result.line("line-x")
        assert line is not None
        kept = gates.line_snapshot(result, line)[LAST_REVIEW_BLOCKERS]
        assert kept["count"] == 40
        assert len(kept["blockers"]) < _BRIEF_MAX_BLOCKERS
        assert await apply_human_reassign(result, line, B, None, gates) is None
        brief = reassign_brief(result, line)
        assert brief is not None
        # At most the cap, plus the one stop-reason line every brief may add.
        assert len(brief) <= _BRIEF_MAX_BLOCKERS + 1
        assert all(len(item) <= _BRIEF_MAX_CHARS for item in brief)
        assert TIP_REJECTED in brief[0]
        text = "\n".join(brief)
        assert secret not in text
        assert "\x1b" not in text
        assert "finding 0:" in text

        launcher.script.append(("empty", H3, True))
        await _run_again(_machine, result)
        prompt = launcher.tasks[-1]
        assert len(prompt.encode("utf-8")) <= MAX_LINE_TASK_BYTES
        assert secret not in prompt
        assert TIP_REJECTED in prompt

    asyncio.run(scenario())


def test_a_passing_review_forgets_the_findings(tmp_path: Path) -> None:
    """Findings are kept only while the tip stands rejected."""

    class BlockThenPass:
        def __init__(self) -> None:
            self.reviewed: list[tuple[str, str]] = []

        async def review_line(self, line_id: str, model: str) -> Verdict:
            self.reviewed.append((line_id, model))
            if len(self.reviewed) == 1:
                return Verdict.blocked(list(FINDINGS))
            return Verdict.ok("fine")

    async def scenario() -> None:
        launcher = PromptedWorkers([("ok", BASE, H1), ("ok", H1, H2)])
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, reviewer=BlockThenPass()
        )
        collaborators.gates.handoff_facts = _handoff
        dispatch = _dispatch("d-n79-pass", model=A)
        result = await machine.run(dispatch)
        line = result.line("line-x")
        assert line is not None
        assert line.status is LineStatus.PASSED
        assert LAST_REVIEW_BLOCKERS not in collaborators.gates.line_snapshot(result, line)

    asyncio.run(scenario())


def test_an_uncombined_park_keeps_the_findings_for_the_reassign(tmp_path: Path) -> None:
    """``replan_uncombined`` also replaces line.blockers; the brief still has the findings."""
    from tests.unit.tools.auto_dispatch.test_machine_gates import _line
    from tests.unit.tools.auto_dispatch.test_machine_replan_cap import (
        _git_heads,
        _passed,
        _ReplanPlanner,
    )

    async def scenario() -> None:
        dispatch = _dispatch("d-n79-uncombined")
        dispatch.lines.append(_passed("line-a", dispatch))
        dispatch.lines.append(_passed("line-b", dispatch))
        line = dispatch.line("line-x")
        assert line is not None
        line.depends_on = ("line-a", "line-b")
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path, [], planner=_ReplanPlanner([_line("child-a")])
        )
        gates = collaborators.gates
        gates.handoff_facts = _handoff
        await collaborators.store.create(dispatch)
        gates.prepare(dispatch)
        gates._ensure_root(dispatch)["base_commit"] = BASE
        gates._revisions = _git_heads(
            {
                f"{dispatch.branch_prefix}/line-line-a": H1,
                f"{dispatch.branch_prefix}/line-line-b": H2,
            }
        )
        snap = gates.line_snapshot(dispatch, line)
        snap["machine"].update(
            stage="replanning",
            replan_options=["resplit_task", "replace_developer"],
            author_provider=A,
            developer_provider=A,
            contributors=[A],
        )
        # What the rejecting review left on the snapshot before the replan.
        snap[LAST_REVIEW_BLOCKERS] = {
            "head": H1,
            "branch": line.branch,
            "reviewer": "grok",
            "count": len(FINDINGS),
            "blockers": list(FINDINGS),
            "at": "now",
        }
        await machine._claim_drive()
        try:
            await machine._replan(dispatch, line, list(FINDINGS), "two_failed_rounds")
        finally:
            machine._release_drive()
        stored = await collaborators.store.get(dispatch.id)
        assert stored is not None
        parked = stored.line("line-x")
        assert parked is not None
        assert parked.failure_reason == "replan_uncombined"
        assert "Planner" in parked.blockers[0]
        assert await apply_human_reassign(stored, parked, C, None, gates) is None
        brief = reassign_brief(stored, parked)
        text = _brief_text(brief)
        assert "grok" in text and TIP_REJECTED in text
        for finding in FINDINGS:
            assert finding in text
        # The operator's park message is still in the brief, after the findings.
        assert brief is not None and "share no commit" in brief[len(FINDINGS) + 1]

    asyncio.run(scenario())


def test_a_task_near_the_cap_still_carries_the_rejected_tip_instruction(
    tmp_path: Path,
) -> None:
    """The task text is cut before the brief, so the instruction reaches the author."""

    async def scenario() -> None:
        launcher = PromptedWorkers([("ok", BASE, H1), ("ok", H1, H2), ("ok", H2, H3)])
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path, [], launcher=launcher, reviewer=BlockWith(FINDINGS)
        )
        gates = collaborators.gates
        gates.handoff_facts = _handoff
        dispatch = _dispatch("d-n79-bigtask", model=A)
        line = dispatch.line("line-x")
        assert line is not None
        line.task = "t" * (MAX_LINE_TASK_BYTES - 100)
        _as_spent_child(gates, dispatch)
        result = await machine.run(dispatch)
        parked = result.line("line-x")
        assert parked is not None and parked.failure_reason == REPLAN_PARKED
        assert await apply_human_reassign(result, parked, B, None, gates) is None
        launcher.script.append(("empty", H3, True))
        await _run_again(machine, result)
        prompt = launcher.tasks[-1]
        assert len(prompt.encode("utf-8")) <= MAX_LINE_TASK_BYTES
        assert prompt.startswith("ttt")
        assert "[task truncated]" in prompt
        assert TIP_REJECTED in prompt
        for finding in FINDINGS:
            assert finding in prompt

    asyncio.run(scenario())


def test_an_in_scope_adjudication_keeps_its_findings_for_a_later_reassign(
    tmp_path: Path,
) -> None:
    """The ruled finding is the author's to fix, so it survives a later park too."""
    from tests.unit.server.services.test_dispatch_unwedge import _awaiting

    async def scenario() -> None:
        _store, _launcher, _reviewer, service, parked = await _awaiting(tmp_path)
        snap = parked.gates["lines"]["a"]
        # A conflict waiting for its ruling is not yet the author's to fix.
        assert LAST_REVIEW_BLOCKERS not in snap
        ruled = await service.adjudicate(parked.id, "a", "in-scope", note="billing is part of it")
        kept = ruled.gates["lines"]["a"][LAST_REVIEW_BLOCKERS]
        assert kept["blockers"] == ["scope: this change also rewrites the billing module"]

    asyncio.run(scenario())


def test_an_out_of_scope_ruling_never_hands_its_finding_to_the_author(
    tmp_path: Path,
) -> None:
    from tests.unit.server.services.test_dispatch_unwedge import _awaiting

    async def scenario() -> None:
        _store, _launcher, _reviewer, service, parked = await _awaiting(tmp_path)
        ruled = await service.adjudicate(parked.id, "a", "out_of_scope", note="separate ticket")
        assert LAST_REVIEW_BLOCKERS not in ruled.gates["lines"]["a"]

    asyncio.run(scenario())
