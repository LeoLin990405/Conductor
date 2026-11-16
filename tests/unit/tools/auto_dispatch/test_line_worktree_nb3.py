"""NB3: poisoned line-worktree Git config fails closed at the machine boundary."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

import src.server.services.autonomy.git_delivery as git_delivery_module
import src.tools.auto_dispatch.execution as execution_module
from src.llms.native_cli.base import NativeCliUnavailable
from src.server.services.autonomy.git_delivery import GitDeliveryError, WorkspaceDeliveryLock
from src.tools.auto_dispatch import (
    Collaborators,
    Dispatch,
    DispatchMachine,
    DispatchState,
    LineStatus,
)
from src.tools.auto_dispatch.execution import ScriptLauncher
from src.tools.auto_dispatch.gates import GateSettings
from src.tools.auto_dispatch.protocols import DispatchError
from src.tools.auto_dispatch.types import Line
from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

pytestmark = pytest.mark.asyncio

WORKER_LOG_SECRET = "NB3_WORKER_LOG_OUTPUT_MUST_NOT_LEAK"
GIT_OUTPUT_SECRET = "NB3_GIT_OUTPUT_MUST_NOT_LEAK"
ERROR_MESSAGE = "Git evidence for line L is unavailable"
CLEANUP_SUFFIX = "; terminal worktree cleanup failed"
FIX_TASK = "repair through a non-empty dataclasses.replace task"
EXCEPTION_FACTORIES = (
    pytest.param(lambda message: GitDeliveryError(message), id="git-delivery-error"),
    pytest.param(lambda message: TimeoutError(message), id="timeout-error"),
    pytest.param(
        lambda message: subprocess.TimeoutExpired(
            ["git", "status"], 1, output=message, stderr=message
        ),
        id="subprocess-timeout-expired",
    ),
    pytest.param(lambda message: NativeCliUnavailable(message), id="native-cli-unavailable"),
)


@pytest.fixture(autouse=True)
def _temporary_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))
    monkeypatch.setenv("CONDUCTOR_WORKER_ISOLATION_ROOT", str(tmp_path / "isolation"))
    monkeypatch.setenv("CONDUCTOR_WORKER_LOCK_ROOT", str(tmp_path / "locks"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("NB3_POISON", "0")
    monkeypatch.setenv("NB3_INCLUDE", "")
    monkeypatch.setenv("NB3_PROOF", "")
    monkeypatch.setenv("NB3_DONE", "")
    monkeypatch.setenv("NB3_UNEXPECTED", "")


def _git(cwd: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "NB3 Test")
    _git(repo, "config", "user.email", "nb3@example.invalid")
    _git(repo, "config", "core.hooksPath", "/dev/null")
    _git(repo, "config", "core.fsmonitor", "false")
    (repo / "README").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _dispatch(base: str, dispatch_id: str = "d-nb3") -> Dispatch:
    line = _line("L", assigned_model="kimi", status=LineStatus.ASSIGNED)
    line.branch = f"feature/auto-dispatch-{dispatch_id}/line-L"
    line.base_commit = base
    return Dispatch(
        id=dispatch_id,
        goal="exercise NB3",
        user_id="u",
        created_at="now",
        lines=[line],
        integrate_branch=f"integrate/auto-dispatch-{dispatch_id}",
        state=DispatchState.LAUNCHING,
    )


def _worker_script(path: Path) -> Path:
    path.write_text(
        """#!/bin/sh
set -eu
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null
printf '%s\n' 'NB3_WORKER_LOG_OUTPUT_MUST_NOT_LEAK'
printf 'work\n' > worker.txt
if [ "${NB3_POISON:-}" = "1" ]; then
    common=$(git rev-parse --path-format=absolute --git-common-dir)
    git config --file "$common/config" include.path "$NB3_INCLUDE"
fi
git add worker.txt
git commit -qm 'NB3 worker commit'
if [ "${NB3_POISON:-}" = "1" ]; then
    {
        git rev-parse HEAD
        git config --file "$common/config" --get include.path
    } > "$NB3_PROOF"
    # Keep the include valid: include.path itself is the poison.
fi
if [ -n "${NB3_DONE:-}" ]; then
    : > "$NB3_DONE"
