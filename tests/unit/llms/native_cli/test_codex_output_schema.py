"""Codex ``--output-schema`` is opt-in and depends on the installed binary.

Fake CLIs in ``tmp_path`` record every argv. An old binary, a failed help
probe, or an unset opt-in must leave the exec argv byte-identical to
``build_invocation``. A new binary with the opt-in appends the schema flags
and the final message is read from ``-o``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.llms.native_cli.codex import (
    CODEX_OUTPUT_SCHEMA_ENV,
    CodexExecAdapter,
    _EXECUTABLE_CWD,
    _FINAL_MESSAGE_LIMIT,
    _OUTPUT_SCHEMA_PROBE_TIMEOUT_SECONDS,
    _OUTPUT_SCHEMA_SUPPORT,
    _SCHEMA_COPIES,
    _attach_isolated_output_schema,
    _attach_output_schema,
    _binary_supports_output_schema,
    _discard_schema_output,
    _probe_output_schema,
    _read_final_message,
)
from src.llms.native_cli.isolation import WorkerIsolation, _CONTEXT

_FAKE_CODEX = """\
#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
from pathlib import Path

log = os.environ.get("CODEX_ARGV_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv) + "\\n")
cwd_log = os.environ.get("CODEX_CWD_LOG")
if cwd_log:
    with open(cwd_log, "a", encoding="utf-8") as handle:
        handle.write(os.getcwd() + "\\n")

kind = os.environ.get("CODEX_FAKE_KIND", "old")
args = sys.argv[1:]
if args[:2] == ["exec", "--help"]:
    if kind == "fail":
        sys.stderr.write("help failed\\n")
        raise SystemExit(1)
    if kind == "slow":
        time.sleep(30)
    if kind == "tree":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        pid_log = os.environ.get("CODEX_CHILD_PID_LOG")
        if pid_log:
            Path(pid_log).write_text(str(child.pid), encoding="utf-8")
        print(child.pid, flush=True)
        time.sleep(30)
    if kind in {"new", "large"}:
        sys.stdout.write(
            "Options:\\n      --output-schema <FILE>\\n"
            "  -o, --output-last-message <FILE>\\n"
        )
    else:
        sys.stdout.write("Usage: codex exec [OPTIONS] [PROMPT]\\n")
    raise SystemExit(0)

if "-o" in args:
    out = Path(args[args.index("-o") + 1])
    if kind == "large":
        message = json.dumps(
            {"passed": False, "blockers": ["x" * 70000], "verdict": "block"}
        )
    else:
        message = '{"passed": true, "blockers": [], "verdict": "from-file"}'
    out.write_text(message + "\\n", encoding="utf-8")

sys.stdout.write(
    json.dumps({"type": "thread.started", "thread_id": "thread-1"}) + "\\n"
)
sys.stdout.write(
    json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "from-jsonl"},
        }
    )
    + "\\n"
)
sys.stdout.write(
    json.dumps(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }
    )
    + "\\n"
)
"""


def _install_codex(directory: Path, kind: str) -> Path:
    binary = directory / f"codex-{kind}"
    binary.write_text(_FAKE_CODEX, encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _install_local_codex(directory: Path, kind: str) -> Path:
    """A ``./codex`` whose help behavior is the file, not ``CODEX_FAKE_KIND``."""
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "codex"
    needle = 'kind = os.environ.get("CODEX_FAKE_KIND", "old")'
    script = _FAKE_CODEX.replace(needle, f"kind = {kind!r}", 1)
    if script == _FAKE_CODEX:
        raise RuntimeError("cwd-local codex kind was not baked into the script")
    binary.write_text(script, encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _recorded(log: Path) -> list[list[str]]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def _help_rows(rows: list[list[str]]) -> list[list[str]]:
    return [row for row in rows if row[1:3] == ["exec", "--help"]]


def _exec_rows(rows: list[list[str]]) -> list[list[str]]:
    return [row for row in rows if row[1:3] != ["exec", "--help"]]


def _publish_verdict_schema(monkeypatch: pytest.MonkeyPatch, path: Path) -> str:
    """Point the adapter at a schema path the owning line publishes.

    ``review_schema.py`` belongs to that line. Tests install only the
    ``VERDICT_SCHEMA_PATH`` attribute it exports.
    """
    module = types.ModuleType("src.tools.auto_dispatch.review_schema")
    module.VERDICT_SCHEMA_PATH = path
    monkeypatch.setitem(sys.modules, "src.tools.auto_dispatch.review_schema", module)
    parent = sys.modules.get("src.tools.auto_dispatch")
    if parent is not None:
        monkeypatch.setattr(parent, "review_schema", module, raising=False)
    return os.fspath(path)


@pytest.fixture(autouse=True)
def _clear_probe_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    _OUTPUT_SCHEMA_SUPPORT.clear()
    _SCHEMA_COPIES.clear()
    _EXECUTABLE_CWD.clear()
    monkeypatch.delenv(CODEX_OUTPUT_SCHEMA_ENV, raising=False)
    monkeypatch.delenv("CODEX_ARGV_LOG", raising=False)
    monkeypatch.delenv("CODEX_CWD_LOG", raising=False)
    monkeypatch.delenv("CODEX_FAKE_KIND", raising=False)
    yield
    _OUTPUT_SCHEMA_SUPPORT.clear()
    _SCHEMA_COPIES.clear()
    _EXECUTABLE_CWD.clear()


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    binary: Path,
    log: Path,
    *,
    kind: str,
    opt_in: str | None,
    prompt: str = "ping",
):
    if opt_in is None:
        monkeypatch.delenv(CODEX_OUTPUT_SCHEMA_ENV, raising=False)
    else:
        monkeypatch.setenv(CODEX_OUTPUT_SCHEMA_ENV, opt_in)
    monkeypatch.setenv("CODEX_ARGV_LOG", str(log))
    monkeypatch.setenv("CODEX_FAKE_KIND", kind)
    adapter = CodexExecAdapter(binary=str(binary), timeout_seconds=5)
    baseline = adapter.build_invocation(prompt, cwd=str(binary.parent))
    result = await adapter.run(prompt, cwd=str(binary.parent))
    return adapter, baseline.argv, result


@pytest.mark.asyncio
async def test_old_codex_keeps_main_argv_when_opt_in_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _install_codex(tmp_path, "old")
    log = tmp_path / "argv.jsonl"
    _adapter, baseline, result = await _run(monkeypatch, binary, log, kind="old", opt_in="1")
    rows = _recorded(log)
    assert _help_rows(rows) == [[str(binary), "exec", "--help"]]
    assert tuple(_exec_rows(rows)[0]) == baseline
    assert "--output-schema" not in baseline
    assert result.text == "from-jsonl"
    assert result.session_id == "thread-1"


@pytest.mark.asyncio
async def test_new_codex_without_opt_in_skips_probe_and_matches_main_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _install_codex(tmp_path, "new")
    log = tmp_path / "argv.jsonl"
    _adapter, baseline, result = await _run(monkeypatch, binary, log, kind="new", opt_in=None)
    rows = _recorded(log)
    assert _help_rows(rows) == []
    assert tuple(_exec_rows(rows)[0]) == baseline
    assert result.text == "from-jsonl"


@pytest.mark.asyncio
@pytest.mark.parametrize("opt_in", ["0", "false", "off", ""])
async def test_disabled_opt_in_values_leave_argv_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opt_in: str
) -> None:
    binary = _install_codex(tmp_path, "new")
    log = tmp_path / "argv.jsonl"
    _adapter, baseline, result = await _run(monkeypatch, binary, log, kind="new", opt_in=opt_in)
    rows = _recorded(log)
    assert _help_rows(rows) == []
    assert tuple(_exec_rows(rows)[0]) == baseline
    assert result.text == "from-jsonl"


@pytest.mark.asyncio
async def test_new_codex_appends_schema_flags_and_reads_final_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An older binary probed first must not poison the cache for this one.
    schema = tmp_path / "verdict.schema.json"
    schema.write_text("{}\n", encoding="utf-8")
    published = _publish_verdict_schema(monkeypatch, schema)
    old = _install_codex(tmp_path, "old")
    new = _install_codex(tmp_path, "new")
    old_log = tmp_path / "old.jsonl"
    await _run(monkeypatch, old, old_log, kind="old", opt_in="1")
    log = tmp_path / "argv.jsonl"
    _adapter, baseline, result = await _run(monkeypatch, new, log, kind="new", opt_in="true")
    rows = _recorded(log)
    executed = tuple(_exec_rows(rows)[0])
    output_path = executed[-1]
    assert executed == (*baseline, "--output-schema", published, "-o", output_path)
    assert os.path.isfile(published)
    assert result.text == '{"passed": true, "blockers": [], "verdict": "from-file"}'
    assert result.text != "from-jsonl"
    assert result.session_id == "thread-1"
    assert result.usage.input_tokens == 3
    assert not Path(output_path).exists()
    # build_invocation itself stays the main argv; only the exec got the flags.
    assert _adapter.build_invocation("ping", cwd=str(tmp_path)).argv == baseline


@pytest.mark.asyncio
async def test_probe_is_cached_per_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    new = _install_codex(tmp_path, "new")
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = _install_codex(other_dir, "new")
    log = tmp_path / "argv.jsonl"
    await _run(monkeypatch, new, log, kind="new", opt_in="yes")
    await _run(monkeypatch, new, log, kind="new", opt_in="yes")
    await _run(monkeypatch, other, log, kind="new", opt_in="on")
    rows = _recorded(log)
    new_help = [row for row in _help_rows(rows) if row[0] == str(new)]
    other_help = [row for row in _help_rows(rows) if row[0] == str(other)]
    assert new_help == [[str(new), "exec", "--help"]]
    assert other_help == [[str(other), "exec", "--help"]]
    assert len(_exec_rows(rows)) == 3


def test_probe_of_relative_binary_uses_invocation_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_probe_output_schema('./codex')`` runs in the invocation cwd, not the daemon's."""
    seen: dict[str, object] = {}

    def spy(*args, **kwargs):
        seen["cwd"] = kwargs.get("cwd")
        seen["argv"] = list(args[0] if args else kwargs["args"])

        class _Done:
            returncode = 0
            stdout = None
            stderr = None

            def communicate(self, timeout=None):
                return ("--output-schema\n", "")

            def wait(self, timeout=None):
                return 0

        return _Done()

    monkeypatch.setattr("src.llms.native_cli.codex.subprocess.Popen", spy)
    invocation = CodexExecAdapter(binary="./codex", cwd="/workspace/old").build_invocation("x")
    assert _probe_output_schema(invocation.argv[0]) is True
    assert seen["cwd"] == invocation.cwd
    assert seen["argv"][:3] == ["./codex", "exec", "--help"]


