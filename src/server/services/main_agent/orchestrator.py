"""Durable planner/coordinator state machine for main-agent requests."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .adapters import PassthroughClarificationGate, validate_deliberation_result
from .interfaces import (
    ClarificationGate,
    Deliberation,
    Divider,
    MainAgentRepository,
    Planner,
    ReplySynthesizer,
    ResultMerger,
)
from .models import (
    ConcurrentUpdate,
    CoordinatorBusy,
    InvalidPlan,
    MainAgentThresholds,
    RunNotFound,
    TaskOwnershipError,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    )


def validate_dag(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize task records and reject ambiguous or impossible dependencies."""
    by_id: dict[str, dict[str, Any]] = {}
    normalized: list[dict[str, Any]] = []
    for raw in tasks:
        task = dict(raw)
        task_id = str(task.get("id") or "").strip()
        if not task_id or task_id in by_id:
            raise InvalidPlan("plan task ids must be non-empty and unique")
        deps_raw = task.get("depends_on", [])
        if not isinstance(deps_raw, list):
            raise InvalidPlan(f"task {task_id} dependencies must be a list")
        deps = [str(dep) for dep in deps_raw]
        if len(deps) != len(set(deps)):
            raise InvalidPlan(f"task {task_id} has duplicate dependencies")
        if task_id in deps:
            raise InvalidPlan(f"task {task_id} cannot depend on itself")
        task.update(
            id=task_id,
            description=str(task.get("description") or "").strip(),
            depends_on=deps,
            status="queued",
            revision=0,
            attempt=0,
            attempt_history=[],
            retryable=False,
            # Ownership is worker-acquired, never model-assigned.
            assigned_agent_id=None,
            daemon_id=None,
            claim_token=None,
            current_step=None,
            lease_until=None,
            heartbeat_at=None,
            progress_at=None,
            result=None,
            error=None,
            deliberation_agent_id=(
                str(task["deliberation_agent_id"])
                if task.get("deliberation_agent_id") is not None
                else None
            ),
        )
        by_id[task_id] = task
        normalized.append(task)
    for task in normalized:
        missing = sorted(set(task["depends_on"]) - by_id.keys())
        if missing:
            raise InvalidPlan(
                f"task {task['id']} depends on missing task(s): {', '.join(missing)}"
            )
    indegree = {task_id: 0 for task_id in by_id}
    children: dict[str, list[str]] = {task_id: [] for task_id in by_id}
    for task in normalized:
        for dep in task["depends_on"]:
            indegree[task["id"]] += 1
            children[dep].append(task["id"])
    ready = [task_id for task_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        task_id = ready.pop()
        visited += 1
        for child in children[task_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if visited != len(by_id):
        raise InvalidPlan("plan dependencies contain a cycle")
    return normalized


def run_needs_reconciliation(run: dict[str, Any]) -> bool:
    """Share scheduler eligibility rules so idle worker runs cannot starve settled runs."""
    phase = str(run.get("phase") or "")
    status = str(run.get("status") or "")
    if phase == "clarify":
        return status == "accepted"
    if phase in {
        "intake",
        "divide",
        "deliberate",
        "plan_dag",
        "self_claim",
        "merge_results",
        "reply",
    }:
        return status in {"accepted", "queued", "running"}
    if phase not in {"execute", "track"} or status not in {"queued", "running"}:
        return False

    tasks = list(run.get("tasks", []))
    states = {str(task.get("id")): str(task.get("status")) for task in tasks}
    if any(state == "running" for state in states.values()):
        return any(task.get("child_ticket_id") for task in tasks)
    if any(state in {"failed", "cancelled"} for state in states.values()):
        return True
    if tasks and all(state == "succeeded" for state in states.values()):
        return True
    return any(
        task.get("status") == "queued"
        and (
            bool(task.get("child_ticket_id"))
            or
            any(states.get(str(dep)) in {"failed", "cancelled"} for dep in task.get("depends_on", []))
            or (
                not task.get("child_ticket_id")
                and set(task.get("depends_on") or [])
                <= {task_id for task_id, state in states.items() if state == "succeeded"}
            )
        )
        for task in tasks
    )


class MainAgentOrchestrator:
    """Coordinate durable plans while independent workers claim and run tasks."""

    def __init__(
        self,
        *,
        repository: MainAgentRepository,
        divider: Divider,
        deliberation: Deliberation,
        planner: Planner,
        merger: ResultMerger,
        reply_synthesizer: ReplySynthesizer,
        clarification_gate: ClarificationGate | None = None,
        run_creator: Any | None = None,
        execution_context_resolver: Any | None = None,
        child_ticket_reconciler: Any | None = None,
        run_finalizer: Any | None = None,
        thresholds: MainAgentThresholds | None = None,
        max_phase_steps: int = 16,
    ) -> None:
        self.repository = repository
        self.divider = divider
        self.deliberation = deliberation
        self.planner = planner
        self.merger = merger
        self.reply_synthesizer = reply_synthesizer
        self.clarification_gate = clarification_gate or PassthroughClarificationGate()
        self.run_creator = run_creator
        self.execution_context_resolver = execution_context_resolver
        self.child_ticket_reconciler = child_ticket_reconciler
        self.run_finalizer = run_finalizer
        self.thresholds = thresholds or MainAgentThresholds()
        self.max_phase_steps = max_phase_steps

    async def submit(self, user_id: str, request: dict[str, Any]) -> dict[str, Any]:
        safe_request = dict(request)
        safe_request["user_id"] = user_id
        now = _iso()
        if self.execution_context_resolver is not None and self.run_creator is not None:
            context = dict(safe_request.get("context") or {})
            execution = await self.execution_context_resolver(user_id, context)
            record = await self.run_creator(
                user_id=user_id,
                objective=str(safe_request.get("objective") or ""),
                context=context,
                execution=execution,
            )
            return await self.advance(str(record["id"]), user_id)
        record = await self.repository.create_run(
            user_id,
            {
                "id": uuid4().hex,
                "user_id": user_id,
                "status": "accepted",
                "revision": 0,
                "request": safe_request,
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
            },
        )
        return await self.advance(str(record["id"]), user_id)

    async def get(self, run_id: str, user_id: str) -> dict[str, Any]:
        run = await self.repository.get_run(run_id, user_id)
        if run is None:
            raise RunNotFound(run_id)
        return run

    async def _patch(self, run: dict[str, Any], **patch: Any) -> dict[str, Any]:
        updated = await self.repository.transition_run(
            str(run["id"]), int(run.get("revision", 0)), {**patch, "updated_at": _iso()}
        )
        if updated is None:
            raise ConcurrentUpdate(
                f"run {run['id']} changed while coordinator was working"
            )
        return updated

    async def answer_clarification(
        self, run_id: str, user_id: str, answer: dict[str, Any]
    ) -> dict[str, Any]:
        async with self.repository.run_guard(run_id) as acquired:
            if not acquired:
                raise CoordinatorBusy("Run is already being coordinated; retry this request.")
            run = await self.get(run_id, user_id)
            if (
                run.get("phase") != "clarify"
                or run.get("status") != "clarification_required"
            ):
                raise InvalidPlan("run is not waiting for clarification")
            clarification = dict(run.get("clarification") or {})
            answer_text = str(answer.get("answer") or answer.get("text") or "").strip()
            if not answer_text:
                raise InvalidPlan(
                    "clarification answer must include edited or confirmed text"
                )
            request_context = run.get("request") or {}
            ticket = {}
            if request_context.get("ticket_id"):
                confirm = getattr(self.clarification_gate, "confirm", None)
                if confirm is None:
                    raise InvalidPlan("linked clarification gate cannot confirm the Ticket")
                ticket = await confirm(request_context, answer_text)
            clarification["answer"] = {"answer": answer_text}
            clarification["required"] = False
            request = dict(run.get("request") or {})
            request["objective"] = answer_text
            request["clarification_answer"] = {"answer": answer_text}
            request["confirmed_ticket_status"] = ticket.get("status")
            run = await self._patch(
                run, clarification=clarification, request=request, status="accepted"
            )
            return await self._advance(run_id, user_id)

    async def advance(self, run_id: str, user_id: str) -> dict[str, Any]:
        async with self.repository.run_guard(run_id) as acquired:
            if not acquired:
                return await self.get(run_id, user_id)
            return await self._advance(run_id, user_id)

    async def _advance(self, run_id: str, user_id: str) -> dict[str, Any]:
        run = await self.get(run_id, user_id)
        if run.get("status") in {"completed", "failed"}:
            return run
        if run.get("status") == "clarification_required":
            return run
        for _ in range(self.max_phase_steps):
            phase = run.get("phase", "intake")
            request = dict(run.get("request") or {})
            if phase == "intake":
                run = await self._patch(run, phase="clarify", status="accepted")
            elif phase == "clarify":
                clarification = dict(run.get("clarification") or {})
                if clarification.get("answer") is None:
                    result = await self.clarification_gate.check(request)
                    required = bool(result.get("required") or result.get("questions"))
                    clarification = {**result, "required": required}
                else:
                    required = False
                if required:
                    return await self._patch(
                        run,
                        clarification=clarification,
                        status="clarification_required",
                    )
                run = await self._patch(
                    run, clarification=clarification, phase="divide", status="accepted"
                )
            elif phase == "divide":
                tasks = await self.divider.divide(request)
                if not tasks:
                    raise InvalidPlan("divider returned no tasks")
                tasks = validate_dag(tasks)
                run = await self._patch(
                    run, division=tasks, tasks=tasks, phase="deliberate"
                )
            elif phase == "deliberate":
                result = await self.deliberation.deliberate(
                    request, run.get("tasks", [])
                )
                result = validate_deliberation_result(result, run.get("tasks", []))
                run = await self._patch(run, deliberation=result, phase="plan_dag")
            elif phase == "plan_dag":
                result = await self.planner.plan(
                    request, run.get("tasks", []), run.get("deliberation") or {}
                )
                tasks = validate_dag(list(result.get("tasks") or []))
                if not tasks:
                    raise InvalidPlan("planner returned no tasks")
                run = await self._patch(
                    run, plan=result, tasks=tasks, phase="self_claim", status="queued"
                )
            elif phase == "self_claim":
                # Workers poll and claim each ready node under their own identity.
                run = await self._patch(run, phase="execute", status="queued")
            elif phase == "execute":
                return run
            elif phase == "track":
                statuses = {str(task.get("status")) for task in run.get("tasks", [])}
                if "failed" in statuses:
                    if "running" in statuses:
                        return run
                    await self._finalize(run, "failed", {"error": "one or more daemon Tickets failed"})
                    return await self._patch(run, status="failed", phase="track")
                if "cancelled" in statuses:
                    if "running" in statuses:
                        return run
                    await self._finalize(run, "failed", {"error": "one or more daemon Tickets were cancelled"})
                    return await self._patch(run, status="failed", phase="track")
                if statuses and statuses <= {"succeeded"}:
                    run = await self._patch(
                        run, phase="merge_results", status="running"
                    )
                else:
                    return run
            elif phase == "merge_results":
                tasks = list(run.get("tasks", []))
                if not tasks or any(
                    task.get("status") != "succeeded" for task in tasks
                ):
                    return run
                merged = await self.merger.merge(request, tasks)
                run = await self._patch(run, merged_result=merged, phase="reply")
            elif phase == "reply":
                reply = dict(run.get("reply") or {})
                if not reply:
                    reply = await self.reply_synthesizer.synthesize(
                        request, run.get("merged_result") or {}
                    )
                    # Store the exact final response by revision CAS before any
                    # external delivery. Retries reuse this durable value.
                    run = await self._patch(run, reply=reply, status="running")
                await self._finalize(run, "completed", reply)
                return await self._patch(
                    run, phase="reply", status="completed"
                )
            else:
                raise InvalidPlan(f"unknown main-agent phase: {phase}")
        raise InvalidPlan("phase advancement exceeded its bounded step count")

    async def tick(self, run_id: str, user_id: str) -> dict[str, Any]:
        async with self.repository.run_guard(run_id) as acquired:
            if not acquired:
                raise CoordinatorBusy("Run is already being coordinated; retry this request.")
            run = await self._advance(run_id, user_id)
            if self.child_ticket_reconciler is not None and run.get("phase") in {
                "execute",
                "track",
            }:
                run = await self.child_ticket_reconciler(run, user_id)
            if run.get("phase") == "execute":
                run = await self._patch(run, phase="track")
            if run.get("phase") == "track":
                return await self._advance(run_id, user_id)
            return run

    async def _finalize(self, run: dict[str, Any], status: str, result: dict[str, Any]) -> None:
        if self.run_finalizer is not None:
            await self.run_finalizer(run, status, result)

    async def ready_tasks(self, run_id: str, user_id: str) -> list[dict[str, Any]]:
        run = await self.get(run_id, user_id)
        if run.get("phase") not in {"execute", "track"} or run.get("status") not in {
            "queued",
            "running",
        }:
            return []
        completed = {
            str(t["id"]) for t in run.get("tasks", []) if t.get("status") == "succeeded"
        }
        return [
            {**dict(task), "run_id": run_id}
            for task in run.get("tasks", [])
            if task.get("status") == "queued"
            and set(task.get("depends_on") or []) <= completed
        ]

    async def claim_task(
        self,
        run_id: str,
        user_id: str,
        task_id: str,
        agent_id: str,
        *,
        daemon_id: str | None = None,
    ) -> dict[str, Any]:
        run = await self.get(run_id, user_id)
        self._require_legacy_task_path(run)
        if run.get("phase") not in {"execute", "track"} or run.get("status") not in {
            "queued",
            "running",
        }:
            raise TaskOwnershipError("run is not accepting task claims")
        task = self._task(run, task_id)
        claimed = await self.repository.claim_task(
            run_id,
            task_id,
            agent_id,
            int(task.get("revision", 0)),
            self.thresholds.lease_seconds,
            daemon_id=daemon_id,
            user_id=user_id,
        )
        if claimed is None:
            raise TaskOwnershipError(task_id)
        return claimed

    async def heartbeat_task(
        self,
        run_id: str,
        user_id: str,
        task_id: str,
        agent_id: str,
        *,
        current_step: str | None = None,
        claim_token: str,
    ) -> dict[str, Any]:
        run = await self.get(run_id, user_id)
        self._require_legacy_task_path(run)
        task = self._task(run, task_id)
        updated = await self.repository.heartbeat_task(
            run_id,
            task_id,
            agent_id,
            int(task.get("revision", 0)),
            current_step,
            lease_seconds=self.thresholds.lease_seconds,
            user_id=user_id,
            claim_token=claim_token,
        )
        if updated is None:
            raise TaskOwnershipError(task_id)
        return updated

    async def complete_task(
        self,
        run_id: str,
        user_id: str,
        task_id: str,
        agent_id: str,
        result: dict[str, Any],
        *,
        claim_token: str,
    ) -> dict[str, Any]:
        run = await self.get(run_id, user_id)
        self._require_legacy_task_path(run)
        task = self._task(run, task_id)
        updated = await self.repository.complete_task(
            run_id,
            task_id,
            agent_id,
            int(task.get("revision", 0)),
            result,
            user_id=user_id,
            claim_token=claim_token,
        )
        if updated is None:
            raise TaskOwnershipError(task_id)
        return updated

    async def fail_task(
        self,
        run_id: str,
        user_id: str,
        task_id: str,
        agent_id: str,
        error: dict[str, Any],
        *,
        retryable: bool = False,
        claim_token: str,
    ) -> dict[str, Any]:
        run = await self.get(run_id, user_id)
        self._require_legacy_task_path(run)
        task = self._task(run, task_id)
        updated = await self.repository.fail_task(
            run_id,
            task_id,
            agent_id,
            int(task.get("revision", 0)),
            error,
            retryable,
            user_id=user_id,
            claim_token=claim_token,
        )
        if updated is None:
            raise TaskOwnershipError(task_id)
        return updated

    async def retry_task(
        self, run_id: str, user_id: str, task_id: str
    ) -> dict[str, Any]:
        run = await self.get(run_id, user_id)
        self._require_legacy_task_path(run)
        if run.get("status") == "completed":
            raise TaskOwnershipError("terminal runs cannot be retried")
        task = self._task(run, task_id)
        lease_until = _parse_time(task.get("lease_until"))
        expired_lease = (
            task.get("status") == "running"
            and lease_until is not None
            and lease_until <= _now()
        )
        if not (
            (task.get("status") == "failed" and task.get("retryable", False))
            or expired_lease
        ):
            raise TaskOwnershipError("task is not eligible for retry")
        retried = await self.repository.retry_task(
            run_id, task_id, int(task.get("revision", 0)), user_id=user_id
        )
        if retried is None:
            raise ConcurrentUpdate(f"task {task_id} changed before retry")
        return retried

    @staticmethod
    def _task(run: dict[str, Any], task_id: str) -> dict[str, Any]:
        for task in run.get("tasks", []):
            if task.get("id") == task_id:
                return dict(task)
        raise RunNotFound(f"task {task_id}")

    @staticmethod
    def _require_legacy_task_path(run: dict[str, Any]) -> None:
        if (run.get("request") or {}).get("ticket_id"):
            raise TaskOwnershipError(
                "This run is linked to daemon Tickets; use daemon claim and report endpoints."
            )

    async def watchdog_sweep(self, user_id: str | None = None) -> list[dict[str, Any]]:
        now = _now()
        runs = await self.repository.list_runs(
            user_id, ["queued", "running"], oldest_first=user_id is None
        )
        alerts: list[dict[str, Any]] = []
        for run in runs:
            for task in run.get("tasks", []):
                if task.get("status") != "running":
                    continue
                heartbeat = _parse_time(task.get("heartbeat_at"))
                progress = _parse_time(task.get("progress_at"))
                stale_kind: str | None = None
                anchor: datetime | None = None
                if (
                    heartbeat is not None
                    and (now - heartbeat).total_seconds()
                    >= self.thresholds.heartbeat_stale_seconds
                ):
                    stale_kind, anchor = "heartbeat", heartbeat
                elif (
                    progress is not None
                    and (now - progress).total_seconds()
                    >= self.thresholds.progress_stale_seconds
                ):
                    stale_kind, anchor = "progress", progress
                if stale_kind is None or anchor is None:
                    continue
                anchor_text = anchor.isoformat()
                alert = {
                    "id": str(uuid4()),
                    "run_id": str(run["id"]),
                    "task_id": str(task["id"]),
                    "daemon_id": task.get("daemon_id"),
                    "severity": "warning",
                    "kind": f"{stale_kind}_stalled",
                    "message": f"Worker {stale_kind} is stale for task {task['id']}",
                    "created_at": _iso(now),
                    "resolved_at": None,
                }
                key = f"stall:{task['id']}:{stale_kind}:{anchor_text}"
                inserted = await self.repository.record_alert_once(
                    str(run["id"]), str(task["id"]), key, alert
                )
                if inserted:
                    alerts.append(alert)
        return alerts

    async def reconcile_nonterminal_runs(self, limit: int = 25) -> int:
        """Advance planning and settled work; idle worker waits do not write rows."""
        runs = await self.repository.list_runs(
            None,
            ["accepted", "clarification_required", "queued", "running"],
            limit=5000,
        )
        reconciled = 0
        for run in runs:
            if reconciled >= max(1, limit):
                break
            if not run_needs_reconciliation(run):
                continue
            phase = run.get("phase")
            try:
                await self.tick(str(run["id"]), str(run["user_id"]))
                reconciled += 1
            except Exception:
                logger.warning(
                    "main_agent.reconcile_failed",
                    extra={"run_id": run.get("id"), "phase": phase},
                    exc_info=True,
                )
        return reconciled

    async def get_status_board(self, user_id: str) -> dict[str, Any]:
        runs = await self.repository.list_runs(
            user_id, ["queued", "running", "clarification_required"]
        )
        rows: list[dict[str, Any]] = []
        alerts: list[dict[str, Any]] = []
        now = _now()
        for run in runs:
            alerts.extend(run.get("alerts", []))
            for task in run.get("tasks", []):
                heartbeat = _parse_time(task.get("heartbeat_at"))
                progress = _parse_time(task.get("progress_at"))
                heartbeat_stale = bool(
                    task.get("status") == "running"
                    and heartbeat is not None
                    and (now - heartbeat).total_seconds()
                    >= self.thresholds.heartbeat_stale_seconds
                )
                progress_stale = bool(
                    task.get("status") == "running"
                    and progress is not None
                    and (now - progress).total_seconds()
                    >= self.thresholds.progress_stale_seconds
                )
                stall_reason = (
                    "heartbeat_timeout"
                    if heartbeat_stale
                    else "progress_timeout"
                    if progress_stale
                    else None
                )
                rows.append(
                    {
                        "run_id": run["id"],
                        "task_id": task["id"],
                        "ticket_id": run.get("ticket_id")
                        or (run.get("request") or {}).get("ticket_id"),
                        "agent_id": task.get("assigned_agent_id"),
                        "daemon_id": task.get("daemon_id"),
                        "state": task.get("status"),
                        "last_heartbeat": task.get("heartbeat_at"),
                        "current_step": task.get("current_step"),
                        "stalled": heartbeat_stale or progress_stale,
                        "stall_reason": stall_reason,
                    }
                )
        return {
            "agents": rows,
            "alerts": [
                {
                    "id": alert.get("id") or alert.get("alert_key"),
                    "run_id": alert.get("run_id"),
                    "task_id": alert.get("task_id"),
                    "severity": alert.get("severity", "warning"),
                    "message": alert.get("message", "Task is stalled"),
                    "created_at": alert.get("created_at"),
                    "resolved_at": alert.get("resolved_at"),
                }
                for alert in alerts
            ],
            "thresholds": {
                "heartbeat_seconds": self.thresholds.heartbeat_stale_seconds,
                "progress_seconds": self.thresholds.progress_stale_seconds,
            },
        }
