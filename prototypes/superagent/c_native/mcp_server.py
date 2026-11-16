"""MCP server: the autonomy tools, and nothing else.

The Claude Code CLI reaches the Router and Watchdog tools through MCP. This
server speaks MCP over **stdio** — JSON-RPC on standard input and standard
output — which is the transport the real Claude Code uses for a server named
in ``--mcp-config``. It binds no socket and opens no TCP connection.

The server publishes **only** the role's autonomy tools. The role comes from
the session row in the store, so a Router tool is not even listed for a
Watchdog session, and the bridge refuses such a call a second time. The tool
schemas come from ``roles.json``, which the adapter writes from the product
tool registry when the session starts.

The session is the server's own configuration, read from ``session.json`` at
startup. A client speaks plain MCP to it: ``initialize`` with the protocol
version and the client info, then ``tools/list``, then ``tools/call``. No
session field is needed in ``initialize``, and none is invented.

    -> {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "claude-code", "version": "2.1.311"}}}
    <- {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}}, "serverInfo": {...}}}
    -> {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    <- {"jsonrpc": "2.0", "id": 2, "result": {"tools": [...]}}
    -> {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "autonomy_route", "arguments": {...}}}
    <- {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", ...}]}}
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from prototypes.superagent.bridge import BridgeClient, BridgeError

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "autonomy"


class McpError(Exception):
    """A request the server refuses. Reported as a JSON-RPC error."""


class McpServer:
    """Publish the role's autonomy tools and dispatch their calls."""

    def __init__(self, bridge_path: str, scratch: Path) -> None:
        self.bridge_path = bridge_path
        self.scratch = scratch
        self.session_id = self._read_session(scratch)
        self.roles = self._read_roles(scratch)
        self.bridge = BridgeClient(bridge_path)

    @staticmethod
    def _read_session(scratch: Path) -> str:
        try:
            raw = json.loads((scratch / "session.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        return str(raw.get("session_id") or "")

    @staticmethod
    def _read_roles(scratch: Path) -> dict[str, list[dict[str, Any]]]:
        try:
            raw = json.loads((scratch / "roles.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(role): list(tools) for role, tools in (raw or {}).items()}

    def serve(self) -> None:
        """Serve MCP on standard input and output until end of input."""

        (self.scratch / "mcp.pid").write_text(str(os.getpid()), encoding="utf-8")
        # The adapter waits for this file before it launches the CLI.
        (self.scratch / "mcp.ready").write_text(str(os.getpid()), encoding="utf-8")
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    continue
                reply = self._handle(request)
                if reply is None:
                    continue
                sys.stdout.write(json.dumps(reply) + "\n")
                sys.stdout.flush()
        finally:
            self.bridge.close()

    def _handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = str(request.get("method") or "")
        params = request.get("params") or {}
        if not request_id:
            # A notification. Nothing here is worth answering.
            return None
        try:
            if method == "initialize":
                result: Any = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
                }
            elif method == "tools/list":
                result = {"tools": self._tools()}
            elif method == "tools/call":
                result = self._call(params)
            elif method == "ping":
                result = {}
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"unknown method: {method}"},
                }
        except McpError as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": str(exc)},
            }
        except BridgeError as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _role(self) -> str:
        row = self.bridge.call("store.get_session", session_id=self.session_id)
        if row is None:
            raise McpError(f"session {self.session_id} is not started")
        return str(row["role"])

    def _tools(self) -> list[dict[str, Any]]:
        return list(self.roles.get(self._role(), []))

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        allowed = {str(tool["name"]) for tool in self._tools()}
        if name not in allowed:
            # The bridge refuses this too; refusing here keeps the tool out
            # of the model's reach in the first place.
            raise McpError(f"tool {name} is not offered to this session")
        done = self.bridge.call(
            "tools.call", session_id=self.session_id, name=name, arguments=arguments
        )
        return {
            "content": [{"type": "text", "text": done["result"]}],
            "structuredContent": {"tool": name, "result": done["result"]},
            "isError": False,
        }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write("usage: mcp_server.py <bridge_path> <scratch_dir>\n")
        return 2
    McpServer(argv[1], Path(argv[2])).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
