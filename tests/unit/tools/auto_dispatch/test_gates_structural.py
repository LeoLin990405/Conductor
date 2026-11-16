"""The structural checks G2 makes on the worktree it just tested.

N47: the focused test run must not leave changes in the checkout. A test
that writes a stub module into ``src/`` and leaves it there breaks the rule
that tests write to tmp only.

N52: the line's diff must fit its write scope. The gate compares the file
names itself and emits a structured conflict tagged ``scope:``, so the
verdict no longer depends on a reviewer writing the word "scope".
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from src.tools.auto_dispatch.check_modes import ENFORCE, WORKTREE_CHECK_ENV, SCOPE_CHECK_ENV

import src.tools.auto_dispatch.gates as gates_module
from src.tools.auto_dispatch.gates import (
    G2_SCOPE_CONFLICT,
    G2_WORKTREE_DIRTY,
    CommandResult,
    GateRunner,
    GateSettings,
    g2_failure_note,
    path_is_temporary,
    scope_conflict_finding,
    scope_violations,
)
from src.tools.auto_dispatch.machine import conflict_tag
from src.tools.auto_dispatch.types import Dispatch, Line


@pytest.fixture(autouse=True)
def _n73_enforce_still_reachable(monkeypatch):
    """N73 made env ``enforce`` record-only; these tests pin the enforce path."""
    monkeypatch.setenv("CONDUCTOR_DISPATCH_RECORD_ONLY_CHECKS", "0")


@pytest.fixture(autouse=True)
def _enforce_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests pin the line's enforce behaviour; check_modes covers the rest (N58)."""
    monkeypatch.setenv(WORKTREE_CHECK_ENV, ENFORCE)
    monkeypatch.setenv(SCOPE_CHECK_ENV, ENFORCE)



def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class _ScriptedCommands:
    """A command runner that leaves behind whatever the test run wrote."""

    def __init__(
        self,
        writes: Sequence[tuple[str, str]] = (),
        *,
        moves: Sequence[tuple[str, str]] = (),
        exit_code: int = 0,
    ) -> None:
        self._writes = list(writes)
        self._moves = list(moves)
        self._exit_code = exit_code
        self.calls: list[tuple[list[str], Path]] = []

    async def run(self, argv, *, cwd, timeout, log_path):
        self.calls.append((list(argv), Path(cwd)))
        for relative, content in self._writes:
            target = Path(cwd) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for source, destination in self._moves:
            target = Path(cwd) / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            (Path(cwd) / source).rename(target)
        return CommandResult(exit_code=self._exit_code, duration_ms=1)


class _Repo:
    """A repository with one branch holding the line's work."""

    def __init__(self, tmp_path: Path, changed: Sequence[str]) -> None:
        self.root = tmp_path / "repo"
        self.root.mkdir()
        _git(self.root, "init", "-q", "-b", "main")
        _git(self.root, "config", "user.name", "Dispatch Test")
        _git(self.root, "config", "user.email", "dispatch@example.invalid")
        (self.root / "base.txt").write_text("base\n", encoding="utf-8")
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "base")
        self.base = _git(self.root, "rev-parse", "HEAD")
        self.branch = "feature/auto-dispatch-d1/line-line-0"
        _git(self.root, "checkout", "-q", "-b", self.branch)
        for name in changed:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"{name} changed\n", encoding="utf-8")
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "the line's work")
        self.revision = _git(self.root, "rev-parse", "HEAD")
        _git(self.root, "checkout", "-q", "main")


def _gate(tmp_path: Path, repo: _Repo, commands: _ScriptedCommands) -> GateRunner:
    settings = GateSettings(
        runs_dir=tmp_path / "state" / "runs",
        repo_root=repo.root,
        g2_command="pytest -q",
        worktrees_dir=tmp_path / "state" / "worktrees",
    )
    return GateRunner(settings, commands=commands)


def _line(repo: _Repo, write_scope: Sequence[str]) -> Line:
    return Line(
        id="line-0",
        name="test line",
        task="Implement the line",
        acceptance=("cmd: pytest -q",),
        branch=repo.branch,
        base_commit=repo.base,
        write_scope=tuple(write_scope),
    )


def _dispatch(line: Line) -> Dispatch:
    return Dispatch(
        id="d1", goal="goal", user_id="u", created_at="now", lines=[line]
    )


