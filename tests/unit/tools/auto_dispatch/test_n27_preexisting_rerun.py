"""N27 follow-up: the base re-run is bounded and uses the line's interpreter.

The shadow re-run doubled G2 time (it used the full G2 timeout) and ran in a
base checkout with no venv. ``CONDUCTOR_DISPATCH_PREEXISTING_RERUN`` says
which failed commands are re-run (default: recognised pytest only; shadow
never re-runs anything else), the re-run has its own bounded timeout, and a
leading ``python``/``pytest`` is the line worktree's interpreter, never one the
base checkout's files pick.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from src.tools.auto_dispatch.check_modes import SCOPE_CHECK_ENV, WORKTREE_CHECK_ENV
from src.tools.auto_dispatch.gates import (
    FOCUSED_TESTS_FAILED,
    CommandResult,
    GateRunner,
    GateSettings,
)
from src.tools.auto_dispatch.preexisting import (
    PREEXISTING_FAILURE,
    PREEXISTING_MODE_ENV,
    RERUN_ENV,
    RERUN_TIMEOUT_ENV,
    is_pytest_command,
    rerun_argv,
    rerun_scope,
    rerun_timeout,
)
from src.tools.auto_dispatch.types import Dispatch, Line

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _modes(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (PREEXISTING_MODE_ENV, RERUN_ENV, RERUN_TIMEOUT_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(WORKTREE_CHECK_ENV, "off")
    monkeypatch.setenv(SCOPE_CHECK_ENV, "off")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(
    tmp_path: Path, *, base_venv: bool = False, line_drops_venv: bool = False
) -> tuple[Path, str, str]:
    """Head and base both fail ``t::a`` with exit 1 (a preexisting failure)."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Dispatch Test")
    _git(root, "config", "user.email", "dispatch@example.invalid")
    (root / "results.txt").write_text("1\nt::a", encoding="utf-8")
    if base_venv:
        planted = root / ".venv" / "bin" / "python"
        planted.parent.mkdir(parents=True)
        planted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        planted.chmod(0o755)
        _git(root, "add", "-f", ".venv/bin/python")
    _git(root, "add", "results.txt")
    _git(root, "commit", "-q", "-m", "base")
    base_sha = _git(root, "rev-parse", "HEAD")
    branch = "feature/auto-dispatch-d1/line-line-0"
    _git(root, "checkout", "-q", "-b", branch)
    (root / "work.py").write_text("done = True\n", encoding="utf-8")
    if line_drops_venv:
        _git(root, "rm", "-q", ".venv/bin/python")
    _git(root, "add", "work.py")
    _git(root, "commit", "-q", "-m", "work")
    _git(root, "checkout", "-q", "main")
    return root, base_sha, branch


class _Fake:
    """Prints a pytest-like summary; can give the line worktree a venv."""

    def __init__(self, *, line_venv: bool = False) -> None:
        self.calls: list[dict] = []
        self.line_venv = line_venv

    async def run(self, argv, *, cwd, timeout, log_path):
        cwd = Path(cwd)
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        if self.line_venv and len(self.calls) == 1:
            python = cwd / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            python.chmod(0o755)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        first, *ids = (cwd / "results.txt").read_text().splitlines()
        body = "".join(f"FAILED {item} - AssertionError: boom\n" for item in ids)
        log_path.write_text("=== short test summary info ===\n" + body, encoding="utf-8")
        return CommandResult(exit_code=int(first), duration_ms=1)


def _gate(tmp_path: Path, root: Path, base: str, branch: str, commands, command: str):
    settings = GateSettings(
        runs_dir=tmp_path / "state" / "runs",
        repo_root=root,
        worktrees_dir=tmp_path / "state" / "worktrees",
    )
    gate = GateRunner(settings, commands=commands)
    line = Line(
        id="line-0",
        name="line",
        task="Implement",
        acceptance=(f"cmd: {command}",),
        branch=branch,
        base_commit=base,
    )
    dispatch = Dispatch(id="d1", goal="g", user_id="u", created_at="now", lines=[line])
    return gate, dispatch, line


