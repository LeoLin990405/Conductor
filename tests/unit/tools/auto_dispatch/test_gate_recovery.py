"""Recovery for the W3a blockers: process groups, G2 binding, resume, worktrees.

These tests drive the shipped runner and machine. Process and git checks use
a real local subprocess and a real temporary repository. They do not call a
model CLI or the network.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.tools.auto_dispatch import (
    AlwaysPassReviewer,
    Collaborators,
    Dispatch,
    DispatchMachine,
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    InMemoryOutcomeStore,
    LineStatus,
    NoopMerger,
    RecordingLauncher,
    RoundRobinSelector,
    ScriptedMonitor,
)
from src.tools.auto_dispatch.execution import ScriptLauncher, _run_command
from src.tools.auto_dispatch.protocols import LaunchError
from src.tools.auto_dispatch.gates import (
    CommandResult,
    GateRunner,
    GateSettings,
    GitWorktreeManager,
    SubprocessCommandRunner,
    _read_all,
    _stop_command,
)
from src.tools.auto_dispatch.types import Line, Verdict

pytestmark = pytest.mark.asyncio

WHEN = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


def _running(pid: int) -> bool:
    done = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    stat = done.stdout.strip()
    if not stat:
        return False
    return not stat.startswith("Z")


def _live_group(pgid: int) -> list[int]:
    """Pids still running in ``pgid``. A zombie counts as exited."""
    done = subprocess.run(
        ["ps", "-e", "-o", "pid=,pgid=,stat="],
        capture_output=True,
        text=True,
        check=False,
    )
    found: list[int] = []
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            pid, group = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if group == pgid and not parts[2].startswith("Z"):
            found.append(pid)
    return found


def _git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    done = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return done.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "gates@example.com")
    _git(path, "config", "user.name", "gates")
    return path


class ScriptedCommands:
    def __init__(self, script: list[CommandResult] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[list[str]] = []

    async def run(self, argv, *, cwd, timeout, log_path):
        self.calls.append(list(argv))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_bytes(b"")
        if self.script:
            return self.script.pop(0)
        return CommandResult(exit_code=0, duration_ms=5)


class HoldCommands:
    """Blocks inside the G2 command so a test can snapshot the saved dispatch."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[list[str]] = []

    async def run(self, argv, *, cwd, timeout, log_path):
        self.calls.append(list(argv))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.started.set()
        await self.release.wait()
        return CommandResult(exit_code=0, duration_ms=1)


class FakeWorktrees:
    def __init__(self) -> None:
        self.added: list[str] = []

    async def add_detached(self, repo, path, start_point) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self.added.append(path.name)
        if path.name == "integration":
            versions = path / "migrations" / "versions"
            versions.mkdir(parents=True, exist_ok=True)
            (versions / "141.py").write_text(
                'revision = "141"\ndown_revision = None\n',
                encoding="utf-8",
            )

    async def remove(self, repo, path) -> None:
        if path.exists():
            shutil.rmtree(path)


class Planner:
    def __init__(self, lines: list[Line]) -> None:
        self._lines = lines

    async def plan(self, goal: str) -> list[Line]:
        return self._lines


class BlockingReviewer:
    def __init__(self) -> None:
        self.reviewed: list[str] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.reviewed.append(line_id)
        return Verdict.blocked(["no tests"])


def _line(line_id: str, **extra) -> Line:
    depends_on = extra.pop("depends_on", ())
    acceptance = extra.pop(
        "acceptance",
        ("cmd: pytest -q tests/unit/server/app/test_health.py",),
    )
    return Line(
        id=line_id,
        name=extra.pop("name", line_id),
        task=extra.pop("task", f"do {line_id}"),
        depends_on=depends_on,
        acceptance=acceptance,
        **extra,
    )


