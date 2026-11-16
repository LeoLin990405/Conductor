"""Glue-only SuperAgent slot over ``superagent_runtime.loop.run_turn``.

Wraps the contract's scripted double in ``ScriptedLangChainChatModel``, maps
store rows to ``TurnInput.history``, adapts credit/memory/subscriber/tool
ports, and commits ``TurnResult`` in one ``next_seq`` CAS transaction. The
turn order itself is only ``run_turn`` — not a second loop.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
)
from langchain_core.outputs import ChatGenerationChunk, ChatResult

from prototypes.superagent.contract import (
    FAKE_TOOL_HANDLERS,
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
    scripted_tool_call_text,
)
from prototypes.superagent.p_python.chat_model import (
    ScriptedLangChainChatModel,
    _chunk_to_ai_message,
)
from prototypes.superagent.p_python.messages import messages_to_scripted_dicts

_RUNTIME_MODULES = (
    "src.server.services.superagent_runtime.loop",
    "src.server.services.superagent_runtime.ports",
    "src.server.services.superagent_runtime.prompts",
)


def unavailable_reason() -> str | None:
    """Skip when the P-lane runtime package is not on this tree yet."""

    for name in _RUNTIME_MODULES:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError:
            return f"{name} is not installed"
    return None


def _load_runtime():
    """Import run_turn and port types only when a turn actually runs."""

    from src.server.services.superagent_runtime.loop import run_turn
    from src.server.services.superagent_runtime.ports import (
        CreditVerdict,
        LoopEvent,
        TurnInput,
    )
    from src.server.services.superagent_runtime.prompts import (
        SUPERVISOR_SYSTEM_PROMPT,
        WATCHDOG_SYSTEM_PROMPT,
    )

    return run_turn, CreditVerdict, LoopEvent, TurnInput, (
        SUPERVISOR_SYSTEM_PROMPT,
        WATCHDOG_SYSTEM_PROMPT,
    )


def make_adapter(
    *,
    model: ScriptedFakeChatModel,
    store: FakePostgresStore,
    credit_gate: FakeCreditGate,
    memory: FakeMemoryRecall,
    subscriber: EventSubscriber,
    runtime: RuntimeContext,
) -> ProductionSuperAgent:
    """Build the production-loop adapter from the contract doubles."""

    del runtime
    provider = getattr(model, "provider_name", None)
    if provider != "scripted":
        raise RuntimeError(f"production adapter refuses provider {provider!r}")
    return ProductionSuperAgent(
        model=model,
        store=store,
        credit_gate=credit_gate,
        memory=memory,
        subscriber=subscriber,
    )


class _ScriptedChat(ScriptedLangChainChatModel):
    """Scripted chat that forwards structured ``AIMessage.tool_calls`` to the double.

    ``ScriptedLangChainChatModel`` flattens tool calls to plain content; the
    production loop keeps real ``tool_calls`` / ``ToolMessage`` pairs, so the
    conversion happens here instead of editing ``p_python``.
    """

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del stop, run_manager
        tools = kwargs.get("tools")
        dicts = _messages_to_scripted_dicts(messages)
        async for chunk in self.scripted.stream(dicts, tools=tools):
            yield ChatGenerationChunk(message=_chunk_to_ai_message(chunk))

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ):
        del stop, run_manager
        tools = kwargs.get("tools")
        dicts = _messages_to_scripted_dicts(messages)
        self.scripted.open_round(dicts, tools)
        while (chunk := self.scripted.next_chunk()) is not None:
            yield ChatGenerationChunk(message=_chunk_to_ai_message(chunk))


def _messages_to_scripted_dicts(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """Like ``messages_to_scripted_dicts``, but keeps OpenAI-shaped tool_calls."""

    rows = messages_to_scripted_dicts(messages)
    for message_index, message in enumerate(messages):
        if not isinstance(message, AIMessage) or not message.tool_calls:
            continue
        if message_index >= len(rows):
            continue
        rows[message_index]["tool_calls"] = [
            {
                "type": "function",
                "id": str(call.get("id") or ""),
                "function": {
                    "name": str(call.get("name") or ""),
                    "arguments": json.dumps(
                        call.get("args") or {}, ensure_ascii=False, sort_keys=True
                    ),
                },
            }
            for call in message.tool_calls
        ]
    return rows


class ProductionSuperAgent:
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
        self.chat = _ScriptedChat(scripted=model)
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
        run_turn, CreditVerdict, LoopEvent, TurnInput, prompts = _load_runtime()
        supervisor_prompt, watchdog_prompt = prompts
        session = self._require_session(session_id)
        role = str(session["role"])
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

        class _Credit:
            async def authorize(self_inner):
                decision = self.credit_gate.authorize(
                    user_id=session["user_id"], session_id=session_id
                )
                return CreditVerdict(allowed=decision.allowed, reason=decision.reason)

        class _Recall:
            async def recall(self_inner) -> str:
                hits = self.memory.recall(
                    user_id=session["user_id"], query=user_text
                )
                text = "\n".join(hit.text for hit in hits)
                record("memory", text)
                return text

        class _Tools:
            def tool_objects(self_inner) -> list[dict[str, str]]:
                return [{"name": name} for name in autonomy_tool_names(role)]

            async def call(
                self_inner, name: str, args: dict[str, Any], call_id: str
            ) -> str:
                del call_id
                # Look up at call time: the contract spy replaces map values.
                handler = FAKE_TOOL_HANDLERS.get(name)
                if handler is None:
                    raise RuntimeError(f"no fake handler for {name}")
                return handler(args)

        class _Sink:
            def emit(self_inner, event) -> None:
                if event.kind == "text_delta":
                    emit("token", event.text, transcript_kind="assistant")
                elif event.kind == "thinking_delta":
                    return
                elif event.kind == "tool_started":
                    args = event.data.get("args") or {}
                    emit(
                        "tool_call",
                        _tool_call_transcript_text(
                            event.tool_name, args, event.tool_call_id
                        ),
                    )
                elif event.kind == "tool_result":
                    emit("tool_result", event.text)
                elif event.kind == "tool_refused":
                    # Persist the paired AI tool_call so restart can rebuild
                    # AIMessage(tool_calls)+ToolMessage; the live event stays
                    # tool_refused for the contract.
                    args = event.data.get("args") or {}
                    record(
                        "tool_call",
                        _tool_call_transcript_text(
                            event.tool_name or event.text,
                            args,
                            event.tool_call_id,
                        ),
                    )
                    emit("tool_refused", event.tool_name or event.text)
                elif event.kind == "stop":
                    emit("stop", event.text)
                elif event.kind == "done":
                    emit("done", "")

        # Contract scripts (no-shell) fire one refused tool per round; keep the
        # production default of 12 for real lanes, but allow long scripted runs.
        result = await run_turn(
            self.chat,
            _turn_input(
                TurnInput,
                role=role,
                system_prompt=_system_prompt_for(
                    role, supervisor_prompt, watchdog_prompt
                ),
                history=_history_from_store(self.store.transcript_rows(session_id)),
                user_text=user_text,
            ),
            credit=_Credit(),
            recall=_Recall(),
            tools=_Tools(),
            sink=_Sink(),
            max_rounds=128,
        )
        status = "ready" if result.status == "ready" else "stopped"
        self._commit(session, seq, status, transcript_rows, event_rows)
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


def _system_prompt_for(
    role: str, supervisor_prompt: str, watchdog_prompt: str
) -> str:
    """Static prompt for the stored role (router/supervisor → supervisor text)."""

    key = canonical_role(role)
    if key == "watchdog":
        return watchdog_prompt
    return supervisor_prompt


def _turn_input(
    turn_input_cls: Any,
    *,
    role: str,
    system_prompt: str,
    history: list[BaseMessage],
    user_text: str,
) -> Any:
    """Build ``TurnInput`` against whichever fields the ports module exposes."""

    field_names = {field.name for field in dataclasses.fields(turn_input_cls)}
    kwargs: dict[str, Any] = {
        "role": role,
        "history": history,
        "user_text": user_text,
        "system_prompt": system_prompt,
    }
    if "offered_tool_names" in field_names:
        kwargs["offered_tool_names"] = autonomy_tool_names(role)
    return turn_input_cls(
        **{key: value for key, value in kwargs.items() if key in field_names}
    )


def _history_from_store(rows: Sequence[dict[str, Any]]) -> list[BaseMessage]:
    """Rebuild langchain history from committed transcript rows.

    Built with the runtime's own message helpers and finished by its pairing
    repair, so a restarted session hands the loop the same shapes a
    production checkpoint would: every tool call paired with exactly one
    result, and no result without a call.
    """

    from src.server.services.superagent_runtime.messages import (
        refused_tool_messages,
        repair_tool_pairs,
        tool_call_ai_message,
        tool_result_message,
    )

    messages: list[BaseMessage] = []
    index = 0
    while index < len(rows):
        row = rows[index]
        kind = str(row["kind"])
        text = str(row["text"])
        if kind == "user":
            messages.append(HumanMessage(content=text))
        elif kind in {"assistant", "token"}:
            messages.append(AIMessage(content=text))
        elif kind == "memory":
            messages.append(
                HumanMessage(content=text, additional_kwargs={"role": "memory"})
            )
        elif kind == "tool_call":
            name, args, call_id = _parse_tool_call_text(text)
            follower = str(rows[index + 1]["kind"]) if index + 1 < len(rows) else ""
            if follower == "tool_refused":
                index += 1
                messages.extend(
                    refused_tool_messages(name=name, args=args, call_id=call_id)
                )
            else:
                messages.append(
                    tool_call_ai_message(name=name, args=args, call_id=call_id, content=text)
                )
                if follower == "tool_result":
                    index += 1
                    messages.append(
                        tool_result_message(
                            name=name, call_id=call_id, content=str(rows[index]["text"])
                        )
                    )
        elif kind == "tool_refused":
            # A refusal row with no preceding call row: still a valid pair.
            name = text.removeprefix("refused:").strip() or text
            messages.extend(refused_tool_messages(name=name))
        # A tool_result row with no call before it has nothing to pair with
        # and is not replayed.
        index += 1
    return repair_tool_pairs(messages)


def _tool_call_transcript_text(
    name: str, args: dict[str, Any], call_id: str
) -> str:
    """Durable tool-call text; includes id when it differs from the tool name."""

    payload: dict[str, Any] = {
        "arguments": dict(args),
        "name": name,
    }
    if call_id and call_id != name:
        payload["id"] = call_id
    # Keep the contract's canonical shape when id is the tool name.
    if "id" not in payload:
        return scripted_tool_call_text(name, args)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def new_tool_call_id() -> str:
    from src.server.services.superagent_runtime.loop import new_tool_call_id as _new

    return _new()


def _parse_tool_call_text(text: str) -> tuple[str, dict[str, Any], str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "tool", {}, new_tool_call_id()
    name = str(payload.get("name") or "tool")
    args = payload.get("arguments") or {}
    if not isinstance(args, dict):
        args = {}
    # Rows written without an id still need one unique to this pair; the
    # tool name would collide for two same-name calls.
    call_id = str(payload.get("id") or new_tool_call_id())
    return name, dict(args), call_id
