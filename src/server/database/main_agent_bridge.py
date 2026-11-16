"""Atomic bridge between Main Agent DAG tasks and autonomy daemon Tickets.

The run ledger owns planning state. Autonomy Tickets remain the durable
execution envelope that daemon claims and lease-fenced reports already use.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection


class ExecutionContextLike(Protocol):
    workspace_id: str
    source_id: str | None
    repository: str
    agent_daemons: dict[str, str]

    def daemon_for(self, agent_key: str) -> str: ...


class MainAgentBridgeConflict(RuntimeError):
    """A run/task or its execution Ticket changed before the bridge committed."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _decode(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return deepcopy(dict(value or {}))


def _ticket_id(run_id: str, task_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"conductor-main-agent:{run_id}:{task_id}"))


async def _project_deliberation_step(
    cur: Any, run: Mapping[str, Any], task_id: str, *, status: str,
    child_ticket_id: str | None = None, daemon_id: str | None = None,
) -> None:
    deliberation_id = str((run.get("deliberation") or {}).get("deliberation_id") or "")
    if not deliberation_id:
        return
    await cur.execute(
        "UPDATE autonomy_deliberation_steps SET status=%s, "
        "child_ticket_id=COALESCE(%s,child_ticket_id), daemon_id=COALESCE(%s,daemon_id) "
        "WHERE deliberation_id=%s AND step_key=%s",
        (status, child_ticket_id, daemon_id, deliberation_id, task_id),
    )
    if cur.rowcount != 1:
        raise MainAgentBridgeConflict(
            f"Consensus step {task_id} is missing from deliberation {deliberation_id}"
        )


