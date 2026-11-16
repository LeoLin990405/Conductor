"""Part-1 third-review follow-ups to N42's per-line review retry.

- A parked line, a human reassign and a fresh launch drop any scheduled
  review retry and its count, so none carries over to another run.
- The drive's loop guard is offset by 2 per review retry, so a long reviewer
  outage plus fix rounds is not read as "not a DAG".
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest

from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus, Verdict
from src.tools.auto_dispatch.execution import LaunchResult
from src.tools.auto_dispatch.machine import (
    REVIEW_RETRY,
    MachineConfig,
    apply_human_reassign,
)
from tests.unit.server.services.test_dispatch_r2_stale_result import Copies, _service
from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line

pytestmark = pytest.mark.asyncio

BASE = "b" * 40
HEAD = "1" * 40


def _handoff(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "branch": "feature/x",
        "commit": "abc123",
        "files": ("x.py",),
        "test_command": "pytest",
        "results": "ok",
        "known_limitations": "none",
    }


class OverlapLauncher:
    """Each worker waits (bounded) until ``expect`` workers are in at once."""

    def __init__(self, expect: int = 2, *, script: dict[str, list[object]] | None = None) -> None:
        self.expect = expect
        self.inside = 0
        self.peak = 0
        self.calls: list[tuple[str, str]] = []
        self.run_ids: list[str] = []
        self.script = {key: list(value) for key, value in (script or {}).items()}
        self._crowd = asyncio.Event()

    async def launch(self, line, model, skills):
        raise AssertionError("the recovery machine uses launch_worker")

    async def launch_worker(self, line, model, skills, *, session_id, session_mode, handoff_path, **_kw):
        self.calls.append((line.id, model))
        run_id = line.current_run
        assert run_id, "a worker never starts without its reserved run"
        self.run_ids.append(run_id)
        steps = self.script.get(line.id) or []
        step = steps.pop(0) if steps else None
        if isinstance(step, BaseException):
            raise step
        self.inside += 1
        self.peak = max(self.peak, self.inside)
        if self.inside >= self.expect:
            self._crowd.set()
        try:
            await asyncio.wait_for(self._crowd.wait(), timeout=0.5)
        except TimeoutError:
            pass
        finally:
            self.inside -= 1
        return LaunchResult(run_id, session_id, "committed", start_head=BASE, head=HEAD)


async def _setup(tmp_path: Path, lines, *, launcher, parallel: int = 2, reviewer=None):
    store = Copies()
    overrides = {"store": store, "launcher": launcher}
    if reviewer is not None:
        overrides["reviewer"] = reviewer
    _collab, machine, _commands, gates = _bundle(tmp_path, [], **overrides)
    if parallel > 1:
        machine._config = MachineConfig(max_parallel_lines=parallel)
    gates.handoff_facts = _handoff
    gates._revisions = lambda _line: None
    row = Dispatch(
        id="par",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.ASSIGNED,
        lines=lines,
    )
    gates.prepare(row)
    await store.create(row)
    service = _service(tmp_path, store, lambda _dispatch: machine)
    service._gate_runner = lambda: gates

    async def no_schedule(dispatch: Dispatch) -> Dispatch:
        return dispatch

    service._resume = no_schedule
    return store, machine, gates, service, row


def _assigned(line_id: str, model: str, scope: tuple[str, ...], **extra):
    return _line(
        line_id,
        status=LineStatus.ASSIGNED,
        assigned_model=model,
        base_commit=BASE,
        write_scope=scope,
        **extra,
    )


async def _drive(service, dispatch_id: str) -> None:
    await asyncio.wait_for(service._drive(dispatch_id), timeout=20)


# -- part-1 third review follow-ups ------------------------------------------


async def test_block_reassign_and_a_fresh_launch_clear_the_review_retry(tmp_path: Path) -> None:
    launcher = OverlapLauncher(expect=1)
    store, machine, gates, _service, row = await _setup(
        tmp_path, [_assigned("a", "kimi", ("src/a/",))], launcher=launcher, parallel=1
    )
    dispatch = await store.get(row.id)
    line = dispatch.line("a")
    snap = gates.line_snapshot(dispatch, line)

    def seed() -> None:
        snap[REVIEW_RETRY] = {"attempt": 1, "retry_at": 9e12, "failed": ["x"]}
        snap["review_attempts"] = 1

    seed()
    machine._block_line(dispatch, line, "review_blocked")
    assert REVIEW_RETRY not in snap and "review_attempts" not in snap

    seed()
    line.status = LineStatus.BLOCKED
    assert await apply_human_reassign(dispatch, line, "glm", None, None) is None
    assert REVIEW_RETRY not in snap and "review_attempts" not in snap

    seed()
    line.status = LineStatus.ASSIGNED
    await machine._claim_drive()
    try:
        await machine._launch_line(dispatch, line)
    finally:
        machine._release_drive()
    assert REVIEW_RETRY not in gates.line_snapshot(dispatch, line)


async def test_the_loop_guard_is_offset_per_review_retry(tmp_path: Path) -> None:
    """A long reviewer outage plus fix rounds must not read as a cycle."""

    class OutageThenBlocks:
        def __init__(self) -> None:
            self.calls = 0

        async def review_line(self, line_id: str, model: str) -> Verdict:
            self.calls += 1
            # Three outages before each verdict: BLOCK, BLOCK, then a pass.
            if self.calls % 4 != 0:
                raise RuntimeError("reviewer down")
            if self.calls in (4, 8):
                return Verdict.blocked(["fix x"])
            return Verdict.ok("ok")

    reviewer = OutageThenBlocks()
    launcher = OverlapLauncher(expect=1)
    store, machine, gates, service, row = await _setup(
        tmp_path, [_assigned("a", "kimi", ("src/a/",))], launcher=launcher, reviewer=reviewer, parallel=1
    )
    gates._settings = copy.copy(gates._settings)
    object.__setattr__(gates._settings, "max_g2_fixes", 0)
    object.__setattr__(gates._settings, "max_replans", 0)
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert after.state is not DispatchState.ERROR, after.message
    assert after.line("a").status is LineStatus.PASSED
