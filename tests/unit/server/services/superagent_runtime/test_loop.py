"""Unit tests for the pure P turn loop against the real sibling modules.

No stub modules, no server app, no network: the fake model is a hand-written
``BaseChatModel`` that yields prepared ``AIMessageChunk`` rounds.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from pydantic import ConfigDict, Field

from src.server.services.superagent_runtime import loop as loop_module
from src.server.services.superagent_runtime import messages as messages_module
from src.server.services.superagent_runtime import tools as tools_module
from src.server.services.superagent_runtime.loop import run_turn
from src.server.services.superagent_runtime.ports import (
    CreditVerdict,
    LoopEvent,
    TurnInput,
)

REPO_ROOT = Path(__file__).resolve().parents[5]


class ScriptedChunkChat(BaseChatModel):
    """Yields one prepared list of chunks per model round."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    rounds: list[list[AIMessageChunk]] = Field(default_factory=list)
    bound_tools: list[Any] = Field(default_factory=list)
    fail_times: int = 0
    fail_exc: Any = None
    fail_after_first_chunk: bool = False
    stream_attempts: int = 0
    seen_messages: list[list[BaseMessage]] = Field(default_factory=list)
    seen_configs: list[Any] = Field(default_factory=list)
    sink: Any = None
    sink_sizes_at_yield: list[int] = Field(default_factory=list)
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-chunk"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable:
        del kwargs
        self.bound_tools = list(tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        del messages, stop, run_manager, kwargs
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])

    async def astream(self, input: Any, config: Any = None, **kwargs: Any):  # noqa: A002
        self.seen_configs.append(config)
        async for chunk in super().astream(input, config=config, **kwargs):
            yield chunk

    async def _astream(
        self, messages, stop=None, run_manager=None, **kwargs
    ) -> AsyncIterator[ChatGenerationChunk]:
        del stop, run_manager, kwargs
        self.stream_attempts += 1
        self.seen_messages.append(list(messages))
        if self.fail_times > 0 and not self.fail_after_first_chunk:
            self.fail_times -= 1
            raise self.fail_exc or ConnectionError("transient")
        if self.cursor >= len(self.rounds):
            return
        chunks = self.rounds[self.cursor]
        self.cursor += 1
        for position, chunk in enumerate(chunks):
            if self.sink is not None:
                self.sink_sizes_at_yield.append(len(self.sink.events))
            yield ChatGenerationChunk(message=chunk)
            if self.fail_after_first_chunk and position == 0 and self.fail_times > 0:
                self.fail_times -= 1
                raise self.fail_exc or ConnectionError("transient")


@dataclass
class _Credit:
    allowed_checks: int | None = None
    reason: str = "Stopped by the credit gate."
    calls: int = 0
    log: list[str] | None = None

    async def authorize(self) -> CreditVerdict:
        self.calls += 1
        if self.log is not None:
            self.log.append("credit")
        if self.allowed_checks is None or self.calls <= self.allowed_checks:
            return CreditVerdict(allowed=True)
        return CreditVerdict(allowed=False, reason=self.reason)


@dataclass
class _Recall:
    text: str = ""
    calls: int = 0
    log: list[str] | None = None

    async def recall(self) -> str:
        self.calls += 1
        if self.log is not None:
            self.log.append("recall")
        return self.text


@dataclass
class _Tools:
    names: list[str] = field(default_factory=lambda: ["alpha", "beta"])
    errors: dict[str, Exception] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)

    def tool_objects(self) -> list[dict[str, str]]:
        return [{"name": name} for name in self.names]

    async def call(self, name: str, args: dict[str, Any], call_id: str) -> str:
        self.calls.append((name, dict(args), call_id))
        if name in self.errors:
            raise self.errors[name]
        return json.dumps({"ok": name, "args": args}, sort_keys=True)


@dataclass
class _Sink:
    events: list[LoopEvent] = field(default_factory=list)

    def emit(self, event: LoopEvent) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


def _turn(**kwargs: Any) -> TurnInput:
    values: dict[str, Any] = {
        "role": "supervisor",
        "system_prompt": "sys",
        "history": [],
        "user_text": "hi",
    }
    values.update(kwargs)
    return TurnInput(**values)


