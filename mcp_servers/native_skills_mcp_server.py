"""Native Skills MCP server.

Exposes the workspace-effective Conductor Skill catalog to a vendor-native CLI
through a short-lived capability token.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

try:
    from _bootstrap import MCPServer
except ModuleNotFoundError:
    from mcp_servers._bootstrap import MCPServer

mcp = MCPServer("ConductorNativeSkills")


def _get(path: str) -> dict:
    base = (os.environ.get("CONDUCTOR_API_BASE") or "http://127.0.0.1:8000").rstrip("/")
    token = os.environ.get("CONDUCTOR_MEMORY_TOKEN") or ""
    req = urllib.request.Request(
        f"{base}{path}",
        headers={"X-Native-Memory-Token": token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode() or "{}")
    except Exception as exc:
        return {"error": str(exc)[:500]}


@mcp.tool()
def list_skills() -> dict:
    """List Conductor skills available to this workspace.

    Returns `{skills: [{name, description, command}]}`. Use `load_skill` to
    read a skill's SKILL.md before following it.
    """
    return _get("/api/v1/native/skills")


@mcp.tool()
def load_skill(name: str, path: str = "SKILL.md") -> dict:
    """Read one file (default SKILL.md) from an Conductor skill by name."""
    clean = (name or "").strip()
    if not clean:
        return {"error": "skill name is empty"}
    query = f"?path={urllib.parse.quote(path or 'SKILL.md')}"
    return _get(f"/api/v1/native/skills/{urllib.parse.quote(clean)}{query}")


if __name__ == "__main__":
    mcp.run(transport="stdio")
