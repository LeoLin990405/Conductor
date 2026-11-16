"""N76: a line and its Stage-Gate stage are checked before recovery.

A known park whose ``block`` event never landed is repaired by appending that
one event (the N75 fix, generalized). A drift with no safe repair becomes
``liveness_inconsistent`` and a person's line. Shadow records both without
touching the gate.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.server.services.autonomy.stage_gate import Stage
from src.tools.auto_dispatch.liveness import modes, record
from src.tools.auto_dispatch.types import DispatchState, LineStatus

from .n76_fakes import make_dispatch, make_gates, make_line, make_machine, park_line, stored

pytestmark = pytest.mark.asyncio

WHEN = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


def _config(mode):
    from src.tools.auto_dispatch import MachineConfig

    return MachineConfig(liveness_mode=mode)


async def test_a_missed_block_event_is_appended_before_recovery(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=_config(modes.ENFORCE)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    park_line(gates, dispatch, line, "review_unavailable", stage=Stage.PRIMARY_REVIEW)

    await machine._sweep_liveness(dispatch, now=WHEN)

    snapshot = gates.line_snapshot(dispatch, line)
    assert snapshot["machine"]["stage"] == Stage.BLOCKED.value
    assert snapshot["events"][-1]["to_stage"] == Stage.BLOCKED.value
    assert snapshot["events"][-1]["reason"] == "operator_blocked"
    updated = record.load(snapshot)
    assert updated is not None
    assert updated.group == "R-REVIEW"
    assert updated.next_action == "review"
    assert line.status is LineStatus.BLOCKED


async def test_shadow_records_the_repair_without_writing_it(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=_config(modes.SHADOW)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    park_line(gates, dispatch, line, "review_unavailable", stage=Stage.PRIMARY_REVIEW)

    await machine._sweep_liveness(dispatch, now=WHEN)

    snapshot = gates.line_snapshot(dispatch, line)
    assert snapshot["machine"]["stage"] == Stage.PRIMARY_REVIEW.value
    observed = record.load(snapshot)
    assert observed is not None
    assert observed.shadowed == 1
    assert observed.history[-1].action == "repair"


async def test_an_unrepairable_drift_becomes_a_person_s_line(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=_config(modes.ENFORCE)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    # A conflict park only allows BLOCKED / AWAITING_ADJUDICATION. A fix stage
    # is not a safe automatic repair.
    park_line(
        gates, dispatch, line, "reviewer_conflict_scope", stage=Stage.FIXING
    )

    await machine._sweep_liveness(dispatch, now=WHEN)

    snapshot = gates.line_snapshot(dispatch, line)
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "liveness_inconsistent"
    updated = record.load(snapshot)
    assert updated is not None and updated.needs_human
    assert updated.human_reason == "liveness_inconsistent"
    assert dispatch.state is DispatchState.REVIEW
    assert any("needs a person" in item for item in line.blockers)
    notes = dispatch.gates.get("needs_human")
    assert isinstance(notes, dict) and "line-0" in notes


async def test_a_blocked_line_without_a_reason_is_inconsistent(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=_config(modes.ENFORCE)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    park_line(gates, dispatch, line, "review_unavailable", stage=Stage.PRIMARY_REVIEW)
    line.failure_reason = None
    gates.line_snapshot(dispatch, line)["reason"] = None

    await machine._sweep_liveness(dispatch, now=WHEN)

    assert line.failure_reason == "liveness_inconsistent"
    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None and updated.needs_human
    assert "no failure reason" in updated.human_reason or updated.human_reason == "liveness_inconsistent"


async def test_shadow_does_not_latch_an_inconsistent_line(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=_config(modes.SHADOW)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    park_line(
        gates, dispatch, line, "reviewer_conflict_scope", stage=Stage.FIXING
    )

    await machine._sweep_liveness(dispatch, now=WHEN)

    assert line.failure_reason == "reviewer_conflict_scope"
    observed = record.load(gates.line_snapshot(dispatch, line))
    assert observed is not None
    assert not observed.needs_human
    assert observed.history[-1].action == "human"


@pytest.mark.parametrize("mode", [modes.OFF, modes.SHADOW, modes.ENFORCE])
async def test_the_inline_review_unavailable_count_park_is_repaired(tmp_path, mode) -> None:
    """C3: three inline review failures park the line with Stage-Gate still at
    primary_review (main's behaviour, kept by every mode). Only enforce's sweep
    appends the missing block event; shadow records the repair; off does
    nothing, as on main."""
    from dataclasses import replace

    from src.tools.auto_dispatch.machine import (
        REVIEW_ATTEMPT_LIMIT,
        _load_state,
        _snapshot_stage,
        _store_state,
    )

    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(tmp_path, gates=gates, config=_config(mode))
    line = make_line(assigned_model="codex")
    line.status = LineStatus.RUNNING
    dispatch = await stored(collaborators, make_dispatch(line))
    dispatch.state = DispatchState.REVIEW
    snapshot = gates.line_snapshot(dispatch, line)
    _store_state(snapshot, replace(_load_state(snapshot), stage=Stage.PRIMARY_REVIEW))

    for _ in range(REVIEW_ATTEMPT_LIMIT):
        await machine._note_review_unavailable(dispatch, line)

    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "review_unavailable"
    assert _snapshot_stage(gates.line_snapshot(dispatch, line)) is Stage.PRIMARY_REVIEW

    await machine._sweep_liveness(dispatch, now=WHEN)
    cell = gates.line_snapshot(dispatch, line)
    if mode == modes.OFF:
        assert _snapshot_stage(cell) is Stage.PRIMARY_REVIEW
        assert "liveness" not in cell
    elif mode == modes.SHADOW:
        assert _snapshot_stage(cell) is Stage.PRIMARY_REVIEW
        observed = record.load(cell)
        assert observed is not None
        assert [step.action for step in observed.history] == ["repair"]
        assert observed.history[0].result == record.SHADOW
    else:
        assert _snapshot_stage(cell) is Stage.BLOCKED
        assert line.status is LineStatus.BLOCKED
        assert record.load(cell) is not None
