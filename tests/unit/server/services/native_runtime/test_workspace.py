"""Host-native workspace mirror: only safe regular files cross the boundary."""

from __future__ import annotations


from src.server.services.native_runtime.workspace import (
    _safe_rel_path,
    collect_changed_files,
)


def test_unsafe_manifest_paths_are_refused():
    assert _safe_rel_path("data/report.md") == "data/report.md"
    assert _safe_rel_path("../etc/passwd") is None
    assert _safe_rel_path("/abs/path") is None
    assert _safe_rel_path("") is None


def test_walk_collects_new_and_changed_files_and_prunes_deleted(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "changed.md").write_text("new")
    (tmp_path / "data" / "new.md").write_text("fresh")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "huge.js").write_text("skip")
    (tmp_path / ".mimocode").mkdir()
    (tmp_path / ".mimocode" / ".cron-lock").write_text("skip")
    (tmp_path / ".mcp.json").write_text("{}")
    previous = {
        "data/changed.md": {"content_hash": "old"},
        "data/deleted.md": {"content_hash": "gone"},
        "node_modules/huge.js": {"content_hash": "never"},
    }
    upserts, removed = collect_changed_files(tmp_path, previous)
    by_path = {row["file_path"]: row for row in upserts}
    assert set(by_path) == {"data/changed.md", "data/new.md"}
    assert by_path["data/new.md"]["file_name"] == "new.md"
    assert by_path["data/new.md"]["is_binary"] is False
    assert removed == ["data/deleted.md"]


def test_binary_file_keeps_bytes_not_decoded_text(tmp_path):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\x00\xff\x01")
    upserts, _ = collect_changed_files(tmp_path, {})
    row = next(r for r in upserts if r["file_path"] == "blob.bin")
    assert row["is_binary"] is True
    assert row["content_binary"] == b"\x00\xff\x01"
    assert row["content_text"] is None
