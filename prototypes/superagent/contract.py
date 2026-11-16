"""SuperAgent adapter contract and the doubles the contract tests inject.

The product Router still boots a full agent graph. These prototypes replace
that path with a small adapter so later implementations can be compared on
the same behaviors: restart from a Postgres-shaped store, the exact autonomy
tool list for the role, scripted tool calls with durable arguments and
results, a durable transcript, ordered streaming, a credit-gate stop, and
memory that is already in the model request.

Nothing here opens a network connection or a shell. The scripted model is the
only chat backend the tests allow. Role tools run through ``FAKE_TOOL_HANDLERS``
and never through the product host.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from src.tools.autonomy.tools import AUTONOMY_ROUTER_TOOLS, AUTONOMY_WATCHDOG_TOOLS

# Same sentence the product gate uses when a denial arrives without copy.
CREDIT_STOP_TEXT = "Stopped by the credit gate."

# Names that must never be executed, even if a model script asks for them.
# ``autonomy_read`` is a record read, not a file read, so it is not in this set.
# The capitalized names are the built-in tools of native agent CLIs (Claude
# Code and its peers): shell, file, notebook, web, and subagent access.
# Matching is case-insensitive; see ``is_shell_or_file_tool``.
SHELL_OR_FILE_TOOLS = frozenset(
    {
        "apply_patch",
        "bash",
        "computer",
        "delete_file",
        "edit_file",
        "exec",
        "execute",
        "execute_code",
        "read_file",
        "run_terminal_cmd",
        "sh",
        "shell",
        "str_replace",
        "str_replace_editor",
        "write_file",
        "Bash",
        "Edit",
        "Glob",
        "Grep",
        "LS",
        "MultiEdit",
        "NotebookEdit",
        "Read",
        "Task",
        "WebFetch",
        "WebSearch",
        "Write",
    }
)

_SHELL_OR_FILE_FOLDED = frozenset(name.casefold() for name in SHELL_OR_FILE_TOOLS)

# ``mcp__<server>__<tool>`` is how MCP-backed CLIs name a server's tools.
_MCP_PREFIX = re.compile(r"^mcp__[^_].*?__(?=[^_])")


def normalize_tool_name(name: str) -> str:
    """Strip an MCP ``mcp__<server>__`` prefix. Case is kept."""

    return _MCP_PREFIX.sub("", name, count=1)


def is_shell_or_file_tool(name: str) -> bool:
    """True for a shell, file, web, or subagent tool, in any letter case.

    ``Bash``, ``BASH``, and ``mcp__fs__write_file`` all match.
    """

    return normalize_tool_name(name).casefold() in _SHELL_OR_FILE_FOLDED


def offered_tool_name(definition: Any) -> str:
    """Name of one tool definition a runtime offered the model.

    Accepts a bare name, ``{"name": ...}`` (Anthropic and MCP shape),
    ``{"function": {"name": ...}}`` (OpenAI shape), or an object with a
    ``name`` attribute (a LangChain tool). The MCP prefix is stripped.
    """

    if isinstance(definition, str):
        name = definition
    elif isinstance(definition, Mapping):
        if "name" in definition:
            name = definition["name"]
        elif isinstance(definition.get("function"), Mapping):
            name = definition["function"].get("name")
        else:
            name = None
    else:
        name = getattr(definition, "name", None)
    if not isinstance(name, str) or not name:
        raise ValueError(f"tool definition has no name: {definition!r}")
    return normalize_tool_name(name)


_ROLE_ALIASES = {
    "router": "router",
    "supervisor": "router",
    "watchdog": "watchdog",
}


def canonical_role(role: str) -> str:
    """Map a caller's role word onto ``router`` or ``watchdog``.

    The live thread role is ``supervisor``; the prototype contract also
    accepts ``router`` so tests can name the tool set directly.
    """

    key = role.strip().lower()
    try:
        return _ROLE_ALIASES[key]
    except KeyError as exc:
        raise ValueError(f"unknown superagent role: {role}") from exc


def autonomy_tool_names(role: str) -> list[str]:
    """Tool names bound for ``role``, in the product list's order."""

    canonical = canonical_role(role)
    tools = AUTONOMY_ROUTER_TOOLS if canonical == "router" else AUTONOMY_WATCHDOG_TOOLS
    return [tool.name for tool in tools]


def scripted_tool_call_text(tool_name: str, arguments: Mapping[str, Any]) -> str:
    """Canonical transcript text for one successful fake tool call.

    Arguments are JSON-serializable. The text is what a restarted adapter
    reads back, so every implementation stores this string rather than a
    private encoding of the same call.
    """

    return json.dumps(
        {"arguments": dict(arguments), "name": tool_name},
        ensure_ascii=False,
        sort_keys=True,
    )


