"""F1, F2, and F3: one launch barrier, a state lock, and a released reservation."""

from __future__ import annotations

import ast
import asyncio
import copy
from pathlib import Path

import pytest

from src.server.app.background_starts import _start_tasks
from src.server.services import dispatch as dispatch_module
from src.server.services.dispatch import (
    DispatchCoordinator,
    DispatchService,
    DispatchSettings,
    _MergingStore,
    bind_process_coordinator,
)
from src.tools.auto_dispatch import Dispatch, DispatchState, Line, LineStatus, Verdict
from src.tools.auto_dispatch.gates import bump_epoch, drive_epoch
from src.tools.auto_dispatch.types import RunStatus
from tests.unit.server.services.test_dispatch_r2_stale_result import Copies
from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line

ROOT = Path(__file__).resolve().parents[4]


def _settings(tmp_path: Path) -> DispatchSettings:
    return DispatchSettings(
        proposed_dir=tmp_path / "proposed",
        runs_dir=tmp_path / "runs",
        repo_root=tmp_path,
    )


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _enclosing(node: ast.AST, parents: dict[int, ast.AST]) -> str | None:
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            return current.name
        current = parents.get(id(current))
    return None


def test_launcher_is_called_only_from_launch_guarded() -> None:
    """A fix launch cannot reach the launcher by skipping the epoch save."""
    source = (ROOT / "src/tools/auto_dispatch/machine.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents = _parent_map(tree)
    owners: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "launch":
            continue
        segment = ast.get_source_segment(source, node) or ""
        if "launcher" not in segment:
            continue
        owners.append(_enclosing(node, parents) or "")
    assert owners == ["_launch_guarded"]


class _BarrierReviewer:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def review_line(self, line_id: str, model: str) -> Verdict:
        del line_id, model
        self.entered.set()
        await self.release.wait()
        return Verdict.blocked(["stale reviewer blocker"])


@pytest.mark.asyncio
async def test_stale_review_does_not_launch_fix(tmp_path: Path) -> None:
    store = Copies()
    reviewer = _BarrierReviewer()
    line = _line("a", assigned_model="kimi", branch="feat/a")
    collab, machine, _commands, gates = _bundle(
        tmp_path / "bundle",
        [line],
        store=store,
        reviewer=reviewer,
    )
    gates.handoff_facts = lambda dispatch, item, **_kw: {
        "branch": item.branch,
        "commit": "a" * 40,
        "files": ("src/example.py",),
        "test_command": "pytest -q",
        "results": "exit 0",
        "known_limitations": "none",
    }
    row = Dispatch(
        id="stale-review-fix",
        goal="x",
        user_id="u",
        created_at="now",
        state=DispatchState.SUBMITTED,
    )
    await store.create(row)
    service = DispatchService(
        _settings(tmp_path),
        store=store,
        machine_factory=lambda _row: machine,
    )
    service._gate_runner = lambda: gates
    task = asyncio.create_task(service._drive(row.id))
    await asyncio.wait_for(reviewer.entered.wait(), timeout=10)

    def drift(current: Dispatch) -> Dispatch:
        bump_epoch(current)
        return current

    await service.apply_mutation(row.id, drift)
    reviewer.release.set()
    await asyncio.wait_for(task, timeout=10)
    stored = await store.get(row.id)
    assert stored is not None
    assert len(collab.launcher.launches) == 1
    assert stored.line("a") is not None
    run_id = stored.line("a").current_run or ""
    dispatch_id, line_id, attempt, suffix = run_id.split(":")
    assert (dispatch_id, line_id, attempt) == ("stale-review-fix", "a", "0")
    assert len(suffix) == 12
    audit = (stored.gates.get("scheduler") or {}).get("audit") or []
    assert any(isinstance(item, dict) and item.get("kind") == "stale_drive" for item in audit)
    assert machine._abandoned

    follow = asyncio.create_task(service._drive(row.id))
    await asyncio.wait_for(follow, timeout=10)
    assert not machine._abandoned
    followed = await store.get(row.id)
    assert followed is not None
    assert drive_epoch(followed) >= 1


class _HeldSave(Copies):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.held_first = False

    async def save(self, dispatch: Dispatch) -> Dispatch:
        snapshot = copy.deepcopy(dispatch)
        if not self.held_first:
            self.held_first = True
            self.entered.set()
            await self.release.wait()
        self.rows[dispatch.id] = snapshot
        return copy.deepcopy(snapshot)


@pytest.mark.asyncio
async def test_poll_completion_survives_inflight_heartbeat_save(tmp_path: Path) -> None:
    store = _HeldSave()
    line = Line(
        id="a",
        name="a",
        task="run",
        assigned_model="kimi",
        status=LineStatus.RUNNING,
        attempts=0,
        current_run="run-a",
    )
    row = Dispatch(
        id="poll-race",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        lines=[line],
    )
    collab, machine, _commands, gates = _bundle(tmp_path / "poll", [line], store=store)
    gates.prepare(row)
    await store.create(row)
    service = DispatchService(_settings(tmp_path), store=store)
    collab.store = _MergingStore(service, store, {row.id: copy.deepcopy(row)})
    local = copy.deepcopy(row)
    local_line = local.line("a")
    assert local_line is not None
    machine._epoch0 = drive_epoch(local)
    machine._baseline = copy.deepcopy(local)
    gates.beat(local)

    heartbeat = asyncio.create_task(machine._save(local))
    await asyncio.wait_for(store.entered.wait(), timeout=2)
    poll = asyncio.create_task(machine._poll_line(local, local_line))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not poll.done()
    store.release.set()
    await asyncio.wait_for(asyncio.gather(heartbeat, poll), timeout=3)
    saved = await store.get(row.id)
    assert saved is not None
    saved_line = saved.line("a")
    assert saved_line is not None
    sched = saved.gates.get("scheduler") or {}
    assert saved_line.log_tail == "done run-a"
    assert sched.get("settled_lines") == [
        {"dispatch_id": "poll-race", "line_id": "a", "run_id": "run-a", "attempt": 0}
    ]
    assert "run-run-a" in (sched.get("handled") or [])
    assert sched.get("heartbeat_at")

    class _Spy:
        def __init__(self) -> None:
            self.polls: list[str] = []

        async def poll(self, run_id: str) -> RunStatus:
            self.polls.append(run_id)
            return RunStatus(output=f"done {run_id}", complete=True, tokens=1)

        async def cancel(self, run_id: str) -> None:
            del run_id

    restored = copy.deepcopy(saved)
    _collab2, revived, _commands2, gates2 = _bundle(tmp_path / "revived", [line], store=store)
    gates2.prepare(restored)
    spy = _Spy()
    revived._c.monitor = spy
    revived._c.store = store
    await revived._adopt_settled_runs(restored)
    await revived._poll_running(restored, list(restored.lines))
    assert spy.polls == []
    assert revived._runs["a"].complete


@pytest.mark.asyncio
async def test_cancel_before_drive_starts_releases_reservation(tmp_path: Path) -> None:
    store = Copies()
    row = Dispatch(
        id="prestart",
        goal="x",
        user_id="u",
        created_at="now",
        state=DispatchState.REVIEW,
        lines=[
            Line(
                id="l",
                name="l",
                task="x",
                assigned_model="kimi",
                status=LineStatus.BLOCKED,
            )
        ],
    )
    await store.create(row)
    entered = asyncio.Event()

    class _Machine:
        async def run(self, dispatch: Dispatch) -> Dispatch:
            entered.set()
            dispatch.state = DispatchState.ASSIGNED
            return dispatch

    service = DispatchService(
        _settings(tmp_path),
        store=store,
        machine_factory=lambda _row: _Machine(),
    )
    await service._resume(row)
    key = f"dispatch-{row.id}"
    task = _start_tasks[key]
    owner = service.active_drives.get(row.id)
    assert owner is not None
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert service.active_drives.get(row.id) is None
    recovered = await service._drive(row.id)
    assert recovered.state is DispatchState.ASSIGNED
    assert service.active_drives.get(row.id) is not owner
    assert entered.is_set()
    saved = await service.reassign(row.id, "l", "stepfun")
    assert saved.line("l") is not None
    assert saved.line("l").assigned_model == "stepfun"
    _start_tasks.pop(key, None)


@pytest.mark.asyncio
async def test_stop_wakeup_drops_the_process_service() -> None:
    class _FakeService:
        def __init__(self, *, coordinator=None, **_kwargs: object) -> None:
            self._coordinator = coordinator
            self.opened = 0

        async def open(self) -> None:
            self.opened += 1

        async def close(self) -> None:
            return None

    original = dispatch_module.DispatchService
    try:
        dispatch_module.DispatchService = _FakeService  # type: ignore[misc]
        first_coordinator = DispatchCoordinator()
        bind_process_coordinator(first_coordinator)
        await dispatch_module.start_wakeup()
        first = dispatch_module._wakeup_service
        await dispatch_module.stop_wakeup()
        second_coordinator = DispatchCoordinator()
        bind_process_coordinator(second_coordinator)
        await dispatch_module.start_wakeup()
        second = dispatch_module._wakeup_service
        assert second is not first
        assert second._coordinator is second_coordinator
        assert second.opened == 1
    finally:
        dispatch_module.DispatchService = original
        await dispatch_module.stop_wakeup()
        dispatch_module._wakeup_service = None
        bind_process_coordinator(None)
