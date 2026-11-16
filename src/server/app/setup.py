"""
FastAPI application setup, initialization, and middleware configuration.

This module contains:
- Application lifespan management (startup/shutdown)
- Global state initialization (agent_config, workspace_manager, checkpointer)
- Middleware setup (CORS, request ID)
- Router registration
"""

# ============================================================================
# Windows Event Loop Fix (must be before any async imports)
# ============================================================================
# On Windows, Python 3.8+ defaults to ProactorEventLoop, which is incompatible
# with psycopg's async mode. Set WindowsSelectorEventLoopPolicy before any
# async code runs to avoid "ProactorEventLoop" errors when opening connection pools.
import sys
import asyncio
import gc

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ============================================================================
# Imports and Global Variables
# ============================================================================
import importlib
import logging
import os
import certifi

os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware

from ptc_agent.core.sandbox.livefs_runtime.protocol import PREFIX as LIVEFS_PREFIX
from ptc_agent.core.sandbox.platform_secrets import PlatformSecretError
from ptc_agent.core.sandbox.providers.ssh_errors import SSHConnectError
from ptc_agent.core.sandbox.runtime import SandboxGoneError, SandboxTransientError
from src.server.preview_proxy import install_preview_proxy
from src.config.logging_config import configure_logging
from src.config.settings import (
    get_allowed_origins,
)
from src.observability import init_otel, init_otel_runtime, shutdown_otel_runtime
from src.observability.private_query import drop_span_query
from src.server.services.agent_config_state import set_agent_config
from src.server.services.runs.executor import LocalRunExecutor
from src.server.services.runs.task_registry_store import BackgroundRegistryStore
from src.server.utils.api import find_malformed_route_ids  # TEMP (malformed-id-diag)
from src.server.services.preview_base import (
    PreviewProxyConfigurationError,
    check_preview_proxy_config,
    preview_proxy_requirements,
)
from src.server.app.error_handlers import unhandled_exception_handler
from src.server.utils.error_sanitization import (
    caller_runs,
    public_validation_errors,
    route_schema_names,
    sandbox_failure_http,
    single_line,
)

# Phase 1: install fork-safe class-level instrumentor patches BEFORE FastAPI(...)
# is constructed. FastAPIInstrumentor patches the FastAPI class — must run
# before any instance exists. No providers, no daemon threads here.
#
# Phase 2 (init_otel_runtime) runs in the lifespan startup below, AFTER any
# fork performed by uvicorn --workers N. Daemon threads inside BatchSpanProcessor
# / PeriodicExportingMetricReader do not survive fork(), so they must be
# created per-worker.
#
# No-op when OTEL_EXPORTER_OTLP_ENDPOINT is unset; wrapped in try/except
# internally so a broken instrumentor cannot prevent server startup.
_otel_enabled = init_otel()

logger = logging.getLogger(__name__)


INTERNAL_SERVER_ERROR_DETAIL = "Internal Server Error"

# Global variables
agent_config = None  # PTC Agent configuration (loaded from config files)
# Which bundle owns each shipped MCP server and skill, as of the read that
# composed `agent_config`. Enforcement reads this rather than re-reading
# plugins/, so it cannot disagree with the server set actually running; the
# Plugins page keeps the live read, where showing an edited manifest at once
# is the point. See services/plugins/bundled.enforcement_owners.
bundle_owners = None
workspace_manager = None  # Workspace manager instance
checkpointer = None  # PTC Agent LangGraph checkpointer for state persistence
store = None  # LangGraph Store for cross-turn metadata persistence
graph = None  # Most recently used LangGraph (for persistence snapshots)
llm_service = None  # Generic one-shot LLM call wrapper (BYOK/OAuth-aware)

# PID 1 process names that correctly reap orphaned subprocesses.
# `docker-init` is Docker's bundled tini wrapper (what `init: true` in compose
# launches when no explicit entrypoint uses tini); `podman-init` is the
# equivalent shim rootless podman injects for `init: true` (catatonit under the
# hood). Must be in the allowlist or this diagnostic logs a false "init: true
# failed" warning on every startup of an otherwise-correctly-hardened container.
_ACCEPTABLE_INIT_COMMS = (
    "tini",
    "docker-init",
    "catatonit",
    "dumb-init",
    "podman-init",
)


# Per-worker background singletons sharing one lifecycle shape: importable
# lazily, ``get_instance()``, sync ``start()``, async ``stop()``. A table
# rather than a stanza each, so adding one is a row and neither direction can
# drift out of sync with the other.
_REDIS_BACKGROUND_SINGLETONS: tuple[tuple[str, str], ...] = (
    # A run that dies before its terminal never stamps a TTL, so its event
    # stream would stay resident forever.
    ("StreamRetentionSweeper", "src.server.services.stream_retention_sweep"),
    # Tells apart "Redis is slow" from "this worker's loop was blocked", which
    # read identically at the redis-py boundary.
    ("EventLoopLagMonitor", "src.observability.loop_lag"),
    # Refreshes expiring MCP OAuth connections ahead of the hot path; the
    # per-connection advisory try-lock dedups it across workers.
    ("McpOAuthRefreshSweeper", "src.server.services.mcp_oauth.sweep"),
    # Takes tunnels no computer uses off their machines; a claim on each
    # tunnel's row in Postgres dedups it across workers.
    ("TunnelSweeper", "src.server.services.ssh_tunnel_sweep"),
    ("CollabLeaseSweeper", "src.server.services.collab.sweep"),  # N80 collab_lease_sweep
)


def _background_singleton(name: str, module: str):
    """Resolve one singleton, importing its module on first use.

    Imports stay lazy for the same reason the hand-written blocks did it: these
    modules pull in Redis and service layers that must not load at import time.
    """
    return getattr(importlib.import_module(module), name).get_instance()


def _log_container_hardening() -> None:
    """Log PID 1 comm and cgroup PID limits so deploys catch missing tini."""
    try:
        with open("/proc/1/comm", encoding="utf-8", errors="replace") as f:
            pid1 = f.read().strip()
    except (FileNotFoundError, OSError):
        return  # Not Linux (macOS dev, etc.) — skip silently

    try:
        with open("/sys/fs/cgroup/pids.max", encoding="utf-8", errors="replace") as f:
            pids_max = f.read().strip()
        with open(
            "/sys/fs/cgroup/pids.current", encoding="utf-8", errors="replace"
        ) as f:
            pids_current = f.read().strip()
    except (FileNotFoundError, OSError):
        pids_max = pids_current = "unknown"

    logger.info(
        f"Container hardening: PID 1 = {pid1!r}, "
        f"pids.current = {pids_current}, pids.max = {pids_max}"
    )
    if pid1 not in _ACCEPTABLE_INIT_COMMS:
        logger.warning(
            f"PID 1 is {pid1!r}, not an init process. `init: true` in compose "
            "may have silently failed — orphaned subprocesses will not be reaped. "
            "Verify tini is installed in the image."
        )


