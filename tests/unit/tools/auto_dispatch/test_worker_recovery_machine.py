"""Recovery-machine transport regressions using the real launch script and fake CLI."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.tools.auto_dispatch.execution import (
    LaunchResult,
    LogMonitor,
    ScriptLauncher,
    WorkerRunStatus,
    WorkerLaunchFailure,
    supports_explicit_session_id,
    supports_session_resume,
)
from src.tools.auto_dispatch.types import Line
from src.llms.native_cli.failure import FailureClass
from src.tools.auto_dispatch.types import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch.types import RunStatus
from src.server.services.autonomy.stage_gate import Stage, StageGateState


LAUNCH_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "dispatch_launch.sh"


def _fake_kimi(tmp_path: Path, replies: list[tuple[str, int]]) -> tuple[Path, Path]:
    """Install a deterministic CLI double that records the exact session argv.

    Like Kimi 2.5 it creates the session itself: a run without ``--session``
    mints ``kimi-made-<turn>`` and names it in a ``session.resume_hint`` meta
    event, whatever the turn's outcome.
    """
    binary = tmp_path / "fake-kimi"
    trace = tmp_path / "trace.jsonl"
    response_file = tmp_path / "responses.json"
    response_file.write_text(json.dumps(replies), encoding="utf-8")
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, subprocess, sys\n"
        "trace = pathlib.Path(os.environ['FAKE_KIMI_TRACE'])\n"
        "responses = json.loads(pathlib.Path(os.environ['FAKE_KIMI_RESPONSES']).read_text())\n"
        "index = len(trace.read_text().splitlines()) if trace.exists() else 0\n"
        "args = sys.argv[1:]\n"
        "sid = args[args.index('--session') + 1] if '--session' in args else None\n"
        "prompt = args[args.index('-p') + 1] if '-p' in args else ''\n"
        "branch = subprocess.run(['git', 'branch', '--show-current'], capture_output=True, text=True).stdout.strip()\n"
        "with trace.open('a') as stream: stream.write(json.dumps({'sid': sid, 'branch': branch, 'handoff': 'deterministic handoff notes at:' in prompt}) + '\\n')\n"
        "message, code = responses[index]\n"
        "if os.environ.get('FAKE_KIMI_COMMIT') == '1' and code == 0:\n"
        " pathlib.Path('fake-result.txt').write_text(message)\n"
        " subprocess.run(['git', 'add', 'fake-result.txt'], check=True)\n"
        " subprocess.run(['git', 'commit', '-qm', 'fake worker result'], check=True)\n"
        "print(message, flush=True)\n"
        "if sid is None:\n"
        " print(json.dumps({'role': 'meta', 'type': 'session.resume_hint', 'session_id': f'kimi-made-{index}'}), flush=True)\n"
        "sys.exit(code)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary, trace


def _line(line_id: str = "line-a") -> Line:
    return Line(
        id=line_id,
        name=line_id,
        task="implement the bounded change",
        acceptance=("cmd: true", "result is correct"),
        current_run=f"dispatch:{line_id}:run-1",
    )


def _launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replies: list[tuple[str, int]],
    *,
    dispatch_id: str | None = None,
):
    binary, trace = _fake_kimi(tmp_path, replies)
    monkeypatch.setenv("NATIVE_CLI_KIMI_BIN", str(binary))
    monkeypatch.setenv("FAKE_KIMI_TRACE", str(trace))
    monkeypatch.setenv("FAKE_KIMI_RESPONSES", str(tmp_path / "responses.json"))
    monkeypatch.setenv(
        "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-script-logs"),
    )
    monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT", "10")
    monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_STALL", "10")
    # Host-side run metadata and worktree state must not create untracked files
    # inside the worker repository.
    runs = tmp_path.parent / f"{tmp_path.name}-runs"
    runs.mkdir(exist_ok=True)
    if dispatch_id:
        monkeypatch.setenv("FAKE_KIMI_COMMIT", "1")
        git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        git_env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True, env=git_env)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True, env=git_env)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True, env=git_env)
        subprocess.run(["git", "-C", str(tmp_path), "config", "core.hooksPath", "/dev/null"], check=True, env=git_env)
        (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, env=git_env)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True, env=git_env)
        launcher = ScriptLauncher(
            runs,
            script=LAUNCH_SCRIPT,
            cwd=tmp_path,
            worktrees_dir=tmp_path.parent / f"{tmp_path.name}-worktrees",
            dispatch_id=dispatch_id,
        )
        launcher._base_commit = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            env=git_env,
        ).stdout.strip()
    else:
        launcher = ScriptLauncher(runs, script=LAUNCH_SCRIPT, cwd=tmp_path)
    return launcher, trace, runs


def test_real_kimi_transport_keeps_the_cli_id_for_retry_then_rotates_fresh_with_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher, trace, runs = _launcher(
        tmp_path,
        monkeypatch,
        [
            ("HTTP 429 Too Many Requests\nRetry-After: 2", 75),
            ("context_length_exceeded: context length maximum", 78),
            ("completed", 0),
        ],
    )
    line = _line()
    # Kimi created this one on the first, fresh run and named it in its stream.
    first_id = "kimi-made-0"

    async def scenario() -> None:
        with pytest.raises(WorkerLaunchFailure) as rate:
            await launcher.launch_worker(
                line, "kimi", [], session_id=None, session_mode=None, handoff_path=None
            )
        assert rate.value.failure_class is FailureClass.RATE_LIMITED
        assert rate.value.retry_after_seconds == 2
        assert rate.value.session_id == first_id

        with pytest.raises(WorkerLaunchFailure) as context:
            await launcher.launch_worker(
                line, "kimi", [], session_id=first_id, session_mode="resume", handoff_path=None
            )
        assert context.value.failure_class is FailureClass.CONTEXT_EXHAUSTED
        manager = launcher.recovery_session_manager()
        key = "dispatch:d:line:line-a:author:kimi"
        manager.store.set(key, first_id)
        manager.record_failure(key, FailureClass.CONTEXT_EXHAUSTED.value)
        rotation = manager.rotate(key, reason=FailureClass.CONTEXT_EXHAUSTED.value)
        assert rotation.rotated and rotation.session_id
        assert rotation.handoff_path and rotation.handoff_path.exists()
        # The rotated id is the session manager's, not Kimi's: the new session
        # starts fresh, carries the handoff, and Kimi names it.
        result = await launcher.launch_worker(
            line,
            "kimi",
            [],
            session_id=None,
            session_mode=None,
            handoff_path=rotation.handoff_path,
        )
        assert result.session_id == "kimi-made-2"
        assert result.run_id == line.current_run

    asyncio.run(scenario())
    records = [json.loads(row) for row in trace.read_text(encoding="utf-8").splitlines()]
    assert [row["sid"] for row in records] == [None, first_id, None]
    assert records[2]["handoff"] is True
    assert (runs / f"{line.current_run}.exit").read_text(encoding="utf-8") == "0"


def test_public_machine_run_executes_rate_context_rotation_and_success_via_real_kimi_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    launcher, trace, runs = _launcher(
        tmp_path,
        monkeypatch,
        [
            ("HTTP 429 Too Many Requests\nRetry-After: 2", 75),
            ("context_length_exceeded: context length maximum", 78),
            ("completed", 0),
        ],
        dispatch_id="d-auto-recovery",
    )

    async def scenario() -> None:
        line = _line("line-auto", assigned_model="kimi", status=LineStatus.ASSIGNED)
        line.branch = "feature/auto-dispatch-d-auto-recovery/line-line-auto"
        line.base_commit = launcher._base_commit
        from src.tools.auto_dispatch.gates import GateSettings

        collaborators, machine, *_ = _bundle(
            tmp_path,
            [line],
            launcher=launcher,
            monitor=LogMonitor(runs),
            settings=GateSettings(
                runs_dir=runs,
                repo_root=tmp_path,
                g5_command="pytest -q",
            ),
        )
        delays: list[float] = []

        async def sleep(seconds: float) -> None:
            delays.append(seconds)

        machine._sleep = sleep
        dispatch = Dispatch(
            id="d-auto-recovery",
            goal="recover a real worker",
            user_id="u",
            created_at="now",
            lines=[line],
            state=DispatchState.LAUNCHING,
        )
        result = await machine.run(dispatch)
        records = [json.loads(row) for row in trace.read_text(encoding="utf-8").splitlines()]
        # Fresh run with no id; the rate-limit retry resumes the id Kimi
        # created; the rotated session starts fresh again (N22).
        assert [record["sid"] for record in records] == [None, "kimi-made-0", None]
        assert result.gates["worker_recovery"][line.id]["session_id"] == "kimi-made-2"
        assert result.gates["worker_recovery"][line.id]["session_origin"] == "cli"
        assert [record["branch"] for record in records] == [line.branch] * 3
        assert records[2]["handoff"] is True
        assert delays[0] == 2
        assert all(delay >= 0 for delay in delays)
        recovery_events = result.gates["worker_recovery"][line.id]["events"]
        assert [(event["failure_class"], event["action"]) for event in recovery_events] == [
            ("rate_limited", "retry_same"),
            ("context_exhausted", "rotate_session"),
        ]
        formal_timeline = collaborators.gates.line_snapshot(result, line)["events"]
        assert [(event["class"], event["action"]) for event in formal_timeline if "class" in event] == [
            ("rate_limited", "retry_same"),
            ("context_exhausted", "rotate_session"),
        ]
        assert result.line(line.id).status is LineStatus.PASSED
        assert result.gates["worker_recovery"][line.id]["attempts"] == {}

        # With the persisted consecutive-failure counter cleared by that
        # successful turn, the next context failure remains eligible to rotate.
        from src.tools.auto_dispatch.worker_recovery import decide

        next_context = decide(
            FailureClass.CONTEXT_EXHAUSTED,
            None,
            result.gates["worker_recovery"][line.id]["attempts"],
            True,
            datetime.now(timezone.utc),
        )
        assert next_context.kind == "rotate_session"

    asyncio.run(scenario())


def test_generic_worker_exit_keeps_network_or_overload_classifier_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher, _trace, _runs = _launcher(tmp_path, monkeypatch, [("HTTP 503 Service Unavailable", 79)])

    async def scenario() -> None:
        with pytest.raises(WorkerLaunchFailure) as failure:
            await launcher.launch_worker(
                _line(), "kimi", [], session_id=None, session_mode=None, handoff_path=None
            )
        assert failure.value.exit_code == 79
        assert failure.value.failure_class is FailureClass.OVERLOADED
        assert failure.value.reason_code == "worker_overloaded"
        assert "HTTP 503" in failure.value.output
        assert "HTTP 503" not in str(failure.value)
        assert "HTTP 503" not in repr(failure.value)
        failure.value.reason_code = "API_KEY=REDACTION_SENTINEL"
        assert "REDACTION_SENTINEL" not in repr(failure.value)

    asyncio.run(scenario())


def test_two_lines_in_one_worktree_each_resume_the_distinct_session_kimi_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher, trace, _runs = _launcher(
        tmp_path, monkeypatch, [("ok", 0), ("ok", 0), ("ok", 0), ("ok", 0)]
    )

    async def scenario() -> list[str | None]:
        created = []
        for index in range(2):
            result = await launcher.launch_worker(
                _line(f"line-{index}"),
                "kimi",
                [],
                session_id=None,
                session_mode=None,
                handoff_path=None,
            )
            created.append(result.session_id)
        for index, session_id in enumerate(created):
            await launcher.launch_worker(
                _line(f"line-{index}"),
                "kimi",
                [],
                session_id=session_id,
                session_mode="resume",
                handoff_path=None,
            )
        return created

    created = asyncio.run(scenario())
    assert created == ["kimi-made-0", "kimi-made-1"]
    records = [json.loads(row) for row in trace.read_text(encoding="utf-8").splitlines()]
    # Each line resumes its own id, never the cwd's latest session.
    assert [record["sid"] for record in records] == [None, None, *created]


def test_cli_session_capability_map_is_explicit() -> None:
    # N22: Kimi and Grok resume only ids they created; no caller-owned fresh id.
    assert not supports_explicit_session_id("kimi")
    assert not supports_explicit_session_id("grok")
    assert supports_session_resume("kimi")
    assert supports_session_resume("grok")
    assert supports_session_resume("cursor")
    assert supports_session_resume("glm")
    assert not supports_explicit_session_id("stepfun")


def test_log_monitor_classifies_nonzero_exit_but_exit_zero_wins_over_error_text(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "bad.log").write_text("HTTP 429 Too Many Requests", encoding="utf-8")
    (runs / "bad.exit").write_text("75", encoding="utf-8")
    (runs / "ok.log").write_text(
        "HTTP 429 Too Many Requests; context_length_exceeded", encoding="utf-8"
    )
    (runs / "ok.exit").write_text("0", encoding="utf-8")
    monitor = LogMonitor(runs)

    async def scenario() -> None:
        failed = await monitor.poll("bad", model="kimi")
        assert failed.complete and failed.exit_code == 75
        assert failed.failure_class == FailureClass.RATE_LIMITED.value
        assert failed.reason_code == "worker_rate_limited"
        assert failed.retry_after_seconds is None
        succeeded = await monitor.poll("ok", model="kimi")
        assert succeeded.complete and succeeded.exit_code == 0
        assert succeeded.failure_class is None

    asyncio.run(scenario())


def test_public_run_recovers_a_restored_running_line_from_nonzero_exit(
    tmp_path: Path,
) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    class RecoveryLauncher:
        def __init__(self) -> None:
            self.calls: list[tuple[str | None, str | None]] = []

        async def launch_worker(
            self, line, model, skills, *, session_id, session_mode, handoff_path
        ):
            self.calls.append((session_id, session_mode))
            return LaunchResult(line.current_run or "retry", session_id, "recovered")

        async def launch(self, line, model, skills):
            raise AssertionError("the recovery-aware worker seam should be used")

    class RestoredRunMonitor:
        def __init__(self, runs: Path) -> None:
            self.real = LogMonitor(runs)
            self.calls = 0

        async def poll(self, run_id: str, model: str | None = None):
            self.calls += 1
            if self.calls == 1:
                status = await self.real.poll(run_id, model=model)
                return WorkerRunStatus(
                    output=status.output,
                    complete=status.complete,
                    tokens=status.tokens,
                    exit_code=status.exit_code,
                    failure_class=status.failure_class,
                    reason_code="API_KEY=REDACTION_SENTINEL",
                    retry_after_seconds=status.retry_after_seconds,
                )
            return WorkerRunStatus(output="recovered", complete=True, tokens=2, exit_code=0)

        async def cancel(self, run_id: str) -> None:
            await self.real.cancel(run_id)

    async def scenario() -> None:
        old_run = "restored-run"
        line = _line(
            "line-a",
            assigned_model="kimi",
            status=LineStatus.RUNNING,
            attempts=1,
        )
        line.current_run = old_run
        launcher = RecoveryLauncher()
        monitor = RestoredRunMonitor(tmp_path / "runs")
        collaborators, machine, *_ = _bundle(
            tmp_path, [line], launcher=launcher, monitor=monitor
        )
        (tmp_path / "runs").mkdir(exist_ok=True)
        (tmp_path / "runs" / f"{old_run}.log").write_text(
            "HTTP 429 Too Many Requests", encoding="utf-8"
        )
        (tmp_path / "runs" / f"{old_run}.exit").write_text("75", encoding="utf-8")
        dispatch = Dispatch(
            id="d-restored",
            goal="resume a running line",
            user_id="u",
            created_at="now",
            lines=[line],
            state=DispatchState.MONITORING,
        )

        result = await machine.run(dispatch)
        assert monitor.calls >= 2
        # The failed run named no Kimi session, so the retry runs fresh: no
        # caller-owned id is invented for it (N22).
        assert launcher.calls == [(None, None)]
        assert result.gates["worker_recovery"]["line-a"]["last"]["failure_class"] == "rate_limited"
        assert "REDACTION_SENTINEL" not in json.dumps(result.gates)
        assert result.line("line-a").current_run != old_run
        # The retry's completed G2 can pass; without the recovery launch, the
        # old nonzero exit would have been incorrectly treated as its run.
        assert result.line("line-a").status is LineStatus.PASSED

    asyncio.run(scenario())


def _set_stage_state(collaborators, dispatch: Dispatch, line: Line, state: StageGateState) -> None:
    collaborators.gates.prepare(dispatch)
    snapshot = collaborators.gates.line_snapshot(dispatch, line)
    snapshot["machine"] = state.to_dict()


def test_fix_quota_parks_then_retry_after_resumes_original_author_and_stage(
    tmp_path: Path,
) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line
    from src.tools.auto_dispatch.machine import DispatchMachine

    class Launcher:
        def __init__(self) -> None:
            self.models: list[str] = []

        async def launch_worker(
            self, line, model, skills, *, session_id, session_mode, handoff_path
        ):
            self.models.append(model)
            return LaunchResult(line.current_run or "after-quota", session_id, "ok")

    class Monitor:
        def __init__(self) -> None:
            self.calls = 0

        async def poll(self, run_id: str, model: str | None = None):
            self.calls += 1
            if self.calls == 1:
                return WorkerRunStatus(
                    output="quota exhausted",
                    complete=True,
                    exit_code=76,
                    failure_class="quota_exhausted",
                    reason_code="worker_quota_exhausted",
                    retry_after_seconds=60,
                )
            return RunStatus(output="fixed", complete=True, tokens=1)

        async def cancel(self, run_id: str) -> None:
            return None

    async def scenario() -> None:
        line = _line(
            "line-quota",
            assigned_model="kimi",
            status=LineStatus.RUNNING,
            attempts=1,
        )
        line.current_run = "quota-run"
        launcher = Launcher()
        monitor = Monitor()
        collaborators, machine, *_ = _bundle(
            tmp_path, [line], launcher=launcher, monitor=monitor
        )
        machine._sleep = lambda _seconds: asyncio.sleep(0)
        dispatch = Dispatch(
            id="d-quota",
            goal="finish fix with author quota reset",
            user_id="u",
            created_at="now",
            lines=[line],
            state=DispatchState.MONITORING,
        )
        _set_stage_state(
            collaborators,
            dispatch,
            line,
            StageGateState(
                task_id=line.id,
                stage=Stage.FIXING,
                author_provider="kimi",
                developer_provider="kimi",
                return_stage=Stage.PRIMARY_REVIEW.value,
                contributors=("kimi",),
            ),
        )

        parked = await machine.run(dispatch)
        blocked = StageGateState.from_dict(
            collaborators.gates.line_snapshot(parked, line)["machine"]
        )
        assert parked.line(line.id).status is LineStatus.BLOCKED
        assert parked.line(line.id).failure_reason == "author_quota_exhausted"
        assert blocked.stage is Stage.BLOCKED
        assert blocked.blocked_from == Stage.FIXING.value
        assert blocked.return_stage == Stage.PRIMARY_REVIEW.value
        assert parked.gates["worker_recovery"][line.id]["quota_retry_at"] is not None
        await collaborators.store.save(parked)
        restarted = DispatchMachine(collaborators, sleep=lambda _seconds: asyncio.sleep(0))
        before_deadline = await restarted.run(parked)
        assert before_deadline.line(line.id).status is LineStatus.BLOCKED
        assert launcher.models == []
        before_deadline.gates["worker_recovery"][line.id]["quota_retry_at"] = 0
        await collaborators.store.save(before_deadline)

        restarted_after_deadline = DispatchMachine(
            collaborators, sleep=lambda _seconds: asyncio.sleep(0)
        )
        resumed = await restarted_after_deadline.run(before_deadline)
        state = StageGateState.from_dict(
            collaborators.gates.line_snapshot(resumed, line)["machine"]
        )
        assert launcher.models == ["kimi"], (
            resumed.line(line.id),
            resumed.state,
            resumed.gates["worker_recovery"][line.id],
        )
        assert state.author_provider == "kimi"
        assert state.developer_provider == "kimi"
        assert state.contributors == ("kimi",)
        assert state.stage is Stage.AWAITING_HUMAN
        assert state.return_stage == Stage.PRIMARY_REVIEW.value
        timeline = collaborators.gates.line_snapshot(resumed, line)["events"]
        assert any(
            event["from_stage"] == Stage.BLOCKED.value
            and event["to_stage"] == Stage.FIXING.value
            and event["reason"] == "resumed"
            for event in timeline
        )

    asyncio.run(scenario())


def test_auth_park_ignores_stale_quota_retry_deadline(
    tmp_path: Path,
) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    class NoLaunch:
        def __init__(self) -> None:
            self.calls = 0

        async def launch_worker(self, *args, **kwargs):
            self.calls += 1
            return LaunchResult("unexpected", kwargs.get("session_id"), "")

    class Monitor:
        async def poll(self, run_id: str, model: str | None = None):
            return WorkerRunStatus(
                output="authentication failed",
                complete=True,
                exit_code=77,
                failure_class="auth_failed",
                reason_code="worker_auth_failed",
            )

        async def cancel(self, run_id: str) -> None:
            return None

    async def scenario() -> None:
        line = _line("line-auth", assigned_model="kimi", status=LineStatus.RUNNING, attempts=1)
        line.current_run = "auth-run"
        launcher = NoLaunch()
        collaborators, machine, *_ = _bundle(
            tmp_path, [line], launcher=launcher, monitor=Monitor()
        )
        dispatch = Dispatch(
            id="d-auth",
            goal="park authentication failure",
            user_id="u",
            created_at="now",
            lines=[line],
            state=DispatchState.MONITORING,
        )
        _set_stage_state(
            collaborators,
            dispatch,
            line,
            StageGateState(
                task_id=line.id,
                stage=Stage.FIXING,
                author_provider="kimi",
                developer_provider="kimi",
                return_stage=Stage.PRIMARY_REVIEW.value,
            ),
        )
        parked = await machine.run(dispatch)
        entry = parked.gates["worker_recovery"][line.id]
        assert parked.line(line.id).failure_reason == "worker_auth_failed"
        assert entry.get("quota_retry_at") is None
        auth_timeline = collaborators.gates.line_snapshot(parked, line)["events"]
        assert any(
            event.get("class") == "auth_failed"
            and event.get("action") == "park"
            and event.get("reason") == "worker_auth_failed"
            for event in auth_timeline
        )
        # Simulate a stale deadline left by a prior quota park; auth remains terminal.
        entry["quota_retry_at"] = 0
        entry["parked_reason"] = "author_quota_exhausted"
        resumed = await machine.run(parked)
        assert resumed.line(line.id).status is LineStatus.BLOCKED
        assert StageGateState.from_dict(
            collaborators.gates.line_snapshot(resumed, line)["machine"]
        ).stage is Stage.BLOCKED
        assert launcher.calls == 0

    asyncio.run(scenario())


def test_repeated_overload_parks_after_three_retries_with_gate_timeline(
    tmp_path: Path,
) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    class OverloadLauncher:
        def __init__(self) -> None:
            self.calls = 0
            self.sessions: list[str | None] = []
            self.branches: list[str] = []

        async def launch_worker(
            self, line, model, skills, *, session_id, session_mode, handoff_path
        ):
            self.calls += 1
            self.sessions.append(session_id)
            self.branches.append(line.branch)
            raise WorkerLaunchFailure(
                run_id=line.current_run or f"attempt-{self.calls}",
                exit_code=79,
                output="HTTP 503 Service Unavailable",
                session_id=session_id,
                retry_after_seconds=None,
                failure_class=FailureClass.OVERLOADED,
                reason_code="worker_overloaded",
            )

        async def launch(self, *args, **kwargs):
            raise AssertionError("legacy launch path called")

    async def scenario() -> None:
        line = _line("line-overload", assigned_model="kimi", status=LineStatus.ASSIGNED)
        line.branch = "feature/auto-dispatch-d-overload/line-line-overload"
        line.base_commit = "pinned-base"
        launcher = OverloadLauncher()
        collaborators, machine, *_ = _bundle(tmp_path, [line], launcher=launcher)
        delays: list[float] = []

        async def sleep(delay: float) -> None:
            delays.append(delay)

        machine._sleep = sleep
        dispatch = Dispatch(
            id="d-overload",
            goal="park after repeated server overload",
            user_id="u",
            created_at="now",
            lines=[line],
            state=DispatchState.LAUNCHING,
        )
        result = await machine.run(dispatch)
        entry = result.gates["worker_recovery"][line.id]
        assert launcher.calls == 4
        assert len(delays) == 6
        assert 10 <= delays[0] < 11 and 10 <= delays[1] < 11
        assert 20 <= delays[2] < 21 and 20 <= delays[3] < 21
        assert 40 <= delays[4] < 41 and 40 <= delays[5] < 41
        assert len(set(launcher.sessions)) == 1
        assert launcher.branches == [line.branch] * 4
        assert result.line(line.id).status is LineStatus.BLOCKED
        assert entry["attempts"]["overloaded"] == 3
        timeline = collaborators.gates.line_snapshot(result, line)["events"]
        overload_events = [event for event in timeline if event.get("class") == "overloaded"]
        assert [event["action"] for event in overload_events] == [
            "retry_same",
            "retry_same",
            "retry_same",
            "park",
        ]
        assert overload_events[-1]["reason"] == "overloaded_retries_exhausted"

    asyncio.run(scenario())


def test_cancel_during_retry_sleep_does_not_launch_another_attempt(tmp_path: Path) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    class Launcher:
        def __init__(self) -> None:
            self.calls = 0

        async def launch_worker(
            self, line, model, skills, *, session_id, session_mode, handoff_path
        ):
            self.calls += 1
            raise WorkerLaunchFailure(
                run_id=line.current_run or "run",
                exit_code=79,
                output="HTTP 503 Service Unavailable",
                session_id=session_id,
                retry_after_seconds=None,
                failure_class=FailureClass.OVERLOADED,
                reason_code="worker_overloaded",
            )

    async def scenario() -> None:
        line = _line("line-cancel", assigned_model="kimi", status=LineStatus.ASSIGNED)
        launcher = Launcher()
        _collaborators, machine, *_ = _bundle(tmp_path, [line], launcher=launcher)
        sleep_started = asyncio.Event()

        async def blocked_sleep(_seconds: float) -> None:
            sleep_started.set()
            await asyncio.Future()

        machine._sleep = blocked_sleep
        dispatch = Dispatch(
            id="d-cancel",
            goal="cancel before the retry starts",
            user_id="u",
            created_at="now",
            lines=[line],
            state=DispatchState.LAUNCHING,
        )
        task = asyncio.create_task(machine.run(dispatch))
        await asyncio.wait_for(sleep_started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert launcher.calls == 1

    asyncio.run(scenario())


def test_backoff_deadline_survives_machine_reconstruction_before_launch(
    tmp_path: Path,
) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line
    from src.tools.auto_dispatch.machine import DispatchMachine

    class Launcher:
        def __init__(self) -> None:
            self.calls = 0

        async def launch_worker(
            self, line, model, skills, *, session_id, session_mode, handoff_path
        ):
            self.calls += 1
            return LaunchResult(line.current_run or "resume-after-backoff", session_id, "ok")

        async def launch(self, *args, **kwargs):
            raise AssertionError("recovery-aware launch seam required")

    async def scenario() -> None:
        line = _line("line-restart", assigned_model="kimi", status=LineStatus.ASSIGNED)
        launcher = Launcher()
        collaborators, _first_machine, *_ = _bundle(tmp_path, [line], launcher=launcher)
        dispatch = Dispatch(
            id="d-restart",
            goal="resume a rate-limit backoff after process restart",
            user_id="u",
            created_at="now",
            lines=[line],
            state=DispatchState.LAUNCHING,
        )
        collaborators.gates.prepare(dispatch)
        snapshot = collaborators.gates.line_snapshot(dispatch, line)
        snapshot["machine"] = StageGateState(
            task_id=line.id,
            stage=Stage.DAG_READY,
            author_provider="kimi",
            developer_provider="kimi",
        ).to_dict()
        dispatch.gates["worker_recovery"] = {
            line.id: {
                "attempts": {"rate_limited": 1},
                "session_id": "session-before-restart",
                "session_mode": "resume",
                "retry_at": datetime.now(timezone.utc).timestamp() + 60,
                "retry_source": "retry_after",
            }
        }
        await collaborators.store.save(dispatch)
        started = asyncio.Event()
        release = asyncio.Event()
        delays: list[float] = []

        async def blocking_sleep(delay: float) -> None:
            delays.append(delay)
            started.set()
            await release.wait()

        restarted = DispatchMachine(collaborators, sleep=blocking_sleep)
        task = asyncio.create_task(restarted.run(dispatch))
        await asyncio.wait_for(started.wait(), timeout=5)
        assert 59 <= delays[0] <= 60
        assert launcher.calls == 0
        release.set()
        result = await asyncio.wait_for(task, timeout=5)
        assert launcher.calls == 1
        entry = result.gates["worker_recovery"][line.id]
        assert "retry_at" not in entry and "retry_source" not in entry

    asyncio.run(scenario())


def test_fallback_provider_does_not_reuse_prior_session_handoff_or_counters(
    tmp_path: Path,
) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line
    from src.server.services.autonomy.stage_gate import StageGateState

    class SelectGrok:
        async def select(self, line, exclude, *, role):
            assert "kimi" in exclude
            return "grok"

    class Launcher:
        def __init__(self, dispatch: Dispatch) -> None:
            self.dispatch = dispatch
            self.calls: list[tuple[str, str | None, str | None, Path | None, dict]] = []
            self.branches: list[str] = []

        async def launch_worker(
            self, line, model, skills, *, session_id, session_mode, handoff_path
        ):
            self.branches.append(line.branch)
            if model == "kimi":
                raise WorkerLaunchFailure(
                    run_id=line.current_run or "quota-run",
                    exit_code=76,
                    output="quota exhausted",
                    session_id=session_id,
                    retry_after_seconds=None,
                    failure_class=FailureClass.QUOTA_EXHAUSTED,
                    reason_code="worker_quota_exhausted",
                )
            entry = self.dispatch.gates["worker_recovery"][line.id]
            state = StageGateState.from_dict(
                self.dispatch.gates["lines"][line.id]["machine"]
            )
            self.calls.append((model, session_id, session_mode, handoff_path, {
                "attempts": dict(entry["attempts"]),
                "entry_session_id": entry.get("session_id"),
                "entry_handoff_path": entry.get("handoff_path"),
                "author_provider": state.author_provider,
                "developer_provider": state.developer_provider,
                "contributors": state.contributors,
            }))
            return LaunchResult(line.current_run or "fallback-run", session_id, "ok")

        async def launch(self, *args, **kwargs):
            raise AssertionError("recovery-aware launch seam required")

    class Monitor:
        async def poll(self, run_id: str, model: str | None = None):
            return RunStatus(output="ok", complete=True, tokens=1)

        async def cancel(self, run_id: str) -> None:
            return None

    async def scenario() -> None:
        line = _line("line-fallback", assigned_model="kimi", status=LineStatus.ASSIGNED)
        line.branch = "feature/auto-dispatch-d-fallback/line-line-fallback"
        line.base_commit = "pinned-base"
        dispatch = Dispatch(
            id="d-fallback",
            goal="replace a quota-exhausted worker",
            user_id="u",
            created_at="now",
            lines=[line],
            state=DispatchState.LAUNCHING,
        )
        collaborators, machine, *_ = _bundle(
            tmp_path, [line], selector=SelectGrok(), monitor=Monitor()
        )
        collaborators.gates.prepare(dispatch)
        snapshot = collaborators.gates.line_snapshot(dispatch, line)
        snapshot["machine"] = StageGateState(
            task_id=line.id,
            stage=Stage.DAG_READY,
            author_provider="kimi",
            developer_provider="kimi",
            contributors=("kimi",),
        ).to_dict()
        dispatch.gates["worker_recovery"] = {
            line.id: {
                "attempts": {"overloaded": 2, "context_exhausted": 1},
                "session_id": "kimi-old-session",
                "session_mode": "resume",
                "handoff_path": "/old/handoff.md",
            }
        }
        launcher = Launcher(dispatch)
        collaborators.launcher = launcher

        result = await machine.run(dispatch)
        assert len(launcher.calls) == 1
        model, session_id, session_mode, handoff_path, before_launch = launcher.calls[0]
        assert model == "grok"
        # Grok creates its own session: the fallback run takes no id at all.
        assert session_id is None
        assert session_mode is None
        assert handoff_path is None
        assert before_launch["attempts"] == {}
        assert before_launch["entry_session_id"] is None
        assert before_launch["entry_handoff_path"] is None
        assert before_launch["author_provider"] == "grok"
        assert before_launch["developer_provider"] == "grok"
        assert before_launch["contributors"] == ("grok",)
        assert result.line(line.id).assigned_model == "grok"
        assert launcher.branches == [
            "feature/auto-dispatch-d-fallback/line-line-fallback",
            "feature/auto-dispatch-d-fallback/line-line-fallback-a1",
        ]
        branch_events = result.gates["worker_recovery"][line.id]["branch_events"]
        assert branch_events[-1]["old_branch"] == launcher.branches[0]
        assert branch_events[-1]["new_branch"] == launcher.branches[1]

    asyncio.run(scenario())


def test_same_author_reassignment_keeps_prior_epoch_branch_launchable(tmp_path: Path) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    class SelectGrokAgain:
        async def select(self, line, exclude, *, role):
            return "grok"

    async def scenario() -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        git_env = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        git_env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
        commands = (
            ["git", "init", "-q", "-b", "main", str(repo)],
            ["git", "-C", str(repo), "config", "user.name", "Dispatch Test"],
            ["git", "-C", str(repo), "config", "user.email", "dispatch-test@example.invalid"],
            ["git", "-C", str(repo), "config", "core.hooksPath", "/dev/null"],
        )
        for command in commands:
            subprocess.run(command, check=True, env=git_env)
        (repo / "README").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README"], check=True, env=git_env)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True, env=git_env)
        base = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            env=git_env,
        ).stdout.strip()
        line = _line("line-same-author", assigned_model="kimi", status=LineStatus.ASSIGNED)
        initial = "feature/auto-dispatch-d-same-author/line-line-same-author"
        line.branch = initial
        line.base_commit = base
        dispatch = Dispatch(
            id="d-same-author",
            goal="A to B to B keeps B's branch epoch",
            user_id="u",
            created_at="now",
            lines=[line],
            state=DispatchState.LAUNCHING,
        )
        collaborators, machine, *_ = _bundle(
            tmp_path, [line], selector=SelectGrokAgain()
        )
        collaborators.gates.prepare(dispatch)
        await collaborators.store.create(dispatch)

        assert await machine._reassign(dispatch, line, "first author change")
        branch_after_author_change = line.branch
        assert branch_after_author_change == f"{initial}-a1"
        assert await machine._reassign(dispatch, line, "same author selected again")
        assert line.assigned_model == "grok"
        assert line.attempts == 2
        assert line.branch == branch_after_author_change

        launcher = ScriptLauncher(
            tmp_path.parent / f"{tmp_path.name}-runs",
            cwd=repo,
            worktrees_dir=tmp_path.parent / f"{tmp_path.name}-worktrees",
            dispatch_id=dispatch.id,
        )
        worktree = await launcher._prepare_line_worktree(line)
        head = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            env=git_env,
        ).stdout.strip()
        assert head == base

    asyncio.run(scenario())


def test_a_to_b_to_a_reassignment_launches_fresh_epoch_from_base(tmp_path: Path) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    class SelectAThenB:
        def __init__(self):
            self.models = iter(("grok", "kimi"))

        async def select(self, line, exclude, *, role):
            return next(self.models)

    async def scenario() -> None:
        repo = tmp_path / "repo-aba"
        repo.mkdir()
        git_env = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        git_env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})

        def git(*args: str) -> str:
            result = subprocess.run(
                ["git", "-C", str(repo), *args], check=True,
                capture_output=True, text=True, env=git_env,
            )
            return result.stdout.strip()

        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=git_env)
        git("config", "user.name", "Dispatch Test")
        git("config", "user.email", "dispatch-test@example.invalid")
        git("config", "core.hooksPath", "/dev/null")
        (repo / "README").write_text("base\n", encoding="utf-8")
        git("add", "README")
        git("commit", "-qm", "base")
        base = git("rev-parse", "HEAD")
        initial = "feature/auto-dispatch-d-aba/line-line-aba"
        git("branch", initial, base)
        git("checkout", "-q", initial)
        (repo / "author-a.txt").write_text("A work\n", encoding="utf-8")
        git("add", "author-a.txt")
        git("commit", "-qm", "author A work")
        author_a_commit = git("rev-parse", "HEAD")
        git("checkout", "-q", "main")

        line = _line("line-aba", assigned_model="kimi", status=LineStatus.ASSIGNED)
        line.branch = initial
        line.base_commit = base
        dispatch = Dispatch(
            id="d-aba", goal="A to B to A starts a fresh epoch", user_id="u",
            created_at="now", lines=[line], state=DispatchState.LAUNCHING,
        )
        collaborators, machine, *_ = _bundle(
            tmp_path / "bundle-aba", [line], selector=SelectAThenB()
        )
        collaborators.gates.prepare(dispatch)
        await collaborators.store.create(dispatch)

        assert await machine._reassign(dispatch, line, "A to B")
        branch_b = line.branch
        assert branch_b.endswith("-a1")
        launcher = ScriptLauncher(
            tmp_path / "runs-aba", cwd=repo,
            worktrees_dir=tmp_path / "worktrees-aba", dispatch_id=dispatch.id,
        )
        b_worktree = await launcher._prepare_line_worktree(line)
        try:
            (b_worktree / "author-b.txt").write_text("B work\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(b_worktree), "add", "author-b.txt"], check=True, env=git_env)
            subprocess.run(["git", "-C", str(b_worktree), "commit", "-qm", "author B work"], check=True, env=git_env)
            author_b_commit = subprocess.run(
                ["git", "-C", str(b_worktree), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True, env=git_env,
            ).stdout.strip()

            assert await machine._reassign(dispatch, line, "B to A")
            branch_a2 = line.branch
            assert line.assigned_model == "kimi"
            assert branch_a2.endswith("-a2")
            events = collaborators.store.rows[dispatch.id].gates["worker_recovery"][line.id]["branch_events"]
            assert events[-1]["old_branch"] == branch_b
            assert events[-1]["new_branch"] == branch_a2

            a2_worktree = await launcher._prepare_line_worktree(line)
            assert a2_worktree != b_worktree
            try:
                a2_head = subprocess.run(
                    ["git", "-C", str(a2_worktree), "rev-parse", "HEAD"],
                    check=True, capture_output=True, text=True, env=git_env,
                ).stdout.strip()
                assert a2_head == base
                for old_commit in (author_a_commit, author_b_commit):
                    result = subprocess.run(
                        ["git", "-C", str(repo), "merge-base", "--is-ancestor", old_commit, base],
                        env=git_env,
                    )
                    assert result.returncode == 1
                assert not (a2_worktree / "author-a.txt").exists()
                assert not (a2_worktree / "author-b.txt").exists()
            finally:
                subprocess.run(
                    ["git", "-C", str(repo), "worktree", "remove", "--force", str(a2_worktree)],
                    check=True, capture_output=True, env=git_env,
                )
        finally:
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(b_worktree)],
                check=True, capture_output=True, env=git_env,
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("stale_field", ["run_id", "attempt", "author", "epoch"])
def test_stale_worker_failure_cannot_write_recovery_state(tmp_path: Path, stale_field: str) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    async def scenario() -> None:
        line = _line("line-a", assigned_model="kimi", status=LineStatus.PASSED, attempts=1)
        line.current_run = "run-current"
        _collaborators, machine, *_ = _bundle(tmp_path, [line])
        dispatch = Dispatch(
            id="d-stale",
            goal="stale fence",
            user_id="u",
            created_at="now",
            state=DispatchState.LAUNCHING,
            lines=[line],
        )
        before = json.dumps(dispatch.gates, sort_keys=True)
        failure = WorkerLaunchFailure(
            run_id=line.current_run or "run-current",
            exit_code=79,
            output="HTTP 503 Service Unavailable",
            session_id="session-old",
            retry_after_seconds=None,
            failure_class=FailureClass.OVERLOADED,
            reason_code="worker_overloaded",
        )
        from src.tools.auto_dispatch.gates import drive_epoch

        attempt = line.attempts
        author = line.assigned_model or ""
        epoch = drive_epoch(dispatch)
        if stale_field == "run_id":
            failure.run_id = "run-stale"
        elif stale_field == "attempt":
            attempt += 1
        elif stale_field == "author":
            author = "grok"
        else:
            epoch += 1
        applied = await machine._recover_worker_failure(
            dispatch, line, failure, False, attempt=attempt, author=author, epoch=epoch
        )
        assert applied is False
        assert json.dumps(dispatch.gates, sort_keys=True) == before
        assert line.current_run == ("run-current" if stale_field == "run_id" else None)

    asyncio.run(scenario())


def test_fix_round_crash_then_fresh_retry_crash_parks_with_original_author(
    tmp_path: Path,
) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line
    from src.server.services.autonomy.stage_gate import StageGateState

    class NoReassign:
        async def select(self, line, exclude, *, role):
            raise AssertionError("a fix crash must keep its original author")

    class CrashTwice:
        def __init__(self) -> None:
            self.models: list[str] = []
            self.branches: list[str] = []

        async def launch_worker(self, line, model, skills, *, session_id, session_mode, handoff_path):
            self.models.append(model)
            self.branches.append(line.branch)
            raise WorkerLaunchFailure(
                run_id=line.current_run or f"crash-{len(self.models)}",
                exit_code=79,
                output="worker crashed",
                session_id=session_id,
                retry_after_seconds=None,
                failure_class=FailureClass.CRASH,
                reason_code="worker_crashed",
            )

    async def scenario() -> None:
        line = _line("line-fix-crash", assigned_model="kimi", status=LineStatus.ASSIGNED)
        line.branch = "feature/auto-dispatch-d-fix-crash/line-line-fix-crash"
        line.base_commit = "pinned-base"
        launcher = CrashTwice()
        collaborators, machine, *_ = _bundle(
            tmp_path, [line], launcher=launcher, selector=NoReassign()
        )
        dispatch = Dispatch(
            id="d-fix-crash",
            goal="keep fix work with its author",
            user_id="u",
            created_at="now",
            lines=[line],
            state=DispatchState.LAUNCHING,
        )
        _set_stage_state(
            collaborators,
            dispatch,
            line,
            StageGateState(
                task_id=line.id,
                stage=Stage.FIXING,
                author_provider="kimi",
                developer_provider="kimi",
                return_stage=Stage.PRIMARY_REVIEW.value,
                contributors=("kimi",),
            ),
        )

        result = await machine.run(dispatch)
        state = StageGateState.from_dict(
            collaborators.gates.line_snapshot(result, line)["machine"]
        )
        assert launcher.models == ["kimi", "kimi"]
        assert launcher.branches == [line.branch, line.branch]
        assert result.line(line.id).status is LineStatus.BLOCKED
        assert result.line(line.id).assigned_model == "kimi"
        assert result.line(line.id).failure_reason == "fix_launch_failed"
        assert state.stage is Stage.BLOCKED
        assert state.author_provider == "kimi"
        assert state.developer_provider == "kimi"
        assert state.contributors == ("kimi",)

    asyncio.run(scenario())


def test_fix_poll_rate_limit_then_relaunch_quota_parks_author(
    tmp_path: Path,
) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line
    from src.server.services.autonomy.stage_gate import StageGateState

    class NoReassign:
        async def select(self, line, exclude, *, role):
            raise AssertionError("a fix quota failure must not reassign")

    class QuotaOnRelaunch:
        def __init__(self) -> None:
            self.models: list[str] = []

        async def launch_worker(self, line, model, skills, *, session_id, session_mode, handoff_path):
            self.models.append(model)
            raise WorkerLaunchFailure(
                run_id=line.current_run or "quota-relaunch",
                exit_code=76,
                output="quota exhausted",
                session_id=session_id,
                retry_after_seconds=60,
                failure_class=FailureClass.QUOTA_EXHAUSTED,
                reason_code="worker_quota_exhausted",
            )

    class RateLimitedMonitor:
        async def poll(self, run_id: str, model: str | None = None):
            return WorkerRunStatus(
                output="HTTP 429 Too Many Requests",
                complete=True,
                exit_code=75,
                failure_class="rate_limited",
                reason_code="worker_rate_limited",
                retry_after_seconds=0,
            )

        async def cancel(self, run_id: str) -> None:
            return None

    async def scenario() -> None:
        line = _line(
            "line-fix-poll-quota",
            assigned_model="kimi",
            status=LineStatus.RUNNING,
            attempts=1,
        )
        line.current_run = "rate-limited-run"
        launcher = QuotaOnRelaunch()
        collaborators, machine, *_ = _bundle(
            tmp_path,
            [line],
            launcher=launcher,
            monitor=RateLimitedMonitor(),
            selector=NoReassign(),
        )
        machine._sleep = lambda _seconds: asyncio.sleep(0)
        dispatch = Dispatch(
            id="d-fix-poll-quota",
            goal="park fix quota after retry",
            user_id="u",
            created_at="now",
            lines=[line],
            state=DispatchState.MONITORING,
        )
        _set_stage_state(
            collaborators,
            dispatch,
            line,
            StageGateState(
                task_id=line.id,
                stage=Stage.FIXING,
                author_provider="kimi",
                developer_provider="kimi",
                return_stage=Stage.PRIMARY_REVIEW.value,
                contributors=("kimi",),
            ),
        )

        result = await machine.run(dispatch)
        state = StageGateState.from_dict(
            collaborators.gates.line_snapshot(result, line)["machine"]
        )
        assert launcher.models == ["kimi"]
        assert result.line(line.id).status is LineStatus.BLOCKED
        assert result.line(line.id).assigned_model == "kimi"
        assert result.line(line.id).failure_reason == "author_quota_exhausted"
        assert state.stage is Stage.BLOCKED
        assert state.author_provider == "kimi"
        events = result.gates["worker_recovery"][line.id]["events"]
        assert [(item["failure_class"], item["action"]) for item in events] == [
            ("rate_limited", "retry_same"),
            ("quota_exhausted", "park"),
        ]

    asyncio.run(scenario())


def test_stale_worker_failure_clears_only_its_matching_reserved_run(
    tmp_path: Path,
) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line
    from src.tools.auto_dispatch.gates import drive_epoch

    async def scenario() -> None:
        line = _line("line-stale-reservation", assigned_model="kimi", status=LineStatus.ASSIGNED)
        line.current_run = "new-run"
        _collaborators, machine, *_ = _bundle(tmp_path, [line])
        dispatch = Dispatch(
            id="d-stale-reservation",
            goal="preserve a newer run",
            user_id="u",
            created_at="now",
            lines=[line],
            state=DispatchState.LAUNCHING,
        )
        failure = WorkerLaunchFailure(
            run_id="old-run",
            exit_code=79,
            output="worker crashed",
            session_id=None,
            retry_after_seconds=None,
            failure_class=FailureClass.CRASH,
            reason_code="worker_crashed",
        )
        applied = await machine._recover_worker_failure(
            dispatch,
            line,
            failure,
            False,
            attempt=line.attempts + 1,
            author="old-author",
            epoch=drive_epoch(dispatch),
        )
        assert applied is False
        assert line.current_run == "new-run"
        assert line.assigned_model == "kimi"

    asyncio.run(scenario())


def test_uninspectable_poll_callable_uses_run_id_only(tmp_path: Path) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    class OpaquePoll:
        @property
        def __signature__(self):
            raise ValueError("signature unavailable")

        async def __call__(self, run_id: str):
            return RunStatus(output=run_id, complete=True)

    class Monitor:
        poll = OpaquePoll()

        async def cancel(self, run_id: str) -> None:
            return None

    _collaborators, machine, *_ = _bundle(
        tmp_path, [_line("line-opaque")], monitor=Monitor()
    )
    assert machine._poll_accepts_model is False


def test_poll_signature_cache_respects_keyword_callability():
    from src.tools.auto_dispatch.machine import _callable_accepts_keyword

    def positional_only(run_id, model, /):
        return run_id, model

    def keyword_only(run_id, *, model):
        return run_id, model

    def accepts_kwargs(run_id, **kwargs):
        return run_id, kwargs

    assert _callable_accepts_keyword(positional_only, "model") is False
    assert _callable_accepts_keyword(keyword_only, "model") is True
    assert _callable_accepts_keyword(accepts_kwargs, "model") is True
