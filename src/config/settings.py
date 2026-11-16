"""
Centralized configuration access.

Credentials come from environment variables (.env). Infrastructure settings
come from config.yaml via InfrastructureConfig. Agent/tool settings come from
agent_config.yaml via AgentConfig (src.config.runtime).
"""

import logging
import math
import os
from typing import Any, Dict, List, Literal, Optional

from src.config.core import get_infrastructure_config
from src.config.models import (
    InfrastructureConfig,
    UsageHeadroomConfig,
    AgentFleetConfig,
    NativeRuntimeRoutingConfig,
    MemoryFederationUserBinding,
    ReviewMergeConfig,
    WorkflowOrchestrationConfig,
)

# Re-export env-var constants for backward compatibility
from src.config.env import (  # noqa: F401
    AUTH_SERVICE_URL,
    AUTOMATION_WEBHOOK_SECRET,
    AUTOMATION_WEBHOOK_URL,
    HOST_MODE,
    LOCAL_DEV_USER_ID,
    SEARCH_PROVIDER_MIN_TIER,
    SUPABASE_URL,
)

logger = logging.getLogger(__name__)


def background_dispatch_requires_token() -> bool:
    """True when auth is enabled but no INTERNAL_SERVICE_TOKEN is configured.

    Internal background dispatch is rejected with 403 in that state, so callers
    must not fire one. In oss mode the self-dispatch is trusted without a token.
    """
    return HOST_MODE != "oss" and not os.environ.get("INTERNAL_SERVICE_TOKEN", "").strip()


# =============================================================================
# Application Settings — delegates to InfrastructureConfig
# =============================================================================


def get_debug_mode() -> bool:
    return get_infrastructure_config().debug


def get_workflow_timeout() -> int:
    """Workflow timeout in seconds."""
    return get_infrastructure_config().workflow_timeout


# Deep-refactor deployment flags. These gate dual-write/dual-read paths while
# old and new implementations run side by side: default off, enabled per
# deployment for acceptance, never a user-facing preference. Read at call time
# so the differential harness can flip a flag within one process.
_REFACTOR_FLAG_ENV: dict[str, str] = {
    "goals": "REFACTOR_GOALS_ENABLED",
    "events": "REFACTOR_EVENTS_ENABLED",
    "memory_index": "REFACTOR_MEMORY_INDEX_ENABLED",
    "task_tree": "REFACTOR_TASK_TREE_ENABLED",
    "subagent_contract": "REFACTOR_SUBAGENT_CONTRACT_ENABLED",
    "permissions": "REFACTOR_PERMISSIONS_ENABLED",
    "approval_chain": "REFACTOR_APPROVAL_CHAIN_ENABLED",
    "compaction_events": "REFACTOR_COMPACTION_EVENTS_ENABLED",
}
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def get_refactor_flag(flag: str) -> bool:
    """One refactor deployment flag by short name (``goals``, ``events``, ...).

    Raises ``KeyError`` for a name outside the catalog: a typo silently reading
    as "off" would leave a dual-write path dark with no diagnostic.
    """
    env_name = _REFACTOR_FLAG_ENV[flag]
    return os.getenv(env_name, "").strip().lower() in _TRUTHY


def get_refactor_flags() -> dict[str, bool]:
    """Every refactor flag and its current state, for diagnostics and tests."""
    return {flag: get_refactor_flag(flag) for flag in _REFACTOR_FLAG_ENV}


def get_refactor_flag_envs() -> dict[str, str]:
    """Short flag name -> environment variable, for canary/rollback tooling.

    Read-only copy: callers list what to unset on rollback without importing the
    private catalog.
    """
    return dict(_REFACTOR_FLAG_ENV)


def get_workflow_orchestration_config() -> WorkflowOrchestrationConfig:
    """Caps and timeouts for RunWorkflow programmatic subagent runs."""
    return get_infrastructure_config().workflow


def get_sse_keepalive_interval() -> float:
    """SSE keepalive interval in seconds."""
    return get_infrastructure_config().sse_keepalive_interval


def get_prompt_guidance_default() -> str | None:
    """Deployment-pinned prompt scaffolding level, or None for 'auto'."""
    guidance = get_infrastructure_config().prompt.guidance
    return None if guidance == "auto" else guidance


def get_memory_federation_binding(
    user_id: str,
) -> MemoryFederationUserBinding | None:
    """Return only this user's explicit local-memory binding, if configured."""
    if not user_id:
        return None
    return get_infrastructure_config().memory_federation.user_bindings.get(user_id)


