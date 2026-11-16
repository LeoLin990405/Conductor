"""Task-first interaction: create, track, and close a task object.

A worker thread is an execution detail. The activity projected here is the
record a person tracks, and close is the sign-off on that same object.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

_INSTRUCTION_KINDS = frozenset({"answer", "instruction"})
_KIND_ORDER = {"created": 0, "source": 1, "instruction": 2, "report": 3, "closed": 4}
_TERMINAL = frozenset({"completed", "cancelled", "archived"})
_REASON_LIMIT = 500


def _at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    raise TypeError(f"activity timestamp must be a datetime, got {type(value).__name__}")


def undelivered_instructions(inputs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Instructions stored on the task that no worker session has taken yet."""
    rows: list[dict[str, Any]] = []
    for item in inputs:
        if item.get("delivered_at"):
            continue
        if str(item.get("kind") or "") not in _INSTRUCTION_KINDS:
            continue
        if not str(item.get("text") or "").strip():
            continue
        rows.append(dict(item))
    return rows


def project_task_activity(
    ticket: Mapping[str, Any],
    inputs: Iterable[Mapping[str, Any]],
    reports: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project the task record. Session and run ids are not activity fields."""
    ticket_id = str(ticket.get("ticket_id") or "")
    title = str(ticket.get("title") or "").strip()
    scope = str(ticket.get("scope") or "").strip()
    events: list[dict[str, Any]] = [
        {
            "event_id": f"created:{ticket_id}",
            "kind": "created",
            "at": _at(ticket.get("created_at")),
            "summary": title or scope or "Task created",
            "detail": scope if title and scope and scope != title else "",
            "state": "",
        }
    ]
    for item in inputs:
        kind = str(item.get("kind") or "input")
        if kind in _INSTRUCTION_KINDS:
            event_kind = "instruction"
        elif kind == "trigger":
            event_kind = "source"
        else:
            event_kind = kind
        events.append(
            {
                "event_id": f"input:{item.get('input_id') or ''}",
                "kind": event_kind,
                "at": _at(item.get("created_at")),
                "summary": str(item.get("text") or ""),
                "detail": "",
                "state": "",
            }
        )
    for report in reports:
        events.append(
            {
                "event_id": f"report:{report.get('report_id') or ''}",
                "kind": "report",
                "at": _at(report.get("created_at")),
                "summary": str(report.get("summary") or ""),
                "detail": str(report.get("question") or ""),
                "state": str(report.get("state") or ""),
            }
        )
    closed_at = ticket.get("closed_at")
    if closed_at:
        reason = str(ticket.get("close_reason") or "").strip()
        events.append(
            {
                "event_id": f"closed:{ticket_id}",
                "kind": "closed",
                "at": _at(closed_at),
                "summary": reason or "Task closed",
                "detail": "",
                "state": "",
            }
        )
    events.sort(
        key=lambda event: (
            _at(event["at"]),
            _KIND_ORDER.get(str(event["kind"]), 9),
            str(event["event_id"]),
        )
    )
    return events


def close_update(
    ticket: Mapping[str, Any], *, reason: str, now: datetime
) -> dict[str, Any] | None:
    """Fields that close a task, or None when it is already closed.

    A non-terminal task is cancelled so execution stops. A completed,
    cancelled, or archived task keeps that outcome and only gains the close stamp.
    """
    if ticket.get("closed_at"):
        return None
    fields: dict[str, Any] = {
        "closed_at": now,
        "close_reason": (reason or "").strip()[:_REASON_LIMIT],
    }
    status = str(ticket.get("status") or "")
    if status not in _TERMINAL:
        fields["status"] = "cancelled"
        fields["attention"] = None
        if not ticket.get("ended_at"):
            fields["ended_at"] = now
    return fields
