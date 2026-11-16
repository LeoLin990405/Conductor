"""Actor loop: one writer, a deadline that is not stuck behind selection, unique run ids."""

from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.tools.auto_dispatch import (
    AlwaysPassReviewer,
    Collaborators,
    Dispatch,
    DispatchError,
    DispatchMachine,
    DispatchState,
    FileApprovalGate,
    FixedPlanner,
    InMemoryDispatchStore,
    InMemoryOutcomeStore,
    Line,
    LineStatus,
    NoopMerger,
    RecordingLauncher,
    RoundRobinSelector,
)
from src.tools.auto_dispatch.execution import LogMonitor, ScriptLauncher
from src.tools.auto_dispatch.gates import GateRunner, GateSettings
from src.tools.auto_dispatch.types import RunStatus

ROOT = Path(__file__).resolve().parents[4]
_STATE_FIELDS = frozenset(
    {"_local", "_runs", "_outputs", "_epoch0", "_abandoned", "_baseline"}
)
# These actor scenarios perform several isolated Git reads while a machine
# advances. Keep a bounded allowance for subprocess and concurrent gate load.
_REAL_GIT_TEST_TIMEOUT_SECONDS = 30


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _chain(node: ast.AST, parents: dict[int, ast.AST]) -> list[str]:
    names: list[str] = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            names.append(current.name)
        current = parents.get(id(current))
    return names


def test_only_the_actor_loop_assigns_machine_state() -> None:
    """Assignments to machine state stay in ``__init__`` or the writer setup.

    A name that holds ``self._local`` or another state field is the same write.
    Nested attribute and subscript chains, ``setattr``, ``update``, and
    ``append`` all count.
    """
    source = (ROOT / "src/tools/auto_dispatch/machine.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents = _parents(tree)
    offenders: list[str] = []
    assert "_state_lock" not in source
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        chain = _chain(node, parents)
        if "_actor_loop" in chain or "_install_owners" in chain or node.name == "__init__":
            continue
        aliases = _aliases(node)
        for child in ast.walk(node):
            if child is node:
                continue
            if _chain(child, parents)[:1] != [node.name]:
                continue
            if _mutates_state(child, aliases):
                offenders.append(node.name)
                break
    assert offenders == []


def test_fix_launches_enter_only_through_begin_fix() -> None:
    """A repair launch reaches the guarded launcher only through ``_begin_fix``."""
    source = (ROOT / "src/tools/auto_dispatch/machine.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents = _parents(tree)
    forced: list[str] = []
    begin_callers: list[str] = []
    remember_callers: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = ""
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        chain = _chain(node, parents)
        owner = chain[0] if chain else ""
        if name == "_begin_fix":
            begin_callers.append(owner)
        if name == "_remember_pending_fix":
            remember_callers.append(owner)
        if name not in {"_launch_line", "_launch_guarded"}:
            continue
        if owner in {"_launch_fix", "_resume_fix"}:
            forced.append(f"{owner}->{name}")
        for keyword in node.keywords:
            if keyword.arg != "force_g2":
                continue
            if isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                if owner != "_begin_fix":
                    forced.append(f"{owner}->{name}")
    assert sorted(begin_callers) == ["_launch_fix", "_resume_fix"]
    assert remember_callers == ["_begin_fix"]
    assert forced == []


def _read_field(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr in _STATE_FIELDS
    ):
        return node.attr
    return None


def _state_root(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """Follow attribute and subscript chains back to a machine state field."""
    chain: list[ast.AST] = []
    current = node
    while isinstance(current, ast.Attribute | ast.Subscript):
        chain.append(current)
        current = current.value
    if isinstance(current, ast.Name):
        found = aliases.get(current.id)
        if found is not None:
            return found
        if current.id == "self" and chain and isinstance(chain[-1], ast.Attribute):
            if chain[-1].attr in _STATE_FIELDS:
                return chain[-1].attr
    return _read_field(node)


def _aliases(func: ast.AST) -> dict[str, str]:
    """Names in ``func`` that point at a machine state field or a nested value."""
    found: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(func):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            field = _read_field(node.value)
            if field is None and isinstance(node.value, ast.Name):
                field = found.get(node.value.id)
            if field is None and isinstance(node.value, ast.Attribute | ast.Subscript):
                field = _state_root(node.value, found)
            if field is not None and found.get(target.id) != field:
                found[target.id] = field
                changed = True
    return found


def _receiver_field(node: ast.AST, aliases: dict[str, str]) -> str | None:
    return _state_root(node, aliases)


def _mutates_state(node: ast.AST, aliases: dict[str, str]) -> bool:
    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        targets = [node.target]
    elif isinstance(node, ast.AugAssign):
        targets = [node.target]
    for target in targets:
        if isinstance(target, ast.Name) and target.id in aliases:
            return True
        owner = target.value if isinstance(target, ast.Attribute | ast.Subscript) else None
        if owner is not None and _receiver_field(owner, aliases) is not None:
            return True
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "setattr" and len(node.args) >= 2:
        return _receiver_field(node.args[0], aliases) is not None
    if isinstance(func, ast.Attribute) and func.attr in {"update", "append", "extend", "pop", "clear"}:
        return _receiver_field(func.value, aliases) is not None
    return False


def _state_target(target: ast.AST) -> str | None:
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        if target.value.id == "self" and target.attr in _STATE_FIELDS:
            return target.attr
    if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Attribute):
        inner = target.value
        if (
            isinstance(inner.value, ast.Name)
            and inner.value.id == "self"
            and inner.attr in {"_runs", "_outputs"}
        ):
            return inner.attr
    return None


class _Clock:
    """A fake clock. ``sleep`` waits until ``advance`` reaches the requested instant."""

    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._waiters: list[tuple[datetime, asyncio.Future[None]]] = []

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        target = self.now + timedelta(seconds=seconds)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        self._waiters.append((target, future))
        try:
            await future
        except asyncio.CancelledError:
            self._waiters = [
                (item, waiter) for item, waiter in self._waiters if waiter is not future
            ]
            raise

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        pending: list[tuple[datetime, asyncio.Future[None]]] = []
        ready: list[asyncio.Future[None]] = []
        for target, future in self._waiters:
            if future.done():
                continue
            if target <= self.now:
                ready.append(future)
            else:
                pending.append((target, future))
        self._waiters = pending
        for future in ready:
            future.set_result(None)


def _machine(tmp_path: Path, **overrides: object) -> tuple[Collaborators, DispatchMachine]:
    store = overrides.pop("store", None) or InMemoryDispatchStore()
    collaborators = Collaborators(
        planner=FixedPlanner(),
        selector=overrides.pop("selector", None) or RoundRobinSelector(),  # type: ignore[arg-type]
        launcher=overrides.pop("launcher", None) or RecordingLauncher(),  # type: ignore[arg-type]
        monitor=overrides.pop("monitor", None) or _Finish(),  # type: ignore[arg-type]
        reviewer=AlwaysPassReviewer(),
        merger=NoopMerger(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        outcomes=InMemoryOutcomeStore(),
        store=store,  # type: ignore[arg-type]
        gates=overrides.pop("gates", None),  # type: ignore[arg-type]
    )
    sleep = overrides.pop("sleep", asyncio.sleep)
    machine = DispatchMachine(collaborators, sleep=sleep)  # type: ignore[arg-type]
    return collaborators, machine


class _Finish:
    async def poll(self, run_id: str) -> RunStatus:
        return RunStatus(output=f"done {run_id}", complete=True, tokens=1)

    async def cancel(self, run_id: str) -> None:
        del run_id


class _Stall:
    async def poll(self, run_id: str) -> RunStatus:
        del run_id
        return RunStatus(output="stalled", stalled=True)

    async def cancel(self, run_id: str) -> None:
        del run_id


def _row(dispatch_id: str, *, task: str, status: LineStatus = LineStatus.ASSIGNED) -> Dispatch:
    return Dispatch(
        id=dispatch_id,
        goal="ship",
        user_id="u",
        created_at="now",
        state=DispatchState.LAUNCHING,
        lines=[
            Line(
                id="a",
                name="a",
                task=task,
                assigned_model="kimi",
                status=status,
                current_run=None,
            )
        ],
    )


@pytest.mark.asyncio
async def test_watchdog_advances_while_selector_sleeps(tmp_path: Path) -> None:
    """A 60s selector wait must not hold the deadline or the heartbeat."""
    clock = _Clock()
    gates = GateRunner(
        GateSettings(
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            watchdog_seconds=5,
            heartbeat_interval_seconds=1,
            heartbeat_threshold_seconds=30,
        ),
        clock=clock,
    )

    class _SlowSelect:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.finished = False

        async def select(self, line: Line, used: list[str], *, role: object) -> str:
            del line, used, role
            self.entered.set()
            await clock.sleep(60)
            self.finished = True
            return "stepfun"

    selector = _SlowSelect()
    row = _row("wd", task="work", status=LineStatus.RUNNING)
    row.lines[0].current_run = "already"
    _collaborators, machine = _machine(
        tmp_path, selector=selector, monitor=_Stall(), gates=gates, sleep=clock.sleep
    )
    gates.prepare(row)
    gates.beat(row)
    before = row.gates["scheduler"]["heartbeat_at"]
    await machine._c.store.create(row)
    task = asyncio.create_task(machine._monitor(row))
    await asyncio.wait_for(selector.entered.wait(), timeout=2)
    for _ in range(8):
        clock.advance(1)
        for _yield in range(10):
            del _yield
            await asyncio.sleep(0)
        if row.lines[0].status is not LineStatus.RUNNING:
            break
    await asyncio.wait_for(task, timeout=2)
    assert row.lines[0].status is not LineStatus.RUNNING
    assert row.lines[0].failure_reason == "watchdog_timeout"
    assert selector.finished is False
    saved = await machine._c.store.get(row.id)
    assert saved is not None
    assert saved.lines[0].status is not LineStatus.RUNNING
    assert saved.gates["scheduler"]["heartbeat_at"] != before


@pytest.mark.asyncio
async def test_two_dispatches_do_not_share_a_log(tmp_path: Path) -> None:
    script = tmp_path / "launch.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    git_env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    for argv in (
        ["git", "init", "-q", str(repo)],
        ["git", "-C", str(repo), "config", "core.hooksPath", "/dev/null"],
        ["git", "-C", str(repo), "config", "core.fsmonitor", "false"],
        ["git", "-C", str(repo), "config", "user.name", "Dispatch Test"],
        ["git", "-C", str(repo), "config", "user.email", "dispatch-test@example.invalid"],
    ):
        subprocess.run(argv, check=True, env=git_env)
    (repo / "README").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True, env=git_env)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base"],
        check=True,
        env=git_env,
    )
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    ).stdout.strip()
    runs = tmp_path.parent / f"{tmp_path.name}-runs"
    worktrees = tmp_path.parent / f"{tmp_path.name}-worktrees"
    store = InMemoryDispatchStore()
    entered = asyncio.Event()
    release = asyncio.Event()
    paths: list[Path] = []

    async def runner(argv: list[str], cwd: Path, log_path: Path) -> int:
        worker = "second-worker" if "second-worker" in argv[2] else "first-worker"
        change = cwd / f"worker-{hashlib.sha256(log_path.name.encode()).hexdigest()[:12]}.txt"
        change.write_text(worker, encoding="utf-8")
        subprocess.run(["git", "add", change.name], cwd=cwd, check=True, env=git_env)
        subprocess.run(
            ["git", "commit", "-q", "-m", f"{worker} invocation {log_path.name}"],
            cwd=cwd,
            check=True,
            env=git_env,
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_bytes(worker.encode())
        paths.append(log_path)
        if worker == "second-worker":
            entered.set()
            await release.wait()
        log_path.with_suffix(".exit").write_text("0", encoding="utf-8")
        return 0

    def make(row: Dispatch) -> DispatchMachine:
        line = row.lines[0]
        line.branch = f"feature/auto-dispatch-{row.id}/line-{line.id}"
        line.base_commit = base
        launcher = ScriptLauncher(
            runs,
            script=script,
            cwd=repo,
            worktrees_dir=worktrees,
            dispatch_id=row.id,
            runner=runner,
        )
        _collaborators, machine = _machine(tmp_path, launcher=launcher, store=store)
        return machine

    first = _row("dispatch-one", task="first-worker")
    second = _row("dispatch-two", task="second-worker")
    await store.create(first)
    await store.create(second)
    first_machine = make(first)
    second_machine = make(second)
    await first_machine._launch_line(first, first.lines[0])
    first_run = first.lines[0].current_run
    assert first_run is not None
    second_task = asyncio.create_task(second_machine._launch_line(second, second.lines[0]))
    await asyncio.wait_for(entered.wait(), timeout=_REAL_GIT_TEST_TIMEOUT_SECONDS)
    saved_second = await store.get(second.id)
    assert saved_second is not None
    reserved = saved_second.lines[0].current_run
    assert reserved is not None and reserved != first_run
    assert paths[0] != paths[1]
    assert paths[0].read_text(encoding="utf-8") == "first-worker"
    stale = await LogMonitor(runs).poll(reserved)
    assert stale.complete is False
    first_machine._c.monitor = LogMonitor(runs)
    await first_machine._poll_line(first, first.lines[0])
    kept = await store.get(first.id)
    assert kept is not None
    assert kept.lines[0].log_tail == "first-worker"
    release.set()
    await second_task
    resumed = await store.get(second.id)
    assert resumed is not None
    previous = resumed.lines[0].current_run
    resumed.lines[0].status = LineStatus.ASSIGNED
    resumed.lines[0].current_run = None
    await store.save(resumed)
    third = make(resumed)
    await third._launch_line(resumed, resumed.lines[0])
    assert resumed.lines[0].current_run not in {first_run, previous}


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [124, 127])
async def test_dirty_ordinary_worker_exit_retries_in_same_owned_worktree(
    tmp_path: Path, exit_code: int
) -> None:
    """Ordinary worker exits retry even when the failed run left dirty files."""
    script = tmp_path / "launch.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    git_env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    for argv in (
        ["git", "init", "-q", str(repo)],
        ["git", "-C", str(repo), "config", "core.hooksPath", "/dev/null"],
        ["git", "-C", str(repo), "config", "core.fsmonitor", "false"],
        ["git", "-C", str(repo), "config", "user.name", "Dispatch Test"],
        ["git", "-C", str(repo), "config", "user.email", "dispatch-test@example.invalid"],
    ):
        subprocess.run(argv, check=True, env=git_env)
    (repo / "README").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True, env=git_env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "base"],
        check=True,
        env=git_env,
    )
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    ).stdout.strip()
    runs = tmp_path / "runs"
    worktrees = tmp_path / "worktrees"
    row = _row("dirty-exit-dispatch", task="worker", status=LineStatus.ASSIGNED)
    line = row.lines[0]
    line.branch = f"feature/auto-dispatch-{row.id}/line-{line.id}"
    line.base_commit = base
    calls: list[Path] = []

    async def runner(argv: list[str], cwd: Path, log_path: Path) -> int:
        del argv
        calls.append(cwd)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if len(calls) == 1:
            (cwd / "partial.txt").write_text("preserve for retry\n", encoding="utf-8")
            log_path.write_text("worker failed after start\n", encoding="utf-8")
            log_path.with_suffix(".exit").write_text(str(exit_code), encoding="utf-8")
            return exit_code
        assert (cwd / "partial.txt").read_text(encoding="utf-8") == "preserve for retry\n"
        (cwd / "completed.txt").write_text("committed\n", encoding="utf-8")
        subprocess.run(["git", "add", "partial.txt", "completed.txt"], cwd=cwd, check=True, env=git_env)
        subprocess.run(["git", "commit", "-q", "-m", "retry success"], cwd=cwd, check=True, env=git_env)
        log_path.write_text("retry succeeded\n", encoding="utf-8")
        log_path.with_suffix(".exit").write_text("0", encoding="utf-8")
        return 0

    launcher = ScriptLauncher(
        runs,
        script=script,
        cwd=repo,
        worktrees_dir=worktrees,
        dispatch_id=row.id,
        runner=runner,
    )
    collaborators, machine = _machine(tmp_path, launcher=launcher)
    await collaborators.store.create(row)

    await machine._launch_line(row, line)

    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert line.status is LineStatus.RUNNING
    assert line.current_run is not None
    code, head, _ = await launcher._line_git(repo, calls[1], ["rev-parse", "HEAD"])
    assert code == 0
    assert head.strip() != base
    assert (calls[1] / "partial.txt").exists()


