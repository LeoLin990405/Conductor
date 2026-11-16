"""R6 L5: the secondary reviewer sees facts (diff, G2), never the primary's conclusions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.tools.auto_dispatch import review_isolation
from src.tools.auto_dispatch.execution import CrossReviewer, line_evidence
from src.tools.auto_dispatch.machine import RULING_UNCONFIRMED, DispatchMachine
from src.tools.auto_dispatch.types import Dispatch, Line, LineStatus

SENTINEL = "PRIMARY-CONCLUSION-SENTINEL"


def _dispatch(stage: str, *, blocking: str = "codex") -> tuple[Dispatch, Line, dict]:
    line = Line(
        id="line-0",
        name="line-0",
        task="do a",
        branch="wave/line-0",
        status=LineStatus.RUNNING,
        acceptance=["the thing works"],
    )
    snap = {
        "machine": {
            "task_id": "line-0",
            "stage": stage,
            "blocking_reviewer": blocking,
            "primary_reviewer": "codex",
            "primary_verdict": SENTINEL,
            "primary_reason": SENTINEL,
        },
        "primary_verdict": SENTINEL,
        "blockers": [SENTINEL],
        "gates": {
            "G2": {"result": "passed", "reason": "ok", "commands": [{"index": 0, "exit_code": 0}]},
            "primary_review": {"verdict": SENTINEL, "blockers": [SENTINEL]},
            "handoff": {
                "branch": "wave/line-0",
                "commit": "abc",
                "files": ["a.py"],
                "test_command": "pytest",
                "results": "1 passed",
                "blocker_count": 3,
            },
        },
    }
    dispatch = Dispatch(
        id="d1",
        goal="goal",
        user_id="u1",
        created_at="now",
        lines=[line],
        gates={"schema_version": "dispatch_gates_v1", "lines": {"line-0": snap}},
    )
    return dispatch, line, snap


def test_secondary_evidence_carries_facts_but_no_primary_conclusion() -> None:
    dispatch, _line, _snap = _dispatch("secondary_review")
    text = line_evidence(dispatch, {})("line-0")
    assert SENTINEL not in text
    # Facts are shared.
    assert "G2 passed ok" in text and "command 0 exit 0" in text
    assert "results: 1 passed" in text
    # The blocking reviewer's blocker count is a conclusion: hidden (failed on old code).
    assert "blocker_count" not in text


def test_primary_evidence_keeps_blocker_count() -> None:
    dispatch, _line, _snap = _dispatch("primary_review")
    text = line_evidence(dispatch, {})("line-0")
    assert "blocker_count: 3" in text
    assert SENTINEL not in text


@pytest.mark.asyncio
async def test_secondary_prompt_never_contains_primary_verdict() -> None:
    dispatch, _line, _snap = _dispatch("secondary_review")
    seen: list[str] = []

    class _Backend:
        async def review(self, model: str, prompt: str) -> str:
            seen.append(prompt)
            return '{"passed": true, "blockers": [], "verdict": "ok"}'

    reviewer = CrossReviewer(_Backend(), line_evidence(dispatch, {}))
    await reviewer.review_line("line-0", "stepfun")
    assert seen and all(SENTINEL not in prompt for prompt in seen)


def _machine(snapshot: dict) -> DispatchMachine:
    class _Gates:
        def line_snapshot(self, dispatch, line):
            return snapshot

    machine = object.__new__(DispatchMachine)
    machine._c = SimpleNamespace(gates=_Gates())
    return machine


def test_ruling_notes_from_primary_block_do_not_reach_secondary() -> None:
    dispatch, line, snap = _dispatch("secondary_review", blocking="codex")
    snap[RULING_UNCONFIRMED] = [f"RULING M1: not confirmed: {SENTINEL}"]
    machine = _machine(snap)
    assert machine._review_notes(line, dispatch, reviewer="stepfun") == ""
    # The secondary that issued the BLOCK still gets its own ruling back.
    snap["machine"]["blocking_reviewer"] = "stepfun"
    assert SENTINEL in machine._review_notes(line, dispatch, reviewer="stepfun")


def test_ruling_notes_unchanged_outside_secondary_stage() -> None:
    dispatch, line, snap = _dispatch("primary_review", blocking="codex")
    snap[RULING_UNCONFIRMED] = ["RULING M1: not confirmed: missing test"]
    machine = _machine(snap)
    assert "missing test" in machine._review_notes(line, dispatch, reviewer="codex")
    assert "missing test" in machine._review_notes(line, dispatch)


def test_notes_visible_rules() -> None:
    assert review_isolation.notes_visible({}, None)
    assert not review_isolation.notes_visible(
        {"machine": {"stage": "secondary_review"}}, "stepfun"
    )
