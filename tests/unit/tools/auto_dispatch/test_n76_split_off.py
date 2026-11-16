"""N76 R-SPLIT: remove a stuck line and its dependants, keep the round moving.

The removed lines are FAILED/``split_off``, get a follow-up draft on disk and
are listed under ``outcomes["split_off"]``. The remaining PASSED lines still
reach G5 and approval; a dispatch that split off lines never auto-approves.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.tools.auto_dispatch import MachineConfig
from src.tools.auto_dispatch.approval import (
    FileApprovalGate,
    auto_approve_allowed,
    has_split_off,
    split_off_lines,
)
from src.tools.auto_dispatch.machine import (
    _copy_row,
    _ready_for_g5,
    _retarget_dependents,
)
from src.tools.auto_dispatch.types import (
    DispatchState,
    LineStatus,
    SplitOffOutcome,
)

from .n76_fakes import make_dispatch, make_gates, make_line, make_machine, park_line, stored

pytestmark = pytest.mark.asyncio

WHEN = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


async def test_split_offs_the_line_and_every_dag_dependant(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(tmp_path, gates=gates)
    first = make_line(
        "line-0",
        assigned_model="codex",
        branch="feature/auto-dispatch-d1/line-line-0",
    )
    second = make_line("line-1", depends_on=("line-0",))
    independent = make_line("line-2", status=LineStatus.PASSED)
    dispatch = await stored(collaborators, make_dispatch(first, second, independent))
    snapshot = park_line(gates, dispatch, first, "no_new_commit")
    snapshot["escalation"] = {
        "summary": "two rounds of the same edge-case patch",
        "rounds": 2,
        "blockers": ["same bug"],
    }

    removed = await machine._split_off_line(
        dispatch, first, "no_new_commit", WHEN
    )

    assert removed == ["line-0", "line-1"]
    for line in (first, second):
        assert line.status is LineStatus.FAILED
        assert line.failure_reason == "split_off"
    split = dispatch.outcomes["split_off"]
    assert isinstance(split, SplitOffOutcome)
    assert set(split) == {"line-0", "line-1"}
    followup = tmp_path / "followups" / "d1-line-0.md"
    assert followup.is_file()
    text = followup.read_text(encoding="utf-8")
    assert "do line-0" in text
    assert "cmd: pytest -q tests/unit/x.py" in text
    assert "returns 200" in text
    assert "feature/auto-dispatch-d1/line-line-0" in text
    assert "Parking history" in text
    assert "two rounds of the same edge-case patch" in text


async def test_remaining_passed_lines_are_still_ready_for_g5(tmp_path) -> None:
    passed = make_line("line-0", status=LineStatus.PASSED)
    split = make_line("line-1", status=LineStatus.FAILED)
    split.failure_reason = "split_off"
    dispatch = make_dispatch(passed, split)

    assert _ready_for_g5(dispatch) is True

    passed.status = LineStatus.BLOCKED
    assert _ready_for_g5(dispatch) is False


async def test_the_sweep_takes_the_r_split_step_after_two_reassigns(tmp_path) -> None:
    from src.tools.auto_dispatch.liveness import modes, record

    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path,
        gates=gates,
        config=MachineConfig(liveness_mode=modes.ENFORCE),
    )
    line = make_line("line-0")
    remaining = make_line("line-1", status=LineStatus.PASSED)
    dispatch = await stored(collaborators, make_dispatch(line, remaining))
    snapshot = park_line(gates, dispatch, line, "no_new_commit")
    spent = record.new_record("no_new_commit", "R-EXIT", budget=4, at=WHEN)
    for _ in range(2):
        spent = spent.with_step("reassign", at=WHEN, result=record.APPLIED)
    spent = spent.with_schedule(at=WHEN, next_at=WHEN, next_action="split")
    snapshot.update(record.store(snapshot, spent))

    await machine._sweep_liveness(dispatch, now=WHEN)

    assert line.status is LineStatus.FAILED
    assert line.failure_reason == "split_off"
    assert (tmp_path / "followups" / "d1-line-0.md").is_file()
    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None
    assert updated.outcome == record.SPLIT_OFF
    assert updated.history[-1].action == "split"


async def test_a_round_with_no_passed_lines_stays_in_review(tmp_path) -> None:
    from src.tools.auto_dispatch.liveness import record

    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(tmp_path, gates=gates)
    line = make_line("line-0")
    dispatch = await stored(collaborators, make_dispatch(line))
    park_line(gates, dispatch, line, "no_new_commit")

    removed = await machine._split_off_line(dispatch, line, "no_new_commit", WHEN)

    assert removed == []
    assert dispatch.state is DispatchState.REVIEW
    assert "R-SPLIT refused" in (dispatch.message or "")
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "no_new_commit"
    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None and updated.needs_human
    assert not (tmp_path / "followups" / "d1-line-0.md").exists()


async def test_approval_summary_states_split_off_and_disables_auto_approval(tmp_path) -> None:
    dispatch = make_dispatch(
        make_line("line-0", status=LineStatus.PASSED),
        make_line("line-1", status=LineStatus.FAILED),
    )
    dispatch.lines[1].failure_reason = "split_off"
    split = SplitOffOutcome()
    split.add(
        "line-1",
        followup="/tmp/followups/d1-line-1.md",
        branch="feature/auto-dispatch-d1/line-line-1",
        reason="split_off",
    )
    dispatch.outcomes["split_off"] = split

    assert has_split_off(dispatch) is True
    assert auto_approve_allowed(dispatch) is False
    assert set(split_off_lines(dispatch)) == {"line-1"}

    gate = FileApprovalGate(tmp_path / "proposed")
    await gate.request_approval(dispatch)
    summary = (tmp_path / "proposed" / "d1" / "summary.md").read_text(encoding="utf-8")
    assert "## Split off" in summary
    assert "line-1" in summary
    assert "d1-line-1.md" in summary
    assert "auto-approval is disabled" in summary


async def test_a_plain_dispatch_is_still_auto_approvable(tmp_path) -> None:
    dispatch = make_dispatch(make_line("line-0", status=LineStatus.PASSED))
    assert has_split_off(dispatch) is False
    assert auto_approve_allowed(dispatch) is True


async def test_an_atomic_partner_is_never_left_behind_for_g5(tmp_path) -> None:
    from src.tools.auto_dispatch.liveness import record

    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(tmp_path, gates=gates)
    blocked = make_line("line-0", atomic_with=("line-1",))
    passed = make_line("line-1", status=LineStatus.PASSED)
    dispatch = await stored(collaborators, make_dispatch(blocked, passed))
    assert _ready_for_g5(dispatch) is False
    snap = park_line(gates, dispatch, blocked, "no_new_commit")

    removed = await machine._split_off_line(
        dispatch, blocked, "no_new_commit", WHEN
    )

    assert removed == []
    assert blocked.status is LineStatus.BLOCKED
    assert passed.status is LineStatus.PASSED
    assert _ready_for_g5(dispatch) is False
    updated = record.load(snap)
    assert updated is not None and updated.needs_human
    assert not (tmp_path / "followups" / "d1-line-0.md").exists()


async def test_atomic_metadata_round_trips_on_the_line() -> None:
    from src.tools.auto_dispatch.types import Line

    line = make_line("line-0", atomic_with=("line-1",))
    restored = Line.from_dict(line.to_dict())
    assert restored.atomic_with == ("line-1",)


async def test_the_sweep_keeps_a_refused_split_as_needs_human(tmp_path) -> None:
    from src.tools.auto_dispatch.liveness import modes, record

    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path,
        gates=gates,
        config=MachineConfig(liveness_mode=modes.ENFORCE),
    )
    line = make_line("line-0")
    dispatch = await stored(collaborators, make_dispatch(line))
    snapshot = park_line(gates, dispatch, line, "no_new_commit")
    spent = record.new_record("no_new_commit", "R-EXIT", budget=4, at=WHEN)
    for _ in range(2):
        spent = spent.with_step("reassign", at=WHEN, result=record.APPLIED)
    spent = spent.with_schedule(at=WHEN, next_at=WHEN, next_action="split")
    snapshot.update(record.store(snapshot, spent))

    await machine._sweep_liveness(dispatch, now=WHEN)

    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None and updated.needs_human
    assert line.status is LineStatus.BLOCKED
    assert not (tmp_path / "followups" / "d1-line-0.md").exists()


async def test_the_planner_accepts_and_propagates_atomic_groups() -> None:
    """``atomic_with`` must survive real planner validation, not just Line.

    The split protection reads ``Line.atomic_with``, but the planner whitelist
    rejected the field as ``unknown_field`` and never copied it into the line.
    A real plan can only ask for the protection if validation accepts the
    planner's names and maps them to ``line-N`` ids.
    """
    from src.tools.auto_dispatch.planning import PlanRejection, validate_plan

    def entry(name: str, partner: str) -> dict[str, object]:
        return {
            "name": name,
            "task": "work",
            "depends_on": [],
            "write_scope": [f"src/{name}.py"],
            "acceptance": [f"cmd: pytest -q tests/unit/{name}.py"],
            "risk_bearing": False,
            "atomic_with": [partner],
        }

    result = validate_plan(
        {
            "schema_version": 1,
            "spec": {"title": "Work", "body": "Ship it"},
            "lines": [entry("a", "b"), entry("b", "a")],
        },
        atomic=True,
    )

    assert not isinstance(result, PlanRejection)
    by_name = {line.name: line for line in result.lines}
    assert by_name["a"].atomic_with == ("line-1",)
    assert by_name["b"].atomic_with == ("line-0",)
    # The propagated group is what keeps a split partner out of G5: the
    # passed member alone is not a complete atomic unit.
    by_name["a"].status = LineStatus.PASSED
    by_name["b"].status = LineStatus.FAILED
    by_name["b"].failure_reason = "split_off"
    assert _ready_for_g5(make_dispatch(*result.lines)) is False

    unknown = validate_plan(
        {
            "schema_version": 1,
            "spec": {"title": "Work", "body": "Ship it"},
            "lines": [entry("a", "missing")],
        },
        atomic=True,
    )
    assert isinstance(unknown, PlanRejection)
    assert unknown.reason == "missing_dependency"


@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_atomic_with_is_main_unknown_field_outside_enforce(monkeypatch, mode) -> None:
    """Only enforce teaches or accepts ``atomic_with``; off/shadow are main."""
    from src.tools.auto_dispatch import planner_prompt
    from src.tools.auto_dispatch.planning import PlanRejection, validate_plan

    monkeypatch.setenv("CONDUCTOR_DISPATCH_LIVENESS_MODE", mode)
    payload = {
        "schema_version": 1,
        "spec": {"title": "Work", "body": "Ship it"},
        "lines": [
            {
                "name": "a",
                "task": "work",
                "depends_on": [],
                "write_scope": ["src/a.py"],
                "acceptance": ["cmd: pytest -q tests/unit/a.py"],
                "risk_bearing": False,
                "atomic_with": "not-a-list",
            }
        ],
    }
    rejected = validate_plan(payload)
    assert isinstance(rejected, PlanRejection)
    assert (rejected.reason, rejected.detail) == ("unknown_field", "atomic_with")
    assert planner_prompt.planner_system_prompt() == planner_prompt.PLANNER_SYSTEM_PROMPT
    assert "atomic_with" not in planner_prompt.PLANNER_SYSTEM_PROMPT
    assert planner_prompt.plan_json_schema() is planner_prompt.PLAN_JSON_SCHEMA
    monkeypatch.setenv("CONDUCTOR_DISPATCH_LIVENESS_MODE", "enforce")
    assert "atomic_with" in planner_prompt.planner_system_prompt()
    assert "atomic_with" in str(planner_prompt.plan_json_schema())


def _closed_recovery(reason: str, group: str, action: str, times: int):
    """A finished episode: ``times`` applied steps, budget kept, schedule cleared."""
    from src.tools.auto_dispatch.liveness import record

    done = record.new_record(reason, group, budget=4, at=WHEN)
    for _ in range(times):
        done = done.with_step(action, at=WHEN, result=record.APPLIED)
    return done.finish(record.RECOVERED, at=WHEN)


async def test_a_repark_after_two_reassigns_splits_instead_of_reassigning(tmp_path) -> None:
    """R-EXIT's third row stays SPLIT across a new park.

    Two recovered reassignments leave ``spent`` at 2 and clear ``next_action``.
    Re-parking used to schedule plan[0] (reassign). The sweep then selected
    SPLIT and overrode it, and MAX_REASSIGNS refused that reassign, so the
    line never split.
    """
    from src.tools.auto_dispatch.liveness import modes, record

    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path,
        gates=gates,
        config=MachineConfig(liveness_mode=modes.ENFORCE),
    )
    line = make_line("line-0", assigned_model="codex", attempts=2)
    remaining = make_line("line-1", status=LineStatus.PASSED)
    dispatch = await stored(collaborators, make_dispatch(line, remaining))
    snapshot = park_line(gates, dispatch, line, "no_new_commit")
    snapshot.update(record.store(snapshot, _closed_recovery("no_new_commit", "R-EXIT", "reassign", 2)))

    await machine._sweep_liveness(dispatch, now=WHEN)

    assert line.status is LineStatus.FAILED
    assert line.failure_reason == "split_off"
    assert line.assigned_model == "codex"
    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None
    assert updated.outcome == record.SPLIT_OFF
    assert updated.history[-1].action == "split"
    assert updated.needs_human is False


async def test_a_repark_after_one_replan_reassign_splits(tmp_path) -> None:
    """R-REPLAN's second row is SPLIT. Another reassign would still be under the cap."""
    from src.tools.auto_dispatch.liveness import modes, record

    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path,
        gates=gates,
        config=MachineConfig(liveness_mode=modes.ENFORCE),
    )
    line = make_line("line-0", assigned_model="codex", attempts=1)
    remaining = make_line("line-1", status=LineStatus.PASSED)
    dispatch = await stored(collaborators, make_dispatch(line, remaining))
    snapshot = park_line(gates, dispatch, line, "replan_parked")
    snapshot.update(
        record.store(snapshot, _closed_recovery("replan_parked", "R-REPLAN", "reassign", 1))
    )

    await machine._sweep_liveness(dispatch, now=WHEN)

    assert line.status is LineStatus.FAILED
    assert line.failure_reason == "split_off"
    assert line.assigned_model == "codex"
    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None
    assert updated.outcome == record.SPLIT_OFF
    assert updated.history[-1].action == "split"


