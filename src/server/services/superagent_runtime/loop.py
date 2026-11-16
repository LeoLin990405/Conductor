"""Pure Super-agent P-lane turn loop.

The turn order is ported from ``prototypes.superagent.p_python.adapter``:
credit, then recall, then model/tool rounds with a credit check before every
later round, then done. Persistence is the caller's job (a LangGraph node in
production, the contract store in ``prototypes/superagent/production``).

What the prototype only got away with against a scripted double, and this
loop does for real providers:

- tool arguments arrive split across ``tool_call_chunks``; they are joined
  per chunk ``index`` and parsed once the round has finished streaming;
- one round can hold several tool calls, valid and malformed mixed; every
  call keeps the provider's chunk order in the ``AIMessage`` and in the
  ``ToolMessage`` feedback, and each call carries its own id and arguments
  so a malformed call can never stand in for a valid one;
- ``content`` can be a list of blocks; reasoning goes to ``thinking_delta``
  and never into the answer or the replayed history;
- rounds and history are capped.

Only ``langchain_core`` and the stdlib are imported, so the offline contract
suite can load this module without the server app.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig

from .messages import (
    build_model_messages,
    iter_message_deltas,
    prepare_history,
)
from .ports import (
    CreditPort,
    EventSink,
    LoopEvent,
    RecallPort,
    ToolPort,
    TurnInput,
    TurnResult,
)
from .tools import is_shell_or_file_tool, normalize_tool_name

MAX_ROUNDS_REASON = "max_rounds"
DEFAULT_CREDIT_REASON = "credit"

# Planner ruling 3: retry connection errors and 5xx twice (three attempts in
# total), no fallback model. A retry is only safe before anything reached the
# sink.
_STREAM_ATTEMPTS = 3

# Provider transport errors (httpx, openai, anthropic) do not subclass the
# stdlib ConnectionError. Matching by class name keeps this module free of
# provider imports.
_RETRYABLE_EXC_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ConnectionError",
        "InternalServerError",
        "NetworkError",
        "PoolTimeout",
        "ReadTimeout",
        "RemoteProtocolError",
        "ServiceUnavailableError",
        "TimeoutError",
        "TimeoutException",
        "TransportError",
        "WriteTimeout",
    }
)


@dataclass
class _ToolCallSlot:
    """One tool call of a round, assembled from its stream chunks.

    The id, name and raw arguments live together, so pairing never has to
    look a call up by id or name afterwards.
    """

    position: int
    raw_id: str = ""
    name: str = ""
    raw_args: Any = None
    parse_error: str = ""
    args_parts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedToolCall:
    """A round's tool call after id assignment and argument parsing."""

    id: str
    name: str
    args: dict[str, Any]
    error: str = ""

    def as_tool_call(self) -> dict[str, Any]:
        return {"name": self.name, "args": dict(self.args), "id": self.id, "type": "tool_call"}