def _bundle(tmp_path: Path, lines: list[Line], **overrides):
    commands = overrides.pop("commands", None) or ScriptedCommands()
    settings = overrides.pop(
        "settings",
        GateSettings(
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            g5_command="pytest -q",
        ),
    )
    gates = overrides.pop("gates", None) or GateRunner(
        settings,
        commands=commands,
        worktrees=overrides.pop("trees", None) or FakeWorktrees(),
        clock=lambda: WHEN,
    )
    reviewer = overrides.pop("reviewer", None) or AlwaysPassReviewer()
    collaborators = Collaborators(
        planner=overrides.pop("planner", None) or Planner(lines),
        selector=RoundRobinSelector(),
        launcher=overrides.pop("launcher", None) or RecordingLauncher(),
        monitor=ScriptedMonitor(),
        reviewer=reviewer,
        merger=NoopMerger(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        outcomes=InMemoryOutcomeStore(),
        store=overrides.pop("store", None) or InMemoryDispatchStore(),
        gates=gates,
    )
    machine = DispatchMachine(collaborators, sleep=_asleep)
    return collaborators, machine, commands, gates


async def _asleep(_seconds: float) -> None:
    return None


async def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"pid file never appeared: {path}")
        await asyncio.sleep(0.02)


async def test_a_timeout_kills_a_child_that_inherited_stdout(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    script = tmp_path / "hold.py"
    script.write_text(
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
        f"Path({str(pids)!r}).write_text(str(os.getpid()) + '\\n' + str(child.pid) + '\\n')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    runner = SubprocessCommandRunner()
    log = tmp_path / "g2.log"
    started = time.monotonic()
    result = await runner.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        timeout=2.0,
        log_path=log,
    )
    elapsed = time.monotonic() - started
    try:
        assert result.timed_out is True
        assert elapsed < 8.0
        text = pids.read_text(encoding="utf-8").split()
        assert len(text) == 2
        assert not _running(int(text[0]))
        assert not _running(int(text[1]))
    finally:
        if pids.exists():
            for raw in pids.read_text(encoding="utf-8").split():
                try:
                    os.kill(int(raw), signal.SIGKILL)
                except OSError:
                    pass


async def test_cancelling_a_command_reaps_parent_and_child(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    script = tmp_path / "hold.py"
    script.write_text(
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
        f"Path({str(pids)!r}).write_text(str(os.getpid()) + '\\n' + str(child.pid) + '\\n')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    runner = SubprocessCommandRunner()
    task = asyncio.create_task(
        runner.run(
            [sys.executable, str(script)],
            cwd=tmp_path,
            timeout=30.0,
            log_path=tmp_path / "g2.log",
        )
    )
    try:
        await _wait_for(pids)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=8)
        text = pids.read_text(encoding="utf-8").split()
        assert len(text) == 2
        assert not _running(int(text[0]))
        assert not _running(int(text[1]))
    finally:
        if not task.done():
            task.cancel()
        if pids.exists():
            for raw in pids.read_text(encoding="utf-8").split():
                try:
                    os.kill(int(raw), signal.SIGKILL)
                except OSError:
                    pass


async def test_cancel_during_the_grace_wait_still_kills(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    script = tmp_path / "hold.py"
    script.write_text(
        "import os, signal, sys, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(pids)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    reader = asyncio.create_task(_read_all(process.stdout))
    await _wait_for(pids)
    task = asyncio.create_task(_stop_command(process, reader))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=8)
        assert not _running(int(pids.read_text(encoding="utf-8").strip()))
    finally:
        if not task.done():
            task.cancel()
        try:
            os.kill(int(pids.read_text(encoding="utf-8").strip()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


async def test_cancelling_a_launch_kills_a_detached_child_that_ignores_term(
    tmp_path: Path,
) -> None:
    parent_file = tmp_path / "parent.pid"
    child_file = tmp_path / "child.pid"
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import os, signal, sys, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]], start_new_session=True)\n"
        "Path(sys.argv[3]).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    log = tmp_path / "runs" / "line.log"
    task = asyncio.create_task(
        _run_command(
            [
                sys.executable,
                str(parent),
                str(child_script),
                str(child_file),
                str(parent_file),
            ],
            tmp_path,
            log,
        )
    )
    pids: list[int] = []
    try:
        await _wait_for(parent_file)
        await _wait_for(child_file)
        pids = [
            int(parent_file.read_text(encoding="utf-8").strip()),
            int(child_file.read_text(encoding="utf-8").strip()),
        ]
        assert _running(pids[0])
        assert _running(pids[1])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=8)
        assert not _running(pids[0])
        assert not _running(pids[1])
    finally:
        if not task.done():
            task.cancel()
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


async def test_script_launcher_cancel_reaps_its_process_group(tmp_path: Path) -> None:
    """``ScriptLauncher.cancel`` stops the launcher and a worker in its group.

    Both ignore SIGTERM, so only the group KILL makes them exit. The child
    does not start a new session: the launch script keeps the worker there.
    """
    parent_file = tmp_path / "parent.pid"
    child_file = tmp_path / "child.pid"
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import os, signal, sys, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()) + '\\n' + str(os.getpgrp()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    script = tmp_path / "launch.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "subprocess.Popen([sys.executable, "
        f"{str(child_script)!r}, {str(child_file)!r}])\n"
        f"Path({str(parent_file)!r}).write_text("
        "str(os.getpid()) + '\\n' + str(os.getpgrp()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    run_id = "run-cancel"
    launcher = ScriptLauncher(
        tmp_path / "runs",
        script=script,
        cwd=tmp_path,
        id_factory=lambda _line: run_id,
    )
    task = asyncio.create_task(
        launcher.launch(Line(id="line-0", name="n", task="stay"), "codex", [])
    )
    pids: list[int] = []
    pgid = 0
    try:
        await _wait_for(parent_file)
        await _wait_for(child_file)
        parent_pid_text, parent_pgid_text = parent_file.read_text(encoding="utf-8").split()
        child_pid_text, child_pgid_text = child_file.read_text(encoding="utf-8").split()
        parent_pid, child_pid = int(parent_pid_text), int(child_pid_text)
        pgid = int(parent_pgid_text)
        pids = [parent_pid, child_pid]
        assert int(child_pgid_text) == pgid
        assert pgid == parent_pid
        assert pgid != os.getpgrp()
        assert _running(parent_pid) and _running(child_pid)
        assert run_id in launcher._processes
        await launcher.cancel(run_id)
        assert not _running(parent_pid)
        assert not _running(child_pid)
        assert _live_group(pgid) == []
        with pytest.raises(LaunchError):
            await asyncio.wait_for(task, timeout=8)
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, LaunchError):
            pass
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if pgid > 1 and pgid != os.getpgrp():
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass


async def test_a_reassign_and_a_new_commit_run_g2_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scripted commands pin today's sequence: no N27 base re-run.
    monkeypatch.setenv("CONDUCTOR_DISPATCH_G2_PREEXISTING_MODE", "off")
    repo = _init_repo(tmp_path / "repo")
    (repo / "README").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    branch = "feature/auto-dispatch-d1/line-line-0"
    _git(repo, "branch", branch)
    sha1 = _git(repo, "rev-parse", branch)
    commands = ScriptedCommands(
        [
            CommandResult(exit_code=0, duration_ms=4),
            CommandResult(exit_code=1, duration_ms=4),
        ]
    )
    reviewer = BlockingReviewer()
    settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=repo,
        g5_command="pytest -q",
        max_g2_fixes=0,
    )
    collaborators, machine, commands, _gates = _bundle(
        tmp_path,
        [_line("line-0")],
        commands=commands,
        settings=settings,
        reviewer=reviewer,
    )
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    parked = await machine.run(dispatch)
    assert parked.state is DispatchState.REVIEW
    assert reviewer.reviewed == ["line-0"]
    g2 = parked.gates["lines"]["line-0"]["gates"]["G2"]
    assert g2["result"] == "pass"
    assert g2["revision"] == sha1
    assert commands.calls == [["pytest", "-q", "tests/unit/server/app/test_health.py"]]
    assert collaborators.merger.merged == []

    _git(repo, "checkout", branch)
    (repo / "README").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "again")
    sha2 = _git(repo, "rev-parse", branch)
    assert sha2 != sha1
    line = parked.line("line-0")
    assert line is not None
    line.assigned_model = "kimi"
    line.attempts += 1
    line.status = LineStatus.ASSIGNED
    line.current_run = None
    line.blockers = []
    again = await DispatchMachine(collaborators, sleep=_asleep).run(parked)
    assert again.state is DispatchState.REVIEW
    assert again.line("line-0").status is LineStatus.BLOCKED
    assert again.line("line-0").failure_reason == "focused_tests_exhausted"
    assert reviewer.reviewed == ["line-0"]
    assert commands.calls == [
        ["pytest", "-q", "tests/unit/server/app/test_health.py"],
        ["pytest", "-q", "tests/unit/server/app/test_health.py"],
    ]
    assert collaborators.merger.merged == []
    assert "integration" not in again.gates or again.gates.get("integration", {}).get(
        "phase"
    ) in (
        None,
        "pending",
    )