def scripted_tool_result(tool_name: str, arguments: Mapping[str, Any]) -> str:
    """Deterministic success payload for a fake Router or watchdog tool.

    The payload echoes the arguments the model supplied. It does not call
    the product tool, open a shell, or write a file.
    """

    return json.dumps(
        {"arguments": dict(arguments), "ok": True, "tool": tool_name},
        ensure_ascii=False,
        sort_keys=True,
    )


_TEXT_BLOCKS = frozenset({"text", "input_text", "output_text"})
_RESULT_BLOCKS = frozenset({"tool_result", "function_call_output"})


def content_texts(content: Any) -> list[str]:
    """Text pieces of one message ``content``, in order.

    ``content`` is a string, or a list of blocks in Anthropic or OpenAI
    shape: ``text`` (also ``input_text`` and ``output_text``), ``tool_use``
    (normalized to ``scripted_tool_call_text(name, input)``), and
    ``tool_result`` / ``function_call_output`` (its content, recursively).
    A bare string in the list is one piece. Any other block raises.
    """

    if content is None:
        return []
    if isinstance(content, str):
        return [content]
    pieces: list[str] = []
    for block in content:
        if isinstance(block, str):
            pieces.append(block)
            continue
        if not isinstance(block, Mapping):
            raise ValueError(f"unsupported content block: {block!r}")
        kind = block.get("type")
        if kind in _TEXT_BLOCKS:
            pieces.append(str(block["text"]))
        elif kind == "tool_use":
            pieces.append(
                scripted_tool_call_text(str(block["name"]), block.get("input") or {})
            )
        elif kind in _RESULT_BLOCKS:
            pieces.extend(content_texts(block.get("content", block.get("output"))))
        else:
            raise ValueError(f"unsupported content block type: {kind!r}")
    return pieces


def message_texts(message: Mapping[str, Any]) -> list[str]:
    """Text pieces of one model message: its content, then OpenAI ``tool_calls``.

    Each ``tool_calls`` entry (``{"function": {"name", "arguments"}}``, where
    ``arguments`` may be a JSON string) becomes
    ``scripted_tool_call_text(name, arguments)``.
    """

    pieces = content_texts(message.get("content"))
    for call in message.get("tool_calls") or []:
        function = call.get("function") or call
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            arguments = json.loads(arguments) if arguments.strip() else {}
        pieces.append(scripted_tool_call_text(str(function["name"]), arguments))
    return pieces


def _fake_handler(tool_name: str) -> Callable[[Mapping[str, Any]], str]:
    def handle(arguments: Mapping[str, Any]) -> str:
        return scripted_tool_result(tool_name, arguments)

    handle.__name__ = tool_name
    return handle


# One pure handler per Router or watchdog tool. Adapters dispatch permitted
# model requests here, then continue the scripted model with the result.
FAKE_TOOL_HANDLERS: dict[str, Callable[[Mapping[str, Any]], str]] = {
    name: _fake_handler(name)
    for name in dict.fromkeys(
        [*autonomy_tool_names("router"), *autonomy_tool_names("watchdog")]
    )
}


@dataclass(frozen=True)
class CreditDecision:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class MemoryHit:
    text: str
    memory_id: str = ""


@dataclass
class ScriptedChunk:
    """One scripted model yield: assistant text, or one tool request.

    A chunk is text or a tool request. When both are set, the adapter emits
    the text and then handles the tool. ``tool_args`` is the argument object
    the fake handler receives on success.
    """

    text: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelCall:
    """The request the scripted model actually saw, copied at call time.

    ``tools`` is the normalized name of every tool definition the runtime
    put in that request, in the order offered, or ``None`` when the request
    carried no tool list at all. ``tool_definitions`` keeps the raw shapes.
    """

    messages: list[dict[str, Any]]
    tools: list[str] | None = None
    tool_definitions: list[Any] | None = None

    def message_texts(self) -> list[list[str]]:
        """``message_texts`` for each message, in request order."""

        return [message_texts(message) for message in self.messages]

    def texts(self) -> list[str]:
        """Every text piece of the request, flattened. Tests compare these."""

        return [piece for pieces in self.message_texts() for piece in pieces]


@dataclass(frozen=True)
class StreamEvent:
    seq: int
    kind: str
    text: str


@dataclass(frozen=True)
class TranscriptEntry:
    seq: int
    kind: str
    text: str


