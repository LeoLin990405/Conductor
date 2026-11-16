from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from src.server.services.native_runtime.router import (
    RuntimeRequest,
    RuntimeRoutingPolicy,
    route_runtime,
)


@dataclass
class Status:
    provider: str
    installed: bool = True
    live_verified: bool = True
    capabilities: dict | None = None


def _statuses():
    return [
        Status("codex", capabilities={"streaming": True, "approvals": True}),
        Status("deepseek-dsh", capabilities={"streaming": True, "approvals": False}),
        Status("minimax", capabilities={"streaming": True, "approvals": True}),
    ]


def test_fresh_available_provider_outranks_unknown_baseline():
    decision = route_runtime(
        _statuses(),
        RuntimeRequest(task="refactor code"),
        headroom={"deepseek-dsh": "available"},
    )

    assert decision.runtime == "deepseek-dsh"
    assert decision.fallbacks[0] == "codex"
    assert "fresh quota headroom available" in decision.reason


def test_exhausted_provider_is_skipped_when_unknown_provider_is_available():
    decision = route_runtime(
        _statuses(),
        RuntimeRequest(task="refactor code"),
        headroom={"codex": "exhausted"},
    )

    assert decision.runtime == "deepseek-dsh"
    assert "codex" not in decision.fallbacks


def test_low_balance_ranks_below_unknown_even_when_low_provider_has_affinity():
    decision = route_runtime(
        _statuses(),
        RuntimeRequest(task="refactor code"),
        headroom={"codex": "low", "deepseek-dsh": "unknown", "minimax": "exhausted"},
    )

    assert decision.runtime == "deepseek-dsh"
    assert decision.fallbacks == ("codex", "ptc")
    assert "minimax" not in decision.fallbacks


def test_automatic_routing_never_selects_exhausted_with_only_one_non_exhausted():
    decision = route_runtime(
        _statuses(), RuntimeRequest(task="refactor code"),
        headroom={"codex": "exhausted", "deepseek-dsh": "exhausted", "minimax": "low"},
    )

    assert decision.runtime == "minimax"
    assert "codex" not in decision.fallbacks
    assert "deepseek-dsh" not in decision.fallbacks


def test_all_known_exhausted_uses_ptc_or_none():
    exhausted = {"codex": "exhausted", "deepseek-dsh": "exhausted", "minimax": "exhausted"}

    decision = route_runtime(_statuses(), RuntimeRequest(task="refactor code"), headroom=exhausted)
    assert decision.runtime == "ptc"
    assert "all eligible native providers are known exhausted" in decision.reason

    denied = route_runtime(
        _statuses(), RuntimeRequest(task="refactor code", allow_ptc=False), headroom=exhausted
    )
    assert denied.runtime == "none"


def test_general_chat_stays_ptc_with_headroom_ranked_non_exhausted_fallbacks():
    decision = route_runtime(
        _statuses(),
        RuntimeRequest(task="hello, just chatting"),
        headroom={"codex": "exhausted", "deepseek-dsh": "available"},
    )

    assert decision.runtime == "ptc"
    assert decision.fallbacks == ("deepseek-dsh", "minimax")


def test_explicit_provider_choice_is_not_overridden_by_headroom():
    decision = route_runtime(
        _statuses(),
        RuntimeRequest(preferred_provider="codex", task="chat"),
        headroom={"codex": "exhausted", "deepseek-dsh": "available"},
    )

    assert decision.runtime == "codex"
    assert decision.fallbacks == ("ptc",)
    assert "explicit preference" in decision.reason


def test_fallback_group_stays_after_regular_providers_before_quota_tier():
    statuses = [
        Status(name, capabilities={"streaming": True})
        for name in ("claude-code", "codex", "deepseek-dsh")
    ]
    decision = route_runtime(
        statuses,
        RuntimeRequest(task="review architecture"),
        policy=RuntimeRoutingPolicy(
            preferred_order=("deepseek-dsh", "codex"),
            fallback_only=("claude-code",),
        ),
        headroom={
            "claude": "available",
            "deepseek-dsh": "unknown",
            "codex": "low",
        },
    )

    assert decision.runtime == "deepseek-dsh"
    assert decision.fallbacks == ("codex", "claude-code", "ptc")
    ranked = [name for name in decision.scores if name in decision.fallbacks]
    assert ranked == ["codex", "claude-code"]
    assert list(decision.scores.values()) == sorted(
        decision.scores.values(), reverse=True
    )


@pytest.mark.parametrize(
    "headroom",
    [
        {"zcode": "exhausted"},
        {"zhipu": "available", "zcode": "exhausted"},
        {"zcode": "available", "zhipu": "exhausted"},
        {"zai": "exhausted"},
    ],
)
def test_exhausted_zcode_snapshot_also_excludes_zhipu_alias(headroom):
    statuses = [
        Status("zhipu", capabilities={"streaming": True}),
        Status("codex", capabilities={"streaming": True}),
    ]
    decision = route_runtime(
        statuses,
        RuntimeRequest(task="review architecture"),
        policy=RuntimeRoutingPolicy(
            preferred_order=("zhipu", "codex"), fallback_only=()
        ),
        headroom=headroom,
    )

    assert decision.runtime == "codex"
    assert "zhipu" not in decision.fallbacks


