"""Session handoff: the old session's self-report before a proactive rotation.

The dispatcher runs the real launch script against a fake CLI. Only minimax
self-reports: kimi and grok cannot (N22), so no report turn starts for them.
Nothing here calls a model or opens a network socket.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from src.llms.native_cli.base import NativeCliUsage
from src.llms.native_cli.session_manager import (
    ACTIVE_ROTATION,
    CONTEXT_EXHAUSTED,
    WorkerSessionManager,
)
from src.llms.native_cli.sessions import NativeCliSessionStore
from src.llms.native_cli.worker_report import WORKER_REPORT_PROMPT, worker_report_from_json
from src.tools.auto_dispatch.execution import LaunchResult, LogMonitor, ScriptLauncher
from src.tools.auto_dispatch.self_report import (
    SelfReportReply,
    lesson_candidates,
    read_self_report,
)
from src.tools.auto_dispatch.types import Dispatch, DispatchState, LineStatus

LAUNCH_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "dispatch_launch.sh"
SYSTEM_HEADER = "# 交接摘要（Conductor 自动生成）"
SECRET = "ghp_" + "a1B2c3D4e5" * 4


def _report(**overrides: object) -> str:
    payload: dict[str, object] = {
        "done": ["parsed the registry"],
        "in_progress": ["checking each adapter class"],
        "next": ["run the dispatch tests"],
        "pitfalls": ["the fake CLI needs --session"],
        "open_questions": ["is doubao still listed"],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


# -- reading the answer ---------------------------------------------------


def test_a_matching_answer_becomes_the_report() -> None:
    outcome = read_self_report(SelfReportReply("answered", _report()))
    assert outcome.status == "ok"
    assert outcome.report is not None
    assert outcome.report.pitfalls == ("the fake CLI needs --session",)
    assert outcome.byte_count == len(_report().encode("utf-8"))


def test_prose_around_the_object_is_invalid() -> None:
    # read_self_report sees the final text: prose around the object is not one report.
    output = "session resumed\nthinking...\n" + _report() + "\n[exit 0]\n"
    outcome = read_self_report(SelfReportReply("answered", output))
    assert outcome.status == "invalid"
    assert outcome.report is None


def test_one_json_fence_is_the_report() -> None:
    fenced = "```json\n" + _report() + "\n```"
    outcome = read_self_report(SelfReportReply("answered", fenced))
    assert outcome.status == "ok"
    assert outcome.report is not None
    assert outcome.report.done == ("parsed the registry",)


@pytest.mark.parametrize(
    ("reply", "status"),
    [
        (None, "unsupported"),
        (SelfReportReply("timeout"), "timeout"),
        (SelfReportReply("failed"), "failed"),
        (SelfReportReply("unsupported"), "unsupported"),
        (SelfReportReply("answered", "I could not finish the report."), "invalid"),
        (SelfReportReply("answered", "Report: " + _report()), "invalid"),
        (SelfReportReply("answered", _report(notes=["extra key"])), "invalid"),
        (SelfReportReply("answered", _report(done=["x"] * 11)), "invalid"),
    ],
    ids=["no-reply", "timeout", "failed", "unsupported", "prose", "prefixed", "extra-key", "too-many"],
)
def test_a_reply_that_is_not_one_report_falls_back(reply, status: str) -> None:
    outcome = read_self_report(reply)
    assert outcome.report is None
    assert outcome.status == status


def test_an_oversize_answer_is_discarded_before_it_is_parsed() -> None:
    big = _report(done=["x" * 200] * 10, next=["y" * 200] * 10, pitfalls=["z" * 200] * 10)
    outcome = read_self_report(SelfReportReply("answered", big))
    assert outcome.status == "oversize"
    assert outcome.report is None
    assert outcome.byte_count > 4096


def test_a_secret_in_the_answer_is_redacted_before_the_schema_check() -> None:
    outcome = read_self_report(
        SelfReportReply("answered", _report(done=[f"called the API with {SECRET}"]))
    )
    assert outcome.status == "ok"
    assert outcome.report is not None
    assert SECRET not in json.dumps(outcome.report.done)
    assert outcome.report.done == ("[REDACTED]",)


def test_lesson_candidates_are_agent_inferred_and_need_review() -> None:
    report = worker_report_from_json(_report())
    assert report is not None
    [candidate] = lesson_candidates(report, worker="kimi", dispatch_id="d", line_id="l")
    assert candidate["class"] == "Lesson"
    assert candidate["provenance_type"] == "agent_inferred"
    assert candidate["review"] == "required"
    assert candidate["text"] == "the fake CLI needs --session"


# -- the note and the rotation event --------------------------------------


def _manager(tmp_path: Path) -> WorkerSessionManager:
    root = tmp_path / "recovery"
    return WorkerSessionManager(
        path=root / "worker-sessions.json",
        store=NativeCliSessionStore(root / "sessions.json"),
        worktree=tmp_path,
        handoff_dir=root / "handoffs",
    )


def _line_state(manager: WorkerSessionManager, key: str) -> dict:
    return json.loads(manager.path.read_text(encoding="utf-8"))["lines"][key]


KEY = "dispatch:d:line:l:author:kimi"


def test_a_proactive_rotation_note_has_both_sections_and_records_the_event(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.record_turn(KEY, session_id="old-session", usage=NativeCliUsage(input_tokens=220000))
    assert manager.proactive_rotation_due(KEY)
    report = worker_report_from_json(_report())
    result = manager.rotate(
        KEY,
        reason=ACTIVE_ROTATION,
        worker_report=report,
        self_report="ok",
        self_report_bytes=report.byte_count,
    )
    note = result.handoff_path.read_text(encoding="utf-8")
    assert note.startswith(SYSTEM_HEADER)
    assert note.index("## 下一步") < note.index("## worker_reported")
    assert "- the fake CLI needs --session" in note
    [event] = _line_state(manager, KEY)["archived"]
    assert event["session_id"] == "old-session"
    # The rot- placeholder stays on the line; the archive event must not store it.
    assert event.get("new_session_id") in (None, "")
    assert event["new_session_pending"] is True
    assert "rot-" not in json.dumps(event)
    assert event["self_report"] == "ok"
    assert event["self_report_bytes"] == report.byte_count
    assert "the fake CLI" not in manager.path.read_text(encoding="utf-8")


def test_a_passive_rotation_note_is_the_system_section_only(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.record_turn(KEY, session_id="old-session", usage=NativeCliUsage(input_tokens=220000))
    manager.record_failure(KEY, CONTEXT_EXHAUSTED)
    assert manager.should_rotate(KEY)
    assert not manager.proactive_rotation_due(KEY)
    result = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    note = result.handoff_path.read_text(encoding="utf-8")
    assert note.startswith(SYSTEM_HEADER)
    assert "worker_reported" not in note
    [event] = _line_state(manager, KEY)["archived"]
    assert event["self_report"] == "not_asked"
    assert event["self_report_bytes"] == 0


def test_a_rotation_cut_short_drops_the_report_and_keeps_the_system_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    manager.record_turn(KEY, session_id="old-session", usage=NativeCliUsage(input_tokens=220000))
    real_write = WorkerSessionManager._write_handoff
    calls = {"n": 0}

    def disk_full(self, *args, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(WorkerSessionManager, "_write_handoff", disk_full)
    report = worker_report_from_json(_report())
    with pytest.raises(OSError):
        manager.rotate(
            KEY,
            reason=ACTIVE_ROTATION,
            worker_report=report,
            self_report="ok",
            self_report_bytes=report.byte_count,
        )
    reopened = _manager(tmp_path)
    note = reopened.handoff_path(KEY).read_text(encoding="utf-8")
    assert note.startswith(SYSTEM_HEADER)
    assert "worker_reported" not in note
    [event] = _line_state(reopened, KEY)["archived"]
    assert event["self_report"] == "dropped"
    assert event["self_report_bytes"] == 0


def test_an_unknown_status_is_stored_as_unknown(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.record_turn(KEY, session_id="old-session")
    manager.rotate(KEY, reason=ACTIVE_ROTATION, self_report=f"ok {SECRET}")
    [event] = _line_state(manager, KEY)["archived"]
    assert event["self_report"] == "unknown"
    assert SECRET not in manager.path.read_text(encoding="utf-8")


# -- the dispatcher, through the real launch script ------------------------


def _fake_kimi(tmp_path: Path, *, report: str, work: list[tuple[str, int]]) -> Path:
    """A kimi double: the report prompt gets ``report``, every other turn the next ``work``."""
    root = tmp_path / "fake"
    root.mkdir()
    (root / "report.txt").write_text(report, encoding="utf-8")
    (root / "work.json").write_text(json.dumps(work), encoding="utf-8")
    binary = root / "kimi"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys, time\n"
        "root = pathlib.Path(os.environ['FAKE_KIMI_ROOT'])\n"
        "trace = root / 'trace.jsonl'\n"
        "args = sys.argv[1:]\n"
        "sid = args[args.index('--session') + 1] if '--session' in args else None\n"
        "if '-p' in args:\n"
        "    prompt = args[args.index('-p') + 1]\n"
        "elif '--prompt' in args:\n"
        "    prompt = args[args.index('--prompt') + 1]\n"
        "else:\n"
        "    prompt = sys.stdin.read()\n"
        f"asking = prompt.startswith({WORKER_REPORT_PROMPT[:40]!r})\n"
        "rows = trace.read_text().splitlines() if trace.exists() else []\n"
        "with trace.open('a') as stream:\n"
        "    stream.write(json.dumps({'sid': sid, 'asking': asking, 'prompt': prompt}) + '\\n')\n"
        "leak = os.environ.get('FAKE_LEAK', '')\n"
        "if leak:\n"
        "    print(leak, flush=True)\n"
        "if asking:\n"
        "    time.sleep(float(os.environ.get('FAKE_REPORT_SLEEP', '0')))\n"
        "    transport = os.environ.get('FAKE_TRANSPORT_FILE', '')\n"
        "    if transport:\n"
        "        sys.stdout.write(pathlib.Path(transport).read_text())\n"
        "        sys.stdout.flush()\n"
        "    else:\n"
        "        text = (root / 'report.txt').read_text()\n"
        "        if os.environ.get('FAKE_PROVIDER') == 'minimax':\n"
        "            event = {'schemaVersion': 1, 'type': 'exec.result', 'status': 'succeeded',\n"
        "                     'sessionId': sid, 'events': [], 'output': text}\n"
        "        else:\n"
        "            event = {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}\n"
        "        print(json.dumps(event), flush=True)\n"
        "    sys.exit(int(os.environ.get('FAKE_EXIT_CODE', '0') or '0'))\n"
        "done = sum(1 for row in rows if not json.loads(row)['asking'])\n"
        "message, code = json.loads((root / 'work.json').read_text())[done]\n"
        "print(message, flush=True)\n"
        "if os.environ.get('FAKE_PROVIDER') == 'kimi' and sid is None:\n"
        "    # Kimi names the session it created at the end of its stream.\n"
        "    hint = {'role': 'meta', 'type': 'session.resume_hint', 'session_id': f'kimi-cli-{done + 1}'}\n"
        "    print(json.dumps(hint), flush=True)\n"
        "if code == 0:\n"
        "    import subprocess\n"
        "    pathlib.Path('worker-result.txt').write_text(message + '\\n')\n"
        "    subprocess.run(['git', 'add', 'worker-result.txt'], check=False)\n"
        "    subprocess.run(['git', '-c', 'user.email=dispatch@example.com', '-c', 'user.name=dispatch', 'commit', '-qm', message or 'work'], check=False)\n"
        "sys.exit(code)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return root


def _git_repo(path: Path) -> Path:
    """A one-commit repo the line worktree can be added to. Config stays in the repo."""
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    if (path / ".git").is_dir():
        return path

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=path, check=True, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.email", "dispatch@example.com")
    git("config", "user.name", "dispatch")
    (path / "README").write_text("base\n", encoding="utf-8")
    git("add", "README")
    git("commit", "-m", "init")
    return path


def _launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    report: str,
    work: list[tuple[str, int]],
    dispatch_id: str = "",
    provider: str = "minimax",
) -> tuple[ScriptLauncher, Path, Path]:
    root = _fake_kimi(tmp_path, report=report, work=work)
    monkeypatch.setenv("NATIVE_CLI_KIMI_BIN", str(root / "kimi"))
    monkeypatch.setenv("NATIVE_CLI_MINIMAX_BIN", str(root / "kimi"))
    monkeypatch.setenv("FAKE_PROVIDER", provider)
    monkeypatch.setenv("FAKE_KIMI_ROOT", str(root))
    monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR", str(tmp_path / "script-logs"))
    monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT", "10")
    monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_STALL", "10")
    if dispatch_id:
        # Task 6 refuses Git mutations when any parent of the worktree root is a
        # symlink. pytest's tmp is under /tmp, which is a symlink on this Mac.
        import tempfile

        private = Path(tempfile.mkdtemp(prefix="handoff-wt-", dir="/private/tmp"))
        repo = _git_repo(private / "repo")
        state = private / "state"
        runs = state / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        launcher = ScriptLauncher(
            runs,
            script=LAUNCH_SCRIPT,
            cwd=repo,
            dispatch_id=dispatch_id,
            worktrees_dir=state / "worktrees",
        )
    else:
        runs = tmp_path / "runs"
        runs.mkdir(exist_ok=True)
        launcher = ScriptLauncher(runs, script=LAUNCH_SCRIPT, cwd=tmp_path)
    return launcher, root, runs


def _trace(root: Path) -> list[dict]:
    path = root / "trace.jsonl"
    if not path.exists():
        return []
    return [json.loads(row) for row in path.read_text(encoding="utf-8").splitlines()]


def _seed_resumable(
    collaborators, dispatch: Dispatch, line_id: str, session: str, provider: str = "kimi"
) -> None:
    """The state a successful earlier turn leaves: a resumable session the CLI reported.

    The gate runner builds its root first, as it does in a real run; the
    recovery entry is added to that root, where the machine keeps it.
    """
    collaborators.gates._ensure_root(dispatch)
    dispatch.gates["worker_recovery"] = {
        line_id: {
            "attempts": {},
            "session_id": session,
            "session_mode": "resume",
            "session_author": provider,
            "session_origin": "cli",
        }
    }


def test_a_proactive_rotation_asks_once_and_the_next_prompt_carries_the_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    launcher, root, runs = _launcher(
        tmp_path,
        monkeypatch,
        report=_report(),
        work=[("completed", 0)],
        dispatch_id="d-proactive",
    )
    line = _line("line-full", assigned_model="minimax", status=LineStatus.ASSIGNED)
    dispatch = Dispatch(
        id="d-proactive",
        goal="rotate before the window fills",
        user_id="u",
        created_at="now",
        lines=[line],
        state=DispatchState.LAUNCHING,
    )
    manager = launcher.recovery_session_manager()
    key = f"dispatch:{dispatch.id}:line:{line.id}:author:minimax"
    # Past 80% of minimax's 1,000,000-token window: due for a proactive rotation.
    manager.record_turn(key, session_id="old-kimi-session", usage=NativeCliUsage(input_tokens=900000))
    collaborators, machine, *_ = _bundle(tmp_path, [line], launcher=launcher, monitor=LogMonitor(runs))
    _seed_resumable(collaborators, dispatch, line.id, "old-kimi-session", "minimax")
    lessons: list[list[dict[str, object]]] = []
    collaborators.lesson_sink = lessons.append

    result = asyncio.run(machine.run(dispatch))

    calls = _trace(root)
    assert [call["asking"] for call in calls] == [True, False]
    assert calls[0]["sid"] == "old-kimi-session"
    # No provider takes a caller-owned fresh id: the rotated turn starts a new
    # session with no --session, and the handoff note carries the context.
    assert calls[1]["sid"] is None
    prompt = calls[1]["prompt"]
    assert "deterministic handoff notes at:" in prompt
    assert SYSTEM_HEADER in prompt
    assert "## worker_reported" in prompt
    assert "- the fake CLI needs --session" in prompt
    assert prompt.index(SYSTEM_HEADER) < prompt.index("## worker_reported")
    [event] = _line_state(manager, key)["archived"]
    assert event["session_id"] == "old-kimi-session"
    # The rot- placeholder minted at rotation is not an id the archive event may keep.
    assert "rot-" not in json.dumps(event)
    assert event["self_report"] == "ok"
    assert event["self_report_bytes"] > 0
    assert [item["text"] for item in lessons[0]] == ["the fake CLI needs --session"]
    assert result.line(line.id).status is LineStatus.PASSED
    assert not list((runs).glob("*--self-report.log"))


def test_a_bad_answer_falls_back_to_the_system_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    launcher, root, runs = _launcher(
        tmp_path,
        monkeypatch,
        report=f"Here is my report: {SECRET}",
        work=[("completed", 0)],
        dispatch_id="d-bad-answer",
    )
    line = _line("line-bad", assigned_model="minimax", status=LineStatus.ASSIGNED)
    dispatch = Dispatch(
        id="d-bad-answer",
        goal="fall back",
        user_id="u",
        created_at="now",
        lines=[line],
        state=DispatchState.LAUNCHING,
    )
    manager = launcher.recovery_session_manager()
    key = f"dispatch:{dispatch.id}:line:{line.id}:author:minimax"
    # Past 80% of minimax's 1,000,000-token window: due for a proactive rotation.
    manager.record_turn(key, session_id="old-kimi-session", usage=NativeCliUsage(input_tokens=900000))
    collaborators, machine, *_ = _bundle(tmp_path, [line], launcher=launcher, monitor=LogMonitor(runs))
    _seed_resumable(collaborators, dispatch, line.id, "old-kimi-session", "minimax")
    lessons: list[object] = []
    collaborators.lesson_sink = lessons.append

    asyncio.run(machine.run(dispatch))

    calls = _trace(root)
    assert [call["asking"] for call in calls] == [True, False]
    prompt = calls[1]["prompt"]
    assert SYSTEM_HEADER in prompt
    assert "worker_reported" not in prompt
    assert SECRET not in prompt
    [event] = _line_state(manager, key)["archived"]
    assert event["self_report"] == "invalid"
    assert lessons == []


def test_a_kimi_proactive_rotation_starts_no_self_report_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N22: kimi cannot self-report, so the rotation writes the system note only."""
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    launcher, root, runs = _launcher(
        tmp_path,
        monkeypatch,
        report=_report(),
        work=[("completed", 0)],
        dispatch_id="d-kimi-unsupported",
        provider="kimi",
    )
    line = _line("line-kimi", assigned_model="kimi", status=LineStatus.ASSIGNED)
    dispatch = Dispatch(
        id="d-kimi-unsupported",
        goal="rotate before the window fills",
        user_id="u",
        created_at="now",
        lines=[line],
        state=DispatchState.LAUNCHING,
    )
    manager = launcher.recovery_session_manager()
    key = f"dispatch:{dispatch.id}:line:{line.id}:author:kimi"
    manager.record_turn(key, session_id="kimi-cli-1", usage=NativeCliUsage(input_tokens=220000))
    collaborators, machine, *_ = _bundle(tmp_path, [line], launcher=launcher, monitor=LogMonitor(runs))
    _seed_resumable(collaborators, dispatch, line.id, "kimi-cli-1", "kimi")

    result = asyncio.run(machine.run(dispatch))

    calls = _trace(root)
    assert [call["asking"] for call in calls] == [False]
    assert calls[0]["sid"] is None
    prompt = calls[0]["prompt"]
    assert "deterministic handoff notes at:" in prompt
    assert SYSTEM_HEADER in prompt
    assert "worker_reported" not in prompt
    [event] = _line_state(manager, key)["archived"]
    assert event["session_id"] == "kimi-cli-1"
    assert event["self_report"] == read_self_report(None).status == "unsupported"
    assert result.line(line.id).status is LineStatus.PASSED
    assert not list(runs.glob("*--self-report.log"))


