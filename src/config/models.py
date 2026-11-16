"""
Pydantic models for infrastructure configuration.

These models define the schema for config.yaml (infrastructure settings).
"""

import logging
import os
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from src.utils.provider_usage import (
    DEFAULT_LOW_BALANCE_THRESHOLDS,
    normalize_low_balance_thresholds,
)

logger = logging.getLogger(__name__)

# Operator worktree directory. Codex reads the same environment variable.
WORKTREE_ROOT_DEFAULT = "~/.conductor/worktrees"


def conductor_worktree_root() -> str:
    """Return the worktree root.

    ``CONDUCTOR_WORKTREE_ROOT`` wins. ``CONDUCTOR_WT_ROOT`` is the legacy alias.
    The default is ``~/.conductor/worktrees``.
    """
    for name in ("CONDUCTOR_WORKTREE_ROOT", "CONDUCTOR_WT_ROOT"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return WORKTREE_ROOT_DEFAULT


class BackgroundExecutionConfig(BaseModel):
    """Configuration for background workflow execution."""

    max_concurrent_workflows: int = Field(
        default=100, description="Maximum number of concurrent background workflows"
    )
    workflow_result_ttl: int = Field(
        default=86400, description="Workflow result retention time in seconds (24 hours)"
    )
    abandoned_workflow_timeout: int = Field(
        default=3600,
        description="Auto-cleanup timeout for workflows with no active connections (1 hour)",
    )
    cleanup_interval: int = Field(
        default=300, description="Background cleanup task interval in seconds (5 minutes)"
    )
    enable_intermediate_storage: bool = Field(
        default=True, description="Store intermediate results during execution"
    )
    max_stored_messages_per_agent: int = Field(
        default=150000, description="Maximum events to buffer per workflow"
    )
    max_stream_bytes_per_run: int = Field(
        default=96 * 1024 * 1024,
        description=(
            "Maximum bytes one run's event stream may hold in Redis, counting "
            "every copy a frame is kept as (a subagent's task stream keeps up "
            "to three); a run past it finalizes error(transport_lost), as at "
            "the event cap"
        ),
    )
    event_storage_backend: Literal["redis", "memory"] = Field(
        default="redis", description='Backend for event buffering: "redis" or "memory"'
    )
    subagent_collector_timeout: float = Field(
        default=120, description="Initial subagent collector timeout in seconds"
    )
    subagent_orphan_collector_timeout: float = Field(
        default=600, description="Orphan subagent collector idle timeout in seconds"
    )

    # Streaming & queue settings
    subagent_task_max_wait: int = Field(
        default=30, description="Max seconds to wait for subagent task to appear in registry"
    )

    # Subagent event capture
    in_memory_event_tail_max_events: int = Field(
        default=1000,
        description=(
            "Max captured-event records held in the in-memory hot tail per subagent task. "
            "Older events spill to Redis."
        ),
    )
    spill_subagent_events_to_redis: bool = Field(
        default=True,
        description=(
            "Kill-switch: when True, subagent events are also spilled to Redis so "
            "the in-memory tail stays bounded without losing events for reconnect/persistence."
        ),
    )

    # Timeout settings
    sse_drain_timeout: float = Field(
        default=30.0, description="Seconds to wait for per-task SSE drain before clearing events"
    )
    shutdown_timeout: float = Field(
        default=50.0, description="Max seconds for graceful shutdown of running workflows"
    )
    checkpoint_flush_timeout: float = Field(
        default=10.0, description="Timeout (seconds) for checkpoint state reads/writes"
    )
    admission_compaction_wait_timeout: float = Field(
        default=260.0,
        gt=0,
        description=(
            "Max seconds a new turn blocks at admission for an in-progress "
            "compaction (auto Tier-2 summarize or manual /compact|/offload) to "
            "finish before returning 'compacting' -> 409 retry. Summarize is a "
            "full LLM call over a large transcript and can run minutes, so this "
            "is generous to avoid spurious 409s; keep it under the governing "
            "in-request limits — the upstream nginx proxy_read_timeout and the "
            "client read timeout (the connection is held this long while the "
            "turn waits). NOTE: timeout_keep_alive (300s) is NOT the bound; it "
            "only caps idle time BETWEEN requests, not an in-flight request. "
            "Admission floors this at compaction_timeout + a small margin so a "
            "healthy in-progress compaction is never 409'd before its own call "
            "budget self-terminates and releases the guard."
        ),
    )
    compaction_timeout: float = Field(
        default=240.0,
        gt=0,
        description=(
            "Wall-clock budget (seconds) for a single compaction LLM call "
            "(auto Tier-2 summarize and manual /compact). The call is wrapped "
            "in asyncio.wait_for so a hung summarize fails naturally — auto "
            "emits a 'summarize error' (closing the window), manual raises -> "
            "HTTP 500 — instead of blocking the thread forever. Bounded below "
            "the httpx client timeout (600s) and below admission_compaction_"
            "wait_timeout so the call self-terminates before admission 409s."
        ),
    )
    wait_for_persistence_timeout: float = Field(
        default=30.0, description="Max seconds callers block waiting for persistence completion"
    )
    stop_drain_timeout: float = Field(
        default=1.5,
        description="Max seconds to drain killed-subagent events before the stop teardown sentinel",
    )
    max_workflow_retries: int = Field(
        default=3, description="Max transient-error retry count for workflow execution"
    )


class RedisTTLConfig(BaseModel):
    """Redis TTL settings for various cache types."""

    results_list: int = Field(default=300, description="Results list cache TTL (5 minutes)")
    result_detail: int = Field(default=900, description="Result detail cache TTL (15 minutes)")
    metadata: int = Field(default=900, description="Metadata tags cache TTL (15 minutes)")
    metadata_summary: int = Field(
        default=600, description="Metadata summary cache TTL (10 minutes)"
    )
    workflow_events: int = Field(
        default=86400, description="Workflow event buffer TTL (24 hours)"
    )
    workflow_status: int = Field(
        default=3600, description="TTL for completed/cancelled workflow status keys (1 hour)"
    )
    cancel_flag: int = Field(
        default=300, description="TTL for workflow cancel flag (5 minutes)"
    )
    memo_metadata_inflight: int = Field(
        default=300,
        description=(
            "TTL for the cross-worker visibility key marking a memo metadata "
            "task as in flight (5 minutes)"
        ),
    )
    memo_metadata_cancel: int = Field(
        default=60,
        description=(
            "TTL for the cooperative cross-worker memo metadata cancel flag (1 minute)"
        ),
    )


class RedisSWRConfig(BaseModel):
    """Stale-While-Revalidate configuration for Redis cache."""

    enabled: bool = Field(default=True, description="Enable SWR for cache reads")
    soft_ttl_ratio: float = Field(
        default=0.6,
        description="Refresh when remaining TTL < this ratio of original",
    )
    warm_after_invalidation: bool = Field(
        default=True, description="Pre-populate cache after invalidation"
    )


class RedisConfig(BaseModel):
    """Redis cache configuration."""

    cache_enabled: bool = Field(default=True, description="Enable/disable caching globally")
    max_connections: int = Field(default=10, description="Connection pool size")
    # Pools are split by connection LIFETIME, not by feature: short cache ops
    # must never queue behind a stream reader parked in XREAD BLOCK, and a
    # subscriber holding a connection for 30 minutes must not sit in either.
    stream_max_connections: int = Field(
        default=100,
        ge=1,
        le=10000,
        description="Pool size for blocking stream readers (XREAD)",
    )
    pubsub_max_connections: int = Field(
        default=150,
        ge=1,
        le=10000,
        description="Pool size for long-lived pub/sub subscriptions",
    )
    pool_timeout: float = Field(
        default=2.0,
        gt=0,
        le=60,
        description="Seconds a caller queues for a free cache connection before failing",
    )
    socket_timeout: int = Field(
        default=5, description="Redis socket read/write timeout in seconds"
    )
    socket_connect_timeout: int = Field(
        default=5, description="Redis socket connect timeout in seconds"
    )
    ttl: RedisTTLConfig = Field(default_factory=RedisTTLConfig)
    cache_invalidate_on_write: bool = Field(
        default=True, description="Invalidate cache on writes"
    )
    swr: RedisSWRConfig = Field(default_factory=RedisSWRConfig)


class FeatureFlagOverride(BaseModel):
    """Deployment override for a code-declared feature (src/config/features.py).

    Unset fields inherit the catalog defaults, so a config.yaml entry only
    needs the fields it wants to change.
    """

    enabled: Optional[bool] = Field(
        default=None, description="Kill switch: false turns the feature off for everyone"
    )
    gate: Optional[Literal["none", "opt_in", "opt_out", "plan"]] = Field(
        default=None,
        description=(
            "Access model while enabled: everyone / user opt-in / user opt-out "
            "/ platform plan tier"
        ),
    )
    min_tier: Optional[int] = Field(
        default=None, description="Plan gate only: minimum platform access tier"
    )


class WorkflowOrchestrationConfig(BaseModel):
    """Caps and timeouts for RunWorkflow programmatic subagent runs."""

    enabled: bool = Field(
        default=True,
        description="Expose the RunWorkflow tool to the PTC main agent",
    )
    memory_limit_mb: int = Field(
        default=128,
        ge=16,
        le=1024,
        description="QuickJS heap limit per workflow run (MB)",
    )
    cpu_budget_s: float = Field(
        default=30.0,
        gt=0,
        le=600,
        description="Continuous-JS interrupt budget in seconds (host awaits excluded)",
    )
    schema_max_bytes: int = Field(
        default=4096,
        ge=256,
        le=65536,
        description="Max serialized size of a dispatch schema",
    )
    schema_max_depth: int = Field(
        default=5,
        ge=1,
        le=16,
        description="Max nesting depth of a dispatch schema",
    )
    schema_max_properties: int = Field(
        default=32,
        ge=1,
        le=256,
        description="Max keys at any schema object level",
    )
    max_dispatches_per_run: int = Field(
        default=64, ge=1, le=256, description="Max subagent dispatches per run"
    )
    max_concurrent_children: int = Field(
        default=8, ge=1, le=32, description="Max concurrently running children"
    )
    child_timeout: int = Field(
        default=1800, ge=1, le=7200, description="Per-child subagent timeout in seconds"
    )
    run_timeout: int = Field(
        default=16200,
        ge=1,
        le=86400,
        description=(
            "Whole-run wall-clock timeout in seconds; must exceed "
            "child_timeout or the run dies before any child of it can. The "
            "default clears several sequential waves of children"
        ),
    )
    max_runs_per_thread: int = Field(
        default=2, ge=1, le=8, description="Max concurrently active runs per thread"
    )
    max_result_bytes: int = Field(
        default=1024 * 1024,
        ge=1024,
        le=16 * 1024 * 1024,
        description=(
            "Per-child result dump cap; bounds what the script manipulates in "
            "JS, not what any reader is handed"
        ),
    )
    max_summary_bytes: int = Field(
        default=96 * 1024,
        ge=1024,
        le=1024 * 1024,
        description=(
            "Cap on the run summary — the text handed to the model and "
            "archived for TaskOutput; the full value stays in result.json"
        ),
    )
    max_prompt_chars: int = Field(
        default=400_000,
        ge=1,
        le=1_000_000,
        description="Per-dispatch prompt length cap",
    )
    max_script_bytes: int = Field(
        default=200 * 1024,
        ge=1024,
        le=4 * 1024 * 1024,
        description="Workflow script size cap in bytes",
    )

    @model_validator(mode="after")
    def _run_timeout_outlasts_one_child(self) -> "WorkflowOrchestrationConfig":
        """Reject a run timeout no child can time out inside.

        At or below one ``child_timeout`` the run-level kill always beats the
        per-child one, so every timeout is reported as the blunt whole-run
        failure and no child is ever named. The bound stops at one child on
        purpose: covering a worst-case wave would assume every child burns its
        full timeout with none overlapping, which is a property of the script
        rather than of the configuration — and asserting it here left in-range
        settings (``max_concurrent_children: 1``) unsatisfiable at every
        ``run_timeout``. The shipped default still carries several waves of
        margin, as a recommendation rather than a rule.
        """
        if self.run_timeout <= self.child_timeout:
            raise ValueError(
                f"workflow.run_timeout={self.run_timeout} must exceed "
                f"child_timeout={self.child_timeout}, or the run dies before "
                "any child of it can"
            )
        return self


class PromptConfig(BaseModel):
    """Deployment-wide prompt settings."""

    guidance: Literal["auto", "lean", "detailed"] = Field(
        default="auto",
        description=(
            "Prompt scaffolding level for this deployment. 'auto' defers to the "
            "model's prompt_guidance in models.json; 'lean'/'detailed' pin it. A "
            "per-user model_preference.prompt_guidance still wins over this — a "
            "profile for the running model first, then the account-wide value."
        ),
    )


class UsageHeadroomConfig(BaseModel):
    """Optional local snapshot input for automatic native-provider routing."""

    enabled: bool = Field(default=False, description="Use a local CodexBar snapshot for automatic routing")
    snapshot_path: Optional[str] = Field(
        default=None,
        max_length=4096,
        description="Explicit path to a sanitized CodexBar snapshot; no path discovery is performed",
    )
    max_age_seconds: int = Field(default=900, ge=1, le=604800)
    max_bytes: int = Field(default=262144, ge=256, le=1048576)
    low_balance_thresholds: Dict[str, Dict[str, float]] = Field(
        default_factory=lambda: {
            provider: dict(currencies)
            for provider, currencies in DEFAULT_LOW_BALANCE_THRESHOLDS.items()
        },
        description="Low-balance thresholds by provider and currency; no conversion is applied",
    )

    @field_validator("low_balance_thresholds", mode="before")
    @classmethod
    def _validate_low_balance_thresholds(
        cls, value: Dict[str, Dict[str, float]]
    ) -> Dict[str, Dict[str, float]]:
        return normalize_low_balance_thresholds(value)


class NativeRuntimeConfig(BaseModel):
    """Deployment controls for native runtime selection."""

    usage_headroom: UsageHeadroomConfig = Field(default_factory=UsageHeadroomConfig)


class AgentFleetConfig(BaseModel):
    """Read-only discovery of the local agent worktree fleet for the dashboard.

    The scan only reads: process table, git worktrees, and the log files the
    agent CLIs already write. It never writes to a worktree, and no field here
    grants access to anything.
    """

    enabled: bool = Field(
        default=True,
        description="Serve the read-only agent fleet dashboard endpoints",
    )
    root: str = Field(
        default="~/.conductor/worktrees",
        min_length=1,
        max_length=4096,
        description="Directory holding the agent worktrees (CONDUCTOR_WORKTREE_ROOT)",
    )
    logs_dir: str = Field(
        default="",
        max_length=4096,
        description="Directory holding per-agent logs; empty means <root>/logs",
    )
    prompts_dir: str = Field(
        default="",
        max_length=4096,
        description="Directory holding per-agent prompts; empty means <root>/prompts",
    )
    stall_mins: int = Field(
        default=5,
        ge=1,
        le=1440,
        description="Minutes without log growth after which a live agent reads as stalled",
    )
    base_ref: str = Field(
        default="main",
        max_length=200,
        description="Ref the fleet's branches are compared against for commits ahead",
    )
    log_tail_bytes: int = Field(
        default=4096,
        ge=256,
        le=65536,
        description="Bytes read from the end of each agent log",
    )
    max_worktrees: int = Field(default=64, ge=1, le=512)
    command_timeout_seconds: float = Field(default=5.0, ge=0.5, le=60.0)


class ReviewMergeConfig(BaseModel):
    """Host-local review and merge API: where the line worktrees live.

    Merge executes ``git`` and a test command on this machine, so the paths
    here are operator-controlled and a request names a *line* inside
    ``worktree_root``, never a filesystem path. The root is deliberately
    separate from the agent fleet scan: discovery may be disabled while the
    interactive review/merge surface keeps working.
    """

    worktree_root: str = Field(
        default="~/.conductor/worktrees",
        min_length=1,
        max_length=4096,
        description=(
            "Directory holding one git worktree per line. A merge request can "
            "only name lines directly under this root."
        ),
    )
    test_command: str = Field(
        default="",
        max_length=4096,
        description=(
            "Default test command run in the temporary merge worktree. Empty "
            "means the line worktree's .venv interpreter when present, else "
            "the server interpreter running pytest."
        ),
    )
    test_timeout_seconds: float = Field(
        default=3600.0,
        ge=1.0,
        le=7200.0,
        description=(
            "Hard cap on the test run executed in the temporary merge worktree. "
            "Test + Codex timeouts must stay below the CLI's merge wait."
        ),
    )
    output_tail_chars: int = Field(
        default=2000,
        ge=0,
        le=20000,
        description=(
            "Upper bound on the credential-scrubbed failing-output tail returned "
            "to the caller; 0 means no truncation (the operator's explicit "
            "choice). Raw process output is never written to the audit table."
        ),
    )
    codex_command: str = Field(
        default="codex",
        min_length=1,
        max_length=4096,
        description=(
            "Executable used for the mandatory Codex review before a merge "
            "lands. The server appends 'exec --sandbox read-only ...'. Empty "
            "would disable the gate, so it is rejected."
        ),
    )
    codex_timeout_seconds: float = Field(
        default=900.0,
        ge=1.0,
        le=3600.0,
        description="Hard cap on the merge-time Codex review.",
    )
    local_hosts: List[str] = Field(
        default_factory=lambda: ["127.0.0.1", "::1", "localhost"],
        description=(
            "Host-header names a merge request may arrive under. The transport "
            "peer must still be loopback and forwarding headers are refused; "
            "adding a local host name is the explicit local permission an "
            "operator can grant to a same-host front end."
        ),
    )

    @field_validator("local_hosts")
    @classmethod
    def _normalize_local_hosts(cls, value: List[str]) -> List[str]:
        hosts: List[str] = []
        for raw in value:
            host = str(raw or "").strip().lower().rstrip(".")
            if (
                not host
                or "/" in host
                or " " in host
                or "@" in host
                or "[" in host
                or "]" in host
                or any(ord(char) < 0x20 for char in host)
            ):
                raise ValueError("local host entries must be bare host names")
            if len(host) > 255:
                raise ValueError("local host entries are limited to 255 characters")
            hosts.append(host)
        if not hosts:
            raise ValueError("at least one local host is required")
        return hosts


class NativeRuntimeRoutingConfig(BaseModel):
    """Default order and fallback barriers for native-runtime selection."""

    preferred_order: List[str] = Field(
        default_factory=lambda: [
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
            "cursor",
        ],
        description="Provider preference order for automatic native routing",
    )
    fallback_only: List[str] = Field(
        default_factory=lambda: ["claude-code"],
        description="Providers considered only after regular candidates",
    )
    disabled_providers: List[str] = Field(
        default_factory=list,
        description=(
            "Native runtimes blocked by operator policy; removing a provider is "
            "an explicit opt-in, but does not establish live availability"
        ),
    )


class MemoryFederationUserBinding(BaseModel):
    """Explicit local sources this user may include in federated recall."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    memfw_path: str | None = Field(
        default=None,
        description="Absolute path to a local MemFW repository; no default is inferred.",
    )
    memfw_url: str | None = Field(
        default=None,
        description="Read-only MemFW HTTP endpoint; mutually exclusive with memfw_path.",
    )
    memfw_tenant: str | None = Field(
        default=None,
        description="Tenant sent to the MemFW HTTP endpoint; only valid with memfw_url.",
    )

    @field_validator("memfw_path")
    @classmethod
    def _absolute_memfw_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from pathlib import Path

        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("memory federation source paths must be absolute")
        return str(path)

    @field_validator("memfw_url")
    @classmethod
    def _valid_memfw_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("memory federation URL must use HTTP(S)")
        return value.rstrip("/")

    @field_validator("memfw_tenant")
    @classmethod
    def _bounded_memfw_tenant(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > 120:
            raise ValueError("memory federation tenant must be at most 120 characters")
        return value.strip() or None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "MemoryFederationUserBinding":
        if bool(self.memfw_path) == bool(self.memfw_url):
            raise ValueError("memory federation binding needs exactly one of memfw_path/memfw_url")
        return self

    @model_validator(mode="after")
    def _tenant_requires_url(self) -> "MemoryFederationUserBinding":
        if self.memfw_tenant is not None and not self.memfw_url:
            raise ValueError("memory federation tenant requires memfw_url")
        return self


class MemoryFederationConfig(BaseModel):
    """Per-user source bindings for opt-in memory federation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_bindings: Dict[str, MemoryFederationUserBinding] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_cross_user_repository_sharing(self) -> "MemoryFederationConfig":
        import ipaddress
        from pathlib import Path
        from urllib.parse import urlsplit

        owners_by_path: dict[str, str] = {}
        for user_id, binding in self.user_bindings.items():
            if binding.memfw_path is not None:
                canonical = str(Path(binding.memfw_path).resolve(strict=False))
            else:
                raw_url = str(binding.memfw_url or "")
                try:
                    parts = urlsplit(raw_url)
                    scheme = parts.scheme.casefold()
                    host = (parts.hostname or "").casefold()
                    port = parts.port
                except ValueError:
                    canonical = raw_url
                else:
                    try:
                        address = ipaddress.ip_address(host)
                        if (
                            isinstance(address, ipaddress.IPv6Address)
                            and address.ipv4_mapped is not None
                        ):
                            host = str(address.ipv4_mapped)
                        else:
                            host = str(address)
                    except ValueError:
                        pass
                    # Host aliases must not let two users share one remote vault.
                    if host in {"localhost", "127.0.0.1", "::1"}:
                        host = "localhost"
                    if port is None:
                        port = {"http": 80, "https": 443}.get(scheme)
                    authority = f"[{host}]" if ":" in host else host
                    canonical = f"{scheme}://{authority}"
                    if port is not None:
                        canonical += f":{port}"
                    if parts.path not in ("", "/"):
                        canonical += parts.path
            other = owners_by_path.get(canonical)
            if other is not None and other != user_id:
                raise ValueError(
                    "a memory federation source may be bound to one user only"
                )
            owners_by_path[canonical] = user_id
        return self


class SuperagentRuntimeConfig(BaseModel):
    """Which runtime runs each background Super-agent role (R8c).

    ``ptc`` keeps the PTC agent graph and its sandbox; ``p`` runs the role on
    the sandbox-free P turn loop. One switch per role so watchdog and
    supervisor can be cut over (and rolled back) one at a time. Deployment
    level, not a per-user feature flag: these roles run in the background and
    a per-user switch means nothing there. Workers always stay on PTC.

    An unrecognised value falls back to ``ptc`` with a warning instead of
    failing config load, so a typo can never take the whole server down or
    move a role onto the new runtime by accident.
    """

    supervisor: Literal["ptc", "p"] = "ptc"
    watchdog: Literal["ptc", "p"] = "ptc"

    @field_validator("supervisor", "watchdog", mode="before")
    @classmethod
    def _unknown_runtime_is_ptc(cls, value: Any, info: Any) -> str:
        if value is None:
            return "ptc"
        normalized = str(value).strip().lower()
        if normalized in ("ptc", "p"):
            return normalized
        logger.warning(
            "superagent_runtime.%s=%r is not 'ptc' or 'p'; using 'ptc'",
            info.field_name,
            value,
        )
        return "ptc"


class InfrastructureConfig(BaseModel):
    """Root model for infrastructure configuration (config.yaml)."""

    model_config = ConfigDict(extra="allow")

    prompt: PromptConfig = Field(default_factory=PromptConfig)
    native_runtime: NativeRuntimeConfig = Field(default_factory=NativeRuntimeConfig)
    agent_fleet: AgentFleetConfig = Field(default_factory=AgentFleetConfig)
    review_merge: ReviewMergeConfig = Field(default_factory=ReviewMergeConfig)
    native_runtime_routing: NativeRuntimeRoutingConfig = Field(
        default_factory=NativeRuntimeRoutingConfig
    )
    memory_federation: MemoryFederationConfig = Field(
        default_factory=MemoryFederationConfig
    )
    autonomy_router_model: Optional[str] = Field(
        default=None,
        max_length=200,
        description=(
            "Optional model override for Super-agent Router candidate and report turns. "
            "Unset follows the user's normal model preference."
        ),
    )
    superagent_runtime: SuperagentRuntimeConfig = Field(
        default_factory=SuperagentRuntimeConfig,
        description=(
            "Per-role runtime for background Super-agent dispatches: `ptc` "
            "(default, unchanged) or `p` (sandbox-free turn loop). The "
            "CONDUCTOR_SUPERAGENT_RUNTIME_<ROLE> environment variables win."
        ),
    )
    autonomy_router_snapshot_enabled: bool = Field(
        default=False,
        description=(
            "Enable durable Router baseline snapshots and bounded decision metrics. "
            "Off by default until the snapshot contract is accepted."
        ),
    )
    autonomy_cloud_worker_runtime: Optional[str] = Field(
        default=None,
        max_length=64,
        description=(
            "OS-isolated runtime id for cloud autonomous Workers. Unset (or "
            "none/off/disabled) keeps cloud launches refused and work on local "
            "daemons only; `docker` selects the shipped Docker runtime, which "
            "verifies the engine, image, isolation flags and worker CLI before "
            "launching. See docs/CLOUD-WORKERS.md."
        ),
    )
    autonomy_stop_ack_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=300,
        description=(
            "Seconds a native daemon gets to acknowledge a stop and prove its "
            "CLI process exited before the stop is recorded as unverified."
        ),
    )

    # Application Settings
    debug: bool = Field(default=False, description="Debug mode flag")
    workflow_timeout: int = Field(default=21600, description="Workflow timeout in seconds")
    sse_keepalive_interval: float = Field(
        default=15.0, description="SSE keepalive interval in seconds"
    )

    # Feature Flags
    result_log_db_enabled: bool = Field(
        default=True, description="Enable result logging to database"
    )
    redis_warm_on_startup: bool = Field(
        default=True, description="Enable Redis cache warming on startup"
    )
    langsmith_tracing: bool = Field(default=False, description="Enable LangSmith tracing")

    # User-facing product features (deployment overrides of the code catalog
    # in src/config/features.py). Distinct from the infra toggles above, which
    # are operator-only and never exposed to users.
    features: Dict[str, FeatureFlagOverride] = Field(default_factory=dict)

    # Dashboard data sources: provider adapter + timeout per logical feed.
    # Missing keys mean the built-in defaults; a deployment can add another
    # vendor by registering an adapter and naming it here.

    # SSE Event Logging
    sse_event_log_enabled: bool = Field(default=True, description="Enable SSE event logging")
    sse_event_log_level: str = Field(default="info", description="SSE event log level")

    # General Application Logging
    log_level: str = Field(default="error", description="Root logger level")
    log_format: str = Field(
        default="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        description="Log format string",
    )
    module_log_levels: Dict[str, str] = Field(
        default_factory=dict, description="Module-specific log levels"
    )

    # CORS Settings
    allowed_origins: List[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:5170",
            "http://localhost:5170",
        ],
        description="Allowed CORS origins",
    )

    # Background Execution
    background_execution: BackgroundExecutionConfig = Field(
        default_factory=BackgroundExecutionConfig
    )

    # Redis Cache
    redis: RedisConfig = Field(default_factory=RedisConfig)

    # RunWorkflow orchestration caps
    workflow: WorkflowOrchestrationConfig = Field(
        default_factory=WorkflowOrchestrationConfig
    )
