import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from src.server.services.refactor.memory_federation.conductor import ConductorPgMemory
from src.server.services.refactor.memory_federation.models import MemoryEntry, MemoryHit

pytestmark = pytest.mark.asyncio


async def test_explicit_inference_origin_overrides_conflicting_provenance():
    entry = MemoryEntry(
        key="fact",
        text="fact text",
        tier="user",
        origin="assistant_inference",
        confidence=0.99,
        provenance={
            "origin": "user",
            "confidence": 0.99,
            "evidence_count": 7,
            "counts_as_evidence": True,
            "evidence_refs": [
                {"kind": "user_message", "id": "m1", "verified": True}
            ],
        },
    )
    assert entry.origin == "assistant_inference"
    assert entry.confidence == 0.5
    assert entry.evidence_refs[0]["verified"] is False
    assert entry.provenance["evidence_count"] == 0
    assert entry.provenance["counts_as_evidence"] is False


async def test_pg_recall_is_read_only_scoped_and_keeps_content_identity(monkeypatch):
    from src.server.services.refactor import memory_embeddings

    search = AsyncMock(
        return_value=[
            {
                "entry_id": "row-1",
                "user_id": "11111111-1111-1111-1111-111111111111",
                "tier": "workspace",
                "namespace": [
                    "11111111-1111-1111-1111-111111111111",
                    "workspaces",
                    "22222222-2222-2222-2222-222222222222",
                    "memory",
                ],
                "entry_key": "prefs.md",
                "content_hash": "sha256:abc",
                "index_text": "Use short answers",
                "status": "active",
                "metadata": {"valid_until": "2999-01-01T00:00:00Z"},
                "provenance": {"source": "store_dual_write"},
            },
            {
                "entry_key": "proposed/unsafe.md",
                "index_text": "candidate",
                "status": "active",
            },
        ]
    )
    monkeypatch.setattr(memory_embeddings, "hybrid_search", search)
    hits = await ConductorPgMemory().recall(
        "11111111-1111-1111-1111-111111111111",
        "answer length",
        5,
        ["workspace"],
        workspace_id="22222222-2222-2222-2222-222222222222",
    )
    assert [hit.key for hit in hits] == ["prefs.md"]
    assert isinstance(hits[0], MemoryHit)
    assert hits[0].provenance["content_hash"] == "sha256:abc"
    kwargs = dict(search.await_args.kwargs)
    assert kwargs.pop("semantic_deadline") > time.monotonic()
    assert kwargs == {
        "user_id": "11111111-1111-1111-1111-111111111111",
        "query": "answer length",
        "tier": "workspace",
        "thread_id": None,
        "workspace_id": "22222222-2222-2222-2222-222222222222",
        "limit": 5,
    }


async def test_pg_recall_uses_hybrid_embeddings_path(monkeypatch):
    """Opting into federation must not downgrade Conductor recall to lexical."""
    from src.server.services.refactor import memory_embeddings

    search = AsyncMock(return_value=[])
    monkeypatch.setattr(memory_embeddings, "hybrid_search", search)
    await ConductorPgMemory().recall("user-id", "query", 10, ["user"])
    search.assert_awaited_once()


async def test_pg_recall_falls_back_to_lexical_when_embeddings_exceed_budget(monkeypatch):
    """A slow embedding round-trip must not discard the lexical rows already found."""
    from src.server.services.refactor import memory_embeddings

    monkeypatch.setenv("MEMORY_EMBEDDING_ENABLED", "1")
    monkeypatch.setenv("MEMORY_EMBEDDING_PROVIDER", "minimax")
    monkeypatch.setenv("MEMORY_EMBEDDING_MODEL", "embo-01")
    monkeypatch.setenv("MEMORY_EMBEDDING_API_KEY", "test-key")
    owner = "11111111-1111-1111-1111-111111111111"
    row = {
        "user_id": owner,
        "tier": "user",
        "namespace": [owner, "memory"],
        "entry_key": "fact.md",
        "content_hash": "sha256:fact",
        "index_text": "the fact found by the lexical search",
        "status": "active",
    }
    monkeypatch.setattr(
        memory_embeddings.db_memory, "search_entries", AsyncMock(return_value=[row])
    )

    async def slow_embed(*args, **kwargs):
        await asyncio.sleep(5)
        return [[1.0, 0.0]]

    monkeypatch.setattr(memory_embeddings, "embed_texts", slow_embed)
    retrieve = AsyncMock()
    monkeypatch.setattr(memory_embeddings.db_memory, "list_retrieval_rows", retrieve)

    started = time.monotonic()
    hits = await ConductorPgMemory(recall_budget_seconds=0.08).recall(
        owner, "query", 5, ["user"]
    )
    elapsed = time.monotonic() - started

    assert [hit.key for hit in hits] == ["fact.md"]
    assert elapsed < 1.0
    retrieve.assert_not_awaited()


