"""Runtime composition checks for the frozen tool-policy gate."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from ptc_agent.agent.middleware.memory_tool_policy import MemoryToolPolicyMiddleware
from src.server.contracts.memory_tool_policy import MemoryToolPolicySnapshot, ToolIdentifier


def test_policy_is_outermost_in_parent_and_child_middleware_stacks():
    source_path = Path(__file__).parents[4] / "src/ptc_agent/agent/agent.py"
    tree = ast.parse(source_path.read_text())
    assignments = {
        node.targets[0].id: node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }

    policy_assignment = assignments["memory_tool_policy_middleware"]
    assert isinstance(policy_assignment, ast.IfExp)
    assert "MemoryToolPolicyMiddleware" in ast.unparse(policy_assignment)
    assert "extra_tools" in ast.unparse(assignments["policy_tools"])

    stack_assignments = {
        name: value
        for name, value in assignments.items()
        if name in {"subagent_middleware", "deepagent_middleware"}
    }
    assert set(stack_assignments) == {"subagent_middleware", "deepagent_middleware"}
    for name, value in stack_assignments.items():
        list_nodes = [node for node in ast.walk(value) if isinstance(node, ast.List)]
        stack = next(
            node
            for node in list_nodes
            if any(
                isinstance(child, ast.Name)
                and child.id == "memory_tool_policy_middleware"
                for child in ast.walk(node)
            )
        )
        names = [node.id for node in stack.elts if isinstance(node, ast.Name)]
        assert names[0] == "memory_tool_policy_middleware", name
    deep_stack = next(
        node
        for node in ast.walk(stack_assignments["deepagent_middleware"])
        if isinstance(node, ast.List)
        and any(
            isinstance(child, ast.Name)
            and child.id == "memory_tool_policy_middleware"
            for child in ast.walk(node)
        )
    )
    deep_names = [node.id for node in deep_stack.elts if isinstance(node, ast.Name)]
    assert deep_names.index("memory_tool_policy_middleware") < deep_names.index(
        "subagent_task_middleware"
    )


@pytest.mark.asyncio
async def test_denied_task_never_reaches_child_launch_interceptor():
    snapshot = MemoryToolPolicySnapshot(
        denied_tools=(ToolIdentifier(type="tool", name="Task"),)
    )
    middleware = MemoryToolPolicyMiddleware(snapshot)
    launches = []

    async def launch_child(_request):
        launches.append("started")
        return ToolMessage(content="launched", tool_call_id="task-call")

    request = SimpleNamespace(
        tool_call={"name": "Task", "id": "task-call", "args": {"task": "work"}}
    )
    result = await middleware.awrap_tool_call(request, launch_child)

    assert result.status == "error"
    assert "owner-approved memory constraint" in result.content
    assert launches == []


@pytest.mark.asyncio
async def test_registered_extra_tool_stamp_is_checked_by_exact_mcp_identity():
    extra_tool = SimpleNamespace(
        name="BrokerTrade",
        metadata={"direct_mcp": {"server": "broker", "tool": "place_order"}},
    )
    snapshot = MemoryToolPolicySnapshot(
        denied_tools=(
            ToolIdentifier(type="mcp", server="broker", tool="place_order"),
        )
    )
    middleware = MemoryToolPolicyMiddleware(snapshot, tools=[extra_tool])
    invoked = []

    async def execute(_request):
        invoked.append(True)
        return ToolMessage(content="unexpected", tool_call_id="broker-call")

    request = SimpleNamespace(
        tool_call={"name": "BrokerTrade", "id": "broker-call", "args": {}}
    )
    result = await middleware.awrap_tool_call(request, execute)
    assert result.status == "error"
    assert not invoked
