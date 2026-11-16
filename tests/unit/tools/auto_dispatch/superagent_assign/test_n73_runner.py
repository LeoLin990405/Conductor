"""N73 L4: ask, check, re-ask once, fall back; ERROR is never a choice."""

from __future__ import annotations

import pytest

from src.tools.auto_dispatch.superagent_assign import runner
from src.tools.auto_dispatch.superagent_assign.context import AssignmentInputs
from src.tools.auto_dispatch.superagent_assign.feasibility import Snapshot
from src.tools.auto_dispatch.superagent_assign.launch_env import (
    CLI_EFFORT_ENV,
    MODEL_ID_ENV,
    RUNTIME_ENV,
    env_for_choice,
)
from src.tools.auto_dispatch.superagent_assign.types import (
    AssignmentPlan,
    LineChoice,
    call_error,
)
from src.tools.auto_dispatch.types import Line

pytestmark = pytest.mark.asyncio

ROSTER = ("kimi", "glm", "stepfun", "grok")


def _snap(**overrides) -> Snapshot:
    base = dict(
        eligible_models=ROSTER,
        can_take_line=ROSTER,
        cli_found=ROSTER,
        headroom={name: 0.9 for name in ROSTER},
    )
    base.update(overrides)
    return Snapshot(**base)


def _choice(line_id: str, model: str, effort: str = "default", runtime: str = "default"):
    return LineChoice(
        line_id=line_id, model=model, effort=effort, runtime=runtime,
        reason="stepfun 9/10 on web", confidence="medium",
    )


class _Backend:
    def __init__(self, *plans):
        self.plans = list(plans)
        self.calls: list[AssignmentInputs] = []

    async def assign(self, inputs):
        self.calls.append(inputs)
        return self.plans.pop(0)


async def _inputs(lines):
    return AssignmentInputs(lines=(), success_by_provider=(), success_rate_by_area=())


LINES = [Line(id="l1", name="l1", task="t"), Line(id="l2", name="l2", task="t")]


async def test_feasible_choices_are_kept_with_one_call() -> None:
    backend = _Backend(AssignmentPlan(choices=(_choice("l1", "stepfun"), _choice("l2", "kimi"))))
    out = await runner.decide(
        LINES, mode="shadow", backend=backend, snapshot_for=lambda line: _snap(), inputs_for=_inputs
    )
    assert [out[i].source for i in ("l1", "l2")] == ["superagent", "superagent"]
    assert out["l1"].choice.model == "stepfun" and len(backend.calls) == 1


async def test_infeasible_is_reasked_once_with_its_reason_code() -> None:
    backend = _Backend(
        AssignmentPlan(choices=(_choice("l1", "codex"), _choice("l2", "kimi"))),
        AssignmentPlan(choices=(_choice("l1", "glm"),)),
    )
    out = await runner.decide(
        LINES, mode="enforce", backend=backend, snapshot_for=lambda line: _snap(),
        inputs_for=_inputs, hints=(("preferred", "kimi"),),
    )
    assert out["l1"].source == "superagent_reask" and out["l1"].reask
    assert out["l1"].choice.model == "glm"
    assert [a.get("reason_code", "") for a in out["l1"].attempts] == ["reserved_role", ""]
    assert backend.calls[1].infeasible == (("l1", "reserved_role"),)
    assert backend.calls[1].hints == (("preferred", "kimi"),)
    assert out["l2"].source == "superagent" and not out["l2"].reask


async def test_infeasible_twice_falls_back_to_the_selector() -> None:
    backend = _Backend(
        AssignmentPlan(choices=(_choice("l1", "claude"), _choice("l2", "kimi"))),
        AssignmentPlan(choices=(_choice("l1", "codex"),)),
    )
    out = await runner.decide(
        LINES, mode="enforce", backend=backend, snapshot_for=lambda line: _snap(), inputs_for=_inputs
    )
    assert out["l1"].source == "selector_fallback"
    assert out["l1"].fallback_reason == runner.FALLBACK_INFEASIBLE and out["l1"].choice is None


async def test_error_falls_back_without_a_reask() -> None:
    backend = _Backend(call_error("RuntimeError"))
    out = await runner.decide(
        LINES, mode="shadow", backend=backend, snapshot_for=lambda line: _snap(), inputs_for=_inputs
    )
    assert {d.source for d in out.values()} == {"selector_fallback"}
    assert out["l1"].fallback_reason == "error:call_error" and len(backend.calls) == 1


async def test_off_never_calls_the_backend() -> None:
    backend = _Backend()
    assert await runner.decide(
        LINES, mode="off", backend=backend, snapshot_for=lambda line: _snap(), inputs_for=_inputs
    ) == {}


def test_adoption_follows_mode_and_sample() -> None:
    decision = runner.LineDecision("l1", "enforce", "superagent", choice=_choice("l1", "kimi"))
    assert decision.adopted(1.0) and not decision.adopted(0.0)
    shadow = runner.LineDecision("l1", "shadow", "superagent", choice=_choice("l1", "kimi"))
    assert not shadow.adopted(1.0)


def test_launch_env_maps_effort_per_provider_and_default_is_empty() -> None:
    assert env_for_choice(_choice("l1", "kimi")) == {}
    assert env_for_choice(_choice("l1", "kimi", effort="high")) == {}  # n/a provider
    stepfun = env_for_choice(_choice("l1", "stepfun", effort="high"))
    assert stepfun == {CLI_EFFORT_ENV: "high"}
    glm = env_for_choice(_choice("l1", "glm", runtime="clean-room"))
    assert glm == {RUNTIME_ENV: "clean-room"}
    assert MODEL_ID_ENV not in glm