async def test_a_different_group_starts_at_its_first_row(tmp_path) -> None:
    """Spent R-EXIT steps do not skip, or exhaust, a later R-SPLIT park."""
    from src.tools.auto_dispatch.liveness import modes, record

    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path,
        gates=gates,
        config=MachineConfig(liveness_mode=modes.ENFORCE),
    )
    line = make_line("line-0", assigned_model="codex", attempts=2)
    remaining = make_line("line-1", status=LineStatus.PASSED)
    dispatch = await stored(collaborators, make_dispatch(line, remaining))
    snapshot = park_line(gates, dispatch, line, "git_evidence_unavailable")
    snapshot.update(record.store(snapshot, _closed_recovery("no_new_commit", "R-EXIT", "reassign", 2)))

    await machine._sweep_liveness(dispatch, now=WHEN)

    assert line.failure_reason == "split_off"
    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None
    assert updated.needs_human is False
    assert updated.outcome == record.SPLIT_OFF
    assert updated.history[-1].action == "split"


async def test_replan_retargets_atomic_partners_onto_the_replacement() -> None:
    """A replanned parent must not leave its atomic partner pointing at it."""
    parent = make_line("p", status=LineStatus.FAILED, atomic_with=("b",))
    parent.failure_reason = "replanned"
    partner = make_line("b", status=LineStatus.PASSED, atomic_with=("p",))
    child = make_line("p-r1-1", status=LineStatus.PASSED)
    downstream = make_line("down", status=LineStatus.PASSED, depends_on=("p",))
    dispatch = make_dispatch(parent, partner, child, downstream)

    _retarget_dependents(dispatch, "p", (child.id,))

    assert _ready_for_g5(dispatch) is True
    assert parent.atomic_with == ()
    assert partner.atomic_with == ("p-r1-1",)
    assert child.atomic_with == ("b",)
    assert downstream.depends_on == ("p-r1-1",)


