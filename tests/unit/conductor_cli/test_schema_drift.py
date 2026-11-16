"""Doctor schema drift: fake connections only, and the committed snapshot stays at the Alembic head."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.conductor_cli import schema_drift

OS_USER = "osuser"


class _Result:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)


class FakeConn:
    def __init__(self, catalog: dict[str, list[str]], revision: str | None) -> None:
        self.catalog = catalog
        self.revision = revision
        self.closed = False
        self.queries: list[str] = []

    def execute(self, query: str) -> _Result:
        self.queries.append(query)
        if query == schema_drift.PUBLIC_SCHEMA_SQL:
            rows: list[tuple[object, ...]] = []
            for table, columns in self.catalog.items():
                rows.extend((table, column) for column in columns)
                if not columns:
                    rows.append((table, None))
            return _Result(rows)
        if query == schema_drift.REVISION_SQL:
            return _Result([] if self.revision is None else [(self.revision,)])
        raise AssertionError(query)

    def close(self) -> None:
        self.closed = True


class FakeConnect:
    def __init__(self, conn: object | Exception) -> None:
        self.conn = conn
        self.calls: list[str] = []

    def __call__(self, conninfo: str) -> object:
        from psycopg.conninfo import conninfo_to_dict

        self.calls.append(conninfo)
        assert conninfo_to_dict(conninfo)["dbname"].casefold() != "postgres"
        if isinstance(self.conn, Exception):
            raise self.conn
        return self.conn


SNAPSHOT = {
    "revision": "146",
    "tables": {
        "alembic_version": ["version_num"],
        "tickets": ["id", "status", "title"],
        "users": ["email", "id"],
    },
}


@pytest.fixture
def snapshot_path(tmp_path: Path) -> Path:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("DB_HOST", "DB_PORT", "DB_USER", "DB_NAME", "DB_PASSWORD", "DB_SSLMODE"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text(
        "DB_HOST=127.0.0.1\nDB_PORT=54329\nDB_USER=op\nDB_NAME=conductor\nDB_PASSWORD=p@ss word'\n",
        encoding="utf-8",
    )
    return root


def _checks(repo: Path, snapshot_path: Path, connect, env=None, sensitive=None):
    return schema_drift.schema_checks(
        repo,
        connect,
        environ={} if env is None else env,
        os_user=lambda: OS_USER,
        snapshot_path=snapshot_path,
        sensitive=sensitive,
    )


def test_matching_catalog_is_one_ok_line(repo, snapshot_path) -> None:
    conn = FakeConn({k: list(v) for k, v in SNAPSHOT["tables"].items()}, "146")
    connect = FakeConnect(conn)
    sensitive: list[str] = []
    checks = _checks(repo, snapshot_path, connect, sensitive=sensitive)
    assert checks == [{"name": "schema_drift", "ok": True, "detail": "matches snapshot at revision 146"}]
    assert conn.closed
    assert sensitive == ["p@ss word'"]
    from psycopg.conninfo import conninfo_to_dict

    params = conninfo_to_dict(connect.calls[0])
    assert params["dbname"] == "conductor"
    assert params["password"] == "p@ss word'"
    assert "default_transaction_read_only=on" in params["options"]


def test_drift_names_each_table_and_column(repo, snapshot_path) -> None:
    live = {
        "alembic_version": ["version_num"],
        "tickets": ["id", "title", "priority"],
        "stray": ["a"],
    }
    checks = _checks(repo, snapshot_path, FakeConnect(FakeConn(live, "145")))
    assert [(c["ok"], c.get("warn", False), c["detail"]) for c in checks] == [
        (False, False, "alembic revision 145, snapshot expects 146"),
        (False, False, "missing table users"),
        (False, False, "missing column tickets.status"),
        (True, True, "extra table stray (not in the snapshot)"),
        (True, True, "extra column tickets.priority (not in the snapshot)"),
    ]
    assert all(c["name"] == "schema_drift" for c in checks)


def test_extra_objects_alone_are_warnings_and_doctor_stays_ok(repo, snapshot_path) -> None:
    from src.conductor_cli.doctor import format_doctor

    live = {k: list(v) for k, v in SNAPSHOT["tables"].items()}
    live["users"].append("nickname")
    live["operator_notes"] = ["id"]
    checks = _checks(repo, snapshot_path, FakeConnect(FakeConn(live, "146")))
    assert all(c["ok"] is True and c["warn"] is True for c in checks)
    text = format_doctor({"ok": all(c["ok"] for c in checks), "checks": checks})
    assert "WARN: schema_drift (extra table operator_notes (not in the snapshot))" in text
    assert "WARN: schema_drift (extra column users.nickname (not in the snapshot))" in text
    assert text.endswith("doctor: ok\n")


def test_doctor_formats_drift_as_fail_lines(repo, snapshot_path) -> None:
    from src.conductor_cli.doctor import format_doctor

    live = {"alembic_version": ["version_num"], "tickets": ["id", "title"], "users": ["email", "id"]}
    checks = _checks(repo, snapshot_path, FakeConnect(FakeConn(live, "146")))
    text = format_doctor({"ok": False, "checks": checks})
    assert "FAIL: schema_drift (missing column tickets.status)" in text
    assert text.endswith("doctor: failed\n")


def test_dotted_names_do_not_cancel(repo, snapshot_path) -> None:
    assert schema_drift.diff_catalogs({"a.b": ["c"], "a": []}, {"a": ["b.c"], "a.b": []}) == [
        "missing column a.b.c",
        "extra column a.b.c",
    ]


def test_long_drift_is_capped_and_failures_are_never_hidden(repo, snapshot_path) -> None:
    live = {"alembic_version": ["version_num"], **{f"t{i:03d}": ["x"] for i in range(100)}}
    checks = _checks(repo, snapshot_path, FakeConnect(FakeConn(live, "146")))
    assert len(checks) == schema_drift.MAX_DETAIL_LINES + 1
    # The two missing tables come first even though extras sort before them.
    assert [c["detail"] for c in checks[:2]] == ["missing table tickets", "missing table users"]
    assert checks[-1] == {
        "name": "schema_drift", "ok": True, "warn": True, "detail": "62 more drift lines not shown",
    }


def test_a_capped_failure_keeps_the_summary_failing() -> None:
    snapshot = schema_drift.Snapshot("146", {f"t{i:03d}": ["x"] for i in range(60)})
    checks = schema_drift.drift_checks({}, "146", snapshot)
    assert checks[-1] == {"name": "schema_drift", "ok": False, "detail": "20 more drift lines not shown"}


@pytest.mark.parametrize(
    ("dotenv", "env"),
    [
        ("DB_NAME=postgres\n", {}),
        ("DB_NAME=POSTGRES\n", {}),
        ("", {}),  # the backend default database is postgres
        ("DB_NAME=\n", {}),
        ("DB_NAME=conductor\n", {"DB_NAME": "Postgres"}),
    ],
)
def test_postgres_is_a_warning_and_never_connects(repo, snapshot_path, monkeypatch, dotenv, env) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    (repo / ".env").write_text("DB_HOST=h\nDB_USER=op\n" + dotenv, encoding="utf-8")
    connect = FakeConnect(AssertionError("connected"))
    checks = schema_drift.schema_checks(repo, connect, os_user=lambda: OS_USER, snapshot_path=snapshot_path)
    assert connect.calls == []
    assert len(checks) == 1
    assert checks == [
        {
            "name": "schema_drift",
            "ok": True,
            "warn": True,
            "detail": "skipped: the backend database is named postgres, which doctor never opens; "
            "the check runs once DB_NAME names a dedicated database",
        }
    ]


def test_service_in_environment_is_refused_before_connect(repo, snapshot_path) -> None:
    connect = FakeConnect(AssertionError("connected"))
    checks = _checks(repo, snapshot_path, connect, env={"PGSERVICE": "prod"})
    assert connect.calls == []
    assert checks[0]["warn"] is True and "service" in str(checks[0]["detail"])


def test_missing_connector_is_a_warning(repo, snapshot_path) -> None:
    checks = _checks(repo, snapshot_path, None)
    assert checks == [
        {"name": "schema_drift", "ok": True, "detail": "skipped: no database connector", "warn": True}
    ]


def test_driver_error_text_is_not_reported(repo, snapshot_path) -> None:
    import psycopg

    connect = FakeConnect(psycopg.OperationalError("password=p@ss word' host=127.0.0.1"))
    checks = _checks(repo, snapshot_path, connect)
    assert checks == [
        {"name": "schema_drift", "ok": False, "detail": "live schema unreadable (OperationalError)"}
    ]


def test_unreadable_snapshot_fails_without_connecting(repo, tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    connect = FakeConnect(AssertionError("connected"))
    assert _checks(repo, bad, connect) == [
        {"name": "schema_drift", "ok": False, "detail": "snapshot unreadable"}
    ]
    assert connect.calls == []


def test_committed_snapshot_is_canonical_and_at_the_alembic_head() -> None:
    from scripts.generate_schema_snapshot import script_head

    snapshot = schema_drift.load_snapshot()
    assert snapshot.revision == script_head()
    assert "alembic_version" in snapshot.tables
    text = schema_drift.SNAPSHOT_PATH.read_text(encoding="utf-8")
    document = schema_drift.snapshot_document(snapshot.tables, snapshot.revision)
    assert text == schema_drift.render_snapshot(document)
