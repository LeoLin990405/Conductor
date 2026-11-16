"""W3a: DAG order, G2, G5 and the no-secondary reason, with injected fakes."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.tools.auto_dispatch import (
    AlwaysPassReviewer,
    Collaborators,
    Dispatch,
    DispatchMachine,
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    InMemoryOutcomeStore,
    LineStatus,
    MachineConfig,
    NoopMerger,
    RecordingLauncher,
    RoundRobinSelector,
    ScriptedMonitor,
)
from src.tools.auto_dispatch.execution import line_evidence
from src.tools.auto_dispatch.execution import GitMerger, LogMonitor, ScriptLauncher
from src.tools.auto_dispatch.gates import (
    NO_SECONDARY_REVIEWER,
    SCHEMA_VERSION,
    CommandResult,
    GateRunner,
    GateSettings,
)
from src.tools.auto_dispatch.selection import QuotaAwareModelSelector
from src.tools.auto_dispatch.types import Line, Verdict

pytestmark = pytest.mark.asyncio

WHEN = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
MARKER = "SECRET_OUTPUT_MARKER"


class ScriptedCommands:
    def __init__(self, script: list[CommandResult] | None = None, output: bytes = b"") -> None:
        self.script = list(script or [])
        self.output = output
        self.calls: list[list[str]] = []

    async def run(self, argv, *, cwd, timeout, log_path):
        self.calls.append(list(argv))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_bytes(self.output)
        if self.script:
            return self.script.pop(0)
        return CommandResult(exit_code=0, duration_ms=5)


class FakeWorktrees:
    def __init__(self, seed=None) -> None:
        self.added: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self._seed = seed

    async def add_detached(self, repo, path, start_point) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self.added.append((path.name, start_point))
        if self._seed is not None and path.name == "integration":
            self._seed(path)

    async def remove(self, repo, path) -> None:
        self.removed.append(path.name)
        if path.exists():
            shutil.rmtree(path)


def _one_head(worktree: Path) -> None:
    versions = worktree / "migrations" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    (versions / "141.py").write_text(
        'revision = "141"\ndown_revision = "140"\n',
        encoding="utf-8",
    )
    (versions / "140.py").write_text(
        'revision = "140"\ndown_revision = None\n',
        encoding="utf-8",
    )


def _two_heads(worktree: Path) -> None:
    versions = worktree / "migrations" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    (versions / "a.py").write_text('revision = "a"\ndown_revision = None\n', encoding="utf-8")
    (versions / "b.py").write_text('revision = "b"\ndown_revision = None\n', encoding="utf-8")


class Planner:
    def __init__(self, lines: list[Line]) -> None:
        self._lines = lines
        self.calls: list[str] = []

    async def plan(self, goal: str) -> list[Line]:
        self.calls.append(goal)
        if self.calls and "Replan this line." in goal:
            return []
        return self._lines


def _git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    done = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return done.stdout.strip()


def _repo_with_lines(repo: Path, *line_ids: str) -> Path:
    """A repo whose line branches each add one file, so a fix handoff is complete."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "dispatch@example.com")
    _git(repo, "config", "user.name", "dispatch")
    (repo / "README").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    for line_id in line_ids:
        branch = f"feature/auto-dispatch-d1/line-{line_id}"
        _git(repo, "checkout", "-b", branch)
        (repo / f"{line_id}.txt").write_text("work\n", encoding="utf-8")
        _git(repo, "add", f"{line_id}.txt")
        _git(repo, "commit", "-m", line_id)
        _git(repo, "checkout", "main")
    return repo


def _line(line_id: str, *, depends_on: tuple[str, ...] = (), **extra) -> Line:
    acceptance = extra.pop("acceptance", ("cmd: pytest -q tests/unit/server/app/test_health.py", "returns 200"))
    return Line(
        id=line_id,
        name=extra.pop("name", line_id),
        task=extra.pop("task", f"do {line_id}"),
        depends_on=depends_on,
        acceptance=acceptance,
        **extra,
    )


async def _asleep(_seconds: float) -> None:
    return None