class _RoundCollector:
    """Accumulates one model round from its stream, in provider order."""

    def __init__(self) -> None:
        self.merged: AIMessageChunk | None = None
        self.text_parts: list[str] = []
        self._slots: list[_ToolCallSlot] = []
        self._by_index: dict[Any, _ToolCallSlot] = {}

    def add(self, chunk: AIMessageChunk) -> None:
        if chunk.tool_call_chunks:
            for part in chunk.tool_call_chunks:
                self._add_tool_call_chunk(part)
        else:
            # A whole message (non-streaming fallback, or a model that emits
            # complete calls) has no chunks. LangChain has already split it
            # into valid and invalid lists, so explicit ``index`` values are
            # the only order left to recover.
            whole: list[tuple[int, int, Any, bool]] = []
            for pos, call in enumerate(chunk.tool_calls or []):
                whole.append((_index_of(call, pos), pos, call, True))
            offset = len(whole)
            for pos, call in enumerate(chunk.invalid_tool_calls or []):
                whole.append((_index_of(call, offset + pos), offset + pos, call, False))
            whole.sort(key=lambda item: (item[0], item[1]))
            for _index, _pos, call, valid in whole:
                self._add_whole_call(call, valid=valid)
        self.merged = chunk if self.merged is None else self.merged + chunk

    def _add_tool_call_chunk(self, part: Mapping[str, Any]) -> None:
        index = part.get("index")
        slot = self._by_index.get(index) if index is not None else None
        if slot is None and index is None and self._slots and self._continues_last(part):
            slot = self._slots[-1]
        if slot is None:
            slot = _ToolCallSlot(position=len(self._slots))
            self._slots.append(slot)
            if index is not None:
                self._by_index[index] = slot
        if part.get("id") and not slot.raw_id:
            slot.raw_id = str(part["id"])
        if part.get("name") and not slot.name:
            slot.name = str(part["name"])
        args = part.get("args")
        if isinstance(args, str):
            slot.args_parts.append(args)
        elif args is not None:
            slot.raw_args = args

    def _continues_last(self, part: Mapping[str, Any]) -> bool:
        """Whether an index-less fragment belongs to the previous call.

        Some OpenAI-compatible servers omit ``index`` and repeat the id, or
        the name with no id, on every fragment of one call.
        """

        last = self._slots[-1]
        part_id = str(part.get("id") or "")
        part_name = str(part.get("name") or "")
        if part_id:
            return part_id == last.raw_id
        if part_name:
            return not last.raw_id and part_name == last.name
        return True

    def _add_whole_call(self, call: Any, *, valid: bool) -> None:
        slot = _ToolCallSlot(position=len(self._slots))
        slot.raw_id = str(_field(call, "id") or "")
        slot.name = str(_field(call, "name") or "")
        slot.raw_args = _field(call, "args")
        if not valid:
            slot.parse_error = str(_field(call, "error") or "tool arguments are not valid JSON")
        self._slots.append(slot)

    def tool_calls(self, used_ids: set[str]) -> list[ParsedToolCall]:
        """Every call in provider order, with turn-unique ids and its own args.

        ``used_ids`` holds every tool-call id already in the transcript and is
        updated in place, so a provider that restarts its numbering each round
        cannot give two ToolMessages the same id.
        """

        parsed: list[ParsedToolCall] = []
        for slot in sorted(self._slots, key=lambda item: item.position):
            if not slot.name:
                # Nothing can execute a nameless call, and providers reject an
                # empty tool name when it is replayed.
                continue
            call_id = slot.raw_id
            if not call_id or call_id in used_ids:
                # A missing or repeated id would make two calls share one
                # ToolMessage. Never fall back to the tool name.
                call_id = new_tool_call_id()
            used_ids.add(call_id)
            raw = "".join(slot.args_parts) if slot.args_parts else slot.raw_args
            args, error = _parse_args(raw)
            if slot.parse_error:
                error = slot.parse_error
                args = args or {}
            parsed.append(ParsedToolCall(id=call_id, name=slot.name, args=args, error=error))
        return parsed


def new_tool_call_id() -> str:
    return f"call_{uuid4().hex}"


async def run_turn(
    chat: Any,
    turn: TurnInput,
    *,
    credit: CreditPort,
    recall: RecallPort,
    tools: ToolPort,
    sink: EventSink,
    callbacks: Sequence[Any] = (),
    max_rounds: int = 12,
    prepare_messages: Callable[[list[BaseMessage]], list[BaseMessage]] | None = None,
) -> TurnResult:
    """Run one turn: credit, recall, model/tool rounds, done.

    A credit denial before the first round returns before ``recall`` runs. A
    denial before a later round, or reaching ``max_rounds``, returns
    ``stopped`` with every message produced so far, so the caller can still
    checkpoint a valid transcript. Refused tools, tool exceptions and
    unparsable arguments become ``status="error"`` ToolMessages and the loop
    continues.

    ``prepare_messages`` rewrites a copy of the request on its way to the
    provider (wire-shape rules such as DeepSeek's thinking block on every
    tool-calling turn); what the loop returns and checkpoints is untouched.
    """

    new_messages: list[BaseMessage] = [HumanMessage(content=turn.user_text)]

    verdict = await _maybe_await(credit.authorize())
    if not verdict.allowed:
        return _stopped(sink, verdict.reason or DEFAULT_CREDIT_REASON, new_messages, 0, "")

    memory_text = str(await _maybe_await(recall.recall()) or "")
    offered = list(tools.tool_objects())
    allowed = _allowed_names(offered, turn.offered_tool_names)
    messages = build_model_messages(
        system_prompt=turn.system_prompt,
        tool_names=sorted(allowed),
        memory_text=memory_text,
        history=prepare_history(list(turn.history)),
        user_text=turn.user_text,
        history_turns=turn.history_turns,
        history_chars=turn.history_chars,
    )
    bound = chat.bind_tools(offered) if offered else chat
    config = RunnableConfig(callbacks=list(callbacks), tags=["nostream"])

    used_ids = {
        str(call.get("id"))
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls or []
        if call.get("id")
    }
    rounds = 0
    while True:
        if rounds >= max_rounds:
            return _stopped(sink, MAX_ROUNDS_REASON, new_messages, rounds, memory_text)
        if rounds > 0:
            verdict = await _maybe_await(credit.authorize())
            if not verdict.allowed:
                return _stopped(
                    sink, verdict.reason or DEFAULT_CREDIT_REASON, new_messages, rounds, memory_text
                )

        rounds += 1
        message_id = f"msg-{uuid4().hex}"
        outgoing = prepare_messages(list(messages)) if prepare_messages else messages
        collected = await _stream_round(bound, outgoing, config=config, sink=sink, message_id=message_id)
        text = "".join(collected.text_parts)
        calls = collected.tool_calls(used_ids)

        if not calls:
            if text:
                ai = _ai_message(collected, text, [], message_id)
                messages.append(ai)
                new_messages.append(ai)
            sink.emit(LoopEvent(kind="done", message_id=message_id))
            return TurnResult(
                status="ready",
                stop_reason="",
                new_messages=new_messages,
                rounds=rounds,
                memory_text=memory_text,
            )

        ai = _ai_message(collected, text, [call.as_tool_call() for call in calls], message_id)
        messages.append(ai)
        new_messages.append(ai)
        for call in calls:
            tool_message = await _run_tool_call(call, allowed=allowed, tools=tools, sink=sink, message_id=message_id)
            messages.append(tool_message)
            new_messages.append(tool_message)