# --------------------------------------------------------------------------
# N47: the test run must not leave changes outside tmp
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_test_that_writes_a_stub_into_the_source_tree_fails_g2(
    tmp_path: Path,
) -> None:
    repo = _Repo(tmp_path, ("in_scope.txt",))
    commands = _ScriptedCommands([("src/stub_module.py", "stub\n")])
    gate = _gate(tmp_path, repo, commands)
    line = _line(repo, ("in_scope.txt",))
    dispatch = _dispatch(line)

    step = await gate.run_g2(dispatch, line)

    assert not step.passed
    assert step.reason == G2_WORKTREE_DIRTY
    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    assert g2["result"] == "fail"
    assert g2["dirty_worktree"]["paths"] == ["src/stub_module.py"]
    assert g2["dirty_worktree"]["unknown"] is False
    # The recorded reason does not smuggle the file's content in.
    assert "stub\n" not in repr(g2)


@pytest.mark.asyncio
async def test_a_test_that_writes_only_into_tmp_passes_g2(tmp_path: Path) -> None:
    repo = _Repo(tmp_path, ("in_scope.txt",))
    commands = _ScriptedCommands([("tmp/scratch.txt", "scratch\n")])
    gate = _gate(tmp_path, repo, commands)
    line = _line(repo, ("in_scope.txt",))
    dispatch = _dispatch(line)

    step = await gate.run_g2(dispatch, line)

    assert step.passed
    assert step.reason == "focused_tests_passed"
    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    assert "dirty_worktree" not in g2


@pytest.mark.asyncio
async def test_a_test_that_leaves_a_generated_cache_passes_g2(tmp_path: Path) -> None:
    repo = _Repo(tmp_path, ("in_scope.txt",))
    commands = _ScriptedCommands(
        [
            ("src/__pycache__/module.cpython-313.pyc", "bytecode\n"),
            (".pytest_cache/CACHEDIR.TAG", "tag\n"),
        ]
    )
    gate = _gate(tmp_path, repo, commands)
    line = _line(repo, ("in_scope.txt",))
    dispatch = _dispatch(line)

    step = await gate.run_g2(dispatch, line)

    assert step.passed
    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    assert "dirty_worktree" not in g2


@pytest.mark.asyncio
async def test_a_test_that_renames_a_source_file_away_fails_g2(tmp_path: Path) -> None:
    repo = _Repo(tmp_path, ("in_scope.txt",))
    commands = _ScriptedCommands(moves=[("in_scope.txt", "tmp/moved.txt")])
    gate = _gate(tmp_path, repo, commands)
    line = _line(repo, ("in_scope.txt",))
    dispatch = _dispatch(line)

    step = await gate.run_g2(dispatch, line)

    # A rename out of the source tree removes a tracked file, which is a
    # change outside tmp.
    assert not step.passed
    assert step.reason == G2_WORKTREE_DIRTY
    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    assert "in_scope.txt" in g2["dirty_worktree"]["paths"]


@pytest.mark.asyncio
async def test_a_test_that_edits_a_tracked_file_outside_tmp_fails_g2(
    tmp_path: Path,
) -> None:
    repo = _Repo(tmp_path, ("in_scope.txt",))
    commands = _ScriptedCommands([("base.txt", "edited by a test\n")])
    gate = _gate(tmp_path, repo, commands)
    line = _line(repo, ("in_scope.txt",))
    dispatch = _dispatch(line)

    step = await gate.run_g2(dispatch, line)

    assert not step.passed
    assert step.reason == G2_WORKTREE_DIRTY
    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    assert g2["dirty_worktree"]["paths"] == ["base.txt"]


@pytest.mark.asyncio
async def test_a_worktree_whose_status_cannot_be_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _Repo(tmp_path, ("in_scope.txt",))
    gate = _gate(tmp_path, repo, _ScriptedCommands())
    line = _line(repo, ("in_scope.txt",))
    dispatch = _dispatch(line)
    monkeypatch.setattr(gates_module, "git_status_paths", lambda _checkout: None)

    step = await gate.run_g2(dispatch, line)

    assert not step.passed
    assert step.reason == G2_WORKTREE_DIRTY
    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    assert g2["dirty_worktree"]["unknown"] is True


@pytest.mark.asyncio
async def test_the_dirty_finding_tells_the_worker_which_paths_it_left(
    tmp_path: Path,
) -> None:
    repo = _Repo(tmp_path, ("in_scope.txt",))
    commands = _ScriptedCommands([("src/stub_module.py", "stub\n")])
    gate = _gate(tmp_path, repo, commands)
    line = _line(repo, ("in_scope.txt",))
    dispatch = _dispatch(line)

    await gate.run_g2(dispatch, line)

    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    note = g2_failure_note(g2, gate._settings.runs_dir)
    assert "src/stub_module.py" in note
    assert "tmp" in note


