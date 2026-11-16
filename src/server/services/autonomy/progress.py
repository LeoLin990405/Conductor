"""Read-only progress for the Supervisor.

The view is the Ticket plus the activity and report rows already stored for
it. It names the phase and which of the three visible stages have happened.
It does not carry a live Worker transcript, tool call, prompt, or file body.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

# Ticket status → Supervisor phase. Statuses that are not in this table stay
# as themselves so an unknown value is not relabeled.
_PHASE = {
    "queued": "queued",
    "running": "running",
    "waiting": "waiting",
    "blocked": "waiting",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "archived": "cancelled",
}

# Report states a Worker settlement actually writes. ``progress`` is a
# heartbeat summary, not a final result.
_REPORT_STATES = frozenset({"ready", "blocked", "failed", "dropped", "waiting", "progress"})
_FINAL_REPORT_STATES = frozenset({"ready", "blocked", "failed", "dropped", "waiting"})

# Rows from the live Worker stream. They are not progress evidence.
_STREAM_KINDS = frozenset({"text", "reasoning", "tool", "stop", "token", "prompt"})

_CREATED = {"key": "created", "label": "任务已创建", "state": "done"}
_WORKER_LABEL = "执行中"
_REPORT_LABEL = "结果已生成"


def build_ticket_progress(
    ticket: Mapping[str, Any],
    activity_rows: Iterable[Mapping[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project one Ticket into the Supervisor progress payload.

    ``activity_rows`` accepts report rows (``report_id`` / ``state``) or the
    task-activity events from ``project_task_activity``. Stream rows are
    ignored. Summary is the Ticket's own summary, unchanged.
    """
    status = str(ticket.get("status") or "")
    phase = _PHASE.get(status, status)
    reports = _report_rows(activity_rows or ())
    summary = _text(ticket.get("summary"))
    latest = max(reports, key=_report_order, default=None)
    return {
        "ticket_id": str(ticket.get("ticket_id") or ""),
        "status": status,
        "phase": phase,
        "elapsed_seconds": _elapsed_seconds(ticket, now=now),
        "steps": _steps(ticket, phase, reports),
        "summary": summary,
        "body": latest.get("body") if latest is not None else None,
        "question": _question(ticket, phase, reports),
        "error": _text(ticket.get("error")) if phase == "failed" else "",
    }


def _report_rows(activity_rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Report rows only. Stream and instruction rows drop out.

    ``order`` keeps the caller's position so a tie picks the first row.
    ``list_reports`` returns newest first, which is the route's caller.
    """
    found: list[dict[str, Any]] = []
    for index, row in enumerate(activity_rows):
        if not isinstance(row, Mapping):
            continue
        kind = str(row.get("kind") or "")
        if kind in _STREAM_KINDS:
            continue
        state = str(row.get("state") or "")
        is_report = kind == "report" or "report_id" in row or state in _REPORT_STATES
        if not is_report:
            continue
        question = row.get("question")
        if not _text(question) and kind == "report":
            # Task activity stores the report question on ``detail``.
            question = row.get("detail")
        found.append(
            {
                "state": state,
                "question": _text(question),
                "body": row.get("body"),
                "at": _as_utc(row.get("created_at", row.get("at"))),
                "order": index,
            }
        )
    return found


def _report_order(row: Mapping[str, Any]) -> tuple[bool, datetime, int]:
    at = row.get("at")
    return (
        at is not None,
        at if isinstance(at, datetime) else datetime.min.replace(tzinfo=timezone.utc),
        -int(row.get("order") or 0),
    )


def _steps(
    ticket: Mapping[str, Any],
    phase: str,
    reports: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """The stages that already have evidence. Later stages are left out."""
    steps: list[dict[str, str]] = [dict(_CREATED)]
    started = _as_utc(ticket.get("started_at")) is not None
    has_report = bool(reports)
    has_final = any(row["state"] in _FINAL_REPORT_STATES for row in reports)

    # ``running`` / ``waiting`` / ``completed`` are themselves evidence that a
    # Worker turn exists. A failure or a cancel before ``started_at`` and
    # before any report has no execution stage to show.
    show_worker = phase in {"running", "waiting", "completed"} or (
        phase in {"failed", "cancelled"} and (started or has_report)
    )
    if show_worker:
        if phase == "running":
            worker_state = "active"
        elif phase == "failed":
            worker_state = "failed"
        else:
            worker_state = "done"
        steps.append({"key": "worker", "label": _WORKER_LABEL, "state": worker_state})

    # A progress heartbeat is not a result. A queued Ticket keeps any older
    # report off this view so a requeue does not look finished.
    show_report = phase in {"completed", "waiting", "failed"} or (
        phase == "cancelled" and (has_final or _text(ticket.get("summary")) != "")
    )
    if show_report:
        steps.append(
            {
                "key": "report",
                "label": _REPORT_LABEL,
                "state": "failed" if phase == "failed" else "done",
            }
        )
    return steps


def _question(
    ticket: Mapping[str, Any],
    phase: str,
    reports: list[dict[str, Any]],
) -> str:
    """The question a person must answer. Empty unless the phase is waiting."""
    if phase != "waiting":
        return ""
    attention = ticket.get("attention")
    if isinstance(attention, dict):
        asked = _text(attention.get("question"))
        if asked:
            return asked
    chosen = ""
    chosen_key: tuple[bool, datetime, int] | None = None
    for row in reports:
        if row["state"] == "progress" or not row["question"]:
            continue
        at = row.get("at")
        # A real timestamp beats a missing one. On a tie the earlier input
        # row wins, matching list_reports (newest first).
        key = (
            at is not None,
            at if isinstance(at, datetime) else datetime.min.replace(tzinfo=timezone.utc),
            -int(row.get("order") or 0),
        )
        if chosen_key is None or key > chosen_key:
            chosen_key = key
            chosen = row["question"]
    return chosen


def _elapsed_seconds(ticket: Mapping[str, Any], *, now: datetime | None) -> float:
    """Seconds from ``created_at`` to ``ended_at``, or to ``now`` while open."""
    start = _as_utc(ticket.get("created_at"))
    if start is None:
        return 0.0
    end = _as_utc(ticket.get("ended_at"))
    if end is None:
        end = _as_utc(now) or datetime.now(timezone.utc)
    seconds = (end - start).total_seconds()
    if seconds < 0:
        return 0.0
    return round(seconds, 1)


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)
