"""PTC (Programmatic Tool Calling) workflow — async SSE generator.

This module contains the ``astream_ptc_workflow`` async generator, refactored
from the monolithic ``chat_handler.py``.  Request preparation, persistence,
error handling, and streaming logic is delegated to ``request_prep`` and
``services.runs.admission``; PTC-specific concerns (workspace session,
sandbox, background subagent orchestration, completion callback)
remain inline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import datetime
from functools import partial

from fastapi import HTTPException
from langgraph.types import Command

from src.server.services.project_context import ProjectContext, run_with_project
from src.server.app import setup
from src.server.database.conversation.threads_read import (
    read_thread_subagents_allowed,
)
from src.server.database.conversation.native_runtime import get_thread_metadata
from src.server.services.native_runtime import read_spec
from src.server.database.user import get_user_profile_for_prompt
from src.server.database.workspace import update_workspace_activity
from src.server.services.computer_disk import TURN_MEASURE_MIN_INTERVAL_SECONDS
from src.server.services.history.window import runs_held
from src.server.services.runs.sse_producer import RunSSEProducer
from src.server.models.chat import (
    ChatRequest,
    serialize_hitl_response_map,
)
from src.server.services.runs.task_registry_store import BackgroundRegistryStore
from src.server.services.runs.executor import LocalRunExecutor
from src.server.services.workspace_manager import WorkspaceManager
from src.server.services.workspace_layout import resolve_project_placement
from src.observability import (
    chat_turn_phase_duration_ms,
    safe_record,
)
from src.server.utils.directive_context import (
    build_directive_reminder,
    parse_directive_contexts,
)
from src.server.utils.widget_context import (
    build_widget_context_reminder,
    parse_widget_contexts,
    serialize_widget_contexts_for_metadata,
)
from src.server.utils.credit_resume_context import build_credit_resume_update
from src.server.utils.multimodal_context import (
    build_attachment_metadata,
    parse_multimodal_contexts,
)

from ptc_agent.agent.graph import build_ptc_graph_with_session
from src.server.services.credit_gate import run_with_credit_gate

from .native_run import astream_native_workflow
from .request_prep import (
    DISPATCH_STARTED_MARKER,
    _resolve_fork,
    build_graph_config,
    build_turn_context,
    turn_start_checkpoint_id,
    read_disk_notice,
    ensure_thread,
    keep_named_model,
    init_tracking,
    inject_inline_reminders,
    logger,
    normalize_request_messages,
    prepare_skill_contexts,
    process_hitl_response,
    provenance_metadata,
    retried_on_its_surface,
    serialize_context_metadata,
    surface_stamp,
    turn_skill_names,
    turn_surface,
    user_skill_commands,
)
from src.server.contracts.turn_kinds import announced
from src.server.services.credit_gate_port import build_run_credit_gate
from src.server.services.runs.admission import (
    RunScope,
    begin_run,
    dedup_retransmit_or_raise,
)
from src.server.services.turn_runtime import TurnRoute

from .admission_gate import agent_steer_of, steer_allowed, wait_or_steer
from .attachments import attach_request_files
from .error_handling import handle_workflow_error
from src.server.services.llm.clients import is_own_key_turn
from src.server.services.llm.config import apply_fetch_override, resolve_llm_config
from src.server.services.llm.thread_model import NamedModel
from .steering import open_with_steers_ahead
from src.server.services.runs.stream_reader import stream_from_log
from .detached import fire_and_forget as _fire_and_forget
from .turn_prelude import TurnTail, prepare_turn

#: Collaborators ``turn_prelude`` looks up through this module at call time,
#: so ``patch("src.server.handlers.chat.ptc_run.<name>")`` still reaches them.
_TURN_PRELUDE_LOOKUPS = (
    get_user_profile_for_prompt,
    LocalRunExecutor,
    parse_widget_contexts,
    serialize_widget_contexts_for_metadata,
    build_attachment_metadata,
    parse_multimodal_contexts,
    _resolve_fork,
    build_turn_context,
    read_disk_notice,
    ensure_thread,
    process_hitl_response,
    provenance_metadata,
    retried_on_its_surface,
    serialize_context_metadata,
    surface_stamp,
    RunScope,
    begin_run,
    dedup_retransmit_or_raise,
    agent_steer_of,
    steer_allowed,
    wait_or_steer,
    handle_workflow_error,
    is_own_key_turn,
    resolve_llm_config,
)


#: Roles whose graph is built without Task/TaskOutput/RunWorkflow (RA-3).
NO_SUBAGENT_ROLES = frozenset({"supervisor", "worker"})


async def _resolve_project(session, workspace_id: str) -> ProjectContext | None:
    """The workspace folder this turn runs in, for the sandbox path layer.

    One read: the folder, and the folders parked beside it on the same machine
    so the agent can name a sibling directory as someone else's work rather
    than as material it is expected to read.

    Propagates ``WorkspaceLayoutUnavailable``, which fails the turn. The turn
    would otherwise run at the computer root, where it writes into a shared
    directory and mirrors every sibling's files back as its own.
    """
    sandbox = getattr(session, "sandbox", None)
    if sandbox is None:
        return None
    placement = await resolve_project_placement(workspace_id, root=sandbox.working_dir)
    return ProjectContext(
        workspace_id=workspace_id,
        dir_name=placement.dir_name,
        sibling_dir_names=placement.sibling_dir_names,
        layout_origin=placement.layout_origin,
        previous_dir_names=placement.previous_dir_names,
    )




def _artifact_hook(session, *, thread_id: str, project: ProjectContext | None):
    """The finalize's pre-terminal hook: upload the sandbox images the turn's
    saved messages still name, returning each one's URL for its row."""

    async def capture_artifacts(image_paths) -> dict[str, str]:
        if not (session and session.sandbox):
            return {}
        from src.server.services.persistence.image_capture import capture_images

        return await capture_images(
            image_paths, session.sandbox, thread_id, project=project
        )

    return capture_artifacts