@pytest.mark.asyncio
async def test_stale_worker_exit_does_not_retry_or_enable_dirty_worktree(
    tmp_path: Path,
) -> None:
    script = tmp_path / "launch.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    git_env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    for argv in (
        ["git", "init", "-q", str(repo)],
        ["git", "-C", str(repo), "config", "core.hooksPath", "/dev/null"],
        ["git", "-C", str(repo), "config", "core.fsmonitor", "false"],
        ["git", "-C", str(repo), "config", "user.name", "Dispatch Test"],
        ["git", "-C", str(repo), "config", "user.email", "dispatch-test@example.invalid"],
    ):
        subprocess.run(argv, check=True, env=git_env)
    (repo / "README").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True, env=git_env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "base"],
        check=True,
        env=git_env,
    )
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    ).stdout.strip()
    row = _row("stale-exit-dispatch", task="worker", status=LineStatus.ASSIGNED)
    line = row.lines[0]
    line.branch = f"feature/auto-dispatch-{row.id}/line-{line.id}"
    line.base_commit = base
    calls = 0
    reserved_runs: list[str | None] = []

    async def runner(argv: list[str], cwd: Path, log_path: Path) -> int:
        nonlocal calls
        del argv
        calls += 1
        reserved_runs.append(line.current_run)
        (cwd / "partial.txt").write_text("keep\n", encoding="utf-8")
        line.assigned_model = "new-owner"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("old owner failed\n", encoding="utf-8")
        log_path.with_suffix(".exit").write_text("127", encoding="utf-8")
        return 127

    launcher = ScriptLauncher(
        tmp_path / "runs",
        script=script,
        cwd=repo,
        worktrees_dir=tmp_path / "worktrees",
        dispatch_id=row.id,
        runner=runner,
    )
    collaborators, machine = _machine(tmp_path, launcher=launcher)
    await collaborators.store.create(row)

    await machine._launch_line(row, line)

    assert calls == 1
    assert line.assigned_model == "new-owner"
    assert reserved_runs[0] is not None
    assert line.current_run == reserved_runs[0]
    assert line.attempts == 0
    saved = await collaborators.store.get(row.id)
    assert saved is not None
    saved_line = saved.line(line.id)
    assert saved_line is not None
    assert saved_line.assigned_model == "new-owner"
    assert saved_line.current_run == reserved_runs[0]
    assert saved_line.attempts == 0
    recovery = saved.gates.get("worker_recovery", {})
    line_recovery = recovery.get(line.id, {})
    assert "allow_dirty_worktree" not in line_recovery
    assert line_recovery.get("attempts", {}) == {}


