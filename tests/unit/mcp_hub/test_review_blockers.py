"""Regressions for the MCP hub review blockers.

Each test pins one failure mode: a secret in an apply response, two env
identities collapsed on import, a stale render winning a race, a manual
entry claimed by ordinary apply, an env override that cannot be stored,
a check that never runs, and a rollback that writes without ``--apply``.
"""

from __future__ import annotations

import json
import os
import pwd
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from src.conductor_cli.cli import main
from src.mcp_hub import (
    Registry,
    Server,
    apply_all,
    import_existing,
    rollback,
    save_registry,
)
from src.mcp_hub.check import probe_local_server
from src.mcp_hub.editing import toml_value
from src.mcp_hub.store import load_managed, load_registry

_FAKE_MCP = """\
import json
import sys


def read():
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


def write(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


msg = read()
write(
    {
        "jsonrpc": "2.0",
        "id": msg["id"],
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0"},
        },
    }
)
note = read()
if note.get("method") != "notifications/initialized":
    raise SystemExit(2)
msg = read()
write(
    {
        "jsonrpc": "2.0",
        "id": msg["id"],
        "result": {
            "tools": [
                {"name": "alpha", "inputSchema": {"type": "object"}},
                {"name": "beta", "inputSchema": {"type": "object"}},
            ]
        },
    }
)
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.delenv("CONDUCTOR_URL", raising=False)
    return root


def _run(argv: list[str]) -> tuple[int, str, str]:
    import contextlib
    import io

    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def _loads(text: str) -> dict:
    return json.loads(text)


def test_apply_response_does_not_leak_replaced_secret(home: Path) -> None:
    secret = "synthetic-old-secret-value"
    code, out, err = _run(
        [
            "mcp",
            "add",
            "--name",
            "search",
            "--command",
            "echo",
            "--env",
            "TOKEN",
            "--target",
            "cursor",
            "--apply",
            "--json",
        ]
    )
    assert code == 0 and err == ""
    code, out, err = _run(["mcp", "apply", "--apply", "--json"])
    assert code == 0 and err == ""
    path = home / ".cursor" / "mcp.json"
    planted = path.read_text(encoding="utf-8").replace("${TOKEN}", secret)
    assert secret in planted
    path.write_text(planted, encoding="utf-8")
    registry = load_registry(home)
    current = registry.servers["search"]
    registry.servers["search"] = Server(
        name="search",
        command="echo-new",
        args=current.args,
        env=dict(current.env),
        targets=current.targets,
    )
    save_registry(home, registry)

    code, out, err = _run(["mcp", "apply", "--apply", "--json"])

    assert code == 0 and err == ""
    assert secret not in out
    assert secret not in err
    assert secret not in path.read_text(encoding="utf-8")
    diff = _loads(out)["data"]["diffs"]["cursor"]["unified_diff"]
    assert diff
    assert secret not in diff
    assert "[redacted]" in diff


def test_apply_response_redacts_short_single_quoted_secret(home: Path) -> None:
    short = "s3cr3t"
    code, out, err = _run(
        [
            "mcp",
            "add",
            "--name",
            "search",
            "--command",
            "echo",
            "--env",
            "TOKEN",
            "--target",
            "codex",
            "--apply",
            "--json",
        ]
    )
    assert code == 0 and err == ""
    code, out, err = _run(["mcp", "apply", "--apply", "--json"])
    assert code == 0 and err == ""
    codex = home / ".codex" / "config.toml"
    codex.write_text(
        "\n".join(
            [
                "[mcp_servers.search]",
                'command = "echo"',
                "args = []",
                "",
                "[mcp_servers.search.env]",
                f"TOKEN = '{short}'",
                "",
            ]
        ),
        encoding="utf-8",
    )
    registry = load_registry(home)
    current = registry.servers["search"]
    registry.servers["search"] = Server(
        name="search",
        command="echo-new",
        args=current.args,
        env=dict(current.env),
        targets=current.targets,
    )
    save_registry(home, registry)

    code, out, err = _run(["mcp", "apply", "--apply", "--json"])

    assert code == 0 and err == ""
    assert short not in out
    assert short not in err
    assert short not in codex.read_text(encoding="utf-8")


def test_import_keeps_distinct_plaintext_instances(tmp_path: Path) -> None:
    """Same command and env key, different plaintext values, stay two servers."""
    import hashlib

    value_a = "SYNTHETIC_VALUE_A"
    value_b = "SYNTHETIC_VALUE_B"
    path = tmp_path / ".claude.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "prod": {
                        "command": "echo",
                        "args": ["pkg"],
                        "env": {"DB_URL": value_a},
                    },
                    "stage": {
                        "command": "echo",
                        "args": ["pkg"],
                        "env": {"DB_URL": value_b},
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    imported = import_existing(tmp_path)
    applied = apply_all(tmp_path, apply=True)

    assert applied["applied"] is True
    assert len(imported) == 2
    remaining = sorted(json.loads(path.read_text(encoding="utf-8"))["mcpServers"])
    assert remaining == ["prod", "stage"]
    registry = (tmp_path / ".conductor" / "mcp" / "servers.toml").read_text(
        encoding="utf-8"
    )
    rendered = path.read_text(encoding="utf-8")
    for blob in (registry, rendered):
        assert value_a not in blob
        assert value_b not in blob
        assert hashlib.sha256(value_a.encode("utf-8")).hexdigest() not in blob
        assert hashlib.sha256(value_b.encode("utf-8")).hexdigest() not in blob
    refs = {
        name: body["env"]["DB_URL"]
        for name, body in json.loads(rendered)["mcpServers"].items()
    }
    assert refs["prod"].startswith("${") and refs["prod"].endswith("}")
    assert refs["stage"].startswith("${") and refs["stage"].endswith("}")
    assert refs["prod"] != refs["stage"]
    again = import_existing(tmp_path)
    assert len(again) == 2
    assert sorted(again) == ["prod", "stage"]


def test_second_target_conflict_rolls_back_the_first(
    tmp_path: Path,
) -> None:
    """A later conflict leaves the first publish in place and registered.

    Round 3 does not roll back automatically. Retry still publishes the second
    target. A later registry removal releases the published keys, because that
    ownership was recorded at publish.
    """
    server = Server(
        name="hubbed", command="echo", args=["new"], targets=("claude", "cursor")
    )
    save_registry(tmp_path, Registry(servers={"hubbed": server}))
    claude_path = tmp_path / ".claude.json"
    cursor_path = tmp_path / ".cursor" / "mcp.json"
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    import src.mcp_hub.apply as apply_mod

    real_atomic_write = apply_mod.atomic_write
    planted = False

    def conflict_cursor(path: Path, data: bytes, **kwargs: object) -> None:
        nonlocal planted
        if Path(path) == cursor_path and not planted:
            cursor_path.write_text(
                '{"mcpServers":{"manual":{"command":"echo"}}}\n',
                encoding="utf-8",
            )
            planted = True
        real_atomic_write(path, data, **kwargs)

    apply_mod.atomic_write = conflict_cursor
    try:
        result = apply_all(tmp_path, apply=True)
    finally:
        apply_mod.atomic_write = real_atomic_write

    assert result["applied"] is False
    assert result["conflict"] is True
    assert planted is True
    assert "claude" in result["succeeded"]
    managed = load_managed(tmp_path)
    claude_text = claude_path.read_text(encoding="utf-8")
    assert "hubbed" in claude_text
    assert "hubbed" in managed.get("claude", set())
    assert "manual" in cursor_path.read_text(encoding="utf-8")

    retry = apply_all(tmp_path, apply=True)

    assert retry["applied"] is True
    claude_servers = json.loads(claude_path.read_text(encoding="utf-8"))["mcpServers"]
    cursor_servers = json.loads(cursor_path.read_text(encoding="utf-8"))["mcpServers"]
    assert "hubbed" in claude_servers
    assert "hubbed" in cursor_servers
    assert "manual" in cursor_servers
    owned = load_managed(tmp_path)
    assert "hubbed" in owned["claude"]
    assert "hubbed" in owned["cursor"]

    save_registry(tmp_path, Registry())
    removed = apply_all(tmp_path, apply=True)

    assert removed["applied"] is True
    claude_servers = json.loads(claude_path.read_text(encoding="utf-8"))["mcpServers"]
    assert "hubbed" not in claude_servers
    assert removed["diffs"]["claude"]["released"] == [
        {"key": "hubbed", "action": "delete"}
    ]
    cursor_servers = json.loads(cursor_path.read_text(encoding="utf-8"))["mcpServers"]
    assert "manual" in cursor_servers
    assert "hubbed" not in cursor_servers
    assert removed["diffs"]["cursor"]["released"] == [
        {"key": "hubbed", "action": "delete"}
    ]


def test_import_preserves_distinct_env_refs(tmp_path: Path) -> None:
    def write(relative: str, env: dict[str, str]) -> None:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "mcpServers": {
                "api": {"command": "uvx", "args": ["pkg"], "env": env},
            }
        }
        path.write_text(json.dumps(body), encoding="utf-8")

    write(".claude.json", {"TOKEN": "${PROD_TOKEN}"})
    write(".cursor/mcp.json", {"TOKEN": "${STAGE_TOKEN}"})
    write(".kimi-code/mcp.json", {"TOKEN": "keychain:svc/acct"})

    imported = import_existing(tmp_path)

    envs = [item["env"] for item in imported.values()]
    assert {"TOKEN": "${PROD_TOKEN}"} in envs
    assert {"TOKEN": "${STAGE_TOKEN}"} in envs
    assert {"TOKEN": "keychain:svc/acct"} in envs
    assert len(imported) == 3
    stored = (tmp_path / ".conductor" / "mcp" / "servers.toml").read_text(
        encoding="utf-8"
    )
    assert "${PROD_TOKEN}" in stored
    assert "${STAGE_TOKEN}" in stored
    assert "keychain:svc/acct" in stored
    assert "supersecret" not in stored


def test_apply_refuses_stale_render_over_concurrent_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".cursor" / "mcp.json"
    path.parent.mkdir(parents=True)
    original = '{\n  "mcpServers": {"hand": {"command": "echo", "args": ["stay"]}}\n}\n'
    path.write_text(original, encoding="utf-8")
    save_registry(
        tmp_path,
        Registry(
            servers={
                "hubbed": Server(
                    name="hubbed", command="uvx", args=["pkg"], targets=("cursor",)
                )
            }
        ),
    )
    import src.mcp_hub.apply as apply_mod

    real = apply_mod.render_agent

    def wrapped(
        home: Path, agent: str, registry: Registry | None = None, **kwargs: object
    ):
        view = real(home, agent, registry, **kwargs)
        if agent == "cursor":
            path.write_text(original.replace("stay", "concurrent"), encoding="utf-8")
        return view

    monkeypatch.setattr(apply_mod, "render_agent", wrapped)
    result = apply_all(tmp_path, apply=True)

    assert result["applied"] is False
    assert result["conflict"] is True
    text = path.read_text(encoding="utf-8")
    assert "concurrent" in text
    assert "hubbed" not in text
    assert "uvx" not in text


def test_apply_refuses_edit_in_the_replace_gap(tmp_path: Path) -> None:
    """A write after the render compare and before os.replace stays on disk."""
    path = tmp_path / ".cursor" / "mcp.json"
    path.parent.mkdir(parents=True)
    original = '{\n  "mcpServers": {"hand": {"command": "echo", "args": ["stay"]}}\n}\n'
    path.write_text(original, encoding="utf-8")
    save_registry(
        tmp_path,
        Registry(
            servers={
                "hubbed": Server(
                    name="hubbed", command="uvx", args=["pkg"], targets=("cursor",)
                )
            }
        ),
    )
    import src.mcp_hub.atomic as atomic_mod

    def plant_gap() -> None:
        path.write_text(original.replace("stay", "gap"), encoding="utf-8")

    atomic_mod.before_replace_hook = plant_gap
    try:
        result = apply_all(tmp_path, apply=True)
    finally:
        atomic_mod.before_replace_hook = None

    assert result["applied"] is False
    assert result["conflict"] is True
    text = path.read_text(encoding="utf-8")
    assert "gap" in text
    assert "hubbed" not in text
    assert "uvx" not in text


def test_ordinary_apply_does_not_claim_or_remove_manual_entry(tmp_path: Path) -> None:
    path = tmp_path / ".cursor" / "mcp.json"
    path.parent.mkdir(parents=True)
    manual = json.dumps(
        {"mcpServers": {"hand": {"command": "echo", "args": ["stay"]}}},
        indent=2,
    )
    path.write_text(manual + "\n", encoding="utf-8")
    before = path.read_bytes()
    save_registry(
        tmp_path,
        Registry(
            servers={
                "hand": Server(
                    name="hand",
                    command="echo",
                    args=["stay"],
                    targets=("cursor",),
                )
            }
        ),
    )

    apply_all(tmp_path, apply=True)

    assert path.read_bytes() == before
    assert "hand" not in load_managed(tmp_path)["cursor"]
    save_registry(tmp_path, Registry())
    apply_all(tmp_path, apply=True)
    assert path.read_bytes() == before


def test_env_override_dict_round_trips(tmp_path: Path) -> None:
    env = {"TOKEN": "${PROD_TOKEN}", "OTHER": "keychain:svc/acct"}
    encoded = toml_value({"TOKEN": "${PROD_TOKEN}"})
    parsed = tomllib.loads("env = " + encoded)
    assert parsed["env"] == {"TOKEN": "${PROD_TOKEN}"}
    server = Server(
        name="hubbed",
        command="echo",
        overrides={"claude": {"command": "echo-claude", "env": dict(env)}},
    )
    save_registry(tmp_path, Registry(servers={"hubbed": server}))
    loaded = load_registry(tmp_path)
    assert loaded.servers["hubbed"].overrides["claude"]["env"] == env
    assert loaded.servers["hubbed"].overrides["claude"]["command"] == "echo-claude"


def test_check_fake_server_returns_tool_count(home: Path) -> None:
    script = home / "fake_mcp.py"
    script.write_text(_FAKE_MCP, encoding="utf-8")
    code, out, err = _run(
        [
            "mcp",
            "add",
            "--name",
            "fake",
            "--command",
            sys.executable,
            "--arg",
            str(script),
            "--target",
            "cursor",
            "--apply",
            "--json",
        ]
    )
    assert code == 0 and err == ""

    code, out, err = _run(["mcp", "check", "fake", "--json"])

    assert code == 0 and err == ""
    data = _loads(out)["data"]
    assert data["status"] == "ok"
    assert data["status"] != "not-run"
    assert data["tool_count"] == 2
    assert isinstance(data["elapsed_ms"], int)
    assert "env" not in data


def test_check_missing_server_fails_without_env_values(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "synthetic-check-secret-value"
    monkeypatch.setenv("TOKEN", secret)
    missing = home / "missing-mcp-bin"
    code, _out, err = _run(
        [
            "mcp",
            "add",
            "--name",
            "gone",
            "--command",
            str(missing),
            "--env",
            "TOKEN",
            "--target",
            "cursor",
            "--apply",
            "--json",
        ]
    )
    assert code == 0 and err == ""

    code, out, err = _run(["mcp", "check", "gone", "--json"])

    assert code == 0 and err == ""
    data = _loads(out)["data"]
    assert data["status"] == "error"
    assert data["status"] != "not-run"
    assert data["tool_count"] is None
    blob = out + err
    assert secret not in blob
    assert "TOKEN" not in data.get("reason", "")


def test_check_does_not_open_public_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("opened a socket")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    server = Server(name="remote", transport="http", url="https://example.com/mcp")

    result = probe_local_server(server)

    assert result["status"] == "error"
    assert result["tool_count"] is None
    # A public host is attempted. It is not banned for failing to be loopback.
    assert result.get("reason") != "refusing to contact a remote server"
    assert "example.com" not in json.dumps(result)


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
def test_check_http_loopback_lists_one_tool() -> None:
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            msg = json.loads(self.rfile.read(length))
            method = msg.get("method")
            if method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
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
            elif method == "tools/list":
                body = {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {"tools": [{"name": "one"}]},
                }
            else:
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            raw = json.dumps(body).encode("utf-8")
            content_type = "application/json"
            if self.path.startswith("/sse"):
                raw = b"data: " + raw + b"\n\n"
                content_type = "text/event-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd, base = _serve(Handler)
    try:
        result = probe_local_server(
            Server(name="local", transport="http", url=f"{base}/mcp"),
            timeout=2,
        )
        sse = probe_local_server(
            Server(name="stream", transport="sse", url=f"{base}/sse"),
            timeout=2,
        )
    finally:
        _stop(httpd)

    assert result["status"] == "ok"
    assert result["tool_count"] == 1
    assert sse["status"] == "ok"
    assert sse["tool_count"] == 1
    assert "127.0.0.1" not in json.dumps(result)


@pytest.mark.enable_socket
def test_check_refuses_cross_host_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    from http.server import BaseHTTPRequestHandler

    hits = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            hits["count"] += 1
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(302)
            self.send_header("Location", "http://example.com/mcp")
            self.send_header("Content-Length", "0")
            self.end_headers()

    real_connect = socket.create_connection

    def guarded(address: object, *args: object, **kwargs: object) -> object:
        host = address[0] if isinstance(address, tuple) else address
        if host not in {"127.0.0.1", "localhost"}:
            raise AssertionError("left loopback")
        return real_connect(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", guarded)
    httpd, base = _serve(Handler)
    try:
        result = probe_local_server(
            Server(name="local", transport="http", url=f"{base}/mcp"),
            timeout=2,
        )
    finally:
        _stop(httpd)

    assert result["status"] == "error"
    assert result.get("reason") == "refusing cross-host redirect"
    assert hits["count"] == 1
    assert "example.com" not in json.dumps(result)


@pytest.mark.enable_socket
def test_check_follows_same_host_redirect() -> None:
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            msg = json.loads(self.rfile.read(length) or b"{}")
            path = self.path.split("?", 1)[0]
            if path == "/old":
                port = self.server.server_address[1]
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{port}/mcp")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            method = msg.get("method")
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
            else:
                body = {
                    "jsonrpc": "2.0",
                    "id": msg.get("id", 1),
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "0"},
                    },
                }
            raw = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd, base = _serve(Handler)
    try:
        result = probe_local_server(
            Server(name="local", transport="http", url=f"{base}/old"),
            timeout=2,
        )
    finally:
        _stop(httpd)

    assert result["status"] == "ok"
    assert result["tool_count"] == 1


def test_check_stdio_passes_only_declared_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "needs_env.py"
    script.write_text(
        """\
