from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from ptc_agent.agent.middleware.memory_tool_policy import MemoryToolPolicyMiddleware
from src.server.contracts.memory_tool_policy import ToolIdentifier
from src.server.services.refactor.memory_tool_policy import MemoryToolPolicySnapshot


@pytest.mark.asyncio
async def test_denies_exact_direct_name_and_execution_surfaces():
    snapshot = MemoryToolPolicySnapshot(
        denied_tools=(ToolIdentifier(type="tool", name="DeleteRecord"),)
    )
    middleware = MemoryToolPolicyMiddleware(snapshot)
    invoked = []

    async def handler(request):
        invoked.append(request.tool_call["name"])
        return ToolMessage(content="ok", tool_call_id=request.tool_call["id"])

    async def call(name):
        request = SimpleNamespace(tool_call={"name": name, "id": f"call-{name}"})
        return await middleware.awrap_tool_call(request, handler)

    refused = await call("DeleteRecord")
    allowed = await call("ReadRecord")
    blocked_surfaces = [
        await call(name)
        for name in (
            "ExecuteCode",
            "Bash",
            "BashOutput",
            "NativeAgent",
            "NativeAgents",
        )
    ]

    assert refused.status == "error"
    assert allowed.content == "ok"
    assert all(result.status == "error" for result in blocked_surfaces)
    assert invoked == ["ReadRecord"]


@pytest.mark.asyncio
async def test_denies_exact_mcp_stamp_and_fails_closed_on_loader_error():
    snapshot = MemoryToolPolicySnapshot(
        denied_tools=(
            ToolIdentifier(type="mcp", server="inventory", tool="delete_item"),
        )
    )
    direct_tool = SimpleNamespace(
        name="InventoryDelete",
        metadata={
            "direct_mcp": {
                "server": "inventory",
                "tool": "delete_item",
                "vendor": "warehouse",
                "sandboxed": False,
            }
        },
    )
    middleware = MemoryToolPolicyMiddleware(snapshot, tools=[direct_tool])
    request = SimpleNamespace(tool_call={"name": "InventoryDelete", "id": "call-1"})

    async def handler(_request):
        raise AssertionError("denied tool must not execute")

    result = await middleware.awrap_tool_call(request, handler)
    assert result.status == "error"

    fail_closed = MemoryToolPolicyMiddleware(MemoryToolPolicySnapshot(deny_all=True))
    read_request = SimpleNamespace(tool_call={"name": "ReadRecord", "id": "call-2"})
    refused = await fail_closed.awrap_tool_call(read_request, handler)
    assert refused.status == "error"
    assert "policy is unavailable" in refused.content
