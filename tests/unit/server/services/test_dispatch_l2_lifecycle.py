"""L2: abort in any state, restart recovery during planning, per-dispatch workers.

Dev adapters and in-memory fakes only; no drive reaches a real Worker.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.server.services.dispatch import DispatchService, DispatchSettings
from src.tools.auto_dispatch import (
    Dispatch,
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    Line,
    LineStatus,
)
from src.tools.auto_dispatch.gates import RESUME_SPEC_MISSING, drive_epoch
from src.tools.auto_dispatch.selection import ALLOWED_WORKERS

pytestmark = pytest.mark.asyncio


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch, *, poll: float = 10.0) -> None:
        self.proposed = tmp_path / "proposed"
        self.store = InMemoryDispatchStore()
        self.gate = FileApprovalGate(self.proposed)
        self.scheduled: list[str] = []

        def _schedule(key, factory):
            self.scheduled.append(key)

        monkeypatch.setattr("src.server.services.dispatch._schedule", _schedule)
        self.service = DispatchService(
            DispatchSettings(
                proposed_dir=self.proposed,
                runs_dir=tmp_path / "runs",
                poll_interval_seconds=poll,
            ),
            store=self.store,
            gate=self.gate,
        )

    async def row(self, state: DispatchState, *, lines: int = 0, **fields) -> Dispatch:
        dispatch = await self.service.start("ship the thing", "u1", **fields)
        dispatch.state = state
        dispatch.lines = [
            Line(id=f"line-{index}", name=f"n{index}", task=f"task {index}")
            for index in range(lines)
        ]
        return await self.store.save(dispatch)


@pytest.fixture
def harness(tmp_path, monkeypatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


# -- cancel / abort ---------------------------------------------------------------


@pytest.mark.parametrize(
    "state", [DispatchState.SUBMITTED, DispatchState.PLANNING, DispatchState.MERGE]
)
async def test_cancel_ends_a_dispatch_that_is_not_parked(harness: Harness, state) -> None:
    row = await harness.row(state, lines=1 if state is DispatchState.MERGE else 0)

    ended = await harness.service.cancel(row.id, note="not now")

    assert ended.state is DispatchState.ABORT
    assert f"while it was {state.value}" in ended.message
    assert "not now" in ended.message


@pytest.mark.parametrize(
    "state",
    [
        DispatchState.SUBMITTED,
        DispatchState.PLANNING,
        DispatchState.ASSIGNED,
        DispatchState.LAUNCHING,
        DispatchState.MONITORING,
        DispatchState.REVIEW,
        DispatchState.MERGE,
    ],
)
async def test_abort_ends_every_live_state(harness: Harness, state) -> None:
    row = await harness.row(state, lines=1)

    ended = await harness.service.abort(row.id)

    assert ended.state is DispatchState.ABORT


async def test_abort_at_the_gate_rejects_and_moves_the_branch_aside(harness: Harness) -> None:
    started = await harness.service.start("ship the thing", "u1")
    parked = await harness.service._drive(started.id)
    assert parked.state is DispatchState.APPROVAL

    await harness.service.abort(parked.id, note="no")
    ended = await harness.service._drive(parked.id)

    assert ended.state is DispatchState.ABORT
    assert "rejected" in (ended.message or "")


@pytest.mark.parametrize(
    "state", [DispatchState.COMPLETE, DispatchState.ABORT, DispatchState.ERROR]
)
async def test_abort_refuses_a_terminal_dispatch(harness: Harness, state) -> None:
    row = await harness.row(state)
    with pytest.raises(ValueError, match=f"already {state.value}"):
        await harness.service.abort(row.id)


async def test_a_planning_drive_needs_force_and_force_fences_it(harness: Harness) -> None:
    row = await harness.row(DispatchState.PLANNING)
    before = drive_epoch(row)
    harness.service.active_drives[row.id] = object()
    try:
        with pytest.raises(ValueError, match="^drive_active"):
            await harness.service.abort(row.id)
        ended = await harness.service.abort(row.id, force=True)
    finally:
        harness.service.active_drives.pop(row.id, None)
    assert ended.state is DispatchState.ABORT
    assert drive_epoch(ended) > before


async def test_the_abort_route_ends_a_planning_dispatch(tmp_path, monkeypatch) -> None:
    from unittest.mock import patch

    from httpx import ASGITransport, AsyncClient

    from src.server.app.dispatch import router
    from tests.conftest import create_test_app

    harness = Harness(tmp_path, monkeypatch)
    row = Dispatch(id="d-plan", goal="g", user_id="test-user-123", created_at="now")
    row.state = DispatchState.PLANNING
    await harness.store.create(row)
    app = create_test_app(router)
    with patch("src.server.app.dispatch._service", return_value=harness.service):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            response = await http.post("/api/v1/dispatch/d-plan/abort", json={"note": "x"})
            again = await http.post("/api/v1/dispatch/d-plan/abort", json={})
    assert response.status_code == 200
    assert response.json()["state"] == "ABORT"
    assert again.status_code == 409


# -- restart during planning --------------------------------------------------------


async def _wait_for(store, dispatch_id: str, states: set[DispatchState]) -> Dispatch:
    for _ in range(200):
        row = await store.get(dispatch_id)
        if row is not None and row.state in states:
            return row
        await asyncio.sleep(0.02)
    raise AssertionError(f"dispatch stayed {row.state if row else None}")


@pytest.mark.parametrize(
    "stranded",
    [
        {"state": DispatchState.PLANNING},
        {"state": DispatchState.REVIEW, "message": RESUME_SPEC_MISSING},
    ],
    ids=["planning-without-spec", "parked-resume-spec-missing"],
)
async def test_a_restart_during_planning_replans_instead_of_stranding(
    tmp_path, monkeypatch, stranded
) -> None:
    harness = Harness(tmp_path, monkeypatch, poll=0.05)
    row = Dispatch(id="d-restart", goal="ship the thing", user_id="u1", created_at="now")
    await harness.store.create(row)
    row.state = stranded["state"]
    row.message = stranded.get("message")
    await harness.store.save(row)

    # A fresh backend: open() starts the wakeup, which recovers the row.
    await harness.service.open()
    try:
        parked = await _wait_for(
            harness.store, row.id, {DispatchState.APPROVAL, DispatchState.REVIEW}
        )
        if parked.state is DispatchState.REVIEW:
            parked = await _wait_for(harness.store, row.id, {DispatchState.APPROVAL})
    finally:
        await harness.service.close()

    assert parked.state is DispatchState.APPROVAL
    assert len(parked.lines) == 3
    assert parked.gates["planning_restarts"] == 1


async def test_planning_restarts_are_bounded(tmp_path, monkeypatch) -> None:
    harness = Harness(tmp_path, monkeypatch)
    row = Dispatch(id="d-loop", goal="g", user_id="u1", created_at="now")
    row.state = DispatchState.PLANNING
    row.gates = {"planning_restarts": 3}
    await harness.store.create(row)

    assert await harness.service._restart_stranded_planning(row.id) is False
    assert (await harness.store.get(row.id)).state is DispatchState.PLANNING


async def test_a_row_with_lines_or_a_spec_is_not_restarted(harness: Harness) -> None:
    with_lines = await harness.row(DispatchState.PLANNING, lines=1)
    with_spec = await harness.row(DispatchState.PLANNING)
    with_spec.spec_ref = "spec:0123456789abcdef"
    await harness.store.save(with_spec)

    assert await harness.service._restart_stranded_planning(with_lines.id) is False
    assert await harness.service._restart_stranded_planning(with_spec.id) is False


# -- per-dispatch workers ------------------------------------------------------------


async def test_start_keeps_the_worker_restriction_on_the_row(harness: Harness) -> None:
    row = await harness.service.start("g", "u1", workers="Kimi, glm,kimi")
    stored = await harness.store.get(row.id)
    assert stored.gates[ALLOWED_WORKERS] == ["kimi", "glm"]
    plain = await harness.service.start("g", "u1")
    assert ALLOWED_WORKERS not in plain.gates
    assert "allowed_workers" not in plain.to_dict()


async def test_start_refuses_a_reserved_or_malformed_worker(harness: Harness) -> None:
    reserved = sorted(harness.service._policy.canonical_reserved())[0]
    with pytest.raises(ValueError, match="reserved"):
        await harness.service.start("g", "u1", workers=[reserved])
    with pytest.raises(ValueError, match="Invalid worker name"):
        await harness.service.start("g", "u1", workers=["../x"])


async def test_a_restricted_dev_drive_assigns_only_the_allowed_worker(harness: Harness) -> None:
    started = await harness.service.start("ship", "u1", workers=["glm"])
    parked = await harness.service._drive(started.id)
    assert parked.state is DispatchState.APPROVAL
    assert {line.assigned_model for line in parked.lines} == {"glm"}


async def test_reassign_is_held_to_the_dispatch_workers(harness: Harness) -> None:
    row = await harness.row(DispatchState.REVIEW, lines=1, workers=["kimi"])
    line = row.lines[0]
    line.status = LineStatus.BLOCKED
    line.assigned_model = "kimi"
    await harness.store.save(row)

    with pytest.raises(ValueError, match="^worker_not_allowed: glm"):
        await harness.service.reassign(row.id, line.id, "glm")
    kept = await harness.store.get(row.id)
    assert kept.line(line.id).assigned_model == "kimi"


async def test_a_write_that_resets_gates_keeps_the_service_keys(harness: Harness) -> None:
    row = await harness.row(DispatchState.MONITORING, workers=["kimi"])

    def reset(r: Dispatch) -> Dispatch:
        # What a live drive's first Stage-Gate snapshot does to the row.
        r.gates = {"schema_version": "dispatch_gates_v1", "lines": {}}
        return r

    saved = await harness.service.apply_mutation(row.id, reset)

    assert saved.gates[ALLOWED_WORKERS] == ["kimi"]
    assert saved.gates["schema_version"] == "dispatch_gates_v1"
