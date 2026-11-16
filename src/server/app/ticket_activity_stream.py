"""Live Ticket activity as Server-Sent Events.

A Ticket's activity record is projected from its own rows (ticket inputs,
reports, close stamp).  That record is the durable part a person follows; the
Worker's token stream is a live extra.  The thread messaging routes cannot
express either: the replay route is history-only and the POST route starts a
new turn, so this module tails the Ticket's Worker run log directly and falls
back to its activity rows.

Two independent sources feed one SSE cursor:

* ``activity:<id>`` — a projected activity event, replayed from the DB.
* ``run:<seq>`` — one stored Worker run frame from the Ticket's Worker
  thread/run, replayed from the run's Redis event log.

The cursor is opaque to clients: ``<activity event id>~<run id>~<run seq>``.
Neither a run event id nor an activity event id contains ``~``; both parts may
be empty.  A reconnect that names a different run id starts the run source at
zero, because a seq from another run names unrelated frames.

Activity rows have no durable total order: inputs and reports are separate
tables read by separate queries, so an event can commit after its own table's
read but carry a timestamp earlier than a later row in the other table.  The
activity half of the cursor therefore names the last delivered event for
diagnostics only; every attach replays the full projection and the client
deduplicates by ``event_id``.  Parsing the activity cursor and skipping rows
before it would silently lose a row committed between the two reads.

Worker output comes from two live channels, both forwarded as ``output``
events (Codex reports completed items rather than token deltas, so its channel
is line-incremental; cloud run frames are forwarded as they arrive):

* Cloud/orchestration Worker runs (a Ticket with ``worker_thread_id`` and
  ``worker_run_id``) write into the run's Redis event log, which is tailed
  directly.
* Local daemon/native Workers post redacted model text and tool names to
  ``daemon_output`` (a bounded per-Ticket event log in the database, fenced by
  a server-allocated epoch) while the vendor CLI runs.  That channel is best
  effort: a store outage leaves the activity record and status frames intact,
  just without live tokens.  An epoch change, a taint or a trimmed gap is
  announced with an ``output_reset`` payload before any later text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any

from src.server.database.autonomy import tickets as tickets_db
from src.server.services.autonomy import daemon_output as daemon_output_store
from src.server.services.autonomy.task_interaction import project_task_activity

logger = logging.getLogger(__name__)

# One poll is one ticket read plus the two activity side reads.  Activity rows
# land at human scale (an instruction, a report, a close), so this pace keeps
# latency low without making the stream a DB load source.
ACTIVITY_STREAM_POLL_S = 1.0
# Same ceiling as the workspace/computer status streams; a watch that outlives
# it ends with a timeout frame instead of a dropped connection.
ACTIVITY_STREAM_MAX_S = 900.0
# Keepalive comment cadence for an idle stream (a token stream rarely idles).
ACTIVITY_STREAM_PING_S = 15.0
# A busy Worker can keep the output queue non-empty indefinitely. Drain in
# bounded batches so a single client (and the event loop) still re-checks
# Ticket state, keepalives and the stream ceiling between batches.
ACTIVITY_STREAM_OUTPUT_BATCH = 64
# Per-connection buffer bound. A paused HTTP consumer must push back on the
# run reader (which re-reads from its Redis cursor) instead of growing this
# process's memory with every frame the Worker produces.
ACTIVITY_STREAM_OUTPUT_QUEUE_MAX = 512
# A transient run-ledger/Redis error must not silently stop token delivery
# while the SSE keeps polling. Retry the tail from the last queued sequence
# with bounded exponential backoff; once exhausted, tell the client so it can
# reattach (a fresh connection gets a fresh retry budget).
ACTIVITY_STREAM_TAIL_RETRIES = 5
ACTIVITY_STREAM_TAIL_BACKOFF_S = 0.5
ACTIVITY_STREAM_TAIL_BACKOFF_MAX_S = 8.0
# A retry that then streams healthily for this long earns a fresh budget; a
# flapping tail does not (otherwise repeated one-frame bursts retry forever).
ACTIVITY_STREAM_TAIL_HEALTHY_S = 60.0
# How long a terminal ticket waits for its Worker run to close.  A Worker can
# persist its report (which can flip the Ticket terminal) and keep emitting
# model output inside the same run, so the watch holds the stream open until
# the ``run_end`` frame arrives.  The bound only exists so a run that never
# closes (or a stream we cannot tail) cannot hold the client forever.
ACTIVITY_STREAM_TERMINAL_RUN_GRACE_S = 30.0
# Daemon output arrives through a database event log, not an in-process queue, so a
# short dedicated cadence gives it token-stream latency without making the
# durable Ticket poll more frequent. Every read is a pool checkout per watcher,
# so the cadence holds at POLL_S while output flows, eases to MID_POLL_S once
# nothing has moved for FAST_HOLD_S (a model thinking between bursts, where the
# next token should still appear within half a second) and to IDLE_POLL_S after
# MID_HOLD_S, where a watcher on a quiet Ticket costs about what the durable poll
# does. The first event after a pause waits at most the delay of its tier, and
# the read after it is fast again.
ACTIVITY_STREAM_DAEMON_POLL_S = 0.25
ACTIVITY_STREAM_DAEMON_MID_POLL_S = 0.5
ACTIVITY_STREAM_DAEMON_IDLE_POLL_S = 1.0
ACTIVITY_STREAM_DAEMON_FAST_HOLD_S = 3.0
ACTIVITY_STREAM_DAEMON_MID_HOLD_S = 30.0
ACTIVITY_STREAM_DAEMON_BATCH = 64

CURSOR_SEP = "~"
# A cursor is echoed in the ``id:`` line of every frame, and an SSE line ends at
# a CR or LF: a part holding either would let the caller write fields (a forged
# ``end`` frame) into its own stream. Every id the server mints is printable
# ASCII without a blank, so a part that is anything else is not one of ours.
_CURSOR_PART_RE = re.compile(r"[\x21-\x7e]*")
# What a daemon position that is not safe to echo becomes: no position parses
# from it, so the store answers it with a ``cursor_invalid`` reset.
_UNSAFE_DAEMON_PART = "!"


def _safe_part(part: str, replacement: str = "") -> str:
    return part if _CURSOR_PART_RE.fullmatch(part) else replacement

_TERMINAL_STATUSES = frozenset(
    {"completed", "cancelled", "failed", "blocked", "waiting", "archived"}
)
_FORWARDED_TEXT_EVENTS = frozenset({"message_chunk", "compaction_chunk"})


def ticket_activity_cursor(
    activity_event_id: str = "",
    run_id: str = "",
    run_seq: int = 0,
    daemon_cursor: str = "",
    source: str = "",
) -> str:
    """Encode the composite resume cursor.  Opaque to clients.

    ``source`` names the live-output source the client last received (see
    :class:`OutputSourceFence`) and rides every frame's cursor, status frames
    included, so a reconnect knows what the watcher still holds.
    """
    activity = _safe_part(activity_event_id.replace(CURSOR_SEP, ""))
    run = _safe_part(run_id.replace(CURSOR_SEP, ""))
    daemon = _safe_part(daemon_cursor.replace(CURSOR_SEP, ""), _UNSAFE_DAEMON_PART)
    source = _safe_part(source.replace(CURSOR_SEP, ""))
    base = (
        f"{activity}{CURSOR_SEP}{run}{CURSOR_SEP}"
        f"{max(0, int(run_seq or 0))}{CURSOR_SEP}{daemon}"
    )
    return f"{base}{CURSOR_SEP}{source}" if source else base


def parse_ticket_activity_cursor(
    raw: str | None,
) -> tuple[str, str, int, str]:
    """Split a cursor from ``Last-Event-ID``/``last_event_id`` into its parts.

    The daemon-output part is ``<epoch>.<seq>`` (see ``daemon_output``), added
    after the run cursor; a three-part cursor from a pre-daemon-stream client
    parses with an empty daemon position. A part that no longer fits the stream
    makes the next daemon read start with an ``output_reset``.
    """
    if not raw:
        return "", "", 0, ""
    activity, _, rest = str(raw).partition(CURSOR_SEP)
    run_id, _, rest = rest.partition(CURSOR_SEP)
    seq_text, _, rest = rest.partition(CURSOR_SEP)
    daemon_cursor, _, _source = rest.partition(CURSOR_SEP)
    try:
        seq = max(0, int(seq_text))
    except (TypeError, ValueError):
        seq = 0
    return (
        _safe_part(activity),
        _safe_part(run_id),
        seq,
        _safe_part(daemon_cursor, _UNSAFE_DAEMON_PART),
    )


def cursor_output_source(raw: str | None) -> str:
    """The live-output source recorded in a cursor ('' for older cursors)."""
    if not raw:
        return ""
    parts = str(raw).split(CURSOR_SEP)
    return _safe_part(parts[4]) if len(parts) > 4 else ""


_RESET_REASONS = frozenset(
    {
        daemon_output_store.RESET_NEW_EPOCH,
        daemon_output_store.RESET_CONTINUITY,
        daemon_output_store.RESET_GAP,
        daemon_output_store.RESET_CURSOR,
    }
)


def daemon_poll_delay(idle_s: float) -> float:
    """Seconds to the next daemon-output read, given how long it found nothing."""
    if idle_s < ACTIVITY_STREAM_DAEMON_FAST_HOLD_S:
        return ACTIVITY_STREAM_DAEMON_POLL_S
    if idle_s < ACTIVITY_STREAM_DAEMON_MID_HOLD_S:
        return ACTIVITY_STREAM_DAEMON_MID_POLL_S
    return ACTIVITY_STREAM_DAEMON_IDLE_POLL_S


def _reset_reason(reason: str) -> str:
    """A reset reason is one of a fixed set; anything else is generic."""
    return reason if reason in _RESET_REASONS else daemon_output_store.RESET_CONTINUITY


def _sse_frame(event: str, payload: Mapping[str, Any], cursor: str | None) -> str:
    """One SSE frame. A ``None`` cursor omits the ``id:`` line, which leaves the
    client's last event id where it was (the SSE rule for a frame with no id)."""
    data = json.dumps(payload, ensure_ascii=False, default=str)
    head = f"id: {cursor}\n" if cursor is not None else ""
    return f"{head}event: {event}\ndata: {data}\n\n"


