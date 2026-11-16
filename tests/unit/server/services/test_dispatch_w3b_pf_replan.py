"""A pending fix refuses human reassign. A stale intent does not overwrite the line."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.server.services.dispatch import DispatchService, DispatchSettings
from src.tools.auto_dispatch import Dispatch
from src.tools.auto_dispatch.gates import FIX_LAUNCH_FAILED, HANDOFF_INCOMPLETE
from tests.unit.tools.auto_dispatch.test_w3b_actor import (
    _FixLauncher,
    _HoldSibling,
    _JsonStore,
    _OnceBlock,
    _direct_reservation,
    _pair,
    _prepared,
    _solo,
    _watch_intent,
    _watch_tasks,
)

pytestmark = pytest.mark.asyncio


def _service(tmp_path: Path, store: _JsonStore, gates: object) -> DispatchService:
    service = DispatchService(
        DispatchSettings(
            proposed_dir=tmp_path / "proposed",
            runs_dir=tmp_path / "runs-svc",
            repo_root=tmp_path,
        ),
        store=store,
        machine_factory=lambda _dispatch: None,
    )
    service._gate_runner = lambda: gates

    async def no_schedule(row: Dispatch) -> Dispatch:
        return row

    service._resume = no_schedule
    return service


def _dump(row: Dispatch) -> str:
    return json.dumps(row.to_dict(), sort_keys=True)


def _audit_kinds(gates: object) -> list[object]:
    if not isinstance(gates, dict):
        return []
    scheduler = gates.get("scheduler")
    if not isinstance(scheduler, dict):
        return []
    audit = scheduler.get("audit")
    if not isinstance(audit, list):
        return []
    return [item.get("kind") for item in audit if isinstance(item, dict)]


def _line_row(snapshot: dict, line_id: str = "a") -> dict:
    return next(item for item in snapshot["lines"] if item["id"] == line_id)


async def _deferred_snapshot(tmp_path: Path, kind: str):
    """The first pending_fix save while a sibling can still defer the fix."""
    from src.tools.auto_dispatch.gates import CommandResult
    from tests.unit.tools.auto_dispatch.test_gate_recovery import ScriptedCommands

    repo, lines = _pair(tmp_path)
    overrides: dict[str, object] = {}
    if kind == "review":
        overrides["reviewer"] = _OnceBlock("missing regression test")
    else:
        overrides["commands"] = ScriptedCommands(
            script=[CommandResult(exit_code=1, duration_ms=4)]
        )
    collaborators, machine, gates, restored = _prepared(
        tmp_path, lines, repo, launcher=_FixLauncher(fail=False), **overrides
    )
    tasks = _watch_tasks(machine)
    seen = _watch_intent(collaborators.store)
    collaborators.monitor = _HoldSibling()
    collaborators.monitor.release.set()
    await collaborators.store.create(restored)
    await asyncio.wait_for(machine.run(restored), timeout=8)
    await machine.aclose()
    snapshot = seen["row"]
    assert isinstance(snapshot, dict)
    return repo, tasks, snapshot, gates


async def _pending_snapshot(tmp_path: Path, mode: str, kind: str):
    if mode == "direct":
        repo, tasks, seen = await _direct_reservation(tmp_path / "capture", kind=kind)
        snapshot = seen["reservation"]
        assert isinstance(snapshot, dict)
        assert "reserved_run_id" in snapshot["gates"]["pending_fix"]["a"]
        return repo, tasks, snapshot
    repo, tasks, snapshot, _gates = await _deferred_snapshot(tmp_path / "capture", kind)
    assert "reserved_run_id" not in snapshot["gates"]["pending_fix"]["a"]
    return repo, tasks, snapshot


async def _refuse(tmp_path: Path, snapshot: dict) -> None:
    row = Dispatch.from_dict(json.loads(json.dumps(snapshot)))
    store = _JsonStore()
    await store.create(row)
    stored = await store.get(row.id)
    assert stored is not None
    before = _dump(stored)
    held = store.rows[row.id]
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    _collaborators, _machine, gates, _restored = _prepared(
        tmp_path / "gates", [], repo_root
    )
    service = _service(tmp_path, store, gates)
    saves = {"n": 0}
    original = store.save

    async def save(dispatch: Dispatch) -> Dispatch:
        saves["n"] += 1
        return await original(dispatch)

    store.save = save
    with pytest.raises(ValueError, match="^fix_pending$"):
        await service.reassign(row.id, "a", "stepfun")
    assert saves["n"] == 0
    assert store.rows[row.id] is held
    after = await store.get(row.id)
    assert after is not None
    assert _dump(after) == before
    landed = after.line("a")
    assert landed is not None
    assert landed.assigned_model == "kimi"
    assert landed.attempts == 0
    assert after.gates.get("pending_fix", {}).get("a") == snapshot["gates"]["pending_fix"]["a"]


async def _resume_fix(tmp_path: Path, repo: Path, snapshot: dict, tasks: list[str], *, mode: str) -> None:
    restarted = Dispatch.from_dict(json.loads(json.dumps(snapshot)))
    launcher = _FixLauncher(fail=False)
    collaborators, machine, _gates, _restored = _prepared(
        tmp_path / "resume", [], repo, launcher=launcher
    )
    if mode == "deferred":
        collaborators.monitor = _HoldSibling()
        collaborators.monitor.release.set()
    await collaborators.store.create(restarted)
    await asyncio.wait_for(machine.run(restarted), timeout=8)
    await machine.aclose()
    fix_calls = [call for call in launcher.calls if call["line"] == "a"]
    assert tasks
    assert fix_calls
    assert all(call["task"] == tasks[0] for call in fix_calls)
    assert all(call["model"] == "kimi" and call["attempts"] == 0 for call in fix_calls)
    line = restarted.line("a")
    assert line is not None
    assert line.assigned_model == "kimi"
    assert line.attempts == 0
    assert line.failure_reason != HANDOFF_INCOMPLETE


@pytest.mark.parametrize("mode", ["direct", "deferred"])
@pytest.mark.parametrize("kind", ["review", "g2"])
async def test_reassign_during_pending_fix_writes_nothing_and_the_fix_still_launches(
    tmp_path: Path, mode: str, kind: str
) -> None:
    """409 fix_pending leaves the row byte-identical. The original author still launches."""
    repo, tasks, snapshot = await _pending_snapshot(tmp_path, mode, kind)
    await _refuse(tmp_path / "refuse", snapshot)
    await _resume_fix(tmp_path / "after", repo, snapshot, tasks, mode=mode)


async def _parked(tmp_path: Path, mode: str, kind: str):
    from src.tools.auto_dispatch.gates import CommandResult
    from tests.unit.tools.auto_dispatch.test_gate_recovery import ScriptedCommands

    if mode == "direct":
        repo, lines = _solo(tmp_path)
    else:
        repo, lines = _pair(tmp_path)
    overrides: dict[str, object] = {}
    if kind == "review":
        overrides["reviewer"] = _OnceBlock("missing regression test")
    else:
        overrides["commands"] = ScriptedCommands(
            script=[CommandResult(exit_code=1, duration_ms=4)]
        )
    collaborators, machine, gates, restored = _prepared(
        tmp_path, lines, repo, launcher=_FixLauncher(fail=True), **overrides
    )
    tasks = _watch_tasks(machine)
    if mode == "deferred":
        collaborators.monitor = _HoldSibling()
        collaborators.monitor.release.set()
    await collaborators.store.create(restored)
    await asyncio.wait_for(machine.run(restored), timeout=8)
    await machine.aclose()
    parked = await collaborators.store.get(restored.id)
    assert parked is not None
    line = parked.line("a")
    assert line is not None
    assert line.assigned_model == "kimi"
    assert line.attempts == 0
    assert line.failure_reason == FIX_LAUNCH_FAILED
    assert "pending_fix" not in parked.gates
    return repo, tasks, json.loads(json.dumps(parked.to_dict())), gates


@pytest.mark.parametrize("mode", ["direct", "deferred"])
@pytest.mark.parametrize("kind", ["review", "g2"])
async def test_reassign_after_the_fix_parks_starts_attempt_one(
    tmp_path: Path, mode: str, kind: str
) -> None:
    """After the fix is parked, stepfun starts a new cycle and the old task stays put."""
    repo, tasks, snapshot, gates = await _parked(tmp_path / "park", mode, kind)
    assert tasks
    old_task = tasks[0]
    row = Dispatch.from_dict(snapshot)
    store = _JsonStore()
    await store.create(row)
    service = _service(tmp_path / "svc", store, gates)
    accepted = await service.reassign(row.id, "a", "stepfun")
    landed = accepted.line("a")
    assert landed is not None
    assert landed.assigned_model == "stepfun"
    assert landed.attempts == 1
    assert landed.failure_reason != HANDOFF_INCOMPLETE
    assert "pending_fix" not in accepted.gates
    launcher = _FixLauncher(fail=False)
    collaborators, machine, _gates, _restored = _prepared(
        tmp_path / "next", [], repo, launcher=launcher
    )
    if mode == "deferred":
        collaborators.monitor = _HoldSibling()
        collaborators.monitor.release.set()
    await collaborators.store.create(accepted)
    await asyncio.wait_for(machine.run(accepted), timeout=8)
    await machine.aclose()
    calls = [call for call in launcher.calls if call["line"] == "a"]
    assert calls
    assert all(call["task"] != old_task for call in calls)
    # The parked fix intent is not reused. The new model gets a fresh
    # reassign prompt instead: the line task, a handoff, and the previous
    # blockers, built like a fix round's (N25b).
    assert all("Blockers:\n" in str(call["task"]) for call in calls)
    assert all(call["model"] == "stepfun" and call["attempts"] == 1 for call in calls)
    final = accepted.line("a")
    assert final is not None
    assert final.assigned_model == "stepfun"
    assert final.attempts == 1
    assert final.failure_reason != HANDOFF_INCOMPLETE


@pytest.mark.parametrize("mode", ["direct", "deferred"])
@pytest.mark.parametrize("field", ["author", "epoch"])
async def test_a_stale_fix_intent_is_cleared_without_overwriting_the_line(
    tmp_path: Path, mode: str, field: str
) -> None:
    """Restart drops an intent whose author or epoch disagrees and keeps the line."""
    repo, tasks, snapshot = await _pending_snapshot(tmp_path / "capture", mode, "review")
    assert tasks
    old_task = tasks[0]
    edited = json.loads(json.dumps(snapshot))
    line_row = _line_row(edited)
    intent = edited["gates"]["pending_fix"]["a"]
    if field == "author":
        line_row["assigned_model"] = "stepfun"
        line_row["attempts"] = 1
        expected_author = "stepfun"
        expected_attempt = 1
    else:
        intent["epoch"] = int(intent.get("epoch") or 0) + 9
        expected_author = line_row["assigned_model"]
        expected_attempt = line_row["attempts"]
    row = Dispatch.from_dict(edited)
    launcher = _FixLauncher(fail=False)
    collaborators, machine, _gates, _restored = _prepared(
        tmp_path / "resume", [], repo, launcher=launcher
    )
    if mode == "deferred":
        collaborators.monitor = _HoldSibling()
        collaborators.monitor.release.set()
    seen: dict[str, object] = {}
    original = collaborators.store.save

    async def save(dispatch: Dispatch) -> Dispatch:
        saved = await original(dispatch)
        if "row" not in seen and "stale_fix_intent" in _audit_kinds(saved.gates):
            seen["row"] = json.loads(json.dumps(saved.to_dict()))
        return saved

    collaborators.store.save = save
    await collaborators.store.create(row)
    await asyncio.wait_for(machine.run(row), timeout=8)
    await machine.aclose()
    cleared = seen["row"]
    assert isinstance(cleared, dict)
    cleared_line = _line_row(cleared)
    assert cleared_line["assigned_model"] == expected_author
    assert cleared_line["attempts"] == expected_attempt
    assert "a" not in (cleared["gates"].get("pending_fix") or {})
    assert "stale_fix_intent" in _audit_kinds(cleared["gates"])
    assert all(call["task"] != old_task for call in launcher.calls)
    final = row.line("a")
    assert final is not None
    assert final.assigned_model == expected_author
    assert final.attempts == expected_attempt
    assert final.failure_reason != HANDOFF_INCOMPLETE
