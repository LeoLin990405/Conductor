"""Origin and evidence metadata stay conservative at trust boundaries."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.server.contracts.memory_origin import (
    MEMORY_ORIGINS,
    normalize_memory_metadata,
)
from src.server.services.refactor.memory_projection import projectable_rows
from src.server.contracts.memory_validity import is_current
from src.server.services.refactor.memory_context import select_memory_context


@pytest.mark.parametrize("origin", sorted(MEMORY_ORIGINS))
def test_origin_enum_accepts_only_the_closed_set(origin):
    assert normalize_memory_metadata({"origin": origin})["origin"] == origin


@pytest.mark.parametrize("origin", [None, "assistant", "human", "system", "bogus", 3])
def test_missing_or_unknown_origin_defaults_to_assistant_inference(origin):
    supplied = {} if origin is None else {"origin": origin}
    result = normalize_memory_metadata(supplied)
    assert result["origin"] == "assistant_inference"
    assert result["confidence"] <= 0.5
    assert result["evidence_count"] == 0
    assert result["counts_as_evidence"] is False


def test_assistant_inference_confidence_is_capped_and_caller_counts_are_ignored():
    result = normalize_memory_metadata(
        {
            "origin": "assistant_inference",
            "confidence": 0.97,
            "evidence_count": 99,
            "counts_as_evidence": True,
            "evidence_refs": [
                {"kind": "assistant_inference", "id": "claim-1", "verified": True}
            ],
        }
    )
    assert result["confidence"] == 0.5
    assert result["evidence_count"] == 0
    assert result["counts_as_evidence"] is False


def test_only_distinct_verified_user_messages_count_as_evidence():
    result = normalize_memory_metadata(
        {
            "origin": "user",
            "evidence_count": 500,
            "counts_as_evidence": True,
            "evidence_refs": [
                {"kind": "user_message", "id": "msg-1", "verified": True},
                {"kind": "user_message", "id": "msg-1", "verified": True},
                {"kind": "user_message", "id": "msg-2", "verified": False},
                {"kind": "approval", "id": "approval-1", "verified": True},
                {"kind": "tool_call", "id": "tool-1", "verified": True},
                {"kind": "web_source", "id": "web-1", "verified": True},
            ],
        },
        trust_evidence_refs=True,
    )
    assert result["evidence_count"] == 1
    assert result["counts_as_evidence"] is True
    assert len(result["evidence_refs"]) == 4


def test_untrusted_boundary_cannot_accept_caller_verified_or_caller_counts():
    result = normalize_memory_metadata(
        {
            "origin": "user",
            "evidence_count": 500,
            "counts_as_evidence": True,
            "evidence_refs": [
                {"kind": "user_message", "id": "msg-1", "verified": True}
            ],
        }
    )
    assert result["evidence_refs"] == [
        {"kind": "user_message", "id": "msg-1", "verified": False}
    ]
    assert result["evidence_count"] == 0
    assert result["counts_as_evidence"] is False


def test_repeated_inference_references_never_become_corroboration():
    result = normalize_memory_metadata(
        {
            "origin": "assistant_inference",
            "evidence_refs": [
                {"kind": "user_message", "id": "same-claim", "verified": True},
                {"kind": "user_message", "id": "same-claim", "verified": True},
                {"kind": "user_message", "id": "other-claim", "verified": True},
            ],
        },
        trust_evidence_refs=True,
    )
    assert result["evidence_count"] == 0
    assert result["counts_as_evidence"] is False


def test_refs_are_id_only_and_unverified_refs_are_retained_but_not_counted():
    result = normalize_memory_metadata(
        {
            "origin": "user",
            "evidence_refs": [
                {
                    "kind": "user_message",
                    "id": "msg-3",
                    "verified": False,
                    "text": "private content must not become provenance",
                }
            ],
        }
    )
    assert result["evidence_refs"] == [{"kind": "user_message", "id": "msg-3", "verified": False}]
    assert result["evidence_count"] == 0
    assert result["counts_as_evidence"] is False


def test_backfill_projection_preserves_origin_and_rederives_untrusted_evidence():
    rows, skipped = projectable_rows(
        user_id="u1",
        tier="user",
        namespace=("u1", "memory"),
        items=[
            (
                "claim.md",
                {
                    "content": (
                        "---\ndescription: User assertion\norigin: user\nconfidence: 0.8\n"
                        "evidence_count: 44\ncounts_as_evidence: true\nevidence_refs:\n"
                        "  - kind: user_message\n    id: msg-8\n    verified: true\n"
                        "---\nThe source said this."
                    )
                },
            ),
            ("legacy.md", {"content": "A legacy memory without frontmatter."}),
        ],
    )
    assert not skipped
    metadata = rows[0]["metadata"]
    assert metadata["origin"] == "user"
    assert metadata["confidence"] == 0.8
    assert metadata["evidence_refs"] == [
        {"kind": "user_message", "id": "msg-8", "verified": False}
    ]
    assert metadata["evidence_count"] == 0
    assert metadata["counts_as_evidence"] is False
    legacy = next(row for row in rows if row["entry_key"] == "legacy.md")
    assert legacy["metadata"]["origin"] == "assistant_inference"
    assert legacy["metadata"]["confidence"] == 0.5
    assert legacy["metadata"]["evidence_count"] == 0


@pytest.mark.parametrize(
    ("lifecycle_yaml", "expected_lifecycle", "expected_status", "current"),
    [
        ("lifecycle: archived\n", "archived", "superseded", False),
        ("lifecycle: done\n", "done", "superseded", False),
        ("lifecycle: ''\n", "", "superseded", False),
        ("lifecycle: ' active '\n", " active ", "superseded", False),
        ("lifecycle: true\n", "True", "superseded", False),
        ("lifecycle: 17\n", "17", "superseded", False),
        ("lifecycle: active\n", "active", "active", True),
        ("lifecycle: null\n", None, "active", True),
        ("", None, "active", True),
    ],
)
def test_backfill_lifecycle_projection_matches_baseline_policy(
    lifecycle_yaml, expected_lifecycle, expected_status, current
):
    content = f"---\n{lifecycle_yaml}---\nOld memory."
    rows, skipped = projectable_rows(
        user_id="u1",
        tier="user",
        namespace=("u1", "memory"),
        items=[("lifecycle.md", {"content": content})],
    )

    assert not skipped
    assert rows[0]["metadata"].get("lifecycle") == expected_lifecycle
    assert rows[0]["status"] == expected_status
    assert is_current(rows[0]["metadata"]) is current


def test_lifecycle_truncation_cannot_promote_unknown_value_to_active():
    rows, skipped = projectable_rows(
        user_id="u1",
        tier="user",
        namespace=("u1", "memory"),
        items=[
            (
                "long-lifecycle.md",
                {"content": f"---\nlifecycle: {'active' + 'x' * 80}\n---\nOld memory."},
            )
        ],
    )

    assert not skipped
    assert rows[0]["metadata"]["lifecycle"] != "active"
    assert rows[0]["status"] == "superseded"
    assert not is_current(rows[0]["metadata"])


@pytest.mark.asyncio
async def test_federated_memory_metadata_reaches_selected_context():
    from src.server.services.refactor.memory_federation.integration import _as_context_rows
    from src.server.services.refactor.memory_federation.models import MemoryHit

    hit = MemoryHit(
        source="farm",
        key="claim-1",
        text="A weakly supported claim.",
        score=1.0,
        provenance={
            "memory_metadata": {
                "origin": "web",
                "confidence": 0.3,
                "evidence_refs": [
                    {"kind": "web_source", "id": "source-1", "verified": False}
                ],
            }
        },
        tier="user",
    )
    rows = _as_context_rows([hit])

    async def search(
        user_id, tier, query, limit, thread_id=None, workspace_id=None
    ):
        return rows if tier == "user" else []

    context = await select_memory_context(
        user_id="u1", query="claim", budget_tokens=200, search=search
    )
    entry = context.entries[0]
    assert entry.confidence == 0.3 and entry.low_confidence
    assert entry.provenance["memory_metadata"]["origin"] == "web"
    assert "[low confidence]" in context.render_index_block()


@pytest.mark.asyncio
async def test_conductor_federation_proposal_forwards_typed_origin(monkeypatch):
    from src.server.services.refactor.memory_federation.conductor import ConductorPgMemory
    from src.server.services.refactor.memory_federation.models import MemoryEntry

    monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
    insert = AsyncMock(return_value="proposal-1")
    monkeypatch.setattr(
        "src.server.services.refactor.memory_federation.conductor.db_proposals.insert_proposal_for_run",
        insert,
    )
    entry = MemoryEntry(
        key="fact",
        text="A sourced web fact.",
        tier="reference",
        thread_id="thread-1",
        task_run_id="run-1",
        origin="web",
        confidence=0.8,
        evidence_refs=({"kind": "web_source", "id": "source-2", "verified": True},),
    )
    proposal_id = await ConductorPgMemory().propose(
        "11111111-1111-1111-1111-111111111111", entry
    )
    assert proposal_id == "proposal-1"
    kwargs = insert.await_args.kwargs
    assert kwargs["origin"] == "web"
    assert kwargs["confidence"] == 0.8
    assert kwargs["evidence_refs"] == ({
        "kind": "web_source", "id": "source-2", "verified": False
    },)