async def _run_tool_call(
    call: ParsedToolCall,
    *,
    allowed: Collection[str],
    tools: ToolPort,
    sink: EventSink,
    message_id: str,
) -> ToolMessage:
    """Execute (or refuse) one call and return its paired ToolMessage."""

    if is_shell_or_file_tool(call.name) or normalize_tool_name(call.name) not in allowed:
        sink.emit(
            LoopEvent(
                kind="tool_refused",
                text=call.name,
                tool_name=call.name,
                tool_call_id=call.id,
                data={"args": dict(call.args)},
                message_id=message_id,
            )
        )
        return ToolMessage(
            content=f"refused: {call.name}",
            tool_call_id=call.id,
            name=call.name,
            status="error",
        )

    sink.emit(
        LoopEvent(
            kind="tool_started",
            tool_name=call.name,
            tool_call_id=call.id,
            data={"args": dict(call.args)},
            message_id=message_id,
        )
    )
    if call.error:
        result, status = f"error: invalid tool arguments: {call.error}", "error"
    else:
        try:
            result = str(await _maybe_await(tools.call(call.name, call.args, call.id)))
            status = "success"
        except Exception as exc:  # noqa: BLE001 - the model sees the failure and may retry
            result, status = f"error: {type(exc).__name__}: {exc}", "error"
    sink.emit(
        LoopEvent(
            kind="tool_result",
            text=result,
            tool_name=call.name,
            tool_call_id=call.id,
            data={"content": result, "status": status},
            message_id=message_id,
        )
    )
    return ToolMessage(content=result, tool_call_id=call.id, name=call.name, status=status)


async def _stream_round(
    bound: Any,
    messages: list[BaseMessage],
    *,
    config: RunnableConfig,
    sink: EventSink,
    message_id: str,
) -> _RoundCollector:
    """Stream one round; each delta reaches the sink before the next chunk is pulled."""

    for attempt in range(_STREAM_ATTEMPTS):
        collected = _RoundCollector()
        emitted = False
        try:
            stream: AsyncIterator[Any] = bound.astream(messages, config=config)
            async for raw in stream:
                chunk = _as_ai_chunk(raw)
                if chunk is None:
                    continue
                for kind, text in iter_message_deltas(chunk):
                    if not text:
                        continue
                    emitted = True
                    if kind == "thinking":
                        sink.emit(LoopEvent(kind="thinking_delta", text=text, message_id=message_id))
                    else:
                        collected.text_parts.append(text)
                        sink.emit(LoopEvent(kind="text_delta", text=text, message_id=message_id))
                if chunk.tool_call_chunks or chunk.tool_calls or chunk.invalid_tool_calls:
                    emitted = True
                collected.add(chunk)
            return collected
        except Exception as exc:
            if emitted or attempt + 1 >= _STREAM_ATTEMPTS or not is_retryable_error(exc):
                raise
    raise AssertionError("unreachable")  # pragma: no cover


