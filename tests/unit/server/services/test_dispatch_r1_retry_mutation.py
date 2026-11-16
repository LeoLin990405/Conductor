"""R1: a wakeup retry stamp is a mutation, and it cannot roll back reassign."""

from __future__ import annotations

import ast
import asyncio
import copy
from pathlib import Path

import pytest

from src.server.services.dispatch import DispatchService, DispatchSettings
from src.server.services.dispatch_wakeup import DispatchWakeup, MemoryWakeupLease
from src.tools.auto_dispatch import (
    Dispatch,
    DispatchState,
    InMemoryDispatchStore,
    Line,
    LineStatus,
)

pytestmark = pytest.mark.asyncio

ROOT = Path(__file__).resolve().parents[4]
SCAN = (
    ROOT / "src/server/services/dispatch_wakeup.py",
    ROOT / "src/server/services/dispatch.py",
    ROOT / "src/tools/auto_dispatch/machine.py",
)


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _enclosing_function(node: ast.AST, parents: dict[int, ast.AST]) -> str | None:
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            return current.name
        current = parents.get(id(current))
    return None


async def test_r1_store_save_only_inside_apply_mutation() -> None:
    offenders: list[str] = []
    for path in SCAN:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents = _parent_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            segment = ast.get_source_segment(source, node) or ""
            if "store.save(" not in segment:
                continue
            owner = _enclosing_function(node, parents)
            # apply_mutation and drive_mutation share one shielded save.
            if path.name == "dispatch.py" and owner == "_await_durable_save":
                continue
            offenders.append(f"{path.name}:{getattr(node, 'lineno', 0)}:{owner}")
    assert offenders == []


class BarrierCopies(InMemoryDispatchStore):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def create(self, dispatch: Dispatch) -> Dispatch:
        self.rows[dispatch.id] = copy.deepcopy(dispatch)
        return copy.deepcopy(dispatch)

    async def save(self, dispatch: Dispatch) -> Dispatch:
        if isinstance(dispatch.gates, dict) and "wakeup_retry" in dispatch.gates:
            self.entered.set()
            await self.release.wait()
        self.rows[dispatch.id] = copy.deepcopy(dispatch)
        return copy.deepcopy(dispatch)

    async def get(self, dispatch_id: str) -> Dispatch | None:
        row = self.rows.get(dispatch_id)
        return None if row is None else copy.deepcopy(row)


async def test_r1_retry_save_keeps_a_concurrent_reassign(tmp_path) -> None:
    store = BarrierCopies()
    await store.create(
        Dispatch(
            id="race",
            goal="x",
            user_id="u",
            created_at="now",
            state=DispatchState.MONITORING,
            lines=[
                Line(
                    id="b",
                    name="b",
                    task="x",
                    assigned_model="kimi",
                    status=LineStatus.BLOCKED,
                )
            ],
        )
    )
    service = DispatchService(
        DispatchSettings(
            proposed_dir=tmp_path / "proposed",
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path,
        ),
        store=store,
        machine_factory=lambda _dispatch: None,
    )

    async def no_schedule(row: Dispatch) -> Dispatch:
        return row

    service._resume = no_schedule

    async def fail(_dispatch_id: str) -> None:
        raise RuntimeError("transient")

    wake = DispatchWakeup(
        store=store,
        recover=fail,
        poll_interval_seconds=60,
        lease=MemoryWakeupLease(),
    )
    task = asyncio.create_task(wake.consume())
    await asyncio.wait_for(store.entered.wait(), timeout=2)

    async def _release() -> None:
        await asyncio.sleep(0.05)
        store.release.set()

    asyncio.create_task(_release())
    reassigned = await asyncio.wait_for(service.reassign("race", "b", "stepfun"), timeout=2)
    kept = reassigned.line("b")
    assert kept is not None
    assert kept.assigned_model == "stepfun"
    assert kept.attempts == 1
    await asyncio.wait_for(task, timeout=2)
    after = await store.get("race")
    assert after is not None
    landed = after.line("b")
    assert landed is not None
    assert landed.assigned_model == "stepfun"
    assert landed.attempts == 1
    assert isinstance(after.gates, dict)
    assert "wakeup_retry" in after.gates


class SnapshotThenWait(InMemoryDispatchStore):
    """Freeze the row before the await, as ``save_dispatch`` builds its JSON."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.saves = 0

    async def create(self, dispatch: Dispatch) -> Dispatch:
        self.rows[dispatch.id] = copy.deepcopy(dispatch)
        return copy.deepcopy(dispatch)

    async def save(self, dispatch: Dispatch) -> Dispatch:
        frozen = copy.deepcopy(dispatch)
        if isinstance(dispatch.gates, dict) and "wakeup_retry" in dispatch.gates:
            self.saves += 1
            if self.saves == 1:
                self.entered.set()
                await self.release.wait()
        self.rows[dispatch.id] = frozen
        return copy.deepcopy(frozen)

    async def get(self, dispatch_id: str) -> Dispatch | None:
        row = self.rows.get(dispatch_id)
        return None if row is None else copy.deepcopy(row)


async def test_r1_an_eager_snapshot_still_keeps_the_reassignment(tmp_path) -> None:
    store = SnapshotThenWait()
    await store.create(
        Dispatch(
            id="race",
            goal="x",
            user_id="u",
            created_at="now",
            state=DispatchState.MONITORING,
            lines=[
                Line(
                    id="b",
                    name="b",
                    task="x",
                    assigned_model="kimi",
                    status=LineStatus.BLOCKED,
                )
            ],
        )
    )
    service = DispatchService(
        DispatchSettings(
            proposed_dir=tmp_path / "proposed",
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path,
        ),
        store=store,
        machine_factory=lambda _dispatch: None,
    )

    async def no_schedule(row: Dispatch) -> Dispatch:
        return row

    service._resume = no_schedule

    async def fail(_dispatch_id: str) -> None:
        raise RuntimeError("transient")

    wake = DispatchWakeup(
        store=store,
        recover=fail,
        poll_interval_seconds=60,
        lease=MemoryWakeupLease(),
    )
    task = asyncio.create_task(wake.consume())
    await asyncio.wait_for(store.entered.wait(), timeout=2)

    async def _release() -> None:
        await asyncio.sleep(0.05)
        store.release.set()

    asyncio.create_task(_release())
    reassigned = await asyncio.wait_for(service.reassign("race", "b", "stepfun"), timeout=2)
    kept = reassigned.line("b")
    assert kept is not None
    assert kept.assigned_model == "stepfun"
    assert kept.attempts == 1
    await asyncio.wait_for(task, timeout=2)
    after = await store.get("race")
    assert after is not None
    landed = after.line("b")
    assert landed is not None
    assert landed.assigned_model == "stepfun"
    assert landed.attempts == 1
    assert store.saves >= 2
    assert isinstance(after.gates, dict)
    assert "wakeup_retry" in after.gates