import json, os, sys
if os.environ.get("SYNTHETIC_MCP_TOKEN") != "synthetic-only":
    raise SystemExit(9)
if "UNRELATED_SECRET" in os.environ:
    raise SystemExit(8)

def read():
    return json.loads(sys.stdin.readline())

def write(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

msg = read()
write(
    {
        "jsonrpc": "2.0",
        "id": msg["id"],
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0"},
        },
    }
)
read()
msg = read()
write(
    {
        "jsonrpc": "2.0",
        "id": msg["id"],
        "result": {"tools": [{"name": "one"}]},
    }
)
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SYNTHETIC_MCP_TOKEN", "synthetic-only")
    monkeypatch.setenv("UNRELATED_SECRET", "synthetic-unrelated")
    server = Server(
        name="needsenv",
        command=sys.executable,
        args=[str(script)],
        env={"SYNTHETIC_MCP_TOKEN": "${SYNTHETIC_MCP_TOKEN}"},
    )

    result = probe_local_server(server, timeout=2)

    assert result["status"] == "ok"
    assert result["tool_count"] == 1
    blob = json.dumps(result)
    assert "synthetic-only" not in blob
    assert "synthetic-unrelated" not in blob
    assert "UNRELATED_SECRET" not in blob


def test_rollback_without_apply_does_not_write(tmp_path: Path) -> None:
    path = tmp_path / ".cursor" / "mcp.json"
    save_registry(
        tmp_path,
        Registry(
            servers={
                "hubbed": Server(name="hubbed", command="uvx", targets=("cursor",))
            }
        ),
    )
    applied = apply_all(tmp_path, apply=True)
    snapshot = applied["snapshot_id"]
    assert path.is_file()
    current = path.read_bytes()

    preview = rollback(tmp_path, snapshot)

    assert preview["dry_run"] is True
    assert path.read_bytes() == current
    restored = rollback(tmp_path, snapshot, apply=True)
    assert restored["dry_run"] is False
    assert not path.exists()