def _bundle(tmp_path: Path, lines: list[Line], **overrides):
    commands = overrides.pop("commands", None) or ScriptedCommands(output=MARKER.encode())
    trees = overrides.pop("trees", None) or FakeWorktrees(seed=_one_head)
    settings = overrides.pop(
        "settings",
        GateSettings(
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            g5_command="pytest -q",
        ),
    )
    gates = GateRunner(settings, commands=commands, worktrees=trees, clock=lambda: WHEN)
    collaborators = Collaborators(
        planner=overrides.pop("planner", None) or Planner(lines),
        selector=overrides.pop("selector", None) or RoundRobinSelector(),
        launcher=overrides.pop("launcher", None) or RecordingLauncher(),
        monitor=overrides.pop("monitor", None) or ScriptedMonitor(),
        reviewer=overrides.pop("reviewer", None) or AlwaysPassReviewer(),
        merger=overrides.pop("merger", None) or NoopMerger(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        outcomes=InMemoryOutcomeStore(),
        store=InMemoryDispatchStore(),
        gates=gates,
    )
    machine = DispatchMachine(collaborators, sleep=_asleep)
    return collaborators, machine, commands, trees


async def _run(tmp_path: Path, lines: list[Line], **overrides):
    collaborators, machine, commands, trees = _bundle(tmp_path, lines, **overrides)
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    result = await machine.run(dispatch)
    return collaborators, machine, commands, trees, result


async def test_happy_path_merges_before_approval_and_does_not_merge_again(tmp_path: Path) -> None:
    collaborators, _machine, commands, trees, parked = await _run(tmp_path, [_line("line-0")])
    assert parked.state is DispatchState.APPROVAL
    assert parked.gates["schema_version"] == SCHEMA_VERSION
    assert parked.line("line-0").status is LineStatus.PASSED
    assert collaborators.merger.merged == ["line-0"]
    assert parked.message == (
        "Integrate branch integrate/auto-dispatch-d1 passed G5; awaiting human approval"
    )
    snap = parked.gates["lines"]["line-0"]
    assert snap["primary_reviewer"]
    assert snap["secondary_reviewer"] is None
    assert snap["gates"]["primary_review"]["result"] == "pass"
    assert snap["gates"]["primary_review"]["reason"] == "primary_merge_no_risk"
    assert snap["gates"]["secondary_review"] is None
    assert snap["history"][0]["gate"] == "primary_review"
    assert snap["history"][0]["blocker_count"] == 0
    assert "blockers" not in snap["history"][0]
    assert snap["machine"]["handoff"] is None
    assert snap["machine"]["secondary_skipped"] is True
    assert snap["gates"]["G2"]["result"] == "pass"
    assert snap["gates"]["G2"]["reason"] == "focused_tests_passed"
    assert snap["gates"]["G5"]["reason"] == "g5_passed"
    assert snap["phase"] == "awaiting_human"
    assert parked.gates["integration"]["phase"] == "passed"
    assert parked.gates["integration"]["checks"]["alembic_single_head"]["heads"] == ["141"]
    assert commands.calls[0] == ["pytest", "-q", "tests/unit/server/app/test_health.py"]
    assert commands.calls[1] == ["pytest", "-q"]
    assert trees.removed  # worktrees are removed after the commands
    fixture = json.loads(
        Path("tests/fixtures/dispatch_gates_v1.json").read_text(encoding="utf-8")
    )
    assert set(fixture["gates"]) <= set(parked.gates)
    assert "scheduler" in parked.gates
    assert set(snap) == set(fixture["gates"]["lines"]["line-0"])
    assert set(snap["machine"]) == set(fixture["gates"]["lines"]["line-0"]["machine"])
    assert MARKER not in json.dumps(parked.gates)
    summary = (tmp_path / "proposed" / "d1" / "summary.md").read_text(encoding="utf-8")
    assert "already merged into the integrate branch and G5 passed" in summary

    await collaborators.gate.resolve_dispatch(parked.id, True)
    completed = await _machine.run(parked)
    assert completed.state is DispatchState.COMPLETE
    assert collaborators.merger.merged == ["line-0"]
    assert completed.gates["lines"]["line-0"]["gates"]["G6"]["reason"] == "human_approved"
    assert set(completed.outcomes) == {"line-0"}


async def test_reject_aborts_without_a_second_merge(tmp_path: Path) -> None:
    collaborators, machine, _commands, _trees, parked = await _run(tmp_path, [_line("line-0")])
    await collaborators.gate.resolve_dispatch(parked.id, False, "no")
    aborted = await machine.run(parked)
    assert aborted.state is DispatchState.ABORT
    assert collaborators.merger.merged == ["line-0"]
    assert aborted.gates["approval"]["result"] == "fail"
    assert aborted.gates["lines"]["line-0"]["gates"]["G6"]["reason"] == "human_rejected"


async def test_lines_launch_and_merge_in_dependency_order(tmp_path: Path) -> None:
    lines = [
        _line("line-c", depends_on=("line-a",)),
        _line("line-b", depends_on=("line-a",)),
        _line("line-a"),
    ]
    collaborators, _machine, _commands, _trees, parked = await _run(tmp_path, lines)
    assert parked.state is DispatchState.APPROVAL
    assert [line_id for line_id, _model, _skills in collaborators.launcher.launches] == [
        "line-a",
        "line-c",
        "line-b",
    ]
    assert collaborators.merger.merged == ["line-a", "line-c", "line-b"]


async def test_a_cycle_fails_before_any_launch(tmp_path: Path) -> None:
    lines = [
        _line("line-a", depends_on=("line-b",)),
        _line("line-b", depends_on=("line-a",)),
    ]
    collaborators, _machine, _commands, _trees, failed = await _run(tmp_path, lines)
    assert failed.state is DispatchState.ERROR
    assert failed.message == "The plan's line dependencies are not a DAG"
    assert collaborators.launcher.launches == []


async def test_a_missing_dependency_is_the_same_error(tmp_path: Path) -> None:
    _collaborators, _machine, _commands, _trees, failed = await _run(
        tmp_path, [_line("line-a", depends_on=("missing",))]
    )
    assert failed.state is DispatchState.ERROR
    assert failed.message == "The plan's line dependencies are not a DAG"


async def test_a_failing_focused_test_blocks_without_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scripted commands pin today's sequence: no N27 base re-run.
    monkeypatch.setenv("CONDUCTOR_DISPATCH_G2_PREEXISTING_MODE", "off")
    commands = ScriptedCommands(
        [
            CommandResult(exit_code=1, duration_ms=12),
            CommandResult(exit_code=1, duration_ms=12),
            CommandResult(exit_code=1, duration_ms=12),
        ],
        output=MARKER.encode(),
    )
    repo = _repo_with_lines(tmp_path / "repo", "line-0")
    settings = GateSettings(
        runs_dir=tmp_path / "runs", repo_root=repo, g5_command="pytest -q"
    )
    collaborators, _machine, commands, _trees, parked = await _run(
        tmp_path, [_line("line-0")], commands=commands, settings=settings
    )
    assert parked.state is DispatchState.REVIEW
    assert parked.line("line-0").status is LineStatus.BLOCKED
    assert parked.line("line-0").failure_reason == "focused_tests_exhausted"
    assert parked.line("line-0").attempts == 0
    assert parked.approval_token is None
    assert collaborators.merger.merged == []
    assert MARKER not in (parked.message or "")
    assert MARKER not in json.dumps(parked.gates)
    log = tmp_path / "runs" / "gates" / "d1" / "line-0" / "g2-0-0.log"
    assert log.read_bytes() == MARKER.encode()
    gate_runner = GateRunner(settings)
    assert not gate_runner._gate_worktree_path(parked, "line", "line-0").exists()
    assert parked.gates["lines"]["line-0"]["gates"]["G2"]["commands"][0]["exit_code"] == 1
    assert [item[0] for item in collaborators.launcher.launches] == ["line-0", "line-0", "line-0"]


class _BlockOne:
    def __init__(self, line_id: str) -> None:
        self._line_id = line_id

    async def review_line(self, line_id: str, model: str) -> Verdict:
        if line_id == self._line_id:
            return Verdict.blocked(["no tests"])
        return Verdict.ok()


async def test_a_review_block_does_not_strand_an_independent_line(tmp_path: Path) -> None:
    lines = [
        _line("line-a"),
        _line("line-c"),
        _line("line-b", depends_on=("line-a",)),
    ]
    repo = _repo_with_lines(tmp_path / "repo", "line-a", "line-c", "line-b")
    settings = GateSettings(
        runs_dir=tmp_path / "runs", repo_root=repo, g5_command="pytest -q"
    )
    collaborators, _machine, commands, _trees, parked = await _run(
        tmp_path,
        lines,
        reviewer=_BlockOne("line-c"),
        settings=settings,
    )
    assert parked.state is DispatchState.REVIEW
    assert parked.line("line-a").status is LineStatus.PASSED
    assert parked.line("line-b").status is LineStatus.PASSED
    assert parked.line("line-c").status is LineStatus.BLOCKED
    assert parked.line("line-c").failure_reason == "replan_exhausted"
    assert parked.message == (
        "Line line-c was blocked twice after fixes and could not be replanned; "
        "reassign it or reject the dispatch"
    )
    assert collaborators.merger.merged == []
    assert "line-a" in [item[0] for item in collaborators.launcher.launches]
    assert "line-b" in [item[0] for item in collaborators.launcher.launches]
    assert [item[0] for item in collaborators.launcher.launches].count("line-c") == 3
    assert len(commands.calls) == 5


async def test_reassigning_a_failed_focused_test_runs_it_again(tmp_path: Path) -> None:
    commands = ScriptedCommands(
        [
            CommandResult(exit_code=1, duration_ms=4),
            CommandResult(exit_code=0, duration_ms=4),
            CommandResult(exit_code=0, duration_ms=4),
        ]
    )
    repo = _repo_with_lines(tmp_path / "repo", "line-0")
    settings = GateSettings(
        runs_dir=tmp_path / "runs", repo_root=repo, g5_command="pytest -q"
    )
    collaborators, _machine, commands, _trees, parked = await _run(
        tmp_path, [_line("line-0")], commands=commands, settings=settings
    )
    assert parked.state is DispatchState.APPROVAL
    assert parked.line("line-0").status is LineStatus.PASSED
    assert parked.line("line-0").failure_reason is None
    assert parked.line("line-0").attempts == 0
    assert parked.gates["lines"]["line-0"]["gates"]["G2"]["result"] == "pass"
    assert parked.gates["lines"]["line-0"]["fix_round"] == 1
    models = [model for _line_id, model, _skills in collaborators.launcher.launches]
    assert models[0] == models[1]
    assert collaborators.merger.merged == ["line-0"]


async def test_approve_without_a_runner_does_not_merge_again(tmp_path: Path) -> None:
    collaborators, machine, _commands, _trees, parked = await _run(tmp_path, [_line("line-0")])
    assert parked.gates["integration"]["phase"] == "passed"
    collaborators.gates = None
    await collaborators.gate.resolve_dispatch(parked.id, True)
    completed = await machine.run(parked)
    assert completed.state is DispatchState.COMPLETE
    assert collaborators.merger.merged == ["line-0"]


async def test_a_repository_without_alembic_passes_the_alembic_check(tmp_path: Path) -> None:
    # A target checkout with no migrations/versions tree (any repository other
    # than Conductor's own) must reach APPROVAL, not fail G5 on alembic_no_head.
    trees = FakeWorktrees(seed=lambda _worktree: None)
    _collaborators, _machine, _commands, _trees, parked = await _run(
        tmp_path, [_line("line-0")], trees=trees
    )
    assert parked.state is DispatchState.APPROVAL
    check = parked.gates["integration"]["checks"]["alembic_single_head"]
    assert check == {"result": "pass", "heads": [], "reason": "alembic_not_applicable"}


def _empty_versions(worktree: Path) -> None:
    (worktree / "migrations" / "versions").mkdir(parents=True, exist_ok=True)


async def test_an_empty_versions_directory_still_fails_with_no_head(tmp_path: Path) -> None:
    trees = FakeWorktrees(seed=_empty_versions)
    _collaborators, _machine, _commands, _trees, failed = await _run(
        tmp_path, [_line("line-0")], trees=trees
    )
    assert failed.state is DispatchState.ERROR
    assert failed.gates["integration"]["reason"] == "alembic_no_head"


def _ini_without_versions(worktree: Path) -> None:
    # Conductor's own layout with migrations/versions deleted by a worker.
    (worktree / "alembic.ini").write_text(
        "[alembic]\nscript_location = migrations\n", encoding="utf-8"
    )


async def test_alembic_ini_without_versions_fails_with_no_head(tmp_path: Path) -> None:
    trees = FakeWorktrees(seed=_ini_without_versions)
    _collaborators, _machine, _commands, _trees, failed = await _run(
        tmp_path, [_line("line-0")], trees=trees
    )
    assert failed.state is DispatchState.ERROR
    assert failed.gates["integration"]["reason"] == "alembic_no_head"


def _custom_script_location(worktree: Path) -> None:
    (worktree / "alembic.ini").write_text(
        "[alembic]\nscript_location = %(here)s/db/alembic\n", encoding="utf-8"
    )
    versions = worktree / "db" / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "001.py").write_text(
        'revision = "001"\ndown_revision = None\n', encoding="utf-8"
    )