# ============================================================================
# Lifespan Context Manager
# ============================================================================
JUDGMENT_DRAIN_SECONDS = 3.0


async def drain_judgments(timeout: float = JUDGMENT_DRAIN_SECONDS) -> None:
    """Wait briefly for background N61 judgments at shutdown. Never raises."""
    try:
        from src.server.services.autonomy import judgment_hooks

        await asyncio.wait_for(judgment_hooks.drain_pending(timeout), timeout + 1)
    except Exception as e:
        logger.warning(f"Error draining judgment hooks: {e}")
    try:
        from src.tools.auto_dispatch import completion_judgment

        await asyncio.wait_for(completion_judgment.drain(timeout), timeout + 1)
    except Exception as e:
        logger.warning(f"Error draining completion judgments: {e}")
    try:
        from src.server.services.judgment.record import drain_shared

        # The writer thread is a daemon: whatever is still queued after this
        # bounded wait cannot hold up exit.
        await asyncio.wait_for(drain_shared(timeout), timeout + 1)
    except Exception as e:
        logger.warning(f"Error draining judgment records: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources when server starts, cleanup when stops."""
    global \
        agent_config, \
        bundle_owners, \
        workspace_manager, \
        checkpointer, \
        store, \
        llm_service

    # Configure logging based on environment settings (first thing on startup)
    configure_logging()

    # Phase 2 of the OTel bootstrap (see module-top init_otel comment). Runs
    # per-worker so BatchSpanProcessor / PeriodicExportingMetricReader daemon
    # threads are owned by the worker process, not a dead pre-fork parent.
    init_otel_runtime()

    # Container hardening diagnostics: confirm tini is PID 1 and log cgroup PID limit.
    # If PID 1 is python (not tini), `init: true` in compose silently failed and
    # orphaned browser subprocesses will accumulate until cgroup exhausts.
    _log_container_hardening()

    # Security warnings (one-time, startup only)
    from src.config.env import HOST_MODE

    if HOST_MODE == "oss":
        logger.warning(
            "HOST_MODE=oss: authentication is disabled. "
            "All endpoints are accessible without a token. "
            "Set HOST_MODE=platform for production use."
        )
    if os.getenv("BYOK_ENCRYPTION_KEY") == "conductor-local-dev-encryption-key":
        logger.warning(
            "BYOK_ENCRYPTION_KEY is set to the default value from the repository. "
            "User API keys are encrypted with a publicly known key. "
            "Run `make config` or set a unique BYOK_ENCRYPTION_KEY."
        )

    # Initialize and open conversation database pool
    from src.server.database.pool import get_or_create_pool

    conv_pool = get_or_create_pool()
    # Extract connection details from pool
    conninfo = conv_pool._conninfo if hasattr(conv_pool, "_conninfo") else "unknown"
    try:
        # Parse basic connection info (format: postgresql://user:pass@host:port/dbname?sslmode=...)
        import re

        match = re.search(r"@([^:]+):(\d+)/([^?]+)", conninfo)
        if match:
            db_host, db_port, db_name = match.groups()
            await conv_pool.open()
            # Validate pool is ready with a simple health check
            async with conv_pool.connection() as conn:
                await conn.execute("SELECT 1")
            logger.info(f"Conversation DB: Connected to {db_host}:{db_port}/{db_name}")
        else:
            await conv_pool.open()
            # Validate pool is ready with a simple health check
            async with conv_pool.connection() as conn:
                await conn.execute("SELECT 1")
            logger.info("Conversation DB: Connected successfully")
    except Exception as e:
        if match:
            logger.error(
                f"Conversation DB: Failed to connect to {db_host}:{db_port}/{db_name} - {e}"
            )
        else:
            logger.error(f"Conversation DB: Failed to connect - {e}")
        raise

    # R6 L1: load the stored role registry (none stored = legacy settings).
    from src.server.services.autonomy.role_registry import install_at_startup

    await install_at_startup()

    # Auto-provision local dev user when Supabase auth is disabled
    from src.config.settings import HOST_MODE, LOCAL_DEV_USER_ID

    if HOST_MODE == "oss":
        from src.server.database.user import get_user, create_user_from_auth

        # Only provision if name is missing or user doesn't exist
        existing = await get_user(LOCAL_DEV_USER_ID)
        if not existing or not existing.get("name"):
            await create_user_from_auth(
                user_id=LOCAL_DEV_USER_ID,
                name="Local User",
            )
            logger.info(f"[auth] Local dev user provisioned: {LOCAL_DEV_USER_ID}")

    # Initialize Redis cache
    try:
        from src.utils.cache.redis_cache import init_cache

        logger.info("Initializing Redis cache client...")
        await init_cache()
        logger.info("Redis cache client initialized")

    except Exception as e:
        logger.warning(f"Redis cache initialization failed: {e}")
        logger.warning("Server will continue without caching")

    # Sandbox path writes take a Redis lease so the workers agree on one
    # writer per file. Fails open when the cache is down (see the module).
    from src.server.services.path_lock_coordination import install as install_path_lease

    install_path_lease()

    # Start LocalRunExecutor cleanup task
    try:
        manager = LocalRunExecutor.get_instance()
        await manager.start_cleanup_task()
    except Exception as e:
        logger.warning(f"Failed to start LocalRunExecutor cleanup task: {e}")

    # Warm and validate repository-shipped workflows so bad seeds are logged
    # during startup rather than on the first request.
    try:
        from ptc_agent.agent.middleware.task_runtime.workflow.prebuilt import (
            get_prebuilt_workflows,
        )

        prebuilts = get_prebuilt_workflows()
        logger.info(f"Loaded {len(prebuilts.names())} pre-built workflow(s)")
    except Exception as e:
        logger.warning(f"Pre-built workflow warmup failed: {e}")

    # Initialize PTC Agent configuration and session service
    try:
        from src.config.runtime import load_from_files, ConfigContext

        logger.info("Loading PTC Agent configuration...")
        agent_config = await load_from_files(context=ConfigContext.SDK)
        set_agent_config(agent_config)

        from src.config.env import PUBLIC_APP_URL, SERVER_BASE_URL

        check_preview_proxy_config(
            proxy_required_by=preview_proxy_requirements(agent_config.sandbox),
            app_urls=(SERVER_BASE_URL, PUBLIC_APP_URL, *get_allowed_origins()),
        )

        from src.server.services.plugins.bundled import component_owners

        bundle_owners = component_owners()

        from src.server.services.platform_secret_rollout import (
            reconcile_platform_secrets_at_boot,
        )

        await reconcile_platform_secrets_at_boot(agent_config)
        logger.info("PTC Agent configuration loaded successfully")

        # Connect once, freeze, install as the process-global registry so every
        # Session borrows the same tool snapshot instead of spawning its own
        # MCP cohort. Failures here are non-fatal: Sessions fall back to a
        # per-instance registry.
        from src.server.services.mcp_core.registry import (
            MCPRegistry,
            set_global_registry,
        )

        mcp_registry = None
        try:
            core_config = agent_config.to_core_config()
            mcp_registry = MCPRegistry(core_config)
            await mcp_registry.connect_all(retry_builtin_remote=True)
            await mcp_registry.freeze()
            set_global_registry(mcp_registry)
            logger.info(
                "Global MCP registry frozen at startup (servers=%d)",
                len(mcp_registry.connectors),
            )
        except Exception as exc:
            # Tear down any subprocesses connect_all already spawned, regardless
            # of how far freeze() got. ``disconnect_all`` short-circuits when
            # _frozen=True, so use the force variant here to avoid leaks if the
            # exception fired between freeze setting _frozen and the install.
            if mcp_registry is not None:
                try:
                    await mcp_registry._force_disconnect_all()
                except Exception:
                    pass
            logger.warning(
                "Failed to install global MCP registry "
                "(error_type=%s); sessions will fall back to per-instance "
                "registries.",
                type(exc).__name__,
            )

        # Initialize generic one-shot LLM call wrapper. Constructed once so
        # every server-side utility LLM call (memo metadata, thread titles,
        # follow-up suggestions, etc.) shares a single BYOK/OAuth-aware entry
        # point.
        from src.server.services.llm.service import LLMService

        llm_service = LLMService(agent_config=agent_config, logger=logger)
        app.state.llm_service = llm_service
        from src.server.database.main_agent import PostgresMainAgentRepository
        from src.server.services.main_agent import create_main_agent_orchestrator

        main_agent_repository = PostgresMainAgentRepository()
        app.state.main_agent_repository = main_agent_repository
        app.state.main_agent_orchestrator = create_main_agent_orchestrator(
            llm_service=llm_service,
            repository=main_agent_repository,
        )
        logger.info("LLMService initialized")

        # Initialize workspace manager. Its idle sweep times each computer by
        # that computer's own provider settings.
        from src.server.services.workspace_manager import WorkspaceManager

        workspace_manager = WorkspaceManager.get_instance(
            config=agent_config,
            cleanup_interval=300,  # 5 minutes
        )
        await workspace_manager.start_cleanup_task()
        logger.info("Workspace Manager initialized")

        # An ssh computer logs in with its owner's key, which the provider
        # reads through this loader; any worker may start one. A machine that
        # connects out is reached through the gateway the second loader names.
        from src.server.services.ssh_computers import install_client_key_loader
        from src.server.services.ssh_tunnels import install_gateway_loader

        install_client_key_loader()
        install_gateway_loader()

        # Platform-secret migration sweep: converges running sandboxes left
        # behind the fleet generation (always-on sandboxes never re-init, so
        # only this sweep ever scrubs them). Inert without an active catalog.
        # Started after the Workspace Manager, which creates the
        # ComputerManager its first pass needs.
        try:
            from src.server.services.platform_secret_sweep import (
                PlatformSecretSweeper,
            )

            PlatformSecretSweeper.get_instance().start(agent_config.to_core_config())
        except Exception as e:
            logger.warning(f"Failed to start PlatformSecretSweeper: {e}")

        # Initialize PTC Agent checkpointer for state persistence
        from src.server.utils.checkpointer import (
            get_checkpointer,
            open_checkpointer_pool,
            get_store,
            setup_store,
        )

        checkpointer = get_checkpointer(
            memory_type=os.getenv("MEMORY_DB_TYPE", "postgres"),
            db_host=os.getenv("DB_HOST", "localhost"),
            db_port=os.getenv("DB_PORT", "5432"),
            db_name=os.getenv("DB_NAME", "postgres"),
            db_user=os.getenv("DB_USER", "postgres"),
            db_password=os.getenv("DB_PASSWORD", "postgres"),
        )
        await open_checkpointer_pool(checkpointer)
        # Validate checkpointer pool is ready with a health check
        if checkpointer and hasattr(checkpointer, "conn"):
            pool = checkpointer.conn
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
        logger.info("PTC Agent checkpointer initialized")

        # Initialize LangGraph Store (shares pool with checkpointer)
        try:
            store = get_store(checkpointer)
            if store:
                await setup_store(store)
                logger.info("LangGraph Store initialized")
        except Exception as e:
            logger.warning(f"LangGraph Store setup failed: {e}")
            logger.warning("Offloaded ID dedup will use in-memory fallback")
            store = None

        # Super-agent intake/effects. The scheduler itself is idle until a
        # source or ticket exists; the feature flag gates user surfaces, not
        # the durable drainer (an in-flight ticket must still be able to finish).
        try:
            from src.server.services.autonomy.scheduler import (
                start_autonomy_scheduler,
            )

            start_autonomy_scheduler(
                main_agent_orchestrator=app.state.main_agent_orchestrator,
                memory_store=store,
            )
        except Exception as e:
            logger.warning(f"Autonomy scheduler startup failed: {e}")

        # Phase 2 (I2): the pinned-session writer pool. Only meaningful with
        # a Postgres checkpointer in the SAME database as the app tables —
        # advisory locks are database-local, so a split deployment gets no
        # fence (and must stay single-worker).
        try:
            from src.server.database.pool import get_db_connection_string
            from src.server.services import writer_guard

            if checkpointer is not None and hasattr(checkpointer, "conn"):
                app_dsn = get_db_connection_string()
                cp_pool = checkpointer.conn
                cp_dsn = getattr(cp_pool, "conninfo", None) or getattr(
                    cp_pool, "_conninfo", ""
                )
                if writer_guard.same_database(app_dsn, cp_dsn):
                    await writer_guard.open_writer_pool(app_dsn)
                else:
                    logger.warning(
                        "WriterGuard disabled: checkpointer database differs "
                        "from the app database; runs are unfenced and "
                        "--workers>1 is unsupported"
                    )
            else:
                logger.warning(
                    "WriterGuard disabled: non-Postgres checkpointer; runs "
                    "are unfenced and --workers>1 is unsupported"
                )
        except Exception as e:
            logger.warning(f"WriterGuard pool setup failed: {e}; running unfenced")

    except FileNotFoundError as e:
        logger.warning(f"PTC Agent config not found: {e}")
        logger.warning("PTC Agent endpoints will not be available")
    except PlatformSecretError:
        # Required platform Secrets are a configuration gate. Hosted mode
        # must never continue into a plaintext fallback or partial startup.
        raise
    except PreviewProxyConfigurationError:
        # The same kind of gate: private computers with nowhere to serve
        # their previews would hand out only dead links.
        raise
    except Exception as e:
        logger.warning(f"Failed to initialize PTC Agent: {e}")
        logger.warning("PTC Agent endpoints may not work correctly")

    # Multi-worker hard gate (top level — must not be swallowed by the PTC
    # init catch-all): without the fence, two workers could both write one
    # thread's checkpoints. Refuse to boot rather than corrupt.
    _workers = int(os.getenv("CONDUCTOR_WORKERS", "1") or 1)
    if _workers > 1:
        from src.server.services import writer_guard as _wg

        if not _wg.guard_enabled():
            raise RuntimeError(
                f"--workers={_workers} requires the WriterGuard fence: "
                f"use a Postgres checkpointer in the same database as "
                f"the app tables, or run a single worker."
            )

    # Start the hook-outbox drainer BEFORE the stale-run sweep so the jobs
    # the sweep enqueues (and any left over from the previous process — the
    # lease-expiry reclaim doubles as startup recovery) start executing
    # immediately. Executor registration must precede start(): the outbox
    # never imports handlers, and an unregistered type's jobs nack toward
    # dead.
    # Imports + registration OUTSIDE the guard: a failure there is a code bug
    # that must crash the worker loudly — swallowed, it leaves the drainer
    # permanently off behind one WARN line (report-backs undelivered, burst
    # slots leaking).
    from src.server.services.runs.reports import steer_executor, subagent
    from src.server.services.hook_outbox import HookOutboxDrainer
    from src.server.services.user_feed import (
        register_outbox_executors as register_user_feed_executors,
    )

    subagent.register_outbox_executors()
    steer_executor.register_outbox_executors()
    register_user_feed_executors()
    try:
        HookOutboxDrainer.get_instance().start()
        logger.info("HookOutboxDrainer started")
    except Exception as e:
        logger.warning(f"Failed to start HookOutboxDrainer: {e}")

    # Recovery scanner (turn lifecycle v4, Phase 2.2): one startup pass
    # (assume_dead covers the unfenced single-worker case — this process
    # restarting proves any open run's executor is dead), one-time thread
    # projection heal, then the periodic guarded loop when the fence is up.
    try:
        from src.server.services.runs.recovery import RecoveryScanner

        scanner = RecoveryScanner.get_instance()
        recovered = await scanner.scan_once(assume_dead=True)
        if recovered:
            logger.warning(f"Startup recovery finalized {recovered} stale run(s)")
        await scanner.heal_thread_projections()
        scanner.start()
    except Exception as e:
        logger.warning(f"Recovery scanner startup failed: {e}")

    # Turn-cancel nudge listener (v4 Phase 2.4e): a /cancel landing on
    # another worker signals this one's executor via pub/sub.
    try:
        from src.server.services.runs.cancel import TurnCancelListener

        TurnCancelListener.get_instance().start()
        logger.info("TurnCancelListener started")
    except Exception as e:
        logger.warning(f"Failed to start TurnCancelListener: {e}")

    for name, module in _REDIS_BACKGROUND_SINGLETONS:
        try:
            _background_singleton(name, module).start()
            logger.info(f"{name} started")
        except Exception as e:
            logger.warning(f"Failed to start {name}: {e}")

    # Start ProvenanceGCService (daily mark-sweep of orphaned result bodies)
    try:
        from src.server.services.provenance_gc import ProvenanceGCService

        provenance_gc = ProvenanceGCService.get_instance()
        await provenance_gc.start()
        logger.info("ProvenanceGCService started")
    except Exception as e:
        logger.warning(f"Failed to start ProvenanceGCService: {e}")

    # Start WorkspaceFileGCService (condemn-then-reap of unreferenced file blobs)
    try:
        from src.server.services.workspace_file_gc import WorkspaceFileGCService

        await WorkspaceFileGCService.get_instance().start()
    except Exception as e:
        logger.warning(f"Failed to start WorkspaceFileGCService: {e}")

    # A multipart upload the process died holding is never aborted by us;
    # only the bucket's own expiry rule reclaims its parts. The check only
    # feeds a warning, so an unresponsive store must not hold up startup.
    try:
        from src.utils.storage import has_multipart_cleanup_rule

        if (
            await asyncio.wait_for(
                asyncio.to_thread(has_multipart_cleanup_rule), timeout=5
            )
            is False
        ):
            logger.warning(
                "Storage bucket has no rule expiring incomplete multipart uploads; "
                "parts of an upload interrupted mid-transfer will be kept and billed. "
                "Add an AbortIncompleteMultipartUpload lifecycle rule (e.g. 1 day)."
            )
    except Exception as e:
        logger.info(f"Skipped the multipart cleanup rule check: {e!r}")

    # Confirm the runtime credit gate can reach its lease service, and on
    # terms its refresher can work with. Both failures it catches are silent
    # at request time.
    try:
        from src.server.services.credit_gate_port import verify_credit_gate_wiring

        await verify_credit_gate_wiring()
    except Exception as e:
        logger.warning(f"Credit gate wiring check failed: {e}")

    from src.server.auth.jwt_bearer import warm_jwks
    from src.server.auth.oauth_resource import warm_oauth_resource_keys

    await warm_jwks()
    await warm_oauth_resource_keys()

    # A built-in remote server still down after boot's retry budget is retried
    # in the background, so this worker heals when it answers instead of
    # serving without its tools until a restart. Last, so nothing above can
    # fail startup with the task running.
    from src.server.services.mcp_core.registry import (
        get_global_registry,
        reconnect_builtin_remote,
    )

    mcp_reconnect: asyncio.Task[None] | None = None
    booted_registry = get_global_registry()
    if booted_registry is not None and booted_registry.failed_builtin_remote:
        mcp_reconnect = asyncio.create_task(
            reconnect_builtin_remote(), name="mcp-builtin-reconnect"
        )
        logger.info(
            "Reconnecting built-in MCP servers in the background: %s",
            ", ".join(sorted(booted_registry.failed_builtin_remote)),
        )

    # Resume the Weixin ClawBot long-poll worker when its vault session
    # survived a restart. It is best-effort: a broken bot must not block boot.
    from src.server.app.optional_startup import start_optional, stop_optional

    weixin_bot = await start_optional(
        "src.server.services.weixin_bot",
        "start_configured_manager",
        label="Weixin bot",
    )
    # Resumes saved dispatches through DispatchService._drive. A failure here
    # does not restart the API, the database, or any other worker.
    # One coordinator for HTTP, the scheduled drive, and this wakeup.
    bind_dispatch_coordinator(app)
    dispatch_wakeup = await start_optional(
        "src.server.services.dispatch",
        "start_wakeup",
        label="Dispatch wakeup",
    )
    wechat_subscriptions = await start_optional(
        "src.server.services.wechat_subscriptions",
        "start_worker",
        label="WeChat subscription worker",
    )

    # Startup leaves ~700k import-time objects (pydantic schemas, routes,
    # module state) that never die, and every full collection re-walks them
    # while the loop is frozen. Freezing moves them out of the collector for
    # good; the collect first keeps startup garbage from being frozen with them.
    gc.collect()
    gc.freeze()
    logger.info(f"Froze {gc.get_freeze_count()} startup objects out of the GC")

    yield  # Server is running

    # Shutdown
    logger.info("Application shutdown started...")
    await stop_optional(dispatch_wakeup, "stop_wakeup", label="Dispatch wakeup")
    from src.server.services.dispatch import bind_process_coordinator

    bind_process_coordinator(None)
    await stop_optional(weixin_bot, "shutdown_managers", label="Weixin bot")
    await stop_optional(
        wechat_subscriptions,
        "stop_worker",
        label="WeChat subscription worker",
    )
    # Before the registry is cleared below, which would end it anyway, so an
    # attempt in flight closes its connection while the loop still runs.
    if mcp_reconnect is not None:
        mcp_reconnect.cancel()
        await asyncio.gather(mcp_reconnect, return_exceptions=True)
    from src.server.services.brand_icons import stop_refreshes

    await stop_refreshes()

    # 0.0. Stop the recovery scanner first — no new recovery work while the
    # process drains (live runs hold their guards and are skipped anyway).
    try:
        from src.server.services.runs.recovery import RecoveryScanner

        await RecoveryScanner.get_instance().stop()
    except Exception as e:
        logger.warning(f"Error stopping RecoveryScanner: {e}")

    # 0.0a. Stop the platform-secret sweep — no new scrubs while draining.
    try:
        from src.server.services.platform_secret_sweep import (
            PlatformSecretSweeper,
        )

        await PlatformSecretSweeper.get_instance().stop()
    except Exception as e:
        logger.warning(f"Error stopping PlatformSecretSweeper: {e}")

    try:
        from src.server.services.egress.relay import close_relay_client

        await close_relay_client()
    except Exception as e:
        logger.warning(f"Error closing egress relay client: {e}")

    # 0.0b. Stop the turn-cancel nudge listener.
    try:
        from src.server.services.runs.cancel import TurnCancelListener

        await TurnCancelListener.get_instance().stop()
    except Exception as e:
        logger.warning(f"Error stopping TurnCancelListener: {e}")

    # 0.0b. Stop the Redis-backed background singletons, in reverse start
    # order so nothing is still publishing into a listener that has gone.
    for name, module in reversed(_REDIS_BACKGROUND_SINGLETONS):
        try:
            await _background_singleton(name, module).stop()
        except Exception as e:
            logger.warning(f"Error stopping {name}: {e}")

    # 0.0c. Stop the autonomy pass before project workers: neither should
    # outlive the run executor below. The effect runner is stopped with it.
    try:
        from src.server.services.autonomy.scheduler import stop_autonomy_scheduler

        await stop_autonomy_scheduler()
    except Exception as e:
        logger.warning(f"Error stopping autonomy scheduler: {e}")

    # N61: drain the background shadow judgments (U1/U3 hooks, U2) and their
    # records only now, after the autonomy pass and its effect runner (which
    # run the watchdog and Router) have stopped and can start no more.
    await drain_judgments()

    # 0.1. Shutdown ProvenanceGCService
    try:
        from src.server.services.provenance_gc import ProvenanceGCService

        await ProvenanceGCService.get_instance().stop()
    except Exception as e:
        logger.warning(f"Error shutting down ProvenanceGCService: {e}")

    try:
        from src.server.services.workspace_file_gc import WorkspaceFileGCService

        await WorkspaceFileGCService.get_instance().stop()
    except Exception as e:
        logger.warning(f"Error shutting down WorkspaceFileGCService: {e}")

    # 2. Cancel background subagent tasks
    try:
        registry_store = BackgroundRegistryStore.get_instance()
        await registry_store.cancel_all(force=True)
    except Exception as e:
        logger.warning(f"Error cancelling background subagent tasks: {e}")

    # 2.5. Close the preview proxy's upstream pool; its keep-alive renewals
    # reach the Workspace Manager, so they go before it does.
    preview_proxy = getattr(app.state, "preview_proxy", None)
    if preview_proxy is not None:
        try:
            await preview_proxy.aclose()
        except Exception as e:
            logger.warning(f"Error closing the preview proxy: {e}")

    # 3. Shutdown Workspace Manager (stop cleanup task, clear cache)
    if workspace_manager is not None:
        try:
            # Drain in-flight warm tasks first so a task cancelled mid-Phase-2
            # reverts its 'starting' row to 'stopped' (CancelledError revert)
            # instead of being torn down abruptly and left wedged.
            from src.server.app.background_starts import drain_start_tasks

            await drain_start_tasks()
        except Exception as e:
            logger.warning(f"Error draining background starts: {e}")
        try:
            logger.info("Shutting down Workspace Manager...")
            await workspace_manager.shutdown()
            logger.info("Workspace Manager shutdown complete")
        except Exception as e:
            logger.warning(f"Error during Workspace Manager shutdown: {e}")

    # 4. Forget this worker's PTC handles. A sibling worker may still be using
    # the same computer, so process shutdown must not stop the shared sandbox.
    try:
        from ptc_agent.core.session import SessionManager

        logger.info("Detaching PTC sessions...")
        SessionManager.detach_all()
        logger.info("PTC sessions detached")
    except Exception as e:
        logger.warning(f"Error stopping PTC sessions: {e}")

    # 4.5. Drop the global MCP registry reference (frozen snapshot — no
    # subprocesses to terminate). Hygiene only.
    try:
        from src.server.services.mcp_core.registry import clear_global_registry

        clear_global_registry()
    except Exception as e:
        logger.debug(f"Error clearing global MCP registry: {e}")

    # 6. Gracefully shutdown background workflows
    try:
        manager = LocalRunExecutor.get_instance()
        await manager.shutdown()  # Uses shutdown_timeout from config.yaml
    except Exception as e:
        logger.error(f"Error during LocalRunExecutor shutdown: {e}")

    # 6b. Stop the hook-outbox drainer AFTER BTM shutdown so jobs enqueued by
    # those final finalizes still execute; anything unclaimed survives
    # durably for the next startup's reclaim.
    try:
        from src.server.services.hook_outbox import HookOutboxDrainer

        await HookOutboxDrainer.get_instance().stop()
        logger.info("HookOutboxDrainer stopped")
    except Exception as e:
        logger.warning(f"Error stopping HookOutboxDrainer: {e}")

    # 6c. Close the writer-guard pool AFTER BTM shutdown: the final
    # finalizes run on pinned guard sessions checked out of this pool.
    try:
        from src.server.services.writer_guard import close_writer_pool

        await close_writer_pool()
    except Exception as e:
        logger.warning(f"Error closing writer-guard pool: {e}")

    # 6d. Close the checkpointer pool AFTER BTM shutdown, for the same reason
    # as 6b/6c: cancelling a live run flushes its checkpoint on the way out.
    # Closing first left that flush — and the history projection behind it —
    # retrying against a dead pool and falling back to a slow listing, which
    # dragged graceful shutdown past the deploy's grace period.
    if checkpointer is not None:
        try:
            from src.server.utils.checkpointer import close_checkpointer_pool

            logger.info("Closing PTC Agent checkpointer pool...")
            await close_checkpointer_pool(checkpointer)
            logger.info("PTC Agent checkpointer pool closed")
        except Exception as e:
            logger.warning(f"Error closing PTC Agent checkpointer pool: {e}")

    # 7. Close database pools, and the session folder holds share
    try:
        from src.server.database.session_lock import close_shared_lock_session

        await close_shared_lock_session()
    except Exception as e:
        logger.warning(f"Error closing shared lock session: {e}")

    try:
        from src.server.database.pool import get_or_create_pool

        conv_pool = get_or_create_pool()
        if not conv_pool.closed:
            logger.info("Closing conversation database pool...")
            await conv_pool.close()
            logger.info("Conversation database pool closed successfully")
    except Exception as e:
        logger.warning(f"Error closing conversation database pool: {e}")

    # 8. Close the Redis pools. All three, unconditionally — the status pubsub
    # pool used to be closed only when a workspace manager existed, so some
    # shutdown paths leaked it.
    try:
        from src.server.services.workspace_status_pubsub import (
            close_status_pubsub_pool,
        )

        await close_status_pubsub_pool()
    except Exception as e:
        logger.warning(f"Error closing status pubsub pool: {e}")

    try:
        from src.utils.cache.stream_pool import close_stream_reader_pool

        await close_stream_reader_pool()
    except Exception as e:
        logger.warning(f"Error closing Redis stream-reader pool: {e}")

    try:
        from src.utils.cache.redis_cache import close_cache

        logger.info("Closing Redis cache client...")
        await close_cache()
        logger.info("Redis cache client closed")
    except Exception as e:
        logger.warning(f"Error closing Redis cache: {e}")

    # 9. Close usage-limits HTTP client
    try:
        from src.server.dependencies.usage_limits import close_http_client

        await close_http_client()
        logger.info("Usage limits HTTP client closed")
    except Exception as e:
        logger.warning(f"Error closing usage limits HTTP client: {e}")

    # 9.1. Close the messaging gateway HTTP client
    try:
        from src.tools.messaging.tools import close_client

        await close_client()
    except Exception as e:
        logger.warning(f"Error closing messaging HTTP client: {e}")

    # 9.5. Close the PDF render browser singleton (headless Chromium), if one
    # was launched to serve ?format=pdf. No-op when the pdf extra is unused.
    try:
        from src.server.services.pdf_render import close_browser

        await close_browser()
    except Exception as e:
        logger.warning(f"Error closing PDF render browser: {e}")

    # 10. Flush + shut down OTel providers last, so spans/metrics emitted by
    # the earlier shutdown steps reach the collector before the daemon threads
    # exit. No-op when OTel is disabled. Run on a worker thread because
    # BatchSpanProcessor.force_flush() is synchronous and can block up to its
    # default 30s timeout — we must not stall the event loop here.
    try:
        await asyncio.to_thread(shutdown_otel_runtime)
    except Exception as e:
        logger.warning(f"Error shutting down OTel runtime: {e}")

    logger.info("Application shutdown complete")


# ============================================================================
# FastAPI App Initialization and Middleware Setup
# ============================================================================
app = FastAPI(
    version="0.1.0",
    lifespan=lifespan,
)

# Per-app FastAPI instrumentation. The global ``FastAPIInstrumentor().instrument()``
# called in ``init_otel()`` only patches the class — instances constructed inside a
# uvicorn ``--reload`` worker can race against that patching depending on import
# order. Explicit per-app instrumentation here closes the gap with a known-good app.
if _otel_enabled:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, server_request_hook=drop_span_query)
    except Exception as _otel_exc:  # noqa: BLE001
        logger.warning("FastAPIInstrumentor.instrument_app failed: %s", _otel_exc)


