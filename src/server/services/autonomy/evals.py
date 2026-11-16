"""Batching queued Triggers into a Router candidate batch."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from src.server.database.autonomy import evals as eval_db
from src.server.database.autonomy import router as router_db
from src.server.database.autonomy import sources as source_db
from src.server.database.autonomy import tickets as ticket_db
from src.server.database.autonomy import triggers as trigger_db
from src.server.database.autonomy.effects import enqueue_effect
from src.server.services.autonomy import policy, router, shard as router_shard
from src.server.services.autonomy.context import thread_context
from src.server.services.autonomy.models import EFFECT_ROUTER_TURN

logger = logging.getLogger(__name__)

MAX_ROUTER_BATCH_EVALS = 8


def router_eval_batches(eval_ids: list[str]) -> list[list[str]]:
    """Partition immutable Eval identities into bounded Router requests."""
    unique_ids = list(dict.fromkeys(str(item) for item in eval_ids if item))
    return [
        unique_ids[offset : offset + MAX_ROUTER_BATCH_EVALS]
        for offset in range(0, len(unique_ids), MAX_ROUTER_BATCH_EVALS)
    ]

# Dispatch will not wait longer than this, and never past the eval's own
# deadline_at. create/start and the stale sweep stamp the same five minutes.
ROUTER_EVAL_DEADLINE_SECONDS = 300


async def recover_stale_evals() -> int:
    """Requeue Trigger batches whose Router Turn never decided.

    The scheduler calls this on every pass, including the first pass after a
    restart. Expired batch runs are cancelled by their persisted batch request
    key only after every member Eval is no longer live.
    """
    expired = await eval_db.expire_stale_evals()
    await _cancel_expired_router_batches(expired)
    if expired:
        logger.warning("[autonomy] recovered %s expired Router eval(s)", len(expired))
    return len(expired)


def _router_dispatch_timeout(eval_row: dict[str, Any]) -> float:
    """Seconds dispatch may block: remaining deadline, capped at five minutes.

    A missing deadline uses the cap. Zero means the deadline is already past
    and the caller must not dispatch.
    """
    cap = max(0.0, float(ROUTER_EVAL_DEADLINE_SECONDS))
    deadline = eval_row.get("deadline_at")
    if deadline is None:
        return cap
    if isinstance(deadline, str):
        deadline = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    if not isinstance(deadline, datetime):
        return cap
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        return 0.0
    return min(cap, remaining)


async def _abandon_router_eval(eval_id: str, *, cancel_run: bool = True) -> None:
    """Expire one eval; optionally cancel its batch when no peer remains live.

    A raised error would nack the effect and retry this same eval. The retry
    belongs to the thread requeue inside ``expire_one``. Lookup and cancel
    failures are logged and do not undo the expiry.
    """
    try:
        await eval_db.expire_one(eval_id)
    except Exception:
        logger.exception("[autonomy] expire router eval %s failed", eval_id)
    if cancel_run:
        await _cancel_router_eval_run(eval_id)


async def _cancel_router_eval_run(eval_id: str) -> None:
    """Cancel the persisted batch containing this Eval only if no peer is live."""
    try:
        batches = await eval_db.router_effect_batches([eval_id])
    except Exception:
        logger.exception("[autonomy] router eval %s batch lookup failed", eval_id)
        return
    if batches:
        await _cancel_expired_router_batches([eval_id], batches=batches)
        return
    await _cancel_router_eval_run_legacy(eval_id)


async def _cancel_expired_router_batches(
    expired_ids: list[str], *, batches: list[dict[str, Any]] | None = None
) -> None:
    """Cancel each persisted batch once, after all member Evals stop being live."""
    if not expired_ids:
        return
    if batches is None:
        try:
            batches = await eval_db.router_effect_batches(expired_ids)
        except Exception:
            logger.exception("[autonomy] expired Router batch lookup failed")
            return
    covered: set[str] = set()
    seen: set[str] = set()
    expired_set = set(expired_ids)
    for batch in batches:
        request_key = str(batch.get("idempotency_key") or "")
        members = [str(item) for item in batch.get("eval_ids") or []]
        covered.update(expired_set.intersection(members))
        if not request_key or request_key in seen:
            continue
        seen.add(request_key)
        live = False
        for member_id in members:
            row = await eval_db.get_eval(member_id)
            if row and row.get("status") in {"queued", "delivered"}:
                live = True
                break
        if not live:
            await _cancel_router_request(request_key, ",".join(members))
    for eval_id in expired_ids:
        if eval_id not in covered:
            await _cancel_router_eval_run_legacy(eval_id)


async def _cancel_router_eval_run_legacy(eval_id: str) -> None:
    """Cancel a pre-batching run stored under the per-Eval request key."""
    from src.server.database.runs.lifecycle import find_run_by_request_key
    from src.server.services.cancel_dispatch import cancel_workflow
    from src.server.services.projects.dispatch import stable_request_key

    request_key = stable_request_key(f"autonomy:router:{eval_id}")
    try:
        run = await find_run_by_request_key(request_key)
    except Exception:
        logger.exception("[autonomy] router eval %s run lookup failed", eval_id)
        return
    if not run:
        return
    run_id = str(run.get("conversation_response_id") or "").strip()
    thread_id = str(run.get("conversation_thread_id") or "").strip()
    if not run_id or not thread_id:
        logger.warning(
            "[autonomy] router eval %s run row has no run id; not cancelling",
            eval_id,
        )
        return
    try:
        await cancel_workflow(thread_id, run_id, by_user=False)
    except Exception:
        logger.exception(
            "[autonomy] router eval %s cancel failed for run %s",
            eval_id,
            run_id,
        )


def _issue_context(trigger: dict[str, Any]) -> dict[str, Any] | None:
    """Project Issue fields onto a candidate without widening every card.

    Comment triggers often have no Issue payload. Leaving the key off keeps
    those cards the same shape as before, so the Router batch and the snapshot
    prompt gain the Issue only when the Trigger actually carried one. Labels
    and other Issue metadata stay on the Trigger; title, body, truncated, and
    url (the stored ``overflow_url`` when only that is present) are the
    routing facts.
    """
    payload = trigger.get("payload")
    if not isinstance(payload, dict):
        return None
    raw = payload.get("issue_context")
    if not isinstance(raw, dict):
        return None
    projected: dict[str, Any] = {}
    for key in ("title", "body"):
        value = raw.get(key)
        if isinstance(value, str):
            projected[key] = value
    # The issue context contract stores the spill link as ``overflow_url``.
    # The card keeps one ``url`` key and accepts either spelling.
    for key in ("url", "overflow_url"):
        value = raw.get(key)
        if isinstance(value, str):
            projected["url"] = value
            break
    truncated = raw.get("truncated")
    if isinstance(truncated, bool):
        projected["truncated"] = truncated
    return projected or None


def candidate_card(
    trigger: dict[str, Any],
    source: dict[str, Any],
    *,
    strength: str = "explicit",
) -> dict[str, Any]:
    card = {
        "trigger_id": trigger["trigger_id"],
        "source": str(source.get("kind") or "github"),
        "source_id": str(trigger.get("source_id") or ""),
        "repository": (source.get("config") or {}).get("repository")
        or (source.get("config") or {}).get("chat_id", ""),
        "thread_key": trigger["thread_key"],
        "actor": trigger["actor_login"],
        "addressed": bool(trigger.get("addressed")),
        "strength": strength,
        "text": str(trigger.get("text") or "")[:8000],
        "text_is_untrusted_source_content": True,
        "url": trigger.get("url") or "",
    }
    issue_context = _issue_context(trigger)
    if issue_context is not None:
        card["issue_context"] = issue_context
    return card


async def start_due_evals() -> int:
    """Create per-Thread Evals, then batch their IDs per user's Router turn."""
    by_user: dict[str, list[str]] = {}
    for row in await eval_db.list_queued_evals_without_effect():
        by_user.setdefault(row["user_id"], []).append(row["eval_id"])
    for thread in await eval_db.list_pending_threads():
        trigger_ids = list(thread.get("pending_trigger_ids") or [])
        if not trigger_ids:
            continue
        triggers = [row for row in [await trigger_db.get_trigger(tid) for tid in trigger_ids] if row]
        # Admission and Source routing mode decide whether each queued event
        # deserves a Router decision; rejected events leave the durable queue.
        source = await source_db.get_source_any_user(str(thread["source_id"]))
        if source is None:
            continue
        current = await policy.current_policy(str(thread["user_id"]))
        admission = policy.effective_admission(
            current.get("admission"), source.get("config") or {}
        )
        accepted = [
            trigger
            for trigger in triggers
            if trigger
            and policy.is_source_candidate(
                trigger,
                source.get("config") or {},
                str(current.get("accept_mode") or "explicit"),
                admission,
            )
        ]
        rejected = [
            str(trigger.get("trigger_id") or "")
            for trigger in triggers
            if trigger and not policy.is_source_candidate(
                trigger, source.get("config") or {},
                str(current.get("accept_mode") or "explicit"), admission,
            )
        ]
        if rejected:
            await eval_db.prune_pending_triggers(str(thread["thread_key"]), rejected)
        if not accepted:
            continue
        # Eval ownership remains per Thread, so batching does not serialize
        # unrelated Threads or their Worker execution.
        trigger = accepted[0]
        eval_row = await eval_db.start_thread_eval(
            thread_key=str(thread["thread_key"]),
            trigger_ids=[str(trigger["trigger_id"])],
            policy_version=str(current.get("version") or ""),
        )
        if eval_row is None:
            continue
        by_user.setdefault(str(thread["user_id"]), []).append(
            str(eval_row["eval_id"])
        )
    started = 0
    for user_id, eval_ids in by_user.items():
        # Load above the configured threshold spreads batches across separate
        # Router sessions; at or below it every batch stays on shard 0.
        shard_count = router_shard.router_shard_count(len(eval_ids))
        for batch_ids in router_eval_batches(eval_ids):
            batch_key = hashlib.sha256(",".join(batch_ids).encode()).hexdigest()[:20]
            shard = router_shard.router_shard_for(batch_key, shard_count)
            if await enqueue_effect(
                user_id=user_id,
                kind=EFFECT_ROUTER_TURN,
                idempotency_key=f"autonomy:router-batch:{batch_key}",
                payload={
                    "eval_ids": batch_ids,
                    "user_id": user_id,
                    "shard": shard,
                },
            ):
                started += len(batch_ids)
    return started