async def test_alembic_ini_script_location_is_respected(tmp_path: Path) -> None:
    trees = FakeWorktrees(seed=_custom_script_location)
    _collaborators, _machine, _commands, _trees, parked = await _run(
        tmp_path, [_line("line-0")], trees=trees
    )
    assert parked.state is DispatchState.APPROVAL
    check = parked.gates["integration"]["checks"]["alembic_single_head"]
    assert check == {"result": "pass", "heads": ["001"], "reason": "alembic_single_head"}


def _binary_migration(worktree: Path) -> None:
    versions = worktree / "migrations" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    (versions / "141.py").write_text(
        'revision = "141"\ndown_revision = None\n',
        encoding="utf-8",
    )
    (versions / "bad.py").write_bytes(b"\xff\xfe not utf-8")


async def test_an_undecodable_migration_fails_the_alembic_check(tmp_path: Path) -> None:
    trees = FakeWorktrees(seed=_binary_migration)
    _collaborators, _machine, _commands, _trees, failed = await _run(
        tmp_path, [_line("line-0")], trees=trees
    )
    assert failed.state is DispatchState.ERROR
    assert failed.message == (
        "G5 integration checks failed on integrate/auto-dispatch-d1: alembic_single_head"
    )
    assert failed.gates["integration"]["reason"] == "alembic_unparseable"
    assert failed.gates["integration"]["checks"]["alembic_single_head"]["heads"] == []


