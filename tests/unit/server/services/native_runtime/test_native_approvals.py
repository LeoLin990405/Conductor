"""Native HITL pause/resume goes through the ordinary LangGraph interrupt."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from src.server.services.native_runtime.approvals import (
    PendingNativeApproval,
    get_approval_broker,
)
from src.server.services.native_runtime.contracts import NativeEvent, NativeRuntimeSpec
from src.server.services.native_runtime.graph import build_native_graph

THREAD_ID = "thread-native-approval"


class ApprovalRuntime:
    provider = "fake-approval"

    def __init__(self) -> None:
        self.broker = get_approval_broker()
        self.started = 0

    def capabilities(self):
        return None

    async def stream_turn(self, prompt, *, session_id=None, cwd=None):
        self.started += 1
        yield NativeEvent(kind="text_delta", text="before ", session_id="native-session")
        pending = PendingNativeApproval(
            thread_id=THREAD_ID,
            runtime=self,
            request_id=7,
            approval_id="approval-7",
            kind="command",
            interrupt_payload={
                "action_requests": [
                    {"name": "mcp__codex__command", "args": {"command": "echo hi"}}
                ],
                "kind": "tool_approval",
            },
            text_parts=["before "],
        )
        self.broker.park(pending)
        yield NativeEvent(
            kind="approval_requested",
            text="approval needed",
            session_id="native-session",
            data={"approval_id": "approval-7"},
        )

    async def resume_turn(self, response):
        self.broker.clear(THREAD_ID)
        yield NativeEvent(kind="text_delta", text="after", session_id="native-session")
        yield NativeEvent(kind="turn_completed", session_id="native-session")

    async def cancel(self):
        pass


@pytest.mark.asyncio
async def test_native_graph_pauses_and_resumes_through_hitl():
    get_approval_broker().clear(THREAD_ID)
    runtime = ApprovalRuntime()
    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(provider="fake-approval"),
        checkpointer=MemorySaver(),
        thread_id=THREAD_ID,
    )
    config = {"configurable": {"thread_id": THREAD_ID}}

    first = await graph.ainvoke(
        {"messages": [HumanMessage(content="do the thing")]}, config=config
    )
    assert runtime.started == 1
    assert first.get("__interrupt__")
    pending = get_approval_broker().get(THREAD_ID)
    assert pending is not None
    assert pending.interrupt_payload["action_requests"][0]["name"] == "mcp__codex__command"

    second = await graph.ainvoke(
        Command(resume={"interrupt-id": {"decisions": [{"type": "approve"}]}}),
        config=config,
    )
    projection = second["messages"][-1]
    assert isinstance(projection, AIMessage)
    assert projection.content == "before after"
    assert get_approval_broker().get(THREAD_ID) is None


def test_broker_maps_approve_and_reject_decisions():
    pending = PendingNativeApproval(
        thread_id="t",
        runtime=object(),
        request_id=1,
        approval_id="a",
        kind="command",
        interrupt_payload={},
    )
    assert pending.decision_for({"x": {"decisions": [{"type": "approve"}]}}) == "accept"
    assert pending.decision_for({"x": {"decisions": [{"type": "reject"}]}}) == "decline"
    assert pending.decision_for({}) == "cancel"