async def test_a_new_commit_at_the_same_attempt_runs_g2_again(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "README").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    branch = "feature/auto-dispatch-d1/line-line-0"
    _git(repo, "branch", branch)
    commands = ScriptedCommands([CommandResult(exit_code=0, duration_ms=4)])
    settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=repo,
        g5_command="pytest -q",
    )
    collaborators, _machine, commands, gates = _bundle(
        tmp_path,
        [_line("line-0")],
        commands=commands,
        settings=settings,
    )
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    line = _line("line-0", branch=branch)
    dispatch.lines = [line]
    await gates.run_g2(dispatch, line)
    assert gates.settled(dispatch, line.id)
    assert dispatch.gates["lines"][line.id]["gates"]["G2"]["revision"] == _git(
        repo, "rev-parse", branch
    )
    _git(repo, "checkout", branch)
    (repo / "README").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "again")
    await gates.run_g2(dispatch, line)
    assert len(commands.calls) == 2
    assert dispatch.gates["lines"]["line-0"]["gates"]["G2"]["attempt"] == 0
    assert dispatch.gates["lines"]["line-0"]["gates"]["G2"]["revision"] == _git(
        repo, "rev-parse", branch
    )


async def test_a_new_attempt_at_the_same_commit_runs_g2_again(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "README").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    branch = "feature/auto-dispatch-d1/line-line-0"
    _git(repo, "branch", branch)
    sha = _git(repo, "rev-parse", branch)
    commands = ScriptedCommands([CommandResult(exit_code=0, duration_ms=4)])
    reviewer = BlockingReviewer()
    settings = GateSettings(
        runs_dir=tmp_path / "runs", repo_root=repo, g5_command="pytest -q"
    )
    collaborators, machine, commands, _gates = _bundle(
        tmp_path,
        [_line("line-0")],
        commands=commands,
        settings=settings,
        reviewer=reviewer,
    )
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    parked = await machine.run(dispatch)
    assert parked.gates["lines"]["line-0"]["gates"]["G2"]["revision"] == sha
    line = parked.line("line-0")
    assert line is not None
    line.attempts += 1
    line.status = LineStatus.ASSIGNED
    line.current_run = None
    line.blockers = []
    again = await DispatchMachine(collaborators, sleep=_asleep).run(parked)
    assert len(commands.calls) == 2
    assert again.gates["lines"]["line-0"]["gates"]["G2"]["attempt"] == 1
    assert again.gates["lines"]["line-0"]["gates"]["G2"]["revision"] == sha
    assert collaborators.merger.merged == []