@pytest.mark.asyncio
async def test_cwd_local_binaries_do_not_share_output_schema_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new ``./codex`` in the daemon cwd must not enable flags for an old one."""
    daemon = tmp_path / "daemon"
    old_ws = tmp_path / "old"
    new_ws = tmp_path / "new"
    _install_local_codex(daemon, "new")
    _install_local_codex(old_ws, "old")
    _install_local_codex(new_ws, "new")
    schema = tmp_path / "verdict.schema.json"
    schema.write_text("{}\n", encoding="utf-8")
    published = _publish_verdict_schema(monkeypatch, schema)
    monkeypatch.chdir(daemon)
    monkeypatch.setenv(CODEX_OUTPUT_SCHEMA_ENV, "1")
    log = tmp_path / "argv.jsonl"
    cwd_log = tmp_path / "cwd.txt"
    monkeypatch.setenv("CODEX_ARGV_LOG", str(log))
    monkeypatch.setenv("CODEX_CWD_LOG", str(cwd_log))

    old_adapter = CodexExecAdapter(binary="./codex", timeout_seconds=5)
    old_baseline = old_adapter.build_invocation("ping", cwd=str(old_ws))
    old_result = await old_adapter.run("ping", cwd=str(old_ws))
    assert old_result.text == "from-jsonl"
    assert tuple(_exec_rows(_recorded(log))[0]) == old_baseline.argv
    assert "--output-schema" not in old_baseline.argv

    new_adapter = CodexExecAdapter(binary="./codex", timeout_seconds=5)
    new_baseline = new_adapter.build_invocation("ping", cwd=str(new_ws))
    new_result = await new_adapter.run("ping", cwd=str(new_ws))
    executed = tuple(_exec_rows(_recorded(log))[1])
    output_path = executed[-1]
    assert executed == (*new_baseline.argv, "--output-schema", published, "-o", output_path)
    assert new_result.text == '{"passed": true, "blockers": [], "verdict": "from-file"}'

    await old_adapter.run("ping", cwd=str(old_ws))
    rows = _recorded(log)
    assert _help_rows(rows) == [
        ["./codex", "exec", "--help"],
        ["./codex", "exec", "--help"],
    ]
    assert tuple(_exec_rows(rows)[2]) == old_baseline.argv
    cwd_lines = cwd_log.read_text(encoding="utf-8").splitlines()
    assert Path(cwd_lines[0]).resolve() == old_ws.resolve()
    assert Path(cwd_lines[2]).resolve() == new_ws.resolve()


def test_bare_name_cache_uses_child_cwd_for_relative_which(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative ``shutil.which`` hit is the child's file, not the daemon's."""
    monkeypatch.setattr(
        "src.llms.native_cli.codex.shutil.which",
        lambda *_args, **_kwargs: "bin/codex",
    )
    answers = iter((True, False))
    monkeypatch.setattr(
        "src.llms.native_cli.codex._probe_output_schema",
        lambda *_args, **_kwargs: next(answers),
    )
    assert _binary_supports_output_schema("codex", "/new") is True
    assert _binary_supports_output_schema("codex", "/old") is False


