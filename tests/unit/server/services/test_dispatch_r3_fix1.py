"""C2, C3, and C4: owner token, a stale drive stops, baseline stays paired."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from src.server.services.dispatch import DispatchService, DispatchSettings, _MergingStore
from src.tools.auto_dispatch import Dispatch, DispatchState, Line, LineStatus
from src.tools.auto_dispatch.gates import bump_epoch
from src.tools.auto_dispatch.machine import DispatchMachine, StaleDrive
from tests.unit.server.services.test_dispatch_r2_stale_result import Copies
from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line

pytestmark = pytest.mark.asyncio


def _settings(tmp_path) -> DispatchSettings:
    return DispatchSettings(
        proposed_dir=tmp_path / "proposed",
        runs_dir=tmp_path / "runs",
        repo_root=tmp_path,
    )


async def _blocked(store: Copies, dispatch_id: str) -> None:
    await store.create(
        Dispatch(
            id=dispatch_id,
            goal="test",
            user_id="u",
            created_at="now",
            state=DispatchState.REVIEW,
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


async def test_c2_a_cancelled_queued_drive_does_not_clear_the_owner(tmp_path) -> None:
    store = Copies()
    await _blocked(store, "live")
    entered, release = asyncio.Event(), asyncio.Event()

    class Machine:
        async def run(self, dispatch: Dispatch) -> Dispatch:
            entered.set()
            await release.wait()
            return dispatch

    service = DispatchService(
        _settings(tmp_path),
        store=store,
        machine_factory=lambda _row: Machine(),
    )
    primary = asyncio.create_task(service._drive("live"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    queued = asyncio.create_task(service._drive("live"))
    await asyncio.sleep(0.05)
    owner = service.active_drives.get("live")
    assert owner is not None
    assert owner is not primary
    assert owner is not queued
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert service.active_drives.get("live") is owner
    before = await store.get("live")
    assert before is not None
    # The owner still refuses human writes. (A BLOCKED line's reassign is
    # queued for the drive since N25b, so approve is the probe here.)
    with pytest.raises(ValueError, match="^drive_active$"):
        await service.approve("live", "note")
    held = await store.get("live")
    assert held is not None
    assert held.line("b") is not None
    assert held.line("b").assigned_model == "kimi"
    assert held.line("b").attempts == before.line("b").attempts
    release.set()
    await asyncio.wait_for(primary, timeout=2)
    assert "live" not in service.active_drives
    saved = await service.reassign("live", "b", "stepfun")
    assert saved.line("b") is not None
    assert saved.line("b").assigned_model == "stepfun"


async def test_c3_a_stale_launch_stops_and_the_next_drive_runs_assigned(tmp_path) -> None:
    store = Copies()
    collab, machine, _commands, gates = _bundle(tmp_path / "launch", [_line("a")], store=store)
    row = Dispatch(
        id="launch-race",
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
    g2_calls = {"n": 0}
    original_g2 = gates.run_g2

    async def _count_g2(*args, **kwargs):
        g2_calls["n"] += 1
        return await original_g2(*args, **kwargs)

    gates.run_g2 = _count_g2
    entered, release = asyncio.Event(), asyncio.Event()
    original = machine._save

    async def held_save(current: Dispatch) -> None:
        if current.state is DispatchState.LAUNCHING and not entered.is_set():
            entered.set()
            await release.wait()
        await original(current)

    machine._save = held_save
    task = asyncio.create_task(service._drive(row.id))
    await asyncio.wait_for(entered.wait(), timeout=5)

    def drift(current: Dispatch) -> Dispatch:
        bump_epoch(current)
        return current

    await service.apply_mutation(row.id, drift)
    release.set()
    await asyncio.wait_for(task, timeout=5)
    assert collab.launcher.launches == []
    assert g2_calls["n"] == 0
    stored = await store.get(row.id)
    assert stored is not None
    assert stored.state is DispatchState.ASSIGNED
    audit = (stored.gates.get("scheduler") or {}).get("audit") or []
    assert any(isinstance(item, dict) and item.get("kind") == "stale_drive" for item in audit)
    assert machine._abandoned

    follow = asyncio.create_task(service._drive(row.id))
    await asyncio.wait_for(follow, timeout=5)
    assert collab.launcher.launches
    assert not machine._abandoned


async def test_c3_a_stale_g2_save_does_not_run_the_command(tmp_path) -> None:
    store = Copies()
    _collab, machine, _commands, gates = _bundle(tmp_path / "g2", [_line("a")], store=store)
    line = _line("a", assigned_model="kimi", status=LineStatus.RUNNING, current_run="run-a")
    row = Dispatch(
        id="g2-race",
        goal="x",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        lines=[line],
        gates={"epoch": 0},
    )
    gates.prepare(row)
    await store.create(row)
    service = DispatchService(_settings(tmp_path), store=store, machine_factory=lambda _row: machine)
    machine._epoch0 = 0
    machine._runs[line.id] = SimpleNamespace(complete=True)
    calls: list[str] = []

    async def _run_g2(*_args, **_kwargs):
        calls.append("g2")
        raise AssertionError("old G2 ran")

    gates.run_g2 = _run_g2
    original = machine._save
    bumped = {"done": False}

    async def held_save(current: Dispatch) -> None:
        if not bumped["done"]:
            bumped["done"] = True

            def drift(fresh: Dispatch) -> Dispatch:
                bump_epoch(fresh)
                return fresh

            await service.apply_mutation(row.id, drift)
        await original(current)

    machine._save = held_save
    with pytest.raises(StaleDrive):
        await machine._apply_g2(row)
    assert calls == []
    stored = await store.get(row.id)
    assert stored is not None
    audit = (stored.gates.get("scheduler") or {}).get("audit") or []
    assert any(isinstance(item, dict) and item.get("kind") == "stale_drive" for item in audit)


async def test_c4_claim_and_commit_line_keep_unowned_fields(tmp_path) -> None:
    store = Copies()
    local = Dispatch(
        id="baseline",
        goal="x",
        user_id="u",
        created_at="now",
        lines=[
            Line(id="a", name="a", task="a", assigned_model="kimi", status=LineStatus.ASSIGNED),
            Line(id="b", name="b", task="b", assigned_model="glm", status=LineStatus.PENDING),
        ],
        gates={"epoch": 0, "scheduler": {"pending": [], "handled": []}},
    )
    await store.create(local)
    service = DispatchService(_settings(tmp_path), store=store)
    wrapped = _MergingStore(service, store, {local.id: copy.deepcopy(local)})
    machine = DispatchMachine(SimpleNamespace(store=wrapped, gates=None))

    def concurrent(row: Dispatch) -> Dispatch:
        row.gates["wakeup_retry"] = {"attempts": 1, "next_at": "2099-01-01T00:00:00+00:00"}
        row.gates["scheduler"]["pending"] = [
            {"id": "late", "kind": "run_complete", "line_id": "a", "run_id": "run-a", "attempt": 0}
        ]
        row.gates["scheduler"]["handled"] = ["old"]
        other = row.line("b")
        assert other is not None
        other.status = LineStatus.RUNNING
        return row

    await service.apply_mutation(local.id, concurrent)
    assert await machine._claim_epoch(local)
    local.state = DispatchState.REVIEW
    await machine._save(local)
    after = await store.get(local.id)
    assert after is not None
    assert after.state is DispatchState.REVIEW
    assert after.gates["wakeup_retry"] == {
        "attempts": 1,
        "next_at": "2099-01-01T00:00:00+00:00",
    }
    assert after.gates["scheduler"]["pending"] == [
        {"id": "late", "kind": "run_complete", "line_id": "a", "run_id": "run-a", "attempt": 0}
    ]
    assert after.gates["scheduler"]["handled"] == ["old"]
    assert after.line("b") is not None
    assert after.line("b").status is LineStatus.RUNNING

    def again(row: Dispatch) -> Dispatch:
        row.gates["scheduler"]["handled"] = [*row.gates["scheduler"]["handled"], "newer"]
        other = row.line("b")
        assert other is not None
        other.status = LineStatus.BLOCKED
        return row

    await service.apply_mutation(local.id, again)
    line = local.line("a")
    assert line is not None
    line.failure_reason = "log stalled"
    saved = await machine._commit_line(local, line, bump=True)
    assert saved is not None
    kept = await store.get(local.id)
    assert kept is not None
    assert kept.gates["wakeup_retry"]["attempts"] == 1
    assert kept.gates["scheduler"]["pending"][0]["id"] == "late"
    assert kept.gates["scheduler"]["handled"] == ["old", "newer"]
    assert kept.line("b") is not None
    assert kept.line("b").status is LineStatus.BLOCKED
    assert kept.line("a") is not None
    assert kept.line("a").failure_reason == "log stalled"
