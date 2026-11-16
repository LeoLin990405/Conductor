"""R6 L3: plan, assign and review are recorded service-role calls."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from src.server.services.autonomy.service_roles import (
    CHAIN_KEY,
    ServiceChain,
    instrument_planner,
    instrument_reviewer,
    instrument_selector,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _chain_on(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DISPATCH_SERVICE_CHAIN", "1")


def _dispatch():
    return SimpleNamespace(id="d1", gates={"schema_version": "dispatch_gates_v1"})


class _Planner:
    extra = "kept"

    async def plan(self, goal):
        return ["l1", "l2"]


class _Reviewer:
    def __init__(self):
        self.calls = []

    async def review_line(self, line_id, model, *, notes=""):
        self.calls.append((line_id, model, notes))
        return SimpleNamespace(passed=False)


class _Selector:
    def __init__(self):
        self.owner_preference = ()

    async def select(self, line, used_models, *, role=None, exclude=(), dispatch=None):
        return "kimi"


async def test_chain_records_plan_assign_review_in_order() -> None:
    dispatch = _dispatch()
    chain = ServiceChain(dispatch, clock=iter(range(100)).__next__)
    planner = instrument_planner(_Planner(), chain, model="claude-code")
    reviewer = instrument_reviewer(_Reviewer(), chain)
    selector = instrument_selector(_Selector(), chain)
    assert await planner.plan("goal") == ["l1", "l2"]
    assert await selector.select(SimpleNamespace(id="l1"), [], role=SimpleNamespace(value="work")) == "kimi"
    verdict = await reviewer.review_line("l1", "codex", notes="n")
    assert verdict.passed is False
    entries = dispatch.gates[CHAIN_KEY]
    assert [(e["role"], e["action"], e["step"]) for e in entries] == [
        ("planner", "plan", 1),
        ("router", "assign", 2),
        ("reviewer", "review", 3),
    ]
    assert entries[0]["outcome"] == "lines:2" and entries[0]["model"] == "claude-code"
    assert entries[1]["model"] == "kimi" and entries[1]["subject"] == "l1:work"
    # Review fix 4: a reviewer entry never carries the verdict.
    assert entries[2]["outcome"] == "returned" and entries[2]["session"] == "dispatch:d1"
    # Behaviour and types are unchanged; signatures are kept.
    assert isinstance(planner, _Planner) and planner.extra == "kept"
    assert "exclude" in inspect.signature(selector.select).parameters
    selector.owner_preference = ("a",)
    assert selector.owner_preference == ("a",)


async def test_errors_are_recorded_and_reraised() -> None:
    dispatch = _dispatch()
    chain = ServiceChain(dispatch)

    class _Bad:
        async def plan(self, goal):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await instrument_planner(_Bad(), chain).plan("g")
    assert dispatch.gates[CHAIN_KEY][-1]["outcome"] == "error:RuntimeError"


async def test_no_gates_snapshot_records_nothing() -> None:
    dispatch = SimpleNamespace(id="d", gates={})
    chain = ServiceChain(dispatch)
    await instrument_planner(_Planner(), chain).plan("g")
    assert dispatch.gates == {}


async def test_reviewer_without_notes_calls_two_arguments() -> None:
    inner = _Reviewer()
    await instrument_reviewer(inner, ServiceChain(_dispatch())).review_line("l", "codex")
    assert inner.calls == [("l", "codex", "")]


async def test_no_registry_and_no_switch_records_nothing(monkeypatch) -> None:
    """Review fix 3 (fails on 3025c108e): the row keeps main's shape."""
    monkeypatch.delenv("CONDUCTOR_DISPATCH_SERVICE_CHAIN")
    dispatch = _dispatch()
    await instrument_planner(_Planner(), ServiceChain(dispatch)).plan("g")
    assert CHAIN_KEY not in dispatch.gates