def _syntax_migration(worktree: Path) -> None:
    versions = worktree / "migrations" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    (versions / "one.py").write_text(
        'revision = "one"\ndown_revision = None\n',
        encoding="utf-8",
    )
    (versions / "bad.py").write_text('revision = "hidden-head"\n(', encoding="utf-8")


def _unreadable_migration(worktree: Path) -> None:
    versions = worktree / "migrations" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    (versions / "one.py").write_text(
        'revision = "one"\ndown_revision = None\n',
        encoding="utf-8",
    )
    (versions / "bad.py").mkdir()


async def test_a_syntax_error_fails_the_alembic_check(tmp_path: Path) -> None:
    trees = FakeWorktrees(seed=_syntax_migration)
    _collaborators, _machine, _commands, _trees, failed = await _run(
        tmp_path, [_line("line-0")], trees=trees
    )
    assert failed.state is DispatchState.ERROR
    assert failed.message.endswith(": alembic_single_head")
    check = failed.gates["integration"]["checks"]["alembic_single_head"]
    assert check["reason"] == "alembic_unparseable"
    assert check["heads"] == []
    assert failed.gates["integration"]["reason"] == "alembic_unparseable"


async def test_an_unreadable_migration_fails_the_alembic_check(tmp_path: Path) -> None:
    trees = FakeWorktrees(seed=_unreadable_migration)
    _collaborators, _machine, _commands, _trees, failed = await _run(
        tmp_path, [_line("line-0")], trees=trees
    )
    assert failed.state is DispatchState.ERROR
    assert failed.message.endswith(": alembic_single_head")
    assert failed.gates["integration"]["reason"] == "alembic_unparseable"


