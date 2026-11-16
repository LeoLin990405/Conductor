#!/usr/bin/env python3
"""Exercise the production Nginx config against a harmless Unix-socket fixture.

Run as host root with DOCKER_HOST pointing at the dedicated rootless daemon.
The client must reach a loopback-published port; the service UID's egress policy
deliberately refuses initiating that host-local connection. No app/LLM calls,
image pulls, production containers or existing networks are involved.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler
import http.client
import json
import os
from pathlib import Path
import re
import socketserver
import subprocess
import tempfile
import threading
import time
import uuid


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], text=True, capture_output=True,
                          check=True, timeout=30).stdout.strip()


def run(config: Path, image: str) -> dict:
    if os.geteuid() != 0:
        raise ValueError("Run this trusted loopback client as host root with the rootless DOCKER_HOST")
    if not re.fullmatch(r"unix:///run/user/[0-9]+/docker.sock", os.environ.get("DOCKER_HOST", "")):
        raise ValueError("DOCKER_HOST must explicitly name the dedicated user socket")
    options = json.loads(docker("info", "--format", "{{json .SecurityOptions}}"))
    if "name=rootless" not in options:
        raise ValueError("Refusing a rootful Docker daemon")
    image_id = docker("image", "inspect", image, "--format", "{{.Id}}")
    calls: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self):
            calls.append((self.command, self.path))
            if self.path.startswith("/__probe_upstream_error__"):
                self.close_connection = True
                return
            if self.headers.get("Upgrade", "").lower() == "websocket":
                key = self.headers.get("Sec-WebSocket-Key", "")
                accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
            else:
                body = b"harmless-unix-backend"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        do_GET = respond
        do_POST = respond

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    result = {"status": "failed", "scope": "real nginx Host/Origin/Fetch-Site/WS routing, harmless UDS fixture", "image": image_id}
    name = "conductor-guard-probe-" + uuid.uuid4().hex[:12]
    container_created = False
    with tempfile.TemporaryDirectory(prefix="conductor-guard-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        socket_path = root / "backend.sock"
        server = Server(str(socket_path), Handler)
        socket_path.chmod(0o777)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # A timed-out Docker CLI can leave the named container created.
            # Always attempt cleanup once creation has been requested.
            container_created = True
            docker("run", "-d", "--pull=never", "--name", name,
                   "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
                   "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
                   "--cpus=0.5", "--memory=128m", "--pids-limit=64",
                   "-p", "127.0.0.1::8080",
                   "--mount", f"type=bind,src={root},dst=/run/conductor,readonly",
                   "--mount", f"type=bind,src={config.resolve()},dst=/etc/nginx/conf.d/default.conf,readonly",
                   image)
            bindings = json.loads(docker("inspect", name, "--format", "{{json .NetworkSettings.Ports}}"))
            port = int(bindings["8080/tcp"][0]["HostPort"])

            def request(method, path, headers):
                client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    client.request(method, path, body=b"", headers=headers)
                    response = client.getresponse()
                    body = b"" if response.status == 101 else response.read()
                    return response.status, body
                finally:
                    client.close()

            for _ in range(50):
                try:
                    status, _ = request("GET", "/healthz", {"Host": "127.0.0.1:8080"})
                    if status == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.2)
            else:
                raise RuntimeError("Nginx did not start: " + docker("logs", name)[-1500:])
            docker("exec", name, "nginx", "-t")
            result["nginx_config_test"] = "passed"
            host = "localhost:18100"
            preview = "8080-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.preview.localhost:18100"
            private_marker = "conductor-capability-" + uuid.uuid4().hex
            same = {"Host": host, "Origin": "http://" + host, "Sec-Fetch-Site": "same-origin"}
            ws = {"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="}
            cases = [
                ("same_origin_get", "GET", "/api/probe", same, 200),
                ("same_origin_post", "POST", "/api/v1/computers/example/stop", same, 200),
                ("trusted_cli", "POST", "/api/probe", {"Host": host}, 200),
                ("trusted_ip", "GET", "/api/probe", {"Host": "127.0.0.1:18100", "Origin": "http://127.0.0.1:18100"}, 200),
                ("preview_origin", "POST", "/api/v1/computers/example/stop", {"Host": host, "Origin": "http://" + preview, "Content-Type": "application/x-www-form-urlencoded"}, 403),
                ("null_origin", "POST", "/api/probe", {"Host": host, "Origin": "null"}, 403),
                ("unknown_origin", "POST", "/api/probe", {"Host": host, "Origin": "https://evil.example"}, 403),
                ("unknown_host", "POST", "/api/probe", {**same, "Host": "evil.example:18100"}, 403),
                ("cross_site", "POST", "/api/probe", {**same, "Sec-Fetch-Site": "cross-site"}, 403),
                ("cross_site_no_origin", "POST", "/api/probe", {"Host": host, "Sec-Fetch-Site": "cross-site"}, 403),
                ("same_site", "POST", "/api/probe", {**same, "Sec-Fetch-Site": "same-site"}, 403),
                ("unknown_fetch_site", "POST", "/api/probe", {**same, "Sec-Fetch-Site": "unknown"}, 403),
                ("websocket_same_origin", "GET", "/ws/probe", {**same, **ws}, 101),
                ("websocket_preview_origin", "GET", "/ws/probe", {"Host": host, "Origin": "http://" + preview, **ws}, 403),
                ("websocket_null_origin", "GET", "/ws/probe", {"Host": host, "Origin": "null", **ws}, 403),
                ("websocket_unknown_host", "GET", "/ws/probe", {**same, **ws, "Host": "evil.example:18100"}, 403),
                ("preview_not_guarded", "GET", "/any-preview-path", {"Host": preview, "Origin": "null", "Sec-Fetch-Site": "cross-site"}, 200),
                ("private_referer_query", "GET", "/api/probe?token=" + private_marker, {**same, "Referer": "http://" + private_marker + ".preview.localhost:18100/"}, 200),
                ("private_upstream_error", "GET", "/__probe_upstream_error__?token=" + private_marker, {"Host": private_marker + ".preview.localhost:18100", "Referer": "http://" + private_marker + ".preview.localhost:18100/"}, 502),
            ]
            passed = []
            for label, method, path, headers, expected in cases:
                before = len(calls)
                actual, body = request(method, path, headers)
                if actual != expected or (expected == 403 and len(calls) != before):
                    raise RuntimeError(f"{label}: status {actual}, expected {expected}; denied requests must not reach UDS")
                if expected == 200 and body != b"harmless-unix-backend":
                    raise RuntimeError(f"{label}: did not reach the intended UDS fixture")
                passed.append(label)
            logs = subprocess.run(["docker", "logs", name], text=True, capture_output=True, check=True, timeout=30)
            if private_marker in logs.stdout + logs.stderr:
                raise RuntimeError("Capability-like request metadata leaked to Nginx logs")
            result.update(status="passed", cases=passed, denied_requests_never_reached_backend=True,
                          private_request_metadata_absent_from_logs=True)
        except Exception as exc:
            result.update(error_type=type(exc).__name__, error=str(exc))
        finally:
            if container_created:
                try:
                    docker("rm", "-f", name)
                    result["container_removed"] = True
                except Exception:
                    result.update(status="failed", container_removed=False)
            server.shutdown()
            server.server_close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="nginx config file to probe")
    parser.add_argument("--image", default="nginxinc/nginx-unprivileged:1.28-alpine")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.config, args.image)
    document = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(document)
    print(document, end="")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