def _tc(
    *, name: str | None = None, args: str | None = None, call_id: str | None = None, index: int | None = 0
) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "index": index, "type": "tool_call_chunk"}


def _chunk(*parts: dict[str, Any], content: Any = "") -> AIMessageChunk:
    return AIMessageChunk(content=content, tool_call_chunks=list(parts))


def _text(text: str) -> AIMessageChunk:
    return AIMessageChunk(content=text)


async def _run(chat: ScriptedChunkChat, *, tools: _Tools | None = None, **kwargs: Any):
    sink = kwargs.pop("sink", None) or _Sink()
    tools = tools or _Tools()
    result = await run_turn(
        chat,
        kwargs.pop("turn", None) or _turn(),
        credit=kwargs.pop("credit", None) or _Credit(),
        recall=kwargs.pop("recall", None) or _Recall(),
        tools=tools,
        sink=sink,
        **kwargs,
    )
    return result, sink, tools


def _ai_with_calls(result) -> AIMessage:
    return next(m for m in result.new_messages if isinstance(m, AIMessage) and m.tool_calls)


def _tool_messages(result) -> list[ToolMessage]:
    return [m for m in result.new_messages if isinstance(m, ToolMessage)]


# --- turn order and credit -------------------------------------------------


@pytest.mark.asyncio
async def test_credit_then_recall_then_model() -> None:
    log: list[str] = []
    chat = ScriptedChunkChat(rounds=[[_text("ok")]])
    result, _sink, _ = await _run(chat, credit=_Credit(log=log), recall=_Recall(text="mem", log=log))
    assert log == ["credit", "recall"]
    assert result.status == "ready"
    assert result.memory_text == "mem"
    system = chat.seen_messages[0][0]
    assert isinstance(system, SystemMessage)
    assert "mem" in system.content
    assert isinstance(chat.seen_messages[0][-1], HumanMessage)


@pytest.mark.asyncio
async def test_credit_denied_before_first_round_skips_recall_and_model() -> None:
    chat = ScriptedChunkChat(rounds=[[_text("never")]])
    recall = _Recall(text="mem")
    result, sink, _ = await _run(chat, credit=_Credit(allowed_checks=0, reason="no credit"), recall=recall)
    assert result.status == "stopped"
    assert result.stop_reason == "no credit"
    assert recall.calls == 0
    assert chat.stream_attempts == 0
    assert sink.kinds() == ["stop"]
    assert [type(m) for m in result.new_messages] == [HumanMessage]


@pytest.mark.asyncio
async def test_mid_turn_credit_denial_keeps_paired_partial_messages() -> None:
    chat = ScriptedChunkChat(
        rounds=[[_chunk(_tc(name="alpha", args="{}", call_id="c1"))], [_text("never")]]
    )
    credit = _Credit(allowed_checks=1, reason="spent")
    result, sink, tools = await _run(chat, credit=credit)
    assert result.status == "stopped"
    assert result.stop_reason == "spent"
    assert result.rounds == 1
    assert credit.calls == 2
    assert [name for name, _, _ in tools.calls] == ["alpha"]
    assert [type(m) for m in result.new_messages] == [HumanMessage, AIMessage, ToolMessage]
    assert sink.kinds()[-1] == "stop"


@pytest.mark.asyncio
async def test_max_rounds_stops_before_round_thirteen() -> None:
    rounds = [[_chunk(_tc(name="alpha", args="{}", call_id=f"c{i}"))] for i in range(20)]
    chat = ScriptedChunkChat(rounds=rounds)
    credit = _Credit()
    result, sink, tools = await _run(chat, credit=credit)
    assert result.status == "stopped"
    assert result.stop_reason == "max_rounds"
    assert result.rounds == 12
    assert chat.stream_attempts == 12
    assert len(tools.calls) == 12
    assert credit.calls == 12
    assert sink.kinds()[-1] == "stop"


# --- streaming --------------------------------------------------------------