@dataclass
class Transcript:
    session_id: str
    entries: list[TranscriptEntry]

    def text(self) -> str:
        return "\n".join(entry.text for entry in self.entries if entry.text)

    def kinds(self) -> list[str]:
        return [entry.kind for entry in self.entries]


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    user_id: str
    role: str
    status: str


@dataclass(frozen=True)
class RuntimeContext:
    """What the harness hands an adapter besides the five doubles.

    ``scratch_dir`` is an empty directory owned by this one adapter instance.
    It is the only place an adapter (or its runtime) may write while serving
    ``start`` and ``turn``. A restarted adapter gets a fresh, empty one.

    ``bridge_path`` is a unix socket the harness serves for this adapter's
    doubles (model, store, credit gate, memory, subscriber). An out-of-process
    runtime speaks the JSON-lines protocol in ``prototypes.superagent.bridge``
    to it. An in-process adapter may ignore it.
    """

    scratch_dir: Path
    bridge_path: str


class ScriptedFakeChatModel:
    """Chat stand-in that yields a script and records the prompt it was given.

    ``provider_name`` stays ``scripted`` so an adapter can refuse anything
    that would talk to a hosted model. ``prefix_lengths`` records how many
    subscriber events were already published before each yield, which is how
    the streaming test checks that the adapter does not buffer the script.

    Each ``stream`` call is one model round. The round stops after it yields
    a tool request so the adapter can run the fake handler and call
    ``stream`` again with that result in ``messages``. Text-only scripts
    stay a single round. ``rounds`` keeps the chunks each call actually
    yielded. ``calls[i].tools`` is the tool list the runtime offered on that
    call, which is what the tool-set test compares, not ``tool_names``.
    """

    provider_name = "scripted"

    def __init__(
        self,
        script: Sequence[str | ScriptedChunk] | None = None,
        *,
        trace: list[str] | None = None,
        subscriber: EventSubscriber | None = None,
    ) -> None:
        chunks: list[ScriptedChunk] = []
        for item in script or []:
            if isinstance(item, str):
                chunks.append(ScriptedChunk(text=item))
            else:
                chunks.append(
                    ScriptedChunk(
                        text=item.text,
                        tool_name=item.tool_name,
                        tool_args=dict(item.tool_args),
                    )
                )
        self.script = tuple(chunks)
        self.trace = trace
        self.subscriber = subscriber
        self.calls: list[ModelCall] = []
        self.prefix_lengths: list[int] = []
        self.rounds: list[tuple[ScriptedChunk, ...]] = []
        self._cursor = 0
        self._round_open = False

    def open_round(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Any] | None = None,
    ) -> None:
        """Start one model round. ``stream`` and the bridge both use this."""

        if self.trace is not None:
            self.trace.append("model")
        definitions = None if tools is None else [copy.deepcopy(t) for t in tools]
        names = None if tools is None else [offered_tool_name(t) for t in tools]
        self.calls.append(
            ModelCall(
                messages=[dict(message) for message in messages],
                tools=names,
                tool_definitions=definitions,
            )
        )
        # Stored before each yield. Breaking out of ``async for`` after a tool
        # request does not reliably run a finally on this generator.
        self.rounds.append(())
        self._round_open = True

    def next_chunk(self) -> ScriptedChunk | None:
        """The next chunk of the open round, or ``None`` when it has ended.

        The subscriber's event count is recorded before the chunk is handed
        out. That is the per-chunk lockstep the streaming test reads.
        """

        if not self._round_open or self._cursor >= len(self.script):
            self._round_open = False
            return None
        chunk = self.script[self._cursor]
        self._cursor += 1
        if self.subscriber is not None:
            self.prefix_lengths.append(len(self.subscriber.events))
        self.rounds[-1] = (*self.rounds[-1], chunk)
        if chunk.tool_name:
            self._round_open = False
        return chunk

    async def stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Any] | None = None,
    ) -> AsyncIterator[ScriptedChunk]:
        """One model round. ``tools`` is every tool definition offered."""

        self.open_round(messages, tools)
        while (chunk := self.next_chunk()) is not None:
            yield chunk


