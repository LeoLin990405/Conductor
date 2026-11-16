"""agents: the shared cross-tool ``~/.agents/mcp.json`` (``mcpServers`` map).

Readers found (checked read-only against installed builds, 2026-11):

* ZCode CLI (``ZCode.app/Contents/Resources/glm/zcode.cjs``) reads
  ``<workspace>/.agents/mcp.json`` as a fallback, only when the workspace has
  no project MCP servers of its own; with the workspace at HOME that is this
  file. Each ``mcpServers`` entry is validated with ZCode's own server schema:
  ``{command, args?, env?, cwd?}`` (``type`` inferred as ``stdio``) or
  ``{type: "http"|"sse", url, headers?}`` (``type`` inferred from ``url``).
  The schema is strict, so unknown keys make the entry invalid.
* FreeBuff, per the REA installer's client table (``rea-agents``
  ``SupportedClients.js``): it "loads MCP servers from the shared user agents
  directory" and REA writes it the plain ``{command, args, env?}`` shape.
  FreeBuff itself is not installed here, so its remote dialect is unverified.
* Not readers: Kimi Code reads ``~/.agents/skills`` and ``~/.agents/AGENTS.md``
  only; Codex reads ``.agents/skills`` and ``.agents/plugins``; cursor-agent has
  no reference to the file.

Projection: the shape both readers accept, ``{command, args, env?}``. Remote
servers are refused: ZCode would take ``{type, url}`` but FreeBuff's remote
key is unknown, and a wrong guess makes one reader drop the entry.

Env: neither reader expands ``${VAR}`` in this file (ZCode's only ``${...}``
expander serves plugin ``.mcp.json`` root variables), so a reference would
reach the server as literal text. It is refused unless the server is named in
``allow_inline_secret``. Keychain references are refused by the generic checks.

Only ``mcp.json`` is touched; ``.agents/skills`` and ``.agents/AGENTS.md`` are
other tools' storage and are never read or written here.
"""

from __future__ import annotations

from typing import Any

from ..model import classify_env_value
from .base import is_remote, json_adapter, stdio_object


def native(effective: dict[str, Any]) -> dict[str, Any]:
    return stdio_object(effective)


def cannot(name: str, effective: dict[str, Any], allow: set[str]) -> str | None:
    if is_remote(effective) or not effective["command"]:
        return (
            f"{name}: agents (~/.agents/mcp.json) only holds stdio servers; "
            "the remote dialect is not shared by its readers"
        )
    if name not in allow:
        for key, value in effective["env"].items():
            if classify_env_value(value) == "var":
                return (
                    f"{name}: agents readers pass env {key} literally "
                    f"(no ${{VAR}} expansion); pass allow_inline_secret to write it"
                )
    return None


SPEC = json_adapter("agents", ".agents", "mcp.json", native=native, cannot=cannot)
