"""145's upgrade against real PostgreSQL: the market-data token table goes.

W10b-1 removed the whole data-token chain from the build (nothing under
``src/`` mints, keeps, revokes or reads a computer's data token), so 145
drops the one table the chain left behind. The trade-off is documented in
the migration itself: the drop destroys the rows, and the ``downgrade``
rebuilds the table *empty* -- the shape 144 had, none of the data.

This file asserts exactly that against a real server:

* ``fresh`` -- a new database run to ``head``: ``computer_data_tokens`` is
  gone (143 deliberately kept it; 145 is the round that drops it), the
  five revocation columns are gone with it, and every other table of the
  finance vertical that 143 dropped stays dropped;
* ``roundtrip`` -- the chain to ``head`` and back to ``144``: the table is
  back with 076's columns plus 080's five revocation columns, and it is
  empty.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SCRATCH_DB = "conductor_migration_145_{}_{}"

#: The table 145 drops (076 created it, 080 widened it).
DROPPED_TABLE = "computer_data_tokens"
#: The columns the downgrade's DDL recreates, in its order: 076's table
#: (computer_id is the primary key -- the table has no id column) plus 080's
#: five revocation columns.
COLUMNS_144 = (
    "computer_id",
    "user_id",
    "expires_at",
    "held_by",
    "generation",
    "attempted_at",
    "updated_at",
    "revoke_until",
    "revoke_generation",
    "revoke_users",
    "revoke_attempted_at",
    "tokens_until",
)


def _uri(dbname: str) -> str:
    from tests.integration.conftest import _build_db_uri

    parts = urlparse(_build_db_uri())
    return urlunparse(parts._replace(path=f"/{dbname}"))


async def _alembic(uri: str, action: str, revision: str) -> None:
    """Threaded, as the suite's upgrade helper is: a migration calls
    ``asyncio.run`` internally."""
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option(
        "sqlalchemy.url", uri.replace("postgresql://", "postgresql+psycopg://", 1)
    )
    cfg.config_file_name = None
    run = {"upgrade": command.upgrade, "downgrade": command.downgrade}[action]
    await asyncio.to_thread(run, cfg, revision)


async def _fetch(uri: str, sql: str) -> list[tuple]:
    async with await psycopg.AsyncConnection.connect(uri, autocommit=True) as conn:
        cur = await conn.execute(sql)
        return await cur.fetchall()


async def _reachable(uri: str) -> bool:
    try:
        await _fetch(uri, "SELECT 1")
    except psycopg.OperationalError:
        return False
    return True


async def _table_columns(uri: str, table: str) -> list[tuple]:
    return await _fetch(
        uri,
        "SELECT column_name, data_type, is_nullable, column_default,"
        " ordinal_position FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name ="
        f" '{table}' ORDER BY ordinal_position",
    )


async def _table_exists(uri: str, table: str) -> bool:
    rows = await _fetch(
        uri,
        "SELECT 1 FROM information_schema.tables"
        " WHERE table_schema = 'public' AND table_name ="
        f" '{table}'",
    )
    return bool(rows)


async def _alembic_version(uri: str) -> str:
    rows = await _fetch(uri, "SELECT version_num FROM alembic_version")
    return rows[0][0]


@pytest_asyncio.fixture
async def scratch() -> str:
    import uuid

    name = SCRATCH_DB.format(uuid.uuid4().hex[:8], "roundtrip")
    admin = _uri("template1")
    async with await psycopg.AsyncConnection.connect(admin, autocommit=True) as conn:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await conn.execute(f'CREATE DATABASE "{name}"')
    yield name
    async with await psycopg.AsyncConnection.connect(admin, autocommit=True) as conn:
        await conn.execute(
            f'SELECT pg_terminate_backend(pid) FROM pg_stat_activity'
            f" WHERE datname = '{name}' AND pid <> pg_backend_pid()"
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


async def test_145_drops_computer_data_tokens_and_downgrade_rebuilds_it_empty(
    scratch,
) -> None:
    uri = _uri(scratch)

    # Fresh database, the chain up to 145 (pinned, so later heads do not move it).
    await _alembic(uri, "upgrade", "145")
    assert await _alembic_version(uri) == "145"
    assert not await _table_exists(uri, DROPPED_TABLE), (
        "145 must drop computer_data_tokens: the token chain no longer has "
        "any code under src/ that could read it"
    )

    # Downgrade to 144: the table is back, empty, with 076's columns plus
    # 080's five revocation columns in order.
    await _alembic(uri, "downgrade", "144")
    assert await _alembic_version(uri) == "144"
    assert await _table_exists(uri, DROPPED_TABLE)
    columns = await _table_columns(uri, DROPPED_TABLE)
    assert [name for name, *_ in columns] == list(COLUMNS_144)
    rows = await _fetch(uri, f"SELECT * FROM {DROPPED_TABLE}")
    assert rows == [], "the downgrade rebuilds the shape, never the rows"

    # Upgrade again: the table goes away once more.
    await _alembic(uri, "upgrade", "145")
    assert not await _table_exists(uri, DROPPED_TABLE)
