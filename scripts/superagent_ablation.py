#!/usr/bin/env python3
"""Offline, seeded ablation harness for the Super-agent §7.1 workload.

The workload and task policy are synthetic. Event intake calls Conductor's
real ``receive_trigger`` function with in-memory persistence/effect fakes.
No provider, backend, database, network, or child process is used.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import heapq
import json
import math
import os
import random
import socket
import statistics
import subprocess
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# Must be set before importing any Conductor service module.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

DEFAULT_SEED = 7101
EVENTS = 300
TASKS = 20
RUNS = 5
CONDITIONS = (
    "super_agent",
    "super_agent_without_pass_through",
    "single_session_neutral",
    "single_session_delegation_first",
)
EVENT_COUNTS = {"work": 20, "follow_up": 65, "cancellation": 5, "casual": 100, "ci": 110}
SOURCE = {
    "source_id": "offline-ablation-source",
    "user_id": "offline-ablation-user",
    "config": {"allowed_logins": ["alice"], "routing_mode": "candidate"},
}


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def quantile(values: list[float], q: float) -> float | None:
    """Linear interpolation at rank ``(n - 1) * q``; null for no samples."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * min(1.0, max(0.0, q))
    low = int(rank)
    high = min(len(ordered) - 1, low + 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def make_workload(seed: int) -> list[dict[str, Any]]:
    """Create one matched 300-event stream with 20 tasks and five cancels."""
    rng = random.Random(seed)
    task_arrivals = {f"task-{i + 1:02d}": rng.uniform(0.0, 300.0) for i in range(TASKS)}
    events: list[dict[str, Any]] = []
    seq = 0

    def add(kind: str, at: float, task_id: str | None = None) -> None:
        nonlocal seq
        seq += 1
        events.append({
            "event_id": f"event-{seq:03d}",
            "sequence": seq,
            "arrival_virtual_seconds": round(at, 6),
            "kind": kind,
            "task_id": task_id,
            "thread_key": f"thread-{task_id or 'general'}",
        })

    for task_id, at in task_arrivals.items():
        add("work", at, task_id)
    for _ in range(EVENT_COUNTS["follow_up"]):
        task_id = rng.choice(list(task_arrivals))
        at = max(rng.uniform(0.0, 300.0), task_arrivals[task_id] + 0.01)
        add("follow_up", at, task_id)
    cancel_ids = rng.sample(list(task_arrivals), EVENT_COUNTS["cancellation"])
    for task_id in cancel_ids:
        add("cancellation", max(rng.uniform(0.0, 300.0), task_arrivals[task_id] + 0.02), task_id)
    for kind in ("casual", "ci"):
        for _ in range(EVENT_COUNTS[kind]):
            add(kind, rng.uniform(0.0, 300.0))
    events.sort(key=lambda row: (row["arrival_virtual_seconds"], row["sequence"]))
    for index, event in enumerate(events, 1):
        event["sequence"] = index
        event["event_id"] = f"event-{index:03d}"
    return events


def _task_durations(seed: int) -> dict[str, float]:
    rng = random.Random(seed ^ 0xA51A)
    return {f"task-{i + 1:02d}": round(rng.uniform(35.0, 115.0), 6) for i in range(TASKS)}


class _MemoryIntake:
    """Minimal async fakes for the DB and outbox functions called by intake."""

    def __init__(self, *, pass_through: bool, live_event_ids: set[str] | None = None):
        self.pass_through = pass_through
        self.live_event_ids = live_event_ids or set()
        self.triggers: set[str] = set()
        self.inputs: list[dict[str, Any]] = []
        self.effects: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.current_task: str | None = None
        self.current_event_id: str | None = None

    async def insert_trigger(self, **row: Any) -> dict[str, Any] | None:
        if row["trigger_id"] in self.triggers:
            return None
        self.triggers.add(row["trigger_id"])
        return {"trigger_id": row["trigger_id"]}

    async def ensure_thread(self, **_: Any) -> None:
        return None

    async def list_tickets_for_thread(self, thread_key: str) -> list[dict[str, Any]]:
        if (
            not self.pass_through
            or self.current_event_id not in self.live_event_ids
            or not thread_key.startswith("thread-task-")
        ):
            return []
        task_id = thread_key.removeprefix("thread-")
        if task_id != self.current_task:
            return []
        return [{
            "ticket_id": task_id,
            "status": "running",
            "origin_thread_key": thread_key,
            "subscription_kind": "own",
            "worker_thread_id": f"worker-{task_id}",
        }]

    async def add_input(self, **row: Any) -> dict[str, Any]:
        input_id = f"input-{len(self.inputs) + 1:04d}"
        self.inputs.append({**row, "input_id": input_id})
        return {"input_id": input_id}

    async def context_subscribers(self, *_: Any, **__: Any) -> list[Any]:
        return []

    async def append_pending_trigger(self, thread_key: str, trigger_id: str) -> None:
        self.pending.append({"thread_key": thread_key, "trigger_id": trigger_id})

    async def enqueue_effect(self, **row: Any) -> bool:
        self.effects.append(row)
        return True


class _WorkerPool:
    """Virtual FIFO pool; cancellation removes active or queued work."""

    def __init__(self, slots: int = 4):
        self.slots = slots
        self.rows: dict[str, dict[str, Any]] = {}
        self.active: dict[str, float] = {}
        self.queue: list[str] = []

    def _fill(self, at: float) -> None:
        while self.queue and len(self.active) < self.slots:
            task_id = self.queue.pop(0)
            row = self.rows[task_id]
            if row["status"] != "queued":
                continue
            row["status"] = "running"
            row["worker_start_virtual_seconds"] = at
            self.active[task_id] = at + row["synthetic_duration_seconds"]

    def advance(self, at: float) -> None:
        while self.active:
            task_id, finish = min(self.active.items(), key=lambda item: item[1])
            if finish > at:
                break
            del self.active[task_id]
            row = self.rows[task_id]
            row["status"] = "completed"
            row["result_virtual_seconds"] = finish
            self._fill(finish)

    def submit(self, task_id: str, arrival: float, duration: float, at: float) -> None:
        if task_id in self.rows:
            return
        self.rows[task_id] = {
            "status": "queued",
            "arrival_virtual_seconds": arrival,
            "synthetic_duration_seconds": duration,
            "worker_start_virtual_seconds": None,
            "result_virtual_seconds": None,
        }
        self.queue.append(task_id)
        self._fill(at)

    def cancel(self, task_id: str, at: float) -> None:
        self.advance(at)
        row = self.rows.get(task_id)
        if row is None or row["status"] in {"completed", "cancelled"}:
            return
        if row["status"] == "running":
            self.active.pop(task_id, None)
        else:
            self.queue.remove(task_id)
        row["status"] = "cancelled"
        row["result_virtual_seconds"] = at
        self._fill(at)

    def is_running(self, task_id: str | None) -> bool:
        return bool(task_id and task_id in self.active)

    def settle(self) -> None:
        while self.active:
            finish = min(self.active.values())
            self.advance(finish)


def _install_offline_guards() -> dict[str, Any]:
    """Deny network and child-process use while importing/running the service."""
    originals: dict[str, Any] = {}

    def deny(*_: Any, **__: Any) -> Any:
        raise RuntimeError("offline ablation guard blocked network or process access")

    originals["socket.socket"] = socket.socket

    base_socket = originals["socket.socket"]

    class GuardedSocket(base_socket):  # type: ignore[misc, valid-type]
        def __new__(cls, family: int = socket.AF_INET, *args: Any, **kwargs: Any) -> Any:
            if family != socket.AF_UNIX:
                deny()
            return super().__new__(cls, family, *args, **kwargs)

        def connect(self, address: Any) -> None:
            deny()

        def connect_ex(self, address: Any) -> int:
            deny()

    socket.socket = GuardedSocket  # type: ignore[assignment]
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        originals[f"socket.{name}"] = getattr(socket, name)
        setattr(socket, name, deny)
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        originals[f"subprocess.{name}"] = getattr(subprocess, name)
        setattr(subprocess, name, deny)
    process_calls = (
        "system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp",
        "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
        "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
    )
    for name in process_calls:
        if hasattr(os, name):
            originals[f"os.{name}"] = getattr(os, name)
            setattr(os, name, deny)
    return originals


def _restore_guards(originals: dict[str, Any]) -> None:
    for key, value in originals.items():
        module_name, name = key.split(".", 1)
        module = {"socket": socket, "subprocess": subprocess, "os": os}[module_name]
        setattr(module, name, value)


async def _call_real_intake(
    events: list[dict[str, Any]], *, condition: str, live_event_ids: set[str] | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pass_through = condition == "super_agent"
    memory = _MemoryIntake(pass_through=pass_through, live_event_ids=live_event_ids)
    from src.server.services.autonomy import intake

    original = {
        "insert_trigger": intake.trigger_db.insert_trigger,
        "ensure_thread": intake.trigger_db.ensure_thread,
        "list_tickets_for_thread": intake.ticket_db.list_tickets_for_thread,
        "add_input": intake.ticket_db.add_input,
        "context_subscribers": intake.ticket_db.context_subscribers,
        "append_pending_trigger": intake.trigger_db.append_pending_trigger,
        "enqueue_effect": intake.enqueue_effect,
        "thread_routing_lock": intake.ticket_db.thread_routing_lock,
    }

    @asynccontextmanager
    async def _memory_routing_lock(*_args, **_kwargs):
        # The harness has no database. The lock still wraps the real intake
        # path in production; here every persistence call is already in memory.
        yield

    intake.trigger_db.insert_trigger = memory.insert_trigger
    intake.trigger_db.ensure_thread = memory.ensure_thread
    intake.ticket_db.list_tickets_for_thread = memory.list_tickets_for_thread
    intake.ticket_db.add_input = memory.add_input
    intake.ticket_db.context_subscribers = memory.context_subscribers
    intake.trigger_db.append_pending_trigger = memory.append_pending_trigger
    intake.enqueue_effect = memory.enqueue_effect
    intake.ticket_db.thread_routing_lock = _memory_routing_lock
    ledger: list[dict[str, Any]] = []
    try:
        for event in events:
            memory.current_task = event["task_id"] if event["kind"] in {"follow_up", "cancellation"} else None
            memory.current_event_id = event["event_id"]
            draft = {
                "trigger_id": f"{condition}-{event['event_id']}",
                "external_id": event["event_id"],
                "kind": "ci" if event["kind"] == "ci" else "comment",
                "thread_key": event["thread_key"],
                "actor_login": "buildbot" if event["kind"] == "ci" else "alice",
                "actor_is_bot": event["kind"] == "ci",
                "addressed": event["kind"] in {"work", "follow_up", "cancellation"},
                "text": event["kind"],
                "payload": {"task_id": event["task_id"]},
            }
            accepted = await intake.receive_trigger(source=SOURCE, draft=draft)
            row = {
                **event,
                "condition": condition,
                "intake_accepted": accepted,
                "intake_path": "pass_through_input" if accepted and len(memory.inputs) and memory.inputs[-1]["trigger_id"] == draft["trigger_id"] else (
                    "pending_trigger" if accepted else "filtered"),
                "input_count": len(memory.inputs),
                "pending_count": len(memory.pending),
            }
            ledger.append(row)
    finally:
        intake.trigger_db.insert_trigger = original["insert_trigger"]
        intake.trigger_db.ensure_thread = original["ensure_thread"]
        intake.ticket_db.list_tickets_for_thread = original["list_tickets_for_thread"]
        intake.ticket_db.add_input = original["add_input"]
        intake.ticket_db.context_subscribers = original["context_subscribers"]
        intake.trigger_db.append_pending_trigger = original["append_pending_trigger"]
        intake.enqueue_effect = original["enqueue_effect"]
        intake.ticket_db.thread_routing_lock = original["thread_routing_lock"]
    return ledger, {
        "inputs": len(memory.inputs),
        "delivery_effects": len(memory.effects),
        "pending_triggers": len(memory.pending),
        "accepted_triggers": sum(bool(row["intake_accepted"]) for row in ledger),
        "filtered_triggers": sum(not row["intake_accepted"] for row in ledger),
    }


def _simulate_tasks(
    events: list[dict[str, Any]], durations: dict[str, float], condition: str
) -> tuple[list[dict[str, Any]], dict[str, Any], set[str], dict[str, str]]:
    task_events = {event["task_id"]: event for event in events if event["kind"] == "work"}
    delegated = set(task_events)
    if condition == "single_session_neutral":
        delegated = set()
    elif condition == "single_session_delegation_first":
        # The four earliest work requests stay in-session; the other sixteen
        # are delegated under a fixed assumption, not a prompt/model result.
        ordered = sorted(task_events, key=lambda key: task_events[key]["arrival_virtual_seconds"])
        delegated = set(ordered[4:])

    pool = _WorkerPool()
    own_results: dict[str, dict[str, Any]] = {}
    paths: dict[str, str] = {}
    live_event_ids: set[str] = set()
    router_actions: list[tuple[float, int, str, str | None]] = []
    router_free = 0.0
    serial = 0
    router_decisions = 0
    main_decisions = 0
    session_free = 0.0
    route_counts: Counter[str] = Counter()

    def finish_router_actions(until: float) -> None:
        while router_actions and router_actions[0][0] <= until:
            finish, _, action, task_id = heapq.heappop(router_actions)
            pool.advance(finish)
            if action == "submit" and task_id is not None:
                task = task_events[task_id]
                pool.submit(task_id, task["arrival_virtual_seconds"], durations[task_id], finish)
            elif action == "cancel" and task_id is not None:
                pool.cancel(task_id, finish)
        pool.advance(until)

    for event in events:
        at = event["arrival_virtual_seconds"]
        if condition.startswith("single_session"):
            process_at = max(at, session_free)
            pool.advance(process_at)
            path = "pending_trigger" if event["kind"] != "ci" else "filtered"
            accepted = event["kind"] != "ci"
            if accepted:
                main_decisions += 1
        else:
            finish_router_actions(at)
            path = "pending_trigger" if event["kind"] != "ci" else "filtered"
            accepted = event["kind"] != "ci"
            if (
                condition == "super_agent"
                and event["kind"] in {"follow_up", "cancellation"}
                and pool.is_running(event["task_id"])
            ):
                path = "pass_through_input"
                live_event_ids.add(event["event_id"])
            process_at = at
        paths[event["event_id"]] = path
        route_counts[event["kind"] + ":" + path] += 1

        if not accepted:
            continue
        task_id = event["task_id"]
        if condition.startswith("single_session"):
            if event["kind"] == "work":
                if task_id in delegated:
                    dispatch_at = process_at + 0.35
                    pool.advance(dispatch_at)
                    pool.submit(task_id, at, durations[task_id], dispatch_at)
                    session_free = dispatch_at
                else:
                    start = process_at + 0.15
                    finish = start + durations[task_id]
                    own_results[task_id] = {"status": "completed", "worker_start_virtual_seconds": start, "result_virtual_seconds": finish}
                    session_free = finish
            else:
                handled_at = process_at + 0.15
                if event["kind"] == "cancellation":
                    pool.cancel(task_id, handled_at)
                session_free = handled_at
        elif path == "pass_through_input":
            if event["kind"] == "cancellation":
                pool.cancel(task_id, at)
        else:
            router_decisions += 1
            router_free = max(at, router_free) + 0.35
            action = "submit" if event["kind"] == "work" else "cancel" if event["kind"] == "cancellation" else "noop"
            serial += 1
            heapq.heappush(router_actions, (router_free, serial, action, task_id))

    if condition.startswith("single_session"):
        pool.settle()
    else:
        while router_actions:
            finish_router_actions(router_actions[0][0])
        pool.settle()

    task_rows = []
    for task_id, event in task_events.items():
        outcome = own_results.get(task_id) or pool.rows.get(task_id)
        if outcome is None:
            raise RuntimeError(f"task {task_id} has no terminal outcome")
        result_at = outcome["result_virtual_seconds"]
        arrival = event["arrival_virtual_seconds"]
        if result_at < arrival:
            raise RuntimeError(f"task {task_id} result precedes arrival")
        task_rows.append({
            "condition": condition,
            "task_id": task_id,
            "arrival_virtual_seconds": arrival,
            "worker_start_virtual_seconds": outcome["worker_start_virtual_seconds"],
            "result_virtual_seconds": result_at,
            "arrival_to_result_virtual_seconds": round(result_at - arrival, 6),
            "actual_task_elapsed_seconds": None,
            "actual_task_elapsed_note": "not measured; task execution is virtual",
            "status": outcome["status"],
            "synthetic_duration_seconds": durations[task_id],
            "delegated_by_fixed_policy": task_id in delegated,
            "simulated_router_decisions": router_decisions,
            "measured_router_calls": None,
            "main_session_message_decisions": main_decisions,
            "measured_tokens": None,
            "measured_cache_read_tokens": None,
        })
    completed_latencies = [row["arrival_to_result_virtual_seconds"] for row in task_rows if row["status"] == "completed"]
    all_result_latencies = [row["arrival_to_result_virtual_seconds"] for row in task_rows]
    completed = sum(row["status"] == "completed" for row in task_rows)
    cancelled = len(task_rows) - completed
    return task_rows, {
        "condition": condition,
        "task_count": len(task_rows),
        "completed": completed,
        "cancelled": cancelled,
        "p50_completed_latency_virtual_seconds": quantile(completed_latencies, 0.50),
        "p95_completed_latency_virtual_seconds": quantile(completed_latencies, 0.95),
        "p50_all_task_result_latency_virtual_seconds": quantile(all_result_latencies, 0.50),
        "p95_all_task_result_latency_virtual_seconds": quantile(all_result_latencies, 0.95),
        "simulated_router_decisions": router_decisions,
        "measured_router_calls": None,
        "main_session_message_decisions": main_decisions,
        "intake_paths": dict(route_counts),
        "token_usage": None,
        "cache_read_tokens": None,
        "latency_basis": "virtual simulation seconds; no real Worker or model clock",
    }, live_event_ids, paths


async def _execute_run(seed: int, condition: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], str]:
    events = make_workload(seed)
    workload_hash = _canonical_hash(events)
    durations = _task_durations(seed)
    task_rows, summary, live_event_ids, expected_paths = _simulate_tasks(events, durations, condition)
    intake_rows, intake_summary = await _call_real_intake(events, condition=condition, live_event_ids=live_event_ids)
    if any(row["intake_path"] != expected_paths[row["event_id"]] for row in intake_rows):
        raise RuntimeError(f"real intake path disagreed with simulated subscription state for {condition}")
    for row in (*intake_rows, *task_rows):
        row["seed"] = seed
        row["workload_sha256"] = workload_hash
    summary.update({"seed": seed, "workload_sha256": workload_hash, "intake": intake_summary})
    return intake_rows, task_rows, summary, workload_hash


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(131072), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def run(*, seed: int = DEFAULT_SEED, runs: int = RUNS, events: int = EVENTS, tasks: int = TASKS, output: Path) -> dict[str, Any]:
    if events != EVENTS or tasks != TASKS or runs != RUNS:
        raise ValueError("this article-scale harness requires exactly 300 events, 20 tasks, and 5 runs")
    output.mkdir(parents=True, exist_ok=True)
    all_runs: list[dict[str, Any]] = []
    all_event_rows: list[dict[str, Any]] = []
    all_task_rows: list[dict[str, Any]] = []
    workload_hashes: dict[str, list[str]] = {condition: [] for condition in CONDITIONS}
    started = time.perf_counter()
    for run_index in range(runs):
        run_seed = seed + run_index
        for condition in CONDITIONS:
            event_rows, task_rows, summary, workload_hash = await _execute_run(run_seed, condition)
            workload_hashes[condition].append(workload_hash)
            all_runs.append(summary)
            all_event_rows.extend(event_rows)
            all_task_rows.extend(task_rows)
    actual_harness_elapsed = time.perf_counter() - started
    aggregates = {}
    for condition in CONDITIONS:
        rows = [row for row in all_runs if row["condition"] == condition]
        condition_tasks = [task for task in all_task_rows if task["condition"] == condition]
        latencies = [task["arrival_to_result_virtual_seconds"] for task in condition_tasks]
        completed_latencies = [task["arrival_to_result_virtual_seconds"] for task in condition_tasks if task["status"] == "completed"]
        aggregates[condition] = {
            "runs": len(rows),
            "tasks": sum(row["task_count"] for row in rows),
            "completed": sum(row["completed"] for row in rows),
            "cancelled": sum(row["cancelled"] for row in rows),
            "pooled_completed_latency_p50_virtual_seconds": quantile(completed_latencies, 0.50),
            "pooled_completed_latency_p95_virtual_seconds": quantile(completed_latencies, 0.95),
            "pooled_all_task_result_latency_p50_virtual_seconds": quantile(latencies, 0.50),
            "pooled_all_task_result_latency_p95_virtual_seconds": quantile(latencies, 0.95),
            "median_run_completed_p50_virtual_seconds": statistics.median(row["p50_completed_latency_virtual_seconds"] for row in rows),
            "median_run_completed_p95_virtual_seconds": statistics.median(row["p95_completed_latency_virtual_seconds"] for row in rows),
            "mean_simulated_router_decisions_per_run": statistics.fmean(row["simulated_router_decisions"] for row in rows),
            "mean_main_session_message_decisions_per_run": statistics.fmean(row["main_session_message_decisions"] for row in rows),
            "mean_intake_inputs_per_run": statistics.fmean(row["intake"]["inputs"] for row in rows),
            "mean_pending_triggers_per_run": statistics.fmean(row["intake"]["pending_triggers"] for row in rows),
            "measured_tokens": None,
            "measured_cache_read_tokens": None,
        }
    result = {
        "kind": "offline-seeded-superagent-ablation",
        "evidence_class": "synthetic policy and virtual-time simulation; real intake service function with in-memory fakes",
        "seed": seed,
        "runs_per_condition": runs,
        "budget_usd": 0.0,
        "workload": {"events_per_run": events, "tasks_per_run": tasks, "event_counts": EVENT_COUNTS},
        "conditions": list(CONDITIONS),
        "workload_sha256_by_condition": workload_hashes,
        "matched_workload_check": all(hashes == workload_hashes[CONDITIONS[0]] for hashes in workload_hashes.values()),
        "summary_method": {
            "quantile": "linear interpolation at rank (n - 1) * q",
            "pooled": "all completed task latencies per condition across five runs; all-task result latency is separate",
            "median_run": "median of the five run-level completed-task p50 or p95 values",
        },
        "actual_harness_elapsed_seconds": round(actual_harness_elapsed, 6),
        "actual_task_elapsed_seconds": None,
        "token_usage": None,
        "router_token_proxy": None,
        "model_policy": {
            "super_agent": "all work delegated to four simulated Worker slots",
            "super_agent_without_pass_through": "all work delegated; follow-up subscription disabled",
            "single_session_neutral": "no tasks delegated",
            "single_session_delegation_first": "fixed assumption: first four tasks stay in session; remaining sixteen delegated",
            "note": "These fixed policies are not observations of prompt or model behavior.",
        },
        "assumptions": {
            "task_durations": "seeded uniform 35 to 115 virtual seconds",
            "router_service": "0.35 virtual seconds per synthetic work dispatch",
            "main_session_message_cost": "0.15 virtual seconds to handle each admitted non-work message",
            "main_session_delegation_cost": "0.35 virtual seconds after the main session is available",
            "worker_slots": 4,
            "cancellation": "five seeded cancellation messages; an active or queued delegated task stops when the message is routed; a single-session message is handled when that session is available",
        },
        "aggregates": aggregates,
        "runs": all_runs,
    }
    repo_root = Path(__file__).resolve().parents[1]
    source_paths = [Path(__file__).resolve(), repo_root / "src/server/services/autonomy/intake.py", repo_root / "src/server/services/autonomy/routing.py"]
    result["source_sha256"] = {str(path.relative_to(repo_root)): _sha256(path) for path in source_paths}
    result["config_sha256"] = _canonical_hash({"event_counts": EVENT_COUNTS, "conditions": CONDITIONS, "seed": seed, "runs": runs, "assumptions": result["assumptions"], "policies": result["model_policy"]})
    _write_jsonl(output / "events.jsonl", all_event_rows)
    _write_jsonl(output / "tasks.jsonl", all_task_rows)
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result


def _write_jsonl(path: Path, rows: Any) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--runs", type=int, default=RUNS)
    parser.add_argument("--events", type=int, default=EVENTS)
    parser.add_argument("--tasks", type=int, default=TASKS)
    parser.add_argument("--budget-usd", type=float, default=0.0)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "docs/experiments/superagent-ablation/results")
    args = parser.parse_args(argv)
    if not math.isfinite(args.budget_usd) or args.budget_usd != 0.0:
        parser.error("only a finite $0.00 budget is allowed")
    if args.seed < 0 or args.runs != RUNS or args.events != EVENTS or args.tasks != TASKS:
        parser.error("requires non-negative seed, exactly 5 runs, 300 events, and 20 tasks")
    originals = _install_offline_guards()
    try:
        result = asyncio.run(run(seed=args.seed, runs=args.runs, events=args.events, tasks=args.tasks, output=args.output))
    finally:
        _restore_guards(originals)
    print(json.dumps({"output": str(args.output), "matched_workload_check": result["matched_workload_check"], "aggregates": result["aggregates"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
