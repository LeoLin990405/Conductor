"""Central MCP registry for the agent fleet.

One ``servers.toml`` under ``<home>/.conductor/mcp`` is rendered into each
agent's native config. Only names the journal replay says this hub owns are
replaced; everything else in those files is left byte for byte.
``managed.toml`` is a cache of that replay for people to read.
"""

from __future__ import annotations

from .apply import apply_all, diff_all, recover, rollback
from .check import check_server, list_status
from .errors import McpHubError
from .importer import import_existing
from .model import Registry, Server
from .render import render_agent
from .store import load_registry, save_registry

__all__ = [
    "McpHubError",
    "Registry",
    "Server",
    "apply_all",
    "check_server",
    "diff_all",
    "import_existing",
    "list_status",
    "load_registry",
    "recover",
    "render_agent",
    "rollback",
    "save_registry",
]