async def test_slow_lexical_does_not_discard_rows_at_federation_timeout(monkeypatch):
    """A 400ms lexical query plus slow embeddings must still return lexical rows.

    The embedding stage gets one absolute deadline per recall. If its timeout
    starts only after lexical retrieval, 400ms + 750ms overruns the federation's
    one-second provider timeout and the already-fetched lexical rows are lost.
    """
    from src.server.services.refactor import memory_embeddings
    from src.server.services.refactor.memory_federation.provider import MemoryFederation

    monkeypatch.setenv("MEMORY_EMBEDDING_ENABLED", "1")
    monkeypatch.setenv("MEMORY_EMBEDDING_PROVIDER", "minimax")
    monkeypatch.setenv("MEMORY_EMBEDDING_MODEL", "embo-01")
    monkeypatch.setenv("MEMORY_EMBEDDING_API_KEY", "test-key")
    owner = "11111111-1111-1111-1111-111111111111"
    row = {
        "user_id": owner,
        "tier": "user",
        "namespace": [owner, "memory"],
        "entry_key": "fact.md",
        "content_hash": "sha256:fact",
        "index_text": "the fact found by the lexical search",
        "status": "active",
        "metadata": {
            "origin": "user",
            "valid_until": "2999-01-01T00:00:00Z",
            "confidence": 0.8,
        },
    }
    excluded = [
        {
            **row,
            "entry_key": "expired.md",
            "metadata": {"valid_until": "2000-01-01T00:00:00Z"},
        },
        {
            **row,
            "entry_key": "future.md",
            "metadata": {"valid_from": "2999-01-01T00:00:00Z"},
        },
        {
            **row,
            "entry_key": "superseded.md",
            "metadata": {"status": "superseded"},
        },
        {
            **row,
            "entry_key": "proposed/unsafe.md",
            "metadata": {"origin": "user"},
        },
        {
            **row,
            "entry_key": "secret.md",
            "metadata": {"sensitivity": "secret"},
        },
    ]

    async def slow_lexical(**kwargs):
        await asyncio.sleep(0.4)
        return [row, *excluded]

    async def slow_embed(*args, **kwargs):
        await asyncio.sleep(5)
        return [[1.0, 0.0]]

    monkeypatch.setattr(memory_embeddings.db_memory, "search_entries", slow_lexical)
    monkeypatch.setattr(memory_embeddings, "embed_texts", slow_embed)
    retrieve = AsyncMock()
    monkeypatch.setattr(memory_embeddings.db_memory, "list_retrieval_rows", retrieve)

    federation = MemoryFederation([ConductorPgMemory()])
    started = time.monotonic()
    hits = await federation.recall(owner, "query", 5, ["user"])
    elapsed = time.monotonic() - started

    assert [hit.key for hit in hits] == ["fact.md"]
    assert hits[0].provenance["memory_metadata"]["origin"] == "user"
    assert elapsed < 1.0
    retrieve.assert_not_awaited()


async def test_pg_recall_excludes_secret_invalid_and_malformed_metadata(monkeypatch):
    from src.server.services.refactor.memory_federation import conductor

    owner = "11111111-1111-1111-1111-111111111111"
    namespace = [owner, "memory"]
    search = AsyncMock(
        return_value=[
            {
                "user_id": owner,
                "tier": "user",
                "namespace": namespace,
                "entry_key": "secret.md",
                "index_text": "secret",
                "status": "active",
                "metadata": {"sensitivity": "secret"},
            },
            {
                "user_id": owner,
                "tier": "user",
                "namespace": namespace,
                "entry_key": "nested-secret.md",
                "index_text": "secret",
                "status": "active",
                "metadata": {"sensitivity": "public"},
                "provenance": {
                    "sensitivity": "public",
                    "memory_metadata": {"sensitivity": "secret"},
                },
            },
            {
                "user_id": owner,
                "tier": "user",
                "namespace": namespace,
                "entry_key": "invalid.md",
                "index_text": "invalid sensitivity",
                "status": "active",
                "metadata": {"sensitivity": "classified"},
            },
            {
                "user_id": owner,
                "tier": "user",
                "namespace": namespace,
                "entry_key": "malformed.md",
                "index_text": "malformed metadata",
                "status": "active",
                "metadata": "not-an-object",
            },
            {
                "user_id": owner,
                "tier": "user",
                "namespace": namespace,
                "entry_key": "private.md",
                "index_text": "private local fact",
                "status": "active",
                "metadata": {"sensitivity": "private"},
            },
        ]
    )
    monkeypatch.setattr(conductor.db_memory, "search_entries", search)
    hits = await ConductorPgMemory().recall(owner, "fact", 10, ["user"])
    assert [hit.key for hit in hits] == ["private.md"]


async def test_pg_recall_needs_explicit_scope_for_private_tiers(monkeypatch):
    from src.server.services.refactor import memory_embeddings

    search = AsyncMock()
    monkeypatch.setattr(memory_embeddings, "hybrid_search", search)
    hits = await ConductorPgMemory().recall("user-id", "query", 10, ["session"])
    assert hits == []
    search.assert_not_awaited()