@pytest.mark.asyncio
async def test_a_failing_test_that_also_pollutes_the_worktree_reports_both(
    tmp_path: Path,
) -> None:
    repo = _Repo(tmp_path, ("in_scope.txt",))
    commands = _ScriptedCommands([("src/stub_module.py", "stub\n")], exit_code=1)
    gate = _gate(tmp_path, repo, commands)
    line = _line(repo, ("in_scope.txt",))
    dispatch = _dispatch(line)

    step = await gate.run_g2(dispatch, line)

    # The failing test is the reason the round failed; the pollution is
    # reported next to it rather than hidden.
    assert not step.passed
    assert step.reason == "focused_tests_failed"
    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    assert g2["dirty_worktree"]["paths"] == ["src/stub_module.py"]
    note = g2_failure_note(g2, gate._settings.runs_dir)
    assert "src/stub_module.py" in note


# --------------------------------------------------------------------------
# N52: the diff must fit the write scope
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_file_outside_the_write_scope_fails_g2_with_a_structured_conflict(
    tmp_path: Path,
) -> None:
    repo = _Repo(tmp_path, ("in_scope.txt", "out_of_scope.txt"))
    gate = _gate(tmp_path, repo, _ScriptedCommands())
    line = _line(repo, ("in_scope.txt",))
    dispatch = _dispatch(line)

    step = await gate.run_g2(dispatch, line)

    assert not step.passed
    assert step.reason == G2_SCOPE_CONFLICT
    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    conflict = g2["scope_conflict"]
    assert conflict["conflict"] == "scope"
    assert conflict["files"] == ["out_of_scope.txt"]
    assert conflict["write_scope"] == ["in_scope.txt"]
    # The finding carries the tag the reviewer's findings use.
    assert conflict_tag(conflict["finding"]) == "scope"
    assert conflict["finding"].startswith("scope: ")


@pytest.mark.asyncio
async def test_a_file_inside_the_write_scope_passes_g2(tmp_path: Path) -> None:
    repo = _Repo(tmp_path, ("in_scope.txt",))
    gate = _gate(tmp_path, repo, _ScriptedCommands())
    line = _line(repo, ("in_scope.txt",))
    dispatch = _dispatch(line)

    step = await gate.run_g2(dispatch, line)

    assert step.passed
    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    assert "scope_conflict" not in g2


@pytest.mark.asyncio
async def test_a_glob_write_scope_covers_a_file_inside_it(tmp_path: Path) -> None:
    repo = _Repo(tmp_path, ("src/tools/thing.py",))
    gate = _gate(tmp_path, repo, _ScriptedCommands())
    line = _line(repo, ("src/tools/**",))
    dispatch = _dispatch(line)

    step = await gate.run_g2(dispatch, line)

    assert step.passed


@pytest.mark.asyncio
async def test_a_line_without_a_write_scope_is_not_judged(tmp_path: Path) -> None:
    repo = _Repo(tmp_path, ("in_scope.txt", "out_of_scope.txt"))
    gate = _gate(tmp_path, repo, _ScriptedCommands())
    line = _line(repo, ())
    dispatch = _dispatch(line)

    step = await gate.run_g2(dispatch, line)

    assert step.passed
    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    assert "scope_conflict" not in g2


@pytest.mark.asyncio
async def test_a_line_without_a_pinned_base_is_judged_against_the_repository_head(
    tmp_path: Path,
) -> None:
    repo = _Repo(tmp_path, ("in_scope.txt", "out_of_scope.txt"))
    gate = _gate(tmp_path, repo, _ScriptedCommands())
    line = _line(repo, ("in_scope.txt",))
    line.base_commit = ""
    dispatch = _dispatch(line)

    step = await gate.run_g2(dispatch, line)

    assert not step.passed
    assert step.reason == G2_SCOPE_CONFLICT
    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    assert g2["scope_conflict"]["files"] == ["out_of_scope.txt"]


@pytest.mark.asyncio
async def test_the_scope_conflict_reaches_the_fix_note(tmp_path: Path) -> None:
    repo = _Repo(tmp_path, ("in_scope.txt", "out_of_scope.txt"))
    gate = _gate(tmp_path, repo, _ScriptedCommands())
    line = _line(repo, ("in_scope.txt",))
    dispatch = _dispatch(line)

    await gate.run_g2(dispatch, line)

    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    note = g2_failure_note(g2, gate._settings.runs_dir)
    assert "out_of_scope.txt" in note
    # The finding is a tagged line of its own, the way a reviewer's scope
    # finding is.
    tagged = [
        line for line in note.splitlines() if conflict_tag(line) == "scope"
    ]
    assert len(tagged) == 1
    assert "out_of_scope.txt" in tagged[0]


