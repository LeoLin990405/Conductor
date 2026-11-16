"""Write-path memory dual write against the real projection (integration).

The unit tests pin scoping/mapping/failure rules with mocks; this proves the
same writes land in Postgres and behave like the backfill's rows: pointer text in
``index_text``, an index edit updating existing rows, and a delete superseding
rather than erasing the active row.
"""

from __future__ import annotations

import uuid

import pytest

from src.server.services.refactor import memory_dual_write as dual

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

INDEX = "- [Language](prefs/language.md) — prefers Simplified Chinese\n"
INDEX_V2 = "- [Language](prefs/language.md) — prefers Traditional Chinese now\n"


class _Item:
    def __init__(self, value):
        self.value = value


class FakeRawStore:
    def __init__(self, items=None):
        self.items = {(tuple(ns), key): value for (ns, key), value in (items or {}).items()}

    async def aget(self, namespace, key, **kwargs):
        value = self.items.get((tuple(namespace), key))
        return _Item(value) if value is not None else None

    async def asearch(self, namespace, *, limit=10, offset=0, **kwargs):
        return []

    async def aput(self, namespace, key, value, **kwargs):
        self.items[(tuple(namespace), key)] = value
        return value

    async def adelete(self, namespace, key, **kwargs):
        self.items.pop((tuple(namespace), key), None)
        return None


async def _active(pool, user_id: str) -> list[dict]:
    from src.server.database.refactor import memory as db_memory

    return await db_memory.list_entries(user_id=user_id, status="active", limit=10)


class TestProjectionWrite:
    async def test_detail_write_projects_pointer_text(self, refactor_db):
        from src.server.database.refactor import memory as db_memory

        user_id = str(uuid.uuid4())
        namespace = (user_id, "memory")
        raw = FakeRawStore({(namespace, "memory.md"): {"content": INDEX}})
        store = dual.MemoryProjectionStore(raw)
        await store.aput(
            namespace, "prefs/language.md", {"content": "prefers Simplified Chinese"}
        )
        rows = await _active(refactor_db, user_id)
        assert [(row["entry_key"], row["index_text"]) for row in rows] == [
            ("prefs/language.md", "Language — prefers Simplified Chinese")
        ]
        # The retrieval path (which includes CJK support) sees the row.
        hits = await db_memory.search_entries(user_id=user_id, query="Simplified Chinese")
        assert [row["entry_key"] for row in hits] == ["prefs/language.md"]

    async def test_index_edit_updates_existing_rows(self, refactor_db):
        user_id = str(uuid.uuid4())
        namespace = (user_id, "memory")
        raw = FakeRawStore({(namespace, "memory.md"): {"content": INDEX}})
        store = dual.MemoryProjectionStore(raw)
        await store.aput(namespace, "prefs/language.md", {"content": "body"})
        await store.aput(namespace, "memory.md", {"content": INDEX_V2})
        rows = await _active(refactor_db, user_id)
        assert rows[0]["index_text"] == "Language — prefers Traditional Chinese now"

    async def test_detail_arriving_after_index_uses_current_pointer(self, refactor_db):
        user_id = str(uuid.uuid4())
        namespace = (user_id, "memory")
        raw = FakeRawStore()
        store = dual.MemoryProjectionStore(raw)
        # Index edited before the detail exists: nothing to update.
        await store.aput(namespace, "memory.md", {"content": INDEX})
        await store.aput(
            namespace,
            "prefs/language.md",
            {"content": "---\ndescription: language preference\n---\nbody"},
        )
        rows = await _active(refactor_db, user_id)
        assert rows[0]["index_text"] == "Language — prefers Simplified Chinese"
        # The backfill (or the next index edit) repairs the pointer text.
        await store.aput(namespace, "memory.md", {"content": INDEX})
        rows = await _active(refactor_db, user_id)
        assert rows[0]["index_text"] == "Language — prefers Simplified Chinese"

    async def test_delete_supersedes_instead_of_erasing(self, refactor_db):
        from src.server.database.refactor import memory as db_memory

        user_id = str(uuid.uuid4())
        namespace = (user_id, "memory")
        raw = FakeRawStore({(namespace, "memory.md"): {"content": INDEX}})
        store = dual.MemoryProjectionStore(raw)
        await store.aput(namespace, "prefs/language.md", {"content": "body"})
        await store.adelete(namespace, "prefs/language.md")
        assert await db_memory.list_entries(user_id=user_id, status="active") == []
        superseded = await db_memory.list_entries(user_id=user_id, status="superseded")
        assert [row["entry_key"] for row in superseded] == ["prefs/language.md"]

    async def test_workspace_namespace_projects_workspace_tier(self, refactor_db):
        from src.server.database.refactor import memory as db_memory

        user_id = str(uuid.uuid4())
        workspace_id = str(uuid.uuid4())
        namespace = (user_id, "workspaces", workspace_id, "memory")
        store = dual.MemoryProjectionStore(FakeRawStore())
        await store.aput(namespace, "benchmark.md", {"content": "CSI 300 total return"})
        rows = await db_memory.list_entries(user_id=user_id, tier="workspace", limit=10)
        assert [row["entry_key"] for row in rows] == ["benchmark.md"]
        assert rows[0]["provenance"]["workspace_id"] == workspace_id

    async def test_non_memory_namespace_is_not_projected(self, refactor_db):
        from src.server.database.refactor import memory as db_memory

        user_id = str(uuid.uuid4())
        store = dual.MemoryProjectionStore(FakeRawStore())
        await store.aput((user_id, "memos"), "note.md", {"content": "x"})
        assert await db_memory.list_entries(user_id=user_id) == []
