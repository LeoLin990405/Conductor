"""The dispatch launch script starts a development Worker, and only that.

``scripts/dispatch_launch.sh`` is the seam ``ScriptLauncher`` calls:
``dispatch_launch.sh <model> <task> [--skills=<s>] [--impact-map=<p>]``.
These tests drive it with fake CLIs on PATH — no real model is started and no
quota is spent — and pin the argv each Worker receives, the exit codes, and
the properties the launcher depends on: a Worker runs in the caller's working
directory, its prompt carries the task and the optional lines, and a stopped
run leaves nothing behind.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "dispatch_launch.sh"

# The roster name, the CLI binary it is served by, and the argv that binary
# must receive. The prompt is replaced by <PROMPT> in the expected argv.
WORKERS = [
    (
        "kimi",
        "kimi",
        # stream-json makes Kimi name the session it created (N22).
        ["-p", "<PROMPT>", "--output-format", "stream-json"],
    ),
    (
        "glm",
        "zcode",
        [
            "--prompt",
            "<PROMPT>",
            "--output-format",
            "stream-json",
            "--mode",
            "yolo",
            "--surface",
            "terminal",
            "--no-color",
        ],
    ),
    (
        "grok",
        "grok",
        [
            "--prompt-file",
            "<PROMPT_FILE>",
            "--always-approve",
            "--reasoning-effort",
            "high",
            "--max-turns",
            "150",
            "--output-format",
            "streaming-json",
        ],
    ),
    (
        "cursor",
        "cursor-agent",
        [
            "-p",
            "--force",
            "--trust",
            "--workspace",
            "<PWD>",
            "--output-format",
            "stream-json",
            "<PROMPT>",
        ],
    ),
    (
        "stepfun",
        "step",
        [
            "-p",
            "--no-session",
            "--approval-mode",
            "auto",
            "--non-interactive-approval",
            "allow",
            "--mode",
            "json",
            "<PROMPT>",
        ],
    ),
    (
        "deepseek",
        "dsh",
        ["--profile", "headless", "-"],
    ),
    (
        "minimax",
        "mcode",
        [
            "exec",
            "--cwd",
            "<PWD>",
            "--prompt-mode",
            "coding",
            "--permission",
            "full",
            "--output-format",
            "stream-json",
            "--input",
            "-",
        ],
    ),
    (
        "doubao",
        "arkcli",
        ["+chat", "--format", "json", "--no-progress", "--", "<PROMPT>"],
    ),
]

# A CLI alias folds onto the roster name.
ALIASES = [
    ("zcode", "glm"),
    ("zcode-cli", "glm"),
    ("step", "stepfun"),
    ("dsh", "deepseek"),
    ("mcode", "minimax"),
    ("cursor-agent", "cursor"),
    ("arkcli", "doubao"),
]

# Reserved roles are never Workers.
RESERVED = ["codex", "claude", "claude-code", "codexrev", "claudedev"]

# Not a Worker, and not a CLI this script knows.
UNKNOWN = ["foo", "mmx", "mimo"]

FAKE_CLI = """#!/bin/bash
REC="$FAKE_RECORD"
if [ "${FAKE_IGNORE_TERM:-0}" = "1" ]; then
    trap '' TERM
fi
# Publish the whole minimal record in one rename: the timeout path may read it
# before the delayed full record is written.
PID_RECORD="$(mktemp "${REC}.tmp.XXXXXX")"
printf '{"pid":%s}\n' "$$" > "$PID_RECORD"
mv "$PID_RECORD" "$REC"
if [ -n "${FAKE_READY:-}" ]; then
    : > "$FAKE_READY"
fi
STDIN_COPY="$(mktemp "${TMPDIR:-/tmp}/fake-stdin.XXXXXX")"
cat > "$STDIN_COPY"
sleep "${FAKE_RECORD_DELAY:-0}"
python3 - "$REC" "$FAKE_NAME" "$STDIN_COPY" "$$" "$0" "$@" <<'PYEOF'
import json, os, sys
rec, name, stdin_path, pid, path = sys.argv[1:6]
argv = sys.argv[6:]
try:
    with open(stdin_path) as fh:
        stdin = fh.read()
except OSError:
    stdin = ""
with open(rec, "w") as fh:
    json.dump(
        {
            "name": name,
            "path": path,
            "argv": argv,
            "cwd": os.getcwd(),
            "stdin": stdin,
            "pid": int(pid),
            # The group the Worker runs in: the launch's own group, which is
            # what the launch has to leave empty behind it.
            "pgid": os.getpgid(0),
            # The git environment the Worker was given (the deepseek object
            # quarantine).
            "git_env": {k: v for k, v in os.environ.items() if k.startswith("GIT_")},
        },
        fh,
    )
os.unlink(stdin_path)
PYEOF
if [ -n "${FAKE_STDOUT:-}" ]; then
    printf '%s\\n' "$FAKE_STDOUT"
fi
if [ -n "${FAKE_STDERR:-}" ]; then
    printf '%s\\n' "$FAKE_STDERR" >&2
fi
if [ "${FAKE_CHILD:-0}" = "1" ]; then
    ( trap '' TERM; while true; do echo "fake child tick"; sleep 0.2; done ) &
    printf '%s\\n' "$!" > "$REC.child"
fi
if [ "${FAKE_ORPHAN:-0}" = "1" ]; then
    # A child that leaves the worker's tree: the inner shell is re-parented
    # to init, so a walk of the worker's descendants cannot reach it. It
    # still holds the run's pipe open.
    ( ( trap '' TERM; while true; do echo "fake orphan tick"; sleep 0.2; done ) & echo "$!" > "$REC.orphan" ) &
fi
if [ "${FAKE_SPAWNER:-0}" = "1" ]; then
    # A worker that keeps forking children: one of them is always born
    # while the group is being stopped, so the reclaim has to collect the
    # group again instead of trusting one list.
    (
        trap '' TERM
        while true; do
            ( trap '' TERM; while true; do echo "spawned tick"; sleep 0.3; done ) &
            sleep 0.3
        done
    ) &
fi
if [ "${FAKE_REPLACE_ON_TERM:-0}" = "1" ]; then
    # A helper whose TERM handler forks a replacement that ignores TERM and
    # keeps writing the heartbeat: the escape a single-snapshot group sweep
    # misses. The helper is forked by the worker, so it is out of the
    # worker's tree the moment the worker exits, but it stays in the
    # launcher's process group. The worker itself exits as soon as the
    # helper is up, which is what sends the launch down the success path.
    (
        replace_on_term() {
            (
                trap '' TERM
                while true; do
                    printf 'x' >> "${PROBE_HEARTBEAT:-$REC.heartbeat}"
                    sleep 0.2
                done
            ) &
            printf '%s\\n' "$!" > "$REC.replacement"
            exit 0
        }
        trap replace_on_term TERM
        printf 'ready\\n' > "$REC.helper"
        while true; do
            sleep 5
        done
    ) &
    printf '%s\\n' "$!" > "$REC.helper_pid"
    while ! [ -f "$REC.helper" ]; do
        sleep 0.1
    done
fi
if [ "${FAKE_LOOP:-0}" = "1" ]; then
    while true; do
        echo "fake worker tick"
        sleep 0.2
    done
fi
sleep "${FAKE_SLEEP:-0}"
exit "${FAKE_EXIT:-0}"
"""


def _write_fake(directory: Path, name: str) -> Path:
    """A fake CLI that records how it was called."""
    path = directory / name
    path.write_text(FAKE_CLI, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    """A directory of fake CLIs, each recording what it was called with."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for _, cli, _ in WORKERS:
        _write_fake(bin_dir, cli)
    return bin_dir


@pytest.fixture
def run(tmp_path: Path, fake_bin: Path):
    """Run the script with the fake CLIs first on PATH."""
    log_dir = tmp_path / "logs"

    def _run(
        *args: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> subprocess.CompletedProcess[str]:
        record = tmp_path / "record.json"
        environment = {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(log_dir),
            "FAKE_RECORD": str(record),
            "FAKE_NAME": "fake",
        }
        if env:
            environment.update(env)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=str(cwd or tmp_path),
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    return _run


def _record(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "record.json").read_text(encoding="utf-8"))


def _wait_for_record(path: Path, timeout: float = 20.0) -> dict:
    """Wait for the fake CLI to record how it was called."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        time.sleep(0.1)
    raise AssertionError(f"the fake CLI never recorded a call: {path}")


def _is_running(pid: str) -> bool:
    return (
        subprocess.run(["kill", "-0", pid], capture_output=True, check=False).returncode
        == 0
    )


def _wait_for_exit(pid: str, timeout: float = 10.0) -> bool:
    """True once the pid is gone. A zombie still answers, so this waits."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_running(pid):
            return True
        time.sleep(0.1)
    return False


def test_the_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), f"{SCRIPT} is missing"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable"


def test_the_script_is_valid_bash() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("roster", "cli", "argv"), WORKERS, ids=[w[0] for w in WORKERS]
)
def test_each_worker_receives_its_own_argv(run, tmp_path, roster, cli, argv) -> None:
    result = run(roster, "do the thing")
    assert result.returncode == 0, result.stderr
    record = _record(tmp_path)
    assert record["name"] == "fake"
    actual = list(record["argv"])
    if roster == "doubao" and "--model" in actual:
        model_index = actual.index("--model")
        assert model_index == 4
        assert len(actual) > model_index + 1
        del actual[model_index : model_index + 2]
    expected = [
        item.replace("<PROMPT_FILE>", "<PROMPT_FILE>")
        if item == "<PROMPT_FILE>"
        else item
        for item in argv
    ]
    if "<PROMPT_FILE>" in expected:
        # grok reads the prompt from a file: the argv carries the path, and
        # the prompt is in that file.
        index = expected.index("<PROMPT_FILE>")
        path = Path(actual[index])
        expected[index] = str(path)
        assert path.read_text(encoding="utf-8").startswith("do the thing")
    if "<PROMPT>" in expected:
        index = expected.index("<PROMPT>")
        expected[index] = actual[index]
        assert "do the thing" in expected[index]
    if "<PWD>" in expected:
        index = expected.index("<PWD>")
        expected[index] = str(tmp_path)
    assert actual == expected