def get_autonomy_router_model() -> str | None:
    """Optional configured model for Router decisions and human-facing replies."""
    model = get_infrastructure_config().autonomy_router_model
    return model.strip() or None if model else None


_SUPERAGENT_RUNTIMES = ("ptc", "p")
# Read on every admission: warn once per bad value, not once per turn.
_SUPERAGENT_RUNTIME_WARNED: set[tuple[str, str]] = set()


def _warn_unknown_superagent_runtime(source: str, value: Any) -> None:
    key = (source, repr(value))
    if key in _SUPERAGENT_RUNTIME_WARNED:
        return
    _SUPERAGENT_RUNTIME_WARNED.add(key)
    logger.warning("%s=%r is not 'ptc' or 'p'; using 'ptc'", source, value)


def get_superagent_runtime(role: str) -> Literal["ptc", "p"]:
    """Runtime for one background Super-agent role: ``ptc`` or ``p``.

    Only ``supervisor`` and ``watchdog`` can move; any other role (Workers
    included) is always ``ptc``. ``CONDUCTOR_SUPERAGENT_RUNTIME_<ROLE>`` wins
    over config.yaml so a role can be cut over or rolled back with a restart
    and no edit to the committed deployment file. A set-but-empty environment
    value explicitly means ``ptc``. Anything unrecognised is ``ptc`` with a
    warning, so a typo never moves a role onto the new runtime.
    """
    if role not in ("supervisor", "watchdog"):
        return "ptc"
    env_name = f"CONDUCTOR_SUPERAGENT_RUNTIME_{role.upper()}"
    env_value = os.getenv(env_name)
    if env_value is not None:
        normalized = env_value.strip().lower()
        if not normalized:
            return "ptc"
        if normalized in _SUPERAGENT_RUNTIMES:
            return normalized  # type: ignore[return-value]
        _warn_unknown_superagent_runtime(env_name, env_value)
        return "ptc"
    # Absent from configs built without the field (old fixtures, stubs): ptc.
    runtime = getattr(get_infrastructure_config(), "superagent_runtime", None)
    if runtime is None:
        return "ptc"
    value = getattr(runtime, role, "ptc")
    normalized = str(value or "").strip().lower()
    if normalized in _SUPERAGENT_RUNTIMES:
        return normalized  # type: ignore[return-value]
    _warn_unknown_superagent_runtime(f"superagent_runtime.{role}", value)
    return "ptc"


def get_autonomy_router_snapshot_enabled() -> bool:
    """Whether durable Router snapshots and decision metrics are enabled.

    Always off while the Router runs on the P runtime (Planner ruling 2 of
    R8c): on a snapshot cache hit the batch prompt omits the channel notes
    and relies on earlier turns, which P's four-turn history window may
    already have dropped.
    """
    if get_superagent_runtime("supervisor") == "p":
        return False
    env_value = os.getenv("AUTONOMY_ROUTER_SNAPSHOT_ENABLED")
    if env_value is not None:
        return env_value.strip().lower() in _TRUTHY
    return get_infrastructure_config().autonomy_router_snapshot_enabled


def get_autonomy_cloud_worker_runtime() -> str | None:
    """OS-isolated runtime id requested for cloud Workers; None means off.

    ``AUTONOMY_CLOUD_WORKER_RUNTIME`` wins over config.yaml so one host can
    request or clear a runtime without editing the committed deployment file.
    A set-but-empty environment value explicitly means off.
    """
    env_value = os.getenv("AUTONOMY_CLOUD_WORKER_RUNTIME")
    if env_value is not None:
        return env_value.strip() or None
    value = get_infrastructure_config().autonomy_cloud_worker_runtime
    return value.strip() or None if value else None


def get_autonomy_stop_ack_timeout() -> float:
    """Seconds to wait for a native daemon to verify that a stop took effect."""
    return get_infrastructure_config().autonomy_stop_ack_timeout_seconds


def get_native_runtime_usage_headroom_config() -> UsageHeadroomConfig:
    """Typed operator settings for the optional local usage snapshot reader."""
    return get_infrastructure_config().native_runtime.usage_headroom