fi
exit 0
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _launcher(state: Path, repo: Path, dispatch: Dispatch, script: Path) -> ScriptLauncher:
    return ScriptLauncher(
        state / "runs",
        script=script,
        cwd=repo,
        worktrees_dir=state / "worktrees",
        dispatch_id=dispatch.id,
    )


class _Selector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    async def select(self, line, exclude, *, role):
        self.calls.append((line.id, str(role), tuple(exclude)))
        return "grok"


class _TaskCopyMachine(DispatchMachine):
    async def _launch_line(self, dispatch, line, *, task=None, force_g2=False, fix=None):
        del task
        return await super()._launch_line(
            dispatch,
            line,
            task=FIX_TASK,
            force_g2=force_g2,
            fix=fix,
        )


async def _drive(
    bundle_root: Path,
    repo: Path,
    dispatch: Dispatch,
    launcher: ScriptLauncher,
) -> tuple[Collaborators, DispatchMachine, Dispatch]:
    settings = GateSettings(
        runs_dir=bundle_root / "gate-runs",
        repo_root=repo,
        g5_command="true",
    )
    collaborators, machine, _commands, _trees = _bundle(
        bundle_root / "bundle",
        dispatch.lines,
        settings=settings,
        selector=_Selector(),
        launcher=launcher,
    )
    assert collaborators.gates is not None
    collaborators.gates.prepare(dispatch)
    await collaborators.store.create(dispatch)
    return collaborators, machine, dispatch


def _include_file(state: Path) -> Path:
    included = state / f"{GIT_OUTPUT_SECRET}.cfg"
    included.write_text('[filter "evil"]\n\tclean = cat\n', encoding="utf-8")
    return included


def _corrupt_include(included: Path) -> None:
    included.write_text(
        f'[filter "evil"\n{GIT_OUTPUT_SECRET} = true\n',
        encoding="utf-8",
    )


def _failure_once_after_worker(
    monkeypatch: pytest.MonkeyPatch,
    done: Path,
    error: BaseException,
) -> list[bool]:
    real_helper = execution_module.run_isolated_git_readonly
    fired: list[bool] = []

    def fail_once(*args, **kwargs):
        if done.exists() and not fired:
            fired.append(True)
            raise error
        return real_helper(*args, **kwargs)

    monkeypatch.setattr(execution_module, "run_isolated_git_readonly", fail_once)
    monkeypatch.setattr(git_delivery_module, "run_isolated_git_readonly", fail_once)
    return fired


def _raise_readonly_when(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    when,
) -> None:
    real_helper = execution_module.run_isolated_git_readonly

    def fail_when(cwd, args, **kwargs):
        if when(tuple(args)):
            raise error
        return real_helper(cwd, args, **kwargs)

    monkeypatch.setattr(execution_module, "run_isolated_git_readonly", fail_when)
    monkeypatch.setattr(git_delivery_module, "run_isolated_git_readonly", fail_when)


def _assert_failed_line(line: Line | None) -> None:
    assert line is not None
    assert line.status is LineStatus.FAILED
    assert line.current_run is None
    assert line.failure_reason == "git_evidence_unavailable"


def _assert_safe_error(result: Dispatch, state: Path, *secrets: str) -> None:
    assert result.state is DispatchState.ERROR
    message = result.message or ""
    assert message in {ERROR_MESSAGE, ERROR_MESSAGE + CLEANUP_SUFFIX}
    paths = {str(state), str(state.resolve())}
    paths.update(str(path) for path in state.parents if path.name.startswith("pytest-"))
    for leak in (*paths, *secrets):
        assert leak not in message


def _worktree_path(launcher: ScriptLauncher, dispatch: Dispatch) -> Path:
    repo = launcher._cwd.resolve()
    root = launcher._worktrees_dir.resolve()
    line = dispatch.lines[0]
    return launcher._line_worktree_path(
        root,
        hashlib.sha256(str(repo).encode()).hexdigest()[:24],
        hashlib.sha256(dispatch.id.encode()).hexdigest()[:24],
        line,
    )


