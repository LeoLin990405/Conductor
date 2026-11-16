"""Provider connection capability catalog — the one source of truth for the UI.

The connection pages render every provider the app can offer from this tuple
instead of hard-coding a card list. An entry says what the provider supports
(device code, paste code, browser redirect, or API key only), which API-key
provider it maps to when an account login mints a coding-plan key, which models
it unlocks, and — for providers with no account login — the reason, so the UI
can explain the two-choice fallback instead of silently hiding them.

Qwen (DashScope) is listed as unavailable on purpose: Qwen Code's OAuth free
quota ended 2026-04-15 (the warning ships in qwen-code 0.25.1), so the only
remaining route is an API Key / Token Plan key. It stays in the catalog rather
than being deleted because users still search for it and need the explanation.

This module is pure data: no secrets, no I/O, and no imports outside the
standard library. The provider ids are manifest provider ids (including the
OAuth variants), so a descriptor's ``models`` can be checked against the
manifest in tests.
"""

from __future__ import annotations

from dataclasses import dataclass

# Regions and flow kinds the UI switches on.
REGION_CN = "cn"
REGION_INTL = "intl"
KIND_DEVICE = "device"
KIND_PASTE = "paste"
KIND_REDIRECT = "redirect"
KIND_API_KEY_ONLY = "api_key_only"


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """One provider connection the UI can render.

    ``login_supported`` is False only for ``api_key_only`` entries; those carry
    both unavailable-reason languages so the UI never has to invent copy.
    ``api_key_provider`` is the BYOK provider id to write when the user chooses
    the API-key path (for a login it is the coding-plan variant the login
    mints; for an api_key_only entry it is the provider itself).
    """

    id: str
    display_name: str
    display_name_en: str
    brand: str
    region: str
    kind: str
    login_supported: bool = True
    api_key_provider: str | None = None
    website_url: str | None = None
    models: tuple[str, ...] = ()
    order: int = 0
    unavailable_reason: str | None = None
    unavailable_reason_en: str | None = None
    # Coding/token-plan subscription caveats shown on the card: quota tied to
    # the person, official-tool restrictions, automation bans. None for
    # ordinary API-key/OAuth routes.
    usage_hint: str | None = None
    usage_hint_en: str | None = None
    usage_policy_url: str | None = None

    def to_dict(self) -> dict:
        """The JSON shape the API returns (JSON keys are the wire contract)."""
        return {
            "id": self.id,
            "display_name": self.display_name,
            "display_name_en": self.display_name_en,
            "brand": self.brand,
            "region": self.region,
            "kind": self.kind,
            "login_supported": self.login_supported,
            "unavailable_reason": self.unavailable_reason,
            "unavailable_reason_en": self.unavailable_reason_en,
            "usage_hint": self.usage_hint,
            "usage_hint_en": self.usage_hint_en,
            "usage_policy_url": self.usage_policy_url,
            "api_key_provider": self.api_key_provider,
            "website_url": self.website_url,
            "models": list(self.models),
            "order": self.order,
        }


