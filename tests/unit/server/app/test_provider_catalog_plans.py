"""The provider catalog exposes Token/Agent plans and endpoint regions."""

from src.server.app.api_keys import _build_provider_catalog


def _by_provider() -> dict[str, dict]:
    _build_provider_catalog.cache_clear()
    return {entry["provider"]: entry for entry in _build_provider_catalog()}


def test_families_carry_plan_and_region_metadata():
    entries = _by_provider()

    # API-only, one global endpoint: no plan and no domestic/overseas split.
    deepseek = entries["deepseek"]
    assert deepseek["plan_capabilities"] == {"token_plan": False, "agent_plan": False}
    assert deepseek["api_regions"] == ["global"]

    # Token Plan capability with no selectable variant in this build (Qwen).
    qwen = entries["dashscope"]
    assert qwen["plan_capabilities"] == {"token_plan": True, "agent_plan": False}
    assert qwen["api_regions"] == ["cn", "intl"]

    # Agent plans and domestic/overseas API endpoints.
    zhipu = entries["z-ai"]
    assert zhipu["plan_capabilities"] == {"token_plan": False, "agent_plan": True}
    assert zhipu["api_regions"] == ["cn", "intl"]


def test_plan_variants_carry_their_product_name_and_kind():
    entries = _by_provider()

    assert entries["moonshot-coding"]["plan_kind"] == "agent"
    assert entries["moonshot-coding"]["plan_name"] == "Kimi Code"
    assert entries["mimo"]["plan_kind"] == "token"
    assert entries["mimo"]["plan_name"] == "Token Plan"
    assert entries["doubao-coding"]["plan_kind"] == "agent"
    assert entries["doubao-coding"]["plan_name"] == "Coding Plan"
    assert entries["doubao-plan"]["plan_name"] == "Agent Plan"
