"""In-process SuperAgent that satisfies the contract without a provider.

The reference exists so the contract tests have one implementation that is
always present. It persists sessions in the injected store, streams the
scripted model as each chunk arrives, and runs role tools only through the
contract's fake handlers.
"""

from __future__ import annotations

import time
from typing import Any

from prototypes.superagent.contract import (
    FAKE_TOOL_HANDLERS,
    EventSubscriber,
    FakeCreditGate,
    FakeMemoryRecall,
    FakePostgresStore,
    RuntimeContext,
    ScriptedChunk,
    ScriptedFakeChatModel,
    SessionRecord,
    StreamEvent,
    Transcript,
    TranscriptEntry,
    autonomy_tool_names,
    canonical_role,
    is_shell_or_file_tool,
    scripted_tool_call_text,
)


def make_adapter(
    *,
    model: ScriptedFakeChatModel,
    store: FakePostgresStore,
    credit_gate: FakeCreditGate,
    memory: FakeMemoryRecall,
    subscriber: EventSubscriber,
    runtime: RuntimeContext,
) -> ReferenceSuperAgent:
    """Build the reference adapter from the contract doubles.

    A model whose ``provider_name`` is not ``scripted`` is refused so this
    package cannot be pointed at a hosted provider by swapping the double.
    The reference runs in-process, so it ignores ``runtime`` and declares no
    ``runtime_argv``.
    """

    del runtime

    provider = getattr(model, "provider_name", None)
    if provider != "scripted":
        raise RuntimeError(f"reference adapter refuses provider {provider!r}")
    return ReferenceSuperAgent(
        model=model,
        store=store,
        credit_gate=credit_gate,
        memory=memory,
        subscriber=subscriber,
    )