@pytest.mark.asyncio
async def test_lockstep_each_delta_reaches_sink_before_next_chunk() -> None:
    sink = _Sink()
    chat = ScriptedChunkChat(rounds=[[_text("a"), _text("b"), _text("c")]], sink=sink)
    result, _, _ = await _run(chat, sink=sink)
    assert chat.sink_sizes_at_yield == [0, 1, 2]
    assert [e.text for e in sink.events if e.kind == "text_delta"] == ["a", "b", "c"]
    final = result.new_messages[-1]
    assert isinstance(final, AIMessage)
    assert final.content == "abc"
    assert {e.message_id for e in sink.events} == {final.id}


@pytest.mark.asyncio
async def test_list_content_text_goes_to_answer_and_reasoning_to_thinking() -> None:
    chunk = AIMessageChunk(
        content=[
            {"type": "thinking", "thinking": "let me think"},
            {"type": "text", "text": "answer"},
        ]
    )
    chat = ScriptedChunkChat(rounds=[[chunk]])
    result, sink, _ = await _run(chat)
    assert [(e.kind, e.text) for e in sink.events if e.kind != "done"] == [
        ("thinking_delta", "let me think"),
        ("text_delta", "answer"),
    ]
    final = result.new_messages[-1]
    assert final.content == "answer"


@pytest.mark.asyncio
async def test_run_config_carries_callbacks_and_nostream_tag() -> None:
    from langchain_core.callbacks import BaseCallbackHandler

    marker = BaseCallbackHandler()
    chat = ScriptedChunkChat(rounds=[[_text("ok")]])
    await _run(chat, callbacks=[marker])
    config = chat.seen_configs[0]
    assert config["tags"] == ["nostream"]
    assert config["callbacks"] == [marker]


@pytest.mark.asyncio
async def test_connection_error_is_retried_once_before_any_output() -> None:
    chat = ScriptedChunkChat(rounds=[[_text("ok")]], fail_times=1)
    result, _, _ = await _run(chat)
    assert result.status == "ready"
    assert chat.stream_attempts == 2


@pytest.mark.asyncio
async def test_non_retryable_error_is_not_retried() -> None:
    chat = ScriptedChunkChat(rounds=[[_text("ok")]], fail_times=1, fail_exc=ValueError("bad request"))
    with pytest.raises(ValueError):
        await _run(chat)
    assert chat.stream_attempts == 1


@pytest.mark.asyncio
async def test_no_retry_after_output_reached_the_sink() -> None:
    chat = ScriptedChunkChat(rounds=[[_text("a"), _text("b")]], fail_times=1, fail_after_first_chunk=True)
    with pytest.raises(ConnectionError):
        await _run(chat)
    assert chat.stream_attempts == 1


def test_retryable_error_matches_5xx_status() -> None:
    class _Err(Exception):
        status_code = 503

    assert loop_module.is_retryable_error(_Err())
    assert not loop_module.is_retryable_error(RuntimeError("x"))


# --- tool calls ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_args_split_across_three_chunks_are_joined() -> None:
    chat = ScriptedChunkChat(
        rounds=[
            [
                _chunk(_tc(name="alpha", args='{"ticket', call_id="c1", index=0)),
                _chunk(_tc(args='_id": "T-', index=0)),
                _chunk(_tc(args='7"}', index=0)),
            ],
            [_text("done")],
        ]
    )
    result, _, tools = await _run(chat)
    assert tools.calls == [("alpha", {"ticket_id": "T-7"}, "c1")]
    assert _ai_with_calls(result).tool_calls[0]["args"] == {"ticket_id": "T-7"}