def _assert_terminal_resources(
    machine: DispatchMachine,
    launcher: ScriptLauncher,
    result: Dispatch,
    state: Path,
) -> None:
    assert launcher._processes == {}
    assert machine._runs == {}
    assert not machine._mailbox.locked()
    dispatch_lock = getattr(launcher, "_dispatch_lock", None)
    if dispatch_lock is not None and hasattr(dispatch_lock, "locked"):
        assert not dispatch_lock.locked()
    for workspace in (launcher._cwd.resolve(), _worktree_path(launcher, result)):
        probe = WorkspaceDeliveryLock(
            str(workspace),
            timeout=0,
            lock_root=str(state / "locks"),
            workspace_root=str(workspace),
        )
        probe.acquire()
        probe.release()

    _assert_failed_line(result.line("L"))


async def _assert_saved_terminal(collaborators: Collaborators, result: Dispatch) -> Dispatch:
    saved = await collaborators.store.get(result.id)
    assert saved is not None
    assert saved.state is DispatchState.ERROR
    assert saved.message == result.message
    _assert_failed_line(saved.line("L"))
    return saved


def _assert_no_retry_or_reassign(
    collaborators: Collaborators,
    result: Dispatch,
    state: Path,
    *,
    worker_exits: int,
) -> None:
    selector = collaborators.selector
    assert isinstance(selector, _Selector)
    assert selector.calls == []
    line = result.line("L")
    assert line is not None
    assert line.assigned_model == "kimi"
    assert line.attempts == 0
    exits = sorted((state / "runs").glob("*.exit"))
    assert len(exits) == worker_exits
    assert [path.read_text(encoding="utf-8").strip() for path in exits] == ["0"] * worker_exits


def _assert_conversion(caught, error: BaseException) -> None:
    assert type(caught.value) is DispatchError
    assert str(caught.value) == ERROR_MESSAGE
    assert caught.value.__cause__ is error


async def _direct_launch(launcher: ScriptLauncher, line: Line) -> None:
    await launcher.launch_worker(
        line,
        "kimi",
        [],
        session_id=None,
        session_mode=None,
        handoff_path=None,
    )


async def test_post_run_common_config_include_is_terminal_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base = _repo(tmp_path)
    dispatch = _dispatch(base)
    included = _include_file(tmp_path)
    proof = tmp_path / "worker-proof"
    monkeypatch.setenv("NB3_POISON", "1")
    monkeypatch.setenv("NB3_INCLUDE", str(included))
    monkeypatch.setenv("NB3_PROOF", str(proof))
    launcher = _launcher(tmp_path, repo, dispatch, _worker_script(tmp_path / "worker.sh"))
    collaborators, machine, dispatch = await _drive(tmp_path, repo, dispatch, launcher)

    result = await asyncio.wait_for(machine.run(dispatch), timeout=15)

    committed, configured_include = proof.read_text(encoding="utf-8").splitlines()
    assert committed != base
    assert configured_include == str(included)
    _assert_safe_error(
        result,
        tmp_path,
        str(included),
        str(included.resolve()),
        WORKER_LOG_SECRET,
        GIT_OUTPUT_SECRET,
    )
    _assert_terminal_resources(machine, launcher, result, tmp_path)
    _assert_no_retry_or_reassign(collaborators, result, tmp_path, worker_exits=1)
    await _assert_saved_terminal(collaborators, result)


async def test_isolation_timeout_is_terminal_and_releases_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base = _repo(tmp_path)
    dispatch = _dispatch(base)
    done = tmp_path / "worker-done"
    monkeypatch.setenv("NB3_DONE", str(done))
    launcher = _launcher(tmp_path, repo, dispatch, _worker_script(tmp_path / "worker.sh"))
    collaborators, machine, dispatch = await _drive(tmp_path, repo, dispatch, launcher)
    fired = _failure_once_after_worker(
        monkeypatch,
        done,
        TimeoutError(f"{GIT_OUTPUT_SECRET} at {tmp_path}"),
    )

    result = await asyncio.wait_for(machine.run(dispatch), timeout=15)

    assert fired == [True]
    _assert_safe_error(result, tmp_path, WORKER_LOG_SECRET, GIT_OUTPUT_SECRET)
    _assert_terminal_resources(machine, launcher, result, tmp_path)
    _assert_no_retry_or_reassign(collaborators, result, tmp_path, worker_exits=1)
    await _assert_saved_terminal(collaborators, result)


