"""Custom surface values pass through storage and delivery classification.

The platform grammar is open: a client may introduce a surface this build does
not know. Its value is stored exactly as sent, its identity block stays generic,
and its steers follow the channel path unless conductor draws the surface.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.server.handlers.chat.steering import is_chat_app, steer_payload
from src.server.models.chat import ChatRequest
from src.server.models.conversation import ThreadCreateRequest

CUSTOM = "custom_view:REPORT-1"


class TestTheValueIsAcceptedExactlyAsSent:
    @pytest.mark.parametrize(
        "platform",
        [CUSTOM, "custom_view", "custom_view:REPORT-2", "custom_view:REPORT-3"],
    )
    def test_the_request_models_do_not_reject_it(self, platform):
        assert ChatRequest(platform=platform).platform == platform
        assert (
            ThreadCreateRequest(first_query="what moved?", platform=platform).platform
            == platform
        )

    def test_the_thread_column_and_the_provenance_row_agree_with_the_request(self):
        request = ChatRequest(platform=CUSTOM)
        from src.server.handlers.chat.request_prep import provenance_metadata

        assert request.platform == CUSTOM
        assert provenance_metadata(request)["platform"] == CUSTOM


class TestTheIdentityBlockStaysGeneric:
    def test_the_identity_block_states_name_timezone_and_locale(self):
        """Identity is name, timezone and locale only, at either guidance level."""
        from ptc_agent.agent.prompts import guidance_template_vars, init_loader

        loader = init_loader()
        for level in ("lean", "detailed"):
            text = loader.render(
                "envelope/baseline_identity.md.j2",
                **guidance_template_vars(level),
                name="Ada",
                timezone="UTC",
                locale="en-US",
            )
            assert "- Name: Ada" in text
            assert "- Timezone: UTC" in text
            assert "- Locale: en-US" in text


class TestSteerDelivery:
    def test_a_custom_surface_is_stamped_as_a_channel(self):
        payload = steer_payload("also cover this", "u-1", platform=CUSTOM)

        assert payload["chat_app"] is True
        assert payload["platform"] == CUSTOM

    @pytest.mark.parametrize(
        ("platform", "expected"),
        [
            (CUSTOM, True),
            ("custom_view", True),
            ("custom_view:REPORT-2", True),
            ("web", False),
            ("web:CHART-1", False),
            ("telegram", True),
            ("slack", True),
        ],
    )
    def test_the_delivery_classification(self, platform, expected):
        assert is_chat_app(platform) is expected


# ---------------------------------------------------------------------------
# The write side, which the classes above cannot see.
#
# ``test_the_thread_column_and_the_provenance_row_agree_with_the_request``
# compares ``request.platform`` with ``provenance_metadata(request)["platform"]``.
# Both sides read the SAME request field, so the assertion holds no matter what
# a write path does with the value: injecting a normalization inside the write
# left every test in this file green. Nothing here until now runs a statement.
#
# The class below drives the real INSERT and asserts on the parameters the
# database actually binds, which is the only thing that can tell "accepted as
# sent" from "accepted, then rewritten on the way to the row".
# ---------------------------------------------------------------------------

#: Fixed ids, so the row store can be read back by the id it was written under.
THREAD_ID = "0f4b1d6c-2f6a-4a3e-9c11-6b1f5a2e7d01"
WORKSPACE_ID = "3c9d8a52-7b1e-4f0a-8d63-1e2b7c9a4f55"
USER_ID = "u-1"

PREP = "src.server.handlers.chat.request_prep"


def _bound_insert_values(sql, params):
    """The INSERT's columns paired with the values each one binds.

    Same pairing ``test_conversation_db`` uses: a column's value expression can
    bind more than one (the subagent switch also binds the workspace its owner's
    default is read from), so the placeholders and the parameters are walked
    together rather than zipped positionally.
    """
    columns = sql.split("INSERT INTO conversation_threads (", 1)[1].split(")", 1)[0]
    names = [c.strip() for c in columns.split(",")]
    values_sql = sql.split("VALUES (", 1)[1]
    expressions, depth, current = [], 0, ""
    for ch in values_sql:
        if ch == ")" and depth == 0:
            break
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            expressions.append(current)
            current = ""
        else:
            current += ch
    expressions.append(current)
    assert len(expressions) == len(names)
    assert sql.count("%s") == len(params)
    bound, rest = {}, list(params)
    for name, expression in zip(names, expressions):
        count = expression.count("%s")
        bound[name], rest = tuple(rest[:count]), rest[count:]
    return {name: b[0] if len(b) == 1 else b for name, b in bound.items()}


class _StoreCursor:
    """A cursor that runs the write chain's statements against one row store.

    Only the statements the chain actually runs are answered, and each one is
    answered from the store rather than from a canned value, so what comes back
    is a consequence of what the write bound.
    """

    def __init__(self, store):
        self._store = store
        self._row = None

    async def execute(self, sql, params=None):
        params = tuple(params or ())
        self._store.executed.append((sql, params))
        if "INSERT INTO conversation_threads" in sql:
            values = _bound_insert_values(sql, params)
            row = dict(values)
            row.setdefault("created_at", datetime.now(timezone.utc))
            row.setdefault("updated_at", row["created_at"])
            # Keyed by the id the INSERT bound, as the primary key would be.
            self._store.rows[values["conversation_thread_id"]] = row
            self._row = row
        elif "MAX(thread_index)" in sql:
            self._row = {"next_index": len(self._store.rows)}
        elif "FROM workspaces" in sql:
            self._row = {"workspace_id": params[0]}
        elif "FROM conversation_threads" in sql:
            self._row = self._store.rows.get(params[0])
        else:
            self._row = None

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return []


class _StoreConnection:
    """A connection whose every cursor reads and writes the same row store."""

    def __init__(self, store):
        self._store = store

    def cursor(self, row_factory=None, **kwargs):
        store = self._store

        @asynccontextmanager
        async def _cursor_cm():
            yield _StoreCursor(store)

        return _cursor_cm()


class _RowStore:
    """The ``conversation_threads`` rows a mocked connection can hold.

    ``mock_cursor`` answers every statement with one canned value, so a test
    that wants to know what a send *stored* has nothing to read back from --
    which is why the earlier tests settled for comparing two reads of the
    request. This keeps the row an INSERT builds and answers the SELECTs the
    chain runs from it, so the read-back below goes through the real
    ``get_thread_by_id`` statement instead of a dict the test wrote itself.

    What it stores is only ever what the write path bound: a write that rewrote
    the platform would store the rewritten value, and the read-back would show
    it. A mocked connection cannot stand in for Postgres' constraints or its
    RETURNING projection beyond the columns written, so this is a row store and
    not a database; the statements, their order and their parameters are the
    production ones.
    """

    def __init__(self):
        self.rows: dict = {}
        self.executed: list = []

    @property
    def inserts(self):
        """Every INSERT's SQL and bound parameters, in the order they ran."""
        return [entry for entry in self.executed if "INSERT INTO conversation_threads" in entry[0]]

    def connection(self):
        return _StoreConnection(self)


