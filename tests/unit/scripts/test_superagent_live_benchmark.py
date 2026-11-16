"""Live benchmark helpers: quantiles, summaries and the terminal poll loop."""

from __future__ import annotations


from scripts import superagent_live_benchmark as bench


def test_quantile_interpolates():
    assert bench.quantile([], 0.5) == 0.0
    assert bench.quantile([10.0], 0.95) == 10.0
    assert bench.quantile([10.0, 20.0], 0.5) == 15.0


def test_arm_summary_counts_only_completed_requests():
    rows = [
        {"status": "completed", "seconds": 10.0},
        {"status": "completed", "seconds": 20.0},
        {"status": "timeout", "seconds": 180.0},
    ]
    summary = bench._arm_summary(rows)
    assert summary["completed"] == 2
    assert summary["failed"] == 1
    assert summary["p50_seconds"] == 15.0
    assert summary["p95_seconds"] == 19.5


def test_wait_terminal_stops_on_a_terminal_status(monkeypatch):
    calls = []

    class FakeClient:
        def ticket(self, ticket_id):
            calls.append(ticket_id)
            return {"status": "completed" if len(calls) > 1 else "running"}

    monkeypatch.setattr(bench.time, "sleep", lambda _: None)
    status, error = bench.wait_terminal(
        FakeClient(), "ticket-1", timeout=5, poll=0
    )
    assert status == "completed"
    assert error == ""
