"""Native-runtime chat lane.

This is the transport boundary for a thread whose canonical Agent is a vendor
CLI. It deliberately reuses Conductor's run admission, Redis stream, SSE producer,
checkpoint and finalize pipeline; the native graph only supplies one AIMessage
projection. The CLI still owns its prompt, tool loop, session and cache.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from langgraph.types import Command

from src.server.database.conversation.native_runtime import save_native_session
from src.server.models.chat import ChatRequest, serialize_hitl_response_map
from src.server.services.native_runtime import NativeRuntimeSpec, get_runtime
from src.server.services.native_runtime.graph import build_native_graph
from src.server.services.native_runtime.mcp import NativeMCPServer, collect_builtin_mcp_servers
from src.server.services.native_runtime.memory_token import issue_memory_token
from src.server.services.native_runtime.credentials import minimax_api_key, mimo_api_key
from src.server.services.native_runtime.workspace import (
    materialize_workspace,
    sync_workspace_after_turn,
)
from src.server.services.runs.executor import LocalRunExecutor
from src.server.services.runs.sse_producer import RunSSEProducer
from src.server.services.turn_runtime import TurnRoute

from .error_handling import handle_workflow_error
from .request_prep import (
    build_graph_config,
    init_tracking,
    logger,
    normalize_request_messages,
    turn_surface,
)
from src.server.services.runs.stream_reader import stream_from_log

def _zcode_is_clean_room() -> bool:
    from src.llms.native_cli.zcode import ZCodeCliAdapter

    return ZCodeCliAdapter().clean_room


def _native_cwd(thread_id: str, spec: NativeRuntimeSpec) -> str:
    """The stable cwd a native CLI owns for this thread.

    A native CLI session is cache-sensitive to cwd. The directory is private to
    the thread until the workspace-mirror phase binds it to the real project
    folder; it must never be a random temp directory.
    """
    if spec.cwd:
        return spec.cwd
    path = (
        Path.home()
        / ".cache"
        / "conductor"
        / "native-cli"
        / "workspaces"
        / thread_id
    )
    path.mkdir(parents=True, exist_ok=True)
    return str(path)



def native_capability_servers(*, worker_role: bool, memory_server, skills_server) -> list[dict]:
    """The Conductor MCP servers a native turn gets, as dicts.

    P0 privacy: a Worker (Ticket) runs another vendor's model on delegated
    work, so it gets no ``conductor_memory`` server and cannot write into the
    operator's long-term memory. Router and operator-facing turns keep it.
    """
    servers = [] if worker_role else [memory_server.as_dict()]
    servers.append(skills_server.as_dict())
    return servers

async def _persist_native_delivery_before_run_end(
    source,
    *,
    workspace_id: str,
    native_cwd: str,
    thread_id: str,
    run_id: str,
    attempt: int,
    baseline,
    baseline_error: bool,
):
    """Close the source on every exit and persist only after normal exhaustion."""
    completed = False
    try:
        async for event in source:
            yield event
        completed = True
    finally:
        close = getattr(source, "aclose", None)
        if close is not None:
            await close()
    if completed:
        await _finish_native_worker_delivery(
            workspace_id=workspace_id,
            native_cwd=native_cwd,
            thread_id=thread_id,
            run_id=run_id,
            attempt=attempt,
            baseline=baseline,
            baseline_error=baseline_error,
            run_completed=True,
        )


def _native_notice_run_id(run_id: str | None, *, worker_role: bool) -> str | None:
    """The run id that turns on native notice intake, or None to leave it off.

    Notice intake ends a running CLI turn whenever a steer arrives, so it is
    tied to ``CONDUCTOR_WORKER_IDLE_WAKE_MINUTES`` (default off): with the
    wake off, a Worker turn behaves exactly as before the wake existed.
    """
    if not worker_role or not run_id:
        return None
    from src.server.services.autonomy.worker_idle import idle_wake_minutes

    return run_id if idle_wake_minutes() is not None else None


async def astream_native_workflow(
    *,
    request: ChatRequest,
    thread_id: str,
    run_id: str,
    user_input: str,
    user_id: str,
    route: TurnRoute,
    spec: NativeRuntimeSpec,
    scope,
    run_handle,
    turn_context,
    config,
    is_byok: bool,
    dispatched: bool = False,
    extra_configurable: dict | None = None,
    memory_context_block: str | None = None,
):
    """Stream one native turn through the existing run/SSE pipeline."""
    workspace_id = route.workspace_id
    start_time = time.time()
    handler = None
    token_callback = None
    tool_tracker = None
    native_cwd: str | None = None
    delivery_baseline = None
    delivery_baseline_error = False
    delivery_attempt = 0
    run_execution = None
    worker_role = str(getattr(route.role, "value", route.role)) == "worker"

    try:
        if config is None:
            raise HTTPException(
                status_code=503,
                detail="Native runtime requires a resolved agent configuration",
            )

        token_callback, tool_tracker = init_tracking(thread_id)
        native_cwd = _native_cwd(thread_id, spec)
        if spec.cwd is None and not spec.session_id:
            # First private-cwd turn: seed the CLI cwd from the workspace
            # manifest. An explicit native_cwd is the user's own tree and is
            # never overwritten from a stale manifest.
            try:
                await materialize_workspace(
                    workspace_id=workspace_id,
                    cwd=native_cwd,
                    user_id=user_id,
                )
            except Exception:
                logger.warning(
                    f"[NATIVE_RUN] workspace materialization failed for "
                    f"thread={thread_id}",
                    exc_info=True,
                )
        if worker_role:
            from src.server.database.autonomy import tickets as ticket_db

            delivery_ticket = None
            metadata_error = None
            for metadata_attempt in range(3):
                try:
                    delivery_ticket = await ticket_db.get_ticket_by_worker_thread(
                        thread_id
                    )
                    metadata_error = None
                    break
                except Exception as exc:
                    metadata_error = exc
                    if metadata_attempt < 2:
                        await asyncio.sleep(0.05)
            if metadata_error is not None:
                delivery_baseline_error = True
                logger.warning(
                    f"[NATIVE_RUN] Git baseline metadata unavailable for thread={thread_id}",
                    exc_info=(type(metadata_error), metadata_error, metadata_error.__traceback__),
                )
            mandate = dict((delivery_ticket or {}).get("mandate") or {})
            read_only_worker = bool(mandate.get("read_only") or mandate.get("speculative"))
            (
                delivery_baseline,
                setup_baseline_error,
                delivery_attempt,
            ) = await _setup_worker_git_delivery(
                thread_id,
                run_id,
                native_cwd,
                ticket=delivery_ticket,
                allow_capture=not read_only_worker and not delivery_baseline_error,
            )
            delivery_baseline_error = delivery_baseline_error or setup_baseline_error
        runtime_kwargs: dict = {"cwd": native_cwd}
        if str(getattr(route.role, "value", route.role)) == "worker":
            if spec.provider == "codex":
                runtime_kwargs["isolate_user_instructions"] = True
                runtime_kwargs["developer_instructions"] = (
                    "Follow the fixed Ticket delivery contract at the start of "
                    "the user prompt. It controls this Ticket's scope and report. "
                    "Memory instructions do not apply, wherever they come from "
                    "(operator-global guidance or a repository AGENTS.md copy of "
                    "it): do not search Fleet Memory, memfw or agent memory files "
                    "and do not write memory records. Operator-global instructions "
                    "about publishing, notifications, and similar work do not apply."
                )
            elif spec.provider in {"claude", "claude-code"}:
                runtime_kwargs["safe_mode"] = True
        # The clean-room zcode-cli has no MCP bridge and no permission
        # broker, so none of that is injected for it.
        zcode_clean_room = spec.provider == "zcode" and _zcode_is_clean_room()
        if spec.provider in {
            "codex", "deepseek-dsh", "mimo", "minimax", "zcode", "kimi", "grok"
        } and not zcode_clean_room:
            try:
                builtin_mcp = await collect_builtin_mcp_servers(
                    user_id=user_id, workspace_id=workspace_id
                )
                if builtin_mcp:
                    runtime_kwargs["mcp_servers"] = [
                        server.as_dict() for server in builtin_mcp
                    ]
            except Exception:
                logger.warning(
                    f"[NATIVE_RUN] builtin MCP bridge skipped for thread={thread_id}",
                    exc_info=True,
                )
        if spec.provider in {
            "codex", "deepseek-dsh", "minimax", "mimo", "zcode", "kimi", "grok"
        } and not zcode_clean_room:
            repo_root = Path(__file__).resolve().parents[4]
            capability_env = (
                ("CONDUCTOR_API_BASE", os.environ.get("NATIVE_MEMORY_API_BASE", "http://127.0.0.1:8000")),
                (
                    "CONDUCTOR_MEMORY_TOKEN",
                    issue_memory_token(user_id, thread_id, workspace_id=workspace_id),
                ),
            )
            memory_server = NativeMCPServer(
                name="conductor_memory",
                command="uv",
                args=(
                    "run",
                    "--directory",
                    str(repo_root),
                    "python",
                    str(repo_root / "mcp_servers" / "native_memory_mcp_server.py"),
                ),
                env=capability_env,
            )
            skills_server = NativeMCPServer(
                name="conductor_skills",
                command="uv",
                args=(
                    "run",
                    "--directory",
                    str(repo_root),
                    "python",
                    str(repo_root / "mcp_servers" / "native_skills_mcp_server.py"),
                ),
                env=capability_env,
            )
            runtime_kwargs["mcp_servers"] = [
                *native_capability_servers(
                    worker_role=worker_role,
                    memory_server=memory_server,
                    skills_server=skills_server,
                ),
                *runtime_kwargs.get("mcp_servers", []),
            ]

        if spec.provider == "codex":
            # The native lane uses the bidirectional app-server protocol so
            # token deltas and human approvals are real. The standalone
            # NativeAgent tool keeps its one-shot exec adapter.
            runtime_kwargs["transport"] = "app-server"
            runtime_kwargs["broker_thread_id"] = thread_id
            runtime_kwargs["approval_policy"] = "on-request"
        elif spec.provider == "minimax":
            # Project .mcp.json is authoritative for mcode; keep the bridged
            # set deliberately small so cold-start discovery stays bounded.
            limit = int(os.environ.get("NATIVE_CLI_MINIMAX_MCP_LIMIT", "4"))
            if runtime_kwargs.get("mcp_servers"):
                runtime_kwargs["mcp_servers"] = runtime_kwargs["mcp_servers"][:limit]
            key = await minimax_api_key(user_id)
            if key:
                runtime_kwargs["api_key"] = key
                runtime_kwargs["model"] = spec.model or "MiniMax-M3"
        elif spec.provider == "grok":
            runtime_kwargs["read_only"] = False
        elif spec.provider == "stepfun":
            runtime_kwargs["read_only"] = False
            if os.environ.get("NATIVE_CLI_STEPFUN_APPROVALS", "").strip().lower() in {
                "1", "true", "yes", "on"
            }:
                runtime_kwargs["transport"] = "stepfun-rpc"
                runtime_kwargs["broker_thread_id"] = thread_id
        elif spec.provider == "zcode":
            runtime_kwargs["read_only"] = False
            if not zcode_clean_room and os.environ.get(
                "NATIVE_CLI_ZCODE_APPROVALS", ""
            ).strip().lower() in {"1", "true", "yes", "on"}:
                runtime_kwargs["transport"] = "zcode-approval"
                runtime_kwargs["broker_thread_id"] = thread_id
        elif spec.provider == "mimo":
            key, base = await mimo_api_key(user_id)
            if key:
                runtime_kwargs["api_key"] = key
                runtime_kwargs["base_url"] = base
                runtime_kwargs["model"] = spec.model or "xiaomi/mimo-v2.6-pro"
            # MiMo's MCP local processes start at session bootstrap. Native
            # threads own their workspace mirror, so approve routine calls.
            runtime_kwargs["yolo"] = True
        acp_providers = {
            name.strip()
            for name in os.environ.get("NATIVE_CLI_ACP", "").split(",")
            if name.strip()
        }
        if spec.provider in acp_providers:
            runtime_kwargs["transport"] = "acp"
            runtime_kwargs["broker_thread_id"] = thread_id
            if spec.provider == "deepseek-dsh":
                runtime_kwargs["argv"] = ["dsh", "--profile", "acp"]
                runtime_kwargs["model_config"] = os.environ.get(
                    "NATIVE_CLI_ACP_DSH_MODEL",
                    '["deepseek-account","deepseek-flash"]',
                )
            elif spec.provider == "minimax":
                runtime_kwargs["argv"] = ["mcode", "acp"]
                runtime_kwargs["config_options"] = {"permissionMode": "default"}
            elif spec.provider == "kimi":
                runtime_kwargs["argv"] = ["kimi", "acp"]
            elif spec.provider == "grok":
                from src.llms.native_cli.grok import ensure_grok_approval_home

                runtime_kwargs["argv"] = ["grok", "agent", "stdio"]
                runtime_kwargs["env"] = {"GROK_HOME": ensure_grok_approval_home()}
            elif spec.provider == "mimo":
                key, base = await mimo_api_key(user_id)
                model = spec.model or "xiaomi/mimo-v2.6-pro"
                bare = model.split("/", 1)[-1]
                config = {
                    "$schema": "https://mimo.xiaomi.com/mimocode/config.json",
                    "model": model if "/" in model else f"xiaomi/{model}",
                    "provider": {
                        "xiaomi": {
                            "options": {
                                "baseURL": base
                                or "https://token-plan-sgp.xiaomimimo.com/v1",
                                "apiKey": "{env:MIMOCODE_MIMO_API_KEY}",
                            },
                            "models": {bare: {"name": bare}},
                        }
                    },
                    "permission": {
                        "external_directory": "ask",
                        "edit": "allow",
                        "write": "allow",
                    },
                }
                runtime_kwargs["argv"] = ["mimo", "acp"]
                runtime_kwargs["env"] = {
                    "MIMOCODE_MIMO_API_KEY": key or "",
                    "MIMOCODE_CONFIG_CONTENT": json.dumps(config),
                }
        runtime = get_runtime(spec.provider, **runtime_kwargs)

        async def record_usage(data):
            token_callback.record_native_usage(
                model_name=spec.model or spec.provider,
                input_tokens=int(data.get("input_tokens") or 0),
                output_tokens=int(data.get("output_tokens") or 0),
                cache_read_tokens=int(data.get("cache_read_tokens") or 0),
                cache_write_tokens=int(data.get("cache_write_tokens") or 0),
                served_model=spec.provider,
            )

        graph = build_native_graph(
            runtime=runtime,
            spec=spec,
            checkpointer=run_handle.checkpointer,
            on_session=lambda session_id: save_native_session(thread_id, session_id),
            on_usage=record_usage,
            context_block=memory_context_block,
            thread_id=thread_id,
            # A Worker turn takes in the notices the server sends into it, so
            # a Worker that stopped moving is woken instead of only looked at.
            # Only while the idle wake is on: with it off, a steer waits for
            # the run's end as it always did.
            run_id=_native_notice_run_id(run_id, worker_role=worker_role),
            # A Worker turn that wrote no text has no completion report; it
            # must reach the executor as a completed run that settles waiting.
            allow_empty_answer=worker_role,
        )

        effective_model = config.llm.name if getattr(config, "llm", None) else None
        graph_config = build_graph_config(
            thread_id=thread_id,
            user_id=user_id,
            workspace_id=workspace_id,
            timezone_str=turn_context.tool_timezone,
            token_callback=token_callback,
            request=request,
            effective_model=effective_model,
            extra_configurable=extra_configurable,
            run_id=run_id,
            turn_index=run_handle.turn_index,
            surface=turn_surface(request, None),
        )
        if request.hitl_response:
            input_state = Command(
                resume=serialize_hitl_response_map(request.hitl_response)
            )
        else:
            messages = normalize_request_messages(request)
            if not messages and user_input:
                messages = [
                    {
                        "id": f"native-input-{run_id}",
                        "role": "user",
                        "content": user_input,
                    }
                ]
            input_state = {"messages": messages}

        handler = RunSSEProducer(
            thread_id=thread_id,
            run_id=run_id,
            token_callback=token_callback,
            tool_tracker=tool_tracker,
            agent_config=config,
            user_id=user_id,
            turn_index=run_handle.turn_index,
        )
        manager = LocalRunExecutor.get_instance()

        def on_registered() -> None:
            nonlocal run_execution
            run_execution = getattr(manager, "executions", {}).get(
                (thread_id, run_id)
            )
            scope.transfer_to_executor()

        source_generator = handler.stream_workflow(
            graph=graph,
            input_state=input_state,
            config=graph_config,
        )
        if worker_role:
            workflow_generator = _persist_native_delivery_before_run_end(
                source_generator,
                workspace_id=workspace_id,
                native_cwd=native_cwd,
                thread_id=thread_id,
                run_id=run_id,
                attempt=delivery_attempt,
                baseline=delivery_baseline,
                baseline_error=delivery_baseline_error,
            )
        else:
            workflow_generator = source_generator
        run_execution = await manager.start_run(
            thread_id=thread_id,
            run_id=run_id,
            workflow_generator=workflow_generator,
            metadata={
                "workspace_id": workspace_id,
                "user_id": user_id,
                "sandbox_id": None,
                "sandbox": None,
                "started_at": datetime.now().isoformat(),
                "start_time": start_time,
                "is_byok": is_byok,
                "locale": request.locale,
                "timezone": turn_context.tool_timezone,
                "handler": handler,
                "token_callback": token_callback,
                "run_handle": run_handle,
                "native_provider": spec.provider,
                "native_session_id": spec.session_id,
            },
            graph=graph,
            on_registered=on_registered,
        )
        scope.release_admission()

        logger.info(
            f"[NATIVE_RUN] thread_id={thread_id} provider={spec.provider} "
            f"run={run_id} started"
        )
        async for event in stream_from_log(thread_id, run_id, last_event_id=None):
            yield event
        if run_execution is not None and run_execution.task is not None:
            await asyncio.shield(run_execution.task)
        if not worker_role:
            await _sync_native_workspace(workspace_id, native_cwd, thread_id)

    except Exception as exc:
        if native_cwd is not None:
            await _sync_native_workspace(workspace_id, native_cwd, thread_id)
        async for event in handle_workflow_error(
            exc,
            thread_id=thread_id,
            user_id=user_id,
            workspace_id=workspace_id,
            handler=handler,
            token_callback=token_callback,
            scope=scope,
            start_time=start_time,
            request=request,
            is_byok=is_byok,
            turn_context=turn_context,
        ):
            yield event
        raise
    finally:
        scope.release_admission()


async def _finish_native_worker_delivery(
    *,
    workspace_id: str,
    native_cwd: str,
    thread_id: str,
    run_id: str,
    attempt: int,
    baseline,
    baseline_error: bool,
    run_completed: bool,
) -> None:
    """Sync and persist the Git result after the executor finishes its child.

    The report has arrived: the branch HEAD is frozen here, and the delivery
    is measured against that commit. The measurement runs before the
    workspace sync, so a commit that lands while the sync runs is after the
    report and is not this delivery's. The frozen HEAD is recorded with the
    result, so a later settlement measures the same commit instead of a later
    one.
    """
    if not run_completed:
        await _sync_native_workspace(workspace_id, native_cwd, thread_id)
        return

    from src.server.services.autonomy.delivery_freeze import (
        DeliveryFreeze,
        FrozenDelivery,
        delivery_state_fields,
        measure_delivery,
        resolve_delivery_head,
    )
    from src.server.services.autonomy.git_delivery import (
        AsyncWorkspaceDeliveryLock,
        GitDeliveryError,
        safe_git_workspace_summary,
    )

    if baseline is None and not baseline_error:
        await _sync_native_workspace(workspace_id, native_cwd, thread_id)
        return
    freeze = None
    git_summary = None
    commit_delivery = "unknown"
    commits = None
    try:
        async with AsyncWorkspaceDeliveryLock(native_cwd, timeout=30.0):
            # The completion contract needs a commit verdict as well as the
            # workspace state: a clean tree with no new commit delivered
            # nothing. Both are measured against the frozen HEAD, read here
            # and now, before the workspace sync runs.
            delivery = await measure_delivery(
                native_cwd,
                baseline=baseline,
                baseline_unavailable=baseline_error,
            )
            freeze = delivery.freeze
            git_summary = delivery.summary
            commit_delivery = delivery.verdict
            commits = delivery.commits
    except GitDeliveryError:
        if freeze is None:
            freeze = await asyncio.to_thread(resolve_delivery_head, native_cwd)
        git_summary = await asyncio.to_thread(
            safe_git_workspace_summary,
            native_cwd,
            None,
            baseline_unavailable=True,
        )
        commit_delivery = "unknown"
    except (OSError, UnicodeError):
        # The snapshot can fail outside the Git layer (an unreadable entry in
        # the workspace). The sync used to run before the measurement, so it
        # must still run before that failure propagates as it always did.
        await _sync_native_workspace(workspace_id, native_cwd, thread_id)
        raise
    # The workspace manifest is synced after the delivery was measured: the
    # measurement reads the workspace itself, and a commit that lands while
    # the sync runs is not this delivery's.
    await _sync_native_workspace(workspace_id, native_cwd, thread_id)
    if git_summary is None:
        # None means the checked workspace is not a Git repository; an empty
        # durable summary records that clean/unsupported check for this run.
        git_summary = ""
    result_attempt = attempt
    if attempt == 0:
        # A transient baseline metadata read can fail before it supplies the
        # attempt number. Resolve it again at completion, fenced to this run,
        # so the premarked unknown baseline still gets a durable result.
        from src.server.database.autonomy import tickets as ticket_db

        try:
            ticket = await ticket_db.get_ticket_by_worker_thread(thread_id)
        except Exception:
            logger.warning(
                f"[NATIVE_RUN] Git completion metadata unavailable for thread={thread_id}",
                exc_info=True,
            )
            return
        if (
            ticket is None
            or str(ticket.get("status") or "") != "running"
            or str(ticket.get("worker_run_id") or "") not in ("", run_id)
        ):
            return
        result_attempt = int(ticket.get("attempts") or 0)
    try:
        await _save_native_git_delivery(
            thread_id,
            run_id,
            result_attempt,
            delivery_state_fields(
                freeze
                if freeze is not None
                else DeliveryFreeze(),
                FrozenDelivery(
                    verdict=commit_delivery,
                    summary=git_summary,
                    commits=commits,
                    freeze=freeze,
                ),
                thread_id=thread_id,
                run_id=run_id,
                attempt=result_attempt,
                # No uncommitted work before the run: an unchanged clean
                # workspace then really holds nothing undelivered.
                baseline_clean=bool(
                    baseline is not None
                    and baseline.root_id is not None
                    and not baseline.entries
                ),
            ),
            allow_unpublished_run=True,
        )
    except Exception:
        logger.warning(
            f"[NATIVE_RUN] Git completion status persistence failed for thread={thread_id}",
            exc_info=True,
        )


async def _save_native_git_delivery(
    thread_id: str,
    run_id: str,
    attempt: int,
    fields: dict,
    *,
    allow_unpublished_run: bool = False,
) -> None:
    from src.server.database.autonomy import tickets as ticket_db

    ticket = await ticket_db.get_ticket_by_worker_thread(thread_id)
    if ticket is None or str(ticket.get("status") or "") != "running":
        raise RuntimeError("Worker Ticket is no longer running")
    if int(ticket.get("attempts") or 0) != attempt:
        raise RuntimeError("Worker Ticket attempt changed")
    current_run = str(ticket.get("worker_run_id") or "")
    if current_run != run_id and not (allow_unpublished_run and not current_run):
        raise RuntimeError("Worker Ticket run changed")
    mandate = dict(ticket.get("mandate") or {})
    if not current_run and "git_delivery_result" in fields:
        baseline = mandate.get("git_delivery_baseline")
        if not (
            isinstance(baseline, dict)
            and baseline.get("version") == 2
            and baseline.get("thread_id") == thread_id
            and baseline.get("run_id") == run_id
            and baseline.get("attempt") == attempt
            and not baseline.get("pending")
        ):
            raise RuntimeError("Worker Ticket baseline is not bound to this unpublished run")
    mandate.update(fields)
    kwargs = {
        "expected_status": "running",
        "expected_thread_id": thread_id,
        "mandate": mandate,
    }
    if isinstance(ticket.get("revision"), int):
        kwargs["expected_revision"] = int(ticket["revision"])
    elif not current_run:
        raise RuntimeError("Worker Ticket revision is unavailable before run publication")
    if current_run:
        kwargs["expected_run_id"] = run_id
    if await ticket_db.update_ticket(str(ticket["ticket_id"]), **kwargs) is None:
        raise RuntimeError("Worker Ticket changed while saving Git delivery status")


async def _setup_worker_git_delivery(
    thread_id: str,
    run_id: str,
    native_cwd: str,
    *,
    ticket: dict | None = None,
    allow_capture: bool = True,
):
    from src.server.services.autonomy.git_delivery import (
        DELIVERY_ORIGIN_KEY,
        AsyncWorkspaceDeliveryLock,
        GitDeliveryError,
        GitSnapshot,
        capture_git_snapshot,
        delivery_origin_record,
        with_delivery_origin,
    )

    baseline = None
    baseline_error = False
    attempt = 0
    try:
        from src.server.database.autonomy import tickets as ticket_db

        if ticket is None:
            ticket = await ticket_db.get_ticket_by_worker_thread(thread_id)
        if ticket is None:
            return None, True, 0
        attempt = int((ticket or {}).get("attempts") or 0)
        mandate = dict((ticket or {}).get("mandate") or {})
        read_only_worker = bool(
            mandate.get("read_only") or mandate.get("speculative")
        )
        cwd_id = hashlib.sha256(os.fsencode(os.path.realpath(native_cwd))).hexdigest()
        prior = mandate.get("git_delivery_baseline")
        reusable = (
            not read_only_worker
            and isinstance(prior, dict)
            and prior.get("version") == 2
            and prior.get("thread_id") == thread_id
            and prior.get("cwd_id") == cwd_id
            and isinstance(prior.get("snapshot"), dict)
            and not prior.get("pending")
            and not prior.get("snapshot", {}).get("error")
        )
        if read_only_worker:
            return None, False, attempt
        if reusable:
            baseline = GitSnapshot.from_record(prior["snapshot"])
            baseline_error = baseline is None
        elif (
            isinstance(prior, dict)
            and prior.get("version") == 2
            and prior.get("thread_id") == thread_id
            and prior.get("cwd_id") == cwd_id
            and int(prior.get("attempt") or 0) == attempt
            and isinstance(prior.get("snapshot"), dict)
            and not prior.get("pending")
            and prior["snapshot"].get("error")
        ):
            # Do not take a post-write snapshot for the same run attempt.
            baseline_error = True
        elif not allow_capture:
            baseline_error = True
        else:
            try:
                async with AsyncWorkspaceDeliveryLock(native_cwd, timeout=30.0):
                    baseline = await asyncio.to_thread(capture_git_snapshot, native_cwd)
            except GitDeliveryError:
                baseline_error = True
        snapshot_record = (
            baseline.to_record()
            if baseline is not None
            else {"version": 1, "error": True}
        )
        binding = {
            "version": 2,
            "thread_id": thread_id,
            "run_id": run_id,
            "attempt": attempt,
            "cwd_id": cwd_id,
            "snapshot": snapshot_record,
            "pending": False,
        }
        fields: dict = {"git_delivery_baseline": binding}
        # The commit check counts from the Ticket's first baseline, not from
        # this run's: a retry or a new thread must still see commits an
        # earlier run made. worker.py marks the origin unknown when a
        # baseline existed before the origin was ever recorded.
        origin = mandate.get(DELIVERY_ORIGIN_KEY)
        if not isinstance(origin, dict):
            # Any earlier binding that is not this launch's placeholder means
            # an earlier run; a run without a baseline cannot know its origin.
            prior_run = isinstance(prior, dict) and not prior.get("pending")
            origin = delivery_origin_record(baseline, prior_run=prior_run)
            fields[DELIVERY_ORIGIN_KEY] = origin
        await _save_native_git_delivery(
            thread_id,
            run_id,
            attempt,
            fields,
            allow_unpublished_run=True,
        )
        baseline = with_delivery_origin(baseline, origin)
    except Exception:
        # Baseline and metadata faults must not prevent the Worker launch.
        logger.warning(
            f"[NATIVE_RUN] Git baseline setup failed for thread={thread_id}; continuing with unknown status",
            exc_info=True,
        )
        baseline = None
        baseline_error = True
    return baseline, baseline_error, attempt


async def _sync_native_workspace(
    workspace_id: str, native_cwd: str, thread_id: str
) -> None:
    """Bridge native CLI writes into the workspace file manifest.

    Best-effort: the answer is already delivered, and a manifest fault must not
    turn a completed native turn into a failure. The next successful sync
    repairs the rows.
    """
    try:
        added, removed = await sync_workspace_after_turn(
            workspace_id=workspace_id, cwd=native_cwd
        )
        if added or removed:
            logger.info(
                f"[NATIVE_RUN] workspace synced thread={thread_id} "
                f"upserts={added} removed={removed}"
            )
    except Exception:
        logger.warning(
            f"[NATIVE_RUN] workspace sync failed for thread={thread_id}",
            exc_info=True,
        )