@pytest.mark.asyncio
async def test_path_local_binaries_do_not_share_output_schema_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PATH=bin`` resolves in the child cwd, so workspaces do not share a cache."""
    daemon = tmp_path / "daemon"
    old_ws = tmp_path / "old"
    new_ws = tmp_path / "new"
    _install_local_codex(daemon / "bin", "new")
    _install_local_codex(old_ws / "bin", "old")
    _install_local_codex(new_ws / "bin", "new")
    schema = tmp_path / "verdict.schema.json"
    schema.write_text("{}\n", encoding="utf-8")
    published = _publish_verdict_schema(monkeypatch, schema)
    monkeypatch.chdir(daemon)
    monkeypatch.setenv("PATH", "bin" + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv(CODEX_OUTPUT_SCHEMA_ENV, "1")
    log = tmp_path / "argv.jsonl"
    cwd_log = tmp_path / "cwd.txt"
    monkeypatch.setenv("CODEX_ARGV_LOG", str(log))
    monkeypatch.setenv("CODEX_CWD_LOG", str(cwd_log))

    new_adapter = CodexExecAdapter(binary="codex", timeout_seconds=5)
    new_baseline = new_adapter.build_invocation("ping", cwd=str(new_ws))
    new_result = await new_adapter.run("ping", cwd=str(new_ws))
    executed = tuple(_exec_rows(_recorded(log))[0])
    output_path = executed[-1]
    # A shebang script reports the PATH entry the kernel opened (``bin/codex``),
    # not the bare argv[0] exec received. The rest of argv is the invocation.
    assert executed[0] == "bin/codex"
    assert executed[1:] == (
        *new_baseline.argv[1:],
        "--output-schema",
        published,
        "-o",
        output_path,
    )
    assert new_result.text == '{"passed": true, "blockers": [], "verdict": "from-file"}'

    old_adapter = CodexExecAdapter(binary="codex", timeout_seconds=5)
    old_baseline = old_adapter.build_invocation("ping", cwd=str(old_ws))
    old_result = await old_adapter.run("ping", cwd=str(old_ws))
    old_executed = tuple(_exec_rows(_recorded(log))[1])
    assert old_result.text == "from-jsonl"
    assert old_executed[0] == "bin/codex"
    assert old_executed[1:] == old_baseline.argv[1:]
    assert "--output-schema" not in old_executed

    await new_adapter.run("ping", cwd=str(new_ws))
    rows = _recorded(log)
    assert [row[1:] for row in _help_rows(rows)] == [
        ["exec", "--help"],
        ["exec", "--help"],
    ]
    assert all(row[0] == "bin/codex" for row in _help_rows(rows))
    third = tuple(_exec_rows(rows)[2])
    assert third[:-1] == executed[:-1]
    cwd_lines = cwd_log.read_text(encoding="utf-8").splitlines()
    assert Path(cwd_lines[0]).resolve() == new_ws.resolve()
    assert Path(cwd_lines[2]).resolve() == old_ws.resolve()


@pytest.mark.asyncio
async def test_help_failure_counts_as_unsupported_and_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _install_codex(tmp_path, "fail")
    log = tmp_path / "argv.jsonl"
    _adapter, baseline, result = await _run(monkeypatch, binary, log, kind="fail", opt_in="1")
    await _run(monkeypatch, binary, log, kind="fail", opt_in="1")
    rows = _recorded(log)
    assert _help_rows(rows) == [[str(binary), "exec", "--help"]]
    assert all(tuple(row) == baseline for row in _exec_rows(rows))
    assert result.text == "from-jsonl"


@pytest.mark.asyncio
async def test_help_timeout_counts_as_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _OUTPUT_SCHEMA_PROBE_TIMEOUT_SECONDS <= 5
    monkeypatch.setattr(
        "src.llms.native_cli.codex._OUTPUT_SCHEMA_PROBE_TIMEOUT_SECONDS", 0.3
    )
    probes: list[tuple[list[str], bool | None]] = []
    real_popen = subprocess.Popen

    def spy_popen(*args, **kwargs):
        command = list(args[0] if args else kwargs["args"])
        if command[1:3] == ["exec", "--help"]:
            probes.append((command, kwargs.get("start_new_session")))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("src.llms.native_cli.codex.subprocess.Popen", spy_popen)
    binary = _install_codex(tmp_path, "slow")
    log = tmp_path / "argv.jsonl"
    started = time.monotonic()
    _adapter, baseline, result = await _run(monkeypatch, binary, log, kind="slow", opt_in="1")
    assert time.monotonic() - started < 2
    assert probes == [([str(binary), "exec", "--help"], True)]
    rows = _recorded(log)
    assert tuple(_exec_rows(rows)[0]) == baseline
    assert result.text == "from-jsonl"


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.requires_ps
def test_help_timeout_kills_tree_and_reaps_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _install_codex(tmp_path, "tree")
    pid_log = tmp_path / "child.pid"
    monkeypatch.setenv("CODEX_FAKE_KIND", "tree")
    monkeypatch.setenv("CODEX_CHILD_PID_LOG", str(pid_log))
    monkeypatch.setattr(
        "src.llms.native_cli.codex._OUTPUT_SCHEMA_PROBE_TIMEOUT_SECONDS", 1.5
    )
    real_popen = subprocess.Popen
    leaders: list[subprocess.Popen[str]] = []

    def capture_popen(*args, **kwargs):
        command = list(args[0] if args else kwargs["args"])
        process = real_popen(*args, **kwargs)
        if command[0] == str(binary):
            leaders.append(process)
        return process

    monkeypatch.setattr("src.llms.native_cli.codex.subprocess.Popen", capture_popen)
    child_pid = None
    try:
        assert _probe_output_schema(str(binary)) is False
        child_pid = int(pid_log.read_text(encoding="utf-8"))
        assert len(leaders) == 1
        assert leaders[0].returncode is not None

        def child_running() -> bool:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                return False
            status = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child_pid)],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            return bool(status) and not status.startswith("Z")

        deadline = time.monotonic() + 3
        while child_running() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not child_running()
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.requires_ps
def test_exited_probe_with_detached_pipe_owner_has_bounded_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leader that exits during the help timeout must not leave its setsid child."""
    pid_log = tmp_path / "detached-child.pid"
    leader_code = """\
import subprocess
import sys
from pathlib import Path

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    start_new_session=True,
)
Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
"""
    monkeypatch.setattr(
        "src.llms.native_cli.isolation.prepare_process",
        lambda *args, **kwargs: (
            (sys.executable, "-c", leader_code, str(pid_log)),
            None,
            None,
        ),
    )
    monkeypatch.setattr(
        "src.llms.native_cli.codex._OUTPUT_SCHEMA_PROBE_TIMEOUT_SECONDS", 0.1
    )
    child_pid = None
    try:
        started = time.monotonic()
        assert _probe_output_schema("fake") is False
        assert time.monotonic() - started < 1
        child_pid = int(pid_log.read_text(encoding="utf-8"))

        def child_running() -> bool:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                return False
            status = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child_pid)],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            return bool(status) and not status.startswith("Z")

        deadline = time.monotonic() + 1
        while child_running() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not child_running()
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass


