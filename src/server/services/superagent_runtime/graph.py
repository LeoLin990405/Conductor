"""One-node LangGraph wrapper that runs one P turn on the run's checkpointer.

Same reason as ``services/native_runtime/graph.py``: the graph exists only so
admission, the SSE producer, checkpointing, finalize and transcript rendering
carry a turn that is not a PTC graph.

The graph is compiled on PTC's ``MainAgentState`` rather than a bare
``add_messages`` state. A Router thread already holds PTC checkpoints, whose
``messages`` channel is a ``DeltaChannel`` (``ptc_agent/agent/state.py``): a
plain ``add_messages`` channel cannot read its sentinel/snapshot blobs, and a
graph that leaves PTC's other channels undeclared erases their inline values
on its first write (``main_state.py``). The node itself only reads and writes
``messages``, so either runtime can continue the other's thread after a
switch or a rollback.

The node never calls ``interrupt()``. A credit stop ends the node normally
with the messages produced so far plus a stop note, and the lane turns the
recorded outcome into ``CreditStopError`` after the stream (Planner ruling 1).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.config import get_stream_writer
from langgraph.constants import TAG_HIDDEN
from langgraph.graph import END, START, StateGraph

from .bindings import NativeEventSink
from .ports import CreditPort, RecallPort, ToolPort, TurnInput

NODE_NAME = "superagent"
MAX_ROUNDS_REASON = "max_rounds"

TurnInputFactory = Callable[[list[BaseMessage], str], TurnInput]


@dataclass
class SuperagentOutcome:
    """What the node learned, read by the lane after the stream ends.

    The node cannot raise on a credit stop without losing the messages it is
    returning, so it records the stop here and the lane raises afterwards.
    """

    status: str = ""
    stop_reason: str = ""
    rounds: int = 0

    @property
    def credit_stopped(self) -> bool:
        return self.status == "stopped" and self.stop_reason != MAX_ROUNDS_REASON


@dataclass
class SuperagentPorts:
    """The loop's injected ports plus the per-run outcome holder."""

    credit: CreditPort
    recall: RecallPort
    tools: ToolPort
    callbacks: Sequence[Any] = ()
    max_rounds: int = 12
    prepare_messages: Callable[[list[Any]], list[Any]] | None = None
    outcome: SuperagentOutcome = field(default_factory=SuperagentOutcome)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    from .messages import content_text

    return content_text(content)


def _stop_note(reason: str, *, max_rounds: int) -> AIMessage:
    if reason == MAX_ROUNDS_REASON:
        text = f"Stopped: this turn reached its limit of {max_rounds} model rounds."
        kind = "max_rounds"
    else:
        text = reason
        kind = "credit"
    return AIMessage(
        content=text,
        id=f"superagent-stop-{uuid.uuid4().hex}",
        additional_kwargs={"superagent_stop": kind},
    )


