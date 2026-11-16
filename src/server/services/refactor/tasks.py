"""Dual-write entry point for the persisted agent task tree.

The TodoWrite middleware's SSE events remain the live source until Phase 3 flips
the reader; this service persists the same tree when ``task_tree`` is on so the
timeline/audit and the differential harness have a durable row to compare. The
tree is advisory: a failure here must never fail a turn, which is why every
function returns a row/None and the caller is expected to treat recording as
best-effort (documented at the call site when Phase 2 wires it).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.server.contracts.refactor import TASK_SOURCES, TASK_STATUSES, ContractError
from src.server.database.refactor import tasks as db_tasks
from src.server.services.refactor_flags import refactor_flag_enabled

MAX_TITLE_LENGTH = 500
MAX_SUBAGENT_TASK_REF_LENGTH = 256


def _validate_title(title: str) -> str:
    text = (title or "").strip()
    if not text:
        raise ContractError("task title must not be empty")
    return text[:MAX_TITLE_LENGTH]


async def record_task(
    *,
    user_id: str,
    thread_id: str,
    title: str,
    status: str = "pending",
    source: str = "todo",
    parent_node_id: str | None = None,
    subagent_task_ref: str | None = None,
) -> dict[str, Any] | None:
    """Persist one task node, or ``None`` when the ``task_tree`` flag is off."""
    if not refactor_flag_enabled("task_tree"):
        return None
    if status not in TASK_STATUSES:
        raise ContractError(f"unknown task status {status!r}")
    if source not in TASK_SOURCES:
        raise ContractError(f"unknown task source {source!r}")
    ref = (subagent_task_ref or "").strip() or None
    if ref is not None and len(ref) > MAX_SUBAGENT_TASK_REF_LENGTH:
        raise ContractError(f"subagent task ref exceeds {MAX_SUBAGENT_TASK_REF_LENGTH} chars")
    return await db_tasks.insert_task(
        user_id=user_id,
        thread_id=thread_id,
        title=_validate_title(title),
        status=status,
        source=source,
        parent_node_id=parent_node_id,
        subagent_task_ref=ref,
    )


async def set_task_status(
    *,
    task_node_id: str,
    status: str,
) -> dict[str, Any] | None:
    if not refactor_flag_enabled("task_tree"):
        return None
    if status not in TASK_STATUSES:
        raise ContractError(f"unknown task status {status!r}")
    return await db_tasks.update_task_status(task_node_id=task_node_id, status=status)


async def list_task_tree(*, thread_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """The thread's task rows in creation order; empty when the flag is off."""
    if not refactor_flag_enabled("task_tree"):
        return []
    return await db_tasks.list_tasks(thread_id=thread_id, limit=limit)


#: A TodoWrite payload is a plan, not a backlog: more items than this is a
#: prompt bug, and the sync is one round trip per snapshot either way.
MAX_TODO_ITEMS = 100


async def record_todo_snapshot(
    *,
    thread_id: str,
    todos: object,
) -> dict[str, Any] | None:
    """Reconcile the main agent's TodoWrite snapshot into the task tree.

    Flag off → ``None``. Items are identified by their ``content`` line (the
    title the model rewrites in place); an item missing from the new snapshot is
    cancelled by the SQL sync. Unknown statuses fail before touching the
    database — the middleware normalizes to the three known states, so anything
    else is a contract bug rather than data to coerce.
    """
    if not refactor_flag_enabled("task_tree"):
        return None
    if not isinstance(todos, list):
        raise ContractError("todo snapshot must be a list")
    if len(todos) > MAX_TODO_ITEMS:
        raise ContractError(f"todo snapshot exceeds {MAX_TODO_ITEMS} items")
    normalized: list[dict[str, str]] = []
    for item in todos:
        if not isinstance(item, Mapping):
            continue
        title = str(item.get("content") or "").strip()
        if not title:
            continue
        status = str(item.get("status") or "").strip().lower()
        if status not in TASK_STATUSES:
            raise ContractError(f"unknown task status {status!r}")
        normalized.append({"title": title[:MAX_TITLE_LENGTH], "status": status})
    rows = await db_tasks.sync_todo_snapshot(thread_id=thread_id, todos=normalized)
    if rows is None:
        return None
    return {
        "written": len(normalized),
        "rows": len(rows),
        "open": sum(1 for row in rows if row["status"] in ("pending", "in_progress")),
        "completed": sum(1 for row in rows if row["status"] == "completed"),
    }
