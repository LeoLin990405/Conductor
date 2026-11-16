"""Reader parity: flag-off hands off to the legacy path, flag-on returns views.

``None`` and ``[]`` are different contracts here: ``None`` means "legacy path,
read the SSE/store state", ``[]`` means "the new reader ran and found nothing".
A caller that confuses them either hides a goal or renders an empty page over
real data, so both are pinned.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.server.contracts.refactor import ContractError
from src.server.database.refactor import events as db_events
from src.server.database.refactor import goals as db_goals
from src.server.database.refactor import memory as db_memory
from src.server.database.refactor import tasks as db_tasks
from src.server.services.refactor import readers


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


GOAL_ID = str(uuid.uuid4())
THREAD_ID = str(uuid.uuid4())
CREATED = datetime(2026, 10, 25, 1, 2, 3, tzinfo=timezone.utc)


def _goal_row() -> dict:
    return {
        "goal_id": GOAL_ID,
        "thread_id": THREAD_ID,
        "workspace_id": None,
        "title": "Research",
        "objective": "Do it",
        "stop_conditions": ["done"],
        "status": "active",
        "budget": {"max_turns": 2},
        "verifier_config": {},
        "verifier_verdict": None,
        "created_at": CREATED,
        "updated_at": CREATED,
        "completed_at": None,
        "internal_extra_column": "must not leak",
    }


def _plan_row() -> dict:
    return {
        "plan_id": str(uuid.uuid4()),
        "version": 2,
        "steps": [{"step": "collect"}],
        "status": "awaiting_approval",
        "feedback": None,
        "created_at": CREATED,
        "decided_at": None,
    }


class TestReadGoal:
    async def test_flag_off_is_none_without_database(self, monkeypatch):
        get_live = AsyncMock()
        monkeypatch.setattr(db_goals, "get_live_goal", get_live)
        assert await readers.read_goal(thread_id=THREAD_ID) is None
        get_live.assert_not_awaited()

    async def test_no_live_goal_is_none(self, goals_flag, monkeypatch):
        monkeypatch.setattr(db_goals, "get_live_goal", AsyncMock(return_value=None))
        assert await readers.read_goal(thread_id=THREAD_ID) is None

    async def test_view_is_json_safe_and_whitelisted(self, goals_flag, monkeypatch):
        monkeypatch.setattr(db_goals, "get_live_goal", AsyncMock(return_value=_goal_row()))
        monkeypatch.setattr(db_goals, "get_latest_plan", AsyncMock(return_value=_plan_row()))
        view = await readers.read_goal(thread_id=THREAD_ID)
        assert view["goal"]["created_at"] == "2026-10-25T01:02:03+00:00"
        assert "internal_extra_column" not in view["goal"]
        assert view["plan"]["version"] == 2

    async def test_missing_plan_renders_null(self, goals_flag, monkeypatch):
        monkeypatch.setattr(db_goals, "get_live_goal", AsyncMock(return_value=_goal_row()))
        monkeypatch.setattr(db_goals, "get_latest_plan", AsyncMock(return_value=None))
        view = await readers.read_goal(thread_id=THREAD_ID)
        assert view["plan"] is None


class TestReadEvents:
    async def test_flag_off_is_none(self, monkeypatch):
        list_events = AsyncMock()
        monkeypatch.setattr(db_events, "list_events", list_events)
        assert await readers.read_events(thread_id=THREAD_ID) is None
        list_events.assert_not_awaited()

    async def test_on_empty_is_a_list(self, events_flag, monkeypatch):
        monkeypatch.setattr(db_events, "list_events", AsyncMock(return_value=[]))
        assert await readers.read_events(thread_id=THREAD_ID) == []

    async def test_delegates_cursor_and_projects_rows(self, events_flag, monkeypatch):
        row = {
            "event_id": 7,
            "thread_id": THREAD_ID,
            "response_id": str(uuid.uuid4()),
            "task_run_id": None,
            "seq": 3,
            "type": "goal.created",
            "payload": {"api_key": "[REDACTED]"},
            "created_at": CREATED,
            "internal": "x",
        }
        list_events = AsyncMock(return_value=[row])
        monkeypatch.setattr(db_events, "list_events", list_events)
        page = await readers.read_events(thread_id=THREAD_ID, after_event_id=5, limit=10)
        assert list_events.await_args.kwargs == {
            "thread_id": THREAD_ID,
            "after_event_id": 5,
            "limit": 10,
        }
        assert page[0]["created_at"] == "2026-10-25T01:02:03+00:00"
        assert "internal" not in page[0]


class TestTaskTree:
    async def test_flag_off_is_none(self, monkeypatch):
        list_tasks = AsyncMock()
        monkeypatch.setattr(db_tasks, "list_tasks", list_tasks)
        assert await readers.read_task_tree(thread_id=THREAD_ID) is None
        list_tasks.assert_not_awaited()

    async def test_nests_rows(self, tasks_flag, monkeypatch):
        parent_id, child_id = str(uuid.uuid4()), str(uuid.uuid4())
        rows = [
            {"task_node_id": parent_id, "parent_node_id": None, "title": "root"},
            {"task_node_id": child_id, "parent_node_id": parent_id, "title": "child"},
        ]
        monkeypatch.setattr(db_tasks, "list_tasks", AsyncMock(return_value=rows))
        tree = await readers.read_task_tree(thread_id=THREAD_ID)
        assert tree[0]["title"] == "root"
        assert tree[0]["children"][0]["title"] == "child"

    async def test_limit_is_bounded(self, tasks_flag):
        with pytest.raises(ValueError, match="bounded|between"):
            await readers.read_task_tree(thread_id=THREAD_ID, limit=0)

class TestReadMemoryIndex:
    async def test_flag_off_is_none(self, monkeypatch):
        list_entries = AsyncMock()
        monkeypatch.setattr(db_memory, "list_entries", list_entries)
        assert await readers.read_memory_index(user_id="u") is None
        list_entries.assert_not_awaited()

    async def test_on_projects_and_passes_filters(self, memory_flag, monkeypatch):
        row = {
            "entry_id": str(uuid.uuid4()),
            "tier": "user",
            "entry_key": "prefs/lang",
            "index_text": "prefers Chinese",
            "status": "active",
            "provenance": {},
            "source_thread_id": None,
            "created_at": CREATED,
            "updated_at": CREATED,
        }
        list_entries = AsyncMock(return_value=[row])
        monkeypatch.setattr(db_memory, "list_entries", list_entries)
        view = await readers.read_memory_index(user_id="u", tier="user", limit=7)
        assert list_entries.await_args.kwargs == {
            "user_id": "u",
            "tier": "user",
            "status": "active",
            "limit": 7,
        }
        assert view[0]["index_text"] == "prefers Chinese"
        assert view[0]["created_at"] == "2026-10-25T01:02:03+00:00"

    async def test_unknown_tier_rejected(self, memory_flag):
        with pytest.raises(ContractError, match="unknown memory tier"):
            await readers.read_memory_index(user_id="u", tier="global")