async def route_eval_effect(effect) -> None:
    """Dispatch one bounded batch of candidate Evals to the Router Session."""
    eval_ids = [str(item) for item in effect.payload.get("eval_ids") or [] if item]
    if not eval_ids and effect.payload.get("eval_id"):
        eval_ids = [str(effect.payload["eval_id"])]
    if not eval_ids:
        raise ValueError("router effect has no eval ids")

    batch_identity = ",".join(eval_ids)
    batch_request_key = (
        f"autonomy:router-batch:{hashlib.sha256(batch_identity.encode()).hexdigest()[:20]}"
    )
    shard = int(effect.payload.get("shard") or 0)
    eval_rows = []
    for eval_id in eval_ids:
        row = await eval_db.get_eval(eval_id)
        if row and row["status"] in {"queued", "delivered"}:
            eval_rows.append(row)
    if not eval_rows:
        return

    # A batch is one Router run. Bound it by the earliest member deadline and
    # expire all members together if that run cannot finish within the bound.
    expired_rows = [row for row in eval_rows if _router_dispatch_timeout(row) <= 0]
    live_rows = [row for row in eval_rows if row not in expired_rows]
    if expired_rows:
        for row in expired_rows:
            await _abandon_router_eval(str(row["eval_id"]), cancel_run=False)
        if live_rows:
            await _enqueue_router_batch(
                user_id=str(effect.payload.get("user_id") or ""),
                rows=live_rows,
                shard=shard,
            )
            return
        logger.warning("[autonomy] router batch %s deadline already past", batch_identity)
        await _cancel_router_request(batch_request_key, batch_identity)
        return
    timeout = min(_router_dispatch_timeout(row) for row in eval_rows)

    user_id = str(effect.payload.get("user_id") or "")
    triggers_with_evals = []
    for eval_row in eval_rows:
        for trigger_id in eval_row["trigger_ids"]:
            trigger = await trigger_db.get_trigger(str(trigger_id))
            if trigger:
                triggers_with_evals.append((eval_row, trigger))
    if not triggers_with_evals:
        return
    sources = {
        str(row.get("source_id") or ""): row
        for row in [
            await source_db.get_source_any_user(str(trigger.get("source_id") or ""))
            for _, trigger in triggers_with_evals
        ]
        if row
    }
    current = await policy.current_policy(user_id)
    snapshot_enabled = router.router_snapshot_enabled()
    candidates = []
    for eval_row, trigger in triggers_with_evals:
        source = sources.get(str(trigger.get("source_id") or ""))
        if source is None:
            continue
        config = source.get("config") or {}
        strength = policy.admit(
            trigger, config,
            policy.effective_admission(current.get("admission"), config),
        ) or "ignore"
        card = candidate_card(trigger, source, strength=strength)
        card["eval_id"] = str(eval_row["eval_id"])
        if snapshot_enabled:
            card["channel"] = str(config.get("channel") or "")
        candidates.append(card)
    if not candidates:
        return

    thread_keys = list(dict.fromkeys(str(row["thread_key"]) for row in eval_rows))
    per_thread_budget = max(1, 12_000 // max(1, len(thread_keys)))
    context_by_thread: dict[str, dict[str, Any]] = {}
    if snapshot_enabled:
        lookups = []
        lookup_contexts = []
        for eval_row in eval_rows:
            current_eval_id = str(eval_row["eval_id"])
            trigger_ids = [
                str(card["trigger_id"])
                for card in candidates
                if str(card.get("eval_id") or "") == current_eval_id
                and card.get("trigger_id")
            ]
            if not trigger_ids:
                continue
            stable_key = router.stable_router_decision_key(
                user_id,
                trigger_ids,
                accept_mode=str(current.get("accept_mode") or "explicit"),
                policy_version=str(current.get("version") or ""),
            )
            lookups.append(
                asyncio.wait_for(
                    router_db.latest_router_decision_for_batch(
                        user_id,
                        stable_key,
                        exclude_eval_id=current_eval_id,
                        max_age_seconds=3600,
                    ),
                    timeout=0.2,
                )
            )
            lookup_contexts.append((current_eval_id, trigger_ids))
        results = await asyncio.gather(*lookups, return_exceptions=True)
        recent_by_thread: dict[str, dict[str, Any]] = {}
        eval_thread = {
            str(row["eval_id"]): str(row.get("thread_key") or "") for row in eval_rows
        }
        for (current_eval_id, trigger_ids), result in zip(
            lookup_contexts, results, strict=True
        ):
            if not isinstance(result, dict):
                if isinstance(result, BaseException):
                    logger.warning(
                        "[autonomy] prior Router decision lookup failed eval=%s",
                        current_eval_id,
                    )
                continue
            thread_key = eval_thread.get(current_eval_id, "")
            if not thread_key:
                continue
            recent_by_thread[thread_key] = {
                "eval_id": str(result.get("eval_id") or ""),
                "trigger_id": str(result.get("routed_trigger_id") or ""),
                "decision": str(result.get("route_decision") or ""),
                "decided_at": str(result.get("decided_at") or ""),
                "scope": "same_trigger_batch",
                "informational_only": True,
                "candidate_ids": trigger_ids[:8],
            }
    for thread_key in thread_keys:
        if snapshot_enabled:
            # The live batch already carries its candidate rows. Keep this
            # context scoped to that batch and leave older history on demand.
            current_candidates = [
                {
                    "eval_id": str(card.get("eval_id") or ""),
                    "trigger_id": str(card.get("trigger_id") or ""),
                }
                for card in candidates
                if str(card.get("thread_key") or "") == thread_key
            ]
            context_by_thread[thread_key] = {
                "thread_key": thread_key,
                "scope": "current_router_batch",
                "candidates": current_candidates,
                "history": (
                    "Read older trigger or ticket history with autonomy_read "
                    "only when it affects this decision."
                ),
            }
            if thread_key in recent_by_thread:
                context_by_thread[thread_key]["recent_decision"] = recent_by_thread[
                    thread_key
                ]
        else:
            thread_triggers = await trigger_db.list_triggers_for_thread(
                thread_key, limit=20
            )
            thread_tickets = await ticket_db.list_tickets_for_thread(thread_key)
            context_by_thread[thread_key] = thread_context(
                thread_triggers,
                thread_tickets,
                max_chars=per_thread_budget,
                thread_key=thread_key,
            )

    try:
        await asyncio.wait_for(
            router.dispatch_candidate_batch(
                user_id=user_id,
                eval_id=str(eval_rows[0]["eval_id"]),
                eval_ids=[str(row["eval_id"]) for row in eval_rows],
                candidates=candidates,
                accept_mode=str(current.get("accept_mode") or "explicit"),
                policy_version=str(current.get("version") or ""),
                thread_context={"threads": context_by_thread},
                request_key=batch_request_key,
                shard=shard,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning("[autonomy] router batch %s dispatch timed out", batch_identity)
        expired_rows = [row for row in eval_rows if _router_dispatch_timeout(row) <= 0]
        live_rows = [row for row in eval_rows if row not in expired_rows]
        # The five-minute cap is itself a terminal deadline for the entire
        # dispatch. If the DB deadline has elapsed, preserve still-live peers
        # and let their own replacement batch finish.
        if not expired_rows:
            expired_rows = eval_rows
            live_rows = []
        for row in expired_rows:
            await _abandon_router_eval(str(row["eval_id"]), cancel_run=False)
        if live_rows:
            await _enqueue_router_batch(user_id=user_id, rows=live_rows, shard=shard)
            return
        await _cancel_router_request(batch_request_key, batch_identity)
        return

    for eval_row in eval_rows:
        await eval_db.mark_delivered(str(eval_row["eval_id"]))
    logger.info(
        "[autonomy] router batch evals=%s candidates=%s",
        len(eval_rows), len(candidates),
    )


async def _enqueue_router_batch(
    *, user_id: str, rows: list[dict[str, Any]], shard: int = 0
) -> None:
    ids = [str(row["eval_id"]) for row in rows]
    batch_key = hashlib.sha256(",".join(ids).encode()).hexdigest()[:20]
    await enqueue_effect(
        user_id=user_id,
        kind=EFFECT_ROUTER_TURN,
        idempotency_key=f"autonomy:router-batch:{batch_key}",
        payload={"eval_ids": ids, "user_id": user_id, "shard": int(shard)},
    )


async def _cancel_router_request(request_key: str, label: str) -> None:
    """Cancel only the Router run adopted by this exact dispatch key."""
    from src.server.database.runs.lifecycle import find_run_by_request_key
    from src.server.services.cancel_dispatch import cancel_workflow
    from src.server.services.projects.dispatch import stable_request_key

    try:
        run = await find_run_by_request_key(stable_request_key(request_key))
    except Exception:
        logger.exception("[autonomy] router batch %s run lookup failed", label)
        return
    if not run:
        return
    run_id = str(run.get("conversation_response_id") or "").strip()
    thread_id = str(run.get("conversation_thread_id") or "").strip()
    if not run_id or not thread_id:
        logger.warning("[autonomy] router batch %s run row has no run id", label)
        return
    try:
        await cancel_workflow(thread_id, run_id, by_user=False)
    except Exception:
        logger.exception(
            "[autonomy] router batch %s cancel failed for run %s", label, run_id
        )
