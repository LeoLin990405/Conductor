"""Proposal queue service: queueing, flag gating, bounds and decision CAS."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import yaml

from src.server.contracts.refactor import ContractError
from src.server.services.refactor import proposal_approval
from src.server.services.refactor import proposals as svc
from src.server.services.refactor.memory_tool_policy_approval import (
    sync_approved_memory_policy,
)

pytestmark = pytest.mark.asyncio


class TestQueue:
    @pytest.fixture(autouse=True)
    def automatic_memory_on(self, monkeypatch):
        monkeypatch.setattr(
            svc, "automatic_memory_enabled", AsyncMock(return_value=True)
        )

    async def test_default_identity_keeps_legacy_hash_and_nondefault_salts_it(self):
        text = "same text"
        legacy_hash, legacy_key = svc.proposal_identity("reference", text)
        web_hash, web_key = svc.proposal_identity("reference", text, "web")
        assert legacy_hash == hashlib.sha256(text.encode()).hexdigest()
        assert web_hash == hashlib.sha256(b"web\0same text").hexdigest()
        assert legacy_key != web_key
        correction_a, _ = svc.proposal_identity(
            "feedback", text, change_kind="correction", replaces_entry_key="a.md", reason="wrong"
        )
        correction_b, _ = svc.proposal_identity(
            "feedback", text, change_kind="correction", replaces_entry_key="b.md", reason="wrong"
        )
        assert correction_a != correction_b

    async def test_project_is_flag_gated_and_hashes_proposals(self, monkeypatch):
        monkeypatch.delenv("REFACTOR_MEMORY_INDEX_ENABLED", raising=False)
        insert = AsyncMock()
        monkeypatch.setattr(svc.db_proposals, "insert_proposals", insert)
        assert (
            await svc.project_proposals(
                user_id="u1",
                thread_id="t1",
                task_run_id="r1",
                proposals=[{"type": "feedback", "text": "cite sources"}],
            )
            == 0
        )
        insert.assert_not_awaited()

    async def test_user_memory_off_skips_proposal_queue(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        monkeypatch.setattr(
            svc, "automatic_memory_enabled", AsyncMock(return_value=False)
        )
        insert = AsyncMock(return_value=1)
        monkeypatch.setattr(svc.db_proposals, "insert_proposals", insert)

        count = await svc.project_proposals(
            user_id="u1",
            thread_id="t1",
            task_run_id="r1",
            proposals=[{"type": "feedback", "text": "cite sources"}],
        )

        assert count == 0
        insert.assert_not_awaited()

    async def test_project_hashes_type_and_text(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        insert = AsyncMock(return_value=1)
        monkeypatch.setattr(svc.db_proposals, "insert_proposals", insert)
        count = await svc.project_proposals(
            user_id="u1",
            thread_id="t1",
            task_run_id="r1",
            proposals=[{"type": "feedback", "text": "cite sources"}],
        )
        assert count == 1
        rows = insert.await_args.kwargs["proposals"]
        text_hash, proposal_key = svc.proposal_identity("feedback", "cite sources")
        assert rows == [
            {
                "type": "feedback",
                "text": "cite sources",
                "text_hash": text_hash,
                "proposal_key": proposal_key,
                "change_kind": "long_term",
                "replaces_entry_key": None,
                "reason": None,
                "origin": "assistant_inference",
                "confidence": 0.5,
                "evidence_refs": [],
                "evidence_count": 0,
                "counts_as_evidence": False,
                "metadata": {"sensitivity": "private"},
            }
        ]

    async def test_once_never_reaches_durable_proposal_queue(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        insert = AsyncMock(return_value=0)
        monkeypatch.setattr(svc.db_proposals, "insert_proposals", insert)
        count = await svc.project_proposals(
            user_id="u1",
            thread_id="t1",
            task_run_id="r1",
            proposals=[
                {"type": "feedback", "text": "only for this task", "change_kind": "once"}
            ],
        )
        assert count == 0
        insert.assert_not_awaited()

    async def test_origin_changes_queue_identity_and_untrusted_refs_stay_unverified(
        self, monkeypatch
    ):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        insert = AsyncMock(return_value=2)
        monkeypatch.setattr(svc.db_proposals, "insert_proposals", insert)
        await svc.project_proposals(
            user_id="u1",
            thread_id="t1",
            task_run_id="r1",
            proposals=[
                {
                    "type": "reference",
                    "text": "same source fact",
                    "origin": "web",
                    "evidence_refs": [
                        {"kind": "web_source", "id": "web-1", "verified": True}
                    ],
                    "evidence_count": 999,
                    "counts_as_evidence": True,
                },
                {"type": "reference", "text": "same source fact", "origin": "tool"},
            ],
        )
        rows = insert.await_args.kwargs["proposals"]
        assert len(rows) == 2
        assert rows[0]["text_hash"] != rows[1]["text_hash"]
        assert rows[0]["proposal_key"] != rows[1]["proposal_key"]
        assert [row["origin"] for row in rows] == ["web", "tool"]
        assert rows[0]["evidence_refs"] == [
            {"kind": "web_source", "id": "web-1", "verified": False}
        ]
        assert rows[0]["evidence_count"] == 0
        assert rows[0]["counts_as_evidence"] is False
        assert rows[0]["metadata"] == {"sensitivity": "private"}

    async def test_policy_metadata_and_secret_are_preserved_for_owner_review(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        insert = AsyncMock(return_value=1)
        monkeypatch.setattr(svc.db_proposals, "insert_proposals", insert)
        await svc.project_proposals(
            user_id="u1",
            thread_id="t1",
            task_run_id="r1",
            proposals=[
                {
                    "type": "reference",
                    "text": "private local credential hint",
                    "metadata": {
                        "sensitivity": "secret",
                        "applies_when": "only for this owner",
                        "evidence_quote_ref": {
                            "message_id": "m1",
                            "start": 0,
                            "end": 6,
                            "unit": "unicode_codepoint",
                        },
                    },
                }
            ],
        )
        row = insert.await_args.kwargs["proposals"][0]
        assert row["metadata"] == {
            "sensitivity": "secret",
            "applies_when": "only for this owner",
            "evidence_quote_ref": {
                "message_id": "m1",
                "start": 0,
                "end": 6,
                "unit": "unicode_codepoint",
            },
        }

    async def test_typed_tool_denials_participate_in_queue_identity(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        insert = AsyncMock(return_value=1)
        monkeypatch.setattr(svc.db_proposals, "insert_proposals", insert)
        await svc.project_proposals(
            user_id="u1",
            thread_id="t1",
            task_run_id="r1",
            proposals=[
                {
                    "type": "user",
                    "text": "Do not make transfers.",
                    "kind": "constraint",
                    "denied_tools": [{"type": "tool", "name": "TransferFunds"}],
                    "valid_until": "2027-01-01T00:00:00Z",
                    "metadata": {"applies_when": "while travelling"},
                }
            ],
        )

        row = insert.await_args.kwargs["proposals"][0]
        assert row["metadata"] == {
            "sensitivity": "private",
            "applies_when": "while travelling",
            "kind": "constraint",
            "denied_tools": [{"type": "tool", "name": "TransferFunds"}],
            "valid_until": "2027-01-01T00:00:00+00:00",
        }
        assert row["text_hash"] != svc.proposal_identity(
            "user", "Do not make transfers.", policy_metadata={"sensitivity": "private"}
        )[0]

    async def test_typed_policy_roundtrips_through_review_yaml_and_authority_write(
        self, monkeypatch
    ):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        proposal = {
            "type": "user",
            "text": "Do not use shell execution.",
            "kind": "constraint",
            "denied_tools": [{"type": "tool", "name": "Bash"}],
            "valid_from": "2099-01-01T00:00:00Z",
            "valid_until": "2099-02-01T00:00:00Z",
            "metadata": {
                "applies_when": "while travelling",
                "exceptions": "only descriptive",
            },
        }
        queued = AsyncMock(return_value=1)
        monkeypatch.setattr(svc.db_proposals, "insert_proposals", queued)
        await svc.project_proposals(
            user_id="u1", thread_id="t1", task_run_id="r1", proposals=[proposal]
        )
        stored_row = queued.await_args.kwargs["proposals"][0]
        monkeypatch.setattr(
            svc.db_proposals,
            "list_proposals",
            AsyncMock(
                return_value=[
                    {
                        "proposal_id": "p1",
                        "proposal_type": "user",
                        "text": proposal["text"],
                        "status": "proposed",
                        "metadata": stored_row["metadata"],
                    }
                ]
            ),
        )
        reviewed = (await svc.list_pending_proposals(user_id="u1"))[0]
        assert reviewed["metadata"] == stored_row["metadata"]

        detail = proposal_approval._detail_content(
            {
                "proposal_type": reviewed["type"],
                "text": reviewed["text"],
                "metadata": reviewed["metadata"],
            },
            now="2098-01-01T00:00:00+00:00",
        )
        approved_frontmatter = yaml.safe_load(detail["content"].split("---\n", 2)[1])
        assert approved_frontmatter["denied_tools"] == [
            {"type": "tool", "name": "Bash"}
        ]
        assert approved_frontmatter["valid_from"] == "2099-01-01T00:00:00+00:00"
        assert approved_frontmatter["valid_until"] == "2099-02-01T00:00:00+00:00"

        writes = []

        async def read_entry(**_kwargs):
            return {
                "entry_id": "22222222-2222-2222-2222-222222222222",
                "content_hash": "sha256:" + "a" * 64,
            }

        async def persist(**values):
            writes.append(values)

        await sync_approved_memory_policy(
            user_id="u1",
            proposal={"metadata": reviewed["metadata"]},
            approved_entry_key="approved.md",
            read_entry=read_entry,
            persist=persist,
        )
        assert writes[0]["denied_tools"] == approved_frontmatter["denied_tools"]
        assert writes[0]["valid_from"] == approved_frontmatter["valid_from"]
        assert writes[0]["valid_until"] == approved_frontmatter["valid_until"]


class TestList:
    async def test_flag_off_is_none_without_reading(self, monkeypatch):
        monkeypatch.delenv("REFACTOR_MEMORY_INDEX_ENABLED", raising=False)
        read = AsyncMock()
        monkeypatch.setattr(svc.db_proposals, "list_proposals", read)
        assert await svc.list_pending_proposals(user_id="u1") is None
        read.assert_not_awaited()

    async def test_maps_rows_and_bounds(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        read = AsyncMock(
            return_value=[
                {
                    "proposal_id": "p1",
                    "proposal_type": "feedback",
                    "text": "cite sources",
                    "task_run_id": "r1",
                    "thread_id": "t1",
                    "status": "proposed",
                    "created_at": datetime(2026, 10, 25, tzinfo=timezone.utc),
                    "origin": "web",
                    "confidence": 0.4,
                    "evidence_refs": [
                        {"kind": "web_source", "id": "web-1", "verified": True}
                    ],
                    "evidence_count": 10,
                    "counts_as_evidence": True,
                }
            ]
        )
        monkeypatch.setattr(svc.db_proposals, "list_proposals", read)
        rows = await svc.list_pending_proposals(user_id="u1", limit=10)
        assert rows[0]["proposal_id"] == "p1"
        assert rows[0]["type"] == "feedback"
        assert rows[0]["status"] == "proposed"
        assert rows[0]["proposed_at"] == "2026-10-25 00:00:00+00:00"
        assert rows[0]["origin"] == "web"
        assert rows[0]["confidence"] == 0.4
        assert rows[0]["evidence_count"] == 0
        assert rows[0]["counts_as_evidence"] is False
        read.assert_awaited_once_with(user_id="u1", status="proposed", limit=10)
        with pytest.raises(ContractError, match="page size"):
            await svc.list_pending_proposals(user_id="u1", limit=0)

    async def test_owner_review_keeps_secret_proposals_visible(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        monkeypatch.setattr(
            svc.db_proposals,
            "list_proposals",
            AsyncMock(
                return_value=[
                    {
                        "proposal_id": "p-secret",
                        "proposal_type": "user",
                        "text": "private local fact",
                        "status": "proposed",
                        "metadata": {"sensitivity": "secret"},
                    }
                ]
            ),
        )
        rows = await svc.list_pending_proposals(user_id="u1")
        assert rows[0]["metadata"] == {"sensitivity": "secret"}
        assert rows[0]["text"] == "private local fact"

    async def test_owner_review_shows_exact_denials_and_approved_window(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        monkeypatch.setattr(
            svc.db_proposals,
            "list_proposals",
            AsyncMock(
                return_value=[
                    {
                        "proposal_id": "p-constraint",
                        "proposal_type": "user",
                        "text": "Do not make transfers.",
                        "status": "proposed",
                        "metadata": {
                            "kind": "constraint",
                            "denied_tools": [
                                {"type": "tool", "name": "TransferFunds"},
                                {"type": "mcp", "server": "banking", "tool": "send"},
                            ],
                            "valid_from": "2026-11-01T00:00:00+00:00",
                            "valid_until": "2027-01-01T00:00:00+00:00",
                            "applies_when": "while travelling",
                            "exceptions": "unless urgent",
                        },
                    }
                ]
            ),
        )

        rows = await svc.list_pending_proposals(user_id="u1")

        assert rows[0]["metadata"]["denied_tools"] == [
            {"type": "tool", "name": "TransferFunds"},
            {"type": "mcp", "server": "banking", "tool": "send"},
        ]
        assert rows[0]["metadata"]["valid_from"] == "2026-11-01T00:00:00+00:00"
        assert rows[0]["metadata"]["valid_until"] == "2027-01-01T00:00:00+00:00"
        # Conditions and exceptions are shown for owner context only; the
        # typed deny remains exact and unconditional within its validity window.
        assert rows[0]["metadata"]["applies_when"] == "while travelling"
        assert rows[0]["metadata"]["exceptions"] == "unless urgent"


class TestDecide:
    async def test_flag_off_is_noop(self, monkeypatch):
        monkeypatch.delenv("REFACTOR_MEMORY_INDEX_ENABLED", raising=False)
        assert await svc.decide_proposal(
            proposal_id="p1", user_id="u1", approve=True, approval_source="manual"
        ) is None

    async def test_missing_proposal_is_contract_error(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        monkeypatch.setattr(svc.db_proposals, "decide_proposal", AsyncMock(return_value=None))
        with pytest.raises(ContractError, match="not found"):
            await svc.decide_proposal(
                proposal_id="p1", user_id="u1", approve=True,
                approval_source="manual", store=object()
            )

    async def test_already_decided_is_contract_error(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        monkeypatch.setattr(
            svc.db_proposals, "decide_proposal",
            AsyncMock(return_value={"status": "approved"})
        )
        with pytest.raises(ContractError, match="already"):
            await svc.decide_proposal(
                proposal_id="p1", user_id="u1", approve=False,
                approval_source="manual"
            )

    async def test_rejection_moves_owned_proposal_without_store_write(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        decide = AsyncMock(return_value={"proposal_id": "p1", "status": "rejected"})
        monkeypatch.setattr(svc.db_proposals, "decide_proposal", decide)
        row = await svc.decide_proposal(
            proposal_id="p1", user_id="u1", approve=False, approval_source="manual"
        )
        assert row["status"] == "rejected"
        decide.assert_awaited_once_with(
            proposal_id="p1", user_id="u1", expected_status="proposed",
            target_status="rejected", approval_source="manual", before_decide=None
        )

    async def test_approval_writes_store_inside_locked_decision(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        proposal = {"proposal_id": "p1", "user_id": "u1", "status": "proposed"}
        store = object()
        write = AsyncMock()
        monkeypatch.setattr(
            "src.server.services.refactor.proposal_approval.approve_proposal", write
        )

        async def decide(**kwargs):
            assert kwargs["approval_source"] == "manual"
            await kwargs["before_decide"](proposal)
            return {**proposal, "status": "approved"}

        decide_mock = AsyncMock(side_effect=decide)
        monkeypatch.setattr(svc.db_proposals, "decide_proposal", decide_mock)
        row = await svc.decide_proposal(
            proposal_id="p1", user_id="u1", approve=True,
            approval_source="manual", store=store
        )
        assert row["status"] == "approved"
        write.assert_awaited_once_with(
            proposal=proposal, store=store, owner_id="u1", approval_source="manual"
        )

    async def test_consolidation_decision_reports_projection_repair_state(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        proposal = {
            "proposal_id": "p2",
            "user_id": "u1",
            "status": "proposed",
            "producer_kind": "memory_consolidation",
        }
        write = AsyncMock(return_value={
            "detail_key": "consolidated.md",
            "index_key": "memory.md",
            "projected": 1,
            "projection_status": "pending_backfill",
        })
        monkeypatch.setattr(
            "src.server.services.refactor.proposal_approval.approve_proposal", write
        )

        async def decide(**kwargs):
            await kwargs["before_decide"](proposal)
            return {**proposal, "status": "approved"}

        monkeypatch.setattr(svc.db_proposals, "decide_proposal", AsyncMock(side_effect=decide))
        result = await svc.decide_proposal(
            proposal_id="p2", user_id="u1", approve=True,
            approval_source="manual", store=object()
        )

        assert result["status"] == "approved"
        assert result["approval"]["projection_status"] == "pending_backfill"
        assert result["approval"]["projected"] == 1

    @pytest.mark.parametrize("source", ["web", "tool", "assistant", "policy"])
    async def test_automated_approval_is_rejected_before_database_or_store(
        self, monkeypatch, source
    ):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        decide = AsyncMock()
        monkeypatch.setattr(svc.db_proposals, "decide_proposal", decide)
        with pytest.raises(ContractError, match="manual authority"):
            await svc.decide_proposal(
                proposal_id="p1", user_id="u1", approve=True,
                approval_source=source, store=object()
            )
        decide.assert_not_awaited()


class TestOwnerRead:
    async def test_flag_off_is_none(self, monkeypatch):
        monkeypatch.delenv("REFACTOR_MEMORY_INDEX_ENABLED", raising=False)
        assert (
            await svc.get_proposal_for_owner(proposal_id="p1", user_id="u1") is None
        )

    async def test_missing_is_none(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        monkeypatch.setattr(svc.db_proposals, "get_proposal", AsyncMock(return_value=None))
        assert (
            await svc.get_proposal_for_owner(proposal_id="p1", user_id="u1") is None
        )

    async def test_wrong_owner_is_none(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        monkeypatch.setattr(
            svc.db_proposals,
            "get_proposal",
            AsyncMock(
                return_value={
                    "user_id": "22222222-2222-2222-2222-222222222222",
                    "status": "proposed",
                }
            ),
        )
        assert (
            await svc.get_proposal_for_owner(
                proposal_id="p1", user_id="11111111-1111-1111-1111-111111111111"
            )
            is None
        )

    async def test_owner_gets_the_row(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        row = {
            "user_id": "11111111-1111-1111-1111-111111111111",
            "status": "proposed",
        }
        monkeypatch.setattr(svc.db_proposals, "get_proposal", AsyncMock(return_value=row))
        assert (
            await svc.get_proposal_for_owner(
                proposal_id="p1", user_id="11111111-1111-1111-1111-111111111111"
            )
            == row
        )