def test_a_passive_rotation_does_not_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    launcher, root, runs = _launcher(
        tmp_path,
        monkeypatch,
        report=_report(),
        work=[("context_length_exceeded: context length maximum", 78), ("completed", 0)],
        dispatch_id="d-passive",
        provider="kimi",
    )
    line = _line("line-passive", assigned_model="kimi", status=LineStatus.ASSIGNED)
    dispatch = Dispatch(
        id="d-passive",
        goal="rotate after the context ran out",
        user_id="u",
        created_at="now",
        lines=[line],
        state=DispatchState.LAUNCHING,
    )
    collaborators, machine, *_ = _bundle(tmp_path, [line], launcher=launcher, monitor=LogMonitor(runs))

    async def no_wait(_seconds: float) -> None:
        return None

    machine._sleep = no_wait
    result = asyncio.run(machine.run(dispatch))

    calls = _trace(root)
    assert [call["asking"] for call in calls] == [False, False]
    # Kimi takes no caller-owned id (N22): both turns start without --session,
    # and the rotated turn gets the handoff note instead.
    assert [call["sid"] for call in calls] == [None, None]
    prompt = calls[1]["prompt"]
    assert SYSTEM_HEADER in prompt
    assert "worker_reported" not in prompt
    manager = launcher.recovery_session_manager()
    key = f"dispatch:{dispatch.id}:line:{line.id}:author:kimi"
    [event] = _line_state(manager, key)["archived"]
    # The session rotated away from is the one Kimi created and reported.
    assert event["session_id"] == "kimi-cli-1"
    assert event["reason"] == CONTEXT_EXHAUSTED
    assert event["self_report"] == "not_asked"
    assert result.line(line.id).status is LineStatus.PASSED


