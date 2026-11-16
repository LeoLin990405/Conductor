"""Super-agent P lane: one Router / Watchdog turn without the PTC graph.

``astream_ptc_workflow`` runs its prelude (admission, dedup, ``begin_run``,
memory selection), yields ``DISPATCH_STARTED_MARKER`` itself, and then hands
the turn here, the same split the native lane uses. This module therefore
starts at the PTC half that follows the marker and keeps only what a Router
or Watchdog turn needs: token tracking, the run credit gate, the producer,
the executor and the Redis read-back. There is no sandbox session, no PTC
graph and no middleware stack; the model is the config's resolved client.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from src.server.services.credit_gate import CreditStopError, run_with_credit_gate
from src.observability import chat_turn_phase_duration_ms, safe_record
from src.server.models.chat import ChatRequest
from src.server.services.credit_gate_port import build_run_credit_gate
from src.server.services.runs.executor import LocalRunExecutor
from src.server.services.runs.sse_producer import RunSSEProducer
from src.server.services.superagent_runtime.bindings import (
    HostToolPort,
    LeaseCreditPort,
    PrecomputedRecall,
    provider_wire_adapter,
)
from src.server.services.superagent_runtime.graph import (
    SuperagentOutcome,
    SuperagentPorts,
    build_superagent_graph,
    checkpoint_has_pending_work,
)
from src.server.services.superagent_runtime.ports import TurnInput
from src.server.services.turn_runtime import TurnRoute

from .error_handling import SuperagentPendingPtcError, handle_workflow_error
from .request_prep import build_graph_config, init_tracking, logger, turn_surface
from src.server.services.runs.stream_reader import stream_from_log

__all__ = ["astream_superagent_workflow", "superagent_needs_ptc_fallback"]


async def superagent_needs_ptc_fallback(*, checkpointer: Any, thread_id: str) -> bool:
    """True when this thread's checkpoint must be finished by PTC first.

    A pending interrupt (a PTC credit pause) or unfinished PTC work would be
    left behind by a P turn, because new input only discards the receiving
    graph's own tasks. Read-only: nobody else's checkpoint is rewritten.
    """
    if checkpointer is None:
        return False
    checkpoint_tuple = await checkpointer.aget_tuple(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    )
    return checkpoint_has_pending_work(checkpoint_tuple)


async def settle_superagent_lane(
    lane: Any, *, checkpointer: Any, thread_id: str, run_id: str
) -> Any | None:
    """The lane, or None when this turn must stay on PTC.

    Decided before admission so a turn that falls back gets the whole PTC
    path, stale-run cancel included. A probe that fails falls back too, as
    the native lane does on a metadata fault: PTC can always run the turn.
    """
    try:
        pending = await superagent_needs_ptc_fallback(
            checkpointer=checkpointer, thread_id=thread_id
        )
    except Exception:
        logger.warning(
            f"[SUPERAGENT_P] fallback=probe_error thread_id={thread_id} "
            f"run_id={run_id} role={lane.role}",
            exc_info=True,
        )
        return None
    if pending:
        logger.info(
            f"[SUPERAGENT_P] fallback=pending_interrupt thread_id={thread_id} "
            f"run_id={run_id} role={lane.role}"
        )
        return None
    return lane


async def check_p_lane_admissible(
    *, request: Any, run_handle: Any, thread_id: str, run_id: str, lane: Any
) -> None:
    """Refuse what P cannot run, after START and before the dispatch marker.

    Before the marker so a dispatch is refused the way PTC refuses one. The
    pending-work re-check reads through the run's own fenced checkpointer,
    inside the slot begin_run took: the probe before admission can race a
    PTC run on another worker that crashed mid-tool and finalized since.
    """
    if request.checkpoint_id:
        raise HTTPException(
            status_code=409,
            detail="Super-agent P runtime does not support checkpoint replay",
        )
    if request.hitl_response:
        raise HTTPException(
            status_code=409,
            detail="Super-agent P runtime has no interrupts to resume",
        )
    if await superagent_needs_ptc_fallback(
        checkpointer=getattr(run_handle, "checkpointer", None), thread_id=thread_id
    ):
        logger.warning(
            f"[SUPERAGENT_P] pending_ptc_after_start thread_id={thread_id} "
            f"run_id={run_id} role={lane.role}"
        )
        raise SuperagentPendingPtcError(
            "Unfinished PTC work appeared on this thread after admission; "
            "the turn ends before any model call and the next dispatch runs on PTC."
        )


def _system_prompt(role: str, turn_context: Any) -> str:
    """Role prompt plus the clock, the only runtime context a Router needs.

    PTC's runtime-context middleware also states disk, workspace files and
    skills; none of that means anything to a turn that has no sandbox.
    """
    from src.server.services.superagent_runtime.prompts import (
        SUPERVISOR_SYSTEM_PROMPT,
        WATCHDOG_SYSTEM_PROMPT,
    )

    base = WATCHDOG_SYSTEM_PROMPT if role == "watchdog" else SUPERVISOR_SYSTEM_PROMPT
    zone_name = (
        getattr(turn_context, "timezone", None)
        or getattr(turn_context, "tool_timezone", None)
        or "UTC"
    )
    try:
        zone = ZoneInfo(zone_name)
    except Exception:
        zone_name, zone = "UTC", ZoneInfo("UTC")
    now = datetime.now(zone)
    return (
        f"{base.rstrip()}\n\n"
        f"Current time: {now.strftime('%Y-%m-%d %H:%M')} ({zone_name})."
    )


def _turn_input_factory(role: str, system_prompt: str, tools: Any):
    offered = tuple(str(tool.name) for tool in tools)

    def factory(history, user_text: str) -> TurnInput:
        return TurnInput(
            role=role,
            system_prompt=system_prompt,
            history=list(history),
            user_text=user_text,
            offered_tool_names=offered,
        )

    return factory


async def _end_with_credit_stop(stream, outcome: SuperagentOutcome):
    """Raise ``CreditStopError`` once the producer has drained.

    Raised after the stream rather than from the node, so the checkpoint
    keeps the turn's partial messages and stop note and the producer still
    sends ``credit_usage``. The executor then finalizes the run as failed.
    """
    async with contextlib.aclosing(stream):
        async for frame in stream:
            yield frame
    if outcome.credit_stopped:
        raise CreditStopError(outcome.stop_reason)


def _model_tag(config: Any) -> str:
    llm_def = getattr(config, "llm_definition", None)
    if llm_def:
        return f"{llm_def.provider}/{llm_def.model_id}"
    llm = getattr(config, "llm", None)
    return getattr(llm, "name", None) or "unknown"


async def astream_superagent_workflow(
    *,
    request: ChatRequest,
    thread_id: str,
    run_id: str,
    user_input: str,
    user_id: str,
    route: TurnRoute,
    lane: Any,
    scope: Any,
    run_handle: Any,
    turn_context: Any,
    config: Any,
    is_byok: bool,
    memory_context_block: str | None,
    memory_tool_policy: Any,
):
    """Stream one P-lane turn through the existing run/SSE pipeline.

    The caller has already yielded ``DISPATCH_STARTED_MARKER``; this
    generator yields only SSE frames read back from the run's Redis log.
    """
    workspace_id = route.workspace_id
    start_time = time.time()
    handler = None
    token_callback = None
    phase_times: dict[str, float] = {}
    phase_t0 = start_time

    def mark_phase(name: str) -> None:
        nonlocal phase_t0
        now = time.time()
        phase_times[name] = (now - phase_t0) * 1000
        phase_t0 = now

    try:
        if config is None:
            raise HTTPException(
                status_code=503,
                detail="Super-agent runtime requires a resolved agent configuration",
            )
        from src.server.services.llm.clients import is_own_key_turn

        token_callback, tool_tracker = init_tracking(thread_id)
        effective_model = config.llm.name if getattr(config, "llm", None) else None
        credit_gate = build_run_credit_gate(
            user_id,
            run_id,
            token_callback,
            tool_tracker,
            effective_model,
            is_byok=is_own_key_turn(config),
        )
        mark_phase("db_setup")

        # Already resolved by messaging.py (OAuth, BYOK, platform key); the
        # loop adds its own bounded retry, so nothing is wrapped here.
        chat = config.get_llm_client()
        mark_phase("model")

        ports = SuperagentPorts(
            credit=LeaseCreditPort(credit_gate),
            recall=PrecomputedRecall(memory_context_block),
            tools=HostToolPort(lane.tools, lane.host, policy=memory_tool_policy),
            callbacks=(token_callback,),
            prepare_messages=provider_wire_adapter(chat),
        )
        graph = build_superagent_graph(
            chat,
            _turn_input_factory(
                lane.role, _system_prompt(lane.role, turn_context), lane.tools
            ),
            ports,
            checkpointer=run_handle.checkpointer,
        )
        graph_config = build_graph_config(
            thread_id=thread_id,
            user_id=user_id,
            workspace_id=workspace_id,
            timezone_str=turn_context.tool_timezone,
            token_callback=token_callback,
            request=request,
            effective_model=effective_model,
            run_id=run_id,
            turn_index=run_handle.turn_index,
            surface=turn_surface(request, None),
        )
        graph_config["run_id"] = run_id
        # A run-scoped id keeps a retransmitted input from becoming a second
        # copy, the reason normalize_request_messages mints ids up front.
        input_state = {
            "messages": [
                {
                    "id": f"superagent-input-{run_id}",
                    "role": "user",
                    "content": user_input,
                }
            ]
        }
        mark_phase("lane_build")

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
        await manager.start_run(
            thread_id=thread_id,
            run_id=run_id,
            workflow_generator=run_with_credit_gate(
                credit_gate,
                _end_with_credit_stop(
                    handler.stream_workflow(
                        graph=graph,
                        input_state=input_state,
                        config=graph_config,
                    ),
                    ports.outcome,
                ),
            ),
            metadata={
                "workspace_id": workspace_id,
                "user_id": user_id,
                "sandbox_id": None,
                "sandbox": None,
                "started_at": datetime.now().isoformat(),
                "start_time": start_time,
                "is_byok": is_byok,
                "burst_slot_id": request.burst_slot_id,
                "locale": request.locale,
                "timezone": turn_context.tool_timezone,
                "handler": handler,
                "token_callback": token_callback,
                "run_handle": run_handle,
                "superagent_runtime": "p",
                "superagent_role": lane.role,
            },
            graph=graph,
            on_registered=scope.transfer_to_executor,
        )
        scope.release_admission()

        mark_phase("workflow_start")
        total_ms = (time.time() - start_time) * 1000
        phases = " ".join(f"{k}={v:.0f}ms" for k, v in phase_times.items())
        logger.info(
            f"[SUPERAGENT_P] thread_id={thread_id} role={lane.role} "
            f"model={_model_tag(config)} total={total_ms:.0f}ms ({phases})"
        )
        for phase, value in phase_times.items():
            safe_record(
                chat_turn_phase_duration_ms,
                value,
                {"phase": phase, "mode": "superagent_p"},
            )

        async for event in stream_from_log(thread_id, run_id, last_event_id=None):
            yield event

    except (asyncio.CancelledError, GeneratorExit):
        if scope.slot_owned:
            await scope.fail_open("client disconnected during setup")
        raise

    except Exception as exc:
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