async def test_a_focused_test_timeout_uses_its_own_reason(tmp_path: Path) -> None:
    commands = ScriptedCommands(
        [
            CommandResult(exit_code=124, duration_ms=15, timed_out=True),
            CommandResult(exit_code=124, duration_ms=15, timed_out=True),
            CommandResult(exit_code=124, duration_ms=15, timed_out=True),
        ]
    )
    repo = _repo_with_lines(tmp_path / "repo", "line-0")
    settings = GateSettings(
        runs_dir=tmp_path / "runs", repo_root=repo, g5_command="pytest -q"
    )
    _collaborators, _machine, _commands, _trees, parked = await _run(
        tmp_path, [_line("line-0")], commands=commands, settings=settings
    )
    assert parked.line("line-0").failure_reason == "focused_tests_exhausted"
    assert parked.gates["lines"]["line-0"]["gates"]["G2"]["reason"] == "g2_timeout"


async def test_no_commands_skips_a_normal_line_and_fails_a_risk_line(tmp_path: Path) -> None:
    prose = _line("line-0", acceptance=("returns 200",))
    _collaborators, _machine, commands, _trees, parked = await _run(tmp_path, [prose])
    assert parked.state is DispatchState.APPROVAL
    assert parked.gates["lines"]["line-0"]["gates"]["G2"]["result"] == "skipped"
    assert parked.gates["lines"]["line-0"]["gates"]["G2"]["reason"] == "g2_no_commands"
    assert commands.calls == [["pytest", "-q"]]  # G5 only

    risky = _line("line-0", acceptance=("returns 200",), risk_bearing=True)
    commands = ScriptedCommands()
    _collaborators, _machine, _commands, _trees, blocked = await _run(
        tmp_path, [risky], commands=commands, settings=GateSettings(
            runs_dir=tmp_path / "runs2",
            repo_root=tmp_path / "repo",
            g5_command="pytest -q",
        )
    )
    assert blocked.state is DispatchState.REVIEW
    assert blocked.line("line-0").failure_reason == "g2_no_commands_risk_bearing"


