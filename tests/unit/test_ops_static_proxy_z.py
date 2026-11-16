"""Static console proxy: streamed replies, Host check, request-body limits."""

from __future__ import annotations

import http.client
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.conductor_cli import ops_static

WAIT = 5.0


class _Probe(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # chunked, like uvicorn's StreamingResponse
    first_received: threading.Event
    second_sent: threading.Event

    def log_message(self, *_args) -> None:
        return

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/v1/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._chunk(b"event: one\ndata: 1\n\n")
            # Hold the second event until the client has the first one.
            self.server.first_received.wait(WAIT * 2)
            self.server.second_sent.set()
            self._chunk(b"event: two\ndata: 2\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.close_connection = True
            return
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        got = self.rfile.read(length)
        self.send_response(201)
        self.send_header("Content-Length", str(len(got)))
        self.end_headers()
        self.wfile.write(got)


@pytest.fixture
def front(tmp_path):
    dist = tmp_path.resolve() / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>console</html>", encoding="utf-8")
    backend = ThreadingHTTPServer(("127.0.0.1", 0), _Probe)
    backend.first_received = threading.Event()
    backend.second_sent = threading.Event()
    console = ops_static.build_server(
        "127.0.0.1", 0, dist, ("127.0.0.1", backend.server_address[1])
    )
    for server in (backend, console):
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
    try:
        yield console.server_address[1], backend
    finally:
        backend.first_received.set()
        for server in (console, backend):
            server.shutdown()
            server.server_close()


def _request(port: int, path: str, host: str | None = None, method: str = "GET"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT)
    try:
        headers = {"Host": host} if host is not None else {}
        conn.request(method, path, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _raw(port: int, head: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=WAIT) as sock:
        sock.sendall(head)
        data = b""
        while b"\r\n" not in data:
            chunk = sock.recv(1024)
            if not chunk:
                break
            data += chunk
        return data.split(b"\r\n", 1)[0]


@pytest.mark.enable_socket
def test_event_stream_reaches_the_client_before_it_ends(front) -> None:
    port, backend = front
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT)
    try:
        conn.request("GET", "/api/v1/stream")
        response = conn.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream"
        assert response.getheader("Content-Length") is None
        seen = b""
        while b"data: 1\n\n" not in seen:
            piece = response.read1(1024)
            assert piece, "stream ended before the first event"
            seen += piece
        assert not backend.second_sent.is_set()
        backend.first_received.set()
        rest = response.read()
        assert b"event: two\ndata: 2\n\n" in seen + rest
    finally:
        conn.close()


@pytest.mark.enable_socket
def test_content_length_replies_still_carry_a_length(front) -> None:
    port, _ = front
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT)
    try:
        conn.request("GET", "/api/v1/plain")
        response = conn.getresponse()
        assert response.getheader("Content-Length") == "11"
        assert response.read() == b'{"ok":true}'
    finally:
        conn.close()


@pytest.mark.enable_socket
@pytest.mark.parametrize(
    "host", ["127.0.0.1", "localhost", "[::1]", "LOCALHOST", "{loop}:{port}"]
)
def test_loopback_hosts_are_accepted(front, host) -> None:
    port, _ = front
    for loop in ("127.0.0.1", "localhost", "[::1]"):
        value = host.format(loop=loop, port=port)
        assert _request(port, "/api/v1/plain", value)[0] == 200
        assert _request(port, "/", value)[0] == 200


@pytest.mark.enable_socket
@pytest.mark.parametrize(
    "host",
    ["evil.example", "evil.example:{port}", "127.0.0.1:1", "localhost.evil", "::1", ""],
)
def test_other_hosts_get_403(front, host) -> None:
    port, _ = front
    value = host.format(port=port)
    assert _request(port, "/api/v1/plain", value)[0] == 403
    assert _request(port, "/api/v1/plain", value, "POST")[0] == 403
    assert _request(port, "/", value)[0] == 403
    head = (
        f"GET /ws/v1/stream HTTP/1.1\r\nHost: {value}\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\n\r\n"
    ).encode()
    assert b" 403 " in _raw(port, head)


@pytest.mark.enable_socket
@pytest.mark.parametrize("length", ["abc", "-1", "1e3", " 5x"])
def test_invalid_content_length_is_400(front, length) -> None:
    port, _ = front
    head = (
        f"POST /api/v1/x HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: {length}\r\n\r\n"
    ).encode()
    assert b" 400 " in _raw(port, head)


@pytest.mark.enable_socket
def test_body_over_the_cap_is_413(front) -> None:
    port, _ = front
    head = (
        "POST /api/v1/x HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"Content-Length: {ops_static.MAX_BODY_BYTES + 1}\r\n\r\n"
    ).encode()
    assert b" 413 " in _raw(port, head)


@pytest.mark.enable_socket
def test_chunked_request_body_is_411(front) -> None:
    port, _ = front
    head = (
        "POST /api/v1/x HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        "Transfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n"
    ).encode()
    assert b" 411 " in _raw(port, head)


@pytest.mark.enable_socket
def test_body_under_the_cap_is_forwarded_whole(front) -> None:
    port, _ = front
    body = b"x" * (3 * 65536 + 7)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT)
    try:
        conn.request("POST", "/api/v1/x", body=body)
        response = conn.getresponse()
        assert (response.status, response.read()) == (201, body)
    finally:
        conn.close()
