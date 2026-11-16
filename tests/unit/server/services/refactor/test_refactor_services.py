"""Service-layer contracts: flag gating, validation, and what reaches the SQL layer.

The SQL layer is mocked here; the real statements are covered by
``tests/unit/server/database/refactor/test_refactor_db.py`` and the migrations by
the Postgres integration test. These tests pin the dual-write contract: flag off
must be a strict no-op, flag on must validate before touching the database, and
event payloads must be redacted before the insert.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from src.server.contracts.refactor import ContractError, GoalSpec
from src.server.database.refactor import events as db_events
from src.server.database.refactor import goals as db_goals
from src.server.database.refactor import memory as db_memory
from src.server.database.refactor import tasks as db_tasks
from src.server.services.refactor import events as svc_events
from src.server.database.refactor import permissions as db_permissions
from src.server.services.refactor import goals as svc_goals
from src.server.services.refactor import permissions as svc_permissions
from src.server.services.refactor import memory as svc_memory
from src.server.services.refactor import tasks as svc_tasks
from src.server.services.refactor.redaction import REDACTED

pytestmark = pytest.mark.asyncio


@pytest.fixture
def goals_flag(monkeypatch):
    monkeypatch.setenv("REFACTOR_GOALS_ENABLED", "1")


@pytest.fixture
def events_flag(monkeypatch):
    monkeypatch.setenv("REFACTOR_EVENTS_ENABLED", "1")


@pytest.fixture
def memory_flag(monkeypatch):
    monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")


@pytest.fixture
def tasks_flag(monkeypatch):
    monkeypatch.setenv("REFACTOR_TASK_TREE_ENABLED", "1")


SPEC = {
    "title": "Research the atlas release",
    "objective": "Summarize changelogs",
    "stop_conditions": ["each release has a citation"],
}


class TestGoalsService:
    async def test_flag_off_is_a_noop(self, monkeypatch):
        insert = AsyncMock()
        monkeypatch.setattr(db_goals, "insert_goal", insert)
        assert (
            await svc_goals.create_goal(user_id="u", thread_id="t", spec=SPEC) is None
        )
        insert.assert_not_awaited()

    async def test_flag_on_validates_then_writes(self, goals_flag, monkeypatch):
        insert = AsyncMock(return_value={"goal_id": "g"})
        monkeypatch.setattr(db_goals, "insert_goal", insert)
        assert (
            await svc_goals.create_goal(user_id="u", thread_id="t", spec=SPEC)
            == {"goal_id": "g"}
        )
        kwargs = insert.await_args.kwargs
        assert kwargs["spec"]["stop_conditions"] == ["each release has a citation"]

    async def test_invalid_spec_never_reaches_database(self, goals_flag, monkeypatch):
        insert = AsyncMock()
        monkeypatch.setattr(db_goals, "insert_goal", insert)
        with pytest.raises(ContractError, match="stop condition"):
            await svc_goals.create_goal(
                user_id="u", thread_id="t", spec={"title": "t", "objective": "o"}
            )
        insert.assert_not_awaited()

    async def test_live_goal_conflict_is_typed(self, goals_flag, monkeypatch):
        monkeypatch.setattr(db_goals, "insert_goal", AsyncMock(return_value=None))
        with pytest.raises(ContractError, match="live goal"):
            await svc_goals.create_goal(user_id="u", thread_id="t", spec=SPEC)

    async def test_goal_spec_instance_is_accepted(self, goals_flag, monkeypatch):
        insert = AsyncMock(return_value={"goal_id": "g"})
        monkeypatch.setattr(db_goals, "insert_goal", insert)
        await svc_goals.create_goal(
            user_id="u", thread_id="t", spec=GoalSpec.from_mapping(SPEC)
        )
        insert.assert_awaited_once()

    async def test_transition_validates_then_cas(self, goals_flag, monkeypatch):
        monkeypatch.setattr(
            db_goals, "get_goal", AsyncMock(return_value={"status": "active"})
        )
        transition = AsyncMock(return_value={"status": "completed"})
        monkeypatch.setattr(db_goals, "transition_goal", transition)
        assert await svc_goals.transition_goal(
            goal_id="g", target_status="completed"
        ) == {"status": "completed"}
        assert transition.await_args.kwargs["expected_status"] == "active"

    async def test_illegal_transition_never_cas(self, goals_flag, monkeypatch):
        monkeypatch.setattr(
            db_goals, "get_goal", AsyncMock(return_value={"status": "completed"})
        )
        transition = AsyncMock()
        monkeypatch.setattr(db_goals, "transition_goal", transition)
        with pytest.raises(ContractError, match="illegal goal transition"):
            await svc_goals.transition_goal(goal_id="g", target_status="active")
        transition.assert_not_awaited()

    async def test_missing_goal_is_typed(self, goals_flag, monkeypatch):
        monkeypatch.setattr(db_goals, "get_goal", AsyncMock(return_value=None))
        with pytest.raises(svc_goals.GoalNotFoundError):
            await svc_goals.transition_goal(goal_id="g", target_status="paused")

    async def test_lost_cas_race_is_typed(self, goals_flag, monkeypatch):
        monkeypatch.setattr(
            db_goals, "get_goal", AsyncMock(return_value={"status": "active"})
        )
        monkeypatch.setattr(db_goals, "transition_goal", AsyncMock(return_value=None))
        with pytest.raises(svc_goals.GoalTransitionConflictError):
            await svc_goals.transition_goal(goal_id="g", target_status="completed")

    async def test_plan_review_requires_reviewable_state(self, goals_flag, monkeypatch):
        monkeypatch.setattr(
            db_goals, "get_plan", AsyncMock(return_value={"status": "approved"})
        )
        decide = AsyncMock()
        monkeypatch.setattr(db_goals, "decide_plan", decide)
        with pytest.raises(ContractError, match="illegal plan transition"):
            await svc_goals.decide_plan(plan_id="p", approve=False)
        decide.assert_not_awaited()

    async def test_plan_approval_cas(self, goals_flag, monkeypatch):
        monkeypatch.setattr(
            db_goals,
            "get_plan",
            AsyncMock(return_value={"status": "awaiting_approval"}),
        )
        decide = AsyncMock(return_value={"status": "approved"})
        monkeypatch.setattr(db_goals, "decide_plan", decide)
        await svc_goals.decide_plan(plan_id="p", approve=True, feedback="ok")
        assert decide.await_args.kwargs["target_status"] == "approved"


class TestEventsService:
    async def test_flag_off_is_a_noop(self, monkeypatch):
        append = AsyncMock()
        monkeypatch.setattr(db_events, "append_event", append)
        assert (
            await svc_events.record_event(
                user_id="u", event_type="goal.created", payload={"x": 1}
            )
            is None
        )
        append.assert_not_awaited()

    async def test_unknown_event_type_rejected(self, events_flag, monkeypatch):
        append = AsyncMock()
        monkeypatch.setattr(db_events, "append_event", append)
        with pytest.raises(ContractError, match="unknown event type"):
            await svc_events.record_event(
                user_id="u", event_type="goal.exploded", payload={}
            )
        append.assert_not_awaited()

    async def test_payload_is_redacted_before_insert(self, events_flag, monkeypatch):
        append = AsyncMock(return_value={"event_id": 1})
        monkeypatch.setattr(db_events, "append_event", append)
        await svc_events.record_event(
            user_id="u",
            event_type="permission.decision",
            payload={"api_key": "sk-x", "decision": "allow"},
        )
        sent = append.await_args.kwargs["payload"]
        assert sent["api_key"] == REDACTED
        assert sent["decision"] == "allow"

    async def test_oversized_payload_rejected_after_redaction(
        self, events_flag, monkeypatch
    ):
        append = AsyncMock()
        monkeypatch.setattr(db_events, "append_event", append)
        with pytest.raises(ContractError, match="payload exceeds"):
            await svc_events.record_event(
                user_id="u",
                event_type="goal.created",
                payload={"items": [{"n": i, "v": "x"} for i in range(4000)]},
            )
        append.assert_not_awaited()


class TestMemoryService:
    async def test_flag_off_returns_empty_shapes(self, monkeypatch):
        upsert = AsyncMock()
        search = AsyncMock()
        monkeypatch.setattr(db_memory, "upsert_entry", upsert)
        monkeypatch.setattr(db_memory, "search_entries", search)
        assert (
            await svc_memory.project_memory_entry(
                user_id="u",
                tier="user",
                namespace=["n"],
                entry_key="k",
                content_hash="h",
            )
            is None
        )
        assert await svc_memory.search_memory(user_id="u", query="q") == []
        upsert.assert_not_awaited()
        search.assert_not_awaited()

    async def test_invalid_tier_and_status(self, memory_flag):
        with pytest.raises(ContractError, match="tier"):
            await svc_memory.project_memory_entry(
                user_id="u", tier="global", namespace=["n"], entry_key="k", content_hash="h"
            )
        with pytest.raises(ContractError, match="status"):
            await svc_memory.project_memory_entry(
                user_id="u",
                tier="user",
                namespace=["n"],
                entry_key="k",
                content_hash="h",
                status="gone",
            )

    async def test_identity_fields_required(self, memory_flag):
        with pytest.raises(ContractError, match="non-empty key"):
            await svc_memory.project_memory_entry(
                user_id="u", tier="user", namespace=["n"], entry_key=" ", content_hash="h"
            )
        with pytest.raises(ContractError, match="content hash"):
            await svc_memory.project_memory_entry(
                user_id="u", tier="user", namespace=["n"], entry_key="k", content_hash=""
            )
        with pytest.raises(ContractError, match="namespace"):
            await svc_memory.project_memory_entry(
                user_id="u", tier="user", namespace=[], entry_key="k", content_hash="h"
            )

    async def test_index_line_is_bounded_and_written(self, memory_flag, monkeypatch):
        upsert = AsyncMock(return_value={"entry_id": "e"})
        monkeypatch.setattr(db_memory, "upsert_entry", upsert)
        await svc_memory.project_memory_entry(
            user_id="u",
            tier="user",
            namespace=["n"],
            entry_key="k",
            content_hash="h",
            index_text="x" * 5000,
        )
        written = upsert.await_args.kwargs["index_text"]
        assert len(written) <= svc_memory.MAX_INDEX_TEXT_LENGTH
        assert written.endswith("[truncated]")

    async def test_search_query_bounded(self, memory_flag):
        with pytest.raises(ContractError, match="exceeds"):
            await svc_memory.search_memory(user_id="u", query="q" * 513)


class TestTasksService:
    async def test_flag_off_is_a_noop(self, monkeypatch):
        insert = AsyncMock()
        monkeypatch.setattr(db_tasks, "insert_task", insert)
        assert (
            await svc_tasks.record_task(user_id="u", thread_id="t", title="x") is None
        )
        insert.assert_not_awaited()
        assert await svc_tasks.list_task_tree(thread_id="t") == []

    async def test_invalid_status_source_title(self, tasks_flag):
        with pytest.raises(ContractError, match="status"):
            await svc_tasks.record_task(
                user_id="u", thread_id="t", title="x", status="done"
            )
        with pytest.raises(ContractError, match="source"):
            await svc_tasks.record_task(
                user_id="u", thread_id="t", title="x", source="manual"
            )
        with pytest.raises(ContractError, match="title"):
            await svc_tasks.record_task(user_id="u", thread_id="t", title="  ")

    async def test_record_passes_normalized_fields(self, tasks_flag, monkeypatch):
        insert = AsyncMock(return_value={"task_node_id": "n"})
        monkeypatch.setattr(db_tasks, "insert_task", insert)
        row = await svc_tasks.record_task(
            user_id="u",
            thread_id="t",
            title="  collect changelogs  ",
            status="in_progress",
            source="subagent",
            subagent_task_ref=" task-1 ",
        )
        assert row == {"task_node_id": "n"}
        kwargs = insert.await_args.kwargs
        assert kwargs["title"] == "collect changelogs"
        assert kwargs["subagent_task_ref"] == "task-1"


class TestTodoSnapshotService:
    async def test_flag_off_is_noop(self, monkeypatch):
        sync = AsyncMock()
        monkeypatch.setattr(db_tasks, "sync_todo_snapshot", sync)
        assert (
            await svc_tasks.record_todo_snapshot(
                thread_id="t", todos=[{"content": "x", "status": "pending"}]
            )
            is None
        )
        sync.assert_not_awaited()

    async def test_validates_snapshot_shape(self, tasks_flag):
        with pytest.raises(ContractError, match="must be a list"):
            await svc_tasks.record_todo_snapshot(thread_id="t", todos={"content": "x"})
        with pytest.raises(ContractError, match="exceeds"):
            await svc_tasks.record_todo_snapshot(
                thread_id="t",
                todos=[
                    {"content": f"t{i}", "status": "pending"}
                    for i in range(svc_tasks.MAX_TODO_ITEMS + 1)
                ],
            )
        with pytest.raises(ContractError, match="unknown task status"):
            await svc_tasks.record_todo_snapshot(
                thread_id="t", todos=[{"content": "x", "status": "queued"}]
            )

    async def test_skips_bad_items_and_summarizes(self, tasks_flag, monkeypatch):
        sync = AsyncMock(
            return_value=[
                {"status": "pending"},
                {"status": "in_progress"},
                {"status": "completed"},
            ]
        )
        monkeypatch.setattr(db_tasks, "sync_todo_snapshot", sync)
        result = await svc_tasks.record_todo_snapshot(
            thread_id="t",
            todos=[
                "not-a-dict",
                {"content": "  ", "status": "pending"},
                {"content": "collect", "status": "IN_PROGRESS"},
                {"content": "build", "status": "pending"},
                {"content": "write", "status": "completed"},
            ],
        )
        assert result == {"written": 3, "rows": 3, "open": 2, "completed": 1}
        assert sync.await_args.kwargs["todos"] == [
            {"title": "collect", "status": "in_progress"},
            {"title": "build", "status": "pending"},
            {"title": "write", "status": "completed"},
        ]

    async def test_unknown_owner_returns_none(self, tasks_flag, monkeypatch):
        monkeypatch.setattr(db_tasks, "sync_todo_snapshot", AsyncMock(return_value=None))
        assert (
            await svc_tasks.record_todo_snapshot(thread_id="t", todos=[])
            is None
        )


class TestPermissionsService:
    async def test_flag_off_is_default_and_read_free(self, monkeypatch):
        read = AsyncMock()
        monkeypatch.setattr(db_permissions, "get_mode", read)
        monkeypatch.delenv("REFACTOR_PERMISSIONS_ENABLED", raising=False)
        assert await svc_permissions.read_permission_mode(str(uuid.uuid4())) == "auto"
        read.assert_not_awaited()

    @pytest.fixture
    def permissions_flag(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_PERMISSIONS_ENABLED", "1")
        yield

    async def test_reads_persisted_mode(self, permissions_flag, monkeypatch):
        monkeypatch.setattr(
            db_permissions, "get_mode", AsyncMock(return_value={"mode": "manual"})
        )
        assert await svc_permissions.read_permission_mode(str(uuid.uuid4())) == "manual"

    async def test_missing_row_is_the_default(self, permissions_flag, monkeypatch):
        monkeypatch.setattr(db_permissions, "get_mode", AsyncMock(return_value=None))
        assert await svc_permissions.read_permission_mode(str(uuid.uuid4())) == "auto"

    async def test_unknown_stored_value_degrades_to_default(
        self, permissions_flag, monkeypatch
    ):
        monkeypatch.setattr(
            db_permissions, "get_mode", AsyncMock(return_value={"mode": "galactic"})
        )
        assert await svc_permissions.read_permission_mode(str(uuid.uuid4())) == "auto"

    async def test_turn_read_never_raises(self, permissions_flag, monkeypatch):
        monkeypatch.setattr(
            db_permissions, "get_mode", AsyncMock(side_effect=RuntimeError("db down"))
        )
        assert await svc_permissions.effective_permission_mode(str(uuid.uuid4())) == "auto"
        assert await svc_permissions.effective_permission_mode(None) == "auto"

    async def test_set_validates_supported_modes(self, permissions_flag):
        with pytest.raises(ContractError, match="unsupported permission mode"):
            await svc_permissions.set_permission_mode(
                thread_id=str(uuid.uuid4()), mode="skip"
            )

    async def test_set_is_noop_when_flag_off(self):
        assert (
            await svc_permissions.set_permission_mode(
                thread_id=str(uuid.uuid4()), mode="manual"
            )
            is None
        )

    async def test_set_persists(self, permissions_flag, monkeypatch):
        write = AsyncMock(return_value={"mode": "manual"})
        monkeypatch.setattr(db_permissions, "set_mode", write)
        thread_id = str(uuid.uuid4())
        assert (
            await svc_permissions.set_permission_mode(
                thread_id=thread_id, mode="manual", updated_by="u"
            )
        ) == {"mode": "manual"}
        write.assert_awaited_once_with(
            thread_id=thread_id, mode="manual", updated_by="u"
        )
