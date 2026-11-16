"""Per-app MCP adapters that need more than a row in ``agents.SPECS``.

Each module exposes ``SPEC``. This file is the only registry; adapter modules
never import each other.
"""

from __future__ import annotations

from . import agents_shared, antigravity, claude_desktop, dsh, gemini, qwen

ADAPTER_SPECS = (
    dsh.SPEC,
    claude_desktop.SPEC,
    gemini.SPEC,
    antigravity.SPEC,
    qwen.SPEC,
    agents_shared.SPEC,
)
