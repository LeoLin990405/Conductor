"""Fresh-install ``cond up`` regressions: dispatch default, cleanup, console backend.

Fakes only for cond; the static console test runs real loopback servers on
ephemeral ports (never 8000 or 5170).
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.conductor_cli import ops_runtime, ops_static
from src.conductor_cli.ops_runtime import (
    Component,
    pid_path,
    prepare,
    start_all,
    web_argv,
)
from src.conductor_cli.ops_support import OpsError
from src.server.app.ready import probe_dispatch_config
from tests.unit.test_cond_ops import FakeSystem, _layout


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if any(
            mark in name.upper()
            for mark in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
        ):
            monkeypatch.delenv(name, raising=False)
    for name in ("CONDUCTOR_DISPATCH_REPO", "VITE_PROXY_BACKEND", "CONDUCTOR_WEB_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")


# -- 1. dispatch_config defaults to the backend's cwd -------------------------


def test_unset_dispatch_repo_defaults_to_git_cwd(tmp_path, monkeypatch) -> None:
    repo = tmp_path.resolve() / "checkout"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    result = probe_dispatch_config()
    assert result["ok"] is True, result
    assert result["detail"] == "default_cwd"


def test_unset_dispatch_repo_in_a_worktree_checkout(tmp_path, monkeypatch) -> None:
    repo = tmp_path.resolve() / "wt"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    assert probe_dispatch_config()["ok"] is True


def test_unset_dispatch_repo_outside_git_is_still_not_ready(
    tmp_path, monkeypatch
) -> None:
    plain = tmp_path.resolve() / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert probe_dispatch_config() == {"ok": False, "detail": "not_a_git_repo"}


def test_explicit_dispatch_repo_is_still_checked(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CONDUCTOR_DISPATCH_REPO", str(tmp_path.resolve() / "gone"))
    assert probe_dispatch_config() == {"ok": False, "detail": "missing"}


# -- 1b. a failed backend readiness stops the backend cond up started ---------


class BackendNotReady(FakeSystem):
    def http_get(self, url: str, timeout: float) -> tuple[int, str]:
        if url.endswith("/ready?scope=backend"):
            self.urls.append(url)
            return 503, json.dumps(
                {
                    "status": "not_ready",
                    "checks": {"dispatch_config": {"ok": False, "detail": "not_configured"}},
                }
            )
        return super().http_get(url, timeout)


def _backend_pids(fake: FakeSystem) -> list[int]:
    return [pid for pid in fake.procs if "server.py" in fake.command(pid)]


def test_failed_backend_readiness_stops_the_backend_it_started(
    tmp_path, monkeypatch
) -> None:
    _layout(tmp_path.resolve(), monkeypatch)
    fake = BackendNotReady()
    system, settings, fleet = prepare(fake)
    with pytest.raises(OpsError) as caught:
        start_all(system, settings, fleet)
    message = str(caught.value)
    assert "stopped the backend this cond up started" in message
    started = _backend_pids(fake)
    assert started and not any(fake.alive(pid) for pid in started)
    assert not pid_path(settings, Component("backend")).exists()
    # The web console and daemons never started.
    assert not any("vite" in fake.command(pid) for pid in fake.procs)


def test_failed_readiness_leaves_a_preexisting_backend_and_says_so(
    tmp_path, monkeypatch
) -> None:
    _layout(tmp_path.resolve(), monkeypatch)
    fake = BackendNotReady()
    system, settings, fleet = prepare(fake)
    pid = fake.popen(
        ops_runtime.backend_argv(settings),
        cwd=settings.repo,
        env={"PATH": "/usr/bin"},
        log_path=settings.log_dir / "backend.log",
        max_bytes=1024,
        backups=1,
    )
    path = pid_path(settings, Component("backend"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")
    with pytest.raises(OpsError) as caught:
        start_all(system, settings, fleet)
    assert "already running before this cond up and was left running" in str(
        caught.value
    )
    assert fake.alive(pid)


# -- 2. the console reaches the backend cond started --------------------------


def test_preview_console_gets_the_backend_port(tmp_path, monkeypatch) -> None:
    _layout(tmp_path.resolve(), monkeypatch, port=54391, web=54392)
    fake = FakeSystem()
    system, settings, fleet = prepare(fake)
    start_all(system, settings, fleet)
    web_envs = [
        env
        for argv, env in zip(
            [fake.procs[pid]["argv"] for pid in sorted(fake.procs)],
            fake.popen_env,
            strict=True,
        )
        if "vite" in argv
    ]
    assert web_envs, "web console did not start"
    assert web_envs[0]["VITE_PROXY_BACKEND"] == "http://127.0.0.1:54391"
    assert web_envs[0]["CONDUCTOR_BACKEND_PORT"] == "54391"


def test_static_console_argv_names_the_backend(tmp_path, monkeypatch) -> None:
    _layout(tmp_path.resolve(), monkeypatch, port=54393)
    _system, settings, _fleet = prepare(FakeSystem())
    settings.web_mode = "static"
    argv, _cwd, _marker = web_argv(settings)
    assert argv[argv.index("--backend") + 1] == "http://127.0.0.1:54393"


def test_static_default_backend_follows_the_port_setting() -> None:
    assert ops_static.default_backend({"CONDUCTOR_BACKEND_PORT": "54394"}) == (
        "http://127.0.0.1:54394"
    )
    assert (
        ops_static.default_backend({"VITE_PROXY_BACKEND": "http://127.0.0.1:1"})
        == "http://127.0.0.1:1"
    )


def test_static_backend_must_be_loopback_http() -> None:
    with pytest.raises(SystemExit):
        ops_static.parse_backend("http://example.com:80")
    with pytest.raises(SystemExit):
        ops_static.parse_backend("https://127.0.0.1:9")
    assert ops_static.parse_backend("http://127.0.0.1:54395") == ("127.0.0.1", 54395)


class _Backend(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # silence
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.end_headers()
            self.wfile.flush()
            data = self.connection.recv(1024)
            self.connection.sendall(b"echo:" + data)
            self.close_connection = True
            return
        body = json.dumps({"path": self.path, "method": "GET"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        got = self.rfile.read(length).decode()
        body = json.dumps({"path": self.path, "method": "POST", "body": got}).encode()
        self.send_response(201)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def console(tmp_path):
    dist = tmp_path.resolve() / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>console</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("js", encoding="utf-8")
    (tmp_path.resolve() / "secret.txt").write_text("nope", encoding="utf-8")
    (dist / "escape.txt").symlink_to(tmp_path.resolve() / "secret.txt")
    backend = ThreadingHTTPServer(("127.0.0.1", 0), _Backend)
    front = ops_static.build_server(
        "127.0.0.1", 0, dist, ("127.0.0.1", backend.server_address[1])
    )
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (backend, front)
    ]
    for thread in threads:
        thread.start()
    try:
        yield front.server_address[1]
    finally:
        for server in (front, backend):
            server.shutdown()
            server.server_close()


def _get(port: int, path: str, method: str = "GET", body: str | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body)
        response = conn.getresponse()
        return response.status, response.read().decode()
    finally:
        conn.close()


@pytest.mark.enable_socket
def test_static_console_spa_fallback_serves_index_for_deep_links(console) -> None:
    assert _get(console, "/app/tickets/42") == (200, "<html>console</html>")
    assert _get(console, "/") == (200, "<html>console</html>")
    assert _get(console, "/assets/app.js") == (200, "js")
    assert _get(console, "/assets/missing.js")[0] == 404
    assert _get(console, "/assets/")[0] == 404


@pytest.mark.enable_socket
def test_static_console_proxies_the_api(console) -> None:
    status, body = _get(console, "/api/v1/tickets?limit=1")
    assert status == 200
    assert json.loads(body) == {"path": "/api/v1/tickets?limit=1", "method": "GET"}
    status, body = _get(console, "/api/v1/tickets", "POST", '{"a":1}')
    assert status == 201
    assert json.loads(body)["body"] == '{"a":1}'
    assert json.loads(_get(console, "/health")[1])["path"] == "/health"


@pytest.mark.enable_socket
def test_static_console_tunnels_websockets(console) -> None:
    with socket.create_connection(("127.0.0.1", console), timeout=5) as sock:
        sock.sendall(
            b"GET /ws/v1/stream HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
        )
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(1024)
            assert chunk
            head += chunk
        assert head.startswith(b"HTTP/1.") and b" 101 " in head.split(b"\r\n")[0]
        sock.sendall(b"ping")
        rest = head.split(b"\r\n\r\n", 1)[1]
        while b"echo:ping" not in rest:
            chunk = sock.recv(1024)
            assert chunk
            rest += chunk


def test_static_console_refuses_a_build_without_index(tmp_path) -> None:
    empty = tmp_path.resolve() / "dist"
    empty.mkdir()
    with pytest.raises(SystemExit, match="index.html"):
        ops_static.main(["--port", "0", "--directory", str(empty)])


@pytest.mark.enable_socket
def test_path_outside_build_is_404(console) -> None:
    assert _get(console, "/../secret.txt")[0] in {400, 404}
    assert _get(console, "/%2e%2e/secret.txt")[0] == 404
    assert "nope" not in _get(console, "/%2e%2e/secret.txt")[1]
    assert _get(console, "/escape.txt")[0] == 404

