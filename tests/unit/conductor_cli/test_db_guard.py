"""The postgres guard resolves the database the way libpq does and never reaches the connector for it.

Every case runs against a fake connector that fails the test if it is called
with a refused string. Nothing here opens a socket: the guard only parses.
The expected names were checked against libpq's own resolution (``PQdb`` on a
``PQconnectStart`` aimed at a missing socket directory) while writing them.
"""

from __future__ import annotations

import pytest

from src.conductor_cli import db_guard

OS_USER = "osuser"


class FakeConnector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, conninfo: str, **kwargs: object) -> object:
        from psycopg.conninfo import conninfo_to_dict

        self.calls.append((conninfo, kwargs))
        name = conninfo_to_dict(conninfo).get("dbname")
        assert name is not None and str(name).casefold() != "postgres", conninfo
        return object()


# (conninfo, keyword overrides, environment, effective dbname or None when refused as unresolvable)
CASES: list[tuple[str, dict[str, str], dict[str, str], str | None]] = [
    # The review examples.
    ("postgresql://review@localhost?dbname=postgres&application_name=/audit", {}, {}, "postgres"),
    ("postgresql://u@h/conductor#ignored?dbname=postgres", {}, {}, "postgres"),
    ("postgresql://u@h", {}, {"PGDATABASE": "postgres"}, "postgres"),
    ("postgresql://postgres@h", {}, {}, "postgres"),
    ("postgresql://postgres@h/", {}, {}, "postgres"),
    ("postgresql://h/?user=postgres", {}, {}, "postgres"),
    ("postgresql://h/conductor?dbname=", {"user": "postgres"}, {}, "postgres"),
    # Query wins over path; the last repeated key wins.
    ("postgresql://u@h/conductor?dbname=postgres", {}, {}, "postgres"),
    ("postgresql://u@h/conductor?dbname=a&dbname=postgres", {}, {}, "postgres"),
    ("postgresql://u@h/postgres?dbname=conductor", {}, {}, "conductor"),
    # Percent-encoding and case.
    ("postgresql://u@h/%70ostgres", {}, {}, "postgres"),
    ("postgresql://%70ostgres@h", {}, {}, "postgres"),
    ("postgres://u@h/POSTGRES", {}, {}, "POSTGRES"),
    ("postgresql:///?host=/tmp&dbname=Postgres", {}, {}, "Postgres"),
    # Empty and missing names fall back to the effective user.
    ("postgresql://u@h/", {}, {}, "u"),
    ("postgresql://u@h/conductor?dbname=", {}, {"PGDATABASE": "conductor"}, "u"),
    ("postgresql://h", {}, {"PGUSER": "postgres"}, "postgres"),
    ("postgresql://h", {}, {"PGDATABASE": "", "PGUSER": "postgres"}, "postgres"),
    ("postgresql://h", {}, {"PGUSER": ""}, OS_USER),
    ("host=h", {}, {}, OS_USER),
    ("host=h user=''", {}, {"PGUSER": "postgres"}, OS_USER),
    ("host=h dbname=''", {}, {"PGDATABASE": "conductor", "PGUSER": "postgres"}, "postgres"),
    ("", {}, {"PGDATABASE": "postgres"}, "postgres"),
    ("", {}, {}, OS_USER),
    # Keyword strings: libpq quoting, not whitespace splitting.
    ("dbname= user=x", {}, {}, "user=x"),
    ("host=h dbname='postgres'", {}, {}, "postgres"),
    ("host=h dbname='postgresql://u@h/postgres'", {}, {}, "postgresql://u@h/postgres"),
    ("host=h dbname=conductor", {}, {"PGDATABASE": "postgres"}, "conductor"),
    # Keyword overrides are merged by make_conninfo, the same way connect merges them.
    ("postgresql://u@h/conductor", {"dbname": "postgres"}, {}, "postgres"),
    ("postgresql://u@h/postgres", {"dbname": "conductor"}, {}, "conductor"),
    ("host=h", {"user": "postgres"}, {}, "postgres"),
    # Multi-host keeps one dbname.
    ("postgresql://u@h1,h2/conductor", {}, {}, "conductor"),
    ("postgresql://u@h1:5432,h2:5433/postgres", {}, {}, "postgres"),
]

# Strings libpq rejects, and service lookups that could choose the database.
UNRESOLVABLE: list[tuple[str, dict[str, str]]] = [
    ("postgresql://u@h/conductor?database=postgres", {}),
    ("postgresql:///conductor?dbname=postgres%00", {}),
    ("host=h dbname='post''gres'", {}),
    ("not a conninfo", {}),
    ("service=prod", {}),
    ("postgresql://u@h/?service=prod", {}),
    ("postgresql://u@h/conductor", {"PGSERVICE": "prod"}),
    ("postgresql://u@h/conductor", {"PGSERVICE": ""}),
]


