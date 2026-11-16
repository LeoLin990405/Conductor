from urllib.parse import urlparse

import pytest

from tests.integration import (
    test_cloud_worker_slots_postgres as slots,
    test_daemon_leases_postgres as leases,
    test_daemon_output_postgres as output,
)
from tests.integration.conftest import _build_db_uri
from tests.integration.db_guard import require_test_database_name

_OWN_DSN_MODULES = (slots, leases, output)


@pytest.mark.parametrize("name", ["postgres", "POSTGRES", " postgres "])
def test_guard_refuses_the_production_database(monkeypatch, name):
    monkeypatch.setenv("TEST_DB_NAME", name)
    with pytest.raises(RuntimeError, match="database 'postgres'"):
        require_test_database_name()
    with pytest.raises(RuntimeError, match="database 'postgres'"):
        _build_db_uri()
    for module in _OWN_DSN_MODULES:
        with pytest.raises(RuntimeError, match="database 'postgres'"):
            module._dsn()


@pytest.mark.parametrize(
    "name", ["", "   ", "'postgres'", "x dbname=postgres", "a;b", "a-b", "9db"]
)
def test_guard_refuses_an_empty_or_non_identifier_name(monkeypatch, name):
    monkeypatch.setenv("TEST_DB_NAME", name)
    with pytest.raises(RuntimeError, match="Refusing to run integration tests"):
        require_test_database_name()
    with pytest.raises(RuntimeError, match="Refusing to run integration tests"):
        _build_db_uri()
    for module in _OWN_DSN_MODULES:
        with pytest.raises(RuntimeError, match="Refusing to run integration tests"):
            module._dsn()


def test_guard_defaults_to_conductor_test(monkeypatch):
    monkeypatch.delenv("TEST_DB_NAME", raising=False)
    monkeypatch.setenv("DB_NAME", "postgres")  # a production-shaped .env must not leak in
    assert urlparse(_build_db_uri()).path == "/conductor_test"
    for module in _OWN_DSN_MODULES:
        assert "dbname=conductor_test " in module._dsn()
