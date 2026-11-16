"""Child runtime for ``reference_bridge``. Stands in for a Node service or CLI.

It talks to its parent adapter on stdin/stdout (one JSON object per line)
and to the harness over the unix-socket bridge. It imports only the standard
library and ``prototypes.superagent.bridge``: everything it knows about the
model, the store, credit, memory, and tools comes over that socket.

    python runtime.py <bridge_path> <scratch_dir>
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from prototypes.superagent.bridge import BridgeClient


class Runtime:
    def __init__(self, bridge: BridgeClient) -> None:
        self.bridge = bridge
        self.role_tools: dict[str, list[str]] = {}

    def hello(self, role_tools: dict[str, list[str]]) -> None:
        self.role_tools = {key: list(value) for key, value in role_tools.items()}

    def start(self, session_id: str, role: str, user_id: str) -> None:
        if self.bridge.call("store.get_session", session_id=session_id) is not None:
            return
        row = {
            "session_id": session_id,
            "user_id": user_id,
            "role": role,
            "status": "ready",
            "next_seq": 1,
        }
        self.bridge.call(
            "store.commit",
            ops=[{"op": "upsert_session", "row": row}],
            expect={session_id: None},
        )

    def session(self, session_id: str) -> dict[str, Any]:
        row = self.bridge.call("store.get_session", session_id=session_id)
        if row is None:
            raise RuntimeError(f"session {session_id} is not started")
        return row

    def rows(self, kind: str, session_id: str) -> list[dict[str, Any]]:
        return self.bridge.call(f"store.{kind}_rows", session_id=session_id)

    def turn(self, session_id: str, user_text: str) -> list[dict[str, Any]]:
        session = self.session(session_id)
        user_id, role = session["user_id"], session["role"]
        original = int(session["next_seq"])
        seq = original
        ops: list[dict[str, Any]] = []
        published: list[dict[str, Any]] = []

        def take() -> int:
            # Shared allocator: a model shim and an MCP server writing rows
            # for the same turn would both take seqs here.
            nonlocal seq
            first = self.bridge.call("seq.allocate", session_id=session_id)["first"]
            seq = first + 1
            return first

        def row(at: int, kind: str, text: str) -> dict[str, Any]:
            return {"session_id": session_id, "seq": at, "kind": kind, "text": text}

        def record(kind: str, text: str) -> None:
            ops.append({"op": "append_transcript", "row": row(take(), kind, text)})

        def emit(kind: str, text: str, transcript_kind: str | None = None) -> None:
            at = take()
            ops.append(
                {
                    "op": "append_transcript",
                    "row": row(at, transcript_kind or kind, text),
                }
            )
            ops.append({"op": "append_event", "row": row(at, kind, text)})
            event = {"seq": at, "kind": kind, "text": text}
            self.bridge.call("subscriber.publish", **event)
            published.append(event)

        def commit(status: str) -> list[dict[str, Any]]:
            updated = dict(session, next_seq=seq, status=status)
            ops.append({"op": "upsert_session", "row": updated})
            self.bridge.call("store.commit", ops=ops, expect={session_id: original})
            return published

        def allowed() -> bool:
            decision = self.bridge.call(
                "credit.authorize", user_id=user_id, session_id=session_id
            )
            if not decision["allowed"]:
                emit("stop", decision["reason"])
            return bool(decision["allowed"])

        history = self.rows("transcript", session_id)
        record("user", user_text)
        if not allowed():
            return commit("stopped")
        hits = self.bridge.call("memory.recall", user_id=user_id, query=user_text)
        memory_text = "\n".join(hit["text"] for hit in hits)
        record("memory", memory_text)
        names = self.role_tools[role]
        messages = self.messages(role, names, history, memory_text, user_text)
        tools = [{"name": name} for name in names]
        first_round = True
        while True:
            if not first_round and not allowed():
                return commit("stopped")
            first_round = False
            saw_tool = False
            for chunk in self.bridge.model_round(messages, tools):
                if chunk["text"]:
                    emit("token", chunk["text"], "assistant")
                name = chunk["tool_name"]
                if not name:
                    continue
                saw_tool = True
                if name not in names:
                    emit("tool_refused", name)
                    messages.append(_text("user", f"refused:{name}"))
                    break
                done = self.bridge.call(
                    "tools.call",
                    session_id=session_id,
                    name=name,
                    arguments=chunk["tool_args"],
                )
                emit("tool_call", done["call"])
                emit("tool_result", done["result"])
                messages.extend(_tool_messages(done["call"], done["result"]))
                break
            if not saw_tool:
                break
        emit("done", "")
        return commit("ready")

    @staticmethod
    def messages(
        role: str,
        names: list[str],
        history: list[dict[str, Any]],
        memory_text: str,
        user_text: str,
    ) -> list[dict[str, Any]]:
        """Anthropic-shaped request: content blocks, tool_use, tool_result.

        The harness normalizes these with ``message_texts``. A native CLI's
        shim forwards the same shape.
        """

        messages = [
            _text(
                "system",
                f"role={role}\n"
                "Route or review only. Do not run shell commands or edit files.",
            ),
            _text("user", memory_text),
        ]
        pending_call = ""
        for entry in history:
            kind, text = entry["kind"], entry["text"]
            if kind == "tool_call":
                pending_call = text
            elif kind == "tool_result" and pending_call:
                messages.extend(_tool_messages(pending_call, text))
                pending_call = ""
            elif kind in {"user", "memory", "tool_refused"}:
                messages.append(_text("user", text))
            elif kind == "assistant":
                messages.append(_text("assistant", text))
        messages.append(_text("user", user_text))
        return messages


def _text(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _tool_messages(call_text: str, result: str) -> list[dict[str, Any]]:
    call = json.loads(call_text)
    use_id = f"toolu_{abs(hash(call_text)) % 10**12}"
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


def main() -> int:
    bridge_path, scratch = sys.argv[1], Path(sys.argv[2])
    (scratch / "runtime.pid").write_text(str(os.getpid()), encoding="utf-8")
    bridge = BridgeClient(bridge_path)
    runtime = Runtime(bridge)
    out = sys.stdout
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            request = json.loads(line)
            op = request["op"]
            try:
                if op == "close":
                    out.write(json.dumps({"ok": True, "result": None}) + "\n")
                    out.flush()
                    return 0
                result: Any = None
                if op == "hello":
                    runtime.hello(request["role_tools"])
                elif op == "start":
                    runtime.start(
                        request["session_id"], request["role"], request["user_id"]
                    )
                elif op == "turn":
                    result = runtime.turn(request["session_id"], request["user_text"])
                elif op == "session":
                    result = runtime.session(request["session_id"])
                elif op in {"transcript", "event"}:
                    result = runtime.rows(op, request["session_id"])
                else:
                    raise RuntimeError(f"unknown op {op}")
                reply = {"ok": True, "result": result}
            except Exception as exc:  # reported to the parent adapter
                reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            out.write(json.dumps(reply) + "\n")
            out.flush()
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
