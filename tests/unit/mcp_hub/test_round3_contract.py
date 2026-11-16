"""Regressions for skills-mcp fix round 3 (apply transaction and HTTP session).

These tests describe the planner contract. On 8c4332339 apply rolls a later
conflict back, the ownership sidecar has no post-hash, rollback overwrites a
file the user edited after publish, and the HTTP probe drops Mcp-Session-Id.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.mcp_hub import Registry, Server, apply_all, rollback, save_registry
from src.mcp_hub.check import probe_local_server
from src.mcp_hub.errors import PublishConflict


def _hub(tmp_path: Path) -> None:
    save_registry(
        tmp_path,
        Registry(
            servers={
                "hubbed": Server(
                    name="hubbed",
                    command="echo",
                    args=["new"],
                    targets=("claude", "cursor"),
                )
            }
        ),
    )


def test_snapshot_records_pre_hash_before_publish(tmp_path: Path) -> None:
    """The snapshot taken before the write records the pre-image hash."""
    path = tmp_path / ".claude.json"
    original = b'{"mcpServers": {}}\n'
    path.write_bytes(original)
    save_registry(
        tmp_path,
        Registry(
            servers={
                "hubbed": Server(
                    name="hubbed", command="echo", args=["new"], targets=("claude",)
                )
            }
        ),
    )

    result = apply_all(tmp_path, apply=True)

    assert result["applied"] is True
    snapshot_id = result["snapshot_id"]
    manifest = json.loads(
        (
            tmp_path
            / ".conductor"
            / "mcp"
            / "snapshots"
            / snapshot_id
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    entry = manifest["files"][".claude.json"]
    assert entry["pre_hash"] == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() != original


def test_post_hash_is_persisted_before_the_next_target(tmp_path: Path) -> None:
    """The ownership sidecar stores a target's post-hash before the next write."""
    _hub(tmp_path)
    claude = tmp_path / ".claude.json"
    cursor = tmp_path / ".cursor" / "mcp.json"
    import src.mcp_hub.apply as apply_mod

    real = apply_mod.atomic_write
    seen: dict[str, object] = {}

    def wrapped(path: Path, data: bytes, **kwargs: object) -> None:
        if Path(path) == cursor and "sidecar" not in seen:
            sidecar = tmp_path / ".conductor" / "mcp" / "managed.toml"
            seen["sidecar_exists"] = sidecar.is_file()
            seen["sidecar"] = (
                sidecar.read_text(encoding="utf-8") if sidecar.is_file() else ""
            )
            seen["claude"] = claude.read_bytes() if claude.is_file() else b""
        real(path, data, **kwargs)

    apply_mod.atomic_write = wrapped
    try:
        result = apply_all(tmp_path, apply=True)
    finally:
        apply_mod.atomic_write = real

    assert result["applied"] is True
    assert seen["sidecar_exists"] is True
    claude_bytes = seen["claude"]
    assert isinstance(claude_bytes, bytes) and b"hubbed" in claude_bytes
    sidecar = seen["sidecar"]
    assert isinstance(sidecar, str)
    assert hashlib.sha256(claude_bytes).hexdigest() in sidecar
    assert "[published]" in sidecar


def test_oserror_on_second_target_keeps_the_first_and_names_rollback(
    tmp_path: Path,
) -> None:
    """An OSError stops the batch. The published target stays, with a rollback command."""
    _hub(tmp_path)
    cursor = tmp_path / ".cursor" / "mcp.json"
    claude = tmp_path / ".claude.json"
    import src.mcp_hub.apply as apply_mod

    real = apply_mod.atomic_write

    def boom(path: Path, data: bytes, **kwargs: object) -> None:
        if Path(path) == cursor:
            raise OSError("cursor target failed")
        real(path, data, **kwargs)

    apply_mod.atomic_write = boom
    try:
        result = apply_all(tmp_path, apply=True)
    finally:
        apply_mod.atomic_write = real

    assert result["applied"] is False
    assert "claude" in result["succeeded"]
    failed = result["failed"]
    assert any(item["agent"] == "cursor" for item in failed)
    blob = json.dumps(result)
    assert "cursor target failed" in blob
    snapshot_id = result["snapshot_id"]
    assert result["rollback_command"] == f"conductor mcp rollback {snapshot_id} --apply"
    assert "hubbed" in claude.read_text(encoding="utf-8")
    assert (
        "hubbed" not in cursor.read_text(encoding="utf-8") if cursor.is_file() else True
    )
    from src.mcp_hub.store import load_managed

    assert "hubbed" in load_managed(tmp_path)["claude"]