def test_the_self_report_turn_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _line

    launcher, root, runs = _launcher(tmp_path, monkeypatch, report=_report(), work=[])
    monkeypatch.setenv("FAKE_REPORT_SLEEP", "8")
    line = _line("line-slow", assigned_model="minimax", status=LineStatus.ASSIGNED)

    async def ask() -> SelfReportReply:
        return await launcher.request_self_report(
            line, "minimax", session_id="old-kimi-session", timeout=1.0
        )

    started = time.monotonic()
    reply = asyncio.run(ask())
    elapsed = time.monotonic() - started
    assert reply.status == "timeout"
    assert reply.output == ""
    assert elapsed < 6
    assert not list(runs.glob("*--self-report.log"))
    assert _trace(root)[0]["sid"] == "old-kimi-session"


def test_a_provider_that_cannot_resume_is_not_asked(tmp_path: Path) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _line

    runs = tmp_path / "runs"
    runs.mkdir()
    launcher = ScriptLauncher(runs, script=LAUNCH_SCRIPT, cwd=tmp_path)
    line = _line("line-step", assigned_model="stepfun", status=LineStatus.ASSIGNED)
    reply = asyncio.run(
        launcher.request_self_report(line, "stepfun", session_id="s", timeout=1.0)
    )
    # Providers outside the self-report set are not asked. None is that answer.
    assert reply is None


