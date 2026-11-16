import asyncio
from unittest.mock import AsyncMock

import pytest

from src.server.services import system_readiness


@pytest.mark.asyncio
async def test_bounded_probe_reports_timeout_without_exception_details():
    async def too_slow():
        await asyncio.sleep(0.02)

    original = system_readiness._CHECK_TIMEOUT
    system_readiness._CHECK_TIMEOUT = 0.001
    try:
        assert await system_readiness._bounded(too_slow) == "unknown"
    finally:
        system_readiness._CHECK_TIMEOUT = original


@pytest.mark.asyncio
async def test_model_readiness_uses_credential_presence_metadata_only(monkeypatch):
    from src.llms import llm as llm_module
    from src.llms import preferences
    from src.server.services.llm import clients, thread_model

    class ModelCatalog:
        def get_model_config(self, name):
            return {"provider": "example"} if name == "known-model" else None

        def get_provider_info(self, provider):
            return {"access_type": "api_key"}

    catalog = ModelCatalog()

    class FakeLLM:
        @staticmethod
        def get_model_config():
            return catalog

    monkeypatch.setattr(llm_module, "LLM", FakeLLM)
    monkeypatch.setattr(
        thread_model, "effective_default_model", lambda _prefs: "known-model"
    )
    monkeypatch.setattr(preferences, "custom_model", lambda _prefs, _name: None)
    monkeypatch.setattr(clients, "_candidate_slugs", lambda _provider, _mc: ["example"])
    monkeypatch.setattr(llm_module, "platform_key_configured", lambda _name: False)

    async def present(_user_id):
        return {"enabled": True, "providers": {"example": True}}

    async def absent(_user_id):
        return {"enabled": False, "providers": {"example": False}}

    monkeypatch.setattr(
        "src.server.database.api_keys.get_user_provider_credential_status", present
    )
    status, summary = await system_readiness._model_config_check("user", {})
    assert status == "present_unverified"
    assert "not tested" in summary
    assert "secret" not in summary.lower()

    monkeypatch.setattr(
        "src.server.database.api_keys.get_user_provider_credential_status", absent
    )
    status, summary = await system_readiness._model_config_check("user", {})
    assert status == "missing"
    assert "credential" in summary


@pytest.mark.asyncio
async def test_auto_without_any_chain_credential_reports_missing(monkeypatch):
    from src.llms import llm as llm_module
    from src.server.services.llm import auto_model, thread_model

    class ModelCatalog:
        def get_model_config(self, name):
            return {"provider": "example"} if name == "example-model" else None

        def get_provider_info(self, _provider):
            return {"access_type": "api_key", "env_key": "EXAMPLE_KEY"}

        def get_parent_provider(self, provider):
            return provider

        def get_child_variants(self, _provider):
            return []

    class FakeLLM:
        @staticmethod
        def get_model_config():
            return ModelCatalog()

    monkeypatch.setattr(llm_module, "LLM", FakeLLM)
    monkeypatch.setattr(thread_model, "effective_default_model", lambda _prefs: "auto")
    monkeypatch.setattr(auto_model, "auto_chain", lambda: ["example-model"])
    monkeypatch.delenv("EXAMPLE_KEY", raising=False)
    monkeypatch.setattr(
        "src.server.database.api_keys.get_user_provider_credential_status",
        AsyncMock(return_value={"enabled": False, "providers": {}}),
    )
    monkeypatch.setattr(
        "src.server.services.llm.clients._candidate_slugs", lambda *_: ["example"]
    )
    monkeypatch.setattr(llm_module, "platform_key_configured", lambda _name: False)

    status, summary = await system_readiness._model_config_check("user", {})
    assert status == "missing"
    assert "credential" in summary