async def test_a_new_machine_resumes_g2_from_a_serialized_store(tmp_path: Path) -> None:
    hold = HoldCommands()
    collaborators, machine, _commands, _gates = _bundle(
        tmp_path, [_line("line-0")], commands=hold
    )
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    task = asyncio.create_task(machine.run(dispatch))
    await asyncio.wait_for(hold.started.wait(), timeout=5)
    raw = json.loads(json.dumps(dispatch.to_dict()))
    assert raw["state"] == DispatchState.MONITORING.value
    assert raw["lines"][0]["status"] == LineStatus.RUNNING.value
    assert raw["gates"]["lines"]["line-0"]["gates"]["G2"]["result"] == "running"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    restored = Dispatch.from_dict(raw)
    commands = ScriptedCommands()
    settings = GateSettings(
        runs_dir=tmp_path / "runs-restored",
        repo_root=tmp_path / "repo",
        g5_command="pytest -q",
    )
    resumed_collaborators, resumed, commands, _gates = _bundle(
        tmp_path,
        [],
        commands=commands,
        settings=settings,
        store=InMemoryDispatchStore(),
    )
    result = await resumed.run(restored)
    assert result.state is DispatchState.APPROVAL
    assert "already MONITORING" not in (result.message or "")
    assert commands.calls[0] == ["pytest", "-q", "tests/unit/server/app/test_health.py"]
    assert result.line("line-0").status is LineStatus.PASSED
    assert resumed_collaborators.merger.merged == ["line-0"]