# -- parser final text, leftover secrets, serial wait, provider ids --------


TOKEN = "fak3-" + "H4ndOff" * 4
DECOY = _report(done=["intermediate decoy"], pitfalls=["do not keep this"])


def _kimi_transport(final: str, intermediate: str = DECOY) -> str:
    rows = [
        {"role": "assistant", "content": [{"type": "text", "text": intermediate}]},
        {"role": "assistant", "content": [{"type": "text", "text": final}]},
    ]
    return "".join(json.dumps(row) + "\n" for row in rows)


def _minimax_transport(final: str, intermediate: str = DECOY) -> str:
    payload = {
        "schemaVersion": 1,
        "type": "exec.result",
        "status": "succeeded",
        "sessionId": "minimax-session",
        "events": [{"type": "assistant", "text": intermediate}],
        "output": final,
    }
    return json.dumps(payload) + "\n"


def _transport_stdout(provider: str, final: str, intermediate: str = DECOY) -> str:
    if provider == "minimax":
        return _minimax_transport(final, intermediate)
    return _kimi_transport(final, intermediate)


def _bind_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    stdout: str,
    *,
    leak: str = "",
    exit_code: int = 0,
    sleep: str = "0",
) -> tuple[ScriptLauncher, Path, Path, Path]:
    launcher, root, runs = _launcher(tmp_path, monkeypatch, report=_report(), work=[])
    transport = root / "transport.txt"
    transport.write_text(stdout, encoding="utf-8")
    monkeypatch.setenv("FAKE_TRANSPORT_FILE", str(transport))
    monkeypatch.setenv("FAKE_LEAK", leak)
    monkeypatch.setenv("FAKE_EXIT_CODE", str(exit_code))
    monkeypatch.setenv("FAKE_REPORT_SLEEP", sleep)
    monkeypatch.setenv("NATIVE_CLI_MINIMAX_BIN", str(root / "kimi"))
    monkeypatch.setenv("NATIVE_CLI_KIMI_BIN", str(root / "kimi"))
    return launcher, root, runs, tmp_path / "script-logs"


