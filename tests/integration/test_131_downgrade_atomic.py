"""131's downgrade against real PostgreSQL: one command, one transaction.

The unit tests read 131's statements off a mock ``op``, so none of its SQL ever
ran and nothing could show that a failure *after* 131's statements undoes them
along with everything the command did above. Each scenario builds its own
database at head with Tickets in seven statuses (two of them ``archived``):

* a view that depends on ``autonomy_daemon_claims.lease_grant`` makes
  ``alembic downgrade 128`` fail at 130, below 131: the database must stay at
  146, untouched;
* a writer commits an ``archived`` Ticket after the downgrade's update ran, so
  the restored CHECK meets a row it cannot hold, inside 131: nothing of 131 may
  stay applied;
* with nothing in the way the same command runs through 131 to 123 and the
  upgrade brings the schema back to head;
* the longest command that is meant to be one transaction, ``alembic downgrade
  115`` (146 down to 116), is failed at its last step, 116, by a view over the
  column it drops: a downgrade above it that had committed would leave the
  schema different, so the database must be exactly what it was, whatever the
  unit test's reading of the revisions says.
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
from sqlalchemy.exc import DBAPIError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: One database per scenario: these tests downgrade the schema, which the shared
#: session-scoped one cannot survive, and a scenario that fails on a broken
#: migration must not decide what the next one starts from. The name carries a
#: random suffix, so two runs against one server (parallel jobs, a developer
#: beside CI) never share a database, and the forced drop at the end can only
#: end sessions this run opened.
SCRATCH_DB = "conductor_migration_131_{}_{}"

USER = "user-131"
LINE = str(uuid.uuid4())
TICKET = {
    name: str(uuid.uuid4())
    for name in (
        "queued", "running", "waiting", "clarifying", "completed", "cancelled",
        "archived_a", "archived_b",
    )
}
# What each seeded Ticket reads as at head and once 131 is undone.
STATUS_AT_HEAD = {
    "queued": "queued",
    "running": "running",
    "waiting": "waiting",
    "clarifying": "awaiting_clarification",
    "completed": "completed",
    "cancelled": "cancelled",
    "archived_a": "archived",
    "archived_b": "archived",
}
STATUS_BELOW_131 = {
    **STATUS_AT_HEAD, "archived_a": "cancelled", "archived_b": "cancelled",
}
# A Ticket a writer archives while the downgrade is waiting for its table lock.
LATE = "late_archived"


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


async def _execute(uri: str, statements: list[tuple[str, tuple]]) -> None:
    async with await psycopg.AsyncConnection.connect(uri, autocommit=True) as conn:
        for sql, params in statements:
            await conn.execute(sql, params)


async def _fetch(uri: str, sql: str, params: tuple = ()) -> list[tuple]:
    async with await psycopg.AsyncConnection.connect(uri, autocommit=True) as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


def _seed() -> list[tuple[str, tuple]]:
    ticket = (
        "INSERT INTO autonomy_tickets"
        " (ticket_id, user_id, workspace_id, title, status, line_id)"
        " VALUES (%s, %s, 'w1', %s, %s, %s)"
    )
    on_line = {"running", "completed", "archived_a"}
    rows: list[tuple[str, tuple]] = [
        (
            "INSERT INTO autonomy_lines (line_id, user_id, name) VALUES (%s, %s, 'line-a')",
            (LINE, USER),
        ),
    ]
    for name, ticket_id in TICKET.items():
        rows.append((
            ticket,
            (ticket_id, USER, name, STATUS_AT_HEAD[name],
             LINE if name in on_line else None),
        ))
    rows.append((
        "INSERT INTO autonomy_ticket_events (ticket_id, user_id, actor, action)"
        " VALUES (%s, %s, %s, 'archived')",
        (TICKET["archived_a"], USER, USER),
    ))
    return rows


async def _state(uri: str) -> dict[str, Any]:
    (version,) = (await _fetch(uri, "SELECT version_num FROM alembic_version"))[0]
    statuses = dict(await _fetch(uri, "SELECT title, status FROM autonomy_tickets"))
    check = await _fetch(
        uri,
        "SELECT convalidated, pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conname = 'autonomy_tickets_status_check'"
        " AND conrelid = 'autonomy_tickets'::regclass",
    )
    (objects,) = await _fetch(
        uri,
        "SELECT to_regclass('autonomy_lines') IS NOT NULL,"
        " to_regclass('autonomy_ticket_events') IS NOT NULL,"
        " to_regclass('autonomy_daemon_output_sessions') IS NOT NULL,"
        " EXISTS (SELECT 1 FROM information_schema.columns"
        "   WHERE table_name = 'autonomy_tickets' AND column_name = 'line_id'),"
        " EXISTS (SELECT 1 FROM information_schema.columns"
        "   WHERE table_name = 'autonomy_daemon_claims'"
        "   AND column_name = 'lease_grant')",
    )
    lines, events, output_sessions, line_id, lease_grant = objects
    return {
        "version": version,
        "statuses": statuses,
        "check_validated": check[0][0],
        "check_allows_archived": "'archived'" in check[0][1],
        "lines": lines,
        "events": events,
        "output_sessions": output_sessions,
        "line_id": line_id,
        "lease_grant": lease_grant,
    }


async def _fingerprint(uri: str) -> dict[str, list[tuple]]:
    """Every column, constraint (with its validated flag), index, relation,
    function and trigger of the ``public`` schema, and the Tickets themselves."""
    queries = {
        "columns": "SELECT table_name, column_name, data_type, is_nullable,"
        " column_default FROM information_schema.columns"
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
        "tickets": "SELECT md5(string_agg(t::text, ',' ORDER BY ticket_id))"
        " FROM autonomy_tickets t",
        "version": "SELECT version_num FROM alembic_version",
    }
    return {name: await _fetch(uri, sql) for name, sql in queries.items()}


@asynccontextmanager
async def _scratch(name: str):
    """A database at head with the seeded Tickets; dropped afterwards."""
    admin = _uri("template1")
    db = SCRATCH_DB.format(name, uuid.uuid4().hex[:8])

    async def _run_admin(sql: str) -> None:
        async with await psycopg.AsyncConnection.connect(admin, autocommit=True) as c:
            await c.execute(sql)

    await _run_admin(f'CREATE DATABASE "{db}"')
    try:
        uri = _uri(db)
        await _alembic(uri, "upgrade", "head")
        await _execute(uri, _seed())
        yield uri
    finally:
        await _run_admin(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')


@pytest_asyncio.fixture(scope="module")
async def below_131() -> dict[str, Any]:
    """The command fails at 130, after 146 down to 132 and 131 itself ran."""
    out: dict[str, Any] = {}
    async with _scratch("below") as uri:
        out["head"] = await _state(uri)
        await _execute(uri, [(
            "CREATE VIEW dependent_on_lease_grant AS"
            " SELECT claim_id, lease_grant FROM autonomy_daemon_claims", (),
        )])
        try:
            await _alembic(uri, "downgrade", "128")
        except DBAPIError as error:
            out["failure"] = str(error.orig)
        out["after"] = await _state(uri)
    return out


@pytest_asyncio.fixture(scope="module")
async def inside_131() -> dict[str, Any]:
    """The restored CHECK finds an ``archived`` row the update did not see.

    The command starts in 131 (146 down to 132 are undone first), so the
    writer's open transaction does not hold up 140's drops before 131 runs. The
    update cannot see the uncommitted row and the ALTER TABLE waits for the
    writer's lock; the writer commits once that wait is seen.
    """
    out: dict[str, Any] = {}
    async with _scratch("inside") as uri:
        await _alembic(uri, "downgrade", "131")
        out["before"] = await _state(uri)
        async with await psycopg.AsyncConnection.connect(uri) as writer:
            await writer.execute(
                "INSERT INTO autonomy_tickets"
                " (ticket_id, user_id, workspace_id, title, status)"
                " VALUES (%s, %s, 'w1', %s, 'archived')",
                (str(uuid.uuid4()), USER, LATE),
            )
            command = asyncio.ensure_future(_alembic(uri, "downgrade", "130"))
            try:
                for _ in range(400):
                    waiting = await _fetch(
                        uri,
                        "SELECT 1 FROM pg_stat_activity"
                        " WHERE datname = current_database()"
                        " AND wait_event_type = 'Lock'"
                        # %% is a literal % to psycopg, which reads placeholders.
                        " AND query LIKE '%%DROP CONSTRAINT IF EXISTS"
                        " autonomy_tickets_status_check%%'",
                    )
                    if waiting or command.done():
                        break
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("the downgrade never waited for the table lock")
            finally:
                await writer.commit()
            try:
                await command
            except DBAPIError as error:
                out["failure"] = str(error.orig)
        out["after"] = await _state(uri)
    return out


@pytest_asyncio.fixture(scope="module")
async def round_trip() -> dict[str, Any]:
    out: dict[str, Any] = {}
    async with _scratch("trip") as uri:
        out["head"] = await _state(uri)
        await _alembic(uri, "downgrade", "123")
        out["down"] = await _state(uri)
        await _alembic(uri, "upgrade", "head")
        out["again"] = await _state(uri)
    return out


@pytest_asyncio.fixture(scope="module")
async def longest() -> dict[str, Any]:
    """``alembic downgrade 115`` fails at 116, after 146 down to 117 ran.

    116 drops ``autonomy_daemon_claims.input_snapshot``; a view over it makes
    that the one step that cannot run. The command is then run again with the
    view gone, and the upgrade brings the schema back.
    """
    out: dict[str, Any] = {}
    async with _scratch("longest") as uri:
        out["clean"] = await _fingerprint(uri)
        await _execute(uri, [(
            "CREATE VIEW dependent_on_input_snapshot AS"
            " SELECT claim_id, input_snapshot FROM autonomy_daemon_claims", (),
        )])
        out["before"] = await _fingerprint(uri)
        try:
            await _alembic(uri, "downgrade", "115")
        except DBAPIError as error:
            out["failure"] = str(error.orig)
        out["after"] = await _fingerprint(uri)
        await _execute(uri, [("DROP VIEW dependent_on_input_snapshot", ())])
        await _alembic(uri, "downgrade", "115")
        out["down"] = await _state(uri)
        await _alembic(uri, "upgrade", "head")
        out["again"] = await _fingerprint(uri)
    return out


class TestAFailureBelow131:
    async def test_it_fails_where_the_view_depends_on_the_column(self, below_131):
        assert "lease_grant" in below_131["failure"]

    async def test_the_database_stays_at_the_revision_the_command_started_from(
        self, below_131
    ):
        assert below_131["after"]["version"] == "151"

    async def test_nothing_131_or_the_revisions_above_it_did_is_left_undone(
        self, below_131
    ):
        after = below_131["after"]
        assert after["statuses"] == STATUS_AT_HEAD
        assert after["check_allows_archived"] is True
        assert after["check_validated"] is True
        assert (after["lines"], after["events"], after["line_id"]) == (True, True, True)
        # 140's tables are back from the rolled-back downgrade and 130's column
        # is still there: the whole command, not a prefix of it, was undone.
        assert after["output_sessions"] is True
        assert after["lease_grant"] is True
        assert after == below_131["head"]


class TestAFailureInside131:
    async def test_the_restored_check_refuses_the_late_row_and_says_so(
        self, inside_131
    ):
        assert "violated by some row" in inside_131["failure"]

    async def test_the_command_that_started_in_131_leaves_131_untouched(
        self, inside_131
    ):
        # The update that settled the other archived Tickets was rolled back
        # with everything else; the wider CHECK is still the validated one.
        before, after = inside_131["before"], inside_131["after"]
        assert before["version"] == after["version"] == "131"
        assert after["statuses"] == {**STATUS_AT_HEAD, LATE: "archived"}
        assert after["check_allows_archived"] is True
        assert after["check_validated"] is True
        assert (after["lines"], after["events"], after["line_id"]) == (True, True, True)


class TestTheCommandWithNothingInTheWay:
    async def test_it_runs_through_131_to_123(self, round_trip):
        down = round_trip["down"]
        assert down["version"] == "123"
        assert (down["lines"], down["events"], down["line_id"]) == (False, False, False)
        assert (down["output_sessions"], down["lease_grant"]) == (False, False)

    async def test_archived_tickets_read_as_cancelled_and_the_rest_are_untouched(
        self, round_trip
    ):
        assert round_trip["down"]["statuses"] == STATUS_BELOW_131

    async def test_the_restored_check_is_the_old_vocabulary_and_validated(
        self, round_trip
    ):
        down = round_trip["down"]
        assert down["check_allows_archived"] is False
        assert down["check_validated"] is True

    async def test_upgrading_again_reaches_head_with_archived_allowed_and_validated(
        self, round_trip
    ):
        again = round_trip["again"]
        assert again["version"] == "151"
        assert again["check_allows_archived"] is True
        assert again["check_validated"] is True
        assert (again["lines"], again["events"], again["line_id"]) == (True, True, True)
        # The settled rows stay settled: the way down is not undone on the way up.
        assert again["statuses"] == STATUS_BELOW_131


class TestTheLongestRangeFailingAtItsLastStep:
    async def test_it_fails_at_116_where_the_view_depends_on_the_column(
        self, longest
    ):
        assert "input_snapshot" in longest["failure"]

    async def test_the_database_is_exactly_what_it_was(self, longest):
        # 146 down to 117 and 116's own statements ran before the failure; one
        # of them committed and the fingerprint (schema, functions, triggers,
        # the Tickets, alembic_version) would differ.
        assert longest["before"]["version"] == [("151",)]
        assert longest["after"] == longest["before"]

    async def test_the_same_command_completes_once_the_cause_is_gone(
        self, longest
    ):
        down = longest["down"]
        assert down["version"] == "115"
        assert (down["lines"], down["events"], down["line_id"]) == (False, False, False)
        assert (down["output_sessions"], down["lease_grant"]) == (False, False)

    async def test_the_upgrade_restores_the_schema_it_started_from(self, longest):
        again, clean = longest["again"], longest["clean"]
        assert again["version"] == [("151",)]
        for name in ("columns", "constraints", "indexes", "relations",
                     "functions", "triggers"):
            assert again[name] == clean[name], name
