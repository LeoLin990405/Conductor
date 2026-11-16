"""Offline stress run exercises the production Eval scheduler and route path."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from scripts import superagent_router_stress as stress

ROOT = Path(__file__).resolve().parents[3]


def test_seeded_arrivals_are_reproducible_and_matched_for_baseline():
    first = stress._poisson_arrivals(40, 120, 91)
    again = stress._poisson_arrivals(40, 120, 91)
    assert first == again
    assert stress._trace_hash(first) == stress._trace_hash(again)


def test_amortized_inference_weights_candidates_across_unequal_batches():
    decisions = [
        {"batch_size": 8, "inference_seconds_virtual": 8.0},
        {"batch_size": 1, "inference_seconds_virtual": 4.0},
    ]
    assert stress._amortized_inference_per_candidate(decisions) == pytest.approx(12 / 9)


def test_service_harness_conserves_arrivals_and_enforces_router_cap():
    result = stress.run(
        rates=[40],
        replicates=2,
        duration_seconds=120,
        window_seconds=4,
        seed=19,
        root=ROOT,
    )
    reps = result["rates"][0]["replicates"]
    assert len(reps) == 2
    for replicate in reps:
        batched = replicate["batched"]
        baseline = replicate["unbatched"]
        assert batched["arrival_trace_sha256"] == baseline["arrival_trace_sha256"]
        for arm in (batched, baseline):
            assert arm["arrival_count"] == arm["completed_count"]
            assert arm["lost_trigger_ids"] == []
            assert arm["duplicate_completion_count"] == 0
            assert arm["cap_violations"] == 0
            assert arm["max_batch_size"] <= arm["batch_cap"]
            assert not arm["cross_user_batches"]
        assert batched["avg_batch_size"] > baseline["avg_batch_size"]
        assert batched["request_count"] >= sum(
            batched["batch_size_distribution"].values()
        )


def test_two_router_users_keep_independent_batches_and_capacity():
    result = stress.run(
        rates=[40],
        replicates=1,
        duration_seconds=120,
        window_seconds=4,
        seed=8,
        users=2,
        root=ROOT,
    )
    batched = result["rates"][0]["replicates"][0]["batched"]
    assert set(batched["per_user_decisions"]) == {"user-1", "user-2"}
    assert batched["cross_user_batches"] is False
    assert batched["completed_count"] == batched["arrival_count"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rates": []},
        {"rates": [0]},
        {"rates": [math.nan]},
        {"rates": [1001]},
        {"rates": [1000], "duration_seconds": 1800, "replicates": 20},
        {"replicates": 0},
        {"duration_seconds": -1},
        {"window_seconds": 0},
        {"users": 9},
    ],
)
def test_invalid_or_unbounded_configuration_is_rejected(kwargs):
    config = {
        "rates": [1],
        "replicates": 1,
        "duration_seconds": 10,
        "window_seconds": 1,
        "users": 1,
    }
    config.update(kwargs)
    with pytest.raises(ValueError):
        stress._validate_config(
            config["rates"],
            config["replicates"],
            config["duration_seconds"],
            config["window_seconds"],
            config["users"],
        )