def test_usage_does_not_bypass_installed_or_capability_gates():
    statuses = [
        Status("deepseek-dsh", installed=False, capabilities={"approvals": True}),
        Status("minimax", capabilities={"approvals": False}),
    ]

    decision = route_runtime(
        statuses,
        RuntimeRequest(task="review architecture", required_capabilities=("approvals",)),
        headroom={"deepseek-dsh": "available", "minimax": "available"},
    )

    assert decision.runtime == "ptc"
    assert decision.scores == {}


def test_legacy_sync_resolver_remains_unchanged_for_non_auto():
    from src.server.services.native_runtime.router import resolve_requested_provider

    provider, decision = resolve_requested_provider("deepseek-dsh")

    assert provider == "deepseek-dsh"
    assert decision is None


def test_async_auto_resolver_uses_headroom_but_explicit_choice_skips_reader(monkeypatch):
    import asyncio

    from src.server.services.native_runtime import router

    reader = AsyncMock(return_value={"deepseek-dsh": "available"})
    monkeypatch.setattr(
        "src.server.services.native_runtime.registry.list_statuses", _statuses
    )
    monkeypatch.setattr(
        "src.server.services.native_runtime.usage_headroom.load_usage_headroom", reader
    )

    provider, decision = asyncio.run(
        router.aresolve_requested_provider("auto", task="refactor code")
    )
    assert provider == "deepseek-dsh"
    assert decision is not None

    reader.reset_mock()
    explicit, explicit_decision = asyncio.run(
        router.aresolve_requested_provider("codex", task="refactor code")
    )
    assert explicit == "codex"
    assert explicit_decision is None
    reader.assert_not_awaited()

    explicit_alias, alias_decision = asyncio.run(
        router.aresolve_requested_provider("glm", task="chat")
    )
    assert explicit_alias == "zcode"
    assert alias_decision is None
    reader.assert_not_awaited()


@pytest.mark.asyncio
async def test_recommendation_endpoint_uses_same_headroom_ranking(monkeypatch):
    from src.server.app import native_agents

    reader = AsyncMock(return_value={"deepseek-dsh": "available"})
    monkeypatch.setattr(native_agents, "list_statuses", _statuses)
    monkeypatch.setattr(native_agents, "load_usage_headroom", reader)

    result = await native_agents.route_native_agent(
        native_agents.RuntimeRouteRequest(task="refactor code"), "user-1"
    )

    assert result.runtime == "deepseek-dsh"
    assert "fresh quota headroom available" in result.reason
    reader.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_native_agent_auto_dispatch_uses_headroom_and_stubs_cli(monkeypatch, tmp_path):
    from src.llms.native_cli.base import NativeCliResult
    from src.ptc_agent.agent.tools import native_agent  # noqa: TID251

    selected = []
    monkeypatch.setenv("NATIVE_CLI_ENABLED", "1")
    monkeypatch.setenv("NATIVE_CLI_WORKSPACE_ROOT", str(tmp_path / "native-workspace"))
    monkeypatch.setattr(
        "src.server.services.native_runtime.registry.list_statuses", _statuses
    )
    monkeypatch.setattr(
        "src.server.services.native_runtime.usage_headroom.load_usage_headroom",
        AsyncMock(return_value={"deepseek-dsh": "available"}),
    )

    class FakeAdapter:
        pass

    def fake_get_adapter(provider, **_kwargs):
        selected.append(provider)
        return FakeAdapter()

    async def fake_run(_adapter, _prompt, **_kwargs):
        return NativeCliResult(provider="deepseek-dsh", text="stubbed result", session_id="s-1")

    monkeypatch.setattr(native_agent, "get_adapter", fake_get_adapter)
    monkeypatch.setattr(native_agent, "run_with_session", fake_run)
    monkeypatch.setattr(native_agent, "NativeCliSessionStore", lambda: object())

    tool = native_agent.create_native_agent_tool(thread_id="thread-headroom")
    result = await tool.ainvoke({"prompt": "refactor code", "provider": "auto"})

    assert result == "stubbed result"
    assert selected == ["deepseek-dsh"]


@pytest.mark.asyncio
async def test_native_agent_candidate_helper_passes_loaded_headroom(monkeypatch):
    from src.ptc_agent.agent.tools import native_agent  # noqa: TID251

    reader = AsyncMock(return_value={"deepseek-dsh": "available"})
    seen = {}

    def route_with_headroom(prompt, *, headroom=None):
        seen["prompt"] = prompt
        seen["headroom"] = headroom
        return ["deepseek-dsh"], "fresh quota headroom available"

    monkeypatch.setattr(
        "src.server.services.native_runtime.usage_headroom.load_usage_headroom", reader
    )
    monkeypatch.setattr(native_agent, "_auto_candidates", route_with_headroom)

    assert await native_agent._auto_candidates_async("refactor") == (
        ["deepseek-dsh"],
        "fresh quota headroom available",
    )
    assert seen == {"prompt": "refactor", "headroom": {"deepseek-dsh": "available"}}
    reader.assert_awaited_once_with()
