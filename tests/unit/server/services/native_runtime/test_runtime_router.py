from dataclasses import dataclass

from src.server.services.native_runtime.router import (
    RuntimeRequest,
    RuntimeRoutingPolicy,
    route_runtime,
)
from src.utils.provider_roles import route_research_provider


@dataclass
class FakeCaps:
    values: dict[str, bool]

    def as_dict(self):
        return dict(self.values)


@dataclass
class FakeStatus:
    provider: str
    installed: bool = True
    live_verified: bool = True
    capabilities: FakeCaps | None = None


def statuses():
    return [
        FakeStatus("codex", capabilities=FakeCaps({
            "streaming": True, "tool_events": True, "session_resume": True,
            "cache_usage": True, "approvals": True, "mcp": True,
            "memory": True, "skills": True, "workspace_files": True, "cancel": True,
        })),
        FakeStatus("deepseek-dsh", capabilities=FakeCaps({
            "streaming": True, "tool_events": True, "session_resume": True,
            "cache_usage": True, "approvals": False, "mcp": True,
            "memory": True, "skills": True, "workspace_files": True, "cancel": True,
        })),
        FakeStatus("zcode", capabilities=FakeCaps({
            "session_resume": True, "cache_usage": True, "workspace_files": True,
        })),
        FakeStatus("claude-code", live_verified=False, capabilities=FakeCaps({})),
    ]


def test_capability_gate_picks_codex_when_approvals_required():
    decision = route_runtime(statuses(), RuntimeRequest(required_capabilities=("approvals",)))
    assert decision.runtime == "codex"
    assert decision.fallbacks == ("ptc",)


def test_explicit_preference_wins_when_eligible():
    decision = route_runtime(statuses(), RuntimeRequest(preferred_provider="deepseek-dsh"))
    assert decision.runtime == "deepseek-dsh"
    assert "explicit preference" in decision.reason


def test_ineligible_preference_falls_to_ptc_with_reason():
    decision = route_runtime(
        statuses(),
        RuntimeRequest(preferred_provider="deepseek-dsh", required_capabilities=("approvals",)),
    )
    assert decision.runtime == "ptc"
    assert "missing approvals" in decision.reason


def test_unverified_provider_is_never_selected():
    decision = route_runtime(statuses(), RuntimeRequest(preferred_provider="claude-code"))
    assert decision.runtime == "ptc"


def test_no_live_provider_falls_back_to_ptc():
    only_unverified = [FakeStatus("claude-code", live_verified=False)]
    decision = route_runtime(only_unverified, RuntimeRequest())
    assert decision.runtime == "ptc"
    assert decision.fallbacks == ()


def test_ptc_can_be_disallowed():
    decision = route_runtime([], RuntimeRequest(allow_ptc=False))
    assert decision.runtime == "none"


def test_auto_resolver_returns_provider_and_decision(monkeypatch):
    from src.server.services.native_runtime import router as router_module

    monkeypatch.setattr(
        "src.server.services.native_runtime.registry.list_statuses",
        lambda: statuses(),
    )
    provider, decision = router_module.resolve_requested_provider(
        "auto", task="refactor code"
    )
    assert provider == "deepseek-dsh"
    assert decision is not None
    assert decision.runtime == "deepseek-dsh"


def test_non_auto_resolver_is_unchanged():
    from src.server.services.native_runtime import router as router_module

    provider, decision = router_module.resolve_requested_provider("deepseek-dsh")
    assert provider == "deepseek-dsh"
    assert decision is None


def test_explicit_glm_alias_binds_to_zcode():
    from src.server.services.native_runtime.router import resolve_requested_provider

    provider, decision = resolve_requested_provider("glm")
    assert provider == "zcode"
    assert decision is None


def test_general_chat_stays_on_ptc_without_signal():
    decision = route_runtime(statuses(), RuntimeRequest(task="你好，聊聊天"))
    assert decision.runtime == "ptc"
    assert "PTC is the default" in decision.reason
    assert decision.fallbacks[0] == "deepseek-dsh"


def test_explicit_native_still_wins_for_general_chat():
    decision = route_runtime(statuses(), RuntimeRequest(task="你好", preferred_provider="deepseek-dsh"))
    assert decision.runtime == "deepseek-dsh"


def test_configured_chinese_order_precedes_codex_and_unlisted_providers():
    all_caps = FakeCaps({name: True for name in (
        "streaming", "tool_events", "session_resume", "cache_usage", "approvals",
        "mcp", "memory", "skills", "workspace_files", "cancel",
    )})
    candidates = [
        FakeStatus(name, capabilities=all_caps)
        for name in (
            "claude-code", "cursor", "codex", "deepseek-dsh", "zcode",
            "kimi", "mimo", "stepfun", "minimax", "doubao",
        )
    ]
    decision = route_runtime(
        candidates,
        RuntimeRequest(task="code review mcp architecture"),
        policy=RuntimeRoutingPolicy(
            preferred_order=(
                "doubao", "minimax", "stepfun", "mimo", "kimi", "zhipu", "zcode",
                "deepseek-dsh", "codex",
            ),
            fallback_only=("claude-code",),
        ),
    )
    assert decision.runtime == "doubao"
    assert decision.fallbacks == (
        "minimax", "stepfun", "mimo", "kimi", "zcode", "deepseek-dsh",
        "codex", "cursor", "claude-code", "ptc",
    )
    assert decision.reason.startswith("doubao ranks highest among eligible")


