import importlib

import pytest


@pytest.mark.asyncio
async def test_explicit_disabled_provider_never_reaches_adapter(monkeypatch, tmp_path):
    from src.config.models import NativeRuntimeRoutingConfig
    from src.llms.native_cli.sessions import NativeCliSessionStore

    module = importlib.import_module("ptc_agent.agent.tools.native_agent")
    monkeypatch.setattr(
        "src.config.settings.get_native_runtime_routing_config",
        lambda: NativeRuntimeRoutingConfig(disabled_providers=["grok"]),
    )
    adapter_calls = []
    monkeypatch.setattr(
        module,
        "get_adapter",
        lambda *args, **kwargs: adapter_calls.append((args, kwargs)),
    )

    result = await module._delegate(
        prompt="hello",
        provider="grok",
        thread="t-disabled-grok",
        sessions=NativeCliSessionStore(tmp_path / "sessions.json"),
        mcp_servers=[],
    )

    assert "disabled by operator policy" in result
    assert adapter_calls == []
