"""Bounded, read-only readiness facts for the authenticated user."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone


_CHECK_TIMEOUT = 1.0


async def _bounded(check):
    try:
        async with asyncio.timeout(_CHECK_TIMEOUT):
            return await check()
    except TimeoutError:
        return "unknown"
    except Exception:
        # The endpoint deliberately reports no exception text or connection data.
        return "degraded"


def _check(
    id: str,
    status: str,
    summary: str,
    *,
    required: bool = True,
    verification: str = "not_checked",
) -> dict:
    return {
        "id": id,
        "status": status,
        "required": required,
        "summary": summary,
        "verification": verification,
    }


def _overall_status(checks: list[dict]) -> str:
    if any(c["required"] and c["status"] == "missing" for c in checks):
        return "needs_action"
    if any(c["required"] and c["status"] in ("degraded", "unknown") for c in checks):
        return "degraded"
    return "ready"


async def _database_check() -> str:
    from src.server.database.pool import get_db_connection

    async with get_db_connection() as conn:
        await conn.execute("SELECT 1")
    return "ok"


async def _redis_check() -> str:
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled:
        return "missing"
    if cache.client is None:
        return "unknown"
    return "ok" if await cache.client.ping() else "degraded"


async def _checkpointer_check() -> str:
    from src.server.app import setup

    cp = setup.checkpointer
    if cp is None:
        return "missing"
    if not hasattr(cp, "conn"):
        return "ok"  # in-memory checkpointer is an explicit deployment mode
    pool = cp.conn
    async with pool.connection() as conn:
        await conn.execute("SELECT 1")
    return "ok"


async def _model_config_check(user_id: str, prefs: dict) -> tuple[str, str]:
    from src.llms.llm import LLM, platform_key_configured
    from src.llms.preferences import custom_model
    from src.server.database.api_keys import get_user_provider_credential_status
    from src.server.database.oauth_tokens import get_oauth_status
    from src.server.services.llm.clients import _candidate_slugs
    from src.server.services.llm.thread_model import effective_default_model
    from src.server.services.llm.user_models import get_custom_provider_config

    model_name = effective_default_model(prefs)
    if not model_name:
        return "missing", "No effective primary model is selected."
    from src.server.services.llm.auto_model import AUTO_MODEL, resolve_auto_model

    if model_name == AUTO_MODEL:
        model_name, _fallbacks = resolve_auto_model()
        if not model_name:
            return "missing", "No configured model is available in the Auto chain."
    custom_entry = custom_model(prefs, model_name)
    model_info = custom_entry or LLM.get_model_config().get_model_config(model_name)
    if not model_info:
        return (
            "missing",
            "The selected primary model is not in the available model catalog.",
        )
    provider = model_info.get("provider")
    if not provider:
        return "missing", "The selected model has no configured provider."
    mc = LLM.get_model_config()
    custom_provider = await get_custom_provider_config(
        user_id, provider, _pref_cache=prefs
    )
    provider_info = mc.get_provider_info(
        custom_provider.get("parent_provider") if custom_provider else provider
    )
    if provider_info.get("access_type") == "oauth":
        status = await get_oauth_status(user_id, provider)
        return (
            (
                "present_unverified",
                "An OAuth connection is recorded; model access was not tested.",
            )
            if status.get("connected")
            else ("missing", "Connect the required model provider account.")
        )

    credentials = await get_user_provider_credential_status(user_id)
    provider_candidates = [model_name, provider]
    if custom_provider:
        provider_candidates.append(custom_provider.get("parent_provider"))
    elif mc.get_provider_info(provider):
        # A custom model may still use a built-in provider family. Reuse the
        # same candidate order as the canonical BYOK resolver in that case.
        provider_candidates.extend(_candidate_slugs(provider, mc))
    if credentials["enabled"]:
        if any(
            credentials["providers"].get(slug) for slug in provider_candidates if slug
        ):
            return (
                "present_unverified",
                "A BYOK credential or endpoint is configured; model access was not tested.",
            )
    if not custom_entry and not custom_provider and mc.get_model_config(model_name):
        try:
            deployment_credential = platform_key_configured(model_name)
        except ValueError:
            deployment_credential = False
    else:
        deployment_credential = False
    if deployment_credential:
        return (
            "present_unverified",
            "A deployment credential is configured; model access was not tested.",
        )
    return (
        "missing",
        "No usable credential is configured for the selected primary model.",
    )


async def get_readiness(user_id: str) -> dict:
    """Report local dependency checks and user configuration without probing providers."""
    from src.server.app import setup
    from src.server.services.llm.user_models import get_model_preference
    from src.server.database.home_workspace import get_home_workspace_id
    from src.server.services.computer_manager.manager import ComputerManager

    checks = []
    dependency_names = ("database", "checkpointer", "redis")
    dependency_results = await asyncio.gather(
        *(
            _bounded(check)
            for check in (_database_check, _checkpointer_check, _redis_check)
        )
    )
    for name, result in zip(dependency_names, dependency_results, strict=True):
        checks.append(
            _check(
                name,
                result,
                {
                    "ok": "Dependency responded to a read-only check.",
                    "missing": "Dependency is not configured.",
                    "unknown": "Check exceeded its time limit.",
                    "degraded": "Dependency is unavailable.",
                }[result],
                required=True,
                verification="observed" if result == "ok" else "not_checked",
            )
        )

    registry = None
    try:
        from src.server.services.mcp_core.registry import get_global_registry

        registry = get_global_registry()
        mcp_status = (
            "unknown"
            if registry is None
            else ("degraded" if registry.failed_builtin_remote else "ok")
        )
    except Exception:
        mcp_status = "unknown"
    checks.append(
        _check(
            "mcp",
            mcp_status,
            "Built-in MCP discovery state; remote tool calls were not tested.",
            required=True,
            verification="observed"
            if mcp_status in ("ok", "degraded")
            else "not_checked",
        )
    )

    sandbox_provider = None
    if setup.agent_config and setup.agent_config.sandbox:
        sandbox_provider = setup.agent_config.sandbox.provider
    checks.append(
        _check(
            "sandbox",
            "ok" if sandbox_provider and sandbox_provider != "none" else "missing",
            "Sandbox provider configuration is present."
            if sandbox_provider and sandbox_provider != "none"
            else "No sandbox provider is configured.",
            required=True,
            verification="configuration_only",
        )
    )

    manager = ComputerManager.current()
    if manager is None:
        sandbox_connection = "unknown"
    else:
        try:
            sandbox_connection = (
                "ok"
                if manager.has_ready_session(get_home_workspace_id(user_id))
                else "unknown"
            )
        except Exception:
            sandbox_connection = "unknown"
    checks.append(
        _check(
            "sandbox_session",
            sandbox_connection,
            "A ready Home sandbox session is present in this worker."
            if sandbox_connection == "ok"
            else "No ready Home sandbox session is currently observed in this worker.",
            required=False,
            verification="observed" if sandbox_connection == "ok" else "not_checked",
        )
    )

    async def model_check():
        prefs = await get_model_preference(user_id)
        return await _model_config_check(user_id, prefs)

    result = await _bounded(model_check)
    if result in ("unknown", "degraded"):
        model_status, model_summary = (
            result,
            "Model configuration could not be checked.",
        )
    else:
        model_state, model_summary = result
        model_status = "ok" if model_state == "present_unverified" else model_state
    checks.append(
        _check(
            "primary_model",
            model_status,
            model_summary,
            required=True,
            verification="configuration_only"
            if model_status in ("ok", "missing")
            else "not_checked",
        )
    )

    return {
        "status": _overall_status(checks),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "verification": "local_checks_only",
        "checks": checks,
    }