class TestACustomMessageLandsOnTheRowUnchanged:
    """Drive the real INSERT chain and read back the custom surface value.

    Comparing two reads of the request would not catch a write that rewrote the
    platform. The row store instead keeps the values the production statements
    bind, so normalization anywhere in the chain fails these tests.
    """

    @pytest.fixture(autouse=True)
    def _no_existence_cache(self):
        """A disabled cache, so the chain always walks the database path
        instead of answering the existence probes from Redis."""
        with patch(
            "src.utils.cache.redis_cache.get_cache_client",
            return_value=SimpleNamespace(enabled=False, client=None),
        ):
            yield

    @pytest.fixture
    def store(self):
        """A connection backed by one row store, standing in for Postgres.

        ``pool.get_db_connection`` is the only thing replaced, so the chain --
        ``ensure_thread`` → ``ensure_thread_exists`` → ``create_thread`` -- runs
        unmodified against it.
        """
        rows = _RowStore()

        @asynccontextmanager
        async def _fake_get_db_connection(conn=None):
            # A passed connection is yielded unchanged, as the real one does.
            yield conn if conn is not None else rows.connection()

        with patch(
            "src.server.database.pool.get_db_connection",
            new=_fake_get_db_connection,
        ), patch(
            "src.server.database.pool.get_dedicated_connection",
            new=_fake_get_db_connection,
        ):
            yield rows

    @pytest.mark.asyncio
    async def test_the_insert_binds_the_platform_exactly_as_it_came(self, store):
        """The INSERT binds the custom surface exactly as sent.

        Reading the parameters ``create_thread`` handed psycopg catches a
        normalization anywhere between the call and the bind.
        """
        from src.server.database.conversation import create_thread

        await create_thread(
            conversation_thread_id=THREAD_ID,
            workspace_id=WORKSPACE_ID,
            current_status="in_progress",
            platform=CUSTOM,
        )

        (sql, params), = store.inserts
        assert "platform" in sql
        values = _bound_insert_values(sql, params)
        assert values["platform"] == CUSTOM
        assert values["platform"] != "web"

    @pytest.mark.asyncio
    async def test_the_row_it_stores_reads_back_as_the_custom_surface(self, store):
        """A read of the row the write created names the same surface.

        The row store is filled only by the INSERT's own bound parameters, and
        the read goes through the production ``get_thread_by_id`` SELECT, so
        this is the stored value rather than the value that was passed in. A
        read-back straight off a mocked connection is not reachable -- a mock
        answers a SELECT with whatever the test told it to -- which is why the
        store exists.
        """
        from src.server.database.conversation import (
            ensure_thread_exists,
            get_thread_by_id,
        )

        created = await ensure_thread_exists(
            workspace_id=WORKSPACE_ID,
            conversation_thread_id=THREAD_ID,
            user_id=USER_ID,
            initial_query="what moved?",
            platform=CUSTOM,
        )

        assert created is True
        assert len(store.inserts) == 1

        row = await get_thread_by_id(THREAD_ID)
        assert row is not None
        assert row["platform"] == CUSTOM
        assert row["platform"] != "web"

    @pytest.mark.asyncio
    async def test_the_message_entry_stores_and_labels_the_same_surface(self, store):
        """The request's own value reaches the row and the history label alike.

        ``ensure_thread`` is where a send hands the platform to the write chain,
        so the custom spelling has to survive that hand-off into the bound
        parameter. ``provenance_metadata`` is the turn's own label; the column
        is the thread's. Both name the surface the client sent.
        """
        from src.server.handlers.chat.request_prep import (
            ensure_thread,
            provenance_metadata,
        )
        from src.server.handlers.chat.request_prep import PriorThread
        from src.server.database.conversation import get_thread_by_id

        request = ChatRequest(platform=CUSTOM, workspace_id=WORKSPACE_ID)

        with patch(
            f"{PREP}._read_prior_thread",
            new_callable=AsyncMock,
            return_value=PriorThread(),
        ), patch(
            "src.server.services.thread_title.schedule_title_generation"
        ) as title:
            await ensure_thread(
                request,
                THREAD_ID,
                WORKSPACE_ID,
                USER_ID,
                initial_query="what moved?",
            )

        # The send created the row rather than resuming one, so the title
        # generator ran off the query that created it.
        title.assert_called_once()
        (sql, params), = store.inserts
        assert _bound_insert_values(sql, params)["platform"] == CUSTOM

        row = await get_thread_by_id(THREAD_ID)
        assert row is not None
        assert row["platform"] == request.platform == CUSTOM
        assert provenance_metadata(request)["platform"] == row["platform"]