@pytest.mark.asyncio
async def test_missing_schema_file_leaves_argv_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_verdict_schema(monkeypatch, tmp_path / "missing.json")
    binary = _install_codex(tmp_path, "new")
    log = tmp_path / "argv.jsonl"
    _adapter, baseline, result = await _run(monkeypatch, binary, log, kind="new", opt_in="1")
    rows = _recorded(log)
    assert tuple(_exec_rows(rows)[0]) == baseline
    assert result.text == "from-jsonl"


def _readonly_cwd_snapshot(binary: Path, cwd: Path) -> tuple[str, ...]:
    """Main exec argv: read-only sandbox and an explicit cwd. Not from the adapter."""
    return (
        str(binary),
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-C",
        str(cwd),
        "-",
    )


def _resume_snapshot(binary: Path) -> tuple[str, ...]:
    """Resume argv. Sandbox and ``-C`` stay off even when a cwd is passed."""
    return (
        str(binary),
        "exec",
        "resume",
        "thread-9",
        "--json",
        "--skip-git-repo-check",
        "-",
    )


async def _snapshot_run(
    monkeypatch: pytest.MonkeyPatch,
    binary: Path,
    log: Path,
    *,
    kind: str,
    opt_in: str | None,
    cwd: Path,
    session_id: str | None = None,
):
    if opt_in is None:
        monkeypatch.delenv(CODEX_OUTPUT_SCHEMA_ENV, raising=False)
    else:
        monkeypatch.setenv(CODEX_OUTPUT_SCHEMA_ENV, opt_in)
    monkeypatch.setenv("CODEX_ARGV_LOG", str(log))
    monkeypatch.setenv("CODEX_FAKE_KIND", kind)
    adapter = CodexExecAdapter(binary=str(binary), timeout_seconds=5)
    result = await adapter.run("ping", session_id=session_id, cwd=str(cwd))
    rows = _recorded(log)
    return rows, result


