"""Provider connection catalog: the descriptor contract the UI renders.

The catalog is a static module, so these tests pin the wire contract (ids,
labels, kinds, API-key fallbacks) and its consistency with the manifest models
each login unlocks. The endpoint test calls the route directly, matching the
Kimi/Zhipu route-test pattern (no TestClient, no network).
"""

from __future__ import annotations

import json

import pytest

from src.server.services import oauth_catalog
from src.server.services.oauth_catalog import (
    KIND_API_KEY_ONLY,
    KIND_DEVICE,
    KIND_PASTE,
    KIND_REDIRECT,
    PROVIDER_DESCRIPTORS,
    REGION_CN,
    REGION_INTL,
    get_provider_descriptor,
)

DESCRIPTOR_KEYS = {
    "id",
    "display_name",
    "display_name_en",
    "brand",
    "region",
    "kind",
    "login_supported",
    "unavailable_reason",
    "unavailable_reason_en",
    "usage_hint",
    "usage_hint_en",
    "usage_policy_url",
    "api_key_provider",
    "website_url",
    "models",
    "order",
}

LOGIN_IDS = {
    "codex-oauth",
    "claude-oauth",
    "grok-oauth",
    "gemini-oauth",
    "kimi-oauth",
    "deepseek-account",
    "z-ai-cn-oauth",
    "z-ai-oauth",
    "mimo-oauth",
}

API_KEY_ONLY_IDS = {"dashscope", "minimax", "stepfun", "doubao-plan", "deepseek"}

# id -> (display_name, display_name_en, brand, region, kind, api_key_provider,
#        website_url)
EXPECTED = {
    "codex-oauth": (
        "ChatGPT Codex",
        "ChatGPT Codex",
        "openai",
        REGION_INTL,
        KIND_DEVICE,
        "openai",
        "https://platform.openai.com/api-keys",
    ),
    "claude-oauth": (
        "Claude 订阅登录",
        "Claude subscription",
        "anthropic",
        REGION_INTL,
        KIND_PASTE,
        "anthropic",
        "https://console.anthropic.com/settings/keys",
    ),
    "grok-oauth": (
        "Grok 订阅登录",
        "Grok subscription",
        "xai",
        REGION_INTL,
        KIND_DEVICE,
        "xai",
        "https://console.x.ai",
    ),
    "gemini-oauth": (
        "Antigravity 登录",
        "Antigravity",
        "gemini",
        REGION_INTL,
        KIND_PASTE,
        "gemini",
        "https://antigravity.google",
    ),
    "kimi-oauth": (
        "Kimi 订阅登录",
        "Kimi subscription",
        "moonshot",
        REGION_CN,
        KIND_DEVICE,
        "moonshot-coding",
        "https://www.kimi.com/code/",
    ),
    "deepseek-account": (
        "DeepSeek 账号登录",
        "DeepSeek account",
        "deepseek",
        REGION_CN,
        KIND_REDIRECT,
        "deepseek",
        "https://platform.deepseek.com",
    ),
    "z-ai-cn-oauth": (
        "智谱订阅登录",
        "Zhipu subscription",
        "z-ai-cn",
        REGION_CN,
        KIND_REDIRECT,
        "z-ai-cn-coding",
        "https://bigmodel.cn",
    ),
    "z-ai-oauth": (
        "Z.ai 订阅登录",
        "Z.ai subscription",
        "z-ai",
        REGION_INTL,
        KIND_REDIRECT,
        "z-ai-coding",
        "https://z.ai",
    ),
    "mimo-oauth": (
        "MiMo 订阅登录",
        "MiMo subscription",
        "mimo",
        REGION_CN,
        KIND_PASTE,
        "mimo",
        "https://platform.xiaomimimo.com/token-plan",
    ),
    "dashscope": (
        "通义千问 / 百炼",
        "Qwen (DashScope)",
        "dashscope",
        REGION_CN,
        KIND_API_KEY_ONLY,
        "dashscope",
        "https://bailian.console.aliyun.com/",
    ),
    "minimax": (
        "MiniMax",
        "MiniMax",
        "minimax",
        REGION_CN,
        KIND_API_KEY_ONLY,
        "minimax",
        "https://platform.minimax.cn/",
    ),
    "stepfun": (
        "阶跃星辰 StepFun",
        "StepFun",
        "stepfun",
        REGION_CN,
        KIND_API_KEY_ONLY,
        "stepfun",
        "https://platform.stepfun.com/",
    ),
    "doubao-plan": (
        "豆包 / 火山方舟 ARK",
        "Doubao / Volcengine Ark",
        "volcengine",
        REGION_CN,
        KIND_API_KEY_ONLY,
        "doubao-plan",
        "https://console.volcengine.com/ark",
    ),
    "deepseek": (
        "DeepSeek",
        "DeepSeek",
        "deepseek",
        REGION_CN,
        KIND_API_KEY_ONLY,
        "deepseek",
        "https://platform.deepseek.com/",
    ),
}

