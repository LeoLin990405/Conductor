"""Shared turn prelude and tail for the chat turn entry (R8c ruling 7).

``prepare_turn`` is everything a turn does before it picks a lane: the
admission lock, the retransmit dedup, the stale-workflow cancel and the
steer-or-admit decision, the thread row, the LLM config, the START txn
(``begin_run``) and the turn's memory policy and recall. ``TurnTail`` is the
async context manager every lane runs inside: it owns the ``RunScope``, turns
a client disconnect during setup into ``fail_open``, hands an error to
``handle_workflow_error`` (see ``TurnTail.error_events``) and releases the
admission lock last, on every exit.

Collaborators are looked up through ``ptc_run`` at call time, so tests that
patch ``src.server.handlers.chat.ptc_run.<name>`` keep reaching them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


def _ptc():
    from . import ptc_run

    return ptc_run


@dataclass
class TurnPrelude:
    """What ``prepare_turn`` resolved for the lane that runs the turn.

    ``done`` means the turn ends here (steered into a running turn, or
    refused as busy): the caller yields ``early_events`` and returns. Every
    other field is set only when ``done`` is false.
    """

    done: bool = False
    early_events: list[str] = field(default_factory=list)
    request: Any = None
    superagent_lane: Any = None
    run_metadata: dict | None = None
    config: Any = None
    manager: Any = None
    workspace_manager: Any = None
    needs_startup: bool = False
    prior_thread: Any = None
    user_profile: Any = None
    turn_context: Any = None
    query_type: Any = None
    is_checkpoint_replay: bool = False
    effective_model: str | None = None
    own_key: bool = False
    query_metadata: dict = field(default_factory=dict)
    widget_ctxs: Any = None
    run_handle: Any = None
    memory_tool_policy: Any = None
    memory_enabled: bool = False
    memory_context_block: str | None = None


class TurnTail:
    """Async context manager around a whole turn: cancel, error, release.

    The order is the one the turn entry always had: on a disconnect or
    cancellation raised by the turn body, ``fail_open`` (while this
    generator still owns the slot) and only then release the admission
    lock; on an error, the caller yields ``error_events`` and re-raises, and
    the lock is released after that. A disconnect while the error frames
    stream is not treated as a setup disconnect, as before.
    """

    def __init__(
        self,
        *,
        request,
        thread_id: str,
        user_id: str,
        workspace_id: str,
        start_time: float,
        is_byok: bool,
    ) -> None:
        # Owns the burst lease, admission lock, and open START row until the
        # executor's done-callback is armed (transfer_to_executor).
        self.scope = _ptc().RunScope(
            user_id=user_id, burst_slot_id=request.burst_slot_id
        )
        self.request = request
        self.thread_id = thread_id
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.start_time = start_time
        self.is_byok = is_byok
        # Filled in by ``prepare_turn`` as it gets them, so an error raised
        # part-way reaches ``handle_workflow_error`` with what existed then.
        self.turn_context = None
        self.run_handle = None
        self._in_error_path = False

    async def __aenter__(self) -> TurnTail:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        try:
            if (
                exc_type is not None
                and issubclass(exc_type, (asyncio.CancelledError, GeneratorExit))
                and not self._in_error_path
            ):
                await self._on_cancel()
        finally:
            # Backstop for any error path that bypassed the normal release
            # (e.g., exception before start_run); idempotent on the scope.
            self.scope.release_admission()
        return False

    async def _on_cancel(self) -> None:
        logger = _ptc().logger
        if self.scope.slot_owned:
            logger.warning(
                f"[PTC_CHAT] Generator cancelled before workflow started: "
                f"thread_id={self.thread_id} workspace_id={self.workspace_id}"
            )
            await self.scope.fail_open("client disconnected during setup")
        else:
            logger.warning(
                f"[PTC_CHAT] Generator cancelled (client disconnect?): "
                f"thread_id={self.thread_id} workspace_id={self.workspace_id}"
            )

    def error_events(self, e: Exception, *, handler, token_callback):
        """The SSE frames for ``e``; the caller yields them and re-raises.

        The scope encodes ownership: its owned_run_handle is non-None only
        while this generator still owns the run (pre-handoff). After
        start_run, BTM's _finalize_run owns the terminal write and a
        finalize here would race it.
        """
        self._in_error_path = True
        return _ptc().handle_workflow_error(
            e,
            thread_id=self.thread_id,
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            handler=handler,
            token_callback=token_callback,
            scope=self.scope,
            start_time=self.start_time,
            request=self.request,
            is_byok=self.is_byok,
            turn_context=self.turn_context,
        )


async def prepare_turn(
    tail: TurnTail,
    *,
    request,
    thread_id: str,
    run_id: str,
    user_input: str,
    user_id: str,
    route,
    is_byok: bool,
    config,
    dispatched: bool,
    steerable: bool,
    run_metadata: dict | None,
    superagent_lane,
) -> TurnPrelude:
    """Admit the turn and open its run; see the module docstring.

    Must run inside ``async with tail``: it takes the admission lock onto
    ``tail.scope`` and leaves its release to the tail.
    """
    p = _ptc()
    setup = p.setup
    HTTPException = p.HTTPException
    LocalRunExecutor = p.LocalRunExecutor
    WorkspaceManager = p.WorkspaceManager
    dedup_retransmit_or_raise = p.dedup_retransmit_or_raise
    wait_or_steer = p.wait_or_steer
    agent_steer_of = p.agent_steer_of
    steer_allowed = p.steer_allowed
    ensure_thread = p.ensure_thread
    retried_on_its_surface = p.retried_on_its_surface
    read_disk_notice = p.read_disk_notice
    get_user_profile_for_prompt = p.get_user_profile_for_prompt
    build_turn_context = p.build_turn_context
    _resolve_fork = p._resolve_fork
    resolve_llm_config = p.resolve_llm_config
    is_own_key_turn = p.is_own_key_turn
    provenance_metadata = p.provenance_metadata
    parse_widget_contexts = p.parse_widget_contexts
    parse_multimodal_contexts = p.parse_multimodal_contexts
    build_attachment_metadata = p.build_attachment_metadata
    serialize_widget_contexts_for_metadata = p.serialize_widget_contexts_for_metadata
    serialize_context_metadata = p.serialize_context_metadata
    user_skill_commands = p.user_skill_commands
    turn_skill_names = p.turn_skill_names
    process_hitl_response = p.process_hitl_response
    begin_run = p.begin_run
    surface_stamp = p.surface_stamp
    logger = p.logger

    workspace_id = route.workspace_id
    scope = tail.scope
    # Phase-2 render switch: a selected memory block for this turn, or None for
    # the legacy per-tier baseline.
    memory_context_block: str | None = None

    if not setup.agent_config:
        raise HTTPException(
            status_code=503,
            detail="PTC Agent not initialized. Check server startup logs.",
        )

    # =====================================================================
    # Admission gate
    # =====================================================================
    # Per-thread asyncio.Lock that serializes the
    # ``wait_or_steer → start_run → start_run`` window.
    # Without this, two simultaneous cold POSTs on an idle thread both
    # see "no in-flight task" in ``wait_or_steer`` and both attempt
    # START; the loser now fails on the in_progress slot index instead
    # of admitting, but serializing here routes it to steering.
    manager = LocalRunExecutor.get_instance()
    admission_lock = await manager.get_admission_lock(thread_id)
    await admission_lock.acquire()
    scope.hold_admission(admission_lock)

    # Idempotency probe under the admission lock: a retransmitted
    # request_key resolves to its existing run HERE, before the steering
    # or fork paths below can act on the duplicate (raises
    # DuplicateRequestError → structured duplicate_request SSE).
    await dedup_retransmit_or_raise(
        thread_id, request.request_key, except_job_id=request.steer_job_id
    )

    # =====================================================================
    # Early steering routing
    # =====================================================================
    # If a workflow is already running for this thread, route this POST
    # into it as a steer *before* any turn row is written. Detecting steering
    # here keeps ``conversation_queries`` clean — a steering message is
    # neither a run nor a turn (v4 identity model); its content is archived
    # on the owning response's metadata at finalize.
    workspace_manager = WorkspaceManager.get_instance()
    needs_startup = not workspace_manager.has_ready_session(workspace_id)
    # When the workspace was evicted/restarted, any in-BTM LocalRunExecution for
    # this thread holds a stale sandbox reference — cancel it first so
    # admission/steering routes against live state only (this run isn't
    # registered anywhere yet; its row commits at START, below).
    # R8c P lane: a thread PTC left unfinished stays on PTC this turn. P
    # never opens a sandbox, so only a turn that stays on P skips this.
    if superagent_lane is not None:
        from .superagent_run import settle_superagent_lane

        superagent_lane = await settle_superagent_lane(
            superagent_lane, checkpointer=setup.checkpointer, thread_id=thread_id, run_id=run_id
        )
        if superagent_lane is None and run_metadata:
            run_metadata = {k: v for k, v in run_metadata.items() if k != "superagent_runtime"}
    if needs_startup and superagent_lane is None:
        await manager.cancel_stale_workflow(thread_id)
    # Admit a fresh turn, steer the running one, or 409 — see
    # ``wait_or_steer``; who may steer at all is ``steer_allowed``.
    ready, steering_event = await wait_or_steer(
        manager,
        thread_id,
        user_input,
        user_id,
        steer_only=request.steer_only,
        platform=request.platform,
        request_key=request.request_key,
        workspace_id=workspace_id,
        steer_job_id=request.steer_job_id,
        # A hand-off into a running turn answers the dispatch primer
        # with DISPATCH_STEERED_MARKER and that run's id.
        agent=agent_steer_of(
            request,
            dispatched=dispatched,
            run_id=run_id,
            request_key=request.request_key,
        ),
        can_steer=steer_allowed(
            request, dispatched=dispatched, steerable=steerable
        ),
    )
    if not ready:
        await scope.release_slot()
        # Release admission immediately — no workflow will register
        # under this lock, so holding it would needlessly block any
        # follow-up POST.
        scope.release_admission()
        return TurnPrelude(
            done=True, early_events=[steering_event] if steering_event else []
        )

    # =====================================================================
    # Database Persistence Setup
    # =====================================================================

    prior_thread = await ensure_thread(
        request,
        thread_id,
        workspace_id,
        user_id,
        initial_query=user_input,
    )
    # /retry builds its attempt with no surface: it runs where the
    # attempt it retries did, under the same rules.
    request, inherits_rules = await retried_on_its_surface(request)
    tail.request = request
    disk_free_mb, disk_known = await read_disk_notice(workspace_id)
    user_profile = await get_user_profile_for_prompt(user_id) if user_id else None
    turn_context = build_turn_context(
        request,
        prior_thread,
        user_profile=user_profile,
        disk_free_mb=disk_free_mb,
        disk_known=disk_known,
        inherits_rules=inherits_rules,
    )
    tail.turn_context = turn_context

    query_type, fork = _resolve_fork(request=request, thread_id=thread_id)
    is_checkpoint_replay = bool(request.checkpoint_id and not request.messages)

    # Resolve LLM config (pre-resolved by the route handler, fallback for
    # standalone use). Ahead of the metadata below on purpose: that block
    # records the model and the detected slash command, and both are read
    # off this config. Resolved late, the standalone path would stamp a
    # skill the turn then refuses, because activation gates on the
    # resolved registry while the metadata had nothing to gate on. The
    # route already resolves before this generator runs, so this only
    # moves the standalone path onto the ordering production has.
    if config is None:
        config = await resolve_llm_config(
            setup.agent_config,
            user_id,
            request.llm_model,
            is_byok,
            reasoning_effort=getattr(request, "reasoning_effort", None),
            fast_mode=getattr(request, "fast_mode", None),
            thread_id=thread_id,
            enabled_subagents=request.subagents_enabled,
            workspace_id=workspace_id,
            role=route.role,
        )

    # Persist query start
    feedback_action = None
    query_content = user_input
    effective_model = config.llm.name if config and config.llm else None
    # Off the resolved credential, not off ``is_byok``: that flag answers
    # which ladder to try, and an automation with only an OAuth token
    # passes it false while still paying its own vendor bill.
    own_key = is_own_key_turn(config)
    query_metadata = {
        # The resolved one: a request may name a former Home row.
        "workspace_id": workspace_id,
    }
    if effective_model:
        query_metadata["llm_model"] = effective_model
    query_metadata.update(provenance_metadata(request))

    # Extract attachment and context metadata for display in history
    # (skipped for HITL resumes)
    widget_ctxs = parse_widget_contexts(request.additional_context)
    if request.additional_context and not request.hitl_response:
        multimodal_ctxs = parse_multimodal_contexts(request.additional_context)
        # A retry writes no query row, so a second upload would be
        # stored for nothing.
        if multimodal_ctxs and not request.retry_of_run_id:
            query_metadata["attachments"] = await build_attachment_metadata(
                multimodal_ctxs, thread_id
            )
        if widget_ctxs:
            query_metadata["widget_contexts"] = (
                serialize_widget_contexts_for_metadata(widget_ctxs)
            )

    # Persist lightweight additional_context + slash command fallback
    # (serialize_context_metadata's slash-command branch already guards
    # on `not request.hitl_response`, so this is safe to call always.)
    if not request.hitl_response:
        serialize_context_metadata(
            request,
            query_metadata,
            user_input,
            extra_commands=user_skill_commands(config),
            allowed_skills=turn_skill_names(config),
        )

    if request.hitl_response:
        prepared = process_hitl_response(request)
        feedback_action = prepared.feedback_action
        query_content = prepared.query_content
        query_metadata.update(prepared.metadata)

    # =====================================================================
    # START txn (v4): query row + in_progress run row + thread projection
    # in one transaction (begin_run owns the attempt-chain derivation).
    # =====================================================================
    # Resolved ONCE and reused by the tracker re-mark below: the raw
    # request field is empty on public follow-ups (HITL resume, user
    # continuation), and stamping the durable row with the inherited gen
    # while re-marking the tracker with None would make a live admitted
    # run read as unadmitted to the fenced-teardown probe.
    run_handle = await begin_run(
        request,
        scope=scope,
        thread_id=thread_id,
        run_id=run_id,
        workspace_id=workspace_id,
        user_id=user_id,
        is_byok=is_byok,
        query_content=query_content,
        query_type=query_type,
        feedback_action=feedback_action,
        query_metadata=query_metadata,
        fork=fork,
        is_checkpoint_replay=is_checkpoint_replay,
        extra_run_metadata={
            **surface_stamp(
                request, prior_thread, inherits_rules=turn_context.inherits_rules
            ),
            **(run_metadata or {}),
        },
    )
    tail.run_handle = run_handle
    # Tool policy is an execution guard, not a recall preference. Read it
    # once after START and freeze it for this run, even when memory recall
    # is disabled. The loader itself fails closed; the fallback covers an
    # import or contract failure before the loader can return.
    from src.server.contracts.memory_tool_policy import MemoryToolPolicySnapshot

    try:
        from src.server.services.refactor.memory_tool_policy import (
            load_memory_tool_policy,
        )

        memory_tool_policy = await load_memory_tool_policy(user_id=user_id)
    except Exception:
        logger.error(
            "[PTC_TURN] memory tool policy unavailable; denying execution",
            exc_info=True,
        )
        memory_tool_policy = MemoryToolPolicySnapshot(
            deny_all=True, digest="runtime-load-error"
        )
    # The owner's server-side feature choice governs every automatic
    # memory source for this turn. Treat a preference read failure as off;
    # caller metadata and Store flags are not authority for this gate.
    try:
        from src.server.services.refactor.memory_runtime_controls import (
            automatic_memory_enabled,
        )

        memory_enabled = await automatic_memory_enabled(user_id)
    except Exception:
        memory_enabled = False
        logger.warning(
            "[PTC_TURN] memory preference unavailable; automatic memory disabled",
            exc_info=True,
        )
    if not memory_enabled:
        # A present empty block is the frozen no-memory answer. This also
        # prevents replay/legacy baseline paths from re-reading old rows.
        memory_context_block = ""
    # Phase-2 render switch: select this turn's memory index when the
    # owner's gate is on, audit the selection, and hand the rendered block
    # to the graph. A present block suppresses legacy per-tier indices;
    # preference-off uses an empty block so replay cannot restore them.
    # P0 privacy: every turn's model is another vendor's, so the block goes
    # through the egress gate (ticket_memory_policy). A Worker-role turn is a
    # Ticket: its prompt already carries the Ticket-gated block from
    # worker_prompt, and the prompt itself is no memory query, so the turn
    # injects nothing more.
    worker_turn = str(getattr(route.role, "value", route.role)) == "worker"
    if worker_turn:
        memory_context_block = ""
    if (
        memory_enabled
        and not worker_turn
        and user_input.strip()
        and not is_checkpoint_replay
    ):
        try:
            from src.server.services.autonomy import (
                ticket_memory_policy as memory_policy,
            )
            from src.server.services.refactor.dual_write import (
                record_memory_injection,
            )

            planned = await memory_policy.gated_turn_memory_block(
                user_id=user_id,
                query=user_input,
                scope=memory_policy.turn_scope(
                    query=user_input,
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                ),
                thread_id=thread_id,
                workspace_id=workspace_id,
                label="turn",
                ident=str(run_id),
            )
            if planned is None:
                # Render switch off: the legacy per-tier baseline would put
                # raw, ungated memory files in front of a vendor's model, so
                # the turn takes the empty block instead (fail closed).
                memory_context_block = ""
            else:
                memory_context, memory_context_block = planned
                await record_memory_injection(
                    user_id=user_id,
                    thread_id=thread_id,
                    response_id=run_id,
                    context=memory_context,
                )
        except Exception:
            if memory_context_block is None:
                memory_context_block = ""
            logger.debug(
                f"[PTC_TURN] memory selection failed for run={run_id}; "
                "no memory for this turn",
                exc_info=True,
            )
    if not is_checkpoint_replay:
        logger.debug(
            f"[PTC_CHAT] Run started: workspace_id={workspace_id} "
            f"thread_id={thread_id} query_type={query_type} "
            f"turn_index={run_handle.turn_index}"
        )


    return TurnPrelude(
        request=request,
        superagent_lane=superagent_lane,
        run_metadata=run_metadata,
        config=config,
        manager=manager,
        workspace_manager=workspace_manager,
        needs_startup=needs_startup,
        prior_thread=prior_thread,
        user_profile=user_profile,
        turn_context=turn_context,
        query_type=query_type,
        is_checkpoint_replay=is_checkpoint_replay,
        effective_model=effective_model,
        own_key=own_key,
        query_metadata=query_metadata,
        widget_ctxs=widget_ctxs,
        run_handle=run_handle,
        memory_tool_policy=memory_tool_policy,
        memory_enabled=memory_enabled,
        memory_context_block=memory_context_block,
    )
