"""N29: ``launch_worker(context_key=...)`` -- budget file, manifest, handoff request.

A fake launch script of this file's own (not scripts/dispatch_launch.sh)
records its argv and, steered by environment variables, prints log bytes,
leaves the worktree dirty and writes (or plants) a handoff request at the
``--handoff-request`` path. No model, no network.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from src.tools.auto_dispatch import context_budget as cb
from src.tools.auto_dispatch.execution import (
    LaunchResult,
    ScriptLauncher,
    WorkerLaunchFailure,
)
from src.tools.auto_dispatch.protocols import LaunchError, UncommittedChangesError
from src.tools.auto_dispatch.types import Line

pytestmark = pytest.mark.asyncio

DISPATCH_ID = "d-n29"
KEY = "kimi:d-n29:line-b"

FAKE_SCRIPT = """\
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_ARGV_FILE"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
sys.stdout.write("x" * int(os.environ.get("FAKE_LOG_BYTES", "0")))
sys.stdout.flush()
if os.environ.get("FAKE_DIRTY") == "1":
    with open("dirty.txt", "w", encoding="utf-8") as fh:
        fh.write("unfinished\\n")
if os.environ.get("FAKE_COMMIT") == "1":
    import subprocess
    with open("work.txt", "w", encoding="utf-8") as fh:
        fh.write("the work\\n")
    git = ["git", "-c", "user.name=W", "-c", "user.email=w@example.com"]
    subprocess.run(git + ["add", "work.txt"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "work"], check=True)
request = None
for arg in argv:
    if arg.startswith("--handoff-request="):
        request = arg.split("=", 1)[1]
mode = os.environ.get("FAKE_REQUEST", "none")
if request is not None and mode != "none":
    if mode == "valid":
        with open(request, "w", encoding="utf-8") as fh:
            json.dump({"reason": "context_budget", "report": {
                "done": ["wrote the parser"], "in_progress": ["tests"],
                "next": ["run the suite"], "pitfalls": [], "open_questions": []}}, fh)
    elif mode == "bad":
        with open(request, "w", encoding="utf-8") as fh:
            fh.write("{not json")
    elif mode == "symlink":
        os.symlink(os.environ["FAKE_SYMLINK_TARGET"], request)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


def _git(cwd, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "dispatch@example.com")
    _git(root, "config", "user.name", "Dispatch Test")
    _git(root, "config", "core.hooksPath", "/dev/null")
    _git(root, "config", "core.fsmonitor", "false")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture
