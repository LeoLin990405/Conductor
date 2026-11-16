"""langchain-core in-process SuperAgent adapter for the prototype contract.

The turn loop streams through ``BaseChatModel.astream`` (after ``bind_tools``).
It never builds a LangGraph graph, opens a sandbox, or imports PTC.
"""

from __future__ import annotations

import time
from typing import Any

from prototypes.superagent.contract import (
    EventSubscriber,
    FakeCreditGate,
    FakeMemoryRecall,
    FakePostgresStore,
    RuntimeContext,
    ScriptedFakeChatModel,
    SessionRecord,
    StreamEvent,
    Transcript,
    TranscriptEntry,
    autonomy_tool_names,
    canonical_role,
)
from prototypes.superagent.p_python.chat_model import ScriptedLangChainChatModel
from prototypes.superagent.p_python.messages import transcript_to_messages
from prototypes.superagent.p_python.tools import (
    handle_tool_request,
    offered_tool_definitions,
    tool_request_from_chunk,
)


def make_adapter(
    *,
    model: ScriptedFakeChatModel,
    store: FakePostgresStore,
    credit_gate: FakeCreditGate,
    memory: FakeMemoryRecall,
    subscriber: EventSubscriber,
    runtime: RuntimeContext,
) -> PythonLoopSuperAgent:
    """Build the langchain-core loop adapter from the contract doubles.

    A model whose ``provider_name`` is not ``scripted`` is refused so this
    package cannot be pointed at a hosted provider by swapping the double.
    The loop runs in-process, so it ignores ``runtime`` and declares no
    ``runtime_argv``.
    """

    del runtime

    provider = getattr(model, "provider_name", None)
    if provider != "scripted":
        raise RuntimeError(f"p_python adapter refuses provider {provider!r}")
    return PythonLoopSuperAgent(
        model=model,
        store=store,
        credit_gate=credit_gate,
        memory=memory,
        subscriber=subscriber,
    )


class PythonLoopSuperAgent:
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
        # Keep the injected double for contract assertions; drive turns through
        # the langchain-core chat-model interface wrapped around it.
        self.model = model
        self.chat = ScriptedLangChainChatModel(scripted=model)
        self.store = store
        self.credit_gate = credit_gate
        self.memory = memory
        self.subscriber = subscriber
        self._cold_start_ms = 0.0

    def tool_names(self, role: str) -> list[str]:
        return autonomy_tool_names(role)

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
            # Keep the stored identity; a restart often passes a default role.
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

        role = str(session["role"])
        lc_messages = transcript_to_messages(
            self.transcript(session_id),
            role=role,
            tool_names=self.tool_names(role),
            memory_text=memory_text,
            user_text=user_text,
        )
        tools = offered_tool_definitions(role)
        bound = self.chat.bind_tools(tools)
        first_round = True
        while True:
            if not first_round:
                # Every model round spends credit, so a tool call can end the turn.
                decision = self.credit_gate.authorize(
                    user_id=session["user_id"], session_id=session_id
                )
                if not decision.allowed:
                    emit("stop", decision.reason)
                    self._commit(session, seq, "stopped", transcript_rows, event_rows)
                    return published
            first_round = False
            saw_tool = False
            # Per-chunk lockstep via BaseChatModel.astream: publish each token
            # before the chat model yields the next chunk.
            async for chunk in bound.astream(lc_messages):
                text = chunk.content if isinstance(chunk.content, str) else ""
                if text:
                    emit("token", text, transcript_kind="assistant")
                request = tool_request_from_chunk(chunk)
                if request is not None:
                    name, arguments = request
                    handle_tool_request(
                        name,
                        arguments,
                        role=role,
                        messages=lc_messages,
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