# Order groups: 10 international logins, 20 domestic logins, 30 domestic
# API-key-only entries. A single digit is left between groups for insertion.
PROVIDER_DESCRIPTORS: tuple[ProviderDescriptor, ...] = (
    # ── International account logins ──────────────────────────────────────
    ProviderDescriptor(
        id="codex-oauth",
        display_name="ChatGPT Codex",
        display_name_en="ChatGPT Codex",
        brand="openai",
        region=REGION_INTL,
        kind=KIND_DEVICE,
        api_key_provider="openai",
        website_url="https://platform.openai.com/api-keys",
        order=10,
    ),
    ProviderDescriptor(
        id="claude-oauth",
        display_name="Claude 订阅登录",
        display_name_en="Claude subscription",
        brand="anthropic",
        region=REGION_INTL,
        kind=KIND_PASTE,
        api_key_provider="anthropic",
        website_url="https://console.anthropic.com/settings/keys",
        order=10,
    ),
    ProviderDescriptor(
        id="grok-oauth",
        display_name="Grok 订阅登录",
        display_name_en="Grok subscription",
        brand="xai",
        region=REGION_INTL,
        kind=KIND_DEVICE,
        api_key_provider="xai",
        website_url="https://console.x.ai",
        order=10,
    ),
    ProviderDescriptor(
        id="gemini-oauth",
        display_name="Antigravity 登录",
        display_name_en="Antigravity",
        brand="gemini",
        region=REGION_INTL,
        kind=KIND_PASTE,
        api_key_provider="gemini",
        website_url="https://antigravity.google",
        order=10,
    ),
    # ── Domestic account logins ───────────────────────────────────────────
    ProviderDescriptor(
        id="kimi-oauth",
        display_name="Kimi 订阅登录",
        display_name_en="Kimi subscription",
        brand="moonshot",
        region=REGION_CN,
        kind=KIND_DEVICE,
        api_key_provider="moonshot-coding",
        website_url="https://www.kimi.com/code/",
        usage_hint=(
            "Kimi Code 额度仅限本人使用，禁止脚本或后端自动化；请保持真实客户端标识，"
            "不要篡改 User-Agent。"
        ),
        usage_hint_en=(
            "Kimi Code quota is personal; scripts and backend automation are "
            "prohibited. Keep the real client identity and do not spoof the "
            "User-Agent."
        ),
        usage_policy_url="https://www.kimi.com/code/docs/",
        models=(
            "kimi-for-coding-oauth",
            "kimi-k3-oauth",
            "kimi-k3.1-pro-highspeed-oauth",
            "kimi-k3.1-pro-oauth",
        ),
        order=20,
    ),
    ProviderDescriptor(
        id="deepseek-account",
        display_name="DeepSeek 账号登录",
        display_name_en="DeepSeek account",
        brand="deepseek",
        region=REGION_CN,
        kind=KIND_REDIRECT,
        api_key_provider="deepseek",
        website_url="https://platform.deepseek.com",
        models=("deepseek-flash-account", "deepseek-v4-pro-account"),
        order=20,
    ),
    ProviderDescriptor(
        id="z-ai-cn-oauth",
        usage_hint='智谱 Coding Plan 仅限官方支持的编程工具使用，Conductor 不在官方白名单；请先确认订阅协议再接入。',
        usage_hint_en='Zhipu Coding Plan is limited to officially supported coding tools; Conductor is not on that list. Confirm the subscription terms before connecting.',
        usage_policy_url='https://bigmodel.cn/glm-coding',
        display_name="智谱订阅登录",
        display_name_en="Zhipu subscription",
        brand="z-ai-cn",
        region=REGION_CN,
        kind=KIND_REDIRECT,
        api_key_provider="z-ai-cn-coding",
        website_url="https://bigmodel.cn",
        models=("glm-5.3-cn-oauth", "glm-5.3-flash-cn-oauth"),
        order=20,
    ),
    ProviderDescriptor(
        id="z-ai-oauth",
        usage_hint='Z.ai Coding Plan 仅限官方支持的编程工具使用，Conductor 不在官方白名单；请先确认订阅协议再接入。',
        usage_hint_en='Z.ai Coding Plan is limited to officially supported coding tools; Conductor is not on that list. Confirm the subscription terms before connecting.',
        usage_policy_url='https://z.ai/subscribe',
        display_name="Z.ai 订阅登录",
        display_name_en="Z.ai subscription",
        brand="z-ai",
        region=REGION_INTL,
        kind=KIND_REDIRECT,
        api_key_provider="z-ai-coding",
        website_url="https://z.ai",
        models=("glm-5.3-oauth",),
        order=20,
    ),
    ProviderDescriptor(
        id="mimo-oauth",
        display_name="MiMo 订阅登录",
        display_name_en="MiMo subscription",
        brand="mimo",
        region=REGION_CN,
        kind=KIND_PASTE,
        api_key_provider="mimo",
        usage_hint=(
            "MiMo Token Plan 仅限编程工具与个人交互，禁止自动化；Token Plan Key "
            "与标准 API Key 不通用。"
        ),
        usage_hint_en=(
            "MiMo Token Plan is for coding tools and personal interaction only; "
            "automation is prohibited. Token Plan keys differ from standard API keys."
        ),
        usage_policy_url="https://platform.xiaomimimo.com/token-plan",
        website_url="https://platform.xiaomimimo.com/token-plan",
        models=("mimo-v2.6-flash-oauth", "mimo-v2.6-pro-oauth"),
        order=20,
    ),
    # ── Domestic providers with no account login (API key only) ───────────
    ProviderDescriptor(
        id="dashscope",
        display_name="通义千问 / 百炼",
        display_name_en="Qwen (DashScope)",
        brand="dashscope",
        region=REGION_CN,
        kind=KIND_API_KEY_ONLY,
        login_supported=False,
        api_key_provider="dashscope",
        website_url="https://bailian.console.aliyun.com/",
        order=30,
        unavailable_reason=(
            "Qwen Code OAuth 免费额度已于 2026-04-15 停止（qwen-code 0.25.1 内提示），"
            "仅提供 API Key / Token Plan Key 接入。"
        ),
        unavailable_reason_en=(
            "Qwen Code's OAuth free quota ended on 2026-04-15 (shown in "
            "qwen-code 0.25.1); only API key / Token Plan key access remains."
        ),
    ),
    ProviderDescriptor(
        id="minimax",
        display_name="MiniMax",
        display_name_en="MiniMax",
        brand="minimax",
        region=REGION_CN,
        kind=KIND_API_KEY_ONLY,
        login_supported=False,
        api_key_provider="minimax",
        website_url="https://platform.minimax.cn/",
        order=30,
        unavailable_reason="官方 CLI 登录基于 API Key（mmx auth login --api-key），不提供账号 OAuth。",
        unavailable_reason_en=(
            "The official CLI signs in with an API key "
            "(mmx auth login --api-key); no account OAuth is offered."
        ),
    ),
    ProviderDescriptor(
        id="stepfun",
        display_name="阶跃星辰 StepFun",
        display_name_en="StepFun",
        brand="stepfun",
        region=REGION_CN,
        kind=KIND_API_KEY_ONLY,
        login_supported=False,
        api_key_provider="stepfun",
        website_url="https://platform.stepfun.com/",
        order=30,
        unavailable_reason="官方平台仅提供 API Key / Step Plan Key 接入。",
        unavailable_reason_en="The official platform provides API keys / Step Plan keys only.",
    ),
    ProviderDescriptor(
        id="doubao-plan",
        display_name="豆包 / 火山方舟 ARK",
        display_name_en="Doubao / Volcengine Ark",
        brand="volcengine",
        region=REGION_CN,
        kind=KIND_API_KEY_ONLY,
        login_supported=False,
        api_key_provider="doubao-plan",
        website_url="https://console.volcengine.com/ark",
        order=30,
        unavailable_reason="火山方舟 ARK 使用 API Key / AK-SK 接入，不提供第三方账号 OAuth。",
        unavailable_reason_en=(
            "Volcengine Ark uses API keys / AK-SK; no third-party account OAuth is offered."
        ),
    ),
    ProviderDescriptor(
        id="deepseek",
        display_name="DeepSeek",
        display_name_en="DeepSeek",
        brand="deepseek",
        region=REGION_CN,
        kind=KIND_API_KEY_ONLY,
        login_supported=False,
        api_key_provider="deepseek",
        website_url="https://platform.deepseek.com/",
        order=30,
        unavailable_reason="仅提供 API Key 接入。",
        unavailable_reason_en="API key access only.",
    ),
)


def describe_providers() -> list[dict]:
    """Every descriptor as the JSON-ready list the UI consumes, UI order."""
    return [descriptor.to_dict() for descriptor in PROVIDER_DESCRIPTORS]


def get_provider_descriptor(provider_id: str) -> ProviderDescriptor | None:
    """The descriptor for a connection/provider id, or None when not listed."""
    wanted = (provider_id or "").strip()
    if not wanted:
        return None
    return next(
        (descriptor for descriptor in PROVIDER_DESCRIPTORS if descriptor.id == wanted),
        None,
    )