class RequestIDMiddleware:
    """Add request ID for tracing without using BaseHTTPMiddleware"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Let OPTIONS requests pass through immediately for CORS preflight
        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        trace_id = str(uuid4())
        scope["state"] = {"trace_id": trace_id}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-trace-id", trace_id.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


class MalformedIdDiagnosticMiddleware:
    """TEMP (malformed-id-diag): log non-UUID workspace/thread ids with their Referer.

    Pairs with the ``db_get_workspace`` UUID guard. When a file/dir name from the
    SPA file tree reaches a workspace_id/thread_id slot the request now 404s
    cleanly; this records the bad value, route, and Referer/User-Agent so the
    next real prod occurrence names the SPA page that built it. Remove (with
    ``find_malformed_route_ids``) once the frontend writer is identified.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("method") != "OPTIONS":
            try:
                findings = find_malformed_route_ids(
                    scope.get("path", ""), scope.get("query_string", b"")
                )
                if findings:
                    headers = {
                        k.decode("latin-1").lower(): v.decode("latin-1")
                        for k, v in scope.get("headers", [])
                    }
                    # %r on every user-controlled field so repr() escapes any
                    # embedded CR/LF (the ASGI path is percent-decoded, so a
                    # %0a in the URL would otherwise forge a log line); length
                    # caps bound the log volume an attacker can spam.
                    path = scope.get("path", "")[:512]
                    referer = headers.get("referer", "")[:256]
                    user_id = headers.get("x-user-id", "")[:128]
                    ua = headers.get("user-agent", "")[:256]
                    for slot, value in findings:
                        logger.warning(
                            "[malformed-id-diag] %s=%r path=%r "
                            "referer=%r user_id=%r ua=%r",
                            slot,
                            value[:256],
                            path,
                            referer,
                            user_id,
                            ua,
                        )
            except Exception:  # noqa: BLE001 — diagnostics must never break a request
                pass
        await self.app(scope, receive, send)