def _record(gate: GateRunner, dispatch: Dispatch, line: Line) -> dict:
    return gate._line(dispatch, line.id)["gates"]["G2"]["commands"][0]


async def test_shadow_never_reruns_a_non_pytest_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even when the scope is forced to ``all``, shadow re-runs only pytest.
    monkeypatch.setenv(RERUN_ENV, "all")
    root, base, branch = _repo(tmp_path)
    fake = _Fake()
    gate, dispatch, line = _gate(tmp_path, root, base, branch, fake, "make check")

    step = await gate.run_g2(dispatch, line)

    assert step.reason == FOCUSED_TESTS_FAILED
    assert len(fake.calls) == 1
    assert _record(gate, dispatch, line)["preexisting"]["reason"] == "rerun_not_pytest"


async def test_default_scope_skips_non_pytest_even_in_enforce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PREEXISTING_MODE_ENV, "enforce")
    root, base, branch = _repo(tmp_path)
    fake = _Fake()
    gate, dispatch, line = _gate(tmp_path, root, base, branch, fake, "make check")

    step = await gate.run_g2(dispatch, line)

    assert step.reason == FOCUSED_TESTS_FAILED
    assert len(fake.calls) == 1


async def test_enforce_with_all_reruns_a_non_pytest_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PREEXISTING_MODE_ENV, "enforce")
    monkeypatch.setenv(RERUN_ENV, "all")
    root, base, branch = _repo(tmp_path)
    fake = _Fake()
    gate, dispatch, line = _gate(tmp_path, root, base, branch, fake, "make check")

    step = await gate.run_g2(dispatch, line)

    assert len(fake.calls) == 2
    assert fake.calls[1]["argv"] == ["make", "check"]
    assert step.reason == PREEXISTING_FAILURE


async def test_rerun_off_reruns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PREEXISTING_MODE_ENV, "enforce")
    monkeypatch.setenv(RERUN_ENV, "off")
    root, base, branch = _repo(tmp_path)
    fake = _Fake()
    gate, dispatch, line = _gate(tmp_path, root, base, branch, fake, "pytest -q t.py")

    step = await gate.run_g2(dispatch, line)

    assert step.reason == FOCUSED_TESTS_FAILED
    assert len(fake.calls) == 1
    assert "preexisting" not in _record(gate, dispatch, line)


async def test_rerun_has_its_own_default_timeout(tmp_path: Path) -> None:
    root, base, branch = _repo(tmp_path)
    fake = _Fake()
    gate, dispatch, line = _gate(tmp_path, root, base, branch, fake, "pytest -q t.py")

    await gate.run_g2(dispatch, line)

    assert fake.calls[0]["timeout"] == 900.0
    assert fake.calls[1]["timeout"] == 300.0
    assert _record(gate, dispatch, line)["preexisting"]["rerun_timeout_seconds"] == 300.0


async def test_rerun_timeout_env_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RERUN_TIMEOUT_ENV, "45")
    root, base, branch = _repo(tmp_path)
    fake = _Fake()
    gate, dispatch, line = _gate(tmp_path, root, base, branch, fake, "pytest -q t.py")

    await gate.run_g2(dispatch, line)

    assert fake.calls[1]["timeout"] == 45.0


async def test_rerun_uses_the_line_worktree_interpreter(tmp_path: Path) -> None:
    root, base, branch = _repo(tmp_path)
    fake = _Fake(line_venv=True)
    gate, dispatch, line = _gate(tmp_path, root, base, branch, fake, "pytest -q t.py")

    await gate.run_g2(dispatch, line)

    line_cwd = fake.calls[0]["cwd"]
    assert fake.calls[1]["argv"] == [
        str(line_cwd / ".venv" / "bin" / "python"), "-m", "pytest", "-q", "t.py",
    ]
    assert fake.calls[1]["cwd"] != line_cwd
    found = _record(gate, dispatch, line)["preexisting"]
    assert found["interpreter"] == "line_venv"
    assert found["classification"] == PREEXISTING_FAILURE