def _assigned(line_id: str, model: str = "kimi"):
    from tests.unit.tools.auto_dispatch.test_machine_gates import _line

    return _line(line_id, assigned_model=model, status=LineStatus.ASSIGNED)


def _files_with(root: Path, needle: str) -> list[Path]:
    if not root.exists():
        return []
    blob = needle.encode("utf-8")
    found: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if blob in data:
            found.append(path)
    return found


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    state = tmp_path / "xdg-state"
    home.mkdir()
    state.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    return home, state


def _assert_secret_gone(
    launcher: ScriptLauncher, runs: Path, roots: list[Path], token: str
) -> None:
    assert launcher._processes == {}
    leftovers = [
        path
        for root in roots
        if root.exists()
        for path in root.rglob("self-report-*")
    ]
    assert leftovers == []
    hits = [path for root in roots for path in _files_with(root, token)]
    assert hits == []
    assert not list(runs.glob("*--self-report.log"))


@pytest.mark.parametrize("provider", ["minimax"])
@pytest.mark.parametrize("fenced", [False, True], ids=["object", "fence"])
def test_an_intermediate_report_loses_to_the_parser_final_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, fenced: bool
) -> None:
    final_report = _report(done=["fenced final"] if fenced else ["parsed the registry"])
    final = "```json\n" + final_report + "\n```" if fenced else final_report
    launcher, _root, _runs, _logs = _bind_transport(
        tmp_path, monkeypatch, provider, _transport_stdout(provider, final)
    )
    line = _assigned(f"line-{provider}-{int(fenced)}", provider)

    reply = asyncio.run(
        launcher.request_self_report(line, provider, session_id="old-session", timeout=5)
    )

    assert reply.status == "answered"
    assert reply.output.strip() == final
    assert "intermediate decoy" not in reply.output
    outcome = read_self_report(reply)
    assert outcome.status == "ok"
    assert outcome.report is not None
    assert outcome.report.done == (("fenced final",) if fenced else ("parsed the registry",))
    assert outcome.report.pitfalls == ("the fake CLI needs --session",)