def _gzip_exempt(path: str) -> bool:
    """Whether GZip leaves the response at ``path`` as it is.

    The workspace file download is the file's own bytes, often already
    compressed, and can be gigabytes streamed from a sandbox: compressing it
    spends a worker's CPU for little, and drops the Content-Length the
    client's progress bar needs.

    The file mount's endpoint names a file's version in an ETag, which the
    next save is checked against. A CDN in front of the server (Cloudflare
    for one) asks for gzip whatever its client asked, and decompressing for
    the daemon, which asks for no encoding, weakens or drops that tag.
    """
    return path.endswith("/files/download") or path.startswith(f"{LIVEFS_PREFIX}/")


class _SelectiveGZip(GZipMiddleware):
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and _gzip_exempt(scope["path"]):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


# Register GZip compression middleware (compresses JSON responses >= 1KB)
app.add_middleware(_SelectiveGZip, minimum_size=1000)

# TEMP (malformed-id-diag): log malformed workspace/thread ids + Referer so the next
# real prod occurrence names the SPA route that built the bad request.
# To remove (once the frontend writer is identified): delete this call, the
# MalformedIdDiagnosticMiddleware class above, the setup.py import of
# find_malformed_route_ids, then find_malformed_route_ids + its module
# constants in src/server/utils/api.py and the test file
# tests/unit/server/utils/test_malformed_route_ids.py. Every site is tagged
# `malformed-id-diag` — `grep -rn malformed-id-diag src tests` lists them all.
app.add_middleware(MalformedIdDiagnosticMiddleware)