@pytest.mark.asyncio
@pytest.mark.parametrize(("opt_in", "kind"), [(None, "new"), ("1", "old")])
async def test_readonly_cwd_snapshot_when_schema_flags_stay_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opt_in: str | None, kind: str
) -> None:
    binary = _install_codex(tmp_path, kind)
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    log = tmp_path / "argv.jsonl"
    rows, result = await _snapshot_run(
        monkeypatch, binary, log, kind=kind, opt_in=opt_in, cwd=cwd
    )
    assert tuple(_exec_rows(rows)[0]) == _readonly_cwd_snapshot(binary, cwd)
    if opt_in is None:
        assert _help_rows(rows) == []
    else:
        assert _help_rows(rows) == [[str(binary), "exec", "--help"]]
    assert result.text == "from-jsonl"


@pytest.mark.asyncio
@pytest.mark.parametrize(("opt_in", "kind"), [(None, "new"), ("1", "old")])
async def test_resume_snapshot_when_schema_flags_stay_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opt_in: str | None, kind: str
) -> None:
    binary = _install_codex(tmp_path, kind)
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    log = tmp_path / "argv.jsonl"
    rows, result = await _snapshot_run(
        monkeypatch,
        binary,
        log,
        kind=kind,
        opt_in=opt_in,
        cwd=cwd,
        session_id="thread-9",
    )
    assert tuple(_exec_rows(rows)[0]) == _resume_snapshot(binary)
    assert "--sandbox" not in _exec_rows(rows)[0]
    assert "-C" not in _exec_rows(rows)[0]
    assert result.text == "from-jsonl"