def _os_user() -> str:
    return OS_USER


@pytest.mark.parametrize(("conninfo", "params", "env", "expected"), CASES)
def test_effective_dbname_matches_libpq(conninfo, params, env, expected) -> None:
    assert db_guard.effective_dbname(conninfo, environ=env, os_user=_os_user, **params) == expected


@pytest.mark.parametrize(("conninfo", "params", "env", "expected"), CASES)
def test_guarded_connect_never_reaches_the_connector_for_postgres(conninfo, params, env, expected) -> None:
    fake = FakeConnector()
    if expected.strip().casefold() == "postgres":
        with pytest.raises(db_guard.DatabaseRefused, match="postgres"):
            db_guard.guarded_connect(conninfo, connector=fake, environ=env, os_user=_os_user, **params)
        assert fake.calls == []
    else:
        db_guard.guarded_connect(conninfo, connector=fake, environ=env, os_user=_os_user, **params)
        assert len(fake.calls) == 1


@pytest.mark.parametrize(("conninfo", "env"), UNRESOLVABLE)
def test_unresolvable_connections_are_refused_before_connect(conninfo, env) -> None:
    fake = FakeConnector()
    with pytest.raises(db_guard.DatabaseRefused):
        db_guard.guarded_connect(conninfo, connector=fake, environ=env, os_user=_os_user)
    assert fake.calls == []


@pytest.mark.parametrize("error", [KeyError("uid"), OSError("passwd")])
def test_an_unknown_os_user_is_refused_when_libpq_would_need_it(error) -> None:
    def broken() -> str:
        raise error

    fake = FakeConnector()
    with pytest.raises(db_guard.DatabaseRefused, match="user cannot be resolved"):
        db_guard.guarded_connect("host=h", connector=fake, environ={}, os_user=broken)
    assert fake.calls == []


def test_os_user_is_read_from_the_passwd_entry_not_user(monkeypatch) -> None:
    import pwd

    monkeypatch.setenv("USER", "postgres")
    monkeypatch.setenv("LOGNAME", "postgres")
    real = pwd.getpwuid(__import__("os").geteuid()).pw_name
    assert db_guard.effective_dbname("host=h", environ={}) == real


def test_the_connector_receives_exactly_the_checked_string() -> None:
    from psycopg.conninfo import conninfo_to_dict

    fake = FakeConnector()
    db_guard.guarded_connect(
        "postgresql://u@h/postgres",
        connector=fake,
        environ={},
        os_user=_os_user,
        autocommit=True,
        dbname="conductor",
        password="two words '\\ +%",
    )
    conninfo, kwargs = fake.calls[0]
    assert kwargs == {"autocommit": True}
    parsed = conninfo_to_dict(conninfo)
    assert parsed["dbname"] == "conductor"
    assert parsed["password"] == "two words '\\ +%"


def test_guard_reads_the_process_environment_by_default(monkeypatch) -> None:
    monkeypatch.setenv("PGDATABASE", "postgres")
    monkeypatch.delenv("PGSERVICE", raising=False)
    with pytest.raises(db_guard.DatabaseRefused):
        db_guard.checked_conninfo("host=h user=u")
    monkeypatch.setenv("PGDATABASE", "conductor")
    assert "host=h" in db_guard.checked_conninfo("host=h user=u")


def test_tripwire_guards_psycopg_connect_and_restores_it(monkeypatch) -> None:
    import psycopg

    module_connect = psycopg.connect
    sync_connect = psycopg.Connection.__dict__["connect"]
    async_connect = psycopg.AsyncConnection.__dict__["connect"]
    monkeypatch.setenv("PGUSER", "u")
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.delenv("PGSERVICE", raising=False)
    with db_guard.psycopg_tripwire():
        for connect in (psycopg.connect, psycopg.Connection.connect, psycopg.AsyncConnection.connect):
            with pytest.raises(db_guard.DatabaseRefused):
                connect("postgresql://review@localhost?dbname=postgres&application_name=/audit")
            with pytest.raises(db_guard.DatabaseRefused):
                connect("host=localhost", dbname="postgres", autocommit=True)
            with pytest.raises(db_guard.DatabaseRefused):
                connect("host=localhost user=postgres")
    assert psycopg.connect is module_connect
    assert psycopg.Connection.__dict__["connect"] is sync_connect
    assert psycopg.AsyncConnection.__dict__["connect"] is async_connect


def test_the_resolved_default_database_is_pinned_into_the_checked_string() -> None:
    from psycopg.conninfo import conninfo_to_dict

    checked = db_guard.checked_conninfo("host=h", environ={"PGUSER": "u"}, os_user=_os_user)
    assert conninfo_to_dict(checked)["dbname"] == "u"
    # The pinned string resolves the same under a hostile environment later.
    assert db_guard.effective_dbname(checked, environ={"PGDATABASE": "postgres"}) == "u"