def env(tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_ARGV_FILE", str(argv_file))
    for name in ("FAKE_LOG_BYTES", "FAKE_DIRTY", "FAKE_COMMIT", "FAKE_REQUEST", "FAKE_EXIT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(cb.SOFT_RATIO_ENV, raising=False)
    return argv_file


def _launcher(tmp_path) -> ScriptLauncher:
    script = tmp_path / "fake-launch"
    script.write_text(f"#!{sys.executable}\n" + FAKE_SCRIPT, encoding="utf-8")
    script.chmod(0o755)
    return ScriptLauncher(
        tmp_path / "runs",
        script=script,
        cwd=tmp_path / "repo",
        worktrees_dir=tmp_path / "dispatch-worktrees",
        dispatch_id=DISPATCH_ID,
    )


def _line(repo, run: str = "run0") -> Line:
    return Line(
        id="line-b",
        name="the line",
        task="do the work",
        branch=f"feature/auto-dispatch-{DISPATCH_ID}/line-line-b",
        base_commit=_git(repo, "rev-parse", "HEAD"),
        current_run=f"{DISPATCH_ID}:line-b:{run}",
    )


async def _run(launcher, line, *, context_key=KEY, model="kimi", **kwargs):
    extra = {} if context_key is None else {"context_key": context_key}
    return await launcher.launch_worker(
        line, model, [], session_id=None, session_mode=None, handoff_path=None,
        **extra, **kwargs,
    )


def _argvs(argv_file: Path) -> list[list[str]]:
    return [json.loads(line) for line in argv_file.read_text(encoding="utf-8").splitlines()]


def _opt(argv: list[str], name: str) -> str:
    return next(arg.split("=", 1)[1] for arg in argv if arg.startswith(name + "="))


def _manifest(tmp_path) -> list[dict]:
    path = cb.manifest_path(tmp_path / "runs", KEY)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_without_context_key_argv_and_files_are_unchanged(tmp_path, repo, env, monkeypatch):
    monkeypatch.setenv("FAKE_COMMIT", "1")
    launcher = _launcher(tmp_path)
    line = _line(repo)
    result = await _run(launcher, line, context_key=None)

    argv = _argvs(env)[0]
    assert argv == launcher.argv(line, "kimi", [])[1:]
    assert not (tmp_path / "runs" / cb.RECOVERY_DIR).exists()
    assert result.handoff_request == cb.HANDOFF_NONE
    assert result.estimate is None and result.worker_report is None
    assert result.self_report_bytes == 0


async def test_context_key_adds_four_options_budget_file_and_manifest(tmp_path, repo, env, monkeypatch):
    monkeypatch.setenv("FAKE_COMMIT", "1")
    monkeypatch.setenv("FAKE_LOG_BYTES", "4000")
    launcher = _launcher(tmp_path)
    line = _line(repo)
    result = await _run(launcher, line)

    argv = _argvs(env)[0]
    budget_file = Path(_opt(argv, cb.ARG_CONTEXT_BUDGET))
    request_file = Path(_opt(argv, cb.ARG_HANDOFF_REQUEST))
    manifest = Path(_opt(argv, cb.ARG_HISTORY_MANIFEST))
    run_log = Path(_opt(argv, cb.ARG_RUN_LOG))
    assert manifest == cb.manifest_path(tmp_path / "runs", KEY)
    assert run_log == launcher.log_path(line.current_run)
    assert request_file.name == cb.HANDOFF_REQUEST_FILENAME
    assert stat.S_IMODE(os.stat(budget_file).st_mode) == 0o600
    block = budget_file.read_text(encoding="utf-8")
    assert block.startswith("<conductor_context_budget>")
    baseline = cb.parse_budget_block(block)
    assert baseline is not None and baseline.source == "launches" and baseline.used_tokens == 0

    entries = _manifest(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["run_id"] == line.current_run and entry["exit_code"] == 0
    assert entry["log_path"] == str(run_log)
    assert entry["handoff_request"] == cb.HANDOFF_NONE
    assert entry["estimate_source"] == "launches" and entry["session_generation"] == 1
    lineage = launcher.recovery_session_manager().lineage(KEY)
    assert entry["lineage_current"] == lineage.current == entry["lineage_first"]
    assert entry["started_at"].endswith("+00:00") and entry["ended_at"].endswith("+00:00")
    assert "xxxx" not in manifest.read_text(encoding="utf-8")  # no log content

    assert result.handoff_request == cb.HANDOFF_NONE
    assert result.estimate is not None and result.estimate.source == "launches"
    assert result.estimate.used_tokens > 0  # this run counts as one launch


async def test_valid_request_skips_verification_and_returns_report(tmp_path, repo, env, monkeypatch):
    monkeypatch.setenv("FAKE_REQUEST", "valid")
    monkeypatch.setenv("FAKE_DIRTY", "1")
    launcher = _launcher(tmp_path)
    result = await _run(launcher, _line(repo))

    assert len(_argvs(env)) == 1  # no commit nudge turn
    assert result.handoff_request == cb.HANDOFF_OK
    assert result.worker_report is not None
    assert result.worker_report.done == ("wrote the parser",)
    assert result.self_report_bytes > 0
    assert result.head == result.start_head is not None
    # The request stays until the machine has saved the handoff decision.
    request_file = Path(_opt(_argvs(env)[0], cb.ARG_HANDOFF_REQUEST))
    assert result.handoff_request_file == request_file
    assert request_file.exists()
    assert cb.pending_handoff_request(tmp_path / "runs", result.run_id).status == cb.HANDOFF_OK
    assert _manifest(tmp_path)[0]["handoff_request"] == cb.HANDOFF_OK


async def test_bad_json_request_is_invalid_and_takes_the_normal_exit_path(
    tmp_path, repo, env, monkeypatch, caplog
):
    """Planner ruling: only ok skips the submission check; invalid is a normal exit."""
    monkeypatch.setenv("FAKE_REQUEST", "bad")
    monkeypatch.setenv("FAKE_DIRTY", "1")
    with caplog.at_level("WARNING"), pytest.raises(UncommittedChangesError):
        # The dirty tree gets the commit nudge (a second turn), which refuses.
        await _run(_launcher(tmp_path), _line(repo))
    assert len(_argvs(env)) == 2
    assert not Path(_opt(_argvs(env)[0], cb.ARG_HANDOFF_REQUEST)).exists()
    assert _manifest(tmp_path)[0]["handoff_request"] == cb.HANDOFF_INVALID
    assert "invalid" in caplog.text and "{not json" not in caplog.text


async def test_bad_json_request_on_a_committed_run_is_a_normal_success(
    tmp_path, repo, env, monkeypatch
):
    monkeypatch.setenv("FAKE_REQUEST", "bad")
    monkeypatch.setenv("FAKE_COMMIT", "1")
    result = await _run(_launcher(tmp_path), _line(repo))
    assert result.handoff_request == cb.HANDOFF_INVALID
    assert result.handoff_request_file is None and result.worker_report is None
    assert result.head is not None and result.head != result.start_head
    assert not Path(_opt(_argvs(env)[0], cb.ARG_HANDOFF_REQUEST)).exists()


async def test_symlink_request_is_refused_and_takes_the_normal_path(tmp_path, repo, env, monkeypatch, caplog):
    target = tmp_path / "secret.txt"
    target.write_text("do not read\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_REQUEST", "symlink")
    monkeypatch.setenv("FAKE_SYMLINK_TARGET", str(target))
    monkeypatch.setenv("FAKE_DIRTY", "1")
    with pytest.raises(UncommittedChangesError):
        # Normal path: the dirty tree gets the commit nudge, which refuses.
        await _run(_launcher(tmp_path), _line(repo))
    assert len(_argvs(env)) == 2
    assert target.read_text(encoding="utf-8") == "do not read\n"
    assert "do not read" not in caplog.text


async def test_symlink_request_on_a_committed_run_reports_refused(tmp_path, repo, env, monkeypatch, caplog):
    target = tmp_path / "secret.txt"
    target.write_text("do not read\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_REQUEST", "symlink")
    monkeypatch.setenv("FAKE_SYMLINK_TARGET", str(target))
    monkeypatch.setenv("FAKE_COMMIT", "1")
    with caplog.at_level("WARNING"):
        result = await _run(_launcher(tmp_path), _line(repo))
    assert result.handoff_request == cb.HANDOFF_REFUSED_SYMLINK
    assert result.worker_report is None
    assert result.head is not None and result.head != result.start_head
    assert _manifest(tmp_path)[0]["handoff_request"] == cb.HANDOFF_REFUSED_SYMLINK
    assert "symlink" in caplog.text and "do not read" not in caplog.text
    assert target.read_text(encoding="utf-8") == "do not read\n"
    assert not os.path.lexists(_opt(_argvs(env)[-1], cb.ARG_HANDOFF_REQUEST))


@pytest.mark.parametrize("exit_code, error", [(1, LaunchError), (75, WorkerLaunchFailure)])
async def test_nonzero_exit_ignores_and_removes_request_and_appends_manifest(
    tmp_path, repo, env, monkeypatch, exit_code, error
):
    monkeypatch.setenv("FAKE_REQUEST", "valid")
    monkeypatch.setenv("FAKE_EXIT", str(exit_code))
    with pytest.raises(error):
        await _run(_launcher(tmp_path), _line(repo))
    request_file = Path(_opt(_argvs(env)[0], cb.ARG_HANDOFF_REQUEST))
    assert not request_file.exists()
    entry = _manifest(tmp_path)[0]
    assert entry["exit_code"] == exit_code
    assert entry["handoff_request"] == cb.HANDOFF_NONE


async def test_estimate_grows_with_log_size(tmp_path, repo, env, monkeypatch):
    monkeypatch.setenv("FAKE_REQUEST", "valid")
    monkeypatch.setenv("FAKE_LOG_BYTES", "400")
    small = await _run(_launcher(tmp_path), _line(repo), model="grok")
    monkeypatch.setenv("FAKE_LOG_BYTES", "40000")
    big = await _run(_launcher(tmp_path), _line(repo, "run1"), model="grok", context_key="grok:other")
    assert small.estimate.source == big.estimate.source == "log_bytes"
    assert big.estimate.used_tokens > small.estimate.used_tokens >= 100


async def test_second_launch_in_same_lineage_sees_prior_log_bytes(tmp_path, repo, env, monkeypatch):
    monkeypatch.setenv("FAKE_REQUEST", "valid")
    monkeypatch.setenv("FAKE_LOG_BYTES", "8000")
    launcher = _launcher(tmp_path)
    first = await _run(launcher, _line(repo), model="grok")
    second = await _run(launcher, _line(repo, "run1"), model="grok")

    budget = Path(_opt(_argvs(env)[1], cb.ARG_CONTEXT_BUDGET))
    baseline = cb.parse_budget_block(budget.read_text(encoding="utf-8"))
    first_log = launcher.log_path(_line(repo).current_run).stat().st_size
    assert baseline is not None and baseline.used_tokens == first_log // cb.BYTES_PER_TOKEN
    assert second.estimate.used_tokens > first.estimate.used_tokens
    entries = _manifest(tmp_path)
    assert len(entries) == 2 and entries[0]["lineage_current"] == entries[1]["lineage_current"]


async def test_budget_trouble_launches_without_the_options(tmp_path, repo, env, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk says no")

    monkeypatch.setenv("FAKE_COMMIT", "1")
    monkeypatch.setattr(cb, "prepare_run_paths", broken)
    result = await _run(_launcher(tmp_path), _line(repo))
    argv = _argvs(env)[0]
    assert not any(arg.startswith("--context-budget") for arg in argv)
    assert result.handoff_request == cb.HANDOFF_NONE and result.estimate is None


async def test_manager_without_ensure_lineage_skips_the_budget(tmp_path, repo, env, monkeypatch):
    monkeypatch.setenv("FAKE_COMMIT", "1")
    launcher = _launcher(tmp_path)

    class Bare:
        def current(self, key):
            return None

    monkeypatch.setattr(launcher, "recovery_session_manager", lambda: Bare())
    await _run(launcher, _line(repo))
    assert not any(arg.startswith("--context-budget") for arg in _argvs(env)[0])


async def test_positional_launch_result_still_constructs():
    result = LaunchResult("run", "sid", "")
    assert result.handoff_request == "none" and result.estimate is None
    assert result.worker_report is None and result.self_report_bytes == 0
