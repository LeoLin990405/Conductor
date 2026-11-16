"""Memory projection backfill: pointer parsing, row mapping, idempotent apply.

The store is the source of truth; these tests pin the mapping that turns it into
projection rows (one per detail entry, index line from memory.md or a content
fallback), and that apply is idempotent and failure-isolated.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from scripts import refactor_memory_backfill as backfill
from src.server.services.refactor import memory_projection as projection


@pytest.fixture(autouse=True)
def _noop_owner_lock(monkeypatch):
    """The apply path serializes with memory forget; keep it off the database."""

    @asynccontextmanager
    async def _lock(_user_id):
        yield

    monkeypatch.setattr(
        "src.server.database.refactor.memory_revisions.owner_memory_lock", _lock
    )

pytestmark = pytest.mark.asyncio


def _item(namespace: tuple[str, ...], key: str, content: Any) -> Any:
    class Item:
        pass

    item = Item()
    item.namespace = namespace
    item.key = key
    item.value = {"content": content} if not isinstance(content, dict) else content
    return item


class FakeStore:
    def __init__(self, items: list[Any], page: int = 100) -> None:
        self.items = items
        self.page = page
        self.calls: list[tuple[tuple[str, ...], int, int]] = []

    async def asearch(self, namespace, *, limit, offset):  # noqa: ANN001
        self.calls.append((tuple(namespace), limit, offset))
        matching = [item for item in self.items if item.namespace == tuple(namespace)]
        return matching[offset : offset + limit]


INDEX = """# Memory index
- [Language](prefs/language.md) — user prefers Simplified Chinese
- [Symbols](fact/symbols.md) — tracked RFC-9110
"""


class TestParsing:
    def test_parses_pointer_lines(self):
        pointers = projection.parse_memory_index(INDEX)
        assert set(pointers) == {"prefs/language.md", "fact/symbols.md"}
        pointer = pointers["prefs/language.md"]
        assert pointer.title == "Language"
        assert pointer.hook == "user prefers Simplified Chinese"
        assert pointer.line.startswith("- [Language]")

    def test_ignores_non_pointer_lines_and_links(self):
        pointers = projection.parse_memory_index(
            "# heading\n- not a pointer\n- [Wiki](https://example.com) — ignore\n"
        )
        assert pointers == {}

    def test_normalizes_dot_slash_paths(self):
        pointers = projection.parse_memory_index("- [A](./a.md) — hook")
        assert "a.md" in pointers

    def test_parses_bracket_bearing_titles_and_keeps_hook(self):
        pointers = projection.parse_memory_index(
            "- [Forgotten [secret] title](secret.md) — hidden detail"
        )
        pointer = pointers["secret.md"]
        assert pointer.title == "Forgotten [secret] title"
        assert pointer.hook == "hidden detail"

    def test_rejects_ambiguous_nested_markdown_link_title(self):
        pointers = projection.parse_memory_index(
            "- [Title [nested](safe.md) more](secret.md) — hidden detail"
        )
        assert pointers == {}


class TestFallback:
    def test_frontmatter_description_wins(self):
        content = "---\ndescription: tracked symbols for Q3\ntype: project\n---\n# Body\n"
        assert projection.fallback_index_text(content) == "tracked symbols for Q3"

    def test_first_meaningful_body_line(self):
        content = "---\ntype: reference\n---\n# Tracking notes\nmore text"
        assert projection.fallback_index_text(content) == "Tracking notes"

    def test_empty_content_has_no_index_text(self):
        assert projection.fallback_index_text("") == ""


class TestBuildRows:
    def test_one_row_per_detail_with_pointer_index_text(self):
        items = [
            ("memory.md", {"content": INDEX}),
            ("prefs/language.md", {"content": "---\ndescription: pref\n---\nbody"}),
            ("fact/symbols.md", {"content": "body without pointer"}),
        ]
        rows, skipped = backfill.build_namespace_rows(
            user_id="u1", tier="user", namespace=("u1", "memory"), items=items
        )
        assert skipped == []
        assert [row["entry_key"] for row in rows] == ["fact/symbols.md", "prefs/language.md"]
        symbols, language = rows
        assert language["index_text"] == "Language — user prefers Simplified Chinese"
        assert symbols["index_text"] == "Symbols — tracked RFC-9110"
        assert language["provenance"]["pointed"] is True
        assert language["tier"] == "user"
        assert language["status"] == "active"
        assert language["content_hash"] == projection.content_hash(
            "---\ndescription: pref\n---\nbody"
        )

    def test_unpointed_detail_falls_back(self):
        items = [
            ("memory.md", {"content": ""}),
            ("note.md", {"content": "---\ndescription: remembered fact\n---\n"}),
        ]
        rows, _skipped = backfill.build_namespace_rows(
            user_id="u1", tier="workspace", namespace=("u1", "workspaces", "w", "memory"), items=items
        )
        assert rows[0]["index_text"] == "remembered fact"
        assert rows[0]["provenance"]["pointed"] is False

    def test_basename_pointer_match(self):
        items = [
            ("memory.md", {"content": "- [Nested](language.md) — nested hook\n"}),
            ("sub/language.md", {"content": "body"}),
        ]
        rows, _skipped = backfill.build_namespace_rows(
            user_id="u1", tier="user", namespace=("u1", "memory"), items=items
        )
        assert rows[0]["entry_key"] == "sub/language.md"
        assert rows[0]["index_text"] == "Nested — nested hook"

    def test_unprojectable_detail_is_marked_for_deactivation(self):
        items = [
            ("memory.md", {"content": INDEX}),
            ("gone.md", {"content": "---\nsensitivity: secret\nunterminated"}),
        ]
        rows, skipped = backfill.build_namespace_rows(
            user_id="u1", tier="user", namespace=("u1", "memory"), items=items
        )
        assert [row["entry_key"] for row in rows] == ["gone.md"]
        assert rows[0]["deactivate"] is True
        assert rows[0]["status"] == "superseded"
        assert rows[0]["index_text"] == ""
        assert skipped == [
            "gone.md: no pointer line or usable description; stale projection deactivated"
        ]

    def test_non_string_content_is_skipped(self):
        items = [
            ("memory.md", {"content": INDEX}),
            ("binary.md", {"content": {"x": 1}}),
            ("ok.md", {"content": "body"}),
        ]
        rows, skipped = backfill.build_namespace_rows(
            user_id="u1", tier="user", namespace=("u1", "memory"), items=items
        )
        assert rows
        assert skipped == ["binary.md: non-string content"]


@pytest.mark.asyncio
async def test_apply_deactivates_stale_rows_for_unprojectable_entries(monkeypatch):
    supersede = AsyncMock(return_value=1)
    upsert = AsyncMock()
    monkeypatch.setattr(backfill.db_memory, "supersede_entry", supersede)
    monkeypatch.setattr(backfill.db_memory, "upsert_entry", upsert)
    monkeypatch.setattr(
        backfill.db_memory, "list_entries", AsyncMock(return_value=[])
    )
    store = FakeStore(
        [
            _item(("u1", "memory"), "gone.md", "---\nsensitivity: secret\nunterminated"),
        ]
    )

    report = await backfill.backfill_namespace(
        store, user_id="u1", tier="user", namespace=("u1", "memory"), apply=True
    )

    supersede.assert_awaited_once_with(user_id="u1", tier="user", entry_key="gone.md")
    upsert.assert_not_awaited()
    assert report["applied"] == 1


class TestBackfillNamespace:
    async def test_dry_run_never_writes(self, monkeypatch):
        upsert = AsyncMock()
        monkeypatch.setattr(backfill.db_memory, "upsert_entry", upsert)
        monkeypatch.setattr(
            backfill.db_memory, "list_entries", AsyncMock(return_value=[])
        )
        store = FakeStore(
            [
                _item(("u1", "memory"), "memory.md", INDEX),
                _item(("u1", "memory"), "prefs/language.md", "body"),
                _item(("u1", "memory"), "fact/symbols.md", "body"),
            ]
        )
        report = await backfill.backfill_namespace(
            store, user_id="u1", tier="user", namespace=("u1", "memory")
        )
        assert report["dry_run"] is True
        assert report["store_items"] == 3
        assert report["rows"] == 2
        assert report["applied"] == 0
        upsert.assert_not_awaited()
        assert report["parity"]["missing"] == ["user:fact/symbols.md", "user:prefs/language.md"]

    async def test_apply_writes_each_row_and_reports(self, monkeypatch):
        written = []

        async def fake_upsert(**kwargs):
            written.append(kwargs)
            return {"entry_id": "e"}

        monkeypatch.setattr(backfill.db_memory, "upsert_entry", fake_upsert)
        projected = [
            {
                "tier": "user",
                "entry_key": "prefs/language.md",
                "content_hash": projection.content_hash("body"),
                "index_text": "Language — user prefers Simplified Chinese",
            }
        ]
        monkeypatch.setattr(
            backfill.db_memory, "list_entries", AsyncMock(return_value=projected)
        )
        store = FakeStore(
            [
                _item(("u1", "memory"), "memory.md", INDEX),
                _item(("u1", "memory"), "prefs/language.md", "body"),
                _item(("u1", "memory"), "fact/symbols.md", "body"),
            ]
        )
        report = await backfill.backfill_namespace(
            store, user_id="u1", tier="user", namespace=("u1", "memory"), apply=True
        )
        assert report["applied"] == 2
        assert {call["entry_key"] for call in written} == {
            "prefs/language.md",
            "fact/symbols.md",
        }
        assert written[0]["status"] == "active"
        assert report["parity"]["missing"] == ["user:fact/symbols.md"]

    async def test_apply_replays_all_multi_source_lineage_edges(self, monkeypatch):
        source_ids = [
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        ]
        upsert = AsyncMock(return_value={"entry_id": "33333333-3333-4333-8333-333333333333"})
        lineage = AsyncMock(return_value=2)
        monkeypatch.setattr(backfill.db_memory, "upsert_entry", upsert)
        monkeypatch.setattr(backfill.db_memory, "record_entry_lineage", lineage)
        monkeypatch.setattr(backfill.db_memory, "list_entries", AsyncMock(return_value=[]))
        content = (
            "---\ndescription: merged fact\nsupersedes_entry_ids:\n"
            f"  - {source_ids[0]}\n  - {source_ids[1]}\n---\nMerged body"
        )
        store = FakeStore(
            [
                _item(("u1", "memory"), "memory.md", "- [Merged](merged.md) — merged\n"),
                _item(("u1", "memory"), "merged.md", content),
            ]
        )

        report = await backfill.backfill_namespace(
            store,
            user_id="u1",
            tier="user",
            namespace=("u1", "memory"),
            apply=True,
        )

        assert report["applied"] == 1
        # Row and lineage commit atomically through upsert_entry; there is no
        # separate lineage write that could fail after the row was committed.
        lineage.assert_not_awaited()
        assert upsert.await_args.kwargs["source_entry_ids"] == source_ids

    async def test_one_failed_write_does_not_stop_the_namespace(self, monkeypatch):
        calls = []

        async def flaky_upsert(**kwargs):
            calls.append(kwargs["entry_key"])
            if len(calls) == 1:
                raise RuntimeError("db blip")

        monkeypatch.setattr(backfill.db_memory, "upsert_entry", flaky_upsert)
        monkeypatch.setattr(
            backfill.db_memory, "list_entries", AsyncMock(return_value=[])
        )
        store = FakeStore(
            [
                _item(("u1", "memory"), "memory.md", INDEX),
                _item(("u1", "memory"), "prefs/language.md", "body"),
                _item(("u1", "memory"), "fact/symbols.md", "body"),
            ]
        )
        report = await backfill.backfill_namespace(
            store, user_id="u1", tier="user", namespace=("u1", "memory"), apply=True
        )
        assert report["applied"] == 1
        assert len(report["failures"]) == 1
        assert len(calls) == 2

    async def test_foreign_namespace_items_are_ignored(self, monkeypatch):
        monkeypatch.setattr(backfill.db_memory, "upsert_entry", AsyncMock())
        monkeypatch.setattr(
            backfill.db_memory, "list_entries", AsyncMock(return_value=[])
        )
        store = FakeStore(
            [
                _item(("u1", "memory"), "memory.md", INDEX),
                _item(("u1", "memory-v2"), "memory.md", INDEX),
            ]
        )
        report = await backfill.backfill_namespace(
            store, user_id="u1", tier="user", namespace=("u1", "memory")
        )
        assert report["store_items"] == 1


class TestBackfillOntology:
    """The backfill writes the ontology of the memory it projects.

    The projection upsert overwrites the class and the concept, so a backfill
    that left them out would wipe the classification of every annotated memory
    it re-projected -- including one that an earlier backfill had classified.
    """

    ANNOTATED = (
        "---\n"
        "class: Preference\n"
        "about:\n"
        "  - project:conductor\n"
        "  - person:leo\n"
        "---\n"
        "the user prefers Simplified Chinese\n"
    )

    async def _run(self, monkeypatch, content: str) -> list[dict]:
        written: list[dict] = []

        async def fake_upsert(**kwargs):
            written.append(kwargs)
            return {"entry_id": "e"}

        monkeypatch.setattr(backfill.db_memory, "upsert_entry", fake_upsert)
        monkeypatch.setattr(
            backfill.db_memory, "list_entries", AsyncMock(return_value=[])
        )
        store = FakeStore(
            [
                _item(("u1", "memory"), "memory.md", INDEX),
                _item(("u1", "memory"), "prefs/language.md", content),
            ]
        )
        await backfill.backfill_namespace(
            store, user_id="u1", tier="user", namespace=("u1", "memory"), apply=True
        )
        return written

    @staticmethod
    def _detail(written: list[dict]) -> dict:
        detail = [call for call in written if call["entry_key"] == "prefs/language.md"]
        assert detail, written
        return detail[0]

    async def test_a_fresh_projection_carries_the_ontology(self, monkeypatch):
        written = await self._run(monkeypatch, self.ANNOTATED)

        detail = self._detail(written)
        assert detail["memory_class"] == "Preference"
        assert detail["about"] == "project:conductor"
        # The full list of concepts stays in the metadata, which is what the
        # browse page and the audit read.
        assert detail["metadata"]["about"] == ["project:conductor", "person:leo"]

    async def test_rebuilding_a_classified_row_keeps_its_classification(self, monkeypatch):
        first = await self._run(monkeypatch, self.ANNOTATED)
        second = await self._run(monkeypatch, self.ANNOTATED)

        for run in (first, second):
            detail = self._detail(run)
            # Never NULL: a NULL here would wipe the classification the
            # previous backfill wrote.
            assert detail["memory_class"] == "Preference"
            assert detail["about"] == "project:conductor"

    async def test_a_memory_without_an_ontology_writes_none(self, monkeypatch):
        written = await self._run(monkeypatch, "body")

        detail = self._detail(written)
        assert detail["memory_class"] is None
        assert detail["about"] is None


class TestSummary:
    def test_summary_totals(self):
        summary = backfill.render_summary(
            [
                {"user_id": "u1", "tier": "user", "namespace": "u1.memory", "store_items": 3,
                 "rows": 2, "applied": 2, "failures": [],
                 "parity": {"missing": [], "extra": ["user:x"], "changed": []}},
            ]
        )
        assert "namespaces 1" in summary
        assert "rows 2" in summary
        assert "applied 2" in summary