def test_conflict_on_second_target_does_not_roll_back_the_first(tmp_path: Path) -> None:
    """PublishConflict leaves earlier targets published and registered."""
    _hub(tmp_path)
    cursor = tmp_path / ".cursor" / "mcp.json"
    claude = tmp_path / ".claude.json"
    import src.mcp_hub.apply as apply_mod

    real = apply_mod.atomic_write
    planted = '{"mcpServers": {"manual": {"command": "echo"}}}\n'

    def conflict_cursor(path: Path, data: bytes, **kwargs: object) -> None:
        if Path(path) == cursor:
            cursor.parent.mkdir(parents=True, exist_ok=True)
            cursor.write_text(planted, encoding="utf-8")
            raise PublishConflict("config changed before publish")
        real(path, data, **kwargs)

    apply_mod.atomic_write = conflict_cursor
    try:
        result = apply_all(tmp_path, apply=True)
    finally:
        apply_mod.atomic_write = real

    assert result["applied"] is False
    assert result["conflict"] is True
    assert "claude" in result["succeeded"]
    assert any(item["agent"] == "cursor" for item in result["failed"])
    assert "PublishConflict" in json.dumps(result["failed"])
    assert result["rollback_command"] == (
        f"conductor mcp rollback {result['snapshot_id']} --apply"
    )
    assert "hubbed" in claude.read_text(encoding="utf-8")
    assert cursor.read_text(encoding="utf-8") == planted
    from src.mcp_hub.store import load_managed

    assert "hubbed" in load_managed(tmp_path)["claude"]


def test_rollback_leaves_a_user_modified_file_unchanged(tmp_path: Path) -> None:
    """Manual rollback restores only a target whose hash still equals the post-hash."""
    _hub(tmp_path)
    applied = apply_all(tmp_path, apply=True)
    assert applied["applied"] is True
    claude = tmp_path / ".claude.json"
    cursor = tmp_path / ".cursor" / "mcp.json"
    published = claude.read_bytes()
    edited = published + b"\n# user edit\n"
    claude.write_bytes(edited)
    cursor_published = cursor.read_bytes()

    restored = rollback(tmp_path, applied["snapshot_id"], apply=True)

    assert "modified_since_apply" in json.dumps(restored)
    assert claude.read_bytes() == edited
    assert not cursor.exists() or cursor.read_bytes() != cursor_published


def _serve(handler: type) -> tuple[object, str]:
    import threading
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    return httpd, f"http://127.0.0.1:{port}"


def _stop(httpd: object) -> None:
    httpd.shutdown()  # type: ignore[attr-defined]
    httpd.server_close()  # type: ignore[attr-defined]


@pytest.mark.enable_socket
def test_http_probe_sends_mcp_session_id_on_later_requests() -> None:
    """A loopback server that requires Mcp-Session-Id after initialize is probed."""
    from http.server import BaseHTTPRequestHandler

    issued = "sess-round3"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            msg = json.loads(self.rfile.read(length) or b"{}")
            method = msg.get("method")
            if method == "initialize":
                body = {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "0"},
                    },
                }
                raw = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", issued)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if self.headers.get("Mcp-Session-Id") != issued:
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "tools/list":
                body = {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {"tools": [{"name": "one"}]},
                }
                raw = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()

    httpd, base = _serve(Handler)
    try:
        result = probe_local_server(
            Server(name="local", transport="http", url=f"{base}/mcp"),
            timeout=2,
        )
    finally:
        _stop(httpd)

    assert result["status"] == "ok"
    assert result["tool_count"] == 1
