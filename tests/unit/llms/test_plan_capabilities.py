"""Token/Agent plan and API-region product metadata."""

from src.llms import plan_capabilities as pc


def test_named_plan_kinds_are_not_the_generic_wire_access_type():
    assert pc.plan_metadata("moonshot-coding") == {"kind": "agent", "name": "Kimi Code"}
    assert pc.plan_metadata("mimo") == {"kind": "token", "name": "Token Plan"}
    assert pc.plan_metadata("doubao-coding") == {"kind": "agent", "name": "Coding Plan"}
    assert pc.plan_metadata("doubao-plan") == {"kind": "agent", "name": "Agent Plan"}
    # An API-key variant has no plan metadata.
    assert pc.plan_metadata("deepseek") is None


def test_families_carry_token_agent_and_region_capabilities():
    deepseek = pc.family_capabilities("deepseek")
    assert deepseek == {
        "token_plan": False,
        "agent_plan": False,
        "api_regions": ["global"],
    }

    qwen = pc.family_capabilities("dashscope")
    assert qwen is not None
    assert qwen["token_plan"] is True
    assert qwen["agent_plan"] is False
    assert qwen["api_regions"] == ["cn", "intl"]

    kimi = pc.family_capabilities("kimi-oauth")
    assert kimi is not None
    assert kimi["agent_plan"] is True
    assert kimi["api_regions"] == ["cn", "intl"]

    mimo = pc.family_capabilities("mimo-oauth")
    assert mimo is not None
    assert mimo["token_plan"] is True
    assert mimo["agent_plan"] is False