async def test_replan_does_not_invent_an_atomic_group() -> None:
    parent = make_line("p", status=LineStatus.FAILED)
    parent.failure_reason = "replanned"
    first = make_line("p-r1-1", status=LineStatus.PASSED)
    second = make_line("p-r1-2", status=LineStatus.PASSED)
    dispatch = make_dispatch(parent, first, second)

    _retarget_dependents(dispatch, "p", (first.id, second.id))

    assert first.atomic_with == ()
    assert second.atomic_with == ()
    assert _ready_for_g5(dispatch) is True


async def test_replan_atomic_retarget_survives_merge_and_copy() -> None:
    """``atomic_with`` is a line field. A reload must not point the partner at the parent."""
    from copy import deepcopy

    from src.server.services.dispatch import merge_drive
    from src.tools.auto_dispatch.types import LineStatus as Status

    before = make_dispatch(
        make_line("p", atomic_with=("b",)),
        make_line("b", status=Status.PASSED, atomic_with=("p",)),
    )
    after = deepcopy(before)
    after.line("p").status = Status.FAILED
    after.line("p").failure_reason = "replanned"
    after.lines.append(make_line("p-r1-1", status=Status.PASSED))
    _retarget_dependents(after, "p", ("p-r1-1",))
    assert _ready_for_g5(after) is True

    saved = merge_drive(deepcopy(before), before, after)
    assert _ready_for_g5(saved) is True
    assert saved.line("b").atomic_with == ("p-r1-1",)
    assert saved.line("p").atomic_with == ()
    assert saved.line("p-r1-1").atomic_with == ("b",)

    copied = deepcopy(before)
    _copy_row(copied, after)
    assert _ready_for_g5(copied) is True