@pytest.mark.parametrize("provider", ["minimax"])
def test_a_parser_error_is_an_invalid_self_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    if provider == "minimax":
        stdout = json.dumps(
            {"type": "exec.result", "status": "failed", "error": {"message": "parser boom"}}
        )
    else:
        stdout = json.dumps({"role": "assistant", "error": {"message": "parser boom"}})
    launcher, _root, _runs, _logs = _bind_transport(tmp_path, monkeypatch, provider, stdout + "\n")
    line = _assigned(f"line-parser-{provider}", provider)

    reply = asyncio.run(
        launcher.request_self_report(line, provider, session_id="old-session", timeout=5)
    )

    assert reply.status == "invalid"
    assert reply.output == ""


@pytest.mark.parametrize("provider", ["minimax"])
def test_prose_around_the_final_json_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    prose = "Notes before the object.\n" + _report() + "\nNotes after the object."
    launcher, _root, _runs, _logs = _bind_transport(
        tmp_path, monkeypatch, provider, _transport_stdout(provider, prose)
    )
    line = _assigned(f"line-prose-{provider}", provider)

    reply = asyncio.run(
        launcher.request_self_report(line, provider, session_id="old-session", timeout=5)
    )

    assert reply.status == "answered"
    assert reply.output.strip() == prose
    assert "intermediate decoy" not in reply.output
    outcome = read_self_report(reply)
    assert outcome.status == "invalid"
    assert outcome.report is None


@pytest.mark.parametrize(
    ("label", "exit_code", "sleep", "cancel"),
    [
        ("success", 0, "0", False),
        ("failure", 1, "0", False),
        ("timeout", 0, "8", False),
        ("cancel", 0, "8", True),
    ],
)
def test_a_fake_token_does_not_remain_after_the_self_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    exit_code: int,
    sleep: str,
    cancel: bool,
) -> None:
    home, state = _isolate_home(tmp_path, monkeypatch)
    launcher, _root, runs, script_logs = _bind_transport(
        tmp_path,
        monkeypatch,
        "minimax",
        # minimax's parser takes exactly one JSON document, so on success the
        # token rides inside it; the other cases print it as a stray line.
        _minimax_transport(_report(), intermediate=TOKEN) if label == "success" else _minimax_transport(_report()),
        leak="" if label == "success" else TOKEN,
        exit_code=exit_code,
        sleep=sleep,
    )
    line = _assigned(f"line-token-{label}", "minimax")
    roots = [runs, script_logs, home, state]
    # Cancel must land while the child is still asleep. A 1s budget would
    # time out and return a reply before this test can cancel the task.
    call_timeout = 1.0 if label == "timeout" else 15.0

    async def once() -> SelfReportReply | None:
        call = launcher.request_self_report(
            line, "minimax", session_id="old-kimi-session", timeout=call_timeout
        )
        if not cancel:
            return await call
        task = asyncio.create_task(call)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any(_files_with(root, TOKEN) for root in roots):
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return None

    reply = asyncio.run(once())
    if label == "success":
        assert reply is not None and reply.status == "answered"
        assert reply.output.strip() == _report()
        assert read_self_report(reply).status == "ok"
    elif label == "failure":
        assert reply is not None and reply.status == "failed"
    elif label == "timeout":
        assert reply is not None and reply.status == "timeout"
    else:
        assert reply is None
    _assert_secret_gone(launcher, runs, roots, TOKEN)


class _CountingLauncher:
    """Counts self-report and launch calls. The session store is the real one."""

    def __init__(self, source: ScriptLauncher) -> None:
        self._source = source
        self.self_reports = 0
        self.launches: list[str] = []
        self.order: list[str] = []
        self.on_report = None

    def recovery_session_manager(self, **kwargs):
        return self._source.recovery_session_manager(**kwargs)

    async def request_self_report(self, line, model, *, session_id, timeout=60.0):
        del line, model, session_id, timeout
        self.self_reports += 1
        self.order.append("self-report")
        hook = self.on_report
        if hook is not None:
            await hook()
        return SelfReportReply("answered", _report())

    async def launch_worker(self, line, model, skills, **kwargs):
        del model, skills, kwargs
        self.launches.append(line.id)
        self.order.append("launch")
        return LaunchResult(
            run_id=line.current_run or line.id,
            session_id="provider-session",
            output="ok",
        )


def _arm_kimi(collaborators, launcher: _CountingLauncher, dispatch: Dispatch, line) -> None:
    manager = launcher.recovery_session_manager()
    key = f"dispatch:{dispatch.id}:line:{line.id}:author:kimi"
    manager.record_turn(key, session_id="old-kimi-session", usage=NativeCliUsage(input_tokens=220000))
    _seed_resumable(collaborators, dispatch, line.id, "old-kimi-session")


def test_a_running_line_blocks_the_self_report_until_it_settles(tmp_path: Path) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    runs = tmp_path / "runs"
    runs.mkdir()
    source = ScriptLauncher(runs, script=LAUNCH_SCRIPT, cwd=tmp_path)
    launcher = _CountingLauncher(source)
    busy = _line("busy", assigned_model="kimi", status=LineStatus.RUNNING, current_run="run-busy")
    waiting = _line("waiting", assigned_model="kimi", status=LineStatus.ASSIGNED)
    collaborators, machine, *_ = _bundle(tmp_path, [busy, waiting], launcher=launcher)
    dispatch = Dispatch(
        id="d-serial",
        goal="one line at a time",
        user_id="u",
        created_at="now",
        lines=[busy, waiting],
        state=DispatchState.LAUNCHING,
    )
    _arm_kimi(collaborators, launcher, dispatch, waiting)

    async def scenario() -> None:
        await collaborators.store.create(dispatch)
        try:
            await asyncio.wait_for(
                machine._submit(machine._launch_guarded, dispatch, waiting), timeout=8
            )
            assert launcher.self_reports == 0
            assert launcher.launches == []
            assert waiting.failure_reason == "serial_wait"
            busy.status = LineStatus.PASSED
            busy.current_run = None
            await asyncio.wait_for(
                machine._submit(machine._launch_guarded, dispatch, waiting), timeout=8
            )
            assert launcher.self_reports == 1
            assert launcher.order == ["self-report", "launch"]
            assert launcher.launches == ["waiting"]
        finally:
            await machine.aclose()

    asyncio.run(scenario())