@pytest.mark.asyncio
async def test_two_calls_in_one_round_run_in_order_with_paired_messages() -> None:
    chat = ScriptedChunkChat(
        rounds=[
            [
                _chunk(_tc(name="alpha", args='{"n": 1}', call_id="c1", index=0)),
                _chunk(_tc(name="beta", args='{"n": 2}', call_id="c2", index=1)),
            ],
            [_text("done")],
        ]
    )
    result, sink, tools = await _run(chat)
    assert [(n, a, c) for n, a, c in tools.calls] == [("alpha", {"n": 1}, "c1"), ("beta", {"n": 2}, "c2")]
    ai = _ai_with_calls(result)
    assert [c["id"] for c in ai.tool_calls] == ["c1", "c2"]
    assert [m.tool_call_id for m in _tool_messages(result)] == ["c1", "c2"]
    assert [type(m) for m in result.new_messages] == [HumanMessage, AIMessage, ToolMessage, ToolMessage, AIMessage]
    # The second round sees the paired history.
    assert chat.seen_messages[1][-3:] == result.new_messages[1:4]
    assert [e.kind for e in sink.events if e.kind.startswith("tool")] == [
        "tool_started",
        "tool_result",
        "tool_started",
        "tool_result",
    ]


@pytest.mark.asyncio
async def test_refused_shell_tool_gets_error_tool_message_and_stays_in_ai_calls() -> None:
    chat = ScriptedChunkChat(
        rounds=[[_chunk(_tc(name="Bash", args='{"command": "ls"}', call_id="c1"))], [_text("ok")]]
    )
    tools = _Tools(names=["alpha", "Bash"])
    result, sink, tools = await _run(chat, tools=tools)
    assert tools.calls == []
    ai = _ai_with_calls(result)
    assert ai.tool_calls[0]["name"] == "Bash"
    (message,) = _tool_messages(result)
    assert message.status == "error"
    assert message.content == "refused: Bash"
    assert message.tool_call_id == ai.tool_calls[0]["id"]
    assert "tool_refused" in sink.kinds()
    assert "tool_started" not in sink.kinds()


@pytest.mark.asyncio
async def test_out_of_role_tool_is_refused() -> None:
    chat = ScriptedChunkChat(rounds=[[_chunk(_tc(name="gamma", args="{}", call_id="c1"))], [_text("ok")]])
    result, _, tools = await _run(chat)
    assert tools.calls == []
    assert _tool_messages(result)[0].content == "refused: gamma"


@pytest.mark.asyncio
async def test_offered_tool_names_narrow_the_allow_list() -> None:
    chat = ScriptedChunkChat(rounds=[[_chunk(_tc(name="beta", args="{}", call_id="c1"))], [_text("ok")]])
    result, _, tools = await _run(chat, turn=_turn(offered_tool_names=["alpha"]))
    assert tools.calls == []
    assert _tool_messages(result)[0].status == "error"


@pytest.mark.asyncio
async def test_tool_exception_and_bad_args_become_errors_and_loop_continues() -> None:
    chat = ScriptedChunkChat(
        rounds=[
            [
                _chunk(_tc(name="alpha", args="{}", call_id="c1", index=0)),
                _chunk(_tc(name="beta", args='{"x": ', call_id="c2", index=1)),
            ],
            [_text("recovered")],
        ]
    )
    tools = _Tools(errors={"alpha": RuntimeError("boom")})
    result, _, tools = await _run(chat, tools=tools)
    assert [name for name, _, _ in tools.calls] == ["alpha"]  # beta never ran
    first, second = _tool_messages(result)
    assert first.status == "error" and "boom" in first.content
    assert second.status == "error" and "invalid tool arguments" in second.content
    assert result.status == "ready"
    assert result.new_messages[-1].content == "recovered"


# Review blocker 1: a call without an id used to fall back to the tool name,
# so a malformed call could replace a valid call of the same name.
@pytest.mark.asyncio
async def test_same_name_calls_without_ids_stay_distinct() -> None:
    chat = ScriptedChunkChat(
        rounds=[
            [
                _chunk(_tc(name="alpha", args='{"n": 1}', index=0)),
                _chunk(_tc(name="alpha", args='{"n": ', index=1)),
            ],
            [_text("ok")],
        ]
    )
    result, _, tools = await _run(chat)
    ai = _ai_with_calls(result)
    ids = [call["id"] for call in ai.tool_calls]
    assert len(set(ids)) == 2
    assert "alpha" not in ids
    assert ai.tool_calls[0]["args"] == {"n": 1}
    assert tools.calls == [("alpha", {"n": 1}, ids[0])]
    valid, malformed = _tool_messages(result)
    assert (valid.tool_call_id, valid.status) == (ids[0], "success")
    assert (malformed.tool_call_id, malformed.status) == (ids[1], "error")


