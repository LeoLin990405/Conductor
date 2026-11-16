"""Write-path memory dual write: scope, mapping, best-effort failure rules.

The facade may only touch memory namespaces, must delegate everything else
untouched, and must never make a store write fail because the projection write
failed (the backfill repairs it). Index-file writes update existing index lines;
detail writes use the pointer line when the index has one.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from src.server.services.refactor import memory_dual_write as dual

pytestmark = pytest.mark.asyncio

USER = str(uuid.uuid4())
WORKSPACE = str(uuid.uuid4())
USER_NS = (USER, "memory")
WORKSPACE_NS = (USER, "workspaces", WORKSPACE, "memory")
INDEX = "- [Language](prefs/language.md) — prefers Simplified Chinese\n"


class _Item:
    def __init__(self, value):
        self.value = value


class FakeRawStore:
    def __init__(self, items=None):
        self.items = {(tuple(ns), key): value for (ns, key), value in (items or {}).items()}
        self.aput_calls = []
        self.adelete_calls = []

    async def aget(self, namespace, key, **kwargs):
        value = self.items.get((tuple(namespace), key))
        return _Item(value) if value is not None else None

    async def asearch(self, namespace, *, limit=10, offset=0, **kwargs):
        return []

    async def aput(self, namespace, key, value, **kwargs):
        self.aput_calls.append((tuple(namespace), key, value))
        self.items[(tuple(namespace), key)] = value
        return value

    async def adelete(self, namespace, key, **kwargs):
        self.adelete_calls.append((tuple(namespace), key))
        self.items.pop((tuple(namespace), key), None)
        return None


@pytest.fixture
def memory_flag(monkeypatch):
    monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
    yield


class TestScope:
    async def test_namespace_mapping(self):
        assert dual.memory_tier_for_namespace(USER_NS) == ("user", USER, None)
        assert dual.memory_tier_for_namespace(WORKSPACE_NS) == (
            "workspace",
            USER,
            WORKSPACE,
        )
        assert dual.memory_tier_for_namespace((USER, "memos")) is None
        assert dual.memory_tier_for_namespace((USER, "workspaces", WORKSPACE)) is None

    async def test_dot_escaped_label_is_unescaped(self):
        assert dual.memory_tier_for_namespace(("a%2Eb", "memory")) == ("user", "a.b", None)

    async def test_flag_off_returns_the_raw_store(self, monkeypatch):
        monkeypatch.delenv("REFACTOR_MEMORY_INDEX_ENABLED", raising=False)
        raw = FakeRawStore()
        assert dual.maybe_wrap_memory_store(raw) is raw

    async def test_flag_on_wraps_and_delegates_reads(self, memory_flag):
        raw = FakeRawStore({(USER_NS, "memory.md"): {"content": INDEX}})
        wrapper = dual.maybe_wrap_memory_store(raw)
        assert isinstance(wrapper, dual.MemoryProjectionStore)
        item = await wrapper.aget(USER_NS, "memory.md")
        assert item.value["content"] == INDEX
        assert await wrapper.asearch(USER_NS) == []

    async def test_non_memory_namespace_is_delegated_without_projection(
        self, memory_flag, monkeypatch
    ):
        upsert = AsyncMock()
        monkeypatch.setattr(dual.db_memory, "upsert_entry", upsert)
        raw = FakeRawStore()
        wrapper = dual.MemoryProjectionStore(raw)
        await wrapper.aput((USER, "memos"), "note.md", {"content": "x"})
        assert raw.aput_calls
        upsert.assert_not_awaited()


class TestWrites:
    async def test_lineage_failure_is_not_reported_as_complete_projection(
        self, memory_flag, monkeypatch
    ):
        """Row and lineage commit atomically: a lineage failure is a failure."""
        upsert = AsyncMock(side_effect=RuntimeError("lineage unavailable"))
        monkeypatch.setattr(dual.db_memory, "upsert_entry", upsert)
        ids = [
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        ]

        with pytest.raises(RuntimeError, match="lineage unavailable"):
            await dual.project_memory_write(
                store=FakeRawStore({(USER_NS, "memory.md"): {"content": INDEX}}),
                namespace=USER_NS,
                key="merged.md",
                value={
                    "content": (
                        "---\nsupersedes_entry_ids:\n"
                        f"  - {ids[0]}\n  - {ids[1]}\n---\nMerged body"
                    )
                },
            )

        upsert.assert_awaited_once()
        assert upsert.await_args.kwargs["source_entry_ids"] == ids

    async def test_detail_write_uses_pointer_line(self, memory_flag, monkeypatch):
        upsert = AsyncMock(return_value={"entry_id": "e"})
        monkeypatch.setattr(dual.db_memory, "upsert_entry", upsert)
        raw = FakeRawStore({(USER_NS, "memory.md"): {"content": INDEX}})
        wrapper = dual.MemoryProjectionStore(raw)
        await wrapper.aput(
            USER_NS, "prefs/language.md", {"content": "prefers Simplified Chinese"}
        )
        kwargs = upsert.await_args.kwargs
        assert kwargs["user_id"] == USER
        assert kwargs["tier"] == "user"
        assert kwargs["entry_key"] == "prefs/language.md"
        assert kwargs["index_text"] == "Language — prefers Simplified Chinese"
        assert kwargs["provenance"]["pointed"] is True
        assert kwargs["content_hash"].startswith("sha256:")

    async def test_unprojectable_replacement_deactivates_the_previous_row(
        self, memory_flag, monkeypatch
    ):
        """A valid detail replaced by malformed content must not stay searchable.

        The malformed/unterminated document fails closed, so the new write
        projects nothing; the previously active row for the same key still has
        to be deactivated.
        """
        upsert = AsyncMock(return_value={"entry_id": "e"})
        supersede = AsyncMock(return_value=1)
        monkeypatch.setattr(dual.db_memory, "upsert_entry", upsert)
        monkeypatch.setattr(dual.db_memory, "supersede_entry", supersede)
        store = FakeRawStore()

        await dual.project_memory_write(
            store=store,
            namespace=USER_NS,
            key="note.md",
            value={"content": "remembered fact"},
        )
        assert upsert.await_count == 1

        await dual.project_memory_write(
            store=store,
            namespace=USER_NS,
            key="note.md",
            value={"content": "---\nsensitivity: secret\nunterminated"},
        )

        supersede.assert_awaited_once_with(
            user_id=USER, tier="user", entry_key="note.md"
        )
        assert upsert.await_count == 1

    async def test_detail_write_falls_back_without_pointer(self, memory_flag, monkeypatch):
        upsert = AsyncMock(return_value={"entry_id": "e"})
        monkeypatch.setattr(dual.db_memory, "upsert_entry", upsert)
        wrapper = dual.MemoryProjectionStore(FakeRawStore())
        await wrapper.aput(
            USER_NS,
            "note.md",
            {"content": "---\ndescription: remembered fact\n---\nbody"},
        )
        assert upsert.await_args.kwargs["index_text"] == "remembered fact"
        assert upsert.await_args.kwargs["provenance"]["pointed"] is False
        metadata = upsert.await_args.kwargs["metadata"]
        assert metadata["origin"] == "assistant_inference"
        assert metadata["confidence"] == 0.5
        assert metadata["evidence_count"] == 0

    async def test_detail_write_projects_origin_and_untrusted_evidence_refs(
        self, memory_flag, monkeypatch
    ):
        upsert = AsyncMock(return_value={"entry_id": "e"})
        monkeypatch.setattr(dual.db_memory, "upsert_entry", upsert)
        wrapper = dual.MemoryProjectionStore(FakeRawStore())
        await wrapper.aput(
            USER_NS,
            "claim.md",
            {
                "content": (
                    "---\norigin: user\nconfidence: 0.9\nevidence_refs:\n"
                    "  - kind: user_message\n    id: msg-7\n    verified: true\n"
                    "---\nA durable user fact."
                )
            },
        )
        metadata = upsert.await_args.kwargs["metadata"]
        assert metadata["origin"] == "user"
        assert metadata["confidence"] == 0.9
        assert metadata["evidence_refs"] == [
            {"kind": "user_message", "id": "msg-7", "verified": False}
        ]
        assert metadata["evidence_count"] == 0
        assert metadata["counts_as_evidence"] is False

    @pytest.mark.parametrize(
        ("lifecycle_yaml", "expected_lifecycle", "expected_status"),
        [
            ("lifecycle: archived\n", "archived", "superseded"),
            ("lifecycle: done\n", "done", "superseded"),
            ("lifecycle: ''\n", "", "superseded"),
            ("lifecycle: ' active '\n", " active ", "superseded"),
            ("lifecycle: true\n", "True", "superseded"),
            ("lifecycle: 17\n", "17", "superseded"),
            ("lifecycle: active\n", "active", "active"),
            ("lifecycle: null\n", None, "active"),
            ("", None, "active"),
        ],
    )
    async def test_lifecycle_projection_matches_baseline_policy(
        self, memory_flag, monkeypatch, lifecycle_yaml, expected_lifecycle, expected_status
    ):
        supersede = AsyncMock(return_value=1)
        upsert = AsyncMock(return_value={"entry_id": "e"})
        monkeypatch.setattr(dual.db_memory, "supersede_entry", supersede)
        monkeypatch.setattr(dual.db_memory, "upsert_entry", upsert)

        await dual.project_memory_write(
            store=FakeRawStore(),
            namespace=USER_NS,
            key="lifecycle.md",
            value={"content": f"---\n{lifecycle_yaml}---\nMemory."},
        )

        kwargs = upsert.await_args.kwargs
        assert kwargs["status"] == expected_status
        assert kwargs["metadata"].get("lifecycle") == expected_lifecycle
        if expected_status == "superseded":
            supersede.assert_awaited_once_with(
                user_id=USER, tier="user", entry_key="lifecycle.md"
            )
        else:
            supersede.assert_not_awaited()

    async def test_lifecycle_truncation_cannot_promote_unknown_value_to_active(
        self, memory_flag, monkeypatch
    ):
        supersede = AsyncMock(return_value=1)
        upsert = AsyncMock(return_value={"entry_id": "e"})
        monkeypatch.setattr(dual.db_memory, "supersede_entry", supersede)
        monkeypatch.setattr(dual.db_memory, "upsert_entry", upsert)

        await dual.project_memory_write(
            store=FakeRawStore(),
            namespace=USER_NS,
            key="long-lifecycle.md",
            value={"content": f"---\nlifecycle: {'active' + 'x' * 80}\n---\nMemory."},
        )

        kwargs = upsert.await_args.kwargs
        assert kwargs["metadata"]["lifecycle"] != "active"
        assert kwargs["status"] == "superseded"
        supersede.assert_awaited_once()

    async def test_index_write_updates_existing_rows(self, memory_flag, monkeypatch):
        update = AsyncMock(return_value=2)
        monkeypatch.setattr(dual.db_memory, "update_index_texts", update)
        raw = FakeRawStore()
        wrapper = dual.MemoryProjectionStore(raw)
        await wrapper.aput(USER_NS, "memory.md", {"content": INDEX})
        kwargs = update.await_args.kwargs
        assert kwargs["user_id"] == USER
        assert kwargs["tier"] == "user"
        assert kwargs["updates"] == {
            "prefs/language.md": "Language — prefers Simplified Chinese"
        }

    async def test_workspace_write_maps_tier_and_workspace(self, memory_flag, monkeypatch):
        upsert = AsyncMock(return_value={"entry_id": "e"})
        monkeypatch.setattr(dual.db_memory, "upsert_entry", upsert)
        wrapper = dual.MemoryProjectionStore(FakeRawStore())
        await wrapper.aput(WORKSPACE_NS, "benchmark.md", {"content": "CSI 300"})
        kwargs = upsert.await_args.kwargs
        assert kwargs["tier"] == "workspace"
        assert kwargs["provenance"]["workspace_id"] == WORKSPACE

    async def test_delete_supersedes_the_row(self, memory_flag, monkeypatch):
        supersede = AsyncMock(return_value=1)
        monkeypatch.setattr(dual.db_memory, "supersede_entry", supersede)
        wrapper = dual.MemoryProjectionStore(FakeRawStore())
        await wrapper.adelete(USER_NS, "prefs/language.md")
        supersede.assert_awaited_once_with(
            user_id=USER, tier="user", entry_key="prefs/language.md"
        )

    async def test_non_uuid_user_is_mapped_into_projection(
        self, memory_flag, monkeypatch
    ):
        upsert = AsyncMock(return_value={"entry_id": "e1"})
        monkeypatch.setattr(dual.db_memory, "upsert_entry", upsert)
        raw = FakeRawStore()
        wrapper = dual.MemoryProjectionStore(raw)
        await wrapper.aput(
            ("local-dev", "memory"), "note.md", {"content": "The durable fact."}
        )
        assert raw.aput_calls  # the store write still happened
        # OSS owners are non-UUID; the row is still projected under the mapped id.
        from src.server.utils.pg_sanitize import refactor_owner_uuid

        upsert.assert_awaited_once()
        assert upsert.await_args.kwargs["user_id"] == refactor_owner_uuid("local-dev")


class TestFailureRules:
    async def test_store_write_failure_propagates(self, memory_flag, monkeypatch):
        class FailingStore(FakeRawStore):
            async def aput(self, namespace, key, value, **kwargs):
                raise RuntimeError("store down")

        upsert = AsyncMock()
        monkeypatch.setattr(dual.db_memory, "upsert_entry", upsert)
        wrapper = dual.MemoryProjectionStore(FailingStore())
        with pytest.raises(RuntimeError, match="store down"):
            await wrapper.aput(USER_NS, "note.md", {"content": "x"})
        upsert.assert_not_awaited()

    async def test_projection_failure_does_not_fail_the_store_write(
        self, memory_flag, monkeypatch
    ):
        bad = AsyncMock(side_effect=RuntimeError("db down"))
        monkeypatch.setattr(dual.db_memory, "upsert_entry", bad)
        raw = FakeRawStore()
        wrapper = dual.MemoryProjectionStore(raw)
        result = await wrapper.aput(USER_NS, "note.md", {"content": "x"})
        assert result == {"content": "x"}
        assert raw.aput_calls


class TestProposedLifecycle:
    async def test_proposed_detail_is_never_projected_as_active(self, memory_flag, monkeypatch):
        upsert = AsyncMock()
        monkeypatch.setattr(dual.db_memory, "upsert_entry", upsert)
        raw = FakeRawStore()
        wrapper = dual.MemoryProjectionStore(raw)
        await wrapper.aput(
            USER_NS,
            "proposed/feedback-abc.md",
            {"content": "---\ndescription: candidate\n---\ncandidate"},
        )
        assert raw.aput_calls  # the store write still happened
        upsert.assert_not_awaited()

    async def test_pending_revision_is_projected_only_as_non_active(self, memory_flag, monkeypatch):
        upsert = AsyncMock(return_value={"entry_id": "e"})
        supersede = AsyncMock(return_value=0)
        monkeypatch.setattr(dual.db_memory, "upsert_entry", upsert)
        monkeypatch.setattr(dual.db_memory, "supersede_entry", supersede)
        raw = FakeRawStore()
        wrapper = dual.MemoryProjectionStore(raw)
        await wrapper.aput(
            USER_NS,
            "revision.md",
            {"content": "---\nlifecycle: pending\ndescription: staged\n---\nbody"},
        )
        supersede.assert_awaited_once_with(
            user_id=USER, tier="user", entry_key="revision.md"
        )
        assert upsert.await_args.kwargs["status"] == "superseded"

    async def test_proposed_delete_supersedes_legacy_row(self, memory_flag, monkeypatch):
        supersede = AsyncMock(return_value=1)
        monkeypatch.setattr(dual.db_memory, "supersede_entry", supersede)
        wrapper = dual.MemoryProjectionStore(FakeRawStore())
        await wrapper.adelete(USER_NS, "proposed/feedback-abc.md")
        supersede.assert_awaited_once_with(
            user_id=USER, tier="user", entry_key="proposed/feedback-abc.md"
        )


class TestSessionTier:
    async def test_session_namespace_mapping(self):
        assert dual.memory_tier_for_namespace((USER, "threads", "thread-1", "memory")) == (
            "session",
            USER,
            None,
        )

    async def test_session_write_projects_thread_provenance(self, memory_flag, monkeypatch):
        upsert = AsyncMock(return_value={"tier": "session"})
        monkeypatch.setattr(dual.db_memory, "upsert_entry", upsert)
        raw = FakeRawStore()
        wrapper = dual.MemoryProjectionStore(raw)
        await wrapper.aput(
            (USER, "threads", "thread-1", "memory"),
            "decision.md",
            {"content": "ship on Friday", "encoding": "utf-8"},
        )
        kwargs = upsert.await_args.kwargs
        assert kwargs["tier"] == "session"
        assert kwargs["provenance"]["thread_id"] == "thread-1"
