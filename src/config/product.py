"""Product profile for Conductor.

Conductor is a single product: a multi-agent collaboration and aggregation
operating platform. No route serves this profile; only its unit test reads it.
"""

from __future__ import annotations


def product_capabilities() -> dict[str, object]:
    """Conductor's name, tagline and capability list."""
    return {
        "name": "Conductor",
        "tagline": "多 Agent 协作的聚合操作平台",
        "tagline_en": "The aggregation operating platform for multi-agent collaboration",
        "platform": "multi-agent",
        "vertical": "core",
        "orchestrator": True,
        "capabilities": [
            "super-agent",
            "tickets",
            "concurrent-workers",
            "scheduled-tasks",
            "native-cli-agents",
            "workspaces",
            "plugins",
            "mcp",
            "hitl",
        ],
    }
