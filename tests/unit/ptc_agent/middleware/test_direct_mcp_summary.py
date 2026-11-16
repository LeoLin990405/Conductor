from __future__ import annotations

from langchain_core.tools import StructuredTool

from ptc_agent.agent.middleware.direct_mcp import METADATA_KEY, direct_tool_summary


def _tool(name: str, description: str) -> StructuredTool:
    tool = StructuredTool.from_function(
        func=lambda: None, name=name, description=description
    )
    tool.metadata = {METADATA_KEY: {"server": "inventory", "tool": name}}
    return tool


def test_summary_is_one_line_per_tool_cut_to_the_first_line():
    out = direct_tool_summary(
        [
            _tool("mcp__inventory__reserve", "Reserve an item.\nLong detail follows."),
            _tool("mcp__inventory__count", "x" * 200),
        ]
    )
    lines = out.splitlines()
    assert lines[0] == "- `mcp__inventory__reserve`: Reserve an item."
    assert lines[1].endswith("...")
    assert len(lines[1]) < 200
