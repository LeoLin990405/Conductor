"""Worker lifecycle observations made by the autonomy pass."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from src.server.database.autonomy import tickets as ticket_db
from src.server.services.autonomy.completion import settle_native_completion
from src.server.services.autonomy.host import StaleWorkerReport, apply_worker_report
from src.server.services.autonomy.report_continuation import (
    CONTINUE_AS_STATE,
    continuation_marker,
    push_queued_items,
    report_digest,
    queued_next_items,
    report_may_continue,
)
from src.server.services.projects.dispatch import ProjectDispatcher

logger = logging.getLogger(__name__)


async def ensure_launch_effects() -> int:
    """Re-enqueue a lost Worker launch for a queued Ticket.

    A Ticket row and its launch effect normally commit together; this sweep is
    the crash window when a process died between the two.
    """
    from src.server.database.autonomy.effects import enqueue_effect
    from src.server.services.autonomy.models import EFFECT_LAUNCH_WORKER

    queued = 0
    # list_tickets is per-user; the pass needs every user's queued tickets.
    from src.server.database.pool import get_db_connection

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT ticket_id, user_id, attempts FROM autonomy_tickets
                WHERE status = 'queued'
                  AND mandate->>'orchestration_only' IS DISTINCT FROM 'true'
                ORDER BY created_at
                LIMIT 100
                """
            )
            rows = await cur.fetchall()
    for ticket_id, user_id, attempts in rows:
        # Revision is deliberately not part of the key. A summary, line, or
        # attention write bumps it between sweeps and would insert another
        # launch row for the same attempt. The handler re-reads under the
        # launch lock, so one row per attempt is enough.
        if await enqueue_effect(
            user_id=str(user_id),
            kind=EFFECT_LAUNCH_WORKER,
            idempotency_key=(
                f"autonomy:launch:{ticket_id}:attempt:{int(attempts or 0) + 1}"
            ),
            payload={"ticket_id": str(ticket_id)},
        ):
            queued += 1
    return queued


async def _release_settled_worktree(ticket_id: str) -> None:
    """Release the server checkout of a Ticket whose native report ended it.

    Blocked/waiting Tickets stay open and keep their checkout for a dirty
    resume (``release_server_ticket_worktree`` skips open Tickets). A failed
    removal becomes durable pending state for the retry sweep.
    """
    from src.server.services.autonomy.ticket_worktree import (
        release_server_ticket_worktree,
    )

    try:
        settled = await ticket_db.get_ticket(ticket_id)
        if settled is not None:
            await release_server_ticket_worktree(settled, reason="report")
    except Exception:  # noqa: BLE001 - the report is already settled
        logger.warning(
            "[autonomy] ticket worktree release failed after report ticket=%s",
            ticket_id,
            exc_info=True,
        )


async def sweep_worktree_cleanups() -> int:
    """Executor-tick entry for the durable Ticket worktree cleanup retry."""
    from src.server.services.autonomy.ticket_worktree import (
        sweep_ticket_worktree_cleanups,
    )

    try:
        return await sweep_ticket_worktree_cleanups()
    except Exception:  # noqa: BLE001 - housekeeping must not stall the pass
        logger.exception("[autonomy] ticket worktree cleanup sweep failed")
        return 0