def get_agent_fleet_config() -> AgentFleetConfig:
    """Typed settings for the read-only agent fleet dashboard.

    Three environment overrides exist because the fleet is machine-shaped: an
    operator tuning a stall threshold on one host should not have to edit a
    committed config file, and a host whose worktrees live elsewhere, or whose
    mainline is named differently, needs a different root or base ref without a
    config overlay.
    """
    config = get_infrastructure_config().agent_fleet
    stall = os.environ.get("AGENT_STALL_MINS", "").strip()
    root = os.environ.get("AGENT_FLEET_ROOT", "").strip()
    base_ref = os.environ.get("AGENT_FLEET_BASE_REF", "").strip()
    if not stall and not root and not base_ref:
        return config
    overrides: dict[str, Any] = {}
    if stall:
        try:
            minutes = int(stall)
        except ValueError:
            logger.warning("ignoring invalid AGENT_STALL_MINS: %s", stall)
        else:
            overrides["stall_mins"] = max(1, min(1440, minutes))
    if root:
        overrides["root"] = root
    if base_ref:
        overrides["base_ref"] = base_ref[:200]
    return config.model_copy(update=overrides)


def get_review_merge_config() -> ReviewMergeConfig:
    """Typed settings for the host-local review and merge API.

    The worktree root is machine-shaped the same way the fleet root is: the
    CLI already honours ``CONDUCTOR_WT_ROOT`` for its own line resolution, so
    the server reads the same variable to stay on the same root, and falls
    back to the committed ``review_merge.worktree_root``. Values are read at
    call time so a test or a long-running server can point at another root
    without a restart; an invalid timeout is clamped, never raised, because
    this runs on a request path.
    """
    config = get_infrastructure_config().review_merge
    overrides: dict[str, Any] = {}
    root = os.environ.get("CONDUCTOR_WT_ROOT", "").strip()
    if root:
        overrides["worktree_root"] = root
    command = os.environ.get("CONDUCTOR_MERGE_TEST_CMD", "").strip()
    if command:
        overrides["test_command"] = command[:4096]
    timeout = os.environ.get("CONDUCTOR_MERGE_TEST_TIMEOUT_SECONDS", "").strip()
    if timeout:
        try:
            seconds = float(timeout)
        except ValueError:
            logger.warning(
                "ignoring invalid CONDUCTOR_MERGE_TEST_TIMEOUT_SECONDS: %s", timeout
            )
        else:
            overrides["test_timeout_seconds"] = max(1.0, min(7200.0, seconds))
    codex_command = os.environ.get("CONDUCTOR_CODEX_COMMAND", "").strip()
    if codex_command:
        overrides["codex_command"] = codex_command[:4096]
    codex_timeout = os.environ.get("CONDUCTOR_CODEX_TIMEOUT_SECONDS", "").strip()
    if codex_timeout:
        try:
            seconds = float(codex_timeout)
        except ValueError:
            logger.warning(
                "ignoring invalid CONDUCTOR_CODEX_TIMEOUT_SECONDS: %s", codex_timeout
            )
        else:
            overrides["codex_timeout_seconds"] = max(1.0, min(3600.0, seconds))
    local_hosts = os.environ.get("CONDUCTOR_MERGE_LOCAL_HOSTS", "").strip()
    if local_hosts:
        # Validate through the model below, so a typo in an operator env var
        # answers the same way a bad config.yaml entry does instead of
        # opening the gate with a malformed host.
        parsed = [part.strip().lower() for part in local_hosts.split(",") if part.strip()]
        if parsed:
            overrides["local_hosts"] = parsed
    if not overrides:
        return config
    try:
        return ReviewMergeConfig(**{**config.model_dump(), **overrides})
    except ValueError:
        logger.warning("ignoring invalid review_merge environment overrides")
        return config


def get_native_runtime_routing_config() -> NativeRuntimeRoutingConfig:
    """Provider order and fallback-only rules for automatic native routing."""
    return get_infrastructure_config().native_runtime_routing


# =============================================================================
# Feature Flags
# =============================================================================




def is_result_log_db_enabled() -> bool:
    return get_infrastructure_config().result_log_db_enabled


def is_redis_warm_on_startup_enabled() -> bool:
    return get_infrastructure_config().redis_warm_on_startup



def is_langsmith_tracing_enabled() -> bool:
    return get_infrastructure_config().langsmith_tracing


# =============================================================================
# SSE Event Logging
# =============================================================================


def is_sse_event_log_enabled() -> bool:
    return get_infrastructure_config().sse_event_log_enabled


def get_sse_event_log_level() -> str:
    return get_infrastructure_config().sse_event_log_level


# =============================================================================
# General Application Logging
# =============================================================================


def get_log_level() -> str:
    return get_infrastructure_config().log_level.upper()


def get_log_format() -> str:
    return get_infrastructure_config().log_format