@pytest.mark.parametrize(
    ("roster", "flag"), [("kimi", "--session"), ("grok", "--resume")]
)
def test_kimi_and_grok_get_an_id_only_to_resume_one_they_created(
    run, tmp_path, roster, flag
) -> None:
    """N22: a caller-owned id never reaches either CLI on a fresh run."""
    result = run(
        roster, "do the thing", "--session-id=cli-made-1", "--session-mode=resume"
    )
    assert result.returncode == 0, result.stderr
    resumed = list(_record(tmp_path)["argv"])
    assert resumed[resumed.index(flag) + 1] == "cli-made-1"

    for args in (
        ("--session-id=caller-owned", "--session-mode=fresh"),
        ("--session-id=continue", "--session-mode=resume"),
        # Backstop: an id that could read as a flag or split into words is no
        # session at all, whatever the caller sent.
        ("--session-id=-rf bad id", "--session-mode=resume"),
        ("--session-id=--continue", "--session-mode=resume"),
        ("--session-id=a;b", "--session-mode=resume"),
    ):
        result = run(roster, "do the thing", *args)
        assert result.returncode == 0, result.stderr
        argv = list(_record(tmp_path)["argv"])
        for forbidden in ("--session", "--resume", "--session-id", "--continue"):
            assert forbidden not in argv, (args, argv)
        assert "caller-owned" not in argv and "continue" not in argv
        assert not any("bad id" in item or item == "a;b" for item in argv)


