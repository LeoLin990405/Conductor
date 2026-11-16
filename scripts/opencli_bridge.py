#!/usr/bin/env python3
"""Minimal host-side HTTP bridge for OpenCLI.

Run this on the machine that has Chrome + the OpenCLI Browser Bridge
extension when the Conductor backend itself runs in a container:

    OPENCLI_BRIDGE_TOKEN=change-me python scripts/opencli_bridge.py --port 8787

The backend calls POST /run with {"args": [...], "timeout_seconds": N}.
Only the two adapters used by the CN news feed are allowed:

    xueqiu hot
    weixin search <query>

The OpenCLI token is never logged. This process should be bound to loopback
and reachable only by the backend (or put it behind the deployment's private
network).
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_ALLOWED = {
    ("xueqiu", "hot"),
    ("weixin", "search"),
}
_MAX_TIMEOUT = 30
_MAX_BODY = 16 * 1024


def _allowed(args: list[str]) -> bool:
    if len(args) < 2:
        return False
    if (args[0], args[1]) not in _ALLOWED:
        return False
    # The remaining argv is forwarded to OpenCLI. It may contain a search query
    # and fixed flags, but never another command position.
    allowed_flags = {"--limit", "--page"}
    return all(
        not str(arg).startswith("--") or str(arg) in allowed_flags
        for arg in args[2:]
    )


def _run(args: list[str], timeout: float) -> dict[str, Any]:
    binary = (os.environ.get("OPENCLI_BIN") or "").strip() or shutil.which("opencli")
    if not binary:
        return {"ok": False, "error": "opencli not found"}
    try:
        result = subprocess.run(
            [binary, *args, "-f", "json"],
            capture_output=True,
            text=True,
            timeout=min(max(timeout, 1), _MAX_TIMEOUT),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)[:300]}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": (result.stderr or result.stdout or "non-json output")[:300],
        }
    if isinstance(payload, dict) and payload.get("ok") is False:
        return payload
    return {"ok": True, "data": payload.get("data", payload) if isinstance(payload, dict) else payload}


def make_handler(token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ConductorOpenCLIBridge/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            # Never write the Authorization header or its token.
            print(f"opencli-bridge: {fmt % args}", flush=True)

        def _authorized(self) -> bool:
            if not token:
                return True
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied, f"Bearer {token}")

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._json(200, {"ok": True, "opencli": bool(shutil.which("opencli"))})
                return
            self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/run":
                self._json(404, {"ok": False, "error": "not found"})
                return
            if not self._authorized():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > _MAX_BODY:
                self._json(400, {"ok": False, "error": "invalid body size"})
                return
            try:
                body = json.loads(self.rfile.read(length).decode())
                args = body.get("args")
                timeout = float(body.get("timeout_seconds") or 10)
                if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                    raise ValueError("args must be a string list")
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"ok": False, "error": str(exc)[:200]})
                return
            if not _allowed(args):
                self._json(403, {"ok": False, "error": "command not allowed"})
                return
            self._json(200, _run(args, timeout))

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    token = (os.environ.get("OPENCLI_BRIDGE_TOKEN") or "").strip()
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not token:
        raise SystemExit("refusing non-loopback bind without OPENCLI_BRIDGE_TOKEN")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(token))
    print(f"opencli-bridge listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