def test_a_blocker_during_the_self_report_does_not_launch(tmp_path: Path) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    runs = tmp_path / "runs"
    runs.mkdir()
    source = ScriptLauncher(runs, script=LAUNCH_SCRIPT, cwd=tmp_path)
    launcher = _CountingLauncher(source)
    busy = _line("busy", assigned_model="kimi", status=LineStatus.ASSIGNED)
    waiting = _line("waiting", assigned_model="kimi", status=LineStatus.ASSIGNED)
    collaborators, machine, *_ = _bundle(tmp_path, [busy, waiting], launcher=launcher)
    dispatch = Dispatch(
        id="d-serial-mid",
        goal="a sibling appears",
        user_id="u",
        created_at="now",
        lines=[busy, waiting],
        state=DispatchState.LAUNCHING,
    )
    _arm_kimi(collaborators, launcher, dispatch, waiting)

    async def appear() -> None:
        busy.status = LineStatus.RUNNING
        busy.current_run = "run-busy"

    launcher.on_report = appear

    async def scenario() -> None:
        await collaborators.store.create(dispatch)
        try:
            launched = await asyncio.wait_for(
                machine._submit(machine._launch_guarded, dispatch, waiting), timeout=8
            )
            assert launched is None
            assert launcher.self_reports == 1
            assert launcher.launches == []
            assert launcher.order == ["self-report"]
            assert waiting.failure_reason == "serial_wait"
        finally:
            await machine.aclose()

    asyncio.run(scenario())