async def test_fake_worker_commit_handoff_flows_through_g2_review_and_g5(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Gate Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "gate-test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", "/dev/null"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", "false"], check=True)
    versions = repo / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "141.py").write_text(
        'revision = "141"\ndown_revision = None\n', encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    base_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    fake_worker = tmp_path / "fake-worker"
    fake_worker.write_text(
        "#!/bin/sh\n"
        'case "$2" in *"Default git branch: feature/auto-dispatch-d1/line-line-0"*) ;; *) exit 40 ;; esac\n'
        'case "$2" in *"commit the Ticket changes before finishing"*) ;; *) exit 41 ;; esac\n'
        'test "$(git branch --show-current)" = "feature/auto-dispatch-d1/line-line-0" || exit 42\n'
        'printf "committed\\n" > worker.txt\n'
        'git add worker.txt && git commit -qm "fake worker change"\n',
        encoding="utf-8",
    )
    fake_worker.chmod(0o755)
    g2_command = "/bin/sh -c 'test \"$(cat worker.txt)\" = committed'"
    line = Line(
        id="line-0",
        name="worker line",
        task="create worker result",
        acceptance=(f"cmd: {g2_command}",),
    )
    dispatch_id = "d1"
    runs = tmp_path / "runs"
    worktrees = tmp_path / "dispatch-worktrees"
    launcher = ScriptLauncher(
        runs,
        script=fake_worker,
        cwd=repo,
        dispatch_id=dispatch_id,
        worktrees_dir=worktrees,
    )
    gate_runner = GateRunner(
        GateSettings(
            runs_dir=runs,
            repo_root=repo,
            g5_command=f"{sys.executable} -c pass",
            g5_timeout_seconds=30,
            worktrees_dir=worktrees,
        )
    )
    approval = FileApprovalGate(tmp_path / "proposed")
    dispatch = Dispatch(id=dispatch_id, goal="g", user_id="u", created_at="now")
    collaborators = Collaborators(
        planner=type("Planner", (), {"plan": lambda self, goal: _await_lines(line)})(),
        selector=RoundRobinSelector(pool=("fake", "reviewer")),
        launcher=launcher,
        monitor=LogMonitor(runs, stall_seconds=30, timeout_seconds=60, canceller=launcher.cancel),
        reviewer=AlwaysPassReviewer(),
        merger=GitMerger(
            repo, dispatch, gate=approval, worktrees_dir=worktrees
        ),
        gate=approval,
        outcomes=InMemoryOutcomeStore(),
        store=InMemoryDispatchStore(),
        gates=gate_runner,
    )
    machine = DispatchMachine(collaborators, config=MachineConfig(poll_interval_seconds=0.001))
    dispatch = await collaborators.store.create(dispatch)

    parked = await machine.run(dispatch)

    assert parked.state is DispatchState.APPROVAL
    line_result = parked.line("line-0")
    assert line_result is not None and line_result.status is LineStatus.PASSED
    snapshot = parked.gates["lines"]["line-0"]
    g2 = snapshot["gates"]["G2"]
    handoff = snapshot["gates"]["handoff"]
    assert handoff["branch"] == line_result.branch
    assert handoff["commit"] == g2["revision"]
    assert "worker.txt" in handoff["files"]
    safe_display = shlex.join(shlex.split(g2_command))
    safe_digest = hashlib.sha256(safe_display.encode("utf-8")).hexdigest()
    assert handoff["test_command"] == (
        f"{safe_display} [safe-display-sha256:{safe_digest}]"
    )
    assert handoff["results"] == "0:0"
    assert snapshot["handoff_ref"]
    assert parked.gates["integration"]["phase"] == "passed"
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip() == base_head
    assert subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True, check=True
    ).stdout.strip() == "main"
    assert not (repo / "worker.txt").exists()