@pytest.mark.asyncio
async def test_repeated_provider_ids_are_made_unique() -> None:
    chat = ScriptedChunkChat(
        rounds=[
            [
                _chunk(_tc(name="alpha", args="{}", call_id="dup", index=0)),
                _chunk(_tc(name="beta", args="{}", call_id="dup", index=1)),
            ],
            [_text("ok")],
        ]
    )
    result, _, _ = await _run(chat)
    ids = [call["id"] for call in _ai_with_calls(result).tool_calls]
    assert ids[0] == "dup" and ids[1] != "dup"
    assert [m.tool_call_id for m in _tool_messages(result)] == ids


# Review blocker 2: valid calls used to be collected before invalid ones.
@pytest.mark.asyncio
async def test_mixed_validity_keeps_chunk_order_in_ai_message_and_feedback() -> None:
    chat = ScriptedChunkChat(
        rounds=[
            [
                _chunk(_tc(name="alpha", args="{bad", call_id="c0", index=0)),
                _chunk(_tc(name="beta", args='{"n": 1}', call_id="c1", index=1)),
                _chunk(_tc(name="alpha", args="[1]", call_id="c2", index=2)),
                _chunk(_tc(name="alpha", args='{"n": 3}', call_id="c3", index=3)),
            ],
            [_text("ok")],
        ]
    )
    result, sink, tools = await _run(chat)
    ai = _ai_with_calls(result)
    assert [c["id"] for c in ai.tool_calls] == ["c0", "c1", "c2", "c3"]
    feedback = _tool_messages(result)
    assert [(m.tool_call_id, m.status) for m in feedback] == [
        ("c0", "error"),
        ("c1", "success"),
        ("c2", "error"),
        ("c3", "success"),
    ]
    assert [c for _, _, c in tools.calls] == ["c1", "c3"]
    started = [e.tool_call_id for e in sink.events if e.kind == "tool_started"]
    assert started == ["c0", "c1", "c2", "c3"]


@pytest.mark.asyncio
async def test_mixed_validity_interleaved_chunks_keep_first_seen_order() -> None:
    # Arguments for two calls interleave across chunks; order is first-seen index.
    chat = ScriptedChunkChat(
        rounds=[
            [
                _chunk(_tc(name="beta", args='{"n"', call_id="b", index=1)),
                _chunk(_tc(name="alpha", args="{oops", call_id="a", index=0)),
                _chunk(_tc(args=": 2}", index=1)),
            ],
            [_text("ok")],
        ]
    )
    result, _, tools = await _run(chat)
    assert [c["id"] for c in _ai_with_calls(result).tool_calls] == ["b", "a"]
    assert [(m.tool_call_id, m.status) for m in _tool_messages(result)] == [("b", "success"), ("a", "error")]
    assert tools.calls == [("beta", {"n": 2}, "b")]


@pytest.mark.asyncio
async def test_whole_message_with_valid_and_invalid_calls_runs_only_the_valid_one() -> None:
    # A non-streaming fallback yields one whole message; LangChain turns its
    # valid and invalid calls into id-less chunks, valid first.
    whole = AIMessageChunk(
        content="",
        tool_calls=[{"name": "beta", "args": {"n": 1}, "id": "v", "type": "tool_call"}],
        invalid_tool_calls=[
            {"name": "alpha", "args": "{bad", "id": None, "error": "bad json", "type": "invalid_tool_call"}
        ],
    )
    chat = ScriptedChunkChat(rounds=[[whole], [_text("ok")]])
    result, _, tools = await _run(chat)
    calls = _ai_with_calls(result).tool_calls
    assert [c["name"] for c in calls] == ["beta", "alpha"]
    assert calls[1]["id"] and calls[1]["id"] != "alpha"
    assert tools.calls == [("beta", {"n": 1}, "v")]


# --- history ------------------------------------------------------------------


