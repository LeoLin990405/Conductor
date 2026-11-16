"""N38b: a line parked by worker recovery after its worker exited can be reassigned.

The real case (R8b-2 line-1): stepfun exited 77, so worker recovery parked
the line ``worker_auth_failed``. Stage-Gate recorded the pause from
``implementing``, and ``dispatch reassign`` answered
reassignment_not_at_checkpoint. The worker had exited and nothing ran, so the
park now records that checkpoint and Stage-Gate accepts the swap. A line
whose worker may still be out keeps the refusal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.llms.native_cli.failure import FailureClass
from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch.execution import LaunchResult, WorkerLaunchFailure
from src.tools.auto_dispatch.machine import WORKER_EXITED
from tests.unit.server.services.test_dispatch_r2_stale_result import Copies, _service
from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line

pytestmark = pytest.mark.asyncio

BASE = "b" * 40
HEAD = "1" * 40


def _handoff(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "branch": "feature/b",
        "commit": "abc123",
        "files": ("b.py",),
        "test_command": "pytest",
        "results": "auth failed",
        "known_limitations": "none",
    }


class AuthThenOk:
    """The first worker exits 77 (auth failed); every later one commits."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def launch(self, line, model, skills):
        raise AssertionError("the recovery machine uses launch_worker")

    async def launch_worker(self, line, model, skills, *, session_id, session_mode, handoff_path):
        self.calls.append((line.id, model))
        run_id = line.current_run or f"run-{len(self.calls)}"
        if len(self.calls) == 1:
            raise WorkerLaunchFailure(
                run_id=run_id,
                exit_code=77,
                output="authentication failed",
                session_id=None,
                retry_after_seconds=None,
                failure_class=FailureClass.AUTH_FAILED,
                reason_code="worker_auth_failed",
            )
        return LaunchResult(run_id, session_id, "committed", start_head=BASE, head=HEAD)


async def _parked(tmp_path: Path):
    store = Copies()
    launcher = AuthThenOk()
    _collaborators, machine, _commands, gates = _bundle(
        tmp_path, [], store=store, launcher=launcher
    )
    gates.handoff_facts = _handoff
    gates._revisions = lambda _line: None
    row = Dispatch(
        id="auth-park",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.ASSIGNED,
        lines=[_line("b", status=LineStatus.ASSIGNED, assigned_model="deepseek", base_commit=BASE)],
    )
    gates.prepare(row)
    await store.create(row)
    service = _service(tmp_path, store, lambda _dispatch: machine)
    service._gate_runner = lambda: gates

    async def no_schedule(dispatch: Dispatch) -> Dispatch:
        return dispatch

    service._resume = no_schedule
    await asyncio.wait_for(service._drive(row.id), timeout=10)
    parked = await store.get(row.id)
    assert parked is not None
    return store, launcher, gates, service, parked


async def test_a_worker_that_exited_non_retryably_can_be_reassigned_and_relaunched(
    tmp_path: Path,
) -> None:
    store, launcher, _gates, service, parked = await _parked(tmp_path)
    line = parked.line("b")
    assert line is not None
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "worker_auth_failed"
    snap = parked.gates["lines"]["b"]
    assert snap["machine"]["stage"] == "blocked"
    assert snap["machine"]["blocked_from"] == "implementing"
    assert snap[WORKER_EXITED]["author"] == "deepseek"

    reassigned = await service.reassign(parked.id, "b", "stepfun")
    assert reassigned.line("b").status is LineStatus.ASSIGNED
    assert reassigned.line("b").assigned_model == "stepfun"
    assert WORKER_EXITED not in reassigned.gates["lines"]["b"]

    await asyncio.wait_for(service._drive(parked.id), timeout=10)

    after = await store.get(parked.id)
    assert after is not None
    assert launcher.calls == [("b", "deepseek"), ("b", "stepfun")]
    assert after.line("b").status is LineStatus.PASSED
    assert after.line("b").assigned_model == "stepfun"


async def test_a_pause_whose_worker_may_still_be_out_is_refused(tmp_path: Path) -> None:
    store, launcher, _gates, service, parked = await _parked(tmp_path)

    def worker_out(row: Dispatch) -> Dispatch:
        line = row.line("b")
        assert line is not None
        # A run is out again on this line: no checkpoint any more.
        line.current_run = "run-still-out"
        return row

    await service.apply_mutation(parked.id, worker_out)
    with pytest.raises(ValueError, match="^reassignment_not_at_checkpoint$"):
        await service.reassign(parked.id, "b", "stepfun")
    kept = await store.get(parked.id)
    assert kept is not None
    assert kept.line("b").assigned_model == "deepseek"
    assert launcher.calls == [("b", "deepseek")]


async def test_a_checkpoint_from_an_older_attempt_is_not_reused(tmp_path: Path) -> None:
    store, _launcher, _gates, service, parked = await _parked(tmp_path)

    def stale(row: Dispatch) -> Dispatch:
        row.gates["lines"]["b"][WORKER_EXITED]["attempt"] = -1
        return row

    await service.apply_mutation(parked.id, stale)
    with pytest.raises(ValueError, match="^reassignment_not_at_checkpoint$"):
        await service.reassign(parked.id, "b", "stepfun")


async def test_a_checkpoint_for_another_run_is_refused(tmp_path: Path) -> None:
    """The checkpoint's run must be the line's last recovery event."""
    store, _launcher, _gates, service, parked = await _parked(tmp_path)
    assert parked.gates["lines"]["b"][WORKER_EXITED]["run_id"] == (
        parked.gates["worker_recovery"]["b"]["last"]["run_id"]
    )

    def other_run(row: Dispatch) -> Dispatch:
        row.gates["worker_recovery"]["b"]["last"]["run_id"] = "auth-park:b:0:later-run"
        return row

    await service.apply_mutation(parked.id, other_run)
    with pytest.raises(ValueError, match="^reassignment_not_at_checkpoint$"):
        await service.reassign(parked.id, "b", "stepfun")
    kept = await store.get(parked.id)
    assert kept is not None
    assert kept.line("b").assigned_model == "deepseek"
