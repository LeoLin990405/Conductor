"""N29: the Worker's own handoff request and the context estimate, end to end.

The real machine drives the real ``ScriptLauncher`` and the real
``scripts/dispatch_launch.sh`` against a fake CLI (``NATIVE_CLI_<X>_BIN``).
The fake reads ``$CONDUCTOR_HANDOFF_REQUEST_FILE``, follows a per-turn plan and
records its argv, prompt and the Conductor context env. No model, no network.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import subprocess
import threading
from pathlib import Path

import pytest

from src.llms.native_cli.base import NativeCliUsage
from src.llms.native_cli.session_manager import (
    ACTIVE_ROTATION,
    ROTATION_EXHAUSTED_REASON,
    WORKER_REQUESTED,
    WorkerSessionManager,
)
from src.llms.native_cli.sessions import NativeCliSessionStore
from src.tools.auto_dispatch import context_budget
from src.tools.auto_dispatch.dev import AlwaysPassReviewer
from src.tools.auto_dispatch import machine as machine_module
from src.tools.auto_dispatch.execution import LaunchResult, LogMonitor, ScriptLauncher
from src.tools.auto_dispatch.gates import GateSettings
from src.tools.auto_dispatch.machine import DispatchMachine, apply_human_reassign
from src.tools.auto_dispatch.types import Dispatch, DispatchState, LineStatus
from tests.unit.tools.auto_dispatch.test_machine_gates import _asleep, _bundle, _line
from tests.unit.tools.auto_dispatch.test_n22_cli_owned_sessions import BlockOnce, _handoff
from tests.unit.tools.auto_dispatch.test_parallel_lines_n31 import BASE as N31_BASE
from tests.unit.tools.auto_dispatch.test_parallel_lines_n31 import HEAD as N31_HEAD
from tests.unit.tools.auto_dispatch.test_parallel_lines_n31 import _assigned as n31_assigned
from tests.unit.tools.auto_dispatch.test_parallel_lines_n31 import _drive as n31_drive
from tests.unit.tools.auto_dispatch.test_parallel_lines_n31 import _setup as n31_setup

LAUNCH_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "dispatch_launch.sh"
SECRET = "ghp_" + "Z9y8X7w6V5" * 4
CONTEXT_ENV = (
    context_budget.ENV_BUDGET_FILE,
    context_budget.ENV_HANDOFF_REQUEST_FILE,
    context_budget.ENV_HISTORY_CMD,
    context_budget.ENV_HISTORY_MANIFEST,
    context_budget.ENV_RUN_LOG,
)

# One fake for cursor, grok and kimi. FAKE_PLAN is a JSON list of per-turn
# actions; the last one repeats.
_FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys

provider = os.environ["FAKE_PROVIDER"]
state = pathlib.Path(os.environ["FAKE_STATE"])
state.mkdir(parents=True, exist_ok=True)
trace = state / "trace.jsonl"
args = sys.argv[1:]


def value(flag):
    return args[args.index(flag) + 1] if flag in args else None


if provider == "cursor":
    prompt, resume = args[-1], value("--resume")
elif provider == "grok":
    prompt, resume = pathlib.Path(value("--prompt-file")).read_text(), value("--resume")
else:
    prompt, resume = value("-p") or "", value("--session")
turn = (len(trace.read_text().splitlines()) if trace.exists() else 0) + 1
plan = json.loads(os.environ.get("FAKE_PLAN", '["commit"]'))
action = plan[min(turn, len(plan)) - 1]
names = os.environ["FAKE_ENV_NAMES"].split(",")
dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout
row = {"turn": turn, "action": action, "argv": args, "prompt": prompt, "resume": resume,
       "env": {name: os.environ.get(name) for name in names}, "dirty": bool(dirty.strip())}
with trace.open("a") as stream:
    stream.write(json.dumps(row) + "\n")

request = pathlib.Path(os.environ.get("CONDUCTOR_HANDOFF_REQUEST_FILE") or state / "none")
report = {"done": [f"turn {turn} parsed the registry"], "in_progress": ["the adapter table"],
          "next": ["run the dispatch tests"], "pitfalls": ["the fixture is slow"],
          "open_questions": []}


def commit():
    pathlib.Path(f"work-{turn}.txt").write_text(f"turn {turn}\n")
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "-c", "user.name=fake", "-c", "user.email=fake@example.invalid",
                    "commit", "-qm", f"fake {provider} turn {turn}"], check=True)


kind, _, arg = action.partition(":")
if kind in ("request", "secret", "badjson", "oversize"):
    # A clean stop: work left uncommitted in the worktree for the next session.
    pathlib.Path(f"wip-{turn}.txt").write_text("uncommitted\n")
    if kind == "secret":
        report["done"] = [f"called the API with {os.environ['FAKE_SECRET']}"]
    body = {"reason": "context_budget", "report": report}
    if kind == "badjson":
        text = '{"reason": "context_budget", "report": '
    elif kind == "oversize":
        text = json.dumps(dict(body, pad="x" * 9000))
    else:
        text = json.dumps(body)
    request.write_text(text)
elif kind == "symlink":
    target = state / "target.json"
    target.write_text(json.dumps({"reason": "other", "report": report}))
    os.symlink(target, request)
    commit()
else:
    commit()
    if kind == "big":
        chunk = "x" * 1023
        for _ in range(int(arg) // 1024 + 1):
            print(json.dumps({"type": "text", "data": chunk}))

sid = resume or f"{provider}-cli-{turn}"
tokens = int(os.environ.get("FAKE_USAGE", "0"))
if provider == "cursor":
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}))
    print(json.dumps({"type": "result", "subtype": "success", "result": f"done {turn}",
                      "session_id": sid, "usage": {"input_tokens": tokens}}))
elif provider == "grok":
    print(json.dumps({"type": "text", "data": f"done {turn}"}))
    print(json.dumps({"type": "end", "stopReason": "end_turn", "sessionId": sid}))
else:
    print(json.dumps({"role": "assistant", "content": [{"type": "text", "text": f"done {turn}"}]}))
    print(json.dumps({"role": "meta", "type": "session.resume_hint", "session_id": sid}))
sys.stdout.flush()
sys.exit(0)
'''


def _git(repo: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


class Run:
    """One dispatch of one line through the real launcher and launch script."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        provider: str,
        plan: list[str],
        *,
        reviewer: object | None = None,
        usage: int = 0,
    ) -> None:
        fake = tmp_path / "bin" / provider
        fake.parent.mkdir()
        fake.write_text(_FAKE_CLI, encoding="utf-8")
        fake.chmod(0o755)
        self.state = tmp_path / "cli-state"
        for name in ("CURSOR", "GROK", "KIMI"):
            monkeypatch.setenv(f"NATIVE_CLI_{name}_BIN", str(fake))
        monkeypatch.setenv("FAKE_PROVIDER", provider)
        monkeypatch.setenv("FAKE_STATE", str(self.state))
        monkeypatch.setenv("FAKE_PLAN", json.dumps(plan))
        monkeypatch.setenv("FAKE_USAGE", str(usage))
        monkeypatch.setenv("FAKE_SECRET", SECRET)
        monkeypatch.setenv("FAKE_ENV_NAMES", ",".join(CONTEXT_ENV))
        monkeypatch.delenv(context_budget.SOFT_RATIO_ENV, raising=False)
        for name in ("CURSOR", "GROK", "KIMI"):
            monkeypatch.delenv(f"{context_budget.WINDOW_ENV_PREFIX}{name}", raising=False)
        monkeypatch.setenv(
            "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR", str(tmp_path.parent / f"{tmp_path.name}-logs")
        )
        monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT", "30")
        monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_STALL", "30")
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "config", "user.email", "test@example.invalid")
        _git(repo, "config", "core.hooksPath", "/dev/null")
        (repo / "base.txt").write_text("base\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "base")
        self.runs = tmp_path.parent / f"{tmp_path.name}-runs"
        self.runs.mkdir()
        self.dispatch_id = f"d-n29-{provider}"
        self.launcher = ScriptLauncher(
            self.runs,
            script=LAUNCH_SCRIPT,
            cwd=repo,
            worktrees_dir=tmp_path.parent / f"{tmp_path.name}-worktrees",
            dispatch_id=self.dispatch_id,
        )
        self.manager = self.launcher.recovery_session_manager()
        self.line = _line("line-x", assigned_model=provider, status=LineStatus.ASSIGNED)
        self.line.branch = f"feature/auto-dispatch-{self.dispatch_id}/line-line-x"
        self.line.base_commit = _git(repo, "rev-parse", "HEAD")
        self.key = f"dispatch:{self.dispatch_id}:line:line-x:author:{provider}"
        self.collaborators, self.machine, self.commands, _trees = _bundle(
            tmp_path,
            [self.line],
            launcher=self.launcher,
            monitor=LogMonitor(self.runs),
            reviewer=reviewer or AlwaysPassReviewer(),
            settings=GateSettings(
                runs_dir=self.runs, repo_root=repo, g5_command="pytest -q"
            ),
        )
        self.collaborators.gates.handoff_facts = _handoff
        self.dispatch = Dispatch(
            id=self.dispatch_id,
            goal="ship the line",
            user_id="u",
            created_at="now",
            lines=[self.line],
            state=DispatchState.LAUNCHING,
        )

    def seed_resumable(self, session: str) -> None:
        """A successful earlier turn: a resumable session the CLI reported."""
        self.collaborators.gates._ensure_root(self.dispatch)
        self.dispatch.gates["worker_recovery"] = {
            "line-x": {
                "attempts": {},
                "session_id": session,
                "session_mode": "resume",
                "session_author": self.line.assigned_model,
                "session_origin": "cli",
            }
        }

    def go(self) -> Dispatch:
        return asyncio.run(self.machine.run(self.dispatch))

    def trace(self) -> list[dict]:
        path = self.state / "trace.jsonl"
        if not path.exists():
            return []
        return [json.loads(row) for row in path.read_text(encoding="utf-8").splitlines()]

    def line_state(self) -> dict:
        return json.loads(self.manager.path.read_text(encoding="utf-8"))["lines"][self.key]

    def archived(self) -> list[dict]:
        return list(self.line_state().get("archived") or [])

    def manifest(self) -> list[context_budget.ManifestEntry]:
        return context_budget.read_manifest(context_budget.manifest_path(self.runs, self.key))


_BUDGET = re.compile(
    r"session_generation: (\d+)\s+lineage_first: (\S+)\s+lineage_previous: (\S+)"
    r"\s+lineage_current: (\S+)"
)


def _budget(prompt: str) -> tuple[int, str, str, str]:
    match = _BUDGET.search(prompt)
    assert match is not None, "no context budget block in the prompt"
    return int(match[1]), match[2], match[3], match[4]


def _fix_round(result: Dispatch) -> int:
    return int(result.gates["lines"]["line-x"].get("fix_round") or 0)


# -- (3) a valid worker-requested handoff ---------------------------------


def test_a_valid_request_rotates_and_relaunches_the_same_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Run(tmp_path, monkeypatch, "cursor", ["request", "commit"])
    attempts = run.line.attempts

    result = run.go()

    first, second = run.trace()
    line = result.line("line-x")
    assert line is not None and line.status is LineStatus.PASSED
    # The handoff run never reached review or G2: one review, of the second run.
    assert len(run.collaborators.reviewer.reviewed) == 1
    assert _fix_round(result) == 0
    assert line.attempts == attempts
    [event] = run.archived()
    assert event["reason"] == WORKER_REQUESTED
    assert event["self_report"] == "ok"
    assert event["session_id"] == "cursor-cli-1"
    # The relaunch is a fresh session that carries the note with the report.
    assert second["resume"] is None
    prompt = second["prompt"]
    assert "## worker_reported" in prompt
    assert "turn 1 parsed the registry" in prompt
    assert "do line-x" in prompt  # the original task
    # The uncommitted work the Worker left stayed for the next session.
    assert second["dirty"] is True
    gen1, first_id1, _prev1, current1 = _budget(first["prompt"])
    gen2, first_id2, prev2, _current2 = _budget(prompt)
    assert (gen1, gen2) == (1, 2)
    assert first_id2 == first_id1
    assert prev2 == current1
    request = Path(first["env"][context_budget.ENV_HANDOFF_REQUEST_FILE])
    assert not request.exists() and not request.is_symlink()
    assert [entry.handoff_request for entry in run.manifest()] == ["ok", "none"]


# -- (4) a request that is broken -------------------------------------------


@pytest.mark.parametrize("kind", ["badjson", "oversize"])
def test_a_broken_request_takes_the_normal_exit_path_and_never_rotates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Planner ruling: only a valid request rotates; an invalid one is a normal exit."""
    run = Run(tmp_path, monkeypatch, "cursor", [kind, "commit"])

    result = run.go()

    first, nudge = run.trace()
    assert result.line("line-x").status is LineStatus.PASSED
    assert len(run.collaborators.reviewer.reviewed) == 1
    # No rotation: the second CLI turn is the submission check's commit nudge
    # on the same session, not a relaunch with a handover note.
    assert run.archived() == []
    assert nudge["resume"] == "cursor-cli-1"
    assert "deterministic handoff notes at:" not in nudge["prompt"]
    assert [entry.handoff_request for entry in run.manifest()] == ["invalid"]
    request = Path(first["env"][context_budget.ENV_HANDOFF_REQUEST_FILE])
    assert not request.exists()


def test_a_secret_in_the_request_report_is_redacted_in_the_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Run(tmp_path, monkeypatch, "cursor", ["secret", "commit"])

    run.go()

    _first, second = run.trace()
    [event] = run.archived()
    assert event["reason"] == WORKER_REQUESTED
    assert "## worker_reported" in second["prompt"]
    assert SECRET not in second["prompt"]
    for path in run.runs.rglob("*"):
        if path.is_file() and path.suffix in (".md", ".json", ".txt"):
            assert SECRET not in path.read_text(encoding="utf-8", errors="replace")


def test_a_symlinked_request_is_refused_and_the_run_is_a_normal_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Run(tmp_path, monkeypatch, "cursor", ["symlink"])

    result = run.go()

    [only] = run.trace()
    assert result.line("line-x").status is LineStatus.PASSED
    assert len(run.collaborators.reviewer.reviewed) == 1
    assert run.archived() == []
    request = Path(only["env"][context_budget.ENV_HANDOFF_REQUEST_FILE])
    assert not request.is_symlink()
    assert [entry.handoff_request for entry in run.manifest()] == ["refused_symlink"]


def test_a_request_is_ignored_when_the_author_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Run(tmp_path, monkeypatch, "cursor", ["request", "commit"])
    real = run.launcher.launch_worker
    rotations: list[str] = []
    real_rotate = run.manager.rotate

    def rotate(key: str, **kwargs: object):
        rotations.append(str(kwargs.get("reason")))
        return real_rotate(key, **kwargs)

    run.manager.rotate = rotate  # type: ignore[method-assign]
    seen: list[str] = []

    async def launch_worker(subject, model: str, skills, **kwargs):
        result = await real(subject, model, skills, **kwargs)
        seen.append(model)
        if len(seen) == 1:
            # A reassign landed while the run was out: the request is void.
            assert result.handoff_request == "ok"
            subject.assigned_model = "grok"
        return result

    run.launcher.launch_worker = launch_worker  # type: ignore[method-assign]

    run.go()

    assert WORKER_REQUESTED not in rotations


# -- (5) a Worker that always asks is parked --------------------------------


def test_a_worker_that_always_requests_a_handoff_is_parked_on_the_fourth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Run(tmp_path, monkeypatch, "cursor", ["request"])

    result = run.go()

    calls = run.trace()
    assert len(calls) == 4
    line = result.line("line-x")
    assert line is not None and line.status is LineStatus.BLOCKED
    assert line.failure_reason == ROTATION_EXHAUSTED_REASON
    assert result.state is DispatchState.REVIEW
    assert run.collaborators.reviewer.reviewed == []
    assert _fix_round(result) == 0
    assert [event["reason"] for event in run.archived()] == [WORKER_REQUESTED] * 3
    assert run.line_state().get("parked_reason") == ROTATION_EXHAUSTED_REASON
    assert [_budget(call["prompt"])[0] for call in calls] == [1, 2, 3, 4]


# -- (6) the proxy estimate drives the hard threshold ------------------------


def test_a_large_grok_log_rotates_before_the_next_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 0.8 x 0.95 x 500,000 tokens x 4 bytes = 1,520,000 bytes of log.
    run = Run(tmp_path, monkeypatch, "grok", ["big:1600000", "commit"], reviewer=BlockOnce())

    result = run.go()

    first, second = run.trace()
    assert result.line("line-x").status is LineStatus.PASSED
    assert first["resume"] is None
    [event] = run.archived()
    assert event["reason"] == ACTIVE_ROTATION
    assert event["session_id"] == "grok-cli-1"
    # The rotated fix round runs fresh, with the note.
    assert second["resume"] is None
    assert "deterministic handoff notes at:" in second["prompt"]
    assert _budget(second["prompt"])[0] == 2
    assert [entry.estimate_source for entry in run.manifest()] == ["log_bytes", "log_bytes"]


def test_kimi_rotates_before_its_third_resume_on_the_launch_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Run(tmp_path, monkeypatch, "kimi", ["commit"], reviewer=BlockOnce())
    # Two earlier turns of the same session: this run is its 3rd launch.
    for _ in range(2):
        run.manager.record_turn(run.key, session_id="kimi-cli-0")
    run.seed_resumable("kimi-cli-0")

    run.go()

    first, second = run.trace()
    assert first["resume"] == "kimi-cli-0"
    [event] = run.archived()
    assert event["reason"] == ACTIVE_ROTATION
    assert second["resume"] is None
    assert [entry.estimate_source for entry in run.manifest()][0] == "launches"


# -- (7) reported cumulative tokens no longer rotate an estimate provider -----


def test_reported_usage_above_the_window_does_not_rotate_grok_with_a_small_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Run(tmp_path, monkeypatch, "grok", ["commit"], reviewer=BlockOnce())
    # An earlier turn reported a cumulative total past the window, with a small log.
    run.manager.record_turn(
        run.key,
        session_id="grok-cli-0",
        usage=NativeCliUsage(input_tokens=900_000),
        estimate=context_budget.estimate("grok", session_turns=1, current_run_log_bytes=4096),
    )
    run.seed_resumable("grok-cli-0")

    run.go()

    first, second = run.trace()
    assert run.archived() == []
    assert first["resume"] == second["resume"] == "grok-cli-0"
    state = run.line_state()
    # The reported value is still accumulated (for N30); it decides nothing.
    assert int(state["input_tokens"]) >= 900_000
    assert state["context_estimate"]["source"] == "log_bytes"


@pytest.mark.parametrize("provider", ["glm", "cursor"])
def test_glm_and_cursor_keep_the_legacy_reported_rotation(
    tmp_path: Path, provider: str
) -> None:
    root = tmp_path / "recovery"
    manager = WorkerSessionManager(
        path=root / "worker-sessions.json",
        store=NativeCliSessionStore(root / "sessions.json"),
        worktree=tmp_path,
        handoff_dir=root / "handoffs",
    )
    key = f"dispatch:d:line:l:author:{provider}"
    small = context_budget.estimate(provider, session_turns=1, current_run_log_bytes=4096)
    assert small.drives_rotation is False
    manager.record_turn(key, session_id="s-1", usage=NativeCliUsage(input_tokens=10), estimate=small)
    assert manager.proactive_rotation_due(key) is False
    if provider == "glm":
        # Past 80% of glm's window by the reported total: the legacy path rotates.
        manager.record_turn(
            key, session_id="s-1", usage=NativeCliUsage(input_tokens=900_000), estimate=small
        )
        assert manager.proactive_rotation_due(key) is True


# -- (8) R7d fix: the handoff is decided before LaunchDone (N31 interleaving) --


class _BlockingRotate(WorkerSessionManager):
    """A real manager whose rotate waits (bounded) for the test to let it go."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.started = asyncio.Event()
        self.release = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None

    def rotate(self, key: str, **kwargs: object):
        assert self.loop is not None
        self.loop.call_soon_threadsafe(self.started.set)
        self.release.wait(timeout=10)
        return super().rotate(key, **kwargs)


class _HandoffLauncher:
    """Line ``a`` asks for a handoff on its first run; every other run commits."""

    def __init__(self, manager: WorkerSessionManager) -> None:
        self.manager = manager
        self.calls: list[str] = []
        self.lines: dict[str, object] = {}

    def recovery_session_manager(self, **_kw: object) -> WorkerSessionManager:
        return self.manager

    async def launch(self, line, model, skills):
        raise AssertionError("the recovery machine uses launch_worker")

    async def launch_worker(self, line, model, skills, *, session_id, session_mode, handoff_path, **_kw):
        self.calls.append(line.id)
        self.lines[line.id] = line
        run_id = line.current_run
        if line.id == "a" and self.calls.count("a") == 1:
            return LaunchResult(
                run_id, None, "asked", start_head=N31_BASE, head=N31_BASE,
                handoff_request=context_budget.HANDOFF_OK,
            )
        if line.id == "b" and isinstance(self.manager, _BlockingRotate):
            # Line b finishes only once line a is inside its rotation, so the
            # drive has a running line to poll during that window.
            await asyncio.wait_for(self.manager.started.wait(), timeout=10)
        return LaunchResult(run_id, session_id, "committed", start_head=N31_BASE, head=N31_HEAD)


@pytest.mark.asyncio
async def test_a_poll_and_a_cancel_during_the_rotation_never_complete_or_relaunch_the_line(
    tmp_path: Path,
) -> None:
    """N31: while line a rotates, line b's task runs and the drive polls.

    Line a is never polled, reported complete, sent to G2 or review, and a
    change made to it in that window (a cancel) is not overwritten.
    """
    root = tmp_path / "recovery"
    manager = _BlockingRotate(
        path=root / "worker-sessions.json",
        store=NativeCliSessionStore(root / "sessions.json"),
        worktree=tmp_path,
        handoff_dir=root / "handoffs",
    )
    manager.loop = asyncio.get_running_loop()
    launcher = _HandoffLauncher(manager)
    store, machine, gates, service, row = await n31_setup(
        tmp_path,
        [n31_assigned("a", "kimi", ("src/a/",)), n31_assigned("b", "glm", ("src/b/",))],
        launcher=launcher,
    )
    monitor = machine._c.monitor
    real_poll = monitor.poll
    polled: list[str] = []
    during: list[str] = []

    async def poll(run_id: str):
        polled.append(run_id)
        if manager.started.is_set() and not manager.release.is_set():
            # The rotation is in flight: the drive is polling right now.
            during.append(run_id)
            line_a = launcher.lines["a"]
            line_a.status = LineStatus.BLOCKED
            line_a.failure_reason = "operator_cancelled"
            manager.release.set()
        return await real_poll(run_id)

    monitor.poll = poll  # type: ignore[method-assign]

    await n31_drive(service, row.id)

    after = await store.get(row.id)
    line_a = after.line("a")
    run_a = line_a.current_run
    assert during, "the drive never polled while line a was rotating"
    assert launcher.calls.count("a") == 1  # never relaunched
    assert line_a.status is LineStatus.BLOCKED
    assert line_a.failure_reason == "operator_cancelled"
    assert run_a and run_a not in polled  # never polled, so never complete
    assert all(line_id != "a" for line_id, _model in machine._c.reviewer.reviewed)
    snap = after.gates["lines"]["a"]
    g2 = (snap.get("gates") or {}).get("G2") or {}
    assert g2.get("result") in (None, "pending") and not g2.get("commands")
    assert after.line("b").status is LineStatus.PASSED


# -- (9) R7d fix: a crash in the middle of the rotation -----------------------


class Crash(Exception):
    """Stands in for the process dying at one point of the handoff."""


def _restart(run: Run) -> Dispatch:
    """A fresh launcher, manager and machine on the saved row and the same files."""
    saved = asyncio.run(run.collaborators.store.get(run.dispatch_id))
    assert saved is not None
    launcher = ScriptLauncher(
        run.runs,
        script=LAUNCH_SCRIPT,
        cwd=run.launcher._cwd,
        worktrees_dir=run.launcher._worktrees_dir,
        dispatch_id=run.dispatch_id,
    )
    machine = DispatchMachine(dataclasses.replace(run.collaborators, launcher=launcher), sleep=_asleep)
    run.launcher = launcher
    run.manager = launcher.recovery_session_manager()
    return asyncio.run(machine.run(saved))


@pytest.mark.parametrize("point", ["before_marker", "after_rotate", "after_confirm"])
def test_a_crash_mid_handoff_finishes_the_handoff_once_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    run = Run(tmp_path, monkeypatch, "cursor", ["request", "commit"])
    if point == "before_marker":
        # After the run, before the decision is saved: no marker, the request
        # file is all there is.
        def lineage(key: str):
            raise Crash(point)

        run.manager.lineage = lineage  # type: ignore[method-assign]
    elif point == "after_rotate":
        real_rotate = run.manager.rotate

        def rotate(key: str, **kwargs: object):
            real_rotate(key, **kwargs)
            raise Crash(point)

        run.manager.rotate = rotate  # type: ignore[method-assign]
    else:
        def refuse(path: Path) -> None:
            raise Crash(point)

        monkeypatch.setattr(machine_module, "delete_handoff_request", refuse)

    with pytest.raises(Crash):
        run.go()
    first_only = run.trace()
    assert len(first_only) == 1  # the crash came before any relaunch
    request = Path(first_only[0]["env"][context_budget.ENV_HANDOFF_REQUEST_FILE])
    assert request.exists()  # deleted only after the confirming save
    crashed = asyncio.run(run.collaborators.store.get(run.dispatch_id))
    assert crashed.line("line-x").status is LineStatus.ASSIGNED
    assert run.collaborators.reviewer.reviewed == []
    monkeypatch.undo()
    # monkeypatch.undo() dropped the harness env too; put the fake CLI back.
    run = _rearm(run, tmp_path, monkeypatch)

    result = _restart(run)

    first, second = run.trace()
    line = result.line("line-x")
    assert line is not None and line.status is LineStatus.PASSED
    # One review, of the relaunch; the handoff run itself never reached it.
    assert len(run.collaborators.reviewer.reviewed) == 1
    events = [event for event in run.archived() if event["reason"] == WORKER_REQUESTED]
    assert len(events) == 1  # rotated once, never twice
    assert run.manager.lineage(run.key).generation == 2
    # The relaunch used the rotated session: fresh, with the note and gen 2.
    assert second["resume"] is None
    assert "deterministic handoff notes at:" in second["prompt"]
    gen1, _f1, _p1, current1 = _budget(first["prompt"])
    gen2, _f2, prev2, _c2 = _budget(second["prompt"])
    assert (gen1, gen2) == (1, 2) and prev2 == current1
    assert not request.exists() and not request.is_symlink()
    entry = result.gates["worker_recovery"]["line-x"]
    assert "handoff_pending" not in entry


def _rearm(run: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Run:
    """The same env the harness set, for the restarted process."""
    fake = tmp_path / "bin" / "cursor"
    for name in ("CURSOR", "GROK", "KIMI"):
        monkeypatch.setenv(f"NATIVE_CLI_{name}_BIN", str(fake))
    monkeypatch.setenv("FAKE_PROVIDER", "cursor")
    monkeypatch.setenv("FAKE_STATE", str(run.state))
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["request", "commit"]))
    monkeypatch.setenv("FAKE_USAGE", "0")
    monkeypatch.setenv("FAKE_SECRET", SECRET)
    monkeypatch.setenv("FAKE_ENV_NAMES", ",".join(CONTEXT_ENV))
    monkeypatch.setenv(
        "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR", str(tmp_path.parent / f"{tmp_path.name}-logs")
    )
    monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT", "30")
    monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_STALL", "30")
    return run


# -- (10) R7d fix: an operator retry unparks a rotation-exhausted session -----


def test_an_operator_retry_of_a_rotation_parked_line_unparks_its_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = ["request"] * 4 + ["commit"]
    run = Run(tmp_path, monkeypatch, "cursor", plan)
    parked = run.go()
    line = parked.line("line-x")
    assert line.failure_reason == ROTATION_EXHAUSTED_REASON
    assert run.manager.is_parked(run.key)

    # The operator retries the line (a human reassign; the fake serves grok too).
    refused = asyncio.run(
        apply_human_reassign(parked, line, "grok", None, run.collaborators.gates)
    )
    assert refused is None
    asyncio.run(run.collaborators.store.save(parked))
    result = _restart(run)

    assert result.line("line-x").status is LineStatus.PASSED
    assert len(run.trace()) == 5
    assert not run.manager.is_parked(run.key)
    state = run.line_state()
    assert "parked_reason" not in state and int(state.get("rotations") or 0) == 0
    assert "rotation_parked_key" not in result.gates["worker_recovery"]["line-x"]