EXPECTED_MODELS = {
    "kimi-oauth": [
        "kimi-for-coding-oauth",
        "kimi-k3-oauth",
        "kimi-k3.1-pro-highspeed-oauth",
        "kimi-k3.1-pro-oauth",
    ],
    "deepseek-account": ["deepseek-flash-account", "deepseek-v4-pro-account"],
    "z-ai-cn-oauth": ["glm-5.3-cn-oauth", "glm-5.3-flash-cn-oauth"],
    "z-ai-oauth": ["glm-5.3-oauth"],
    "mimo-oauth": ["mimo-v2.6-flash-oauth", "mimo-v2.6-pro-oauth"],
}


# ─── Contract ───────────────────────────────────────────────────────────────


def test_catalog_lists_every_provider_once_in_ui_order():
    ids = [descriptor.id for descriptor in PROVIDER_DESCRIPTORS]

    assert len(ids) == len(set(ids)) == len(EXPECTED) == 14
    assert set(ids) == LOGIN_IDS | API_KEY_ONLY_IDS
    assert ids == list(EXPECTED)
    orders = [descriptor.order for descriptor in PROVIDER_DESCRIPTORS]
    assert orders == sorted(orders)
    # Grouping: intl logins, cn logins, cn api-key-only.
    assert {descriptor.order for descriptor in PROVIDER_DESCRIPTORS} == {10, 20, 30}


@pytest.mark.parametrize("provider_id", list(EXPECTED))
def test_descriptor_matches_the_ui_contract(provider_id):
    descriptor = get_provider_descriptor(provider_id)
    assert descriptor is not None
    (
        display_name,
        display_name_en,
        brand,
        region,
        kind,
        api_key_provider,
        website_url,
    ) = EXPECTED[provider_id]

    assert descriptor.display_name == display_name
    assert descriptor.display_name_en == display_name_en
    assert descriptor.brand == brand
    assert descriptor.region == region
    assert descriptor.kind == kind
    assert descriptor.api_key_provider == api_key_provider
    assert descriptor.website_url == website_url
    assert descriptor.login_supported is (kind != KIND_API_KEY_ONLY)
    assert list(descriptor.models) == EXPECTED_MODELS.get(provider_id, [])


def test_every_descriptor_is_json_safe_with_exactly_the_contract_keys():
    for descriptor in oauth_catalog.describe_providers():
        assert set(descriptor) == DESCRIPTOR_KEYS
        assert isinstance(descriptor["models"], list)
        assert all(isinstance(model, str) and model for model in descriptor["models"])
        # A round trip proves nothing in the payload is non-JSON (dataclass,
        # tuple, enum) and therefore that FastAPI can serialize the response.
        assert json.loads(json.dumps(descriptor)) == descriptor


def test_login_entries_are_connectable_and_point_at_an_api_key_fallback():
    for descriptor in PROVIDER_DESCRIPTORS:
        if descriptor.id not in LOGIN_IDS:
            continue
        assert descriptor.login_supported is True
        assert descriptor.kind in (KIND_DEVICE, KIND_PASTE, KIND_REDIRECT)
        assert descriptor.api_key_provider
        assert descriptor.website_url and descriptor.website_url.startswith("https://")
        assert descriptor.unavailable_reason is None
        assert descriptor.unavailable_reason_en is None