async def test_terminal_cleanup_failure_is_fail_closed_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base = _repo(tmp_path)
    dispatch = _dispatch(base)
    done = tmp_path / "worker-done"
    monkeypatch.setenv("NB3_DONE", str(done))
    launcher = _launcher(tmp_path, repo, dispatch, _worker_script(tmp_path / "worker.sh"))
    collaborators, machine, dispatch = await _drive(tmp_path, repo, dispatch, launcher)
    fired = _failure_once_after_worker(monkeypatch, done, TimeoutError(GIT_OUTPUT_SECRET))

    async def fail_cleanup(_dispatch, _line):
        raise RuntimeError(f"{GIT_OUTPUT_SECRET} at {tmp_path}")

    monkeypatch.setattr(launcher, "_owned_line_worktrees", fail_cleanup)

    result = await asyncio.wait_for(machine.run(dispatch), timeout=15)

    assert fired == [True]
    _assert_safe_error(result, tmp_path, WORKER_LOG_SECRET, GIT_OUTPUT_SECRET)
    assert result.message == ERROR_MESSAGE + CLEANUP_SUFFIX
    _assert_terminal_resources(machine, launcher, result, tmp_path)
    _assert_no_retry_or_reassign(collaborators, result, tmp_path, worker_exits=1)
    saved = await _assert_saved_terminal(collaborators, result)
    expected_errors = ["line L: terminal worktree cleanup failed (RuntimeError)"]
    assert result.gates["worktree_cleanup"]["errors"] == expected_errors
    assert saved.gates["worktree_cleanup"]["errors"] == expected_errors
    for error in expected_errors:
        assert str(tmp_path) not in error
        assert str(tmp_path.resolve()) not in error
        assert GIT_OUTPUT_SECRET not in error
        assert "at " not in error


