"""The machine uses the two helpers from fix/dispatch-failure-feedback (PR #92).

- N27: a G2 fix round's prompt carries the failed G2 command, its exit code and
  output tail (``GateRunner.g2_fix_task``), on the direct and resumed fix path.
- N39: a recovery ``Action`` with ``prompt_note`` (a confirmation refusal) puts
  that note in the retried run's prompt, once.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.llms.native_cli.failure import (
    CONFIRMATION_REQUIRED_PROMPT_NOTE,
    FailureClass,
)
from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch.execution import LaunchResult, WorkerLaunchFailure
from src.tools.auto_dispatch.gates import CommandResult
from tests.unit.server.services.test_dispatch_r2_stale_result import Copies, _service
from tests.unit.server.services.test_dispatch_unwedge import (
    BASE,
    HEAD,
    _assigned,
    _handoff,
)
from tests.unit.tools.auto_dispatch.test_gate_recovery import ScriptedCommands, _bundle

pytestmark = pytest.mark.asyncio

G2_MARK = "G2 FAILED: pytest -q exited 1"


class Launcher:
    """Records each prompt; the first run per line can fail with a classified failure."""

    def __init__(self, first_failure: FailureClass | None = None) -> None:
        self.first_failure = first_failure
        self.tasks: list[str] = []

    async def launch(self, line, model, skills):
        raise AssertionError("the recovery machine uses launch_worker")

    async def launch_worker(
        self, line, model, skills, *, session_id, session_mode, handoff_path, **_kw
    ):
        self.tasks.append(line.task)
        run_id = line.current_run or f"run-{len(self.tasks)}"
        if self.first_failure is not None and len(self.tasks) == 1:
            raise WorkerLaunchFailure(
                run_id=run_id,
                exit_code=1,
                output="refused: needs confirmation",
                session_id=None,
                retry_after_seconds=None,
                failure_class=self.first_failure,
                reason_code="worker_confirmation_required",
            )
        return LaunchResult(run_id, session_id, "committed", start_head=BASE, head=HEAD)


async def _drive(tmp_path: Path, launcher: Launcher, *, commands=None, patch_gates=None):
    store = Copies()
    overrides = {"store": store, "launcher": launcher}
    if commands is not None:
        overrides["commands"] = commands
    _collab, machine, _commands, gates = _bundle(tmp_path, [], **overrides)
    gates.handoff_facts = _handoff
    gates._revisions = lambda _line: None
    if patch_gates is not None:
        patch_gates(gates)
    row = Dispatch(
        id="feedback",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.ASSIGNED,
        lines=[_assigned("a", "deepseek")],
    )
    gates.prepare(row)
    await store.create(row)
    service = _service(tmp_path, store, lambda _dispatch: machine)
    service._gate_runner = lambda: gates

    async def no_schedule(dispatch: Dispatch) -> Dispatch:
        return dispatch

    service._resume = no_schedule
    await asyncio.wait_for(service._drive(row.id), timeout=10)
    return await store.get(row.id), machine, gates


async def test_a_g2_fix_round_prompt_carries_the_g2_failure(tmp_path: Path) -> None:
    launcher = Launcher()
    commands = ScriptedCommands([CommandResult(exit_code=1, duration_ms=3)])
    after, _machine, _gates = await _drive(tmp_path, launcher, commands=commands)
    assert after.line("a").status is LineStatus.PASSED
    assert len(launcher.tasks) == 2
    assert "exit code: 1" not in launcher.tasks[0]
    # The fix round's prompt carries the failed G2 command and exit code.
    assert "exit code: 1" in launcher.tasks[1]
    assert "pytest -q" in launcher.tasks[1]


async def test_a_resumed_g2_fix_uses_the_note_stored_with_the_intent(
    tmp_path: Path,
) -> None:
    """After a restart the G2 cell is reset; the stored note keeps the task the same."""
    launcher = Launcher()
    _after, machine, gates = await _drive(tmp_path, launcher)
    seen: list[tuple[str, str]] = []

    async def begin_fix(dispatch, line, kind, author, attempt, source_ref, *, task, g2_note=""):
        seen.append((task, g2_note))

    machine._begin_fix = begin_fix
    machine._fix_intent_stale = lambda *_args: False
    row = await machine._c.store.get("feedback")
    line = row.line("a")
    line.blockers = []
    snap = gates.line_snapshot(row, line)
    view = dict(_handoff())
    view["blocker_count"] = 0
    snap.setdefault("gates", {})["handoff"] = view
    from src.tools.auto_dispatch.machine import _handoff_from_view

    snap["handoff_ref"] = _handoff_from_view(view, limitations="none").reference()
    machine._pending_fix = lambda _dispatch, _line_id: {
        "kind": "g2",
        "author": line.assigned_model,
        "attempt": line.attempts,
        "source_ref": "",
        "g2_note": G2_MARK,
    }

    async def pop_run(_line_id):
        return None

    async def submit(fn, *args, **kwargs):
        return await fn(*args, **kwargs)

    machine._submit = submit
    machine._pop_run = pop_run
    await machine._resume_fix(row, line)
    task, note = seen[-1]
    assert note == G2_MARK
    assert task.rstrip().endswith(G2_MARK)


async def test_a_confirmation_retry_carries_the_prompt_note_once(tmp_path: Path) -> None:
    launcher = Launcher(first_failure=FailureClass.CONFIRMATION_REQUIRED)
    after, _machine, _gates = await _drive(tmp_path, launcher)
    assert after.line("a").status is LineStatus.PASSED
    assert len(launcher.tasks) == 2
    assert CONFIRMATION_REQUIRED_PROMPT_NOTE not in launcher.tasks[0]
    assert CONFIRMATION_REQUIRED_PROMPT_NOTE in launcher.tasks[1]
    # Spent once the retried run started: not left for a later run.
    assert "prompt_note" not in after.gates["worker_recovery"]["a"]
