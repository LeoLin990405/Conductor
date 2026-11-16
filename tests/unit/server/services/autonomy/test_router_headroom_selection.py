"""Router worker selection consults the same HeadroomPort as auto-dispatch."""

from __future__ import annotations

import pytest

from src.server.services.autonomy.role_plan import (
    TaskSpec,
    plan_developer_assignments,
    plan_developer_assignments_with_headroom,
    plan_supervisor_mission_with_headroom,
    read_headroom,
    replan_after_failure,
    replan_after_failure_with_headroom,
)
from src.server.services.autonomy.roles import AgentState, RolePolicy
from src.tools.auto_dispatch.pool import MIN_HEADROOM
from src.tools.auto_dispatch.selection import (
    HeadroomPort,
    UnlimitedHeadroom,
    UsageSnapshotHeadroom,
    assign_dag_with_headroom,
)
from src.tools.auto_dispatch.types import Line


class FakeHeadroom:
    """Read-only HeadroomPort stand-in; missing names look fully open."""

    def __init__(self, remaining: dict[str, float]) -> None:
        self.remaining_map = dict(remaining)
        self.calls: list[str] = []

    async def remaining(self, model: str) -> float:
        self.calls.append(model)
        return float(self.remaining_map.get(model, 1.0))


def _agent(provider: str, **kwargs) -> AgentState:
    return AgentState(provider=provider, **kwargs)


def test_fake_headroom_matches_the_auto_dispatch_port() -> None:
    # HeadroomPort is a structural Protocol (not runtime_checkable); the fake
    # must expose the same async remaining(model) -> float surface.
    assert callable(getattr(FakeHeadroom({}), "remaining", None))
    assert callable(getattr(UnlimitedHeadroom(), "remaining", None))
    assert HeadroomPort.__name__ == "HeadroomPort"


def test_low_headroom_ranks_below_high_headroom() -> None:
    policy = RolePolicy(developer_pool=("kimi", "glm"))
    agents = [
        _agent("kimi", success_rate=0.9, headroom=0.2),
        _agent("glm", success_rate=0.9, headroom=0.9),
    ]

    chosen = policy.choose_developer(agents)

    assert chosen is not None
    assert chosen.provider == "glm"


def test_healthy_provider_beats_low_quota_despite_slightly_lower_success() -> None:
    """Headroom tier demotes low quota beyond equal-success-rate ties."""
    policy = RolePolicy(developer_pool=("kimi", "glm"))
    agents = [
        _agent("kimi", success_rate=1.0, headroom=0.25),
        _agent("glm", success_rate=0.99, headroom=1.0),
    ]

    chosen = policy.choose_developer(agents)

    assert chosen is not None
    assert chosen.provider == "glm"


def test_exhausted_provider_is_never_chosen_and_refusal_is_recorded() -> None:
    policy = RolePolicy(developer_pool=("kimi", "glm"))
    agents = [_agent("kimi"), _agent("glm")]

    chosen = policy.choose_developer(
        agents,
        headroom={"kimi": MIN_HEADROOM, "glm": 0.0},
    )

    assert chosen is None
    assert policy.assignment_refusal == "quota_exhausted"


def test_exhausted_provider_is_skipped_for_a_peer_with_headroom() -> None:
    policy = RolePolicy(developer_pool=("kimi", "glm", "grok"))
    agents = [_agent("kimi", success_rate=1.0), _agent("glm"), _agent("grok")]

    chosen = policy.choose_developer(
        agents,
        headroom={"kimi": 0.0, "glm": 0.4, "grok": 0.1},
    )

    assert chosen is not None
    assert chosen.provider == "glm"
    assert policy.assignment_refusal == "quota_exhausted"


