"""Review follow-ups on the R2d G2 checks and the N64 busy markers.

- G2 shadow and enforce records stay small however many files a run touches.
- An enforced scope check fails closed when it cannot run, whatever the
  worktree check's mode.
- ``validate_plan(require_tests_mode=)`` is normalised like the env value.
- G2/G5, reviewer and merger work holds busy markers; the backend reports the
  drain flag and the markers it sees.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import os
from pathlib import Path

import pytest

import src.tools.auto_dispatch.gates as gates_module
from src.conductor_cli import activity
from src.conductor_cli.dispatch import render_status
from src.conductor_cli.drain import set_drain
from src.server.services.dispatch import dispatch_summary, ops_state
from src.tools.auto_dispatch.busy import BusyProxy
from src.tools.auto_dispatch.check_modes import (
    ENFORCE,
    OFF,
    REQUIRE_TESTS_ENV,
    SCOPE_CHECK,
    SCOPE_CHECK_ENV,
    SHADOW,
    WORKTREE_CHECK,
    WORKTREE_CHECK_ENV,
    normalize_mode,
)
from src.tools.auto_dispatch.gates import G2_SCOPE_CONFLICT, G2_WORKTREE_DIRTY
from src.tools.auto_dispatch.planning import PlanRejection, PlanResult, validate_plan

from .test_gates_structural import _dispatch, _gate, _line, _Repo, _ScriptedCommands
from .test_planning_requires_tests import POLICY, _agents, _payload
from .test_planning_requires_tests import _line as _plan_line


@pytest.fixture(autouse=True)
def _n73_enforce_still_reachable(monkeypatch):
    """N73 made env ``enforce`` record-only; these tests pin the enforce path."""
    monkeypatch.setenv("CONDUCTOR_DISPATCH_RECORD_ONLY_CHECKS", "0")

CELL_BUDGET = 40_000


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in (REQUIRE_TESTS_ENV, WORKTREE_CHECK_ENV, SCOPE_CHECK_ENV):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "conductor-home"
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    return home


async def _g2(gate, line):
    dispatch = _dispatch(line)
    step = await gate.run_g2(dispatch, line)
    return step, gate._line(dispatch, line.id)["gates"]["G2"]


# --------------------------------------------------------------------------
# Record sizes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [SHADOW, ENFORCE])
async def test_a_run_that_writes_thousands_of_files_keeps_the_cell_small(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv(WORKTREE_CHECK_ENV, mode)
    repo = _Repo(tmp_path, ("in_scope.txt",))
    writes = [(f"build/gen/f{i:05d}_long_generated_name.py", "x") for i in range(2000)]
    gate = _gate(tmp_path, repo, _ScriptedCommands(writes))
    step, g2 = await _g2(gate, _line(repo, ("in_scope.txt",)))

    assert len(json.dumps(g2)) < CELL_BUDGET
    if mode == ENFORCE:
        assert step.reason == G2_WORKTREE_DIRTY
        record = g2["dirty_worktree"]
    else:
        assert step.passed
        (entry,) = [e for e in g2["shadow"] if e["check"] == WORKTREE_CHECK]
        record = entry["record"]
    assert len(record["paths"]) == 50
    assert record["count"] == 2000
    assert "more" in record["finding"]


@pytest.mark.asyncio
async def test_a_scope_conflict_over_many_files_keeps_the_cell_small(
    tmp_path: Path,
) -> None:
    repo = _Repo(tmp_path, tuple(f"src/out/m{i:04d}.py" for i in range(600)))
    gate = _gate(tmp_path, repo, _ScriptedCommands())
    step, g2 = await _g2(gate, _line(repo, ("docs/only.md",)))

    assert step.passed  # shadow by default
    (entry,) = [e for e in g2["shadow"] if e["check"] == SCOPE_CHECK]
    assert len(entry["record"]["files"]) == 50
    assert entry["record"]["count"] == 600
    assert len(json.dumps(g2)) < CELL_BUDGET


def test_a_shadow_record_clips_its_finding() -> None:
    entry = gates_module.shadow_record("c", "r", "x" * 10_000, {"paths": list(range(500))})
    assert len(entry["finding"]) <= 2000
    assert len(entry["record"]["paths"]) == 50
    assert entry["record"]["count"] == 500


# --------------------------------------------------------------------------
# Enforced scope fails closed
# --------------------------------------------------------------------------


def _broken(*args, **kwargs):
    raise RuntimeError("boom")


@pytest.mark.asyncio
@pytest.mark.parametrize("worktree_mode", [OFF, SHADOW, ENFORCE])
async def test_an_enforced_scope_check_that_raises_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worktree_mode: str
) -> None:
    monkeypatch.setenv(SCOPE_CHECK_ENV, ENFORCE)
    monkeypatch.setenv(WORKTREE_CHECK_ENV, worktree_mode)
    repo = _Repo(tmp_path, ("in_scope.txt",))
    gate = _gate(tmp_path, repo, _ScriptedCommands())
    monkeypatch.setattr(gate, "_structural_findings", _broken)
    step, g2 = await _g2(gate, _line(repo, ("in_scope.txt",)))

    assert not step.passed
    assert step.reason in (G2_SCOPE_CONFLICT, G2_WORKTREE_DIRTY)
    assert g2["scope_conflict"]["unknown"] is True
    if worktree_mode != ENFORCE:
        assert step.reason == G2_SCOPE_CONFLICT


@pytest.mark.asyncio
async def test_an_enforced_scope_check_whose_diff_cannot_be_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SCOPE_CHECK_ENV, ENFORCE)
    monkeypatch.setenv(WORKTREE_CHECK_ENV, OFF)
    monkeypatch.setattr(gates_module, "git_changed_paths", lambda *a, **k: None)
    repo = _Repo(tmp_path, ("in_scope.txt",))
    gate = _gate(tmp_path, repo, _ScriptedCommands())
    step, g2 = await _g2(gate, _line(repo, ("in_scope.txt",)))

    assert step.reason == G2_SCOPE_CONFLICT
    assert g2["scope_conflict"]["unknown"] is True
    assert g2["scope_conflict"]["finding"].startswith("scope: ")


@pytest.mark.asyncio
async def test_a_shadow_scope_check_that_cannot_read_the_diff_only_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gates_module, "git_changed_paths", lambda *a, **k: None)
    repo = _Repo(tmp_path, ("in_scope.txt",))
    gate = _gate(tmp_path, repo, _ScriptedCommands())
    step, g2 = await _g2(gate, _line(repo, ("in_scope.txt",)))

    assert step.passed
    (entry,) = [e for e in g2["shadow"] if e["check"] == SCOPE_CHECK]
    assert entry["record"]["unknown"] is True


# --------------------------------------------------------------------------
# validate_plan(require_tests_mode=)
# --------------------------------------------------------------------------


def _untested() -> dict:
    return _payload(_plan_line("one", acceptance=["cmd: ruff check src/one.py"]))


@pytest.mark.parametrize("raw,refused", [(" ENFORCE ", True), ("Enforce", True), ("bogus", False), ("", False), ("OFF", False)])
def test_the_require_tests_argument_is_normalised(raw: str, refused: bool) -> None:
    outcome = validate_plan(
        _untested(), policy=POLICY, agents=_agents("kimi"), require_tests_mode=raw
    )
    assert isinstance(outcome, PlanRejection if refused else PlanResult)


def test_normalize_mode() -> None:
    assert normalize_mode(" Shadow ") == SHADOW
    assert normalize_mode(None) == SHADOW
    assert normalize_mode("nope") == SHADOW
    assert normalize_mode("OFF") == OFF


# --------------------------------------------------------------------------
# Busy markers
# --------------------------------------------------------------------------


def test_a_busy_marker_lives_for_the_block(_isolated: Path) -> None:
    with activity.busy(activity.REVIEW, "line-0"):
        assert activity.busy_counts(_isolated) == {"review": 1}
    assert activity.busy_counts(_isolated) == {}


def test_a_marker_of_a_dead_process_is_stale_and_pruned(_isolated: Path) -> None:
    directory = activity.busy_dir(_isolated)
    directory.mkdir(parents=True)
    stale = directory / "1-dead.json"
    # A pid far above any live one on this machine.
    stale.write_text(json.dumps({"kind": "merge", "pid": 2**22 + 12345}))
    assert activity.busy_counts(_isolated) == {}
    assert not stale.exists()


def test_a_damaged_marker_counts_as_busy(_isolated: Path) -> None:
    directory = activity.busy_dir(_isolated)
    directory.mkdir(parents=True)
    (directory / f"{os.getpid()}-x.json").write_bytes(b"\xff\xfe")
    assert activity.busy_counts(_isolated) == {"unknown": 1}


@pytest.mark.asyncio
async def test_g2_runs_under_a_busy_marker(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    @contextlib.contextmanager
    def busy(kind: str, detail: str):
        seen.append((kind, detail))
        yield

    repo = _Repo(tmp_path, ("in_scope.txt",))
    gate = _gate(tmp_path, repo, _ScriptedCommands())
    gate._busy = busy
    await _g2(gate, _line(repo, ("in_scope.txt",)))
    assert seen == [("g2", "d1:line-0")]


class _Merger:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.during: dict[str, int] = {}
        self._gate = "gate"

    async def merge_line(self, line_id: str, *, extra: int = 0) -> str:
        self.during = activity.busy_counts(self.home)
        return f"merged {line_id}"

    def describe(self) -> str:
        return "sync"


@pytest.mark.asyncio
async def test_a_busy_proxy_marks_coroutine_methods_only(_isolated: Path) -> None:
    inner = _Merger(_isolated)
    proxy = BusyProxy(inner, activity.MERGE, activity.busy)

    assert await proxy.merge_line("line-0") == "merged line-0"
    assert inner.during == {"merge": 1}
    assert activity.busy_counts(_isolated) == {}
    assert proxy.describe() == "sync"
    assert proxy._gate == "gate"
    assert "extra" in inspect.signature(proxy.merge_line).parameters


# --------------------------------------------------------------------------
# The backend's view of the drain
# --------------------------------------------------------------------------


def test_ops_state_reports_the_drain_and_busy_markers(_isolated: Path) -> None:
    assert ops_state()["drain"]["drained"] is False
    set_drain(_isolated, "deploy")
    with activity.busy(activity.G2, "x"):
        state = ops_state()
    assert state["conductor_home"] == str(_isolated)
    assert state["drain"]["drained"] is True
    assert state["drain"]["reason"] == "deploy"
    assert state["drain"]["path"] == str(_isolated / "drain.json")
    assert state["busy"] == {"g2": 1}


def test_dispatch_status_shows_the_backends_drain(_isolated: Path) -> None:
    from src.tools.auto_dispatch.types import Dispatch

    dispatch = Dispatch(id="d1", goal="g", user_id="u", created_at="now", lines=[])
    assert "drain: off" in "\n".join(render_status(dispatch_summary(dispatch)))
    set_drain(_isolated, "deploy")
    summary = dispatch_summary(dispatch)
    assert summary["drain"]["drained"] is True
    text = "\n".join(render_status(summary))
    assert "drain: on" in text
    assert str(_isolated / "drain.json") in text