async def test_base_checkout_never_picks_the_interpreter(tmp_path: Path) -> None:
    # The base commit carries its own .venv/bin/python; the line removed it
    # and has no venv. A relative interpreter would resolve inside the base
    # checkout, so the command is not re-run.
    root, base, branch = _repo(tmp_path, base_venv=True, line_drops_venv=True)
    fake = _Fake()
    gate, dispatch, line = _gate(
        tmp_path, root, base, branch, fake, ".venv/bin/python -m pytest -q t.py"
    )

    step = await gate.run_g2(dispatch, line)

    assert step.reason == FOCUSED_TESTS_FAILED
    assert len(fake.calls) == 1
    found = _record(gate, dispatch, line)["preexisting"]
    assert found["reason"] == "no_line_interpreter"


async def test_rerun_helpers(tmp_path: Path) -> None:
    assert rerun_scope({}) == "pytest"
    assert rerun_scope({RERUN_ENV: "ALL"}) == "all"
    assert rerun_scope({RERUN_ENV: "off"}) == "off"
    assert rerun_scope({RERUN_ENV: "bogus"}) == "pytest"

    assert rerun_timeout(900.0, {}) == 300.0
    assert rerun_timeout(900.0, {RERUN_TIMEOUT_ENV: "99999"}) == 900.0
    assert rerun_timeout(None, {RERUN_TIMEOUT_ENV: "99999"}) == 3600.0
    assert rerun_timeout(900.0, {RERUN_TIMEOUT_ENV: "0"}) == 1.0
    assert rerun_timeout(900.0, {RERUN_TIMEOUT_ENV: "nan"}) == 300.0
    assert rerun_timeout(900.0, {RERUN_TIMEOUT_ENV: "x"}) == 300.0
    assert rerun_timeout(60.0, {}) == 60.0

    assert is_pytest_command(["pytest", "-q"])
    assert is_pytest_command(["py.test"])
    assert is_pytest_command([".venv/bin/pytest", "-q"])
    assert is_pytest_command(["python3", "-W", "ignore", "-m", "pytest"])
    assert not is_pytest_command(["python", "-c", "import pytest"])
    assert not is_pytest_command(["python", "-m", "mypy"])
    assert not is_pytest_command(["uv", "run", "pytest"])
    assert not is_pytest_command(["ruff", "check", "tests/pytest"])
    assert not is_pytest_command([])

    line_wt = tmp_path / "line"
    assert rerun_argv(["pytest", "-q"], line_wt) == (["pytest", "-q"], "path")
    assert rerun_argv(["python", "-m", "pytest"], None) == (
        ["python", "-m", "pytest"], "path",
    )
    assert rerun_argv([".venv/bin/python", "-m", "pytest"], line_wt) == (
        None, "no_line_interpreter",
    )
    assert rerun_argv(["/usr/bin/python3", "-m", "pytest"], line_wt) == (
        ["/usr/bin/python3", "-m", "pytest"], "absolute",
    )
    assert rerun_argv(["make", "check"], line_wt) == (["make", "check"], "unchanged")

    python = line_wt / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o644)
    # Not executable: not an interpreter.
    assert rerun_argv(["pytest"], line_wt) == (["pytest"], "path")
    python.chmod(0o755)
    assert os.access(python, os.X_OK)
    assert rerun_argv(["pytest", "-q"], line_wt) == (
        [str(python), "-m", "pytest", "-q"], "line_venv",
    )
    assert rerun_argv([".venv/bin/python", "-m", "pytest", "-q"], line_wt) == (
        [str(python), "-m", "pytest", "-q"], "line_venv",
    )
    assert rerun_argv(["./venv/bin/pytest", "x"], line_wt) == (
        [str(python), "-m", "pytest", "x"], "line_venv",
    )
