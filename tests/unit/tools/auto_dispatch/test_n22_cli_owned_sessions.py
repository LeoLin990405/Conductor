"""N22: Kimi and Grok resume only the session their own CLI created.

Kimi 2.5 answers ``Session "<id>" not found`` and Grok ``No saved copy of
session <id> was found remotely`` for an id they never created. So dispatch
never hands either CLI a caller-owned id: a fresh run takes no id, the id the
CLI reports in its own output is persisted on the line, and the fix round after
a rejected review resumes exactly that id. With no id captured the fix round
runs fresh, and nothing crashes.

The CLIs here are stateful fakes driven through the real launch script. They
remember the sessions they created and reject every other id the way the real
CLIs do, so a caller-owned id anywhere would fail the line.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from src.llms.native_cli.kimi import KimiCliAdapter
from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch import execution
from src.tools.auto_dispatch.execution import (
    SELF_REPORT_PROVIDERS,
    LaunchError,
    LogMonitor,
    ScriptLauncher,
    supports_explicit_session_id,
    supports_session_resume,
)
from src.tools.auto_dispatch.gates import GateSettings
from src.tools.auto_dispatch.types import Line, Verdict
from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

LAUNCH_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "dispatch_launch.sh"

# One fake for both CLIs. It keeps the sessions it created in a state file and
# speaks each CLI's session grammar and output shape.
_FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys

provider = os.environ["FAKE_PROVIDER"]
state_dir = pathlib.Path(os.environ["FAKE_STATE"])
state_dir.mkdir(parents=True, exist_ok=True)
sessions_file = state_dir / "sessions.json"
trace = state_dir / "trace.jsonl"
sessions = json.loads(sessions_file.read_text()) if sessions_file.exists() else []
args = sys.argv[1:]


def value(flag):
    return args[args.index(flag) + 1] if flag in args else None


row = {"argv": args, "cwd": os.getcwd()}
if provider == "kimi":
    resume = value("--session")
    row["resume"] = resume
    prompt = value("-p") or ""
    if "--continue" in args:
        row["refused"] = "continue"
    elif resume is not None and resume not in sessions:
        row["refused"] = resume
else:
    resume = value("--resume")
    row["resume"] = resume
    caller_id = value("--session-id")
    prompt = pathlib.Path(value("--prompt-file")).read_text() if value("--prompt-file") else ""
    if "--continue" in args:
        row["refused"] = "continue"
    elif caller_id is not None:
        row["refused"] = caller_id
    elif resume is not None and resume not in sessions:
        row["refused"] = resume
row["self_report"] = prompt.startswith(os.environ.get("FAKE_REPORT_PREFIX", "\x00"))
with trace.open("a") as stream:
    stream.write(json.dumps(row) + "\n")

if "refused" in row:
    if provider == "kimi":
        print(f'Session "{row["refused"]}" not found', flush=True)
    else:
        print(f"No saved copy of session {row['refused']} was found remotely", flush=True)
    sys.exit(1)

if resume is None:
    sid = f"{provider}-cli-{len(sessions) + 1}"
    sessions.append(sid)
    if os.environ.get("FAKE_FORGET") != "1":
        sessions_file.write_text(json.dumps(sessions))
else:
    sid = resume

turn = len(trace.read_text().splitlines())
pathlib.Path(f"work-{turn}.txt").write_text(f"turn {turn}\n")
subprocess.run(["git", "add", f"work-{turn}.txt"], check=True)
subprocess.run(["git", "commit", "-qm", f"fake {provider} turn {turn}"], check=True)

report = os.environ.get("FAKE_REPORT_ID", "1") == "1"
if provider == "kimi":
    print(json.dumps({"role": "assistant", "content": [{"type": "text", "text": f"done {turn}"}]}))
    if report:
        print(json.dumps({"role": "meta", "type": "session.resume_hint", "session_id": sid}))
else:
    print(json.dumps({"type": "text", "data": f"done {turn}"}))
    end = {"type": "end", "stopReason": "end_turn"}
    if report:
        end["sessionId"] = sid
    print(json.dumps(end))
if os.environ.get("FAKE_DECOY") == "1":
    # stdout and stderr share the log: an MCP server, a child process or a
    # subagent can print its own JSON with a session id after the CLI's.
    print(json.dumps({"role": "tool", "session_id": "sub_agent_x"}))
    print(json.dumps({"type": "tool_call", "sessionId": "mcp_child_y"}), file=sys.stderr)
sys.stdout.flush()
sys.exit(0)
'''


def _git(repo: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, dispatch_id: str
) -> tuple[ScriptLauncher, Path, Path, str]:
    """A real launch script, a fake stateful CLI, and a one-commit repository."""
    fake = tmp_path / "bin" / provider
    fake.parent.mkdir()
    fake.write_text(_FAKE_CLI, encoding="utf-8")
    fake.chmod(0o755)
    state = tmp_path / "cli-state"
    monkeypatch.setenv("FAKE_PROVIDER", provider)
    monkeypatch.setenv("FAKE_STATE", str(state))
    monkeypatch.setenv("NATIVE_CLI_KIMI_BIN", str(fake))
    monkeypatch.setenv("NATIVE_CLI_GROK_BIN", str(fake))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "fake")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "fake@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "fake")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "fake@example.invalid")
    monkeypatch.setenv(
        "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR", str(tmp_path.parent / f"{tmp_path.name}-logs")
    )
    monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT", "20")
    monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_STALL", "20")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "core.hooksPath", "/dev/null")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    runs = tmp_path.parent / f"{tmp_path.name}-runs"
    runs.mkdir()
    launcher = ScriptLauncher(
        runs,
        script=LAUNCH_SCRIPT,
        cwd=repo,
        worktrees_dir=tmp_path.parent / f"{tmp_path.name}-worktrees",
        dispatch_id=dispatch_id,
    )
    return launcher, state, runs, _git(repo, "rev-parse", "HEAD")


def _trace(state: Path) -> list[dict]:
    path = state / "trace.jsonl"
    if not path.exists():
        return []
    return [json.loads(row) for row in path.read_text(encoding="utf-8").splitlines()]


class BlockOnce:
    """The first review asks for a fix; every later review passes."""

    def __init__(self) -> None:
        self.reviewed: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.reviewed.append((line_id, model))
        if len(self.reviewed) == 1:
            return Verdict.blocked(["add a regression test"])
        return Verdict.ok(f"reviewed by {model}")


def _handoff(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "branch": "feature/line-x",
        "commit": "abc123",
        "files": ("a.py",),
        "test_command": "pytest",
        "results": "pass",
        "known_limitations": "none",
    }


def _run_fix_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    *,
    report_id: bool = True,
    decoy: bool = False,
    forget: bool = False,
) -> tuple[Dispatch, list[dict], BlockOnce, list[dict]]:
    """Fresh dispatch, a rejected review, a fix round; returns what the CLI saw."""
    dispatch_id = f"d-n22-{provider}"
    launcher, state, runs, base = _setup(tmp_path, monkeypatch, provider, dispatch_id)
    monkeypatch.setenv("FAKE_REPORT_ID", "1" if report_id else "0")
    monkeypatch.setenv("FAKE_DECOY", "1" if decoy else "0")
    monkeypatch.setenv("FAKE_FORGET", "1" if forget else "0")
    line = _line("line-x", assigned_model=provider, status=LineStatus.ASSIGNED)
    line.branch = f"feature/auto-dispatch-{dispatch_id}/line-line-x"
    line.base_commit = base
    reviewer = BlockOnce()
    persisted: list[dict] = []

    real_launch = launcher.launch_worker

    async def launch_worker(subject: Line, model: str, skills, **kwargs):
        # The machine saves the line's session state just before it launches.
        # What the store holds then proves the captured id was persisted.
        saved = await collaborators.store.get(dispatch_id)
        assert saved is not None
        entry = saved.gates.get("worker_recovery", {}).get("line-x", {})
        persisted.append(
            {
                "session_id": entry.get("session_id"),
                "session_mode": entry.get("session_mode"),
                "session_origin": entry.get("session_origin"),
                "requested": (kwargs.get("session_id"), kwargs.get("session_mode")),
            }
        )
        return await real_launch(subject, model, skills, **kwargs)

    launcher.launch_worker = launch_worker  # type: ignore[method-assign]
    collaborators, machine, *_ = _bundle(
        tmp_path,
        [line],
        launcher=launcher,
        monitor=LogMonitor(runs),
        reviewer=reviewer,
        settings=GateSettings(runs_dir=runs, repo_root=tmp_path / "repo", g5_command="pytest -q"),
    )
    collaborators.gates.handoff_facts = _handoff
    dispatch = Dispatch(
        id=dispatch_id,
        goal="ship the line",
        user_id="u",
        created_at="now",
        lines=[line],
        state=DispatchState.LAUNCHING,
    )
    result = asyncio.run(machine.run(dispatch))
    return result, _trace(state), reviewer, persisted


@pytest.mark.parametrize("decoy", [False, True], ids=["plain", "decoy-json-lines"])
@pytest.mark.parametrize(
    ("provider", "flag"), [("kimi", "--session"), ("grok", "--resume")]
)
def test_fix_round_resumes_the_id_the_cli_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, flag: str, decoy: bool
) -> None:
    # With decoy lines, other JSON carrying a session id follows the CLI's own
    # event in the shared log; only Kimi's resume_hint / Grok's end event count.
    result, calls, reviewer, persisted = _run_fix_round(
        tmp_path, monkeypatch, provider, decoy=decoy
    )

    created = f"{provider}-cli-1"
    # Nothing was ever refused: no caller-owned id and no --continue reached the CLI.
    assert [call.get("refused") for call in calls] == [None, None]
    assert all("--continue" not in call["argv"] for call in calls)
    assert all("--session-id" not in call["argv"] for call in calls)
    # The fresh run took no id; the fix round resumed the id the CLI created.
    assert calls[0]["resume"] is None and flag not in calls[0]["argv"]
    assert calls[1]["resume"] == created
    assert persisted[0]["requested"] == (None, None)
    # Between the runs the captured id is what the line persisted.
    assert persisted[1] == {
        "session_id": created,
        "session_mode": "resume",
        "session_origin": "cli",
        "requested": (created, "resume"),
    }
    assert len(reviewer.reviewed) == 2
    line = result.line("line-x")
    assert line is not None and line.status is LineStatus.PASSED
    assert result.gates["worker_recovery"]["line-x"].get("events", []) == []
    assert result.gates["worker_recovery"]["line-x"]["session_id"] == created


@pytest.mark.parametrize("provider", ["kimi", "grok"])
def test_no_captured_id_means_the_fix_round_runs_fresh_without_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    result, calls, reviewer, persisted = _run_fix_round(
        tmp_path, monkeypatch, provider, report_id=False
    )

    # The fake rejects every id it did not create; no id was ever sent.
    assert [call.get("refused") for call in calls] == [None, None]
    assert [call["resume"] for call in calls] == [None, None]
    assert [entry["requested"] for entry in persisted] == [(None, None), (None, None)]
    assert len(reviewer.reviewed) == 2
    line = result.line("line-x")
    assert line is not None and line.status is LineStatus.PASSED
    # No crash and no fresh-session fallback was needed.
    assert result.gates["worker_recovery"]["line-x"].get("events", []) == []


@pytest.mark.parametrize("provider", ["kimi", "grok"])
def test_dispatch_never_requests_a_fresh_caller_owned_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    assert not supports_explicit_session_id(provider)
    assert supports_session_resume(provider)
    launcher, state, _runs, _base = _setup(tmp_path, monkeypatch, provider, "d-refuse")
    line = Line(id="line-r", name="r", task="t", current_run="d-refuse:line-r:0:x")
    with pytest.raises(LaunchError):
        asyncio.run(
            launcher.launch_worker(
                line, provider, [], session_id="caller-owned", session_mode="fresh", handoff_path=None
            )
        )
    assert _trace(state) == []


def test_a_persisted_caller_owned_id_without_cli_origin_is_not_resumed(tmp_path: Path) -> None:
    """An entry written before N22 holds a caller-owned id in resume mode."""
    collaborators, machine, *_ = _bundle(tmp_path, [])
    line = _line("line-old", assigned_model="kimi", status=LineStatus.ASSIGNED)
    dispatch = Dispatch(id="d-old", goal="g", user_id="u", created_at="now", lines=[line])
    dispatch.gates = {
        "worker_recovery": {
            "line-old": {
                "attempts": {},
                "session_id": "0123456789abcdef0123456789abcdef",
                "session_mode": "resume",
                "session_author": "kimi",
            }
        }
    }
    assert machine._launch_session(dispatch, line) == (None, None)
    entry = dispatch.gates["worker_recovery"]["line-old"]
    assert entry["session_id"] is None and entry["session_mode"] is None


@pytest.mark.parametrize("provider", ["kimi", "grok"])
def test_a_legacy_continue_sentinel_runs_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    # The machine: a stored "continue" is not a session, even marked as the CLI's.
    collaborators, machine, *_ = _bundle(tmp_path, [])
    line = _line("line-legacy", assigned_model=provider, status=LineStatus.ASSIGNED)
    dispatch = Dispatch(id="d-legacy", goal="g", user_id="u", created_at="now", lines=[line])
    dispatch.gates = {
        "worker_recovery": {
            "line-legacy": {
                "attempts": {},
                "session_id": "continue",
                "session_mode": "resume",
                "session_author": provider,
                "session_origin": "cli",
            }
        }
    }
    assert machine._launch_session(dispatch, line) == (None, None)
    assert dispatch.gates["worker_recovery"]["line-legacy"]["session_id"] is None

    # The launch script: even handed "continue" to resume, the CLI runs fresh.
    launcher, state, _runs, _base = _setup(tmp_path, monkeypatch, provider, "d-legacy")
    launched = Line(id="line-l", name="l", task="t", current_run="d-legacy:line-l:0:x")
    result = asyncio.run(
        launcher.launch_worker(
            launched, provider, [], session_id="continue", session_mode="resume", handoff_path=None
        )
    )
    [call] = _trace(state)
    assert call["resume"] is None and call.get("refused") is None
    assert "--continue" not in call["argv"] and "continue" not in call["argv"]
    assert result.session_id == f"{provider}-cli-1"


def test_the_kimi_adapter_treats_a_legacy_continue_as_fresh() -> None:
    adapter = KimiCliAdapter(binary="kimi")
    argv = adapter.build_invocation("again", session_id="continue").argv
    assert "--continue" not in argv
    assert "--session" not in argv
    assert "continue" not in argv
    resumed = adapter.build_invocation("again", session_id="kimi-cli-1").argv
    assert resumed[resumed.index("--session") + 1] == "kimi-cli-1"
    assert "--continue" not in resumed


@pytest.mark.parametrize("provider", ["kimi", "grok"])
def test_no_self_report_process_starts_for_kimi_or_grok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    assert provider not in SELF_REPORT_PROVIDERS
    assert not execution.supports_self_report(provider)
    launcher, state, runs, _base = _setup(tmp_path, monkeypatch, provider, "d-report")
    line = Line(id="line-s", name="s", task="t", current_run="d-report:line-s:0:x")
    reply = asyncio.run(
        launcher.request_self_report(line, provider, session_id=f"{provider}-cli-1", timeout=5)
    )
    assert reply is None
    assert _trace(state) == []
    assert not list(runs.glob("self-report-*"))


@pytest.mark.parametrize(
    ("provider", "reason"),
    [("kimi", "kimi_session_not_found"), ("grok", "grok_session_not_saved")],
)
def test_a_dead_resume_id_is_dropped_and_the_run_goes_fresh_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, reason: str
) -> None:
    """The CLI reported an id, then no longer knows it: never retry that id."""
    result, calls, reviewer, persisted = _run_fix_round(
        tmp_path, monkeypatch, provider, forget=True
    )

    created = f"{provider}-cli-1"
    assert [call["resume"] for call in calls] == [None, created, None]
    assert [call.get("refused") for call in calls] == [None, created, None]
    events = result.gates["worker_recovery"]["line-x"]["events"]
    assert [(event["failure_class"], event["action"], event["reason"]) for event in events] == [
        ("crash", "retry_fresh_once", reason)
    ]
    # The retry ran with no id at all, not with the dead one.
    assert persisted[2]["requested"] == (None, None)
    line = result.line("line-x")
    assert line is not None and line.status is LineStatus.PASSED


@pytest.mark.parametrize(
    ("provider", "reason"),
    [("kimi", "kimi_session_not_found"), ("grok", "grok_session_not_saved")],
)
def test_a_lost_session_is_fresh_once_even_when_the_crash_budget_is_spent(
    tmp_path: Path, provider: str, reason: str
) -> None:
    from src.llms.native_cli.failure import FailureClass
    from src.tools.auto_dispatch.execution import WorkerLaunchFailure
    from src.tools.auto_dispatch.machine import drive_epoch

    collaborators, machine, *_ = _bundle(tmp_path, [])
    line = _line("line-dead", assigned_model=provider, status=LineStatus.ASSIGNED)
    line.current_run = "d-dead:line-dead:1:x"
    line.attempts = 1
    dispatch = Dispatch(id="d-dead", goal="g", user_id="u", created_at="now", lines=[line])
    # Prepare the Stage-Gate root first; a later prepare would wipe the seed.
    collaborators.gates.prepare(dispatch)
    dispatch.gates.update({
        "worker_recovery": {
            "line-dead": {
                "attempts": {"crash": 1},
                "session_id": f"{provider}-cli-1",
                "session_mode": "resume",
                "session_author": provider,
                "session_origin": "cli",
            }
        }
    })
    failure = WorkerLaunchFailure(
        run_id=line.current_run,
        exit_code=79,
        output="",
        session_id=f"{provider}-cli-1",
        retry_after_seconds=None,
        failure_class=FailureClass.CRASH,
        reason_code=reason,
    )

    async def scenario() -> bool:
        await collaborators.store.create(dispatch)
        try:
            return await machine._submit(
                machine._recover_worker_failure,
                dispatch,
                line,
                failure,
                True,
                attempt=1,
                author=provider,
                epoch=drive_epoch(dispatch),
            )
        finally:
            await machine.aclose()

    assert asyncio.run(scenario()) is True
    entry = dispatch.gates["worker_recovery"]["line-dead"]
    assert entry["last"]["action"] == "retry_fresh_once"
    assert entry["session_id"] is None and "session_origin" not in entry
    assert entry["attempts"] == {"crash": 1, "session_lost": 1}


def test_capture_takes_only_the_clis_own_event() -> None:
    from src.llms.native_cli.grok import grok_stream_session_id
    from src.llms.native_cli.kimi import kimi_session_id
    from src.tools.auto_dispatch.execution import _session_metadata

    hint = {"role": "meta", "type": "session.resume_hint", "session_id": "session_0f3a"}
    assert kimi_session_id([hint, {"role": "tool", "session_id": "sub_agent_x"}]) == "session_0f3a"
    assert kimi_session_id([{"session_id": "x"}, {"type": "session.resume_hint", "session_id": "y"}]) is None
    kimi_log = "\n".join(
        json.dumps(row)
        for row in (
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            hint,
            {"role": "tool", "session_id": "sub_agent_x"},
        )
    )
    assert _session_metadata("kimi", kimi_log, None)[0] == "session_0f3a"

    end = {"type": "end", "stopReason": "end_turn", "sessionId": "9f2c4e"}
    grok_log = "\n".join(
        json.dumps(row)
        for row in ({"type": "text", "data": "hi"}, end, {"type": "tool_call", "sessionId": "mcp_child_y"})
    )
    assert grok_stream_session_id(grok_log) == "9f2c4e"
    assert _session_metadata("grok", grok_log, None)[0] == "9f2c4e"


def test_an_id_that_is_not_a_plain_token_is_no_session() -> None:
    from src.llms.native_cli.base import resumable_session_id
    from src.llms.native_cli.grok import grok_stream_session_id
    from src.llms.native_cli.kimi import KimiCliAdapter, kimi_session_id

    hint = {"role": "meta", "type": "session.resume_hint", "session_id": "-rf bad id"}
    assert kimi_session_id([hint]) is None
    end = {"type": "end", "stopReason": "end_turn", "sessionId": "--resume"}
    assert grok_stream_session_id(json.dumps(end)) is None
    argv = KimiCliAdapter(binary="kimi").build_invocation("p", session_id="-rf bad id").argv
    assert "--session" not in argv and "-rf bad id" not in argv
    for bad in ("-rf bad id", "-x", "--session", "a b", "a;b", "a$b", "x" * 129, ""):
        assert resumable_session_id(bad) is None, bad
    for good in ("session_8c1e2f6a-1b2c-4d5e-8f90-123456789abc", "9f2c4e", "kimi-cli-1", "a" * 128):
        assert resumable_session_id(good) == good


def test_only_kimis_quoted_session_error_counts_as_a_lost_session() -> None:
    from src.llms.native_cli.failure import classify_failure

    hint = (
        '{"role":"meta","type":"session.resume_hint","session_id":"session_1",'
        '"content":"To resume this session: kimi -r session_1"}\n'
    )
    lost = hint + 'error: failed to run prompt: Session "session_2" not found.\n'
    other = hint + "bash: rg: command not found\n"
    assert classify_failure("kimi", lost, exit_code=1).reason_code == "kimi_session_not_found"
    assert classify_failure("kimi", other, exit_code=1).reason_code != "kimi_session_not_found"