@pytest.mark.asyncio
async def test_custom_model_without_key_reports_missing_not_manifest_error(monkeypatch):
    from src.llms import llm as llm_module
    from src.llms import preferences
    from src.server.services.llm import thread_model

    class ModelCatalog:
        def get_model_config(self, _name):
            return None

        def get_provider_info(self, _provider):
            return {}

    class FakeLLM:
        @staticmethod
        def get_model_config():
            return ModelCatalog()

    monkeypatch.setattr(llm_module, "LLM", FakeLLM)
    monkeypatch.setattr(
        thread_model, "effective_default_model", lambda _prefs: "custom-model"
    )
    monkeypatch.setattr(
        preferences,
        "custom_model",
        lambda _prefs, _name: {"name": "custom-model", "provider": "custom-provider"},
    )
    monkeypatch.setattr(
        "src.server.services.llm.user_models.get_custom_provider_config",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "src.server.database.api_keys.get_user_provider_credential_status",
        AsyncMock(return_value={"enabled": False, "providers": {}}),
    )

    status, summary = await system_readiness._model_config_check("user", {})
    assert status == "missing"
    assert "credential" in summary


@pytest.mark.asyncio
async def test_custom_model_uses_canonical_builtin_provider_key_candidates(monkeypatch):
    from src.llms import llm as llm_module
    from src.llms import preferences
    from src.server.services.llm import clients, thread_model

    class ModelCatalog:
        def get_model_config(self, _name):
            return None

        def get_provider_info(self, provider):
            return {"access_type": "api_key"} if provider == "moonshot" else {}

        def get_parent_provider(self, provider):
            return provider

        def get_child_variants(self, _provider):
            return ["moonshot-coding"]

    class FakeLLM:
        @staticmethod
        def get_model_config():
            return ModelCatalog()

    monkeypatch.setattr(llm_module, "LLM", FakeLLM)
    monkeypatch.setattr(
        thread_model, "effective_default_model", lambda _prefs: "my-moonshot"
    )
    monkeypatch.setattr(
        preferences,
        "custom_model",
        lambda _prefs, _name: {"name": "my-moonshot", "provider": "moonshot"},
    )
    monkeypatch.setattr(
        "src.server.services.llm.user_models.get_custom_provider_config",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "src.server.database.api_keys.get_user_provider_credential_status",
        AsyncMock(
            return_value={
                "enabled": True,
                "providers": {"moonshot-coding": True},
            }
        ),
    )
    candidates = ["moonshot", "moonshot-coding"]
    monkeypatch.setattr(clients, "_candidate_slugs", lambda *_: candidates)
    monkeypatch.setattr(llm_module, "platform_key_configured", lambda _name: False)

    status, summary = await system_readiness._model_config_check(
        "user", {"custom_models": [{"name": "my-moonshot"}]}
    )
    assert status == "present_unverified"
    assert "not tested" in summary


def test_overall_readiness_ignores_optional_unobserved_sandbox_session():
    ready = [
        {"id": "database", "status": "ok", "required": True},
        {"id": "sandbox_session", "status": "unknown", "required": False},
    ]
    assert system_readiness._overall_status(ready) == "ready"
    ready[0]["status"] = "unknown"
    assert system_readiness._overall_status(ready) == "degraded"
    ready[0]["status"] = "missing"
    assert system_readiness._overall_status(ready) == "needs_action"


@pytest.mark.asyncio
async def test_readiness_without_runtime_or_model_configuration_needs_action(
    monkeypatch,
):
    from src.server.app import setup
    from src.server.services.computer_manager.manager import ComputerManager
    from src.server.services.llm import user_models

    async def ok():
        return "ok"

    monkeypatch.setattr(setup, "agent_config", None)
    monkeypatch.setattr(system_readiness, "_database_check", ok)
    monkeypatch.setattr(system_readiness, "_checkpointer_check", ok)
    monkeypatch.setattr(system_readiness, "_redis_check", ok)
    monkeypatch.setattr(user_models, "get_model_preference", AsyncMock(return_value={}))
    monkeypatch.setattr(ComputerManager, "current", classmethod(lambda cls: None))

    class Registry:
        failed_builtin_remote = []

    monkeypatch.setattr(
        "src.server.services.mcp_core.registry.get_global_registry", lambda: Registry()
    )
    result = await system_readiness.get_readiness("user-a")
    checks = {check["id"]: check for check in result["checks"]}
    assert result["status"] == "needs_action"
    assert checks["primary_model"]["status"] == "missing"
    assert checks["sandbox"]["status"] == "missing"
    assert checks["sandbox_session"]["status"] == "unknown"
    assert checks["sandbox_session"]["required"] is False
    assert {check["status"] for check in result["checks"]} <= {
        "ok",
        "missing",
        "degraded",
        "unknown",
    }
