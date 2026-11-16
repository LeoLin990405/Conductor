"""The N80 collab rollout report reads four row sets and never writes.

Every source here is a fake: a strict in-memory store whose only attributes
are the four reads, a JSON snapshot under ``tmp_path`` and a fake psycopg
connector that records each statement. No database, network or wall clock is
touched, and HOME points at ``tmp_path`` for the whole module.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.conductor_cli.db_guard import DatabaseRefused
from tests.unit._isolated_home import assert_untouched, isolate

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "collab_report.py"
SPEC = importlib.util.spec_from_file_location("collab_report", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
collab_report = importlib.util.module_from_spec(SPEC)
# dataclasses resolves annotations through the module registry during exec.
sys.modules[SPEC.name] = collab_report
SPEC.loader.exec_module(collab_report)

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
NOW_ISO = NOW.isoformat()


@pytest.fixture(autouse=True)
def isolated_report_home(tmp_path, monkeypatch):
    home, original, before = isolate(tmp_path, monkeypatch)
    # libpq defaults must not leak into db_guard's resolution.
    for name in ("PGSERVICE", "PGDATABASE", "PGUSER"):
        monkeypatch.delenv(name, raising=False)
    yield home
    assert_untouched(original, before)


def _at(**delta: float) -> str:
    return (NOW - timedelta(**delta)).isoformat()


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


def _rows() -> dict[str, list[dict[str, Any]]]:
    return {
        "audit": [
            {"app_id": "codex", "code": "ok", "rate_limited": False, "redacted": 2,
             "created_at": _at(days=2)},
            {"app_id": "codex", "code": "rate_limited", "rate_limited": False,
             "redacted": 0, "created_at": _at(days=1)},
            {"app_id": "codex", "code": None, "rate_limited": True, "redacted": "3",
             "created_at": _at(hours=5)},
            {"app_id": "claude", "code": "self_review", "rate_limited": False,
             "redacted": None, "created_at": _at(hours=4)},
            {"app_id": "claude", "code": "not_claimable", "rate_limited": False,
             "redacted": 0, "created_at": _at(hours=3)},
            {"app_id": "", "code": "unauthenticated", "rate_limited": False,
             "redacted": 0, "created_at": _at(hours=2)},
        ],
        "ticket_events": [
            {"ticket_id": "T1", "actor": "app:codex/gpt-5", "action": "comment",
             "detail": {"collab_event": "claimed"}, "created_at": _at(hours=6)},
            {"ticket_id": "T2", "actor": "app:claude", "action": "reported",
             "detail": None, "created_at": _at(hours=5)},
            {"ticket_id": "T3", "actor": "app:codex", "action": "comment",
             "detail": json.dumps({"collab_event": "released", "reason": "out_of_scope"}),
             "created_at": _at(hours=4)},
            {"ticket_id": "T4", "actor": "human:leo", "action": "comment",
             "detail": {"collab_event": "claimed", "reason": "out_of_scope"},
             "created_at": _at(hours=3)},
        ],
        "leases": [
            {"lease_id": "L1", "app_id": "codex", "state": "completed",
             "end_reason": "done", "created_at": _at(days=2)},
            {"lease_id": "L2", "app_id": "codex", "state": "completed",
             "end_reason": "done", "created_at": _at(days=1)},
            {"lease_id": "L3", "app_id": "claude", "state": "expired",
             "end_reason": None, "created_at": _at(hours=10)},
            {"lease_id": "L4", "app_id": "claude", "state": "released",
             "end_reason": "lease_expired", "created_at": _at(hours=9)},
            {"lease_id": "L5", "app_id": "codex", "state": "released",
             "end_reason": "out_of_scope", "created_at": _at(hours=8)},
        ],
        "handoffs": [
            _handoff("H1", 10, "accepted"),
            _handoff("H2", 20, "accepted"),
            _handoff("H3", 30, "accepted"),
            _handoff("H4", 100, "accepted"),
            {"handoff_id": "H5", "state": "pending", "created_at": _at(hours=1),
             "accepted_at": None},
        ],
    }


def _handoff(handoff_id: str, wait_seconds: int, state: str) -> dict[str, Any]:
    start = NOW - timedelta(hours=2)
    return {
        "handoff_id": handoff_id,
        "state": state,
        "created_at": start.isoformat(),
        "accepted_at": (start + timedelta(seconds=wait_seconds)).isoformat(),
    }


class StrictStore:
    """Only the reads exist; anything else (a write) fails the test."""

    _READS = ("audit", "ticket_events", "leases", "handoffs")

    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        object.__setattr__(self, "_rows", rows)
        object.__setattr__(self, "calls", [])

    def __getattribute__(self, name: str) -> Any:
        if name in StrictStore._READS:
            rows = object.__getattribute__(self, "_rows")
            calls = object.__getattribute__(self, "calls")

            def read(since: datetime) -> list[dict[str, Any]]:
                calls.append((name, since))
                return [dict(row) for row in rows.get(name, [])]

            return read
        if name == "first_audit_at":
            rows = object.__getattribute__(self, "_rows")
            calls = object.__getattribute__(self, "calls")

            def first() -> datetime | None:
                calls.append(("first_audit_at", None))
                times = [
                    collab_report.parse_time(row.get("created_at"))
                    for row in rows.get("audit", [])
                ]
                known = [moment for moment in times if moment is not None]
                return min(known) if known else None

            return first
        if name in {"_rows", "calls", "__class__", "__dict__"}:
            return object.__getattribute__(self, name)
        return StrictStore.__getattr__(self, name)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"report touched non-read attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        raise AssertionError(f"report tried to set {name!r} on the store")


def _report(rows: dict[str, list[dict[str, Any]]], **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("now", NOW)
    return collab_report.build_report(StrictStore(rows), **kwargs)


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): (path.read_bytes() if path.is_file() else None)
        for path in sorted(root.rglob("*"))
    }


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn
        self.description: list[tuple[str, ...]] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.conn.executed.append((sql, params))
        if sql == self.conn.fail_on:
            raise RuntimeError("fake server refused the statement")
        table = self.conn.tables.get(sql)
        if table is None:
            self.description, self._rows = None, []
        else:
            names, rows = table
            self.description = [(name, None) for name in names]
            self._rows = list(rows)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class FakeConnection:
    def __init__(self, tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]]) -> None:
        self.tables = tables
        self.fail_on: str | None = None
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.read_only_sets: list[Any] = []
        self.cursors = 0
        self.rolled_back = False
        self.closed = False

    @property
    def read_only(self) -> Any:
        return self.read_only_sets[-1] if self.read_only_sets else None

    @read_only.setter
    def read_only(self, value: Any) -> None:
        self.read_only_sets.append(value)

    def cursor(self) -> FakeCursor:
        self.cursors += 1
        return FakeCursor(self)

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class FakeConnector:
    def __init__(
        self,
        tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]] | None = None,
        *,
        fail_on: str | None = None,
        raises: BaseException | None = None,
    ):
        self.tables = tables or {}
        self.fail_on = fail_on
        self.raises = raises
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.connections: list[FakeConnection] = []

    def __call__(self, conninfo: str, **kwargs: Any) -> FakeConnection:
        self.calls.append((conninfo, kwargs))
        if self.raises is not None:
            raise self.raises
        conn = FakeConnection(self.tables)
        conn.fail_on = self.fail_on
        self.connections.append(conn)
        return conn


def _db_tables() -> dict[str, tuple[list[str], list[tuple[Any, ...]]]]:
    q = collab_report.QUERIES
    return {
        q["audit"]: (
            ["app_id", "agent", "tool", "target", "code", "http_status", "rate_limited",
             "redacted", "duration_ms", "created_at"],
            [("codex", "a", "task_list", None, "ok", 200, False, 1, 5, NOW - timedelta(days=1)),
             ("codex", "a", "task_claim", "T1", "self_review", 403, False, 0, 5,
              NOW - timedelta(hours=1))],
        ),
        q["ticket_events"]: (
            ["ticket_id", "actor", "action", "detail", "created_at"],
            [("T1", "app:codex", "comment", {"collab_event": "claimed"}, NOW)],
        ),
        q["leases"]: (
            ["lease_id", "app_id", "target_ref", "target_kind", "state", "end_reason",
             "created_at", "ended_at"],
            [("L1", "codex", "T1", "ticket", "completed", "done", NOW - timedelta(hours=3), NOW)],
        ),
        q["handoffs"]: (
            ["handoff_id", "lease_id", "from_app_id", "to_target", "reason", "state",
             "accepted_by", "created_at", "accepted_at"],
            [("H1", "L1", "codex", "claude", "x", "accepted", "claude",
              NOW - timedelta(seconds=40), NOW)],
        ),
        q["first_audit"]: (["first_at"], [(NOW - timedelta(days=9),)]),
    }


# ---------------------------------------------------------------------------
# 0. HOME isolation
# ---------------------------------------------------------------------------


def test_home_is_temp_dir_during_a_run(tmp_path, isolated_report_home, capsys):
    snapshot = tmp_path / "snap.json"
    snapshot.write_text(json.dumps(_rows()), encoding="utf-8")
    seen: dict[str, Path] = {}

    class Probe(collab_report.SnapshotStore):
        def audit(self, since: datetime) -> list[dict[str, Any]]:
            seen["env"] = Path(os.environ["HOME"])
            seen["home"] = Path.home()
            return super().audit(since)

    collab_report.build_report(Probe(_rows()), now=NOW)
    assert collab_report.main(["--from-json", str(snapshot), "--now", NOW_ISO]) == 0
    capsys.readouterr()
    assert seen["env"] == isolated_report_home
    assert seen["home"] == isolated_report_home
    assert Path.home() == isolated_report_home
    assert str(isolated_report_home).startswith(str(tmp_path.resolve()))


# ---------------------------------------------------------------------------
# 1. Metrics over a strict read-only store
# ---------------------------------------------------------------------------


def test_strict_store_rejects_anything_but_reads():
    store = StrictStore(_rows())
    with pytest.raises(AssertionError):
        store.insert  # noqa: B018
    with pytest.raises(AssertionError):
        store.close  # noqa: B018
    with pytest.raises(AssertionError):
        store.extra = 1


def test_build_report_numbers():
    store = StrictStore(_rows())
    report = collab_report.build_report(store, now=NOW, days=7, mode="shadow")
    totals = report["totals"]

    assert report["schema"] == "conductor.collab_report/v1"
    assert report["mode"] == "shadow"
    assert totals["calls"] == 6
    assert totals["refusals"] == {
        "not_claimable": 1,
        "rate_limited": 1,
        "self_review": 1,
        "unauthenticated": 1,
    }
    assert totals["rate_limited"] == 2  # flag on one row, code on another
    assert totals["redacted"] == 5
    assert totals["self_review_refusals"] == 1
    assert totals["claims"] == 5
    assert totals["completed"] == 2
    assert totals["done_rate"] == 0.4
    assert totals["lease_expired"] == 2
    assert totals["lease_states"] == {"completed": 2, "expired": 1, "released": 2}
    # One ticket-event veto (JSON-string detail) plus one lease end_reason;
    # the human actor's veto is ignored.
    assert totals["out_of_scope"] == 2
    assert totals["ticket_events"] == {"claimed": 1, "released": 1, "reported": 1}
    assert totals["handoffs"] == {"accepted": 4, "pending": 1}
    assert totals["handoff_accept_seconds"] == {"samples": 4, "median": 25.0, "p95": 100.0}
    assert totals["non_loopback_refusals"] == "not recorded"

    apps = report["apps"]
    assert set(apps) == {"(unauthenticated)", "claude", "codex"}
    assert apps["codex"] == {
        "calls": 3, "refusals": {"rate_limited": 1}, "rate_limited": 2, "redacted": 5,
        "claims": 3, "completed": 2, "lease_expired": 0,
    }
    assert apps["claude"]["calls"] == 2
    assert apps["claude"]["claims"] == 2
    assert apps["claude"]["lease_expired"] == 2
    assert apps["claude"]["refusals"] == {"not_claimable": 1, "self_review": 1}
    assert apps["(unauthenticated)"]["refusals"] == {"unauthenticated": 1}


def test_since_window_passed_to_store_is_now_minus_days():
    store = StrictStore(_rows())
    report = collab_report.build_report(store, now=NOW, days=3)
    since = NOW - timedelta(days=3)
    windowed = [call for call in store.calls if call[0] != "first_audit_at"]
    assert sorted(windowed) == sorted(
        (name, since) for name in ("audit", "ticket_events", "leases", "handoffs")
    )
    assert ("first_audit_at", None) in store.calls
    assert report["window"] == {
        "since": since.isoformat(), "until": NOW.isoformat(), "days": 3,
    }


def test_event_kind_falls_back_to_action_and_ignores_non_app_actors():
    rows = {
        "ticket_events": [
            {"actor": "app:x", "action": "handed_over", "detail": "{}"},
            {"actor": "app:x", "action": "lease_expired", "detail": "not json"},
            {"actor": "bot:x", "action": "claimed", "detail": {}},
            {"actor": None, "action": "claimed", "detail": {}},
        ],
    }
    totals = _report(rows)["totals"]
    assert totals["ticket_events"] == {"handed_over": 1, "lease_expired": 1}


# ---------------------------------------------------------------------------
# 2. Promotion checks
# ---------------------------------------------------------------------------


def _leases(total: int, completed: int, **extra: Any) -> list[dict[str, Any]]:
    return [
        {"lease_id": f"L{i}", "app_id": "codex",
         "state": "completed" if i < completed else "released",
         "created_at": _at(hours=1), **extra}
        for i in range(total)
    ]


def _promotion(report: dict[str, Any], gate: str) -> dict[str, Any]:
    return report["promotion"][gate]


def test_shadow_not_ready_with_under_seven_days():
    report = _report({"audit": [{"app_id": "a", "created_at": _at(days=6)}]}, days=14)
    gate = _promotion(report, "shadow_to_docs")
    assert gate["ready"] is False
    assert gate["checks"]["days_of_data"]["value"] == 6.0
    assert gate["checks"]["days_of_data"]["pass"] is False


def test_shadow_ready_with_seven_days_or_more():
    report = _report({"audit": [{"app_id": "a", "created_at": _at(days=7)}]}, days=14)
    assert _promotion(report, "shadow_to_docs")["ready"] is True
    report = _report({"audit": [{"app_id": "a", "created_at": _at(days=8)}]}, days=14)
    assert _promotion(report, "shadow_to_docs")["ready"] is True


def test_docs_ready_case():
    rows = {"audit": [{"app_id": "a", "created_at": _at(days=8)}], "leases": _leases(10, 8)}
    gate = _promotion(_report(rows, days=14), "docs_to_full")
    assert gate["ready"] is True
    assert all(check["pass"] for check in gate["checks"].values())


def test_docs_ready_on_claims_alone_and_on_days_alone():
    rows = {"audit": [{"app_id": "a", "created_at": _at(days=1)}], "leases": _leases(10, 7)}
    gate = _promotion(_report(rows), "docs_to_full")
    assert gate["ready"] is True  # 10 claims, exactly 0.70
    rows = {"audit": [{"app_id": "a", "created_at": _at(days=8)}], "leases": _leases(2, 2)}
    assert _promotion(_report(rows, days=14), "docs_to_full")["ready"] is True


def test_docs_not_ready_when_neither_duration_nor_claims():
    rows = {"audit": [{"app_id": "a", "created_at": _at(days=3)}], "leases": _leases(9, 9)}
    gate = _promotion(_report(rows), "docs_to_full")
    assert gate["ready"] is False
    assert gate["checks"]["duration_or_claims"]["pass"] is False
    assert gate["checks"]["done_rate"]["pass"] is True
    assert gate["checks"]["out_of_scope"]["pass"] is True


def test_docs_not_ready_when_done_rate_low():
    rows = {"audit": [{"app_id": "a", "created_at": _at(days=8)}], "leases": _leases(10, 6)}
    gate = _promotion(_report(rows, days=14), "docs_to_full")
    assert gate["ready"] is False
    assert gate["checks"]["done_rate"] == {"value": 0.6, "need": 0.7, "pass": False}
    assert gate["checks"]["duration_or_claims"]["pass"] is True
    assert gate["checks"]["out_of_scope"]["pass"] is True


def test_docs_not_ready_when_out_of_scope():
    rows = {
        "audit": [{"app_id": "a", "created_at": _at(days=8)}],
        "leases": _leases(10, 10),
        "ticket_events": [{"actor": "app:a", "action": "x",
                           "detail": {"reason": "out_of_scope"}}],
    }
    gate = _promotion(_report(rows, days=14), "docs_to_full")
    assert gate["ready"] is False
    assert gate["checks"]["out_of_scope"] == {"value": 1, "need": 0, "pass": False}
    assert gate["checks"]["done_rate"]["pass"] is True
    assert gate["checks"]["duration_or_claims"]["pass"] is True


def test_docs_not_ready_without_claims():
    rows = {"audit": [{"app_id": "a", "created_at": _at(days=8)}]}
    report = _report(rows, days=14)
    assert report["totals"]["done_rate"] is None
    gate = _promotion(report, "docs_to_full")
    assert gate["ready"] is False
    assert gate["checks"]["done_rate"] == {"value": None, "need": 0.7, "pass": False}


# ---------------------------------------------------------------------------
# 3. No writes, snapshot filtering
# ---------------------------------------------------------------------------


def test_no_files_written_by_build_report_or_main(tmp_path, capsys):
    snapshot = tmp_path / "snap.json"
    snapshot.write_text(json.dumps(_rows()), encoding="utf-8")
    before = _tree(tmp_path)
    bytes_before = snapshot.read_bytes()

    collab_report.build_report(collab_report.SnapshotStore.from_file(snapshot), now=NOW)
    assert _tree(tmp_path) == before
    code = collab_report.main(["--from-json", str(snapshot), "--now", NOW_ISO, "--json"])
    assert code == 0
    capsys.readouterr()
    assert _tree(tmp_path) == before
    assert snapshot.read_bytes() == bytes_before


def test_snapshot_store_filters_rows_older_than_since():
    since = NOW - timedelta(days=7)
    store = collab_report.SnapshotStore({
        "audit": [
            {"app_id": "old", "created_at": _at(days=8)},
            {"app_id": "edge", "created_at": since.isoformat()},
            {"app_id": "new", "created_at": _at(days=1)},
            {"app_id": "undated"},
            "not a row",
        ],
        "leases": "not a list",
    })
    assert [row["app_id"] for row in store.audit(since)] == ["edge", "new", "undated"]
    assert store.leases(since) == []
    assert store.handoffs(since) == []


# ---------------------------------------------------------------------------
# 4. DatabaseStore with a fake connector
# ---------------------------------------------------------------------------


def test_database_store_runs_only_read_only_selects(capsys):
    connector = FakeConnector(_db_tables())
    code = collab_report.main(
        ["--dsn", "dbname=conductor host=127.0.0.1", "--now", NOW_ISO, "--json",
         "--days", "2"],
        connector=connector,
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)

    assert len(connector.calls) == 1
    conninfo, kwargs = connector.calls[0]
    assert "dbname=conductor" in conninfo
    assert kwargs == {"autocommit": False}
    conn = connector.connections[0]
    assert conn.read_only_sets == [True]

    sqls = [sql for sql, _ in conn.executed]
    allowed = set(collab_report.QUERIES.values()) | {"SET TRANSACTION READ ONLY"}
    assert set(sqls) <= allowed
    assert sqls[0] == "SET TRANSACTION READ ONLY"
    assert sorted(sqls[1:]) == sorted(collab_report.QUERIES.values())
    since = NOW - timedelta(days=2)
    first_audit = collab_report.QUERIES["first_audit"]
    for sql, params in conn.executed[1:]:
        assert sql.lstrip().upper().startswith("SELECT")
        assert params == (() if sql == first_audit else (since,))
    assert conn.executed[0][1] == ()
    assert conn.rolled_back is True
    assert conn.closed is True

    totals = report["totals"]
    assert totals["calls"] == 2
    assert totals["self_review_refusals"] == 1
    assert totals["redacted"] == 1
    assert totals["claims"] == 1
    assert totals["completed"] == 1
    assert totals["ticket_events"] == {"claimed": 1}
    assert totals["handoff_accept_seconds"]["median"] == 40.0
    # The unwindowed first audit row (9 days old) feeds days_of_data.
    days = report["promotion"]["shadow_to_docs"]["checks"]["days_of_data"]
    assert days["value"] == 9.0
    assert days["pass"] is True


def test_database_store_maps_rows_by_description_names():
    connector = FakeConnector(_db_tables())
    store = collab_report.DatabaseStore("dbname=conductor host=127.0.0.1", connector=connector)
    try:
        rows = store.leases(NOW)
    finally:
        store.close()
    assert rows == [{
        "lease_id": "L1", "app_id": "codex", "target_ref": "T1", "target_kind": "ticket",
        "state": "completed", "end_reason": "done",
        "created_at": NOW - timedelta(hours=3), "ended_at": NOW,
    }]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO collab_audit (app_id) VALUES (%s)",
        "UPDATE collab_leases SET state = 'completed' WHERE created_at >= %s",
        "DELETE FROM collab_handoffs WHERE created_at >= %s",
    ],
)
def test_database_store_refuses_writes_before_the_cursor(sql):
    connector = FakeConnector(_db_tables())
    store = collab_report.DatabaseStore("dbname=conductor host=127.0.0.1", connector=connector)
    conn = connector.connections[0]
    cursors_before = conn.cursors
    executed_before = list(conn.executed)
    with pytest.raises(collab_report.ReadOnlyViolation):
        store._run(sql, (NOW,))
    assert conn.cursors == cursors_before
    assert conn.executed == executed_before
    store.close()


# ---------------------------------------------------------------------------
# 5. The postgres database is refused before the connector
# ---------------------------------------------------------------------------

POSTGRES_DSNS = ["dbname=postgres host=127.0.0.1", "postgresql://u@127.0.0.1/postgres"]


@pytest.mark.parametrize("dsn", POSTGRES_DSNS)
def test_main_refuses_postgres_dsn(dsn, capsys):
    connector = FakeConnector()
    code = collab_report.main(["--dsn", dsn, "--now", NOW_ISO], connector=connector)
    assert code == 2
    captured = capsys.readouterr()
    assert "collab_report:" in captured.err
    assert "postgres" in captured.err
    assert captured.out == ""
    assert connector.calls == []


@pytest.mark.parametrize("dsn", POSTGRES_DSNS)
def test_database_store_raises_database_refused(dsn):
    connector = FakeConnector()
    with pytest.raises(DatabaseRefused) as info:
        collab_report.DatabaseStore(dsn, connector=connector)
    assert info.value.reason == "postgres"
    assert connector.calls == []


# ---------------------------------------------------------------------------
# 6. main output, mode, argparse
# ---------------------------------------------------------------------------


@pytest.fixture
def snapshot_path(tmp_path) -> Path:
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(_rows()), encoding="utf-8")
    return path


def test_main_text_output(snapshot_path, monkeypatch, capsys):
    monkeypatch.delenv("CONDUCTOR_COLLAB_MODE", raising=False)
    assert collab_report.main(["--from-json", str(snapshot_path), "--now", NOW_ISO]) == 0
    out = capsys.readouterr().out
    assert out.startswith("collab report  mode=off  ")
    assert "calls 6  refusals 4  rate_limited 2  redacted 5" in out
    assert "non-loopback refusals: not recorded" in out
    assert "handoff accept seconds: n=4 median=25.0 p95=100.0" in out
    assert "shadow_to_docs: not ready: days_of_data" in out
    assert "docs_to_full: not ready: duration_or_claims, done_rate, out_of_scope" in out
    assert "The report never changes CONDUCTOR_COLLAB_MODE." in out


def test_main_json_output(snapshot_path, monkeypatch, capsys):
    monkeypatch.delenv("CONDUCTOR_COLLAB_MODE", raising=False)
    code = collab_report.main(["--from-json", str(snapshot_path), "--now", NOW_ISO, "--json"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "conductor.collab_report/v1"
    assert report["mode"] == "off"
    assert report["window"]["until"] == NOW_ISO
    assert report["totals"]["calls"] == 6


@pytest.mark.parametrize(("value", "expected"), [(None, "off"), ("bogus", "off"),
                                                  ("docs", "docs")])
def test_main_mode_from_environment(value, expected, snapshot_path, monkeypatch, capsys):
    if value is None:
        monkeypatch.delenv("CONDUCTOR_COLLAB_MODE", raising=False)
    else:
        monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", value)
    collab_report.main(["--from-json", str(snapshot_path), "--now", NOW_ISO, "--json"])
    assert json.loads(capsys.readouterr().out)["mode"] == expected
    collab_report.main(["--from-json", str(snapshot_path), "--now", NOW_ISO])
    assert f"mode={expected} " in capsys.readouterr().out


def test_argparse_requires_exactly_one_source(snapshot_path, capsys):
    with pytest.raises(SystemExit) as info:
        collab_report.main(["--now", NOW_ISO])
    assert info.value.code == 2
    connector = FakeConnector()
    with pytest.raises(SystemExit) as info:
        collab_report.main(
            ["--from-json", str(snapshot_path), "--dsn", "dbname=conductor", "--now", NOW_ISO],
            connector=connector,
        )
    assert info.value.code == 2
    assert connector.calls == []
    assert "not allowed with argument" in capsys.readouterr().err


def test_days_zero_is_an_error(snapshot_path, capsys):
    with pytest.raises(SystemExit) as info:
        collab_report.main(["--from-json", str(snapshot_path), "--now", NOW_ISO, "--days", "0"])
    assert info.value.code == 2
    assert "--days must be at least 1" in capsys.readouterr().err


def test_default_window_counts_days_from_first_audit_row_outside_it():
    """--days 7 is enough once the first audit row is older than the window."""
    rows = {"audit": [{"app_id": "a", "created_at": _at(days=8)},
                      {"app_id": "a", "created_at": _at(hours=1)}]}
    gate = _promotion(_report(rows, days=7), "shadow_to_docs")
    assert gate["ready"] is True
    assert gate["checks"]["days_of_data"]["value"] == 8.0


def test_snapshot_first_audit_at_ignores_the_window(tmp_path, capsys):
    rows = _rows()
    rows["audit"].append({"app_id": "codex", "code": "ok", "created_at": _at(days=10)})
    store = collab_report.SnapshotStore(rows)
    assert store.first_audit_at() == NOW - timedelta(days=10)
    report = collab_report.build_report(store, now=NOW, days=7)
    assert report["totals"]["calls"] == 6  # the old row is outside the window
    assert _promotion(report, "shadow_to_docs")["ready"] is True
    path = tmp_path / "old.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    assert collab_report.main(["--from-json", str(path), "--now", NOW_ISO, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["promotion"]["shadow_to_docs"]["ready"] is True


# ---------------------------------------------------------------------------
# Follow-up fixes: out_of_scope dedup, init failure, connect failure
# ---------------------------------------------------------------------------


def _veto_event(ticket_id: str) -> dict[str, Any]:
    return {"ticket_id": ticket_id, "actor": "app:codex", "action": "released",
            "detail": {"collab_event": "released", "reason": "out_of_scope"}}


def test_out_of_scope_lease_on_vetoed_ticket_counts_once():
    rows = {
        "ticket_events": [_veto_event("T9")],
        "leases": [{"lease_id": "L9", "app_id": "codex", "target_ref": "t:T9",
                    "state": "released", "end_reason": "out_of_scope"}],
    }
    assert _report(rows)["totals"]["out_of_scope"] == 1


def test_out_of_scope_lease_on_another_ticket_still_counts():
    rows = {
        "ticket_events": [_veto_event("T9")],
        "leases": [
            {"lease_id": "L9", "app_id": "codex", "target_ref": "t:T9",
             "state": "released", "end_reason": "out_of_scope"},
            {"lease_id": "L8", "app_id": "codex", "target_ref": "t:T8",
             "state": "released", "end_reason": "out_of_scope"},
        ],
    }
    assert _report(rows)["totals"]["out_of_scope"] == 2


def test_read_only_failure_in_init_rolls_back_closes_and_propagates():
    connector = FakeConnector(_db_tables(), fail_on="SET TRANSACTION READ ONLY")
    with pytest.raises(RuntimeError, match="fake server refused"):
        collab_report.DatabaseStore("dbname=conductor host=127.0.0.1", connector=connector)
    conn = connector.connections[0]
    assert conn.executed == [("SET TRANSACTION READ ONLY", ())]
    assert conn.rolled_back is True
    assert conn.closed is True


class FakeOperationalError(Exception):
    pass


def test_main_connect_failure_returns_2_without_leaking_password(capsys):
    dsn = "dbname=conductor host=127.0.0.1 user=u password=s3cret-fake"
    connector = FakeConnector(raises=FakeOperationalError(f"could not connect: {dsn}"))
    code = collab_report.main(["--dsn", dsn, "--now", NOW_ISO, "--json"], connector=connector)
    assert code == 2
    captured = capsys.readouterr()
    assert "cannot open the database (FakeOperationalError)" in captured.err
    assert "s3cret-fake" not in captured.err
    assert "s3cret-fake" not in captured.out
    assert captured.out == ""
    assert len(connector.calls) == 1


def test_main_init_failure_returns_2_and_closes(capsys):
    connector = FakeConnector(_db_tables(), fail_on="SET TRANSACTION READ ONLY")
    code = collab_report.main(
        ["--dsn", "dbname=conductor host=127.0.0.1 password=s3cret-fake", "--now", NOW_ISO],
        connector=connector,
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "cannot open the database (RuntimeError)" in err
    assert "s3cret-fake" not in err
    conn = connector.connections[0]
    assert conn.rolled_back is True
    assert conn.closed is True
