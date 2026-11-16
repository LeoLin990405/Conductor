"""Production ports for the Super-agent P lane (``bindings.py``)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from src.server.services.credit_gate import DEFAULT_STOP_MESSAGE, CreditLease
from src.server.contracts.memory_tool_policy import MemoryToolPolicySnapshot, ToolIdentifier
from src.server.services.superagent_runtime.bindings import (
    HostToolPort,
    LeaseCreditPort,
    NativeEventSink,
    PrecomputedRecall,
)
from src.server.services.superagent_runtime.ports import LoopEvent
from src.tools.autonomy.tools import AUTONOMY_WATCHDOG_TOOLS, autonomy_watchdog

ALLOW_ALL = MemoryToolPolicySnapshot()


def _gate(*, denial=None, ceiling=0.0):
    lease = CreditLease(user_id="u-1", run_ref="run-1", port=None)
    lease.denial = denial
    lease.ceiling_credits = ceiling
    return SimpleNamespace(lease=lease, run_ref="run-1")


# ---------------------------------------------------------------------------
# LeaseCreditPort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_gate_always_allows():
    verdict = await LeaseCreditPort(None).authorize()
    assert verdict.allowed is True


@pytest.mark.asyncio
async def test_gate_without_denial_allows():
    verdict = await LeaseCreditPort(_gate()).authorize()
    assert verdict.allowed is True


@pytest.mark.asyncio
async def test_denial_under_granted_ceiling_still_allows():
    # Spend under a standing grant is entitled to finish, as in the middleware.
    verdict = await LeaseCreditPort(
        _gate(denial={"message": "no"}, ceiling=10.0)
    ).authorize()
    assert verdict.allowed is True


@pytest.mark.asyncio
async def test_denial_past_ceiling_relays_service_message():
    verdict = await LeaseCreditPort(
        _gate(denial={"message": "Out of credits for today."})
    ).authorize()
    assert verdict.allowed is False
    assert verdict.reason == "Out of credits for today."


@pytest.mark.asyncio
async def test_denial_without_copy_falls_back_to_default_message():
    verdict = await LeaseCreditPort(_gate(denial={})).authorize()
    assert verdict.allowed is False
    assert verdict.reason == DEFAULT_STOP_MESSAGE


# ---------------------------------------------------------------------------
# PrecomputedRecall
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_returns_the_prelude_block():
    assert await PrecomputedRecall("## Memory\n- fact").recall() == "## Memory\n- fact"


@pytest.mark.asyncio
async def test_recall_of_no_block_is_empty():
    assert await PrecomputedRecall(None).recall() == ""
    assert await PrecomputedRecall("").recall() == ""


# ---------------------------------------------------------------------------
# HostToolPort
# ---------------------------------------------------------------------------


class _RecordingHost:
    def __init__(self):
        self.calls = []

    async def watchdog(self, **kwargs):
        self.calls.append(("watchdog", kwargs))
        return {"ok": True, "decision": kwargs["decision"]}


@pytest.mark.asyncio
async def test_host_tool_port_hands_the_host_to_a_real_autonomy_tool():
    host = _RecordingHost()
    port = HostToolPort(AUTONOMY_WATCHDOG_TOOLS, host, policy=ALLOW_ALL)

    out = await port.call(
        "autonomy_watchdog",
        {"ticket_id": "t-1", "decision": "continue", "reason": "tests pass"},
        "call-1",
    )

    assert json.loads(out) == {"ok": True, "decision": "continue"}
    assert host.calls == [
        (
            "watchdog",
            {
                "ticket_id": "t-1",
                "decision": "continue",
                "question": "",
                "reason": "tests pass",
            },
        )
    ]


@pytest.mark.asyncio
async def test_host_tool_port_puts_autonomy_host_into_configurable():
    seen = {}

    @tool("probe_tool")
    async def probe_tool(value: str, config: RunnableConfig) -> str:
        """Record the configurable this tool was invoked with."""
        seen.update(config.get("configurable") or {})
        return {"echo": value}

    host = object()
    port = HostToolPort([probe_tool], host, policy=ALLOW_ALL)
    out = await port.call("probe_tool", {"value": "x"}, "call-2")

    assert seen["autonomy_host"] is host
    # Non-string tool output is serialized for the ToolMessage.
    assert json.loads(out) == {"echo": "x"}


def test_tool_objects_are_the_real_tools_for_bind_tools():
    port = HostToolPort(AUTONOMY_WATCHDOG_TOOLS, object(), policy=ALLOW_ALL)
    objects = port.tool_objects()
    assert objects == list(AUTONOMY_WATCHDOG_TOOLS)
    assert objects[0] is autonomy_watchdog
    # A copy: the loop cannot mutate the role's list.
    objects.clear()
    assert len(port.tool_objects()) == len(AUTONOMY_WATCHDOG_TOOLS)


@pytest.mark.asyncio
async def test_unknown_tool_name_raises():
    port = HostToolPort(AUTONOMY_WATCHDOG_TOOLS, object(), policy=ALLOW_ALL)
    with pytest.raises(KeyError):
        await port.call("bash", {"cmd": "ls"}, "call-3")


# ---------------------------------------------------------------------------
# NativeEventSink
# ---------------------------------------------------------------------------


def _emit(event: LoopEvent):
    written = []
    NativeEventSink(written.append).emit(event)
    return written


def test_text_delta_carries_message_id():
    assert _emit(LoopEvent(kind="text_delta", text="hi", message_id="m-1")) == [
        {
            "type": "native_event",
            "payload": {"kind": "text_delta", "text": "hi", "message_id": "m-1"},
        }
    ]


def test_thinking_delta_maps_to_reasoning_kind():
    assert _emit(LoopEvent(kind="thinking_delta", text="hmm", message_id="m-1")) == [
        {
            "type": "native_event",
            "payload": {"kind": "thinking_delta", "text": "hmm", "message_id": "m-1"},
        }
    ]


def test_empty_deltas_are_dropped():
    assert _emit(LoopEvent(kind="text_delta", text="", message_id="m-1")) == []
    assert _emit(LoopEvent(kind="thinking_delta", text="")) == []


def test_tool_started_carries_args():
    assert _emit(
        LoopEvent(
            kind="tool_started",
            tool_name="autonomy_route",
            tool_call_id="c-1",
            data={"args": {"trigger_id": "tr-1"}},
        )
    ) == [
        {
            "type": "native_event",
            "payload": {
                "kind": "tool_started",
                "tool_name": "autonomy_route",
                "tool_call_id": "c-1",
                "data": {"args": {"trigger_id": "tr-1"}},
            },
        }
    ]


def test_tool_result_carries_content_and_status():
    assert _emit(
        LoopEvent(
            kind="tool_result",
            tool_name="autonomy_route",
            tool_call_id="c-1",
            text="{}",
            data={"content": '{"ok": true}', "status": "error"},
        )
    ) == [
        {
            "type": "native_event",
            "payload": {
                "kind": "tool_result",
                "tool_name": "autonomy_route",
                "tool_call_id": "c-1",
                "data": {"content": '{"ok": true}', "status": "error"},
            },
        }
    ]


def test_tool_result_status_defaults_to_success():
    [written] = _emit(
        LoopEvent(kind="tool_result", tool_name="t", tool_call_id="c", text="out")
    )
    assert written["payload"]["data"] == {"content": "out", "status": "success"}


def test_tool_refused_is_an_error_result():
    assert _emit(
        LoopEvent(
            kind="tool_refused",
            tool_name="bash",
            tool_call_id="c-9",
            data={"args": {"cmd": "rm -rf /"}},
        )
    ) == [
        {
            "type": "native_event",
            "payload": {
                "kind": "tool_result",
                "tool_name": "bash",
                "tool_call_id": "c-9",
                "data": {"content": "refused: bash", "status": "error"},
            },
        }
    ]


def test_stop_and_done_have_no_producer_frame():
    assert _emit(LoopEvent(kind="stop", text="Out of credits")) == []
    assert _emit(LoopEvent(kind="done", message_id="m-1")) == []


@pytest.mark.asyncio
async def test_mcp_prefixed_name_reaches_the_real_tool():
    host = _RecordingHost()
    port = HostToolPort(AUTONOMY_WATCHDOG_TOOLS, host, policy=ALLOW_ALL)
    await port.call(
        "mcp__conductor__autonomy_watchdog",
        {"ticket_id": "t-1", "decision": "continue", "reason": "r"},
        "call-4",
    )
    assert host.calls and host.calls[0][0] == "watchdog"


class _Policy:
    def __init__(self, *, deny_all=False, denied=()):
        self.deny_all = deny_all
        self.denied = set(denied)

    def blocks(self, name, stamp=None):
        return self.deny_all or name in self.denied


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "needle"),
    [
        (_Policy(denied={"autonomy_watchdog"}), "owner-approved"),
        (_Policy(deny_all=True), "unavailable"),
    ],
)
async def test_memory_tool_policy_refuses_before_the_host_runs(policy, needle):
    from src.server.services.superagent_runtime.bindings import ToolPolicyRefusal

    host = _RecordingHost()
    port = HostToolPort(AUTONOMY_WATCHDOG_TOOLS, host, policy=policy)
    with pytest.raises(ToolPolicyRefusal, match=needle):
        await port.call(
            "autonomy_watchdog",
            {"ticket_id": "t-1", "decision": "continue", "reason": "r"},
            "call-5",
        )
    assert host.calls == []


@pytest.mark.asyncio
async def test_policy_that_allows_the_tool_lets_it_run():
    host = _RecordingHost()
    port = HostToolPort(AUTONOMY_WATCHDOG_TOOLS, host, policy=_Policy(denied={"other"}))
    await port.call(
        "autonomy_watchdog",
        {"ticket_id": "t-1", "decision": "continue", "reason": "r"},
        "call-6",
    )
    assert len(host.calls) == 1


def test_deepseek_route_pads_every_tool_calling_turn_with_a_thinking_block():
    from langchain_core.messages import AIMessage, HumanMessage

    from src.server.services.superagent_runtime.bindings import provider_wire_adapter

    chat = SimpleNamespace(metadata={"provider_route": "deepseek-account"})
    adapt = provider_wire_adapter(chat)
    assert adapt is not None
    call = AIMessage(
        content="routing",
        tool_calls=[{"name": "autonomy_route", "args": {}, "id": "c1", "type": "tool_call"}],
    )
    plain = AIMessage(content="done")
    out = adapt([HumanMessage(content="q"), call, plain])
    assert out[1].content[0]["type"] == "thinking"
    assert out[1].tool_calls == call.tool_calls
    assert out[2].content == "done"
    # The checkpointed message is untouched.
    assert call.content == "routing"


@pytest.mark.parametrize(
    "metadata", [{"provider_route": "anthropic"}, {"provider_route": "openai"}, {}, None]
)
def test_other_routes_get_no_wire_adapter(metadata):
    from src.server.services.superagent_runtime.bindings import provider_wire_adapter

    assert provider_wire_adapter(SimpleNamespace(metadata=metadata)) is None


@pytest.mark.asyncio
async def test_no_policy_means_deny_all():
    from src.server.services.superagent_runtime.bindings import ToolPolicyRefusal

    host = _RecordingHost()
    port = HostToolPort(AUTONOMY_WATCHDOG_TOOLS, host, policy=None)
    with pytest.raises(ToolPolicyRefusal, match="unavailable"):
        await port.call(
            "autonomy_watchdog",
            {"ticket_id": "t-1", "decision": "continue", "reason": "r"},
            "call-7",
        )
    assert host.calls == []


def test_policy_is_a_required_argument():
    with pytest.raises(TypeError):
        HostToolPort(AUTONOMY_WATCHDOG_TOOLS, object())  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_mcp_denial_matches_the_tool_stamp():
    from src.server.services.superagent_runtime.bindings import ToolPolicyRefusal

    @tool("mcp_probe")
    async def mcp_probe(value: str) -> str:
        """Pretend MCP-backed tool."""
        return value

    mcp_probe.metadata = {"direct_mcp": {"server": "srv", "tool": "probe"}}
    policy = MemoryToolPolicySnapshot(
        denied_tools=(ToolIdentifier(type="mcp", server="srv", tool="probe"),)
    )
    port = HostToolPort([mcp_probe], object(), policy=policy)
    with pytest.raises(ToolPolicyRefusal, match="owner-approved"):
        await port.call("mcp_probe", {"value": "x"}, "call-8")