# Register request ID middleware (will be executed after CORS)
# Note: In FastAPI, middleware is executed in reverse order (last added = first executed)
# So we add RequestIDMiddleware first, then CORS, so CORS executes first
app.add_middleware(RequestIDMiddleware)

# Add CORS middleware LAST (will be executed FIRST)
# This ensures CORS headers are properly set for all requests including OPTIONS preflight
# Allowed origins loaded from config.yaml
allowed_origins = get_allowed_origins()

logger.info(f"Allowed origins: {allowed_origins}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,  # Restrict to specific origins
    allow_credentials=True,
    allow_methods=[
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
    ],  # Use the configured list of methods
    allow_headers=["*"],  # Now allow all headers, but can be restricted further
)

# Previews of private sandboxes, picked out by Host before any middleware
# above sees the request. Installed last, around the stack the tracing wraps.
install_preview_proxy(app)


# ============================================================================
# Exception Handlers
# ============================================================================


async def _sandbox_failure_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer a sandbox failure once, for every route, as the computer routes do.

    ``sandbox_failure_http`` decides, so one failure gets one status whichever
    route met it: 422 with the owner's own sentence when their SSH machine
    answered and refused, which a retry cannot fix, and 503 otherwise. The 503
    detail is a wire contract, not a message: the file panel categorizes the
    error by matching it, and it never quotes the raw exception, which carries
    provider URLs and SDK response bodies that must not leave the server.
    """
    # The exception is the live carrier: it quotes provider response bodies, so
    # it can hold a newline and a forged entry. Starlette's path is already
    # CR/LF-free (urlsplit drops them); it is escaped to keep the rule uniform.
    logger.warning(
        f"Sandbox unreachable for {single_line(request.url.path)}: {single_line(str(exc))}"
    )
    answer = sandbox_failure_http(exc)
    return JSONResponse(
        status_code=answer.status_code, content={"detail": answer.detail}
    )


async def _request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Answer a malformed request with its fields and rules, not its values.

    Replaces FastAPI's default, which echoes the rejected input back. Do not
    log ``exc`` here: its text includes every rejected value.
    """
    owned = route_schema_names(request.scope.get("route"))
    # multi_items(): a repeated query key keeps every value (dict() keeps the last).
    caller = caller_runs(
        exc.body,
        list(request.query_params.multi_items()),
        dict(request.path_params),
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": public_validation_errors(
                exc.errors(), owned=owned, caller=caller
            )
        },
    )


