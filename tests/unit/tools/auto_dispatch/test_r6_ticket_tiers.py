"""R6 L4: fast and deep tiers, deep downgrades on low quota."""

from __future__ import annotations

from src.tools.auto_dispatch.ticket_tiers import DEEP, FAST, choose_tier


def test_fast_stays_fast() -> None:
    plan = choose_tier("fast", headroom={"kimi": 0.0})
    assert plan.tier == FAST and not plan.downgraded and plan.single_worker


def test_deep_with_quota_stays_deep() -> None:
    plan = choose_tier("deep", headroom={"kimi": 0.9, "glm": 0.6}, providers=["kimi", "glm"])
    assert plan.tier == DEEP and plan.planner_split and plan.secondary_review


def test_deep_downgrades_on_low_headroom_with_reason() -> None:
    plan = choose_tier("deep", headroom={"kimi": 0.9, "glm": 0.1}, providers=["kimi", "glm"])
    assert plan.tier == FAST and plan.downgraded
    assert plan.reason.startswith("low_headroom:glm=0.10")


def test_deep_downgrades_on_low_usage_tier() -> None:
    plan = choose_tier("deep", usage_tier="LOW")
    assert plan.tier == FAST and plan.reason == "usage_tier_low"


def test_threshold_is_configurable_and_unknown_tier_is_fast() -> None:
    env = {"CONDUCTOR_TICKET_DEEP_MIN_HEADROOM": "0.05"}
    assert choose_tier("deep", headroom={"glm": 0.1}, env=env).tier == DEEP
    assert choose_tier("whatever").tier == FAST
    assert choose_tier("deep").to_dict()["tier"] == DEEP
