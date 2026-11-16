"""Reconcile linked autonomy Tickets into their durable Main Agent parent."""

from __future__ import annotations

import logging
from typing import Any

from src.server.database.main_agent_bridge import (
    create_ready_child_ticket,
    sync_child_ticket_report,
)
from src.server.services.main_agent.production import resolve_execution_context

logger = logging.getLogger(__name__)


async def reconcile_child_tickets(orchestrator: Any, run: dict[str, Any], user_id: str) -> dict[str, Any]:
    request = dict(run.get("request") or {})
    context = dict(request.get("context") or {})
    execution = await resolve_execution_context(user_id, context)
    for task in await orchestrator.ready_tasks(str(run["id"]), user_id):
        await create_ready_child_ticket(
            run_id=str(run["id"]),
            user_id=user_id,
            task_id=str(task["id"]),
            execution=execution,
        )
    # Synchronize all linked children, including lease/progress-only changes.
    refreshed = await orchestrator.get(str(run["id"]), user_id)
    for task in refreshed.get("tasks", []):
        if task.get("child_ticket_id"):
            await sync_child_ticket_report(
                run_id=str(run["id"]), user_id=user_id, task_id=str(task["id"])
            )
    # create/sync use their own row locks; never CAS against the pre-bridge revision.
    return await orchestrator.get(str(run["id"]), user_id)


async def sync_report_and_reconcile(metadata: dict[str, Any]) -> None:
    """Post-commit report hook; failures are recovered by periodic run reconciliation."""
    from src.server.app.setup import app

    orchestrator = getattr(app.state, "main_agent_orchestrator", None)
    if orchestrator is None:
        return
    run_id = str(metadata.get("run_id") or "")
    user_id = str(metadata.get("user_id") or "")
    task_id = str(metadata.get("task_id") or "")
    if not (run_id and user_id and task_id):
        return
    try:
        # tick's child reconciliation owns the report sync under the run guard.
        await orchestrator.tick(run_id, user_id)
    except Exception:
        logger.warning("main_agent.child_report_reconcile_failed", extra={"run_id": run_id, "task_id": task_id}, exc_info=True)
