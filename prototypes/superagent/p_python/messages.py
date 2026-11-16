"""Transcript ↔ langchain-core messages for the turn loop.

The loop keeps conversation state as ``BaseMessage`` values and streams them
through ``BaseChatModel.astream``. Dict conversion for the scripted backend
lives in ``messages_to_scripted_dicts`` and is used only inside the
``BaseChatModel`` adapter.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from prototypes.superagent.contract import Transcript


def system_prompt(role: str, tool_names: list[str]) -> SystemMessage:
    tools = ",".join(tool_names)
    return SystemMessage(
        content=(
            f"role={role}\n"
            "Route or review only. Do not run shell commands or edit files.\n"
            f"tools={tools}"
        )
    )


def transcript_to_messages(
    history: Transcript,
    *,
    role: str,
    tool_names: list[str],
    memory_text: str,
    user_text: str,
) -> list[BaseMessage]:
    """Committed history plus this turn's memory and user text."""

    messages: list[BaseMessage] = [
        system_prompt(role, tool_names),
        HumanMessage(content=memory_text, additional_kwargs={"role": "memory"}),
    ]
    for entry in history.entries:
        if entry.kind == "user":
            messages.append(HumanMessage(content=entry.text))
        elif entry.kind in {"assistant", "token"}:
            messages.append(AIMessage(content=entry.text))
        elif entry.kind == "memory":
            messages.append(
                HumanMessage(content=entry.text, additional_kwargs={"role": "memory"})
            )
        elif entry.kind == "tool_call":
            messages.append(
                AIMessage(
                    content=entry.text,
                    additional_kwargs={"transcript_kind": "tool_call"},
                )
            )
        elif entry.kind in {"tool_result", "tool_refused"}:
            messages.append(
                ToolMessage(
                    content=entry.text,
                    tool_call_id=entry.kind,
                    name=entry.kind,
                    additional_kwargs={"transcript_kind": entry.kind},
                )
            )
    messages.append(HumanMessage(content=user_text))
    return messages


def messages_to_scripted_dicts(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """Flatten langchain messages into the scripted double's request shape.

    Only ``ScriptedLangChainChatModel`` calls this. Memory is tagged
    ``role=memory`` so ``message_texts`` still sees its text as a distinct
    piece. Committed tool rows may keep their transcript kind as the role;
    live tool results use ``tool``, matching the reference adapter.
    """

    rows: list[dict[str, Any]] = []
    for message in messages:
        extras = message.additional_kwargs or {}
        if extras.get("role") == "memory":
            rows.append({"role": "memory", "content": _content_str(message.content)})
            continue
        kind = extras.get("transcript_kind")
        if isinstance(message, SystemMessage):
            rows.append({"role": "system", "content": _content_str(message.content)})
        elif isinstance(message, HumanMessage):
            rows.append({"role": "user", "content": _content_str(message.content)})
        elif isinstance(message, AIMessage):
            rows.append(
                {
                    "role": kind or "assistant",
                    "content": _content_str(message.content),
                }
            )
        elif isinstance(message, ToolMessage):
            rows.append(
                {
                    "role": kind or "tool",
                    "name": message.name or (kind or "tool"),
                    "content": _content_str(message.content),
                }
            )
        else:
            rows.append(
                {
                    "role": getattr(message, "type", "user"),
                    "content": _content_str(message.content),
                }
            )
    return rows


def append_tool_exchange(
    messages: list[BaseMessage],
    *,
    name: str,
    call_text: str | None,
    result_text: str,
    refused: bool,
) -> None:
    """Extend the in-flight langchain message list after a tool round."""

    if refused:
        messages.append(
            ToolMessage(
                content=result_text,
                tool_call_id=f"refused:{name}",
                name=name,
            )
        )
        return
    assert call_text is not None
    messages.append(AIMessage(content=call_text))
    messages.append(
        ToolMessage(
            content=result_text,
            tool_call_id=name,
            name=name,
        )
    )


def _content_str(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for block in content:
            if isinstance(block, str):
                pieces.append(block)
            elif isinstance(block, dict) and "text" in block:
                pieces.append(str(block["text"]))
            else:
                pieces.append(str(block))
        return "".join(pieces)
    return str(content)
