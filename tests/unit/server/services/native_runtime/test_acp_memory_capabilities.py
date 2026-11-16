def test_acp_capabilities_include_memory_and_skills(monkeypatch):
    from src.server.services.native_runtime import registry

    monkeypatch.setenv("NATIVE_CLI_ACP", "mimo,kimi,grok")
    for provider in ("mimo", "kimi", "grok"):
        caps = registry.status_for(provider).capabilities
        assert caps.mcp is True
        assert caps.memory is True
        assert caps.skills is True
        assert caps.approvals is True