def get_module_log_levels() -> dict:
    levels = get_infrastructure_config().module_log_levels
    return {k: v.upper() for k, v in levels.items()} if levels else {}


# =============================================================================
# CORS Settings
# =============================================================================


def get_allowed_origins() -> List[str]:
    """CORS origins, with ``CONDUCTOR_CORS_ORIGINS`` as a deployment override.

    A blank or comma-only value is treated as unset so a shell that exports an
    empty variable keeps the configured list rather than silently closing CORS.

    The app allows credentials, so a wildcard origin would let any site make
    authenticated requests. ``*`` is refused with a warning: an override
    that contains it is ignored, and a configured ``*`` is dropped (the
    built-in local console origins are used if nothing else is left).
    """
    raw = os.getenv("CONDUCTOR_CORS_ORIGINS", "")
    if raw.strip():
        origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
        if origins and "*" in origins:
            logger.warning(
                "CONDUCTOR_CORS_ORIGINS contains '*', which is refused because "
                "CORS allows credentials; using the configured origins"
            )
        elif origins:
            return origins
    configured = list(get_infrastructure_config().allowed_origins)
    if "*" not in configured:
        return configured
    logger.warning(
        "allowed_origins contains '*', which is refused because CORS allows "
        "credentials; dropping it"
    )
    kept = [origin for origin in configured if origin != "*"]
    if kept:
        return kept
    return list(InfrastructureConfig().allowed_origins)


# =============================================================================
# Locale and Timezone Configuration
# =============================================================================


