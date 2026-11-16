"""Store-writing proposal approval: the store is written before the queue moves.

The approval must produce a real memory file plus its index pointer, and it must
reuse the same file on retry rather than appending a second pointer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import yaml
import hashlib

from src.server.services.refactor import proposal_approval as svc
from src.server.services.refactor.memory_projection import parse_memory_index
from src.server.services.refactor.memory_projection import content_hash, parse_memory_metadata

pytestmark = pytest.mark.asyncio

PROPOSAL = {
    "proposal_id": "p1",
    "user_id": "11111111-1111-1111-1111-111111111111",
    "thread_id": "t1",
    "task_run_id": "r1",
    "proposal_type": "feedback",
    "proposal_key": "feedback-abc123",
    "text": "Always cite sources in research answers.",
    "status": "proposed",
}


class _Item:
    def __init__(self, value):
        self.value = value


class FakeStore:
    def __init__(self, entries=None):
        self.entries = dict(entries or {})
        self.aput_calls = []
        self.fail_after_put = None
        self.fail_after_put_occurrence = 1
        self._put_counts = {}

    async def aget(self, namespace, key):
        value = self.entries.get((tuple(namespace), key))
        return _Item(value) if value is not None else None

    async def aput(self, namespace, key, value):
        self.aput_calls.append((tuple(namespace), key, value))
        self.entries[(tuple(namespace), key)] = value
        identity = (tuple(namespace), key)
        self._put_counts[identity] = self._put_counts.get(identity, 0) + 1
        if (
            self.fail_after_put == identity
            and self._put_counts[identity] == self.fail_after_put_occurrence
        ):
            self.fail_after_put = None
            raise RuntimeError("fixture failed after store write")
        return value

    async def adelete(self, namespace, key):
        self.entries.pop((tuple(namespace), key), None)
        return None


def _patch_projection(monkeypatch):
    project = AsyncMock(return_value={"row": {}})
    monkeypatch.setattr(
        "src.server.services.refactor.memory_dual_write.project_memory_write",
        project,
    )
    monkeypatch.setattr(
        "src.server.database.refactor.memory.get_entry",
        AsyncMock(return_value={"entry_id": "22222222-2222-2222-2222-222222222222"}),
    )
    monkeypatch.setattr(
        "src.server.database.refactor.memory.get_active_entry_for_approval",
        AsyncMock(return_value={
            "entry_id": "22222222-2222-2222-2222-222222222222",
            "content_hash": "sha256:" + "a" * 64,
        }),
    )
    monkeypatch.setattr(
        "src.server.database.refactor.memory_tool_policy.has_active_denials",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "src.server.database.refactor.memory.get_projection_rows",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "src.server.database.refactor.memory.restore_projection_rows",
        AsyncMock(return_value=None),
    )
    return project


class TestApprove:
    @pytest.fixture(autouse=True)
    def _offline_projection_snapshot_db(self, monkeypatch):
        from src.server.database.refactor import memory as db_memory
        from src.server.database.refactor import memory_tool_policy as db_policy

        monkeypatch.setattr(
            db_memory, "get_projection_rows", AsyncMock(return_value=[])
        )
        monkeypatch.setattr(
            db_memory, "restore_projection_rows", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            db_policy, "get_denials_snapshot", AsyncMock(return_value=[])
        )
        monkeypatch.setattr(
            db_policy, "restore_denials_snapshot", AsyncMock(return_value=None)
        )
        yield db_memory

    @pytest.fixture(autouse=True)
    def _offline_policy_db(self, monkeypatch):
        monkeypatch.setattr(
            "src.server.database.refactor.memory_tool_policy.has_active_denials",
            AsyncMock(return_value=False),
        )

    @pytest.mark.parametrize(
        "body",
        [
            "\n  leading blank and indentation\n",
            "  indented first line\nsecond line\n",
            "first line with trailing spaces  \n",
            "CRLF first line\r\nsecond line\r\n",
        ],
    )
    async def test_lifecycle_annotation_preserves_exact_body_bytes(self, body):
        source = f"---\r\ndescription: old\r\nlifecycle: active\r\n---\r\n{body}"

        annotated = svc._annotate_lifecycle(source, lifecycle="active", reason="corrected")
        expected = svc._annotate_lifecycle(source, lifecycle="pending", reason="corrected")

        assert svc._matches_proposal(annotated, expected)
        match = svc._DETAIL_FRONTMATTER_RE.match(annotated)
        assert match is not None
        assert match.group("body") == body

    async def test_writes_detail_and_pointer_then_projects(self, monkeypatch):
        project = _patch_projection(monkeypatch)
        store = FakeStore()
        result = await svc.approve_proposal(
            proposal=PROPOSAL, store=store, owner_id=PROPOSAL["user_id"],
            approval_source="manual"
        )
        assert result["detail_key"] == "feedback-abc123--rp1.md"
        detail = store.entries[(PROPOSAL["user_id"], "memory"), "feedback-abc123--rp1.md"]
        assert detail["content"].startswith("---\ndescription: Always cite sources")
        assert "type: feedback" in detail["content"]
        assert "Always cite sources in research answers." in detail["content"]
        index = store.entries[(PROPOSAL["user_id"], "memory"), "memory.md"]
        assert index["content"].strip().startswith("- [Always cite sources")
        assert "(feedback-abc123--rp1.md)" in index["content"]
        assert project.await_count >= 2
        projected_keys = [call.kwargs["key"] for call in project.await_args_list]
        assert "feedback-abc123--rp1.md" in projected_keys
        assert "memory.md" in projected_keys

    async def test_preserves_origin_without_trusting_claimed_evidence(self, monkeypatch):
        _patch_projection(monkeypatch)
        proposal = {
            **PROPOSAL,
            "text": 'Source says: "use the quoted rule".',
            "origin": "web",
            "confidence": 0.9,
            "evidence_refs": [
                {"kind": "web_source", "id": "https://example.invalid/a", "verified": True}
            ],
            "evidence_count": 10,
            "counts_as_evidence": True,
        }
        store = FakeStore()
        await svc.approve_proposal(
            proposal=proposal, store=store, owner_id=PROPOSAL["user_id"],
            approval_source="manual"
        )

        detail = store.entries[(PROPOSAL["user_id"], "memory"), "feedback-abc123--rp1.md"]
        frontmatter = yaml.safe_load(detail["content"].split("---\n", 2)[1])
        assert frontmatter["origin"] == "web"
        assert frontmatter["confidence"] == 0.9
        assert frontmatter["evidence_refs"] == [
            {"kind": "web_source", "id": "https://example.invalid/a", "verified": False}
        ]
        assert frontmatter["evidence_count"] == 0
        assert frontmatter["counts_as_evidence"] is False
        assert 'Source says: "use the quoted rule".' in detail["content"]

    async def test_policy_metadata_survives_local_approval_and_projection(self, monkeypatch):
        _patch_projection(monkeypatch)
        proposal = {
            **PROPOSAL,
            "metadata": {
                "sensitivity": "secret",
                "applies_when": "only in this owner's local account",
                "exceptions": "never share externally",
                "evidence_quote_ref": {
                    "message_id": "m1",
                    "start": 0,
                    "end": 5,
                    "unit": "unicode_codepoint",
                },
            },
        }
        store = FakeStore()
        await svc.approve_proposal(
            proposal=proposal,
            store=store,
            owner_id=PROPOSAL["user_id"],
            approval_source="manual",
        )
        detail = store.entries[(PROPOSAL["user_id"], "memory"), svc._detail_key(proposal)]
        frontmatter = yaml.safe_load(detail["content"].split("---\n", 2)[1])
        assert frontmatter["sensitivity"] == "secret"
        assert frontmatter["applies_when"] == "only in this owner's local account"
        assert frontmatter["exceptions"] == "never share externally"
        assert frontmatter["evidence_quote_ref"] == proposal["metadata"]["evidence_quote_ref"]
        index_item = store.entries.get(((PROPOSAL["user_id"], "memory"), "memory.md"))
        assert index_item and svc._detail_key(proposal) in index_item["content"]

    async def test_approval_records_only_authoritative_source_response_id(self, monkeypatch):
        _patch_projection(monkeypatch)
        thread_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        response_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        proposal = {
            **PROPOSAL,
            "thread_id": thread_id,
            "task_run_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "source_response_id": response_id,
        }
        store = FakeStore()
        await svc.approve_proposal(
            proposal=proposal,
            store=store,
            owner_id=PROPOSAL["user_id"],
            approval_source="manual",
        )
        detail = store.entries[(PROPOSAL["user_id"], "memory"), svc._detail_key(proposal)]
        frontmatter = yaml.safe_load(detail["content"].split("---\n", 2)[1])
        assert frontmatter["source_thread_id"] == thread_id
        assert frontmatter["source_response_id"] == response_id
        assert frontmatter["source_response_id"] != proposal["task_run_id"]
        assert frontmatter["source_task_run_id"] == proposal["task_run_id"]

    async def test_retry_promotes_the_legacy_pointer_without_duplicating(self, monkeypatch):
        _patch_projection(monkeypatch)
        legacy = "- [Always cite sources in research answers.](proposed/feedback-abc123.md) — Always cite sources in research answers."
        store = FakeStore()
        store.entries[((PROPOSAL["user_id"], "memory"), "memory.md")] = {
            "content": f"# Memory\n{legacy}\n"
        }
        store.entries[((PROPOSAL["user_id"], "memory"), "proposed/feedback-abc123.md")] = {
            "content": "---\ndescription: old\n---\nold"
        }
        before = len(store.aput_calls)
        await svc.approve_proposal(
            proposal=PROPOSAL, store=store, owner_id=PROPOSAL["user_id"],
            approval_source="manual"
        )
        index_puts = [c for c in store.aput_calls[before:] if c[1] == "memory.md"]
        assert len(index_puts) == 1
        content = store.entries[(PROPOSAL["user_id"], "memory"), "memory.md"]["content"]
        assert "proposed/" not in content
        assert content.count("(feedback-abc123--rp1.md)") == 1

        # A second approval is idempotent: no second pointer and no index write.
        before = len(store.aput_calls)
        await svc.approve_proposal(
            proposal=PROPOSAL, store=store, owner_id=PROPOSAL["user_id"],
            approval_source="manual"
        )
        assert [c for c in store.aput_calls[before:] if c[1] == "memory.md"] == []
        assert store.entries[(PROPOSAL["user_id"], "memory"), "memory.md"]["content"].count("(feedback-abc123--rp1.md)") == 1

    async def test_store_failure_propagates(self, monkeypatch):
        _patch_projection(monkeypatch)

        class Failing(FakeStore):
            async def aput(self, namespace, key, value):
                raise RuntimeError("store down")

        with pytest.raises(RuntimeError, match="store down"):
            await svc.approve_proposal(
                proposal=PROPOSAL, store=Failing(), owner_id=PROPOSAL["user_id"],
                approval_source="manual"
            )

    async def test_policy_validation_runs_before_any_activation(
        self, monkeypatch
    ):
        """A replacement that would fail its policy write must fail early."""
        _patch_projection(monkeypatch)
        monkeypatch.setattr(
            "src.server.database.refactor.memory_tool_policy.has_active_denials",
            AsyncMock(return_value=True),
        )
        old_key = "pref-r1.md"
        old_content = "---\ndescription: old\n---\nold body\n"
        namespace = (PROPOSAL["user_id"], "memory")
        index = f"- [old]({old_key}) — old\n"
        store = FakeStore(
            {
                (namespace, old_key): {"content": old_content},
                (namespace, "memory.md"): {"content": index},
            }
        )
        proposal = {
            **PROPOSAL,
            "change_kind": "correction",
            "replaces_entry_key": old_key,
            "reason": "incorrect statement",
            "text": "corrected statement",
        }

        with pytest.raises(ValueError, match="explicit memory kind is required"):
            await svc.approve_proposal(
                proposal=proposal,
                store=store,
                owner_id=PROPOSAL["user_id"],
                approval_source="manual",
            )

        assert (namespace, svc._detail_key(proposal)) not in store.entries
        assert store.entries[(namespace, old_key)]["content"] == old_content
        assert store.entries[(namespace, "memory.md")]["content"] == index

    async def test_policy_sync_failure_rolls_the_activation_back(
        self, monkeypatch, _offline_projection_snapshot_db
    ):
        _patch_projection(monkeypatch)
        monkeypatch.setattr(
            "src.server.services.refactor.memory_tool_policy_approval.sync_approved_memory_policy",
            AsyncMock(side_effect=RuntimeError("policy backend down")),
        )
        old_key = "pref-r1.md"
        old_content = "---\ndescription: old\n---\nold body\n"
        namespace = (PROPOSAL["user_id"], "memory")
        index = f"- [old]({old_key}) — old\n"
        store = FakeStore(
            {
                (namespace, old_key): {"content": old_content},
                (namespace, "memory.md"): {"content": index},
            }
        )
        proposal = {
            **PROPOSAL,
            "change_kind": "long_term",
            "replaces_entry_key": old_key,
            "text": "updated preference",
        }

        with pytest.raises(RuntimeError, match="policy backend down"):
            await svc.approve_proposal(
                proposal=proposal,
                store=store,
                owner_id=PROPOSAL["user_id"],
                approval_source="manual",
            )

        assert (namespace, svc._detail_key(proposal)) not in store.entries
        assert store.entries[(namespace, old_key)]["content"] == old_content
        assert store.entries[(namespace, "memory.md")]["content"] == index
        _offline_projection_snapshot_db.restore_projection_rows.assert_awaited()

    async def test_approval_cancellation_during_policy_sync_rolls_back(
        self, monkeypatch, _offline_projection_snapshot_db
    ):
        import asyncio

        _patch_projection(monkeypatch)
        from src.server.database.refactor import memory_tool_policy as db_policy

        committed = [{"entry_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "active": True}]
        db_policy.get_denials_snapshot.return_value = committed

        async def cancelled_policy(**_kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(
            "src.server.services.refactor.memory_tool_policy_approval.sync_approved_memory_policy",
            cancelled_policy,
        )
        old_key = "pref-r1.md"
        old_content = "---\ndescription: old\n---\nold body\n"
        namespace = (PROPOSAL["user_id"], "memory")
        index = f"- [old]({old_key}) — old\n"
        store = FakeStore(
            {
                (namespace, old_key): {"content": old_content},
                (namespace, "memory.md"): {"content": index},
            }
        )
        proposal = {
            **PROPOSAL,
            "change_kind": "long_term",
            "replaces_entry_key": old_key,
            "text": "updated preference",
        }

        with pytest.raises(asyncio.CancelledError):
            await svc.approve_proposal(
                proposal=proposal,
                store=store,
                owner_id=PROPOSAL["user_id"],
                approval_source="manual",
            )

        assert (namespace, svc._detail_key(proposal)) not in store.entries
        assert store.entries[(namespace, old_key)]["content"] == old_content
        assert store.entries[(namespace, "memory.md")]["content"] == index
        _offline_projection_snapshot_db.restore_projection_rows.assert_awaited()

    async def test_approval_cancellation_during_projection_rolls_back(
        self, monkeypatch, _offline_projection_snapshot_db
    ):
        import asyncio

        _patch_projection(monkeypatch)
        calls = []

        async def cancelling_projection(**kwargs):
            calls.append(kwargs["key"])
            if kwargs["key"] == "memory.md":
                raise asyncio.CancelledError()
            return {"row": {"entry_id": "11111111-1111-4111-8111-111111111111"}}

        monkeypatch.setattr(
            "src.server.services.refactor.memory_dual_write.project_memory_write",
            cancelling_projection,
        )
        old_key = "pref-r1.md"
        old_content = "---\ndescription: old\n---\nold body\n"
        namespace = (PROPOSAL["user_id"], "memory")
        index = f"- [old]({old_key}) — old\n"
        store = FakeStore(
            {
                (namespace, old_key): {"content": old_content},
                (namespace, "memory.md"): {"content": index},
            }
        )
        proposal = {
            **PROPOSAL,
            "change_kind": "long_term",
            "replaces_entry_key": old_key,
            "text": "updated preference",
        }

        with pytest.raises(asyncio.CancelledError):
            await svc.approve_proposal(
                proposal=proposal,
                store=store,
                owner_id=PROPOSAL["user_id"],
                approval_source="manual",
            )

        assert (namespace, svc._detail_key(proposal)) not in store.entries
        assert store.entries[(namespace, old_key)]["content"] == old_content
        assert store.entries[(namespace, "memory.md")]["content"] == index
        _offline_projection_snapshot_db.restore_projection_rows.assert_awaited()
        from src.server.database.refactor import memory_tool_policy as db_policy

        db_policy.restore_denials_snapshot.assert_awaited()

    async def test_approval_cancellation_during_store_writes_rolls_back(
        self, monkeypatch, _offline_projection_snapshot_db
    ):
        import asyncio

        _patch_projection(monkeypatch)
        old_key = "pref-r1.md"
        old_content = "---\ndescription: old\n---\nold body\n"
        namespace = (PROPOSAL["user_id"], "memory")
        index = f"- [old]({old_key}) — old\n"
        store = FakeStore(
            {
                (namespace, old_key): {"content": old_content},
                (namespace, "memory.md"): {"content": index},
            }
        )
        original_put = store.aput

        cancelled_once = False

        async def cancelling_put(namespace_arg, key, value):
            nonlocal cancelled_once
            if key == "memory.md" and not cancelled_once:
                cancelled_once = True
                raise asyncio.CancelledError()
            return await original_put(namespace_arg, key, value)

        monkeypatch.setattr(store, "aput", cancelling_put)
        proposal = {
            **PROPOSAL,
            "change_kind": "long_term",
            "replaces_entry_key": old_key,
            "text": "updated preference",
        }

        with pytest.raises(asyncio.CancelledError):
            await svc.approve_proposal(
                proposal=proposal,
                store=store,
                owner_id=PROPOSAL["user_id"],
                approval_source="manual",
            )

        assert (namespace, svc._detail_key(proposal)) not in store.entries
        assert store.entries[(namespace, old_key)]["content"] == old_content
        assert store.entries[(namespace, "memory.md")]["content"] == index
        _offline_projection_snapshot_db.restore_projection_rows.assert_awaited()

    async def test_policy_rollback_restores_denial_rows_beyond_page_limits(
        self, monkeypatch, _offline_projection_snapshot_db
    ):
        """Rollback restores every snapshotted row (no page limit)."""
        _patch_projection(monkeypatch)
        from src.server.database.refactor import memory_tool_policy as db_policy

        rows = [
            {
                "entry_id": f"{index:08d}-0000-4000-8000-000000000000",
                "active": True,
            }
            for index in range(513)
        ]
        db_policy.get_denials_snapshot.return_value = rows
        monkeypatch.setattr(
            "src.server.services.refactor.memory_tool_policy_approval.sync_approved_memory_policy",
            AsyncMock(side_effect=RuntimeError("policy backend down")),
        )
        old_key = "pref-r1.md"
        namespace = (PROPOSAL["user_id"], "memory")
        store = FakeStore(
            {
                (namespace, old_key): {"content": "---\ndescription: old\n---\nold\n"},
                (namespace, "memory.md"): {"content": f"- [old]({old_key}) — old\n"},
            }
        )
        proposal = {
            **PROPOSAL,
            "change_kind": "long_term",
            "replaces_entry_key": old_key,
            "text": "updated preference",
        }

        with pytest.raises(RuntimeError, match="policy backend down"):
            await svc.approve_proposal(
                proposal=proposal,
                store=store,
                owner_id=PROPOSAL["user_id"],
                approval_source="manual",
            )

        kwargs = db_policy.restore_denials_snapshot.await_args.kwargs
        assert len(kwargs["rows"]) == 513
        assert set(kwargs["entry_ids"]) >= {row["entry_id"] for row in rows}

    @pytest.mark.parametrize(
        ("fail_role", "occurrence"),
        [
            ("detail", 1),
            ("index", 1),
            ("source_a", 1),
            ("source_b", 1),
            ("detail", 2),
        ],
    )
    async def test_manual_consolidation_replaces_all_sources_and_retries(
        self, monkeypatch, fail_role, occurrence
    ):
        user_id = PROPOSAL["user_id"]
        keys = ["duplicate-a.md", "duplicate-b.md"]
        ids = [
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        ]
        body = "  preserve this leading indentation\nsecond line  \n"
        contents = [
            f"---\ndescription: duplicate\nsensitivity: private\n---\n{body}"
            for _ in keys
        ]
        rows = [
            {
                "entry_id": entry_id,
                "entry_key": key,
                "content_hash": content_hash(content),
                "status": "active",
                "namespace": (user_id, "memory"),
                "metadata": {"sensitivity": "private"},
            }
            for entry_id, key, content in zip(ids, keys, contents, strict=True)
        ]
        source_signature = ",".join(
            f"{row['entry_id']}:{row['content_hash']}" for row in rows
        )
        audit = {
            "kind": "duplicate",
            "source_signature": source_signature,
            "source_entry_keys": keys,
            "source_body_hashes": [hashlib.sha256(body.encode("utf-8")).hexdigest()] * 2,
            "source_count": 2,
            "memory_metadata": {},
        }
        proposal = {
            "proposal_id": "consolidation-1",
            "user_id": user_id,
            "producer_kind": "memory_consolidation",
            "producer_metadata": audit,
            "source_entry_ids": ids,
            "proposal_type": "user",
            "proposal_key": "memory-consolidation-test",
            "text": body,
            "metadata": {"sensitivity": "private"},
            "origin": "assistant_inference",
            "evidence_refs": [],
            "evidence_count": 0,
            "counts_as_evidence": False,
            "change_kind": "correction",
        }
        index = "\n".join(
            f"- [duplicate]({key}) — duplicate" for key in keys
        ) + "\n"
        store = FakeStore({
            ((user_id, "memory"), key): {"content": content}
            for key, content in zip(keys, contents, strict=True)
        })
        fail_key = {
            "detail": svc._detail_key(proposal),
            "index": "memory.md",
            "source_a": "duplicate-a.md",
            "source_b": "duplicate-b.md",
        }[fail_role]
        store.fail_after_put = ((user_id, "memory"), fail_key)
        store.fail_after_put_occurrence = occurrence
        store.entries[((user_id, "memory"), "memory.md")] = {"content": index}
        monkeypatch.setattr(
            "src.server.database.refactor.memory_consolidation.get_source_entries_for_retry",
            AsyncMock(side_effect=lambda **_: [dict(row) for row in rows]),
        )
        projected = AsyncMock(return_value={"row": {}})
        monkeypatch.setattr(
            "src.server.services.refactor.memory_dual_write.project_memory_write",
            projected,
        )

        with pytest.raises(RuntimeError, match="after store write"):
            await svc.approve_proposal(
                proposal=proposal,
                store=store,
                owner_id=user_id,
                approval_source="manual",
            )

        # The first source has its own lifecycle marker, while the indexed
        # staged revision proves that this exact proposal owns the partial work.
        result = await svc.approve_proposal(
            proposal=proposal,
            store=store,
            owner_id=user_id,
            approval_source="manual",
        )
        detail_key = result["detail_key"]
        assert result["projection_status"] == "complete"
        namespace = (user_id, "memory")
        detail = store.entries[(namespace, detail_key)]["content"]
        parsed = svc._detail_frontmatter(detail)
        assert parsed is not None
        metadata, actual_body = parsed
        assert actual_body == body
        assert metadata["sensitivity"] == "private"
        assert metadata["supersedes_entry_ids"] == ids
        assert set(parse_memory_index(store.entries[(namespace, "memory.md")]["content"])) == {detail_key}
        for key in keys:
            source_metadata, source_body = svc._detail_frontmatter(
                store.entries[(namespace, key)]["content"]
            )
            assert source_metadata["lifecycle"] == "superseded"
            assert source_metadata["lifecycle_reason"] == "consolidated_duplicate_or_conflict"
            assert source_body == body

        # Simulate a queue transaction rollback after Store and projection writes.
        for row, key in zip(rows, keys, strict=True):
            row["status"] = "superseded"
            current_source = store.entries[(namespace, key)]["content"]
            row["content_hash"] = content_hash(current_source)
            row["metadata"] = parse_memory_metadata(current_source)
        # A failed projection must not report approval success: the retry leaves
        # the proposal undoable (queue CAS never ran) and fails closed.
        projected.return_value = None
        with pytest.raises(RuntimeError, match="projection failed"):
            await svc.approve_proposal(
                proposal=proposal,
                store=store,
                owner_id=user_id,
                approval_source="manual",
            )
        projected.return_value = {"row": {}}
        assert list(parse_memory_index(store.entries[(namespace, "memory.md")]["content"])) == [detail_key]
        metadata_a, _ = svc._detail_frontmatter(
            store.entries[(namespace, "duplicate-a.md")]["content"]
        )
        tampered = store.entries[(namespace, "duplicate-a.md")]["content"].replace(
            body, "changed body\n"
        )
        store.entries[(namespace, "duplicate-a.md")]["content"] = tampered
        assert metadata_a["lifecycle_reason"] == "consolidated_duplicate_or_conflict"
        with pytest.raises(ValueError, match="source body changed"):
            await svc.approve_proposal(
                proposal=proposal,
                store=store,
                owner_id=user_id,
                approval_source="manual",
            )

    async def test_consolidation_rejects_frozen_tool_authority_after_projection_drift(
        self, monkeypatch
    ):
        user_id = PROPOSAL["user_id"]
        source_ids = [
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        ]
        authority_check = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "src.server.database.refactor.memory_tool_policy.has_active_denials",
            authority_check,
        )
        store = FakeStore()
        proposal = {
            "proposal_id": "consolidation-denied",
            "producer_kind": "memory_consolidation",
            "source_entry_ids": source_ids,
            "producer_metadata": {"kind": "duplicate"},
            "metadata": {"kind": "fact"},
        }

        with pytest.raises(ValueError, match="owner-approved tool constraint"):
            await svc.approve_proposal(
                proposal=proposal,
                store=store,
                owner_id=user_id,
                approval_source="manual",
            )

        authority_check.assert_awaited_once_with(
            user_id=user_id, entry_ids=source_ids
        )
        assert store.entries == {}
        assert store.aput_calls == []

    async def test_correction_preserves_old_body_and_marks_it_wrong(self, monkeypatch):
        project = _patch_projection(monkeypatch)
        old_key = "user-preference-r1.md"
        old_body = "The exact old memory body stays available.\n"
        old_content = f"---\ndescription: Old preference\n---\n\n{old_body}"
        store = FakeStore(
            {
                ((PROPOSAL["user_id"], "memory"), old_key): {"content": old_content},
                ((PROPOSAL["user_id"], "memory"), "memory.md"):
                    {"content": f"- [Old preference]({old_key}) — Old preference\n"},
            }
        )
        proposal = {
            **PROPOSAL,
            "change_kind": "correction",
            "replaces_entry_key": old_key,
            "reason": "The user corrected this preference.",
            "text": "Use English answers by default.",
        }
        result = await svc.approve_proposal(
            proposal=proposal,
            store=store,
            owner_id=PROPOSAL["user_id"],
            approval_source="manual",
        )
        old_value = store.entries[((PROPOSAL["user_id"], "memory"), old_key)]
        old_frontmatter = yaml.safe_load(old_value["content"].split("---\n", 2)[1])
        assert old_frontmatter["lifecycle"] == "wrong"
        assert old_frontmatter["reason"] == proposal["reason"]
        assert old_body in old_value["content"]
        assert old_key not in parse_memory_index(
            store.entries[((PROPOSAL["user_id"], "memory"), "memory.md")]["content"]
        )
        assert result["detail_key"] != old_key
        assert result["replaced_entry_key"] == old_key
        assert old_key in [call.kwargs["key"] for call in project.await_args_list]
        new_detail = store.entries[((PROPOSAL["user_id"], "memory"), result["detail_key"])]
        new_frontmatter = yaml.safe_load(new_detail["content"].split("---\n", 2)[1])
        assert new_frontmatter["source_entry_id"] == "22222222-2222-2222-2222-222222222222"

    async def test_replacement_is_retryable_after_index_write_succeeded(self, monkeypatch):
        project = _patch_projection(monkeypatch)
        old_key = "pref-r1.md"
        old_content = "---\ndescription: old\n---\nold body\n"
        store = FakeStore(
            {
                ((PROPOSAL["user_id"], "memory"), old_key): {"content": old_content},
                ((PROPOSAL["user_id"], "memory"), "memory.md"):
                    {"content": f"- [old]({old_key}) — old\n"},
            }
        )
        proposal = {
            **PROPOSAL,
            "change_kind": "long_term",
            "replaces_entry_key": old_key,
            "text": "updated preference",
        }
        project.side_effect = RuntimeError("projection unavailable")
        # Durable lineage is required before approval can report success, and a
        # failed projection rolls the Store materialization back.
        with pytest.raises(RuntimeError, match="projection failed"):
            await svc.approve_proposal(
                proposal=proposal, store=store, owner_id=PROPOSAL["user_id"], approval_source="manual"
            )
        first_detail_key = svc._detail_key(proposal)
        namespace = (PROPOSAL["user_id"], "memory")
        assert (namespace, first_detail_key) not in store.entries
        index = store.entries[(namespace, "memory.md")]["content"]
        assert first_detail_key not in parse_memory_index(index)
        assert store.entries[(namespace, old_key)]["content"] == old_content
        project.side_effect = None
        project.return_value = {"row": {}}
        retried = await svc.approve_proposal(
            proposal=proposal, store=store, owner_id=PROPOSAL["user_id"], approval_source="manual"
        )
        assert retried["detail_key"] == first_detail_key
        index = store.entries[((PROPOSAL["user_id"], "memory"), "memory.md")]["content"]
        assert index.count(f"({first_detail_key})") == 1
        assert index.count(f"({old_key})") == 0

    async def test_partial_projection_failure_rolls_committed_projection_back(
        self, monkeypatch, _offline_projection_snapshot_db
    ):
        """One projection may commit before a later one fails; the Store rollback
        alone would leave that row ahead of the restored Store."""
        _patch_projection(monkeypatch)  # offline DB lookups for the replacement
        calls: list[tuple[str, object]] = []

        async def partial_projection(**kwargs):
            calls.append((kwargs["key"], kwargs.get("value")))
            if kwargs["key"] == "memory.md":
                raise RuntimeError("index projection down")
            return {"row": {}}

        monkeypatch.setattr(
            "src.server.services.refactor.memory_dual_write.project_memory_write",
            partial_projection,
        )
        old_key = "pref-r1.md"
        old_content = "---\ndescription: old\n---\nold body\n"
        namespace = (PROPOSAL["user_id"], "memory")
        store = FakeStore(
            {
                (namespace, old_key): {"content": old_content},
                (namespace, "memory.md"): {"content": f"- [old]({old_key}) — old\n"},
            }
        )
        proposal = {
            **PROPOSAL,
            "change_kind": "long_term",
            "replaces_entry_key": old_key,
            "text": "updated preference",
        }

        with pytest.raises(RuntimeError, match="projection failed"):
            await svc.approve_proposal(
                proposal=proposal,
                store=store,
                owner_id=PROPOSAL["user_id"],
                approval_source="manual",
            )

        # Store restored and the committed projection explicitly undone: the
        # rollback projects the previous Store values back (index included).
        detail_key = svc._detail_key(proposal)
        assert (namespace, detail_key) not in store.entries
        assert store.entries[(namespace, old_key)]["content"] == old_content
        # The index projection fails after the detail/replacement projections
        # committed; the rollback restores the database projection from the
        # pre-activation snapshot instead of re-deriving it.
        db_memory = _offline_projection_snapshot_db
        db_memory.restore_projection_rows.assert_awaited_once()
        kwargs = db_memory.restore_projection_rows.await_args.kwargs
        assert kwargs["entry_keys"][:2] == [detail_key, old_key]
        assert kwargs["rows"] == []
        assert store.entries[(namespace, "memory.md")]["content"] == (
            f"- [old]({old_key}) — old\n"
        )

    async def test_snapshot_read_failure_aborts_before_any_mutation(
        self, monkeypatch
    ):
        _patch_projection(monkeypatch)

        class FailingRead(FakeStore):
            def __init__(self, values):
                super().__init__(values)
                self.fail_reads = 1

            async def aget(self, namespace, key):
                if self.fail_reads and key == "memory.md":
                    self.fail_reads -= 1
                    raise RuntimeError("transient Store read failure")
                return await super().aget(namespace, key)

        old_key = "pref-r1.md"
        old_content = "---\ndescription: old\n---\nold body\n"
        namespace = (PROPOSAL["user_id"], "memory")
        store = FailingRead(
            {
                (namespace, old_key): {"content": old_content},
                (namespace, "memory.md"): {"content": f"- [old]({old_key}) — old\n"},
            }
        )
        proposal = {
            **PROPOSAL,
            "change_kind": "long_term",
            "replaces_entry_key": old_key,
            "text": "updated preference",
        }

        with pytest.raises(RuntimeError, match="Store read failure"):
            await svc.approve_proposal(
                proposal=proposal,
                store=store,
                owner_id=PROPOSAL["user_id"],
                approval_source="manual",
            )

        # Aborted before any activation write.
        assert (namespace, svc._detail_key(proposal)) not in store.entries
        assert store.entries[(namespace, old_key)]["content"] == old_content

    async def test_partial_store_failure_leaves_new_revision_pending_for_retry(self, monkeypatch):
        _patch_projection(monkeypatch)
        old_key = "pref-r1.md"
        old_content = "---\ndescription: old\n---\nold body\n"

        class FailIndexOnce(FakeStore):
            fail = True

            async def aput(self, namespace, key, value):
                if key == "memory.md" and self.fail:
                    self.fail = False
                    raise RuntimeError("index write failed")
                return await super().aput(namespace, key, value)

        store = FailIndexOnce(
            {
                ((PROPOSAL["user_id"], "memory"), old_key): {"content": old_content},
                ((PROPOSAL["user_id"], "memory"), "memory.md"):
                    {"content": f"- [old]({old_key}) — old\n"},
            }
        )
        proposal = {
            **PROPOSAL,
            "change_kind": "correction",
            "replaces_entry_key": old_key,
            "reason": "incorrect statement",
            "text": "corrected statement",
        }
        with pytest.raises(RuntimeError, match="index write failed"):
            await svc.approve_proposal(
                proposal=proposal, store=store, owner_id=PROPOSAL["user_id"], approval_source="manual"
            )
        new_key = svc._detail_key(proposal)
        # Compensation restores the pre-activation Store exactly; the retry
        # starts from a clean state rather than a half-written revision.
        assert ((PROPOSAL["user_id"], "memory"), new_key) not in store.entries
        assert (
            store.entries[((PROPOSAL["user_id"], "memory"), old_key)]["content"]
            == old_content
        )
        assert store.entries[((PROPOSAL["user_id"], "memory"), "memory.md")][
            "content"
        ] == f"- [old]({old_key}) — old\n"
        result = await svc.approve_proposal(
            proposal=proposal,
            store=store,
            owner_id=PROPOSAL["user_id"],
            approval_source="manual",
        )
        assert result["detail_key"] == new_key
        assert yaml.safe_load(
            store.entries[((PROPOSAL["user_id"], "memory"), new_key)]["content"].split(
                "---\n", 2
            )[1]
        )["lifecycle"] == "active"

    async def test_retry_does_not_resurrect_a_superseded_immutable_revision(self, monkeypatch):
        _patch_projection(monkeypatch)
        key = svc._detail_key(PROPOSAL)
        stale = svc._detail_content(PROPOSAL, now="2026-11-02T00:00:00+00:00")
        stale["content"] = svc._annotate_lifecycle(
            stale["content"], lifecycle="superseded", reason="long_term_change"
        )
        store = FakeStore(
            {
                ((PROPOSAL["user_id"], "memory"), key): stale,
                ((PROPOSAL["user_id"], "memory"), "memory.md"):
                    {"content": "- [newer](other-revision.md) — newer\n"},
            }
        )
        with pytest.raises(ValueError, match="immutable revision key"):
            await svc.approve_proposal(
                proposal=PROPOSAL,
                store=store,
                owner_id=PROPOSAL["user_id"],
                approval_source="manual",
            )
        assert store.aput_calls == []

    async def test_replacement_target_must_be_in_the_current_owner_index(self, monkeypatch):
        _patch_projection(monkeypatch)
        proposal = {
            **PROPOSAL,
            "change_kind": "correction",
            "replaces_entry_key": "other-user.md",
            "reason": "wrong",
        }
        with pytest.raises(ValueError, match="missing from this owner namespace"):
            await svc.approve_proposal(
                proposal=proposal,
                store=FakeStore(),
                owner_id=PROPOSAL["user_id"],
                approval_source="manual",
            )

    async def test_missing_owner_is_rejected(self, monkeypatch):
        _patch_projection(monkeypatch)
        with pytest.raises(ValueError, match="no store owner"):
            await svc.approve_proposal(
                proposal={"text": "x"}, store=FakeStore(), approval_source="manual"
            )

    async def test_nonmanual_approval_is_rejected_before_store_write(self):
        store = FakeStore()
        with pytest.raises(ValueError, match="manual authority"):
            await svc.approve_proposal(
                proposal=PROPOSAL, store=store, owner_id=PROPOSAL["user_id"],
                approval_source="assistant"
            )
        assert store.aput_calls == []
