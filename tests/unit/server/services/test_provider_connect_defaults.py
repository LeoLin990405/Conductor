"""Settings' API-key editor gets the manifest endpoint, not an empty URL."""

from src.server.services.provider_connect_defaults import connect_defaults


def test_minimax_prefills_domestic_and_international_endpoints():
    cn = connect_defaults("minimax")
    assert cn is not None
    assert cn["base_url"] == "https://api.minimax.cn/anthropic"
    assert cn["sdk"] == "anthropic"
    assert cn["region"] == "cn"
    assert cn["region_variants"] == [
        {
            "provider": "minimax-intl",
            "display_name": "MiniMax",
            "region": "intl",
            "sdk": "anthropic",
            "base_url": "https://api.minimax.io/anthropic",
            "use_response_api": False,
        }
    ]


def test_coding_plan_does_not_offer_the_other_region_as_a_key_endpoint():
    defaults = connect_defaults("minimax-coding")
    assert defaults is not None
    assert defaults["base_url"] == "https://api.minimax.cn/anthropic"
    assert defaults["region_variants"] == []


def test_unknown_provider_has_no_defaults():
    assert connect_defaults("not-a-provider") is None
    assert connect_defaults(None) is None


def test_sdk_default_endpoints_are_visible_in_the_editor():
    assert connect_defaults("anthropic")["base_url"] == "https://api.anthropic.com"
    assert connect_defaults("gemini")["base_url"] == "https://generativelanguage.googleapis.com"


def test_domestic_and_international_variants_cover_kimi_and_stepfun():
    kimi = connect_defaults("moonshot")
    assert kimi is not None
    assert [v["base_url"] for v in kimi["region_variants"]] == [
        "https://api.moonshot.cn/anthropic"
    ]
    stepfun = connect_defaults("stepfun")
    assert stepfun is not None
    assert stepfun["region_variants"][0]["base_url"] == "https://api.stepfun.ai/v1"
    plan = connect_defaults("stepfun-plan")
    assert plan is not None
    assert plan["region_variants"][0]["base_url"] == "https://api.stepfun.ai/step_plan/v1"
