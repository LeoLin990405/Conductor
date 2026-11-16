"""Ports and turn DTOs for the pure Super-agent P-lane loop.

These Protocols abstract the doubles injected into
``prototypes.superagent.p_python.adapter`` (credit gate, memory recall,
tool handlers, event subscriber) so the production loop stays free of
server and prototype imports.
"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from langchain_core.messages import BaseMessage

LoopEventKind = Literal[
    "text_delta",
    "thinking_delta",
    "tool_started",
    "tool_result",
    "tool_refused",
    "stop",
    "done",
]

TurnStatus = Literal["ready", "stopped"]


@dataclass(frozen=True)
class CreditVerdict:
    """Outcome of one credit check before a model round."""

    allowed: bool
    reason: str = ""


@dataclass
class LoopEvent:
    """One lockstep stream event the loop publishes through ``EventSink``."""

    kind: LoopEventKind
    text: str = ""
    tool_name: str = ""
    tool_call_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    message_id: str = ""


@dataclass
class TurnInput:
    """Everything the pure loop needs to run one turn.

    ``history_turns`` and ``history_chars`` cap how much of ``history`` is
    sent to the model (newest rounds first; tool call/result pairs stay
    together). ``offered_tool_names`` narrows the role's allow-list further;
    empty means "every tool object the ``ToolPort`` offers".
    """

    role: str
    system_prompt: str
    history: list[BaseMessage]
    user_text: str
    history_turns: int = 4
    history_chars: int = 48000
    offered_tool_names: Sequence[str] = ()


@dataclass
class TurnResult:
    """What one ``run_turn`` leaves for the caller to persist.

    ``new_messages`` starts with this turn's ``HumanMessage`` and keeps every
    ``AIMessage(tool_calls=...)`` next to its paired ``ToolMessage`` rows, so
    a checkpoint that appends it stays valid for the next provider call.
    ``stop_reason`` is empty when ``status == "ready"``; otherwise it is the
    credit denial text or ``"max_rounds"``.
    """

    status: TurnStatus
    stop_reason: str
    new_messages: list[BaseMessage]
    rounds: int = 0
    memory_text: str = ""


class CreditPort(Protocol):
    """Authorize (or deny) the next model round."""

    def authorize(self) -> Awaitable[CreditVerdict]: ...


class RecallPort(Protocol):
    """Return the already-rendered memory block for this turn (may be empty)."""

    def recall(self) -> Awaitable[str]: ...


class ToolPort(Protocol):
    """Tools offered to the model and the host that executes them."""

    def tool_objects(self) -> list[Any]: ...

    def call(
        self,
        name: str,
        args: Mapping[str, Any],
        call_id: str,
    ) -> Awaitable[str]: ...


class EventSink(Protocol):
    """Synchronous sink so each chunk can publish before the next model yield."""

    def emit(self, event: LoopEvent) -> None: ...
