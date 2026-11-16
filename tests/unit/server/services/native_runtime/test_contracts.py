"""Native runtime contract, metadata and capability inventory."""

from __future__ import annotations

import pytest

from src.llms.native_cli.base import NativeCliResult, NativeCliUsage
from src.server.services.native_runtime import (
    NativeRuntimeError,
    NativeRuntimeSpec,
    get_runtime,
    list_statuses,
    metadata_for,
    read_spec,
    with_session_metadata,
)


class TestSpec:
    def test_metadata_roundtrip_keeps_session_and_cwd(self):
        spec = NativeRuntimeSpec(
            provider="zcode",
            session_id="sess-1",
            cwd="/tmp/work",
            model="glm-5",
        )
        metadata = spec.to_metadata()
        restored = NativeRuntimeSpec.from_thread_metadata(metadata)
        assert restored == spec
        assert read_spec({"metadata": metadata}) == spec

    def test_ptc_thread_has_no_binding(self):
        assert read_spec({"metadata": {"origin": {"type": "automation"}}}) is None
        assert read_spec({}) is None

    def test_malformed_binding_is_loud(self):
        with pytest.raises(NativeRuntimeError, match="not an object"):
            NativeRuntimeSpec.from_thread_metadata({"native_runtime": "zcode"})
        with pytest.raises(NativeRuntimeError, match="no provider"):
            NativeRuntimeSpec.from_thread_metadata({"native_runtime": {}})

    def test_with_session_preserves_binding_fields(self):
        spec = NativeRuntimeSpec(provider="minimax", cwd="/tmp/w")
        next_spec = spec.with_session("sess-2")
        assert next_spec.provider == "minimax"
        assert next_spec.cwd == "/tmp/w"
        assert next_spec.session_id == "sess-2"

    def test_metadata_for_creates_binding(self):
        assert metadata_for("mimo", cwd="/tmp/w")["native_runtime"]["provider"] == "mimo"

    def test_research_only_provider_cannot_bind_native_thread(self):
        with pytest.raises(NativeRuntimeError, match="research-only"):
            metadata_for("manus")

    def test_with_session_metadata_keeps_origin(self):
        merged = with_session_metadata(
            {"origin": {"type": "automation"}},
            NativeRuntimeSpec(provider="dsh", session_id="s-1"),
        )
        assert merged["origin"] == {"type": "automation"}
        assert merged["native_runtime"]["session_id"] == "s-1"


class TestRegistry:
    def test_statuses_cover_expected_providers(self):
        providers = {status.provider for status in list_statuses()}
        assert {"deepseek-dsh", "codex", "zcode", "minimax", "mimo"} <= providers

    def test_status_never_claims_live_without_proof(self):
        by_provider = {status.provider: status for status in list_statuses()}
        assert by_provider["deepseek-dsh"].live_verified is True
        assert by_provider["codex"].live_verified is True
        assert by_provider["zcode"].live_verified is True
        assert by_provider["zhipu"].live_verified is True
        assert by_provider["zhipu"].capabilities.cancel is True
        assert by_provider["zcode"].capabilities.cache_usage is True
        assert by_provider["minimax"].live_verified is True
        assert by_provider["minimax"].capabilities.mcp is True
        assert by_provider["minimax"].capabilities.memory is True
        assert by_provider["minimax"].capabilities.skills is True
        assert by_provider["mimo"].live_verified is True

    def test_disabled_provider_cannot_become_live_through_registry(self, monkeypatch):
        from src.config.models import NativeRuntimeRoutingConfig
        from src.server.services.native_runtime import registry

        original = registry._ADAPTERS["codex"]
        monkeypatch.setitem(
            registry._ADAPTERS,
            "codex",
            (original[0], original[1], True),
        )
        monkeypatch.setattr(
            "src.config.settings.get_native_runtime_routing_config",
            lambda: NativeRuntimeRoutingConfig(disabled_providers=["codex"]),
        )
        status = registry.status_for("codex")
        assert status.live_verified is False
        assert "disabled by operator policy" in status.error
        with pytest.raises(NativeRuntimeError, match="disabled by operator policy"):
            registry.get_runtime("codex")

    def test_capabilities_are_explicit(self, monkeypatch):
        # This test pins the adapter's native capabilities, not the optional
        # ACP transport override.
        monkeypatch.delenv("NATIVE_CLI_ACP", raising=False)
        by_provider = {status.provider: status for status in list_statuses()}
        assert by_provider["deepseek-dsh"].capabilities.cache_usage is True
        assert by_provider["deepseek-dsh"].capabilities.streaming is True
        assert by_provider["deepseek-dsh"].capabilities.tool_events is True
        assert by_provider["deepseek-dsh"].capabilities.cancel is True


class StubAdapter:
    def __init__(self, *, result=None, error=None):
        self.result = result
        self.error = error

    async def run(self, prompt, *, session_id=None, cwd=None):
        if self.error:
            raise self.error
        return self.result


class TestAdapterRuntime:
    @pytest.mark.asyncio
    async def test_one_shot_adapter_is_normalized(self):
        adapter = StubAdapter(
            result=NativeCliResult(
                provider="deepseek-dsh",
                text="ok",
                session_id="s-1",
                usage=NativeCliUsage(input_tokens=10, cache_read_tokens=90),
            )
        )
        runtime = get_runtime("deepseek-dsh", adapter=adapter)
        events = [event async for event in runtime.stream_turn("hello")]
        assert [event.kind for event in events] == ["text_delta", "usage", "turn_completed"]
        assert events[0].text == "ok"
        assert events[0].session_id == "s-1"
        assert events[1].data["cache_read_tokens"] == 90

    @pytest.mark.asyncio
    async def test_cli_error_is_a_visible_event(self):
        from src.llms.native_cli.base import NativeCliError

        runtime = get_runtime(
            "codex", adapter=StubAdapter(error=NativeCliError("not signed in"))
        )
        events = [event async for event in runtime.stream_turn("hello")]
        assert events == [
            events[0]
        ]  # one error event, no fabricated answer
        assert events[0].kind == "error"
        assert "not signed in" in events[0].text


def test_live_providers_advertise_the_same_product_bridges():
    from src.server.services.native_runtime import status_for

    for provider in ("codex", "deepseek-dsh", "minimax", "mimo"):
        caps = status_for(provider).capabilities
        assert caps.mcp is True
        assert caps.memory is True
        assert caps.skills is True
        assert caps.cancel is True