def get_locale_config(locale: str, prompt_language: str) -> Dict[str, str]:
    """Get locale-specific timezone configuration."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from src.utils.timezone_utils import get_timezone_label

    locale_lower = locale.lower() if locale else ""

    if locale_lower == "en-us":
        timezone = "America/New_York"
    elif locale_lower == "zh-cn":
        timezone = "Asia/Shanghai"
    else:
        timezone = "UTC"

    tz = ZoneInfo(timezone)
    current_time = datetime.now(tz)
    timezone_label = get_timezone_label(current_time)

    return {
        "locale": locale,
        "prompt_language": prompt_language,
        "timezone": timezone,
        "timezone_label": timezone_label,
    }


# =============================================================================
# Redis Configuration
# =============================================================================


def is_redis_cache_enabled() -> bool:
    return get_infrastructure_config().redis.cache_enabled



def get_redis_max_connections() -> int:
    """Get Redis connection pool max connections.

    Env var REDIS_MAX_CONNECTIONS overrides config.yaml so operators can
    bump the pool size without a redeploy when diagnosing pool exhaustion.
    """
    env_override = os.getenv("REDIS_MAX_CONNECTIONS")
    if env_override:
        try:
            parsed = int(env_override)
        except ValueError:
            # A typo during incident response would otherwise silently ignore
            # the operator's intent and use the YAML default — log loudly.
            logger.warning(
                "REDIS_MAX_CONNECTIONS=%r is not an integer; "
                "falling back to config.yaml value",
                env_override,
            )
        else:
            # Bound-check: 0 is a footgun (redis-py silently coerces to a
            # 31-bit cap), negatives break the pool, huge values exhaust fds.
            if 1 <= parsed <= 10000:
                return parsed
            logger.warning(
                "REDIS_MAX_CONNECTIONS=%d is outside the safe range [1, 10000]; "
                "falling back to config.yaml value",
                parsed,
            )
    return get_infrastructure_config().redis.max_connections


def get_redis_stream_max_connections() -> int:
    """Pool size for blocking stream readers. Env REDIS_STREAM_MAX_CONNECTIONS."""
    return _env_pool_size(
        "REDIS_STREAM_MAX_CONNECTIONS",
        default=get_infrastructure_config().redis.stream_max_connections,
        ceiling=10000,
    )


def get_redis_pubsub_max_connections() -> int:
    """Pool size for long-lived subscriptions. Env REDIS_PUBSUB_MAX_CONNECTIONS."""
    return _env_pool_size(
        "REDIS_PUBSUB_MAX_CONNECTIONS",
        default=get_infrastructure_config().redis.pubsub_max_connections,
        ceiling=10000,
    )


def get_redis_pool_timeout() -> float:
    """Seconds a caller queues for a free cache connection. Env REDIS_POOL_TIMEOUT.

    Converts "no slot right now" from an instant error that kills a turn into
    a short wait — safe only because every long-held connection moved off the
    cache pool.
    """
    raw = os.getenv("REDIS_POOL_TIMEOUT")
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            logger.warning("REDIS_POOL_TIMEOUT=%r is not a number; using config", raw)
        else:
            if 0 < parsed <= 60:
                return parsed
            logger.warning(
                "REDIS_POOL_TIMEOUT=%s outside (0, 60]; using config", parsed
            )
    return get_infrastructure_config().redis.pool_timeout


def get_redis_socket_timeout() -> int:
    """Socket read/write timeout in seconds."""
    return get_infrastructure_config().redis.socket_timeout


def _env_pool_size(env_var: str, default: int, ceiling: int = 500) -> int:
    """Read a pool-size env var with bounds check; fall back to `default`."""
    raw = os.getenv(env_var)
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using default=%d", env_var, raw, default)
        return default
    if 1 <= parsed <= ceiling:
        return parsed
    logger.warning("%s=%d outside [1, %d]; using default=%d", env_var, parsed, ceiling, default)
    return default


def get_checkpointer_pool_max() -> int:
    """Env POSTGRES_CHECKPOINTER_POOL_MAX, default 25."""
    return _env_pool_size("POSTGRES_CHECKPOINTER_POOL_MAX", default=25)


def get_conversation_pool_max() -> int:
    """Env POSTGRES_CONVERSATION_POOL_MAX, default 50."""
    return _env_pool_size("POSTGRES_CONVERSATION_POOL_MAX", default=50)


def get_writer_pool_max() -> int:
    """Env POSTGRES_WRITER_POOL_MAX, default 25.

    The pinned-session budget: one physical connection is held per live run
    (WriterGuard), so this IS the effective concurrent-run cap when the
    guard is active. Exhaustion is a bounded 503, never an unfenced run.
    """
    return _env_pool_size("POSTGRES_WRITER_POOL_MAX", default=25)


def get_recovery_scan_interval() -> float:
    """Env RECOVERY_SCAN_INTERVAL seconds, default 30. Bounds how long a
    dead worker's orphaned runs stay in_progress before another worker's
    scanner finalizes them."""
    raw = os.getenv("RECOVERY_SCAN_INTERVAL", "30")
    try:
        value = float(raw)
    except ValueError:
        return 30.0
    # isfinite: "Infinity" parses as a float > 0 but would silently disable
    # scanning forever.
    return value if math.isfinite(value) and value > 0 else 30.0


def get_redis_socket_connect_timeout() -> int:
    return get_infrastructure_config().redis.socket_connect_timeout


def get_redis_ttl_results_list() -> int:
    return get_infrastructure_config().redis.ttl.results_list


def get_redis_ttl_result_detail() -> int:
    return get_infrastructure_config().redis.ttl.result_detail


def get_redis_ttl_metadata() -> int:
    return get_infrastructure_config().redis.ttl.metadata


def get_redis_ttl_metadata_summary() -> int:
    return get_infrastructure_config().redis.ttl.metadata_summary


def get_redis_ttl_workflow_status() -> int:
    return get_infrastructure_config().redis.ttl.workflow_status


def get_redis_ttl_cancel_flag() -> int:
    return get_infrastructure_config().redis.ttl.cancel_flag


def get_redis_ttl_memo_metadata_inflight() -> int:
    """Cross-worker visibility key TTL for in-flight memo metadata tasks (seconds)."""
    return get_infrastructure_config().redis.ttl.memo_metadata_inflight


def get_redis_ttl_memo_metadata_cancel() -> int:
    """Cooperative cross-worker memo metadata cancel flag TTL (seconds)."""
    return get_infrastructure_config().redis.ttl.memo_metadata_cancel


def is_cache_invalidate_on_write_enabled() -> bool:
    return get_infrastructure_config().redis.cache_invalidate_on_write


# =============================================================================
# Background Execution Configuration
# =============================================================================


def get_max_concurrent_workflows() -> int:
    return get_infrastructure_config().background_execution.max_concurrent_workflows


def get_workflow_result_ttl() -> int:
    return get_infrastructure_config().background_execution.workflow_result_ttl


def get_abandoned_workflow_timeout() -> int:
    """Timeout in seconds for workflows with no active connections."""
    return get_infrastructure_config().background_execution.abandoned_workflow_timeout


def get_cleanup_interval() -> int:
    return get_infrastructure_config().background_execution.cleanup_interval


def is_intermediate_storage_enabled() -> bool:
    return get_infrastructure_config().background_execution.enable_intermediate_storage


def get_max_stored_messages_per_agent() -> int:
    return get_infrastructure_config().background_execution.max_stored_messages_per_agent


def get_max_stream_bytes_per_run() -> int:
    return get_infrastructure_config().background_execution.max_stream_bytes_per_run


def get_subagent_collector_timeout() -> float:
    return get_infrastructure_config().background_execution.subagent_collector_timeout


def get_subagent_orphan_collector_timeout() -> float:
    """Orphan collector idle timeout; resets on any subagent progress."""
    return get_infrastructure_config().background_execution.subagent_orphan_collector_timeout


def get_event_storage_backend() -> str:
    return get_infrastructure_config().background_execution.event_storage_backend


def get_subagent_task_max_wait() -> int:
    return get_infrastructure_config().background_execution.subagent_task_max_wait


def get_in_memory_event_tail_max_events() -> int:
    """Max captured-event records held in the per-subagent in-memory hot tail."""
    return get_infrastructure_config().background_execution.in_memory_event_tail_max_events


def is_subagent_event_redis_spill_enabled() -> bool:
    """Kill-switch for the per-event Redis spill of subagent captured events."""
    return get_infrastructure_config().background_execution.spill_subagent_events_to_redis


def get_sse_drain_timeout() -> float:
    return get_infrastructure_config().background_execution.sse_drain_timeout


def get_shutdown_timeout() -> float:
    return get_infrastructure_config().background_execution.shutdown_timeout


def get_checkpoint_flush_timeout() -> float:
    return get_infrastructure_config().background_execution.checkpoint_flush_timeout


def get_admission_compaction_wait_timeout() -> float:
    return get_infrastructure_config().background_execution.admission_compaction_wait_timeout


def get_compaction_timeout() -> float:
    return get_infrastructure_config().background_execution.compaction_timeout


def get_wait_for_persistence_timeout() -> float:
    return get_infrastructure_config().background_execution.wait_for_persistence_timeout


def get_stop_drain_timeout() -> float:
    return get_infrastructure_config().background_execution.stop_drain_timeout


def get_max_workflow_retries() -> int:
    return get_infrastructure_config().background_execution.max_workflow_retries


def get_redis_ttl_workflow_events() -> int:
    return get_infrastructure_config().redis.ttl.workflow_events


# =============================================================================
# Nested config accessor — kept for remaining callers
# =============================================================================


def get_nested_config(
    key_path: str, default: Any = None, config_path: Optional[Any] = None
) -> Any:
    """Dot-notation config lookup; kept for backward-compat callers."""
    cfg = get_infrastructure_config()
    # Walk the pydantic model using getattr for typed access
    keys = key_path.split(".")
    value: Any = cfg
    for key in keys:
        if isinstance(value, dict):
            if key in value:
                value = value[key]
            else:
                return default
        elif hasattr(value, key):
            value = getattr(value, key)
        else:
            return default
    return value


def get_config(
    key: str, default: Any = None, config_path: Optional[Any] = None
) -> Any:
    """Top-level config lookup; kept for backward compatibility."""
    return get_nested_config(key, default)


# =============================================================================
# LangSmith Tracing Configuration
# =============================================================================


def get_langsmith_tags(locale: Optional[str] = None) -> List[str]:
    """Build LangSmith tags list for a chat turn."""
    tags = ["workflow:chat"]
    if locale:
        tags.append(f"locale:{locale}")

    return tags


def get_langsmith_metadata(
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    workflow_type: Optional[str] = None,
    locale: Optional[str] = None,
    timezone: Optional[str] = None,
    llm_model: Optional[str] = None,
    platform: Optional[str] = None,
) -> Dict[str, Any]:
    """Build LangSmith metadata dict for a workflow run (omits None values).

    Turn identity only. How the model was tuned for a call — effort, guidance,
    compaction preset, billing — is stamped on the client in ``LLM.get_llm``
    and lands on the LLM run, because a subagent inherits this dict wholesale
    while running a different model. ``llm_model`` stays as the turn's own
    selection, which is a different question from what any one call hit.
    """
    metadata = {}

    if user_id:
        metadata["user_id"] = user_id
    if workspace_id:
        metadata["workspace_id"] = workspace_id
    if thread_id:
        metadata["thread_id"] = thread_id
    if workflow_type:
        metadata["workflow_type"] = workflow_type
    if locale:
        metadata["locale"] = locale
    if timezone:
        metadata["timezone"] = timezone
    if llm_model:
        metadata["llm_model"] = llm_model
    if platform:
        metadata["platform"] = platform

    return metadata