class ReferenceSuperAgent:
    provider_name = "scripted"

    def __init__(
        self,
        *,
        model: ScriptedFakeChatModel,
        store: FakePostgresStore,
        credit_gate: FakeCreditGate,
        memory: FakeMemoryRecall,
        subscriber: EventSubscriber,
    ) -> None:
        self.model = model
        self.store = store
        self.credit_gate = credit_gate
        self.memory = memory
        self.subscriber = subscriber
        self._cold_start_ms = 0.0

    def tool_names(self, role: str) -> list[str]:
        return autonomy_tool_names(role)

    def offered_tools(self, role: str) -> list[dict[str, str]] | None:
        """Tool definitions put in every model request for ``role``."""

        return [{"name": name} for name in self.tool_names(role)]

    def cold_start_ms(self) -> float:
        return self._cold_start_ms

    def close(self) -> None:
        """Nothing to release: all state is in the injected store."""

    async def start(self, session_id: str, *, role: str, user_id: str) -> None:
        started = time.perf_counter()
        requested = canonical_role(role)
        existing = self.store.get_session(session_id)
        if existing is None:
            with self.store.transaction() as txn:
                txn.upsert_session(
                    {
                        "session_id": session_id,
                        "user_id": user_id,
                        "role": requested,
                        "status": "ready",
                        "next_seq": 1,
                    }
                )
        else:
            # The stored identity is the session. A restarted process often
            # passes a default role; overwriting it would retarget the tools.
            canonical_role(str(existing["role"]))
        self._cold_start_ms = (time.perf_counter() - started) * 1000.0

    async def turn(self, session_id: str, user_text: str) -> list[StreamEvent]:
        session = self._require_session(session_id)
        decision = self.credit_gate.authorize(
            user_id=session["user_id"],
            session_id=session_id,
        )
        seq = int(session["next_seq"])
        transcript_rows: list[dict[str, Any]] = []
        event_rows: list[dict[str, Any]] = []
        published: list[StreamEvent] = []

        def record(kind: str, text: str) -> None:
            nonlocal seq
            transcript_rows.append(
                {
                    "session_id": session_id,
                    "seq": seq,
                    "kind": kind,
                    "text": text,
                }
            )
            seq += 1

        def emit(kind: str, text: str, *, transcript_kind: str | None = None) -> None:
            nonlocal seq
            event = StreamEvent(seq=seq, kind=kind, text=text)
            transcript_rows.append(
                {
                    "session_id": session_id,
                    "seq": seq,
                    "kind": transcript_kind or kind,
                    "text": text,
                }
            )
            event_rows.append(
                {
                    "session_id": session_id,
                    "seq": seq,
                    "kind": kind,
                    "text": text,
                }
            )
            published.append(event)
            self.subscriber.publish(event)
            seq += 1

        record("user", user_text)
        if not decision.allowed:
            emit("stop", decision.reason)
            self._commit(session, seq, "stopped", transcript_rows, event_rows)
            return published

        hits = self.memory.recall(user_id=session["user_id"], query=user_text)
        memory_text = "\n".join(hit.text for hit in hits)
        record("memory", memory_text)
        messages = self._messages(
            role=str(session["role"]),
            history=self.transcript(session_id),
            memory_text=memory_text,
            user_text=user_text,
        )
        tools = self.offered_tools(str(session["role"]))
        first_round = True
        while True:
            if not first_round:
                # Every model round spends credit, so a tool call can be the
                # last thing a turn does.
                decision = self.credit_gate.authorize(
                    user_id=session["user_id"], session_id=session_id
                )
                if not decision.allowed:
                    emit("stop", decision.reason)
                    self._commit(session, seq, "stopped", transcript_rows, event_rows)
                    return published
            first_round = False
            saw_tool = False
            async for chunk in self.model.stream(messages, tools=tools):
                if chunk.text:
                    emit("token", chunk.text, transcript_kind="assistant")
                if chunk.tool_name:
                    self._handle_tool(
                        chunk,
                        role=str(session["role"]),
                        messages=messages,
                        emit=emit,
                    )
                    saw_tool = True
                    break
            if not saw_tool:
                break
        emit("done", "")
        self._commit(session, seq, "ready", transcript_rows, event_rows)
        return published

    def transcript(self, session_id: str) -> Transcript:
        rows = self.store.transcript_rows(session_id)
        return Transcript(
            session_id=session_id,
            entries=[
                TranscriptEntry(
                    seq=int(row["seq"]), kind=str(row["kind"]), text=str(row["text"])
                )
                for row in rows
            ],
        )

    def events(self, session_id: str) -> list[StreamEvent]:
        return [
            StreamEvent(
                seq=int(row["seq"]), kind=str(row["kind"]), text=str(row["text"])
            )
            for row in self.store.event_rows(session_id)
        ]

    def session(self, session_id: str) -> SessionRecord:
        row = self._require_session(session_id)
        return SessionRecord(
            session_id=session_id,
            user_id=str(row["user_id"]),
            role=str(row["role"]),
            status=str(row["status"]),
        )

    def _require_session(self, session_id: str) -> dict[str, Any]:
        row = self.store.get_session(session_id)
        if row is None:
            raise RuntimeError(f"session {session_id} is not started")
        return row

    def _messages(
        self,
        *,
        role: str,
        history: Transcript,
        memory_text: str,
        user_text: str,
    ) -> list[dict[str, str]]:
        tools = ",".join(self.tool_names(role))
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    f"role={role}\n"
                    "Route or review only. Do not run shell commands or edit files.\n"
                    f"tools={tools}"
                ),
            },
            {"role": "memory", "content": memory_text},
        ]
        for entry in history.entries:
            if entry.kind == "user":
                messages.append({"role": "user", "content": entry.text})
            elif entry.kind in {"assistant", "token"}:
                messages.append({"role": "assistant", "content": entry.text})
            elif entry.kind == "memory":
                messages.append({"role": "memory", "content": entry.text})
            elif entry.kind in {"tool_call", "tool_result", "tool_refused"}:
                messages.append({"role": entry.kind, "content": entry.text})
        messages.append({"role": "user", "content": user_text})
        return messages

    def _handle_tool(
        self,
        chunk: ScriptedChunk,
        *,
        role: str,
        messages: list[dict[str, str]],
        emit: Any,
    ) -> None:
        """Run one model tool request, or record that it was refused.

        Permitted Router and watchdog tools use the shared fake handlers.
        Their arguments and results are emitted before the caller continues
        the model. Shell, file, and out-of-role names are not dispatched.
        """

        name = chunk.tool_name
        arguments = dict(chunk.tool_args)
        allowed = set(self.tool_names(role))
        if is_shell_or_file_tool(name) or name not in allowed:
            emit("tool_refused", name)
            messages.append(
                {"role": "tool", "name": name, "content": f"refused:{name}"}
            )
            return
        handler = FAKE_TOOL_HANDLERS.get(name)
        if handler is None:
            raise RuntimeError(f"no fake handler for {name}")
        call_text = scripted_tool_call_text(name, arguments)
        result = handler(arguments)
        emit("tool_call", call_text)
        emit("tool_result", result)
        messages.append({"role": "assistant", "content": call_text})
        messages.append({"role": "tool", "name": name, "content": result})

    def _commit(
        self,
        session: dict[str, Any],
        next_seq: int,
        status: str,
        transcript_rows: list[dict[str, Any]],
        event_rows: list[dict[str, Any]],
    ) -> None:
        session_id = str(session["session_id"])
        original = int(session["next_seq"])
        with self.store.transaction() as txn:
            current = txn.get_session(session_id)
            if current is None or int(current["next_seq"]) != original:
                raise RuntimeError(f"session {session_id} changed during the turn")
            for row in transcript_rows:
                txn.append_transcript(row)
            for row in event_rows:
                txn.append_event(row)
            current["next_seq"] = next_seq
            current["status"] = status
            txn.upsert_session(current)
