"""Legacy parks: lines parked before N40/N42 have none of the new markers.

The old code parked ``review_unavailable`` by blocking the line and leaving
Stage-Gate at the review stage, with no ``review_parked`` / ``review_retry``
record (four prod lines on 2026-11-11: ee81fda5 line-0, 252dad16 line-0-r1-3,
4431c361 line-1-r1-1, dada936b line-1). ``dispatch unblock`` answered
``line_not_wedged`` and ``dispatch reassign`` answered ``stage_gate_order``.
A fix round that made no commit was parked ``no_new_commit`` in ``fixing``
(ee81fda5 line-1), which unblock also refused. Both are unblockable now.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus, Verdict
from src.tools.auto_dispatch.machine import REVIEW_PARKED, WORKER_EXITED
from tests.unit.server.services.test_dispatch_unwedge import (
    ScriptedLauncher,
    _assigned,
    _drive,
    _setup,
)

pytestmark = pytest.mark.asyncio


class Outage:
    """Every review fails while ``down``; then each one passes."""

    def __init__(self) -> None:
        self.down = True
        self.calls: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.calls.append((line_id, model))
        if self.down:
            raise RuntimeError("reviewer transport down")
        return Verdict.ok(f"ok from {model}")


def _legacy_review_park(row: Dispatch, *, blocked_from_review: bool = False) -> Dispatch:
    """Strip what N42 writes, leaving the row the old code left behind."""
    snap = row.gates["lines"]["a"]
    snap.pop(REVIEW_PARKED, None)
    snap.pop("review_retry", None)
    snap["review_attempts"] = 3
    machine = dict(snap["machine"])
    if blocked_from_review:
        machine["stage"] = "blocked"
        machine["blocked_from"] = "primary_review"
    else:
        machine["stage"] = "primary_review"
        machine["blocked_from"] = ""
    snap["machine"] = machine
    snap["events"] = [
        event for event in snap.get("events", []) if event.get("to_stage") != "blocked"
    ]
    line = row.line("a")
    line.status = LineStatus.BLOCKED
    line.failure_reason = "review_unavailable"
    line.current_run = None
    line.blockers = []
    row.state = DispatchState.LAUNCHING
    return row


async def _parked_by_outage(tmp_path: Path):
    reviewer = Outage()
    store, launcher, gates, service, row = await _setup(
        tmp_path, [_assigned("a", "deepseek")], reviewer=reviewer
    )
    await _drive(service, row.id)
    parked = await store.get(row.id)
    assert parked.line("a").failure_reason == "review_unavailable"
    return store, launcher, reviewer, service, parked


@pytest.mark.parametrize("blocked_from_review", [False, True])
async def test_a_legacy_review_park_is_unblocked_and_the_review_resumes(
    tmp_path: Path, blocked_from_review: bool
) -> None:
    store, launcher, reviewer, service, parked = await _parked_by_outage(tmp_path)
    settled = parked.gates["scheduler"]["settled_lines"][-1]["run_id"]
    await service.apply_mutation(
        parked.id,
        lambda row: _legacy_review_park(row, blocked_from_review=blocked_from_review),
    )
    legacy = await store.get(parked.id)
    assert REVIEW_PARKED not in legacy.gates["lines"]["a"]
    if not blocked_from_review:
        # What the operator saw: Stage-Gate still at the review stage.
        with pytest.raises(ValueError, match="^stage_gate_order$"):
            await service.reassign(parked.id, "a", "stepfun")

    unblocked = await service.unblock(parked.id, "a", note="codex is back")
    line = unblocked.line("a")
    assert line.status is LineStatus.RUNNING
    assert line.current_run == settled
    assert line.failure_reason is None
    snap = unblocked.gates["lines"]["a"]
    assert snap["machine"]["stage"] == "primary_review"
    assert "review_attempts" not in snap
    audit = unblocked.gates["scheduler"]["audit"]
    assert any(
        item.get("kind") == "operator_unblock" and item.get("legacy") for item in audit
    )

    reviewer.down = False
    runs = len(launcher.calls)
    await _drive(service, parked.id)
    after = await store.get(parked.id)
    assert after.line("a").status is LineStatus.PASSED
    # The review resumed on the finished run: nothing was relaunched.
    assert len(launcher.calls) == runs


async def test_a_legacy_review_park_without_a_settled_run_is_refused(tmp_path: Path) -> None:
    store, _launcher, _reviewer, service, parked = await _parked_by_outage(tmp_path)

    def no_run(row: Dispatch) -> Dispatch:
        row = _legacy_review_park(row)
        row.gates["scheduler"]["settled_lines"] = []
        return row

    await service.apply_mutation(parked.id, no_run)
    with pytest.raises(ValueError, match="^no_settled_run$"):
        await service.unblock(parked.id, "a")
    kept = await store.get(parked.id)
    assert kept.line("a").status is LineStatus.BLOCKED


async def test_a_legacy_review_park_with_a_run_out_is_refused(tmp_path: Path) -> None:
    store, _launcher, _reviewer, service, parked = await _parked_by_outage(tmp_path)

    def run_out(row: Dispatch) -> Dispatch:
        row = _legacy_review_park(row)
        row.line("a").current_run = "wedge:a:0:out"
        return row

    await service.apply_mutation(parked.id, run_out)
    with pytest.raises(ValueError, match="^run_out$"):
        await service.unblock(parked.id, "a")


async def test_a_legacy_no_new_commit_park_in_fixing_is_unblockable(tmp_path: Path) -> None:
    """ee81fda5 line-1: a fix round that made no commit, parked in ``fixing``."""
    launcher = ScriptedLauncher({"a": ["no_commit"]})
    store, launcher, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "stepfun")], launcher=launcher
    )
    await _drive(service, row.id)

    def legacy(r: Dispatch) -> Dispatch:
        snap = r.gates["lines"]["a"]
        snap.pop(WORKER_EXITED, None)
        machine = dict(snap["machine"])
        machine.update({"stage": "fixing", "blocked_from": "", "return_stage": "focused_tests"})
        snap["machine"] = machine
        recovery = r.gates.get("worker_recovery", {}).get("a")
        if isinstance(recovery, dict):
            recovery.pop("last", None)
        return r

    await service.apply_mutation(row.id, legacy)
    parked = copy.deepcopy(await store.get(row.id))
    assert parked.line("a").failure_reason == "no_new_commit"

    unblocked = await service.unblock(row.id, "a")
    snap = unblocked.gates["lines"]["a"]
    assert snap["machine"]["stage"] == "blocked"
    assert snap["machine"]["blocked_from"] == "fixing"
    assert snap[WORKER_EXITED]["failure_class"] == "operator_unblock"

    reassigned = await service.reassign(row.id, "a", "kimi")
    assert reassigned.line("a").assigned_model == "kimi"
    assert reassigned.line("a").status is LineStatus.ASSIGNED


async def test_a_line_already_at_its_checkpoint_says_so(tmp_path: Path) -> None:
    launcher = ScriptedLauncher({"a": ["no_commit"]})
    _store, _launcher, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "stepfun")], launcher=launcher
    )
    await _drive(service, row.id)
    with pytest.raises(ValueError, match="^already_at_checkpoint$"):
        await service.unblock(row.id, "a")
