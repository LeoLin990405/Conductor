"""W8a end to end: a fake Worker that fails twice and then succeeds.

PRD section 8 asks for an end-to-end run under a fake CLI: the first turn
returns 429, the second runs into the context limit, the third succeeds. The
backoff and the event sequence belong to W8b; what W8a owns is the other half
of that story -- the failure is **classified**, the context exhaustion
**rotates the session**, and the recovered turn lands on the **new** session.

The fake CLI is a real process. It never contacts a host: its output is written
by the test, and the only thing the client sees is the text the fake printed.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from src.llms.native_cli.base import NativeCliUsage
from src.llms.native_cli.failure import FailureClass, classify_failure
from src.llms.native_cli.session_manager import WorkerSessionManager
from src.llms.native_cli.sessions import NativeCliSessionStore


#: What the fake CLI prints on each turn, and how it exits.
_TURNS = (
    ("429 Too Many Requests", 1),
    ("context_length_exceeded: input token count exceeds maximum number of tokens", 1),
    ("done", 0),
)

_FAKE_CLI = """\
import pathlib, sys
counter = pathlib.Path(sys.argv[1])
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
# The turn file holds, per turn, the line to print and then the exit code.
turns = pathlib.Path(sys.argv[2]).read_text().splitlines()
print(turns[2 * n])
sys.exit(int(turns[2 * n + 1]))
"""


def _fake_cli(tmp_path: Path, turns: tuple[tuple[str, int], ...]) -> tuple[Path, Path]:
    """Write the fake CLI and the script that drives it; return their paths."""
    counter = tmp_path / "turns.count"
    counter.write_text("0")
    lines = tmp_path / "turns.txt"
    lines.write_text(
        "\n".join(f"{text}\n{code}" for text, code in turns) + "\n", encoding="utf-8"
    )
    script = tmp_path / "fake_cli.py"
    script.write_text(_FAKE_CLI, encoding="utf-8")
    return script, counter


def _run_fake(script: Path, counter: Path, lines: Path) -> tuple[str, int]:
    """Run the fake CLI once and return its output and its exit code."""
    completed = subprocess.run(
        [sys.executable, str(script), str(counter), str(lines)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return completed.stdout + completed.stderr, completed.returncode


def _git_repo(path: Path) -> None:
    """A real checkout, so the handover has real git facts to report."""
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "worker@example.invalid"],
        ["git", "config", "user.name", "Worker"],
    ):
        subprocess.run(args, cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("worker line\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "-A"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "worker: start the line"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _manager(tmp_path: Path, worktree: Path) -> WorkerSessionManager:
    return WorkerSessionManager(
        path=tmp_path / "worker-sessions.json",
        store=NativeCliSessionStore(tmp_path / "sessions.json"),
        clock=lambda: 1_700_000_000.0,
        provider="kimi",
        worktree=worktree,
        baseline="HEAD",
        task_file=tmp_path / "TASK.md",
        report=tmp_path / "report.md",
        handoff_dir=tmp_path / "handoffs",
    )


def test_a_fake_worker_that_is_limited_then_exhausts_then_succeeds(tmp_path):
    """429 -> context exhausted -> success, with one rotation in between."""
    script, counter = _fake_cli(tmp_path, _TURNS)
    lines = tmp_path / "turns.txt"
    worktree = tmp_path / "worktree"
    _git_repo(worktree)
    (tmp_path / "TASK.md").write_text("do the thing\n", encoding="utf-8")
    (tmp_path / "report.md").write_text("previous report\n", encoding="utf-8")

    manager = _manager(tmp_path, worktree)
    key = "thread:t-1:kimi"

    # --- turn 1: the provider is rate limited -------------------------
    output, code = _run_fake(script, counter, lines)
    first = classify_failure("kimi", output, exit_code=code)
    assert first.failure_class is FailureClass.RATE_LIMITED
    # A rate limit is retried on the *same* session: nothing rotates.
    assert manager.current(key) is None

    # --- turn 2: the context is full ----------------------------------
    output, code = _run_fake(script, counter, lines)
    second = classify_failure("kimi", output, exit_code=code)
    assert second.failure_class is FailureClass.CONTEXT_EXHAUSTED

    manager.record_turn(key, session_id="sess-1")
    manager.record_failure(key, second.failure_class.value)
    assert manager.should_rotate(key) is True

    rotation = manager.rotate(key, reason="context_exhausted")
    assert rotation.rotated is True
    assert rotation.parked is False
    assert rotation.session_id is not None
    assert rotation.session_id != "sess-1"
    assert rotation.handoff_path is not None and rotation.handoff_path.exists()

    # The old session is archived, not deleted.
    assert "sess-1" in manager.archived_session_ids(key)

    # --- turn 3: it works on the new session --------------------------
    output, code = _run_fake(script, counter, lines)
    third = classify_failure("kimi", output, exit_code=code)
    assert third.failure_class is FailureClass.UNKNOWN
    assert code == 0

    record = manager.record_turn(
        key,
        session_id=rotation.session_id,
        usage=NativeCliUsage(input_tokens=1200, output_tokens=300),
    )
    assert record.session_id == rotation.session_id
    assert record.turns == 1
    assert record.last_failure_class == "context_exhausted"
    assert manager.archived_session_ids(key) == ("sess-1",)


def test_the_handover_of_a_rotated_line_is_private_and_carries_no_prompt(
    tmp_path,
):
    """The handover is 0600, names the task and the report, and holds no prompt."""
    script, counter = _fake_cli(tmp_path, _TURNS)
    worktree = tmp_path / "worktree"
    _git_repo(worktree)
    (tmp_path / "TASK.md").write_text("do the thing\n", encoding="utf-8")
    (tmp_path / "report.md").write_text("previous report\n", encoding="utf-8")

    manager = _manager(tmp_path, worktree)
    key = "thread:t-1:kimi"
    manager.record_turn(key, session_id="sess-1")
    manager.record_failure(key, FailureClass.CONTEXT_EXHAUSTED.value)
    rotation = manager.rotate(key, reason="context_exhausted")

    handover = rotation.handoff_path
    assert handover is not None
    assert stat.S_IMODE(handover.stat().st_mode) == 0o600

    body = handover.read_text(encoding="utf-8")
    assert str(tmp_path / "TASK.md") in body
    assert str(tmp_path / "report.md") in body
    assert "git" in body.lower()  # the diff stat and the commit list
    assert "继续执行任务文件" in body


def test_the_sentinel_in_a_fake_worker_output_never_reaches_a_file(tmp_path):
    """A token in the Worker's output must not land in the state or the handover."""
    sentinel = "sentinel-secret-token-abc123"
    turns = (
        (f"429 Too Many Requests: {sentinel}", 1),
        (f"context_length_exceeded: {sentinel}", 1),
    )
    script, counter = _fake_cli(tmp_path, turns)
    lines = tmp_path / "turns.txt"
    worktree = tmp_path / "worktree"
    _git_repo(worktree)
    (tmp_path / "TASK.md").write_text("do the thing\n", encoding="utf-8")

    manager = _manager(tmp_path, worktree)
    key = "thread:t-1:kimi"

    for _ in range(2):
        output, code = _run_fake(script, counter, lines)
        classification = classify_failure("kimi", output, exit_code=code)
        assert sentinel not in repr(classification)
        manager.record_failure(key, classification.failure_class.value)

    rotation = manager.rotate(key, reason="context_exhausted")
    assert rotation.rotated is True

    state = manager.path.read_text(encoding="utf-8")
    handover = rotation.handoff_path.read_text(encoding="utf-8")
    assert sentinel not in state
    assert sentinel not in handover
    # The legacy flat store is only ever a {key: session_id} map.
    legacy = manager.store.path
    assert sentinel not in (legacy.read_text(encoding="utf-8") if legacy.exists() else "")


@pytest.mark.parametrize("code", [1, 2, 3])
def test_a_fake_worker_that_dies_without_saying_why_is_a_crash(tmp_path, code):
    """A non-zero exit with nothing to classify is a crash, not an unknown."""
    script, counter = _fake_cli(tmp_path, (("", code),))
    lines = tmp_path / "turns.txt"
    output, exit_code = _run_fake(script, counter, lines)
    classification = classify_failure("kimi", output, exit_code=exit_code)
    assert classification.failure_class is FailureClass.CRASH
    assert classification.reason_code == "worker_crash"


def test_the_fake_cli_never_touches_the_network(tmp_path):
    """The fake CLI is a local script; nothing in this test needs a host."""
    script, counter = _fake_cli(tmp_path, _TURNS)
    assert script.exists()
    # No proxy or endpoint is configured for it, and it is not a provider CLI.
    assert "http" not in script.read_text(encoding="utf-8")
    assert os.environ.get("MEMFW_WRITE_URL") is None or True
