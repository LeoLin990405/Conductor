"""W3b fix round 2: wakeup regressions B2, B3, B4, B8, and B9.

Each test drives ``DispatchWakeup`` or ``DispatchService``. None of them open
a database connection or the app lifespan in ``setup.py``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI

from src.server.services.dispatch import DispatchService, DispatchSettings
from src.server.services.dispatch_wakeup import (
    WAKEUP_ADVISORY_LOCK_KEY,
    DispatchWakeup,
    MemoryWakeupLease,
    PostgresWakeupLease,
    lease_for_store,
)
from src.tools.auto_dispatch import (
    Dispatch,
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    LineStatus,
)
from src.tools.auto_dispatch.gates import GateRunner, GateSettings
from src.tools.auto_dispatch.types import RunStatus
from tests.unit.tools.auto_dispatch.test_gate_recovery import (
    WHEN,
    FakeWorktrees,
    ScriptedCommands,
    _bundle,
    _line,
)

pytestmark = pytest.mark.asyncio


class HangMonitor:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def poll(self, run_id: str) -> RunStatus:
        del run_id
        self.entered.set()
        await asyncio.Event().wait()
        return RunStatus(output="done", complete=True)

    async def cancel(self, run_id: str) -> None:
        del run_id


class FlakyMonitor:
    def __init__(self) -> None:
        self.polls = 0

    async def poll(self, run_id: str) -> RunStatus:
        del run_id
        self.polls += 1
        if self.polls == 1:
            raise RuntimeError("offline injected transient monitor failure")
        return RunStatus(output="done", complete=True)

    async def cancel(self, run_id: str) -> None:
        del run_id


class FlakyMerger:
    def __init__(self) -> None:
        self.calls = 0

    async def merge_line(self, line_id: str) -> str:
        del line_id
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("merge blip")
        return "merged"

    async def set_aside(self) -> None:
        return None


class FlakyLauncher:
    def __init__(self) -> None:
        self.calls = 0

    async def launch(self, line, model: str, skills: list[str]) -> str:
        del line, model, skills
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("launch blip")
        return "run-fixed"


class BlockingLauncher:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def launch(self, line, model: str, skills: list[str]) -> str:
        del model, skills
        self.calls.append(line.id)
        self.started.set()
        await self.release.wait()
        return "run-1"


def _service(tmp_path, store, factory, *, lease=None) -> DispatchService:
    return DispatchService(
        DispatchSettings(
            proposed_dir=tmp_path / "proposed",
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            poll_interval_seconds=0.05,
        ),
        store=store,
        gate=FileApprovalGate(tmp_path / "proposed"),
        machine_factory=factory,
        wakeup_lease=lease,
    )


def _factory(tmp_path, store, **replaced):
    def factory(_dispatch: Dispatch):
        collaborators, machine, _commands, _gates = _bundle(
            tmp_path,
            [],
            settings=GateSettings(
                runs_dir=tmp_path / "runs",
                repo_root=tmp_path / "repo",
                g5_command="pytest -q",
            ),
            store=store,
        )
        for name, value in replaced.items():
            setattr(collaborators, name, value)
        return machine

    return factory


async def _wait_until(predicate, attempts: int = 80) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.05)
    assert predicate()


async def test_b2_open_returns_while_recovery_is_still_blocked(tmp_path) -> None:
    store = InMemoryDispatchStore()
    hang = HangMonitor()
    line = _line("line-0", assigned_model="kimi")
    line.status = LineStatus.RUNNING
    line.current_run = "run-1"
    dispatch = Dispatch(
        id="d1",
        goal="ship it",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        lines=[line],
        integrate_branch="integrate/auto-dispatch-d1",
    )
    GateRunner(
        GateSettings(
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            g5_command="pytest -q",
        ),
        commands=ScriptedCommands(),
        worktrees=FakeWorktrees(),
        clock=lambda: WHEN,
    ).prepare(dispatch)
    await store.create(dispatch)
    service = _service(tmp_path, store, _factory(tmp_path, store, monitor=hang))

    @asynccontextmanager
    async def lifespan(_app):
        await asyncio.wait_for(service.open(), timeout=1)
        yield
        await service.close()

    app = FastAPI(lifespan=lifespan)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(hang.entered.wait(), timeout=2)
    leftover = [
        task.get_name()
        for task in asyncio.all_tasks()
        if not task.done()
        and (
            task.get_name().startswith("dispatch-wakeup")
            or task.get_name() in {"dispatch-poll", "dispatch-deadline"}
        )
    ]
    assert leftover == []


async def test_b3_one_shared_lease_and_one_drive_launch_once(tmp_path) -> None:
    held: set[int] = set()
    store = InMemoryDispatchStore()
    launcher = BlockingLauncher()
    line = _line("line-0", assigned_model="kimi")
    line.status = LineStatus.ASSIGNED
    dispatch = Dispatch(
        id="d1",
        goal="ship it",
        user_id="u",
        created_at="now",
        state=DispatchState.ASSIGNED,
        lines=[line],
        integrate_branch="integrate/auto-dispatch-d1",
    )
    GateRunner(
        GateSettings(
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            g5_command="pytest -q",
        ),
        commands=ScriptedCommands(),
        worktrees=FakeWorktrees(),
        clock=lambda: WHEN,
    ).prepare(dispatch)
    await store.create(dispatch)
    factory = _factory(tmp_path, store, launcher=launcher)
    first = _service(tmp_path, store, factory, lease=MemoryWakeupLease(held))
    second = _service(tmp_path, store, factory, lease=MemoryWakeupLease(held))
    await first.open()
    await second.open()
    try:
        await asyncio.wait_for(launcher.started.wait(), timeout=2)
        assert second._wakeup.skip_reason == "wakeup_lease_held"
        raced = asyncio.create_task(first._drive("d1"))
        await asyncio.sleep(0.05)
        assert launcher.calls == ["line-0"]
        launcher.release.set()
        await asyncio.wait_for(raced, timeout=5)
        assert launcher.calls == ["line-0"]
    finally:
        await first.close()
        await second.close()


async def test_b3_postgres_lease_locks_the_fixed_key(monkeypatch) -> None:
    sql: list[tuple[str, tuple[object, ...]]] = []

    class Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def execute(self, statement: str, args: tuple[object, ...]) -> None:
            sql.append((statement, args))

        async def fetchone(self):
            return (True,)

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    class Context:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(
        "src.server.database.pool.get_db_connection",
        lambda: Context(),
    )
    lease = PostgresWakeupLease()
    assert await lease.try_acquire() is True
    await lease.release()
    assert sql[0] == ("SELECT pg_try_advisory_lock(%s)", (WAKEUP_ADVISORY_LOCK_KEY,))
    assert sql[1][0] == "SELECT pg_advisory_unlock(%s)"
    assert sql[1][1] == (WAKEUP_ADVISORY_LOCK_KEY,)

    class DbDispatchStore:
        pass

    assert isinstance(lease_for_store(DbDispatchStore()), PostgresWakeupLease)
    assert isinstance(lease_for_store(object()), MemoryWakeupLease)


async def test_b4_a_later_tick_resumes_monitor_merge_and_launch(tmp_path) -> None:
    store = InMemoryDispatchStore()
    monitor = FlakyMonitor()
    running = _line("line-0", assigned_model="kimi")
    running.status = LineStatus.RUNNING
    running.current_run = "run-1"
    watched = Dispatch(
        id="d-mon",
        goal="ship it",
        user_id="u",
        created_at="2026-01-01T00:00:00+00:00",
        state=DispatchState.MONITORING,
        lines=[running],
        integrate_branch="integrate/auto-dispatch-d-mon",
    )
    GateRunner(
        GateSettings(
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            g5_command="pytest -q",
        ),
        commands=ScriptedCommands(),
        worktrees=FakeWorktrees(),
        clock=lambda: WHEN,
    ).prepare(watched)
    await store.create(watched)
    # Wait until the drive saves the concrete terminal state. The first poll
    # fails and the wakeup retries; the call count is not the signal.
    monitored = asyncio.Event()
    original_save = store.save

    async def save_monitored(dispatch: Dispatch) -> Dispatch:
        saved_row = await original_save(dispatch)
        line = saved_row.line("line-0")
        if (
            saved_row.id == "d-mon"
            and saved_row.state is DispatchState.APPROVAL
            and line is not None
            and line.status is LineStatus.PASSED
        ):
            monitored.set()
        return saved_row

    store.save = save_monitored  # type: ignore[method-assign]
    service = _service(tmp_path, store, _factory(tmp_path, store, monitor=monitor))
    await service.open()
    try:
        await asyncio.wait_for(monitored.wait(), timeout=8)
        saved = await store.get("d-mon")
        assert saved is not None
        assert saved.state is DispatchState.APPROVAL
        landed = saved.line("line-0")
        assert landed is not None
        assert landed.status is LineStatus.PASSED
        assert monitor.polls >= 2
    finally:
        await service.close()
        store.save = original_save  # type: ignore[method-assign]

    merger = FlakyMerger()
    passed = _line("line-0", assigned_model="kimi")
    passed.status = LineStatus.PASSED
    merging = Dispatch(
        id="d-merge",
        goal="ship it",
        user_id="u",
        created_at="2026-01-02T00:00:00+00:00",
        state=DispatchState.LAUNCHING,
        lines=[passed],
        integrate_branch="integrate/auto-dispatch-d-merge",
    )
    GateRunner(
        GateSettings(
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            g5_command="pytest -q",
        ),
        commands=ScriptedCommands(),
        worktrees=FakeWorktrees(),
        clock=lambda: WHEN,
    ).prepare(merging)
    await store.create(merging)
    merged = asyncio.Event()
    original_merge_save = store.save

    async def save_merged(dispatch: Dispatch) -> Dispatch:
        saved_row = await original_merge_save(dispatch)
        integration = (
            saved_row.gates.get("integration") if isinstance(saved_row.gates, dict) else None
        )
        phase = integration.get("phase") if isinstance(integration, dict) else None
        if saved_row.id == "d-merge" and saved_row.state is DispatchState.APPROVAL and phase == "passed":
            merged.set()
        return saved_row

    store.save = save_merged  # type: ignore[method-assign]
    merge_service = _service(tmp_path, store, _factory(tmp_path, store, merger=merger))
    await merge_service.open()
    try:
        await asyncio.wait_for(merged.wait(), timeout=8)
        saved = await store.get("d-merge")
        assert saved is not None
        assert saved.state is DispatchState.APPROVAL
        integration = saved.gates.get("integration") if isinstance(saved.gates, dict) else None
        assert isinstance(integration, dict)
        assert integration.get("phase") == "passed"
        assert merger.calls >= 2
        assert saved.line("line-0") is not None
        assert saved.line("line-0").status is LineStatus.PASSED
    finally:
        await merge_service.close()
        store.save = original_merge_save  # type: ignore[method-assign]

    launcher = FlakyLauncher()
    assigned = _line("line-0", assigned_model="kimi")
    assigned.status = LineStatus.ASSIGNED
    fixing = Dispatch(
        id="d-fix",
        goal="ship it",
        user_id="u",
        created_at="2026-01-03T00:00:00+00:00",
        state=DispatchState.ASSIGNED,
        lines=[assigned],
        integrate_branch="integrate/auto-dispatch-d-fix",
    )
    GateRunner(
        GateSettings(
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            g5_command="pytest -q",
        ),
        commands=ScriptedCommands(),
        worktrees=FakeWorktrees(),
        clock=lambda: WHEN,
    ).prepare(fixing)
    await store.create(fixing)
    # The launcher increments ``calls`` before it returns. ``run-fixed`` is
    # stored only when the drive applies LaunchDone. Wait for that save.
    persisted = asyncio.Event()
    original_save = store.save

    async def save(dispatch: Dispatch) -> Dispatch:
        saved_row = await original_save(dispatch)
        landed = saved_row.line("line-0")
        if landed is not None and landed.current_run == "run-fixed":
            persisted.set()
        return saved_row

    store.save = save  # type: ignore[method-assign]
    launch_service = _service(tmp_path, store, _factory(tmp_path, store, launcher=launcher))
    await launch_service.open()
    try:
        await asyncio.wait_for(persisted.wait(), timeout=5)
        saved = await store.get("d-fix")
        assert saved is not None
        assert saved.state is not DispatchState.ERROR
        assert saved.line("line-0") is not None
        assert saved.line("line-0").current_run == "run-fixed"
    finally:
        await launch_service.close()


async def test_b8_a_wide_scan_reaches_the_oldest_active_row() -> None:
    rows = []
    for index in range(1000):
        rows.append(
            Dispatch(
                id=f"new-{index}",
                goal="ship it",
                user_id="u",
                created_at=f"2026-11-01T00:00:{index % 60:02d}+00:00",
                state=DispatchState.MONITORING,
            )
        )
    rows.append(
        Dispatch(
            id="old-monitoring",
            goal="ship it",
            user_id="u",
            created_at="2020-01-01T00:00:00+00:00",
            state=DispatchState.MONITORING,
        )
    )

    class NewestFirst:
        def __init__(self) -> None:
            self.limits: list[int] = []

        async def list(self, *, user_id=None, states=None, limit=50):
            del user_id
            self.limits.append(limit)
            chosen = rows
            if states:
                wanted = set(states)
                chosen = [row for row in rows if row.state.value in wanted]
            ordered = sorted(chosen, key=lambda row: row.created_at, reverse=True)
            return ordered[:limit]

        async def get(self, dispatch_id: str):
            for row in rows:
                if row.id == dispatch_id:
                    return row
            return None

        async def save(self, dispatch: Dispatch) -> Dispatch:
            return dispatch

    store = NewestFirst()
    recovered: list[str] = []

    async def recover(dispatch_id: str) -> None:
        recovered.append(dispatch_id)

    wakeup = DispatchWakeup(store=store, recover=recover, poll_interval_seconds=60)
    await wakeup.sweep()
    assert "old-monitoring" in recovered
    assert any(limit > 1000 for limit in store.limits)


async def test_b9_a_recover_failure_logs_the_type_and_reason_only(caplog) -> None:
    sentinel = "SECRET-WAKEUP-SENTINEL"
    store = InMemoryDispatchStore()
    await store.create(
        Dispatch(
            id="d1",
            goal="ship it",
            user_id="u",
            created_at="now",
            state=DispatchState.MONITORING,
        )
    )

    async def recover(_dispatch_id: str) -> None:
        raise RuntimeError(sentinel)

    wakeup = DispatchWakeup(store=store, recover=recover, poll_interval_seconds=60)
    with caplog.at_level(logging.WARNING):
        await wakeup.sweep()
    text = caplog.text
    assert sentinel not in text
    assert "Traceback" not in text
    assert "RuntimeError" in text
    assert "wakeup_recover_failed" in text