class FakePostgresStore:
    """Process-local tables with commit and rollback.

    Rows live on this object, not on the adapter. A second adapter built
    with the same store is the contract's restart: nothing in the new
    process memory is required to read the session back.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._transcript: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self.commits = 0

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._sessions.get(session_id)
        return copy.deepcopy(row) if row is not None else None

    def transcript_rows(self, session_id: str) -> list[dict[str, Any]]:
        rows = [row for row in self._transcript if row["session_id"] == session_id]
        return copy.deepcopy(sorted(rows, key=lambda row: row["seq"]))

    def event_rows(self, session_id: str) -> list[dict[str, Any]]:
        rows = [row for row in self._events if row["session_id"] == session_id]
        return copy.deepcopy(sorted(rows, key=lambda row: row["seq"]))

    def transaction(self) -> _Transaction:
        return _Transaction(self)


class _Transaction:
    def __init__(self, store: FakePostgresStore) -> None:
        self._store = store
        self._sessions = copy.deepcopy(store._sessions)
        self._transcript = copy.deepcopy(store._transcript)
        self._events = copy.deepcopy(store._events)
        self._closed = False

    def __enter__(self) -> _Transaction:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        if self._closed:
            return False
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._sessions.get(session_id)
        return copy.deepcopy(row) if row is not None else None

    def upsert_session(self, row: Mapping[str, Any]) -> None:
        self._sessions[str(row["session_id"])] = copy.deepcopy(dict(row))

    def append_transcript(self, row: Mapping[str, Any]) -> None:
        self._transcript.append(copy.deepcopy(dict(row)))

    def append_event(self, row: Mapping[str, Any]) -> None:
        self._events.append(copy.deepcopy(dict(row)))

    def commit(self) -> None:
        if self._closed:
            raise RuntimeError("transaction already closed")
        self._store._sessions = self._sessions
        self._store._transcript = self._transcript
        self._store._events = self._events
        self._store.commits += 1
        self._closed = True

    def rollback(self) -> None:
        self._closed = True


class FakeCreditGate:
    """Allow or deny a turn, before the first model round and before each later one.

    ``allowed_checks`` lets the first N checks pass and denies every check
    after that, which is how a turn is stopped after a tool call. ``None``
    means every check returns ``allowed``. A denial uses the product stop
    sentence so adapters surface the gate's reason instead of inventing a
    second message.
    """

    def __init__(
        self,
        *,
        allowed: bool = True,
        allowed_checks: int | None = None,
        trace: list[str] | None = None,
    ) -> None:
        self.allowed = allowed
        self.allowed_checks = allowed_checks
        self.trace = trace
        self.checks: list[dict[str, str]] = []

    def authorize(self, *, user_id: str, session_id: str) -> CreditDecision:
        if self.trace is not None:
            self.trace.append("credit")
        self.checks.append({"user_id": user_id, "session_id": session_id})
        allowed = self.allowed
        if self.allowed_checks is not None:
            allowed = allowed and len(self.checks) <= self.allowed_checks
        if allowed:
            return CreditDecision(allowed=True, reason="")
        return CreditDecision(allowed=False, reason=CREDIT_STOP_TEXT)


class FakeMemoryRecall:
    """Scripted recall source. Hits are data the adapter must place in the prompt."""

    def __init__(
        self,
        hits: Sequence[MemoryHit] | None = None,
        *,
        trace: list[str] | None = None,
    ) -> None:
        self.hits = list(hits or [])
        self.trace = trace
        self.calls: list[dict[str, str]] = []

    def recall(self, *, user_id: str, query: str) -> list[MemoryHit]:
        if self.trace is not None:
            self.trace.append("memory")
        self.calls.append({"user_id": user_id, "query": query})
        return list(self.hits)


class EventSubscriber:
    """In-process consumer of one live turn's stream. Not durable across restart."""

    def __init__(self) -> None:
        self.events: list[StreamEvent] = []
        self.out_of_order: list[int] = []

    def publish(self, event: StreamEvent) -> None:
        if self.events and event.seq <= self.events[-1].seq:
            self.out_of_order.append(event.seq)
        self.events.append(event)


class SuperAgentAdapter(Protocol):
    """What ``make_adapter`` returns, for every discovered implementation.

    ``transcript`` and ``session`` read the injected store. A private cache
    may exist, but a new adapter on a copy of the store must see committed
    turns. ``close`` releases everything the adapter started (a child
    process, a socket); it may be a plain method or a coroutine.
    """

    provider_name: str

    def tool_names(self, role: str) -> list[str]: ...

    async def start(self, session_id: str, *, role: str, user_id: str) -> None: ...

    async def turn(self, session_id: str, user_text: str) -> list[StreamEvent]: ...

    def transcript(self, session_id: str) -> Transcript: ...

    def events(self, session_id: str) -> list[StreamEvent]: ...

    def session(self, session_id: str) -> SessionRecord: ...

    def cold_start_ms(self) -> float: ...

    def close(self) -> None | Awaitable[None]: ...
