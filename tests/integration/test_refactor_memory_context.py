"""Memory-context selection against the real projection (integration).

The unit eval runs the policy against an in-memory store; this test proves the
same selection reads the Postgres projection: the per-tier FTS query returns the
index lines, inactive entries stay out, and the rendered block respects the
budget. Uses the shared ``refactor_db`` fixture (migrated schema, patched pool).
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _seed(user_id: str) -> None:
    from src.server.database.refactor import memory as db_memory

    await db_memory.upsert_entry(
        user_id=user_id,
        tier="user",
        namespace=["uid", "memory"],
        entry_key="prefs/language",
        content_hash="sha256:lang",
        size=30,
        index_text="prefers Chinese answers",
    )
    await db_memory.upsert_entry(
        user_id=user_id,
        tier="workspace",
        namespace=["uid", "workspaces", "w", "memory"],
        entry_key="fact/symbols",
        content_hash="sha256:symbols",
        size=40,
        index_text="tracked symbol RFC-9110",
    )
    await db_memory.upsert_entry(
        user_id=user_id,
        tier="user",
        namespace=["uid", "memory"],
        entry_key="prefs/old-format",
        content_hash="sha256:old",
        size=20,
        index_text="prefers a deprecated format",
        status="superseded",
    )


class TestCjkRetrieval:
    async def test_unspaced_chinese_query_retrieves_the_entry(self, refactor_db):
        from src.server.database.refactor import memory as db_memory

        user_id = str(uuid.uuid4())
        await db_memory.upsert_entry(
            user_id=user_id,
            tier="workspace",
            namespace=["uid", "workspaces", "w", "memory"],
            entry_key="fact/zh-symbols",
            content_hash="sha256:zh",
            size=40,
            index_text="关注的项目：季度规划 RFC-9110。",
        )
        rows = await db_memory.search_entries(user_id=user_id, query="我关注的项目")
        assert [row["entry_key"] for row in rows] == ["fact/zh-symbols"]

    async def test_spaced_chinese_query_matches_each_term(self, refactor_db):
        from src.server.database.refactor import memory as db_memory

        user_id = str(uuid.uuid4())
        await db_memory.upsert_entry(
            user_id=user_id,
            tier="workspace",
            namespace=["uid", "workspaces", "w", "memory"],
            entry_key="fact/zh-symbols",
            content_hash="sha256:zh",
            size=40,
            index_text="关注的项目：季度规划 RFC-9110。",
        )
        rows = await db_memory.search_entries(user_id=user_id, query="项目 季度规划")
        assert [row["entry_key"] for row in rows] == ["fact/zh-symbols"]


class TestMemoryContextDb:
    async def test_selects_from_projection_and_renders_block(self, refactor_db):
        from src.server.services.refactor.memory_context import select_memory_context

        user_id = str(uuid.uuid4())
        await _seed(user_id)
        context = await select_memory_context(
            user_id=user_id, query="prefers", budget_tokens=300
        )
        assert ("user", "prefs/language") in context.keys
        assert context.tokens_used <= 300
        block = context.render_index_block()
        assert "prefs/language" in block
        assert "prefers Chinese answers" in block
        # The inactive entry is projected but never injected.
        assert ("user", "prefs/old-format") not in context.keys

    async def test_workspace_tier_is_searchable(self, refactor_db):
        from src.server.services.refactor.memory_context import select_memory_context

        user_id = str(uuid.uuid4())
        await _seed(user_id)
        context = await select_memory_context(
            user_id=user_id, query="tracked", budget_tokens=200
        )
        assert ("workspace", "fact/symbols") in context.keys

    async def test_other_user_projection_is_not_visible(self, refactor_db):
        from src.server.services.refactor.memory_context import select_memory_context

        user_id = str(uuid.uuid4())
        other = str(uuid.uuid4())
        await _seed(user_id)
        context = await select_memory_context(
            user_id=other, query="prefers", budget_tokens=200
        )
        assert context.entries == ()