async def test_ungated_monitoring_is_still_an_error(tmp_path: Path) -> None:
    collaborators = Collaborators(
        planner=Planner([]),
        selector=RoundRobinSelector(),
        launcher=RecordingLauncher(),
        monitor=ScriptedMonitor(),
        reviewer=AlwaysPassReviewer(),
        merger=NoopMerger(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        outcomes=InMemoryOutcomeStore(),
        store=InMemoryDispatchStore(),
    )
    dispatch = Dispatch(
        id="d1",
        goal="ship it",
        user_id="u",
        created_at="now",
        state=DispatchState.MONITORING,
    )
    result = await DispatchMachine(collaborators, sleep=_asleep).run(dispatch)
    assert result.state is DispatchState.ERROR
    assert result.message == "Dispatch d1 is already MONITORING"
    assert not collaborators.launcher.launches


async def test_a_leftover_g5_worktree_is_reused(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    versions = repo / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "141_dispatch.py").write_text(
        'revision = "141"\ndown_revision = None\n',
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    _git(repo, "branch", "integrate/auto-dispatch-d1")
    sha = _git(repo, "rev-parse", "HEAD")
    runs = tmp_path / "runs"
    marker = tmp_path / "g5-ran"
    code = f"open({str(marker)!r}, 'w').write('ran')"
    settings = GateSettings(
        runs_dir=runs,
        repo_root=repo,
        g5_command=f"{sys.executable} -c {shlex.quote(code)}",
        g5_timeout_seconds=30,
    )
    runner = GateRunner(settings, clock=lambda: WHEN)
    dispatch = Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    dispatch.integrate_branch = "integrate/auto-dispatch-d1"
    dispatch.lines = [Line(id="line-0", name="a", task="t", status=LineStatus.PASSED)]
    worktree = runner._gate_worktree_path(dispatch, "integration")
    _git(repo, "worktree", "add", "--detach", str(worktree), sha)
    step = await runner.run_g5(dispatch)
    assert step.passed is True
    assert step.reason == "g5_passed"
    assert marker.read_text(encoding="utf-8") == "ran"
    assert dispatch.gates["integration"]["phase"] == "passed"
    assert dispatch.gates["integration"]["reason"] == "g5_passed"


async def test_a_foreign_g5_path_is_left_in_place(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "README").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    _git(repo, "branch", "integrate/auto-dispatch-d9")
    settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=repo,
        g5_command=f"{sys.executable} -c pass",
        g5_timeout_seconds=30,
    )
    runner = GateRunner(settings, clock=lambda: WHEN)
    dispatch = Dispatch(id="d9", goal="ship it", user_id="u", created_at="now")
    dispatch.integrate_branch = "integrate/auto-dispatch-d9"
    dispatch.lines = [Line(id="line-0", name="a", task="t", status=LineStatus.PASSED)]
    foreign = runner._gate_worktree_path(dispatch, "integration")
    foreign.mkdir(parents=True)
    (foreign / "keep.txt").write_text("keep", encoding="utf-8")
    step = await runner.run_g5(dispatch)
    assert step.passed is False
    assert step.reason == "g5_full_suite_failed"
    assert (foreign / "keep.txt").read_text(encoding="utf-8") == "keep"


async def test_preassigned_lines_launch_in_dependency_order(tmp_path: Path) -> None:
    lines = [
        _line("line-c", depends_on=("line-a",), assigned_model="kimi"),
        _line("line-b", depends_on=("line-a",), assigned_model="glm"),
        _line("line-a", assigned_model="deepseek"),
    ]
    collaborators, machine, _commands, _gates = _bundle(tmp_path, lines)
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    result = await machine.run(dispatch)
    assert result.state is DispatchState.APPROVAL
    assert [item[0] for item in collaborators.launcher.launches] == [
        "line-a",
        "line-c",
        "line-b",
    ]
    assert [item[1] for item in collaborators.launcher.launches] == [
        "deepseek",
        "kimi",
        "glm",
    ]
    assert {line.status for line in result.lines} == {LineStatus.PASSED}
    assert set(result.outcomes) == {"line-a", "line-b", "line-c"}


def _parent_exits_child_holds_stdout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Parent exits 0. The child ignores SIGTERM and keeps the inherited stdout."""
    parent_pid = tmp_path / "parent.pid"
    child_pid = tmp_path / "child.pid"
    script = tmp_path / "exit_with_child.py"
    child_code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n"
    )
    script.write_text(
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        f"Path({str(parent_pid)!r}).write_text(str(os.getpid()))\n"
        f"Path({str(child_pid)!r}).write_text(str(child.pid))\n",
        encoding="utf-8",
    )
    return script, parent_pid, child_pid


def _kill_recorded(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").split():
        try:
            os.kill(int(raw), signal.SIGKILL)
        except (OSError, ValueError):
            pass


async def test_parent_exit_reaps_a_child_that_inherited_stdout(tmp_path: Path) -> None:
    script, parent_pid, child_pid = _parent_exits_child_holds_stdout(tmp_path)
    runner = SubprocessCommandRunner()
    started = time.monotonic()
    try:
        result = await runner.run(
            [sys.executable, str(script)],
            cwd=tmp_path,
            timeout=30.0,
            log_path=tmp_path / "g2.log",
        )
        elapsed = time.monotonic() - started
        parent = int(parent_pid.read_text(encoding="utf-8"))
        child = int(child_pid.read_text(encoding="utf-8"))
        assert result.exit_code == 0
        assert result.timed_out is False
        assert elapsed < 8.0
        assert not _running(parent)
        assert not _running(child)
        assert _live_group(parent) == []
    finally:
        _kill_recorded(parent_pid)
        _kill_recorded(child_pid)


async def test_cancel_during_the_pipe_wait_reaps_the_child(tmp_path: Path) -> None:
    script, parent_pid, child_pid = _parent_exits_child_holds_stdout(tmp_path)
    runner = SubprocessCommandRunner()
    task = asyncio.create_task(
        runner.run(
            [sys.executable, str(script)],
            cwd=tmp_path,
            timeout=30.0,
            log_path=tmp_path / "g2.log",
        )
    )
    try:
        await _wait_for(child_pid)
        await _wait_for(parent_pid)
        parent = int(parent_pid.read_text(encoding="utf-8"))
        child = int(child_pid.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2.0
        while _running(parent):
            if time.monotonic() > deadline:
                raise AssertionError("parent did not exit")
            await asyncio.sleep(0.01)
        assert _running(child)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=8)
        assert not _running(parent)
        assert not _running(child)
        assert _live_group(parent) == []
    finally:
        if not task.done():
            task.cancel()
        _kill_recorded(parent_pid)
        _kill_recorded(child_pid)


class _MoveBranch:
    """Records the worktree HEAD, then moves ``branch`` on the main repo."""

    def __init__(self, repo: Path, branch: str, *, moves: int) -> None:
        self.repo = repo
        self.branch = branch
        self.moves = moves
        self.heads: list[str] = []

    async def run(self, argv, *, cwd, timeout, log_path):
        del argv, timeout
        head = _git(Path(cwd), "rev-parse", "HEAD")
        self.heads.append(head)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_bytes(b"")
        if len(self.heads) <= self.moves:
            name = f"next-{len(self.heads)}.txt"
            (self.repo / name).write_text("moved\n", encoding="utf-8")
            _git(self.repo, "add", name)
            _git(self.repo, "commit", "-m", f"move {len(self.heads)}")
            _git(self.repo, "update-ref", f"refs/heads/{self.branch}", "HEAD")
        return CommandResult(exit_code=0, duration_ms=1)


def _g2_runner(tmp_path: Path, repo: Path, commands: _MoveBranch) -> tuple[GateRunner, Dispatch, Line]:
    branch = "feature"
    line = _line("line-0", acceptance=("cmd: true",), branch=branch)
    settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=repo,
        g2_timeout_seconds=30,
    )
    runner = GateRunner(settings, commands=commands, clock=lambda: WHEN)
    dispatch = Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    dispatch.lines = [line]
    return runner, dispatch, line


async def test_a_branch_move_during_g2_does_not_reuse_the_old_result(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "README").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    _git(repo, "branch", "feature")
    commands = _MoveBranch(repo, "feature", moves=1)
    runner, dispatch, line = _g2_runner(tmp_path, repo, commands)
    step = await runner.run_g2(dispatch, line)
    assert len(commands.heads) == 2
    assert commands.heads[0] != commands.heads[1]
    g2 = dispatch.gates["lines"]["line-0"]["gates"]["G2"]
    assert step.passed is True
    assert g2["result"] == "pass"
    assert g2["revision"] == commands.heads[1]
    assert g2["revision"] != commands.heads[0]
    assert g2["revision"] == _git(repo, "rev-parse", "feature")
    assert g2["attempt"] == 0
    assert runner.allows_review(dispatch, "line-0") is True


async def test_a_second_branch_move_during_g2_fails_closed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "README").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    _git(repo, "branch", "feature")
    commands = _MoveBranch(repo, "feature", moves=2)
    runner, dispatch, line = _g2_runner(tmp_path, repo, commands)
    step = await runner.run_g2(dispatch, line)
    assert len(commands.heads) == 2
    g2 = dispatch.gates["lines"]["line-0"]["gates"]["G2"]
    tip = _git(repo, "rev-parse", "feature")
    assert step.passed is False
    assert step.reason == "g2_revision_moved"
    assert g2["result"] == "fail"
    assert g2["reason"] == "g2_revision_moved"
    assert g2["revision"] == commands.heads[1]
    assert g2["revision"] != tip
    assert runner.allows_review(dispatch, "line-0") is False


def _g5_repo(tmp_path: Path, check: str) -> tuple[Path, Path]:
    repo = _init_repo(tmp_path / "repo")
    versions = repo / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "141_dispatch.py").write_text(
        'revision = "141"\ndown_revision = None\n',
        encoding="utf-8",
    )
    (repo / "check.py").write_text(check, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    _git(repo, "branch", "integrate/auto-dispatch-d1")
    settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=repo,
        g5_command=f"{sys.executable} check.py",
        g5_timeout_seconds=30,
    )
    runner = GateRunner(settings, clock=lambda: WHEN)
    dispatch = Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    dispatch.integrate_branch = "integrate/auto-dispatch-d1"
    worktree = runner._gate_worktree_path(dispatch, "integration")
    _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    return repo, worktree


def _g5_dispatch(tmp_path: Path, repo: Path) -> tuple[GateRunner, Dispatch]:
    settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=repo,
        g5_command=f"{sys.executable} check.py",
        g5_timeout_seconds=30,
    )
    runner = GateRunner(settings, clock=lambda: WHEN)
    dispatch = Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    dispatch.integrate_branch = "integrate/auto-dispatch-d1"
    dispatch.lines = [Line(id="line-0", name="a", task="t", status=LineStatus.PASSED)]
    return runner, dispatch


async def test_a_dirty_tracked_g5_worktree_runs_the_committed_tree(
    tmp_path: Path,
) -> None:
    repo, worktree = _g5_repo(tmp_path, "raise SystemExit(7)\n")
    (worktree / "check.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    runner, dispatch = _g5_dispatch(tmp_path, repo)
    step = await runner.run_g5(dispatch)
    full = dispatch.gates["integration"]["checks"]["full_suite"]
    assert step.passed is False
    assert step.reason == "g5_full_suite_failed"
    assert step.check == "full_suite"
    assert full["exit_code"] == 7
    assert full["result"] == "fail"


async def test_a_dirty_untracked_g5_worktree_runs_the_committed_tree(
    tmp_path: Path,
) -> None:
    check = (
        "from pathlib import Path\n"
        "raise SystemExit(0 if Path('extra.txt').exists() else 7)\n"
    )
    repo, worktree = _g5_repo(tmp_path, check)
    (worktree / "extra.txt").write_text("leak\n", encoding="utf-8")
    runner, dispatch = _g5_dispatch(tmp_path, repo)
    step = await runner.run_g5(dispatch)
    full = dispatch.gates["integration"]["checks"]["full_suite"]
    assert step.passed is False
    assert step.reason == "g5_full_suite_failed"
    assert full["exit_code"] == 7
    assert full["result"] == "fail"


class _DirtyGit:
    def __init__(self, path: Path, sha: str) -> None:
        self.path = path
        self.sha = sha

    async def __call__(self, argv: list[str], cwd: Path) -> tuple[int, str, str]:
        del cwd
        if argv[:2] == ["rev-parse", "--verify"]:
            return 0, self.sha + "\n", ""
        if argv[:2] == ["worktree", "list"]:
            text = f"worktree {self.path.resolve()}\nHEAD {self.sha}\n\n"
            return 0, text, ""
        if argv[:1] == ["status"]:
            return 0, " M check.py\n", ""
        if argv[:2] == ["worktree", "remove"]:
            return 1, "", "busy"
        return 1, "", "unexpected"


async def test_a_dirty_g5_worktree_that_cannot_be_rebuilt_fails_closed(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "README").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "integrate/auto-dispatch-d1")
    dispatch = Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    dispatch.integrate_branch = "integrate/auto-dispatch-d1"
    worktree_runner = GateRunner(GateSettings(runs_dir=tmp_path / "runs", repo_root=repo))
    worktree = worktree_runner._gate_worktree_path(dispatch, "integration")
    worktree.mkdir(parents=True)
    (worktree / "check.py").write_text("dirty\n", encoding="utf-8")
    settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=repo,
        g5_command=f"{sys.executable} check.py",
        g5_timeout_seconds=30,
    )
    runner = GateRunner(
        settings,
        worktrees=GitWorktreeManager(runner=_DirtyGit(worktree, sha)),
        clock=lambda: WHEN,
    )
    dispatch = Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    dispatch.integrate_branch = "integrate/auto-dispatch-d1"
    dispatch.lines = [Line(id="line-0", name="a", task="t", status=LineStatus.PASSED)]
    step = await runner.run_g5(dispatch)
    assert step.passed is False
    assert step.reason == "g5_worktree_dirty"
    assert step.check == "full_suite"
    assert (worktree / "check.py").read_text(encoding="utf-8") == "dirty\n"