def test_probe_refuses_unapproved_host_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve() / "worker"
    work = tmp_path.resolve() / "work"
    work.mkdir()
    monkeypatch.setenv("exec", "--output-schema")
    monkeypatch.setenv("--help", "synthetic-unapproved-value")
    seen: dict[str, object] = {}
    real_popen = subprocess.Popen

    def spy_popen(*args, **kwargs):
        seen["argv"] = list(args[0] if args else kwargs["args"])
        seen["env"] = kwargs.get("env")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("src.llms.native_cli.codex.subprocess.Popen", spy_popen)
    token = _CONTEXT.set(WorkerIsolation("test", root, work, frozenset()))
    try:
        assert _probe_output_schema("/usr/bin/printenv") is False
    finally:
        _CONTEXT.reset(token)
    child_env = seen["env"]
    assert isinstance(child_env, dict)
    assert "exec" not in child_env
    assert "--help" not in child_env
    assert "synthetic-unapproved-value" not in child_env.values()
    assert child_env.get("TMPDIR") == str(root / "tmp")


def test_probe_on_unwritable_isolation_root_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("exec", "--output-schema")
    monkeypatch.setenv("--help", "synthetic-unapproved-value")
    token = _CONTEXT.set(
        WorkerIsolation("test", Path("/isolated"), Path.cwd(), frozenset())
    )
    try:
        assert _probe_output_schema("/usr/bin/printenv") is False
    finally:
        _CONTEXT.reset(token)


