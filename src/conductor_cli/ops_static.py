"""Serve the built web console on 127.0.0.1, like ``vite preview`` does.

Three things a plain file server lacks, so deep links and the API work:

* ``/api/`` (and ``/health``, ``/ready``) are proxied to the backend.
* ``/ws/`` WebSocket upgrades are tunnelled to the backend.
* Any other GET that names no file under the build serves ``index.html``
  (the SPA fallback), so a reload on ``/app/tickets/42`` opens the console.

The backend is ``--backend``, else ``VITE_PROXY_BACKEND``, else
``http://127.0.0.1:$CONDUCTOR_BACKEND_PORT``. Only a loopback http backend is
accepted. Requests whose path resolves outside the build directory get 404.

Like vite's default host check, a request whose ``Host`` is not 127.0.0.1,
localhost or [::1] (bare, or with this console's own port) gets 403, so a
DNS-rebound page cannot read the API through the console. Streamed backend
replies (``text/event-stream``, chunked, or no Content-Length) are relayed
piece by piece with an idle timeout, so chat output shows as it arrives.
"""

from __future__ import annotations

import argparse
import http.client
import os
import select
import socket
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

PROXY_PREFIXES = ("/api/", "/ws/")
PROXY_EXACT = ("/health", "/ready")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
# Hop-by-hop headers are not forwarded either way (RFC 9110 section 7.6.1).
_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_TIMEOUT = 60.0
# Longest gap allowed between two pieces of a streamed reply.
_STREAM_IDLE_TIMEOUT = 300.0
_PIECE = 65536
# The backend's largest upload cap (RELAY_MAX_BYTES in
# src/server/database/blob_keys.py, the workspace file upload limit). The
# body is streamed to the backend, never held in memory whole.
MAX_BODY_BYTES = 256 * 1024 * 1024
_ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]"}


def default_backend(env: dict[str, str] | None = None) -> str:
    env = dict(os.environ) if env is None else env
    explicit = (env.get("VITE_PROXY_BACKEND") or "").strip()
    if explicit:
        return explicit
    port = (env.get("CONDUCTOR_BACKEND_PORT") or "").strip() or "8000"
    return f"http://127.0.0.1:{port}"


def parse_backend(raw: str) -> tuple[str, int]:
    parts = urlsplit(raw)
    if parts.scheme != "http" or not parts.hostname or parts.hostname not in _LOOPBACK:
        raise SystemExit(f"the static console proxies to a loopback http backend only: {raw!r}")
    if parts.path not in {"", "/"} or parts.query:
        raise SystemExit(f"the backend URL takes no path: {raw!r}")
    try:
        port = parts.port or 80
    except ValueError as exc:
        raise SystemExit(f"bad backend port: {raw!r}") from exc
    return parts.hostname, port


def is_proxied(path: str) -> bool:
    bare = urlsplit(path).path
    return bare.startswith(PROXY_PREFIXES) or bare in PROXY_EXACT


def host_allowed(host: str | None, port: int) -> bool:
    """Loopback name, bare or with ``port`` (vite ignores the port; we pin it)."""
    raw = (host or "").strip().lower()
    if raw in _ALLOWED_HOSTNAMES:
        return True
    name, sep, given = raw.rpartition(":")
    return bool(sep) and name in _ALLOWED_HOSTNAMES and given == str(port)


def _is_stream(response: http.client.HTTPResponse) -> bool:
    kind = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
    return (
        kind == "text/event-stream"
        or response.chunked
        or response.getheader("Content-Length") is None
    )