def _history_turn(i: int, size: int = 10) -> list[BaseMessage]:
    return [
        HumanMessage(content=f"q{i}" + "x" * size),
        AIMessage(content="", tool_calls=[{"name": "alpha", "args": {}, "id": f"h{i}", "type": "tool_call"}]),
        ToolMessage(content="r", tool_call_id=f"h{i}", name="alpha"),
        AIMessage(content=f"a{i}"),
    ]


@pytest.mark.asyncio
async def test_history_keeps_last_turns_without_splitting_tool_pairs() -> None:
    history = [m for i in range(6) for m in _history_turn(i)]
    chat = ScriptedChunkChat(rounds=[[_text("ok")]])
    await _run(chat, turn=_turn(history=history))
    sent = chat.seen_messages[0][1:-1]
    assert len(sent) == 16  # last 4 turns, 4 messages each
    assert sent[0].content.startswith("q2")
    declared = {c["id"] for m in sent if isinstance(m, AIMessage) for c in m.tool_calls}
    answered = {m.tool_call_id for m in sent if isinstance(m, ToolMessage)}
    assert declared == answered


@pytest.mark.asyncio
async def test_history_chars_budget_drops_oldest_turns() -> None:
    history = [m for i in range(4) for m in _history_turn(i, size=100)]
    chat = ScriptedChunkChat(rounds=[[_text("ok")]])
    await _run(chat, turn=_turn(history=history, history_chars=250))
    sent = chat.seen_messages[0][1:-1]
    assert [m.content[:2] for m in sent if isinstance(m, HumanMessage)] == ["q2", "q3"]


# --- module hygiene -------------------------------------------------------------


def test_loop_uses_the_real_sibling_modules() -> None:
    # Review blocker 3: an earlier conftest wrote stub modules into src/.
    package_dir = REPO_ROOT / "src" / "server" / "services" / "superagent_runtime"
    for module in (loop_module, messages_module, tools_module):
        assert Path(module.__file__).resolve().parent == package_dir
    assert loop_module.build_model_messages is messages_module.build_model_messages
    assert loop_module.is_shell_or_file_tool is tools_module.is_shell_or_file_tool
    assert not (Path(__file__).parent / "conftest.py").exists()


