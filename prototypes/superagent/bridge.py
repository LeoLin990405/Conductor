"""Unix-socket bridge from an out-of-process runtime to the contract doubles.

The harness serves one bridge per adapter instance, on
``RuntimeContext.bridge_path``. A Node service, a native CLI's model shim or
MCP server, or a Python child connects to it and reaches the same scripted
model, store, credit gate, memory recall, subscriber, and fake tool handlers
an in-process adapter is handed. No TCP is involved, so the offline guard
stays closed. A Python shim cannot listen on or dial TCP under the guard
either: use this socket, or write the shim in another language.

This module imports only the standard library. A child can import it without
loading the product tool registry.

Protocol: one JSON object per line, UTF-8, request then response, in order on
one connection. Several connections may be open at once (a model shim and an
MCP server, say); calls are serialized by the harness.

Request: ``{"id": <int>, "method": <str>, "params": {...}}``
Response: ``{"id": <same>, "result": <value>}`` or
``{"id": <same>, "error": {"type": <str>, "message": <str>}}``

The first request on every connection must be ``hello`` with
``{"protocol": PROTOCOL_VERSION}``. Anything else first, or another version,
is an error. ``BridgeClient`` sends it on connect.

Methods:

- ``hello`` ``{"protocol": int}`` -> ``{"protocol": int, "methods": [str]}``.
- ``model.open`` ``{"messages": [...], "tools": [...] | null}`` -> ``null``.
  Starts one model round. Each message is ``{"role": str, "content": ...}``
  where content is a string or a list of Anthropic/OpenAI blocks (``text``,
  ``tool_use``, ``tool_result``); OpenAI ``tool_calls`` are read too. See
  ``prototypes.superagent.contract.message_texts``: every transcript entry the
  request carries must come out as one text piece equal to that entry's text.
  ``tools`` is every tool definition the runtime offers the model on this
  request (a name, ``{"name": ...}``, or ``{"function": {"name": ...}}``).
  The harness records both. For a native CLI the shim reports them, so "what
  the model saw" is what the shim forwards.
- ``model.next`` ``{}`` -> ``{"text": str, "tool_name": str, "tool_args": {}}``
  or ``null`` when the round has ended. A round ends after a tool request.
  Publish every event for a chunk before asking for the next one: the
  harness checks per-chunk lockstep. ``subscriber.count`` lets a shim wait
  for another process (an MCP server) to publish first.
- ``subscriber.publish`` ``{"seq": int, "kind": str, "text": str}`` -> ``null``.
- ``subscriber.count`` ``{}`` -> int: events published so far.
- ``seq.allocate`` ``{"session_id": str, "count": int = 1}`` ->
  ``{"first": int}``. Reserves ``count`` consecutive seqs for transcript and
  event rows, starting at the store's ``next_seq`` or after the last seq this
  bridge handed out, whichever is higher. Processes that write rows for the
  same turn take every seq from here so they never collide.
- ``tools.call`` ``{"session_id": str, "name": str, "arguments": {}}`` ->
  ``{"call": str, "result": str}``. Runs the shared fake handler in the
  harness. The bridge reads the session's stored role and refuses any name
  outside that role's tool list (and any unknown name) without dispatching.
  ``call`` and ``result`` are the durable ``tool_call`` and ``tool_result``
  texts. The runtime still refuses shell, file, and out-of-role names itself
  and publishes ``tool_refused``.
- ``credit.authorize`` ``{"user_id": str, "session_id": str}`` ->
  ``{"allowed": bool, "reason": str}``.
- ``memory.recall`` ``{"user_id": str, "query": str}`` ->
  ``[{"text": str, "memory_id": str}, ...]``.
- ``store.get_session`` ``{"session_id": str}`` -> row or ``null``.
- ``store.transcript_rows`` / ``store.event_rows`` ``{"session_id": str}`` ->
  rows sorted by ``seq``.
- ``store.commit`` ``{"ops": [...], "expect": {...}}`` -> ``{"commits": int}``.
  One store transaction. Each op is ``{"op": "upsert_session" |
  "append_transcript" | "append_event", "row": {...}}``. ``expect`` maps a
  session id to the ``next_seq`` it must still have (``null``: must not
  exist); a mismatch is an error and nothing is written.

Shutdown: the harness closes the bridge after the adapter's ``close``, and
before it inspects the store after a restart. A runtime must exit when its
control channel reaches EOF and must treat a closed bridge as fatal.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

PROTOCOL_VERSION = 2


class BridgeError(RuntimeError):
    """The harness answered a bridge call with an error."""


class ContractBridge:
    """Serve one adapter's doubles on a unix socket, from a daemon thread.

    The test's event loop stays free to await the adapter while a child
    process calls back into the doubles.
    """

    def __init__(
        self,
        path: str,
        *,
        model: Any,
        store: Any,
        credit_gate: Any,
        memory: Any,
        subscriber: Any,
    ) -> None:
        self.path = path
        self.model = model
        self.store = store
        self.credit_gate = credit_gate
        self.memory = memory
        self.subscriber = subscriber
        self.calls: list[str] = []
        self._lock = threading.Lock()
        self._server: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._connections: list[socket.socket] = []
        self._closed = False
        self._seq: dict[str, int] = {}
        self._methods: dict[str, Callable[[Mapping[str, Any]], Any]] = {
            "hello": self._hello,
            "subscriber.count": self._count,
            "seq.allocate": self._allocate,
            "model.open": self._model_open,
            "model.next": self._model_next,
            "subscriber.publish": self._publish,
            "tools.call": self._tool_call,
            "credit.authorize": self._authorize,
            "memory.recall": self._recall,
            "store.get_session": self._get_session,
            "store.transcript_rows": self._transcript_rows,
            "store.event_rows": self._event_rows,
            "store.commit": self._commit,
        }

    def start(self) -> ContractBridge:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.path)
        server.listen(16)
        self._server = server
        thread = threading.Thread(target=self._accept, daemon=True)
        thread.start()
        self._threads.append(thread)
        return self

    def close(self) -> None:
        self._closed = True
        if self._server is not None:
            try:
                self._server.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._server.close()
            self._server = None
        for connection in list(self._connections):
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        for thread in self._threads:
            thread.join(timeout=2)

    def _accept(self) -> None:
        while not self._closed and self._server is not None:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            self._connections.append(connection)
            thread = threading.Thread(
                target=self._serve, args=(connection,), daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def _serve(self, connection: socket.socket) -> None:
        state = {"hello": False}
        with connection, connection.makefile("rwb") as stream:
            for raw in stream:
                if not raw.strip():
                    continue
                reply = self._handle(raw, state)
                try:
                    stream.write(json.dumps(reply).encode("utf-8") + b"\n")
                    stream.flush()
                except OSError:
                    return

    def _handle(self, raw: bytes, state: dict[str, bool]) -> dict[str, Any]:
        request_id: Any = None
        try:
            request = json.loads(raw)
            request_id = request.get("id")
            method = str(request["method"])
            params = request.get("params") or {}
            handler = self._methods.get(method)
            if handler is None:
                raise BridgeError(f"unknown bridge method: {method}")
            if method != "hello" and not state["hello"]:
                raise BridgeError("the first bridge call must be hello")
            with self._lock:
                self.calls.append(method)
                result = handler(params)
            if method == "hello":
                state["hello"] = True
            return {"id": request_id, "result": result}
        except Exception as exc:  # reported to the runtime, not swallowed
            return {
                "id": request_id,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }

    def _hello(self, params: Mapping[str, Any]) -> dict[str, Any]:
        version = params.get("protocol")
        if version != PROTOCOL_VERSION:
            raise BridgeError(
                f"bridge protocol {version!r} is not supported; "
                f"this harness speaks {PROTOCOL_VERSION}"
            )
        return {"protocol": PROTOCOL_VERSION, "methods": sorted(self._methods)}

    def _count(self, _params: Mapping[str, Any]) -> int:
        return len(self.subscriber.events)

    def _allocate(self, params: Mapping[str, Any]) -> dict[str, int]:
        session_id = str(params["session_id"])
        count = int(params.get("count", 1))
        if count < 1:
            raise BridgeError("seq.allocate count must be at least 1")
        session = self.store.get_session(session_id)
        if session is None:
            raise BridgeError(f"session {session_id} is not started")
        first = max(int(session["next_seq"]), self._seq.get(session_id, 0))
        self._seq[session_id] = first + count
        return {"first": first}

    def _model_open(self, params: Mapping[str, Any]) -> None:
        tools = params.get("tools")
        self.model.open_round(list(params.get("messages") or []), tools)

    def _model_next(self, _params: Mapping[str, Any]) -> dict[str, Any] | None:
        chunk = self.model.next_chunk()
        if chunk is None:
            return None
        return {
            "text": chunk.text,
            "tool_name": chunk.tool_name,
            "tool_args": dict(chunk.tool_args),
        }

    def _publish(self, params: Mapping[str, Any]) -> None:
        from prototypes.superagent.contract import StreamEvent

        self.subscriber.publish(
            StreamEvent(
                seq=int(params["seq"]),
                kind=str(params["kind"]),
                text=str(params["text"]),
            )
        )

    def _tool_call(self, params: Mapping[str, Any]) -> dict[str, str]:
        from prototypes.superagent.contract import (
            FAKE_TOOL_HANDLERS,
            scripted_tool_call_text,
        )

        from prototypes.superagent.contract import autonomy_tool_names

        session_id = str(params["session_id"])
        session = self.store.get_session(session_id)
        if session is None:
            raise BridgeError(f"session {session_id} is not started")
        name = str(params["name"])
        role = str(session["role"])
        if name not in autonomy_tool_names(role):
            raise BridgeError(f"tool {name} is not offered to the {role} role")
        arguments = dict(params.get("arguments") or {})
        # Looked up per call so a test spy on the shared map sees dispatches.
        handler = FAKE_TOOL_HANDLERS.get(name)
        if handler is None:
            raise BridgeError(f"no fake handler for {name}")
        return {
            "call": scripted_tool_call_text(name, arguments),
            "result": handler(arguments),
        }

    def _authorize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        decision = self.credit_gate.authorize(
            user_id=str(params["user_id"]), session_id=str(params["session_id"])
        )
        return {"allowed": bool(decision.allowed), "reason": str(decision.reason)}

    def _recall(self, params: Mapping[str, Any]) -> list[dict[str, str]]:
        hits = self.memory.recall(
            user_id=str(params["user_id"]), query=str(params["query"])
        )
        return [{"text": hit.text, "memory_id": hit.memory_id} for hit in hits]

    def _get_session(self, params: Mapping[str, Any]) -> dict[str, Any] | None:
        return self.store.get_session(str(params["session_id"]))

    def _transcript_rows(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        return self.store.transcript_rows(str(params["session_id"]))

    def _event_rows(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        return self.store.event_rows(str(params["session_id"]))

    def _commit(self, params: Mapping[str, Any]) -> dict[str, int]:
        with self.store.transaction() as txn:
            for session_id, next_seq in (params.get("expect") or {}).items():
                current = txn.get_session(session_id)
                have = None if current is None else int(current["next_seq"])
                if have != next_seq:
                    raise BridgeError(
                        f"session {session_id} changed: next_seq {have} != {next_seq}"
                    )
            for op in params.get("ops") or []:
                kind, row = op["op"], op["row"]
                if kind == "upsert_session":
                    txn.upsert_session(row)
                elif kind == "append_transcript":
                    txn.append_transcript(row)
                elif kind == "append_event":
                    txn.append_event(row)
                else:
                    raise BridgeError(f"unknown store op: {kind}")
        return {"commits": int(self.store.commits)}


class BridgeClient:
    """Blocking JSON-lines client. One instance per connection."""

    def __init__(self, path: str, *, hello: bool = True) -> None:
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.connect(path)
        self._stream = self._socket.makefile("rwb")
        self._next_id = 0
        if hello:
            self.call("hello", protocol=PROTOCOL_VERSION)

    def call(self, method: str, **params: Any) -> Any:
        self._next_id += 1
        request = {"id": self._next_id, "method": method, "params": params}
        self._stream.write(json.dumps(request).encode("utf-8") + b"\n")
        self._stream.flush()
        line = self._stream.readline()
        if not line:
            raise BridgeError("bridge closed the connection")
        reply = json.loads(line)
        if reply.get("id") != self._next_id:
            raise BridgeError(f"bridge reply out of order: {reply!r}")
        if "error" in reply:
            error = reply["error"]
            raise BridgeError(f"{error.get('type')}: {error.get('message')}")
        return reply.get("result")

    def model_round(
        self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Any] | None
    ):
        """Yield one round's chunks as dicts, asking for each in turn."""

        self.call("model.open", messages=list(messages), tools=tools)
        while (chunk := self.call("model.next")) is not None:
            yield chunk

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            self._socket.close()
