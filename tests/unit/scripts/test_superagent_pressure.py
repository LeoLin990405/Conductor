"""The pressure model is deterministic and separates batching from serial work."""

from __future__ import annotations

import json

from scripts import superagent_pressure as pressure


def test_quantile_handles_empty_and_small_samples():
    assert pressure.quantile([], 0.5) == 0.0
    assert pressure.quantile([10.0], 0.95) == 10.0
    assert pressure.quantile([1.0, 2.0, 3.0], 0.5) == 2.0


def test_two_arrivals_in_one_router_pass_are_batched():
    result = pressure.simulate_superagent([0.0, 0.0], [10.0, 10.0])
    assert result["completed"] == 2
    assert result["avg_batch"] == 2.0
    assert result["max_batch"] == 2


def test_single_session_is_serial_where_superagent_is_parallel():
    arrivals = [0.0, 0.0]
    durations = [10.0, 10.0]
    agent = pressure.simulate_superagent(arrivals, durations)
    single = pressure.simulate_single_session(arrivals, durations)
    assert agent["p50_seconds"] < single["p50_seconds"]


def test_cli_writes_the_json_artifact(tmp_path):
    out = tmp_path / "pressure.json"
    assert pressure.main(["--messages", "10", "--work", "3", "--runs", "1", "--json", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["kind"] == "synthetic-scheduling-model"
    assert data["config"]["work_requests"] == 3
    assert "superagent" in data and "single_session" in data