def test_read_final_message_redacts_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "synthetic-token"
    path = tmp_path / "final.txt"
    path.write_text(secret, encoding="utf-8")
    monkeypatch.setattr(
        "src.llms.native_cli.isolation.credential_values",
        lambda overrides=None: (secret,),
    )
    from src.llms.native_cli.isolation import redact_diagnostic

    assert redact_diagnostic(secret, None) == "[redacted]"
    assert _read_final_message(str(path)) == "[redacted]"


def test_read_final_message_does_not_follow_output_symlink(tmp_path: Path) -> None:
    host = tmp_path / "host"
    host.write_text("HOST_SENTINEL", encoding="utf-8")
    worker = tmp_path / "worker" / "tmp"
    worker.mkdir(parents=True)
    output = worker / "out"
    output.symlink_to(host)
    assert _read_final_message(str(output)) == ""
    assert host.read_text(encoding="utf-8") == "HOST_SENTINEL"


def test_read_final_message_redacts_override_credentials(tmp_path: Path) -> None:
    secret = "synthetic-token"
    path = tmp_path / "final.txt"
    path.write_text(f"blockers mention {secret}\n", encoding="utf-8")
    token = _CONTEXT.set(
        WorkerIsolation("test", tmp_path, tmp_path, frozenset({"MY_TOKEN"}))
    )
    try:
        assert _read_final_message(str(path), {"MY_TOKEN": secret}) == (
            "blockers mention [redacted]"
        )
    finally:
        _CONTEXT.reset(token)