def test_a_minimax_rotation_records_the_provider_id_on_the_next_turn(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    key = "dispatch:d-mm:line:l:author:minimax"
    manager.record_turn(
        key, session_id="old-mm-session", usage=NativeCliUsage(input_tokens=900_000)
    )
    result = manager.rotate(key, reason=ACTIVE_ROTATION)
    [event] = _line_state(manager, key)["archived"]
    assert event.get("new_session_id") in (None, "")
    assert event["new_session_pending"] is True
    assert "rot-" not in json.dumps(event)
    assert str(result.session_id).startswith("rot-")

    manager.record_turn(key, session_id="mm-real-session")
    [event] = _line_state(manager, key)["archived"]
    assert event["new_session_id"] == "mm-real-session"
    assert event["new_session_pending"] is False
    assert "rot-" not in json.dumps(event)
    for archived in _line_state(manager, key)["archived"]:
        assert "rot-" not in json.dumps(archived)


# -- regressions ----------------------------------------------------------


def _archived_ids_are_real(events: list[dict]) -> None:
    for event in events:
        for field in ("session_id", "new_session_id"):
            value = event.get(field)
            assert not (isinstance(value, str) and value.startswith("rot-"))


def test_invalid_oversize_and_missing_replies_keep_their_status() -> None:
    invalid = read_self_report(SelfReportReply("invalid"))
    oversize = read_self_report(SelfReportReply("oversize"))
    missing = read_self_report(None)
    assert invalid.status == "invalid"
    assert invalid.report is None
    assert oversize.status == "oversize"
    assert oversize.report is None
    assert missing.status == "unsupported"
    assert missing.report is None


def test_three_rotations_without_a_turn_keep_one_pending_event(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    key = "dispatch:d-rot:line:l:author:kimi"
    manager.record_turn(key, session_id="real-0")
    manager.rotate(key, reason=ACTIVE_ROTATION)
    manager.rotate(key, reason=ACTIVE_ROTATION)
    manager.rotate(key, reason=ACTIVE_ROTATION)
    archived = _line_state(manager, key)["archived"]
    _archived_ids_are_real(archived)
    [pending] = [event for event in archived if event.get("new_session_pending") is True]
    assert pending["session_id"] == "real-0"

    manager.record_turn(key, session_id="real-1")
    archived = _line_state(manager, key)["archived"]
    _archived_ids_are_real(archived)
    assert [event for event in archived if event.get("new_session_pending")] == []
    [filled] = [event for event in archived if event.get("new_session_id") == "real-1"]
    assert filled["session_id"] == "real-0"
    assert filled["new_session_pending"] is False


def _counting(tmp_path: Path) -> tuple[_CountingLauncher, Path]:
    runs = tmp_path / "runs"
    runs.mkdir()
    source = ScriptLauncher(runs, script=LAUNCH_SCRIPT, cwd=tmp_path)
    return _CountingLauncher(source), runs


def _serial_dispatch(dispatch_id: str, goal: str, lines: list) -> Dispatch:
    return Dispatch(
        id=dispatch_id,
        goal=goal,
        user_id="u",
        created_at="now",
        lines=lines,
        state=DispatchState.LAUNCHING,
    )


def _mark_running(line) -> None:
    line.status = LineStatus.RUNNING
    line.current_run = "run-busy"


@pytest.mark.parametrize(
    "moment",
    ["session_manager", "proactive_rotation_due", "current"],
)
def test_a_sibling_running_before_the_ask_defers_without_a_report(
    tmp_path: Path, moment: str
) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    launcher, _runs = _counting(tmp_path)
    busy = _line("busy", assigned_model="kimi", status=LineStatus.ASSIGNED)
    waiting = _line("waiting", assigned_model="kimi", status=LineStatus.ASSIGNED)
    collaborators, machine, *_ = _bundle(tmp_path, [busy, waiting], launcher=launcher)
    dispatch = _serial_dispatch("d-prep", "a sibling starts during rotation prep", [busy, waiting])
    _arm_kimi(collaborators, launcher, dispatch, waiting)
    manager = launcher.recovery_session_manager()

    if moment == "session_manager":
        original = launcher.recovery_session_manager

        def factory(**kwargs):
            got = original(**kwargs)
            _mark_running(busy)
            return got

        launcher.recovery_session_manager = factory
    elif moment == "proactive_rotation_due":
        original_due = manager.proactive_rotation_due

        def due(key: str) -> bool:
            _mark_running(busy)
            return original_due(key)

        manager.proactive_rotation_due = due
    else:
        original_current = manager.current

        def current(key: str):
            _mark_running(busy)
            return original_current(key)

        manager.current = current

    async def scenario() -> None:
        await collaborators.store.create(dispatch)
        try:
            launched = await asyncio.wait_for(
                machine._submit(machine._launch_guarded, dispatch, waiting), timeout=8
            )
            assert launched is None
            assert launcher.self_reports == 0
            assert launcher.launches == []
            assert waiting.failure_reason == "serial_wait"
            assert busy.status is LineStatus.RUNNING
        finally:
            await machine.aclose()

    asyncio.run(scenario())


def test_a_sibling_defers_during_the_self_report_then_launches_once(tmp_path: Path) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    launcher, _runs = _counting(tmp_path)
    asking = _line("asking", assigned_model="kimi", status=LineStatus.ASSIGNED)
    sibling = _line("sibling", assigned_model="kimi", status=LineStatus.ASSIGNED)
    collaborators, machine, *_ = _bundle(tmp_path, [asking, sibling], launcher=launcher)
    dispatch = _serial_dispatch("d-ask", "one ask holds the serial slot", [asking, sibling])
    _arm_kimi(collaborators, launcher, dispatch, asking)

    async def during_ask() -> None:
        launched = await asyncio.wait_for(
            machine._submit(machine._launch_guarded, dispatch, sibling), timeout=8
        )
        assert launched is None
        assert sibling.failure_reason == "serial_wait"
        assert launcher.launches == []

    launcher.on_report = during_ask

    async def scenario() -> None:
        await collaborators.store.create(dispatch)
        try:
            launched = await asyncio.wait_for(
                machine._submit(machine._launch_guarded, dispatch, asking), timeout=8
            )
            assert launched is not None
            assert launcher.self_reports == 1
            assert launcher.launches == ["asking"]
            asking.status = LineStatus.PASSED
            asking.current_run = None
            await asyncio.wait_for(
                machine._submit(machine._launch_guarded, dispatch, sibling), timeout=8
            )
            assert launcher.launches == ["asking", "sibling"]
            assert launcher.self_reports == 1
        finally:
            await machine.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["cursor", "glm", "grok"])
def test_cursor_glm_and_grok_are_not_spawned_for_a_self_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    async def fail_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("spawn_group was called")

    monkeypatch.setattr("src.tools.auto_dispatch.execution.spawn_group", fail_spawn)
    runs = tmp_path / "runs"
    runs.mkdir()
    launcher = ScriptLauncher(runs, script=LAUNCH_SCRIPT, cwd=tmp_path)
    line = _assigned(f"line-{provider}", provider)

    reply = asyncio.run(
        launcher.request_self_report(line, provider, session_id="old-session", timeout=1.0)
    )

    assert reply is None


def test_a_glm_rotation_still_writes_the_deterministic_system_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

    async def fail_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("spawn_group was called")

    monkeypatch.setattr("src.tools.auto_dispatch.execution.spawn_group", fail_spawn)
    launcher, _runs = _counting(tmp_path)
    source = launcher._source

    async def ask(line, model, *, session_id, timeout=60.0):
        launcher.self_reports += 1
        launcher.order.append("self-report")
        return await source.request_self_report(
            line, model, session_id=session_id, timeout=timeout
        )

    launcher.request_self_report = ask
    line = _line("line-glm", assigned_model="glm", status=LineStatus.ASSIGNED)
    collaborators, machine, *_ = _bundle(tmp_path, [line], launcher=launcher)
    dispatch = _serial_dispatch("d-glm", "rotate without a self-report", [line])
    manager = launcher.recovery_session_manager()
    key = f"dispatch:{dispatch.id}:line:{line.id}:author:glm"
    manager.record_turn(
        key, session_id="old-glm-session", usage=NativeCliUsage(input_tokens=900_000)
    )
    _seed_resumable(collaborators, dispatch, line.id, "old-glm-session")
    dispatch.gates["worker_recovery"][line.id]["session_author"] = "glm"

    async def scenario() -> None:
        await collaborators.store.create(dispatch)
        try:
            launched = await asyncio.wait_for(
                machine._submit(machine._launch_guarded, dispatch, line), timeout=8
            )
            assert launched is not None
            assert launcher.self_reports == 1
            assert launcher.launches == ["line-glm"]
        finally:
            await machine.aclose()

    asyncio.run(scenario())

    note_path = manager.handoff_path(key)
    assert note_path is not None
    note = note_path.read_text(encoding="utf-8")
    assert note.startswith(SYSTEM_HEADER)
    assert "worker_reported" not in note
    [event] = _line_state(manager, key)["archived"]
    assert event["self_report"] == "unsupported"