async def _await_lines(line: Line) -> list[Line]:
    return [line]


async def test_an_unset_g5_command_errors_with_the_full_suite_check(tmp_path: Path) -> None:
    settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=tmp_path / "repo",
        g5_command=None,
    )
    _collaborators, _machine, _commands, _trees, failed = await _run(
        tmp_path, [_line("line-0")], settings=settings
    )
    assert failed.state is DispatchState.ERROR
    assert failed.message == (
        "G5 integration checks failed on integrate/auto-dispatch-d1: full_suite"
    )
    assert failed.gates["lines"]["line-0"]["gates"]["G5"]["reason"] == "g5_not_configured"


async def test_two_alembic_heads_name_the_alembic_check(tmp_path: Path) -> None:
    trees = FakeWorktrees(seed=_two_heads)
    _collaborators, _machine, _commands, _trees, failed = await _run(
        tmp_path, [_line("line-0")], trees=trees
    )
    assert failed.state is DispatchState.ERROR
    assert failed.message == (
        "G5 integration checks failed on integrate/auto-dispatch-d1: alembic_single_head"
    )
    assert failed.gates["integration"]["reason"] == "alembic_multiple_heads"


async def test_a_failing_full_suite_uses_the_full_suite_check(tmp_path: Path) -> None:
    commands = ScriptedCommands(
        [CommandResult(0, 5), CommandResult(exit_code=2, duration_ms=9)]
    )
    _collaborators, _machine, _commands, _trees, failed = await _run(
        tmp_path, [_line("line-0")], commands=commands
    )
    assert failed.state is DispatchState.ERROR
    assert failed.message.endswith(": full_suite")
    assert failed.gates["integration"]["reason"] == "g5_full_suite_failed"
    assert MARKER not in (failed.message or "")


