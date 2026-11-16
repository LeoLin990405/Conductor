"""One rule for every integration test that opens a database connection.

The production database is named ``postgres``. Integration tests create and
drop rows, so they must never run against it, whichever way they build their
connection string (the shared ``_build_db_uri`` fixture or a file's own DSN).
"""

from __future__ import annotations

import os
import re

PRODUCTION_DATABASE = "postgres"
DEFAULT_TEST_DATABASE = "conductor_test"
# A plain identifier only: an empty name lets libpq fall back to the user name
# (often "postgres"), and quotes or spaces could smuggle "dbname=postgres" into
# a key/value DSN.
_SAFE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


def require_test_database_name() -> str:
    """``TEST_DB_NAME`` (default ``conductor_test``); refuses ``postgres``, empty and non-identifier names."""
    name = os.getenv("TEST_DB_NAME", DEFAULT_TEST_DATABASE).strip()
    if not _SAFE_NAME.fullmatch(name):
        raise RuntimeError(
            f"Refusing to run integration tests: TEST_DB_NAME {name!r} is not a plain "
            "database name. Use a dedicated test database such as 'conductor_test'."
        )
    if name.lower() == PRODUCTION_DATABASE:
        raise RuntimeError(
            "Refusing to run integration tests against database 'postgres'. "
            "Set TEST_DB_NAME to a dedicated test database such as 'conductor_test'."
        )
    return name
