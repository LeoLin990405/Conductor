#!/usr/bin/env python3
"""Offline arrival-rate stress harness for Conductor Router Eval batching.

The scheduler, Eval batching, effect routing, and candidate-batch prompt path
are production functions. Persistence and ProjectDispatcher are in-memory fakes;
all schedule and model timings are virtual and never call a provider.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import math
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

DEFAULT_RATES = (1.0, 5.0, 10.0, 20.0, 40.0)
DEFAULT_REPLICATES = 5
DEFAULT_WINDOW_SECONDS = 4.0
DEFAULT_DURATION_SECONDS = 300.0
MAX_DURATION_SECONDS = 1800.0
MAX_RATE_PER_MINUTE = 1000.0
MAX_REPLICATES = 20
MAX_USERS = 8
MAX_RATES = 20
MAX_CASES = 200
MAX_ACTUAL_ARRIVALS_PER_TRACE = 2_000
MAX_EXPECTED_ARRIVALS_PER_REPLICATE = 20_000
MAX_SCHEDULER_TICKS_PER_TRACE = 2_000
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _install_offline_guards() -> None:
    """Deny network and child-process operations before importing services."""

    denied = {
        "os.exec",
        "os.fork",
        "os.forkpty",
        "os.system",
        "socket.bind",
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.sendmsg",
        "socket.sendto",
        "subprocess.Popen",
    }

    def guard(event: str, args: tuple[Any, ...]) -> None:
        if event in denied:
            raise RuntimeError(f"offline Router stress harness blocked {event}")

    sys.addaudithook(guard)


def _validate_config(
    rates: list[float], replicates: int, duration: float, window: float, users: int
) -> None:
    if not rates or any(not math.isfinite(rate) or rate <= 0 for rate in rates):
        raise ValueError("rates must be a non-empty list of finite positive values")
    if len(rates) > MAX_RATES or len(rates) * replicates > MAX_CASES:
        raise ValueError(
            f"the run is limited to {MAX_RATES} rates and {MAX_CASES} rate-replicate cases"
        )
    if any(rate > MAX_RATE_PER_MINUTE for rate in rates):
        raise ValueError(f"rate exceeds the {MAX_RATE_PER_MINUTE:g}/min safety bound")
    if any(
        rate * duration / 60 > MAX_EXPECTED_ARRIVALS_PER_REPLICATE for rate in rates
    ):
        raise ValueError("rate and duration exceed the bounded arrival workload")
    if sum(rates) * duration / 60 * replicates > 100_000:
        raise ValueError(
            "combined rate, duration, and replicate count exceed the workload bound"
        )
    if not 1 <= replicates <= MAX_REPLICATES:
        raise ValueError(f"replicates must be between 1 and {MAX_REPLICATES}")
    if not math.isfinite(duration) or not 1 <= duration <= MAX_DURATION_SECONDS:
        raise ValueError(f"duration must be between 1 and {MAX_DURATION_SECONDS:g}s")
    if not math.isfinite(window) or not 0.1 <= window <= 60:
        raise ValueError("scheduler window must be between 0.1 and 60s")
    if duration / window > MAX_SCHEDULER_TICKS_PER_TRACE:
        raise ValueError(
            f"duration/window exceeds {MAX_SCHEDULER_TICKS_PER_TRACE} scheduler ticks"
        )
    if not 1 <= users <= MAX_USERS:
        raise ValueError(f"users must be between 1 and {MAX_USERS}")


def _poisson_arrivals(
    rate_per_minute: float, duration: float, seed: int, users: int = 1
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    rate_per_second = rate_per_minute / 60.0
    now = 0.0
    arrivals: list[dict[str, Any]] = []
    while True:
        now += rng.expovariate(rate_per_second)
        if now > duration:
            break
        if len(arrivals) >= MAX_ACTUAL_ARRIVALS_PER_TRACE:
            raise ValueError(
                f"Poisson trace exceeded {MAX_ACTUAL_ARRIVALS_PER_TRACE} actual arrivals"
            )
        index = len(arrivals)
        arrivals.append(
            {
                "trigger_id": f"trigger-{seed}-{index:06d}",
                "arrival_seconds": round(now, 9),
                "user_id": f"user-{index % users + 1}",
                "thread_key": f"thread-{seed}-{index:06d}",
                "source_id": f"source-{seed}-{index:06d}",
            }
        )
    return arrivals


def _trace_hash(arrivals: list[dict[str, Any]]) -> str:
    body = json.dumps(arrivals, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def _amortized_inference_per_candidate(decisions: list[dict[str, Any]]) -> float:
    """Weight each candidate equally, even when Router batches differ in size."""
    candidate_count = sum(int(row["batch_size"]) for row in decisions)
    if not candidate_count:
        return 0.0
    return (
        sum(float(row["inference_seconds_virtual"]) for row in decisions)
        / candidate_count
    )


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes(root: Path) -> dict[str, str]:
    paths = (
        "scripts/superagent_router_stress.py",
        "src/server/services/autonomy/evals.py",
        "src/server/services/autonomy/router.py",
        "src/server/services/autonomy/policy.py",
        "src/server/services/autonomy/context.py",
        "src/server/services/autonomy/routing.py",
    )
    return {path: _hash_file(root / path) for path in paths}


@dataclass
class FakeRun:
    result: dict[str, Any]
    events: list[dict[str, Any]]
    effects: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    requests: list[dict[str, Any]]


class _FakeStore:
    def __init__(self, arrivals: list[dict[str, Any]]) -> None:
        self.triggers: dict[str, dict[str, Any]] = {}
        self.sources: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, dict[str, Any]] = {}
        self.evals: dict[str, dict[str, Any]] = {}
        for item in arrivals:
            trigger_id = item["trigger_id"]
            self.triggers[trigger_id] = {
                "trigger_id": trigger_id,
                "source_id": item["source_id"],
                "thread_key": item["thread_key"],
                "actor_login": "synthetic-user",
                "actor_is_bot": False,
                "addressed": True,
                "text": "synthetic offline trigger",
                "url": "",
            }
            self.sources[item["source_id"]] = {
                "source_id": item["source_id"],
                "user_id": item["user_id"],
                "kind": "github",
                "config": {"allowed_logins": ["synthetic-user"]},
            }
            self.threads[item["thread_key"]] = {
                "thread_key": item["thread_key"],
                "source_id": item["source_id"],
                "user_id": item["user_id"],
                "pending_trigger_ids": [],
            }

    async def list_queued_evals_without_effect(self) -> list[dict[str, Any]]:
        return []

    async def list_pending_threads(self) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.threads.values() if row["pending_trigger_ids"]
        ]

    async def get_trigger(self, trigger_id: str) -> dict[str, Any] | None:
        return self.triggers.get(trigger_id)

    async def get_source_any_user(self, source_id: str) -> dict[str, Any] | None:
        return self.sources.get(source_id)

    async def current_policy(self, _user_id: str) -> dict[str, Any]:
        return {
            "version": "offline-v1",
            "accept_mode": "explicit",
            "admission": {"listed_addressed": "explicit"},
        }

    async def start_thread_eval(
        self, *, thread_key: str, trigger_ids: list[str], policy_version: str
    ) -> dict[str, Any] | None:
        row = self.threads[thread_key]
        selected = trigger_ids[0]
        if selected not in row["pending_trigger_ids"]:
            return None
        row["pending_trigger_ids"].remove(selected)
        eval_id = f"eval-{selected.removeprefix('trigger-')}"
        created = {
            "eval_id": eval_id,
            "user_id": row["user_id"],
            "thread_key": thread_key,
            "trigger_ids": [selected],
            "policy_version": policy_version,
            "status": "queued",
            "deadline_at": None,
        }
        self.evals[eval_id] = created
        return created

    async def prune_pending_triggers(self, _thread_key: str, _ids: list[str]) -> None:
        return None

    async def get_eval(self, eval_id: str) -> dict[str, Any] | None:
        row = self.evals.get(eval_id)
        return dict(row) if row else None

    async def mark_delivered(self, eval_id: str) -> None:
        self.evals[eval_id]["status"] = "delivered"

    async def list_triggers_for_thread(
        self, thread_key: str, *, limit: int
    ) -> list[dict[str, Any]]:
        del limit
        return [
            row for row in self.triggers.values() if row["thread_key"] == thread_key
        ]

    async def list_tickets_for_thread(self, _thread_key: str) -> list[dict[str, Any]]:
        return []


class _FakeDispatcher:
    def __init__(
        self,
        *,
        now: Callable[[], float],
        user_free: dict[str, float],
        rng: random.Random,
        decisions: list[dict[str, Any]],
        requests: list[dict[str, Any]],
    ) -> None:
        self.now = now
        self.user_free = user_free
        self.rng = rng
        self.decisions = decisions
        self.requests = requests

    async def dispatch_turn(self, **kwargs: Any) -> dict[str, Any]:
        prompt = str(kwargs["prompt"])
        payload = json.loads(prompt[prompt.index("\n") + 1 :])
        user_id = str(kwargs["user_id"])
        eval_ids = list(payload["eval_ids"])
        candidates = list(payload["candidates"])
        count = 1 + self.rng.choices([0, 1, 2], weights=[0.65, 0.27, 0.08])[0]
        service_start = max(self.now(), self.user_free.get(user_id, 0.0))
        queue_wait = service_start - self.now()
        request_times = [
            max(0.05, 2.5 + 0.035 * len(candidates) + self.rng.uniform(-0.2, 0.2))
            for _ in range(count)
        ]
        cursor = service_start
        for index, request_time in enumerate(request_times):
            finished = cursor + request_time
            self.requests.append(
                {
                    "request_id": f"{kwargs['request_key']}-request-{index + 1}",
                    "request_key": kwargs["request_key"],
                    "user_id": user_id,
                    "eval_ids": eval_ids,
                    "batch_size": len(candidates),
                    "queue_start_seconds": round(cursor, 9),
                    "request_seconds_virtual": round(request_time, 9),
                    "finish_seconds_virtual": round(finished, 9),
                }
            )
            cursor = finished
        self.user_free[user_id] = cursor
        decision = {
            "request_key": kwargs["request_key"],
            "user_id": user_id,
            "eval_ids": eval_ids,
            "candidate_trigger_ids": [str(item["trigger_id"]) for item in candidates],
            "batch_size": len(candidates),
            "dispatch_seconds_virtual": round(self.now(), 9),
            "queue_wait_seconds_virtual": round(queue_wait, 9),
            "request_count": count,
            "request_time_seconds_virtual": round(statistics.fmean(request_times), 9),
            "inference_seconds_virtual": round(sum(request_times), 9),
            "inference_seconds_per_candidate_virtual": round(
                sum(request_times) / max(1, len(candidates)), 9
            ),
            "completion_seconds_virtual": round(cursor, 9),
        }
        self.decisions.append(decision)
        return {"run_id": kwargs["request_key"]}


async def _run_service_trace(
    arrivals: list[dict[str, Any]],
    *,
    rate: float,
    seed: int,
    window_seconds: float,
    unbatched: bool,
) -> FakeRun:
    from unittest.mock import patch

    from src.server.services.autonomy import evals as eval_service

    store = _FakeStore(arrivals)
    events = [dict(item) for item in arrivals]
    pending_effects: list[dict[str, Any]] = []
    effect_ledger: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    trigger_by_id = {item["trigger_id"]: item for item in arrivals}
    current_time = 0.0
    user_free: dict[str, float] = {}
    model_rng = random.Random(seed ^ 0x5A17_2026)
    production_batcher = eval_service.router_eval_batches
    dispatcher = _FakeDispatcher(
        now=lambda: current_time,
        user_free=user_free,
        rng=model_rng,
        decisions=decisions,
        requests=requests,
    )

    async def enqueue_effect(**kwargs: Any) -> bool:
        row = {
            "effect_id": f"effect-{len(effect_ledger):06d}",
            "user_id": kwargs["user_id"],
            "kind": kwargs["kind"],
            "idempotency_key": kwargs["idempotency_key"],
            "eval_ids": list(kwargs["payload"]["eval_ids"]),
            "scheduler_seconds": round(current_time, 9),
        }
        pending_effects.append(row)
        effect_ledger.append(row)
        return True

    async def ensure_session(
        user_id: str, *, dispatcher=None, shard: int = 0
    ) -> dict[str, str]:
        del dispatcher, shard
        return {"thread_id": f"router-{user_id}", "workspace_id": f"home-{user_id}"}

    # The production scheduler and route effect execute against in-memory
    # persistence. Only persistence and the final dispatcher are replaced.
    with (
        patch.object(
            eval_service.eval_db,
            "list_queued_evals_without_effect",
            store.list_queued_evals_without_effect,
        ),
        patch.object(
            eval_service.eval_db, "list_pending_threads", store.list_pending_threads
        ),
        patch.object(
            eval_service.eval_db, "start_thread_eval", store.start_thread_eval
        ),
        patch.object(
            eval_service.eval_db, "prune_pending_triggers", store.prune_pending_triggers
        ),
        patch.object(eval_service.eval_db, "get_eval", store.get_eval),
        patch.object(eval_service.eval_db, "mark_delivered", store.mark_delivered),
        patch.object(eval_service.trigger_db, "get_trigger", store.get_trigger),
        patch.object(
            eval_service.trigger_db,
            "list_triggers_for_thread",
            store.list_triggers_for_thread,
        ),
        patch.object(
            eval_service.source_db, "get_source_any_user", store.get_source_any_user
        ),
        patch.object(
            eval_service.ticket_db,
            "list_tickets_for_thread",
            store.list_tickets_for_thread,
        ),
        patch.object(eval_service.policy, "current_policy", store.current_policy),
        patch.object(eval_service, "enqueue_effect", enqueue_effect),
        patch.object(eval_service.router, "ProjectDispatcher", lambda: dispatcher),
        patch.object(eval_service.router, "ensure_router_session", ensure_session),
        patch.object(
            eval_service,
            "router_eval_batches",
            lambda ids: (
                [[item] for item in ids] if unbatched else production_batcher(ids)
            ),
        ),
    ):
        arrived_count = 0
        next_tick = 0.0
        while arrived_count < len(arrivals):
            current_time = next_tick
            while (
                arrived_count < len(arrivals)
                and arrivals[arrived_count]["arrival_seconds"] <= current_time
            ):
                item = arrivals[arrived_count]
                store.threads[item["thread_key"]]["pending_trigger_ids"].append(
                    item["trigger_id"]
                )
                arrived_count += 1
            await eval_service.start_due_evals()
            newly_queued = pending_effects[:]
            pending_effects.clear()
            for effect_row in newly_queued:
                effect = SimpleNamespace(
                    payload={
                        "user_id": effect_row["user_id"],
                        "eval_ids": effect_row["eval_ids"],
                    }
                )
                await eval_service.route_eval_effect(effect)
            next_tick += window_seconds
            if (
                next_tick
                > max((item["arrival_seconds"] for item in arrivals), default=0)
                + window_seconds
            ):
                break

    completed_by_trigger: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        for trigger_id in decision["candidate_trigger_ids"]:
            arrival = trigger_by_id[trigger_id]["arrival_seconds"]
            completion = decision["completion_seconds_virtual"]
            completed_by_trigger[trigger_id] = {
                "scheduler_wait_seconds_virtual": round(
                    decision["dispatch_seconds_virtual"] - arrival, 9
                ),
                "router_queue_wait_seconds_virtual": decision[
                    "queue_wait_seconds_virtual"
                ],
                "total_queue_wait_seconds_virtual": round(
                    completion - arrival - decision["inference_seconds_virtual"], 9
                ),
                "completion_latency_seconds_virtual": round(completion - arrival, 9),
                "request_key": decision["request_key"],
                "batch_size": decision["batch_size"],
                "user_id": decision["user_id"],
            }
    for event in events:
        event.update(completed_by_trigger.get(event["trigger_id"], {}))
        event["completed"] = event["trigger_id"] in completed_by_trigger
    batches = [int(item["batch_size"]) for item in decisions]
    batch_cap = eval_service.MAX_ROUTER_BATCH_EVALS
    user_of_eval = {eval_id: row["user_id"] for eval_id, row in store.evals.items()}
    cross_user_batch = any(
        len({user_of_eval.get(eval_id) for eval_id in row["eval_ids"]}) > 1
        for row in effect_ledger
        + [{"eval_ids": decision["eval_ids"]} for decision in decisions]
    )
    expected = {item["trigger_id"] for item in arrivals}
    completed = set(completed_by_trigger)
    result = {
        "rate_per_minute": rate,
        "seed": seed,
        "mode": "unbatched" if unbatched else "batched",
        "arrival_count": len(arrivals),
        "completed_count": len(completed),
        "lost_trigger_ids": sorted(expected - completed),
        "duplicate_completion_count": sum(
            len(row["candidate_trigger_ids"]) for row in decisions
        )
        - len(completed),
        "avg_batch_size": round(statistics.fmean(batches), 6) if batches else 0.0,
        "max_batch_size": max(batches, default=0),
        "batch_cap": batch_cap,
        "batch_size_distribution": {
            str(size): batches.count(size) for size in sorted(set(batches))
        },
        "request_count": len(requests),
        "mean_request_seconds_virtual": round(
            statistics.fmean(item["request_seconds_virtual"] for item in requests), 6
        )
        if requests
        else 0.0,
        "amortized_inference_seconds_per_candidate_virtual": round(
            _amortized_inference_per_candidate(decisions), 6
        ),
        "mean_inference_seconds_per_router_turn_virtual": round(
            statistics.fmean(item["inference_seconds_virtual"] for item in decisions), 6
        )
        if decisions
        else 0.0,
        "mean_queue_wait_seconds_virtual": round(
            statistics.fmean(
                item["total_queue_wait_seconds_virtual"]
                for item in events
                if item["completed"]
            ),
            6,
        )
        if completed
        else 0.0,
        "mean_completion_latency_seconds_virtual": round(
            statistics.fmean(
                item["completion_latency_seconds_virtual"]
                for item in events
                if item["completed"]
            ),
            6,
        )
        if completed
        else 0.0,
        "max_completion_latency_seconds_virtual": max(
            (
                item["completion_latency_seconds_virtual"]
                for item in events
                if item["completed"]
            ),
            default=0.0,
        ),
        "max_queue_wait_seconds_virtual": max(
            (
                item["total_queue_wait_seconds_virtual"]
                for item in events
                if item["completed"]
            ),
            default=0.0,
        ),
        "cap_violations": sum(size > batch_cap for size in batches),
        "cross_user_batches": cross_user_batch,
        "per_user_decisions": {
            user: sum(row["user_id"] == user for row in decisions)
            for user in sorted({row["user_id"] for row in decisions})
        },
        "measured_python_wall_seconds": None,
    }
    return FakeRun(result, events, effect_ledger, decisions, requests)


def run(
    *,
    rates: list[float] | None = None,
    replicates: int = DEFAULT_REPLICATES,
    duration_seconds: float = DEFAULT_DURATION_SECONDS,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    seed: int = 20261103,
    overload_rate: float | None = None,
    users: int = 1,
    root: Path | None = None,
) -> dict[str, Any]:
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGCHAIN_TRACING"] = "false"
    root = root or Path(__file__).resolve().parents[1]
    rates = list(DEFAULT_RATES if rates is None else rates)
    tested_rates = rates + ([overload_rate] if overload_rate is not None else [])
    _validate_config(tested_rates, replicates, duration_seconds, window_seconds, users)
    from src.server.services.autonomy import evals as eval_service

    production_batch_cap = eval_service.MAX_ROUTER_BATCH_EVALS
    rate_results: list[dict[str, Any]] = []
    raw: dict[str, list[dict[str, Any]]] = {
        "events": [],
        "effects": [],
        "decisions": [],
        "requests": [],
    }
    for rate_index, rate in enumerate(tested_rates):
        replicates_out = []
        for replicate in range(replicates):
            replicate_seed = seed + rate_index * 10_000 + replicate
            arrivals = _poisson_arrivals(rate, duration_seconds, replicate_seed, users)
            trace_digest = _trace_hash(arrivals)
            started = time.perf_counter()
            cpu_started = time.process_time()
            batched = asyncio.run(
                _run_service_trace(
                    arrivals,
                    rate=rate,
                    seed=replicate_seed,
                    window_seconds=window_seconds,
                    unbatched=False,
                )
            )
            batched_wall = time.perf_counter() - started
            batched_cpu = time.process_time() - cpu_started
            started = time.perf_counter()
            cpu_started = time.process_time()
            unbatched = asyncio.run(
                _run_service_trace(
                    arrivals,
                    rate=rate,
                    seed=replicate_seed,
                    window_seconds=window_seconds,
                    unbatched=True,
                )
            )
            unbatched_wall = time.perf_counter() - started
            unbatched_cpu = time.process_time() - cpu_started
            for label, run_result, wall, cpu in (
                ("batched", batched, batched_wall, batched_cpu),
                ("unbatched", unbatched, unbatched_wall, unbatched_cpu),
            ):
                run_result.result["measured_python_wall_seconds"] = round(wall, 9)
                run_result.result["arrival_trace_sha256"] = trace_digest
                run_result.result["measured_python_cpu_seconds"] = round(cpu, 9)
                final_completion = max(
                    (row["completion_seconds_virtual"] for row in run_result.decisions),
                    default=0.0,
                )
                last_arrival = max(
                    (row["arrival_seconds"] for row in arrivals), default=0.0
                )
                run_result.result.update(
                    {
                        "arrival_window_seconds": duration_seconds,
                        "last_arrival_seconds": round(last_arrival, 9),
                        "virtual_final_completion_seconds": round(final_completion, 9),
                        "virtual_drain_seconds_after_arrival_window": round(
                            max(0.0, final_completion - duration_seconds), 9
                        ),
                        "completed_during_arrival_window": sum(
                            row["batch_size"]
                            for row in run_result.decisions
                            if row["completion_seconds_virtual"] <= duration_seconds
                        ),
                    }
                )
                for collection in ("events", "effects", "decisions", "requests"):
                    for row in getattr(run_result, collection):
                        raw[collection].append(
                            {
                                **row,
                                "rate_per_minute": rate,
                                "seed": replicate_seed,
                                "mode": label,
                                "arrival_trace_sha256": trace_digest,
                            }
                        )
            replicates_out.append(
                {
                    "seed": replicate_seed,
                    "arrival_count": len(arrivals),
                    "arrival_trace_sha256": trace_digest,
                    "batched": batched.result,
                    "unbatched": unbatched.result,
                }
            )
        rate_results.append({"rate_per_minute": rate, "replicates": replicates_out})
    return {
        "kind": "offline-router-arrival-rate-stress",
        "evidence_boundary": {
            "production_functions_exercised": [
                "start_due_evals",
                "route_eval_effect",
                "dispatch_candidate_batch",
            ],
            "fake_boundaries": ["in-memory persistence", "ProjectDispatcher / model"],
            "virtual_metrics": [
                "model request latency",
                "inference time",
                "queue wait",
                "completion latency",
            ],
            "measured_metric": "Python wall time for fake service execution only",
            "live_model_or_backend_claim": False,
            "network_and_child_processes": "blocked before service imports",
            "budget_usd": 0,
        },
        "config": {
            "rates_per_minute": tested_rates,
            "replicates_per_rate": replicates,
            "duration_seconds": duration_seconds,
            "scheduler_window_seconds": window_seconds,
            "batch_cap": production_batch_cap,
            "base_seed": seed,
            "users_per_trace": users,
            "users_effective": users,
            "modeled_model_request_mean_seconds_at_zero_batch": 2.5,
            "model_requests_per_decision_distribution": {
                "1": 0.65,
                "2": 0.27,
                "3": 0.08,
            },
        },
        "source_hashes_sha256": source_hashes(root),
        "rates": rate_results,
        "raw_ledger_counts": {key: len(rows) for key, rows in raw.items()},
        "raw_ledgers": raw,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rates", type=float, nargs="+", default=list(DEFAULT_RATES))
    parser.add_argument("--replicates", type=int, default=DEFAULT_REPLICATES)
    parser.add_argument(
        "--duration-seconds", type=float, default=DEFAULT_DURATION_SECONDS
    )
    parser.add_argument("--window-seconds", type=float, default=DEFAULT_WINDOW_SECONDS)
    parser.add_argument("--seed", type=int, default=20261103)
    parser.add_argument("--overload-rate", type=float, default=None)
    parser.add_argument("--users", type=int, default=1)
    parser.add_argument("--budget-usd", type=float, default=0.0)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not math.isfinite(args.budget_usd) or args.budget_usd != 0:
        parser.error("offline harness budget is fixed at $0; --budget-usd must be 0")
    root = Path(__file__).resolve().parents[1]
    allowed_output = (root / "docs/experiments/superagent-router-stress").resolve()
    output_dir = (
        args.output_dir if args.output_dir.is_absolute() else root / args.output_dir
    )
    resolved_output = output_dir.resolve()
    if (
        resolved_output != allowed_output
        and allowed_output not in resolved_output.parents
    ):
        parser.error(
            "output directory must remain inside docs/experiments/superagent-router-stress"
        )
    result = run(
        rates=args.rates,
        replicates=args.replicates,
        duration_seconds=args.duration_seconds,
        window_seconds=args.window_seconds,
        seed=args.seed,
        overload_rate=args.overload_rate,
        users=args.users,
        root=root,
    )
    output_dir = resolved_output
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = result.pop("raw_ledgers")
    for name, rows in raw.items():
        path = output_dir / f"{name}.jsonl.gz"
        contents = "".join(
            json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rows
        )
        path.write_bytes(gzip.compress(contents.encode("utf-8"), mtime=0))
    result["raw_ledger_files"] = {name: f"{name}.jsonl.gz" for name in raw}
    result["raw_ledger_sha256"] = {
        name: _hash_file(output_dir / filename)
        for name, filename in result["raw_ledger_files"].items()
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    console_rates = []
    for rate_row in result["rates"]:
        modes = {}
        for mode in ("batched", "unbatched"):
            reps = [item[mode] for item in rate_row["replicates"]]
            modes[mode] = {
                "median_batch_size": round(
                    statistics.median(item["avg_batch_size"] for item in reps), 4
                ),
                "median_request_seconds_virtual": round(
                    statistics.median(
                        item["mean_request_seconds_virtual"] for item in reps
                    ),
                    4,
                ),
                "median_amortized_inference_seconds_per_candidate_virtual": round(
                    statistics.median(
                        item["amortized_inference_seconds_per_candidate_virtual"]
                        for item in reps
                    ),
                    4,
                ),
                "median_queue_wait_seconds_virtual": round(
                    statistics.median(
                        item["mean_queue_wait_seconds_virtual"] for item in reps
                    ),
                    4,
                ),
                "median_completion_latency_seconds_virtual": round(
                    statistics.median(
                        item["mean_completion_latency_seconds_virtual"] for item in reps
                    ),
                    4,
                ),
                "max_batch_size": max(item["max_batch_size"] for item in reps),
                "lost_or_duplicate": sum(
                    len(item["lost_trigger_ids"]) + item["duplicate_completion_count"]
                    for item in reps
                ),
            }
        console_rates.append(
            {"rate_per_minute": rate_row["rate_per_minute"], "modes": modes}
        )
    print(
        json.dumps(
            {"summary": str(summary_path), "rates": console_rates},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGCHAIN_TRACING"] = "false"
    _install_offline_guards()
    raise SystemExit(main())
