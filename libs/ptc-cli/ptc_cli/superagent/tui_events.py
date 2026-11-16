"""Live Ticket events for the Super-agent TUI.

The CLI watches one Ticket at a time. This module turns the server's
Server-Sent Event stream into a small, testable async event sequence and keeps
the two behaviours the TUI depends on:

* **Reconnect** — a mid-stream transport failure reattaches with the last
  event id, bounded by exponential backoff, and only while frames keep
  arriving (a clean-but-empty close means the endpoint has no live path).
* **Polling fallback** — an older deployment without the stream (404/405/501,
  or a non-SSE body) yields the durable activity rows instead, deduplicated by
  event id, until the Ticket is terminal.

Everything here is backend-facing and free of UI code; ``tui_blocks`` maps
these events onto conversation blocks.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .client import (
    ActivityStreamEvent,
    ActivityStreamUnavailableError,
    SuperAgentError,
)

# How long one watch may last before it reports a timeout frame.
WATCH_TIMEOUT_S = 900.0
# Reconnect budget and backoff for a stream that keeps failing after frames.
SSE_RECONNECT_ATTEMPTS = 5
SSE_BACKOFF_BASE_S = 0.5
SSE_BACKOFF_MAX_S = 8.0
# The polling fallback's pace.
POLL_INTERVAL_S = 2.0

_TERMINAL_STATUSES = frozenset(
    {"completed", "cancelled", "failed", "blocked", "waiting", "archived"}
)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Activity:
    """One durable row from the Ticket's event record."""

    at: str
    kind: str
    summary: str
    detail: str = ""
    event_id: str = ""


@dataclass(frozen=True, slots=True)
class OutputChunk:
    """Visible Worker model output; ``kind`` is ``text`` or ``reasoning``.

    ``agent`` names the sub-agent that produced the text when the Worker
    streamed it under one; the TUI groups those lines into their own block.
    """

    kind: str
    text: str
    run_id: str = ""
    agent: str = ""
    # The server's stream epoch (0 = the payload carried none, e.g. an older
    # cloud run). Only used to fence fragments; never shown to the user.
    epoch: int = 0


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Tool names the Worker announced, and nothing else.

    Arguments and diffs are deliberately not part of the event: free-text
    arguments and file bodies cannot be made credential-safe by a bounded
    scrubber, so the server never projects them and the client ignores them if a
    payload carries them anyway (a proxy, an older or a modified deployment).
    """

    names: tuple[str, ...]
    # Where the live output came from (empty / 0: the payload named no source).
    run_id: str = ""
    epoch: int = 0


@dataclass(frozen=True, slots=True)
class OutputReset:
    """The live output stream restarted: what came before is a closed fragment.

    Carries no text. Everything received so far for the Ticket's live output is
    an unfinished fragment that must never be joined with text arriving after
    this event (a credential split across a retry would otherwise be stitched
    back together). ``reason`` is server text and is sanitized at render time.
    """

    reason: str
    epoch: int = 0


@dataclass(frozen=True, slots=True)
class OutputError:
    text: str


@dataclass(frozen=True, slots=True)
class Gap:
    text: str


@dataclass(frozen=True, slots=True)
class Status:
    status: str
    summary: str = ""
    question: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class End:
    status: str
    summary: str = ""
    question: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class TimedOut:
    message: str


@dataclass(frozen=True, slots=True)
class StreamNotice:
    """A UI-visible notice about the transport (reconnect, polling fallback)."""

    text: str


TicketEvent = (
    Activity
    | OutputChunk
    | OutputReset
    | ToolCall
    | OutputError
    | Gap
    | Status
    | End
    | TimedOut
    | StreamNotice
)


class WatchClient(Protocol):
    """The slice of :class:`SuperAgentClient` a watch needs."""

    def stream_activity(
        self, ticket_id: str, *, last_event_id: str | None = None
    ) -> AsyncIterator[ActivityStreamEvent]: ...

    async def activity(self, ticket_id: str) -> dict[str, Any]: ...

    async def ticket(self, ticket_id: str) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Frame mapping
# ---------------------------------------------------------------------------


def _activity_from_row(row: dict[str, Any]) -> Activity:
    return Activity(
        at=str(row.get("at") or ""),
        kind=str(row.get("kind") or ""),
        summary=str(row.get("summary") or ""),
        detail=str(row.get("detail") or ""),
        event_id=str(row.get("event_id") or ""),
    )


def _epoch_of(data: dict[str, Any]) -> int:
    """The payload's stream epoch; 0 when absent or not a plain integer."""
    value = data.get("epoch")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def output_reset_notice(reason: str) -> str:
    """Notice text for an ``OutputReset`` (``reason`` is sanitized when drawn)."""
    label = str(reason or "").replace("_", " ").strip()
    return f"live output restarted ({label})" if label else "live output restarted"