def build_superagent_graph(
    chat: Any,
    turn_input_factory: TurnInputFactory,
    ports: SuperagentPorts,
    *,
    checkpointer: Any,
    state_schema: Any = None,
):
    """Compile the one-node P graph.

    ``turn_input_factory(history, user_text)`` builds the loop's ``TurnInput``
    from the checkpointed messages before this turn and the text of this
    turn's ``HumanMessage``, which the caller sends as the graph input so the
    checkpoint holds the user's message even if the turn fails.
    ``state_schema`` defaults to PTC's ``MainAgentState`` (module docstring).
    """
    if state_schema is None:
        from ptc_agent.agent.main_state import MainAgentState

        state_schema = MainAgentState

    async def superagent_node(state: dict[str, Any]) -> dict[str, Any]:
        from .loop import run_turn

        messages = list(state.get("messages") or [])
        if not messages or not isinstance(messages[-1], HumanMessage):
            raise ValueError("superagent turn has no user message")
        current = messages[-1]
        turn = turn_input_factory(messages[:-1], _text_of(current.content))
        result = await run_turn(
            chat,
            turn,
            credit=ports.credit,
            recall=ports.recall,
            tools=ports.tools,
            sink=NativeEventSink(get_stream_writer()),
            callbacks=tuple(ports.callbacks),
            max_rounds=ports.max_rounds,
            prepare_messages=ports.prepare_messages,
        )
        outcome = ports.outcome
        outcome.status = result.status
        outcome.stop_reason = result.stop_reason
        outcome.rounds = result.rounds

        new_messages = list(result.new_messages)
        # The loop's HumanMessage is this turn's input, already in state with
        # the id the caller minted; appending it again would duplicate it.
        if new_messages and isinstance(new_messages[0], HumanMessage):
            new_messages = new_messages[1:]
        if result.status == "stopped":
            new_messages.append(
                _stop_note(result.stop_reason, max_rounds=ports.max_rounds)
            )
        return {"messages": new_messages}

    builder = StateGraph(state_schema)
    builder.add_node(NODE_NAME, superagent_node)
    builder.add_edge(START, NODE_NAME)
    builder.add_edge(NODE_NAME, END)
    graph = builder.compile(checkpointer=checkpointer)
    # Every live frame already left through ``native_event``. Without this
    # tag LangGraph's ``messages`` stream re-emits the node's returned
    # messages at node end, and the producer would send each ToolMessage a
    # second time, attributed to a lane named after this node. ``add_node``
    # takes no tags, so the compiled node is tagged directly.
    node = graph.nodes[NODE_NAME]
    node.tags = [*(node.tags or []), TAG_HIDDEN]
    return graph


_BRANCH_PREFIX = "branch:to:"
_INTERRUPT_CHANNEL = "__interrupt__"
_TASKS_CHANNEL = "__pregel_tasks"


def checkpoint_has_pending_work(checkpoint_tuple: Any) -> bool:
    """True when a thread's latest checkpoint is paused or mid-run for a graph
    other than this one.

    Read straight off the checkpoint because ``aget_state`` only computes
    ``next`` for the reading graph's own nodes, and a PTC checkpoint names
    PTC's nodes. New input discards only the receiving graph's unfinished
    tasks, so a P turn on top of a paused or unfinished PTC run would leave
    that state behind; such a turn goes to PTC instead (PRD 4.2 point 3).
    This node's own leftover trigger (a P turn that failed) is ignored: the
    next P input discards it, exactly as LangGraph does for any graph.
    ``__start__`` is ignored for the same reason, since every graph has it.
    """
    if checkpoint_tuple is None:
        return False
    for write in checkpoint_tuple.pending_writes or ():
        channel = write[1] if len(write) > 1 else None
        if channel in (_INTERRUPT_CHANNEL, _TASKS_CHANNEL):
            return True
    checkpoint = checkpoint_tuple.checkpoint or {}
    versions = checkpoint.get("channel_versions") or {}
    seen = checkpoint.get("versions_seen") or {}
    # Consuming a trigger bumps its version too, so a newer version alone does
    # not mean pending: only a trigger that still holds a value does.
    live = checkpoint.get("channel_values") or {}
    # PTC's create_agent runs tools through Send: a turn that died inside a
    # tool leaves its queued calls here, with no branch trigger at all.
    if live.get(_TASKS_CHANNEL):
        return True
    for channel, version in versions.items():
        if not channel.startswith(_BRANCH_PREFIX) or channel not in live:
            continue
        node = channel[len(_BRANCH_PREFIX) :]
        if node == NODE_NAME:
            continue
        seen_version = (seen.get(node) or {}).get(channel)
        if seen_version is None or _version_newer(version, seen_version):
            return True
    return False


def _version_newer(version: Any, seen_version: Any) -> bool:
    try:
        return version > seen_version
    except TypeError:
        # Mixed int/str versions from different saver generations: only
        # equality is meaningful, and an unseen version means pending.
        return str(version) != str(seen_version)
