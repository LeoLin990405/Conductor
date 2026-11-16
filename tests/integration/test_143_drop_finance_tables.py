"""143's downgrade against real PostgreSQL: the 142 catalog, not a replay of it.

The unit tests read 143's statements off a mock ``op``, so none of its SQL ever
ran and nothing could show that a rebuilt table is not the table 142 had. 143's
downgrade reconstructs the seven finance tables from the DDL of the revisions
that created them, which is exactly where this goes wrong: a later revision can
take something away, and the replay puts it back. 096 removed trading and with
it ``idx_wakeups_armed_order`` (``DROP INDEX IF EXISTS idx_wakeups_armed_order``
beside ``order_attempts``); replaying 082 alone recreates the index, and the
database ends up with an index 142 never had.

So the comparison is not against a hand-written list of objects. Two databases
are built and their whole ``public`` catalogs are read side by side:

* ``fresh`` -- a new database run ``alembic upgrade 142``;
* ``roundtrip`` -- the same chain to ``143`` and back with
  ``alembic downgrade 142``.

Every column, constraint, index, relation, function and trigger must match.
The one difference allowed is disclosed and pinned here rather than waved at:
``ADD COLUMN`` appends ``investment_preference`` to the end of
``user_preferences`` instead of the ordinal position 001 gave it, so the column
moves and nothing else does.

The upgrade half is asserted too, because a downgrade that is faithful to a
schema nobody reaches proves nothing: at 143 the seven tables and the column are
gone and ``computer_data_tokens`` is still there.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: One database per scenario, with a random suffix so two runs against one
#: server never share one and the forced drop can only end sessions this run
#: opened.
SCRATCH_DB = "conductor_migration_143_{}_{}"

#: The objects 143's upgrade drops, in the order it drops them.
DROPPED_TABLES = (
    "chart_annotations",
    "watchlist_items",
    "watchlists",
    "user_portfolios",
    "market_insights",
    "market_watch_targets",
    "wakeups",
)
#: A column on a table that stays, so only the column leaves.
DROPPED_COLUMN = ("user_preferences", "investment_preference")
#: A whole table of its own (076), left for the sibling task, and asserted here
#: so a later round cannot drop it by accident while the finance vertical is
#: being removed.
KEPT_TABLE = "computer_data_tokens"
#: 096 removed this with ``order_attempts``; 082 alone would put it back.
INDEX_096_REMOVED = "idx_wakeups_armed_order"


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


async def _catalog(uri: str) -> dict[str, list[tuple]]:
    """Every column (with its ordinal position), constraint, index, relation,
    function and trigger of the ``public`` schema, and the stored revision."""
    queries = {
        "columns": "SELECT table_name, column_name, data_type, is_nullable,"
        " column_default, ordinal_position FROM information_schema.columns"
        " WHERE table_schema = 'public' ORDER BY 1, 2",
        "constraints": "SELECT conrelid::regclass::text, conname, contype,"
        " convalidated, pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2",
        "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes"
        " WHERE schemaname = 'public' ORDER BY 1, 2",
        "relations": "SELECT relname, relkind FROM pg_class"
        " WHERE relnamespace = 'public'::regnamespace ORDER BY 1, 2",
        "functions": "SELECT proname, md5(prosrc) FROM pg_proc"
        " WHERE pronamespace = 'public'::regnamespace ORDER BY 1, 2",
        "triggers": "SELECT tgrelid::regclass::text, tgname FROM pg_trigger"
        " WHERE NOT tgisinternal ORDER BY 1, 2",
        "version": "SELECT version_num FROM alembic_version",
    }
    return {name: await _fetch(uri, sql) for name, sql in queries.items()}


def _columns_without_ordinal(rows: list[tuple]) -> list[tuple]:
    """The columns as they are shaped, minus the one field that is allowed to
    move, so an equality over this list is the whole comparison."""
    return [row[:-1] for row in rows]


def _ordinal(rows: list[tuple], table: str, column: str) -> int | None:
    for name, col, *_rest, position in rows:
        if name == table and col == column:
            return position
    return None


def _last_ordinal(rows: list[tuple], table: str) -> int:
    return max(position for name, *_rest, position in rows if name == table)


def _index_names(rows: list[tuple]) -> set[str]:
    return {name for _table, name, _def in rows}


def _table_names(rows: list[tuple]) -> set[str]:
    return {name for name, _kind in rows if _kind == "r"}


@asynccontextmanager
async def _scratch(name: str):
    """A database run to 142, then to 143 and back to 142; dropped afterwards."""
    admin = _uri("template1")
    db = SCRATCH_DB.format(name, uuid.uuid4().hex[:8])

    async def _run_admin(sql: str) -> None:
        async with await psycopg.AsyncConnection.connect(admin, autocommit=True) as c:
            await c.execute(sql)

    if not await _reachable(admin):
        pytest.skip("postgres unavailable: the 142-versus-142 catalog comparison needs a database")
    await _run_admin(f'CREATE DATABASE "{db}"')
    try:
        uri = _uri(db)
        await _alembic(uri, "upgrade", "142")
        fresh = await _catalog(uri)
        await _alembic(uri, "upgrade", "143")
        at_head = await _catalog(uri)
        await _alembic(uri, "downgrade", "142")
        roundtrip = await _catalog(uri)
        yield {"uri": uri, "fresh": fresh, "at_head": at_head, "roundtrip": roundtrip}
    finally:
        await _run_admin(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')


@pytest_asyncio.fixture(scope="module")
async def catalogs() -> dict[str, Any]:
    async with _scratch("roundtrip") as built:
        yield built


class TestTheUpgradeDropsWhatItSays:
    async def test_the_seven_tables_are_gone(self, catalogs):
        tables = _table_names(catalogs["at_head"]["relations"])
        assert not set(DROPPED_TABLES) & tables

    async def test_the_investment_column_is_gone(self, catalogs):
        assert _ordinal(catalogs["at_head"]["columns"], *DROPPED_COLUMN) is None

    async def test_the_sibling_task_table_stays(self, catalogs):
        assert KEPT_TABLE in _table_names(catalogs["at_head"]["relations"])

    async def test_the_revision_is_the_new_head(self, catalogs):
        assert catalogs["at_head"]["version"] == [("143",)]


class TestTheDowngradeRestoresThe142Catalog:
    async def test_the_catalog_is_the_one_a_fresh_chain_builds(self, catalogs):
        # Ordinal position is the one field that is allowed to move, and
        # test_the_investment_column_moves_and_nothing_else_does pins exactly
        # how; here it is left out so the equality is the whole rest.
        for name in ("columns", "constraints", "indexes", "relations",
                     "functions", "triggers"):
            rows, want = catalogs["roundtrip"][name], catalogs["fresh"][name]
            if name == "columns":
                rows, want = _columns_without_ordinal(rows), _columns_without_ordinal(want)
            assert rows == want, name

    async def test_no_index_a_later_revision_removed_comes_back(self, catalogs):
        # 096 dropped this beside order_attempts. 082's DDL alone recreates it,
        # which is how it survived the first version of this downgrade.
        assert INDEX_096_REMOVED not in _index_names(
            catalogs["roundtrip"]["indexes"]
        )

    async def test_the_investment_column_moves_and_nothing_else_does(self, catalogs):
        # The disclosed tradeoff, pinned rather than tolerated in silence:
        # ADD COLUMN appends, so the column ends up last in user_preferences.
        fresh, roundtrip = catalogs["fresh"], catalogs["roundtrip"]
        assert _ordinal(fresh["columns"], *DROPPED_COLUMN) is not None
        assert _ordinal(roundtrip["columns"], *DROPPED_COLUMN) == _last_ordinal(
            roundtrip["columns"], "user_preferences"
        )
        assert _ordinal(fresh["columns"], *DROPPED_COLUMN) != _last_ordinal(
            fresh["columns"], "user_preferences"
        )
        # Every other column keeps its shape and its place.
        assert _columns_without_ordinal(roundtrip["columns"]) == (
            _columns_without_ordinal(fresh["columns"])
        )

    async def test_the_revision_is_back_at_142(self, catalogs):
        assert catalogs["roundtrip"]["version"] == [("142",)]

    async def test_a_second_downgrade_is_a_no_op(self, catalogs):
        """The statements are all IF NOT EXISTS, so a re-run cannot fail or
        change anything -- which is what makes the round trip above safe to
        repeat in either direction."""
        uri = catalogs["uri"]
        await _alembic(uri, "downgrade", "142")
        assert await _catalog(uri) == catalogs["roundtrip"]


@pytest.mark.asyncio
async def test_the_round_trip_reaches_head_again(catalogs):
    """143 -> 142 -> 143, so a database that rolled back can still be brought
    forward -- the shape the downgrade rebuilt has to be one 143 can drop."""
    uri = catalogs["uri"]
    await _alembic(uri, "upgrade", "143")
    again = await _catalog(uri)
    assert again["version"] == [("143",)]
    assert not set(DROPPED_TABLES) & _table_names(again["relations"])
    assert _ordinal(again["columns"], *DROPPED_COLUMN) is None
