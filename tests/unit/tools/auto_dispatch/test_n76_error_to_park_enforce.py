"""N76 §4.4 ERROR -> park, driven through the existing W3a/N38 harnesses.

The pre-N76 tests in ``test_machine.py``, ``test_machine_gates.py`` and
``test_n38_unreviewed_head.py`` stay exactly as they are on main and run in the
default liveness mode, so they keep proving that ``shadow`` ends these drives
in main's ERROR. The same drives under ``enforce`` park instead; they live here
so no existing assertion had to change.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.tools.auto_dispatch import DispatchMachine, DispatchState, LineStatus
from src.tools.auto_dispatch.gates import NO_SECONDARY_REVIEWER, GateSettings
from src.tools.auto_dispatch.liveness import modes
from src.tools.auto_dispatch.selection import QuotaAwareModelSelector
from src.tools.auto_dispatch.types import Line
from tests.unit.tools.auto_dispatch import test_machine as machine_tests
from tests.unit.tools.auto_dispatch.test_machine_gates import (
    MARKER,
    CommandResult,
    FakeWorktrees,
    ScriptedCommands,
    _bundle,
    _line,
    _run,
    _syntax_migration,
    _two_heads,
)
from tests.unit.tools.auto_dispatch.test_n34_committed_retry import (
    BASE,
    H1,
    ScriptedWorkers,
    _dispatch,
    _handoff,
    _seed_runs,
)
from tests.unit.tools.auto_dispatch.test_n38_unreviewed_head import _seed_ledger


@pytest.fixture(autouse=True)
def _enforce(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(modes.MODE_ENV, modes.ENFORCE)
    monkeypatch.setenv("CONDUCTOR_DISPATCH_FOLLOWUPS_DIR", str(tmp_path / "followups"))


def _parked(result) -> None:
    assert result.state is DispatchState.REVIEW
    assert "needs a person" in (result.message or "")


@pytest.mark.asyncio
async def test_two_alembic_heads_park_for_a_person(tmp_path: Path) -> None:
    _c, _m, _cmd, _t, failed = await _run(
        tmp_path, [_line("line-0")], trees=FakeWorktrees(seed=_two_heads)
    )
    _parked(failed)
    assert failed.line("line-0").failure_reason == "g5_full_suite_failed"
    assert failed.gates["integration"]["reason"] == "alembic_multiple_heads"


@pytest.mark.asyncio
async def test_an_alembic_syntax_error_parks_for_a_person(tmp_path: Path) -> None:
    _c, _m, _cmd, _t, failed = await _run(
        tmp_path, [_line("line-0")], trees=FakeWorktrees(seed=_syntax_migration)
    )
    _parked(failed)
    check = failed.gates["integration"]["checks"]["alembic_single_head"]
    assert check["reason"] == "alembic_unparseable"


@pytest.mark.asyncio
async def test_a_failing_full_suite_parks_without_integrating(tmp_path: Path) -> None:
    commands = ScriptedCommands([CommandResult(0, 5), CommandResult(exit_code=2, duration_ms=9)])
    collaborators, _m, _cmd, _t, failed = await _run(
        tmp_path, [_line("line-0")], commands=commands
    )
    assert failed.state is not DispatchState.APPROVAL
    _parked(failed)
    assert failed.line("line-0").failure_reason == "g5_full_suite_failed"
    assert failed.gates["integration"]["reason"] == "g5_full_suite_failed"
    assert MARKER not in (failed.message or "")
    # The line was merged into the integrate branch for G5 (as on main), but
    # the round stops before APPROVAL, so nothing is proposed or pushed.
    assert collaborators.merger.pushed == []


@pytest.mark.asyncio
async def test_an_unset_g5_command_parks_for_a_person(tmp_path: Path) -> None:
    settings = GateSettings(
        runs_dir=tmp_path / "runs", repo_root=tmp_path / "repo", g5_command=None
    )
    _c, _m, _cmd, _t, failed = await _run(tmp_path, [_line("line-0")], settings=settings)
    _parked(failed)
    assert failed.line("line-0").failure_reason == "g5_not_configured"


@pytest.mark.asyncio
async def test_no_secondary_reviewer_parks_the_line(tmp_path: Path) -> None:
    selector = QuotaAwareModelSelector(review_refused=lambda model: model in {"codex", "stepfun"})
    collaborators, _m, _cmd, _t, failed = await _run(
        tmp_path, [_line("line-0")], selector=selector
    )
    assert failed.state is DispatchState.REVIEW
    assert NO_SECONDARY_REVIEWER in (failed.message or "")
    assert failed.line("line-0").status is LineStatus.BLOCKED
    assert failed.line("line-0").failure_reason == NO_SECONDARY_REVIEWER
    assert collaborators.merger.merged == []


@pytest.mark.asyncio
async def test_a_text_only_pool_parks_no_eligible_worker(tmp_path: Path) -> None:
    from src.tools.auto_dispatch.pool import eligible_models

    roster = eligible_models("work")

    class Headroom:
        async def remaining(self, model: str) -> float:
            return 0.0 if model in roster and model != "doubao" else 1.0

    selector = QuotaAwareModelSelector(Headroom())
    line = Line(id="line-0", name="edit", task="edit the file", write_scope=("src/a.py",))

    class Planner:
        async def plan(self, goal: str) -> list[Line]:
            return [line]

    machine, _recorder = machine_tests._machine(
        machine_tests._collaborators(tmp_path, planner=Planner(), selector=selector)
    )
    dispatch = await machine.run(machine_tests._dispatch())
    assert dispatch.state is DispatchState.REVIEW
    assert "No eligible worker" in (dispatch.message or "")
    assert dispatch.lines[0].status is LineStatus.BLOCKED
    assert dispatch.lines[0].failure_reason == "no_eligible_worker"
    assert selector.work_refusal == "text_only_worker"


def test_the_availability_probe_parks_no_secondary_reviewer(tmp_path: Path) -> None:
    from src.tools.auto_dispatch.selection import ModelRole

    class Selector:
        def __init__(self) -> None:
            self.review_refusal: str | None = None
            self.review_calls = 0

        async def select(self, line, used, *, role=None):
            if role is not ModelRole.REVIEW:
                return "kimi"
            self.review_calls += 1
            self.review_refusal = "no_secondary_reviewer" if "deepseek" in used else "pool_empty"
            return None

    async def scenario() -> None:
        async def sleep(_seconds: float) -> None:
            return None

        selector = Selector()
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path, [], launcher=ScriptedWorkers([("empty", H1, True)]), selector=selector
        )
        assert isinstance(machine, DispatchMachine)
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
        assert result.state is DispatchState.REVIEW
        assert line.status is LineStatus.BLOCKED
        assert line.failure_reason == "no_secondary_reviewer"
        assert selector.review_calls == 3

    asyncio.run(scenario())
