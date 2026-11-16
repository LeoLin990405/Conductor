"""A planner that raises parks the dispatch as ``planner_failed``.

The failure must not escape the drive, must not launch or review anything, and
must not store the exception text, which may carry provider output.
"""

from __future__ import annotations

import logging

import pytest

from src.tools.auto_dispatch import (
    AlwaysPassReviewer,
    Collaborators,
    Dispatch,
    DispatchMachine,
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    InMemoryOutcomeStore,
    NoopMerger,
    RecordingLauncher,
    RoundRobinSelector,
    ScriptedMonitor,
)
from src.tools.auto_dispatch.gates import PLANNER_FAILED, RESUME_SPEC_MISSING
from src.tools.auto_dispatch.planner_reason import PLANNER_ERROR
from src.tools.auto_dispatch.types import Line
from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line

pytestmark = pytest.mark.asyncio

SECRET = "provider said: sk-" "live-SECRET-TOKEN quota exceeded"


class RaisingPlanner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def plan(self, goal: str) -> list[Line]:
        self.calls.append(goal)
        raise RuntimeError(SECRET)


class EmptyPlanner:
    async def plan(self, goal: str) -> list[Line]:
        return []


async def _asleep(_seconds: float) -> None:
    return None


def _ungated(tmp_path, planner) -> Collaborators:
    return Collaborators(
        planner=planner,
        selector=RoundRobinSelector(),
        launcher=RecordingLauncher(),
        monitor=ScriptedMonitor(),
        reviewer=AlwaysPassReviewer(),
        merger=NoopMerger(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        outcomes=InMemoryOutcomeStore(),
        store=InMemoryDispatchStore(),
    )


def _submitted(dispatch_id: str = "d1") -> Dispatch:
    return Dispatch(id=dispatch_id, goal="ship it", user_id="u", created_at="now")


def _assert_no_secret(dispatch: Dispatch, caplog) -> None:
    assert SECRET not in repr(dispatch.message)
    assert "SECRET" not in repr(dispatch.gates)
    for line in dispatch.lines:
        assert "SECRET" not in repr(line)
    assert "SECRET" not in caplog.text


async def test_a_raising_planner_parks_planner_failed(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    planner = RaisingPlanner()
    collaborators = _ungated(tmp_path, planner)
    machine = DispatchMachine(collaborators, sleep=_asleep)
    dispatch = await collaborators.store.create(_submitted())

    parked = await machine.run(dispatch)

    assert planner.calls == ["ship it"]
    assert parked.state is DispatchState.REVIEW
    assert parked.message == f"{PLANNER_FAILED}:{PLANNER_ERROR}"
    assert parked.lines == []
    assert collaborators.launcher.launches == []
    assert collaborators.reviewer.reviewed == []
    stored = await collaborators.store.get("d1")
    assert stored is not None
    assert stored.state is DispatchState.REVIEW
    assert stored.message == f"{PLANNER_FAILED}:{PLANNER_ERROR}"
    assert stored.lines == []
    _assert_no_secret(stored, caplog)
    assert "RuntimeError" in caplog.text


async def test_a_raising_planner_parks_on_the_gated_path_too(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    planner = RaisingPlanner()
    collaborators, machine, _commands, _gates = _bundle(
        tmp_path, [_line("a")], planner=planner
    )
    dispatch = await collaborators.store.create(_submitted())

    parked = await machine.run(dispatch)

    assert parked.state is DispatchState.REVIEW
    assert parked.message == f"{PLANNER_FAILED}:{PLANNER_ERROR}"
    assert parked.lines == []
    assert collaborators.launcher.launches == []
    assert collaborators.reviewer.reviewed == []
    _assert_no_secret(parked, caplog)


async def test_an_empty_plan_still_fails_with_no_work_lines(tmp_path) -> None:
    collaborators = _ungated(tmp_path, EmptyPlanner())
    machine = DispatchMachine(collaborators, sleep=_asleep)
    failed = await machine.run(await collaborators.store.create(_submitted()))
    assert failed.state is DispatchState.ERROR
    assert "no work lines" in (failed.message or "")


def _planning(dispatch_id: str, spec_ref: str) -> Dispatch:
    return Dispatch(
        id=dispatch_id,
        goal="ship it",
        user_id="u",
        created_at="now",
        state=DispatchState.PLANNING,
        spec_ref=spec_ref,
    )


async def test_resume_with_a_spec_and_a_raising_planner_parks_planner_failed(
    tmp_path, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    planner = RaisingPlanner()
    collaborators, machine, _commands, _gates = _bundle(
        tmp_path, [_line("a")], planner=planner
    )
    dispatch = await collaborators.store.create(_planning("d-spec", "specs/d-spec.md"))

    parked = await machine.run(dispatch)

    assert planner.calls == ["specs/d-spec.md"]
    assert parked.state is DispatchState.REVIEW
    assert parked.message == f"{PLANNER_FAILED}:{PLANNER_ERROR}"
    assert parked.lines == []
    assert collaborators.launcher.launches == []
    assert {"kind": PLANNER_FAILED} in parked.gates["scheduler"]["audit"]
    _assert_no_secret(parked, caplog)


async def test_resume_without_a_spec_keeps_resume_spec_missing(tmp_path) -> None:
    planner = RaisingPlanner()
    collaborators, machine, _commands, _gates = _bundle(
        tmp_path, [_line("a")], planner=planner
    )
    dispatch = await collaborators.store.create(_planning("d-bare", ""))

    parked = await machine.run(dispatch)

    assert planner.calls == []
    assert parked.state is DispatchState.REVIEW
    assert parked.message == RESUME_SPEC_MISSING


async def test_resume_with_an_empty_plan_keeps_resume_spec_missing(tmp_path) -> None:
    collaborators, machine, _commands, _gates = _bundle(
        tmp_path, [_line("a")], planner=EmptyPlanner()
    )
    dispatch = await collaborators.store.create(_planning("d-empty", "specs/d-empty.md"))

    parked = await machine.run(dispatch)

    assert parked.state is DispatchState.REVIEW
    assert parked.message == RESUME_SPEC_MISSING