async def test_a_merge_conflict_aborts_before_g5(tmp_path: Path) -> None:
    commands = ScriptedCommands()
    collaborators, _machine, commands, _trees, parked = await _run(
        tmp_path,
        [_line("line-0")],
        commands=commands,
        merger=NoopMerger(conflict_on="line-0"),
    )
    assert parked.state is DispatchState.APPROVAL or parked.state is DispatchState.ABORT
    # The line passes review, then the pre-G5 merge aborts.
    assert parked.state is DispatchState.ABORT
    assert collaborators.merger.merged == []
    assert commands.calls == [["pytest", "-q", "tests/unit/server/app/test_health.py"]]
    assert parked.gates["integration"]["reason"] == "merge_conflict"


async def test_a_restart_reruns_g5_and_does_not_merge_again(tmp_path: Path) -> None:
    collaborators, machine, commands, _trees, parked = await _run(tmp_path, [_line("line-0")])
    assert parked.state is DispatchState.APPROVAL
    g5_calls = len(commands.calls)
    parked.state = DispatchState.REVIEW
    parked.approval_token = None
    parked.gates["integration"]["phase"] = "running"
    rerun = await machine.run(parked)
    assert rerun.state is DispatchState.APPROVAL
    assert len(commands.calls) == g5_calls + 1
    assert collaborators.merger.merged == ["line-0"]


def _without_run_suffix(value: object) -> object:
    """The 12 hex chars of a run id are random. The rest of the snapshot is not."""
    if isinstance(value, str):
        return re.sub(r":[0-9a-f]{12}(?!\w)", ":<id>", value)
    if isinstance(value, dict):
        return {key: _without_run_suffix(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_run_suffix(item) for item in value]
    return value


async def test_the_same_script_produces_the_same_snapshot(tmp_path: Path) -> None:
    async def once(root: Path) -> dict:
        _collaborators, _machine, _commands, _trees, parked = await _run(root, [_line("line-0")])
        return parked.gates

    first = await once(tmp_path / "a")
    second = await once(tmp_path / "b")
    assert _without_run_suffix(first) == _without_run_suffix(second)


async def test_dev_mode_still_merges_only_after_approval(tmp_path: Path) -> None:
    from tests.unit.tools.auto_dispatch.test_machine import _collaborators, _dispatch, _machine

    collaborators = _collaborators(tmp_path)
    machine, _recorder = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())
    parked = await machine.run(dispatch)
    assert parked.state is DispatchState.APPROVAL
    assert parked.gates == {}
    assert collaborators.merger.merged == []
    await collaborators.gate.resolve_dispatch(parked.id, True)
    completed = await machine.run(parked)
    assert completed.state is DispatchState.COMPLETE
    assert collaborators.merger.merged == ["line-0", "line-1", "line-2"]


async def test_no_secondary_reviewer_is_on_the_dispatch_result(tmp_path: Path) -> None:
    def refuse(model: str) -> bool:
        return model in {"codex", "stepfun"}

    selector = QuotaAwareModelSelector(review_refused=refuse)
    collaborators, _machine, _commands, _trees, failed = await _run(
        tmp_path, [_line("line-0")], selector=selector
    )
    assert failed.state is DispatchState.ERROR
    assert failed.message is not None
    assert NO_SECONDARY_REVIEWER in failed.message
    assert failed.line("line-0").failure_reason == NO_SECONDARY_REVIEWER
    assert selector.review_refusal == NO_SECONDARY_REVIEWER
    assert collaborators.merger.merged == []


async def test_gated_evidence_is_what_the_reviewer_sees(tmp_path: Path) -> None:
    _collaborators, _machine, _commands, _trees, parked = await _run(tmp_path, [_line("line-0")])
    text = line_evidence(parked, {})("line-0")
    assert "returns 200" in text
    assert "command 0 exit 0" in text
    assert MARKER not in text
