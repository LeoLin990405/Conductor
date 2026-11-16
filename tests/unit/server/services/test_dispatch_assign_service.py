"""N73 L5: the live assigner gathers feasibility facts and follows the rollout."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.server.services.autonomy.roles import RolePolicy
from src.server.services.dispatch import assignment_view
from src.server.services.dispatch_assign import LiveAssigner, build_assigner
from src.tools.auto_dispatch.outcomes import InMemoryMemoryPort
from src.tools.auto_dispatch.superagent_assign.record import append_assignment
from src.tools.auto_dispatch.superagent_assign.rollout import InMemoryRolloutStore
from src.tools.auto_dispatch.superagent_assign.types import AssignmentPlan, LineChoice
from src.tools.auto_dispatch.types import Dispatch, Line

pytestmark = pytest.mark.asyncio


class _Headroom:
    async def remaining(self, model):
        return 0.0 if model == "grok" else 0.8

    async def plan_status(self, model):
        return {"plan_full": model == "grok", "credits": 0}


class _Store:
    async def list(self, limit=500):
        return []


class _LLM:
    def __init__(self, plan):
        self.plan = plan
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.plan


def _assigner(llm):
    return LiveAssigner(
        llm,
        policy=RolePolicy(developer_pool=("kimi", "stepfun")),
        headroom=_Headroom(),
        store=_Store(),
        outcomes_port=InMemoryMemoryPort(),
        which=lambda binary: f"/bin/{binary}" if binary != "grok" else None,
        clock=lambda: 1000.0,
        rollout_store=InMemoryRolloutStore(),
    )


def _dispatch():
    line = Line(id="l1", name="l1", task="Change docs.", write_scope=("docs/a.md",))
    return Dispatch(id="d1", goal="g", user_id="u", created_at="now", lines=[line]), line


async def test_snapshot_is_the_advisory_roster_and_live_facts(monkeypatch) -> None:
    monkeypatch.delenv("CONDUCTOR_DISPATCH_ASSIGN_MODE", raising=False)
    assigner = _assigner(_LLM(AssignmentPlan()))
    await assigner._prefetch()
    dispatch, line = _dispatch()
    snap = assigner._snapshot(dispatch, line)
    # S2: past the developer pool, never a reserved role (Q1).
    assert "glm" in snap.eligible_models and "codex" not in snap.eligible_models
    assert "grok" not in snap.cli_found
    assert snap.headroom["grok"] == 0.0 and snap.plan_full["grok"] is True
    assert snap.paid_credits_allowed is True  # S1 default on
    hints = dict(assigner._hints(dispatch))
    assert hints["preferred"].startswith("kimi, stepfun")
    assert "runtimes glm" in hints


async def test_decide_uses_the_router_model_and_shadow_by_rollout(monkeypatch) -> None:
    monkeypatch.delenv("CONDUCTOR_DISPATCH_ASSIGN_MODE", raising=False)
    plan = AssignmentPlan(
        choices=(
            LineChoice(line_id="l1", model="stepfun", effort="high", runtime="default",
                       reason="docs", confidence="high"),
        )
    )
    llm = _LLM(plan)
    assigner = _assigner(llm)
    dispatch, line = _dispatch()
    decisions = await assigner.decide(dispatch, [line])
    assert decisions["l1"].source == "superagent" and decisions["l1"].mode == "shadow"
    assert llm.calls[0]["slot"] == "background"
    assert llm.calls[0]["response_schema"] is AssignmentPlan
    # Unset ASSIGN_MODE is shadow (L1 default), not the staged rollout.
    assert (assigner.rollout.stage, assigner.rollout.mode) == ("override", "shadow")


async def test_operator_off_wires_nothing(monkeypatch) -> None:
    monkeypatch.setenv("CONDUCTOR_DISPATCH_ASSIGN_MODE", "off")
    assert build_assigner(llm=None, policy=RolePolicy(), headroom=_Headroom(),
                          store=_Store(), outcomes_port=None) is None
    monkeypatch.delenv("CONDUCTOR_DISPATCH_ASSIGN_MODE")
    assert build_assigner(llm=None, policy=RolePolicy(), headroom=_Headroom(),
                          store=_Store(), outcomes_port=None) is not None


def test_summary_exposes_the_latest_assignment() -> None:
    gates = append_assignment(
        {"lines": {"l1": {}}}, line_id="l1", run_id="l1:assign:0", mode="shadow",
        source="superagent",
        choice={"line_id": "l1", "model": "stepfun", "effort": "high", "runtime": "default",
                "reason": "docs", "confidence": "high", "model_id": ""},
        selector_choice={"model": "kimi"}, feasibility=[{"status": "ok"}],
    )
    dispatch = SimpleNamespace(gates=gates)
    view = assignment_view(dispatch)
    assert view["l1"]["model"] == "stepfun" and view["l1"]["selector_choice"] == "kimi"
    assert view["l1"]["entries"] == 1
    assert assignment_view(SimpleNamespace(gates={})) == {}


async def test_the_shadow_call_can_be_switched_off(monkeypatch) -> None:
    monkeypatch.delenv("CONDUCTOR_DISPATCH_ASSIGN_MODE", raising=False)
    monkeypatch.setenv("CONDUCTOR_DISPATCH_ASSIGN_SHADOW_CALL", "0")
    llm = _LLM(AssignmentPlan())
    dispatch, line = _dispatch()
    assert await _assigner(llm).decide(dispatch, [line]) == {}
    assert llm.calls == []
