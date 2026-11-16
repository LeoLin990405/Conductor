"""The G2 attempt and worktree SHA are in the serialized store before argv runs.

InMemoryDispatchStore keeps the live object. These tests copy through JSON on
every save, which is what a database store does. A command that starts before
that copy has the worktree HEAD is a failure of the pin.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch.gates import GateRunner, GateSettings, GitWorktreeManager
from tests.unit.tools.auto_dispatch.test_gate_recovery import (
    WHEN,
    ScriptedCommands,
    _bundle,
    _git,
    _init_repo,
    _line,
)

pytestmark = pytest.mark.asyncio


class SerializedStore:
    """Saves a JSON copy. Later mutation of the live dispatch does not show up."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def create(self, dispatch: Dispatch) -> Dispatch:
        return await self.save(dispatch)

    async def save(self, dispatch: Dispatch) -> Dispatch:
        self.rows[dispatch.id] = json.loads(json.dumps(dispatch.to_dict()))
        return dispatch

    async def get(self, dispatch_id: str) -> Dispatch | None:
        raw = self.rows.get(dispatch_id)
        if raw is None:
            return None
        return Dispatch.from_dict(json.loads(json.dumps(raw)))


class MoveBeforeCheckout(GitWorktreeManager):
    """Moves the line branch after the pre-command save and before checkout."""

    def __init__(self) -> None:
        super().__init__()
        self.before: str | None = None

    async def add_detached(self, repo: Path, path: Path, start_point: str) -> None:
        if self.before is None:
            self.before = _git(repo, "rev-parse", start_point)
            (repo / "second.txt").write_text("second\n", encoding="utf-8")
            _git(repo, "add", "second.txt")
            _git(repo, "commit", "-m", "second")
            _git(repo, "update-ref", f"refs/heads/{start_point}", "HEAD")
        await super().add_detached(repo, path, start_point)


class PinProbe:
    """Reads the serialized row at the moment the G2 command starts."""

    def __init__(self, store: SerializedStore) -> None:
        self.store = store
        self.saved: dict | None = None
        self.actual: str | None = None

    async def run(self, argv, *, cwd, timeout, log_path):
        del argv, timeout
        self.actual = _git(Path(cwd), "rev-parse", "HEAD")
        self.saved = self.store.rows["d1"]["gates"]["lines"]["line-0"]["gates"]["G2"]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        raise asyncio.CancelledError


def _repo(tmp_path: Path) -> Path:
    repo = _init_repo(tmp_path / "repo")
    versions = repo / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "141_dispatch.py").write_text(
        'revision = "141"\ndown_revision = None\n',
        encoding="utf-8",
    )
    (repo / "README").write_text("one\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    _git(repo, "branch", "feature/auto-dispatch-d1/line-line-0")
    return repo


async def test_the_store_has_the_worktree_head_before_the_command_and_a_new_machine_reruns(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    store = SerializedStore()
    probe = PinProbe(store)
    trees = MoveBeforeCheckout()
    settings = GateSettings(
        runs_dir=tmp_path / "runs",
        repo_root=repo,
        g5_command="true",
        g2_timeout_seconds=30,
        g5_timeout_seconds=30,
    )
    gates = GateRunner(settings, commands=probe, worktrees=trees, clock=lambda: WHEN)
    collaborators, machine, _commands, _gates = _bundle(
        tmp_path,
        [_line("line-0")],
        store=store,
        gates=gates,
        settings=settings,
    )
    dispatch = await store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now", spec_ref="spec-9")
    )
    with pytest.raises(asyncio.CancelledError):
        await machine.run(dispatch)

    assert probe.saved is not None and probe.actual is not None
    assert probe.saved["result"] == "running"
    assert probe.saved["attempt"] == 0
    assert probe.saved["revision"] == probe.actual
    assert trees.before is not None and probe.saved["revision"] != trees.before
    restored = await store.get("d1")
    assert restored is not None
    pinned = restored.gates["lines"]["line-0"]["gates"]["G2"]
    assert pinned["result"] == "running"
    assert pinned["attempt"] == 0
    assert pinned["revision"] == probe.actual
    assert restored.spec_ref == "spec-9"

    _git(repo, "branch", "integrate/auto-dispatch-d1")
    replay = ScriptedCommands()
    replay_gates = GateRunner(
        settings, commands=replay, worktrees=GitWorktreeManager(), clock=lambda: WHEN
    )
    _collaborators, resumed, replay, _gates = _bundle(
        tmp_path,
        [],
        commands=replay,
        settings=settings,
        store=store,
        gates=replay_gates,
    )
    result = await resumed.run(restored)
    g2_argv = ["pytest", "-q", "tests/unit/server/app/test_health.py"]
    assert replay.calls.count(g2_argv) == 1
    assert result.gates["lines"]["line-0"]["gates"]["G2"]["result"] == "pass"
    assert result.gates["lines"]["line-0"]["gates"]["G2"]["revision"] == probe.actual
    assert result.gates["lines"]["line-0"]["gates"]["G2"]["attempt"] == 0
    assert result.state is DispatchState.APPROVAL

    reused = Dispatch.from_dict(json.loads(json.dumps(result.to_dict())))
    reused.state = DispatchState.MONITORING
    line = reused.line("line-0")
    assert line is not None
    line.status = LineStatus.RUNNING
    reused.gates["lines"]["line-0"]["machine"]["stage"] = "primary_review"
    quiet = ScriptedCommands()
    quiet_gates = GateRunner(
        settings, commands=quiet, worktrees=GitWorktreeManager(), clock=lambda: WHEN
    )
    _collaborators, again, quiet, _gates = _bundle(
        tmp_path, [], commands=quiet, settings=settings, store=store, gates=quiet_gates
    )
    kept = await again.run(reused)
    assert quiet.calls == []
    assert kept.line("line-0").status is LineStatus.PASSED
    assert kept.gates["lines"]["line-0"]["gates"]["G2"]["revision"] == probe.actual

    _git(repo, "checkout", "feature/auto-dispatch-d1/line-line-0")
    (repo / "third.txt").write_text("third\n", encoding="utf-8")
    _git(repo, "add", "third.txt")
    _git(repo, "commit", "-m", "third")
    moved = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    assert moved != probe.actual
    shifted = Dispatch.from_dict(json.loads(json.dumps(kept.to_dict())))
    shifted.state = DispatchState.MONITORING
    shifted_line = shifted.line("line-0")
    assert shifted_line is not None
    shifted_line.status = LineStatus.RUNNING
    shifted.gates["lines"]["line-0"]["machine"]["stage"] = "primary_review"
    follow = ScriptedCommands()
    follow_gates = GateRunner(
        settings, commands=follow, worktrees=GitWorktreeManager(), clock=lambda: WHEN
    )
    _collaborators, third, follow, _gates = _bundle(
        tmp_path, [], commands=follow, settings=settings, store=store, gates=follow_gates
    )
    moved_result = await third.run(shifted)
    assert follow.calls.count(g2_argv) == 1
    assert moved_result.gates["lines"]["line-0"]["gates"]["G2"]["revision"] == moved
    assert moved_result.gates["lines"]["line-0"]["gates"]["G2"]["attempt"] == 0
    del collaborators
