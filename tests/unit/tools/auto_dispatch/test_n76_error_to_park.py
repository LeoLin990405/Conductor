"""N76: the four ex-ERROR parks are recoverable REVIEW stops.

G5 configuration/failure, no secondary reviewer and no eligible worker used
to end the dispatch as ERROR, where neither ``recover`` nor ``cancel`` could
reach it. They now park the line, leave the dispatch in REVIEW and hand the
line to the liveness policy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.server.services.autonomy.stage_gate import Stage
from src.tools.auto_dispatch import MachineConfig
from src.tools.auto_dispatch.liveness import modes, record
from src.tools.auto_dispatch.machine import _load_state, _store_state
from src.tools.auto_dispatch.protocols import DispatchError
from src.tools.auto_dispatch.types import DispatchState, LineStatus

from .n76_fakes import make_dispatch, make_gates, make_line, make_machine, stored

pytestmark = pytest.mark.asyncio

WHEN = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


class _NoSelector:
    """A selector that refuses both work and review for one reason."""

    def __init__(self, *, refusal: str) -> None:
        self.review_refusal = refusal
        self.work_refusal = refusal
        self.work_decision = None

    async def select(self, line, used, *, role=None, **kwargs):
        return None


def _integration_snapshot(gates, dispatch, line) -> dict[str, object]:
    snapshot = gates.line_snapshot(dispatch, line)
    state = _load_state(snapshot)
    _store_state(snapshot, _replace_stage(state, Stage.INTEGRATION_CHECKS))
    return snapshot


def _replace_stage(state, stage):
    from dataclasses import replace

    return replace(state, stage=stage, blocked_from="")


async def test_a_missing_g5_command_parks_in_review(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.ENFORCE)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    _integration_snapshot(gates, dispatch, line)
    line.status = LineStatus.PASSED

    await machine._park_g5_failure(
        dispatch, SimpleNamespace(reason="g5_not_configured", check="full_suite")
    )

    assert dispatch.state is DispatchState.REVIEW
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "g5_not_configured"
    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None and updated.needs_human
    assert "needs a person" in (dispatch.message or "")
    assert "g5_not_configured" in (dispatch.message or "")


@pytest.mark.parametrize("mode", [modes.OFF, modes.SHADOW])
async def test_g5_failure_is_main_error_outside_enforce(tmp_path, mode) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=mode)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    _integration_snapshot(gates, dispatch, line)
    line.status = LineStatus.PASSED

    await machine._park_g5_failure(
        dispatch, SimpleNamespace(reason="g5_not_configured", check="full_suite")
    )

    assert dispatch.state is DispatchState.ERROR
    assert dispatch.message == (
        "G5 integration checks failed on integrate/auto-dispatch-d1: full_suite"
    )
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "g5_not_configured"
    updated = record.load(gates.line_snapshot(dispatch, line))
    if mode == modes.SHADOW:
        assert updated is not None
        assert not updated.needs_human
        assert updated.history[-1].action == "human"
        assert updated.history[-1].result == record.SHADOW
    else:
        assert updated is None


async def test_a_failing_g5_suite_parks_instead_of_error(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.ENFORCE)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    _integration_snapshot(gates, dispatch, line)
    line.status = LineStatus.PASSED

    await machine._park_g5_failure(
        dispatch, SimpleNamespace(reason="g5_full_suite_failed", check="full_suite")
    )

    assert dispatch.state is DispatchState.REVIEW
    assert line.failure_reason == "g5_full_suite_failed"
    assert line.status is LineStatus.BLOCKED
    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None and updated.needs_human


async def test_an_unknown_g5_reason_is_normalized_to_a_known_park(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.ENFORCE)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    _integration_snapshot(gates, dispatch, line)
    line.status = LineStatus.PASSED

    await machine._park_g5_failure(
        dispatch, SimpleNamespace(reason="alembic_unparseable", check="alembic_single_head")
    )

    assert line.failure_reason == "g5_full_suite_failed"
    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None and updated.needs_human


async def test_no_secondary_reviewer_parks_the_line(tmp_path) -> None:
    gates = make_gates(tmp_path)
    selector = _NoSelector(refusal="no_secondary_reviewer")
    machine, collaborators, _ = make_machine(
        tmp_path,
        gates=gates,
        selector=selector,
        config=MachineConfig(liveness_mode=modes.ENFORCE),
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    snapshot = gates.line_snapshot(dispatch, line)
    _store_state(snapshot, _replace_stage(_load_state(snapshot), Stage.PRIMARY_REVIEW))
    line.status = LineStatus.RUNNING

    await machine._review_once(dispatch, line, secondary=False)

    assert dispatch.state is DispatchState.REVIEW
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "no_secondary_reviewer"
    assert "no_secondary_reviewer" in (dispatch.message or "")
    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None
    assert not updated.needs_human
    assert updated.group == "R-QUOTA"


async def test_no_eligible_worker_parks_the_line(tmp_path) -> None:
    gates = make_gates(tmp_path)
    selector = _NoSelector(refusal="text_only_worker")
    machine, collaborators, _ = make_machine(
        tmp_path,
        gates=gates,
        selector=selector,
        config=MachineConfig(liveness_mode=modes.ENFORCE),
    )
    line = make_line(write_scope=("src/a.py",))
    dispatch = await stored(collaborators, make_dispatch(line))

    await machine._assign(dispatch)

    assert dispatch.state is DispatchState.REVIEW
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "no_eligible_worker"
    assert "No eligible worker" in (dispatch.message or "")


async def test_parking_one_unstaffable_line_still_assigns_the_others(tmp_path) -> None:
    """A capacity park must not drop assignments already chosen for other lines.

    Returning from the selector loop left those lines PENDING. REVIEW resumes
    through the gated drive, which does not assign, and the PENDING lines
    later raise a false DAG error.
    """

    class Selector:
        def __init__(self) -> None:
            self.work_refusal: str | None = None

        async def select(self, line, used, **kwargs):
            if line.id == "line-1":
                self.work_refusal = "text_only_worker"
                return None
            self.work_refusal = None
            return "codex" if line.id == "line-0" else "claude"

    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path,
        gates=gates,
        selector=Selector(),
        config=MachineConfig(liveness_mode=modes.ENFORCE),
    )
    first = make_line("line-0", write_scope=("src/a.py",))
    blocked = make_line("line-1", write_scope=("src/b.py",))
    third = make_line("line-2", write_scope=("src/c.py",))
    dispatch = await stored(collaborators, make_dispatch(first, blocked, third))

    await machine._assign(dispatch)

    assert dispatch.state is DispatchState.REVIEW
    assert first.status is LineStatus.ASSIGNED
    assert first.assigned_model == "codex"
    assert blocked.status is LineStatus.BLOCKED
    assert blocked.failure_reason == "no_eligible_worker"
    assert third.status is LineStatus.ASSIGNED
    assert third.assigned_model == "claude"
    assert "No eligible worker" in (dispatch.message or "")


async def test_off_mode_still_errors_when_no_worker_can_write(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path,
        gates=gates,
        selector=_NoSelector(refusal="text_only_worker"),
        config=MachineConfig(liveness_mode=modes.OFF),
    )
    line = make_line(write_scope=("src/a.py",))
    dispatch = await stored(collaborators, make_dispatch(line))

    with pytest.raises(DispatchError, match="no_eligible_worker"):
        await machine._assign(dispatch)


async def test_git_evidence_unavailable_parks_in_enforce(tmp_path) -> None:
    """A launch with no git evidence stays recoverable instead of ERROR."""
    from unittest.mock import AsyncMock

    from src.tools.auto_dispatch.protocols import DispatchError as LaunchDispatchError

    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.ENFORCE)
    )
    line = make_line(
        assigned_model="codex",
        branch="line",
        base_commit="a" * 40,
        status=LineStatus.ASSIGNED,
    )
    dispatch = make_dispatch(line)
    dispatch.state = DispatchState.ASSIGNED
    dispatch.gates = {"base_commit": "a" * 40}
    gates.prepare(dispatch)
    collaborators.launcher.launch_worker = AsyncMock(
        side_effect=LaunchDispatchError("Git evidence for line line-0 is unavailable")
    )
    await collaborators.store.create(dispatch)

    result = await machine.run(dispatch)

    assert result.state is DispatchState.REVIEW
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "git_evidence_unavailable"
    parked = record.load(gates.line_snapshot(dispatch, line))
    assert parked is not None
    assert parked.group == "R-SPLIT"


async def test_git_evidence_unavailable_stays_error_off_mode(tmp_path) -> None:
    from unittest.mock import AsyncMock

    from src.tools.auto_dispatch.protocols import DispatchError as LaunchDispatchError

    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.OFF)
    )
    line = make_line(
        assigned_model="codex",
        branch="line",
        base_commit="a" * 40,
        status=LineStatus.ASSIGNED,
    )
    dispatch = make_dispatch(line)
    dispatch.state = DispatchState.ASSIGNED
    dispatch.gates = {"base_commit": "a" * 40}
    gates.prepare(dispatch)
    collaborators.launcher.launch_worker = AsyncMock(
        side_effect=LaunchDispatchError("Git evidence for line line-0 is unavailable")
    )
    await collaborators.store.create(dispatch)

    result = await machine.run(dispatch)

    assert result.state is DispatchState.ERROR
    assert line.status is LineStatus.FAILED
    assert line.failure_reason == "git_evidence_unavailable"
