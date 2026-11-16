"""Process-wide live-launch registry and the read-only uncommitted diff view."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import src.tools.auto_dispatch.execution as execution_module
from src.tools.auto_dispatch import LineStatus
from src.tools.auto_dispatch.execution import (
    ScriptLauncher,
    cancel_live_run,
    live_run_ids,
    live_runs_for_line,
)
from src.tools.auto_dispatch.types import Line
from tests.unit.tools.auto_dispatch.test_machine_gates import _line

pytestmark = pytest.mark.asyncio

DISPATCH_ID = "d-live"


class _FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode


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


@pytest.fixture(autouse=True)
def _forget_fake_runs():
    yield
    for launcher in list(execution_module._LIVE_LAUNCHERS):
        for run_id, process in list(launcher._processes.items()):
            if isinstance(process, _FakeProcess):
                launcher._processes.pop(run_id, None)


def _launcher(tmp_path: Path, *, cwd: Path | None = None) -> ScriptLauncher:
    state = tmp_path / "state"
    return ScriptLauncher(
        state / "runs",
        cwd=cwd or tmp_path,
        worktrees_dir=state / "worktrees",
        dispatch_id=DISPATCH_ID,
    )


# -- registry ---------------------------------------------------------------


async def test_registry_tracks_live_and_drops_finished(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    live = f"{DISPATCH_ID}:L:1:{'a' * 12}"
    done = f"{DISPATCH_ID}:L:0:{'b' * 12}"
    launcher._processes[live] = _FakeProcess()  # type: ignore[assignment]
    launcher._processes[done] = _FakeProcess(returncode=0)  # type: ignore[assignment]

    ids = live_run_ids()
    assert live in ids
    assert done not in ids

    launcher._processes[live].returncode = -15  # type: ignore[misc]
    assert live not in live_run_ids()


async def test_live_runs_for_line_matches_exact_line_prefix(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    line_1 = f"{DISPATCH_ID}:1:2:{'c' * 12}"
    line_10 = f"{DISPATCH_ID}:10:1:{'d' * 12}"
    other_dispatch = f"{DISPATCH_ID}x:1:1:{'e' * 12}"
    for run_id in (line_1, line_10, other_dispatch):
        launcher._processes[run_id] = _FakeProcess()  # type: ignore[assignment]

    assert live_runs_for_line(DISPATCH_ID, "1") == {line_1}
    assert live_runs_for_line(DISPATCH_ID, "10") == {line_10}
    assert live_runs_for_line(DISPATCH_ID, "2") == set()


async def test_cancel_live_run_uses_launcher_stop_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _launcher(tmp_path)
    run_id = f"{DISPATCH_ID}:L:1:{'f' * 12}"
    process = _FakeProcess()
    launcher._processes[run_id] = process  # type: ignore[assignment]
    stopped: list[object] = []

    async def fake_stop(proc: object) -> None:
        stopped.append(proc)
        proc.returncode = -15  # type: ignore[attr-defined]

    monkeypatch.setattr(execution_module, "_stop_launched", fake_stop)

    assert await cancel_live_run(run_id) is True
    assert stopped == [process]
    assert run_id not in launcher._processes
    assert run_id not in live_run_ids()

    assert await cancel_live_run(run_id) is False
    assert await cancel_live_run("unknown:run:0:000000000000") is False
    assert stopped == [process]


# -- uncommitted_diff --------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Live Runs Test")
    _git(repo, "config", "user.email", "live@example.invalid")
    _git(repo, "config", "core.hooksPath", "/dev/null")
    _git(repo, "config", "core.fsmonitor", "false")
    (repo / "README").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _owned_line(base: str) -> Line:
    line = _line("L", assigned_model="kimi", status=LineStatus.ASSIGNED)
    line.branch = f"feature/auto-dispatch-{DISPATCH_ID}/line-L"
    line.base_commit = base
    return line


async def _prepared(tmp_path: Path) -> tuple[ScriptLauncher, Line, Path]:
    repo, base = _repo(tmp_path)
    launcher = _launcher(tmp_path, cwd=repo)
    line = _owned_line(base)
    worktree = await launcher._prepare_line_worktree(line)
    return launcher, line, worktree


async def test_uncommitted_diff_reports_dirty_worktree(tmp_path: Path) -> None:
    launcher, line, worktree = await _prepared(tmp_path)
    (worktree / "README").write_text("base\nchanged by worker\n", encoding="utf-8")
    (worktree / "notes").mkdir()
    (worktree / "notes" / "scratch.txt").write_text("SECRET_UNTRACKED_BODY\n", encoding="utf-8")

    text = await launcher.uncommitted_diff(line)

    assert text is not None
    assert " M README" in text
    assert "+changed by worker" in text
    assert "notes/scratch.txt" in text
    assert "SECRET_UNTRACKED_BODY" not in text
    # read-only: the worktree is still dirty and still registered
    assert _git(worktree, "status", "--porcelain")


async def test_uncommitted_diff_none_for_clean_worktree(tmp_path: Path) -> None:
    launcher, line, _worktree = await _prepared(tmp_path)
    assert await launcher.uncommitted_diff(line) is None


async def test_uncommitted_diff_none_for_missing_worktree(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    launcher = _launcher(tmp_path, cwd=repo)
    line = _owned_line(base)
    assert await launcher.uncommitted_diff(line) is None
    # nothing was created on the way
    assert not (tmp_path / "state" / "worktrees").exists()

    line.branch = ""
    assert await launcher.uncommitted_diff(line) is None


async def test_uncommitted_diff_none_for_unregistered_branch(tmp_path: Path) -> None:
    launcher, line, worktree = await _prepared(tmp_path)
    (worktree / "README").write_text("dirty\n", encoding="utf-8")
    _git(worktree, "checkout", "-q", "-b", "elsewhere")
    assert await launcher.uncommitted_diff(line) is None


async def test_uncommitted_diff_truncates_on_utf8_boundary(tmp_path: Path) -> None:
    launcher, line, worktree = await _prepared(tmp_path)
    (worktree / "README").write_text("é" * 5000 + "\n", encoding="utf-8")

    text = await launcher.uncommitted_diff(line, max_bytes=301)

    assert text is not None
    assert text.endswith("\n[truncated]")
    assert len(text.encode("utf-8")) <= 301
    text.encode("utf-8").decode("utf-8")


async def test_truncate_helper_keeps_short_text() -> None:
    assert execution_module._truncate_utf8("short", 100) == "short"
    cut = execution_module._truncate_utf8("ü" * 100, 20)
    assert cut.endswith("\n[truncated]")
    assert len(cut.encode("utf-8")) <= 20


async def test_uncommitted_diff_redacts_credentials(tmp_path: Path) -> None:
    """Review fix 3: the diff usually goes to another provider."""
    launcher, line, worktree = await _prepared(tmp_path)
    secret = "sk-" "ant-api03-" + "Z" * 40
    (worktree / "README").write_text(f"base\nOPENAI_API_KEY={secret}\n", encoding="utf-8")
    text = await launcher.uncommitted_diff(line)
    assert text is not None
    assert "+OPENAI_API_KEY=" in text
    assert secret not in text


async def test_uncommitted_diff_names_a_binary_file_only(tmp_path: Path) -> None:
    launcher, line, worktree = await _prepared(tmp_path)
    (worktree / "README").write_text("base\ntext change\n", encoding="utf-8")
    blob = bytes(range(256)) * 8
    (worktree / "image.bin").write_bytes(blob)
    _git(worktree, "add", "image.bin")
    text = await launcher.uncommitted_diff(line)
    assert text is not None
    assert "diff --git a/image.bin b/image.bin" in text
    assert "(binary file; contents not shown)" in text
    assert "GIT binary patch" not in text
    assert "+text change" in text


async def test_uncommitted_diff_skips_reading_a_huge_diff(
    tmp_path: Path, monkeypatch
) -> None:
    launcher, line, worktree = await _prepared(tmp_path)
    monkeypatch.setattr(execution_module, "_DIFF_READ_CAP_BYTES", 64)
    (worktree / "README").write_text("x" * 500 + "\n", encoding="utf-8")
    seen: list[list[str]] = []
    real = launcher._line_git

    async def spy(repo, cwd, argv):
        seen.append(list(argv))
        return await real(repo, cwd, argv)

    launcher._line_git = spy
    text = await launcher.uncommitted_diff(line)
    assert text is not None
    assert " M README" in text
    assert "diff not read" in text
    assert not any(argv[:2] == ["diff", "--binary"] for argv in seen)


async def test_before_spawn_runs_after_the_worktree_and_can_stop_the_spawn(
    tmp_path: Path,
) -> None:
    """Review fix (late launch): the fence runs after worktree prep, before spawn."""
    repo, base = _repo(tmp_path)
    script = tmp_path / "launch.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    spawned: list[list[str]] = []

    async def runner(argv, cwd, log_path):
        spawned.append(list(argv))
        return 0

    launcher = ScriptLauncher(
        tmp_path / "state" / "runs",
        script=script,
        cwd=repo,
        worktrees_dir=tmp_path / "state" / "worktrees",
        dispatch_id=DISPATCH_ID,
        runner=runner,
    )
    line = _owned_line(base)
    order: list[str] = []

    class Fenced(Exception):
        pass

    async def fence() -> None:
        # The worktree exists by now: the slow part is already done.
        order.append("fence")
        assert any((tmp_path / "state" / "worktrees").rglob("README"))
        raise Fenced

    with pytest.raises(Fenced):
        await launcher.launch_worker(
            line, "kimi", [], session_id=None, session_mode=None, handoff_path=None,
            before_spawn=fence,
        )
    assert order == ["fence"]
    assert spawned == []


async def test_a_file_truncated_on_disk_still_counts_its_head_size(
    tmp_path: Path, monkeypatch
) -> None:
    """Review fix 5: the budget uses the HEAD blob size too, not only the disk."""
    repo, _base = _repo(tmp_path)
    (repo / "big.txt").write_text("y" * 4000 + "\n", encoding="utf-8")
    _git(repo, "add", "big.txt")
    _git(repo, "commit", "-qm", "big")
    base = _git(repo, "rev-parse", "HEAD")
    launcher = _launcher(tmp_path, cwd=repo)
    line = _owned_line(base)
    worktree = await launcher._prepare_line_worktree(line)
    monkeypatch.setattr(execution_module, "_DIFF_READ_CAP_BYTES", 1000)
    (worktree / "big.txt").write_text("y", encoding="utf-8")
    seen: list[list[str]] = []
    real = launcher._line_git

    async def spy(repo_path, cwd, argv):
        seen.append(list(argv))
        return await real(repo_path, cwd, argv)

    launcher._line_git = spy
    text = await launcher.uncommitted_diff(line)
    assert text is not None
    assert "big.txt" in text
    assert "diff not read" in text
    assert not any(argv[:2] == ["diff", "--binary"] for argv in seen)
    assert ["ls-tree", "-r", "-l", "-z", "HEAD"] in seen


def test_head_sizes_parses_ls_tree() -> None:
    listing = "100644 blob " + "a" * 40 + "    1234\tsrc/x.py\0" + "040000 tree " + "b" * 40 + "       -\tsrc\0"
    assert execution_module._head_sizes(listing) == {"src/x.py": 1234}