async def test_a_due_split_waits_while_an_independent_line_is_still_running(tmp_path) -> None:
    """Zero PASSED is decided on the finished round, not mid-flight.

    The refusal used to latch a person permanently while the sibling was still
    RUNNING, so a later PASS could never be integrated without a human.
    """
    from datetime import timedelta

    from src.tools.auto_dispatch.liveness import modes, record

    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.ENFORCE)
    )
    stuck = make_line("line-0", assigned_model="codex")
    sibling = make_line("line-1", assigned_model="kimi")
    sibling.status = LineStatus.RUNNING
    sibling.current_run = "run-line-1-1"
    dispatch = await stored(collaborators, make_dispatch(stuck, sibling))
    dispatch.state = DispatchState.REVIEW
    cell = park_line(gates, dispatch, stuck, "git_evidence_unavailable")

    await machine._sweep_liveness(dispatch, now=WHEN)

    waiting = record.load(gates.line_snapshot(dispatch, stuck))
    assert waiting is not None
    assert not waiting.needs_human
    assert waiting.next_action == "split"
    assert waiting.spent == 0
    assert stuck.status is LineStatus.BLOCKED
    assert not (tmp_path / "followups").exists()
    assert cell is not None

    sibling.status = LineStatus.PASSED
    sibling.current_run = None
    await machine._sweep_liveness(dispatch, now=WHEN + timedelta(minutes=10))

    assert stuck.status is LineStatus.FAILED
    assert stuck.failure_reason == "split_off"
    assert (tmp_path / "followups" / "d1-line-0.md").exists()