app.add_exception_handler(RequestValidationError, _request_validation_handler)
app.add_exception_handler(SandboxGoneError, _sandbox_failure_handler)
app.add_exception_handler(SandboxTransientError, _sandbox_failure_handler)
# A refusal a caller let through unwrapped, which would otherwise answer 500.
app.add_exception_handler(SSHConnectError, _sandbox_failure_handler)
# Everything else. HTTPException stays on its own handler: registering
# Exception only replaces Starlette's plain-text 500, and only while
# debug is off (debug would send the traceback page instead).
app.add_exception_handler(Exception, unhandled_exception_handler)


# ============================================================================
# Router Registration
# ============================================================================
# Import routers
from src.server.app.threads import router as threads_router
from src.server.app.utilities import health_router
from src.server.app.ready import router as ready_router
from src.server.app.readiness import router as readiness_router
from src.server.app.local_update import router as local_update_router
from src.server.app.computers import router as computers_router
from src.server.app.computers_ssh import router as computers_ssh_router
from src.server.app.workspaces import router as workspaces_router
from src.server.app.workspace_files import router as workspace_files_router
from src.server.app.workspace_files import wsfiles_router
from src.server.app.workspace_sandbox import router as workspace_sandbox_router
from src.server.app.workspace_sandbox import preview_redirect_router
from src.server.app.users import router as users_router
from src.server.app.features import router as features_router
from src.server.app.api_keys import router as api_keys_router
from src.server.app.oauth import router as oauth_router
from src.server.app.skills import router as skills_router
from src.server.app.subagents import router as subagents_router
from src.server.app.skills import native_router as native_skills_router
from src.server.app.skills import workspace_router as workspace_skills_router
from src.server.app.fleet_skills import router as fleet_skills_router
from src.server.app.autonomy import router as autonomy_router
from src.server.app.main_agent import router as main_agent_router
from src.server.app.deliberation import router as deliberation_router
from src.server.app.memory import router as memory_router
from src.server.app.dispatch import router as dispatch_router
from src.server.app.role_registry import router as role_registry_router
from src.server.app.router_handoff import router as router_handoff_router
from src.server.app.dispatch_workspaces import router as dispatch_workspaces_router
from src.server.app.egress_relay import router as egress_relay_router
from src.server.app.livefs import router as livefs_router
from src.server.app.mcp_catalog import router as mcp_catalog_router
from src.server.app.mcp_builtin import router as mcp_builtin_router
from src.server.app.mcp_icons import router as mcp_icons_router
from src.server.app.mcp_oauth import router as mcp_oauth_router
from src.server.app.plugins import router as plugins_router
from src.server.app.marketplace import router as marketplace_router
from src.server.app.user_vault import router as user_vault_router
from src.server.app.mcp_servers import router as mcp_servers_router
from src.server.app.native_agents import router as native_agents_router
from src.server.app.router_metrics import router as router_metrics_router
from src.server.app.agents import router as agents_router
from src.server.app.review_merge import router as review_merge_router

