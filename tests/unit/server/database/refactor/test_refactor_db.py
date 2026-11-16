"""SQL-layer contracts for the refactor queries (mocked pool).

These tests pin statement shape and parameter normalization, not Postgres
behavior: the DDL and the CHECK constraints are covered by the migration tests,
and the live path is exercised by ``tests/integration/test_090_refactor_migrations.py``
plus the Phase 1 acceptance smoke. What can rot silently here is a query that
forgets to CAS, drops the tenant filter, or binds an unnormalized UUID.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.server.database.refactor import events, goals, memory, permissions, proposals, tasks
from src.server.utils.pg_sanitize import refactor_owner_uuid

pytestmark = pytest.mark.asyncio


async def _noop_proposal_write(_proposal):
    return None


class TestGoals:
    async def test_insert_uses_conflict_do_nothing_and_normalizes_ids(
        self, mock_db_connection, mock_cursor, monkeypatch
    ):
        goal_id = str(uuid4())
        mock_cursor.fetchone.return_value = {"goal_id": goal_id}
        spec = {
            "title": "Research",
            "objective": "Do it",
            "stop_conditions": ["done"],
            "budget": {"max_turns": 3},
            "verifier_config": {"model": "background"},
        }
        row = await goals.insert_goal(
            user_id="u1", thread_id=str(uuid4()).upper(), spec=spec
        )
        assert row == {"goal_id": goal_id}
        sql, params = mock_cursor.execute.await_args.args
        assert "INSERT INTO agent_goals" in sql
        assert "ON CONFLICT DO NOTHING" in sql
        assert params[1] == params[1].lower()  # canonical UUID, not the upper input
        assert params[3] == "Research"

    async def test_insert_rejects_invalid_thread_id(self, mock_db_connection):
        with pytest.raises(ValueError, match="thread_id"):
            await goals.insert_goal(
                user_id="u1",
                thread_id="not-a-uuid",
                spec={"title": "t", "objective": "o", "stop_conditions": ["c"]},
            )

    async def test_live_goal_reads_active_and_paused_only(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchone.return_value = None
        assert await goals.get_live_goal(str(uuid4())) is None
        sql, _params = mock_cursor.execute.await_args.args
        assert "status IN ('active', 'paused')" in sql

    async def test_transition_is_a_cas_on_expected_status(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchone.return_value = {"goal_id": "g", "status": "completed"}
        row = await goals.transition_goal(
            goal_id=str(uuid4()), expected_status="active", target_status="completed"
        )
        assert row == {"goal_id": "g", "status": "completed"}
        sql, params = mock_cursor.execute.await_args.args
        assert "AND status = %s" in sql
        # SafeJson binds as json; COALESCE needs the cast to match the jsonb column.
        assert "COALESCE(%s::jsonb, verifier_verdict)" in sql
        assert params[0] == "completed"
        assert params[1] == "completed"  # completed_at CASE arg
        assert params[4] == "active"

    async def test_transition_returns_none_when_another_writer_won(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchone.return_value = None
        assert (
            await goals.transition_goal(
                goal_id=str(uuid4()), expected_status="active", target_status="paused"
            )
            is None
        )

    async def test_plan_decide_is_cas_scoped(self, mock_db_connection, mock_cursor):
        mock_cursor.fetchone.return_value = {"plan_id": "p", "status": "approved"}
        await goals.decide_plan(
            plan_id=str(uuid4()),
            expected_status="awaiting_approval",
            target_status="approved",
            feedback="looks right",
        )
        sql, _params = mock_cursor.execute.await_args.args
        assert "WHERE plan_id = %s AND status = %s" in sql


class TestEvents:
    async def test_append_with_explicit_seq_does_not_compute(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchone.return_value = {"event_id": 1}
        await events.append_event(
            user_id="u1",
            event_type="goal.created",
            payload={"a": 1},
            thread_id=str(uuid4()),
            response_id=str(uuid4()),
            seq=7,
        )
        assert mock_cursor.execute.await_count == 1
        sql, params = mock_cursor.execute.await_args.args
        assert "INSERT INTO agent_events" in sql
        # Column names must stay real columns; the owner mapping is applied to
        # the bound value, not to the INSERT list (a bug the canary caught).
        assert (
            "(user_id, thread_id, response_id, task_run_id, seq, type, payload)" in sql
        )
        assert params[0] == refactor_owner_uuid("u1")
        assert params[4] == 7

    async def test_append_without_seq_computes_then_inserts(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchone.side_effect = [{"next_seq": 4}, {"event_id": 9}]
        await events.append_event(
            user_id="u1",
            event_type="goal.status_changed",
            payload={},
            response_id=str(uuid4()),
        )
        assert mock_cursor.execute.await_count == 2
        first_sql = mock_cursor.execute.await_args_list[0].args[0]
        assert "COALESCE(MAX(seq), 0) + 1" in first_sql
        assert mock_cursor.execute.await_args_list[1].args[1][4] == 4

    async def test_invalid_response_id_is_rejected(self, mock_db_connection):
        with pytest.raises(ValueError, match="response_id"):
            await events.append_event(
                user_id="u1",
                event_type="goal.created",
                payload={},
                response_id="nope",
            )

    async def test_list_events_is_bounded(self, mock_db_connection):
        with pytest.raises(ValueError, match="page size"):
            await events.list_events(thread_id=str(uuid4()), limit=0)
        with pytest.raises(ValueError, match="page size"):
            await events.list_events(thread_id=str(uuid4()), limit=1001)


class TestMemory:
    async def test_projection_upserts_on_identity_triple(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchone.return_value = {"entry_id": "e"}
        transactions = []

        @asynccontextmanager
        async def transaction():
            transactions.append("entered")
            yield mock_cursor

        mock_db_connection.transaction = transaction
        await memory.upsert_entry(
            user_id="u1",
            tier="user",
            namespace=["uid", "memory"],
            entry_key="prefs/language",
            content_hash="sha256:abc",
            index_text="prefers Chinese",
        )
        sql, params = mock_cursor.execute.await_args.args
        assert transactions == ["entered"]
        assert "ON CONFLICT (user_id, tier, entry_key, status) DO UPDATE" in sql
        assert params[3] == "prefs/language"

    async def test_search_uses_fts_rank_and_tier_filter(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        await memory.search_entries(user_id="u1", query="prefs", tier="user", limit=5)
        sql, params = mock_cursor.execute.await_args.args
        assert "websearch_to_tsquery('simple', %s)" in sql
        assert "ts_rank(search" in sql
        assert "status = 'active'" in sql
        assert "COALESCE(metadata->>'lifecycle', 'active') = 'active'" in sql
        # user, proposed filter, query, tier, rank query, limit
        assert params[1] == "proposed/%"
        assert params[2] == "prefs"
        assert params[3] == "user"

    async def test_empty_query_never_queries(self, mock_db_connection, mock_cursor):
        assert await memory.search_entries(user_id="u1", query="   ") == []
        mock_cursor.execute.assert_not_awaited()

    @pytest.mark.parametrize("tier", ["session", "task"])
    @pytest.mark.parametrize("thread_id", [None, ""])
    async def test_thread_tier_search_without_thread_never_queries(
        self, mock_db_connection, mock_cursor, tier, thread_id
    ):
        assert await memory.search_entries(
            user_id="u1", query="task", tier=tier, thread_id=thread_id
        ) == []
        mock_cursor.execute.assert_not_awaited()

    @pytest.mark.parametrize("tier", ["session", "task"])
    @pytest.mark.parametrize("thread_id", [None, ""])
    async def test_thread_tier_vector_candidates_without_thread_never_query(
        self, mock_db_connection, mock_cursor, tier, thread_id
    ):
        assert await memory.list_retrieval_rows(
            user_id="u1", tier=tier, thread_id=thread_id
        ) == []
        mock_cursor.execute.assert_not_awaited()

    async def test_mixed_cjk_search_excludes_both_thread_tiers(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        await memory.search_entries(user_id="u1", query="记忆内容", tier=None)
        sql, _params = mock_cursor.execute.await_args_list[1].args
        assert "tier NOT IN ('session', 'task')" in sql

    async def test_mixed_spaced_cjk_search_excludes_both_thread_tiers(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        await memory.search_entries(user_id="u1", query="项目 产品路线图", tier=None)
        sql, _params = mock_cursor.execute.await_args_list[1].args
        assert "ILIKE %s ESCAPE" in sql
        assert "tier NOT IN ('session', 'task')" in sql

    async def test_spaced_cjk_query_uses_contains_terms(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        await memory.search_entries(
            user_id="u1", query="项目 产品路线图", tier="workspace", limit=5
        )
        sql, params = mock_cursor.execute.await_args.args
        assert "websearch_to_tsquery" not in sql
        assert sql.count("ILIKE %s ESCAPE") == 2
        assert "word_similarity(%s, (entry_key || ' ' || index_text))" in sql
        assert params[1] == "proposed/%"
        assert params[2:4] == ("%项目%", "%产品路线图%")
        assert params[4] == "workspace"
        assert params[5] == "项目 产品路线图"

    async def test_unspaced_cjk_query_uses_word_similarity_operator(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        await memory.search_entries(user_id="u1", query="我关注的项目")
        set_sql, set_params = mock_cursor.execute.await_args_list[0].args
        assert set_sql == (
            "SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)"
        )
        assert set_params == (str(memory._WORD_SIMILARITY_THRESHOLD),)
        sql, params = mock_cursor.execute.await_args_list[1].args
        # Query on the left of the operator, indexed line on the right; the
        # doubled % is psycopg's literal-percent escape.
        assert "%s <%% (entry_key || ' ' || index_text)" in sql
        assert "word_similarity(%s, (entry_key || ' ' || index_text))" in sql
        assert "ILIKE" not in sql
        assert "tier NOT IN ('session', 'task')" in sql
        assert params == (
            refactor_owner_uuid("u1"),
            "proposed/%",
            "我关注的项目",
            "我关注的项目",
            100,
        )

    async def test_cjk_detection_and_like_escaping(self):
        assert memory.has_cjk("项目")
        assert memory.has_cjk("2026 规划")
        assert not memory.has_cjk("prefs/language")
        assert memory.like_pattern("a%b_c\\d") == "%a\\%b\\_c\\\\d%"

    async def test_update_index_texts_uses_one_unnest_update(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.rowcount = 2
        count = await memory.update_index_texts(
            user_id=str(uuid4()),
            tier="user",
            updates={"a.md": "A — hook", "b.md": "B — hook", "": "skip"},
        )
        assert count == 2
        sql, params = mock_cursor.execute.await_args.args
        assert "UPDATE memory_entries AS m" in sql
        assert "FROM unnest(%s::text[], %s::text[])" in sql
        assert "m.status = 'active'" in sql
        assert params[0] == ["a.md", "b.md"]
        assert params[1] == ["A — hook", "B — hook"]

    async def test_update_index_texts_rejects_unknown_tier(self):
        with pytest.raises(ValueError, match="unknown memory tier"):
            await memory.update_index_texts(user_id=str(uuid4()), tier="galactic", updates={"a": "b"})

    async def test_latin_query_keeps_fts_path(self, mock_db_connection, mock_cursor):
        mock_cursor.fetchall.return_value = []
        await memory.search_entries(user_id="u1", query="prefers")
        sql, _params = mock_cursor.execute.await_args.args
        assert "websearch_to_tsquery('simple', %s)" in sql
        assert "ILIKE" not in sql

    async def test_database_recall_excludes_secret_and_invalid_policy(self, monkeypatch):
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            memory,
            "_search_fts",
            AsyncMock(
                return_value=[
                    {"entry_key": "private.md", "metadata": {"sensitivity": "private"}},
                    {"entry_key": "secret.md", "metadata": {"sensitivity": "secret"}},
                    {"entry_key": "malformed.md", "metadata": "not-an-object"},
                    {"entry_key": "invalid.md", "metadata": {"sensitivity": "classified"}},
                    {"entry_key": "nested.md", "metadata": {"sensitivity": "public", "metadata": {"sensitivity": "secret"}}},
                ]
            ),
        )
        rows = await memory.search_entries(user_id="u1", query="memory", limit=10)
        assert [row["entry_key"] for row in rows] == ["private.md"]

    async def test_embedding_candidate_rows_exclude_secret(self, mock_db_connection, mock_cursor):
        mock_cursor.fetchall.return_value = [
            {"entry_key": "private.md", "metadata": {"sensitivity": "private"}},
            {"entry_key": "secret.md", "metadata": {"sensitivity": "secret"}},
            {"entry_key": "malformed.md", "metadata": "not-an-object"},
        ]
        rows = await memory.list_retrieval_rows(user_id="u1", limit=10)
        assert [row["entry_key"] for row in rows] == ["private.md"]

    async def test_project_tier_is_scoped_in_sql_before_limit(
        self, mock_db_connection, mock_cursor
    ):
        workspace_id = str(uuid4())
        mock_cursor.fetchall.return_value = []
        await memory.search_entries(
            user_id="u1",
            query="project",
            tier="project",
            workspace_id=workspace_id,
            limit=5,
        )
        sql, params = mock_cursor.execute.await_args.args
        assert "tier = %s" in sql
        assert "namespace[3] = %s" in sql
        # owner, proposed, query, tier, workspace, rank query, limit
        assert params[3] == "project"
        assert params[4] == workspace_id

    async def test_mixed_tier_workspace_scope_keeps_user_rows(
        self, mock_db_connection, mock_cursor
    ):
        workspace_id = str(uuid4())
        mock_cursor.fetchall.return_value = []
        await memory.search_entries(
            user_id="u1", query="fact", tier=None, workspace_id=workspace_id, limit=5
        )
        sql, params = mock_cursor.execute.await_args.args
        assert "(tier NOT IN ('workspace', 'project') OR namespace[3] = %s)" in sql
        assert "tier NOT IN ('session', 'task')" in sql
        assert params[3] == workspace_id

    async def test_mixed_tier_retrieval_excludes_thread_tiers_without_thread(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        await memory.list_retrieval_rows(user_id="u1", tier=None, limit=10)
        sql, _params = mock_cursor.execute.await_args.args
        assert "tier NOT IN ('session', 'task')" in sql

    async def test_task_search_with_thread_keeps_authorized_scope(
        self, mock_db_connection, mock_cursor
    ):
        thread_id = str(uuid4())
        mock_cursor.fetchall.return_value = []
        await memory.search_entries(
            user_id="u1", query="task", tier="task", thread_id=thread_id, limit=5
        )
        sql, params = mock_cursor.execute.await_args.args
        assert "tier = %s" in sql
        assert "namespace[3] = %s" in sql
        assert params[3:5] == ("task", thread_id)

    async def test_retrieval_rows_scope_project_before_limit(
        self, mock_db_connection, mock_cursor
    ):
        workspace_id = str(uuid4())
        mock_cursor.fetchall.return_value = []
        await memory.list_retrieval_rows(
            user_id="u1", tier="project", workspace_id=workspace_id, limit=10
        )
        sql, params = mock_cursor.execute.await_args.args
        assert "namespace[3] = %s" in sql
        assert params[3] == workspace_id

    async def test_supersede_moves_only_active_and_returns_count(
        self, mock_db_connection, mock_cursor
    ):
        transactions = []

        @asynccontextmanager
        async def transaction():
            transactions.append("entered")
            yield mock_cursor

        mock_db_connection.transaction = transaction
        mock_cursor.rowcount = 2
        assert (
            await memory.supersede_entry(user_id="u1", tier="user", entry_key="k")
            == 2
        )
        sql, _params = mock_cursor.execute.await_args.args
        assert "status = 'superseded'" in sql
        assert "active.status = 'active'" in sql
        assert "pg_advisory_xact_lock" in sql or any(
            "pg_advisory_xact_lock" in call.args[0]
            for call in mock_cursor.execute.await_args_list
        )
        assert "active.metadata::jsonb" in sql
        assert "::json" in sql
        assert "lifecycle" in sql and "superseded" in sql
        assert transactions == ["entered"]

    async def test_active_read_hides_collision_fallback_lifecycle(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchone.return_value = {
            "entry_id": "e",
            "metadata": {"lifecycle": "superseded"},
        }
        assert await memory.get_entry(
            user_id="u1", tier="user", entry_key="k"
        ) is None

    async def test_backfill_collision_retry_preserves_ids_and_recall_state(
        self, mock_db_connection, mock_cursor
    ):
        active = {
            "entry_id": "active-id",
            "user_id": refactor_owner_uuid("u1"),
            "tier": "user",
            "namespace": ["u1", "memory"],
            "entry_key": "fact.md",
            "content_hash": "sha256:active-before",
            "size": 10,
            "index_text": "old fact",
            "status": "active",
            "metadata": {"lifecycle": "active"},
        }
        snapshot = {
            **active,
            "entry_id": "snapshot-id",
            "content_hash": "sha256:backfilled",
            "index_text": "old fact",
            "status": "superseded",
            "metadata": {"lifecycle": "superseded"},
        }
        rows = [active, snapshot]
        selected = None
        queries = []

        async def execute(sql, params=None):
            nonlocal selected
            query = str(sql)
            normalized_query = query.lstrip()
            queries.append(query)
            if normalized_query.startswith("SELECT pg_advisory_xact_lock"):
                return
            if normalized_query.startswith("UPDATE memory_entries AS active"):
                active_row = next(row for row in rows if row["status"] == "active")
                has_snapshot = any(row["status"] == "superseded" for row in rows)
                if has_snapshot:
                    active_row["metadata"] = {
                        **active_row["metadata"], "lifecycle": "superseded"
                    }
                else:
                    active_row["status"] = "superseded"
                mock_cursor.rowcount = 1
                return
            if normalized_query.startswith("INSERT INTO memory_entries"):
                key, status = params[3], params[10]
                selected = next(
                    (row for row in rows if row["entry_key"] == key and row["status"] == status),
                    None,
                )
                if selected is None:
                    selected = {"entry_id": f"inserted-{status}"}
                    rows.append(selected)
                selected.update({
                    "entry_key": key,
                    "status": status,
                    "content_hash": params[7],
                    "index_text": params[9],
                    "metadata": params[12].obj,
                })
                return
            if normalized_query.startswith("SELECT") and "FROM memory_entries" in query:
                if "entry_key = %s" in query:
                    status = params[3]
                    selected = next(
                        (row for row in rows if row["entry_key"] == params[2]
                         and row["status"] == status),
                        None,
                    )

        async def fetchone():
            return selected

        async def fetchall():
            return [
                row for row in rows
                if row["status"] == "active"
                and row["metadata"].get("lifecycle", "active") == "active"
            ]

        mock_cursor.execute.side_effect = execute
        mock_cursor.fetchone.side_effect = fetchone
        mock_cursor.fetchall.side_effect = fetchall

        for text in ("retry one", "retry two"):
            assert await memory.supersede_entry(
                user_id="u1", tier="user", entry_key="fact.md"
            ) == 1
            await memory.upsert_entry(
                user_id="u1", tier="user", namespace=["u1", "memory"],
                entry_key="fact.md", content_hash=f"sha256:{text}",
                index_text=text, status="superseded",
                metadata={"lifecycle": "superseded"},
            )
            assert await memory.get_entry(
                user_id="u1", tier="user", entry_key="fact.md", status="active"
            ) is None
            assert await memory.list_entries(user_id="u1", tier="user") == []
            assert {row["entry_id"] for row in rows} == {"active-id", "snapshot-id"}

        await memory.upsert_entry(
            user_id="u1", tier="user", namespace=["u1", "memory"],
            entry_key="fact.md", content_hash="sha256:active-again",
            index_text="current fact", status="active",
            metadata={"lifecycle": "active"},
        )
        recalled = await memory.list_entries(user_id="u1", tier="user")
        assert [row["entry_id"] for row in recalled] == ["active-id"]
        assert "pg_advisory_xact_lock" in " ".join(queries)

    async def test_approval_reader_keeps_unfiltered_active_identity(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchone.return_value = {
            "entry_id": "e",
            "metadata": {"lifecycle": "superseded", "valid_until": "2000-01-01"},
        }
        row = await memory.get_active_entry_for_approval(
            user_id="u1", tier="user", entry_key="k"
        )
        assert row["entry_id"] == "e"


class TestTasks:
    async def test_insert_bounds_title_and_normalizes_thread(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchone.return_value = {"task_node_id": "t"}
        await tasks.insert_task(
            user_id="u1", thread_id=str(uuid4()), title="x" * 900, status="pending"
        )
        _sql, params = mock_cursor.execute.await_args.args
        assert len(params[3]) == 500

    async def test_invalid_ids_rejected(self, mock_db_connection):
        with pytest.raises(ValueError, match="thread_id"):
            await tasks.insert_task(user_id="u1", thread_id="bad", title="t")

    async def test_update_returns_none_when_missing(self, mock_db_connection, mock_cursor):
        mock_cursor.fetchone.return_value = None
        assert (
            await tasks.update_task_status(task_node_id=str(uuid4()), status="completed")
            is None
        )

    async def test_list_is_bounded(self, mock_db_connection):
        with pytest.raises(ValueError, match="page size"):
            await tasks.list_tasks(thread_id=str(uuid4()), limit=2001)


async def test_latest_plan_selects_highest_version(mock_db_connection, mock_cursor):
    mock_cursor.fetchone.return_value = {"plan_id": "p", "version": 3}
    row = await goals.get_latest_plan(str(uuid4()))
    assert row == {"plan_id": "p", "version": 3}
    sql, _params = mock_cursor.execute.await_args.args
    assert "ORDER BY version DESC LIMIT 1" in sql
    assert "WHERE goal_id = %s" in sql


class TestTodoSnapshotSync:
    async def test_resolves_owner_upserts_and_cancels(
        self, mock_db_connection, mock_cursor
    ):
        thread_id = str(uuid4())
        mock_cursor.fetchone.side_effect = [
            {"user_id": "user-1"},
        ]
        mock_cursor.fetchall.return_value = [
            {"title": "a", "status": "completed"},
            {"title": "b", "status": "pending"},
        ]
        rows = await tasks.sync_todo_snapshot(
            thread_id=thread_id,
            todos=[{"title": "a", "status": "completed"}, {"title": "b", "status": "pending"}],
        )
        assert rows == [
            {"title": "a", "status": "completed"},
            {"title": "b", "status": "pending"},
        ]
        calls = mock_cursor.execute.await_args_list
        assert "JOIN workspaces" in calls[0].args[0]
        upsert_sql, upsert_params = calls[1].args
        assert "ON CONFLICT (thread_id, title) WHERE source = 'todo'" in upsert_sql
        assert "unnest(%s::text[], %s::text[])" in upsert_sql
        assert upsert_params[0] == refactor_owner_uuid("user-1")
        assert upsert_params[2] == ["a", "b"]
        cancel_sql, cancel_params = calls[2].args
        assert "status NOT IN ('completed', 'cancelled')" in cancel_sql
        assert "title <> ALL(%s::text[])" in cancel_sql
        assert cancel_params[1] == ["a", "b"]
        assert "WHERE thread_id = %s AND source = 'todo'" in calls[3].args[0]

    async def test_empty_snapshot_cancels_all_open(self, mock_db_connection, mock_cursor):
        mock_cursor.fetchone.return_value = {"user_id": "user-1"}
        mock_cursor.fetchall.return_value = []
        assert (
            await tasks.sync_todo_snapshot(thread_id=str(uuid4()), todos=[]) == []
        )
        sql, _params = mock_cursor.execute.await_args_list[1].args
        assert "status NOT IN ('completed', 'cancelled')" in sql
        assert "unnest" not in sql

    async def test_unknown_thread_returns_none(self, mock_db_connection, mock_cursor):
        mock_cursor.fetchone.return_value = None
        assert (
            await tasks.sync_todo_snapshot(thread_id=str(uuid4()), todos=[]) is None
        )

    async def test_invalid_thread_id_rejected(self, mock_db_connection):
        with pytest.raises(ValueError, match="thread_id"):
            await tasks.sync_todo_snapshot(thread_id="bad", todos=[])


class TestPermissions:
    async def test_get_mode_reads_the_thread_row(self, mock_db_connection, mock_cursor):
        thread_id = str(uuid4())
        mock_cursor.fetchone.return_value = {"thread_id": thread_id, "mode": "manual"}
        row = await permissions.get_mode(thread_id)
        assert row["mode"] == "manual"
        sql, params = mock_cursor.execute.await_args.args
        assert "FROM agent_thread_permissions" in sql
        assert params == (thread_id,)

    async def test_get_mode_missing_row_is_none(self, mock_db_connection, mock_cursor):
        mock_cursor.fetchone.return_value = None
        assert await permissions.get_mode(str(uuid4())) is None

    async def test_set_mode_upserts(self, mock_db_connection, mock_cursor):
        thread_id = str(uuid4())
        mock_cursor.fetchone.return_value = {"thread_id": thread_id, "mode": "manual"}
        row = await permissions.set_mode(
            thread_id=thread_id, mode="manual", updated_by="user-1"
        )
        assert row["mode"] == "manual"
        sql, params = mock_cursor.execute.await_args.args
        assert "ON CONFLICT (thread_id) DO UPDATE" in sql
        assert params == (thread_id, "manual", "user-1")

    async def test_set_mode_rejects_non_uuid_thread(self, mock_db_connection):
        with pytest.raises(ValueError, match="thread_id"):
            await permissions.set_mode(thread_id="not-a-uuid", mode="manual")


class TestProposalsDb:
    async def test_insert_is_idempotent_per_identity(self, mock_db_connection, mock_cursor):
        mock_cursor.rowcount = 1
        inserted = await proposals.insert_proposals(
            user_id=str(uuid4()),
            thread_id=str(uuid4()),
            task_run_id=str(uuid4()),
            proposals=[
                {"type": "feedback", "text": "cite sources", "text_hash": "h", "proposal_key": "k"},
                {"type": "galactic", "text": "no", "text_hash": "h2", "proposal_key": "k2"},
            ],
        )
        assert inserted == 1
        sql, params = mock_cursor.execute.await_args.args
        assert "ON CONFLICT (task_run_id, proposal_type, text_hash) DO NOTHING" in sql
        assert params[3] == "feedback"
        assert "origin, confidence, evidence_refs, evidence_count, counts_as_evidence" in " ".join(sql.split())
        assert params[7] == "assistant_inference"
        assert params[8] == 0.5
        assert params[9].obj == []
        assert params[10:12] == (0, False)
        assert params[12:15] == ("long_term", None, None)
        assert params[15].obj == {"sensitivity": "private"}

    async def test_once_is_rejected_before_database_access(self, monkeypatch):
        def fail_connection():
            raise AssertionError("once proposal must not reach the database")

        monkeypatch.setattr(proposals.pool, "get_db_connection", fail_connection)
        inserted = await proposals.insert_proposals(
            user_id=str(uuid4()),
            thread_id=str(uuid4()),
            task_run_id=str(uuid4()),
            proposals=[
                {
                    "type": "feedback",
                    "text": "only this turn",
                    "text_hash": "hash",
                    "proposal_key": "key",
                    "change_kind": "once",
                }
            ],
        )
        assert inserted == 0

    async def test_insert_preserves_secret_metadata_for_local_owner_review(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.rowcount = 1
        inserted = await proposals.insert_proposals(
            user_id=str(uuid4()),
            thread_id=str(uuid4()),
            task_run_id=str(uuid4()),
            proposals=[
                {
                    "type": "user",
                    "text": "private local fact",
                    "text_hash": "secret-hash",
                    "proposal_key": "private-local",
                    "metadata": {
                        "sensitivity": "secret",
                        "applies_when": "inside this account",
                    },
                }
            ],
        )
        assert inserted == 1
        _sql, params = mock_cursor.execute.await_args.args
        assert params[15].obj == {
            "sensitivity": "secret",
            "applies_when": "inside this account",
        }

    async def test_insert_preserves_typed_tool_policy_for_owner_review(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.rowcount = 1
        await proposals.insert_proposals(
            user_id=str(uuid4()),
            thread_id=str(uuid4()),
            task_run_id=str(uuid4()),
            proposals=[
                {
                    "type": "user",
                    "text": "Do not use shell execution.",
                    "text_hash": "typed-policy-hash",
                    "proposal_key": "typed-policy",
                    "kind": "constraint",
                    "denied_tools": [{"type": "tool", "name": "Bash"}],
                    "valid_from": "2099-01-01T00:00:00Z",
                    "metadata": {"sensitivity": "secret"},
                }
            ],
        )
        _sql, params = mock_cursor.execute.await_args.args
        assert params[15].obj == {
            "sensitivity": "secret",
            "kind": "constraint",
            "denied_tools": [{"type": "tool", "name": "Bash"}],
            "valid_from": "2099-01-01T00:00:00+00:00",
        }

    async def test_insert_proposal_for_run_checks_owner_and_returns_queue_id(
        self, mock_db_connection, mock_cursor
    ):
        proposal_id = str(uuid4())
        user_id, thread_id, task_run_id = str(uuid4()), str(uuid4()), str(uuid4())
        mock_cursor.fetchone.return_value = {"proposal_id": proposal_id}
        inserted = await proposals.insert_proposal_for_run(
            user_id=user_id,
            thread_id=thread_id,
            task_run_id=task_run_id,
            proposal_type="feedback",
            proposal_key="feedback-abc123",
            text="cite sources",
            text_hash="abc123",
            origin="web",
            confidence=0.9,
            evidence_refs=(
                {"kind": "web_source", "id": "source-1", "verified": True},
            ),
        )
        assert inserted == proposal_id
        sql, params = mock_cursor.execute.await_args.args
        assert "JOIN workspaces w ON w.workspace_id = t.workspace_id" in sql
        assert "JOIN subagent_runs r" in sql
        assert "w.user_id = %s" in sql
        assert "r.status = 'completed'" in sql
        assert "RETURNING proposal_id::text AS proposal_id" in sql
        assert "origin, confidence, evidence_refs, evidence_count, counts_as_evidence" in " ".join(sql.split())
        assert params[5:7] == ("web", 0.9)
        assert params[7].obj == [
            {"kind": "web_source", "id": "source-1", "verified": False}
        ]
        assert params[8:10] == (0, False)
        assert params[13].obj == {"sensitivity": "private"}
        assert params[-3] == thread_id
        assert params[-2] == user_id
        assert params[-1] == task_run_id

    async def test_insert_proposal_for_run_preserves_typed_tool_policy(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchone.return_value = {"proposal_id": str(uuid4())}
        user_id, thread_id, task_run_id = str(uuid4()), str(uuid4()), str(uuid4())
        await proposals.insert_proposal_for_run(
            user_id=user_id,
            thread_id=thread_id,
            task_run_id=task_run_id,
            proposal_type="user",
            proposal_key="typed-policy",
            text="Do not use shell execution.",
            text_hash="typed-policy-hash",
            metadata={
                "kind": "constraint",
                "denied_tools": [{"type": "tool", "name": "Bash"}],
                "valid_until": "2099-01-01",
            },
        )
        _sql, params = mock_cursor.execute.await_args.args
        assert params[13].obj == {
            "sensitivity": "private",
            "kind": "constraint",
            "denied_tools": [{"type": "tool", "name": "Bash"}],
            "valid_until": "2099-01-01T00:00:00+00:00",
        }

    async def test_insert_proposal_for_run_rejects_invalid_tier_without_database(
        self, mock_db_connection, mock_cursor
    ):
        inserted = await proposals.insert_proposal_for_run(
            user_id=str(uuid4()),
            thread_id=str(uuid4()),
            task_run_id=str(uuid4()),
            proposal_type="workspace",
            proposal_key="k",
            text="fact",
            text_hash="h",
        )
        assert inserted is None
        mock_cursor.execute.assert_not_awaited()

    async def test_list_filters_owner_and_status(self, mock_db_connection, mock_cursor):
        mock_cursor.fetchall.return_value = [{"proposal_id": "p1", "status": "proposed"}]
        rows = await proposals.list_proposals(user_id=str(uuid4()), limit=5)
        assert rows == [{"proposal_id": "p1", "status": "proposed"}]
        sql, params = mock_cursor.execute.await_args.args
        assert "WHERE user_id = %s AND status = %s" in sql
        assert "sr.parent_run_id::text FROM subagent_runs sr" in sql
        assert ") AS source_response_id" in sql
        assert "sr.task_run_id = agent_memory_proposals.task_run_id" in sql
        assert params[1] == "proposed"

    async def test_upsert_rejects_a_tombstoned_identity(
        self, mock_db_connection, mock_cursor
    ):
        """A stale backfill must never recreate a forgotten row's plaintext."""
        mock_cursor.fetchone.return_value = None
        row = await memory.upsert_entry(
            user_id=str(uuid4()),
            tier="user",
            namespace=("u1", "memory"),
            entry_key="fact.md",
            content_hash="sha256:x",
            index_text="remembered detail",
        )
        assert row == {}
        sql, _params = mock_cursor.execute.await_args.args
        assert "memory_forget_tombstones" in sql
        assert "f.identity_hash = v.identity_hash" in sql

    async def test_projection_restore_recreates_parent_edges_it_deleted(
        self, mock_db_connection, mock_cursor
    ):
        entry_id = str(uuid4())
        parent_id = str(uuid4())
        snapshot = {
            "entry_id": entry_id,
            "user_id": str(uuid4()),
            "tier": "user",
            "namespace": ["u1", "memory"],
            "entry_key": "child.md",
            "identity_hash": "idh",
            "content_hash": "sha256:x",
            "size": 1,
            "index_text": "child",
            "status": "active",
            "provenance": {},
            "metadata": {},
            "source_thread_id": None,
            "source_response_id": None,
            "source_entry_id": parent_id,
            "created_at": None,
            "updated_at": None,
        }
        # The snapshot row is the PARENT of a surviving child: the edge must be
        # captured on both directions and restored.
        mock_cursor.fetchall.side_effect = [
            [{"entry_id": "child-entry", "source_entry_id": entry_id}],
            [],
        ]
        await memory.restore_projection_rows(
            user_id=str(uuid4()),
            tier="user",
            entry_keys=["child.md"],
            rows=[snapshot],
            remove_entry_ids=[],
        )
        lineage_inserts = [
            call.args
            for call in mock_cursor.execute.await_args_list
            if "INSERT INTO memory_entry_lineage" in call.args[0]
        ]
        assert lineage_inserts
        _sql, params = lineage_inserts[0]
        assert "child-entry" in params and entry_id in params

    async def test_projection_snapshot_round_trip_preserves_identity_and_lineage(
        self, mock_db_connection, mock_cursor
    ):
        entry_id = str(uuid4())
        source_id = str(uuid4())
        row = {
            "entry_id": entry_id,
            "user_id": str(uuid4()),
            "tier": "user",
            "namespace": ["u1", "memory"],
            "entry_key": "fact.md",
            "identity_hash": "identity-hash",
            "content_hash": "sha256:x",
            "size": 3,
            "index_text": "fact",
            "status": "active",
            "provenance": {},
            "metadata": {},
            "source_thread_id": None,
            "source_response_id": None,
            "source_entry_id": source_id,
            "created_at": None,
            "updated_at": None,
        }
        mock_cursor.fetchall.side_effect = [[], [{"entry_id": entry_id}]]
        await memory.restore_projection_rows(
            user_id=str(uuid4()),
            tier="user",
            entry_keys=["fact.md"],
            rows=[row],
            remove_entry_ids=[],
        )
        inserts = [
            call.args
            for call in mock_cursor.execute.await_args_list
            if "INSERT INTO memory_entries" in call.args[0]
        ]
        assert inserts
        insert_sql, insert_params = inserts[0]
        assert "entry_id" in insert_sql
        assert "identity_hash" in insert_sql
        assert entry_id in insert_params
        assert "identity-hash" in insert_params
        assert source_id in insert_params

    async def test_decide_is_a_cas(self, mock_db_connection, mock_cursor):
        mock_cursor.fetchone.side_effect = [
            None,
            {"proposal_id": "p1", "status": "proposed"},
            {"proposal_id": "p1", "status": "approved"},
        ]
        owner_id = str(uuid4())
        row = await proposals.decide_proposal(
            proposal_id=str(uuid4()),
            user_id=owner_id,
            expected_status="proposed",
            target_status="approved",
            approval_source="manual",
            before_decide=_noop_proposal_write,
        )
        assert row["status"] == "approved"
        assert mock_cursor.execute.await_count == 3
        owner_lock_sql, owner_lock_params = mock_cursor.execute.await_args_list[0].args
        assert "pg_advisory_xact_lock(hashtextextended(%s, 0))" in owner_lock_sql
        assert owner_lock_params[0].endswith(owner_id)
        lock_sql, lock_params = mock_cursor.execute.await_args_list[1].args
        assert "WHERE proposal_id = %s AND user_id = %s FOR UPDATE" in lock_sql
        assert len(lock_params) == 2
        update_sql, update_params = mock_cursor.execute.await_args_list[2].args
        assert "WHERE proposal_id = %s AND user_id = %s AND status = %s" in update_sql
        assert update_params[-1] == "proposed"

    async def test_approval_before_decide_can_use_a_single_slot_pool(
        self, mock_db_connection, mock_cursor, monkeypatch
    ):
        """The approval transaction must not hold a shared-pool slot: the
        source-of-truth write in before_decide acquires its own connection."""
        mock_cursor.fetchone.side_effect = [
            None,
            {"proposal_id": "p1", "status": "proposed"},
            {"proposal_id": "p1", "status": "approved"},
        ]
        owner_id = str(uuid4())
        in_use = 0

        @asynccontextmanager
        async def _single_slot(conn=None):
            nonlocal in_use
            in_use += 1
            assert in_use == 1, "approval holds a shared-pool slot across before_decide"
            try:
                yield mock_db_connection
            finally:
                in_use -= 1

        monkeypatch.setattr(
            "src.server.database.pool.get_db_connection", _single_slot
        )

        async def before(_proposal):
            async with proposals.pool.get_db_connection():
                pass

        row = await proposals.decide_proposal(
            proposal_id=str(uuid4()),
            user_id=owner_id,
            expected_status="proposed",
            target_status="approved",
            approval_source="manual",
            before_decide=before,
        )

        assert row["status"] == "approved"

    async def test_approval_requires_manual_source_write(self, mock_db_connection):
        with pytest.raises(ValueError, match="source-of-truth write"):
            await proposals.decide_proposal(
                proposal_id=str(uuid4()),
                user_id=str(uuid4()),
                expected_status="proposed",
                target_status="approved",
                approval_source="manual",
            )

    async def test_store_failure_prevents_proposal_status_update(
        self, mock_db_connection, mock_cursor
    ):
        mock_cursor.fetchone.side_effect = [
            None,
            {"proposal_id": "p1", "status": "proposed"},
        ]

        async def fail_store(_proposal):
            raise RuntimeError("store unavailable")

        with pytest.raises(RuntimeError, match="store unavailable"):
            await proposals.decide_proposal(
                proposal_id=str(uuid4()),
                user_id=str(uuid4()),
                expected_status="proposed",
                target_status="approved",
                approval_source="manual",
                before_decide=fail_store,
            )
        assert mock_cursor.execute.await_count == 2

    @pytest.mark.parametrize(
        "locked_row",
        [None, {"proposal_id": "p1", "user_id": "owner", "status": "rejected"}],
        ids=["not-owned-or-missing", "stale-decision"],
    )
    async def test_missing_or_stale_proposal_never_writes_store(
        self, mock_db_connection, mock_cursor, locked_row
    ):
        mock_cursor.fetchone.side_effect = [None, locked_row]
        before_decide = AsyncMock()
        row = await proposals.decide_proposal(
            proposal_id=str(uuid4()),
            user_id=str(uuid4()),
            expected_status="proposed",
            target_status="approved",
            approval_source="manual",
            before_decide=before_decide,
        )
        before_decide.assert_not_awaited()
        if locked_row is None:
            assert row is None
        else:
            assert row["status"] == "rejected"
        assert mock_cursor.execute.await_count == 2
