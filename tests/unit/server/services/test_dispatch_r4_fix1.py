"""R4: close joins the actor save before the drive owner is released."""

from __future__ import annotations

import asyncio

import pytest

from src.server.services.dispatch import DispatchService, DispatchSettings
from src.tools.auto_dispatch import Dispatch, DispatchState, InMemoryDispatchStore
from src.tools.auto_dispatch.gates import RESUME_SPEC_MISSING
from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line

pytestmark = pytest.mark.asyncio


async def test_close_joins_the_actor_before_dropping_the_owner(tmp_path) -> None:
    """While the actor's save is held, close stays pending and the owner stays.

    After the save returns, close finishes with no actor left and no owner.
    A later drive of the parked row still reaches ``resume_spec_missing``.
    """
    store = InMemoryDispatchStore()
    save_started = asyncio.Event()
    release_save = asyncio.Event()
    original_save = store.save

    async def blocked_save(dispatch: Dispatch) -> Dispatch:
        if not save_started.is_set():
            save_started.set()
            await release_save.wait()
        return await original_save(dispatch)

    store.save = blocked_save  # type: ignore[method-assign]
    row = Dispatch(
        id="close-during-actor-save",
        goal="ship a feature",
        user_id="u",
        created_at="now",
        state=DispatchState.SUBMITTED,
    )
    await store.create(row)
    machines: list[object] = []

    def factory(_dispatch: Dispatch):
        _collaborators, machine, _commands, _gates = _bundle(tmp_path, [_line("a")], store=store)
        machines.append(machine)
        return machine

    service = DispatchService(
        DispatchSettings(
            proposed_dir=tmp_path / "proposed",
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path,
            poll_interval_seconds=30,
        ),
        store=store,
        machine_factory=factory,
    )
    await service.open()
    await asyncio.wait_for(save_started.wait(), timeout=3)
    machine = machines[0]
    actor = machine._actor_task
    assert row.id in service.active_drives
    assert actor is not None and not actor.done()

    close_task = asyncio.create_task(service.close())
    for _ in range(10):
        await asyncio.sleep(0)
    assert close_task.done() is False
    assert row.id in service.active_drives
    assert not actor.done()

    release_save.set()
    await asyncio.wait_for(close_task, timeout=3)
    assert actor.done()
    assert row.id not in service.active_drives
    stored = await store.get(row.id)
    assert stored is not None
    assert stored.state is DispatchState.PLANNING
    assert not (stored.spec_ref or "").strip()

    resumed = await asyncio.wait_for(service._drive(row.id), timeout=3)
    assert resumed.state is DispatchState.REVIEW
    assert resumed.message == RESUME_SPEC_MISSING
