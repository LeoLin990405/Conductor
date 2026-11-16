"""Production ports for the Super-agent P lane.

The loop in ``loop.py`` only sees the Protocols in ``ports.py``. These classes
bind them to what a server run already owns: the run's credit-gate lease, the
memory block the PTC prelude rendered, the role's autonomy ``@tool`` objects
with their ``AutonomyToolHost``, and LangGraph's custom stream writer, so the
lane reuses the existing admission, SSE and checkpoint pipeline unchanged.

Not imported by ``superagent_runtime/__init__.py``: the pure core and the
offline contract suite must load without pulling in server modules.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .ports import CreditVerdict, LoopEvent

logger = logging.getLogger(__name__)


class LeaseCreditPort:
    """``CreditPort`` over the run's ``CreditGateState`` (or None).

    Mirrors ``CreditGateMiddleware.abefore_model`` for the main lane, except
    that a stop becomes a denied verdict instead of ``interrupt()``: a hidden
    Router/Watchdog thread has nobody to resume a pause (Planner ruling 1).
    ``None`` is the OSS shape where platform gating is inactive, so every
    round is allowed.
    """

    def __init__(self, state: Any | None) -> None:
        self._state = state

    async def authorize(self) -> CreditVerdict:
        state = self._state
        if state is None:
            return CreditVerdict(True)
        lease = state.lease
        # One family-spend read per boundary, as the middleware does, so the
        # stop decision and the log line agree on the same number.
        spent = lease.spend()
        if not lease.should_stop(spent):
            return CreditVerdict(True)
        from src.server.services.credit_gate import DEFAULT_STOP_MESSAGE

        denial = lease.denial or {}
        logger.info(
            "[SUPERAGENT_P] credit stop run=%s spent=%.2f ceiling=%.2f",
            getattr(state, "run_ref", ""),
            spent,
            getattr(lease, "ceiling_credits", 0.0),
        )
        return CreditVerdict(False, denial.get("message") or DEFAULT_STOP_MESSAGE)


class PrecomputedRecall:
    """``RecallPort`` returning the block the PTC prelude already selected.

    The prelude runs ``build_turn_memory_block`` and audits the injection with
    ``record_memory_injection``; recalling again here would double the audit
    and could select a different block than the one recorded.
    """

    def __init__(self, text: str | None) -> None:
        self._text = text or ""

    async def recall(self) -> str:
        return self._text


class ToolPolicyRefusal(Exception):
    """A memory tool-denial policy blocked this call; the model sees why."""


class HostToolPort:
    """``ToolPort`` over the role's autonomy tools and its tool host.

    The tools read their host from ``config["configurable"]["autonomy_host"]``
    (``src/tools/autonomy/tools.py``), which is how the PTC graph hands it to
    them too; the host is the only thing that writes autonomy decisions.

    ``policy`` is the run's frozen ``MemoryToolPolicySnapshot``. PTC enforces
    it per call in ``MemoryToolPolicyMiddleware``; P has no middleware, so the
    same refusal happens here, fail-closed when the policy could not load.
    """

    def __init__(self, tools: Iterable[Any], host: Any, *, policy: Any) -> None:
        # Required on purpose: a port built without a policy must not run
        # tools unguarded, and None is read as "policy unavailable" (deny all).
        self._tools = list(tools)
        self._by_name = {str(tool.name): tool for tool in self._tools}
        self._stamps: dict[str, dict[str, Any]] = {}
        for tool in self._tools:
            metadata = getattr(tool, "metadata", None)
            stamp = metadata.get("direct_mcp") if isinstance(metadata, Mapping) else None
            if isinstance(stamp, Mapping):
                self._stamps[str(tool.name)] = dict(stamp)
        self._host = host
        self._policy = policy

    def tool_objects(self) -> list[Any]:
        # The real @tool objects, so bind_tools sends their full JSON schema.
        return list(self._tools)

    async def call(self, name: str, args: Mapping[str, Any], call_id: str) -> str:
        from .tools import normalize_tool_name

        tool = self._by_name.get(name) or self._by_name.get(normalize_tool_name(name))
        if tool is None:
            # The loop refuses names off the role list before calling; this
            # is the backstop if a caller skips that check.
            raise KeyError(f"tool {name!r} is not offered on this turn")
        policy = self._policy
        tool_name = str(tool.name)
        if policy is None or policy.blocks(tool_name, self._stamps.get(tool_name)):
            if policy is None or getattr(policy, "deny_all", False):
                raise ToolPolicyRefusal(
                    "memory tool policy is unavailable, so tool execution is blocked. "
                    "Retry when policy access is restored."
                )
            raise ToolPolicyRefusal(
                "an owner-approved memory constraint blocks this tool. "
                "Use an allowed tool or ask the owner to update the approved constraint."
            )
        result = await tool.ainvoke(
            dict(args),
            config={"configurable": {"autonomy_host": self._host}},
        )
        return _tool_text(result)


def provider_wire_adapter(chat: Any) -> Callable[[list[Any]], list[Any]] | None:
    """Per-route request rewrite the PTC ``ReasoningCompatibilityMiddleware`` did.

    P never replays reasoning, which is right for every provider except one
    that rejects a tool-calling turn with no thinking block (DeepSeek, the
    default Router model): such a route gets an empty thinking block on every
    tool-calling turn, plus DeepSeek's native history shape. ``None`` when the
    route needs nothing, so other providers see the history unchanged.
    """

    metadata = getattr(chat, "metadata", None)
    route = metadata.get("provider_route") if isinstance(metadata, Mapping) else None
    if not isinstance(route, str) or not route:
        return None
    from src.llms.reasoning_lineage import tool_turns_need_reasoning
    from src.llms.reasoning_payload import deepseek_native_payload, pad_tool_turn

    pad = tool_turns_need_reasoning(route)
    head = route.split("@", 1)[0]
    deepseek = head == "deepseek" or head.startswith("deepseek-")
    if not (pad or deepseek):
        return None

    def adapt(messages: list[Any]) -> list[Any]:
        from langchain_core.messages import AIMessage

        adapted: list[Any] = []
        for message in messages:
            if isinstance(message, AIMessage):
                if pad:
                    message = pad_tool_turn(message)
                if deepseek:
                    message = deepseek_native_payload(message)
                    if message is None:
                        continue
            adapted.append(message)
        return adapted

    return adapt


def _tool_text(result: Any) -> str:
    content = getattr(result, "content", result)
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(content)


class NativeEventSink:
    """``EventSink`` writing the ``native_event`` payloads the producer reads.

    ``RunSSEProducer`` already translates ``native_event`` custom writes into
    ``message_chunk`` / ``tool_calls`` / ``tool_call_result`` frames for the
    native lane (``sse_producer.py`` native_event branch), so the P lane adds
    no event type and no producer change. ``stop`` and ``done`` have no
    producer translation (it drops unknown kinds); the stop reason reaches
    the transcript as a message instead, and the run's end is the producer's
    own ``credit_usage`` plus the executor's ``run_end``.
    """

    def __init__(self, writer: Callable[[Any], None]) -> None:
        self._writer = writer

    def emit(self, event: LoopEvent) -> None:
        payload = native_event_payload(event)
        if payload is not None:
            self._writer({"type": "native_event", "payload": payload})


def native_event_payload(event: LoopEvent) -> dict[str, Any] | None:
    """The producer-facing payload for one loop event, or None to drop it."""
    kind = event.kind
    if kind in ("text_delta", "thinking_delta"):
        if not event.text:
            return None
        return {
            "kind": kind,
            "text": event.text,
            # Shared with the round's final AIMessage id so the producer's
            # complete-message dedup swallows the duplicate.
            "message_id": event.message_id,
        }
    if kind == "tool_started":
        return {
            "kind": "tool_started",
            "tool_name": event.tool_name,
            "tool_call_id": event.tool_call_id,
            "data": {"args": dict(event.data.get("args") or {})},
        }
    if kind == "tool_result":
        return {
            "kind": "tool_result",
            "tool_name": event.tool_name,
            "tool_call_id": event.tool_call_id,
            "data": {
                "content": str(event.data.get("content") or event.text or ""),
                "status": str(event.data.get("status") or "success"),
            },
        }
    if kind == "tool_refused":
        # The producer has no refusal frame; a refusal is an error result,
        # matching the ToolMessage the loop checkpoints for it.
        return {
            "kind": "tool_result",
            "tool_name": event.tool_name,
            "tool_call_id": event.tool_call_id,
            "data": {
                "content": f"refused: {event.tool_name}",
                "status": "error",
            },
        }
    return None