async def create_run_with_root_ticket(
    *,
    user_id: str,
    objective: str,
    context: Mapping[str, Any],
    execution: ExecutionContextLike,
) -> dict[str, Any]:
    """Create the run and its non-executable root Ticket in one transaction."""
    run_id = uuid4().hex
    root_ticket_id = str(uuid5(NAMESPACE_URL, f"conductor-main-agent-root:{run_id}"))
    now = _now()
    stored_context = dict(context)
    stored_context.update(
        source_id=execution.source_id,
        workspace_id=execution.workspace_id,
        agent_daemons=dict(execution.agent_daemons),
        thread_key=execution.thread_key,
    )
    request = {
        "objective": objective,
        "context": stored_context,
        "ticket_id": root_ticket_id,
        "user_id": user_id,
    }
    run = {
        "id": run_id,
        "user_id": user_id,
        "status": "accepted",
        "revision": 0,
        "request": request,
        "phase": "intake",
        "clarification": None,
        "deliberation": None,
        "plan": None,
        "division": [],
        "merged_result": None,
        "reply": None,
        "tasks": [],
        "alerts": [],
        "created_at": now,
        "updated_at": now,
    }
    mandate = {
        "kind": "main_agent_root",
        "orchestration_only": True,
        "main_agent_run_id": run_id,
        "source_id": execution.source_id or "",
        "scope": objective[:1500],
    }
    shared_thread_key = execution.thread_key or f"main-agent:{root_ticket_id}"
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO autonomy_tickets (
                        ticket_id, user_id, workspace_id, title, scope, status,
                        mandate, policy_version, origin_trigger_id,
                        origin_thread_key, worker_daemon_id
                    ) VALUES (%s, %s, %s, %s, %s, 'queued', %s, '', NULL, %s, NULL)
                    """,
                    (
                        root_ticket_id,
                        user_id,
                        execution.workspace_id,
                        objective[:120],
                        objective[:20000],
                        Json(mandate),
                        shared_thread_key,
                    ),
                )
                await cur.execute(
                    "INSERT INTO autonomy_subscriptions (ticket_id,thread_key,kind) "
                    "VALUES (%s,%s,'own') ON CONFLICT (ticket_id,thread_key) DO NOTHING",
                    (root_ticket_id, shared_thread_key),
                )
                await cur.execute(
                    """
                    INSERT INTO main_agent_runs (run_id, user_id, revision, payload)
                    VALUES (%s, %s, 0, %s)
                    """,
                    (run_id, user_id, Json(run)),
                )
    return run


async def finalize_root_ticket(*, ticket_id: str, user_id: str, status: str, summary: str) -> None:
    if status not in {"completed", "failed"}:
        raise ValueError("root Ticket final status must be completed or failed")
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT mandate,status FROM autonomy_tickets WHERE ticket_id=%s AND user_id=%s FOR UPDATE",
                (ticket_id, user_id),
            )
            ticket = await cur.fetchone()
            if ticket is None or not (ticket.get("mandate") or {}).get("orchestration_only"):
                raise MainAgentBridgeConflict("Main Agent root Ticket is missing or executable")
            if ticket["status"] in {"completed", "failed", "cancelled", "archived"}:
                return
            await cur.execute(
                "UPDATE autonomy_tickets SET status=%s,summary=%s,error=%s,ended_at=NOW(),updated_at=NOW(),revision=revision+1 "
                "WHERE ticket_id=%s AND user_id=%s",
                (status, summary[:20000], summary[:2000] if status == "failed" else "", ticket_id, user_id),
            )


async def append_shared_thread_message(
    *,
    thread_key: str,
    author: str,
    kind: str,
    body: str,
    round_index: int,
    event_key: str | None = None,
) -> None:
    """Append idempotently to the Ticket-backed shared discussion thread."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT ticket_id FROM autonomy_subscriptions WHERE thread_key=%s "
                "AND kind='own' ORDER BY ticket_id LIMIT 1",
                (thread_key,),
            )
            row = await cur.fetchone()
    if row is None:
        raise MainAgentBridgeConflict("Shared thread has no owning Ticket")
    ticket_id = str(row["ticket_id"])
    payload = json.dumps(
        {
            "thread_key": thread_key,
            "author": author,
            "kind": kind,
            "body": body,
            "round_index": round_index,
            "event_key": event_key,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    dedupe_key = event_key or f"{round_index}:{author}:{kind}:{body}"
    trigger_id = str(
        uuid5(NAMESPACE_URL, f"main-agent-thread:{thread_key}:{dedupe_key}")
    )
    from src.server.database.autonomy.tickets import add_input

    await add_input(ticket_id=ticket_id, trigger_id=trigger_id, kind="context", text=payload)


async def read_shared_thread_messages(thread_key: str) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT i.text FROM autonomy_ticket_inputs i JOIN autonomy_subscriptions s USING(ticket_id) "
                "WHERE s.thread_key=%s AND s.kind='own' AND i.kind='context' "
                "ORDER BY i.created_at, i.input_id",
                (thread_key,),
            )
            rows = await cur.fetchall()
    messages = []
    for row in rows:
        try:
            value = json.loads(str(row["text"]))
        except (ValueError, TypeError):
            continue
        if value.get("thread_key") == thread_key:
            messages.append(value)
    return messages


async def create_deliberation_worker_ticket(
    *, user_id: str, workspace_id: str, root_ticket_id: str,
    deliberation_id: str, agent_key: str, daemon_id: str, prompt: str,
) -> dict[str, Any]:
    """Create/reuse one daemon-only deliberation participant call."""
    ticket_id = str(uuid5(NAMESPACE_URL, f"main-agent-deliberation:{deliberation_id}:{agent_key}:{prompt}"))
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT origin_thread_key FROM autonomy_tickets WHERE ticket_id=%s AND user_id=%s FOR SHARE",
                    (root_ticket_id, user_id),
                )
                root = await cur.fetchone()
                if root is None:
                    raise MainAgentBridgeConflict("Deliberation root Ticket is missing")
                await cur.execute(
                    "SELECT provider FROM autonomy_daemons WHERE daemon_id=%s AND user_id=%s FOR SHARE",
                    (daemon_id, user_id),
                )
                daemon = await cur.fetchone()
                if daemon is None:
                    raise MainAgentBridgeConflict("Deliberation daemon is no longer owned by this user")
                provider = str(daemon["provider"] or "").lower()
                thread_key = str(root.get("origin_thread_key") or "")
                mandate = {
                    "kind": "main_agent_deliberation",
                    "orchestration_only": False,
                    "main_agent_run_id": "",
                    "root_ticket_id": root_ticket_id,
                    "deliberation_id": deliberation_id,
                    "agent_key": agent_key,
                    "scope": prompt[:1500],
                }
                await cur.execute(
                    "INSERT INTO autonomy_tickets (ticket_id,user_id,workspace_id,title,scope,status,mandate,policy_version,origin_trigger_id,origin_thread_key,worker_provider,worker_daemon_id) "
                    "VALUES (%s,%s,%s,%s,%s,'queued',%s,'',NULL,%s,%s,%s) ON CONFLICT(ticket_id) DO NOTHING",
                    (ticket_id,user_id,workspace_id,f"Deliberation {agent_key}"[:120],prompt[:20000],Json(mandate),thread_key,provider,daemon_id),
                )
                await cur.execute(
                    "SELECT ticket_id,user_id,status,mandate,worker_daemon_id,summary,error FROM autonomy_tickets WHERE ticket_id=%s",
                    (ticket_id,),
                )
                ticket = await cur.fetchone()
                if ticket is None or str(ticket["user_id"]) != user_id or (ticket.get("mandate") or {}).get("deliberation_id") != deliberation_id:
                    raise MainAgentBridgeConflict("Deliberation worker Ticket link is inconsistent")
                if thread_key:
                    await cur.execute(
                        "INSERT INTO autonomy_subscriptions(ticket_id,thread_key,kind) VALUES (%s,%s,'context') ON CONFLICT(ticket_id,thread_key) DO NOTHING",
                        (ticket_id,thread_key),
                    )
                return dict(ticket)


