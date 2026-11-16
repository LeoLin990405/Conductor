"""Memory backfill against the real projection (integration).

The unit tests pin the mapping; this proves the job writes rows the Phase-2
retrieval path can actually use: one row per detail entry (never the index
file), CJK-searchable index text, and a second run that updates in place rather
than duplicating (upsert identity) with an empty parity report afterwards.
"""

from __future__ import annotations

import uuid

import pytest

from scripts import refactor_memory_backfill as backfill

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

INDEX = """# Memory index
- [项目](fact/zh-symbols.md) — 关注的项目：季度规划 RFC-9110
- [Language](prefs/language.md) — prefers Simplified Chinese answers
"""


class _Item:
    def __init__(self, namespace, key, content):
        self.namespace = namespace
        self.key = key
        self.value = {"content": content}


class FakeStore:
    def __init__(self, items):
        self.items = items

    async def asearch(self, namespace, *, limit, offset):
        matching = [item for item in self.items if item.namespace == tuple(namespace)]
        return matching[offset : offset + limit]


async def _seed(user_id: str) -> None:
    user_ns = (user_id, "memory")
    workspace = str(uuid.uuid4())
    workspace_ns = (user_id, "workspaces", workspace, "memory")
    FakeStore.items = [
        _Item(user_ns, "memory.md", INDEX),
        _Item(user_ns, "fact/zh-symbols.md", "---\ndescription: 项目\n---\n正文"),
        _Item(user_ns, "prefs/language.md", "---\ndescription: language\n---\n正文"),
        _Item(workspace_ns, "memory.md", "- [Benchmark](benchmark.md) — latency p95\n"),
        _Item(workspace_ns, "benchmark.md", "正文"),
    ]
    return workspace_ns


class TestBackfillDb:
    async def test_projects_rows_that_cjk_retrieval_can_find(self, refactor_db):
        from src.server.database.refactor import memory as db_memory

        user_id = str(uuid.uuid4())
        workspace_ns = await _seed(user_id)
        store = FakeStore.items and FakeStore(FakeStore.items)

        user_report = await backfill.backfill_namespace(
            store, user_id=user_id, tier="user", namespace=(user_id, "memory"), apply=True
        )
        workspace_report = await backfill.backfill_namespace(
            store, user_id=user_id, tier="workspace", namespace=workspace_ns, apply=True
        )
        assert user_report["applied"] == 2
        assert workspace_report["applied"] == 1
        assert user_report["parity"] == {"missing": [], "extra": [], "changed": []}

        rows = await db_memory.list_entries(user_id=user_id, tier="user", limit=10)
        assert sorted(row["entry_key"] for row in rows) == [
            "fact/zh-symbols.md",
            "prefs/language.md",
        ]
        # The index file itself is never a projected entry.
        assert "memory.md" not in {row["entry_key"] for row in rows}

        # The stored index text is what the CJK retrieval path searches.
        hits = await db_memory.search_entries(user_id=user_id, query="项目 季度规划")
        assert [row["entry_key"] for row in hits] == ["fact/zh-symbols.md"]
        english = await db_memory.search_entries(user_id=user_id, query="Simplified Chinese")
        assert [row["entry_key"] for row in english] == ["prefs/language.md"]

    async def test_second_apply_is_idempotent(self, refactor_db):
        from src.server.database.refactor import memory as db_memory

        user_id = str(uuid.uuid4())
        await _seed(user_id)
        store = FakeStore(FakeStore.items)
        kwargs = dict(user_id=user_id, tier="user", namespace=(user_id, "memory"), apply=True)
        first = await backfill.backfill_namespace(store, **kwargs)
        second = await backfill.backfill_namespace(store, **kwargs)
        assert first["applied"] == second["applied"] == 2
        rows = await db_memory.list_entries(user_id=user_id, tier="user", limit=10)
        assert len(rows) == 2
        assert second["parity"] == {"missing": [], "extra": [], "changed": []}

    async def test_dry_run_leaves_the_projection_empty(self, refactor_db):
        from src.server.database.refactor import memory as db_memory

        user_id = str(uuid.uuid4())
        await _seed(user_id)
        report = await backfill.backfill_namespace(
            FakeStore(FakeStore.items),
            user_id=user_id,
            tier="user",
            namespace=(user_id, "memory"),
        )
        assert report["dry_run"] is True
        assert await db_memory.list_entries(user_id=user_id, tier="user") == []