def is_retryable_error(exc: BaseException) -> bool:
    """Connection errors and 5xx responses, anywhere in the cause chain."""

    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if any(cls.__name__ in _RETRYABLE_EXC_NAMES for cls in type(current).mro()):
            return True
        for status in (
            getattr(current, "status_code", None),
            getattr(current, "status", None),
            getattr(getattr(current, "response", None), "status_code", None),
        ):
            if isinstance(status, int) and 500 <= status < 600:
                return True
        for linked in (current.__cause__, current.__context__):
            if isinstance(linked, BaseException):
                stack.append(linked)
    return False


def _stopped(
    sink: EventSink,
    reason: str,
    new_messages: list[BaseMessage],
    rounds: int,
    memory_text: str,
) -> TurnResult:
    sink.emit(LoopEvent(kind="stop", text=reason))
    return TurnResult(
        status="stopped",
        stop_reason=reason,
        new_messages=new_messages,
        rounds=rounds,
        memory_text=memory_text,
    )


def _ai_message(
    collected: _RoundCollector,
    text: str,
    tool_calls: list[dict[str, Any]],
    message_id: str,
) -> AIMessage:
    """Answer text only: reasoning is streamed but never replayed to the model."""

    merged = collected.merged
    kwargs: dict[str, Any] = {}
    if merged is not None:
        if merged.usage_metadata:
            kwargs["usage_metadata"] = merged.usage_metadata
        if merged.response_metadata:
            kwargs["response_metadata"] = dict(merged.response_metadata)
    return AIMessage(content=text, tool_calls=tool_calls, id=message_id, **kwargs)


def _allowed_names(offered: Sequence[Any], narrowed: Sequence[str]) -> frozenset[str]:
    names = {normalize_tool_name(_tool_object_name(item)) for item in offered}
    if narrowed:
        names &= {normalize_tool_name(name) for name in narrowed}
    return frozenset(name for name in names if not is_shell_or_file_tool(name))


def _tool_object_name(definition: Any) -> str:
    if isinstance(definition, str):
        name: Any = definition
    elif isinstance(definition, Mapping):
        name = definition.get("name")
        if name is None and isinstance(definition.get("function"), Mapping):
            name = definition["function"].get("name")
    else:
        name = getattr(definition, "name", None)
    if not isinstance(name, str) or not name:
        raise ValueError(f"tool definition has no name: {definition!r}")
    return name


def _parse_args(raw: Any) -> tuple[dict[str, Any], str]:
    """Strict JSON: a partially streamed object is an error, not a guess."""

    if raw is None:
        return {}, ""
    if isinstance(raw, Mapping):
        return dict(raw), ""
    if isinstance(raw, str):
        if not raw.strip():
            return {}, ""
        try:
            value = json.loads(raw, parse_constant=_reject_constant)
        except json.JSONDecodeError as exc:
            return {}, f"not valid JSON ({exc.msg})"
        except ValueError as exc:
            return {}, str(exc)
        if isinstance(value, dict):
            return value, ""
        return {}, "arguments must be a JSON object"
    return {}, f"unsupported argument type {type(raw).__name__}"


def _reject_constant(name: str) -> Any:
    # NaN and Infinity are not JSON; Python's parser accepts them by default.
    raise ValueError(f"not valid JSON ({name} is not a JSON value)")


def _field(call: Any, name: str) -> Any:
    if isinstance(call, Mapping):
        return call.get(name)
    return getattr(call, name, None)


def _index_of(call: Any, default: int) -> int:
    index = _field(call, "index")
    return index if isinstance(index, int) else default


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _as_ai_chunk(raw: Any) -> AIMessageChunk | None:
    if isinstance(raw, AIMessageChunk):
        return raw
    message = getattr(raw, "message", None)
    if isinstance(message, AIMessageChunk):
        return message
    if isinstance(raw, AIMessage):
        return AIMessageChunk(
            content=raw.content,
            tool_calls=list(raw.tool_calls or []),
            invalid_tool_calls=list(raw.invalid_tool_calls or []),
            id=raw.id,
            additional_kwargs=dict(raw.additional_kwargs or {}),
            usage_metadata=raw.usage_metadata,
            response_metadata=dict(raw.response_metadata or {}),
        )
    return None
