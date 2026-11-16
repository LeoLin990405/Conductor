"""Model shim: Claude Code's model API, served on a unix socket.

Claude Code talks to Anthropic's Messages API. In production that API is
Anthropic's. Here the CLI reaches the harness's scripted model through this
shim, which serves **the same API** — the same request body, the same
response, the same streaming events — on a unix socket inside the adapter's
scratch directory. No TCP is opened: the offline guard stays closed and the
shim inherits it from ``sitecustomize``.

The endpoint is ``POST /v1/messages`` (a query string such as Claude Code's
``?beta=true`` is allowed; the path itself has to match exactly). The side
endpoint ``POST /v1/messages/count_tokens`` is answered with an estimate and
never touches the turn. Every other path is a 404, and any other method on
``/v1/messages`` is a 405. The request is Anthropic's:

    {"model": "...", "max_tokens": 1024, "messages": [...], "system": "...",
     "tools": [{"name": ..., "description": ..., "input_schema": {...}}],
     "stream": true}

The reply is Anthropic's too. With ``stream`` it is the server-sent event
stream Claude Code expects:

    event: message_start        data: {"type": "message_start", "message": {...}}
    event: content_block_start  data: {"type": "content_block_start", ...}
    event: content_block_delta  data: {"type": "content_block_delta", ...}
    event: content_block_stop   data: {"type": "content_block_stop", ...}
    event: message_delta        data: {"type": "message_delta", ...}
    event: message_stop         data: {"type": "message_stop"}

A model round is always streamed, whatever ``stream`` says: Claude Code
always asks for the stream, and the non-streaming round is not implemented.
Only the credit-stop reply honours ``stream: false`` with one JSON message.

A credit stop is not an API error. The gate's decision is recorded through
the bridge (the ``stop`` event, the commit with status ``stopped``) and the
client gets an ordinary ``end_turn`` reply with no tool use, so it ends the
turn the way it ends any other. A real failure — the bridge refusing a call,
the harness raising — is an error: ``502`` with Anthropic's error body when
nothing has been streamed yet, an ``event: error`` frame when it has. The
turn's uncommitted rows are dropped, so a retried request starts the turn
over instead of continuing a broken one.

**The shim owns the turn.** Credit, memory, the transcript and the events
live here, not in the CLI, so the CLI can be replaced by the real binary
without the turn losing them. On the first request of a turn the shim asks
the credit gate, recalls memory, and takes the conversation out of the
store; on every later request it records the tool exchange the client
reported, asks the gate again, and opens the next model round. The chunks
come from the bridge's ``model.open`` and ``model.next``, one at a time, and
each chunk's event is published before the next chunk is asked for: the shim
reads the subscriber's event count and waits for it to reach the value it
published, which is the per-chunk lockstep the contract requires.

**The shim speaks two languages.** On the wire it speaks the CLI's: the
tools are advertised as ``mcp__<server>__<tool>``, and the model's reply
has to name one of them exactly. Across the bridge it speaks the product's:
the tool names are the bare ones the transcript rows and ``tools.call`` use.
The translation happens in the shim, at the boundary, and nowhere else.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from prototypes.superagent.bridge import BridgeClient, BridgeError

MESSAGES_PATH = "/v1/messages"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
ANTHROPIC_VERSION = "2023-06-01"

# ``mcp__<server>__<tool>`` is how an MCP-backed CLI names a server's tools.
_MCP_PREFIX = re.compile(r"^mcp__[^_].*?__(?=[^_])")

# How long the shim waits for the previous chunk's events before giving up.
_COUNT_TIMEOUT_SECONDS = 30.0
_POLL_SECONDS = 0.002

# Anthropic's stop reasons.
_STOP_END_TURN = "end_turn"
_STOP_TOOL_USE = "tool_use"


class TurnStopped(Exception):
    """The credit gate stopped the turn.

    Not an API error: the client is answered with an ordinary ``end_turn``
    reply, and the stop itself is already recorded through the bridge.
    """


def canonical_tool_call(name: str, arguments: dict[str, Any]) -> str:
    """The canonical text of one tool call.

    Same shape the bridge's ``tools.call`` returns, so the event the shim
    publishes and the transcript row it stores are the same string.
    """

    return json.dumps(
        {"arguments": dict(arguments), "name": name},
        ensure_ascii=False,
        sort_keys=True,
    )


class ModelShim:
    """Serve Claude Code's model API and own the turn behind it."""

    def __init__(self, bridge_path: str, scratch: Path) -> None:
        self.bridge_path = bridge_path
        self.scratch = scratch
        self.socket_path = scratch / "model.sock"
        self.bridge = BridgeClient(bridge_path)
        self.session_id = self._read_session(scratch)
        self.roles = self._read_roles(scratch)
        self._in_turn = False
        self._transcript: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self._memory_text = ""
        # Authorized tool calls awaiting their result, by tool_use id.
        self._pending_tools: dict[str, tuple[str, dict[str, Any]]] = {}
        # Whether the current reply has already sent its 200 and SSE head.
        self._streaming = False
        # How many of the client's messages have already been read.
        self._scanned = 0
        # The names the client advertised this round, bare name -> wire name.
        self._advertised: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _read_session(scratch: Path) -> str:
        try:
            raw = json.loads((scratch / "session.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        return str(raw.get("session_id") or "")

    @staticmethod
    def _read_roles(scratch: Path) -> dict[str, list[str]]:
        try:
            raw = json.loads((scratch / "roles.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {
            str(role): [str(tool["name"]) for tool in tools]
            for role, tools in (raw or {}).items()
        }

    # -- the server ------------------------------------------------------

    def serve(self) -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        server.listen(8)
        (self.scratch / "model.pid").write_text(str(os.getpid()), encoding="utf-8")
        # The adapter waits for this file before it launches the CLI.
        (self.scratch / "model.ready").write_text(str(os.getpid()), encoding="utf-8")
        try:
            while True:
                try:
                    connection, _ = server.accept()
                except OSError:
                    return
                thread = threading.Thread(
                    target=self._serve_connection, args=(connection,), daemon=True
                )
                thread.start()
        finally:
            server.close()
            self.bridge.close()

    def _serve_connection(self, connection: socket.socket) -> None:
        try:
            with connection, connection.makefile("rwb") as stream:
                request = self._read_request(stream)
                if request is None:
                    return
                method, target, headers, body = request
                path = urlsplit(target).path
                if path not in (MESSAGES_PATH, COUNT_TOKENS_PATH):
                    self._write_json(stream, 404, _api_error("not_found_error", path))
                    return
                if method != "POST":
                    self._write_json(
                        stream, 405, _api_error("invalid_request_error", method)
                    )
                    return
                try:
                    payload = json.loads(body or b"{}")
                except json.JSONDecodeError:
                    self._write_json(
                        stream, 400, _api_error("invalid_request_error", "bad JSON")
                    )
                    return
                if path == COUNT_TOKENS_PATH:
                    # A side request. It never reaches the turn: no gate, no
                    # memory, no model round, no rows.
                    self._write_json(
                        stream, 200, {"input_tokens": _estimate_tokens(body)}
                    )
                    return
                self._messages(stream, payload, headers)
        except OSError:
            return

    @staticmethod
    def _read_request(
        stream: Any,
    ) -> tuple[str, str, dict[str, str], bytes] | None:
        line = stream.readline()
        if not line:
            return None
        parts = line.decode("latin-1").split()
        if len(parts) < 2:
            return None
        method, path = parts[0], parts[1]
        headers: dict[str, str] = {}
        while True:
            raw = stream.readline()
            if raw in (b"\r\n", b"\n", b""):
                break
            key, _, value = raw.decode("latin-1").partition(":")
            headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length") or 0)
        body = stream.read(length) if length else b""
        return method, path, headers, body

    @staticmethod
    def _write_json(stream: Any, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        reason = _REASONS.get(status, "Error")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        try:
            stream.write(head + body)
            stream.flush()
        except OSError:
            return

    # -- the turn --------------------------------------------------------

    def _messages(self, stream: Any, payload: dict[str, Any], headers: dict[str, str]) -> None:
        """Serve one model request. One request is one model round."""

        messages = list(payload.get("messages") or [])
        tools = list(payload.get("tools") or [])
        with self._lock:
            self._streaming = False
            try:
                self._advertised = _advertised_names(tools)
                if not self._in_turn:
                    self._begin_turn(messages)
                else:
                    self._record_tool_exchange(messages)
                    self._authorize()
                self._open_round(messages, tools)
                self._stream_round(stream, payload)
            except TurnStopped as stop:
                # Recorded and committed already. The client ends its turn.
                self._write_stopped_reply(stream, payload, str(stop))
            except BridgeError as exc:
                self._abandon_turn()
                self._write_error(stream, str(exc))

    def _abandon_turn(self) -> None:
        """Drop a broken turn's uncommitted state.

        Nothing of it reached the store: the turn commits once, at the end.
        The next request — a client retry or the next turn — starts over.
        """

        self._in_turn = False
        self._transcript = []
        self._events = []
        self._pending_tools = {}
        self._scanned = 0

    def _write_error(self, stream: Any, message: str) -> None:
        """A real failure, reported the way Anthropic reports one."""

        body = _api_error("api_error", message)
        if self._streaming:
            # The 200 is already on the wire; the error goes in the stream.
            self._write_event(stream, "error", {"type": "error", **body})
            return
        self._write_json(stream, 502, body)

    def _write_stopped_reply(
        self, stream: Any, payload: dict[str, Any], reason: str
    ) -> None:
        """An ordinary ``end_turn`` reply that carries the stop reason.

        No tool use, so the client has nothing left to do and ends the turn.
        The text is not a model token: it is not published as an event and
        not written as an assistant row; the ``stop`` event already is.
        """

        model = str(payload.get("model") or "scripted")
        content = [{"type": "text", "text": reason}]
        if not payload.get("stream"):
            self._write_json(
                stream,
                200,
                {
                    "id": "msg_shim",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": content,
                    "stop_reason": _STOP_END_TURN,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            )
            return
        self._write_stream_head(stream)
        self._write_message_start(stream, model)
        self._write_event(
            stream,
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        self._write_event(
            stream,
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": reason},
            },
        )
        self._write_event(
            stream, "content_block_stop", {"type": "content_block_stop", "index": 0}
        )
        self._write_message_end(stream, _STOP_END_TURN)

    def _session(self) -> dict[str, Any]:
        row = self.bridge.call("store.get_session", session_id=self.session_id)
        if row is None:
            raise BridgeError(f"session {self.session_id} is not started")
        return row

    def _authorize(self) -> None:
        """Ask the credit gate. A denial stops the turn."""

        session = self._session()
        decision = self.bridge.call(
            "credit.authorize",
            user_id=str(session["user_id"]),
            session_id=self.session_id,
        )
        if decision["allowed"]:
            return
        reason = str(decision["reason"])
        self._emit("stop", reason)
        self._commit("stopped")
        # The turn is over. The next turn starts from the top: its own user
        # row, its own memory recall, its own round. Leaving the flag set
        # would skip all three and run the next turn as a continuation.
        self._in_turn = False
        raise TurnStopped(reason)

    def _begin_turn(self, messages: list[dict[str, Any]]) -> None:
        """The first request of a turn: credit, memory, and the user text."""

        session = self._session()
        prompt = _first_user_text(messages)
        # The user's turn is part of the transcript whatever the gate says.
        self._record("user", prompt)
        self._authorize()
        hits = self.bridge.call(
            "memory.recall", user_id=str(session["user_id"]), query=prompt
        )
        self._memory_text = "\n".join(str(hit["text"]) for hit in hits)
        self._record("memory", self._memory_text)
        self._in_turn = True

    def _open_round(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> None:
        """Send the round to the scripted model, with the store's history.

        Across the bridge the tool names are the product's own names. The
        ``mcp__<server>__`` names are a wire detail of the CLI: the bridge,
        the transcript rows and ``tools.call`` all speak the bare name.
        """

        history = self.bridge.call(
            "store.transcript_rows", session_id=self.session_id
        )
        forwarded: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Route or review only. Do not run shell commands or edit "
                            "files. The only tools you have are the autonomy tools."
                        ),
                    }
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": self._memory_text}]},
        ]
        forwarded.extend(_history_messages(history))
        forwarded.extend(_bridge_messages(messages))
        self.bridge.call("model.open", messages=forwarded, tools=_bridge_tools(tools))

    def _stream_round(self, stream: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Publish the model's chunks as Anthropic's streaming events.

        The content blocks are indexed the way Anthropic indexes them: one
        index per block, opened once, closed once. The text of a turn is one
        text block that every text chunk adds to, and a tool request is a
        second block that starts where the text block ends. Reusing an index
        for a second block is a protocol error the real CLI would reject.
        """

        self._write_stream_head(stream)
        self._write_message_start(stream, str(payload.get("model") or "scripted"))
        blocks: list[dict[str, Any]] = []
        next_index = 0
        open_index: int | None = None
        while True:
            chunk = self.bridge.call("model.next")
            if chunk is None:
                break
            text = str(chunk.get("text") or "")
            tool_name = str(chunk.get("tool_name") or "")
            if text:
                self._emit("token", text, transcript_kind="assistant")
                if open_index is None:
                    open_index = next_index
                    next_index += 1
                    self._write_event(
                        stream,
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": open_index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                self._write_event(
                    stream,
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": open_index,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            if tool_name:
                arguments = dict(chunk.get("tool_args") or {})
                # The model's reply has to name a tool the client offered.
                advertised = self._advertised.get(
                    _normalize_tool_name(tool_name), tool_name
                )
                use_id = _tool_use_id(tool_name, arguments)
                if open_index is not None:
                    # The text block ends where the tool block starts.
                    self._write_event(
                        stream,
                        "content_block_stop",
                        {"type": "content_block_stop", "index": open_index},
                    )
                    open_index = None
                tool_index = next_index
                next_index += 1
                self._write_event(
                    stream,
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": tool_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": use_id,
                            "name": advertised,
                            "input": {},
                        },
                    },
                )
                self._write_event(
                    stream,
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": tool_index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(arguments),
                        },
                    },
                )
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": use_id,
                        "name": advertised,
                        "input": arguments,
                    }
                )
                open_index = tool_index
                break
        if open_index is not None:
            self._write_event(
                stream,
                "content_block_stop",
                {"type": "content_block_stop", "index": open_index},
            )
        if blocks:
            stop_reason = _STOP_TOOL_USE
        else:
            stop_reason = _STOP_END_TURN
            self._emit("done", "")
            self._commit("ready")
            self._in_turn = False
        self._write_message_end(stream, stop_reason)
        return blocks

    def _write_message_start(self, stream: Any, model: str) -> None:
        self._write_event(
            stream,
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_shim",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )

    def _write_message_end(self, stream: Any, stop_reason: str) -> None:
        self._write_event(
            stream,
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        )
        self._write_event(stream, "message_stop", {"type": "message_stop"})

    def _write_stream_head(self, stream: Any) -> None:
        self._streaming = True
        head = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        try:
            stream.write(head)
            stream.flush()
        except OSError:
            return

    @staticmethod
    def _write_event(stream: Any, event: str, data: dict[str, Any]) -> None:
        frame = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
        try:
            stream.write(frame)
            stream.flush()
        except OSError:
            return

    def _await_events(self, target: int) -> None:
        """Wait until the subscriber has published ``target`` events."""

        deadline = time.monotonic() + _COUNT_TIMEOUT_SECONDS
        while True:
            if int(self.bridge.call("subscriber.count")) >= target:
                return
            if time.monotonic() > deadline:
                raise BridgeError(
                    "the model shim waited for the previous chunk's events and "
                    "gave up"
                )
            time.sleep(_POLL_SECONDS)

    # -- the rows --------------------------------------------------------

    def _allocate(self, count: int = 1) -> int:
        return int(
            self.bridge.call(
                "seq.allocate", session_id=self.session_id, count=count
            )["first"]
        )

    def _row(self, seq: int, kind: str, text: str) -> dict[str, Any]:
        return {"session_id": self.session_id, "seq": seq, "kind": kind, "text": text}

    def _record(self, kind: str, text: str) -> None:
        """A transcript row the model does not see as an event."""

        self._transcript.append(self._row(self._allocate(), kind, text))

    def _emit(self, kind: str, text: str, transcript_kind: str | None = None) -> None:
        """A transcript row and the event the subscriber sees.

        The event is published, and then the shim waits until the subscriber
        has seen it. That is the per-chunk lockstep: the next chunk is only
        asked for once this chunk's event is out.
        """

        seq = self._allocate()
        self._transcript.append(self._row(seq, transcript_kind or kind, text))
        self._events.append(self._row(seq, kind, text))
        seen = int(self.bridge.call("subscriber.count"))
        self.bridge.call("subscriber.publish", seq=seq, kind=kind, text=text)
        self._await_events(seen + 1)

    def _allowed(self) -> list[str]:
        session = self._session()
        return list(self.roles.get(str(session["role"]), []))

    def _record_tool_exchange(self, messages: list[dict[str, Any]]) -> None:
        """Record the tool call and its result, as the client reported them.

        The client sends the whole conversation on every request, so only
        the messages that arrived since the last request are new. The tool
        names arrive with the client's ``mcp__<server>__`` prefix; they are
        normalized before the role's tool list is consulted, so a correctly
        prefixed call is authorized and a bare name that was never offered
        is not.
        """

        allowed = set(self._allowed())
        for message in messages[self._scanned :]:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                kind = block.get("type")
                if kind == "tool_use":
                    asked = str(block.get("name") or "")
                    name = _normalize_tool_name(asked)
                    arguments = dict(block.get("input") or {})
                    if name not in allowed:
                        # A shell, file, web or out-of-role tool never
                        # reaches a handler. The refusal names the tool the
                        # way the model asked for it.
                        self._emit("tool_refused", asked)
                        continue
                    self._emit("tool_call", canonical_tool_call(name, arguments))
                    self._pending_tools[str(block.get("id") or "")] = (name, arguments)
                elif kind == "tool_result":
                    # A result answers the call with its id; several calls in
                    # one message each get their own. A refused call has no
                    # pending entry, so its result is not recorded.
                    use_id = str(block.get("tool_use_id") or "")
                    if self._pending_tools.pop(use_id, None) is not None:
                        self._emit("tool_result", _result_text(block))
        self._scanned = len(messages)

    def _commit(self, status: str) -> None:
        """Commit the whole turn in one transaction."""

        session = self._session()
        rows = [*self._transcript, *self._events]
        next_seq = max((int(row["seq"]) for row in rows), default=0) + 1
        ops: list[dict[str, Any]] = [
            {"op": "append_transcript", "row": row} for row in self._transcript
        ]
        ops.extend({"op": "append_event", "row": row} for row in self._events)
        ops.append(
            {
                "op": "upsert_session",
                "row": {
                    "session_id": self.session_id,
                    "user_id": str(session["user_id"]),
                    "role": str(session["role"]),
                    "status": status,
                    "next_seq": next_seq,
                },
            }
        )
        self.bridge.call(
            "store.commit",
            ops=ops,
            expect={self.session_id: int(session["next_seq"])},
        )
        self._transcript = []
        self._events = []
        self._pending_tools = {}
        self._scanned = 0


_REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    500: "Internal Server Error",
    502: "Bad Gateway",
}


def _api_error(kind: str, message: str) -> dict[str, Any]:
    """Anthropic's error body."""

    return {"type": "error", "error": {"type": kind, "message": message}}


def _estimate_tokens(body: bytes) -> int:
    """A rough token count for ``count_tokens``: four bytes a token."""

    return max(1, len(body) // 4)


def _first_user_text(messages: list[dict[str, Any]]) -> str:
    """The user's turn, as the client sent it."""

    for message in messages:
        if str(message.get("role")) != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return str(block.get("text") or "")
    return ""


def _normalize_tool_name(name: str) -> str:
    """``mcp__<server>__<tool>`` -> ``<tool>``."""

    return _MCP_PREFIX.sub("", name, count=1)


def _advertised_names(tools: list[Any]) -> dict[str, str]:
    """Map every advertised tool onto the name the model has to use.

    The client offers its tools as ``mcp__<server>__<tool>``. A model reply
    has to name one of them exactly, so the bare product name is mapped back
    onto the advertised one. A tool the client never offered has no entry,
    and is emitted unchanged so that it is refused rather than renamed.
    """

    advertised: dict[str, str] = {}
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "")
        if name:
            advertised[_normalize_tool_name(name)] = name
    return advertised


def _bridge_tools(tools: list[Any]) -> list[Any]:
    """The tool definitions in the bridge's language: bare product names."""

    bridged: list[Any] = []
    for tool in tools:
        if not isinstance(tool, dict):
            bridged.append(tool)
            continue
        definition = dict(tool)
        name = str(definition.get("name") or "")
        if name:
            definition["name"] = _normalize_tool_name(name)
        bridged.append(definition)
    return bridged


def _bridge_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The client's messages in the bridge's language.

    The client sends the tool call back with the name the shim gave it, the
    advertised ``mcp__<server>__`` name. The bridge speaks the product's
    bare names, so the names are translated here and nowhere else.
    """

    bridged: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            bridged.append(dict(message))
            continue
        blocks: list[Any] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                block = dict(block)
                name = str(block.get("name") or "")
                if name:
                    block["name"] = _normalize_tool_name(name)
            blocks.append(block)
        copied = dict(message)
        copied["content"] = blocks
        bridged.append(copied)
    return bridged


def _tool_use_id(name: str, arguments: dict[str, Any]) -> str:
    """A stable id for one tool call.

    The same id is used on the wire and in the replayed history, so a
    ``tool_result`` always points at the ``tool_use`` it answers.
    """

    call = canonical_tool_call(name, arguments)
    return f"toolu_{hashlib.sha1(call.encode('utf-8')).hexdigest()[:16]}"


def _result_text(block: dict[str, Any]) -> str:
    """The text of a tool_result block."""

    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "".join(pieces)
    return ""


def _history_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The store's transcript, as Anthropic messages."""

    messages: list[dict[str, Any]] = []
    pending_call = ""
    for row in history:
        kind, text = str(row["kind"]), str(row["text"])
        if kind == "tool_call":
            pending_call = text
        elif kind == "tool_result" and pending_call:
            messages.extend(_tool_messages(pending_call, text))
            pending_call = ""
        elif kind in {"user", "memory", "tool_refused"}:
            messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
        elif kind == "assistant":
            messages.append(
                {"role": "assistant", "content": [{"type": "text", "text": text}]}
            )
    return messages


def _tool_messages(call_text: str, result: str) -> list[dict[str, Any]]:
    """The assistant's tool call and the tool's answer, Anthropic shaped."""

    call = json.loads(call_text)
    use_id = _tool_use_id(str(call["name"]), dict(call["arguments"]))
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": use_id,
                    "name": call["name"],
                    "input": call["arguments"],
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": use_id,
                    "content": [{"type": "text", "text": result}],
                }
            ],
        },
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write("usage: model_shim.py <bridge_path> <scratch_dir>\n")
        return 2
    ModelShim(argv[1], Path(argv[2])).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
