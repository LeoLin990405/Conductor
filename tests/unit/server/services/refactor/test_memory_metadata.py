"""Structured memory metadata: parsing, clamping and temporal validity."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.server.services.refactor.memory_context import select_memory_context
from src.server.services.refactor.memory_projection import (
    fallback_index_text,
    is_current,
    parse_memory_metadata,
    projectable_rows,
)


class TestParse:
    def test_reads_optional_fields(self):
        metadata = parse_memory_metadata(
            "---\n"
            "description: Caching thesis\n"
            "kind: thesis\n"
            "origin: web\n"
            "importance: 0.8\n"
            "confidence: 0.65\n"
            "valid_from: 2026-01-01\n"
            "tags: [caching, infra]\n"
            "entities:\n  - project-atlas\n"
            "supersedes: old-thesis.md\n"
            "source: research/2026-01.md\n"
            "---\n\nbody\n"
        )
        assert metadata == {
            "kind": "thesis",
            "origin": "web",
            "importance": 0.8,
            "confidence": 0.65,
            "valid_from": "2026-01-01T00:00:00",
            "tags": ["caching", "infra"],
            "entities": ["project-atlas"],
            "supersedes": ["old-thesis.md"],
            "source": "research/2026-01.md",
            "evidence_refs": [],
            "evidence_count": 0,
            "counts_as_evidence": False,
            "sensitivity": "private",
        }

    def test_clamps_numbers_and_ignores_bad_yaml(self):
        clamped = parse_memory_metadata(
            "---\nimportance: 9\nconfidence: -1\n---\nbody"
        )
        assert clamped["importance"] == 1.0
        assert clamped["confidence"] == 0.0
        assert parse_memory_metadata("---\n[unterminated\n---\nbody")["origin"] == "assistant_inference"

    def test_nonfinite_boolean_and_out_of_range_confidence_are_unknown(self):
        for value in (".nan", ".inf", "true"):
            metadata = parse_memory_metadata(f"---\nconfidence: {value}\n---\nbody")
            assert metadata["confidence"] == 0.5
            assert metadata["origin"] == "assistant_inference"

    def test_old_files_default_to_inference_metadata(self):
        metadata = parse_memory_metadata("plain body")
        assert metadata["origin"] == "assistant_inference"
        assert metadata["confidence"] == 0.5
        assert metadata["evidence_count"] == 0
        assert not metadata["counts_as_evidence"]

    def test_source_entry_id_is_validated_as_canonical_uuid(self):
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        assert parse_memory_metadata(
            f"---\nsource_entry_id: {source_id}\n---\nbody"
        )["source_entry_id"] == source_id
        assert parse_memory_metadata(
            "---\nsource_entry_id: not-a-uuid\n---\nbody"
        )["origin"] == "assistant_inference"

    def test_policy_and_reversion_audit_metadata_survive_projection(self):
        entry_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        thread_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        response_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        metadata = parse_memory_metadata(
            "---\n"
            "sensitivity: public\n"
            "applies_when: before sending email\n"
            "exceptions: unless explicitly requested\n"
            "evidence_quote_ref:\n"
            "  message_id: msg-1\n"
            "  start: 2\n  end: 5\n  unit: unicode_codepoint\n"
            f"restores_entry_id: {entry_id}\n"
            f"source_thread_id: {thread_id}\n"
            f"source_response_id: {response_id}\n"
            "restores_entry_key: old.md\n"
            "reverted_to_entry_key: current.md\n"
            "restore_reason: owner asked\n"
            "lifecycle_reason: undone by owner\n"
            "---\nbody\n"
        )
        assert metadata["sensitivity"] == "public"
        assert metadata["applies_when"] == "before sending email"
        assert metadata["exceptions"] == "unless explicitly requested"
        assert metadata["evidence_quote_ref"] == {
            "message_id": "msg-1",
            "start": 2,
            "end": 5,
            "unit": "unicode_codepoint",
        }
        assert metadata["restores_entry_id"] == entry_id
        assert metadata["source_thread_id"] == thread_id
        assert metadata["source_response_id"] == response_id
        assert metadata["restores_entry_key"] == "old.md"
        assert metadata["reverted_to_entry_key"] == "current.md"
        assert metadata["restore_reason"] == "owner asked"
        assert metadata["lifecycle_reason"] == "undone by owner"

    def test_unvalidated_source_ids_and_unsafe_keys_are_dropped(self):
        metadata = parse_memory_metadata(
            "---\nsource_thread_id: thread-123\nrestores_entry_id: nope\n"
            "restores_entry_key: ../outside.md\n---\nbody"
        )
        assert "source_thread_id" not in metadata
        assert "restores_entry_id" not in metadata
        assert "restores_entry_key" not in metadata

    def test_source_task_run_id_is_uuid_validated(self):
        task_run_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        metadata = parse_memory_metadata(
            f"---\nsource_task_run_id: {task_run_id}\n---\nbody"
        )
        assert metadata["source_task_run_id"] == task_run_id
        assert "source_task_run_id" not in parse_memory_metadata(
            "---\nsource_task_run_id: task-123\n---\nbody"
        )

    def test_malformed_policy_frontmatter_fails_closed(self):
        metadata = parse_memory_metadata("---\nsensitivity: [unterminated\n---\nbody")
        assert metadata["sensitivity"] == "secret"


class TestValidity:
    def test_interval_contains_now(self):
        now = datetime.now(timezone.utc)
        assert is_current({"valid_from": (now - timedelta(days=1)).isoformat()})
        assert not is_current({"valid_until": (now - timedelta(days=1)).isoformat()})
        assert not is_current({"valid_from": (now + timedelta(days=1)).isoformat()})
        assert not is_current({"lifecycle": "pending"})
        assert not is_current({"lifecycle": "wrong"})
        assert not is_current({"lifecycle": "superseded"})
        assert is_current({"lifecycle": "active"})

    def test_expiry_boundary_is_exclusive_and_start_boundary_inclusive(self):
        boundary = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert not is_current({"valid_until": boundary.isoformat()}, now=boundary)
        assert is_current({"valid_from": boundary.isoformat()}, now=boundary)

    @pytest.mark.asyncio
    async def test_expired_entry_never_enters_context(self):
        async def search(
            user_id, tier, query, limit, thread_id=None, workspace_id=None
        ):
            if tier != "workspace":
                return []
            return [
                {
                    "entry_key": "expired",
                    "index_text": "old view",
                    "content_hash": "h1",
                    "status": "active",
                    "metadata": {"valid_until": "2020-01-01T00:00:00"},
                },
                {
                    "entry_key": "current",
                    "index_text": "new view",
                    "content_hash": "h2",
                    "status": "active",
                    "metadata": {"valid_from": "2020-01-01T00:00:00"},
                },
            ]

        context = await select_memory_context(
            user_id="u1", query="view", budget_tokens=200, search=search
        )
        assert [entry.entry_key for entry in context.entries] == ["current"]

    @pytest.mark.asyncio
    async def test_nonactive_lifecycle_never_enters_context(self):
        async def search(
            user_id, tier, query, limit, thread_id=None, workspace_id=None
        ):
            if tier != "workspace":
                return []
            return [
                {
                    "entry_key": "pending",
                    "index_text": "rule",
                    "content_hash": "h1",
                    "status": "active",
                    "metadata": {"lifecycle": "pending"},
                },
                {
                    "entry_key": "wrong",
                    "index_text": "rule",
                    "content_hash": "h2",
                    "status": "active",
                    "metadata": {"lifecycle": "wrong"},
                },
                {
                    "entry_key": "current",
                    "index_text": "rule",
                    "content_hash": "h3",
                    "status": "active",
                    "metadata": {"lifecycle": "active"},
                },
            ]

        context = await select_memory_context(
            user_id="u1", query="rule", budget_tokens=200, search=search
        )
        assert [entry.entry_key for entry in context.entries] == ["current"]


def test_projection_rows_carry_metadata():
    rows, _ = projectable_rows(
        user_id="u1",
        tier="user",
        namespace=("u1", "memory"),
        items=[
            (
                "thesis.md",
                {
                    "content": "---\nkind: thesis\nimportance: 0.9\n---\nOil is tight.\n",
                    "modified_at": "2026-01-01T00:00:00+00:00",
                },
            )
        ],
    )
    assert rows[0]["metadata"] == {
        "kind": "thesis",
        "importance": 0.9,
        "origin": "assistant_inference",
        "confidence": 0.5,
        "evidence_refs": [],
        "evidence_count": 0,
        "counts_as_evidence": False,
        "sensitivity": "private",
    }


def test_projection_rows_carry_explicit_source_entry_lineage():
    source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    rows, _ = projectable_rows(
        user_id="u1",
        tier="user",
        namespace=("u1", "memory"),
        items=[(
            "derived.md",
            {
                "content": f"---\nsource_entry_id: {source_id}\n---\nDerived text.\n"
            },
        )],
    )
    assert rows[0]["source_entry_id"] == source_id


def test_projection_and_backfill_do_not_reactivate_staged_revision():
    rows, _ = projectable_rows(
        user_id="u1",
        tier="user",
        namespace=("u1", "memory"),
        items=[
            (
                "staged.md",
                {"content": "---\nlifecycle: pending\n---\nTemporary revision.\n"},
            )
        ],
    )
    assert rows[0]["status"] == "superseded"
    assert rows[0]["metadata"]["lifecycle"] == "pending"




class TestMalformedFrontmatterFailsClosed:
    def test_unterminated_frontmatter_is_secret(self):
        # Opening delimiter with no closing one: the body may still be secret,
        # so it must not be reclassified as ordinary private memory.
        metadata = parse_memory_metadata("---\nsensitivity: secret\nstill in the block")
        assert metadata["sensitivity"] == "secret"

    def test_unterminated_frontmatter_does_not_leak_index_text(self):
        assert (
            fallback_index_text("---\nsensitivity: secret\na secret line")
            == ""
        )

    def test_plain_body_without_a_delimiter_still_projects(self):
        assert fallback_index_text("just a body line") == "just a body line"

    def test_closing_delimiter_inside_a_line_does_not_close_the_block(self):
        # `description: Draft---` must not end the metadata block; otherwise a
        # `sensitivity: secret` line following it would be ignored and the
        # document would be misclassified as private.
        content = (
            "---\n"
            "description: Draft---\n"
            "sensitivity: secret\n"
            "---\n"
            "body"
        )
        assert parse_memory_metadata(content)["sensitivity"] == "secret"
        # A secret document contributes no index text.
        assert fallback_index_text(content) == ""

    def test_complete_empty_frontmatter_is_valid_metadata(self):
        # `---` immediately followed by `---` (or a blank line between them) is
        # a complete, empty metadata block; it defaults to private metadata and
        # the body supplies the index text instead of the delimiter line.
        assert parse_memory_metadata("---\n---\nBody line")["sensitivity"] == "private"
        assert parse_memory_metadata("---\n\n---\nBody line")["sensitivity"] == "private"
        assert fallback_index_text("---\n---\nBody line") == "Body line"
        assert fallback_index_text("---\n\n---\nBody line") == "Body line"