def test_large_estimated_task_goes_to_the_provider_with_most_headroom() -> None:
    policy = RolePolicy(developer_pool=("kimi", "cursor", "grok"))
    # kimi is earlier in the roster and has a higher success rate; a large
    # estimate still prefers the widest remaining quota.
    agents = [
        _agent("kimi", success_rate=1.0, headroom=0.3),
        _agent("cursor", success_rate=0.5, headroom=0.95),
        _agent("grok", success_rate=0.8, headroom=0.6),
    ]

    chosen = policy.choose_developer(agents, estimated_size="large")

    assert chosen is not None
    assert chosen.provider == "cursor"


def test_same_tier_still_prefers_higher_success_rate() -> None:
    policy = RolePolicy(developer_pool=("kimi", "cursor"))
    agents = [
        _agent("kimi", success_rate=1.0, headroom=0.6),
        _agent("cursor", success_rate=0.5, headroom=0.7),
    ]

    chosen = policy.choose_developer(agents, estimated_size="medium")

    assert chosen is not None
    assert chosen.provider == "kimi"


@pytest.mark.asyncio
async def test_role_plan_reads_a_fake_headroom_port_and_skips_exhausted() -> None:
    policy = RolePolicy(developer_pool=("kimi", "glm"))
    agents = [_agent("kimi", success_rate=1.0), _agent("glm", success_rate=0.5)]
    port = FakeHeadroom({"kimi": 0.0, "glm": 0.8})

    plan = await plan_developer_assignments_with_headroom(
        [TaskSpec(task_id="line-0")],
        agents,
        port,
        policy=policy,
    )

    assert plan.provider_of("line-0") == "glm"
    assert set(port.calls) == {"kimi", "glm"}


@pytest.mark.asyncio
async def test_role_plan_records_quota_exhausted_when_the_pool_is_dry() -> None:
    policy = RolePolicy(developer_pool=("kimi", "glm"))
    agents = [_agent("kimi"), _agent("glm")]
    port = FakeHeadroom({"kimi": 0.0, "glm": 0.0})
    readings = await read_headroom(("kimi", "glm"), port)

    plan = plan_developer_assignments(
        [TaskSpec(task_id="line-0")],
        agents,
        policy=policy,
        headroom=readings,
    )

    assert plan.assignments == ()
    assert plan.unassigned[0].reason == "quota_exhausted"
    assert policy.assignment_refusal == "quota_exhausted"


@pytest.mark.asyncio
async def test_role_plan_sends_a_large_task_to_most_headroom() -> None:
    policy = RolePolicy(developer_pool=("kimi", "grok", "cursor"))
    agents = [
        _agent("kimi", success_rate=1.0),
        _agent("grok", success_rate=0.9),
        _agent("cursor", success_rate=0.4),
    ]
    port = FakeHeadroom({"kimi": 0.25, "grok": 0.4, "cursor": 0.9})

    plan = await plan_developer_assignments_with_headroom(
        [TaskSpec(task_id="heavy", estimated_size="large")],
        agents,
        port,
        policy=policy,
    )

    assert plan.provider_of("heavy") == "cursor"


@pytest.mark.asyncio
async def test_unlimited_headroom_port_leaves_roster_order_intact() -> None:
    policy = RolePolicy(developer_pool=("kimi", "glm"))
    agents = [_agent("kimi", success_rate=0.5), _agent("glm", success_rate=0.5)]

    plan = await plan_developer_assignments_with_headroom(
        [TaskSpec(task_id="line-0")],
        agents,
        UnlimitedHeadroom(),
        policy=policy,
    )

    assert plan.provider_of("line-0") == "kimi"


@pytest.mark.asyncio
async def test_supervisor_mission_entry_point_skips_exhausted_providers() -> None:
    """Regression through plan_supervisor_mission, not only the test helper."""
    policy = RolePolicy(developer_pool=("kimi", "glm"))
    agents = [
        _agent("claude"),
        _agent("kimi", success_rate=1.0),
        _agent("glm", success_rate=0.5),
    ]
    port = FakeHeadroom({"kimi": 0.0, "glm": 0.8, "claude": 1.0})

    plan = await plan_supervisor_mission_with_headroom(
        [TaskSpec(task_id="line-0")],
        agents,
        port,
        policy=policy,
    )

    assert plan.planner == "claude"
    assert plan.assignments.provider_of("line-0") == "glm"
    assert "kimi" in port.calls
    assert "glm" in port.calls


