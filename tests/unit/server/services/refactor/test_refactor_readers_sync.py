"""Sync reader helpers: task-tree nesting and store/projection parity.

Kept apart from ``test_refactor_readers.py`` because that module is marked
``asyncio`` for the database-backed readers; these are pure functions and run in
the default (sync) collection.
"""

from __future__ import annotations

from src.server.services.refactor.readers import (
    build_task_tree,
    compare_memory_projection,
)


class TestTaskTreeShape:
    def test_orphan_and_cycle_become_roots(self):
        rows = [
            {"task_node_id": "a", "parent_node_id": "missing", "title": "orphan"},
            {"task_node_id": "b", "parent_node_id": "c", "title": "cycle-b"},
            {"task_node_id": "c", "parent_node_id": "b", "title": "cycle-c"},
        ]
        tree = build_task_tree(rows)
        titles = sorted(node["title"] for node in tree)
        assert titles == ["cycle-b", "orphan"]
        # Promoting one edge of the cycle keeps its subtree intact.
        cycle = next(node for node in tree if node["title"] == "cycle-b")
        assert cycle["parent_node_id"] is None
        assert [child["title"] for child in cycle["children"]] == ["cycle-c"]
        assert cycle["children"][0]["children"] == []

    def test_rows_without_id_are_skipped(self):
        assert build_task_tree([{"title": "no id"}]) == []

    def test_children_preserve_input_order(self):
        rows = [
            {"task_node_id": "root", "parent_node_id": None, "title": "root"},
            {"task_node_id": "c1", "parent_node_id": "root", "title": "one"},
            {"task_node_id": "c2", "parent_node_id": "root", "title": "two"},
        ]
        tree = build_task_tree(rows)
        assert [child["title"] for child in tree[0]["children"]] == ["one", "two"]


class TestMemoryParity:
    @staticmethod
    def _entries():
        return [
            {
                "tier": "user",
                "entry_key": "prefs/lang",
                "content_hash": "h1",
                "index_text": "lang",
            },
            {
                "tier": "workspace",
                "entry_key": "fact/x",
                "content_hash": "h2",
                "index_text": "x",
            },
        ]

    def test_identical_projection_is_ok(self):
        report = compare_memory_projection(
            store_entries=self._entries(), projected_entries=self._entries()
        )
        assert report.ok
        assert report.to_dict() == {
            "ok": True,
            "missing": [],
            "extra": [],
            "changed": [],
        }

    def test_missing_extra_and_changed_are_reported(self):
        projected = [
            self._entries()[0],
            {
                "tier": "user",
                "entry_key": "extra",
                "content_hash": "hx",
                "index_text": "",
            },
        ]
        changed_projected = [dict(projected[0], content_hash="different"), projected[1]]
        report = compare_memory_projection(
            store_entries=self._entries(), projected_entries=changed_projected
        )
        assert not report.ok
        assert report.missing == (("workspace", "fact/x"),)
        assert report.extra == (("user", "extra"),)
        assert report.changed == (("user", "prefs/lang"),)

    def test_index_text_change_counts_as_changed(self):
        projected = [dict(self._entries()[0], index_text="new line"), self._entries()[1]]
        report = compare_memory_projection(
            store_entries=self._entries(), projected_entries=projected
        )
        assert report.changed == (("user", "prefs/lang"),)
