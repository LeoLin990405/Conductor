"""The worker's own account of where its work stands, asked for before a proactive rotation.

The handover note's system section is written by the session manager from git
facts and fixed wording, never by a model. This module describes the separate
section a worker may add to that note: what it says is done, in progress and
next, the pitfalls it hit, and what is still open. The section is labelled
``worker_reported``. It is a reference for the next session only and never
decides a Ticket's acceptance.

Validation is strict: a payload that does not match the schema is discarded,
never repaired. Credentials are redacted before this module sees the text (the
dispatcher runs the report sanitizer first), and the session manager redacts
the whole note once more where it writes it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

WORKER_REPORT_LABEL = "worker_reported"
WORKER_REPORT_FIELDS = ("done", "in_progress", "next", "pitfalls", "open_questions")
WORKER_REPORT_MAX_ITEMS = 10
WORKER_REPORT_MAX_ITEM_CHARS = 200
WORKER_REPORT_MAX_BYTES = 4096
WORKER_REPORT_TIMEOUT_SECONDS = 60.0

#: The one turn the old session is sent. It names the exact schema and limits.
WORKER_REPORT_PROMPT = (
    "This session is about to be replaced by a fresh one. Before it ends, report "
    "where the work stands. Reply with one JSON object and nothing else. Use exactly "
    'these keys: "done", "in_progress", "next", "pitfalls", "open_questions". '
    f"Each value is a list of at most {WORKER_REPORT_MAX_ITEMS} strings, each string "
    f"at most {WORKER_REPORT_MAX_ITEM_CHARS} characters, and the whole object at most "
    f"{WORKER_REPORT_MAX_BYTES} bytes. Do not include credentials, tokens or file "
    "contents. Do not run tools or change files in this turn."
)

_TITLES = {
    "done": "已完成",
    "in_progress": "进行中",
    "next": "下一步",
    "pitfalls": "踩过的坑",
    "open_questions": "未决问题",
}


@dataclass(frozen=True)
class WorkerReport:
    """A self-report that matched the schema."""

    done: tuple[str, ...]
    in_progress: tuple[str, ...]
    next: tuple[str, ...]
    pitfalls: tuple[str, ...]
    open_questions: tuple[str, ...]
    byte_count: int


def worker_report_from_json(text: str) -> WorkerReport | None:
    """The report in ``text``, or None when it is not exactly one valid report.

    ``text`` must be one JSON object with exactly the five keys, each a list of
    at most ten strings of at most 200 characters, and at most 4096 bytes in
    all. Anything else is None: a missing or extra key, a number, a nested
    object, one item too many or one character too long.
    """
    size = len(str(text).encode("utf-8"))
    if size > WORKER_REPORT_MAX_BYTES:
        return None
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or set(payload) != set(WORKER_REPORT_FIELDS):
        return None
    fields: dict[str, tuple[str, ...]] = {}
    for name in WORKER_REPORT_FIELDS:
        items = payload[name]
        if not isinstance(items, list) or len(items) > WORKER_REPORT_MAX_ITEMS:
            return None
        for item in items:
            if not isinstance(item, str) or len(item) > WORKER_REPORT_MAX_ITEM_CHARS:
                return None
        fields[name] = tuple(items)
    return WorkerReport(
        done=fields["done"],
        in_progress=fields["in_progress"],
        next=fields["next"],
        pitfalls=fields["pitfalls"],
        open_questions=fields["open_questions"],
        byte_count=size,
    )


def _one_line(text: str) -> str:
    """One list item on one line: a newline cannot open a heading of its own."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())


def worker_report_lines(report: WorkerReport) -> list[str]:
    """The note's ``worker_reported`` section, after the system section."""
    lines = [
        f"## {WORKER_REPORT_LABEL}（Worker 自述，未经验证）",
        "",
        "以下内容由上一个会话自述，只作参考，不参与 Ticket 验收。",
        "",
    ]
    for name in WORKER_REPORT_FIELDS:
        items = getattr(report, name)
        lines.append(f"### {_TITLES[name]}（{name}）")
        lines.append("")
        if items:
            lines.extend(f"- {_one_line(item)}" for item in items)
        else:
            lines.append("- （无）")
        lines.append("")
    return lines
