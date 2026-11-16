"""S2: an old drive stops before merge when a line is reassigned."""

from __future__ import annotations

import asyncio

import pytest

from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch.gates import bump_epoch, drive_epoch
from src.tools.auto_dispatch.machine import human_reassign_refusal
from src.tools.auto_dispatch.types import RunStatus, Verdict
from tests.unit.server.services.test_dispatch_r2_stale_result import Copies, _service
from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line

pytestmark = pytest.mark.asyncio


async def test_s2_an_old_drive_does_not_merge_after_reassign_and_a_new_one_continues(
    tmp_path,
) -> None:
    store = Copies()
    entered, release = asyncio.Event(), asyncio.Event()

    class Reviewer:
        def __init__(self) -> None:
            self.seen: dict[str, int] = {}

        async def review_line(self, line_id: str, model: str) -> Verdict:
            del model
            seen = self.seen.get(line_id, 0)
            self.seen[line_id] = seen + 1
            if line_id == "a" and seen == 0:
                return Verdict.blocked(["fix"])
            return Verdict.ok()

    class Monitor:
        def __init__(self) -> None:
            self._runs: list[str] = []

        async def poll(self, run_id: str) -> RunStatus:
            # The fix launch is the second distinct run of line a. Its id is no
            # longer the old counter ``run-a-2``.
            line_id = run_id.split(":")[1] if run_id.count(":") >= 3 else ""
            earlier = [item for item in self._runs if item.split(":")[1] == line_id]
            self._runs.append(run_id)
            if line_id == "a" and earlier and run_id not in earlier:
                entered.set()
                await release.wait()
            return RunStatus(output="complete", complete=True, tokens=1)

        async def cancel(self, run_id: str) -> None:
            del run_id

    collaborators, machine, _commands, gates = _bundle(
        tmp_path,
        [_line("a"), _line("b")],
        store=store,
        reviewer=Reviewer(),
    )
    collaborators.monitor = Monitor()
    gates.handoff_facts = lambda *_args, **_kwargs: {
        "branch": "feature/a",
        "commit": "abc123",
        "files": ("a.py",),
        "test_command": "pytest",
        "results": "pass",
        "known_limitations": "none",
    }
    row = Dispatch(
        id="b2-mixed",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.SUBMITTED,
    )
    await store.create(row)
    service = _service(tmp_path, store, lambda _dispatch: machine)
    service._gate_runner = lambda: gates

    async def no_schedule(dispatch: Dispatch) -> Dispatch:
        return dispatch

    service._resume = no_schedule
    drive = asyncio.create_task(service._drive(row.id))
    await asyncio.wait_for(entered.wait(), timeout=5)
    before = await store.get(row.id)
    assert before is not None
    with pytest.raises(ValueError, match="^drive_active$"):
        await service.reassign(row.id, "a", "stepfun")
    with pytest.raises(ValueError, match="^drive_active$"):
        await service.approve(row.id, "no")
    with pytest.raises(ValueError, match="^drive_active$"):
        await service.reject(row.id, "no")
    held = await store.get(row.id)
    assert held is not None
    assert held.line("a") is not None
    assert before.line("a") is not None
    assert held.line("a").assigned_model == before.line("a").assigned_model
    assert held.line("a").attempts == before.line("a").attempts
    assert drive_epoch(held) == drive_epoch(before)

    async def mutation(current: Dispatch) -> Dispatch:
        line = current.line("a")
        assert line is not None
        reason = await human_reassign_refusal(current, line, "stepfun", gates)
        assert reason is None
        line.assigned_model = "stepfun"
        line.attempts += 1
        line.status = LineStatus.ASSIGNED
        line.current_run = None
        line.blockers = []
        bump_epoch(current)
        return current

    accepted = await service.apply_mutation(row.id, mutation)
    assert accepted.line("a") is not None
    assert accepted.line("a").assigned_model == "stepfun"
    assert accepted.line("a").attempts == before.line("a").attempts + 1
    assert drive_epoch(accepted) == drive_epoch(before) + 1
    release.set()
    await asyncio.wait_for(drive, timeout=10)
    assert row.id not in service.active_drives
    after = await store.get(row.id)
    assert after is not None
    assert after.state is not DispatchState.APPROVAL
    assert after.line("a") is not None
    assert after.line("a").assigned_model == "stepfun"
    assert after.line("a").attempts == before.line("a").attempts + 1
    integration = (after.gates.get("integration") or {}).get("phase")
    assert integration != "passed"
    assert collaborators.merger.merged == []
    audit = ((after.gates.get("scheduler") or {}).get("audit")) or []
    assert any(isinstance(item, dict) and item.get("kind") == "stale_drive" for item in audit)

    seen = len(collaborators.launcher.launches)

    class Open:
        async def poll(self, run_id: str) -> RunStatus:
            del run_id
            return RunStatus(output="complete", complete=True, tokens=1)

        async def cancel(self, run_id: str) -> None:
            del run_id

    collaborators.monitor = Open()
    follow = asyncio.create_task(service._drive(row.id))
    await asyncio.wait_for(follow, timeout=10)
    assert any(
        launch[0] == "a" and launch[1] == "stepfun"
        for launch in collaborators.launcher.launches[seen:]
    )
