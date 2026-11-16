"""F2: a committed reassignment survives the old drive's final save.

While that drive task is in ``active_drives``, the public reassign API
returns ``drive_active`` and does not write. The epoch check still drops
the old drive's write when a mutation has already moved the row.
"""

from __future__ import annotations

import asyncio
import copy

import pytest

from src.server.services.dispatch import DispatchService, DispatchSettings
from src.tools.auto_dispatch import Dispatch, DispatchState, InMemoryDispatchStore, Line, LineStatus
from src.tools.auto_dispatch.gates import bump_epoch

pytestmark = pytest.mark.asyncio


class Copies(InMemoryDispatchStore):
    async def create(self, dispatch: Dispatch) -> Dispatch:
        self.rows[dispatch.id] = copy.deepcopy(dispatch)
        return copy.deepcopy(dispatch)

    async def save(self, dispatch: Dispatch) -> Dispatch:
        self.rows[dispatch.id] = copy.deepcopy(dispatch)
        return copy.deepcopy(dispatch)

    async def get(self, dispatch_id: str) -> Dispatch | None:
        row = self.rows.get(dispatch_id)
        return None if row is None else copy.deepcopy(row)


async def test_f2_a_drive_save_keeps_the_other_lines_reassignment(tmp_path) -> None:
    store = Copies()
    blocked = Line(
        id="b",
        name="b",
        task="blocked task",
        assigned_model="kimi",
        status=LineStatus.BLOCKED,
    )
    active = Line(
        id="a",
        name="a",
        task="active task",
        assigned_model="glm",
        status=LineStatus.RUNNING,
        current_run="run-active",
    )
    await store.create(
        Dispatch(
            id="race",
            goal="test",
            user_id="u",
            created_at="now",
            state=DispatchState.MONITORING,
            lines=[blocked, active],
        )
    )
    entered, release = asyncio.Event(), asyncio.Event()

    class Machine:
        async def run(self, dispatch: Dispatch) -> Dispatch:
            entered.set()
            await release.wait()
            dispatch.state = DispatchState.REVIEW
            dispatch.lines[1].status = LineStatus.BLOCKED
            return dispatch

    service = DispatchService(
        DispatchSettings(
            proposed_dir=tmp_path / "proposed",
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path,
        ),
        store=store,
        machine_factory=lambda _dispatch: Machine(),
    )

    async def no_schedule(dispatch: Dispatch) -> Dispatch:
        return dispatch

    service._resume = no_schedule
    drive = asyncio.create_task(service._drive("race"))
    await entered.wait()
    before = await store.get("race")
    assert before is not None
    # The RUNNING line belongs to the drive. (A BLOCKED line's reassign is
    # queued for the drive since N25b, so it is no longer the probe.)
    with pytest.raises(ValueError, match="^drive_active$"):
        await service.reassign("race", "a", "stepfun")
    unchanged = await store.get("race")
    assert unchanged is not None
    assert unchanged.line("b") is not None
    assert unchanged.line("b").assigned_model == "kimi"
    assert unchanged.line("b").attempts == before.line("b").attempts

    def mutation(row: Dispatch) -> Dispatch:
        line = row.line("b")
        assert line is not None
        line.assigned_model = "stepfun"
        line.attempts += 1
        line.status = LineStatus.ASSIGNED
        bump_epoch(row)
        return row

    await service.apply_mutation("race", mutation)
    release.set()
    await drive
    after = await store.get("race")
    assert after is not None
    kept = after.line("b")
    assert kept is not None
    assert kept.assigned_model == "stepfun"
    assert kept.attempts == 1
    assert after.state is not DispatchState.REVIEW
    active = after.line("a")
    assert active is not None
    assert active.status is not LineStatus.BLOCKED
    audit = ((after.gates or {}).get("scheduler") or {}).get("audit") or []
    assert any(isinstance(item, dict) and item.get("kind") == "stale_drive" for item in audit)
