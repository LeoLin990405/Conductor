"""N6: two BLOCKed fix rounds escalate the line to the Planner."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.tools.auto_dispatch.escalation import (
    BLOCKER_BRIEF,
    ESCALATION,
    ESCALATION_INSTRUCTION,
    BlockedRound,
    build_escalation,
    escalation_brief,
    escalation_from_snapshot,
    failed_fix_rounds,
    record_blocked_round,
    should_escalate,
    summarize_blockers,
    write_escalation,
)
from src.tools.auto_dispatch.machine import (
    DispatchMachine,
    ReviewResult,
    _fix_task,
    _load_state,
    _remember_review,
)
from src.tools.auto_dispatch.types import Dispatch, Line, Verdict
from src.server.services.autonomy.stage_gate import (
    GateResult,
    Handoff,
    Stage,
)


def _handoff() -> Handoff:
    return Handoff(
        branch="feature/line-0",
        commit="abc123",
        files=("src/app.py",),
        test_command="pytest -q tests/unit/test_app.py",
        results="1 failed",
        known_limitations="none",
    )


def _snapshot(*, blocking_rounds: int = 0, fix_round: int = 0) -> dict[str, object]:
    return {
        "line_id": "line-0",
        "fix_round": fix_round,
        "blocking_rounds": blocking_rounds,
        "blocking_reviewer": "codex",
        "history": [],
    }


def test_the_initial_block_is_not_a_failed_fix_round() -> None:
    snapshot = _snapshot()

    record_blocked_round(snapshot, ["no tests"], round=0, reviewer="codex")

    assert failed_fix_rounds(snapshot) == 0
    assert should_escalate(snapshot) is False
    assert build_escalation("line-0", snapshot) is None


def test_one_blocked_fix_round_is_still_not_an_escalation() -> None:
    snapshot = _snapshot()

    record_blocked_round(snapshot, ["no tests"], round=0, reviewer="codex")
    record_blocked_round(snapshot, ["still no tests"], round=1, reviewer="codex")

    assert failed_fix_rounds(snapshot) == 1
    assert should_escalate(snapshot) is False


def test_two_consecutive_blocked_fix_rounds_escalate_with_every_blocker() -> None:
    snapshot = _snapshot()
    record_blocked_round(snapshot, ["missing unit test"], round=0, reviewer="codex")
    record_blocked_round(snapshot, ["test still missing"], round=1, reviewer="codex")
    record_blocked_round(snapshot, ["and now it regressed"], round=2, reviewer="codex")

    escalation = build_escalation("line-0", snapshot)

    assert escalation is not None
    assert escalation.due
    assert escalation.rounds == 2
    assert escalation.blockers == (
        "missing unit test",
        "test still missing",
        "and now it regressed",
    )
    assert "missing unit test" in escalation.summary
    assert "test still missing" in escalation.summary
    assert "and now it regressed" in escalation.summary
    # The instruction is the product rule, not just a label.
    assert "new approach" in escalation.instruction
    assert "narrower specification" in escalation.instruction
    assert "edge case" in escalation.instruction
    assert escalation.instruction == ESCALATION_INSTRUCTION


def test_a_new_reviewer_restarts_the_count() -> None:
    snapshot = _snapshot()
    record_blocked_round(snapshot, ["a"], round=0, reviewer="codex")
    record_blocked_round(snapshot, ["b"], round=1, reviewer="codex")
    record_blocked_round(snapshot, ["c"], round=0, reviewer="glm")
    record_blocked_round(snapshot, ["d"], round=1, reviewer="glm")

    assert failed_fix_rounds(snapshot) == 1
    assert should_escalate(snapshot) is False

    record_blocked_round(snapshot, ["e"], round=2, reviewer="glm")
    assert failed_fix_rounds(snapshot) == 2
    assert should_escalate(snapshot) is True


def test_blocking_rounds_is_authoritative_when_present() -> None:
    snapshot = _snapshot(blocking_rounds=2, fix_round=2)

    assert failed_fix_rounds(snapshot) == 2
    assert should_escalate(snapshot) is True
    assert build_escalation("line-0", snapshot) is not None


def test_the_summary_is_bounded_and_names_every_round() -> None:
    entries = (
        BlockedRound(0, "codex", ("first",)),
        BlockedRound(1, "codex", ("second",)),
        BlockedRound(2, "codex", ("third",)),
    )

    summary = summarize_blockers(entries)

    assert "3 BLOCK" in summary
    assert "initial BLOCK" in summary
    assert "BLOCK round 1" in summary
    assert "BLOCK round 2" in summary
    for item in ("first", "second", "third"):
        assert item in summary


def test_the_escalation_round_trips_through_the_snapshot() -> None:
    snapshot = _snapshot(blocking_rounds=2, fix_round=2)
    escalation = build_escalation("line-0", snapshot)
    assert escalation is not None

    write_escalation(snapshot, escalation)
    restored = escalation_from_snapshot(snapshot)

    assert restored == escalation
    assert snapshot[ESCALATION]["instruction"] == ESCALATION_INSTRUCTION
    assert escalation_brief(snapshot).startswith("Escalation to the Planner:")


def test_a_legacy_history_with_blockers_is_read_as_a_brief() -> None:
    snapshot = _snapshot(blocking_rounds=2, fix_round=2)
    snapshot["history"] = [
        {"round": 0, "result": "fail", "reviewer": "codex", "blockers": ["one"]},
        {"round": 1, "result": "fail", "reviewer": "codex", "blockers": ["two"]},
        {"round": 2, "result": "fail", "reviewer": "codex", "blockers": ["three"]},
    ]

    escalation = build_escalation("line-0", snapshot)

    assert escalation is not None
    assert escalation.blockers == ("one", "two", "three")


def test_machine_review_history_keeps_the_blockers_beside_the_count() -> None:
    snapshot = _snapshot()
    _remember_review(
        snapshot,
        round_n=0,
        gate="primary_review",
        result="fail",
        reviewer="codex",
        blocker_count=2,
        reason="review_blocked",
        at="2026-11-20T10:00:00+00:00",
        blockers=["missing test", "unused import"],
    )
    _remember_review(
        snapshot,
        round_n=1,
        gate="primary_review",
        result="fail",
        reviewer="codex",
        blocker_count=1,
        reason="review_blocked",
        at="2026-11-20T10:05:00+00:00",
        blockers=["missing test again"],
    )

    assert snapshot["history"][0]["blockers"] == ["missing test", "unused import"]
    # A passing review stays exactly the old shape.
    _remember_review(
        snapshot,
        round_n=0,
        gate="primary_review",
        result="pass",
        reviewer="codex",
        blocker_count=0,
        reason="review_merged",
        at="2026-11-20T10:10:00+00:00",
    )
    assert "blockers" not in snapshot["history"][-1]


def test_the_fix_round_path_appends_the_blocker_brief() -> None:
    snapshot = _snapshot()

    record_blocked_round(snapshot, ["missing test"], reviewer="codex")
    record_blocked_round(snapshot, ["missing test again"], reviewer="codex")

    assert len(snapshot[BLOCKER_BRIEF]) == 2
    assert failed_fix_rounds(snapshot) == 1


def test_the_fix_task_carries_the_escalation_instruction() -> None:
    snapshot = _snapshot(blocking_rounds=2, fix_round=2)
    escalation = build_escalation("line-0", snapshot)
    assert escalation is not None
    from src.tools.auto_dispatch.rulings import build_ruling

    ruling = build_ruling("line-0", ["missing test"], fix_round=2, escalation=escalation)
    task = _fix_task("do a", _handoff(), ["missing test"], ruling=ruling)

    assert "Blockers:\n- missing test\n" in task
    assert "new approach" in task
    assert "narrower specification" in task
    assert "Must not:" in task


def test_the_trigger_is_two_consecutive_blocked_fix_rounds() -> None:
    snapshot = _snapshot()
    for index in range(2):
        record_blocked_round(snapshot, [f"blocker {index}"], round=index, reviewer="codex")

    # One failed fix round is not enough; the second is the product threshold.
    snapshot["blocking_rounds"] = 1
    assert should_escalate(snapshot) is False

    snapshot["blocking_rounds"] = 2
    assert should_escalate(snapshot) is True
    assert build_escalation("line-0", snapshot) is not None


def test_machine_block_vocabulary_keeps_the_blockers_and_the_brief() -> None:
    snapshot = _snapshot()
    snapshot[BLOCKER_BRIEF] = [
        {"round": 0, "reviewer": "codex", "blockers": ["first blocker"]}
    ]

    _remember_review(
        snapshot,
        round_n=1,
        gate="primary_review",
        # The machine's review path supplies Stage-Gate's real verdict word.
        result="block",
        reviewer="codex",
        blocker_count=1,
        reason="review_blocked",
        at="2026-11-20T10:05:00+00:00",
        blockers=["second blocker"],
    )

    assert snapshot[BLOCKER_BRIEF] == [
        {"round": 0, "reviewer": "codex", "blockers": ["first blocker"]}
    ]
    assert snapshot["history"][-1]["blockers"] == ["second blocker"]


def test_actual_review_blocks_keep_every_blocker_in_the_escalation() -> None:
    snapshot = _snapshot(blocking_rounds=2, fix_round=2)

    for index, blocker in enumerate(("first", "second", "third")):
        _remember_review(
            snapshot,
            round_n=index,
            gate="primary_review",
            result="block",
            reviewer="codex",
            blocker_count=1,
            reason="review_blocked",
            at=f"2026-11-20T10:0{index}:00+00:00",
            blockers=[blocker],
        )

    escalation = build_escalation("line-0", snapshot)

    assert escalation is not None
    assert escalation.blockers == ("first", "second", "third")
    assert "first" in escalation.summary
    assert "second" in escalation.summary
    assert "third" in escalation.summary


@pytest.mark.asyncio
async def test_the_review_path_keeps_every_blocker_across_two_blocks() -> None:
    class _Gates:
        def __init__(self) -> None:
            self.snapshot = {
                "line_id": "line-0",
                "fix_round": 0,
                "machine": {
                    "task_id": "line-0",
                    "stage": "primary_review",
                    "blocking_reviewer": "codex",
                    "blocking_rounds": 0,
                    "author_provider": "author",
                    "developer_provider": "author",
                    "contributors": ["author"],
                },
                "history": [],
            }

        def line_snapshot(self, dispatch: Dispatch, line: Line) -> dict[str, object]:
            return self.snapshot

        def stamp(self) -> str:
            return "2026-11-20T10:00:00+00:00"

    gates = _Gates()
    machine = object.__new__(DispatchMachine)
    machine._c = SimpleNamespace(gates=gates)
    machine._on_actor = lambda: True
    machine._clear_review_attempts = lambda dispatch, line: None
    machine._block_line = lambda dispatch, line, reason: None
    launched: list[tuple[str, ...]] = []

    async def launch_fix(dispatch, line, blockers, *, kind="review", resume=False):
        record_blocked_round(gates.snapshot, blockers, reviewer="codex")
        gates.snapshot["fix_round"] = int(gates.snapshot.get("fix_round") or 0) + 1
        launched.append(tuple(blockers))
        return True

    replanned: list[tuple[str, ...]] = []

    async def replan(dispatch, line, blockers, reason):
        record_blocked_round(gates.snapshot, blockers, reviewer="codex")
        replanned.append(tuple(blockers))

    machine._launch_fix = launch_fix
    machine._replan = replan
    dispatch = Dispatch(id="d1", goal="goal", user_id="u1", created_at="now", lines=[])
    line = Line(id="line-0", name="line-0", task="do a")

    for blocker in ("first blocker", "second blocker"):
        state = _load_state(gates.snapshot)
        event = ReviewResult(
            dispatch_id="d1",
            line_id="line-0",
            run_id=None,
            attempt=0,
            passed=False,
            blockers=(blocker,),
        )
        await machine._apply_review_result(
            event,
            dispatch,
            line,
            secondary=False,
            reviewer="codex",
            kind="primary_review",
            risk=False,
            verdict=Verdict.blocked([blocker]),
            state=state,
        )
        if _load_state(gates.snapshot).stage is Stage.FIXING:
            assert machine._advance(dispatch, line, "submit_work").accepted

    assert [entry["result"] for entry in gates.snapshot["history"]] == ["block", "block"]
    assert [entry["blockers"] for entry in gates.snapshot["history"]] == [
        ["first blocker"],
        ["second blocker"],
    ]
    assert launched == [("first blocker",)]
    assert replanned == [("second blocker",)]
    escalation = build_escalation("line-0", gates.snapshot)
    assert escalation is not None
    assert "first blocker" in escalation.summary
    assert "second blocker" in escalation.summary


class _DefaultGates:
    def __init__(self) -> None:
        self.snapshot = {
            "line_id": "line-0",
            "machine": {
                "task_id": "line-0",
                "stage": "primary_review",
                "blocking_reviewer": "codex",
                "blocking_rounds": 0,
                "author_provider": "author",
                "developer_provider": "author",
                "contributors": ["author"],
            },
            "history": [],
        }

    def line_snapshot(self, dispatch: Dispatch, line: Line) -> dict[str, object]:
        return self.snapshot

    def stamp(self) -> str:
        return "2026-11-20T10:00:00+00:00"


def _default_machine() -> DispatchMachine:
    machine = object.__new__(DispatchMachine)
    machine._c = SimpleNamespace(gates=_DefaultGates())
    return machine


def _block(machine: DispatchMachine, dispatch: Dispatch, line: Line):
    return machine._advance(
        dispatch,
        line,
        "primary_review",
        provider="codex",
        verdict="block",
        result=GateResult(outcome="fail", reason="review_blocked"),
    )


def test_the_second_blocked_fix_round_routes_to_the_planner() -> None:
    machine = _default_machine()
    dispatch = Dispatch(id="d1", goal="goal", user_id="u1", created_at="now", lines=[])
    line = Line(id="line-0", name="line-0", task="do a")

    first = _block(machine, dispatch, line)
    assert first.accepted
    assert first.state.stage is Stage.FIXING

    resubmitted = machine._advance(dispatch, line, "submit_work")
    assert resubmitted.accepted
    assert resubmitted.state.stage is Stage.PRIMARY_REVIEW

    second = _block(machine, dispatch, line)
    assert second.accepted
    assert second.state.stage is Stage.REPLANNING
    assert second.state.blocking_rounds == 2
