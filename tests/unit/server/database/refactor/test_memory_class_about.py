"""The projection reads and writes the ontology columns ``class`` and ``about``.

These pin the statement shape and the parameter normalization, not Postgres
behavior: the DDL and the index are covered by the migration test, and the
live path is exercised against a real database.

What can rot silently here is a query that drops a column from the row it
returns, writes a concept id into the wrong column, or filters on a value it
never normalized -- a stale concept would then keep matching a memory that no
longer names it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.server.database.refactor import memory

pytestmark = pytest.mark.asyncio

USER = "11111111-1111-4111-8111-111111111111"


async def test_upsert_writes_the_class_and_the_concept(mock_db_connection, mock_cursor):
    mock_cursor.fetchone.return_value = {
        "entry_id": "e1",
        "class": "Preference",
        "about": "person:leo",
    }
    await memory.upsert_entry(
        user_id=USER,
        tier="user",
        namespace=["owner", "memory"],
        entry_key="prefs/language",
        content_hash="sha256:abc",
        index_text="prefers Chinese",
        memory_class="Preference",
        about="person:leo",
    )
    sql, params = mock_cursor.execute.await_args.args
    assert '"class"' in sql and '"about"' in sql
    assert 'EXCLUDED."class"' in sql and 'EXCLUDED."about"' in sql
    # The two concept values are the last parameters, in the column order.
    assert params[-2:] == ("Preference", "person:leo")


async def test_a_memory_without_a_concept_stores_null_in_both(
    mock_db_connection, mock_cursor
):
    mock_cursor.fetchone.return_value = {"entry_id": "e1"}
    await memory.upsert_entry(
        user_id=USER,
        tier="user",
        namespace=["owner", "memory"],
        entry_key="legacy",
        content_hash="sha256:abc",
    )
    _sql, params = mock_cursor.execute.await_args.args
    assert params[-2:] == (None, None)


async def test_a_blank_concept_is_stored_as_null(mock_db_connection, mock_cursor):
    """A field that names nothing is NULL, not an empty concept."""
    mock_cursor.fetchone.return_value = {"entry_id": "e1"}
    await memory.upsert_entry(
        user_id=USER,
        tier="user",
        namespace=["owner", "memory"],
        entry_key="blank",
        content_hash="sha256:abc",
        memory_class="   ",
        about="",
    )
    _sql, params = mock_cursor.execute.await_args.args
    assert params[-2:] == (None, None)


async def test_the_returned_row_carries_the_two_columns(
    mock_db_connection, mock_cursor
):
    mock_cursor.fetchone.return_value = {"entry_id": "e1"}
    row = await memory.upsert_entry(
        user_id=USER,
        tier="user",
        namespace=["owner", "memory"],
        entry_key="k",
        content_hash="sha256:abc",
    )
    assert row == {"entry_id": "e1"}
    sql, _params = mock_cursor.execute.await_args.args
    assert '"class", "about"' in sql


async def test_list_entries_filters_by_class_and_about(
    mock_db_connection, mock_cursor
):
    mock_cursor.fetchall.return_value = []
    await memory.list_entries(
        user_id=USER,
        memory_class="Preference",
        about="person:leo",
    )
    sql, params = mock_cursor.execute.await_args.args
    # The class is the column (with the metadata as a fallback), and the
    # concept is matched against every concept the row declares.
    assert 'memory_entries."class"' in sql
    assert "= %s" in sql
    assert "jsonb_array_elements" in sql
    assert "Preference" in params
    assert "person:leo" in params


async def test_a_blank_filter_filters_nothing(mock_db_connection, mock_cursor):
    """A filter that names nothing must not narrow by accident."""
    mock_cursor.fetchall.return_value = []
    await memory.list_entries(user_id=USER, memory_class="  ", about="")
    sql, _params = mock_cursor.execute.await_args.args
    assert 'memory_entries."class") = %s' not in sql
    assert "WHERE concept = %s" not in sql


async def test_an_explicit_null_filter_reads_the_unnamed_group(
    mock_db_connection, mock_cursor
):
    """``None`` asks for the rows that name nothing, not for every row.

    The unnamed group is a group of its own, and a filter that names nothing
    is not the same as no filter at all.
    """
    mock_cursor.fetchall.return_value = []
    await memory.list_entries(user_id=USER, memory_class=None, about=None)
    sql, params = mock_cursor.execute.await_args.args
    assert 'memory_entries."class", memory_entries.metadata' in sql
    assert "IS NULL" in sql
    assert "NOT EXISTS" in sql
    # No ontology value is bound: there is no value to compare against.
    # The five are the owner, the status, the proposed pattern and the batch.
    assert len(params) == 5


async def test_an_unset_filter_reads_every_row(mock_db_connection, mock_cursor):
    """Leaving the filter out narrows nothing at all."""
    mock_cursor.fetchall.return_value = []
    await memory.list_entries(user_id=USER)
    sql, params = mock_cursor.execute.await_args.args
    assert "IS NULL" not in sql
    assert "jsonb_array_elements" not in sql
    assert len(params) == 5


async def test_the_rows_carry_every_concept_they_declare(
    mock_db_connection, mock_cursor
):
    """A memory about two things says so on the row, not only in the column."""
    mock_cursor.fetchall.return_value = [
        {
            "entry_id": "e1",
            "about": "project:conductor",
            "metadata": {"about": ["project:conductor", "person:leo"]},
        }
    ]
    rows = await memory.list_entries(user_id=USER)
    assert rows[0]["concepts"] == ["project:conductor", "person:leo"]


async def test_list_concepts_groups_by_the_pair(mock_db_connection, mock_cursor):
    mock_cursor.fetchall.return_value = [
        {"class": "Preference", "about": "person:leo", "entries": 3},
        {"class": None, "about": None, "entries": 1},
    ]
    rows = await memory.list_concepts(user_id=USER)
    sql, _params = mock_cursor.execute.await_args.args
    # The rail groups by the class the entry filter matches: the column, then
    # the metadata. Grouping by the column alone would list a federated row
    # under "unclassified" while the unclassified filter excludes it.
    assert "GROUP BY COALESCE(memory_entries.\"class\", memory_entries.metadata::jsonb->>'class'), c.concept" in sql
    assert rows[0] == {"class": "Preference", "about": "person:leo", "entries": 3}
    # A memory that names neither is reported, not dropped.
    assert rows[1] == {"class": None, "about": None, "entries": 1}


async def test_list_concepts_expands_every_declared_concept(
    mock_db_connection, mock_cursor
):
    """A memory about two things belongs to both groups.

    The ``about`` column holds one concept, so grouping by the column alone
    would drop the memory from the group of its second concept.
    """
    mock_cursor.fetchall.return_value = []
    await memory.list_concepts(user_id=USER)
    sql, _params = mock_cursor.execute.await_args.args
    assert "jsonb_array_elements" in sql
    assert "LEFT JOIN LATERAL" in sql
    assert "memory_entries.metadata::jsonb->'about'" in sql


async def test_list_concepts_hides_forgotten_memories(
    mock_db_connection, mock_cursor
):
    mock_cursor.fetchall.return_value = []
    await memory.list_concepts(user_id=USER)
    sql, _params = mock_cursor.execute.await_args.args
    assert "memory_forget_tombstones" in sql


async def test_the_restore_path_puts_the_two_columns_back(
    mock_db_connection, mock_cursor
):
    """The rollback path re-inserts the snapshot as it was; the concept is part
    of the row, so a restore that dropped it would change the memory."""
    mock_cursor.fetchall.return_value = []
    await memory.restore_projection_rows(
        user_id=USER,
        tier="user",
        entry_keys=["k"],
        rows=[
            {
                "entry_id": "22222222-2222-4222-8222-222222222222",
                "entry_key": "k",
                "namespace": ["owner", "memory"],
                "identity_hash": "h",
                "content_hash": "sha256:abc",
                "size": 1,
                "index_text": "text",
                "status": "active",
                "class": "Preference",
                "about": "person:leo",
            }
        ],
    )
    inserts = [
        call.args[0]
        for call in mock_cursor.execute.await_args_list
        if "INSERT INTO memory_entries" in str(call.args[0])
    ]
    assert inserts, "the restore did not insert the row"
    assert '"class", "about"' in inserts[0]


async def test_a_row_without_a_concept_restores_as_null(
    mock_db_connection, mock_cursor
):
    mock_cursor.fetchall.return_value = []
    await memory.restore_projection_rows(
        user_id=USER,
        tier="user",
        entry_keys=["k"],
        rows=[
            {
                "entry_id": "22222222-2222-4222-8222-222222222222",
                "entry_key": "k",
                "namespace": ["owner", "memory"],
                "identity_hash": "h",
                "content_hash": "sha256:abc",
                "size": 1,
                "index_text": "text",
                "status": "active",
            }
        ],
    )
    inserts = [
        call.args
        for call in mock_cursor.execute.await_args_list
        if "INSERT INTO memory_entries" in str(call.args[0])
    ]
    assert inserts, "the restore did not insert the row"
    assert inserts[0][1][-2:] == (None, None)


async def test_the_columns_are_read_back_with_the_row(mock_db_connection, mock_cursor):
    """Every read path returns the concept, or the browse page cannot group."""
    mock_cursor.fetchall.return_value = []
    await memory.list_entries(user_id=USER)
    sql, _params = mock_cursor.execute.await_args.args
    assert '"class"' in sql and '"about"' in sql


async def test_the_concept_field_is_bounded(mock_db_connection, mock_cursor):
    mock_cursor.fetchone.return_value = {"entry_id": "e1"}
    await memory.upsert_entry(
        user_id=USER,
        tier="user",
        namespace=["owner", "memory"],
        entry_key="k",
        content_hash="sha256:abc",
        memory_class="x" * 5000,
    )
    _sql, params = mock_cursor.execute.await_args.args
    assert len(params[-2]) == memory.MAX_CONCEPT_FIELD_LENGTH


async def test_an_ownerless_read_queries_nothing(mock_db_connection, mock_cursor):
    mock_cursor.execute = AsyncMock()
    assert await memory.list_concepts(user_id="") == []
    mock_cursor.execute.assert_not_awaited()


async def test_the_entries_continue_past_the_first_page(
    mock_db_connection, mock_cursor
):
    """The memories beyond the first hundred are reachable.

    The browse page reads a page at a time, so a listing that stops at the
    hundredth row leaves the older memories unreachable.
    """
    mock_cursor.fetchall.return_value = [
        {"entry_id": f"e{index}", "entry_key": f"key-{index}"} for index in range(250)
    ]

    rows = await memory.list_entries(user_id=USER, limit=100, offset=100)

    assert len(rows) == 100
    # The page starts where the first one ended.
    assert rows[0]["entry_id"] == "e100"
    assert rows[-1]["entry_id"] == "e199"


async def test_the_entry_listing_is_ordered_so_that_the_pages_do_not_overlap(
    mock_db_connection, mock_cursor
):
    """Two pages of one listing must not skip or repeat a row."""
    mock_cursor.fetchall.return_value = []
    await memory.list_entries(user_id=USER, limit=100, offset=100)
    sql, _params = mock_cursor.execute.await_args.args
    # The key breaks a tie on the timestamp, so the order is total.
    assert "ORDER BY updated_at DESC, entry_key ASC" in sql


async def test_a_page_keeps_reading_past_rows_that_are_no_longer_current(
    mock_db_connection, mock_cursor
):
    """Expired rows are dropped after the query, so the page reads on.

    A fixed window would come back short once the expired rows fill it, and
    the browse page takes a short page as the end of the listing.
    """
    expired = {"valid_until": "2000-01-01T00:00:00Z"}
    first = [
        {"entry_id": f"x{index}", "entry_key": f"x{index}", "metadata": expired}
        for index in range(memory._PAGE_BATCH)
    ]
    second = [{"entry_id": f"e{index}", "entry_key": f"e{index}"} for index in range(30)]
    mock_cursor.fetchall.side_effect = [first, second]

    rows = await memory.list_entries(user_id=USER, limit=20, offset=5)

    assert [row["entry_id"] for row in rows] == [f"e{index}" for index in range(5, 25)]
    offsets = [call.args[1][-1] for call in mock_cursor.execute.await_args_list]
    assert offsets == [0, memory._PAGE_BATCH]


async def test_a_short_batch_ends_the_listing(mock_db_connection, mock_cursor):
    mock_cursor.fetchall.return_value = [{"entry_id": "e1", "entry_key": "k"}]
    rows = await memory.list_entries(user_id=USER, limit=100, offset=0)
    assert [row["entry_id"] for row in rows] == ["e1"]
    assert mock_cursor.execute.await_count == 1


async def test_a_class_kept_in_the_metadata_is_the_row_class(
    mock_db_connection, mock_cursor
):
    """The row shows the class the filter matched, not only the column."""
    mock_cursor.fetchall.return_value = [
        {"entry_id": "e1", "entry_key": "k", "class": None, "metadata": {"class": "Lesson"}}
    ]
    rows = await memory.list_entries(user_id=USER, memory_class="Lesson")
    assert rows[0]["class"] == "Lesson"


async def test_the_concepts_continue_past_the_first_page(
    mock_db_connection, mock_cursor
):
    """The concept pairs beyond the first two hundred are reachable."""
    mock_cursor.fetchall.return_value = []
    await memory.list_concepts(user_id=USER, limit=200, offset=200)
    sql, params = mock_cursor.execute.await_args.args
    assert "LIMIT %s OFFSET %s" in sql
    assert params[-2:] == (200, 200)


async def test_a_negative_offset_is_refused(mock_db_connection, mock_cursor):
    """An offset before the start of the listing is a mistake, not a page."""
    mock_cursor.execute = AsyncMock()
    with pytest.raises(ValueError):
        await memory.list_entries(user_id=USER, offset=-1)
    with pytest.raises(ValueError):
        await memory.list_concepts(user_id=USER, offset=-1)
    mock_cursor.execute.assert_not_awaited()