def test_read_final_message_empty_file_skips_redaction(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("  \n", encoding="utf-8")
    assert _read_final_message(str(path), {"MY_TOKEN": "synthetic-token"}) == ""


def test_isolated_attach_setup_failure_keeps_original_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = CodexExecAdapter(binary="fake").build_invocation("review")
    monkeypatch.setattr(
        "src.llms.native_cli.codex._open_isolated_tmp", lambda *args, **kwargs: None
    )

    updated, out = _attach_isolated_output_schema(
        invocation, invocation.argv, "/schema.json", Path("/worker")
    )

    assert updated == invocation
    assert out is None


def test_isolated_attach_copies_schema_into_worker_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve() / "worker"
    work = tmp_path.resolve() / "work"
    work.mkdir()
    root.mkdir()
    schema_file = tmp_path / "verdict.schema.json"
    payload = b'{"type":"object"}\n'
    schema_file.write_bytes(payload)
    _publish_verdict_schema(monkeypatch, schema_file)
    monkeypatch.setenv(CODEX_OUTPUT_SCHEMA_ENV, "1")
    _OUTPUT_SCHEMA_SUPPORT["/fake/codex"] = True

    def refuse_mkstemp(*args, **kwargs):
        raise AssertionError("host mkstemp must not run under worker isolation")

    monkeypatch.setattr("src.llms.native_cli.codex.tempfile.mkstemp", refuse_mkstemp)
    token = _CONTEXT.set(WorkerIsolation("test", root, work, frozenset()))
    try:
        updated, out = _attach_output_schema(
            CodexExecAdapter(binary="/fake/codex").build_invocation("review")
        )
        assert out is not None
        schema = updated.argv[updated.argv.index("--output-schema") + 1]
        assert Path(out).is_relative_to(root / "tmp")
        assert Path(schema).is_relative_to(root / "tmp")
        assert Path(schema).read_bytes() == payload
        assert Path(out).is_file()
        assert Path(schema).stat().st_mode & 0o777 == 0o600
        assert _SCHEMA_COPIES[out] == schema
    finally:
        _CONTEXT.reset(token)


def test_isolated_attach_rejects_symlinked_tmp_before_host_mutations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "worker"
    root.mkdir()
    host = tmp_path / "host"
    host.mkdir()
    (root / "tmp").symlink_to(host)
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    invocation = CodexExecAdapter(binary="/fake").build_invocation("review")

    _attach_isolated_output_schema(invocation, invocation.argv, str(schema), root)

    assert list(host.iterdir()) == []
    assert (root / "tmp").is_symlink()


def test_isolated_read_and_cleanup_reject_symlinked_tmp(tmp_path: Path) -> None:
    root = tmp_path / "worker"
    root.mkdir()
    host = tmp_path / "host"
    host.mkdir()
    (root / "tmp").symlink_to(host)
    output = str(root / "tmp" / "out")
    schema = str(root / "tmp" / "schema")
    (host / "out").write_text("HOST_SENTINEL", encoding="utf-8")
    (host / "schema").write_text("SCHEMA_SENTINEL", encoding="utf-8")
    token = _CONTEXT.set(WorkerIsolation("test", root, tmp_path, frozenset()))
    try:
        assert _read_final_message(output) == ""
        _SCHEMA_COPIES[output] = schema
        _discard_schema_output(output)
    finally:
        _CONTEXT.reset(token)

    assert (host / "out").read_text(encoding="utf-8") == "HOST_SENTINEL"
    assert (host / "schema").read_text(encoding="utf-8") == "SCHEMA_SENTINEL"


def test_read_final_message_rejects_oversized_output_whole(tmp_path: Path) -> None:
    path = tmp_path / "final.json"
    payload = json.dumps({"blockers": ["x" * (_FINAL_MESSAGE_LIMIT + 1)]})
    path.write_text(payload, encoding="utf-8")
    assert json.loads(payload)["blockers"]
    assert _read_final_message(str(path)) == ""


@pytest.mark.asyncio
async def test_oversized_file_output_retains_complete_stdout_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = tmp_path / "verdict.schema.json"
    schema.write_text("{}\n", encoding="utf-8")
    _publish_verdict_schema(monkeypatch, schema)
    binary = _install_codex(tmp_path, "large")
    log = tmp_path / "argv.jsonl"

    _adapter, baseline, result = await _run(
        monkeypatch, binary, log, kind="large", opt_in="1"
    )

    executed = tuple(_exec_rows(_recorded(log))[0])
    output_path = executed[-1]
    assert executed[:-4] == baseline
    assert executed[-4:-2] == ("--output-schema", str(schema))
    assert executed[-2] == "-o"
    assert result.text == "from-jsonl"
    assert result.session_id == "thread-1"
    assert result.usage.input_tokens == 3
    assert not Path(output_path).exists()


@pytest.mark.asyncio
async def test_slow_help_probe_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    monkeypatch.setattr(
        "src.llms.native_cli.codex._OUTPUT_SCHEMA_PROBE_TIMEOUT_SECONDS", 0.8
    )
    binary = _install_codex(tmp_path, "slow")
    log = tmp_path / "argv.jsonl"
    gaps: list[float] = []
    done = asyncio.Event()

    async def ticker() -> None:
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)  # let the ticker start before the probe runs
    try:
        _adapter, baseline, result = await _run(
            monkeypatch, binary, log, kind="slow", opt_in="1"
        )
    finally:
        done.set()
        await task
    assert result.text == "from-jsonl"
    assert tuple(_exec_rows(_recorded(log))[0]) == baseline
    assert gaps and max(gaps) < 0.5, f"event loop stalled for {max(gaps):.2f}s"


@pytest.mark.asyncio
async def test_cancel_during_schema_setup_discards_the_output_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import tempfile
    import threading

    _publish_verdict_schema(monkeypatch, tmp_path / "verdict.schema.json")
    (tmp_path / "verdict.schema.json").write_text("{}", encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(out_dir))
    monkeypatch.setenv(CODEX_OUTPUT_SCHEMA_ENV, "1")
    probing = threading.Event()
    release = threading.Event()

    def slow_probe(binary: str, cwd: object = None) -> bool:
        probing.set()
        release.wait(5)
        return True

    monkeypatch.setattr("src.llms.native_cli.codex._probe_output_schema", slow_probe)
    import src.llms.native_cli.codex as codex_module

    real_attach = codex_module._attach_output_schema
    attached = threading.Event()

    def recording_attach(invocation):
        try:
            return real_attach(invocation)
        finally:
            attached.set()

    monkeypatch.setattr(codex_module, "_attach_output_schema", recording_attach)
    binary = _install_codex(tmp_path, "new")
    adapter = CodexExecAdapter(binary=str(binary), timeout_seconds=5)
    task = asyncio.create_task(adapter.run("ping", cwd=str(tmp_path)))
    while not probing.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.05)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    while not attached.is_set():
        await asyncio.sleep(0.01)
    # Let the attach result reach the loop and its done callback run.
    for _ in range(10):
        await asyncio.sleep(0.01)
    assert list(out_dir.iterdir()) == []
    assert _SCHEMA_COPIES == {}

