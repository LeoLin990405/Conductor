"""Read the self-report a worker's old session gives before a proactive rotation.

``read_self_report`` receives the parser's final text, not a raw log. This
module turns that text into a ``WorkerReport`` or into a status saying why
there is none. The text must already be one JSON object, aside from at most
one markdown fence. The order is fixed: size bound, report sanitizer, schema.
Anything else is invalid or oversize, never repaired, and the rotation falls
back to the deterministic note alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from src.llms.native_cli.worker_report import (
    WORKER_REPORT_MAX_BYTES,
    WorkerReport,
    worker_report_from_json,
)
from src.server.utils.sanitize import sanitize_report_text


@dataclass(frozen=True)
class SelfReportReply:
    """What the launcher got back from the old session.

    ``status`` is ``answered`` when the turn finished and ``output`` holds the
    parser's final text; otherwise it is ``timeout``, ``failed`` or
    ``unsupported`` and ``output`` is empty.
    """

    status: str
    output: str = ""


@dataclass(frozen=True)
class SelfReportOutcome:
    """The report, or the status that says why the note has no worker section."""

    report: WorkerReport | None
    status: str
    byte_count: int = 0


def _opening_fence_line(line: str) -> bool:
    """True when ``line`` is three backticks and an optional json tag."""
    stripped = line.strip()
    if not stripped.startswith("```"):
        return False
    tag = stripped[3:].strip()
    return tag == "" or tag.lower() == "json"


def _report_candidate(output: str) -> str | None:
    """One JSON object from the parser's final text, or None.

    The text is stripped. At most one opening fence line that starts with
    ``` (optional json tag) and one closing line that is ``` are removed.
    The remainder must be exactly one JSON object: ``json.loads`` of the whole
    remainder, and the value a dict. Trailing prose, a second object, or JSON
    that does not start the remainder is rejected. Nothing is repaired.
    """
    text = output.strip()
    lines = text.splitlines(keepends=True)
    if (
        len(lines) >= 2
        and _opening_fence_line(lines[0])
        and lines[-1].strip() == "```"
    ):
        text = "".join(lines[1:-1]).strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    return text


def read_self_report(reply: SelfReportReply | None) -> SelfReportOutcome:
    """Size bound, sanitizer, then schema. Anything that fails is a status, not a report."""
    if reply is None:
        return SelfReportOutcome(None, "unsupported")
    if reply.status != "answered":
        status = reply.status if reply.status in {"timeout", "failed", "unsupported", "invalid", "oversize"} else "failed"
        return SelfReportOutcome(None, status)
    candidate = _report_candidate(reply.output or "")
    if candidate is None:
        return SelfReportOutcome(None, "invalid")
    size = len(candidate.encode("utf-8"))
    if size > WORKER_REPORT_MAX_BYTES:
        return SelfReportOutcome(None, "oversize", size)
    report = worker_report_from_json(sanitize_report_text(candidate))
    if report is None:
        return SelfReportOutcome(None, "invalid", size)
    return SelfReportOutcome(report, "ok", report.byte_count)


def lesson_candidates(
    report: WorkerReport, *, worker: str, dispatch_id: str, line_id: str
) -> list[dict[str, object]]:
    """Lesson candidates from the report's pitfalls, for a human to review.

    Off unless the dispatcher was given a sink. Each candidate is inferred by
    an agent and needs review before it becomes a memory.
    """
    return [
        {
            "class": "Lesson",
            "text": item,
            "provenance_type": "agent_inferred",
            "review": "required",
            "source": {
                "kind": "worker_self_report",
                "worker": worker,
                "dispatch": dispatch_id,
                "line": line_id,
            },
        }
        for item in report.pitfalls
    ]
