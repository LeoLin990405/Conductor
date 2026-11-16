"""S1: a reassign is durable before it returns, and a failed save is not success."""

from __future__ import annotations

import asyncio
import copy

import pytest

from src.server.app.background_starts import _start_tasks
from src.server.services.dispatch import DispatchService, DispatchSettings
from src.tools.auto_dispatch import (
    Dispatch,
    DispatchState,
    InMemoryDispatchStore,
    Line,
    LineStatus,
)

pytestmark = pytest.mark.asyncio


class HoldSave(InMemoryDispatchStore):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def save(self, dispatch: Dispatch) -> Dispatch:
        frozen = copy.deepcopy(dispatch)
        self.entered.set()
        await self.release.wait()
        self.rows[dispatch.id] = frozen
        return copy.deepcopy(frozen)


class Boom(InMemoryDispatchStore):
    async def save(self, dispatch: Dispatch) -> Dispatch:
        del dispatch
        raise RuntimeError("disk")


def _service(tmp_path, store) -> DispatchService:
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
    return service


async def _seed(store: InMemoryDispatchStore, dispatch_id: str) -> None:
    await store.create(
        Dispatch(
            id=dispatch_id,
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


async def test_s1_cancel_during_save_still_persists_the_reassignment(tmp_path) -> None:
    store = HoldSave()
    await _seed(store, "cancel-race")
    service = _service(tmp_path, store)
    task = asyncio.create_task(service.reassign("cancel-race", "b", "stepfun"))
    await asyncio.wait_for(store.entered.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    after = await store.get("cancel-race")
    assert after is not None
    landed = after.line("b")
    assert landed is not None
    assert landed.assigned_model == "stepfun"
    assert landed.attempts == 1


async def test_n2_two_cancels_still_persist_and_release_the_lock(tmp_path) -> None:
    store = HoldSave()
    await _seed(store, "repeat-cancel")
    service = _service(tmp_path, store)
    task = asyncio.create_task(service.reassign("repeat-cancel", "b", "stepfun"))
    await asyncio.wait_for(store.entered.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    after = await store.get("repeat-cancel")
    assert after is not None
    landed = after.line("b")
    assert landed is not None
    assert landed.assigned_model == "stepfun"
    assert landed.attempts == 1
    assert not service._drive_locks["repeat-cancel"].locked()
    assert service._save_tasks == set()
    pending = [item for item in asyncio.all_tasks() if not item.done() and item is not asyncio.current_task()]
    assert pending == []


async def test_s1_a_failed_save_returns_persist_failed_and_does_not_drive(tmp_path) -> None:
    store = Boom()
    await _seed(store, "boom")
    before = await store.get("boom")
    assert before is not None
    before_tasks = set(_start_tasks)
    service = _service(tmp_path, store)
    with pytest.raises(ValueError, match="^persist_failed$"):
        await service.reassign("boom", "b", "stepfun")
    after = await store.get("boom")
    assert after is not None
    landed = after.line("b")
    assert landed is not None
    assert landed.assigned_model == "kimi"
    assert landed.attempts == before.line("b").attempts
    assert landed.status is LineStatus.BLOCKED
    assert set(_start_tasks) == before_tasks
