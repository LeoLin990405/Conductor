"""N27: a G2 failure the base commit already has is ``preexisting_failure``.

The planner wrote an acceptance check main itself failed, so the line could
never pass G2. A failed focused command is re-run at the line's base commit;
the same failure there is classified ``preexisting_failure``. Default mode is
shadow: the classification is recorded and the outcome stays
``focused_tests_failed``. Enforce reports ``preexisting_failure``.
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest

from src.tools.auto_dispatch.check_modes import SCOPE_CHECK_ENV, WORKTREE_CHECK_ENV
from src.tools.auto_dispatch.gates import (
    FOCUSED_TESTS_FAILED,
    FOCUSED_TESTS_PASSED,
    CommandResult,
    GateRunner,
    GateSettings,
    g2_failure_note,
)
from src.tools.auto_dispatch.preexisting import (
    LINE_FAILURE,
    PREEXISTING_FAILURE,
    PREEXISTING_MODE_ENV,
    UNKNOWN,
    classify,
    failed_test_ids,
)
from src.tools.auto_dispatch.types import Dispatch, Line

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PREEXISTING_MODE_ENV, raising=False)
    monkeypatch.setenv(WORKTREE_CHECK_ENV, "off")
    monkeypatch.setenv(SCOPE_CHECK_ENV, "off")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path, base: str, head: str) -> tuple[Path, str, str]:
    """``results.txt`` says what the fake pytest reports: exit code, then FAILED ids."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Dispatch Test")
    _git(root, "config", "user.email", "dispatch@example.invalid")
    (root / "results.txt").write_text(base, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    base_sha = _git(root, "rev-parse", "HEAD")
    branch = "feature/auto-dispatch-d1/line-line-0"
    _git(root, "checkout", "-q", "-b", branch)
    (root / "results.txt").write_text(head, encoding="utf-8")
    (root / "work.py").write_text("done = True\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "work")
    _git(root, "checkout", "-q", "main")
    return root, base_sha, branch


class _FakePytest:
    """Prints a pytest-like summary from ``results.txt`` in its cwd."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []

    async def run(self, argv, *, cwd, timeout, log_path):
        self.calls.append((list(argv), Path(cwd)))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        first, *ids = (Path(cwd) / "results.txt").read_text().splitlines()
        body = "".join(f"FAILED {item} - AssertionError: boom\n" for item in ids)
        log_path.write_text(
            "...F\n=== short test summary info ===\n" + body, encoding="utf-8"
        )
        return CommandResult(exit_code=int(first), duration_ms=1)


def _gate(tmp_path: Path, root: Path, base: str, branch: str, commands):
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
        acceptance=("cmd: pytest -q tests/test_x.py",),
        branch=branch,
        base_commit=base,
    )
    dispatch = Dispatch(id="d1", goal="g", user_id="u", created_at="now", lines=[line])
    return gate, dispatch, line, settings


def _record(gate: GateRunner, dispatch: Dispatch, line: Line) -> dict:
    return gate._line(dispatch, line.id)["gates"]["G2"]["commands"][0]


async def test_shadow_records_preexisting_and_keeps_the_outcome(tmp_path: Path) -> None:
    root, base, branch = _repo(tmp_path, "1\ntests/test_x.py::test_a", "1\ntests/test_x.py::test_a")
    commands = _FakePytest()
    gate, dispatch, line, settings = _gate(tmp_path, root, base, branch, commands)

    step = await gate.run_g2(dispatch, line)

    assert not step.passed
    assert step.reason == FOCUSED_TESTS_FAILED
    found = _record(gate, dispatch, line)["preexisting"]
    assert found["classification"] == PREEXISTING_FAILURE
    assert found["mode"] == "shadow" and found["enforced"] is False
    assert found["base_exit_code"] == 1
    # The base run ran in its own checkout, which is gone afterwards.
    assert len(commands.calls) == 2
    base_cwd = commands.calls[1][1]
    assert base_cwd != commands.calls[0][1] and not base_cwd.exists()
    assert (settings.runs_dir / found["base_log_ref"]).is_file()
    # Shadow changes nothing the author is told: the fix-round note is the
    # one G2 would write without the classification.
    cell = gate._line(dispatch, line.id)["gates"]["G2"]
    note = g2_failure_note(cell, settings.runs_dir)
    assert "preexisting" not in note
    plain = copy.deepcopy(cell)
    for command in plain["commands"]:
        command.pop("preexisting", None)
    assert note == g2_failure_note(plain, settings.runs_dir)


async def test_enforce_reports_preexisting_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PREEXISTING_MODE_ENV, "enforce")
    root, base, branch = _repo(tmp_path, "1\nt::a\nt::b", "1\nt::a")
    gate, dispatch, line, settings = _gate(tmp_path, root, base, branch, _FakePytest())

    step = await gate.run_g2(dispatch, line)

    assert not step.passed
    assert step.reason == PREEXISTING_FAILURE
    assert _record(gate, dispatch, line)["preexisting"]["enforced"] is True
    note = g2_failure_note(gate._line(dispatch, line.id)["gates"]["G2"], settings.runs_dir)
    assert "preexisting failure" in note


async def test_enforce_keeps_a_new_failure_the_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PREEXISTING_MODE_ENV, "enforce")
    root, base, branch = _repo(tmp_path, "1\nt::a", "1\nt::a\nt::new")
    gate, dispatch, line, _settings = _gate(tmp_path, root, base, branch, _FakePytest())

    step = await gate.run_g2(dispatch, line)

    assert step.reason == FOCUSED_TESTS_FAILED
    found = _record(gate, dispatch, line)["preexisting"]
    assert found["classification"] == LINE_FAILURE
    assert found["new_failed_ids"] == ["t::new"]


async def test_base_passing_is_the_lines_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PREEXISTING_MODE_ENV, "enforce")
    root, base, branch = _repo(tmp_path, "0", "1\nt::a")
    gate, dispatch, line, _settings = _gate(tmp_path, root, base, branch, _FakePytest())

    step = await gate.run_g2(dispatch, line)

    assert step.reason == FOCUSED_TESTS_FAILED
    found = _record(gate, dispatch, line)["preexisting"]
    assert (found["classification"], found["reason"]) == (LINE_FAILURE, "base_passed")


async def test_off_reruns_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PREEXISTING_MODE_ENV, "off")
    root, base, branch = _repo(tmp_path, "1\nt::a", "1\nt::a")
    commands = _FakePytest()
    gate, dispatch, line, _settings = _gate(tmp_path, root, base, branch, commands)

    step = await gate.run_g2(dispatch, line)

    assert step.reason == FOCUSED_TESTS_FAILED
    assert len(commands.calls) == 1
    assert "preexisting" not in _record(gate, dispatch, line)


async def test_passing_g2_reruns_nothing(tmp_path: Path) -> None:
    root, base, branch = _repo(tmp_path, "1\nt::a", "0")
    commands = _FakePytest()
    gate, dispatch, line, _settings = _gate(tmp_path, root, base, branch, commands)

    step = await gate.run_g2(dispatch, line)

    assert step.passed and step.reason == FOCUSED_TESTS_PASSED
    assert len(commands.calls) == 1


async def test_no_base_commit_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PREEXISTING_MODE_ENV, "enforce")
    root, _base, branch = _repo(tmp_path, "1\nt::a", "1\nt::a")
    gate, dispatch, line, _settings = _gate(tmp_path, root, "", branch, _FakePytest())

    step = await gate.run_g2(dispatch, line)

    assert step.reason == FOCUSED_TESTS_FAILED
    found = _record(gate, dispatch, line)["preexisting"]
    assert (found["classification"], found["reason"]) == (UNKNOWN, "no_base_commit")


async def test_classify_rules() -> None:
    summary = "FAILED tests/a.py::t[x - y] - AssertionError\nERROR tests/b.py - boom\n"
    assert failed_test_ids(summary) == frozenset({"tests/a.py::t[x - y]", "tests/b.py"})
    assert failed_test_ids("all good\n") is None
    # A test file the line added: pytest exits 4 at the base, 1 at the head.
    assert classify(1, "FAILED t::a", 4, "")["reason"] == "exit_code_differs"
    # Not pytest: a matching non-zero exit code is the evidence.
    plain = classify(2, "boom", 2, "boom")
    assert plain["classification"] == PREEXISTING_FAILURE and plain["basis"] == "exit_code"
    # Head names tests, base does not (e.g. a collection error): the line's.
    assert classify(1, "FAILED t::a", 1, "error")["classification"] == LINE_FAILURE
    assert classify(1, "FAILED t::a", None, "")["classification"] == UNKNOWN