def test_api_key_only_entries_explain_why_there_is_no_login():
    for descriptor in PROVIDER_DESCRIPTORS:
        if descriptor.id not in API_KEY_ONLY_IDS:
            continue
        assert descriptor.login_supported is False
        assert descriptor.kind == KIND_API_KEY_ONLY
        assert descriptor.models == ()
        assert descriptor.unavailable_reason
        assert descriptor.unavailable_reason_en
        assert descriptor.website_url and descriptor.website_url.startswith("https://")
        assert descriptor.api_key_provider


def test_qwen_is_listed_api_key_only_because_its_oauth_free_tier_ended():
    descriptor = get_provider_descriptor("dashscope")

    assert descriptor is not None
    assert descriptor.login_supported is False
    assert "2026-04-15" in descriptor.unavailable_reason
    assert "Qwen Code OAuth" in descriptor.unavailable_reason
    assert "qwen-code 0.25.1" in descriptor.unavailable_reason
    assert "2026-04-15" in descriptor.unavailable_reason_en
    assert "API Key" in descriptor.unavailable_reason


def test_get_provider_descriptor_returns_none_for_unknown_or_empty_ids():
    assert get_provider_descriptor("not-a-provider") is None
    assert get_provider_descriptor("") is None
    assert get_provider_descriptor("   ") is None


# ─── Manifest consistency ──────────────────────────────────────────────────


def _manifest_models_by_provider() -> dict[str, list[str]]:
    from src.llms.llm import ModelConfig

    metadata = ModelConfig().get_model_metadata()
    by_provider: dict[str, list[str]] = {}
    for model_name, model_info in metadata.items():
        by_provider.setdefault(model_info.get("provider", ""), []).append(model_name)
    return {provider: sorted(models) for provider, models in by_provider.items()}


@pytest.mark.parametrize("provider_id", list(EXPECTED_MODELS))
def test_login_models_match_the_manifest(provider_id):
    descriptor = get_provider_descriptor(provider_id)
    assert descriptor is not None

    by_provider = _manifest_models_by_provider()
    assert list(descriptor.models) == by_provider.get(provider_id, [])


def test_every_catalog_model_exists_in_the_manifest_under_its_provider():
    from src.llms.llm import ModelConfig

    metadata = ModelConfig().get_model_metadata()
    for descriptor in PROVIDER_DESCRIPTORS:
        for model_name in descriptor.models:
            assert model_name in metadata, f"{descriptor.id} lists unknown model {model_name}"
            assert metadata[model_name]["provider"] == descriptor.id


def test_every_api_key_provider_is_a_manifest_provider():
    from src.llms.llm import ModelConfig

    config = ModelConfig()
    for descriptor in PROVIDER_DESCRIPTORS:
        assert descriptor.api_key_provider
        assert config.get_provider_info(descriptor.api_key_provider), (
            f"{descriptor.id} maps to unknown provider {descriptor.api_key_provider}"
        )


# ─── Endpoint ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_providers_route_returns_the_catalog_payload():
    from src.server.app import oauth as routes

    result = await routes.oauth_providers("user-1")

    assert set(result) == {"providers"}
    providers = result["providers"]
    assert [provider["id"] for provider in providers] == [
        descriptor.id for descriptor in PROVIDER_DESCRIPTORS
    ]
    assert set(providers[0]) == DESCRIPTOR_KEYS | {"connect_defaults", "connect_options"}
    assert json.loads(json.dumps(result)) == result


def test_coding_plan_cards_carry_usage_hints():
    """Subscription routes must state their person/tool restrictions in the UI."""
    for provider_id in ("kimi-oauth", "z-ai-oauth", "z-ai-cn-oauth", "mimo-oauth"):
        descriptor = get_provider_descriptor(provider_id)
        assert descriptor is not None
        assert descriptor.usage_hint and descriptor.usage_hint_en
        assert descriptor.usage_policy_url
    plain = get_provider_descriptor("deepseek")
    assert plain is not None and plain.usage_hint is None
