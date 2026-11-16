"""R6 L4 (machine side): fresh eyes at repeated debug points."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.tools.auto_dispatch import debug_switch
from src.tools.auto_dispatch.machine import DispatchMachine
from src.tools.auto_dispatch.types import Dispatch, Line


def test_plan_switches_on_second_failure_only() -> None:
    line = Line(id="l", name="l", task="t", assigned_model="kimi")
    first = debug_switch.plan(debug_switch.G2_FAILED, line, 0)
    second = debug_switch.plan(debug_switch.G2_FAILED, line, 1)
    assert not first.switch and first.reason.startswith("same_author_fix")
    assert second.switch and second.from_model == "kimi"
    assert second.to_dict()["role"] == "worker"
    with pytest.raises(ValueError):
        debug_switch.plan("nope", line, 1)


def test_mode_default_shadow() -> None:
    assert debug_switch.mode({}) == "shadow"
    assert debug_switch.mode({debug_switch.MODE_ENV: "enforce"}) == "enforce"


def _machine(snapshot):
    class _Gates:
        def line_snapshot(self, dispatch, line):
            return snapshot

    machine = object.__new__(DispatchMachine)
    machine._c = SimpleNamespace(gates=_Gates())
    machine.reassigned = []

    async def reassign(dispatch, line, reason):
        machine.reassigned.append(reason)
        return True

    machine._reassign = reassign
    return machine


def _case():
    line = Line(id="l", name="l", task="t", assigned_model="kimi")
    return Dispatch(id="d", goal="g", user_id="u", created_at="now", lines=[line]), line


@pytest.mark.asyncio
async def test_shadow_records_in_memory_and_keeps_the_same_author(monkeypatch) -> None:
    monkeypatch.delenv(debug_switch.MODE_ENV, raising=False)
    snapshot: dict = {}
    machine = _machine(snapshot)
    dispatch, line = _case()
    assert await machine._debug_switch(dispatch, line, debug_switch.G2_FAILED, 1) is False
    assert machine.reassigned == [] and snapshot == {}
    assert machine.debug_switch_audit[-1]["switch"] is True


@pytest.mark.asyncio
async def test_enforce_reassigns_on_the_second_failure(monkeypatch) -> None:
    monkeypatch.setenv(debug_switch.MODE_ENV, "enforce")
    snapshot: dict = {}
    machine = _machine(snapshot)
    dispatch, line = _case()
    assert await machine._debug_switch(dispatch, line, debug_switch.G2_FAILED, 0) is False
    assert await machine._debug_switch(dispatch, line, debug_switch.G2_FAILED, 1) is True
    assert machine.reassigned == ["debug_switch:g2_failed:after_1"]
    assert snapshot["debug_switch"][-1]["point"] == "g2_failed"


@pytest.mark.asyncio
async def test_off_does_nothing(monkeypatch) -> None:
    monkeypatch.setenv(debug_switch.MODE_ENV, "off")
    machine = _machine({})
    dispatch, line = _case()
    assert await machine._debug_switch(dispatch, line, debug_switch.REVIEW_BLOCKED, 3) is False
    assert not hasattr(machine, "debug_switch_audit")
