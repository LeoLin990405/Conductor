"""N86 and N87 through ``DispatchService``.

N86: a human reassign records a pin on the line, survives a drive save, and
is refused at reassign time (in ``enforce``) when the chosen worker is skipped
on this dispatch or has no quota. N87: ``recover --rereview`` sends a
replan-exhausted line back to G2 and review on its branch head.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.server.services.autonomy.stage_gate import REPLAN_OPTIONS, Stage
from src.server.services.dispatch import DispatchService, DispatchSettings
from src.tools.auto_dispatch import (
    Dispatch,
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    Line,
    LineStatus,
)
from src.tools.auto_dispatch import human_pin
from src.tools.auto_dispatch.gates import REPLAN_EXHAUSTED
from src.tools.auto_dispatch.machine import _load_state, _store_state
from tests.unit.tools.auto_dispatch.n76_fakes import make_gates

pytestmark = pytest.mark.asyncio


class Headroom:
    def __init__(self, left: dict[str, float]) -> None:
        self.left = left

    async def remaining(self, model: str) -> float:
        return self.left.get(model, 1.0)


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch, *, headroom=None) -> None:
        self.proposed = tmp_path / "proposed"
        self.store = InMemoryDispatchStore()
        self.scheduled: list[str] = []

        def _schedule(key, factory):
            self.scheduled.append(key)

        monkeypatch.setattr("src.server.services.dispatch._schedule", _schedule)
        self.service = DispatchService(
            DispatchSettings(
                proposed_dir=self.proposed,
                runs_dir=tmp_path / "runs",
                poll_interval_seconds=10.0,
            ),
            store=self.store,
            gate=FileApprovalGate(self.proposed),
            headroom=headroom if headroom is not None else Headroom({}),
        )

    async def parked(self, model: str = "deepseek") -> tuple[Dispatch, Line]:
        dispatch = await self.service.start("ship the thing", "u1")
        dispatch.state = DispatchState.REVIEW
        line = Line(
            id="line-0",
            name="n0",
            task="task 0",
            assigned_model=model,
            status=LineStatus.BLOCKED,
            failure_reason="no_new_commit",
        )
        dispatch.lines = [line]
        saved = await self.store.save(dispatch)
        return saved, saved.line("line-0")


# -- N86 ------------------------------------------------------------------------


async def test_a_human_reassign_records_a_pin(tmp_path, monkeypatch) -> None:
    harness = Harness(tmp_path, monkeypatch)
    row, line = await harness.parked()
    after = await harness.service.reassign(row.id, line.id, "kimi")
    assert after.line(line.id).assigned_model == "kimi"
    pin = after.gates[human_pin.HUMAN_PINS][line.id]
    assert pin["model"] == "kimi" and pin["queued"] is False
    assert human_pin.pinned_model(after, line.id) == "kimi"


async def test_the_pin_survives_a_write_that_resets_gates(tmp_path, monkeypatch) -> None:
    harness = Harness(tmp_path, monkeypatch)
    row, line = await harness.parked()
    await harness.service.reassign(row.id, line.id, "kimi")

    def reset(r: Dispatch) -> Dispatch:
        # What a live drive's first Stage-Gate snapshot does to the row.
        r.gates = {"schema_version": "dispatch_gates_v1", "lines": {}}
        return r

    saved = await harness.service.apply_mutation(row.id, reset)
    assert human_pin.pinned_model(saved, line.id) == "kimi"


async def test_enforce_refuses_a_worker_skipped_on_this_dispatch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(human_pin.HUMAN_PIN_MODE_ENV, "enforce")
    harness = Harness(tmp_path, monkeypatch)
    row, line = await harness.parked()
    row.gates["provider_skips"] = {"kimi": {"until": 4_102_444_800.0, "reason": "quota_exhausted"}}
    await harness.store.save(row)
    with pytest.raises(ValueError, match="^worker_skipped: kimi"):
        await harness.service.reassign(row.id, line.id, "kimi")
    kept = await harness.store.get(row.id)
    assert kept.line(line.id).assigned_model == "deepseek"
    assert human_pin.HUMAN_PINS not in kept.gates


async def test_enforce_refuses_a_worker_with_no_quota(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(human_pin.HUMAN_PIN_MODE_ENV, "enforce")
    harness = Harness(tmp_path, monkeypatch, headroom=Headroom({"kimi": 0.0}))
    row, line = await harness.parked()
    with pytest.raises(ValueError, match="^worker_no_headroom: kimi"):
        await harness.service.reassign(row.id, line.id, "kimi")
    # Another worker with quota is accepted.
    after = await harness.service.reassign(row.id, line.id, "glm")
    assert after.line(line.id).assigned_model == "glm"


async def test_shadow_lets_the_reassign_through_and_audits_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(human_pin.HUMAN_PIN_MODE_ENV, "shadow")
    harness = Harness(tmp_path, monkeypatch, headroom=Headroom({"kimi": 0.0}))
    row, line = await harness.parked()
    after = await harness.service.reassign(row.id, line.id, "kimi")
    assert after.line(line.id).assigned_model == "kimi"
    kinds = [item.get("kind") for item in after.gates["scheduler"]["audit"]]
    assert "human_pin_shadow_refusal" in kinds


# -- N87 ------------------------------------------------------------------------


async def test_recover_rereview_sends_a_replan_exhausted_line_back_to_g2(
    tmp_path, monkeypatch
) -> None:
    harness = Harness(tmp_path, monkeypatch)
    row, line = await harness.parked()
    line.branch = "feature/auto-dispatch-x/line-line-0"
    line.failure_reason = REPLAN_EXHAUSTED
    gates = make_gates(tmp_path)
    snap = gates.line_snapshot(row, line)
    _store_state(
        snap,
        replace(
            _load_state(snap),
            stage=Stage.REPLANNING,
            replan_options=REPLAN_OPTIONS,
            developer_provider="deepseek",
        ),
    )
    await harness.store.save(row)
    # Before N87 unblock was the only other verb, and it refuses this park.
    with pytest.raises(ValueError, match="line_not_wedged"):
        await harness.service.unblock(row.id, line.id)

    after = await harness.service.recover(row.id, line.id, "rereview", "fixed by hand")

    moved = after.line(line.id)
    assert moved.status is LineStatus.RUNNING
    assert moved.failure_reason is None
    cell = after.gates["lines"][line.id]
    assert _load_state(cell).stage is Stage.FOCUSED_TESTS
    assert cell["gates"]["G2"]["result"] == "pending"
    settled = after.gates["scheduler"]["settled_lines"]
    assert {"line_id": line.id, "run_id": moved.current_run, "attempt": moved.attempts}.items() <= settled[-1].items()
    # The parked dispatch is driven again so G2 and the review run.
    assert harness.scheduled


async def test_recover_rereview_refuses_an_unparked_line(tmp_path, monkeypatch) -> None:
    harness = Harness(tmp_path, monkeypatch)
    row, line = await harness.parked()
    make_gates(tmp_path).line_snapshot(row, line)
    line.status = LineStatus.PASSED
    await harness.store.save(row)
    with pytest.raises(ValueError, match="line_not_parked"):
        await harness.service.recover(row.id, line.id, "rereview")


async def test_an_unknown_recover_mode_is_refused(tmp_path, monkeypatch) -> None:
    harness = Harness(tmp_path, monkeypatch)
    row, line = await harness.parked()
    with pytest.raises(ValueError, match="rereview"):
        await harness.service.recover(row.id, line.id, "sideways")
