"""Contracts for the bounded offline §7.1 ablation harness."""

from __future__ import annotations

import asyncio
import json

import pytest

from scripts import superagent_ablation as ablation


def test_workload_has_article_shape_and_is_seeded():
    first = ablation.make_workload(7101)
    assert first == ablation.make_workload(7101)
    assert len(first) == 300
    assert {kind: sum(event["kind"] == kind for event in first) for kind in ablation.EVENT_COUNTS} == ablation.EVENT_COUNTS


def test_quantile_uses_linear_interpolation_and_null_for_empty():
    assert ablation.quantile([], 0.5) is None
    assert ablation.quantile([1.0, 2.0, 3.0, 4.0], 0.95) == pytest.approx(3.85)


@pytest.mark.asyncio
async def test_real_intake_exercises_pass_through_and_pending_trigger():
    events = ablation.make_workload(7101)
    live_event_ids = {event["event_id"] for event in events if event["kind"] in {"follow_up", "cancellation"}}
    pass_rows, pass_summary = await ablation._call_real_intake(
        events, condition="super_agent", live_event_ids=live_event_ids
    )
    off_rows, off_summary = await ablation._call_real_intake(events, condition="super_agent_without_pass_through")
    assert pass_summary["inputs"] == 70
    assert pass_summary["delivery_effects"] == 70
    assert pass_summary["pending_triggers"] == 120
    assert off_summary["inputs"] == 0
    assert off_summary["delivery_effects"] == 0
    assert off_summary["pending_triggers"] == 190
    assert sum(row["intake_path"] == "pass_through_input" for row in pass_rows) == 70
    assert all(row["intake_path"] != "pass_through_input" for row in off_rows)


def test_cli_rejects_nonzero_negative_or_nonfinite_budget(tmp_path):
    for value in ("1", "-1", "nan", "inf"):
        with pytest.raises(SystemExit):
            ablation.main(["--budget-usd", value, "--output", str(tmp_path / "out")])


def test_full_harness_writes_ledgers_and_matched_hashes(tmp_path):
    result = asyncio.run(ablation.run(seed=12, runs=5, events=300, tasks=20, output=tmp_path))
    assert result["matched_workload_check"] is True
    assert result["actual_task_elapsed_seconds"] is None
    assert result["token_usage"] is None
    assert result["aggregates"]["super_agent"]["tasks"] == 100
    assert result["aggregates"]["single_session_neutral"]["completed"] + result["aggregates"]["single_session_neutral"]["cancelled"] == 100
    assert (tmp_path / "events.jsonl").read_text().count("\n") == 6000
    assert (tmp_path / "tasks.jsonl").read_text().count("\n") == 400
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["source_sha256"]["src/server/services/autonomy/intake.py"]


def test_cancellation_releases_slot_and_starts_queued_task():
    pool = ablation._WorkerPool(slots=1)
    pool.submit("task-1", 0.0, 100.0, 0.0)
    pool.submit("task-2", 0.1, 20.0, 0.1)
    assert pool.rows["task-2"]["status"] == "queued"
    pool.cancel("task-1", 5.0)
    assert pool.rows["task-1"]["status"] == "cancelled"
    assert pool.rows["task-2"]["status"] == "running"
    assert pool.rows["task-2"]["worker_start_virtual_seconds"] == 5.0


def test_busy_main_session_delays_delegation_and_results_follow_arrivals():
    events = [
        {"event_id": f"e{i}", "arrival_virtual_seconds": float(i), "kind": "work", "task_id": f"task-{i}", "thread_key": f"thread-task-{i}"}
        for i in range(1, 6)
    ]
    durations = {f"task-{i}": 100.0 for i in range(1, 6)}
    rows, _, _, _ = ablation._simulate_tasks(events, durations, "single_session_delegation_first")
    fifth = next(row for row in rows if row["task_id"] == "task-5")
    assert fifth["delegated_by_fixed_policy"] is True
    assert fifth["worker_start_virtual_seconds"] >= 400.0
    assert all(row["result_virtual_seconds"] >= row["arrival_virtual_seconds"] for row in rows)


def test_offline_guards_deny_network_and_child_process_but_restore():
    originals = ablation._install_offline_guards()
    try:
        with pytest.raises(RuntimeError, match="offline ablation guard"):
            ablation.socket.socket(ablation.socket.AF_INET)
        with pytest.raises(RuntimeError, match="offline ablation guard"):
            ablation.subprocess.Popen(["anything"])
        with pytest.raises(RuntimeError, match="offline ablation guard"):
            ablation.os.fork()
        allowed = ablation.socket.socket(ablation.socket.AF_UNIX)
        with pytest.raises(RuntimeError, match="offline ablation guard"):
            allowed.connect("/nonexistent-offline-test")
        with pytest.raises(RuntimeError, match="offline ablation guard"):
            allowed.connect_ex("/nonexistent-offline-test")
        allowed.close()
        pair = ablation.socket.socketpair()
        for endpoint in pair:
            endpoint.close()
    finally:
        ablation._restore_guards(originals)