def test_fallback_only_claude_cannot_be_promoted_by_review_affinity():
    caps = FakeCaps({"streaming": True, "workspace_files": True})
    candidates = [
        FakeStatus(name, capabilities=caps)
        for name in ("claude-code", "cursor", "codex")
    ]
    decision = route_runtime(
        candidates,
        RuntimeRequest(task="code review architecture mcp"),
        policy=RuntimeRoutingPolicy(
            preferred_order=("codex",), fallback_only=("claude-code",)
        ),
    )
    assert decision.runtime == "codex"
    assert decision.fallbacks[-2] == "claude-code"


def test_policy_can_explicitly_remove_claude_fallback_barrier():
    caps = FakeCaps({"streaming": True, "workspace_files": True})
    candidates = [
        FakeStatus(name, capabilities=caps)
        for name in ("claude-code", "codex")
    ]
    decision = route_runtime(
        candidates,
        RuntimeRequest(task="code review architecture"),
        policy=RuntimeRoutingPolicy(
            preferred_order=("claude-code", "codex"), fallback_only=()
        ),
    )
    assert decision.runtime == "claude-code"


def test_missing_preferred_provider_falls_through_to_next_configured_provider():
    candidates = [
        FakeStatus(name, capabilities=FakeCaps({"workspace_files": True}))
        for name in ("minimax", "codex")
    ]
    decision = route_runtime(
        candidates,
        RuntimeRequest(task="code review"),
        policy=RuntimeRoutingPolicy(
            preferred_order=("doubao", "minimax", "codex"),
            fallback_only=("claude-code",),
        ),
    )
    assert decision.runtime == "minimax"


def test_claude_can_be_selected_as_the_only_capable_fallback():
    candidates = [
        FakeStatus(
            "claude-code",
            capabilities=FakeCaps({"workspace_files": True}),
        ),
        FakeStatus(
            "codex",
            capabilities=FakeCaps({"workspace_files": False}),
        ),
    ]
    decision = route_runtime(
        candidates,
        RuntimeRequest(required_capabilities=("workspace_files",)),
        policy=RuntimeRoutingPolicy(
            preferred_order=("codex",), fallback_only=("claude-code",)
        ),
    )
    assert decision.runtime == "claude-code"
    assert "configured fallback" in decision.reason


def test_explicit_preferred_claude_overrides_fallback_only_default():
    candidates = [
        FakeStatus(name, capabilities=FakeCaps({"workspace_files": True}))
        for name in ("claude-code", "codex")
    ]
    decision = route_runtime(
        candidates,
        RuntimeRequest(preferred_provider="claude-code"),
        policy=RuntimeRoutingPolicy(
            preferred_order=("codex",), fallback_only=("claude-code",)
        ),
    )
    assert decision.runtime == "claude-code"
    assert "explicit preference" in decision.reason
    assert decision.fallbacks == ("ptc",)


def test_route_runtime_reads_production_settings_accessor(monkeypatch):
    from src.config.models import NativeRuntimeRoutingConfig

    monkeypatch.setattr(
        "src.config.settings.get_native_runtime_routing_config",
        lambda: NativeRuntimeRoutingConfig(
            preferred_order=["deepseek-dsh", "codex"], fallback_only=[]
        ),
    )
    decision = route_runtime(
        statuses(), RuntimeRequest(task="refactor code")
    )
    assert decision.runtime == "deepseek-dsh"


def test_zhipu_is_routable_without_inheriting_zcode_capabilities():
    capabilities = FakeCaps({"cancel": True})
    candidates = [
        FakeStatus("zhipu", capabilities=capabilities),
        FakeStatus("zcode", capabilities=capabilities),
    ]
    decision = route_runtime(
        candidates,
        RuntimeRequest(required_capabilities=("cancel",)),
        policy=RuntimeRoutingPolicy(
            preferred_order=("zhipu", "zcode"), fallback_only=("claude-code",)
        ),
    )
    assert decision.runtime == "zhipu"
    assert decision.fallbacks == ("zcode", "ptc")


def test_default_policy_keeps_zhipu_and_zcode_distinct():
    from src.config.models import NativeRuntimeRoutingConfig

    policy = NativeRuntimeRoutingConfig()
    assert policy.preferred_order[:10] == [
        "deepseek-dsh",
        "codex",
        "doubao",
        "minimax",
        "stepfun",
        "mimo",
        "kimi",
        "zhipu",
        "zcode",
        "grok",
    ]
    assert policy.preferred_order[-2:] == ["grok", "cursor"]
    assert policy.preferred_order.index("zhipu") < policy.preferred_order.index("zcode")
    assert policy.fallback_only == ["claude-code"]
    assert policy.disabled_providers == []


def test_disabled_provider_is_rejected_even_for_explicit_routing():
    capabilities = FakeCaps({"cancel": True})
    candidates = [
        FakeStatus(name, capabilities=capabilities)
        for name in ("grok", "codex")
    ]
    decision = route_runtime(
        candidates,
        RuntimeRequest(preferred_provider="grok"),
        policy=RuntimeRoutingPolicy(
            preferred_order=("codex",),
            fallback_only=("claude-code",),
            disabled_providers=("grok",),
        ),
    )
    assert decision.runtime == "ptc"
    assert "disabled by operator policy" in decision.reason


def test_research_only_provider_cannot_enter_native_route_even_if_injected():
    injected = [FakeStatus("manus", capabilities=FakeCaps({"streaming": True}))]
    decision = route_runtime(
        injected,
        RuntimeRequest(task="research current climate policy", preferred_provider="manus"),
    )
    assert decision.runtime == "ptc"
    assert "research-only" in decision.reason
    assert "manus" not in decision.fallbacks


def test_research_provider_route_selects_manus_only_for_research_work():
    assert route_research_provider("Research recent papers on battery recycling") == "manus"
    assert route_research_provider(
        "Research how to implement this parser", preferred_provider="manus"
    ) is None
    assert route_research_provider(
        "Research recent papers", preferred_provider="codex"
    ) is None
