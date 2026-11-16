"""A minimal LangGraph wrapper around a vendor-native agent turn.

The graph is deliberately empty except for one node: the CLI owns the tool loop,
prompt and history. The graph exists so Conductor's existing run admission,
checkpointing, SSE producer and finalize/transcript pipeline can carry a native
thread without rewriting them.

The node reads only the current turn's last human message. It never replays
Conductor's transcript into the CLI and never injects Conductor system prompts; the
native session is the canonical conversation. The returned AIMessage is the
projection Conductor persists for display/search.

Approval is the one exception to "one node, one pass": a native CLI can pause
mid-turn on a human decision. The runtime parks the live process in the
approval broker and this node calls LangGraph's ``interrupt`` so the ordinary
HITL pause/resume pipeline owns the card and the user's answer.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from typing import Annotated, Any, Callable, Awaitable, AsyncIterator, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from src.server.services.native_runtime.approvals import get_approval_broker
from src.server.services.native_runtime.contracts import (
    NativeEvent,
    NativeRuntime,
    NativeRuntimeError,
    NativeRuntimeSpec,
)

logger = logging.getLogger(__name__)

#: How long a native turn waits between the notices it may take in. The
#: notice is a message to a Worker that stopped moving, so the wait is short
#: enough to reach a hung Worker and long enough not to read the outbox on
#: every event.
_NOTICE_POLL_SECONDS = 10.0

#: The marker a finished stream leaves in the reader's queue: the turn is
#: over, and no further event will arrive.
_TURN_ENDED = object()


def _queued_failure(buffer: deque) -> BaseException | None:
    """A runtime failure already buffered, wherever it sits among the events.

    The reader queues the failure and then the end marker. Returning a notice
    or a normal end while that failure is anywhere in the buffer would finish
    the turn with the text it already streamed.
    """
    for item in buffer:
        if isinstance(item, BaseException):
            return item
    return None


class NativeTurnState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def _last_human_text(messages: list[AnyMessage] | None) -> str:
    for message in reversed(messages or []):
        if isinstance(message, HumanMessage):
            return str(message.content or "")
    raise NativeRuntimeError("native turn has no user message")


async def _close_stream(stream: AsyncIterator[NativeEvent]) -> None:
    """Close a native event stream so the turn can hand back what it holds.

    A native runtime keeps the provider lock for the whole turn, so a turn
    that stops early -- the graph raises on an error event -- has to give the
    lock back now instead of waiting for the generator to be collected.
    Closing a paused turn is safe: the runtime keeps the lock while an
    approval is pending and releases it when the turn resumes.
    """
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:
        # Cleanup must never mask the error that ended the turn.
        logger.debug("closing a native event stream failed", exc_info=True)


async def _consume_event(
    event: NativeEvent,
    *,
    writer: Callable[[Any], None],
    text_parts: list[str],
    message_id: str,
    tool_projection: list[AnyMessage],
    tool_calls_seen: set[str],
    on_usage: Callable[[dict[str, Any]], Awaitable[None]] | None,
    segment: dict[str, Any] | None = None,
) -> None:
    """Project one native event into the live wire and checkpoint shape.

    ``segment`` tracks where the turn's final assistant message starts in
    ``text_parts``: a new message item starts one, and a tool event, a
    reasoning delta or an approval pause ends the current one once more text
    follows. Text written before a tool call is then not part of the last
    message, and a tool event after the last text leaves that text final.
    """

    if event.kind == "text_delta" and event.text:
        if segment is not None:
            item_id = str((event.data or {}).get("item_id") or "")
            new_item = bool(item_id) and segment.get("item") not in (None, item_id)
            if segment.get("pending") or new_item:
                segment["start"] = len(text_parts)
                segment["pending"] = False
            if item_id:
                segment["item"] = item_id
        text_parts.append(event.text)
        # The final AIMessage is what LangGraph checkpoints; this custom
        # event is the live wire chunk. Both share message_id so the producer
        # suppresses the complete-message duplicate.
        payload = _event_dict(event)
        payload["message_id"] = message_id
        writer({"type": "native_event", "payload": payload})
        return
    if event.kind == "thinking_delta" and event.text:
        if segment is not None and text_parts:
            segment["pending"] = True
        writer({"type": "native_event", "payload": _event_dict(event)})
        return
    if event.kind in ("tool_started", "tool_result", "approval_requested"):
        if segment is not None:
            segment["pending"] = True
            segment["item"] = None
        writer({"type": "native_event", "payload": _event_dict(event)})
        data = dict(event.data)
        call_id = event.tool_call_id or str(data.get("id") or "")
        if event.kind == "tool_started" and call_id and call_id not in tool_calls_seen:
            tool_calls_seen.add(call_id)
            tool_projection.append(
                AIMessage(
                    content="",
                    id=_projection_id("call", message_id, call_id),
                    additional_kwargs={"native_projection": True},
                    tool_calls=[
                        {
                            "name": event.tool_name or str(data.get("name") or "tool"),
                            "args": data.get("args") or {},
                            "id": call_id,
                            "type": "tool_call",
                        }
                    ],
                )
            )
        elif event.kind == "tool_result" and call_id:
            if call_id not in tool_calls_seen:
                tool_calls_seen.add(call_id)
                tool_projection.append(
                    AIMessage(
                        content="",
                        id=_projection_id("call", message_id, call_id),
                        tool_calls=[
                            {
                                "name": event.tool_name or str(data.get("name") or "tool"),
                                "args": {},
                                "id": call_id,
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            tool_projection.append(
                ToolMessage(
                    content=str(data.get("content") or "")[:20000],
                    tool_call_id=call_id,
                    id=_projection_id("result", message_id, call_id),
                    status=str(data.get("status") or "success"),
                    additional_kwargs={"native_projection": True},
                )
            )
        return
    if event.kind == "usage":
        if on_usage is not None:
            await on_usage(dict(event.data))
        data = dict(event.data)
        writer(
            {
                "type": "context_window",
                "action": "token_usage",
                "agent": "main",
                "input_tokens": data.get("input_tokens", 0),
                "output_tokens": data.get("output_tokens", 0),
                "total_tokens": data.get("total_tokens", 0),
                "cache_read_tokens": data.get("cache_read_tokens", 0),
            }
        )
        writer({"type": "native_event", "payload": _event_dict(event)})
        return
    if event.kind == "error":
        raise NativeRuntimeError(event.text or "native runtime failed")


_NO_RESULT = "No result was recorded for this tool call."


def _projection_id(kind: str, message_id: str, call_id: str) -> str:
    """A projected message's id, scoped to the turn that produced it.

    The CLI numbers its tool items from the start of every turn, so a bare call
    id repeats across turns, and the ``add_messages`` reducer replaces a message
    that has the id of an earlier one, in place: turn 2's ``item_1`` would
    overwrite turn 1's.
    """
    return f"native-tool-{kind}-{message_id.removeprefix('native-')}-{call_id}"


def _pair_tool_calls(
    projection: list[AnyMessage], *, message_id: str
) -> list[AnyMessage]:
    """Put each projected tool call directly before its tool message.

    A provider takes an assistant tool call only when a tool message answers it
    right behind it, and the projection arrives as the CLI's events do (call,
    call, result, result). A result can also be missing without the turn failing
    (an oversized CLI record is skipped, or the CLI ends the turn mid-call), so
    a call with no result gets a tool message that says so instead of leaving a
    dangling one in the checkpoint.
    """

    results: dict[str, AnyMessage] = {
        message.tool_call_id: message
        for message in projection
        if isinstance(message, ToolMessage)
    }
    paired: list[AnyMessage] = []
    for message in projection:
        if isinstance(message, ToolMessage):
            continue
        paired.append(message)
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls or []:
            call_id = str(call.get("id") or "")
            if not call_id:
                continue
            answer = results.pop(call_id, None)
            if answer is None:
                answer = ToolMessage(
                    content=_NO_RESULT,
                    tool_call_id=call_id,
                    id=_projection_id("result", message_id, call_id),
                    status="error",
                    additional_kwargs={"native_projection": True},
                )
            paired.append(answer)
    # A result whose call was not projected cannot occur (the consumer
    # synthesizes the call), but nothing the CLI reported is dropped here.
    paired.extend(results.values())
    return paired


def build_native_graph(
    *,
    runtime: NativeRuntime,
    spec: NativeRuntimeSpec,
    checkpointer: Any = None,
    on_session: Callable[[str], Awaitable[None]] | None = None,
    on_usage: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    context_block: str | None = None,
    thread_id: str | None = None,
    run_id: str | None = None,
    allow_empty_answer: bool = False,
):
    """Compile the one-node native graph for a thread.

    ``on_session`` is called with the CLI's new session id after a successful
    turn, so the caller can persist it on the thread. The callback runs inside
    the node after the runtime completes, not from a background task.

    ``run_id`` is the run this turn belongs to. With it, the node takes in the
    notices the server sends into the running turn (see ``wake.py``): a native
    CLI owns its turn, so a notice that must reach it opens the next turn on
    the same session. Without it the turn reads nothing, and a chat turn
    costs no polling.

    The final AIMessage keeps the whole turn's text; when text came before a
    tool call or in an earlier message, ``native_final_text`` holds only the
    last message (a Worker's completion report). ``allow_empty_answer`` (a
    Worker turn) completes a turn that wrote no text with an empty message
    instead of failing it: that turn has no completion report, which the
    executor settles as waiting.
    """

    async def native_node(state: NativeTurnState) -> dict[str, Any]:
        prompt = _last_human_text(state.get("messages"))
        message_id = f"native-{uuid.uuid4().hex}"
        # Memory/workspace context is an explicit first-turn handoff. On a
        # resumed native session the CLI already owns the history; reinjecting
        # it every turn would change the native prompt and invalidate cache.
        if context_block and not spec.session_id:
            prompt = f"{context_block}\n\n{prompt}"
        writer = get_stream_writer()
        broker = get_approval_broker()
        pending = broker.get(thread_id)
        text_parts: list[str] = list(pending.text_parts) if pending is not None else []
        session_id = spec.session_id
        tool_projection: list[AnyMessage] = []
        tool_calls_seen: set[str] = set()
        segment: dict[str, Any] = {"start": 0, "item": None, "pending": False}

        async def take_notice() -> str:
            """The notice the server sent into this turn, or ""."""
            if not thread_id or not run_id:
                return ""
            from src.server.services.native_runtime.wake import take_native_notice

            try:
                return await take_native_notice(thread_id, run_id)
            except Exception:  # noqa: BLE001 - a failed read leaves it parked
                logger.warning(
                    "[NATIVE_WAKE] Reading a notice for thread=%s run=%s failed",
                    thread_id,
                    run_id,
                    exc_info=True,
                )
                return ""

        async def drain(stream: AsyncIterator[NativeEvent], queue: asyncio.Queue) -> None:
            try:
                async for event in stream:
                    await queue.put(event)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - the reader reports it
                # The failure is an event. The consumer raises it before it
                # treats the turn as finished. Re-raising here would leave
                # the failure only on this task, and the consumer's cleanup
                # discards that task's exception.
                queue.put_nowait(exc)
            finally:
                await _close_stream(stream)
                # The turn is over: the reader must not wait for an event that
                # will never come. The failure, when there is one, was queued
                # first.
                queue.put_nowait(_TURN_ENDED)

        def _pull_ready(queue: asyncio.Queue, buffer: deque) -> None:
            while True:
                try:
                    buffer.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    return

        async def consume(
            stream: AsyncIterator[NativeEvent],
        ) -> tuple[str, Any]:
            """Consume the turn's events.

            Returns ``("approval", event)`` when the CLI asked for a decision,
            or ``("notice", text)`` when the server sent a notice into this
            turn: the CLI turn ends there, and the notice opens the next one
            on the same session. A runtime that fails raises, including when
            it already streamed some text. A queued failure is raised before
            a notice or a normal end is returned.

            Notices are polled on elapsed time. A CLI that keeps streaming
            still gets asked: the poll is not gated on an empty queue.
            """
            queue: asyncio.Queue = asyncio.Queue()
            reader = asyncio.create_task(drain(stream, queue))
            buffer: deque = deque()
            approval_event: NativeEvent | None = None
            notices = bool(thread_id and run_id)
            # A notice taken while events were still buffered: those events
            # are consumed first, so nothing the CLI already said is lost.
            pending_notice = ""
            next_poll = time.monotonic() + _NOTICE_POLL_SECONDS
            try:
                while True:
                    _pull_ready(queue, buffer)
                    failure = _queued_failure(buffer)
                    if failure is not None:
                        raise failure
                    if pending_notice and not buffer:
                        return ("notice", pending_notice)
                    # Elapsed time, not an empty queue. A CLI that never lets
                    # the queue drain would otherwise never be asked.
                    if (
                        notices
                        and not pending_notice
                        and time.monotonic() >= next_poll
                    ):
                        next_poll = time.monotonic() + _NOTICE_POLL_SECONDS
                        notice = await take_notice()
                        # The reader runs during that read. Give it one turn
                        # so a failure it has already hit is visible before
                        # the notice is allowed to end the turn.
                        await asyncio.sleep(0)
                        _pull_ready(queue, buffer)
                        failure = _queued_failure(buffer)
                        if failure is not None:
                            raise failure
                        if notice:
                            pending_notice = notice
                            continue
                    if buffer:
                        event = buffer.popleft()
                    elif notices:
                        timeout = max(0.0, next_poll - time.monotonic())
                        # A zero timeout must still yield, or a poll that is
                        # already due spins and never lets the reader run.
                        if timeout <= 0:
                            await asyncio.sleep(0)
                            continue
                        try:
                            event = await asyncio.wait_for(queue.get(), timeout)
                        except asyncio.TimeoutError:
                            continue
                    else:
                        event = await queue.get()
                    if isinstance(event, BaseException):
                        raise event
                    if event is _TURN_ENDED:
                        _pull_ready(queue, buffer)
                        failure = _queued_failure(buffer)
                        if failure is not None:
                            raise failure
                        if pending_notice:
                            # The turn ended on its own after the notice was
                            # taken; the notice still opens the next one.
                            return ("notice", pending_notice)
                        return ("approval", None)
                    session_id_local = event.session_id
                    if session_id_local:
                        nonlocal session_id
                        session_id = session_id_local
                    if event.kind == "approval_requested":
                        if pending_notice:
                            # The CLI paused before the notice could open a
                            # turn: give it back, so the pause does not
                            # confirm a notice the CLI never read.
                            from src.server.services.native_runtime.wake import (
                                release_native_notice,
                            )

                            await release_native_notice(thread_id, run_id)
                            pending_notice = ""
                        approval_event = event
                        # The pause ends the current message: text after
                        # the decision is a new one.
                        segment["pending"] = True
                        segment["item"] = None
                        break
                    await _consume_event(
                        event,
                        writer=writer,
                        text_parts=text_parts,
                        message_id=message_id,
                        tool_projection=tool_projection,
                        tool_calls_seen=tool_calls_seen,
                        on_usage=on_usage,
                        segment=segment,
                    )
            except BaseException:
                # A failure after a notice was held must not leave it
                # acknowledged. Nothing is held until take_notice, and a
                # release with an empty hold does not touch the outbox.
                if thread_id and run_id:
                    from src.server.services.native_runtime.wake import (
                        release_native_notice,
                    )

                    await release_native_notice(thread_id, run_id)
                raise
            finally:
                # The stream is closed before the next one opens: the runtime
                # holds the provider lock for the whole turn. The reader's
                # own exception is not the result: the queued failure is.
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            return ("approval", approval_event)

        async def follow_notice(notice_prompt: str) -> tuple[str, Any]:
            """Open the replacement turn. A failure before it is accepted releases the notice."""
            try:
                return await consume(
                    runtime.stream_turn(
                        notice_prompt, session_id=session_id, cwd=spec.cwd
                    )
                )
            except BaseException:
                if thread_id and run_id:
                    from src.server.services.native_runtime.wake import (
                        release_native_notice,
                    )

                    await release_native_notice(thread_id, run_id)
                raise

        async def accept_notice() -> None:
            """Confirm the notices a turn that already started has carried.

            Called before the node pauses on an approval (``interrupt`` raises
            out of the node, so nothing after it runs on this invocation) and
            when the node returns. The CLI turn the notice opened has already
            read it, so a failed confirm is logged, not raised: failing the
            completed turn over an acknowledgement would lose its work, and
            the hold is settled by the run's ending.
            """
            if not (thread_id and run_id):
                return
            from src.server.services.native_runtime.wake import (
                accept_native_notice,
            )

            try:
                await accept_native_notice(thread_id, run_id)
            except Exception:  # noqa: BLE001 - see the docstring
                logger.warning(
                    "[NATIVE_WAKE] Confirming a notice for thread=%s run=%s failed",
                    thread_id,
                    run_id,
                    exc_info=True,
                )

        if pending is not None:
            # A fresh node seeded with text streamed before the pause: what
            # follows the decision is a new message.
            if text_parts:
                segment["pending"] = True
            # Resume a native turn that paused on a human decision. The live
            # CLI process is still parked in the broker; the interrupt call
            # returns the HITL map this node paused on.
            response = interrupt(pending.interrupt_payload)
            outcome, payload = await consume(pending.runtime.resume_turn(response))
        else:
            outcome, payload = await consume(
                runtime.stream_turn(prompt, session_id=spec.session_id, cwd=spec.cwd)
            )

        while outcome == "notice":
            # The server said something to this Worker while it was running.
            # The CLI turn ends there and the notice opens the next one on
            # the same session, so the CLI keeps its history and reads what
            # was said to it.
            prompt = str(payload)
            segment["start"] = len(text_parts)
            segment["pending"] = True
            segment["item"] = None
            outcome, payload = await follow_notice(prompt)

        approval_event = payload if outcome == "approval" else None

        while approval_event is not None:
            pending = broker.get(thread_id)
            if pending is None:
                raise NativeRuntimeError(
                    "native runtime requested approval without a parked turn"
                )
            # The notice that opened this turn was carried: the pause must
            # not leave it held with no task left to accept or release it.
            await accept_notice()
            response = interrupt(pending.interrupt_payload)
            # The broker's pending record carries the text streamed before the
            # pause; seed it only on a fresh graph invocation.
            if not text_parts:
                text_parts.extend(pending.text_parts)
                if text_parts:
                    segment["pending"] = True
            outcome, payload = await consume(
                pending.runtime.resume_turn(response)
            )
            while outcome == "notice":
                prompt = str(payload)
                segment["start"] = len(text_parts)
                segment["pending"] = True
                segment["item"] = None
                outcome, payload = await follow_notice(prompt)
            approval_event = payload if outcome == "approval" else None

        answer = "".join(text_parts).strip()
        if not answer and not allow_empty_answer:
            raise NativeRuntimeError("native runtime produced no final answer")
        if session_id != spec.session_id and on_session is not None:
            await on_session(session_id)
        additional_kwargs: dict[str, Any] = {
            "native_provider": runtime.provider,
            "native_session_id": session_id,
        }
        final_text = "".join(text_parts[segment["start"] :]).strip()
        if final_text != answer:
            additional_kwargs["native_final_text"] = final_text

        # The replacement turn has finished and this return is the state the
        # graph checkpoints. The notice stays owed until that acceptance.
        await accept_notice()

        return {
            "messages": [
                *_pair_tool_calls(tool_projection, message_id=message_id),
                AIMessage(
                    content=answer,
                    id=message_id,
                    additional_kwargs=additional_kwargs,
                ),
            ]
        }

    builder = StateGraph(NativeTurnState)
    builder.add_node("native", native_node)
    builder.add_edge(START, "native")
    builder.add_edge("native", END)
    return builder.compile(checkpointer=checkpointer)


def _event_dict(event: NativeEvent) -> dict[str, Any]:
    return {
        "kind": event.kind,
        "text": event.text,
        "session_id": event.session_id,
        "tool_name": event.tool_name,
        "tool_call_id": event.tool_call_id,
        "data": dict(event.data),
    }
