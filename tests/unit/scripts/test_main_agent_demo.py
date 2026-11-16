"""The Main Agent demo stays offline and exercises the CLI entry point."""

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_offline_demo_uses_real_service_and_concurrent_workers():
    result = subprocess.run(
        [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/main_agent_demo.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    payload = json.loads(result.stdout)

    assert payload["mode"] == "offline SQLite + mock worker callbacks"
    assert payload["completion_path"] == "scheduler outbox advance effect"
    assert payload["max_concurrent_workers"] >= 2
    assert len(payload["runs"]) == 2
    assert all(run["status"] == "completed" for run in payload["runs"])
    assert all(run["reply"]["content"].startswith("Completed Ticket") for run in payload["runs"])

    dispatches = payload["worker_execution_order"]
    run_ids = {run["run_id"] for run in payload["runs"]}
    assert {row["run_id"] for row in dispatches} == run_ids
    assert {row["daemon_id"] for row in dispatches} == {"demo-daemon"}
    for run_id in run_ids:
        order = [row["task_id"] for row in dispatches if row["run_id"] == run_id]
        assert order.count("join") == 1
        assert max(order.index("inspect"), order.index("count")) < order.index("join")