def test_import_does_not_load_server_app_or_ptc_agent() -> None:
    code = (
        "import sys, json\n"
        "import src.server.services.superagent_runtime.loop\n"
        "import src.server.services.superagent_runtime.messages\n"
        "import src.server.services.superagent_runtime.tools\n"
        "import src.server.services.superagent_runtime.prompts\n"
        "bad = [m for m in sys.modules if m.startswith(('src.server.app', 'ptc_agent', 'src.ptc_agent'))]\n"
        "print(json.dumps(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True, timeout=60
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []


@pytest.mark.asyncio
async def test_chunks_without_index_continue_the_previous_call() -> None:
    chat = ScriptedChunkChat(
        rounds=[
            [
                _chunk(_tc(name="alpha", args='{"n":', call_id="t1", index=None)),
                _chunk(_tc(args=" 1}", index=None)),
                _chunk(_tc(name="beta", args="{}", call_id="t2", index=None)),
            ],
            [_text("ok")],
        ]
    )
    result, _, tools = await _run(chat)
    assert tools.calls == [("alpha", {"n": 1}, "t1"), ("beta", {}, "t2")]
    assert [c["name"] for c in _ai_with_calls(result).tool_calls] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_nameless_call_is_dropped_not_replayed() -> None:
    chat = ScriptedChunkChat(
        rounds=[[_chunk(_tc(name=None, args="{}", call_id="x", index=0)), _text("plain")]]
    )
    result, _, tools = await _run(chat)
    assert tools.calls == []
    assert not any(isinstance(m, AIMessage) and m.tool_calls for m in result.new_messages)
    assert result.new_messages[-1].content == "plain"


@pytest.mark.asyncio
async def test_ids_reused_by_the_provider_across_rounds_are_made_unique() -> None:
    history = _history_turn(0)  # already holds id "h0"
    chat = ScriptedChunkChat(
        rounds=[
            [_chunk(_tc(name="alpha", args="{}", call_id="h0"))],
            [_chunk(_tc(name="alpha", args="{}", call_id="call_0"))],
            [_chunk(_tc(name="alpha", args="{}", call_id="call_0"))],
            [_text("ok")],
        ]
    )
    result, _, _ = await _run(chat, turn=_turn(history=history))
    ids = [m.tool_call_id for m in _tool_messages(result)]
    assert len(ids) == 3 and len(set(ids)) == 3
    assert "h0" not in ids and ids[1] == "call_0"


@pytest.mark.asyncio
async def test_prepare_messages_rewrites_only_the_outgoing_request() -> None:
    seen: list[int] = []

    def prepare(messages: list[BaseMessage]) -> list[BaseMessage]:
        seen.append(len(messages))
        return [*messages, HumanMessage(content="wire-only")]

    chat = ScriptedChunkChat(rounds=[[_chunk(_tc(name="alpha", args="{}", call_id="c1"))], [_text("ok")]])
    result, _, _ = await _run(chat, prepare_messages=prepare)
    assert len(seen) == 2
    assert all(batch[-1].content == "wire-only" for batch in chat.seen_messages)
    assert not any(m.content == "wire-only" for m in result.new_messages)


@pytest.mark.asyncio
async def test_unanswered_ptc_tool_call_in_history_is_paired_before_sending() -> None:
    history: list[BaseMessage] = [
        HumanMessage(content="q"),
        AIMessage(content="", tool_calls=[{"name": "alpha", "args": {}, "id": "lost", "type": "tool_call"}]),
        ToolMessage(content="stray", tool_call_id="orphan", name="alpha"),
    ]
    chat = ScriptedChunkChat(rounds=[[_text("ok")]])
    await _run(chat, turn=_turn(history=history))
    sent = chat.seen_messages[0]
    results = [m for m in sent if isinstance(m, ToolMessage)]
    assert [(m.tool_call_id, m.status) for m in results] == [("lost", "error")]


@pytest.mark.asyncio
async def test_connection_errors_are_retried_twice_then_raised() -> None:
    # Planner ruling 3: two retries, three attempts in total.
    chat = ScriptedChunkChat(rounds=[[_text("ok")]], fail_times=2)
    result, _, _ = await _run(chat)
    assert result.status == "ready"
    assert chat.stream_attempts == 3

    chat = ScriptedChunkChat(rounds=[[_text("ok")]], fail_times=3)
    with pytest.raises(ConnectionError):
        await _run(chat)
    assert chat.stream_attempts == 3


@pytest.mark.asyncio
async def test_index_less_fragments_repeating_the_id_are_one_call() -> None:
    chat = ScriptedChunkChat(
        rounds=[
            [
                _chunk(_tc(name="alpha", args='{"n"', call_id="a", index=None)),
                _chunk(_tc(args=": 1}", call_id="a", index=None)),
                _chunk(_tc(name="beta", args="{}", call_id="b", index=None)),
            ],
            [_text("ok")],
        ]
    )
    _result, _, tools = await _run(chat)
    assert tools.calls == [("alpha", {"n": 1}, "a"), ("beta", {}, "b")]


@pytest.mark.asyncio
async def test_index_less_fragments_repeating_the_name_without_id_are_one_call() -> None:
    chat = ScriptedChunkChat(
        rounds=[
            [
                _chunk(_tc(name="alpha", args='{"n"', index=None)),
                _chunk(_tc(name="alpha", args=": 1}", index=None)),
            ],
            [_text("ok")],
        ]
    )
    result, _, tools = await _run(chat)
    assert [(n, a) for n, a, _ in tools.calls] == [("alpha", {"n": 1})]
    assert len(_ai_with_calls(result).tool_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["NaN", '{"n": NaN}', '{"n": Infinity}', '{"n": -Infinity}'])
async def test_nan_and_infinity_arguments_are_rejected(raw: str) -> None:
    chat = ScriptedChunkChat(rounds=[[_chunk(_tc(name="alpha", args=raw, call_id="c1"))], [_text("ok")]])
    result, _, tools = await _run(chat)
    assert tools.calls == []
    (message,) = _tool_messages(result)
    assert message.status == "error" and "invalid tool arguments" in message.content