def _parse_run_frame(frame: str) -> tuple[int | None, str, dict[str, Any]]:
    """Read one stored run frame back into (event seq, event type, payload)."""
    seq: int | None = None
    event = ""
    data_lines: list[str] = []
    # Split on LF only: ``str.splitlines`` also splits on U+2028/U+2029/NEL,
    # which JSON string content may legitimately contain, corrupting the frame.
    for raw_line in (frame or "").split("\n"):
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        if not line or line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if name == "id":
            try:
                seq = int(value.strip())
            except ValueError:
                seq = None
        elif name == "event":
            event = value.strip()
        elif name == "data":
            data_lines.append(value[1:] if value.startswith(" ") else value)
    text = "\n".join(data_lines)
    data: Any = {}
    if text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {"content": text}
    if not isinstance(data, dict):
        data = {"content": data}
    return seq, event, data


def _content_text(content: Any) -> str:
    """Flatten a streamed model content value into its visible text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _worker_output_payload(
    event_type: str, data: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The client-visible shape of one stored run frame, or None to drop it.

    Only visible model text and tool names cross this boundary: tool arguments
    and results can carry file contents or credentials, and the CLI has no
    reason to print them for an activity watch.
    """
    if event_type in _FORWARDED_TEXT_EVENTS:
        if str(data.get("content_type") or "") == "reasoning_signal":
            return None
        text = _content_text(data.get("content"))
        if not text:
            return None
        payload: dict[str, Any] = {
            "kind": "reasoning" if data.get("content_type") == "reasoning" else "text",
            "text": text,
        }
        agent = str(data.get("agent") or "")
        if agent:
            payload["agent"] = agent
        return payload
    if event_type == "tool_calls":
        names = [
            str(call.get("name"))
            for call in data.get("tool_calls") or []
            if isinstance(call, dict) and call.get("name")
        ]
        if not names:
            return None
        return {"kind": "tool", "tools": names}
    if event_type == "error":
        text = str(
            data.get("message") or data.get("content") or data.get("error") or ""
        ).strip()
        if not text:
            return None
        return {"kind": "error", "text": text}
    if event_type == "stream_gap":
        return {
            "kind": "gap",
            "text": "Some Worker output was dropped before this reconnect.",
        }
    return None