class _BoundedBody:
    """File-like view of exactly ``length`` request-body bytes."""

    def __init__(self, raw, length: int) -> None:  # noqa: ANN001 - rfile
        self._raw = raw
        self._left = length

    def read(self, size: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        size = self._left if size is None or size < 0 else min(size, self._left)
        data = self._raw.read(size)
        self._left -= len(data)
        if not data:
            self._left = 0
        return data


class ConsoleHandler(SimpleHTTPRequestHandler):
    backend_host = "127.0.0.1"
    backend_port = 8000

    def __init__(self, *args, backend: tuple[str, int], **kwargs) -> None:
        self.backend_host, self.backend_port = backend
        super().__init__(*args, **kwargs)

    # -- static files --------------------------------------------------------

    def _root(self) -> Path:
        return Path(self.directory).resolve()

    def _contained(self, path: str) -> bool:
        try:
            Path(path).resolve().relative_to(self._root())
        except (OSError, ValueError):
            return False
        return True

    def send_head(self):  # noqa: ANN201 - stdlib signature
        bare = unquote(urlsplit(self.path).path)
        target = self.translate_path(self.path)
        if not self._contained(target):
            self.send_error(404, "File not found")
            return None
        if not os.path.exists(target) and "." not in bare.rsplit("/", 1)[-1]:
            # SPA fallback: a client-side route names no file in the build.
            # A missing asset (a name with an extension) stays a 404.
            self.path = "/index.html"
        return super().send_head()

    def list_directory(self, path):  # noqa: ANN001, ANN201 - stdlib signature
        self.send_error(404, "File not found")
        return None

    # -- proxy ---------------------------------------------------------------

    def _host_ok(self) -> bool:
        if host_allowed(self.headers.get("Host"), self.server.server_address[1]):
            return True
        self.send_error(403, "Host not allowed")
        return False

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        if not self._host_ok():
            return
        if is_proxied(self.path):
            if (self.headers.get("Upgrade") or "").lower() == "websocket":
                self._tunnel()
            else:
                self._proxy()
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib name
        if not self._host_ok():
            return
        if is_proxied(self.path):
            self._proxy()
            return
        super().do_HEAD()

    def _proxy_or_405(self) -> None:
        if not self._host_ok():
            return
        if is_proxied(self.path):
            self._proxy()
        else:
            self.send_error(405, "Method not allowed")

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _proxy_or_405

    def _forward_headers(self) -> dict[str, str]:
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP and key.lower() != "host"
        }
        headers["Host"] = f"{self.backend_host}:{self.backend_port}"
        return headers

    def _request_body(self) -> tuple[bool, _BoundedBody | None]:
        """The request body to forward, or ``(False, None)`` after an error reply."""
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self.send_error(411, "Length required")
            return False, None
        raw = (self.headers.get("Content-Length") or "").strip()
        if not raw:
            return True, None
        if not raw.isdigit():
            self.close_connection = True
            self.send_error(400, "Invalid Content-Length")
            return False, None
        length = int(raw)
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            self.send_error(413, "Request body too large")
            return False, None
        return True, (_BoundedBody(self.rfile, length) if length else None)

    def _proxy(self) -> None:
        ok, body = self._request_body()
        if not ok:
            return
        headers = self._forward_headers()
        if body is None:
            headers.pop("Content-Length", None)
        conn = http.client.HTTPConnection(
            self.backend_host, self.backend_port, timeout=_TIMEOUT
        )
        try:
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                response = conn.getresponse()
                stream = self.command != "HEAD" and _is_stream(response)
                payload = b"" if stream else response.read()
            except OSError:
                self.close_connection = True
                self.send_error(502, "Backend unreachable")
                return
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() in _HOP or key.lower() == "content-length":
                    continue
                self.send_header(key, value)
            if stream:
                # Close-delimited: no length up front, each piece flushed.
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                self._relay(conn, response)
                return
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        finally:
            conn.close()

    def _relay(
        self, conn: http.client.HTTPConnection, response: http.client.HTTPResponse
    ) -> None:
        if conn.sock is not None:
            conn.sock.settimeout(_STREAM_IDLE_TIMEOUT)
        try:
            while True:
                piece = response.read1(_PIECE)
                if not piece:
                    return
                self.wfile.write(piece)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            # Headers are out; ending the connection is all that is left.
            return

    def _tunnel(self) -> None:
        self.close_connection = True
        try:
            upstream = socket.create_connection(
                (self.backend_host, self.backend_port), timeout=_TIMEOUT
            )
        except OSError:
            self.send_error(502, "Backend unreachable")
            return
        lines = [f"{self.command} {self.path} HTTP/1.1"]
        for key, value in self.headers.items():
            if key.lower() == "host":
                continue
            lines.append(f"{key}: {value}")
        lines.append(f"Host: {self.backend_host}:{self.backend_port}")
        upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        self.wfile.flush()
        # A WebSocket client sends no frame before the 101 answer, so nothing
        # is left in rfile's buffer; the raw sockets carry the rest.
        try:
            _pipe(self.connection, upstream)
        finally:
            upstream.close()
            self.close_connection = True


def _pipe(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    while True:
        readable, _, failed = select.select(sockets, [], sockets, _TIMEOUT * 5)
        if failed or not readable:
            return
        for sock in readable:
            try:
                data = sock.recv(65536)
            except OSError:
                return
            if not data:
                return
            other = right if sock is left else left
            try:
                other.sendall(data)
            except OSError:
                return


def build_server(
    host: str, port: int, root: Path, backend: tuple[str, int]
) -> ThreadingHTTPServer:
    handler = partial(ConsoleHandler, directory=str(root), backend=backend)
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the Conductor web build")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--backend", default="")
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1":
        raise SystemExit("the static console binds 127.0.0.1 only")
    root = args.directory.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"web build directory is missing: {root}")
    if not (root / "index.html").is_file():
        raise SystemExit(f"web build has no index.html: {root}")
    backend = parse_backend(args.backend or default_backend())
    server = build_server(args.host, args.port, root, backend)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