# Include all routers
app.include_router(threads_router)  # /api/v1/threads/* - Thread CRUD, messages, control
app.include_router(workspaces_router)  # /api/v1/workspaces/* - Workspace CRUD
app.include_router(
    computers_ssh_router
)  # /api/v1/computers/ssh/* - Adding an SSH machine, ahead of /{computer_id}
app.include_router(
    computers_router
)  # /api/v1/computers/* - Computer lifecycle (the workspace routes alias these)
app.include_router(
    workspace_files_router
)  # /api/v1/workspaces/{id}/files/* - Live file access
app.include_router(
    workspace_sandbox_router
)  # /api/v1/workspaces/{id}/sandbox/* - Sandbox stats & packages
app.include_router(users_router)  # /api/v1/users/* - User management
# /api/v1/users/me/thread-events - Thread lifecycle SSE feed
app.include_router(
    features_router
)  # /api/v1/features/* - Feature flags (per-user resolved)
app.include_router(
    api_keys_router
)  # /api/v1/users/me/api-keys + /api/v1/models - BYOK & model config
app.include_router(oauth_router)  # /api/v1/oauth/* - OAuth provider connections (Codex)
app.include_router(skills_router)  # /api/v1/skills - Available agent skills
app.include_router(
    native_skills_router
)  # /api/v1/native/skills - token-scoped native bridge
app.include_router(
    subagents_router
)  # /api/v1/subagents - The user's subagents (Plugins pane)
app.include_router(
    workspace_skills_router
)  # /api/v1/workspaces/{id}/skills - Workspace-scoped skills
app.include_router(
    fleet_skills_router
)  # /api/v1/fleet-skills - read-only per-agent inventory for the fleet dashboard
app.include_router(autonomy_router)  # /api/v1/autonomy/* - Super-agent sources/tickets
app.include_router(importlib.import_module("src.server.app.collab").router)  # /api/v1/collab/* - N80
app.include_router(deliberation_router)  # /api/v1/autonomy/* deliberations
app.include_router(router_metrics_router)  # /api/v1/metrics/router
app.include_router(dispatch_router)  # /api/v1/dispatch/* - Auto-dispatch runs and gate
app.include_router(role_registry_router)  # /api/v1/roles/* - R6 role registry
app.include_router(router_handoff_router)  # /api/v1/router/* - R6 Router handoff
app.include_router(
    dispatch_workspaces_router
)  # /api/v1/dispatch-workspaces/* - Checkouts a dispatch may run against
app.include_router(
    main_agent_router
)  # /api/v1/main-agent/* - durable planning and task claims
app.include_router(memory_router)  # /api/v1/memory/* - authenticated memory lifecycle
app.include_router(
    mcp_catalog_router
)  # /api/v1/mcp/servers - User-level MCP servers (Plugins backing store)
app.include_router(
    mcp_builtin_router
)  # /api/v1/mcp/builtin-servers - This build's own MCP servers, per-user toggle
app.include_router(
    mcp_icons_router
)  # /api/v1/mcp/server-icons/{handle} - Marks MCP servers declare, proxied
app.include_router(
    mcp_oauth_router
)  # /api/v1/mcp/servers/{name}/oauth + /api/v1/mcp/oauth/callback - MCP OAuth
app.include_router(
    user_vault_router
)  # /api/v1/mcp/vault/secrets - User-level vault (merged into sandbox pushes)
app.include_router(
    plugins_router
)  # /api/v1/plugins/* - Agent Plugins packages (install/manage/export)
app.include_router(
    marketplace_router
)  # /api/v1/marketplace/* - Plugin catalog for the Discover tab
app.include_router(
    egress_relay_router
)  # /v1/egress/{grant_id} - Sandbox egress relay (relay-JWT auth, not user auth)
app.include_router(
    livefs_router
)  # /api/v1/livefs/* - Sandbox file mount (mount-token auth, not user auth)
app.include_router(
    mcp_servers_router
)  # /api/v1/workspaces/{id}/mcp/servers - Per-workspace MCP server config
app.include_router(
    native_agents_router
)  # /api/v1/native-agents - native CLI capability inventory
app.include_router(
    agents_router
)  # /api/v1/agents/* - read-only local agent fleet dashboard (loopback only)
app.include_router(
    review_merge_router
)  # /api/v1/reviews/* + /api/v1/merges - review queue and host-local merge
app.include_router(health_router)  # /health - Health check
app.include_router(ready_router)  # /ready - database, redis, mcp, dispatch, daemons
app.include_router(
    readiness_router
)  # /api/v1/system/readiness - authenticated diagnostics
app.include_router(local_update_router)  # /api/v1/system/update/* - local OSS updater
app.include_router(
    preview_redirect_router
)  # /api/v1/preview/{workspace_id}/{port} - Old preview URLs, redirected to /a/<code>
app.include_router(
    wsfiles_router
)  # /api/v1/wsfiles/g/{grant}/{path} - Grant-gated path-style file serving


def bind_dispatch_coordinator(app: Any) -> Any:
    """Bind the process dispatch coordinator with the startup LLM service.

    Live dispatch planning calls the LLM through this coordinator. When
    LLMService failed to start, ``app.state.llm_service`` is missing and the
    coordinator is bound without one; startup continues.
    """
    from src.server.services.dispatch import DispatchCoordinator, bind_process_coordinator

    coordinator = DispatchCoordinator(llm_service=getattr(app.state, "llm_service", None))
    bind_process_coordinator(coordinator)
    return coordinator