async def _worker_run_frames(
    thread_id: str, run_id: str, last_event_id: int
) -> AsyncIterator[str]:
    """Stored Worker run frames, resumable by run event seq.

    Imported lazily: the run reader pulls in the run executor, and this module
    is imported by the autonomy router at application import time.
    """
    from src.server.services.runs.stream_reader import stream_from_log

    async for frame in stream_from_log(
        thread_id, run_id, last_event_id=last_event_id or None
    ):
        yield frame


def _status_payload(ticket: Mapping[str, Any]) -> dict[str, Any]:
    attention = ticket.get("attention")
    question = attention.get("question") if isinstance(attention, dict) else ""
    return {
        "status": str(ticket.get("status") or ""),
        "summary": str(ticket.get("summary") or ""),
        "question": str(question or ""),
    }


class _RunPump:
    """Tail one Worker run generation into its own queue.

    Each run gets a fresh queue and every item is tagged with its run id.  A
    retry or a line switch replaces the generation wholesale, so frames the
    old run already queued can never be read under the new run's sequence
    cursor (which would make a reconnect skip the new run's output).

    The queue ends with a ``None`` sentinel when the generation's tail stops,
    and ``run_done`` reports that the closing ``run_end`` frame was seen.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=ACTIVITY_STREAM_OUTPUT_QUEUE_MAX
        )
        self._task: asyncio.Task[None] | None = None
        self._pair: tuple[str, str] | None = None
        self._run_done = False
        self._last_seq = 0
        self._failed = False
        self._ended_clean = False
        self._restarts = 0
        self._retry_at = 0.0
        self._exhausted = False

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def run_done(self) -> bool:
        """True once the current generation's ``run_end`` frame was read."""
        return self._run_done

    @property
    def failed(self) -> bool:
        """True when the current generation's reader failed (may still retry)."""
        return self._failed

    @property
    def exhausted(self) -> bool:
        """True when the tail failed and its bounded retry budget is spent."""
        return self._exhausted

    @property
    def last_seq(self) -> int:
        """Highest run sequence queued so far (resume point for a retry)."""
        return self._last_seq

    def retire(self) -> None:
        """Stop tailing: the Ticket no longer names a live Worker run."""
        task, self._task = self._task, None
        self._pair = None
        self._run_done = False
        self._last_seq = 0
        self._failed = False
        self._ended_clean = False
        self._restarts = 0
        self._retry_at = 0.0
        self._exhausted = False
        # Replace the queue so frames the cancelled tail already buffered are
        # unreachable under the next generation (or under no run at all).
        self._queue = asyncio.Queue(maxsize=ACTIVITY_STREAM_OUTPUT_QUEUE_MAX)
        if task is not None and not task.done():
            task.cancel()

    def _start(self, thread_id: str, run_id: str, seq: int) -> None:
        self._attempt_started = time.monotonic()
        self._task = asyncio.create_task(
            self._pump(thread_id, run_id, max(0, int(seq or 0)), self._queue),
            name=f"ticket-worker-stream:{run_id}",
        )

    def _retry(self, thread_id: str, run_id: str, seq: int) -> None:
        """Restart a failed tail from the queued sequence, or exhaust."""
        if self._exhausted:
            return
        now = time.monotonic()
        if now < self._retry_at:
            return
        if self._restarts >= ACTIVITY_STREAM_TAIL_RETRIES:
            self._exhausted = True
            logger.warning(
                "[autonomy] worker run tail exhausted retries thread=%s run=%s",
                thread_id,
                run_id,
            )
            return
        self._restarts += 1
        self._retry_at = now + min(
            ACTIVITY_STREAM_TAIL_BACKOFF_S * (2 ** (self._restarts - 1)),
            ACTIVITY_STREAM_TAIL_BACKOFF_MAX_S,
        )
        self._failed = False
        self._ended_clean = False
        # Resume after everything already queued: those frames are still in
        # front of the retried tail's first frame for the consumer.
        self._start(thread_id, run_id, max(self._last_seq, int(seq or 0)))

    def ensure(
        self,
        thread_id: str,
        run_id: str,
        *,
        seq: int,
        ticket_terminal: bool,
    ) -> None:
        """Start, restart, or retire the pump for a Ticket's Worker run."""
        if not thread_id or not run_id:
            self.retire()
            return
        pair = (thread_id, run_id)
        if pair == self._pair:
            if self._task is not None and not self._task.done():
                return
            if self._run_done:
                # The closing run_end frame was read: nothing to resume.
                return
            if not self._failed:
                if not self._ended_clean:
                    return
                # The reader returned without a run_end (an unavailable pool,
                # a trimmed stream, a head not written yet). A terminal
                # Ticket's stream is allowed to be closed already; a live one
                # is not, and its token delivery must not silently stop.
                if ticket_terminal:
                    return
                self._failed = True
            self._retry(thread_id, run_id, seq)
            return
        previous, self._task = self._task, None
        if previous is not None and not previous.done():
            previous.cancel()
        # The generation owns its queue: the cancelled tail keeps writing to
        # the old one, so nothing it already buffered leaks into the new run.
        self._queue = asyncio.Queue(maxsize=ACTIVITY_STREAM_OUTPUT_QUEUE_MAX)
        self._pair = pair
        self._run_done = False
        self._last_seq = 0
        self._failed = False
        self._ended_clean = False
        self._restarts = 0
        self._retry_at = 0.0
        self._exhausted = False
        self._start(thread_id, run_id, int(seq or 0))

    async def _pump(
        self,
        thread_id: str,
        run_id: str,
        seq: int,
        queue: asyncio.Queue[dict[str, Any] | None],
    ) -> None:
        try:
            async for frame in _worker_run_frames(thread_id, run_id, seq):
                event_seq, event_type, data = _parse_run_frame(frame)
                if isinstance(event_seq, int) and event_seq > self._last_seq:
                    self._last_seq = event_seq
                # A tail that keeps streaming for long enough earns a fresh
                # retry budget; a quick frame does not (a flapping tail must
                # stay bounded).
                if (
                    self._restarts
                    and time.monotonic() - self._attempt_started
                    >= ACTIVITY_STREAM_TAIL_HEALTHY_S
                ):
                    self._restarts = 0
                if event_type == "run_end":
                    self._run_done = True
                # Backpressure: a paused consumer blocks this reader rather
                # than letting the queue grow without bound. The run stream
                # keeps the backlog; we resume from its cursor.
                await queue.put(
                    {
                        "run_id": run_id,
                        "seq": event_seq,
                        "payload": _worker_output_payload(event_type, data),
                    }
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the activity half must survive this
            self._failed = True
            self._ended_clean = False
            logger.warning(
                "[autonomy] worker run tail failed thread=%s run=%s",
                thread_id,
                run_id,
                exc_info=True,
            )
        else:
            # The reader returned. Without a run_end this may be a premature
            # EOF (unavailable pool, trimmed head) that a retry can recover.
            self._ended_clean = not self._run_done
        finally:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                # The consumer went away with a full buffer; nobody is left to
                # observe the sentinel.
                pass

    def get_nowait(self) -> tuple[bool, dict[str, Any] | None]:
        """(present, item): present is False when nothing is queued."""
        try:
            return True, self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return False, None

    async def wait(self, timeout: float) -> tuple[bool, dict[str, Any] | None]:
        try:
            return True, await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return False, None

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - teardown must not mask the close
            logger.debug("[autonomy] worker run tail shutdown failed", exc_info=True)


def _consume_worker_item(
    item: dict[str, Any] | None, state: dict[str, Any]
) -> dict[str, Any] | None:
    """Advance the run cursor for one item; return a payload to emit.

    A frame from another run generation is discarded rather than allowed to
    move the current run's sequence cursor.
    """
    if item is None:
        return None
    frame_run = str(item.get("run_id") or "")
    current_run = str(state.get("run_id") or "")
    if frame_run != current_run:
        # Includes an empty current run (the Ticket cleared its Worker ids):
        # a retired generation's frames must never be emitted or move a cursor.
        return None
    seq = item.get("seq")
    if isinstance(seq, int) and seq > int(state["run_seq"] or 0):
        state["run_seq"] = seq
    payload = item.get("payload")
    return payload if isinstance(payload, dict) and payload else None


async def _poll_activity(
    ticket_id: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """One durable-state read: the ticket row and its projected activity."""
    ticket = await tickets_db.get_ticket(ticket_id)
    if ticket is None:
        return None, []
    inputs = await tickets_db.list_inputs(ticket_id)
    reports = await tickets_db.list_reports(ticket_id)
    return dict(ticket), project_task_activity(ticket, inputs, reports)


class OutputSourceFence:
    """Resets a watcher whenever its live output changes source or generation.

    A Ticket's live output can come from a cloud Worker run (identified by its
    run id) or from a local daemon (identified by its stream epoch), and a
    reassignment or retry moves it from one to another. A watcher that already
    holds a fragment from the old source must never be handed text from the new
    one as if it continued, whichever way the Ticket moved and whatever the
    client remembers, so the server says so: an ``output_reset`` goes out ahead
    of the first payload of any source that differs from the last one shown.

    A fresh watcher (no resume cursor) has seen nothing and needs none. A
    resumed one starts from the source its cursor names; a cursor naming both a
    run and a daemon position is ambiguous and so counts as a different source.
    """

    UNKNOWN: tuple = ("?",)

    def __init__(
        self, resume_run_id: str, resume_daemon: str, resume_source: str = ""
    ) -> None:
        parsed = daemon_output_store.parse_cursor(resume_daemon) if resume_daemon else None
        recorded = self.decode(resume_source)
        if recorded is not None:
            # The cursor says exactly what the watcher last received.
            self._source: tuple | None = recorded
        elif resume_run_id and (resume_daemon or parsed):
            self._source = self.UNKNOWN
        elif resume_daemon:
            self._source = ("daemon", parsed[0]) if parsed else self.UNKNOWN
        elif resume_run_id:
            self._source = ("run", resume_run_id)
        else:
            self._source = None

    @staticmethod
    def decode(raw: str) -> tuple | None:
        if (
            raw.startswith("d")
            and raw[1:].isascii()
            and raw[1:].isdigit()
            and len(raw) <= 19  # bound the digits before int() sees them
        ):
            return ("daemon", int(raw[1:]))
        if raw.startswith("r"):
            return ("run", raw[1:])
        return None

    def encoded(self, current_run_id: str = "", daemon_cursor: str = "") -> str:
        """The current source as the cursor carries it ('' before any output).

        A run source that is the Ticket's own current run is already named by
        the cursor's run part and is not repeated, unless the cursor also holds
        a daemon position: a run part beside a daemon part and no source is the
        shape ``__init__`` reads as unknown, so a watcher that already holds
        this run's text would be reset for nothing on every reconnect.
        """
        source = self._source
        if source is None or source == self.UNKNOWN:
            return ""
        if source[0] == "daemon":
            return f"d{source[1]}"
        if source[1] == current_run_id and not daemon_cursor:
            return ""
        return f"r{source[1]}"

    @staticmethod
    def source_of(payload: Mapping[str, Any]) -> tuple:
        run = str(payload.get("run") or "")
        if run.startswith("daemon"):
            epoch = payload.get("epoch")
            return ("daemon", epoch if isinstance(epoch, int) else 0)
        return ("run", run)

    def check(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        """The reset to send before ``payload``, if its source changed."""
        kind = str(payload.get("kind") or "")
        source = self.source_of(payload)
        if kind == daemon_output_store.RESET_KIND:
            # An explicit reset is itself the boundary and names the new source.
            self._source = source
            return None
        if source == ("run", ""):
            return None  # a payload that names no run says nothing about sources
        previous, self._source = self._source, source
        if previous is None or previous == source:
            return None
        return {
            "kind": daemon_output_store.RESET_KIND,
            "epoch": source[1] if source[0] == "daemon" else 0,
            "reason": daemon_output_store.RESET_NEW_EPOCH,
            "run": str(payload.get("run") or ""),
        }


async def ticket_activity_events(
    ticket_id: str,
    *,
    initial_ticket: Mapping[str, Any],
    resume: str | None = None,
) -> AsyncIterator[str]:
    """The Ticket activity SSE body.

    Emits ``activity`` rows, ``output`` Worker frames when the Ticket has a
    Worker run, ``status`` on a ticket status change, keepalive comments, and a
    closing ``end`` (or ``timeout``/``error``) frame.  A resumed counter
    (``Last-Event-ID``) resumes the Worker run sequence; the activity record
    replays in full on every attach and the client deduplicates by
    ``event_id`` (see the module docstring for why).
    """
    (
        resume_activity,
        resume_run_id,
        resume_seq,
        resume_daemon,
    ) = parse_ticket_activity_cursor(resume)
    ticket = dict(initial_ticket)
    status = str(ticket.get("status") or "")
    run_id = str(ticket.get("worker_run_id") or "")
    thread_id = str(ticket.get("worker_thread_id") or "")
    restorable_run = bool(thread_id and run_id) and resume_run_id == run_id
    daemon_owned = bool(ticket.get("worker_daemon_id"))
    state: dict[str, Any] = {
        "activity": resume_activity,
        "run_id": run_id if restorable_run else "",
        "run_seq": resume_seq if restorable_run else 0,
        # The daemon cursor is kept whoever owns the Ticket now: it names an
        # epoch, so when the log it points into has been replaced the next read
        # resets this watcher instead of silently starting a new stream.
        "daemon": resume_daemon,
    }
    # A daemon stream is only read for Tickets a daemon currently owns; a PTC
    # Ticket must not pay a database round trip on every loop.
    daemon_enabled = daemon_owned
    daemon_at = 0.0
    # Activity is an idempotent snapshot: every attach replays the projection
    # and the client deduplicates by event_id.  Only the run sequences resume.
    seen: set[str] = set()
    started = time.monotonic()
    last_emit = started
    # When a daemon read last moved the cursor (an attach counts): the pace backs
    # off from here, and the next movement returns it to the fast cadence.
    daemon_active = started
    last_poll = 0.0
    pump = _RunPump()

    fence = OutputSourceFence(resume_run_id, resume_daemon, cursor_output_source(resume))

    def cursor() -> str:
        return ticket_activity_cursor(
            str(state["activity"] or ""),
            str(state["run_id"] or ""),
            int(state["run_seq"] or 0),
            str(state["daemon"] or ""),
            fence.encoded(str(state["run_id"] or ""), str(state["daemon"] or "")),
        )

    def output_frame(payload: dict[str, Any]) -> str:
        # The run id rides the payload too: a watcher uses a change of run id
        # to drop any half-parsed terminal sequence that belonged to the old
        # Worker generation. Daemon payloads already name their generation.
        frame = dict(payload)
        if "run" not in frame:
            frame["run"] = str(state["run_id"] or "")
        reset = fence.check(frame)
        # The synthetic reset carries no id: if the connection drops after it
        # but before the payload it announces, the client's last id is still the
        # one from before the payload, so the reconnect re-reads the payload and
        # is reset again instead of skipping it.
        head = _sse_frame("output", reset, None) if reset else ""
        return head + _sse_frame("output", frame, cursor())

    def emit_activity(events: list[dict[str, Any]]) -> list[str]:
        nonlocal last_emit
        frames: list[str] = []
        for event in events:
            event_id = str(event.get("event_id") or "")
            if not event_id or event_id in seen:
                continue
            seen.add(event_id)
            state["activity"] = event_id
            frames.append(_sse_frame("activity", event, cursor()))
        if frames:
            last_emit = time.monotonic()
        return frames

    async def drain(
        deadline: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield one bounded batch of queued Worker output; never waits.

        ``deadline`` stops the batch at the stream ceiling so a slow consumer
        cannot sit through a full batch past it.
        """
        for _ in range(ACTIVITY_STREAM_OUTPUT_BATCH):
            if deadline is not None and time.monotonic() >= deadline:
                return
            present, item = pump.get_nowait()
            if not present or item is None:
                return
            payload = _consume_worker_item(item, state)
            if payload:
                yield payload

    async def drain_daemon() -> AsyncIterator[dict[str, Any]]:
        """Yield the next bounded batch of local-daemon output, if any.

        The store is best effort: an unavailable database returns an empty page
        and this yields nothing, leaving the activity/status stream untouched.
        A page whose cursor no longer fits the stream (another epoch, a trimmed
        gap) leads with an ``output_reset`` payload so the watcher drops what it
        holds before it receives anything new.
        """
        if not daemon_enabled:
            return
        page = await daemon_output_store.read_output(
            ticket_id,
            str(state["daemon"] or "") or None,
            count=ACTIVITY_STREAM_DAEMON_BATCH,
        )
        if page.reset:
            # Stand just before the first retained event: a watcher that drops
            # the connection right after the reset must not skip events, and
            # must not be told to reset again for the same gap.
            state["daemon"] = page.start
            yield {
                "kind": daemon_output_store.RESET_KIND,
                "epoch": page.epoch,
                "reason": _reset_reason(page.reset),
                "run": "daemon",
            }
        if page.cursor and not page.events:
            state["daemon"] = page.cursor
        for entry in page.events:
            state["daemon"] = str(entry["cursor"])
            payload = entry["data"]
            kind = str(payload.get("kind") or "text")
            run = str(payload.get("run_id") or "")
            run_label = f"daemon:{run}" if run else "daemon"
            if kind == daemon_output_store.RESET_KIND:
                yield {
                    "kind": kind,
                    "epoch": page.epoch,
                    "reason": _reset_reason(str(payload.get("reason") or "")),
                    "run": run_label,
                }
                continue
            if kind == "tool":
                tools = [str(name) for name in payload.get("tools") or () if name]
                if tools:
                    yield {
                        "kind": "tool",
                        "tools": tools[:8],
                        "run": run_label,
                        "epoch": page.epoch,
                    }
                continue
            if kind not in {"text", "reasoning"}:
                continue
            text = str(payload.get("text") or "")
            if text:
                yield {
                    "kind": kind,
                    "text": text,
                    "run": run_label,
                    "epoch": page.epoch,
                }

    async def drain_daemon_all(
        *, deadline: float | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Drain every retained daemon event, in bounded batches.

        A deadline (the stream's own timeout) bounds the drain: a live store
        that keeps answering must not postpone ``timeout`` indefinitely. A pass
        that leaves the cursor where it found it made no progress (a store that
        keeps answering the same page), so it ends the drain as well.
        """
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                return
            before = str(state["daemon"] or "")
            got = False
            async for payload in drain_daemon():
                got = True
                yield payload
            if not got or str(state["daemon"] or "") == before:
                return

    try:
        # First batch comes from the row the route already authenticated.
        frames = emit_activity(
            project_task_activity(
                ticket,
                await tickets_db.list_inputs(ticket_id),
                await tickets_db.list_reports(ticket_id),
            )
        )
        for frame in frames:
            yield frame
        pump.ensure(
            thread_id,
            run_id,
            seq=int(state["run_seq"] or 0),
            ticket_terminal=status in _TERMINAL_STATUSES,
        )
        state["run_id"] = run_id
        state["status"] = status
        yield _sse_frame("status", _status_payload(ticket), cursor())
        last_emit = time.monotonic()

        stream_deadline = started + ACTIVITY_STREAM_MAX_S
        # Set while the last durable poll saw a terminal status. A retry can
        # revive the Ticket mid-stream, so a non-terminal poll clears it.
        terminal_since: float | None = (
            last_emit if status in _TERMINAL_STATUSES else None
        )

        while True:
            now = time.monotonic()
            if now >= stream_deadline:
                async for payload in drain_daemon_all(deadline=stream_deadline):
                    yield output_frame(payload)
                    last_emit = time.monotonic()
                yield _sse_frame("timeout", {}, cursor())
                return

            if daemon_enabled and now >= daemon_at:
                emitted = 0
                daemon_before = str(state["daemon"] or "")
                async for payload in drain_daemon():
                    yield output_frame(payload)
                    last_emit = time.monotonic()
                    emitted += 1
                if str(state["daemon"] or "") != daemon_before:
                    daemon_active = now
                daemon_at = (
                    now
                    if emitted >= ACTIVITY_STREAM_DAEMON_BATCH
                    else now + daemon_poll_delay(now - daemon_active)
                )

            poll_at = last_poll + ACTIVITY_STREAM_POLL_S
            if now >= poll_at:
                # Poll the durable record first: a Worker tail that never
                # pauses must not delay a queued/cleared/terminal Ticket. The
                # terminal drain is re-checked here too, so a cancelled ->
                # queued retry ends the old terminal state instead of emitting
                # an obsolete ``end``.
                last_poll = now
                try:
                    fresh, events = await _poll_activity(ticket_id)
                except Exception:  # noqa: BLE001 - surface as a fallback signal
                    logger.exception(
                        "[autonomy] ticket activity poll failed ticket=%s", ticket_id
                    )
                    yield _sse_frame(
                        "error",
                        {"message": "Ticket activity is unavailable."},
                        cursor(),
                    )
                    return
                if fresh is None:
                    yield _sse_frame(
                        "end",
                        {"status": "missing", "summary": "", "question": ""},
                        cursor(),
                    )
                    return
                ticket = fresh
                status = str(ticket.get("status") or "")
                # Reads pause while no daemon owns the Ticket, but the cursor is
                # kept: if a daemon later starts a new epoch, the next read
                # resets this watcher before it receives anything.
                daemon_enabled = bool(ticket.get("worker_daemon_id"))
                thread_id = str(ticket.get("worker_thread_id") or "")
                new_run_id = str(ticket.get("worker_run_id") or "")
                if not thread_id or not new_run_id:
                    # A retry/switch can clear the Worker ids while an old tail
                    # is still attached; retire it and reset the resume cursor.
                    state["run_id"] = ""
                    state["run_seq"] = 0
                elif new_run_id != state["run_id"]:
                    state["run_id"] = new_run_id
                    state["run_seq"] = 0
                pump.ensure(
                    thread_id,
                    new_run_id,
                    seq=int(state["run_seq"] or 0),
                    ticket_terminal=status in _TERMINAL_STATUSES,
                )
                if pump.exhausted:
                    # Activity polling continues fine, but token delivery has
                    # stopped for good on this connection: tell the client so it
                    # reattaches (a fresh connection gets a fresh retry budget).
                    yield _sse_frame(
                        "error",
                        {"message": "Worker output stream is unavailable."},
                        cursor(),
                    )
                    return
                for frame in emit_activity(events):
                    yield frame
                if status != str(state.get("status") or ""):
                    state["status"] = status
                    yield _sse_frame("status", _status_payload(ticket), cursor())
                    last_emit = time.monotonic()
                if status in _TERMINAL_STATUSES:
                    if terminal_since is None:
                        terminal_since = now
                else:
                    terminal_since = None
                if time.monotonic() - last_emit >= ACTIVITY_STREAM_PING_S:
                    yield ": ping\n\n"
                    last_emit = time.monotonic()
                continue

            if terminal_since is not None:
                grace_deadline = terminal_since + ACTIVITY_STREAM_TERMINAL_RUN_GRACE_S
                if now >= grace_deadline:
                    logger.debug(
                        "[autonomy] worker run still open past terminal grace ticket=%s",
                        ticket_id,
                    )
                    async for payload in drain_daemon_all(deadline=stream_deadline):
                        yield output_frame(payload)
                        last_emit = time.monotonic()
                    yield _sse_frame("end", _status_payload(ticket), cursor())
                    return
                drain_deadline = min(poll_at, grace_deadline, stream_deadline)
            else:
                drain_deadline = min(poll_at, stream_deadline)

            # Worker output is the low-latency path. The batch deadline is the
            # next required poll, so a busy tail can never postpone state.
            async for payload in drain(deadline=drain_deadline):
                yield output_frame(payload)
                last_emit = time.monotonic()

            if terminal_since is not None and (
                pump.run_done
                or ((pump.task is None or pump.task.done()) and not pump.failed)
            ):
                if time.monotonic() >= poll_at:
                    # Do not close on a snapshot a due poll may invalidate (a
                    # retried Ticket can clear its terminal status and run).
                    continue
                # The run closed; flush the remaining daemon/run tail, then end.
                got_daemon = False
                daemon_before = str(state["daemon"] or "")
                async for payload in drain_daemon_all(deadline=stream_deadline):
                    yield output_frame(payload)
                    last_emit = time.monotonic()
                    got_daemon = True
                # Only a drain that moved the cursor earns another look at the
                # closing conditions: a store answering the same page forever
                # must not hold a finished Ticket's stream open.
                if got_daemon and str(state["daemon"] or "") != daemon_before:
                    continue
                present, item = pump.get_nowait()
                if not present or item is None:
                    yield _sse_frame("end", _status_payload(ticket), cursor())
                    return
                payload = _consume_worker_item(item, state)
                if payload:
                    yield output_frame(payload)
                    last_emit = time.monotonic()
                continue

            next_wake = poll_at
            if daemon_enabled:
                next_wake = min(next_wake, daemon_at)
            wait_for = max(0.0, next_wake - time.monotonic())
            present, item = await pump.wait(wait_for)
            if present:
                payload = _consume_worker_item(item, state)
                if payload:
                    yield output_frame(payload)
                    last_emit = time.monotonic()
                continue
            # Timed out waiting for output: the next round polls the record.

    finally:
        await pump.close()


__all__ = [
    "ACTIVITY_STREAM_DAEMON_BATCH",
    "ACTIVITY_STREAM_DAEMON_FAST_HOLD_S",
    "ACTIVITY_STREAM_DAEMON_IDLE_POLL_S",
    "ACTIVITY_STREAM_DAEMON_MID_HOLD_S",
    "ACTIVITY_STREAM_DAEMON_MID_POLL_S",
    "ACTIVITY_STREAM_DAEMON_POLL_S",
    "ACTIVITY_STREAM_MAX_S",
    "ACTIVITY_STREAM_PING_S",
    "ACTIVITY_STREAM_POLL_S",
    "ACTIVITY_STREAM_TERMINAL_RUN_GRACE_S",
    "CURSOR_SEP",
    "cursor_output_source",
    "parse_ticket_activity_cursor",
    "ticket_activity_cursor",
    "ticket_activity_events",
]