@pytest.mark.asyncio
async def test_reviewer_prose_about_scope_makes_no_structured_conflict(
    tmp_path: Path,
) -> None:
    repo = _Repo(tmp_path, ("in_scope.txt",))
    gate = _gate(tmp_path, repo, _ScriptedCommands())
    line = _line(repo, ("in_scope.txt",))
    # Prose a reviewer may leave on the line. The word "scope" appears in
    # it, and one entry even carries the tag, but none of it is a verdict
    # about the diff.
    line.blockers = [
        "the fixture is module-scoped",
        "scope: the reviewer raised this by hand",
        "no security review was asked for",
    ]
    dispatch = _dispatch(line)

    step = await gate.run_g2(dispatch, line)

    # The gate reads the diff's file names, never the prose, so an in-scope
    # diff stays in scope.
    assert step.passed
    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    assert "scope_conflict" not in g2
    assert "reviewer_conflict_scope" not in repr(g2)
    assert g2["reason"] == "focused_tests_passed"


@pytest.mark.asyncio
async def test_the_scope_conflict_names_files_not_diffs(tmp_path: Path) -> None:
    repo = _Repo(tmp_path, ("in_scope.txt", "out_of_scope.txt"))
    gate = _gate(tmp_path, repo, _ScriptedCommands())
    line = _line(repo, ("in_scope.txt",))
    dispatch = _dispatch(line)

    await gate.run_g2(dispatch, line)

    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    conflict = g2["scope_conflict"]
    assert set(conflict) == {"conflict", "files", "count", "write_scope", "finding"}
    assert "out_of_scope.txt changed" not in repr(conflict)


class _PlainDirectoryWorktrees:
    """A worktree manager that makes a directory Git never saw."""

    async def add_detached(self, repo, path, start_point) -> None:
        path.mkdir(parents=True, exist_ok=True)

    async def remove(self, repo, path) -> None:
        if path.exists():
            shutil.rmtree(path)


@pytest.mark.asyncio
async def test_a_directory_that_is_not_a_git_checkout_has_nothing_to_report(
    tmp_path: Path,
) -> None:
    repo = _Repo(tmp_path, ("in_scope.txt",))
    settings = GateSettings(
        runs_dir=tmp_path / "state" / "runs",
        repo_root=repo.root,
        g2_command="pytest -q",
        worktrees_dir=tmp_path / "state" / "worktrees",
    )
    gate = GateRunner(
        settings,
        commands=_ScriptedCommands(),
        worktrees=_PlainDirectoryWorktrees(),
    )
    line = _line(repo, ("in_scope.txt",))
    dispatch = _dispatch(line)

    step = await gate.run_g2(dispatch, line)

    # No Git bookkeeping means no Git state, so there is nothing the test
    # run could have left behind that Git could name.
    assert step.passed
    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    assert "dirty_worktree" not in g2


# --------------------------------------------------------------------------
# The helpers the two checks are built from
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "temporary"),
    [
        ("tmp/scratch.txt", True),
        ("tests/tmp/fixture.py", True),
        (".tmp/out.txt", True),
        ("src/__pycache__/module.pyc", True),
        (".pytest_cache/CACHEDIR.TAG", True),
        (".venv/pyvenv.cfg", True),
        ("src/server/stub.py", False),
        ("src/tmpfile.py", False),
        ("temporary/x.txt", False),
        ("", False),
    ],
)
def test_only_a_real_temporary_segment_makes_a_path_temporary(
    path: str, temporary: bool
) -> None:
    assert path_is_temporary(path) is temporary


def test_scope_violations_leaves_a_file_inside_the_scope_alone() -> None:
    changed = ("src/tools/a.py", "src/tools/b.py", "docs/readme.md")
    assert scope_violations(changed, ("src/tools/**",)) == ("docs/readme.md",)


def test_scope_violations_judges_nothing_without_a_write_scope() -> None:
    assert scope_violations(("src/tools/a.py",), ()) == ()


def test_the_scope_finding_carries_the_tag_a_reviewer_finding_uses() -> None:
    finding = scope_conflict_finding(("src/other.py",), ("src/tools/**",))
    assert finding.startswith("scope: ")
    assert conflict_tag(finding) == "scope"
    assert "src/other.py" in finding


def test_the_scope_finding_bounds_the_names_it_lists() -> None:
    files = tuple(f"src/file-{index}.py" for index in range(20))
    finding = scope_conflict_finding(files, ("src/tools/**",))
    assert finding.count("src/file-") == 8
    assert "+12 more" in finding


def test_the_porcelain_reader_understands_the_status_records() -> None:
    raw = (
        b" M src/stub.py\x00"
        b"?? src/new_module.py\x00"
        b"R  new.py\x00old.py\x00"
    )
    paths = gates_module._porcelain_v1_paths(raw)
    # The new path of a rename comes first, then the path it replaced.
    assert paths == ("src/stub.py", "src/new_module.py", "new.py", "old.py")


def test_a_copy_does_not_count_the_original_path_as_changed() -> None:
    raw = b"C  copy.py\x00source.py\x00"
    assert gates_module._porcelain_v1_paths(raw) == ("copy.py",)