@pytest.mark.asyncio
async def test_a_stale_completion_is_discarded(tmp_path: Path) -> None:
    line_box: dict[str, Line] = {}

    class _Move:
        async def poll(self, run_id: str) -> RunStatus:
            del run_id
            line_box["line"].current_run = "replaced-run"
            return RunStatus(output="foreign-output", complete=True, tokens=4)

        async def cancel(self, run_id: str) -> None:
            del run_id

    row = _row("stale-dispatch", task="work", status=LineStatus.RUNNING)
    row.lines[0].current_run = "dispatch-stale:a:0:original"
    line_box["line"] = row.lines[0]
    collaborators, machine = _machine(tmp_path, monitor=_Move())
    await collaborators.store.create(row)
    await machine._poll_line(row, row.lines[0])
    assert row.lines[0].log_tail != "foreign-output"
    saved = await collaborators.store.get(row.id)
    assert saved is not None
    audit = saved.gates["scheduler"]["audit"]
    assert any(isinstance(item, dict) and item.get("kind") == "stale_result" for item in audit)
    assert saved.lines[0].log_tail != "foreign-output"


class _WatchDict(dict[str, object]):
    """Records mutations, including nested dicts and lists."""

    def __init__(self, sink: list[int], data: dict[str, object] | None = None) -> None:
        super().__init__()
        self._sink = sink
        if data:
            for key, value in data.items():
                super().__setitem__(key, _adopt(value, sink))

    def _mark(self) -> None:
        task = asyncio.current_task()
        self._sink.append(id(task) if task is not None else 0)

    def __setitem__(self, key: str, value: object) -> None:
        self._mark()
        super().__setitem__(key, _adopt(value, self._sink))

    def __delitem__(self, key: str) -> None:
        self._mark()
        super().__delitem__(key)

    def update(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        self._mark()
        merged: dict[str, object] = {}
        for arg in args:
            if isinstance(arg, dict):
                merged.update(arg)
        merged.update(kwargs)
        for key, value in merged.items():
            super().__setitem__(key, _adopt(value, self._sink))

    def setdefault(self, key: str, default: object = None) -> object:
        if key not in self:
            self._mark()
            default = _adopt(default, self._sink)
        return super().setdefault(key, default)

    def pop(self, key: str, *args: object) -> object:
        self._mark()
        return super().pop(key, *args)  # type: ignore[arg-type]

    def clear(self) -> None:
        self._mark()
        super().clear()

    def __deepcopy__(self, memo: dict[int, object]) -> dict[str, object]:
        copied: dict[str, object] = {}
        memo[id(self)] = copied
        for key, value in self.items():
            copied[copy.deepcopy(key, memo)] = copy.deepcopy(value, memo)
        return copied


class _WatchList(list[object]):
    def __init__(self, sink: list[int], data: list[object] | None = None) -> None:
        super().__init__()
        self._sink = sink
        for value in data or []:
            super().append(_adopt(value, sink))

    def _mark(self) -> None:
        task = asyncio.current_task()
        self._sink.append(id(task) if task is not None else 0)

    def append(self, value: object) -> None:
        self._mark()
        super().append(_adopt(value, self._sink))

    def extend(self, values: object) -> None:
        self._mark()
        super().extend(_adopt(item, self._sink) for item in values)  # type: ignore[union-attr]

    def __setitem__(self, index: object, value: object) -> None:
        self._mark()
        super().__setitem__(index, _adopt(value, self._sink))  # type: ignore[index, arg-type]

    def pop(self, index: int = -1) -> object:
        self._mark()
        return super().pop(index)

    def clear(self) -> None:
        self._mark()
        super().clear()

    def __deepcopy__(self, memo: dict[int, object]) -> list[object]:
        copied: list[object] = []
        memo[id(self)] = copied
        copied.extend(copy.deepcopy(item, memo) for item in self)
        return copied


def _adopt(value: object, sink: list[int]) -> object:
    if isinstance(value, dict) and not isinstance(value, _WatchDict):
        return _WatchDict(sink, value)
    if isinstance(value, list) and not isinstance(value, _WatchList):
        return _WatchList(sink, value)
    return value


@pytest.mark.asyncio
async def test_a_gated_run_writes_state_only_on_the_actor(tmp_path: Path) -> None:
    """A real gated ``run`` writes every field from the drive task alone.

    Dispatch, Line, nested gates, ``message`` and ``approval_token`` are all
    traced. ``ReviewResult`` is built and applied on that same task. Two
    writer tasks would fail this test.
    """
    import src.tools.auto_dispatch.machine as machine_mod
    from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line

    collaborators, machine, _commands, _gates = _bundle(tmp_path, [_line("a")])
    row = Dispatch(
        id="state-owner",
        goal="ship",
        user_id="u",
        created_at="now",
        state=DispatchState.SUBMITTED,
    )
    await collaborators.store.create(row)
    writes: list[int] = []
    constructed: list[int] = []
    applied: list[int] = []

    def task_id() -> int:
        task = asyncio.current_task()
        return id(task) if task is not None else 0

    if isinstance(row.gates, dict):
        row.gates = _WatchDict(writes, row.gates)
    original_dispatch_set = Dispatch.__setattr__
    original_line_set = Line.__setattr__
    original_review = machine_mod.ReviewResult
    original_apply = machine._apply_review_result

    def dispatch_set(self: Dispatch, name: str, value: object) -> None:
        if self is row:
            writes.append(task_id())
            if name == "gates":
                value = _adopt(value, writes)
            elif name in {"lines", "outcomes"} and isinstance(value, list | dict):
                value = _adopt(value, writes)
        original_dispatch_set(self, name, value)

    def line_set(self: Line, name: str, value: object) -> None:
        writes.append(task_id())
        if isinstance(value, list):
            value = _adopt(value, writes)
        original_line_set(self, name, value)

    def review_result(*args: object, **kwargs: object) -> object:
        event = original_review(*args, **kwargs)
        constructed.append(task_id())
        return event

    async def apply(event: object, *args: object, **kwargs: object) -> object:
        if type(event).__name__ == "ReviewResult":
            applied.append(task_id())
        return await original_apply(event, *args, **kwargs)

    Dispatch.__setattr__ = dispatch_set  # type: ignore[method-assign]
    Line.__setattr__ = line_set  # type: ignore[method-assign]
    machine_mod.ReviewResult = review_result  # type: ignore[misc, assignment]
    machine._apply_review_result = apply  # type: ignore[method-assign]
    driver = asyncio.create_task(machine.run(row), name="dispatch-driver")
    try:
        await driver
    finally:
        Dispatch.__setattr__ = original_dispatch_set  # type: ignore[method-assign]
        Line.__setattr__ = original_line_set  # type: ignore[method-assign]
        machine_mod.ReviewResult = original_review
        machine._apply_review_result = original_apply  # type: ignore[method-assign]
    assert machine._local is row
    assert writes
    assert set(writes) == {id(driver)}
    assert constructed
    assert set(constructed) == {id(driver)}
    assert applied
    assert set(applied) == {id(driver)}
    assert row.state is DispatchState.APPROVAL


@pytest.mark.asyncio
async def test_completion_held_across_the_deadline_stays_complete(tmp_path: Path) -> None:
    """A completion whose save is still in flight beats a deadline queued behind it.

    The persisted line stays the completed run. A stalled run still becomes
    ``BLOCKED`` with ``watchdog_timeout`` in the sibling watchdog test.
    """
    clock = _Clock()
    store = InMemoryDispatchStore()
    entered = asyncio.Event()
    release = asyncio.Event()
    held = False
    original_save = store.save

    async def save(dispatch: Dispatch) -> Dispatch:
        nonlocal held
        tail = dispatch.lines[0].log_tail if dispatch.lines else ""
        if isinstance(tail, str) and tail.startswith("done ") and not held:
            held = True
            entered.set()
            await release.wait()
        return await original_save(dispatch)

    store.save = save  # type: ignore[method-assign]
    gates = GateRunner(
        GateSettings(
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            watchdog_seconds=5,
            heartbeat_interval_seconds=100,
            heartbeat_threshold_seconds=300,
        ),
        clock=clock,
    )
    row = _row("deadline-race", task="work", status=LineStatus.RUNNING)
    row.lines[0].current_run = "deadline-race:a:0:abc123"
    gates.prepare(row)
    gates.beat(row)
    _collaborators, machine = _machine(tmp_path, store=store, gates=gates, sleep=clock.sleep)
    await store.create(row)
    task = asyncio.create_task(machine._monitor(row))
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert machine._runs["a"].complete is True
    clock.advance(6)
    saw_deadline = False
    for _ in range(300):
        await asyncio.sleep(0)
        pending = list(getattr(machine._queue, "_queue", ()))
        if any(args and type(args[0]).__name__ == "Deadline" for _fn, args, _kwargs, _fut in pending):
            saw_deadline = True
            break
    assert saw_deadline
    release.set()
    await asyncio.wait_for(task, timeout=2)
    saved = await store.get(row.id)
    assert saved is not None
    assert saved.lines[0].status is LineStatus.RUNNING
    assert saved.lines[0].failure_reason is None
    assert saved.lines[0].log_tail == "done deadline-race:a:0:abc123"


class _HoldSave(InMemoryDispatchStore):
    def __init__(self) -> None:
        super().__init__()
        self.hold_next_save = False
        self.save_started = asyncio.Event()
        self.release_save = asyncio.Event()

    async def create(self, dispatch: Dispatch) -> Dispatch:
        self.rows[dispatch.id] = copy.deepcopy(dispatch)
        return copy.deepcopy(dispatch)

    async def save(self, dispatch: Dispatch) -> Dispatch:
        if self.hold_next_save:
            self.hold_next_save = False
            self.save_started.set()
            await self.release_save.wait()
        self.rows[dispatch.id] = copy.deepcopy(dispatch)
        return copy.deepcopy(dispatch)

    async def get(self, dispatch_id: str) -> Dispatch | None:
        row = self.rows.get(dispatch_id)
        return copy.deepcopy(row) if row is not None else None


@pytest.mark.asyncio
async def test_deadline_queued_before_completion_blocks(tmp_path: Path) -> None:
    """A deadline enqueued before the completion wins and stays blocked.

    The later completion is only ``stale_result``. The sibling test covers the
    opposite order, where the completion is applied before the deadline.
    """
    from src.tools.auto_dispatch.machine import Heartbeat

    clock = _Clock()
    store = _HoldSave()
    gates = GateRunner(
        GateSettings(
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            watchdog_seconds=5,
            heartbeat_interval_seconds=100,
            heartbeat_threshold_seconds=300,
        ),
        clock=clock,
    )
    row = _row("dispatch-producer", task="offline", status=LineStatus.RUNNING)
    row.lines[0].current_run = "run-1"
    row.lines[0].attempts = 0
    gates.prepare(row)
    gates.beat(row)
    gates.arm_watch(row, 1)
    await store.create(row)
    _collaborators, machine = _machine(
        tmp_path, store=store, gates=gates, monitor=_Finish(), sleep=clock.sleep
    )
    await machine._submit(machine._activate, row)
    store.hold_next_save = True
    heartbeat = asyncio.create_task(
        machine._submit(machine._apply_heartbeat, Heartbeat(row.id), row)
    )
    await asyncio.wait_for(store.save_started.wait(), timeout=2)
    deadline = asyncio.create_task(machine._wait_deadline(row))
    for _ in range(20):
        if clock._waiters:
            break
        await asyncio.sleep(0)
    assert clock._waiters
    clock.advance(6)
    queued = False
    for _ in range(200):
        await asyncio.sleep(0)
        pending = list(getattr(machine._queue, "_queue", ()))
        if any(args and type(args[0]).__name__ == "Deadline" for _fn, args, _kwargs, _fut in pending):
            queued = True
            break
    assert queued
    poll = asyncio.create_task(machine._poll_line(row, row.lines[0]))
    poll_queued = False
    for _ in range(200):
        await asyncio.sleep(0)
        pending = list(getattr(machine._queue, "_queue", ()))
        if any(args and type(args[0]).__name__ == "PollResult" for _fn, args, _kwargs, _fut in pending):
            poll_queued = True
            break
    assert poll_queued
    store.release_save.set()
    await asyncio.wait_for(heartbeat, timeout=3)
    await asyncio.wait_for(deadline, timeout=3)
    await asyncio.wait_for(poll, timeout=3)
    saved = await store.get(row.id)
    assert saved is not None
    assert saved.lines[0].status is LineStatus.BLOCKED
    assert saved.state is DispatchState.REVIEW
    assert saved.lines[0].failure_reason == "watchdog_timeout"
    assert saved.lines[0].log_tail != "done run-1"
    audit = saved.gates["scheduler"]["audit"]
    assert any(isinstance(item, dict) and item.get("kind") == "stale_result" for item in audit)
    await machine.aclose()


@pytest.mark.asyncio
async def test_three_independent_lines_launch_one_at_a_time(tmp_path: Path) -> None:
    """One dispatch runs ready lines one at a time, in topological order."""
    from tests.unit.tools.auto_dispatch.test_gate_recovery import Planner, _bundle, _line

    class _Overlap:
        def __init__(self) -> None:
            self.live = 0
            self.max_live = 0
            self.order: list[str] = []

        async def launch(self, line: Line, model: str, skills: list[str]) -> str:
            del model, skills
            self.live += 1
            self.max_live = max(self.max_live, self.live)
            self.order.append(line.id)
            return line.current_run or f"run-{line.id}"

        async def poll(self, run_id: str) -> RunStatus:
            self.live = max(0, self.live - 1)
            return RunStatus(output=f"done {run_id}", complete=True, tokens=1)

        async def cancel(self, run_id: str) -> None:
            del run_id

    overlap = _Overlap()
    lines = [_line("b"), _line("a", depends_on=("b",)), _line("c")]
    # _bundle hardcodes ScriptedMonitor and ignores a monitor override.
    collaborators, machine, _commands, _gates = _bundle(
        tmp_path,
        lines,
        planner=Planner(lines),
        launcher=overlap,
    )
    collaborators.monitor = overlap
    row = Dispatch(
        id="serial-lines",
        goal="ship",
        user_id="u",
        created_at="now",
        state=DispatchState.SUBMITTED,
    )
    await collaborators.store.create(row)
    await asyncio.wait_for(machine.run(row), timeout=8)
    assert overlap.max_live == 1
    assert overlap.order == ["b", "a", "c"]
    assert row.state is DispatchState.APPROVAL


@pytest.mark.asyncio
async def test_two_dispatches_launch_in_parallel(tmp_path: Path) -> None:
    """Two dispatches may launch at the same time and do not share a line."""
    from tests.unit.tools.auto_dispatch.test_gate_recovery import Planner, _bundle, _line

    release = asyncio.Event()
    arrived = 0

    class _Barrier:
        async def launch(self, line: Line, model: str, skills: list[str]) -> str:
            del model, skills
            nonlocal arrived
            arrived += 1
            if arrived >= 2:
                release.set()
            await release.wait()
            return line.current_run or f"run-{line.id}"

    launcher = _Barrier()

    async def drive(name: str) -> Dispatch:
        planned = [_line(name)]
        collaborators, machine, _commands, _gates = _bundle(
            tmp_path / name,
            planned,
            planner=Planner(planned),
            launcher=launcher,
        )
        row = Dispatch(
            id=name,
            goal="ship",
            user_id="u",
            created_at="now",
            state=DispatchState.SUBMITTED,
        )
        await collaborators.store.create(row)
        await machine.run(row)
        return row

    left, right = await asyncio.wait_for(
        asyncio.gather(drive("left"), drive("right")),
        timeout=8,
    )
    assert arrived >= 2
    assert left.state is DispatchState.APPROVAL
    assert right.state is DispatchState.APPROVAL
    assert left.lines[0].id == "left"
    assert right.lines[0].id == "right"
    assert left.lines[0].current_run != right.lines[0].current_run


@pytest.mark.asyncio
async def test_recovered_running_line_finishes_before_the_next_launch(tmp_path: Path) -> None:
    """A restored RUNNING line is polled before an independent ASSIGNED line launches."""
    from src.server.services.autonomy.stage_gate import Stage
    from src.tools.auto_dispatch.machine import _load_state
    from tests.unit.tools.auto_dispatch.test_gate_recovery import Planner, _bundle, _line

    class _Overlap:
        def __init__(self) -> None:
            self.live = 0
            self.max_live = 0
            self.order: list[str] = []
            self.a_done = False

        async def launch(self, line: Line, model: str, skills: list[str]) -> str:
            del model, skills
            if line.id == "b":
                assert self.a_done
            self.live += 1
            self.max_live = max(self.max_live, self.live)
            self.order.append(line.id)
            return line.current_run or f"run-{line.id}"

        async def poll(self, run_id: str) -> RunStatus:
            if run_id == "existing-a":
                self.a_done = True
            self.live = max(0, self.live - 1)
            return RunStatus(output=f"done {run_id}", complete=True, tokens=1)

        async def cancel(self, run_id: str) -> None:
            del run_id

    overlap = _Overlap()
    lines = [
        _line("a", status=LineStatus.RUNNING, current_run="existing-a", assigned_model="fake"),
        _line("b", status=LineStatus.ASSIGNED, assigned_model="fake"),
    ]
    collaborators, builder, _commands, gates = _bundle(
        tmp_path, lines, planner=Planner(lines), launcher=overlap
    )
    gates.prepare(Dispatch(id="seed", goal="ship", user_id="u", created_at="now"))
    row = Dispatch(
        id="recovered-serial",
        goal="ship",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        lines=lines,
    )
    gates.prepare(row)
    assert builder._enter_implementation(row, lines[0])
    builder._advance(row, lines[1], "assign", provider="fake")
    assert _load_state(gates.line_snapshot(row, lines[0])).stage is Stage.IMPLEMENTING
    await collaborators.store.create(row)
    restored = Dispatch.from_dict(row.to_dict())
    assert _load_state(gates.line_snapshot(restored, restored.lines[0])).stage is Stage.IMPLEMENTING
    overlap.live = 1
    resumed_collaborators, resumed, _commands2, _gates2 = _bundle(
        tmp_path / "resumed", [], planner=Planner([]), launcher=overlap
    )
    resumed_collaborators.monitor = overlap
    await resumed_collaborators.store.create(restored)
    await asyncio.wait_for(resumed.run(restored), timeout=8)
    await builder.aclose()
    await resumed.aclose()
    assert overlap.a_done
    assert overlap.max_live == 1
    assert overlap.order == ["b"]
    assert restored.state is DispatchState.APPROVAL


def test_serial_idle_rejects_a_launch_while_a_line_is_running(tmp_path: Path) -> None:
    """The overlap is still visible to a direct check.

    ``_launch_guarded`` does not raise on it. It marks ``pending_relaunch``
    with ``serial_wait`` and returns. This helper remains for tests.
    """
    from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line

    lines = [
        _line("a", status=LineStatus.RUNNING, current_run="existing-a", assigned_model="fake"),
        _line("b", status=LineStatus.ASSIGNED, assigned_model="fake"),
    ]
    _collaborators, machine, _commands, gates = _bundle(tmp_path, lines)
    row = Dispatch(
        id="serial-idle",
        goal="ship",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        lines=lines,
    )
    gates.prepare(row)
    with pytest.raises(DispatchError, match="serial invariant"):
        machine._require_serial_idle(row)
    with pytest.raises(DispatchError, match="serial invariant"):
        machine._require_serial_idle(row, [lines[1]])


@pytest.mark.asyncio
async def test_reassign_waits_until_the_other_recovered_line_converges(tmp_path: Path) -> None:
    """Two restored RUNNING lines: a stalled relaunch waits until b has finished.

    Every launch sees no other unfinished line. a then starts again and passes.
    """
    from src.server.services.autonomy.stage_gate import Stage
    from src.tools.auto_dispatch.machine import _load_state
    from tests.unit.tools.auto_dispatch.test_gate_recovery import Planner, _bundle, _line

    class _Overlap:
        def __init__(self) -> None:
            self.live = {"existing-a", "existing-b"}
            self.launches: list[tuple[str, set[str], list[str]]] = []
            self.b_live_during_reassign_launch = False
            self.polls: dict[str, int] = {}
            self.saw_serial_wait = False

        async def launch(self, line: Line, model: str, skills: list[str]) -> str:
            del model, skills
            unfinished = [
                other.id
                for other in row.lines
                if other.id != line.id and other.status is LineStatus.RUNNING
            ]
            self.launches.append((line.id, set(self.live), unfinished))
            if line.id == "a" and "existing-b" in self.live:
                self.b_live_during_reassign_launch = True
            self.live.add(f"new-{line.id}")
            return f"new-{line.id}"

        async def poll(self, run_id: str) -> RunStatus:
            sched = row.gates.get("scheduler") if isinstance(row.gates, dict) else None
            pending = sched.get("pending_relaunch") if isinstance(sched, dict) else None
            if isinstance(pending, list) and any(
                isinstance(item, dict) and item.get("reason") == "serial_wait"
                for item in pending
            ):
                self.saw_serial_wait = True
            count = self.polls.get(run_id, 0) + 1
            self.polls[run_id] = count
            if run_id == "existing-a":
                return RunStatus(output="stalled", stalled=True)
            if run_id == "existing-b" and count == 1:
                return RunStatus(output="still running", complete=False)
            self.live.discard(run_id)
            return RunStatus(output=f"done {run_id}", complete=True, tokens=1)

        async def cancel(self, run_id: str) -> None:
            self.live.discard(run_id)

    overlap = _Overlap()
    lines = [
        _line("a", status=LineStatus.RUNNING, current_run="existing-a", assigned_model="kimi"),
        _line("b", status=LineStatus.RUNNING, current_run="existing-b", assigned_model="glm"),
    ]
    collaborators, machine, _commands, gates = _bundle(
        tmp_path, lines, planner=Planner(lines), launcher=overlap
    )
    row = Dispatch(
        id="multi-running-reassign",
        goal="ship",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        lines=lines,
    )
    gates.prepare(row)
    for line in lines:
        gates.clear_run_settled(row, line)
        assert machine._enter_implementation(row, line)
        line.status = LineStatus.RUNNING
        assert _load_state(gates.line_snapshot(row, line)).stage is Stage.IMPLEMENTING
    restored = Dispatch.from_dict(row.to_dict())
    row = restored
    await collaborators.store.create(restored)
    collaborators.monitor = overlap
    await asyncio.wait_for(machine.run(restored), timeout=8)
    await machine.aclose()
    assert overlap.saw_serial_wait
    assert not overlap.b_live_during_reassign_launch
    assert overlap.launches
    assert all(not unfinished for _line_id, _live, unfinished in overlap.launches)
    assert any(line_id == "a" for line_id, _live, _unfinished in overlap.launches)
    assert restored.line("a") is not None
    assert restored.line("a").status is LineStatus.PASSED
    assert restored.line("b") is not None
    assert restored.line("b").status is LineStatus.PASSED
    assert restored.state is DispatchState.APPROVAL
    sched = restored.gates.get("scheduler") if isinstance(restored.gates, dict) else None
    pending = sched.get("pending_relaunch") if isinstance(sched, dict) else None
    assert pending == []


@pytest.mark.asyncio
async def test_stalled_reassign_writes_only_on_the_drive(tmp_path: Path) -> None:
    """A stall reassigns on the drive, including a same-value settled_lines write."""
    from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line

    class _ReassignMonitor:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def poll(self, run_id: str) -> RunStatus:
            line_id = run_id.split(":")[1]
            seen = self.calls.get(line_id, 0) + 1
            self.calls[line_id] = seen
            if line_id == "b" and seen == 1:
                return RunStatus(output="stall", stalled=True)
            return RunStatus(output=f"done {run_id}", complete=True, tokens=2)

        async def cancel(self, run_id: str) -> None:
            del run_id

    collaborators, machine, _commands, _gates = _bundle(tmp_path, [_line("a"), _line("b")])
    collaborators.monitor = _ReassignMonitor()
    row = Dispatch(
        id="reassign-trace",
        goal="ship",
        user_id="u",
        created_at="now",
        state=DispatchState.SUBMITTED,
    )
    await collaborators.store.create(row)
    writes: list[int] = []
    settled: list[tuple[int, object, object]] = []

    def task_id() -> int:
        task = asyncio.current_task()
        return id(task) if task is not None else 0

    if isinstance(row.gates, dict):
        row.gates = _WatchDict(writes, row.gates)
    original_dispatch_set = Dispatch.__setattr__
    original_line_set = Line.__setattr__
    original_dict_set = _WatchDict.__setitem__

    def dict_set(self: _WatchDict, key: str, value: object) -> None:
        if key == "settled_lines":
            before = self.get(key)
            before_copy = list(before) if isinstance(before, list) else before
            after_copy = list(value) if isinstance(value, list) else value
            settled.append((task_id(), before_copy, after_copy))
        original_dict_set(self, key, value)

    def dispatch_set(self: Dispatch, name: str, value: object) -> None:
        if self is row:
            writes.append(task_id())
            if name == "gates":
                value = _adopt(value, writes)
            elif name in {"lines", "outcomes"} and isinstance(value, list | dict):
                value = _adopt(value, writes)
        original_dispatch_set(self, name, value)

    def line_set(self: Line, name: str, value: object) -> None:
        writes.append(task_id())
        if isinstance(value, list):
            value = _adopt(value, writes)
        original_line_set(self, name, value)

    Dispatch.__setattr__ = dispatch_set  # type: ignore[method-assign]
    Line.__setattr__ = line_set  # type: ignore[method-assign]
    _WatchDict.__setitem__ = dict_set  # type: ignore[method-assign]
    driver = asyncio.current_task()
    try:
        driver = asyncio.create_task(machine.run(row), name="dispatch-driver")
        await asyncio.wait_for(driver, timeout=8)
    finally:
        Dispatch.__setattr__ = original_dispatch_set  # type: ignore[method-assign]
        Line.__setattr__ = original_line_set  # type: ignore[method-assign]
        _WatchDict.__setitem__ = original_dict_set  # type: ignore[method-assign]
        await machine.aclose()
    assert driver is not None
    assert row.state is DispatchState.APPROVAL
    assert writes
    assert set(writes) == {id(driver)}
    assert settled
    assert all(item[0] == id(driver) for item in settled)
    assert any(before == after for _task, before, after in settled)
    assert collaborators.monitor.calls["b"] >= 2


class _FixLauncher:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    async def launch(self, line: Line, model: str, skills: list[str]) -> str:
        del skills
        self.calls.append(
            {
                "line": line.id,
                "task": line.task,
                "model": model,
                "attempts": line.attempts,
            }
        )
        if self.fail and line.id == "a":
            from src.tools.auto_dispatch.protocols import LaunchError

            raise LaunchError("synthetic launch failure")
        return f"new-{line.id}-{len(self.calls)}"


class _OnceBlock:
    def __init__(self, blocker: str) -> None:
        self.blocker = blocker
        self.seen = 0

    async def review_line(self, line_id: str, model: str):
        del model
        from src.tools.auto_dispatch.types import Verdict

        if line_id == "a":
            self.seen += 1
            if self.seen == 1:
                return Verdict.blocked([self.blocker])
        return Verdict.ok()


class _HoldSibling:
    """Completes ``a`` immediately and holds ``b`` until ``release`` is set."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def poll(self, run_id: str) -> RunStatus:
        if run_id == "old-b" and not self.release.is_set():
            return RunStatus(output="still running", complete=False)
        return RunStatus(output=f"done {run_id}", complete=True, tokens=1)

    async def cancel(self, run_id: str) -> None:
        del run_id


def _pair(tmp_path: Path):
    from tests.unit.tools.auto_dispatch.test_gate_recovery import _line
    from tests.unit.tools.auto_dispatch.test_machine_gates import _repo_with_lines

    repo = _repo_with_lines(tmp_path / "repo", "a", "b")
    lines = [
        _line(
            "a",
            status=LineStatus.RUNNING,
            current_run="old-a",
            assigned_model="kimi",
            branch="feature/auto-dispatch-d1/line-a",
        ),
        _line(
            "b",
            status=LineStatus.RUNNING,
            current_run="old-b",
            assigned_model="glm",
            branch="feature/auto-dispatch-d1/line-b",
        ),
    ]
    return repo, lines


def _prepared(tmp_path: Path, lines, repo, **overrides):
    from src.tools.auto_dispatch.gates import GateSettings
    from tests.unit.tools.auto_dispatch.test_gate_recovery import Planner, _bundle

    settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=repo,
        g5_command="pytest -q",
    )
    collaborators, machine, _commands, gates = _bundle(
        tmp_path, lines, planner=Planner(lines), settings=settings, **overrides
    )
    row = Dispatch(
        id="pending-fix",
        goal="ship",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
        lines=lines,
    )
    gates.prepare(row)
    for line in lines:
        gates.clear_run_settled(row, line)
        assert machine._enter_implementation(row, line)
        line.status = LineStatus.RUNNING
        line.current_run = f"old-{line.id}"
    restored = Dispatch.from_dict(row.to_dict())
    return collaborators, machine, gates, restored


def _watch_tasks(machine):
    tasks: list[str] = []
    original = machine._launch_line

    async def wrapped(dispatch, line, *, task=None, force_g2=False, fix=None):
        if line.id == "a" and isinstance(task, str):
            tasks.append(task)
        return await original(dispatch, line, task=task, force_g2=force_g2, fix=fix)

    machine._launch_line = wrapped
    return tasks


def _watch_intent(store):
    seen: dict[str, object] = {}
    original = store.save

    async def save(dispatch: Dispatch) -> Dispatch:
        saved = await original(dispatch)
        pending = saved.gates.get("pending_fix") if isinstance(saved.gates, dict) else None
        if isinstance(pending, dict) and "a" in pending and "row" not in seen:
            seen["row"] = json.loads(json.dumps(saved.to_dict()))
        return saved

    store.save = save
    return seen


@pytest.mark.asyncio
async def test_a_deferred_review_fix_relaunches_the_same_task(tmp_path: Path) -> None:
    """b is still running when a's review fix is deferred. The later launch matches."""
    repo, lines = _pair(tmp_path)
    launcher = _FixLauncher(fail=False)
    collaborators, machine, gates, restored = _prepared(
        tmp_path,
        lines,
        repo,
        launcher=launcher,
        reviewer=_OnceBlock("missing regression test"),
    )
    tasks = _watch_tasks(machine)
    seen = _watch_intent(collaborators.store)
    collaborators.monitor = _HoldSibling()
    collaborators.monitor.release.set()
    await collaborators.store.create(restored)
    await asyncio.wait_for(machine.run(restored), timeout=_REAL_GIT_TEST_TIMEOUT_SECONDS)
    await machine.aclose()
    fix_calls = [call for call in launcher.calls if call["line"] == "a"]
    assert tasks
    assert len(set(tasks)) == 1
    assert fix_calls
    assert all(call["task"] == tasks[0] for call in fix_calls)
    assert "Handoff:" in tasks[0]
    assert "missing regression test" in tasks[0]
    assert tasks[0] != "do a"
    assert all(call["model"] == "kimi" and call["attempts"] == 0 for call in fix_calls)
    line = restored.line("a")
    assert line is not None
    assert line.assigned_model == "kimi"
    assert line.attempts == 0
    assert line.status is LineStatus.PASSED
    assert restored.line("b") is not None
    assert restored.line("b").status is LineStatus.PASSED
    assert restored.state is DispatchState.APPROVAL
    assert gates.line_snapshot(restored, line).get("fix_round") == 1
    assert "pending_fix" not in restored.gates
    snapshot = seen["row"]
    assert isinstance(snapshot, dict)
    intent = snapshot["gates"]["pending_fix"]["a"]
    assert set(intent) == {
        "kind",
        "author",
        "attempt",
        "source_ref",
        "policy",
        "fix_round",
        "epoch",
    }
    assert intent == {
        "kind": "review",
        "author": "kimi",
        "attempt": 0,
        "source_ref": snapshot["gates"]["lines"]["a"]["handoff_ref"],
        "policy": "fix",
        "fix_round": 1,
        "epoch": snapshot["gates"].get("epoch", 0),
    }
    assert tasks[0] not in json.dumps(snapshot["gates"])


@pytest.mark.asyncio
async def test_a_deferred_g2_fix_relaunches_the_same_task(tmp_path: Path) -> None:
    """A G2 fix deferred behind b uses the same rebuilt task, author, and attempt."""
    from src.tools.auto_dispatch.gates import CommandResult
    from tests.unit.tools.auto_dispatch.test_gate_recovery import ScriptedCommands

    repo, lines = _pair(tmp_path)
    launcher = _FixLauncher(fail=False)
    commands = ScriptedCommands(script=[CommandResult(exit_code=1, duration_ms=4)])
    collaborators, machine, gates, restored = _prepared(
        tmp_path,
        lines,
        repo,
        launcher=launcher,
        commands=commands,
    )
    tasks = _watch_tasks(machine)
    seen = _watch_intent(collaborators.store)
    collaborators.monitor = _HoldSibling()
    collaborators.monitor.release.set()
    await collaborators.store.create(restored)
    await asyncio.wait_for(machine.run(restored), timeout=_REAL_GIT_TEST_TIMEOUT_SECONDS)
    await machine.aclose()
    fix_calls = [call for call in launcher.calls if call["line"] == "a"]
    assert tasks
    assert len(set(tasks)) == 1
    assert fix_calls
    assert all(call["task"] == tasks[0] for call in fix_calls)
    assert "results: 0:1" in tasks[0]
    assert "Blockers:\n- none\n" in tasks[0]
    assert all(call["model"] == "kimi" and call["attempts"] == 0 for call in fix_calls)
    line = restored.line("a")
    assert line is not None
    assert line.assigned_model == "kimi"
    assert line.attempts == 0
    assert line.status is LineStatus.PASSED
    assert restored.state is DispatchState.APPROVAL
    assert gates.line_snapshot(restored, line).get("fix_round") == 1
    assert "pending_fix" not in restored.gates
    snapshot = seen["row"]
    assert isinstance(snapshot, dict)
    intent = snapshot["gates"]["pending_fix"]["a"]
    snap = snapshot["gates"]["lines"]["a"]
    revision = snap["gates"]["G2"].get("revision")
    assert intent["kind"] == "g2"
    assert intent["author"] == "kimi"
    assert intent["attempt"] == 0
    assert intent["policy"] == "fix"
    assert intent["source_ref"] in {revision, snap.get("handoff_ref")}
    assert isinstance(intent["source_ref"], str) and intent["source_ref"]
    assert tasks[0] not in json.dumps(snapshot["gates"])
    assert intent["fix_round"] == 1
    assert intent["epoch"] == snapshot["gates"].get("epoch", 0)
    # A G2 fix keeps its G2 note (redacted, bounded), never the task text.
    assert "exit code:" in intent["g2_note"]
    assert set(intent) == {
        "kind",
        "author",
        "attempt",
        "source_ref",
        "policy",
        "fix_round",
        "epoch",
        "g2_note",
    }


@pytest.mark.asyncio
async def test_a_deferred_fix_launch_failure_keeps_the_author(tmp_path: Path) -> None:
    """A resumed fix that cannot start parks in REVIEW and does not reassign."""
    from src.tools.auto_dispatch.gates import FIX_LAUNCH_FAILED

    repo, lines = _pair(tmp_path)
    launcher = _FixLauncher(fail=True)
    collaborators, machine, _gates, restored = _prepared(
        tmp_path,
        lines,
        repo,
        launcher=launcher,
        reviewer=_OnceBlock("missing regression test"),
    )
    tasks = _watch_tasks(machine)
    collaborators.monitor = _HoldSibling()
    collaborators.monitor.release.set()
    await collaborators.store.create(restored)
    await asyncio.wait_for(machine.run(restored), timeout=_REAL_GIT_TEST_TIMEOUT_SECONDS)
    await machine.aclose()
    fix_calls = [call for call in launcher.calls if call["line"] == "a"]
    assert len(fix_calls) == 3
    assert tasks
    assert all(call["task"] == tasks[0] for call in fix_calls)
    assert all(call["model"] == "kimi" and call["attempts"] == 0 for call in fix_calls)
    assert "Handoff:" in tasks[0]
    line = restored.line("a")
    assert line is not None
    assert restored.state is DispatchState.REVIEW
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == FIX_LAUNCH_FAILED
    assert line.assigned_model == "kimi"
    assert line.attempts == 0
    assert "pending_fix" not in restored.gates
    sched = restored.gates.get("scheduler") if isinstance(restored.gates, dict) else None
    pending = sched.get("pending_relaunch") if isinstance(sched, dict) else None
    assert pending == []


@pytest.mark.asyncio
async def test_a_pending_fix_survives_a_restart_without_the_task_text(tmp_path: Path) -> None:
    """The saved intent restarts on a new machine and does not contain the task."""
    repo, lines = _pair(tmp_path)
    launcher = _FixLauncher(fail=False)
    collaborators, machine, _gates, restored = _prepared(
        tmp_path,
        lines,
        repo,
        launcher=launcher,
        reviewer=_OnceBlock("missing regression test"),
    )
    tasks = _watch_tasks(machine)
    seen = _watch_intent(collaborators.store)
    collaborators.monitor = _HoldSibling()
    collaborators.monitor.release.set()
    await collaborators.store.create(restored)
    await asyncio.wait_for(machine.run(restored), timeout=_REAL_GIT_TEST_TIMEOUT_SECONDS)
    await machine.aclose()
    snapshot = seen["row"]
    assert isinstance(snapshot, dict)
    assert tasks
    gates_json = json.dumps(snapshot["gates"])
    assert tasks[0] not in gates_json
    intent = snapshot["gates"]["pending_fix"]["a"]
    assert "Handoff:" not in json.dumps(intent)
    assert tasks[0] not in json.dumps(intent)
    restarted = Dispatch.from_dict(json.loads(json.dumps(snapshot)))
    next_launcher = _FixLauncher(fail=False)
    resumed_collaborators, resumed, _gates2, _restored2 = _prepared(
        tmp_path / "resumed",
        [],
        repo,
    )
    resumed_collaborators.launcher = next_launcher
    resumed_collaborators.monitor = _HoldSibling()
    resumed_collaborators.monitor.release.set()
    await resumed_collaborators.store.create(restarted)
    await asyncio.wait_for(resumed.run(restarted), timeout=_REAL_GIT_TEST_TIMEOUT_SECONDS)
    await resumed.aclose()
    fix_calls = [call for call in next_launcher.calls if call["line"] == "a"]
    assert fix_calls
    assert all(call["task"] == tasks[0] for call in fix_calls)
    assert all(call["model"] == "kimi" and call["attempts"] == 0 for call in fix_calls)
    line = restarted.line("a")
    assert line is not None
    assert line.assigned_model == "kimi"
    assert line.attempts == 0
    assert line.status is LineStatus.PASSED
    assert restarted.state is DispatchState.APPROVAL
    assert "pending_fix" not in restarted.gates


async def _capture_fix_saves(tmp_path: Path, *, kind: str):
    """Run one deferred fix and keep the defer, reserve, and pre-LaunchDone rows."""
    repo, lines = _pair(tmp_path)
    launcher = _FixLauncher(fail=False)
    overrides: dict[str, object] = {}
    if kind == "review":
        overrides["reviewer"] = _OnceBlock("missing regression test")
    else:
        from src.tools.auto_dispatch.gates import CommandResult
        from tests.unit.tools.auto_dispatch.test_gate_recovery import ScriptedCommands

        overrides["commands"] = ScriptedCommands(
            script=[CommandResult(exit_code=1, duration_ms=4)]
        )
    collaborators, machine, gates, restored = _prepared(
        tmp_path, lines, repo, launcher=launcher, **overrides
    )
    tasks = _watch_tasks(machine)
    seen: dict[str, object] = {}
    original_save = collaborators.store.save

    async def save(dispatch: Dispatch) -> Dispatch:
        saved = await original_save(dispatch)
        snap = json.loads(json.dumps(saved.to_dict()))
        line = next(item for item in snap["lines"] if item["id"] == "a")
        pending = snap.get("gates", {}).get("pending_fix") or {}
        intent = pending.get("a") if isinstance(pending, dict) else None
        if (
            isinstance(intent, dict)
            and "defer" not in seen
            and "reserved_run_id" not in intent
        ):
            seen["defer"] = snap
        reserved = intent.get("reserved_run_id") if isinstance(intent, dict) else None
        if (
            isinstance(intent, dict)
            and isinstance(reserved, str)
            and reserved
            and line.get("status") == "ASSIGNED"
            and line.get("current_run") == reserved
            and "reserve" not in seen
        ):
            seen["reserve"] = snap
        return saved

    collaborators.store.save = save
    original_done = machine._apply_launch_done

    async def apply(event, dispatch, line):
        if line.id == "a" and "before_launch_done" not in seen:
            seen["before_launch_done"] = json.loads(json.dumps(dispatch.to_dict()))
        return await original_done(event, dispatch, line)

    machine._apply_launch_done = apply
    collaborators.monitor = _HoldSibling()
    collaborators.monitor.release.set()
    await collaborators.store.create(restored)
    await asyncio.wait_for(machine.run(restored), timeout=_REAL_GIT_TEST_TIMEOUT_SECONDS)
    await machine.aclose()
    return repo, tasks, seen, gates


def _pending_ids(snapshot: dict) -> set[str]:
    sched = snapshot["gates"].get("scheduler") or {}
    raw = sched.get("pending_relaunch") or []
    return {
        item["line_id"]
        for item in raw
        if isinstance(item, dict) and isinstance(item.get("line_id"), str)
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["review", "g2"])
@pytest.mark.parametrize("boundary", ["defer", "reserve", "before_launch_done"])
@pytest.mark.parametrize("fail", [False, True])
async def test_a_fix_restart_keeps_the_task_at_each_save_boundary(
    tmp_path: Path, kind: str, boundary: str, fail: bool
) -> None:
    """Restart after defer, reservation, and the moment before LaunchDone."""
    from src.tools.auto_dispatch.gates import FIX_LAUNCH_FAILED

    repo, tasks, seen, _gates = await _capture_fix_saves(tmp_path, kind=kind)
    snapshot = seen.get(boundary)
    assert isinstance(snapshot, dict)
    assert tasks
    original = tasks[0]
    assert original not in json.dumps(snapshot["gates"])
    intent = snapshot["gates"]["pending_fix"]["a"]
    assert original not in json.dumps(intent)
    assert "Handoff:" not in json.dumps(intent)
    assert snapshot["gates"]["lines"]["a"]["fix_round"] == 1
    if boundary == "defer":
        assert "reserved_run_id" not in intent
        assert "a" in _pending_ids(snapshot)
    else:
        assert intent["reserved_run_id"]
        assert "a" not in _pending_ids(snapshot)
        line_row = next(item for item in snapshot["lines"] if item["id"] == "a")
        assert line_row["status"] == "ASSIGNED"
        assert line_row["current_run"] == intent["reserved_run_id"]
    if kind == "review":
        assert "missing regression test" in original
    else:
        assert "results: 0:1" in original
        assert "Blockers:\n- none\n" in original

    restarted = Dispatch.from_dict(json.loads(json.dumps(snapshot)))
    next_launcher = _FixLauncher(fail=fail)
    resumed_collaborators, resumed, gates, _restored = _prepared(
        tmp_path / "resumed", [], repo
    )
    resumed_collaborators.launcher = next_launcher
    resumed_collaborators.monitor = _HoldSibling()
    resumed_collaborators.monitor.release.set()
    await resumed_collaborators.store.create(restarted)
    await asyncio.wait_for(resumed.run(restarted), timeout=_REAL_GIT_TEST_TIMEOUT_SECONDS)
    await resumed.aclose()
    fix_calls = [call for call in next_launcher.calls if call["line"] == "a"]
    assert fix_calls
    assert all(call["task"] == original for call in fix_calls)
    assert all(call["model"] == "kimi" and call["attempts"] == 0 for call in fix_calls)
    assert {call["model"] for call in next_launcher.calls} == {"kimi"}
    line = restarted.line("a")
    assert line is not None
    assert line.assigned_model == "kimi"
    assert line.attempts == 0
    assert gates.line_snapshot(restarted, line).get("fix_round") == 1
    assert "pending_fix" not in restarted.gates
    if fail:
        assert len(fix_calls) == 3
        assert restarted.state is DispatchState.REVIEW
        assert line.status is LineStatus.BLOCKED
        assert line.failure_reason == FIX_LAUNCH_FAILED
    else:
        assert len(fix_calls) == 1
        assert line.status is LineStatus.PASSED
        assert restarted.state is DispatchState.APPROVAL


@pytest.mark.asyncio
async def test_a_confirmed_fix_clears_the_intent_without_another_launch(
    tmp_path: Path,
) -> None:
    """A RUNNING reserved fix is already started. Resume only clears the intent."""
    repo, tasks, seen, _gates = await _capture_fix_saves(tmp_path, kind="review")
    snapshot = seen.get("reserve")
    assert isinstance(snapshot, dict)
    assert tasks
    for item in snapshot["lines"]:
        if item["id"] == "a":
            item["status"] = "RUNNING"
    restarted = Dispatch.from_dict(json.loads(json.dumps(snapshot)))
    next_launcher = _FixLauncher(fail=False)
    resumed_collaborators, resumed, gates, _restored = _prepared(
        tmp_path / "resumed", [], repo
    )
    resumed_collaborators.launcher = next_launcher
    resumed_collaborators.monitor = _HoldSibling()
    resumed_collaborators.monitor.release.set()
    await resumed_collaborators.store.create(restarted)
    await asyncio.wait_for(resumed.run(restarted), timeout=_REAL_GIT_TEST_TIMEOUT_SECONDS)
    await resumed.aclose()
    assert next_launcher.calls == []
    line = restarted.line("a")
    assert line is not None
    assert line.assigned_model == "kimi"
    assert line.attempts == 0
    assert gates.line_snapshot(restarted, line).get("fix_round") == 1
    assert "pending_fix" not in restarted.gates
    assert restarted.state is DispatchState.APPROVAL


class _JsonStore(InMemoryDispatchStore):
    """Each read and write is a new row, so a restart cannot share the object."""

    @staticmethod
    def clone(row: Dispatch) -> Dispatch:
        return Dispatch.from_dict(json.loads(json.dumps(row.to_dict())))

    async def create(self, row: Dispatch) -> Dispatch:
        self.rows[row.id] = self.clone(row)
        return self.clone(self.rows[row.id])

    async def save(self, row: Dispatch) -> Dispatch:
        self.rows[row.id] = self.clone(row)
        return self.clone(self.rows[row.id])

    async def get(self, row_id: str) -> Dispatch | None:
        row = self.rows.get(row_id)
        return self.clone(row) if row is not None else None


def _solo(tmp_path: Path):
    """One line and no sibling, so a fix reserves instead of deferring."""
    from tests.unit.tools.auto_dispatch.test_gate_recovery import _line
    from tests.unit.tools.auto_dispatch.test_machine_gates import _repo_with_lines

    repo = _repo_with_lines(tmp_path / "repo", "a")
    lines = [
        _line(
            "a",
            status=LineStatus.RUNNING,
            current_run="old-a",
            assigned_model="kimi",
            branch="feature/auto-dispatch-d1/line-a",
        )
    ]
    return repo, lines


async def _direct_reservation(tmp_path: Path, *, kind: str):
    """The reservation save of a single-line fix. The intent is already there."""
    repo, lines = _solo(tmp_path)
    launcher = _FixLauncher(fail=False)
    overrides: dict[str, object] = {}
    if kind == "review":
        overrides["reviewer"] = _OnceBlock("missing regression test")
    else:
        from src.tools.auto_dispatch.gates import CommandResult
        from tests.unit.tools.auto_dispatch.test_gate_recovery import ScriptedCommands

        overrides["commands"] = ScriptedCommands(
            script=[CommandResult(exit_code=1, duration_ms=4)]
        )
    collaborators, machine, _gates, restored = _prepared(
        tmp_path, lines, repo, launcher=launcher, **overrides
    )
    tasks = _watch_tasks(machine)
    seen: dict[str, object] = {}
    store = _JsonStore()
    collaborators.store = store
    original = store.save

    async def save(dispatch: Dispatch) -> Dispatch:
        saved = await original(dispatch)
        snap = json.loads(json.dumps(saved.to_dict()))
        line = next(item for item in snap["lines"] if item["id"] == "a")
        pending = snap.get("gates", {}).get("pending_fix") or {}
        intent = pending.get("a") if isinstance(pending, dict) else None
        reserved = intent.get("reserved_run_id") if isinstance(intent, dict) else None
        if (
            "reservation" not in seen
            and isinstance(intent, dict)
            and isinstance(reserved, str)
            and reserved
            and line.get("status") == "ASSIGNED"
            and line.get("current_run") == reserved
            and str(line.get("current_run") or "").startswith("pending-fix:a:")
        ):
            seen["reservation"] = snap
        return saved

    store.save = save
    await store.create(restored)
    await asyncio.wait_for(machine.run(restored), timeout=_REAL_GIT_TEST_TIMEOUT_SECONDS)
    await machine.aclose()
    return repo, tasks, seen


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["review", "g2"])
@pytest.mark.parametrize("fail", [False, True])
async def test_a_direct_fix_reservation_restarts_the_same_task(
    tmp_path: Path, kind: str, fail: bool
) -> None:
    """A single-line fix survives restart from the reservation save."""
    from src.tools.auto_dispatch.gates import FIX_LAUNCH_FAILED

    repo, tasks, seen = await _direct_reservation(tmp_path, kind=kind)
    snapshot = seen.get("reservation")
    assert isinstance(snapshot, dict)
    assert tasks
    original = tasks[0]
    assert original not in json.dumps(snapshot["gates"])
    intent = snapshot["gates"]["pending_fix"]["a"]
    assert original not in json.dumps(intent)
    assert "Handoff:" not in json.dumps(intent)
    # A G2 fix keeps its G2 note (redacted, bounded), never the task text.
    assert (kind == "g2") == ("g2_note" in intent)
    assert set(intent) - {"g2_note"} == {
        "kind",
        "author",
        "attempt",
        "source_ref",
        "policy",
        "fix_round",
        "epoch",
        "reserved_run_id",
    }
    assert intent["epoch"] == snapshot["gates"].get("epoch", 0)
    assert intent["author"] == "kimi"
    assert intent["attempt"] == 0
    assert intent["policy"] == "fix"
    assert intent["fix_round"] == 1
    assert intent["kind"] == kind
    assert snapshot["gates"]["lines"]["a"]["fix_round"] == 1
    assert "a" not in _pending_ids(snapshot)
    if kind == "review":
        assert "missing regression test" in original
    else:
        assert "results: 0:1" in original
        assert "Blockers:\n- none\n" in original

    restarted = Dispatch.from_dict(json.loads(json.dumps(snapshot)))
    next_launcher = _FixLauncher(fail=fail)
    resumed_collaborators, resumed, gates, _restored = _prepared(
        tmp_path / "resumed", [], repo
    )
    resumed_collaborators.store = _JsonStore()
    resumed_collaborators.launcher = next_launcher
    rotations = {"n": 0}
    reassign = resumed._reassign

    async def _count_reassign(dispatch, line, reason):
        rotations["n"] += 1
        return await reassign(dispatch, line, reason)

    resumed._reassign = _count_reassign
    await resumed_collaborators.store.create(restarted)
    await asyncio.wait_for(resumed.run(restarted), timeout=_REAL_GIT_TEST_TIMEOUT_SECONDS)
    await resumed.aclose()
    fix_calls = [call for call in next_launcher.calls if call["line"] == "a"]
    assert fix_calls
    assert all(call["task"] == original for call in fix_calls)
    assert all(call["model"] == "kimi" and call["attempts"] == 0 for call in fix_calls)
    line = restarted.line("a")
    assert line is not None
    assert line.assigned_model == "kimi"
    assert line.attempts == 0
    assert gates.line_snapshot(restarted, line).get("fix_round") == 1
    assert "pending_fix" not in restarted.gates
    assert original not in json.dumps(restarted.gates)
    if fail:
        assert len(fix_calls) == 3
        assert rotations["n"] == 0
        assert sum(resumed_collaborators.selector._counts.values()) == 0
        assert restarted.state is DispatchState.REVIEW
        assert line.status is LineStatus.BLOCKED
        assert line.failure_reason == FIX_LAUNCH_FAILED
    else:
        assert len(fix_calls) == 1
        assert line.status is LineStatus.PASSED
        assert restarted.state is DispatchState.APPROVAL
