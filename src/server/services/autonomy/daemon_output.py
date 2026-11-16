"""Bounded live output from a local daemon/native Worker run.

The daemon runs the vendor CLI on the user's machine. While it runs, it posts
model text and tool names here; the Ticket activity SSE tails this log so
``/jobs watch`` shows the Worker's output as it happens instead of waiting for
the final report.

Protocol (the "stream fence"):

* The server owns the **epoch**. A daemon's first batch for a run carries no
  epoch (and ``batch_seq`` 1); the server draws a fresh epoch from a
  database-wide sequence (never from the session row, so a deleted and
  recreated session cannot reissue one), resets the scan state and answers
  with it. Every later batch echoes that epoch and
  increments ``batch_seq``. A mismatching epoch or lease is refused with
  ``stale_output_epoch``; a replayed ``batch_seq`` with ``duplicate_output_batch``;
  a skipped ``batch_seq`` means a batch was lost, so continuity is unprovable
  and the stream is tainted.
* The **session row** (one per Ticket, ``FOR UPDATE`` on every append) holds the
  epoch, the sequence counters and the bounded scan state of
  :mod:`src.server.utils.output_guard`. Its update and the event inserts share
  one transaction, so a guard and the text it describe cannot diverge, two
  workers cannot decide against the same state, and a failed append produces no
  event at all.
* **Taint is one-way.** Once any text event cannot be proven safe the session is
  tainted: that event and everything after it (text and tool names alike: names
  that outlived the text would form a projection no window tracks) are never
  stored, and watchers receive an ``output_reset`` marker in the event log.
* **Watchers are fenced by the same epoch.** A reader whose cursor names another
  epoch, a trimmed gap, or an impossible position is told to reset before it
  receives anything, so a prefix it already holds is never joined to a suffix
  from a different generation.

Only visible model text and tool *names* cross the boundary. Tool arguments,
tool results and the prompt never enter this log. Streaming is a bonus channel:
every store failure degrades to "not stored", never to a Worker failure.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from src.server.database.autonomy import daemon_output as output_db
from src.server.utils import output_guard as guard
from src.server.utils.sanitize import sanitize_output_text

logger = logging.getLogger(__name__)

# Retained rows per Ticket epoch; a slower watcher gets an explicit reset.
OUTPUT_MAX_EVENTS = 2048
OUTPUT_TEXT_MAX = guard.EVENT_TEXT_MAX
OUTPUT_TOOL_MAX = 8
OUTPUT_TOOL_NAME_MAX = 64
OUTPUT_BATCH_MAX = 64
OUTPUT_KINDS = frozenset({"text", "reasoning", "tool", "stop"})
OUTPUT_READ_MAX = 500
RUN_ID_MAX = 128

CODE_STALE_EPOCH = "stale_output_epoch"
CODE_DUPLICATE = "duplicate_output_batch"
CODE_BAD_BATCH = "bad_output_batch"
CODE_STALE_LEASE = "stale_lease"
CODE_UNAVAILABLE = "store_unavailable"

RESET_NEW_EPOCH = "new_epoch"
RESET_CONTINUITY = "continuity_lost"
RESET_GAP = "gap"
RESET_CURSOR = "cursor_invalid"
RESET_KIND = "output_reset"


@dataclass(frozen=True)
class AppendResult:
    """What the daemon is told about one batch."""

    stored: bool
    epoch: int = 0
    head: str | None = None
    tainted: bool = False
    reason: str = ""
    code: str = ""
    events: int = 0


@dataclass(frozen=True)
class Plan:
    """The decision for one locked session row (pure; applied by the DB layer)."""

    write: bool
    result: AppendResult
    session: dict[str, Any] = field(default_factory=dict)
    rows: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    purge_older_epochs: bool = False
    trim_through: int = 0


_ASCII_DIGITS = re.compile(r"[0-9]+")


def format_cursor(epoch: int, seq: int) -> str:
    return f"{int(epoch)}.{int(seq)}"


def parse_cursor(raw: str | None) -> tuple[int, int] | None:
    """``(epoch, seq)`` for a well-formed cursor; ``None`` when absent/invalid."""
    if not raw:
        return None
    epoch_text, dot, seq_text = str(raw).partition(".")
    # ASCII digits only: str.isdigit() also accepts "²" and the like, which
    # int() then rejects.
    if not (_ASCII_DIGITS.fullmatch(epoch_text) and _ASCII_DIGITS.fullmatch(seq_text)):
        return None
    if not dot:
        return None
    if len(epoch_text) > 18 or len(seq_text) > 18:
        return None
    epoch, seq = int(epoch_text), int(seq_text)
    return (epoch, seq) if epoch >= 1 else None


def normalize_output_events(events: Iterable[Any]) -> list[dict[str, Any]]:
    """The structural shape of a daemon batch.

    Text is kept raw: whether it may be stored is the scanner's decision, made
    against the persisted stream state, never a per-event rewrite.
    """
    normalized: list[dict[str, Any]] = []
    for event in list(events)[:OUTPUT_BATCH_MAX]:
        kind = str(getattr(event, "kind", "") or "")
        if kind not in OUTPUT_KINDS:
            continue
        if kind == "tool":
            names: list[str] = []
            for raw in list(getattr(event, "tools", ()) or ())[:OUTPUT_TOOL_MAX]:
                # Bound the input before scanning it, not only the result.
                bounded = str(raw)[: OUTPUT_TOOL_NAME_MAX * 4]
                name = sanitize_output_text(bounded, limit=OUTPUT_TOOL_NAME_MAX).strip()
                if name:
                    names.append(name)
            if names:
                normalized.append({"kind": "tool", "tools": names})
            continue
        if kind == "stop":
            normalized.append({"kind": "stop"})
            continue
        text = str(getattr(event, "text", "") or "")
        if text:
            row = {"kind": kind, "text": text}
            if getattr(event, "join", False):
                row["join"] = True
            if getattr(event, "delta", False):
                row["delta"] = True
            normalized.append(row)
    return normalized


def _reject(code: str, epoch: int) -> Plan:
    return Plan(write=False, result=AppendResult(stored=False, epoch=epoch, code=code))


def plan_batch(
    row: dict[str, Any],
    *,
    lease_id: str,
    run_id: str,
    epoch: int | None,
    batch_seq: int,
    events: list[dict[str, Any]],
    allocated_epoch: int | None = None,
) -> Plan:
    """Decide one batch against the locked session row. Pure and deterministic.

    ``allocated_epoch`` is the database sequence value the caller drew for an
    allocation (an ``epoch`` of ``None``); it is required then and must not
    precede the row's epoch, so an epoch is never reissued.
    """
    run = sanitize_output_text(run_id, limit=RUN_ID_MAX)
    unallocated = row.get("lease_id") is None
    current = int(row.get("epoch") or 1)
    gap_taint = False
    if not epoch:
        # Allocation: the first batch of a run. The server picks the epoch.
        if batch_seq != 1:
            return _reject(CODE_BAD_BATCH, 0 if unallocated else current)
        if allocated_epoch is None or allocated_epoch < current:
            raise RuntimeError("epoch allocation requires a fresh sequence value")
        new_epoch = int(allocated_epoch)
        state = guard.GuardState()
        seq = 0
        purge = not unallocated
    else:
        if (
            unallocated
            or int(epoch) != current
            or str(lease_id).lower() != str(row.get("lease_id")).lower()
        ):
            return _reject(CODE_STALE_EPOCH, 0 if unallocated else current)
        last_batch = int(row.get("last_batch") or 0)
        if batch_seq <= last_batch:
            return _reject(CODE_DUPLICATE, current)
        new_epoch = current
        purge = False
        seq = int(row.get("next_seq") or 0)
        state = guard.GuardState.from_columns(
            row.get("guard_tail"), row.get("tainted"), row.get("guard_state")
        )
        if state.tainted and not row.get("tainted"):
            # The persisted guard was malformed or incomplete (restored as
            # tainted though the row said otherwise): nothing is stored, and
            # watchers are told instead of the stream just going quiet.
            gap_taint = True
        if batch_seq != last_batch + 1 and not state.tainted:
            # A batch went missing: whatever it held may be the half of a
            # boundary this stream can no longer see.
            state = guard.taint(state, guard.REASON_BATCH_GAP)
            gap_taint = True

    rows: list[tuple[int, dict[str, Any]]] = []

    def emit(data: dict[str, Any]) -> None:
        nonlocal seq
        seq += 1
        rows.append((seq, {**data, "run_id": run}))

    def emit_reset() -> None:
        emit({"kind": RESET_KIND, "reason": RESET_CONTINUITY})

    if gap_taint:
        emit_reset()
    refused = False
    for event in events[:OUTPUT_BATCH_MAX]:
        kind = str(event.get("kind") or "")
        if kind == "tool":
            names = [str(name) for name in event.get("tools") or [] if name]
            if names and not state.tainted:
                # Tool names are rendered beside the text, so they belong to
                # the same continuity contract: `tool: Bearer` followed by a
                # token reads as a credential exactly as if both were text.
                decision = guard.scan(
                    state, "\n".join(names) + "\n", channel=guard.CHANNEL_TOOL
                )
                state = decision.state
                if not decision.ok:
                    refused = True
                    emit_reset()
                    continue
            elif names:
                # Tainted: tool names stop with the text. Names that survive on
                # their own would form a projection of their own (`Basic` in one
                # event, its token in the next) that no window tracks. They are
                # reported as not stored.
                refused = True
                continue
            emit({"kind": "tool", "tools": names})
            continue
        if kind == "stop":
            # The daemon stopped its own text channel (a local scan refusal, a
            # skipped record, ...): close the epoch for text and tell watchers.
            if not state.tainted:
                state = guard.taint(state, guard.REASON_DISCONTINUITY)
                emit_reset()
            continue
        if kind not in {"text", "reasoning"}:
            continue
        if state.tainted:
            refused = True
            continue
        text = str(event.get("text") or "")
        # Older transport models drop the optional ``delta`` key; a join event
        # without the synthetic newline can only be a token delta, so infer it
        # from the wire shape as well.
        delta = bool(event.get("delta")) or (
            bool(event.get("join")) and not text.endswith("\n")
        )
        synthetic = delta and not text.endswith("\n")
        scan_text = text + "\n" if synthetic else text
        decision = guard.scan(
            state,
            scan_text,
            channel=kind,
            join=bool(event.get("join")) or synthetic,
        )
        state = decision.state
        if decision.ok:
            stored = decision.text
            if synthetic and stored.endswith("\n"):
                stored = stored[:-1]
            if stored:
                emit({"kind": kind, "text": stored})
            continue
        refused = True
        emit_reset()

    head = format_cursor(new_epoch, seq) if rows else None
    result = AppendResult(
        stored=not refused,
        epoch=new_epoch,
        head=head,
        tainted=state.tainted,
        reason=state.reason if state.tainted else "",
        events=len(events),
    )
    session = {
        "epoch": new_epoch,
        "lease_id": str(lease_id),
        "run_id": run,
        "next_seq": seq,
        "last_batch": batch_seq,
        "tainted": state.tainted,
        "taint_reason": state.reason if state.tainted else "",
        "guard_tail": state.window,
        "guard_state": state.to_json(),
    }
    return Plan(
        write=True,
        result=result,
        session=session,
        rows=rows,
        purge_older_epochs=purge,
        trim_through=max(0, seq - OUTPUT_MAX_EVENTS),
    )


async def append_output(
    daemon_id: str,
    ticket_id: str,
    lease_id: str,
    run_id: str,
    epoch: int | None,
    batch_seq: int,
    events: list[dict[str, Any]],
) -> AppendResult:
    """Append one batch, atomically. Never raises.

    Any store failure (database down, lock timeout, constraint) rolls the whole
    transaction back and answers ``stored=False``: no event was produced and
    the persisted guard is unchanged, so the next append is judged against
    exactly what watchers could have seen.
    """
    if not events:
        return AppendResult(stored=False, epoch=int(epoch or 0))

    def decide(row: dict[str, Any], allocated: int | None) -> Plan:
        return plan_batch(
            row,
            lease_id=lease_id,
            run_id=run_id,
            epoch=epoch,
            batch_seq=batch_seq,
            events=events,
            allocated_epoch=allocated,
        )

    try:
        live, plan = await output_db.commit_batch(
            daemon_id=daemon_id,
            ticket_id=ticket_id,
            lease_id=lease_id,
            allocate=not epoch,
            decide=decide,
        )
    except Exception:  # noqa: BLE001 - streaming never fails the Worker
        logger.warning(
            "[daemon-output] append failed ticket=%s", ticket_id, exc_info=True
        )
        return AppendResult(
            stored=False, epoch=int(epoch or 0), code=CODE_UNAVAILABLE
        )
    if not live or plan is None:
        return AppendResult(stored=False, epoch=int(epoch or 0), code=CODE_STALE_LEASE)
    return plan.result


@dataclass(frozen=True)
class OutputPage:
    """One bounded read for a watcher."""

    epoch: int = 0
    # Position after the last returned event (or the unchanged position).
    cursor: str = ""
    # Position just before the first returned event: where a watcher that was
    # reset stands before it consumes the events.
    start: str = ""
    reset: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


async def read_output(
    ticket_id: str, cursor: str | None, *, count: int = 200
) -> OutputPage:
    """Read the next events for a watcher; an unavailable store reads as empty.

    ``reset`` is set when the watcher must discard everything it has received
    before consuming ``events``: its cursor names another epoch, a position
    that no longer exists (trimmed) or one that never did.
    """
    limit = max(1, min(int(count), OUTPUT_READ_MAX))
    try:
        session = await output_db.read_session(ticket_id)
        if session is None or session.get("lease_id") is None:
            return OutputPage(cursor=cursor or "")
        epoch = int(session["epoch"])
        next_seq = int(session["next_seq"])
        parsed = parse_cursor(cursor)
        reset: str | None = None
        after = 0
        had_position = False
        if cursor and parsed is None:
            reset = RESET_CURSOR
        elif parsed is not None:
            cursor_epoch, cursor_seq = parsed
            if cursor_epoch != epoch:
                reset = RESET_NEW_EPOCH
            elif cursor_seq > next_seq:
                reset = RESET_CURSOR
            else:
                after = cursor_seq
                had_position = True
        # Events and the session's head commit together, so nothing exists past
        # the head: a watcher already there needs no events query, which makes
        # an idle poll a single statement.
        rows = (
            await output_db.read_events(ticket_id, epoch, after, limit)
            if after < next_seq
            else []
        )
    except Exception:  # noqa: BLE001 - a missing log is a state, not an error
        logger.debug("[daemon-output] read failed ticket=%s", ticket_id, exc_info=True)
        return OutputPage(cursor=cursor or "")
    # Retained rows must be consecutive. A hole inside the page (rows removed
    # by anything other than prefix trimming) is never delivered across: the
    # page stops before it, and the next read sees a position gap and resets.
    contiguous = rows[:1]
    for row in rows[1:]:
        if row[0] != contiguous[-1][0] + 1:
            break
        contiguous.append(row)
    rows = contiguous
    if not had_position and after < next_seq and not rows:
        # The log has events and none is retained (all expired), and this
        # watcher holds nothing. Stand at the head, as a reset does below: the
        # position 0 handed out would be read as a hole on the very next read
        # and reset a watcher that never had anything to lose.
        head = format_cursor(epoch, next_seq)
        return OutputPage(epoch=epoch, cursor=head, start=head, reset=reset)
    if had_position and after < next_seq and (not rows or rows[0][0] != after + 1):
        # Rows between the watcher's position and the oldest retained one were
        # trimmed away (or are not yet visible): it cannot be joined to them.
        reset = RESET_GAP
        if not rows:
            # Everything after the watcher's position has expired. Stand at the
            # known head so the next read is not the same gap again (a reset
            # loop); anything appended later has a higher sequence.
            head = format_cursor(epoch, next_seq)
            return OutputPage(epoch=epoch, cursor=head, start=head, reset=reset)
    last = rows[-1][0] if rows else after
    first = rows[0][0] - 1 if rows else after
    payloads = [
        {"seq": seq, "cursor": format_cursor(epoch, seq), "data": data}
        for seq, data in rows
        if isinstance(data, dict)
    ]
    return OutputPage(
        epoch=epoch,
        cursor=format_cursor(epoch, last),
        start=format_cursor(epoch, first),
        reset=reset,
        events=payloads,
    )


__all__ = [
    "AppendResult",
    "CODE_BAD_BATCH",
    "CODE_DUPLICATE",
    "CODE_STALE_EPOCH",
    "CODE_STALE_LEASE",
    "CODE_UNAVAILABLE",
    "OUTPUT_BATCH_MAX",
    "OUTPUT_KINDS",
    "OUTPUT_MAX_EVENTS",
    "OUTPUT_TEXT_MAX",
    "OutputPage",
    "Plan",
    "RESET_CONTINUITY",
    "RESET_CURSOR",
    "RESET_GAP",
    "RESET_KIND",
    "RESET_NEW_EPOCH",
    "append_output",
    "format_cursor",
    "normalize_output_events",
    "parse_cursor",
    "plan_batch",
    "read_output",
]
