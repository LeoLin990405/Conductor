"""Message assembly, content-block parsing, and history trimming for P.

System prompt carries the role text, tool names, and recalled memory as its
own section. History keeps ``AIMessage(tool_calls=...)`` paired with
``ToolMessage`` rows; a refused or failed call still gets ``status="error"``.
Trim never splits a tool call from its results, and never keeps a window over
``history_chars``.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, Literal
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

_TEXT_TYPES = frozenset({"text", "input_text", "output_text"})
_REASONING_TYPES = frozenset({"reasoning", "thinking", "redacted_thinking"})
_REASONING_KWARGS = frozenset({"reasoning_content", "reasoning"})
_RETAINED_TEXT_KEYS = ("text", "thinking", "reasoning", "data")

ToolStatus = Literal["success", "error"]


def memory_block(memory_text: str) -> str:
    """Format recalled memory as its own system-prompt section.

    PRD §4.5.2: memory is a separate section of the system prompt, injected
    each turn and never written into checkpoint history. Empty recall omits
    the section entirely.
    """

    text = (memory_text or "").strip()
    if not text:
        return ""
    return f"## Memory\n{text}"


def build_system_message(
    prompt: str,
    tool_names: Sequence[str],
    memory_text: str = "",
) -> SystemMessage:
    """Build the system message from ``prompts.py``, tool names, and memory.

    ``prompt`` is the static text from ``prompts.py`` (caller selects the role).
    Tool names and the memory section (when present) follow as separate parts.
    """

    parts = [prompt.rstrip()]
    tools = ",".join(tool_names)
    if tools:
        parts.append(f"tools={tools}")
    memory = memory_block(memory_text)
    if memory:
        parts.append(memory)
    return SystemMessage(content="\n\n".join(parts))


def tool_call_ai_message(
    *,
    name: str,
    args: Mapping[str, Any] | None = None,
    call_id: str | None = None,
    content: str = "",
    message_id: str | None = None,
) -> AIMessage:
    """One ``AIMessage`` declaring a single tool call."""

    tool_call_id = call_id or f"call-{uuid4().hex}"
    return AIMessage(
        content=content,
        tool_calls=[
            {
                "name": name,
                "args": dict(args or {}),
                "id": tool_call_id,
                "type": "tool_call",
            }
        ],
        id=message_id,
    )


def tool_result_message(
    *,
    name: str,
    call_id: str,
    content: str,
    status: ToolStatus = "success",
) -> ToolMessage:
    """Paired ``ToolMessage`` for one tool call id."""

    return ToolMessage(
        content=content,
        tool_call_id=call_id,
        name=name,
        status=status,
    )


def refused_tool_messages(
    *,
    name: str,
    args: Mapping[str, Any] | None = None,
    call_id: str | None = None,
    message_id: str | None = None,
) -> tuple[AIMessage, ToolMessage]:
    """AI tool call + error ``ToolMessage`` for a refused name."""

    ai = tool_call_ai_message(
        name=name,
        args=args,
        call_id=call_id,
        content="",
        message_id=message_id,
    )
    tool_call_id = str(ai.tool_calls[0]["id"])
    return ai, tool_result_message(
        name=name,
        call_id=tool_call_id,
        content=f"refused: {name}",
        status="error",
    )


def failed_tool_message(
    *,
    name: str,
    call_id: str,
    error: str,
) -> ToolMessage:
    """Error ``ToolMessage`` for a call that raised or had bad arguments."""

    return tool_result_message(
        name=name,
        call_id=call_id,
        content=error,
        status="error",
    )


def iter_content_deltas(content: Any) -> Iterator[tuple[str, str]]:
    """Yield ``(kind, text)`` for one chunk's content.

    ``kind`` is ``text`` or ``thinking``. Reasoning / thinking blocks never
    become answer text.
    """

    if content is None:
        return
    if isinstance(content, str):
        if content:
            yield "text", content
        return
    if isinstance(content, dict):
        yield from _iter_block(content)
        return
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                if block:
                    yield "text", block
            elif isinstance(block, dict):
                yield from _iter_block(block)
        return
    text = str(content)
    if text:
        yield "text", text


def _iter_block(block: dict[str, Any]) -> Iterator[tuple[str, str]]:
    kind = block.get("type")
    if kind in _TEXT_TYPES or (
        kind is None
        and "text" in block
        and "thinking" not in block
        and "reasoning" not in block
    ):
        text = block.get("text")
        if text:
            yield "text", str(text)
        return
    if (
        kind in _REASONING_TYPES
        or "thinking" in block
        or (kind == "reasoning" or "reasoning" in block)
        or "summary" in block
    ):
        thinking = block.get("thinking")
        if thinking is None:
            thinking = block.get("reasoning")
        if thinking is None:
            thinking = _summary_text(block.get("summary"))
        if thinking:
            yield "thinking", str(thinking)


def _summary_text(summary: Any) -> str:
    if summary is None:
        return ""
    if isinstance(summary, str):
        return summary
    if isinstance(summary, list):
        parts: list[str] = []
        for item in summary:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
        return "".join(parts)
    return ""


def content_text(content: Any) -> str:
    """Concatenate answer text blocks only (no reasoning)."""

    return "".join(text for kind, text in iter_content_deltas(content) if kind == "text")


def iter_message_deltas(message: Any) -> Iterator[tuple[str, str]]:
    """Yield text/thinking deltas from content, content_blocks, and kwargs.

    Provider streams often put chain-of-thought in ``additional_kwargs``
    (``reasoning_content``) or ``content_blocks`` while leaving ``content`` empty.

    Repeated identical blocks inside ``content`` are preserved (``ha`` + ``ha``
    stays ``haha``). Cross-source duplicates from ``content_blocks`` or kwargs
    are skipped so the same summary is not emitted twice.
    """

    seen: set[tuple[str, str]] = set()

    for kind, text in iter_content_deltas(getattr(message, "content", None)):
        if text:
            seen.add((kind, text))
            yield kind, text

    blocks = getattr(message, "content_blocks", None)
    content = getattr(message, "content", None)
    if blocks is not None and blocks is not content:
        for kind, text in iter_content_deltas(blocks):
            key = (kind, text)
            if text and key not in seen:
                seen.add(key)
                yield kind, text

    extras = getattr(message, "additional_kwargs", None) or {}
    if isinstance(extras, dict):
        for key in _REASONING_KWARGS:
            for text in _reasoning_kwarg_texts(extras.get(key)):
                item = ("thinking", text)
                if item not in seen:
                    seen.add(item)
                    yield item


def _reasoning_kwarg_texts(value: Any) -> Iterator[str]:
    """Extract thinking text from structured ``reasoning_content`` / ``reasoning``.

    OpenAI-style dicts use a ``summary`` list; never fall back to ``str(dict)``.
    """

    if value is None or value == "":
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        thinking = value.get("thinking")
        if thinking is None:
            thinking = value.get("reasoning")
        if thinking is None:
            thinking = _summary_text(value.get("summary"))
        if thinking:
            yield str(thinking)
            return
        for kind, text in iter_content_deltas(value):
            if text:
                yield text
        return
    if isinstance(value, list):
        for item in value:
            yield from _reasoning_kwarg_texts(item)


def build_model_messages(
    *,
    system_prompt: str,
    tool_names: Sequence[str],
    memory_text: str,
    history: list[BaseMessage],
    user_text: str,
    history_turns: int,
    history_chars: int,
) -> list[BaseMessage]:
    """System (prompt + tools + memory section) + trimmed history + user.

    Memory stays inside the system message so it is not checkpointed as a
    HumanMessage turn.
    """

    messages: list[BaseMessage] = [
        build_system_message(system_prompt, tool_names, memory_text),
    ]
    messages.extend(trim_history(history, history_turns, history_chars))
    messages.append(HumanMessage(content=user_text))
    return messages


def trim_history(
    history: list[BaseMessage],
    history_turns: int,
    history_chars: int,
) -> list[BaseMessage]:
    """Keep the last ``history_turns`` user turns within ``history_chars``.

    A turn starts at a ``HumanMessage`` (non-memory). Tool call / result pairs
    stay together: cuts land on turn boundaries only. A turn that still
    exceeds the char budget is dropped whole rather than kept over budget.
    """

    if not history:
        return []
    turns = _split_turns(history)
    if history_turns >= 0:
        turns = turns[-history_turns:] if history_turns else []
    while turns and _messages_chars(m for turn in turns for m in turn) > history_chars:
        turns = turns[1:]
    return [message for turn in turns for message in turn]


def _split_turns(history: list[BaseMessage]) -> list[list[BaseMessage]]:
    turns: list[list[BaseMessage]] = []
    current: list[BaseMessage] = []
    for message in history:
        if _is_turn_start(message) and current:
            turns.append(current)
            current = [message]
        else:
            current.append(message)
    if current:
        turns.append(current)
    return turns


def _is_turn_start(message: BaseMessage) -> bool:
    if not isinstance(message, HumanMessage):
        return False
    extras = message.additional_kwargs or {}
    return extras.get("role") != "memory"


def _messages_chars(messages: Iterable[BaseMessage]) -> int:
    """Count every retained character that will be sent back to the model."""

    total = 0
    for message in messages:
        total += _retained_content_chars(message.content)
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                total += len(str(call.get("name") or ""))
                total += len(str(call.get("args") or ""))
            extras = message.additional_kwargs or {}
            if isinstance(extras, dict):
                for key in _REASONING_KWARGS:
                    for text in _reasoning_kwarg_texts(extras.get(key)):
                        total += len(text)
    return total


def _retained_content_chars(content: Any) -> int:
    """Length of all text still present in a message body, including thinking."""

    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, dict):
        return _retained_block_chars(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, str):
                total += len(block)
            elif isinstance(block, dict):
                total += _retained_block_chars(block)
            elif block is not None:
                total += len(str(block))
        return total
    return len(str(content))


def _retained_block_chars(block: dict[str, Any]) -> int:
    total = 0
    for key in _RETAINED_TEXT_KEYS:
        value = block.get(key)
        if isinstance(value, str):
            total += len(value)
    total += len(_summary_text(block.get("summary")))
    return total


INTERRUPTED_TOOL_RESULT = "error: this tool call did not complete"


def prepare_history(history: list[BaseMessage]) -> list[BaseMessage]:
    """Skip unknown types, strip reasoning, and repair tool-call pairing.

    History may come from a PTC turn that stopped between the model step and
    the tool step (PTC repairs that with its own middleware on the next PTC
    turn). Providers reject an assistant tool call with no result and a tool
    result with no call, so an unanswered call gets an error ``ToolMessage``
    and an orphan result is dropped. The checkpoint itself is not rewritten.
    """

    # The provider layer's own strippers: one rule for what counts as
    # reasoning and when an assistant message is empty enough to drop, shared
    # with PTC's request path. Lazy, so importing this module stays light.
    from src.llms.reasoning_payload import strip_reasoning

    prepared: list[BaseMessage] = []
    for message in history:
        if not isinstance(message, (HumanMessage, AIMessage, ToolMessage, SystemMessage)):
            continue
        if isinstance(message, SystemMessage):
            # Caller's system prompt is added separately.
            continue
        if isinstance(message, AIMessage):
            stripped = strip_reasoning(message)
            if stripped is None:
                # Thinking only: an empty assistant message is a provider 400.
                continue
            message = stripped
        prepared.append(message)
    return repair_tool_pairs(prepared)


def repair_tool_pairs(history: list[BaseMessage]) -> list[BaseMessage]:
    """Make every ``AIMessage`` tool call, valid or invalid, answered right
    after it, exactly once."""

    repaired: list[BaseMessage] = []
    open_calls: dict[str, str] = {}

    def close_open_calls() -> None:
        for call_id, name in open_calls.items():
            repaired.append(
                tool_result_message(
                    name=name, call_id=call_id, content=INTERRUPTED_TOOL_RESULT, status="error"
                )
            )
        open_calls.clear()

    for message in history:
        if isinstance(message, ToolMessage):
            if message.tool_call_id in open_calls:
                open_calls.pop(message.tool_call_id)
                repaired.append(message)
            continue
        close_open_calls()
        repaired.append(message)
        if isinstance(message, AIMessage):
            # invalid_tool_calls count too: PTC's PatchToolCallsMiddleware
            # answers them, and provider formatters send them as tool calls.
            for call in [*(message.tool_calls or []), *(message.invalid_tool_calls or [])]:
                call_id = str(call.get("id") or "")
                if call_id:
                    open_calls[call_id] = str(call.get("name") or "")
    close_open_calls()
    return repaired