@pytest.mark.parametrize(
    ("roster", "cli", "argv"), WORKERS, ids=[w[0] for w in WORKERS]
)
def test_each_worker_runs_in_the_callers_working_directory(
    run, tmp_path, roster, cli, argv
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    result = run(roster, "do the thing", cwd=work)
    assert result.returncode == 0, result.stderr
    assert _record(tmp_path)["cwd"] == str(work)


@pytest.mark.parametrize(("alias", "roster"), ALIASES, ids=[a[0] for a in ALIASES])
def test_a_cli_alias_folds_onto_its_worker(run, tmp_path, alias, roster) -> None:
    result = run(alias, "do the thing")
    assert result.returncode == 0, result.stderr
    record = _record(tmp_path)
    assert record["name"] == "fake"
    expected_cli = next(cli for name, cli, _ in WORKERS if name == roster)
    assert Path(record["path"]).name == expected_cli


def test_the_prompt_carries_the_task_and_the_optional_lines(run, tmp_path) -> None:
    result = run("kimi", "the task", "--skills=a,b", "--impact-map=/tmp/im.json")
    assert result.returncode == 0, result.stderr
    prompt = _record(tmp_path)["argv"][1]
    assert "the task" in prompt
    assert "a,b" in prompt
    assert "/tmp/im.json" in prompt


def test_a_worker_that_reads_stdin_receives_the_prompt(run, tmp_path) -> None:
    result = run("minimax", "the task")
    assert result.returncode == 0, result.stderr
    assert "the task" in _record(tmp_path)["stdin"]


@pytest.mark.parametrize("model", UNKNOWN)
def test_an_unknown_model_is_refused_with_64(run, model) -> None:
    result = run(model, "the task")
    assert result.returncode == 64, result.stderr


@pytest.mark.parametrize("model", RESERVED)
def test_a_reserved_role_is_refused_with_65(run, model) -> None:
    result = run(model, "the task")
    assert result.returncode == 65, result.stderr
    assert "reserved role" in result.stderr


def test_a_reserved_role_never_starts_a_binary(run, tmp_path) -> None:
    result = run("codex", "the task")
    assert result.returncode == 65
    assert not (tmp_path / "record.json").exists(), "a reserved role started a CLI"


def test_a_missing_argument_is_refused_with_64(run) -> None:
    assert run("kimi").returncode == 64


def test_an_unknown_flag_is_refused_with_64(run) -> None:
    assert run("kimi", "the task", "--nope=1").returncode == 64


def test_an_extra_positional_argument_is_refused_with_64(run) -> None:
    assert run("kimi", "the task", "extra").returncode == 64


def test_a_missing_binary_is_refused_with_127(run) -> None:
    result = run("kimi", "the task", env={"NATIVE_CLI_KIMI_BIN": "/nonexistent/kimi"})
    assert result.returncode == 127, result.stderr


@pytest.mark.parametrize("code", [0, 1, 3])
def test_the_cli_exit_code_is_passed_through(run, code) -> None:
    result = run("kimi", "the task", env={"FAKE_EXIT": str(code)})
    assert result.returncode == code, result.stderr


def test_a_run_that_overruns_its_timeout_stops_with_124(run, tmp_path) -> None:
    result = run(
        "kimi",
        "the task",
        env={"CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT": "1", "FAKE_SLEEP": "20"},
        timeout=60,
    )
    assert result.returncode == 124, result.stderr
    leftover = subprocess.run(
        ["pgrep", "-f", str(tmp_path / "bin" / "kimi")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert leftover.stdout.strip() == "", "the stopped worker was left running"


def test_a_worker_that_ignores_sigterm_is_still_stopped_with_124(
    run, tmp_path, fake_bin
) -> None:
    """A CLI that traps SIGTERM must not keep the run, and the worktree, open.

    The stop is escalated to SIGKILL, so the run ends with 124 and the worker
    is gone: a worker that cannot be stopped would keep working in the
    worktree long after the line was reassigned. A private date shim starts
    the launcher's clock only after the fake has published its readiness.
    """
    ready = tmp_path / "fake.ready"
    date_shim = fake_bin / "date"
    date_shim.write_text(
        """#!/bin/bash
if [ "$#" -eq 1 ] && [ "$1" = "+%s" ]; then
    while ! [ -f "$FAKE_READY" ]; do
        sleep 0.01
    done
fi
exec /bin/date "$@"
""",
        encoding="utf-8",
    )
    date_shim.chmod(
        date_shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    result = run(
        "kimi",
        "the task",
        env={
            "CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT": "2",
            "CONDUCTOR_DISPATCH_LAUNCH_STOP_GRACE": "1",
            "FAKE_LOOP": "1",
            "FAKE_IGNORE_TERM": "1",
            "FAKE_RECORD_DELAY": "3",
            "FAKE_READY": str(ready),
        },
        timeout=60,
    )
    assert result.returncode == 124, result.stderr
    worker_pid = str(_record(tmp_path)["pid"])
    assert _wait_for_exit(worker_pid), "the stopped worker was left running"


def test_the_worker_is_stopped_when_the_launch_is_cancelled(tmp_path, fake_bin) -> None:
    """The launcher cancels by killing the script's process group.

    The worker runs in a group of its own, so it would survive that kill. It
    must not: an agent left running in the worktree keeps working on it after
    the line was reassigned.
    """
    environment = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(tmp_path / "logs"),
        "FAKE_RECORD": str(tmp_path / "record.json"),
        "FAKE_NAME": "fake",
        "FAKE_LOOP": "1",
        "CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT": "600",
    }
    process = subprocess.Popen(
        ["bash", str(SCRIPT), "kimi", "the task"],
        cwd=str(tmp_path),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        record = _wait_for_record(tmp_path / "record.json")
        # What kill_child_group does to a cancelled launch.
        os.killpg(process.pid, signal.SIGTERM)
        assert process.wait(timeout=60) != 0, "a cancelled run must not report success"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=30)
    assert _wait_for_exit(str(record["pid"])), "the cancelled worker was left running"


def test_a_worker_that_stalls_is_stopped_with_124(run, tmp_path) -> None:
    result = run(
        "kimi",
        "the task",
        env={
            "CONDUCTOR_DISPATCH_LAUNCH_STALL": "1",
            "CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT": "60",
            "FAKE_SLEEP": "20",
        },
        timeout=60,
    )
    assert result.returncode == 124, result.stderr
    logs = list((tmp_path / "logs").glob("*.log"))
    assert logs, "no run log was written"
    assert "[watchdog]" in logs[0].read_text(encoding="utf-8")


def test_the_log_directory_and_its_files_are_private(run, tmp_path) -> None:
    result = run("kimi", "the task")
    assert result.returncode == 0, result.stderr
    log_dir = tmp_path / "logs"
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    for path in log_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_the_run_log_is_named_after_the_worker_the_run_and_its_pid(
    run, tmp_path
) -> None:
    result = run("kimi", "the task")
    assert result.returncode == 0, result.stderr
    logs = list((tmp_path / "logs").glob("kimi-*.log"))
    assert len(logs) == 1, logs
    name = logs[0].name
    assert name.startswith("kimi-") and name.endswith(".log")
    stamp, _, pid = name[len("kimi-") : -len(".log")].rpartition("-")
    assert stamp and pid.isdigit(), name


def test_the_worker_output_reaches_both_the_log_and_stdout(run, tmp_path) -> None:
    result = run(
        "kimi",
        "the task",
        env={"FAKE_STDOUT": "worker says hello", "FAKE_STDERR": "worker says more"},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    logs = list((tmp_path / "logs").glob("*.log"))
    assert logs
    # The fake really wrote something: an empty string on both sides would
    # compare equal without proving that the output was forwarded. The
    # worker's stderr is folded into its stdout on the way to the log, so
    # both lines arrive there.
    assert "worker says hello" in result.stdout
    assert "worker says more" in result.stdout
    assert result.stderr == "", result.stderr
    assert logs[0].read_text(encoding="utf-8") == result.stdout


def test_the_script_holds_no_credential_and_no_reserved_launch() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("api_key", "API_KEY", "api-key", "TOKEN", "SECRET", ".config/"):
        assert forbidden not in text, forbidden
    for line in text.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("codex "), line
        assert not stripped.startswith("claude "), line


def test_the_launcher_seam_still_works_end_to_end(run, tmp_path, monkeypatch) -> None:
    """``ScriptLauncher`` calls the script; a real launch must succeed."""
    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.types import Line

    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("FAKE_NAME", "fake")
    monkeypatch.setenv("FAKE_STDOUT", "worker says hello")
    monkeypatch.setenv("FAKE_STDERR", "worker says more")
    monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR", str(tmp_path / "logs"))

    line = Line(id="L1", name="the line", task="do the thing")
    launcher = ScriptLauncher(
        tmp_path / "runs",
        script=SCRIPT,
        cwd=tmp_path,
    )
    argv = launcher.argv(line, "kimi", [])
    assert argv[0] == str(SCRIPT)
    assert argv[1] == "kimi"
    assert argv[2] == "do the thing"

    run_id = asyncio.run(launcher.launch(line, "kimi", []))
    assert run_id
    # The launch really ran: the run log holds what the Worker wrote on both
    # of its streams, and the exit code is on disk. An empty log would not
    # prove that anything was collected.
    log = launcher.log_path(run_id)
    assert log.is_file()
    collected = log.read_text(encoding="utf-8")
    assert collected.strip(), "the launch collected no output"
    assert "worker says hello" in collected
    assert "worker says more" in collected
    exit_file = log.with_suffix(".exit")
    assert exit_file.is_file()
    assert exit_file.read_text(encoding="utf-8") == "0"
    assert _record(tmp_path)["argv"][0] == "-p"


def test_the_launcher_reports_a_failed_worker(run, tmp_path, monkeypatch) -> None:
    from src.tools.auto_dispatch.execution import LaunchError, ScriptLauncher
    from src.tools.auto_dispatch.types import Line

    line = Line(id="L1", name="the line", task="do the thing")
    launcher = ScriptLauncher(
        tmp_path / "runs",
        script=SCRIPT,
        cwd=tmp_path,
    )
    # monkeypatch, not os.environ: PATH is read by the whole process, and
    # replacing it outright would leak into every test that runs after this
    # one in the same session.
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("FAKE_NAME", "fake")
    monkeypatch.setenv("FAKE_EXIT", "2")
    monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR", str(tmp_path / "logs"))
    with pytest.raises(LaunchError):
        asyncio.run(launcher.launch(line, "kimi", []))


# --------------------------------------------------------- the lifecycle of a run


def _group_members(pgid: str) -> list[str]:
    """The pids in a process group.

    ``ps`` right-aligns its columns to the widest value it prints, so the
    pid of a process with fewer digits than the widest pid on the machine
    is padded with spaces, and the gap between two columns is a tab on
    some systems. The line is therefore split on whitespace.

    A row that is not exactly two integers is not a row this helper
    understands, and it is skipped: a row it cannot read must not be
    reported as a member that is not there.
    """
    if not pgid.isdigit():
        return []
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,pgid="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    members: list[str] = []
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        pid, group = fields
        if not pid.isdigit() or not group.isdigit():
            continue
        if int(group) == int(pgid):
            members.append(pid)
    return members


def _live_group_members(pgid: str) -> list[str]:
    """The pids in a process group that are still running.

    A zombie is not running: it has stopped, and only the collection of its
    exit is left. ``ps`` prints it in the group it died in, so it is left
    out here — this answers "is anything of the group still alive", which is
    the question the launch has to answer before it returns.
    """
    if not pgid.isdigit():
        return []
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,pgid=,stat="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    live: list[str] = []
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        pid, group, state = fields
        if not pid.isdigit() or not group.isdigit():
            continue
        if state.startswith("Z"):
            continue
        if int(group) == int(pgid):
            live.append(pid)
    return live


def test_the_group_listing_is_read_whatever_ps_pads_it_with(monkeypatch) -> None:
    """The listing ps prints is padded, and its columns are not always spaced.

    ``ps`` right-aligns its columns to the widest pid on the machine, so a
    pid with fewer digits is padded, and the gap between the two columns is
    a tab on some systems. A row that is not exactly two integers is not a
    member and is skipped, rather than being read as one.
    """
    listing = "\n".join(
        [
            "  4321    4321",  # four digits, padded on both sides
            "54321   54321",  # five digits
            "654321  654321",  # six digits
            "  777\t  777",  # a tab between the columns
            "  888   888   888",  # three columns: not a row
            "  999   x999",  # not an integer: not a row
            "1010",  # one column: not a row
            "  111  111  ",  # trailing spaces: still two integers
            "",
        ]
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=listing),
    )
    assert _group_members("4321") == ["4321"]
    assert _group_members("54321") == ["54321"]
    assert _group_members("654321") == ["654321"]
    assert _group_members("777") == ["777"]
    assert _group_members("111") == ["111"]
    # A row that is not two integers is skipped, not guessed at.
    assert _group_members("888") == []
    assert _group_members("999") == []
    assert _group_members("1010") == []
    assert _group_members("222") == []
    # A group that is not a number matches nothing.
    assert _group_members("not-a-group") == []
    assert _group_members("") == []


def _run_in_own_group(
    tmp_path: Path, fake_bin: Path, environment: dict, timeout: int = 60
):
    """Run the script the way the dispatcher does: in a process group of its own."""
    process = subprocess.Popen(
        ["bash", str(SCRIPT), "kimi", "the task"],
        cwd=str(tmp_path),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise AssertionError("the run never ended")
    return process.returncode, out, err


def test_a_worker_that_leaves_an_orphan_behind_does_not_hang_the_run(
    tmp_path, fake_bin
) -> None:
    """A child the worker detached is re-parented to init.

    A walk of the worker's descendants cannot reach such a child, and it
    still holds the run's pipe open. The run has to end anyway, and the
    orphan must not be left running in the worktree.
    """
    environment = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(tmp_path / "logs"),
        "FAKE_RECORD": str(tmp_path / "record.json"),
        "FAKE_NAME": "fake",
        "FAKE_ORPHAN": "1",
        "FAKE_LOOP": "1",
        "CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT": "3",
        "CONDUCTOR_DISPATCH_LAUNCH_STOP_GRACE": "1",
    }
    code, _, _ = _run_in_own_group(tmp_path, fake_bin, environment)
    assert code == 124
    orphan_pid = (tmp_path / "record.json.orphan").read_text(encoding="utf-8").strip()
    assert orphan_pid
    assert _wait_for_exit(orphan_pid), "the orphan was left running"


def test_a_worker_that_leaves_a_child_behind_keeps_its_own_exit_code(
    tmp_path, fake_bin
) -> None:
    """A worker that leaves a child behind must not hang its own successful run."""
    environment = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(tmp_path / "logs"),
        "FAKE_RECORD": str(tmp_path / "record.json"),
        "FAKE_NAME": "fake",
        "FAKE_CHILD": "1",
        "CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT": "60",
    }
    code, _, _ = _run_in_own_group(tmp_path, fake_bin, environment)
    assert code == 0
    child_pid = (tmp_path / "record.json.child").read_text(encoding="utf-8").strip()
    assert child_pid
    assert _wait_for_exit(child_pid), "the child was left running"


def test_a_child_forked_while_the_group_was_stopped_is_reaped(
    tmp_path, fake_bin
) -> None:
    """A process that appears during the stop must not outlive the run.

    The reclaim collects the group again before the kill: a child that
    forked while the group was being stopped was not in the first list, and
    it would otherwise keep running in the worktree.
    """
    environment = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(tmp_path / "logs"),
        "FAKE_RECORD": str(tmp_path / "record.json"),
        "FAKE_NAME": "fake",
        "FAKE_LOOP": "1",
        "FAKE_SPAWNER": "1",
        "CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT": "3",
        "CONDUCTOR_DISPATCH_LAUNCH_STOP_GRACE": "1",
    }
    code, _, _ = _run_in_own_group(tmp_path, fake_bin, environment)
    assert code == 124
    leftover = subprocess.run(
        ["pgrep", "-f", str(fake_bin / "kimi")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert leftover.stdout.strip() == "", "a child was left running"


def test_a_replacement_forked_in_the_term_grace_is_swept(tmp_path, fake_bin) -> None:
    """A TERM handler that forks a TERM-ignoring replacement must not escape.

    The helper TERMed by the sweep forks a replacement that ignores TERM and
    keeps writing a heartbeat. The sweep has to find it by re-enumerating the
    group — a snapshot taken before the TERM would miss it — and the launch
    only counts as a success once the replacement and everything it spawned
    are gone, with the heartbeat still after the launcher returned.
    """
    environment = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(tmp_path / "logs"),
        "FAKE_RECORD": str(tmp_path / "record.json"),
        "FAKE_NAME": "fake",
        "FAKE_REPLACE_ON_TERM": "1",
        "CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT": "60",
        "CONDUCTOR_DISPATCH_LAUNCH_STOP_GRACE": "2",
    }
    code, _, err = _run_in_own_group(tmp_path, fake_bin, environment)
    assert code == 0, err
    assert err == "", err
    replacement = (
        (tmp_path / "record.json.replacement").read_text(encoding="utf-8").strip()
    )
    assert replacement, "the helper never forked its replacement"
    # At the moment the launcher returned, not a moment later: the
    # replacement is already gone and the launch group holds nothing that is
    # still running. A launch that returns while its group still has a live
    # member has not finished its job, even if that member dies right after.
    record = _record(tmp_path)
    assert not _is_running(replacement), (
        "the replacement was still running when the launcher returned"
    )
    assert _live_group_members(str(record["pgid"])) == [], (
        "the launch group still had live members when the launcher returned"
    )
    heartbeat = tmp_path / "record.json.heartbeat"
    at_return = heartbeat.stat().st_size if heartbeat.exists() else 0
    time.sleep(1.0)
    after = heartbeat.stat().st_size if heartbeat.exists() else 0
    assert after == at_return, "the heartbeat grew after the launch returned"


def test_a_cancelled_launch_leaves_no_worker_behind(run, tmp_path, monkeypatch) -> None:
    """A cancelled launch must take the Worker, its children, and the helpers.

    The dispatcher cancels by SIGKILLing the process group the launch runs in.
    A Worker in a process group of its own survives that kill and keeps
    working in the worktree while the line is already reassigned, so the
    launch must not put the Worker outside that boundary.
    """
    from src.tools.auto_dispatch.execution import ScriptLauncher, _run_command
    from src.tools.auto_dispatch.types import Line

    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("FAKE_NAME", "fake")
    monkeypatch.setenv("FAKE_LOOP", "1")
    monkeypatch.setenv("FAKE_CHILD", "1")
    monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR", str(tmp_path / "logs"))

    line = Line(id="L1", name="the line", task="do the thing")
    launcher = ScriptLauncher(tmp_path / "runs", script=SCRIPT, cwd=tmp_path)
    argv = launcher.argv(line, "kimi", [])

    async def _cancel() -> list[str]:
        task = asyncio.create_task(
            _run_command(argv, tmp_path, launcher.log_path("L1")),
        )
        for _ in range(100):
            if (tmp_path / "record.json").exists():
                break
            await asyncio.sleep(0.1)
        record = _record(tmp_path)
        pgid = subprocess.run(
            ["ps", "-o", "pgid=", "-p", str(record["pid"])],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        assert pgid, "the worker has no process group"
        members = _group_members(pgid)
        # The Worker, the child it forked, the script and its helpers all
        # share the launcher's group: that is what a cancellation reclaims.
        assert len(members) >= 4, members
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return members

    members = asyncio.run(_cancel())
    for pid in members:
        assert _wait_for_exit(pid, timeout=15.0), f"pid {pid} was left running"


def test_a_silent_worker_that_finishes_is_not_a_stall(run, tmp_path) -> None:
    """A worker that finishes quietly is a completed run, not a stall.

    The watchdog measures silence in the log. A worker that says nothing and
    finishes is not silent-and-stuck: the completion wins over the watchdog.
    The stall window also covers the worker's start, so it is wide enough
    here for a worker that is slow to say its first word.
    """
    result = run(
        "kimi",
        "the task",
        env={
            "CONDUCTOR_DISPATCH_LAUNCH_STALL": "3",
            "CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT": "30",
        },
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_a_silent_worker_that_fails_keeps_its_own_exit_code(run, tmp_path) -> None:
    """A silent failure is the worker's own code, not a stall."""
    result = run(
        "kimi",
        "the task",
        env={
            "FAKE_EXIT": "3",
            "CONDUCTOR_DISPATCH_LAUNCH_STALL": "3",
            "CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT": "30",
        },
        timeout=60,
    )
    assert result.returncode == 3, result.stderr


# ------------------------------------------------------------- the GLM overrides


def _override_binary(tmp_path: Path, name: str, label: str) -> str:
    """A fake binary that identifies itself, to prove which one was used."""
    directory = tmp_path / f"override-{label}"
    directory.mkdir()
    binary = _write_fake(directory, name)
    return str(binary)


@pytest.mark.parametrize("model", ["glm", "zcode", "zcode-cli"])
def test_the_glm_worker_is_reached_by_every_entry_name(
    run, fake_bin, tmp_path, model
) -> None:
    """The roster name and both CLI aliases serve the same Worker.

    The official zcode CLI is the default (rev19), so every entry name lands
    on it and receives the official grammar, not the clean-room flags.
    """
    result = run(model, "the task", timeout=60)
    assert result.returncode == 0, result.stderr
    record = _record(tmp_path)
    assert Path(record["path"]) == fake_bin / "zcode"
    _assert_official(record["argv"])


def test_the_glm_worker_falls_back_to_zcode_when_zcode_cli_is_absent(tmp_path) -> None:
    """A host with only the official CLI still launches the GLM worker."""
    bin_dir = tmp_path / "only-zcode"
    bin_dir.mkdir()
    binary = _write_fake(bin_dir, "zcode")
    log_dir = tmp_path / "logs"
    record = tmp_path / "record.json"
    result = subprocess.run(
        ["bash", str(SCRIPT), "glm", "the task"],
        cwd=str(tmp_path),
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(log_dir),
            "FAKE_RECORD": str(record),
            "FAKE_NAME": "fake",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    record = _record(tmp_path)
    assert record["path"] == str(binary)
    assert "--model" not in record["argv"]
    assert "--max-turns" not in record["argv"]
    assert "--provider" not in record["argv"]
    assert "--surface" in record["argv"]


# Capability-fixed fakes: each accepts exactly one grammar, whatever its own
# file name is, so a wrong classification by the launcher fails here instead of
# being papered over by the fake switching grammar from its argv[0].
CLEAN_ROOM_STRICT_GLM = """#!/bin/bash
for arg in "$@"; do
    case "$arg" in
        --surface)
            echo "unknown flag: $arg" >&2
            exit 64
            ;;
    esac
done
python3 - "$FAKE_RECORD" "$0" "$@" <<'PY'
import json, sys
record, path = sys.argv[1], sys.argv[2]
with open(record, "w") as handle:
    json.dump({"path": path, "argv": sys.argv[3:]}, handle)
PY
exit 0
"""

OFFICIAL_STRICT_GLM = """#!/bin/bash
for arg in "$@"; do
    case "$arg" in
        --model|--max-turns|--provider|-p)
            echo "unknown flag: $arg" >&2
            exit 64
            ;;
    esac
done
python3 - "$FAKE_RECORD" "$0" "$@" <<'PY'
import json, sys
record, path = sys.argv[1], sys.argv[2]
with open(record, "w") as handle:
    json.dump({"path": path, "argv": sys.argv[3:]}, handle)
PY
exit 0
"""


CLEAN_ROOM_FLAGS = ("--model", "--max-turns", "--provider", "-p")


def _strict_binary(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _clean_room_binary(directory: Path, name: str) -> Path:
    return _strict_binary(directory, name, CLEAN_ROOM_STRICT_GLM)


def _official_binary(directory: Path, name: str) -> Path:
    return _strict_binary(directory, name, OFFICIAL_STRICT_GLM)


def _launch_glm(
    tmp_path: Path, bin_dir: Path, **env: str
) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(tmp_path / "logs"),
        "FAKE_RECORD": str(tmp_path / "record.json"),
    }
    environment.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), "glm", "the task"],
        cwd=str(tmp_path),
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _assert_official(argv: list[str]) -> None:
    assert "--prompt" in argv
    assert "--surface" in argv
    assert argv[argv.index("--surface") + 1] == "terminal"
    for flag in CLEAN_ROOM_FLAGS:
        assert flag not in argv


def _assert_clean_room(argv: list[str]) -> None:
    assert argv[:2] == ["--prompt", "the task"]
    assert "--model" in argv
    assert "--max-turns" in argv
    assert "--surface" not in argv
    assert "--provider" not in argv
    assert "-p" not in argv


def test_only_the_official_glm_cli_rejects_clean_room_flags(tmp_path) -> None:
    """Official-only host: the plain zcode shell script runs, never --model."""
    binary = _official_binary(tmp_path / "bin", "zcode")
    result = _launch_glm(tmp_path, binary.parent)
    assert result.returncode == 0, result.stderr
    record = _record(tmp_path)
    assert record["path"] == str(binary)
    _assert_official(record["argv"])


def test_an_explicit_clean_room_override_receives_its_own_flags(tmp_path) -> None:
    """zcode-cli is no longer chosen from PATH; only an override selects it."""
    bin_dir = tmp_path / "bin"
    official = _official_binary(bin_dir, "zcode")
    clean = _clean_room_binary(tmp_path / "override", "zcode-cli")
    result = _launch_glm(tmp_path, bin_dir, NATIVE_CLI_ZCODE_BIN=str(clean))
    assert result.returncode == 0, result.stderr
    record = _record(tmp_path)
    assert record["path"] == str(clean)
    assert record["path"] != str(official)
    _assert_clean_room(record["argv"])


def test_both_zcode_clis_on_path_choose_the_official_zcode(tmp_path) -> None:
    """With both on PATH and no override, the default is the official zcode."""
    bin_dir = tmp_path / "bin"
    official = _official_binary(bin_dir, "zcode")
    clean = _clean_room_binary(bin_dir, "zcode-cli")
    result = _launch_glm(tmp_path, bin_dir)
    assert result.returncode == 0, result.stderr
    record = _record(tmp_path)
    assert record["path"] == str(official)
    assert record["path"] != str(clean)
    _assert_official(record["argv"])


def test_an_official_override_does_not_receive_clean_room_flags(tmp_path) -> None:
    """An explicit official binary wins, and its grammar wins with it."""
    bin_dir = tmp_path / "bin"
    _clean_room_binary(bin_dir, "zcode-cli")
    official = _official_binary(tmp_path / "override", "zcode")
    result = _launch_glm(tmp_path, bin_dir, NATIVE_CLI_ZCODE_BIN=str(official))
    assert result.returncode == 0, result.stderr
    record = _record(tmp_path)
    assert record["path"] == str(official)
    _assert_official(record["argv"])


def test_a_zcode_cli_link_is_clean_room_by_its_invocation_name(tmp_path) -> None:
    """A link named zcode-cli to cli.js is clean-room: the invocation name wins.

    The two-step rule classifies before resolving symlinks, so the resolved
    target (cli.js) does not demote it to the official grammar. Resolving first
    would pass --surface and this fake (which also follows the two-step rule)
    would refuse it with 64.
    """
    # The target keeps the clean-room capability while its name (cli.js)
    # is not zcode-cli; only the invocation-name fast path makes it good.
    target = _clean_room_binary(tmp_path / "lib", "cli.js")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = bin_dir / "zcode-cli"
    link.symlink_to(target)
    assert link.is_symlink() and target.name == "cli.js"

    result = _launch_glm(tmp_path, bin_dir, NATIVE_CLI_ZCODE_BIN=str(link))
    assert result.returncode == 0, result.stderr
    record = _record(tmp_path)
    assert Path(record["path"]) == link
    assert "--surface" not in record["argv"]
    _assert_clean_room(record["argv"])


def test_a_symlinked_override_resolves_to_the_clean_room_grammar(tmp_path) -> None:
    """glm-custom -> zcode-cli is clean-room by its resolved target.

    The invocation name (glm-custom) does not decide; Path.resolve does, exactly
    like ZCodeCliAdapter._is_clean_room_cli.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    target = _clean_room_binary(bin_dir, "zcode-cli")
    override_dir = tmp_path / "override"
    override_dir.mkdir()
    override = override_dir / "glm-custom"
    override.symlink_to(target)
    assert override.is_symlink()

    result = _launch_glm(tmp_path, bin_dir, NATIVE_CLI_ZCODE_BIN=str(override))
    assert result.returncode == 0, result.stderr
    record = _record(tmp_path)
    assert Path(record["path"]).resolve() == target.resolve()
    _assert_clean_room(record["argv"])


def test_the_glm_worker_uses_the_zcode_binary_override(run, tmp_path) -> None:
    """The registry keys GLM as ``zcode``, so that is the override to use."""
    binary = _override_binary(tmp_path, "zcode-override", "zcode")
    result = run(
        "glm",
        "the task",
        env={"NATIVE_CLI_ZCODE_BIN": binary},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    # The binary that actually ran, not the name the test gave it: a script
    # that ignored the variable would still pass a name assertion.
    assert _record(tmp_path)["path"] == binary


def test_the_glm_worker_still_honours_the_legacy_zhipu_override(run, tmp_path) -> None:
    """The same CLI is registered under both names, so the old one still works."""
    binary = _override_binary(tmp_path, "zhipu-override", "zhipu")
    result = run(
        "glm",
        "the task",
        env={"NATIVE_CLI_ZHIPU_BIN": binary},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert _record(tmp_path)["path"] == binary


def test_the_zcode_override_wins_over_the_legacy_zhipu_override(run, tmp_path) -> None:
    """With both set, the registry's name wins."""
    zcode_binary = _override_binary(tmp_path, "zcode-override", "zcode")
    zhipu_binary = _override_binary(tmp_path, "zhipu-override", "zhipu")
    result = run(
        "glm",
        "the task",
        env={
            "NATIVE_CLI_ZCODE_BIN": zcode_binary,
            "NATIVE_CLI_ZHIPU_BIN": zhipu_binary,
        },
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert _record(tmp_path)["path"] == zcode_binary


# ---------------------------------------------------------------- serial lock


def _serial_env(
    tmp_path: Path, fake_bin: Path, record: Path, **extra: str
) -> dict[str, str]:
    environment = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(tmp_path / "logs"),
        "FAKE_RECORD": str(record),
        "FAKE_NAME": "fake",
    }
    environment.update(extra)
    return environment


def _spawn(
    tmp_path: Path, env: dict[str, str], model: str = "glm"
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        ["bash", str(SCRIPT), model, "the task"],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def test_two_glm_launches_never_run_their_workers_at_the_same_time(
    tmp_path, fake_bin
) -> None:
    first = _spawn(
        tmp_path, _serial_env(tmp_path, fake_bin, tmp_path / "a.json", FAKE_SLEEP="3")
    )
    _wait_for_record(tmp_path / "a.json")
    second = _spawn(tmp_path, _serial_env(tmp_path, fake_bin, tmp_path / "b.json"))
    try:
        time.sleep(1.5)
        assert not (tmp_path / "b.json").exists(), (
            "the second glm worker started while the first ran"
        )
        assert first.wait(timeout=60) == 0
        out, err = second.communicate(timeout=60)
        assert second.returncode == 0, err
        assert "[serial] waiting for another glm worker" in out
    finally:
        for process in (first, second):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
    # The first worker records its call, then sleeps 3 s: the second may only
    # start once that sleep is over.
    started_a = (tmp_path / "a.json").stat().st_mtime
    started_b = (tmp_path / "b.json").stat().st_mtime
    assert started_b - started_a >= 2.5, "the two glm workers overlapped"
    assert not (tmp_path / "logs" / "locks" / "glm.lock").exists(), (
        "the lock was not given back"
    )


def test_launches_with_different_log_dirs_share_an_explicit_lock_dir(
    tmp_path, fake_bin
) -> None:
    # The harness eval keeps its own log directory but must queue behind the
    # live launcher's per-provider lock: the lock dir is set on its own.
    shared = tmp_path / "live-locks"
    first = _spawn(
        tmp_path,
        _serial_env(
            tmp_path,
            fake_bin,
            tmp_path / "a.json",
            FAKE_SLEEP="3",
            CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR=str(tmp_path / "logs-live"),
            CONDUCTOR_DISPATCH_SERIAL_LOCK_DIR=str(shared),
        ),
    )
    _wait_for_record(tmp_path / "a.json")
    second = _spawn(
        tmp_path,
        _serial_env(
            tmp_path,
            fake_bin,
            tmp_path / "b.json",
            CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR=str(tmp_path / "logs-eval"),
            CONDUCTOR_DISPATCH_SERIAL_LOCK_DIR=str(shared),
        ),
    )
    try:
        time.sleep(1.5)
        assert not (tmp_path / "b.json").exists(), (
            "the second worker ignored the shared lock"
        )
        assert first.wait(timeout=60) == 0
        out, err = second.communicate(timeout=60)
        assert second.returncode == 0, err
        assert "[serial] waiting for another glm worker" in out
    finally:
        for process in (first, second):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
    started_a = (tmp_path / "a.json").stat().st_mtime
    started_b = (tmp_path / "b.json").stat().st_mtime
    assert started_b - started_a >= 2.5, "the two glm workers overlapped"
    assert not (tmp_path / "logs-live" / "locks").exists()
    assert not (tmp_path / "logs-eval" / "locks").exists()
    assert not (shared / "glm.lock").exists(), "the lock was not given back"


def test_a_lock_left_by_a_dead_launch_is_taken_over(run, tmp_path) -> None:
    lock = tmp_path / "logs" / "locks" / "glm.lock"
    lock.mkdir(parents=True)
    dead = subprocess.Popen(["true"])
    dead.wait()
    (lock / "pid").write_text(f"{dead.pid}\n", encoding="utf-8")
    result = run("glm", "the task", timeout=60)
    assert result.returncode == 0, result.stderr
    assert not lock.exists()


def test_a_launch_that_waits_past_the_bound_gives_up_as_rate_limited(
    run, tmp_path
) -> None:
    lock = tmp_path / "logs" / "locks" / "glm.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    result = run(
        "glm",
        "the task",
        env={"CONDUCTOR_DISPATCH_SERIAL_WAIT_SECONDS": "2"},
        timeout=60,
    )
    assert result.returncode == 75, result.stderr
    assert not (tmp_path / "record.json").exists(), (
        "the worker started without the lock"
    )
    assert lock.exists(), "a waiting launch must not remove a live holder's lock"


def test_a_provider_outside_the_serial_list_takes_no_lock(run, tmp_path) -> None:
    lock = tmp_path / "logs" / "locks" / "kimi.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    result = run(
        "kimi", "the task", env={"CONDUCTOR_DISPATCH_SERIAL_WAIT_SECONDS": "1"}
    )
    assert result.returncode == 0, result.stderr


def test_an_empty_serial_list_turns_the_lock_off(run, tmp_path) -> None:
    lock = tmp_path / "logs" / "locks" / "glm.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    result = run(
        "glm",
        "the task",
        env={
            "CONDUCTOR_DISPATCH_SERIAL_PROVIDERS": "",
            "CONDUCTOR_DISPATCH_SERIAL_WAIT_SECONDS": "1",
        },
    )
    assert result.returncode == 0, result.stderr


def test_a_cancelled_waiting_launch_leaves_the_holders_lock_alone(
    tmp_path, fake_bin
) -> None:
    lock = tmp_path / "logs" / "locks" / "glm.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    waiting = _spawn(tmp_path, _serial_env(tmp_path, fake_bin, tmp_path / "w.json"))
    time.sleep(1.5)
    os.killpg(waiting.pid, signal.SIGTERM)
    assert waiting.wait(timeout=30) != 0
    assert (lock / "pid").read_text(encoding="utf-8").strip() == str(os.getpid())


def test_a_zero_heartbeat_does_not_break_the_wait(run, tmp_path) -> None:
    lock = tmp_path / "logs" / "locks" / "glm.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    result = run(
        "glm",
        "the task",
        env={
            "CONDUCTOR_DISPATCH_SERIAL_WAIT_SECONDS": "2",
            "CONDUCTOR_DISPATCH_SERIAL_HEARTBEAT_SECONDS": "0",
        },
    )
    assert result.returncode == 75, result.stderr


