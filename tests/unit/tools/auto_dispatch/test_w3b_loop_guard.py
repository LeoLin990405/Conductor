"""A machine bound to one event loop refuses a second loop by name."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.tools.auto_dispatch import (
    AlwaysPassReviewer,
    Collaborators,
    Dispatch,
    DispatchLoopMismatch,
    DispatchMachine,
    DispatchState,
    FileApprovalGate,
    FixedPlanner,
    InMemoryDispatchStore,
    InMemoryOutcomeStore,
    NoopMerger,
    RecordingLauncher,
    RoundRobinSelector,
)
from src.tools.auto_dispatch.types import RunStatus


class _Finish:
    async def poll(self, run_id: str) -> RunStatus:
        return RunStatus(output=f"done {run_id}", complete=True, tokens=1)

    async def cancel(self, run_id: str) -> None:
        del run_id


async def _no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


def _machine(tmp_path: Path) -> tuple[Collaborators, DispatchMachine]:
    collaborators = Collaborators(
        planner=FixedPlanner(),
        selector=RoundRobinSelector(),
        launcher=RecordingLauncher(),
        monitor=_Finish(),  # type: ignore[arg-type]
        reviewer=AlwaysPassReviewer(),
        merger=NoopMerger(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        outcomes=InMemoryOutcomeStore(),
        store=InMemoryDispatchStore(),
    )
    return collaborators, DispatchMachine(collaborators, sleep=_no_sleep)


def test_a_second_loop_raises_dispatch_loop_mismatch(tmp_path: Path) -> None:
    collaborators, machine = _machine(tmp_path)
    store = collaborators.store

    async def start() -> Dispatch:
        created = await store.create(
            Dispatch(
                id="loopguard1",
                goal="guard the loop",
                user_id="u",
                created_at="2026-11-07T00:00:00+00:00",
            )
        )
        return await machine.run(created)

    with asyncio.Runner() as first:
        parked = first.run(start())
        assert parked.state is DispatchState.APPROVAL

        async def from_another_loop() -> None:
            with pytest.raises(DispatchLoopMismatch, match="another event loop"):
                await machine.run(parked)
            with pytest.raises(DispatchLoopMismatch):
                await machine.aclose()

        # A fresh loop is refused with the named error, not asyncio's
        # "bound to a different event loop" RuntimeError.
        asyncio.run(from_another_loop())

        # The refusal did not rebuild or damage the machine: the bound loop
        # still finishes the dispatch after the human approves.
        async def decide() -> Dispatch:
            row = await store.get("loopguard1")
            assert row is not None
            await collaborators.gate.resolve_dispatch("loopguard1", True, "")
            return await machine.run(row)

        finished = first.run(decide())
        assert finished.state is DispatchState.COMPLETE


def test_dispatch_loop_mismatch_is_a_runtime_error() -> None:
    assert issubclass(DispatchLoopMismatch, RuntimeError)
