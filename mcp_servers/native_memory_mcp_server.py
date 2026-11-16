"""Native memory MCP server.

Exposes Conductor's explicit memory write to a vendor-native CLI. The server is
launched with a short-lived capability token; it never sees Conductor auth or
the user's credentials.
"""

from __future__ import annotations

import json
import os
import urllib.request

try:
    from _bootstrap import MCPServer
except ModuleNotFoundError:
    from mcp_servers._bootstrap import MCPServer

mcp = MCPServer("ConductorNativeMemory")


def _post(path: str, payload: dict) -> dict:
    base = (os.environ.get("CONDUCTOR_API_BASE") or "http://127.0.0.1:8000").rstrip("/")
    token = os.environ.get("CONDUCTOR_MEMORY_TOKEN") or ""
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Native-Memory-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode() or "{}")
    except Exception as exc:
        return {"error": str(exc)[:500]}


@mcp.tool()
def remember(text: str) -> dict:
    """Explicitly save one fact/preference to Conductor long-term memory.

    Use only when the user asks to remember something. `text` must be the
    exact fact to store (at most 1000 characters); it is written to the
    user's memory namespace and appears in memory search.
    """
    clean = (text or "").strip()
    if not clean:
        return {"error": "text is empty"}
    return _post("/api/v1/memory/native/remember", {"text": clean})


if __name__ == "__main__":
    mcp.run(transport="stdio")
