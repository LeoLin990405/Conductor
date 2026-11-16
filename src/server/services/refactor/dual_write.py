"""First call-site wiring: record the turn finalize behind the ``events`` flag.

This is the smallest useful dual-write slice, and the only one wired in Phase 1:
when a run's terminal CAS wins, write

1. an ``agent_run_contracts`` row for the response (permission mode, model,
   context budget — empty until Phase 2 fills them), and
2. a ``run.contract`` event in ``agent_events``.

Both are **best-effort after the terminal commit**: the turn is already settled,
so a refactor write failure must be logged and dropped, never propagated. The
call site wraps this in its own try/except as a second guard; this function also
never raises for a store failure.

Flag off is a strict no-op, so the runtime path is unchanged until a deployment
opts in. ``None`` return means "not recorded: flag off or no owner identity";
the dict return is diagnostic (what was written vs. skipped) and is logged at
debug level by the call site.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from src.server.contracts.refactor import (
    SUBAGENT_CONTRACT_VERSION,
    ContractError,
    ProposedMemory,
    SubagentResult,
)
from src.server.database.refactor import runs as db_runs
from src.server.services.refactor.subagent_result import StructuredSubagentResult
from src.server.services.refactor.memory_context import MemoryContext
from src.server.services.refactor import events as svc_events
from src.server.services.refactor_flags import refactor_flag_enabled

logger = logging.getLogger(__name__)

#: The mode a turn runs under when nothing set one. ``auto`` is the behaviour the
#: product already had (the user's standing approvals are honoured); D3's
#: per-thread store can tighten it to ``manual``. Kept as a fallback for the
#: projection write, not a claim about the runtime.
DEFAULT_PERMISSION_MODE = "auto"


def _contract_payload(
    *,
    status: str,
    execution_time: float | None,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    meta = dict(metadata or {})
    payload: dict[str, Any] = {"status": status}
    if execution_time is not None:
        payload["execution_time_s"] = round(float(execution_time), 3)
    for key in ("workspace_id", "is_byok", "model", "model_name"):
        if meta.get(key) is not None:
            payload[key] = meta[key]
    payload["has_errors"] = bool(meta.get("errors"))
    return payload


async def record_turn_finalized(
    *,
    user_id: str | None,
    thread_id: str | None,
    response_id: str | None,
    status: str,
    execution_time: float | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Write the finalize contract/event, best-effort; no-op when the flag is off."""
    if not refactor_flag_enabled("events"):
        return None
    if not user_id or not thread_id or not response_id:
        # Detached/recovery finalizes may lack the owner identity; the run row
        # is still the ledger, so skipping the projection is correct.
        logger.debug(
            "[refactor] finalize projection skipped (missing identity) "
            f"thread={thread_id} run={response_id}"
        )
        return None

    payload = _contract_payload(
        status=status, execution_time=execution_time, metadata=metadata
    )
    written: dict[str, Any] = {"event": None, "contract": None}

    try:
        written["event"] = await svc_events.record_event(
            user_id=user_id,
            event_type="run.contract",
            payload=payload,
            thread_id=thread_id,
            response_id=response_id,
        )
    except Exception:
        logger.warning(
            f"[refactor] finalize event write failed for run={response_id}",
            exc_info=True,
        )

    try:
        from src.server.services.refactor.permissions import (
            effective_permission_mode,
        )

        permission_mode = await effective_permission_mode(thread_id)
    except Exception:
        permission_mode = DEFAULT_PERMISSION_MODE
    try:
        written["contract"] = await db_runs.upsert_contract(
            conversation_response_id=response_id,
            permission_mode=permission_mode,
            model_id=str(payload["model"]) if payload.get("model") else None,
        )
    except Exception:
        logger.warning(
            f"[refactor] finalize contract write failed for run={response_id}",
            exc_info=True,
        )

    return written


def _summarize_hitl_response(hitl_response: Mapping[str, Any] | None) -> dict[str, Any]:
    """Count the decisions in a HITL resume payload — types only, no feedback.

    The feedback message and the action args are user/model content and can be
    sensitive; the event records *that* a decision happened and its shape.
    """
    summary = {
        "interrupts": 0,
        "approve": 0,
        "reject": 0,
        "feedback": False,
    }
    for response in (hitl_response or {}).values():
        summary["interrupts"] += 1
        if hasattr(response, "model_dump"):
            data = response.model_dump()
        elif hasattr(response, "dict"):
            data = response.dict()
        elif isinstance(response, Mapping):
            data = dict(response)
        else:
            continue
        decisions = data.get("decisions") or []
        for decision in decisions:
            if not isinstance(decision, Mapping):
                continue
            if decision.get("type") == "approve":
                summary["approve"] += 1
            elif decision.get("type") == "reject":
                summary["reject"] += 1
            if decision.get("message"):
                summary["feedback"] = True
    return summary