def test_wheel_install_imports_mcp_hub(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[3]
    dist = tmp_path / "dist"
    venv = tmp_path / "venv"
    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        env.pop(key, None)
    cache = env.get("UV_CACHE_DIR")
    if not cache or not Path(cache).is_dir():
        # An isolated HOME hides the login cache. Resolve it from the uid
        # record so the offline build can see hatchling without a fixed path.
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        for candidate in (
            real_home / ".cache" / "uv",
            real_home / "Library" / "Caches" / "uv",
        ):
            if candidate.is_dir():
                cache = str(candidate)
                break
    if not cache:
        pytest.skip("offline uv cache is not available")
    env["UV_CACHE_DIR"] = cache
    subprocess.run(
        ["uv", "build", "--wheel", "--offline", "--out-dir", str(dist)],
        cwd=repo,
        env=env,
        check=True,
        timeout=180,
    )
    wheel = next(dist.glob("*.whl"))
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=60)
    py = venv / "bin" / "python"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(py),
            "--offline",
            "--no-deps",
            str(wheel),
        ],
        env=env,
        check=True,
        timeout=120,
    )
    site = (
        Path(sys.prefix)
        / "lib"
        / (f"python{sys.version_info.major}.{sys.version_info.minor}")
        / "site-packages"
    )
    script = """
import os, site, sys
site.addsitedir(sys.argv[1])
import mcp_hub
import conductor_cli.mcp_cli
root = os.path.realpath(sys.argv[2])
hub = os.path.realpath(mcp_hub.__file__)
cli = os.path.realpath(conductor_cli.mcp_cli.__file__)
assert hub.startswith(root), hub
assert cli.startswith(root), cli
print(hub)
"""
    probe = subprocess.run(
        [str(py), "-c", script, str(site), str(venv)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert probe.returncode == 0, probe.stderr
    assert "mcp_hub" in probe.stdout
    assert str(repo / "src" / "mcp_hub") not in probe.stdout
