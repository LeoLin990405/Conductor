"""Settings rows must expose plans, not just a single API-key action."""

from src.server.services.provider_connect_options import options_for


def _by_provider(brand: str):
    return {option["provider"]: option for option in options_for(brand)}


def test_minimax_has_api_regions_and_m_plan():
    options = _by_provider("minimax")
    assert set(options) == {"minimax", "minimax-intl", "minimax-coding"}
    assert options["minimax"]["access_type"] == "api_key"
    assert options["minimax"]["base_url"] == "https://api.minimax.cn/anthropic"
    assert options["minimax-intl"]["base_url"] == "https://api.minimax.io/anthropic"
    assert options["minimax-coding"]["plan_name"] == "M Plan"
    assert options["minimax-coding"]["access_type"] == "coding_plan"


def test_qwen_has_a_token_plan_route():
    options = _by_provider("dashscope")
    assert options["dashscope-token-plan"]["plan_kind"] == "token"
    assert options["dashscope-token-plan"]["base_url"].startswith("https://token-plan.")


def test_doubao_has_both_agent_plans():
    options = _by_provider("volcengine")
    assert {o["plan_name"] for o in options.values() if o["access_type"] == "coding_plan"} == {
        "Coding Plan",
        "Agent Plan",
    }


def test_unknown_family_has_no_options():
    assert options_for("not-a-provider") == []