async def poll_running_workers() -> int:
    """Observe running Tickets whose Worker thread reached a terminal state."""
    dispatcher = ProjectDispatcher()
    from src.server.database.pool import get_db_connection
    from src.server.services.autonomy.delivery_freeze import (
        DeliveryFreeze,
        DeliveryStateNotDurable,
        bound_freeze,
        delivery_state_fields,
        measure_delivery,
    )

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT ticket_id, user_id, worker_thread_id
                FROM autonomy_tickets
                WHERE status = 'running' AND worker_thread_id IS NOT NULL
                ORDER BY updated_at
                LIMIT 100
                """
            )
            rows = await cur.fetchall()
    checked = 0
    for ticket_id, user_id, thread_id in rows:
        status = await dispatcher.thread_run_status(
            user_id=str(user_id), thread_id=str(thread_id)
        )
        if status in (None, "in_progress"):
            continue
        ticket = await ticket_db.get_ticket(str(ticket_id))
        if ticket is None or ticket["status"] != "running":
            continue
        report = await ticket_db.latest_report(str(ticket_id))
        checked += 1
        # A report older than the current Worker row belongs to a previous
        # attempt: a retry parks the Ticket back to queued/running, so only a
        # report filed after that row was touched can settle this attempt.
        report_is_current = (
            report is not None
            and report.get("created_at") is not None
            and ticket.get("updated_at") is not None
            and report["created_at"] >= ticket["updated_at"]
        )
        if report_is_current:
            continue
        provider = str(ticket.get("worker_provider") or "")
        if provider:
            # A native CLI has no autonomy report tool. Its closing assistant
            # message is the JSON completion report; a completed turn without
            # a valid one waits for a person instead of reading as success.
            completed = status == "completed"
            assistant = None
            try:
                assistant = await dispatcher.thread_last_assistant_text(
                    user_id=str(user_id), thread_id=str(thread_id)
                )
            except Exception:
                logger.warning(
                    "[autonomy] native worker replay read failed ticket=%s",
                    ticket_id,
                    exc_info=True,
                )
            expected_run_id = str(ticket.get("worker_run_id") or "") or None
            mandate = dict(ticket.get("mandate") or {})
            continuation = continuation_marker(mandate, run_id=expected_run_id)
            if continuation is not None and continuation.get(
                "report_digest"
            ) == report_digest(assistant):
                # The run whose report was already filed is still the thread's
                # latest: the pushed item has not started yet. Settling it
                # again would complete the Ticket under the continuation.
                continue
            baseline_binding = mandate.get("git_delivery_baseline")
            git_summary = None
            commit_delivery = "not_checked"
            baseline_clean = False
            mandate_fields: dict[str, Any] | None = None
            if continuation is not None and completed:
                # The recorded delivery state belongs to the run whose report
                # was filed, and nothing rebinds the Ticket to the pushed
                # item's run: reusing it would settle this item on the
                # previous item's verdict. Unknown sends it to a person.
                git_summary = "Git workspace status is unknown because it could not be checked safely."
                commit_delivery = "unknown"
            elif completed and not (mandate.get("speculative") or mandate.get("read_only")):
                result_binding = mandate.get("git_delivery_result")
                if (
                    isinstance(baseline_binding, dict)
                    and baseline_binding.get("version") == 2
                ) or (
                    isinstance(result_binding, dict)
                    and result_binding.get("version") == 2
                ):
                    result_matches = (
                        isinstance(result_binding, dict)
                        and result_binding.get("version") == 2
                        and result_binding.get("thread_id") == str(thread_id)
                        and result_binding.get("run_id") == expected_run_id
                        and result_binding.get("attempt") == int(ticket.get("attempts") or 0)
                        and isinstance(result_binding.get("summary"), str)
                    )
                    if result_matches:
                        git_summary = result_binding["summary"]
                        recorded_delivery = result_binding.get("commit_delivery")
                        # A result written before commit checks existed has
                        # no verdict; it keeps the workspace-only behavior.
                        if recorded_delivery in {
                            "delivered",
                            "missing",
                            "unknown",
                            "unverified",
                        }:
                            commit_delivery = recorded_delivery
                        baseline_clean = result_binding.get("baseline_clean") is True
                        if result_binding.get("frozen") is True:
                            # The result was measured against a frozen HEAD,
                            # taken when the report arrived. The mandate's
                            # freeze must be that one: a result whose freeze
                            # is missing says nothing about which commit it
                            # measured, so the delivery is not confirmed.
                            freeze = bound_freeze(
                                mandate,
                                thread_id=str(thread_id),
                                run_id=expected_run_id or "",
                                attempt=int(ticket.get("attempts") or 0),
                            )
                            if (
                                freeze is None
                                or freeze.head != result_binding.get("frozen_head")
                            ):
                                git_summary = "Git workspace status is unknown because it could not be checked safely."
                                commit_delivery = "unknown"
                    else:
                        # A v2 result is persisted before native run finalization.
                        # Missing or stale data is therefore immediately unknown;
                        # never defer a completed Ticket waiting for a side task.
                        git_summary = "Git workspace status is unknown because it could not be checked safely."
                        commit_delivery = "unknown"
                elif isinstance(baseline_binding, dict) and baseline_binding.get("version") == 1:
                    try:
                        from src.server.database.conversation.native_runtime import (
                            get_thread_metadata,
                        )
                        from src.server.services.native_runtime.contracts import (
                            NativeRuntimeSpec,
                        )
                        from src.server.services.native_runtime.cwd import (
                            native_cwd_for_thread,
                        )
                        from src.server.services.autonomy.git_delivery import (
                            AsyncWorkspaceDeliveryLock,
                            GitSnapshot,
                            DELIVERY_ORIGIN_KEY,
                            safe_git_workspace_summary,
                            with_delivery_origin,
                        )

                        metadata = None
                        metadata_error = None
                        for metadata_attempt in range(3):
                            try:
                                metadata = await get_thread_metadata(str(thread_id))
                                metadata_error = None
                                break
                            except Exception as exc:
                                metadata_error = exc
                                if metadata_attempt < 2:
                                    await asyncio.sleep(0.05)
                        if metadata_error is not None:
                            raise metadata_error
                        spec = NativeRuntimeSpec.from_thread_metadata(metadata)
                        from src.server.services.autonomy.ticket_worktree import (
                            server_worktree_cwd,
                        )

                        # The bound server checkout is the Worker's cwd; the
                        # thread metadata cwd is only the legacy fallback.
                        native_cwd = server_worktree_cwd(ticket) or native_cwd_for_thread(
                            str(thread_id), spec.cwd if spec is not None else None
                        )
                        binding_matches = (
                            baseline_binding.get("thread_id") == str(thread_id)
                            and baseline_binding.get("attempt") == int(ticket.get("attempts") or 0)
                        )
                        if binding_matches:
                            async with AsyncWorkspaceDeliveryLock(native_cwd, timeout=30.0):
                                if isinstance(baseline_binding.get("snapshot"), dict) and baseline_binding[
                                    "snapshot"
                                ].get("error"):
                                    git_summary = await asyncio.to_thread(
                                        safe_git_workspace_summary,
                                        native_cwd,
                                        None,
                                        baseline_unavailable=True,
                                    )
                                    commit_delivery = "unknown"
                                else:
                                    baseline = GitSnapshot.from_record(baseline_binding.get("snapshot"))
                                    if baseline is None:
                                        git_summary = await asyncio.to_thread(
                                            safe_git_workspace_summary,
                                            native_cwd,
                                            None,
                                            baseline_unavailable=True,
                                        )
                                        commit_delivery = "unknown"
                                    else:
                                        # A freeze this run already took is
                                        # reused: a retry measures the same
                                        # commit, never a later one. Without
                                        # one, the HEAD is read here and frozen
                                        # for every later retry.
                                        freeze = bound_freeze(
                                            mandate,
                                            thread_id=str(thread_id),
                                            run_id=expected_run_id or "",
                                            attempt=int(ticket.get("attempts") or 0),
                                        )
                                        delivery = await measure_delivery(
                                            native_cwd,
                                            baseline=with_delivery_origin(
                                                baseline,
                                                mandate.get(DELIVERY_ORIGIN_KEY),
                                            ),
                                            freeze=freeze,
                                        )
                                        git_summary = delivery.summary
                                        commit_delivery = delivery.verdict
                                        baseline_clean = (
                                            baseline.root_id is not None
                                            and not baseline.entries
                                        )
                                        # The frozen HEAD and the delivery it
                                        # produced are recorded with the report:
                                        # a later poll reads them instead of
                                        # measuring a branch that moved on.
                                        mandate_fields = delivery_state_fields(
                                            delivery.freeze
                                            if delivery.freeze is not None
                                            else DeliveryFreeze(),
                                            delivery,
                                            thread_id=str(thread_id),
                                            run_id=expected_run_id or "",
                                            attempt=int(ticket.get("attempts") or 0),
                                            baseline_clean=baseline_clean,
                                        )
                    except Exception:
                        logger.warning(
                            "[autonomy] native worker Git check failed ticket=%s",
                            ticket_id,
                            exc_info=True,
                        )
                        git_summary = "Git workspace status is unknown because it could not be checked safely."
                        commit_delivery = "unknown"
                # No versioned baseline is a pre-contract Ticket. Preserve its
                # legacy behavior and do not attribute historical dirt to it.
            completion_kwargs = {}
            queued_items: list[str] = []
            if completed:
                outcome = settle_native_completion(
                    assistant,
                    git_summary=git_summary,
                    commit_delivery=commit_delivery,
                    speculative=bool(mandate.get("speculative")),
                    baseline_clean=baseline_clean,
                )
                report_state = outcome["state"]
                summary = (
                    outcome["summary"]
                    or "Native Worker completed; open the run for its output."
                )
                question = outcome["question"]
                attention_reason = outcome["attention_reason"]
                from src.llms.native_cli.isolation import redact_diagnostic

                # Keep the report body complete while limiting only summary.
                body = redact_diagnostic(outcome["body"], None)
                if outcome["completion"] is not None:
                    completion_kwargs = {
                        "completion": outcome["reconciled"],
                        "completion_report": outcome["completion"],
                    }
                if report_may_continue(
                    report_state=report_state,
                    has_completion_report=outcome["completion"] is not None,
                    attention_reason=attention_reason,
                    mandate=mandate,
                ):
                    queued_items = await queued_next_items(str(ticket_id))
                    if queued_items is None:
                        # The queue could not be read. Completing now would
                        # strand whatever is queued behind a terminal Ticket,
                        # so the report waits for the next pass.
                        continue
                    if queued_items:
                        # Done with this item, not with the Ticket: file the
                        # report as waiting and push the next item below.
                        report_state = CONTINUE_AS_STATE
                        completion_kwargs["completion"] = replace(
                            outcome["reconciled"], state=CONTINUE_AS_STATE
                        )
            else:
                # A run that did not complete is a failure whatever it claims.
                report_state = "failed"
                summary = assistant or f"Native Worker ended with {status}."
                question = None
                attention_reason = None
                body = assistant
            try:
                report_kwargs: dict[str, Any] = {
                    "ticket_id": str(ticket_id),
                    "state": report_state,
                    "summary": summary[:4000],
                    "body": body,
                    "question": question,
                    "expected_thread_id": str(thread_id),
                    "attention_reason": attention_reason,
                    "expected_run_id": expected_run_id,
                }
                if mandate_fields:
                    # The frozen delivery state is recorded with the report.
                    report_kwargs["mandate_fields"] = mandate_fields
                await apply_worker_report(**report_kwargs, **completion_kwargs)
            except StaleWorkerReport:
                logger.info("[autonomy] ignored report from switched worker ticket=%s", ticket_id)
                continue
            except DeliveryStateNotDurable:
                # The frozen delivery state could not be recorded, so the
                # delivery is not confirmed: the Ticket stays running and the
                # next pass measures it again. When the native run's own write
                # landed, that pass reuses its frozen HEAD; when nothing was
                # persisted, the next pass takes a new freeze.
                logger.warning(
                    "[autonomy] native report was not settled ticket=%s",
                    ticket_id,
                    exc_info=True,
                )
                continue
            if queued_items:
                # The report is filed and its delivery frozen; the next item
                # reopens the Ticket through the follow-up delivery.
                try:
                    await push_queued_items(
                        ticket,
                        queued_items,
                        run_id=expected_run_id,
                        report_text=assistant,
                    )
                except Exception:  # noqa: BLE001 - the Ticket waits for a person
                    logger.warning(
                        "[autonomy] could not push the next queued item ticket=%s",
                        ticket_id,
                        exc_info=True,
                    )
                continue
            await _release_settled_worktree(str(ticket_id))
        else:
            # The PTC turn ended without the report tool. Do not claim success:
            # exactly the state where a person must look at the raw thread.
            # Fence on the observed line so a switched-away turn cannot push a
            # requeued Ticket back to waiting.
            updated = await ticket_db.update_ticket(
                str(ticket_id),
                expected_status="running",
                expected_thread_id=str(thread_id),
                status="waiting",
                summary="Worker ended without a structured report.",
                attention={"question": "The Worker ended without reporting. Review the raw run, then reply or retry."},
            )
            if updated is None:
                logger.info("[autonomy] ignored report from switched worker ticket=%s", ticket_id)
                continue
        logger.info("[autonomy] worker ended without report ticket=%s provider=%s", ticket_id, provider or "ptc")
    return checked