async def astream_ptc_workflow(
    request: ChatRequest,
    thread_id: str,
    run_id: str,
    user_input: str,
    user_id: str,
    route: TurnRoute,
    is_byok: bool = False,
    config=None,
    dispatched: bool = False,
    steerable: bool = True,
    run_metadata: dict | None = None,
    extra_configurable: dict | None = None,
    extra_tools: list | None = None,
    named_model: NamedModel | None = None,
    superagent_lane=None,
):
    """Async generator that streams PTC agent workflow events.

    ``run_id`` is generated at the handler entry in ``threads.py`` and is
    1:1 with ``conversation_response_id``. State (BTM, persistence, Redis
    stream key) is keyed by ``(thread_id, run_id)`` so concurrent turns
    on the same thread share no cross-turn state by construction.

    ``dispatched`` marks the call as an X-Dispatch=background invocation
    whose BTM placeholder was created upstream in ``threads.py``. It, a retry
    and ``steerable=False`` all make a running turn a 409 instead of a
    steer; see ``steer_allowed``. ``run_metadata`` is the caller's own START
    stamp on the run row, which the run's finalize hooks read.
    ``extra_configurable`` joins the graph config's ``configurable``, where
    the turn's tools read it.

    ``named_model`` is the model a client named for this thread, kept on it
    once the turn is admitted; automations never pass one. ``route`` comes
    from ``turn_route`` and has no default, so routing decides the workspace
    and the role in one place and no caller runs Home as an analyst by
    leaving one out.
    """
    workspace_id = route.workspace_id
    start_time = time.time()
    handler = None
    token_callback = None
    tool_tracker = None
    ptc_graph = None

    # Phase timing — collects wall-clock durations for each hot-path phase.
    # Emits a single structured summary line when the workflow starts.
    _phase_times: dict[str, float] = {}
    _phase_t0 = start_time

    def _mark_phase(name: str) -> None:
        nonlocal _phase_t0
        now = time.time()
        _phase_times[name] = (now - _phase_t0) * 1000  # ms
        _phase_t0 = now

    # The tail owns the RunScope (burst lease, admission lock, and open START
    # row until the executor's done-callback is armed by transfer_to_executor
    # below). It wraps every lane: setup disconnects fail_open, errors go to
    # handle_workflow_error, and the admission lock is released last.
    tail = TurnTail(
        request=request,
        thread_id=thread_id,
        user_id=user_id,
        workspace_id=workspace_id,
        start_time=start_time,
        is_byok=is_byok,
    )
    scope = tail.scope
    async with tail:
        try:
            # Admission, dedup, steer-or-admit, thread row, LLM config, START
            # txn, memory policy and recall: see ``turn_prelude``.
            prelude = await prepare_turn(
                tail,
                request=request,
                thread_id=thread_id,
                run_id=run_id,
                user_input=user_input,
                user_id=user_id,
                route=route,
                is_byok=is_byok,
                config=config,
                dispatched=dispatched,
                steerable=steerable,
                run_metadata=run_metadata,
                superagent_lane=superagent_lane,
            )
            if prelude.done:
                for event in prelude.early_events:
                    yield event
                return
            request = prelude.request
            superagent_lane = prelude.superagent_lane
            config = prelude.config
            manager = prelude.manager
            workspace_manager = prelude.workspace_manager
            needs_startup = prelude.needs_startup
            prior_thread = prelude.prior_thread
            user_profile = prelude.user_profile
            turn_context = prelude.turn_context
            is_checkpoint_replay = prelude.is_checkpoint_replay
            effective_model = prelude.effective_model
            own_key = prelude.own_key
            query_metadata = prelude.query_metadata
            widget_ctxs = prelude.widget_ctxs
            run_handle = prelude.run_handle
            memory_tool_policy = prelude.memory_tool_policy
            memory_enabled = prelude.memory_enabled
            memory_context_block = prelude.memory_context_block


            # A native thread is executed by its vendor CLI, not by the PTC graph.
            # The binding is created with the thread and immutable afterwards.
            try:
                thread_metadata = await get_thread_metadata(thread_id)
                native_spec = read_spec({"metadata": thread_metadata})
            except Exception:
                # A metadata read fault must not fail a PTC turn. A native thread
                # needs this row; its absence on a fault is surfaced by the
                # native lane's own runtime error, never by a fabricated PTC run.
                logger.warning(
                    f"[NATIVE_RUNTIME] metadata read failed for thread={thread_id}; "
                    "falling back to PTC routing",
                    exc_info=True,
                )
                native_spec = None
            requested_native = getattr(request, "native_provider", None)
            requested_native = (
                requested_native.strip()
                if isinstance(requested_native, str)
                else ""
            )
            if native_spec is not None:
                if not memory_tool_policy.native_runtime_allowed:
                    # The dispatched HTTP primer needs its durable START receipt
                    # before the terminal refusal frame can be consumed by the
                    # detached stream reader.
                    if dispatched:
                        yield DISPATCH_STARTED_MARKER
                    from .error_handling import MemoryToolPolicyRefusal

                    if memory_tool_policy.deny_all:
                        raise MemoryToolPolicyRefusal(
                            "Memory tool policy is unavailable, so native runtime "
                            "execution is blocked.",
                            code="memory_tool_policy_unavailable",
                        )
                    raise MemoryToolPolicyRefusal(
                        "An owner-approved memory constraint blocks the native runtime."
                    )
                from src.server.services.features import user_feature_enabled

                if not await user_feature_enabled(user_id, "native_runtime"):
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "Native agent runtime is disabled for this user; "
                            "enable native_runtime in Settings → Experiments"
                        ),
                    )
                if requested_native and requested_native != native_spec.provider:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Thread is bound to native runtime "
                            f"{native_spec.provider!r}; it cannot be sent to "
                            f"{requested_native!r}"
                        ),
                    )
                if request.checkpoint_id:
                    raise HTTPException(
                        status_code=409,
                        detail="Native-runtime threads do not support checkpoint replay yet",
                    )
                if dispatched:
                    yield DISPATCH_STARTED_MARKER
                async for event in astream_native_workflow(
                    request=request,
                    thread_id=thread_id,
                    run_id=run_id,
                    user_input=user_input,
                    user_id=user_id,
                    route=route,
                    spec=native_spec,
                    scope=scope,
                    run_handle=run_handle,
                    turn_context=turn_context,
                    config=config,
                    is_byok=is_byok,
                    dispatched=dispatched,
                    extra_configurable=extra_configurable,
                    memory_context_block=memory_context_block,
                ):
                    yield event
                return

            if requested_native:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Thread is on the PTC runtime; it cannot switch to "
                        f"{requested_native!r}. Create a new native thread instead."
                    ),
                )

            # R8c P lane: Router/Watchdog turn without a sandbox session or graph.
            if superagent_lane is not None:
                from .superagent_run import astream_superagent_workflow, check_p_lane_admissible

                await check_p_lane_admissible(
                    request=request, run_handle=run_handle, thread_id=thread_id,
                    run_id=run_id, lane=superagent_lane,
                )
                if dispatched:
                    yield DISPATCH_STARTED_MARKER
                await keep_named_model(thread_id, named_model)
                async for event in astream_superagent_workflow(
                    request=request,
                    thread_id=thread_id,
                    run_id=run_id,
                    user_input=user_input,
                    user_id=user_id,
                    route=route,
                    lane=superagent_lane,
                    scope=scope,
                    run_handle=run_handle,
                    turn_context=turn_context,
                    config=config,
                    is_byok=is_byok,
                    memory_context_block=memory_context_block,
                    memory_tool_policy=memory_tool_policy,
                ):
                    yield event
                return

            if dispatched:
                # Durable receipt (v4 2.4c): the dispatch handler is priming this
                # generator and returns its 200 response only once the START txn
                # above has committed. The marker never reaches the SSE stream.
                yield DISPATCH_STARTED_MARKER

            await keep_named_model(thread_id, named_model)

            # =====================================================================
            # Token and Tool Tracking
            # =====================================================================

            token_callback, tool_tracker = init_tracking(thread_id)

            # Runtime credit gate (None when platform gating is inactive): the
            # run's spend meter, lease, and refresher. Admission above is its
            # seed verdict; the stream wrapper below owns its lifetime.
            credit_gate = build_run_credit_gate(
                user_id,
                run_id,
                token_callback,
                tool_tracker,
                effective_model,
                is_byok=own_key,
            )

            _mark_phase("db_setup")

            # =====================================================================
            # Session and Graph Setup
            # =====================================================================

            # Propagate fetch model override to tool context
            apply_fetch_override(config)

            _mark_phase("pre_session")

            sandbox_id = None

            # The turn's skill state, folded to one value so the acquire can tell
            # a warm sandbox its skills moved (upload/delete/disable) without any
            # extra read — the bundle behind these fields was already loaded by
            # the config resolve above.
            from src.server.services.user_skills import skills_delivery_signature

            skills_signature = skills_delivery_signature(
                config.user_skill_dir, config.disabled_skills
            )

            # ``workspace_manager`` and ``needs_startup`` were resolved above for
            # the pre-steering stale-cancel hook. Reuse them — recomputing here
            # would race with a concurrent reconnect that could flip the state.
            #
            # The branch below emits an early "Starting workspace..." SSE pair so
            # the frontend can show a spinner instead of a silent wait. This is
            # broader than the old `ws_status == "stopped"` check — it also fires
            # on server-restart cold starts (workspace running in Daytona but no
            # session in memory). The extra "starting/ready" SSE pair is harmless.
            if not needs_startup:
                session = await workspace_manager.get_session_for_workspace(
                    workspace_id,
                    user_id=user_id,
                    skills_signature=skills_signature,
                    run_id=run_id,
                )
            else:
                yield f"id: 0\nevent: workspace_status\ndata: {json.dumps({'status': 'starting', 'workspace_id': workspace_id})}\n\n"

                # Learn the pre-start sandbox state via a callback threaded
                # through session init → PTCSandbox.reconnect. The callback
                # fires once with the state string as soon as reconnect reads
                # it (before runtime.start() is invoked). We coordinate via
                # asyncio.Event and the acquisition race each other so a warm sibling
                # does not wait for a reconnect callback it can never emit.
                state_event = asyncio.Event()
                state_box: dict[str, str | None] = {"value": None}

                def _on_state(state: str) -> None:
                    state_box["value"] = state
                    state_event.set()

                session_task = asyncio.create_task(
                    workspace_manager.get_session_for_workspace(
                        workspace_id,
                        user_id=user_id,
                        on_state_observed=_on_state,
                        skills_signature=skills_signature,
                        run_id=run_id,
                    )
                )

                try:

                    async def _wait_for_session():
                        return await asyncio.shield(session_task)

                    state_waiter = asyncio.create_task(state_event.wait())
                    session_waiter = asyncio.create_task(_wait_for_session())
                    try:
                        done, pending = await asyncio.wait(
                            {state_waiter, session_waiter},
                            timeout=5.0,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    except BaseException:
                        state_waiter.cancel()
                        session_waiter.cancel()
                        await asyncio.gather(
                            state_waiter, session_waiter, return_exceptions=True
                        )
                        raise
                    for waiter in pending:
                        waiter.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)

                    logger.info(
                        "[WS_STATUS] state observation",
                        extra={
                            "workspace_id": workspace_id,
                            "sandbox_state": state_box["value"],
                        },
                    )

                    if state_box["value"] == "archived":
                        yield f"id: 0\nevent: workspace_status\ndata: {json.dumps({'status': 'starting', 'workspace_id': workspace_id, 'sandbox_state': 'archived'})}\n\n"

                    session = (
                        await session_waiter
                        if session_waiter in done
                        else await session_task
                    )
                    yield f"id: 0\nevent: workspace_status\ndata: {json.dumps({'status': 'ready', 'workspace_id': workspace_id})}\n\n"
                except BaseException:
                    # Client disconnect / GeneratorExit / any error during the
                    # yield or await chain above must not leak session_task.
                    # Cancel and drain to surface the outcome (or CancelledError).
                    if not session_task.done():
                        session_task.cancel()
                        with contextlib.suppress(BaseException):
                            await session_task
                    raise

            _mark_phase("session")

            # Fire-and-forget: update workspace activity (conditional SQL, skip if <60s)
            _fire_and_forget(
                update_workspace_activity(workspace_id),
                name=f"update_activity_{workspace_id[:8]}",
            )

            registry_store = BackgroundRegistryStore.get_instance()
            background_registry = await registry_store.get_or_create_registry(thread_id)

            # Stamp the current turn's run_id on the registry so newly-registered
            # subagents inherit it (spawned_run_id). The collector filters by this
            # to avoid claiming subagents that belong to prior turns.
            background_registry.current_run_id = run_id

            # Build graph with the workspace's session
            # Note: agent.md is injected by the runtime-context baseline middleware
            # on every model call, ensuring it's always the latest content.
            from src.server.services.preview_links import cache_tool_preview
            from src.server.services.egress.direct_tools import (
                bind_direct_mcp_tools,
                direct_tools_for_turn,
            )

            # Resolved once and used three times: the agent build mounts the
            # workspace's memory off it, the post-turn reconcile materialises that
            # folder's skills, and the run's own task binds it for the path layer.
            project = await _resolve_project(session, workspace_id)

            # Frozen for this project at acquire; the session's own MCP fields are
            # whichever sibling on the machine resolved last.
            tool_view = workspace_manager.tool_view(session, workspace_id)

            # One relay session per directly bound server, held for the run.
            direct_mcp = await direct_tools_for_turn(
                bind_direct_mcp_tools(
                    session, user_id=user_id, workspace_id=workspace_id, view=tool_view
                )
            )

            # Memory dual write: mirror memory-namespace store writes into the
            # projection for this run when the flag is on. The facade delegates every
            # other call and never fails a store write because the projection failed.
            from src.server.services.refactor.memory_dual_write import (
                maybe_wrap_memory_store,
            )

            agent_store = maybe_wrap_memory_store(setup.store)
            # D3: the thread's permission mode decides whether standing approvals
            # are honoured for this turn. Best-effort read; default mode on failure.
            from src.server.services.refactor.permissions import (
                effective_permission_mode,
            )

            permission_mode = await effective_permission_mode(thread_id)
            from src.server.services.refactor_flags import refactor_flag_enabled

            memory_proposals_enabled = (
                memory_enabled and refactor_flag_enabled("memory_index")
            )

            ptc_graph = await build_ptc_graph_with_session(
                session=session,
                config=config,
                # Optional structural gate: builds the agent without
                # Task/TaskOutput. Task report-back turns don't set it (they
                # need TaskOutput to fetch the result); their re-announce
                # recursion is handled by the outbox's ledger arbitration.
                # One capability, not a per-thread preset: the deployment/user
                # feature decides whether sub-agent dispatch exists at all, and
                # the main model decides when to use it.
                # RA-3: the Router and the Worker never spawn subagents. The
                # Worker's roster is empty anyway (subagents run only for the
                # generalist, ``roles.ALL_ROLES``), so without this gate it was
                # handed a Task tool with no types, TaskOutput and RunWorkflow it
                # could never use. Only the Watchdog (generalist) keeps them.
                disable_subagents=bool(request.disable_subagents)
                or route.role in NO_SUBAGENT_ROLES
                or not config.feature_enabled("agent_teams"),
                operation_callback=None,
                # I2: the run's fenced session-bound saver when the WriterGuard
                # is active; the global pooled saver otherwise.
                checkpointer=run_handle.checkpointer,
                background_registry=background_registry,
                # I2 (2.4e): the guard doubles as the task-namespace fence —
                # each background subagent takes exclusive N(thread, task:id) on
                # the run's pinned session before it may write.
                namespace_owner=run_handle.guard,
                user_id=user_id,
                user_profile=user_profile,
                thread_id=thread_id,
                store=agent_store,
                on_signed_url=partial(cache_tool_preview, workspace_id=workspace_id),
                direct_mcp=direct_mcp,
                turn_context=turn_context,
                project=project,
                tool_view=tool_view,
                role=route.role,
                # A port, not a value: the switch can flip mid-turn from any
                # worker, so the agent re-reads the row on every call.
                subagent_switch=partial(read_thread_subagents_allowed, thread_id),
                window_coverage=partial(runs_held, thread_id),
                memory_context_block=memory_context_block,
                permission_mode=permission_mode,
                memory_proposals_enabled=memory_proposals_enabled,
                memory_enabled=memory_enabled,
                memory_tool_policy=memory_tool_policy,
                extra_tools=extra_tools,
            )

            _mark_phase("graph_build")

            base_checkpoint_id = await turn_start_checkpoint_id(
                thread_id, request, getattr(ptc_graph, "checkpointer", None)
            )
            cancelled_calls = []

            if session.sandbox:
                sandbox_id = getattr(session.sandbox, "sandbox_id", None)

            # PTC-only: set global for snapshot access
            setup.graph = ptc_graph

            messages = normalize_request_messages(request)

            # =====================================================================
            # Skill Context Resolution (body injection happens in SkillsMiddleware)
            # =====================================================================
            # Resolve which skills this turn activates — from additional_context or a
            # leading /<command> in the message (stripped in place). The SKILL.md body
            # is injected by SkillsMiddleware at turn entry, which dedups against bodies
            # already live in the thread so a re-sent skill isn't pasted every turn.
            #
            # Only set on normal turns: HITL resumes and checkpoint replays carry no new
            # user message, so the middleware must not inject (mirrors the prior guard).
            if not request.hitl_response and not is_checkpoint_replay:
                skill_contexts = prepare_skill_contexts(
                    messages,
                    request,
                    extra_commands=user_skill_commands(config),
                    allowed_skills=turn_skill_names(config),
                )
            else:
                skill_contexts = None
            skill_dirs = (
                config.skills.local_skill_dirs()
                + ([config.user_skill_dir] if config.user_skill_dir else [])
                if skill_contexts
                else None
            )

            # Multimodal Context Injection
            messages = await attach_request_files(
                messages, request, session, effective_model, config, project=project
            )

            # Build input state or resume command
            if request.hitl_response:
                # Structured HITL resume payload.
                # Pydantic validates this into HITLResponse models, but LangChain's
                # HumanInTheLoopMiddleware expects plain dicts (subscriptable).
                resume_payload = serialize_hitl_response_map(request.hitl_response)
                input_state = Command(
                    resume=resume_payload,
                    # None for a resume that was not credit-paused, which is what
                    # ``update`` defaults to anyway.
                    update=await build_credit_resume_update(thread_id, run_id),
                )
                logger.info(
                    f"[PTC_RESUME] thread_id={thread_id} "
                    f"hitl_response keys={list(request.hitl_response.keys())}"
                )
                # Phase-1 dual write: record the permission decision (shape only —
                # no feedback text or action args) behind the ``events`` flag.
                # Best-effort by contract: the resume must proceed regardless.
                try:
                    from src.server.services.refactor.dual_write import (
                        record_permission_decision,
                    )

                    await record_permission_decision(
                        user_id=user_id,
                        thread_id=thread_id,
                        response_id=run_id,
                        hitl_response=request.hitl_response,
                    )
                except Exception:
                    logger.debug(
                        f"[PTC_RESUME] permission decision projection failed for "
                        f"run={run_id}",
                        exc_info=True,
                    )
            elif is_checkpoint_replay:
                # Checkpoint replay/regenerate: no new messages, resume from checkpoint_id.
                # LangGraph will re-execute from the specified checkpoint state.
                input_state = None
                logger.info(
                    f"[PTC_REPLAY] thread_id={thread_id} "
                    f"checkpoint_id={request.checkpoint_id} (regenerate/retry)"
                )
            else:
                input_state = {
                    "messages": messages,
                    "current_agent": "ptc",  # For FileOperationMiddleware SSE events
                }
                # Skill tools auto-load via SkillsMiddleware (sets loaded_skills in state).

            # =====================================================================
            # Inline Context Injection (directive + widget + chart selection)
            # =====================================================================
            # Each appends a <system-reminder> to the last user message, in order.
            # Widget image bytes ride MultimodalContext(type='image') above. The
            # target is None on HITL-resume / checkpoint-replay (input_state is a
            # Command / None there), so injection is skipped on those turns.
            #
            # No server-injected Swarm directive: parallel fan-out is a choice the
            # main model makes through the ordinary Task tool, not a thread mode.
            directives = parse_directive_contexts(request.additional_context)
            inline_target = (
                input_state["messages"]
                if isinstance(input_state, dict) and input_state.get("messages")
                else None
            )
            inject_inline_reminders(
                inline_target,
                [
                    build_directive_reminder(directives),
                    build_widget_context_reminder(widget_ctxs),
                ],
            )

            # =====================================================================
            # Save user request to system thread directory (non-critical)
            # =====================================================================
            if not request.hitl_response and session.sandbox and project:
                short_id = thread_id[:8]
                try:
                    # The project is passed explicitly: this runs on the request
                    # task, before ``run_with_project`` binds the turn's, so there
                    # is no bound project for the sandbox to resolve against.
                    request_path = (
                        f"{session.sandbox.workspace(project).thread_dir(short_id)}"
                        "/request.md"
                    )
                    _fire_and_forget(
                        session.sandbox.awrite_file_text(request_path, user_input),
                        name=f"write_request_{short_id}",
                    )
                except Exception:
                    pass  # normalize_path is sync, can still throw

            # =====================================================================
            # LangSmith Tracing Configuration
            # =====================================================================

            graph_config = build_graph_config(
                thread_id=thread_id,
                user_id=user_id,
                workspace_id=workspace_id,
                timezone_str=turn_context.tool_timezone,
                token_callback=token_callback,
                request=request,
                effective_model=effective_model,
                skill_contexts=skill_contexts,
                skill_dirs=skill_dirs,
                run_id=run_id,
                turn_index=run_handle.turn_index,
                surface=turn_surface(request, prior_thread),
                extra_configurable=extra_configurable,
                base_checkpoint_id=base_checkpoint_id,
            )
            # Propagate run_id to LangGraph via the top-level config key; it
            # lands on ExecutionInfo.run_id and CheckpointMetadata.run_id so
            # LangSmith / checkpoint inspection can correlate by this UUID.
            graph_config["run_id"] = run_id
            steer_input_id = await open_with_steers_ahead(
                thread_id, run_id, request, input_state
            )
            if steer_input_id:
                graph_config["configurable"]["steer_input_message_id"] = steer_input_id

            handler = RunSSEProducer(
                thread_id=thread_id,
                run_id=run_id,
                token_callback=token_callback,
                tool_tracker=tool_tracker,
                agent_config=config,
                user_id=user_id,
                turn_index=run_handle.turn_index,
                announced=announced(query_metadata),
            )

            # =====================================================================
            # Background Execution with Completion Callback
            # =====================================================================

            # ``manager`` was acquired at the top of this handler for the early
            # steering-routing check; reuse it here. ``cancel_stale_workflow``
            # already ran there (gated on ``needs_startup``) so steering routed
            # against live state.

            # Runs inside _finalize_run, before the terminal txn that records
            # what it captured.
            capture_artifacts = _artifact_hook(
                session, thread_id=thread_id, project=project
            )

            # Post-finalize side effects only (v4): the durable terminal write —
            # response row, usage, projection — already happened in BTM's
            # _finalize_run before this callback fires. A failure here is logged
            # by the caller and never changes the run's outcome.
            async def on_background_workflow_complete(task_info):
                # Report-back moved to the hook outbox (1.7): finalize
                # enqueues a durable report_back job, so a crash right here can
                # no longer drop the dispatch. This callback keeps only
                # best-effort sandbox housekeeping.

                # Post-completion sandbox housekeeping. Reconcile BEFORE the
                # backup — the reconcile mutates the skills ledger and dirs, and
                # the file backup should capture the converged state.
                ws_manager = WorkspaceManager.get_instance()
                if session and session.sandbox:
                    # The run is terminal, so a settle may move the folder now: the
                    # pass reads it under the folder hold, not from the turn start.
                    await ws_manager.reconcile_skills_if_running(
                        workspace_id, user_id, source="post_turn"
                    )
                try:
                    # Any project on the machine may have changed, not only this
                    # one: the sweep finds which, and mirrors those.
                    await ws_manager.backup_changed_projects(
                        workspace_id, session=session
                    )
                except Exception as e:
                    logger.warning(
                        f"[PTC_COMPLETE] file backup failed for {thread_id}: {e}"
                    )
                # A turn is what fills the shared disk, so its end is when the
                # reading the warning and the next turn's context rely on moves.
                if session and session.computer_id:
                    await ws_manager.refresh_computer_disk(
                        session.computer_id,
                        min_age_s=TURN_MEASURE_MIN_INTERVAL_SECONDS,
                    )

            # Start workflow in background with event buffering
            await manager.start_run(
                thread_id=thread_id,
                run_id=run_id,
                # The project is bound outermost, in the run's own task: a graph
                # node runs in a task created from the caller's context, so a
                # ContextVar set inside the graph would not reach the next node.
                workflow_generator=run_with_project(
                    project,
                    run_with_credit_gate(
                        credit_gate,
                        # Relay sessions for direct tools open and close with the
                        # run, in the run's task, not with this request's generator.
                        direct_mcp.drive(
                            handler.stream_workflow(
                                graph=ptc_graph,
                                input_state=input_state,
                                config=graph_config,
                                settled_results=cancelled_calls,
                            )
                        ),
                    ),
                ),
                metadata={
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "sandbox_id": sandbox_id,
                    "sandbox": session.sandbox if session else None,
                    "started_at": datetime.now().isoformat(),
                    "start_time": start_time,
                    "is_byok": is_byok,
                    "burst_slot_id": request.burst_slot_id,
                    "locale": request.locale,
                    "timezone": turn_context.tool_timezone,
                    "handler": handler,
                    "token_callback": token_callback,
                    "run_handle": run_handle,
                    "artifact_hook": capture_artifacts,
                },
                completion_callback=on_background_workflow_complete,
                graph=ptc_graph,
                # Manager owns burst slot release from registration on
                on_registered=scope.transfer_to_executor,
            )
            # Admission complete — release the lock so concurrent POSTs can
            # see the new RUNNING LocalRunExecution via ``wait_or_steer`` and route
            # to steering instead of contending here.
            scope.release_admission()

            _mark_phase("workflow_start")
            total_ms = (time.time() - start_time) * 1000
            phases = " ".join(f"{k}={v:.0f}ms" for k, v in _phase_times.items())
            llm_def = config.llm_definition
            model_tag = (
                f"{llm_def.provider}/{llm_def.model_id}"
                if llm_def
                else config.llm.name
                if config.llm
                else "unknown"
            )
            logger.info(
                f"[PTC_TIMING] thread_id={thread_id} model={model_tag} total={total_ms:.0f}ms ({phases})"
            )

            # Attach phase timings as attributes on the active chat.turn span so
            # traces show the same breakdown the log line does, and emit one
            # histogram sample per phase so dashboards can render the breakdown.
            from opentelemetry import trace as _otel_trace

            _span = _otel_trace.get_current_span()
            if _span is not None and _span.is_recording():
                for _k, _v in _phase_times.items():
                    _span.set_attribute(f"chat.turn.phase.{_k}_ms", _v)
                _span.set_attribute("chat.turn.total_ms", total_ms)
            for _k, _v in _phase_times.items():
                safe_record(chat_turn_phase_duration_ms, _v, {"phase": _k, "mode": "ptc"})

            # Stream-backed first-connect: read from workflow:stream:{tid}:{rid}
            # via XREAD BLOCK. The workflow runs as a fully detached background
            # task — disconnect cannot reach it.
            async for event in stream_from_log(thread_id, run_id, last_event_id=None):
                yield event

        except Exception as e:
            # Pre-START on the dispatched path: the primer at the HTTP boundary
            # is still driving this generator, so admission/dedup failures must
            # surface raw as HTTP errors — never SSE frames into a stream whose
            # run was never dispatched (nothing durable exists yet).
            if dispatched and tail.run_handle is None:
                raise
            # =================================================================
            # Error Recovery with Retry Logic
            # =================================================================
            async for event in tail.error_events(
                e, handler=handler, token_callback=token_callback
            ):
                yield event

            raise
