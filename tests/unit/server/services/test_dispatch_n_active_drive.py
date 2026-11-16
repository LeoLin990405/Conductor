"""N1, N2's sibling, and N3: a live drive refuses human writes, and epoch is a CAS."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from src.server.services.dispatch import DispatchService, DispatchSettings
from src.tools.auto_dispatch import Dispatch, DispatchState, Line, LineStatus
from src.tools.auto_dispatch.gates import bump_epoch, drive_epoch
from src.tools.auto_dispatch.types import Verdict
from tests.unit.server.services.test_dispatch_r2_stale_result import Copies, _service
from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line

ROOT = Path(__file__).resolve().parents[4]


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


def test_n3_drive_saves_go_through_drive_mutation() -> None:
    """Drive-side store writes in the machine and the service stay inside the CAS."""
    machine = (ROOT / "src/tools/auto_dispatch/machine.py").read_text(encoding="utf-8")
    service = (ROOT / "src/server/services/dispatch.py").read_text(encoding="utf-8")
    machine_tree = ast.parse(machine)
    machine_parents = _parent_map(machine_tree)
    offenders: list[str] = []
    for node in ast.walk(machine_tree):
        owner = _enclosing(node, machine_parents)
        if isinstance(node, ast.Attribute) and node.attr == "save" and owner != "drive_mutation":
            offenders.append(f"machine.py:{node.lineno}:{owner}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and any(isinstance(arg, ast.Constant) and arg.value == "save" for arg in node.args)
            and owner != "drive_mutation"
        ):
            offenders.append(f"machine.py:{node.lineno}:{owner}:getattr-save")
    service_tree = ast.parse(service)
    service_parents = _parent_map(service_tree)
    save_owners: list[str] = []
    has_drive_mutation = False
    for node in ast.walk(service_tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "drive_mutation":
            has_drive_mutation = True
        func = getattr(node, "func", None)
        if not isinstance(node, ast.Call) or not isinstance(func, ast.Attribute):
            continue
        if func.attr != "save":
            continue
        segment = ast.get_source_segment(service, node) or ""
        if "store.save(" not in segment:
            continue
        save_owners.append(_enclosing(node, service_parents) or "")
    assert offenders == []
    assert has_drive_mutation
    assert save_owners == ["_await_durable_save"]


def _settings(tmp_path: Path) -> DispatchSettings:
    return DispatchSettings(
        proposed_dir=tmp_path / "proposed",
        runs_dir=tmp_path / "runs",
        repo_root=tmp_path,
    )


async def _row(store: Copies, dispatch_id: str) -> None:
    await store.create(
        Dispatch(
            id=dispatch_id,
            goal="test",
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


@pytest.mark.asyncio
async def test_n1_human_calls_wait_until_the_drive_exits(tmp_path: Path) -> None:
    store = Copies()
    await _row(store, "live")
    entered, release, followed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    runs = {"n": 0}

    class Machine:
        async def run(self, dispatch: Dispatch) -> Dispatch:
            runs["n"] += 1
            if runs["n"] == 1:
                entered.set()
                await release.wait()
                dispatch.state = DispatchState.REVIEW
                return dispatch
            followed.set()
            return dispatch

    service = DispatchService(_settings(tmp_path), store=store, machine_factory=lambda _row: Machine())
    drive = asyncio.create_task(service._drive("live"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    before = await store.get("live")
    assert before is not None
    # A BLOCKED line's reassign is queued for the drive (N25b); approve and
    # reject still wait for the drive to exit.
    for call in (
        service.approve("live", "note"),
        service.reject("live", "note"),
    ):
        with pytest.raises(ValueError, match="^drive_active$"):
            await call
    held = await store.get("live")
    assert held is not None
    assert held.line("b") is not None
    assert held.line("b").assigned_model == "kimi"
    assert held.line("b").attempts == before.line("b").attempts
    assert drive_epoch(held) == 0
    release.set()
    await asyncio.wait_for(drive, timeout=2)
    assert "live" not in service.active_drives
    saved = await service.reassign("live", "b", "stepfun")
    assert saved.line("b") is not None
    assert saved.line("b").assigned_model == "stepfun"
    assert saved.line("b").attempts == 1
    await asyncio.wait_for(followed.wait(), timeout=2)
    assert runs["n"] == 2


@pytest.mark.asyncio
async def test_n1_two_reassigns_only_one_writes(tmp_path: Path) -> None:
    store = Copies()
    await _row(store, "once")
    release = asyncio.Event()

    class Machine:
        async def run(self, dispatch: Dispatch) -> Dispatch:
            await release.wait()
            return dispatch

    service = DispatchService(_settings(tmp_path), store=store, machine_factory=lambda _row: Machine())

    async def call() -> object:
        try:
            return await service.reassign("once", "b", "stepfun")
        except ValueError as exc:
            return exc

    first, second = await asyncio.gather(call(), call())
    winners = [item for item in (first, second) if not isinstance(item, ValueError)]
    refused = [item for item in (first, second) if isinstance(item, ValueError)]
    assert len(winners) == 1
    assert len(refused) == 1
    assert str(refused[0]) == "drive_active"
    after = await store.get("once")
    assert after is not None
    landed = after.line("b")
    assert landed is not None
    assert landed.assigned_model == "stepfun"
    assert landed.attempts == 1
    release.set()
    for _ in range(200):
        if "once" not in service.active_drives:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("the drive was still registered")


def _handoff(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "branch": "feature/a",
        "commit": "abc123",
        "files": ("a.py",),
        "test_command": "pytest",
        "results": "pass",
        "known_limitations": "none",
    }


@pytest.mark.asyncio
async def test_n3_a_planner_barrier_reassign_does_not_launch_children(tmp_path: Path) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class HoldPlanner:
        async def plan(self, goal: str) -> list[Line]:
            del goal
            entered.set()
            await release.wait()
            return [_line("child")]

    class Reviewer:
        async def review_line(self, line_id: str, model: str) -> Verdict:
            del model
            if line_id == "a":
                return Verdict.blocked(["still failing"])
            return Verdict.ok()

    store = Copies()
    collaborators, machine, _commands, gates = _bundle(
        tmp_path,
        [],
        store=store,
        planner=HoldPlanner(),
        reviewer=Reviewer(),
    )
    gates.handoff_facts = _handoff
    gates._revisions = lambda _line: None
    row = Dispatch(
        id="gated-replan",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.REVIEW,
        lines=[
            _line("a", status=LineStatus.RUNNING, assigned_model="kimi", current_run="a-old"),
            _line("b", status=LineStatus.BLOCKED, assigned_model="deepseek"),
        ],
    )
    gates.prepare(row)
    first, second = row.lines
    snap = gates.line_snapshot(row, first)
    assert isinstance(snap.get("machine"), dict)
    snap["machine"].update(
        stage="primary_review",
        author_provider="kimi",
        developer_provider="kimi",
        primary_reviewer="glm",
        blocking_reviewer="glm",
        blocking_rounds=1,
        fix_rounds=2,
    )
    snap["gates"]["G2"] = {"result": "pass", "attempt": 0, "revision": None}
    gates.mark_run_settled(row, first)
    gates.line_snapshot(row, second)["machine"]["stage"] = "fixing"
    await store.create(row)
    service = _service(tmp_path, store, lambda _dispatch: machine)
    service._gate_runner = lambda: gates

    async def no_schedule(dispatch: Dispatch) -> Dispatch:
        return dispatch

    service._resume = no_schedule
    drive = asyncio.create_task(service._drive(row.id))
    await asyncio.wait_for(entered.wait(), timeout=5)
    seen = len(collaborators.launcher.launches)
    with pytest.raises(ValueError, match="^drive_active$"):
        await service.approve(row.id, "note")

    def mutation(current: Dispatch) -> Dispatch:
        line = current.line("b")
        assert line is not None
        line.assigned_model = "stepfun"
        line.attempts += 1
        line.status = LineStatus.ASSIGNED
        bump_epoch(current)
        return current

    await service.apply_mutation(row.id, mutation)
    release.set()
    await asyncio.wait_for(drive, timeout=6)
    after = await store.get(row.id)
    assert after is not None
    assert after.line("a-r1-1") is None
    assert collaborators.launcher.launches[seen:] == []
    assert after.line("b") is not None
    assert after.line("b").assigned_model == "stepfun"
    audit = ((after.gates or {}).get("scheduler") or {}).get("audit") or []
    assert any(isinstance(item, dict) and item.get("kind") == "stale_drive" for item in audit)
    assert machine._abandoned

    class Quiet:
        async def review_line(self, line_id: str, model: str) -> Verdict:
            del line_id, model
            return Verdict.ok()

    collaborators.reviewer = Quiet()
    follow = asyncio.create_task(service._drive(row.id))
    await asyncio.wait_for(follow, timeout=6)
    assert not machine._abandoned