async def test_fresh_drive_rejects_an_already_poisoned_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base = _repo(tmp_path)
    dispatch = _dispatch(base)
    seed_launcher = _launcher(tmp_path, repo, dispatch, _worker_script(tmp_path / "seed.sh"))
    _seed_collaborators, _seed_machine, dispatch = await _drive(
        tmp_path / "seed-machine", repo, dispatch, seed_launcher
    )
    worktree = await seed_launcher._prepare_line_worktree(dispatch.lines[0])

    included = _include_file(tmp_path)
    common = Path(_git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    _git(repo, "config", "--file", str(common / "config"), "include.path", str(included))
    (worktree / "worker.txt").write_text("work\n", encoding="utf-8")
    _git(worktree, "add", "worker.txt")
    _git(worktree, "commit", "-qm", "poisoned worker commit")
    assert _git(worktree, "rev-parse", "HEAD") != base
    _corrupt_include(included)

    unexpected = tmp_path / "unexpected-worker-launch"
    monkeypatch.setenv("NB3_UNEXPECTED", str(unexpected))
    script = tmp_path / "must-not-run.sh"
    script.write_text(
        '#!/bin/sh\nset -eu\n: > "$NB3_UNEXPECTED"\nexit 0\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    recovered = Dispatch.from_dict(dispatch.to_dict())
    fresh_launcher = _launcher(tmp_path, repo, recovered, script)
    collaborators, fresh_machine, recovered = await _drive(
        tmp_path / "restart-machine", repo, recovered, fresh_launcher
    )

    result = await asyncio.wait_for(fresh_machine.run(recovered), timeout=15)

    assert not unexpected.exists()
    _assert_safe_error(result, tmp_path, str(included), str(included.resolve()), GIT_OUTPUT_SECRET)
    _assert_terminal_resources(fresh_machine, fresh_launcher, result, tmp_path)
    _assert_no_retry_or_reassign(collaborators, result, tmp_path, worker_exits=0)
    await _assert_saved_terminal(collaborators, result)


async def test_task_copy_failure_fails_original_line_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base = _repo(tmp_path)
    dispatch = _dispatch(base)
    done = tmp_path / "worker-done"
    monkeypatch.setenv("NB3_DONE", str(done))
    launcher = _launcher(tmp_path, repo, dispatch, _worker_script(tmp_path / "worker.sh"))
    collaborators, ordinary_machine, dispatch = await _drive(tmp_path, repo, dispatch, launcher)
    machine = _TaskCopyMachine(
        collaborators,
        config=ordinary_machine._config,
        sleep=ordinary_machine._sleep,
        clock=ordinary_machine._clock,
    )
    original = dispatch.lines[0]
    subjects: list[tuple[Line, str, str | None]] = []
    real_launch_worker = launcher.launch_worker

    async def capture_subject(subject, *args, **kwargs):
        subjects.append((subject, subject.task, subject.current_run))
        return await real_launch_worker(subject, *args, **kwargs)

    monkeypatch.setattr(launcher, "launch_worker", capture_subject)
    fired = _failure_once_after_worker(monkeypatch, done, TimeoutError(GIT_OUTPUT_SECRET))

    result = await asyncio.wait_for(machine.run(dispatch), timeout=15)

    assert fired == [True]
    assert len(subjects) == 1
    subject, task, reserved_run = subjects[0]
    assert subject is not original
    assert task == FIX_TASK
    assert reserved_run is not None
    assert original.task != FIX_TASK
    _assert_safe_error(result, tmp_path, WORKER_LOG_SECRET, GIT_OUTPUT_SECRET)
    _assert_terminal_resources(machine, launcher, result, tmp_path)
    _assert_no_retry_or_reassign(collaborators, result, tmp_path, worker_exits=1)
    await _assert_saved_terminal(collaborators, result)


@pytest.mark.parametrize("factory", EXCEPTION_FACTORIES)
async def test_prepare_git_failure_converts_with_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory,
) -> None:
    repo, base = _repo(tmp_path)
    dispatch = _dispatch(base)
    line = dispatch.lines[0]
    done = tmp_path / "worker-done"
    monkeypatch.setenv("NB3_DONE", str(done))
    launcher = _launcher(tmp_path, repo, dispatch, _worker_script(tmp_path / "worker.sh"))
    await launcher._prepare_line_worktree(line)
    line.current_run = f"{dispatch.id}:L:0:prepare"
    error = factory(GIT_OUTPUT_SECRET)
    _raise_readonly_when(
        monkeypatch,
        error,
        lambda args: args == ("worktree", "list", "--porcelain"),
    )

    with pytest.raises(DispatchError) as caught:
        await _direct_launch(launcher, line)

    _assert_conversion(caught, error)
    _assert_failed_line(line)
    assert not done.exists()
    assert launcher._processes == {}


@pytest.mark.parametrize("factory", EXCEPTION_FACTORIES)
async def test_start_commit_git_failure_converts_with_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory,
) -> None:
    repo, base = _repo(tmp_path)
    dispatch = _dispatch(base)
    line = dispatch.lines[0]
    line.current_run = f"{dispatch.id}:L:0:start"
    done = tmp_path / "worker-done"
    monkeypatch.setenv("NB3_DONE", str(done))
    launcher = _launcher(tmp_path, repo, dispatch, _worker_script(tmp_path / "worker.sh"))
    error = factory(GIT_OUTPUT_SECRET)
    _raise_readonly_when(monkeypatch, error, lambda args: args == ("rev-parse", "HEAD"))

    with pytest.raises(DispatchError) as caught:
        await _direct_launch(launcher, line)

    _assert_conversion(caught, error)
    _assert_failed_line(line)
    assert not done.exists()
    assert launcher._processes == {}


@pytest.mark.parametrize("factory", EXCEPTION_FACTORIES)
async def test_post_run_verify_git_failure_converts_with_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory,
) -> None:
    repo, base = _repo(tmp_path)
    dispatch = _dispatch(base)
    line = dispatch.lines[0]
    line.current_run = f"{dispatch.id}:L:0:verify"
    done = tmp_path / "worker-done"
    monkeypatch.setenv("NB3_DONE", str(done))
    launcher = _launcher(tmp_path, repo, dispatch, _worker_script(tmp_path / "worker.sh"))
    error = factory(GIT_OUTPUT_SECRET)
    fired = _failure_once_after_worker(monkeypatch, done, error)

    with pytest.raises(DispatchError) as caught:
        await _direct_launch(launcher, line)

    assert fired == [True]
    _assert_conversion(caught, error)
    _assert_failed_line(line)
    assert done.exists()
    assert (tmp_path / "runs" / f"{dispatch.id}:L:0:verify.exit").read_text(
        encoding="utf-8"
    ) == "0"
    assert launcher._processes == {}