@pytest.mark.asyncio
async def test_replan_after_failure_does_not_hand_work_to_exhausted_peer() -> None:
    """Retry must keep HeadroomPort readings; otherwise exhausted kimi is picked."""
    policy = RolePolicy(developer_pool=("kimi", "glm"))
    agents = [
        _agent("kimi", success_rate=1.0, capacity=1),
        _agent("glm", success_rate=0.8, capacity=1),
    ]
    port = FakeHeadroom({"kimi": 0.0, "glm": 0.8})
    readings = await read_headroom(("kimi", "glm"), port)

    original = plan_developer_assignments(
        [TaskSpec(task_id="line-0")],
        agents,
        policy=policy,
        headroom=readings,
    )
    assert original.provider_of("line-0") == "glm"

    # Without headroom on retry, kimi would look fully open and win.
    blind = replan_after_failure(
        original,
        [TaskSpec(task_id="line-0")],
        agents,
        failed_task_id="line-0",
        failed_provider="glm",
        policy=policy,
    )
    assert blind.provider_of("line-0") == "kimi"

    replanned = await replan_after_failure_with_headroom(
        original,
        [TaskSpec(task_id="line-0")],
        agents,
        port,
        failed_task_id="line-0",
        failed_provider="glm",
        policy=policy,
    )
    assert replanned.provider_of("line-0") == ""
    assert replanned.unassigned[0].reason == "quota_exhausted"


@pytest.mark.asyncio
async def test_assign_dag_live_path_skips_exhausted_and_ranks_large_by_headroom() -> None:
    """Production assign_dag (planner-assigned workers) must see HeadroomPort."""
    policy = RolePolicy(developer_pool=("kimi", "glm", "cursor"))
    agents = [
        _agent("kimi", success_rate=1.0),
        _agent("glm", success_rate=0.9),
        _agent("cursor", success_rate=0.4),
    ]
    port = FakeHeadroom({"kimi": 0.0, "glm": 0.3, "cursor": 0.95})
    lines = [
        Line(
            id="line-0",
            name="heavy",
            task="rewrite the module",
            write_scope=("src/heavy.py",),
            risk_bearing=True,
        )
    ]

    plan = await assign_dag_with_headroom(lines, agents, port, policy=policy)

    assert plan.provider_of("line-0") == "cursor"
    assert "kimi" in port.calls


@pytest.mark.asyncio
async def test_zcode_alias_reads_glm_headroom_and_refuses_exhaustion() -> None:
    """Canonicalize like the selector: zcode must see glm's exhausted state."""
    policy = RolePolicy(developer_pool=("zcode", "kimi"))
    agents = [_agent("zcode", success_rate=1.0), _agent("kimi", success_rate=0.5)]
    # The snapshot is keyed by the dispatch name; zcode is the same CLI, so it
    # reads glm's exhausted state instead of looking open.
    port = UsageSnapshotHeadroom({"glm": "exhausted", "kimi": "available"})
    assert await port.remaining("glm") == 0.0
    assert await port.remaining("zcode") == 0.0

    readings = await read_headroom(("zcode", "kimi"), port)
    chosen = policy.choose_developer(agents, headroom=readings)

    assert chosen is not None
    assert chosen.provider == "kimi"
    assert policy.assignment_refusal == "quota_exhausted"

    plan = await assign_dag_with_headroom(
        [Line(id="line-0", name="fix", task="fix it", write_scope=("src/a.py",))],
        agents,
        port,
        policy=policy,
    )
    assert plan.provider_of("line-0") == "kimi"