def test_a_takeover_never_removes_a_lock_that_changed_hands(tmp_path) -> None:
    # The guarded takeover reads the holder again: a lock that a live launch
    # took in the meantime is left alone.
    lock_root = tmp_path / "locks"
    lock = lock_root / "glm.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("take_over_stale_lock() {")
    end = text.index("\n}\n", start) + 3
    probe = (
        "set -euo pipefail\n"
        f"lock_root={lock_root}\ncanonical=glm\nlock={lock}\n"
        "path_mtime() { date +%s; }\n"
        + text[start:end]
        + "take_over_stale_lock 999999\n"
    )
    done = subprocess.run(
        ["bash", "-c", probe], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert (lock / "pid").read_text(encoding="utf-8").strip() == str(os.getpid())
    assert not (lock_root / "glm.takeover").exists()


# ------------------------------------------- N72: deepseek git dirs in sandbox
#
# A deepseek Worker runs under dsh's workspace-write sandbox, so the launcher
# grants it the git metadata a commit on the line's own branch writes (the
# admin dir, the branch's ref and reflog directories) and nothing else of the
# common dir. New objects go to a per-launch quarantine, which the launcher
# moves into the real store once the Worker is gone.

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

LINE_BRANCH = "feature/auto-dispatch-d1/line-0"
BRANCH_DIR = "refs/heads/feature/auto-dispatch-d1"


def _git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        **_GIT_IDENTITY,
    }


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_git_env(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check:
        assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _linked_worktree(
    tmp_path: Path, branch: str = LINE_BRANCH
) -> tuple[Path, Path, Path]:
    """A throwaway repo and a linked worktree of it on a dispatch line branch.

    Returns the worktree, its admin dir and the common dir, physical paths.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "base")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", branch, str(worktree))
    admin = Path(os.path.realpath(_git(worktree, "rev-parse", "--absolute-git-dir")))
    common = Path(os.path.realpath(repo / ".git"))
    assert admin != common and admin.parent.parent == common
    return Path(os.path.realpath(worktree)), admin, common


def _add_dirs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, item in enumerate(argv) if item == "--add-dir"]


def _without_add_dirs(argv: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for item in argv:
        if skip:
            skip = False
            continue
        if item == "--add-dir":
            skip = True
            continue
        out.append(item)
    return out


def _quarantines(common: Path) -> list[Path]:
    root = common / "conductor-objects"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() or p.is_symlink())


def test_deepseek_gets_only_the_line_metadata_of_a_linked_worktree(
    run, tmp_path
) -> None:
    worktree, admin, common = _linked_worktree(tmp_path)
    result = run("deepseek", "do the thing", cwd=worktree)
    assert result.returncode == 0, result.stderr
    argv = list(_record(tmp_path)["argv"])
    grants = _add_dirs(argv)
    assert grants[:3] == [
        str(admin),
        str(common / BRANCH_DIR),
        str(common / "logs" / BRANCH_DIR),
    ]
    assert len(grants) == 4
    quarantine = Path(grants[3])
    assert quarantine.parent == common / "conductor-objects"
    assert quarantine.name.startswith(f"{admin.name}-")
    # Nothing else of the common dir: not the dir itself, not objects/,
    # hooks/, config, info/ or refs/heads as a whole.
    for denied in (
        common,
        common / "objects",
        common / "hooks",
        common / "refs" / "heads",
    ):
        assert str(denied) not in grants
    # The rest of the argv is the one deepseek has always had, in order, and
    # the grants come before the stdin marker.
    assert _without_add_dirs(argv) == ["--profile", "headless", "-"]
    assert argv[-1] == "-"
    # The ref and reflog directories were made on the host; the empty
    # quarantine is gone after the run.
    assert (common / BRANCH_DIR).is_dir() and (common / "logs" / BRANCH_DIR).is_dir()
    assert not quarantine.exists()


def test_deepseek_git_writes_new_objects_to_the_quarantine(run, tmp_path) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    result = run("deepseek", "do the thing", cwd=worktree)
    assert result.returncode == 0, result.stderr
    record = _record(tmp_path)
    quarantine = _add_dirs(list(record["argv"]))[-1]
    env = record["git_env"]
    assert env["GIT_OBJECT_DIRECTORY"] == quarantine
    assert env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] == str(common / "objects")
    assert "'gc.auto'='0'" in env["GIT_CONFIG_PARAMETERS"]
    assert "'maintenance.auto'='false'" in env["GIT_CONFIG_PARAMETERS"]


def test_deepseek_gets_the_git_dirs_from_a_subdirectory_too(run, tmp_path) -> None:
    worktree, admin, common = _linked_worktree(tmp_path)
    sub = worktree / "pkg"
    sub.mkdir()
    result = run("deepseek", "do the thing", cwd=sub)
    assert result.returncode == 0, result.stderr
    grants = _add_dirs(list(_record(tmp_path)["argv"]))
    assert grants[:3] == [
        str(admin),
        str(common / BRANCH_DIR),
        str(common / "logs" / BRANCH_DIR),
    ]


def test_a_branch_directly_in_refs_heads_gets_no_ref_grant(run, tmp_path) -> None:
    # Its directory is refs/heads itself, which holds every other branch.
    worktree, admin, common = _linked_worktree(tmp_path, branch="line")
    result = run("deepseek", "do the thing", cwd=worktree)
    assert result.returncode == 0, result.stderr
    grants = _add_dirs(list(_record(tmp_path)["argv"]))
    assert grants[0] == str(admin)
    assert len(grants) == 2 and Path(grants[1]).parent == common / "conductor-objects"
    assert "sits directly in refs/heads" in result.stderr


def test_deepseek_grants_nothing_in_a_main_worktree(run, tmp_path) -> None:
    # A main worktree's git dir is the whole common dir.
    _linked_worktree(tmp_path)
    result = run("deepseek", "do the thing", cwd=tmp_path / "repo")
    assert result.returncode == 0, result.stderr
    record = _record(tmp_path)
    assert list(record["argv"]) == ["--profile", "headless", "-"]
    assert "GIT_OBJECT_DIRECTORY" not in record["git_env"]


def test_deepseek_gets_nothing_extra_outside_a_git_worktree(run, tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = run(
        "deepseek",
        "do the thing",
        cwd=plain,
        env={"GIT_CEILING_DIRECTORIES": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    record = _record(tmp_path)
    assert list(record["argv"]) == ["--profile", "headless", "-"]
    assert "GIT_OBJECT_DIRECTORY" not in record["git_env"]


@pytest.mark.parametrize("roster", ["kimi", "grok", "cursor", "stepfun", "minimax"])
def test_only_deepseek_gets_git_dir_grants(run, tmp_path, roster) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    result = run(roster, "do the thing", cwd=worktree)
    assert result.returncode == 0, result.stderr
    record = _record(tmp_path)
    assert "--add-dir" not in record["argv"]
    assert "GIT_OBJECT_DIRECTORY" not in record["git_env"]
    assert _quarantines(common) == []


# A fake dsh that commits in the worktree with the env it was given. With
# FAKE_SANDBOX=1 it does what the real headless profile does to git: the work
# runs under a Seatbelt profile that denies every write except the cwd, the
# --add-dir grants and /dev/null (no /tmp: stricter than dsh). FAKE_PROBE is
# shell run in the same sandbox after the commit; FAKE_HOST runs unsandboxed.
GIT_DSH = r"""#!/bin/bash
set -uo pipefail
grants=("$(pwd -P)")
while [ $# -gt 0 ]; do
    case "$1" in
        --add-dir) grants+=("$2"); shift 2 ;;
        *) shift ;;
    esac
done
cat > /dev/null
work='printf "change\n" >> README && git add README && git commit -q -m "sandboxed commit"'
if [ -n "${FAKE_PROBE:-}" ]; then
    work="$work; $FAKE_PROBE"
fi
if [ "${FAKE_SANDBOX:-0}" = "1" ]; then
    profile='(version 1) (allow default) (deny file-write*) (allow file-write* (literal "/dev/null")'
    for grant in "${grants[@]}"; do
        profile="$profile (subpath \"$grant\")"
    done
    profile="$profile)"
    sandbox-exec -p "$profile" /bin/sh -c "$work"
else
    /bin/sh -c "$work"
fi
rc=$?
if [ -n "${FAKE_HOST:-}" ]; then
    /bin/sh -c "$FAKE_HOST"
fi
if [ -n "${FAKE_READY:-}" ]; then
    : > "$FAKE_READY"
fi
sleep "${FAKE_SLEEP:-0}"
exit "${FAKE_EXIT:-$rc}"
"""


def _git_dsh_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    bin_dir = tmp_path / "gbin"
    bin_dir.mkdir(exist_ok=True)
    dsh = bin_dir / "dsh"
    dsh.write_text(GIT_DSH, encoding="utf-8")
    dsh.chmod(0o755)
    environment = _git_env(tmp_path)
    environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
    environment["CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR"] = str(tmp_path / "logs")
    # The classifier is not what these tests are about.
    environment["WORKER_FAILURE_CLASSIFIER_CMD"] = "/usr/bin/true"
    environment.update(extra)
    return environment


def _launch(
    worktree: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "deepseek", "commit the change"],
        cwd=str(worktree),
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _assert_tip_is_in_the_store(tmp_path: Path, worktree: Path, common: Path) -> str:
    """The line's tip, read from the main repo with no quarantine env."""
    tip = _git(tmp_path / "repo", "rev-parse", LINE_BRANCH)
    assert tip == _git(worktree, "rev-parse", "HEAD")
    _git(tmp_path / "repo", "cat-file", "-e", f"{tip}^{{commit}}")
    _git(tmp_path / "repo", "cat-file", "-e", f"{tip}:README")
    assert (
        _git(tmp_path / "repo", "log", "-1", "--format=%s", LINE_BRANCH)
        == "sandboxed commit"
    )
    fsck = _git(tmp_path / "repo", "fsck", "--no-progress", "--connectivity-only")
    assert "missing" not in fsck and "broken" not in fsck, fsck
    assert _quarantines(common) == []
    return tip


def test_the_objects_reach_the_store_after_the_worker_exits(tmp_path) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    before = _git(worktree, "rev-parse", "HEAD")
    result = _launch(worktree, _git_dsh_env(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _assert_tip_is_in_the_store(tmp_path, worktree, common) != before


def test_the_objects_reach_the_store_after_a_failed_worker(tmp_path) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    result = _launch(worktree, _git_dsh_env(tmp_path, FAKE_EXIT="3"))
    assert result.returncode != 0
    _assert_tip_is_in_the_store(tmp_path, worktree, common)


def test_the_objects_reach_the_store_after_sigterm(tmp_path) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    ready = tmp_path / "ready"
    environment = _git_dsh_env(tmp_path, FAKE_READY=str(ready), FAKE_SLEEP="30")
    process = subprocess.Popen(
        ["bash", str(SCRIPT), "deepseek", "commit the change"],
        cwd=str(worktree),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert time.monotonic() < deadline, "the fake dsh never committed"
            assert process.poll() is None, process.communicate()
            time.sleep(0.1)
        # Committed, and the objects are still only in the quarantine.
        assert len(_quarantines(common)) == 1
        tip = _git(worktree, "rev-parse", "HEAD")
        assert (
            subprocess.run(
                ["git", "cat-file", "-e", tip],
                cwd=str(tmp_path / "repo"),
                env=_git_env(tmp_path),
                capture_output=True,
                check=False,
            ).returncode
            != 0
        )
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 143
    _assert_tip_is_in_the_store(tmp_path, worktree, common)


def test_a_quarantine_that_cannot_be_moved_is_kept_and_reported(tmp_path) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    # A loose object file that is not an object: git cannot read it back.
    bogus = (
        'mkdir -p "$GIT_OBJECT_DIRECTORY/ab" && printf junk > "$GIT_OBJECT_DIRECTORY/ab/'
        + "c" * 38
        + '"'
    )
    result = _launch(worktree, _git_dsh_env(tmp_path, FAKE_PROBE=bogus))
    assert result.returncode == 79, result.stdout + result.stderr
    kept = _quarantines(common)
    assert len(kept) == 1
    assert f"object quarantine kept at {kept[0]}" in result.stderr
    assert (
        f"dispatch_launch: objects_not_migrated worker_status=0 quarantine={kept[0]}"
        in (result.stderr.splitlines())
    )
    # The commit is still there, in the kept quarantine.
    tip = _git(worktree, "rev-parse", "HEAD")
    assert (kept[0] / tip[:2] / tip[2:]).is_file()


def test_a_kept_quarantine_is_moved_by_the_next_launch(tmp_path) -> None:
    worktree, admin, common = _linked_worktree(tmp_path)
    leftover = common / "conductor-objects" / f"{admin.name}-earlier"
    leftover.mkdir(parents=True)
    environment = _git_env(tmp_path)
    environment["GIT_OBJECT_DIRECTORY"] = str(leftover)
    oid = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        input="left behind\n",
        cwd=str(worktree),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    result = _launch(worktree, _git_dsh_env(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    _git(tmp_path / "repo", "cat-file", "-e", oid)
    _assert_tip_is_in_the_store(tmp_path, worktree, common)


def test_the_admin_files_the_host_trusts_are_restored(tmp_path) -> None:
    worktree, admin, common = _linked_worktree(tmp_path)
    commondir = (admin / "commondir").read_text(encoding="utf-8")
    scribble = (
        f'printf "/elsewhere\\n" > "{admin}/commondir"; '
        f'printf "[core]\\n\\thooksPath = /tmp\\n" > "{admin}/config.worktree"'
    )
    result = _launch(worktree, _git_dsh_env(tmp_path, FAKE_HOST=scribble))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (admin / "commondir").read_text(encoding="utf-8") == commondir
    assert not (admin / "config.worktree").exists()
    assert "restored commondir" in result.stderr
    _assert_tip_is_in_the_store(tmp_path, worktree, common)


_needs_seatbelt = pytest.mark.skipif(
    not Path("/usr/bin/sandbox-exec").exists(), reason="needs macOS sandbox-exec"
)


@_needs_seatbelt
def test_a_sandboxed_deepseek_can_commit_in_a_linked_worktree(tmp_path) -> None:
    worktree, admin, common = _linked_worktree(tmp_path)
    environment = _git_dsh_env(tmp_path, FAKE_SANDBOX="1")

    # Control: the same sandbox without the grants is the N72 failure.
    control = subprocess.run(
        [
            "sandbox-exec",
            "-p",
            "(version 1) (allow default) (deny file-write*) "
            f'(allow file-write* (literal "/dev/null") (subpath "{worktree}"))',
            "/bin/sh",
            "-c",
            "git add README",
        ],
        cwd=str(worktree),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert control.returncode != 0, "the sandbox did not deny the git dir"
    assert "index.lock" in control.stderr

    before = _git(worktree, "rev-parse", "HEAD")
    result = _launch(worktree, environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _assert_tip_is_in_the_store(tmp_path, worktree, common) != before
    assert _git(worktree, "status", "--porcelain") == ""
    assert admin.is_dir()


@_needs_seatbelt
def test_the_sandboxed_worker_cannot_write_the_rest_of_the_common_dir(tmp_path) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    existing = next(p for p in (common / "objects").glob("??/*") if p.is_file())
    targets = {
        "hooks": common / "hooks" / "x",
        "config": common / "config",
        "main": common / "refs" / "heads" / "main",
        "main-lock": common / "refs" / "heads" / "main.lock",
        "packed-refs": common / "packed-refs",
        "info": common / "info" / "exclude",
        "object": existing,
        "new-object": common / "objects" / "zz-new",
    }
    probe = "; ".join(
        f'if (printf x >> "{path}") 2>/dev/null; then echo "WROTE {name}"; '
        f'else echo "DENIED {name}"; fi'
        for name, path in targets.items()
    )
    probe += f'; if chmod u+w "{existing}" 2>/dev/null; then echo "WROTE chmod"; else echo "DENIED chmod"; fi'
    config_before = (common / "config").read_bytes()
    object_before = existing.read_bytes()
    result = _launch(
        worktree, _git_dsh_env(tmp_path, FAKE_SANDBOX="1", FAKE_PROBE=probe)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for name in [*targets, "chmod"]:
        assert f"DENIED {name}" in result.stdout, result.stdout
        assert f"WROTE {name}" not in result.stdout
    assert (common / "config").read_bytes() == config_before
    assert existing.read_bytes() == object_before
    assert not (common / "hooks" / "x").exists()
    assert _git(tmp_path / "repo", "rev-parse", "main") != _git(
        worktree, "rev-parse", "HEAD"
    )
    # The commit itself still went through.
    _assert_tip_is_in_the_store(tmp_path, worktree, common)


# ------------------------------- deepseek quarantine: hostile Worker leftovers


def _store_has(repo: Path, oid: str) -> bool:
    return (
        subprocess.run(
            ["git", "cat-file", "-e", oid],
            cwd=str(repo),
            env=_git_env(repo),
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def _line_tip(tmp_path: Path) -> str:
    return _git(tmp_path / "repo", "rev-parse", LINE_BRANCH)


def test_a_symlink_in_the_quarantine_keeps_it_and_nothing_is_removed_through_it(
    tmp_path,
) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "alternates").write_text("keep me\n", encoding="utf-8")
    (victim / "precious").write_text("keep me\n", encoding="utf-8")
    plant = f'ln -s "{victim}" "$GIT_OBJECT_DIRECTORY/info"'
    result = _launch(worktree, _git_dsh_env(tmp_path, FAKE_HOST=plant))
    assert result.returncode == 79, result.stdout + result.stderr
    assert "holds a symlink" in result.stderr
    assert "dispatch_launch: objects_not_migrated" in result.stderr
    assert (victim / "alternates").read_text(encoding="utf-8") == "keep me\n"
    assert (victim / "precious").is_file()
    kept = _quarantines(common)
    assert len(kept) == 1 and (kept[0] / "info").is_symlink()


def test_a_quarantine_that_is_itself_a_symlink_is_kept(tmp_path) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious").write_text("keep me\n", encoding="utf-8")
    swap = (
        'mv "$GIT_OBJECT_DIRECTORY" "$GIT_OBJECT_DIRECTORY.moved" && '
        f'ln -s "{elsewhere}" "$GIT_OBJECT_DIRECTORY"'
    )
    result = _launch(worktree, _git_dsh_env(tmp_path, FAKE_HOST=swap))
    assert result.returncode == 79, result.stdout + result.stderr
    assert "not a real directory" in result.stderr
    assert (elsewhere / "precious").is_file()


def test_the_migrated_objects_stay_packed(tmp_path) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    result = _launch(worktree, _git_dsh_env(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    tip = _line_tip(tmp_path)
    assert not (common / "objects" / tip[:2] / tip[2:]).exists()
    assert list((common / "objects" / "pack").glob("pack-*.pack"))
    _assert_tip_is_in_the_store(tmp_path, worktree, common)


def test_an_oversized_quarantine_is_kept(tmp_path) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    result = _launch(
        worktree, _git_dsh_env(tmp_path, CONDUCTOR_DISPATCH_QUARANTINE_MAX_BYTES="64")
    )
    assert result.returncode == 79, result.stdout + result.stderr
    assert "the objects could not be moved" in result.stderr
    assert not _store_has(tmp_path / "repo", _line_tip(tmp_path))
    assert len(_quarantines(common)) == 1


def test_a_corrupt_pack_in_the_quarantine_is_kept(tmp_path) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    # The Worker repacks its own objects, then a byte run in the pack's last
    # object is overwritten: the index still names the objects.
    corrupt = (
        "git repack -q -a -d -l && "
        'f="$(ls "$GIT_OBJECT_DIRECTORY"/pack/pack-*.pack)" && '
        'chmod u+w "$f" && size="$(wc -c < "$f")" && '
        "printf XXXXXXXXXXXXXXXX | "
        'dd of="$f" bs=1 seek="$((size - 36))" conv=notrunc 2>/dev/null'
    )
    result = _launch(worktree, _git_dsh_env(tmp_path, FAKE_HOST=corrupt))
    assert result.returncode == 79, result.stdout + result.stderr
    assert "dispatch_launch: objects_not_migrated" in result.stderr
    assert not _store_has(tmp_path / "repo", _line_tip(tmp_path))
    kept = _quarantines(common)
    assert len(kept) == 1 and list((kept[0] / "pack").glob("pack-*.pack"))


def test_a_rewritten_worktree_pointer_cannot_send_the_migration_to_a_decoy(
    tmp_path,
) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-q", "-b", "main")
    (decoy / "f").write_text("decoy\n", encoding="utf-8")
    _git(decoy, "add", "f")
    _git(decoy, "commit", "-q", "-m", "decoy")
    pointer = (worktree / ".git").read_text(encoding="utf-8")
    rewrite = f'printf "gitdir: {decoy}/.git\\n" > "{worktree}/.git"'
    try:
        result = _launch(worktree, _git_dsh_env(tmp_path, FAKE_HOST=rewrite))
    finally:
        (worktree / ".git").write_text(pointer, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    tip = _line_tip(tmp_path)
    assert _store_has(tmp_path / "repo", f"{tip}^{{commit}}")
    assert _store_has(tmp_path / "repo", f"{tip}:README")
    assert not _store_has(decoy, tip)
    assert _quarantines(common) == []


def _orphan_scribbler(admin: Path, pid_file: Path) -> str:
    """Shell for a Worker leftover that left the Worker's tree and keeps
    rewriting the admin dir's commondir, ignoring TERM."""
    return (
        "( ( trap '' TERM; while true; do "
        f'printf "/elsewhere\\n" > "{admin}/commondir"; sleep 0.02; done ) & '
        f'echo "$!" > "{pid_file}" ) &'
    )


def _kill_pid_file(pid_file: Path) -> None:
    try:
        os.kill(int(pid_file.read_text(encoding="utf-8").strip()), signal.SIGKILL)
    except (OSError, ValueError):
        pass


def test_a_leftover_in_the_group_is_swept_before_the_migration_on_sigterm(
    tmp_path,
) -> None:
    worktree, admin, common = _linked_worktree(tmp_path)
    commondir = (admin / "commondir").read_text(encoding="utf-8")
    ready = tmp_path / "ready"
    pid_file = tmp_path / "scribbler.pid"
    environment = _git_dsh_env(
        tmp_path,
        FAKE_HOST=_orphan_scribbler(admin, pid_file),
        FAKE_READY=str(ready),
        FAKE_SLEEP="30",
        CONDUCTOR_DISPATCH_LAUNCH_STOP_GRACE="2",
    )
    process = subprocess.Popen(
        ["bash", str(SCRIPT), "deepseek", "commit the change"],
        cwd=str(worktree),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not (ready.exists() and pid_file.exists()):
            assert time.monotonic() < deadline, "the fake dsh never got ready"
            time.sleep(0.1)
        process.send_signal(signal.SIGTERM)
        _out, err = process.communicate(timeout=60)
        assert process.returncode == 143, err
        scribbler = pid_file.read_text(encoding="utf-8").strip()
        assert _wait_for_exit(scribbler, timeout=5), "the leftover outlived the launch"
        time.sleep(0.3)
        assert (admin / "commondir").read_text(encoding="utf-8") == commondir
    finally:
        _kill_pid_file(pid_file)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
    _assert_tip_is_in_the_store(tmp_path, worktree, common)


def test_admin_files_that_change_during_the_migration_keep_the_quarantine(
    tmp_path,
) -> None:
    # The launch does not lead its group here (a direct run), so the group
    # cannot be swept and the leftover keeps writing: the second check sees it.
    worktree, admin, common = _linked_worktree(tmp_path)
    pid_file = tmp_path / "scribbler.pid"
    try:
        result = _launch(
            worktree,
            _git_dsh_env(tmp_path, FAKE_HOST=_orphan_scribbler(admin, pid_file)),
        )
    finally:
        _kill_pid_file(pid_file)
    assert result.returncode == 79, result.stdout + result.stderr
    assert "admin files changed again during the migration: commondir" in result.stderr
    assert len(_quarantines(common)) == 1


def _stuck_quarantine(common: Path, admin: Path, attempts: int | None = None) -> Path:
    stuck = common / "conductor-objects" / f"{admin.name}-stuck"
    (stuck / "info").mkdir(parents=True)
    (stuck / "info" / "loop").symlink_to(stuck)
    if attempts is not None:
        (stuck.parent / f"{stuck.name}.attempts").write_text(
            f"{attempts}\n", encoding="utf-8"
        )
    return stuck


def test_a_kept_quarantine_is_a_read_only_alternate_for_the_next_worker(
    run, tmp_path
) -> None:
    worktree, admin, common = _linked_worktree(tmp_path)
    stuck = _stuck_quarantine(common, admin)
    result = run("deepseek", "do the thing", cwd=worktree)
    assert result.returncode == 0, result.stderr
    record = _record(tmp_path)
    alternates = record["git_env"]["GIT_ALTERNATE_OBJECT_DIRECTORIES"].split(":")
    assert alternates == [str(common / "objects"), str(stuck)]
    assert str(stuck) not in _add_dirs(list(record["argv"]))
    assert (stuck.parent / f"{stuck.name}.attempts").read_text(
        encoding="utf-8"
    ).strip() == "1"


def test_a_kept_quarantine_is_not_retried_past_the_cap(run, tmp_path) -> None:
    worktree, admin, common = _linked_worktree(tmp_path)
    stuck = _stuck_quarantine(common, admin, attempts=3)
    result = run("deepseek", "do the thing", cwd=worktree)
    assert result.returncode == 0, result.stderr
    assert (
        f"object quarantine kept at {stuck}: gave up after 3 attempts" in result.stderr
    )
    assert (stuck.parent / f"{stuck.name}.attempts").read_text(
        encoding="utf-8"
    ).strip() == "3"
    alternates = _record(tmp_path)["git_env"]["GIT_ALTERNATE_OBJECT_DIRECTORIES"]
    assert alternates.split(":")[-1] == str(stuck)


# ------------------------------------ deepseek quarantine: re-review follow-ups


def _commit_into(
    worktree: Path, objects: Path, alternates: list[Path], text: str
) -> str:
    """A commit on the line branch whose new objects land in ``objects``."""
    objects.mkdir(parents=True, exist_ok=True)
    environment = _git_env(worktree)
    environment["GIT_OBJECT_DIRECTORY"] = str(objects)
    environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = ":".join(
        str(a) for a in alternates
    )
    (worktree / "README").write_text(text, encoding="utf-8")
    for argv in (["add", "README"], ["commit", "-q", "-m", text.strip()]):
        subprocess.run(
            ["git", *argv],
            cwd=str(worktree),
            env=environment,
            check=True,
            capture_output=True,
        )
    return _git(tmp_path_of(worktree) / "repo", "rev-parse", LINE_BRANCH)


def tmp_path_of(worktree: Path) -> Path:
    return worktree.parent


def _two_kept_quarantines(tmp_path: Path, older_name: str, newer_name: str):
    """Two kept quarantines of earlier launches: the older holds commit A,
    the newer holds commit B (A's child, the line's tip). Older by mtime."""
    worktree, admin, common = _linked_worktree(tmp_path)
    root = common / "conductor-objects"
    older = root / f"{admin.name}-{older_name}"
    newer = root / f"{admin.name}-{newer_name}"
    first = _commit_into(worktree, older, [common / "objects"], "first\n")
    tip = _commit_into(worktree, newer, [common / "objects", older], "second\n")
    base = time.time_ns() - 10_000_000_000
    os.utime(older, ns=(base, base))
    os.utime(newer, ns=(base + 500, base + 500))
    assert not _store_has(tmp_path / "repo", first)
    return worktree, common, older, newer, first, tip


def _assert_both_moved(tmp_path, common, older, newer, first, tip) -> None:
    assert not older.exists() and not newer.exists()
    assert not list((common / "conductor-objects").glob("*.attempts"))
    assert _store_has(tmp_path / "repo", f"{first}^{{commit}}")
    assert _store_has(tmp_path / "repo", f"{tip}^{{commit}}")
    assert _store_has(tmp_path / "repo", f"{tip}:README")


def test_an_older_kept_quarantine_is_not_judged_by_the_newer_tip(run, tmp_path) -> None:
    worktree, common, older, newer, first, tip = _two_kept_quarantines(
        tmp_path, "20260101-000000-1", "20260101-000000-2"
    )
    result = run("deepseek", "do the thing", cwd=worktree, env=_GIT_IDENTITY)
    assert result.returncode == 0, result.stderr
    assert "is not in the store" not in result.stderr
    _assert_both_moved(tmp_path, common, older, newer, first, tip)


def test_kept_quarantines_are_retried_oldest_first_by_time(run, tmp_path) -> None:
    # Same second, pids 9 and 10: the names sort "-10" before "-9" as text,
    # but -9 is older and holds the parent the newer one's objects link to.
    worktree, common, older, newer, first, tip = _two_kept_quarantines(
        tmp_path, "20260101-000000-9", "20260101-000000-10"
    )
    result = run("deepseek", "do the thing", cwd=worktree, env=_GIT_IDENTITY)
    assert result.returncode == 0, result.stderr
    assert "object quarantine kept" not in result.stderr
    _assert_both_moved(tmp_path, common, older, newer, first, tip)


def test_a_quarantine_whose_objects_are_already_in_the_store_is_dropped(
    run, tmp_path
) -> None:
    worktree, admin, common = _linked_worktree(tmp_path)
    duplicate = common / "conductor-objects" / f"{admin.name}-20260101-000000-1"
    duplicate.mkdir(parents=True)
    environment = _git_env(tmp_path)
    environment["GIT_OBJECT_DIRECTORY"] = str(duplicate)
    # The same blob the store already holds, written loose into the leftover.
    subprocess.run(
        ["git", "hash-object", "-w", "README"],
        cwd=str(worktree),
        env=environment,
        check=True,
        capture_output=True,
    )
    # An unreadable index-pack would refuse it; nothing needs moving at all.
    result = run(
        "deepseek",
        "do the thing",
        cwd=worktree,
        env={"CONDUCTOR_DISPATCH_QUARANTINE_MAX_BYTES": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert not duplicate.exists()


def test_a_failed_sweep_keeps_the_quarantine_unmigrated(tmp_path) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    pid_file = tmp_path / "survivor.pid"
    survivor = (
        "( ( trap '' TERM; while true; do sleep 0.2; done ) & "
        f'echo "$!" > "{pid_file}" ) &'
    )
    environment = _git_dsh_env(
        tmp_path, FAKE_HOST=survivor, CONDUCTOR_DISPATCH_LAUNCH_STOP_GRACE="0"
    )
    process = subprocess.Popen(
        ["bash", str(SCRIPT), "deepseek", "commit the change"],
        cwd=str(worktree),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _out, err = process.communicate(timeout=60)
    finally:
        _kill_pid_file(pid_file)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
    assert process.returncode == 79, err
    assert "process group is not empty" in err
    assert (
        "dispatch_launch: objects_not_migrated worker_status=0 sweep=failed quarantine="
        in err
    )
    kept = _quarantines(common)
    assert len(kept) == 1
    tip = _line_tip(tmp_path)
    assert not _store_has(tmp_path / "repo", tip)
    assert (kept[0] / tip[:2] / tip[2:]).is_file()


def test_the_marker_carries_the_workers_own_status(tmp_path) -> None:
    worktree, _, _ = _linked_worktree(tmp_path)
    bogus = (
        'mkdir -p "$GIT_OBJECT_DIRECTORY/ab" && printf junk > "$GIT_OBJECT_DIRECTORY/ab/'
        + "c" * 38
        + '"'
    )
    result = _launch(worktree, _git_dsh_env(tmp_path, FAKE_PROBE=bogus, FAKE_EXIT="76"))
    assert result.returncode == 79, result.stdout + result.stderr
    assert (
        "dispatch_launch: objects_not_migrated worker_status=76 quarantine="
        in result.stderr
    )


_LEGACY_TREE = r"""
blob=$(printf 'hi\n' | git hash-object -w --stdin)
sub=$( { printf '100644 f\0'; printf '%s' "$blob" | xxd -r -p; } | git hash-object -t tree -w --stdin)
tree=$( { printf '040000 d\0'; printf '%s' "$sub" | xxd -r -p; } | git hash-object -t tree -w --literally --stdin)
printf '%s\n' "$tree" > "$LEGACY_OUT"
"""

_BAD_DATE_COMMIT = r"""
tree=$(git write-tree)
bad=$(printf 'tree %s\nauthor x <a@b> notatime +0000\ncommitter x <a@b> 0 +0000\n\nm\n' "$tree" | git hash-object -t commit -w --literally --stdin)
printf '%s\n' "$bad" > "$LEGACY_OUT"
"""


@pytest.mark.skipif(not Path("/usr/bin/xxd").exists(), reason="needs xxd")
def test_a_legacy_tree_with_a_fsck_warning_is_moved(tmp_path) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    out = tmp_path / "legacy"
    result = _launch(
        worktree, _git_dsh_env(tmp_path, FAKE_HOST=_LEGACY_TREE, LEGACY_OUT=str(out))
    )
    assert result.returncode == 0, result.stdout + result.stderr
    legacy = out.read_text(encoding="utf-8").strip()
    assert _store_has(tmp_path / "repo", legacy)
    assert _quarantines(common) == []


@pytest.mark.skipif(not Path("/usr/bin/xxd").exists(), reason="needs xxd")
def test_the_repository_fsck_config_still_wins(tmp_path) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    _git(tmp_path / "repo", "config", "fsck.zeroPaddedFilemode", "error")
    out = tmp_path / "legacy"
    result = _launch(
        worktree, _git_dsh_env(tmp_path, FAKE_HOST=_LEGACY_TREE, LEGACY_OUT=str(out))
    )
    assert result.returncode == 79, result.stdout + result.stderr
    assert len(_quarantines(common)) == 1


def test_an_fsck_error_is_still_refused(tmp_path) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    out = tmp_path / "bad"
    result = _launch(
        worktree,
        _git_dsh_env(tmp_path, FAKE_HOST=_BAD_DATE_COMMIT, LEGACY_OUT=str(out)),
    )
    assert result.returncode == 79, result.stdout + result.stderr
    assert not _store_has(tmp_path / "repo", out.read_text(encoding="utf-8").strip())
    assert len(_quarantines(common)) == 1


# A tree with an entry named NAME pointing at a subtree: path-unsafe history
# that must never reach the shared store, whatever fsck severity the launcher
# relaxes for legacy format.
_UNSAFE_TREE = r"""
blob=$(printf 'hi\n' | git hash-object -w --stdin)
sub=$( { printf '100644 f\0'; printf '%s' "$blob" | xxd -r -p; } | git hash-object -t tree -w --stdin)
tree=$( { printf '40000 %s\0' "$UNSAFE_NAME"; printf '%s' "$sub" | xxd -r -p; } | git hash-object -t tree -w --literally --stdin)
printf '%s\n' "$tree" > "$LEGACY_OUT"
"""


@pytest.mark.skipif(not Path("/usr/bin/xxd").exists(), reason="needs xxd")
@pytest.mark.parametrize("name", [".git", "..", ".GIT"])
def test_a_path_unsafe_tree_is_still_refused(tmp_path, name) -> None:
    worktree, _, common = _linked_worktree(tmp_path)
    out = tmp_path / "unsafe"
    result = _launch(
        worktree,
        _git_dsh_env(
            tmp_path, FAKE_HOST=_UNSAFE_TREE, LEGACY_OUT=str(out), UNSAFE_NAME=name
        ),
    )
    assert result.returncode == 79, result.stdout + result.stderr
    assert (
        "dispatch_launch: objects_not_migrated worker_status=0 quarantine="
        in result.stderr
    )
    unsafe = out.read_text(encoding="utf-8").strip()
    assert not _store_has(tmp_path / "repo", unsafe)
    kept = _quarantines(common)
    assert len(kept) == 1
    assert (kept[0] / unsafe[:2] / unsafe[2:]).is_file()
