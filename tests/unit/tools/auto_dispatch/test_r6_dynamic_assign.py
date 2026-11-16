"""R6 L2: dynamic worker assignment inside the role registry."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.server.services.autonomy import role_types
from src.server.services.autonomy.role_types import (
    SOURCE_STORE,
    ModelRef,
    RoleConstraints,
    RoleEntry,
    fallback_registry,
)
from src.tools.auto_dispatch import dynamic_assign
from src.tools.auto_dispatch.machine import DispatchMachine
from src.tools.auto_dispatch.pool import eligible_models
from src.tools.auto_dispatch.protocols import ModelRole
from src.tools.auto_dispatch.types import Dispatch, Line

ROSTER = ("kimi", "glm", "grok", "cursor", "stepfun", "deepseek", "minimax", "doubao")


def _stored(candidates, *, excluded=(), resume=False):
    base = fallback_registry({}, router_model="router-model")
    worker = RoleEntry(
        "worker",
        tuple(ModelRef.parse(c) for c in candidates),
        ModelRef.parse(candidates[0]),
        RoleConstraints(excluded=tuple(excluded), requires_session_resume=resume),
    )
    reg = base.with_entry(worker)
    return role_types.RoleRegistry(entries=reg.entries, source=SOURCE_STORE, revision=3)


@pytest.fixture(autouse=True)
def _no_provider():
    role_types.set_registry_provider(None)
    yield
    role_types.set_registry_provider(None)


def test_fallback_registry_changes_nothing() -> None:
    line = Line(id="l", name="l", task="t")
    decision = dynamic_assign.decide(line, ROSTER)
    assert decision.never == () and decision.preferred == () and not decision.active
    decision = dynamic_assign.decide(line, ROSTER, registry=fallback_registry({}, router_model="r"))
    assert decision.never == () and not decision.active


def test_store_registry_excludes_non_candidates_and_keeps_order() -> None:
    reg = _stored(["stepfun", "kimi", "glm"], excluded=["glm"])
    decision = dynamic_assign.decide(Line(id="l", name="l", task="t"), ROSTER, registry=reg)
    assert decision.active
    assert set(decision.never) == set(ROSTER) - {"stepfun", "kimi"}
    assert decision.reasons["glm"] == dynamic_assign.EXCLUDED
    assert decision.reasons["grok"] == dynamic_assign.NOT_A_CANDIDATE
    assert decision.preferred == ("stepfun", "kimi")


def test_session_resume_constraint_uses_capability() -> None:
    reg = _stored(["stepfun", "kimi"], resume=True)
    decision = dynamic_assign.decide(
        Line(id="l", name="l", task="t"),
        ROSTER,
        registry=reg,
        resume_supported=lambda name: name == "stepfun",
    )
    assert decision.reasons["kimi"] == dynamic_assign.NO_SESSION_RESUME
    assert decision.preferred == ("stepfun",)


def test_ticket_worker_allowed() -> None:
    assert dynamic_assign.ticket_worker_allowed("grok") == (True, None)
    reg = _stored(["stepfun"])
    assert dynamic_assign.ticket_worker_allowed("grok", registry=reg) == (
        False,
        dynamic_assign.NOT_A_CANDIDATE,
    )
    assert dynamic_assign.ticket_worker_allowed("step", registry=reg) == (True, None)


def test_router_and_worker_never_share_a_session() -> None:
    dynamic_assign.assert_distinct_sessions("s1", "s2")
    dynamic_assign.assert_distinct_sessions("", "")
    with pytest.raises(dynamic_assign.SessionConflict):
        dynamic_assign.assert_distinct_sessions("s1", "s1")


class _Selector:
    def __init__(self) -> None:
        self.owner_preference: tuple[str, ...] = ()
        self.calls: list[tuple[str, ...]] = []

    async def select(self, line, used, *, role=ModelRole.WORK, exclude=(), dispatch=None):
        self.calls.append(tuple(exclude))
        for name in eligible_models("work"):
            if name not in exclude:
                return name
        return None


def _machine(selector, snapshot):
    class _Gates:
        def line_snapshot(self, dispatch, line):
            return snapshot

    machine = object.__new__(DispatchMachine)
    machine._c = SimpleNamespace(selector=selector, gates=_Gates())
    machine._skipped_providers = lambda dispatch: []
    machine._selection_used = lambda dispatch, line, extra, role: []

    async def leave(coro):
        return await coro

    machine._leave = leave
    return machine


@pytest.mark.asyncio
async def test_machine_passes_registry_exclusions_to_the_selector() -> None:
    reg = _stored(["stepfun", "kimi"])
    role_types.set_registry_provider(lambda: reg)
    selector = _Selector()
    snapshot: dict = {}
    machine = _machine(selector, snapshot)
    line = Line(id="l", name="l", task="t")
    dispatch = Dispatch(id="d", goal="g", user_id="u", created_at="now", lines=[line])
    chosen = await machine._select_with_retry(dispatch, line)
    # Fails on old code: the roster order put kimi first and every roster
    # name was allowed. Now the owner order leads and non-candidates are out.
    assert chosen == "stepfun"
    assert "doubao" in selector.calls[0]
    assert selector.owner_preference == ("stepfun", "kimi")
    assert snapshot["registry_assignment"][-1]["source"] == SOURCE_STORE


@pytest.mark.asyncio
async def test_machine_with_fallback_registry_records_nothing() -> None:
    selector = _Selector()
    snapshot: dict = {}
    machine = _machine(selector, snapshot)
    line = Line(id="l", name="l", task="t")
    dispatch = Dispatch(id="d", goal="g", user_id="u", created_at="now", lines=[line])
    assert await machine._select_with_retry(dispatch, line) == eligible_models("work")[0]
    assert selector.calls == [()] and snapshot == {}