async def test_a_split_round_still_reaches_g5_and_approval(tmp_path, monkeypatch) -> None:
    """End to end under enforce: R-REPLAN reassigns once, then splits; the
    PASSED line goes through G5 to APPROVAL, and the pack names the split."""
    from src.tools.auto_dispatch import Dispatch
    from src.tools.auto_dispatch.gates import GateSettings
    from src.tools.auto_dispatch.liveness import modes

    from .test_machine_gates import _BlockOne, _bundle, _line, _repo_with_lines

    monkeypatch.setenv(modes.MODE_ENV, modes.ENFORCE)
    monkeypatch.setenv("CONDUCTOR_DISPATCH_FOLLOWUPS_DIR", str(tmp_path / "followups"))
    repo = _repo_with_lines(tmp_path / "repo", "line-a", "line-b")
    settings = GateSettings(runs_dir=tmp_path / "runs", repo_root=repo, g5_command="pytest -q")
    collaborators, machine, _commands, _trees = _bundle(
        tmp_path,
        [_line("line-a"), _line("line-b", write_scope=("src/b.py",))],
        reviewer=_BlockOne("line-b"),
        settings=settings,
    )
    result = await machine.run(
        await collaborators.store.create(
            Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
        )
    )
    assert result.line("line-b").failure_reason == "replan_exhausted"
    for _ in range(4):
        if result.state is DispatchState.APPROVAL:
            break
        result = await machine.run(result)

    assert result.state is DispatchState.APPROVAL
    assert result.line("line-a").status is LineStatus.PASSED
    assert result.line("line-b").status is LineStatus.FAILED
    assert result.line("line-b").failure_reason == "split_off"
    assert collaborators.merger.merged == ["line-a"]
    assert set(split_off_lines(result)) == {"line-b"}
    assert not auto_approve_allowed(result)
    draft = (tmp_path / "followups" / "d1-line-b.md").read_text(encoding="utf-8")
    for expected in (
        "do line-b",
        "src/b.py",
        "cmd: pytest -q tests/unit/server/app/test_health.py",
        "**Parked reason:** replan_exhausted",
        "Escalation",
        "feature/auto-dispatch-d1/line-line-b",
    ):
        assert expected in draft, expected
    pack = (tmp_path / "proposed").rglob("*")
    text = "\n".join(p.read_text(encoding="utf-8") for p in pack if p.is_file())
    assert "line-b" in text and "split" in text.lower()