async def record_permission_decision(
    *,
    user_id: str | None,
    thread_id: str | None,
    response_id: str | None,
    hitl_response: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Append a ``permission.decision`` event for a HITL resume, best-effort.

    Flag off / missing identity → no-op ``None``. A store failure is logged and
    dropped: the resume must proceed with the user's answer regardless.
    """
    if not refactor_flag_enabled("events"):
        return None
    if not user_id or not thread_id or not response_id:
        return None
    payload = _summarize_hitl_response(hitl_response)
    try:
        return await svc_events.record_event(
            user_id=user_id,
            event_type="permission.decision",
            payload=payload,
            thread_id=thread_id,
            response_id=response_id,
        )
    except Exception:
        logger.warning(
            f"[refactor] permission decision write failed for run={response_id}",
            exc_info=True,
        )
        return None


async def record_memory_injection(
    *,
    user_id: str | None,
    thread_id: str | None,
    response_id: str | None,
    context: MemoryContext,
) -> dict[str, Any] | None:
    """Audit the turn's memory selection on the run contract, best-effort.

    Phase 2 writes the *selection* here while the prompt still renders the
    legacy baseline (shadow mode); Phase 3 flips the renderer and this row
    becomes the record of what was actually injected. The audit stores memory
    identity (tier, key, source) and provider provenance — never index text or
    bodies — so an external memory with the same key as a Conductor one cannot
    collide and the event names every source that contributed.

    Flag off / missing identity → ``None``. Either write failing is logged and
    dropped; a projection problem must never fail a turn.
    """
    from src.server.services.refactor.memory_runtime_controls import (
        automatic_memory_enabled,
    )

    if not await automatic_memory_enabled(user_id or ""):
        return None
    if not refactor_flag_enabled("memory_index"):
        return None
    if not user_id or not thread_id or not response_id:
        return None
    entry_records = list(context.audit_records())
    sources: dict[str, int] = {}
    for entry in context.entries:
        sources[entry.source] = sources.get(entry.source, 0) + 1
    written: dict[str, Any] = {"contract": None, "event": None}
    try:
        written["contract"] = await db_runs.upsert_contract(
            conversation_response_id=response_id,
            context_budget={
                "memory_tokens": context.tokens_used,
                "memory_budget": context.budget_tokens,
            },
            injected_memory=entry_records,
        )
    except Exception:
        logger.warning(
            f"[refactor] memory injection contract write failed for run={response_id}",
            exc_info=True,
        )
    try:
        written["event"] = await svc_events.record_event(
            user_id=user_id,
            event_type="memory.injected",
            payload={
                "entries": len(entry_records),
                "tokens": context.tokens_used,
                "budget": context.budget_tokens,
                "dropped": context.dropped,
                "per_tier": dict(context.per_tier),
                "sources": sources,
            },
            thread_id=thread_id,
            response_id=response_id,
        )
    except Exception:
        logger.warning(
            f"[refactor] memory injection event write failed for run={response_id}",
            exc_info=True,
        )
    return written


#: A completed run's result text is read from the task's checkpoint namespace;
#: that read is bounded so a slow export cannot hold up the subagent's finalize.
_RESULT_RESOLVE_TIMEOUT_S = 5.0
_SUMMARY_MAX_CHARS = 8000


async def record_subagent_result(
    *,
    thread_id: str,
    task_run_id: str,
    task_id: str,
    status: str,
    failure: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Project one finished subagent run's structured result, best-effort.

    Behind ``subagent_contract`` (default off). The ledger finalize is already
    committed when this runs, so every failure here is logged and dropped.

    A completed run's ``summary`` is its actual result text (resolved from the
    task checkpoint namespace); if it cannot be resolved we record nothing rather
    than fabricate a result. Failed/cancelled/interrupted runs summarize the
    terminal status (and error) — the contract requires a non-empty summary, and
    an incomplete run has no result to read.

    The ``subagent.result`` event stores counts and lengths only, never the
    result body: the body belongs to the run's result column/transcript.
    """
    if not refactor_flag_enabled("subagent_contract"):
        return None
    if not thread_id or not task_run_id or not task_id:
        return None
    if status not in ("completed", "interrupted", "error", "cancelled"):
        return None
    try:
        from src.server.database import conversation as conv_db
        from src.server.utils.pg_sanitize import refactor_owner_uuid

        meta = await conv_db.get_thread_auth_meta(thread_id)
        # OSS owners are non-UUID sentinels; the event/queue tables are UUID, so
        # use the same deterministic mapping as every other refactor owner write.
        settings_user_id = str(meta["user_id"]) if meta else None
        user_id = refactor_owner_uuid(settings_user_id)
    except Exception:
        logger.warning(
            f"[refactor] subagent contract owner lookup failed for thread={thread_id}",
            exc_info=True,
        )
        return None
    if user_id is None:
        return None

    structured: StructuredSubagentResult | None = None
    summary: str
    if status == "completed":
        structured = await _resolve_completed_result(
            thread_id=thread_id, task_id=task_id, task_run_id=task_run_id
        )
        if structured is None or not structured.summary:
            logger.debug(
                f"[refactor] subagent result text unavailable; skipping "
                f"contract projection for run={task_run_id}"
            )
            return None
        summary = structured.summary
    else:
        error = failure.get("error") if isinstance(failure, Mapping) else None
        summary = f"run ended {status}" + (f": {error}" if error else "")
    try:
        contract = SubagentResult(
            contract_version=SUBAGENT_CONTRACT_VERSION,
            task_id=str(task_id),
            status=status,
            summary=summary[:_SUMMARY_MAX_CHARS],
            findings=structured.findings if structured else (),
            evidence=structured.evidence if structured else (),
            artifact_ids=structured.artifact_ids if structured else (),
            open_questions=structured.open_questions if structured else (),
            proposed_memories=tuple(
                ProposedMemory(
                    type=item["type"],
                    text=item["text"],
                    change_kind=item.get("change_kind", "long_term"),
                    replaces_entry_key=item.get("replaces_entry_key"),
                    reason=item.get("reason"),
                    origin=item.get("origin", "assistant_inference"),
                    confidence=item.get("confidence", 0.5),
                    evidence_refs=tuple(item.get("evidence_refs") or ()),
                    metadata=item.get("metadata", {}),
                )
                for item in (structured.proposed_memories if structured else ())
            ),
        )
    except ContractError:
        logger.warning(
            f"[refactor] subagent contract rejected for run={task_run_id}",
            exc_info=True,
        )
        return None
    written: dict[str, Any] = {"contract": None, "event": None}
    try:
        written["contract"] = await db_runs.record_subagent_contract(
            task_run_id=task_run_id, result=contract.to_record()
        )
    except Exception:
        logger.warning(
            f"[refactor] subagent contract write failed for run={task_run_id}",
            exc_info=True,
        )
    if structured is not None and structured.proposed_memories:
        try:
            from src.server.services.refactor.proposals import project_proposals

            written["proposals"] = await project_proposals(
                user_id=user_id,
                settings_user_id=settings_user_id,
                thread_id=thread_id,
                task_run_id=task_run_id,
                proposals=structured.proposed_memories,
            )
        except Exception:
            logger.warning(
                f"[refactor] proposal queue write failed for run={task_run_id}",
                exc_info=True,
            )
    try:
        written["event"] = await svc_events.record_event(
            user_id=user_id,
            event_type="subagent.result",
            payload={
                "task_run_id": task_run_id,
                "task_id": str(task_id),
                "status": status,
                "contract_version": SUBAGENT_CONTRACT_VERSION,
                "summary_chars": len(contract.summary),
                "findings": len(contract.findings),
                "artifact_ids": len(contract.artifact_ids),
                "open_questions": len(contract.open_questions),
                "proposed_memories": len(contract.proposed_memories),
            },
            thread_id=thread_id,
            task_run_id=task_run_id,
        )
    except Exception:
        logger.warning(
            f"[refactor] subagent.result event write failed for run={task_run_id}",
            exc_info=True,
        )
    return written


async def _resolve_completed_result(
    *, thread_id: str, task_id: str, task_run_id: str
) -> StructuredSubagentResult | None:
    """One bounded read: the summary plus the fields the transcript supports."""
    try:
        from src.server.services.runs.task_registry_store import (
            resolve_task_structured_result,
        )

        value = await asyncio.wait_for(
            resolve_task_structured_result(thread_id, task_id, task_run_id),
            timeout=_RESULT_RESOLVE_TIMEOUT_S,
        )
    except Exception:
        logger.warning(
            f"[refactor] subagent result resolution failed for run={task_run_id}",
            exc_info=True,
        )
        return None
    if not isinstance(value, StructuredSubagentResult) or not value.summary.strip():
        return None
    return value