def _map_stream_event(event: ActivityStreamEvent) -> TicketEvent | None:
    """Map one SSE frame; ``None`` means the frame carries no UI change."""
    name = event.event.strip()
    data = event.data or {}
    if name == "activity":
        return _activity_from_row(data)
    if name == "output":
        kind = str(data.get("kind") or "")
        if kind in {"text", "reasoning"}:
            text = str(data.get("text") or "")
            return (
                OutputChunk(
                    kind=kind,
                    text=text,
                    run_id=str(data.get("run") or ""),
                    agent=str(data.get("agent") or ""),
                    epoch=_epoch_of(data),
                )
                if text
                else None
            )
        if kind == "output_reset":
            return OutputReset(
                reason=str(data.get("reason") or "")[:80],
                epoch=_epoch_of(data),
            )
        if kind == "tool":
            names = tuple(
                str(value) for value in data.get("tools") or [] if str(value)
            )
            epoch = data.get("epoch")
            return ToolCall(
                names=names,
                run_id=str(data.get("run") or ""),
                epoch=epoch if isinstance(epoch, int) and not isinstance(epoch, bool) else 0,
            )
        if kind == "error":
            return OutputError(text=str(data.get("text") or ""))
        if kind == "gap":
            return Gap(text=str(data.get("text") or ""))
        return None
    if name == "status":
        return Status(
            status=str(data.get("status") or ""),
            summary=str(data.get("summary") or ""),
            question=str(data.get("question") or ""),
            error=str(data.get("error") or ""),
        )
    if name == "end":
        return End(
            status=str(data.get("status") or ""),
            summary=str(data.get("summary") or ""),
            question=str(data.get("question") or ""),
            error=str(data.get("error") or ""),
        )
    if name == "timeout":
        return TimedOut(
            message=str(data.get("message") or "the watch timed out")
        )
    if name == "error":
        # The stream itself failed as a frame: reconnect while frames were
        # flowing, otherwise fall back to polling.
        return Status(status="", summary=str(data.get("message") or "stream error"))
    return None


# ---------------------------------------------------------------------------
# The watch
# ---------------------------------------------------------------------------


