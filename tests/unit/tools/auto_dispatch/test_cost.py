"""N30 v1 cost ledger and read-time metrics (``src/tools/auto_dispatch/cost.py``).

Everything is offline: CodexBar snapshots are written under ``tmp_path`` and
the store is an in-memory fake. Acceptance items 1, 5 and 6 of
``docs/plans/next/n30-line-cost.md``, plus a pure-function version of the GLM
1302 scenario (item 2), the no-output-text rule (item 4) and ``CostStats``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.tools.auto_dispatch import cost
from src.tools.auto_dispatch.types import (
    Dispatch,
    DispatchState,
    Line,
    LineStatus,
    Outcome,
)

T0 = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _merged_gates(*line_ids: str) -> dict:
    return {
        "schema_version": "dispatch_gates_v1",
        "lines": {},
        "integration": {"merged_lines": list(line_ids)},
    }


def _attempt(gates, line_id, run_id, model, *, kind="work", fix_round=0, start=0,
             secs=60, result="ok", failure_class=None, reason_code=None, attempt=1):
    cost.record_attempt_start(
        gates, line_id, run_id=run_id, attempt=attempt, model=model, kind=kind,
        fix_round=fix_round, started_at=T0 + timedelta(seconds=start),
    )
    return cost.record_attempt_end(
        gates, line_id, run_id=run_id, ended_at=T0 + timedelta(seconds=start + secs),
        result=result, failure_class=failure_class, reason_code=reason_code,
        tokens_est=secs * 10,
    )


# --------------------------------------------------------------------------
# Acceptance 1: the ledger


def test_same_run_id_twice_keeps_one_entry():
    gates: dict = {}
    for _ in range(2):
        cost.record_attempt_start(
            gates, "line-0", run_id="r1", attempt=1, model="kimi", kind="work",
            fix_round=0, started_at=T0,
        )
    cost.record_attempt_end(gates, "line-0", run_id="r1", ended_at=T0 + timedelta(seconds=90), result="ok")
    cost.record_attempt_end(gates, "line-0", run_id="r1", ended_at=T0 + timedelta(seconds=95), result="ok")
    entries = gates["cost"]["lines"]["line-0"]
    assert len(entries) == 1
    assert entries[0]["wall_secs"] == 95
    assert gates["cost"]["v"] == cost.LEDGER_VERSION
    assert entries[0]["started_at"] == "2026-11-14T09:00:00Z"


def test_65th_entry_drops_oldest_and_counts_truncated():
    gates: dict = {}
    for index in range(cost.MAX_ATTEMPTS_PER_LINE + 1):
        cost.record_attempt_start(
            gates, "line-0", run_id=f"r{index}", attempt=index, model="glm",
            kind="work", fix_round=0, started_at=T0,
        )
    entries = gates["cost"]["lines"]["line-0"]
    assert len(entries) == cost.MAX_ATTEMPTS_PER_LINE
    assert entries[0]["run_id"] == "r1"
    assert entries[-1]["run_id"] == f"r{cost.MAX_ATTEMPTS_PER_LINE}"
    assert gates["cost"]["truncated"] == {"line-0": 1}


def test_token_basis_cli_vs_estimate_and_tokens_est_always_set():
    gates: dict = {}
    cli = cost.record_attempt_end(
        gates, "l", run_id="a", ended_at=T0, result="ok", tokens_in=100,
        tokens_out=20, tokens_est=900, model="glm",
    )
    est = cost.record_attempt_end(
        gates, "l", run_id="b", ended_at=T0, result="ok", tokens_est=700, model="kimi",
    )
    assert cli["token_basis"] == "cli"
    assert (cli["tokens_in"], cli["tokens_out"], cli["tokens_est"]) == (100, 20, 900)
    assert est["token_basis"] == "estimate"
    assert est["tokens_in"] is None and est["tokens_out"] is None
    assert est["tokens_est"] == 700


def test_end_without_start_is_recorded_with_zero_wall():
    gates: dict = {}
    entry = cost.record_attempt_end(
        gates, "line-1", run_id="r9", ended_at=T0, result="failed",
        failure_class="auth_failed", reason_code="MISSING_CREDENTIAL: no API key",
        model="deepseek-dsh", attempt=1, kind="work", fix_round=0,
    )
    assert entry["started_at"] is None
    assert entry["wall_secs"] == 0
    assert entry["model"] == "deepseek"
    assert entry["reason_code"] == "missing_credential"
    assert entry["failure_class"] == "auth_failed"


def test_reason_code_free_text_never_stored():
    sentinel = "SENTINEL-7f3a needle text"
    gates: dict = {}
    cost.record_attempt_end(
        gates, "l", run_id="r", ended_at=T0, result="failed",
        failure_class="crash", reason_code=f"worker_crashed {sentinel} api_key=sk-abc",
        model="kimi",
    )
    dispatch = Dispatch(
        id="d", goal="g", user_id="u", created_at="",
        lines=[Line(id="l", name="l", task="t", assigned_model="kimi", status=LineStatus.FAILED)],
        gates=gates,
    )
    blob = json.dumps(gates) + json.dumps(cost.dispatch_cost_report(dispatch))
    assert "sentinel" not in blob.lower()
    assert "needle" not in blob
    assert "sk-abc" not in blob
    assert gates["cost"]["lines"]["l"][0]["reason_code"] == "worker_crashed"


def test_reason_code_normalisation():
    assert cost.normalize_reason_code("Worker_Rate_Limited") == "worker_rate_limited"
    assert cost.normalize_reason_code("x" * 100) == "x" * 64
    assert cost.normalize_reason_code("   ") is None
    assert cost.normalize_reason_code("!!! nothing") is None
    assert cost.normalize_reason_code(None) is None


def test_failure_class_outside_vocabulary_is_unknown():
    gates: dict = {}
    entry = cost.record_attempt_end(gates, "l", run_id="r", ended_at=T0, result="failed",
                                    failure_class="made up text", model="kimi")
    assert entry["failure_class"] == "unknown"


def test_ledger_enabled_switch():
    assert cost.ledger_enabled({}) is True
    assert cost.ledger_enabled({"CONDUCTOR_DISPATCH_COST_LEDGER": "on"}) is True
    assert cost.ledger_enabled({"CONDUCTOR_DISPATCH_COST_LEDGER": "off"}) is False
    assert cost.ledger_enabled({"CONDUCTOR_DISPATCH_COST_LEDGER": "OFF"}) is False


# --------------------------------------------------------------------------
# Acceptance 5: CodexBar readings and their delta


def _snapshot(path: Path, *, used: float, updated: datetime, resets: datetime,
              status: str = "ok") -> Path:
    payload = {
        "updatedAt": _iso(updated),
        "providers": [
            {
                "provider": "zai", "kind": "quota", "status": status,
                "updated_at": _iso(updated),
                "windows": [
                    {"used_percent": used, "role": "primary", "resets_at_utc": _iso(resets)},
                    {"used_percent": 90, "role": "secondary", "resets_at_utc": _iso(resets)},
                ],
            },
            {"provider": "deepseek", "kind": "balance", "status": "ok",
             "updated_at": _iso(updated), "balance": {"amount": 12.5, "currency": "CNY"}},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_read_quota_primary_window(tmp_path):
    reset = T0 + timedelta(hours=3)
    snap = _snapshot(tmp_path / "s.json", used=41, updated=T0, resets=reset)
    reading = cost.read_quota("glm", path=snap, now=T0 + timedelta(seconds=30))
    assert reading == {
        "provider": "zai", "used_pct": 41.0, "resets_at": _iso(reset),
        "updated_at": _iso(T0), "read_at": _iso(T0 + timedelta(seconds=30)),
    }
    balance = cost.read_quota("deepseek", path=snap, now=T0)
    assert balance["provider"] == "deepseek" and balance["balance"] is True
    assert "12.5" not in json.dumps(balance)
    assert cost.read_quota("nobody", path=snap, now=T0) is None


def test_read_quota_missing_or_bad_file_is_none(tmp_path):
    assert cost.read_quota("glm", path=tmp_path / "absent.json", now=T0) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cost.read_quota("glm", path=bad, now=T0) is None
    unknown = _snapshot(tmp_path / "u.json", used=1, updated=T0, resets=T0 + timedelta(hours=1),
                        status="unknown")
    assert cost.read_quota("glm", path=unknown, now=T0) is None


def test_quota_delta_fresh_same_window_is_ok(tmp_path):
    reset = T0 + timedelta(hours=3)
    before = cost.read_quota("glm", now=T0 + timedelta(seconds=10),
                             path=_snapshot(tmp_path / "a.json", used=41, updated=T0, resets=reset))
    later = T0 + timedelta(seconds=300)
    after = cost.read_quota("glm", now=later + timedelta(seconds=10),
                            path=_snapshot(tmp_path / "b.json", used=44, updated=later, resets=reset))
    delta = cost.quota_delta(before, after)
    assert delta == {"provider": "zai", "before_pct": 41.0, "after_pct": 44.0,
                     "delta_pct": 3.0, "note": "ok"}


def test_quota_delta_window_reset(tmp_path):
    before = cost.read_quota("glm", now=T0, path=_snapshot(
        tmp_path / "a.json", used=80, updated=T0, resets=T0 + timedelta(minutes=5)))
    later = T0 + timedelta(minutes=6)
    after = cost.read_quota("glm", now=later, path=_snapshot(
        tmp_path / "b.json", used=2, updated=later, resets=T0 + timedelta(hours=5)))
    delta = cost.quota_delta(before, after)
    assert delta["note"] == "window_reset" and delta["delta_pct"] is None


def test_quota_delta_stale(tmp_path):
    reset = T0 + timedelta(hours=3)
    snap = _snapshot(tmp_path / "a.json", used=41, updated=T0, resets=reset)
    before = cost.read_quota("glm", path=snap, now=T0 + timedelta(seconds=901))
    after = cost.read_quota("glm", path=snap, now=T0 + timedelta(seconds=1200))
    delta = cost.quota_delta(before, after)
    assert delta["note"] == "stale" and delta["delta_pct"] is None


def test_quota_delta_unchanged_and_missing(tmp_path):
    snap = _snapshot(tmp_path / "a.json", used=41, updated=T0, resets=T0 + timedelta(hours=3))
    one = cost.read_quota("glm", path=snap, now=T0 + timedelta(seconds=5))
    two = cost.read_quota("glm", path=snap, now=T0 + timedelta(seconds=50))
    assert cost.quota_delta(one, two)["note"] == "unchanged"
    assert cost.quota_delta(None, None) is None
    assert cost.quota_delta(one, None)["note"] == "no_snapshot"
    balance = cost.read_quota("deepseek", path=snap, now=T0)
    assert cost.quota_delta(balance, balance)["note"] == "balance_provider"


def test_missing_snapshot_gives_null_quota_in_ledger(tmp_path):
    gates: dict = {}
    before = cost.read_quota("glm", path=tmp_path / "none.json", now=T0)
    cost.record_attempt_start(gates, "l", run_id="r", attempt=1, model="glm", kind="work",
                              fix_round=0, started_at=T0, quota_before=before)
    entry = cost.record_attempt_end(gates, "l", run_id="r", ended_at=T0 + timedelta(seconds=5),
                                    result="ok", quota_after=None)
    assert entry["quota"] is None
    assert "quota_before" not in entry


def test_quota_before_folded_into_delta(tmp_path):
    reset = T0 + timedelta(hours=3)
    before = cost.read_quota("glm", now=T0, path=_snapshot(
        tmp_path / "a.json", used=10, updated=T0, resets=reset))
    later = T0 + timedelta(seconds=400)
    after = cost.read_quota("glm", now=later, path=_snapshot(
        tmp_path / "b.json", used=12.5, updated=later, resets=reset))
    gates: dict = {}
    cost.record_attempt_start(gates, "l", run_id="r", attempt=1, model="glm", kind="work",
                              fix_round=0, started_at=T0, quota_before=before)
    entry = cost.record_attempt_end(gates, "l", run_id="r", ended_at=later, result="ok",
                                    quota_after=after)
    assert entry["quota"]["delta_pct"] == 2.5
    assert "quota_before" not in gates["cost"]["lines"]["l"][0]


# --------------------------------------------------------------------------
# Line outcome and the 1302 scenario (pure-function version of acceptance 2)


def _scenario_1302() -> Dispatch:
    gates = _merged_gates("line-0")
    for index in range(3):
        _attempt(gates, "line-0", f"glm-{index}", "glm", start=index * 100, secs=40,
                 result="failed", failure_class="rate_limited",
                 reason_code="worker_rate_limited", attempt=index + 1)
    _attempt(gates, "line-0", "kimi-0", "kimi", start=400, secs=600, attempt=4)
    _attempt(gates, "line-0", "kimi-fix", "kimi", kind="fix", fix_round=1, start=1100,
             secs=300, attempt=4)
    line = Line(id="line-0", name="line-0", task="t", assigned_model="kimi",
                status=LineStatus.PASSED)
    return Dispatch(id="d1302", goal="g", user_id="u", created_at="", lines=[line],
                    state=DispatchState.COMPLETE, gates=gates)


def test_1302_scenario_report():
    report = cost.dispatch_cost_report(_scenario_1302())
    assert report["basis"] == "ledger"
    assert report["counts"] == {"lines": 1, "merged": 1, "passed": 0, "blocked": 0, "failed": 0,
                                "open": 0}
    rows = report["attempts"]["line-0"]
    glm = [r for r in rows if r["model"] == "glm"]
    kimi = [r for r in rows if r["model"] == "kimi"]
    assert [(r["use"], r["waste_class"]) for r in glm] == [("waste", "rate_limited")] * 3
    assert sorted(r["use"] for r in kimi) == ["rework", "useful"]
    by_model = {row["model"]: row for row in report["by_model"]}
    assert by_model["glm"]["cost_per_merged_line_wall_secs"] is None
    assert by_model["glm"]["merged"] == 0 and by_model["glm"]["waste_share"] == 1.0
    assert by_model["kimi"]["cost_per_merged_line_wall_secs"] == 900.0
    assert by_model["kimi"]["merge_yield"] == 1.0
    waste = {row["class"]: row for row in report["waste_by_class"]}
    assert waste["rate_limited"]["attempts"] == 3
    assert waste["rate_limited"]["wall_secs"] == 120
    assert waste["rate_limited"]["by_model"] == {"glm": 3}
    assert waste["rate_limited"]["infrastructure"] is False
    line_row = report["lines"][0]
    assert line_row["outcome"] == "merged" and line_row["author"] == "kimi"
    assert line_row["attempts"] == 5 and line_row["fix_rounds"] == 1
    assert line_row["waste"] == [{"model": "glm", "class": "rate_limited", "attempts": 3,
                                  "wall_secs": 120}]
    assert report["fix_rounds"] == {"total": 1, "per_merged_line": 1.0}
    assert report["quota_note"] == cost.QUOTA_NOTE
    json.dumps(report)


def test_summary_has_no_attempts():
    summary = cost.dispatch_cost_summary(_scenario_1302())
    assert set(summary) == {"v", "basis", "counts", "lines"}
    assert summary["v"] == 1


def test_line_outcome_order():
    gates = {"integration": {"merged_lines": ["a"]},
             "lines": {"b": {"phase": "merged"}, "c": {"machine": {"stage": "merged"}}}}
    dispatch = Dispatch(id="d", goal="g", user_id="u", created_at="", gates=gates)
    def line(lid, status):
        return Line(id=lid, name=lid, task="t", status=status)
    assert cost.line_outcome(dispatch, line("a", LineStatus.BLOCKED)) == "merged"
    assert cost.line_outcome(dispatch, line("b", LineStatus.FAILED)) == "merged"
    assert cost.line_outcome(dispatch, line("c", LineStatus.RUNNING)) == "merged"
    assert cost.line_outcome(dispatch, line("d", LineStatus.BLOCKED)) == "blocked"
    assert cost.line_outcome(dispatch, line("e", LineStatus.FAILED)) == "failed"
    assert cost.line_outcome(dispatch, line("f", LineStatus.PASSED)) == "open"


def test_infra_waste_is_flagged_and_aborted_is_abandoned():
    gates: dict = {}
    _attempt(gates, "l1", "r1", "deepseek", secs=0, result="failed",
             failure_class="auth_failed", reason_code="missing_credential")
    _attempt(gates, "l2", "r2", "grok", secs=30)
    dispatch = Dispatch(
        id="d", goal="g", user_id="u", created_at="", state=DispatchState.ABORT, gates=gates,
        lines=[Line(id="l1", name="l1", task="t", assigned_model="deepseek",
                    status=LineStatus.BLOCKED),
               Line(id="l2", name="l2", task="t", assigned_model="grok")],
    )
    waste = {row["class"]: row for row in cost.dispatch_cost_report(dispatch)["waste_by_class"]}
    assert waste["auth_failed"]["infrastructure"] is True
    assert waste["abandoned"]["infrastructure"] is False


# --------------------------------------------------------------------------
# Acceptance 6: legacy dispatches


def test_legacy_report_counts_by_class():
    gates = _merged_gates("line-0")
    gates["worker_recovery"] = {
        "line-0": {"events": [
            {"failure_class": "rate_limited", "reason": "worker_rate_limited",
             "action": "retry_same", "attempt": i + 1, "author": "glm", "run_id": f"g{i}",
             "epoch": 1}
            for i in range(3)
        ]},
        "line-1": {"events": [
            {"failure_class": "auth_failed", "reason": "MISSING_CREDENTIAL: no API key",
             "action": "park", "attempt": 1, "author": "deepseek-dsh", "run_id": "d0",
             "epoch": 1},
        ]},
    }
    dispatch = Dispatch(
        id="old", goal="g", user_id="u", created_at="", gates=gates,
        lines=[Line(id="line-0", name="line-0", task="t", assigned_model="kimi",
                    status=LineStatus.PASSED),
               Line(id="line-1", name="line-1", task="t", assigned_model="deepseek-dsh",
                    status=LineStatus.BLOCKED)],
        outcomes={"line-0": Outcome(line_id="line-0", model="kimi", duration_secs=600,
                                    tokens=41000, passed=True)},
    )
    report = cost.dispatch_cost_report(dispatch)
    assert report["basis"] == "legacy"
    waste = {row["class"]: row for row in report["waste_by_class"]}
    assert waste["rate_limited"]["attempts"] == 3
    assert waste["rate_limited"]["wall_secs"] is None
    assert waste["rate_limited"]["tokens_est"] is None
    assert waste["auth_failed"]["by_model"] == {"deepseek": 1}
    for row in report["lines"]:
        assert row["wall_secs"] is None and row["tokens_cli"] is None and row["tokens_est"] is None
    by_model = {row["model"]: row for row in report["by_model"]}
    assert by_model["kimi"]["merged"] == 1
    assert by_model["kimi"]["spent_wall_secs"] is None
    assert by_model["kimi"]["cost_per_merged_line_wall_secs"] is None
    assert "no api key" not in json.dumps(report).lower()
    agg = cost.aggregate_cost_report([dispatch, _scenario_1302()], limit=50)
    assert agg["basis"] == {"ledger": 1, "legacy": 1}
    assert agg["dispatches"] == 2
    assert {r["class"]: r["attempts"] for r in agg["waste_by_class"]}["rate_limited"] == 6


def test_legacy_dispatch_from_dict_without_gates():
    raw = {"id": "x", "lines": [{"id": "l", "status": "FAILED", "assigned_model": "grok"}]}
    report = cost.dispatch_cost_report(raw)
    assert report["basis"] == "legacy"
    assert report["counts"]["failed"] == 1


def test_aggregate_limit():
    dispatches = [_scenario_1302() for _ in range(3)]
    agg = cost.aggregate_cost_report(dispatches, limit=2)
    assert agg["limit"] == 2 and agg["dispatches"] == 2
    kimi = {r["model"]: r for r in agg["by_model"]}["kimi"]
    assert kimi["merged"] == 2 and kimi["cost_per_merged_line_wall_secs"] == 900.0


# --------------------------------------------------------------------------
# cost_per_merged_line_wall and CostStats


def _infra_dispatch() -> Dispatch:
    """kimi merges a line after 500 s of auth_failed waste of its own."""
    gates = _merged_gates("l")
    _attempt(gates, "l", "bad", "kimi", secs=500, result="failed", failure_class="auth_failed")
    _attempt(gates, "l", "good", "kimi", start=600, secs=100)
    return Dispatch(id="i", goal="g", user_id="u", created_at="", gates=gates,
                    lines=[Line(id="l", name="l", task="t", assigned_model="kimi",
                                status=LineStatus.PASSED)])


def test_cost_per_merged_line_excludes_infra():
    assert cost.cost_per_merged_line_wall([_infra_dispatch()])["kimi"] == (100.0, 1)
    assert cost.cost_per_merged_line_wall([_infra_dispatch()], exclude_infra=False)["kimi"] == (600.0, 1)
    table = cost.cost_per_merged_line_wall([_scenario_1302()])
    assert table["glm"] == (None, 0)
    assert table["kimi"] == (900.0, 1)


class _FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def list(self, *, limit=50):
        self.calls += 1
        return list(self.rows)[:limit]


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_cost_stats_caches_one_list_per_ttl():
    store = _FakeStore([_scenario_1302() for _ in range(3)])
    clock = _Clock()
    stats = cost.CostStats(store, min_merged=3, ttl_seconds=60.0, clock=clock)

    async def run():
        first = await stats.cost_per_merged_line("kimi")
        await stats.cost_per_merged_line("glm")
        clock.now += 59
        await stats.cost_per_merged_line("kimi")
        assert store.calls == 1
        clock.now += 2
        await stats.cost_per_merged_line("kimi")
        assert store.calls == 2
        return first

    assert asyncio.run(run()) == 900.0


def test_cost_stats_min_merged_gate_and_glm():
    store = _FakeStore([_scenario_1302() for _ in range(2)])
    stats = cost.CostStats(store, min_merged=3, clock=_Clock())

    async def run():
        return (await stats.cost_per_merged_line("kimi"),
                await stats.cost_per_merged_line("glm"),
                await stats.cost_per_merged_line("zai"))

    assert asyncio.run(run()) == (None, None, None)


def test_cost_stats_excludes_infra():
    store = _FakeStore([_infra_dispatch()])
    stats = cost.CostStats(store, min_merged=1, clock=_Clock())
    assert asyncio.run(stats.cost_per_merged_line("kimi")) == 100.0


def test_cost_stats_store_failure_is_none():
    class Broken:
        calls = 0

        async def list(self, *, limit=50):
            Broken.calls += 1
            raise RuntimeError("db down")

    stats = cost.CostStats(Broken(), min_merged=1, clock=_Clock())

    async def run():
        assert await stats.cost_per_merged_line("kimi") is None
        assert await stats.cost_per_merged_line("kimi") is None

    asyncio.run(run())
    assert Broken.calls == 1


# --- review fix 1: the snapshot path is resolved at call time ---------------

_REAL_DEFAULT_SNAPSHOT_PATH = cost.default_snapshot_path


def test_default_snapshot_path_follows_home_at_call_time(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _REAL_DEFAULT_SNAPSHOT_PATH() == (
        tmp_path / ".conductor" / "usage" / "codexbar-snapshot.json"
    )


def test_read_quota_without_a_path_uses_the_call_time_path(tmp_path, monkeypatch) -> None:
    from src.tools.auto_dispatch import headroom_codexbar

    opened = []
    monkeypatch.setattr(
        headroom_codexbar, "_read_snapshot", lambda path: opened.append(path) or None
    )
    assert cost.read_quota("glm") is None
    # The unit conftest points the call-time path at tmp_path, never HOME.
    assert opened == [tmp_path / "no-snapshot.json"]
    assert headroom_codexbar.DEFAULT_SNAPSHOT_PATH not in opened
