#!/usr/bin/env python3
"""Synthetic scheduling model for the Super-agent pressure experiment.

This is NOT a live LLM benchmark. It replays the article's workload shape
(~300 messages, ~20 work requests) through a deterministic model of the
scheduler: a 2-second Router pass, a Router service time, a batch of accepted
Tickets, and a bounded pool of Worker slots. The output shows how batching and
parallel Workers change completion p50/p95 versus one sequential Session.

Run:
    .venv/bin/python scripts/superagent_pressure.py --json /tmp/pressure.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from typing import Any


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * min(1.0, max(0.0, q))
    low = int(position)
    high = min(len(ordered) - 1, low + 1)
    fraction = position - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def _arrivals(messages: int, work: int, duration: float, rng: random.Random) -> list[float]:
    times = sorted(rng.uniform(0, max(1.0, duration)) for _ in range(work))
    return times


def _durations(work: int, rng: random.Random, mean: float = 60.0) -> list[float]:
    sigma = 0.5
    mu = math.log(mean) - (sigma * sigma) / 2
    return [max(1.0, rng.lognormvariate(mu, sigma)) for _ in range(work)]


def simulate_superagent(
    arrivals: list[float],
    durations: list[float],
    *,
    router_interval: float = 2.0,
    router_service: float = 1.0,
    workers: int = 2,
) -> dict[str, Any]:
    """Router batches on its interval; Worker slots run accepted Tickets."""
    if len(arrivals) != len(durations):
        raise ValueError("arrivals and durations differ in length")
    queue: list[tuple[float, float]] = []
    completions: list[float] = []
    batches: list[int] = []
    worker_free = [0.0] * max(1, workers)
    router_free = 0.0
    next_router = 0.0
    index = 0
    t = 0.0
    horizon = max(arrivals + [0.0]) + sum(durations) * 2 + 120
    while index < len(arrivals) or queue:
        t = min(horizon, t)
        if index < len(arrivals) and arrivals[index] <= t:
            while index < len(arrivals) and arrivals[index] <= t:
                queue.append((arrivals[index], durations[index]))
                index += 1
        if not queue:
            t = arrivals[index] if index < len(arrivals) else t + 1
            continue
        if t < max(router_free, next_router):
            t = max(router_free, next_router)
            continue
        batch = queue
        queue = []
        batches.append(len(batch))
        start = max(t, router_free)
        accepted_at = start + router_service
        router_free = accepted_at
        next_router = accepted_at + router_interval
        for arrival, duration in batch:
            slot = min(range(len(worker_free)), key=worker_free.__getitem__)
            w_start = max(accepted_at, worker_free[slot])
            finish = w_start + duration
            worker_free[slot] = finish
            completions.append(finish - arrival)
        t = accepted_at
    return {
        "completed": len(completions),
        "p50_seconds": round(quantile(completions, 0.50), 1),
        "p95_seconds": round(quantile(completions, 0.95), 1),
        "mean_seconds": round(statistics.fmean(completions), 1) if completions else 0.0,
        "avg_batch": round(statistics.fmean(batches), 2) if batches else 0.0,
        "max_batch": max(batches, default=0),
        "batches": len(batches),
    }


def simulate_single_session(arrivals: list[float], durations: list[float]) -> dict[str, Any]:
    """One session runs the work requests one after another."""
    completions: list[float] = []
    previous_finish = 0.0
    for arrival, duration in zip(arrivals, durations, strict=True):
        start = max(arrival, previous_finish)
        previous_finish = start + duration
        completions.append(previous_finish - arrival)
    return {
        "completed": len(completions),
        "p50_seconds": round(quantile(completions, 0.50), 1),
        "p95_seconds": round(quantile(completions, 0.95), 1),
        "mean_seconds": round(statistics.fmean(completions), 1) if completions else 0.0,
    }


def run(*, messages: int, work: int, duration: float, runs: int, seed: int) -> dict[str, Any]:
    superagent_runs: list[dict[str, Any]] = []
    single_runs: list[dict[str, Any]] = []
    for run_index in range(runs):
        rng = random.Random(seed + run_index)
        arrivals = _arrivals(messages, work, duration, rng)
        durations = _durations(work, rng)
        superagent_runs.append(simulate_superagent(arrivals, durations))
        single_runs.append(simulate_single_session(arrivals, durations))
    return {
        "kind": "synthetic-scheduling-model",
        "note": "Not a live model benchmark; same arrivals/durations feed both arms.",
        "config": {
            "messages": messages,
            "work_requests": work,
            "duration_seconds": duration,
            "runs": runs,
            "seed": seed,
            "router_interval_seconds": 2.0,
            "router_service_seconds": 1.0,
            "worker_slots": 2,
        },
        "superagent": {
            "p50_seconds": round(statistics.median(r["p50_seconds"] for r in superagent_runs), 1),
            "p95_seconds": round(statistics.median(r["p95_seconds"] for r in superagent_runs), 1),
            "avg_batch": round(statistics.fmean(r["avg_batch"] for r in superagent_runs), 2),
            "max_batch": max(r["max_batch"] for r in superagent_runs),
            "runs": superagent_runs,
        },
        "single_session": {
            "p50_seconds": round(statistics.median(r["p50_seconds"] for r in single_runs), 1),
            "p95_seconds": round(statistics.median(r["p95_seconds"] for r in single_runs), 1),
            "runs": single_runs,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=300)
    parser.add_argument("--work", type=int, default=20)
    parser.add_argument("--duration", type=float, default=300.0)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", type=str, default="")
    args = parser.parse_args(argv)
    result = run(
        messages=args.messages,
        work=args.work,
        duration=args.duration,
        runs=args.runs,
        seed=args.seed,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json:
        from pathlib import Path

        Path(args.json).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