async def watch_ticket(
    client: WatchClient,
    ticket_id: str,
    *,
    timeout: float = WATCH_TIMEOUT_S,
    sleep: Callable[[float], Any] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    should_stop: Callable[[], bool] | None = None,
) -> AsyncIterator[TicketEvent]:
    """Follow one Ticket until it ends, the watch times out, or it is stopped.

    The stream is preferred; a deployment without it is polled. The async
    generator always yields exactly one ``End`` or ``TimedOut`` before
    returning, so the caller can settle the conversation and status bar.
    """
    deadline = clock() + max(0.0, timeout)
    seen_activity: set[str] = set()
    terminal: Status | None = None
    last_event_id: str | None = None
    got_frame = False
    attempts = 0
    # The last nonzero stream epoch seen on text/reasoning output (kept across
    # reconnects; 0 = none yet, or forgotten by a reset).
    last_source: tuple[str, int] | None = None

    def stopped() -> bool:
        return should_stop is not None and should_stop()

    def fresh_deadline() -> float:
        return max(0.0, deadline - clock())

    while not stopped() and clock() < deadline:
        got_frame = False
        remaining = fresh_deadline()
        if remaining <= 0:
            break
        stream = client.stream_activity(ticket_id, last_event_id=last_event_id)
        try:
            # A stalled attach must not outlive the watch deadline: the server
            # can hold an idle stream open with keepalives for up to 15min.
            async with asyncio.timeout(remaining):
                async for raw in stream:
                    if stopped():
                        return
                    if raw.event_id:
                        last_event_id = raw.event_id
                    mapped = _map_stream_event(raw)
                    if mapped is None:
                        continue
                    got_frame = True
                    if isinstance(mapped, Activity):
                        if mapped.event_id and mapped.event_id in seen_activity:
                            continue
                        if mapped.event_id:
                            seen_activity.add(mapped.event_id)
                        yield mapped
                    elif isinstance(mapped, Status):
                        if mapped.status in _TERMINAL_STATUSES:
                            terminal = mapped
                        else:
                            # A retry revives a Ticket after a terminal read.
                            terminal = None
                        if mapped.status:
                            yield mapped
                        elif mapped.summary:
                            raise ActivityStreamUnavailableError(mapped.summary)
                    elif isinstance(mapped, End):
                        status = mapped.status or (terminal.status if terminal else "")
                        error = mapped.error or (terminal.error if terminal else "")
                        if status == "failed" and not error and fresh_deadline() > 0:
                            try:
                                # Older status frames omit Ticket.error; the
                                # durable Ticket still has the actual failure.
                                async with asyncio.timeout(fresh_deadline()):
                                    ticket = await client.ticket(ticket_id)
                                if isinstance(ticket, dict):
                                    error = str(ticket.get("error") or "")
                            except Exception:
                                # A supplemental lookup must not replace the
                                # failed terminal frame we already received.
                                pass
                        yield End(
                            status=status,
                            summary=mapped.summary
                            or (terminal.summary if terminal else ""),
                            question=mapped.question
                            or (terminal.question if terminal else ""),
                            error=error,
                        )
                        return
                    elif isinstance(mapped, TimedOut):
                        yield mapped
                        return
                    elif isinstance(mapped, OutputReset):
                        # A fresh fragment starts after the reset, whatever
                        # source its text comes from.
                        last_source = None
                        yield mapped
                    elif isinstance(mapped, OutputChunk):
                        source = (mapped.run_id, mapped.epoch)
                        if last_source is not None and source != last_source:
                            # Safety net: the live output changed source (a new
                            # epoch, a new run, cloud <-> daemon) without an
                            # announced reset. Never let the new text continue
                            # the previous fragment.
                            yield OutputReset(reason="new_epoch", epoch=mapped.epoch)
                        last_source = source
                        yield mapped
                    elif isinstance(mapped, ToolCall):
                        # Tool names are rendered beside the text, so they belong
                        # to a source too (`tool: Bearer` then a token from
                        # another source reads as a credential).
                        if mapped.run_id or mapped.epoch:
                            source = (mapped.run_id, mapped.epoch)
                            if last_source is not None and source != last_source:
                                yield OutputReset(
                                    reason="new_epoch", epoch=mapped.epoch
                                )
                            last_source = source
                        yield mapped
                    else:
                        yield mapped
        except TimeoutError:
            # The attach consumed the whole watch budget without a frame.
            yield TimedOut(message="watch timed out; the Ticket is still running")
            return
        except ActivityStreamUnavailableError as exc:
            if not got_frame:
                # The endpoint is absent or not SSE: poll the event record.
                break
            yield StreamNotice(f"live stream lost ({exc}); reconnecting")
        except SuperAgentError as exc:
            if not got_frame:
                break
            yield StreamNotice(f"live stream interrupted ({exc}); reconnecting")
        except Exception as exc:
            if not got_frame:
                break
            yield StreamNotice(f"live stream failed ({exc}); reconnecting")
        finally:
            with_close = getattr(stream, "aclose", None)
            if with_close is not None:
                try:
                    await with_close()
                except Exception:
                    pass
        if not got_frame:
            # A clean stream that ends without frames names an endpoint with no
            # live path. Whether this is the first attach or a reconnect after
            # earlier frames, the durable record is still there: poll it rather
            # than abandoning the Ticket.
            break
        attempts += 1
        if attempts > SSE_RECONNECT_ATTEMPTS:
            break
        delay = min(
            SSE_BACKOFF_BASE_S * (2 ** (attempts - 1)),
            SSE_BACKOFF_MAX_S,
        )
        remaining = fresh_deadline()
        if remaining <= 0:
            break
        if stopped():
            return
        yield StreamNotice("reconnecting the live stream")
        await sleep(min(delay, remaining))

    if stopped():
        return
    if clock() >= deadline:
        yield TimedOut(message="watch timed out; the Ticket is still running")
        return

    yield StreamNotice("live stream unavailable; polling the Ticket record")
    while not stopped() and clock() < deadline:
        remaining = fresh_deadline()
        if remaining <= 0:
            break
        try:
            # The durable-record poll must not outlive the watch budget either.
            async with asyncio.timeout(remaining):
                ticket = await client.ticket(ticket_id)
                activity = await client.activity(ticket_id)
        except TimeoutError:
            yield TimedOut(message="watch timed out; the Ticket is still running")
            return
        except SuperAgentError as exc:
            yield StreamNotice(f"polling failed ({exc}); retrying")
            await sleep(min(POLL_INTERVAL_S, max(0.0, deadline - clock())))
            continue
        except Exception as exc:
            yield StreamNotice(f"polling failed ({exc}); retrying")
            await sleep(min(POLL_INTERVAL_S, max(0.0, deadline - clock())))
            continue
        if not isinstance(ticket, dict):
            ticket = {}
        if not isinstance(activity, dict):
            activity = {}
        status = str(ticket.get("status") or "")
        rows = activity.get("events")
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            event = _activity_from_row(row)
            if event.event_id and event.event_id in seen_activity:
                continue
            if event.event_id:
                seen_activity.add(event.event_id)
            yield event
        if status in _TERMINAL_STATUSES:
            attention = ticket.get("attention")
            question = (
                str(attention.get("question") or "")
                if isinstance(attention, dict)
                else ""
            )
            yield End(
                status=status,
                summary=str(ticket.get("summary") or ""),
                question=question,
                error=str(ticket.get("error") or ""),
            )
            return
        remaining = fresh_deadline()
        if remaining <= 0:
            break
        if stopped():
            return
        await sleep(min(POLL_INTERVAL_S, remaining))
    if not stopped() and clock() >= deadline:
        yield TimedOut(message="watch timed out; the Ticket is still running")


__all__ = [
    "Activity",
    "End",
    "Gap",
    "OutputChunk",
    "OutputError",
    "OutputReset",
    "POLL_INTERVAL_S",
    "SSE_BACKOFF_BASE_S",
    "SSE_BACKOFF_MAX_S",
    "SSE_RECONNECT_ATTEMPTS",
    "Status",
    "StreamNotice",
    "TicketEvent",
    "TimedOut",
    "ToolCall",
    "WATCH_TIMEOUT_S",
    "output_reset_notice",
    "watch_ticket",
]
