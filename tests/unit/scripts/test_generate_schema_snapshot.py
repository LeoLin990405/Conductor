"""Snapshot generation with a fake server and a fake migration: no socket is opened."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.generate_schema_snapshot as gen
from src.conductor_cli import db_guard, schema_drift


class _Result:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)


class FakeServer:
    """Records every connect and statement. A postgres connection fails the test."""

    def __init__(self, catalog: dict[str, list[str]], revision: str = "146") -> None:
        self.catalog = catalog
        self.revision = revision
        self.connects: list[tuple[str, dict[str, object]]] = []
        self.statements: list[str] = []
        self.closed = 0

    def __call__(self, conninfo: str, **kwargs: object) -> FakeServer:
        from psycopg.conninfo import conninfo_to_dict

        dbname = conninfo_to_dict(conninfo)["dbname"]
        assert dbname.casefold() != "postgres", conninfo
        self.connects.append((dbname, kwargs))
        return self

    def execute(self, query: object) -> _Result:
        if not isinstance(query, str):
            query = query.as_string(None)  # psycopg.sql.Composed
        self.statements.append(query)
        if query == schema_drift.PUBLIC_SCHEMA_SQL:
            return _Result([(t, c) for t, cols in self.catalog.items() for c in cols])
        if query == schema_drift.REVISION_SQL:
            return _Result([(self.revision,)])
        return _Result([])

    def close(self) -> None:
        self.closed += 1


CATALOG = {"alembic_version": ["version_num"], "users": ["id", "email"], "tickets": ["title", "id"]}
MAINTENANCE = "postgresql://snap%20user:p%40ss%20w%2Brd@127.0.0.1:55432/template1?sslmode=disable"


@pytest.fixture(autouse=True)
def _clean_libpq_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PGDATABASE", "PGUSER", "PGSERVICE", "PGHOST", "PGPASSWORD"):
        monkeypatch.delenv(name, raising=False)


def _migrations() -> tuple[list[tuple[str, dict[str, str]]], gen.Migrate]:
    calls: list[tuple[str, dict[str, str]]] = []

    def migrate(url: str, environ) -> None:
        calls.append((url, dict(environ)))

    return calls, migrate


def test_generate_creates_from_template0_migrates_writes_and_drops(tmp_path: Path) -> None:
    server = FakeServer(CATALOG)
    calls, migrate = _migrations()
    out = tmp_path / "snapshot.json"
    document = gen.generate(
        MAINTENANCE, output=out, database="conductor_snap_test", connector=server, migrate=migrate, head="146"
    )
    assert [name for name, _ in server.connects] == ["template1", "conductor_snap_test"]
    assert server.connects[0][1] == {"autocommit": True}
    assert server.statements[0] == 'CREATE DATABASE "conductor_snap_test" TEMPLATE "template0"'
    assert "template1" not in server.statements[0]
    assert server.statements[-1] == 'DROP DATABASE IF EXISTS "conductor_snap_test" WITH (FORCE)'
    assert server.closed == 2
    url, environ = calls[0]
    assert url == "postgresql+psycopg:///conductor_snap_test"
    # Credentials reach Alembic decoded, through libpq variables, never in the URL.
    assert environ == {
        "PGUSER": "snap user",
        "PGPASSWORD": "p@ss w+rd",
        "PGHOST": "127.0.0.1",
        "PGPORT": "55432",
        "PGSSLMODE": "disable",
    }
    assert document == {
        "revision": "146",
        "tables": {"alembic_version": ["version_num"], "tickets": ["id", "title"], "users": ["email", "id"]},
    }
    assert json.loads(out.read_text(encoding="utf-8")) == document
    assert not out.with_name(out.name + ".tmp").exists()


def test_a_wrong_revision_still_drops_and_writes_nothing(tmp_path: Path) -> None:
    server = FakeServer(CATALOG, revision="145")
    _calls, migrate = _migrations()
    out = tmp_path / "snapshot.json"
    with pytest.raises(gen.SnapshotError, match="145"):
        gen.generate(MAINTENANCE, output=out, connector=server, migrate=migrate, head="146")
    assert server.statements[-1].startswith("DROP DATABASE IF EXISTS")
    assert not out.exists()


def test_a_failed_migration_still_drops(tmp_path: Path) -> None:
    server = FakeServer(CATALOG)

    def migrate(url: str, environ) -> None:
        raise RuntimeError("migration failed")

    with pytest.raises(RuntimeError):
        gen.generate(MAINTENANCE, output=tmp_path / "s.json", connector=server, migrate=migrate, head="146")
    assert server.statements[-1].startswith("DROP DATABASE IF EXISTS")


@pytest.mark.parametrize(
    ("maintenance", "env"),
    [
        ("postgresql://review@localhost?dbname=postgres&application_name=/audit", {}),
        ("postgresql://u@h/template1#ignored?dbname=postgres", {}),
        ("postgresql://postgres@localhost", {}),
        ("postgresql://localhost/", {"PGUSER": "postgres"}),
        ("host=localhost", {"PGDATABASE": "postgres"}),
        ("host=localhost dbname=''", {"PGUSER": "postgres"}),
        ("postgresql://u@h/Postgres", {}),
        ("postgresql://u@h/template1?database=template1", {}),
        ("service=snap", {}),
    ],
)
def test_maintenance_postgres_or_unresolvable_is_refused_before_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maintenance: str, env: dict[str, str]
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    server = FakeServer(CATALOG)
    calls, migrate = _migrations()
    with pytest.raises(db_guard.DatabaseRefused):
        gen.generate(maintenance, output=tmp_path / "s.json", connector=server, migrate=migrate, head="146")
    assert server.connects == []
    assert calls == []


@pytest.mark.parametrize("name", ["postgres", "Postgres", "template0", "template1", "bad-name", "a;drop", ""])
def test_unsafe_scratch_names_are_refused(tmp_path: Path, name: str) -> None:
    server = FakeServer(CATALOG)
    if name == "":
        assert gen.scratch_name(name).startswith("conductor_schema_snapshot_")
        return
    with pytest.raises(gen.SnapshotError):
        gen.generate(MAINTENANCE, output=tmp_path / "s.json", database=name, connector=server, head="146")
    assert server.connects == []


def test_maintenance_and_scratch_must_differ(tmp_path: Path) -> None:
    server = FakeServer(CATALOG)
    with pytest.raises(gen.SnapshotError, match="differ"):
        gen.generate(
            "postgresql://u@h/conductor_snap", output=tmp_path / "s.json", database="conductor_snap",
            connector=server, head="146",
        )
    assert server.connects == []


def test_alembic_upgrade_refuses_a_url_that_resolves_to_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    from alembic import command

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("alembic ran")

    monkeypatch.setattr(command, "upgrade", fail)
    with pytest.raises(db_guard.DatabaseRefused):
        gen.alembic_upgrade("postgresql+psycopg:///", {"PGUSER": "postgres"})
    with pytest.raises(db_guard.DatabaseRefused):
        gen.alembic_upgrade("postgresql+psycopg:///postgres", {"PGUSER": "u"})
    import os

    assert "PGUSER" not in os.environ


def test_alembic_upgrade_runs_inside_the_tripwire(monkeypatch: pytest.MonkeyPatch) -> None:
    from alembic import command

    seen: dict[str, object] = {}

    def upgrade(config, revision: str) -> None:
        import psycopg

        seen["url"] = config.get_main_option("sqlalchemy.url")
        seen["revision"] = revision
        seen["user"] = __import__("os").environ.get("PGUSER")
        with pytest.raises(db_guard.DatabaseRefused):
            psycopg.connect("postgresql://u@h/conductor?dbname=postgres")

    monkeypatch.setattr(command, "upgrade", upgrade)
    gen.alembic_upgrade("postgresql+psycopg:///conductor_snap", {"PGUSER": "u"})
    assert seen == {"url": "postgresql+psycopg:///conductor_snap", "revision": "head", "user": "u"}


def test_libpq_environment_refuses_parameters_without_a_variable() -> None:
    assert gen.libpq_environment("host=h port=1 dbname=template1") == {"PGHOST": "h", "PGPORT": "1"}
    with pytest.raises(gen.SnapshotError):
        gen.libpq_environment("host=h replication=database")


def test_main_requires_a_maintenance_database(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv(gen.MAINTENANCE_ENV, raising=False)
    assert gen.main([]) == 2
    assert "--maintenance" in capsys.readouterr().err


def test_main_reports_a_refusal_without_connecting(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import psycopg

    def connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("connected")

    monkeypatch.setattr(psycopg, "connect", connect)
    assert gen.main(["--maintenance", "postgresql://review@localhost?dbname=postgres&application_name=/audit"]) == 2
    assert "postgres" in capsys.readouterr().err