async def test_pg_recall_scopes_project_to_workspace(monkeypatch):
    from src.server.services.refactor import memory_embeddings

    workspace_id = "22222222-2222-2222-2222-222222222222"
    search = AsyncMock(
        return_value=[
            {
                "user_id": "11111111-1111-1111-1111-111111111111",
                "tier": "project",
                "namespace": [
                    "11111111-1111-1111-1111-111111111111",
                    "workspaces",
                    workspace_id,
                    "memory",
                ],
                "entry_key": "project.md",
                "content_hash": "sha256:project",
                "index_text": "project fact",
                "status": "active",
            }
        ]
    )
    monkeypatch.setattr(memory_embeddings, "hybrid_search", search)
    hits = await ConductorPgMemory().recall(
        "11111111-1111-1111-1111-111111111111",
        "project",
        5,
        ["project"],
        workspace_id=workspace_id,
    )
    assert [hit.key for hit in hits] == ["project.md"]
    assert search.await_args.kwargs["workspace_id"] == workspace_id


async def test_pg_recall_keeps_workspace_tiers_without_a_workspace(monkeypatch):
    """Chief-of-Staff turns keep the baseline workspace/project tiers."""
    from src.server.services.refactor import memory_embeddings

    search = AsyncMock(
        return_value=[
            {
                "user_id": "11111111-1111-1111-1111-111111111111",
                "tier": "workspace",
                "namespace": [
                    "11111111-1111-1111-1111-111111111111",
                    "workspaces",
                    "22222222-2222-2222-2222-222222222222",
                    "memory",
                ],
                "entry_key": "any-workspace.md",
                "content_hash": "sha256:any",
                "index_text": "workspace fact",
                "status": "active",
            }
        ]
    )
    monkeypatch.setattr(memory_embeddings, "hybrid_search", search)
    hits = await ConductorPgMemory().recall(
        "11111111-1111-1111-1111-111111111111", "fact", 5, ["workspace"]
    )
    assert [hit.key for hit in hits] == ["any-workspace.md"]
    assert search.await_args.kwargs["workspace_id"] is None


async def test_pg_recall_decodes_dotted_oss_owner_and_rejects_wrong_namespace(
    monkeypatch,
):
    from src.server.services.refactor import memory_embeddings
    from src.server.utils.pg_sanitize import refactor_owner_uuid

    owner_id = "leo.test@example.com"
    owner_uuid = refactor_owner_uuid(owner_id)
    search = AsyncMock(
        return_value=[
            {
                "user_id": owner_uuid,
                "tier": "user",
                "namespace": ["leo%2Etest@example.com", "memory"],
                "entry_key": "valid.md",
                "content_hash": "sha256:valid",
                "index_text": "valid fact",
                "status": "active",
            },
            {
                "user_id": owner_uuid,
                "tier": "user",
                "namespace": ["another-owner", "memory"],
                "entry_key": "wrong-owner.md",
                "index_text": "wrong owner",
                "status": "active",
            },
            {
                "user_id": owner_uuid,
                "tier": "user",
                "namespace": [owner_id, "threads", "11111111-1111-1111-1111-111111111111", "memory"],
                "entry_key": "wrong-kind.md",
                "index_text": "not user memory",
                "status": "active",
            },
        ]
    )
    monkeypatch.setattr(memory_embeddings, "hybrid_search", search)
    hits = await ConductorPgMemory().recall(owner_id, "fact", 5, ["user"])
    assert [hit.key for hit in hits] == ["valid.md"]


async def test_propose_rejects_unapproved_tier_or_missing_run_without_write(monkeypatch):
    from src.server.services.refactor.memory_federation import conductor

    monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
    insert = AsyncMock()
    monkeypatch.setattr(conductor.db_proposals, "insert_proposal_for_run", insert)
    provider = ConductorPgMemory()
    with pytest.raises(ValueError, match="proposal tier"):
        await provider.propose(
            "user-id", MemoryEntry(key="x", text="fact", tier="workspace")
        )
    with pytest.raises(ValueError, match="thread_id and task_run_id"):
        await provider.propose(
            "user-id", MemoryEntry(key="x", text="fact", tier="user")
        )
    with pytest.raises(ValueError, match="user memory only"):
        await provider.propose(
            "user-id",
            MemoryEntry(
                key="x",
                text="fact",
                tier="project",
                workspace_id="workspace-uuid",
                thread_id="thread-uuid",
                task_run_id="task-uuid",
            ),
        )
    insert.assert_not_awaited()


async def test_propose_returns_real_id_from_approval_queue(monkeypatch):
    from src.server.services.refactor.memory_federation import conductor

    monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
    insert = AsyncMock(return_value="proposal-uuid")
    monkeypatch.setattr(conductor.db_proposals, "insert_proposal_for_run", insert)
    proposal_id = await ConductorPgMemory().propose(
        "user-id",
        MemoryEntry(
            key="x",
            text="A durable fact",
            tier="feedback",
            thread_id="thread-uuid",
            task_run_id="task-uuid",
        ),
    )
    assert proposal_id == "proposal-uuid"
    kwargs = insert.await_args.kwargs
    assert kwargs["proposal_type"] == "feedback"
    assert kwargs["text"] == "A durable fact"
