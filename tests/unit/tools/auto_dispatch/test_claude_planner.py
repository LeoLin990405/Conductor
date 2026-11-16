"""The Claude Code planner backend, driven against a fake ``claude`` executable.

No test here starts the real CLI or reaches a model: the fake script records its
argv, cwd and stdin, then prints whatever reply the test asks for.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from src.server.services.autonomy.roles import RolePolicy
from src.tools.auto_dispatch import (
    AlwaysPassReviewer,
    Collaborators,
    Dispatch,
    DispatchMachine,
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    InMemoryOutcomeStore,
    NoopMerger,
    RecordingLauncher,
    RoundRobinSelector,
    ScriptedMonitor,
)
from src.tools.auto_dispatch.claude_planner import (
    DEFAULT_PLANNER_MODEL,
    DENIED_TOOLS,
    READ_ONLY_TOOLS,
    ClaudeCodeComplete,
    ClaudePlannerError,
    parse_result_envelope,
)
from src.tools.auto_dispatch.gates import PLANNER_FAILED
from src.tools.auto_dispatch.planner_prompt import (
    PLANNER_SYSTEM_PROMPT,
    REPO_GROUNDING_RULE,
    build_planner_prompt,
)
from src.tools.auto_dispatch.planner_reason import (
    INVALID_JSON,
    PLANNER_ERROR,
    PLANNER_TIMEOUT,
)
from src.tools.auto_dispatch.planning import LLMPlanner

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "planner_output_v1.json"
SECRET = "model-said-sk-live-SECRET-TOKEN"

FAKE_CLAUDE = textwrap.dedent(
    """\
    import json, os, subprocess, sys, time
    record = os.environ["FAKE_CLAUDE_RECORD"]
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    if mode == "orphan":
        # The parent exits at once without reading stdin; its child keeps the
        # inherited stdout/stderr pipes open, so the call cannot finish alone.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with open(record + ".child", "w") as fh:
            fh.write(str(child.pid))
        with open(record + ".parent", "w") as fh:
            fh.write(str(os.getpid()))
        os._exit(0)
    if mode == "straggler":
        # A clean reply, but a child that let go of every pipe stays behind.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with open(record + ".child", "w") as fh:
            fh.write(str(child.pid))
    stdin = sys.stdin.read()
    with open(record, "w", encoding="utf-8") as fh:
        json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": stdin,
                   "pid": os.getpid(), "pgid": os.getpgid(0)}, fh)
    secret = os.environ.get("FAKE_CLAUDE_SECRET", "")
    if mode in ("ok", "straggler"):
        result = open(os.environ["FAKE_CLAUDE_RESULT"], encoding="utf-8").read()
        print(json.dumps({"type": "result", "subtype": "success",
                          "is_error": False, "result": result}))
    elif mode == "exit":
        sys.stderr.write(secret)
        print(secret)
        sys.exit(3)
    elif mode == "bad_json":
        print("not json " + secret)
    elif mode == "is_error":
        print(json.dumps({"type": "result", "subtype": "success",
                          "is_error": True, "result": secret}))
    elif mode == "sleep":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        with open(record + ".child", "w") as fh:
            fh.write(str(child.pid))
        time.sleep(60)
    """
)


def _write_fake(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def fake_claude(tmp_path, monkeypatch, _no_real_claude_planner):
    script = _write_fake(tmp_path / "bin" / "claude")
    # The unit conftest only starts a binary on its explicit allowlist.
    _no_real_claude_planner.add(script.resolve())
    record = tmp_path / "record.json"
    result = tmp_path / "result.txt"
    result.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", str(result))
    monkeypatch.setenv("FAKE_CLAUDE_SECRET", SECRET)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    repo = tmp_path / "repo"
    repo.mkdir()
    return script, record, repo


def _read(record: Path) -> dict:
    return json.loads(record.read_text(encoding="utf-8"))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A reaped-but-not-yet-collected zombie still answers kill(0); ask ps.
    import subprocess

    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(out) and not out.startswith("Z")


async def test_argv_stdin_cwd_model_and_tools(fake_claude) -> None:
    script, record, repo = fake_claude
    complete = ClaudeCodeComplete(cwd=repo, binary=str(script), model="claude-test-model")

    reply = await complete("PLAN THIS GOAL", "SYSTEM RULES")

    seen = _read(record)
    assert seen["stdin"] == "PLAN THIS GOAL"
    assert Path(seen["cwd"]).resolve() == repo.resolve()
    argv = seen["argv"]
    assert argv[:7] == [
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "plan",
        "--model",
        "claude-test-model",
    ]
    assert argv[argv.index("--append-system-prompt") + 1] == "SYSTEM RULES"
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert allowed == ["Read", "Grep", "Glob"]
    # --tools is what makes these the only built-in tools that exist.
    assert argv[argv.index("--tools") + 1].split(",") == ["Read", "Grep", "Glob"]
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    for tool in ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        assert tool in denied
        assert tool not in allowed
    # The prompt is never on the command line.
    assert "PLAN THIS GOAL" not in argv
    # The process runs in its own group, so a timeout can kill all of it.
    assert seen["pgid"] == seen["pid"]
    assert reply == FIXTURE.read_text(encoding="utf-8")


async def test_defaults_are_claude_and_the_opus_model(tmp_path) -> None:
    complete = ClaudeCodeComplete(cwd=tmp_path)
    argv = complete.argv("s")
    assert argv[0] == "claude"
    assert DEFAULT_PLANNER_MODEL == "claude-opus-5-5"
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert complete.timeout_seconds == 600.0
    assert READ_ONLY_TOOLS == ("Read", "Grep", "Glob")
    assert set(DENIED_TOOLS).isdisjoint(READ_ONLY_TOOLS)


@pytest.mark.parametrize(
    "envelope",
    [
        {"type": "result", "subtype": "success", "is_error": False, "result": "hi"},
        {"type": "result", "is_error": False, "result": "hi", "num_turns": 3},
    ],
)
async def test_envelope_result_is_returned(envelope) -> None:
    assert parse_result_envelope(json.dumps(envelope).encode()) == "hi"


@pytest.mark.parametrize(
    ("stdout", "code"),
    [
        (b"not json", "claude_planner_bad_json"),
        (b"\xff\xfe", "claude_planner_bad_json"),
        (b"[1, 2]", "claude_planner_bad_envelope"),
        (json.dumps({"type": "assistant", "result": "x"}).encode(), "claude_planner_bad_envelope"),
        (json.dumps({"type": "result", "is_error": True, "result": SECRET}).encode(), "claude_planner_error_result"),
        (json.dumps({"type": "result", "subtype": "error_max_turns", "result": SECRET}).encode(), "claude_planner_error_result"),
        (json.dumps({"type": "result", "subtype": "success"}).encode(), "claude_planner_empty_result"),
        (json.dumps({"type": "result", "subtype": "success", "result": "  "}).encode(), "claude_planner_empty_result"),
    ],
)
async def test_bad_envelopes_raise_a_reason_code_only(stdout, code) -> None:
    with pytest.raises(ClaudePlannerError) as info:
        parse_result_envelope(stdout)
    assert str(info.value) == code
    assert SECRET not in str(info.value)


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("exit", "claude_planner_exit:3"),
        ("bad_json", "claude_planner_bad_json"),
        ("is_error", "claude_planner_error_result"),
    ],
)
async def test_cli_failures_raise_without_model_text(fake_claude, monkeypatch, caplog, mode, code) -> None:
    caplog.set_level(logging.DEBUG)
    script, _record, repo = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    complete = ClaudeCodeComplete(cwd=repo, binary=str(script))

    with pytest.raises(ClaudePlannerError) as info:
        await complete("p", "s")

    assert str(info.value) == code
    assert info.value.__cause__ is None
    assert SECRET not in caplog.text


async def test_a_missing_binary_raises_unavailable(tmp_path, _no_real_claude_planner) -> None:
    _no_real_claude_planner.add((tmp_path / "no-such-claude").resolve())
    complete = ClaudeCodeComplete(cwd=tmp_path, binary=str(tmp_path / "no-such-claude"))
    with pytest.raises(ClaudePlannerError) as info:
        await complete("p", "s")
    assert str(info.value) == "claude_planner_unavailable"


async def test_timeout_kills_the_process_group(fake_claude, monkeypatch) -> None:
    script, record, repo = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    complete = ClaudeCodeComplete(cwd=repo, binary=str(script), timeout_seconds=1.5)

    started = time.monotonic()
    with pytest.raises(ClaudePlannerError) as info:
        await complete("p", "s")
    assert time.monotonic() - started < 15
    assert str(info.value) == "claude_planner_timeout"

    pid = _read(record)["pid"]
    child = int(Path(str(record) + ".child").read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (_alive(pid) or _alive(child)):
        time.sleep(0.05)
    assert not _alive(pid)
    assert not _alive(child)


async def test_llm_planner_on_claude_code_returns_validated_lines(fake_claude) -> None:
    script, record, repo = fake_claude
    planner = LLMPlanner(
        ClaudeCodeComplete(cwd=repo, binary=str(script)), policy=RolePolicy.default()
    )

    lines = await planner.plan("add a dispatch status summary endpoint")

    assert [line.name for line in lines] == ["summary_model", "summary_route"]
    seen = _read(record)
    assert "add a dispatch status summary endpoint" in seen["stdin"]
    assert REPO_GROUNDING_RULE in seen["stdin"]
    assert seen["argv"][seen["argv"].index("--append-system-prompt") + 1] == PLANNER_SYSTEM_PROMPT


def _collaborators(tmp_path, planner) -> Collaborators:
    return Collaborators(
        planner=planner,
        selector=RoundRobinSelector(),
        launcher=RecordingLauncher(),
        monitor=ScriptedMonitor(),
        reviewer=AlwaysPassReviewer(),
        merger=NoopMerger(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        outcomes=InMemoryOutcomeStore(),
        store=InMemoryDispatchStore(),
    )


async def _asleep(_seconds: float) -> None:
    return None


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("exit", PLANNER_ERROR),
        ("bad_json", INVALID_JSON),
        ("is_error", PLANNER_ERROR),
        ("sleep", PLANNER_TIMEOUT),
    ],
)
async def test_a_failing_claude_run_parks_planner_failed(
    fake_claude, monkeypatch, tmp_path, caplog, mode, reason
) -> None:
    caplog.set_level(logging.DEBUG)
    script, _record, repo = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    planner = LLMPlanner(
        ClaudeCodeComplete(cwd=repo, binary=str(script), timeout_seconds=1.5),
        policy=RolePolicy.default(),
    )
    collaborators = _collaborators(tmp_path, planner)
    machine = DispatchMachine(collaborators, sleep=_asleep)
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )

    parked = await machine.run(dispatch)

    assert parked.state is DispatchState.REVIEW
    assert parked.message == f"{PLANNER_FAILED}:{reason}"
    assert parked.lines == []
    assert collaborators.launcher.launches == []
    stored = await collaborators.store.get("d1")
    assert stored is not None and stored.message == f"{PLANNER_FAILED}:{reason}"
    assert SECRET not in repr(stored.message)
    assert SECRET not in repr(stored.gates)
    assert SECRET not in caplog.text
    assert "ClaudePlannerError" in caplog.text


async def test_the_prompt_asks_for_repo_grounding_and_risk_commands() -> None:
    prompt = build_planner_prompt("ship it", {"kimi": 1})
    assert REPO_GROUNDING_RULE in prompt
    assert "Inspect the repository before planning" in prompt
    assert "that exist in it, or that the line itself creates" in prompt
    assert "risk_bearing" in prompt and '"cmd: <argv>"' in prompt
    assert "migrations, schemas, auth" in prompt


async def test_the_unit_suite_never_starts_the_real_claude(tmp_path) -> None:
    # tests/unit/conftest.py refuses a binary looked up on PATH.
    with pytest.raises(ClaudePlannerError) as info:
        await ClaudeCodeComplete(cwd=tmp_path, binary="claude")("p", "s")
    assert str(info.value) == "claude_planner_unavailable"


async def test_the_guard_refuses_an_absolute_binary_not_on_the_allowlist(
    tmp_path, monkeypatch
) -> None:
    # A real executable at an absolute path, but nobody allowlisted it.
    script = _write_fake(tmp_path / "elsewhere" / "claude")
    record = tmp_path / "unlisted.json"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "exit")
    with pytest.raises(ClaudePlannerError) as info:
        await ClaudeCodeComplete(cwd=tmp_path, binary=str(script))("p", "s")
    assert str(info.value) == "claude_planner_unavailable"
    assert not record.exists()


async def test_the_run_is_isolated_from_hooks_settings_plugins_and_mcp(fake_claude) -> None:
    script, record, repo = fake_claude
    await ClaudeCodeComplete(cwd=repo, binary=str(script))("p", "s")
    argv = _read(record)["argv"]
    # Same boundary as the read-only reviewer (src/llms/native_cli/claude.py).
    for flag in (
        "--restricted",
        "--safe-mode",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    ):
        assert argv.count(flag) == 1, flag
    assert json.loads(argv[argv.index("--settings") + 1]) == {"disableAllHooks": True}
    # Nothing may widen the run again.
    for widening in (
        "--mcp-config",
        "--plugin-dir",
        "--plugin-url",
        "--add-dir",
        "--setting-sources",
        "--dangerously-skip-permissions",
        "--resume",
        "--continue",
        "bypassPermissions",
    ):
        assert widening not in argv, widening
    assert argv[argv.index("--permission-mode") + 1] == "plan"


def _wait_dead(*pids: int, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and any(_alive(pid) for pid in pids):
        time.sleep(0.05)


async def test_timeout_after_the_parent_exited_kills_the_orphaned_child(
    fake_claude, monkeypatch
) -> None:
    script, record, repo = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "orphan")
    complete = ClaudeCodeComplete(cwd=repo, binary=str(script), timeout_seconds=2.0)

    with pytest.raises(ClaudePlannerError) as info:
        await complete("p", "s")

    assert str(info.value) == "claude_planner_timeout"
    parent = int(Path(str(record) + ".parent").read_text())
    child = int(Path(str(record) + ".child").read_text())
    _wait_dead(parent, child)
    assert not _alive(parent)
    assert not _alive(child)


async def test_cancellation_after_the_parent_exited_kills_the_orphaned_child(
    fake_claude, monkeypatch
) -> None:
    import asyncio

    script, record, repo = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "orphan")
    complete = ClaudeCodeComplete(cwd=repo, binary=str(script), timeout_seconds=60.0)
    parent_file = Path(str(record) + ".parent")
    child_file = Path(str(record) + ".child")

    task = asyncio.create_task(complete("p", "s"))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not (parent_file.exists() and child_file.exists()):
        await asyncio.sleep(0.02)
    parent = int(parent_file.read_text())
    child = int(child_file.read_text())
    while time.monotonic() < deadline and _alive(parent):
        await asyncio.sleep(0.02)
    assert not _alive(parent), "the fake parent should have exited before the cancel"
    assert _alive(child)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    _wait_dead(child)
    assert not _alive(child)


async def test_cancellation_while_the_cli_runs_kills_the_whole_group(
    fake_claude, monkeypatch
) -> None:
    import asyncio

    script, record, repo = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    complete = ClaudeCodeComplete(cwd=repo, binary=str(script), timeout_seconds=60.0)
    child_file = Path(str(record) + ".child")

    task = asyncio.create_task(complete("p", "s"))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not child_file.exists():
        await asyncio.sleep(0.02)
    pid = _read(record)["pid"]
    child = int(child_file.read_text())
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    _wait_dead(pid, child)
    assert not _alive(pid)
    assert not _alive(child)


async def test_a_clean_exit_still_kills_a_child_left_behind(fake_claude, monkeypatch) -> None:
    script, record, repo = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "straggler")

    reply = await ClaudeCodeComplete(cwd=repo, binary=str(script))("p", "s")

    assert reply == FIXTURE.read_text(encoding="utf-8")
    child = int(Path(str(record) + ".child").read_text())
    _wait_dead(child)
    assert not _alive(child)


@pytest.mark.parametrize("mode", ["exit", "bad_json", "is_error"])
async def test_failed_model_output_is_never_archived_as_a_plan(
    fake_claude, monkeypatch, tmp_path, mode
) -> None:
    from src.tools.auto_dispatch.planning import FileSpecArchive

    script, _record, repo = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    archive_root = tmp_path / "specs"
    planner = LLMPlanner(
        ClaudeCodeComplete(cwd=repo, binary=str(script)),
        policy=RolePolicy.default(),
        archive=FileSpecArchive(archive_root),
    )

    with pytest.raises(ClaudePlannerError):
        await planner.plan("ship it")

    assert planner.last_result is None
    stored = list(archive_root.rglob("*")) if archive_root.exists() else []
    assert stored == []