async def create_ready_child_ticket(
    *,
    run_id: str,
    user_id: str,
    task_id: str,
    execution: ExecutionContextLike,
) -> dict[str, Any] | None:
    """Idempotently create a ready DAG task's daemon-claimable Ticket.

    The insertion and ``child_ticket_id`` CAS share one Postgres transaction,
    so a retry after a worker crash returns the same Ticket.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT revision, payload FROM main_agent_runs "
                    "WHERE run_id=%s AND user_id=%s FOR UPDATE",
                    (run_id, user_id),
                )
                row = await cur.fetchone()
                if row is None:
                    raise MainAgentBridgeConflict("Main Agent run not found")
                run = _decode(row["payload"])
                if run.get("phase") not in {"execute", "track"} or run.get("status") not in {
                    "queued",
                    "running",
                }:
                    return None
                tasks = list(run.get("tasks") or [])
                task = next((item for item in tasks if str(item.get("id")) == task_id), None)
                if task is None:
                    raise MainAgentBridgeConflict("DAG task not found")
                if task.get("child_ticket_id"):
                    await cur.execute(
                        "SELECT ticket_id,user_id,status,summary,mandate,worker_daemon_id "
                        "FROM autonomy_tickets WHERE ticket_id=%s",
                        (str(task["child_ticket_id"]),),
                    )
                    existing = await cur.fetchone()
                    if (
                        existing is None
                        or str(existing["user_id"]) != user_id
                        or str((existing.get("mandate") or {}).get("main_agent_run_id")) != run_id
                        or str((existing.get("mandate") or {}).get("task_id")) != task_id
                    ):
                        raise MainAgentBridgeConflict("Stored child Ticket link is inconsistent")
                    existing_step_status = {
                        "queued": "ready",
                        "running": "running",
                        "waiting": "ready",
                        "ready": "completed",
                        "completed": "completed",
                        "failed": "failed",
                        "blocked": "failed",
                        "cancelled": "failed",
                    }.get(str(existing.get("status") or ""))
                    if existing_step_status is None:
                        raise MainAgentBridgeConflict("Stored child Ticket has an unsupported state")
                    await _project_deliberation_step(
                        cur,
                        run,
                        task_id,
                        status=existing_step_status,
                        child_ticket_id=str(existing["ticket_id"]),
                        daemon_id=str(existing.get("worker_daemon_id") or "") or None,
                    )
                    return dict(existing)
                states = {str(item.get("id")): str(item.get("status")) for item in tasks}
                if task.get("status") != "queued" or any(
                    states.get(str(dep)) != "succeeded"
                    for dep in task.get("depends_on") or []
                ):
                    return None

                root_ticket_id = str((run.get("request") or {}).get("ticket_id") or "")
                await cur.execute(
                    "SELECT mandate,origin_thread_key FROM autonomy_tickets "
                    "WHERE ticket_id=%s AND user_id=%s FOR SHARE",
                    (root_ticket_id, user_id),
                )
                root = await cur.fetchone()
                if root is None or not (root.get("mandate") or {}).get("orchestration_only"):
                    raise MainAgentBridgeConflict("Orchestration root Ticket is missing")

                assigned_agent = str(task.get("deliberation_agent_id") or "default").lower()
                daemon_id = execution.daemon_for(assigned_agent)
                await cur.execute(
                    "SELECT provider FROM autonomy_daemons WHERE daemon_id=%s AND user_id=%s FOR SHARE",
                    (daemon_id, user_id),
                )
                daemon = await cur.fetchone()
                if daemon is None:
                    raise MainAgentBridgeConflict("Assigned daemon is no longer owned by this user")
                provider = str(daemon["provider"] or "").lower()
                if assigned_agent != "default" and provider != assigned_agent:
                    raise MainAgentBridgeConflict("Assigned daemon provider changed")

                source_id = execution.source_id
                if source_id:
                    await cur.execute(
                        "SELECT enabled,config FROM autonomy_sources "
                        "WHERE source_id=%s AND user_id=%s FOR SHARE",
                        (source_id, user_id),
                    )
                    source = await cur.fetchone()
                    if source is None or not source["enabled"]:
                        raise MainAgentBridgeConflict("Selected Source is no longer enabled")
                    source_config = dict(source.get("config") or {})
                    if str(source_config.get("workspace_id") or "") != execution.workspace_id:
                        raise MainAgentBridgeConflict("Selected Source workspace changed")
                else:
                    source_config = {}

                objective = str((run.get("request") or {}).get("objective") or "").strip()
                description = str(task.get("description") or "").strip()
                if not objective or not description:
                    raise MainAgentBridgeConflict("Confirmed objective and planned task are required")
                scope = f"Confirmed objective:\n{objective}\n\nApproved DAG task:\n{description}"[:20000]
                child_id = _ticket_id(run_id, task_id)
                mandate = {
                    "kind": "main_agent_child",
                    "orchestration_child": True,
                    "main_agent_run_id": run_id,
                    "root_ticket_id": root_ticket_id,
                    "task_id": task_id,
                    "source_id": source_id or "",
                    "scope": scope[:1500],
                }
                await cur.execute(
                    """
                    INSERT INTO autonomy_tickets (
                        ticket_id,user_id,workspace_id,title,scope,status,mandate,
                        policy_version,origin_trigger_id,origin_thread_key,
                        worker_provider,worker_model,worker_daemon_id
                    ) VALUES (%s,%s,%s,%s,%s,'queued',%s,'',NULL,%s,%s,%s,%s)
                    ON CONFLICT (ticket_id) DO NOTHING
                    """,
                    (
                        child_id,
                        user_id,
                        execution.workspace_id,
                        (task.get("description") or task_id)[:120],
                        scope,
                        Json(mandate),
                        str(root.get("origin_thread_key") or ""),
                        provider,
                        str(source_config.get("worker_model") or ""),
                        daemon_id,
                    ),
                )
                await cur.execute(
                    "SELECT ticket_id,user_id,status,summary,mandate,worker_daemon_id "
                    "FROM autonomy_tickets WHERE ticket_id=%s",
                    (child_id,),
                )
                child = await cur.fetchone()
                if (
                    child is None
                    or str(child["user_id"]) != user_id
                    or str((child.get("mandate") or {}).get("main_agent_run_id")) != run_id
                    or str((child.get("mandate") or {}).get("task_id")) != task_id
                ):
                    raise MainAgentBridgeConflict("Deterministic child Ticket ID is already in use")

                await _project_deliberation_step(
                    cur, run, task_id, status="ready", child_ticket_id=child_id, daemon_id=daemon_id
                )

                shared_thread_key = str(root.get("origin_thread_key") or "")
                if shared_thread_key:
                    await cur.execute(
                        "INSERT INTO autonomy_subscriptions (ticket_id,thread_key,kind) "
                        "VALUES (%s,%s,'context') ON CONFLICT (ticket_id,thread_key) DO NOTHING",
                        (child_id, shared_thread_key),
                    )
                    # Context is bounded and copied from the persisted root
                    # discussion so a child can read it through its normal
                    # ticket context without granting it new authority.
                    await cur.execute(
                        "SELECT trigger_id,text FROM autonomy_ticket_inputs "
                        "WHERE ticket_id=%s AND kind='context' "
                        "ORDER BY created_at DESC LIMIT 12",
                        (root_ticket_id,),
                    )
                    for source_input in reversed(await cur.fetchall()):
                        trigger_id = str(source_input.get("trigger_id") or "")
                        try:
                            discussion = json.loads(str(source_input.get("text") or ""))
                        except (TypeError, ValueError):
                            discussion = None
                        if not isinstance(discussion, dict) or discussion.get("thread_key") != shared_thread_key:
                            continue
                        await cur.execute(
                            "INSERT INTO autonomy_ticket_inputs (ticket_id,trigger_id,kind,text) "
                            "VALUES (%s,%s,'context',%s) ON CONFLICT (ticket_id,trigger_id) "
                            "WHERE trigger_id IS NOT NULL DO NOTHING",
                            (
                                child_id,
                                str(uuid5(NAMESPACE_URL, f"main-agent-copy:{root_ticket_id}:{trigger_id}")),
                                str(source_input.get("text") or "")[:4000],
                            ),
                        )

                task["child_ticket_id"] = child_id
                task["daemon_id"] = daemon_id
                run["tasks"] = tasks
                run_revision = int(row["revision"]) + 1
                run["revision"] = run_revision
                run["updated_at"] = _now()
                await cur.execute(
                    "UPDATE main_agent_runs SET revision=%s,payload=%s,updated_at=NOW() "
                    "WHERE run_id=%s AND user_id=%s AND revision=%s",
                    (run_revision, Json(run), run_id, user_id, row["revision"]),
                )
                if cur.rowcount != 1:
                    raise MainAgentBridgeConflict("Main Agent run changed during child Ticket creation")
                return dict(child)


async def sync_child_ticket_report(
    *, run_id: str, user_id: str, task_id: str
) -> dict[str, Any] | None:
    """Project a lease-fenced autonomy Ticket report into its parent task once."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT revision,payload FROM main_agent_runs "
                    "WHERE run_id=%s AND user_id=%s FOR UPDATE",
                    (run_id, user_id),
                )
                row = await cur.fetchone()
                if row is None:
                    raise MainAgentBridgeConflict("Main Agent run not found")
                run = _decode(row["payload"])
                tasks = list(run.get("tasks") or [])
                task = next((item for item in tasks if str(item.get("id")) == task_id), None)
                if task is None or not task.get("child_ticket_id"):
                    return None
                await cur.execute(
                    """SELECT t.status,t.summary,t.error,t.mandate,t.worker_daemon_id,
                        t.attempts,t.worker_lease_id,t.worker_lease_expires_at,
                        (SELECT r.created_at FROM autonomy_ticket_reports r
                         WHERE r.ticket_id=t.ticket_id AND r.state='progress'
                         ORDER BY r.created_at DESC LIMIT 1) AS progress_at
                    FROM autonomy_tickets t """
                    "WHERE ticket_id=%s AND user_id=%s FOR SHARE",
                    (str(task["child_ticket_id"]), user_id),
                )
                ticket = await cur.fetchone()
                if ticket is None:
                    raise MainAgentBridgeConflict("Linked child Ticket is missing")
                mandate = dict(ticket.get("mandate") or {})
                if (
                    mandate.get("main_agent_run_id") != run_id
                    or mandate.get("task_id") != task_id
                    or not mandate.get("orchestration_child")
                ):
                    raise MainAgentBridgeConflict("Child Ticket ownership does not match the parent task")
                state = str(ticket["status"])
                projected = {
                    "queued": "queued",
                    "running": "running",
                    "waiting": "queued",
                    "ready": "succeeded",
                    "completed": "succeeded",
                    "failed": "failed",
                    "blocked": "failed",
                    "cancelled": "failed",
                }.get(state)
                if projected is None:
                    raise MainAgentBridgeConflict(f"Unsupported child Ticket state: {state}")
                lease_expiry = ticket.get("worker_lease_expires_at")
                lease_seconds = 300
                try:
                    from src.server.database.autonomy.daemons import LEASE_SECONDS

                    lease_seconds = int(LEASE_SECONDS)
                except (ImportError, TypeError, ValueError):
                    pass
                heartbeat_at = (
                    lease_expiry.timestamp() - lease_seconds
                    if lease_expiry is not None and hasattr(lease_expiry, "timestamp")
                    else None
                )
                progress_at = ticket.get("progress_at")
                projected_fields = {
                    "status": projected,
                    "daemon_id": str(ticket.get("worker_daemon_id") or "") or None,
                    "attempt": int(ticket.get("attempts") or 0),
                    "worker_lease_id": str(ticket.get("worker_lease_id") or "") or None,
                    "lease_until": lease_expiry.isoformat() if hasattr(lease_expiry, "isoformat") else None,
                    "heartbeat_at": datetime.fromtimestamp(heartbeat_at, UTC).isoformat() if heartbeat_at is not None else None,
                    "progress_at": progress_at.isoformat() if hasattr(progress_at, "isoformat") else None,
                    "current_step": str(ticket.get("summary") or "")[:1000] or None,
                }
                step_status = {
                    "queued": "ready",
                    "running": "running",
                    "waiting": "ready",
                    "ready": "completed",
                    "completed": "completed",
                    "failed": "failed",
                    "blocked": "failed",
                    "cancelled": "failed",
                }[state]
                await _project_deliberation_step(
                    cur,
                    run,
                    task_id,
                    status=step_status,
                    child_ticket_id=str(task["child_ticket_id"]),
                    daemon_id=str(ticket.get("worker_daemon_id") or "") or None,
                )
                if all(task.get(key) == value for key, value in projected_fields.items()) and (
                    projected != "succeeded" or (task.get("result") or {}).get("summary") == str(ticket.get("summary") or "")
                ):
                    return deepcopy(task)
                task.update(projected_fields)
                if projected == "succeeded":
                    task["result"] = {
                        "ticket_id": str(task["child_ticket_id"]),
                        "summary": str(ticket.get("summary") or ""),
                    }
                    task["error"] = None
                elif projected == "failed":
                    task["error"] = {
                        "ticket_status": state,
                        "message": str(ticket.get("error") or ticket.get("summary") or state),
                    }
                task["revision"] = int(task.get("revision") or 0) + 1
                run["tasks"] = tasks
                run_revision = int(row["revision"]) + 1
                run["revision"] = run_revision
                run["updated_at"] = _now()
                await cur.execute(
                    "UPDATE main_agent_runs SET revision=%s,payload=%s,updated_at=NOW() "
                    "WHERE run_id=%s AND user_id=%s AND revision=%s",
                    (run_revision, Json(run), run_id, user_id, row["revision"]),
                )
                if cur.rowcount != 1:
                    raise MainAgentBridgeConflict("Main Agent run changed during report projection")
                return deepcopy(task)
