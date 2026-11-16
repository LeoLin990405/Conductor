"""Conversation model for the Super-agent TUI.

The TUI is not a scrolling log of strings: it keeps typed blocks so a tool
call can be collapsed, its duration measured, and a unified diff in the
assistant's text colored. This module is pure state plus two renderers
(prompt_toolkit formatted lines and plain text for the ``-p`` one-shot mode); it
imports no prompt_toolkit code.

The reducer is fed the events produced by :mod:`ptc_cli.superagent.tui_events`
and is deterministic given an injectable clock, so the interesting behaviour
(streamed assistant text, tool status/duration, diff splitting, activity
dedupe, token estimate) is testable without a terminal.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .text import plain
from .tui_events import (
    Activity,
    End,
    Gap,
    OutputChunk,
    OutputError,
    OutputReset,
    Status,
    StreamNotice,
    TimedOut,
    ToolCall,
    output_reset_notice,
)

# A streamed tool line list is capped so a chatty worker cannot fill memory
# with the whole transcript twice (the conversation still shows the block).
MAX_TOOL_LINES = 200
# How many lines an expanded tool block shows in the conversation.
MAX_EXPANDED_TOOL_LINES = 12
# A collapsed tool previews this many lines, then a ctrl+o trailer.
COLLAPSED_TOOL_LINES = 3
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 60 * _SECONDS_PER_MINUTE
# The conversation keeps this many compacted blocks in the history.
MAX_BLOCKS = 800

_CJK = re.compile(
    "[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)
_DIFF_FILE_HEADER = re.compile(r"^diff --git a/\S+ b/\S+$")
_DIFF_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
_MENTION_RE = re.compile(r"(?<![\w@])@([^\s@]+)")


def estimate_tokens(text: str) -> int:
    """A cheap, visible-order-of-magnitude token estimate.

    CJK characters are roughly one token each; other text is estimated at four
    characters per token. This is only for the status bar, never billing.
    """
    body = str(text or "")
    if not body:
        return 0
    cjk = len(_CJK.findall(body))
    other = len(body) - cjk
    return cjk + (other + 3) // 4


def format_duration(seconds: float | None) -> str:
    """Compact duration for a tool/step status (non-finite values omitted)."""
    if seconds is None:
        return ""
    try:
        value = float(seconds)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(value) or value < 0:
        return ""
    if value < 1.0:
        return f"{int(value * 1000)}ms"
    if value < _SECONDS_PER_MINUTE:
        return f"{value:.1f}s"
    # Hour scale drops leftover seconds (2h05m52s is shown as 2h05m). Under an
    # hour the minute form still keeps seconds, matching the existing "1m01s".
    whole = int(value)
    if whole >= _SECONDS_PER_HOUR:
        hours, remainder = divmod(whole, _SECONDS_PER_HOUR)
        text = f"{hours}h{remainder // _SECONDS_PER_MINUTE:02d}m"
    else:
        minutes, rest = divmod(whole, _SECONDS_PER_MINUTE)
        text = f"{minutes}m{rest:02d}s"
    return text


@dataclass(frozen=True, slots=True)
class DiffLine:
    marker: str
    text: str


def parse_unified_diff(body: str) -> list[DiffLine] | None:
    """Split a unified diff into colored lines, or None for ordinary text.

    A diff must carry at least one ``diff --git``/``---``+``+++`` file header
    and one hunk. A lone ``- item`` line is ordinary text, not a removal.
    """
    lines = str(body or "").splitlines()
    if not lines:
        return None
    has_file_header = any(_DIFF_FILE_HEADER.match(line) for line in lines) or (
        any(line.startswith("--- ") for line in lines)
        and any(line.startswith("+++ ") for line in lines)
    )
    has_hunk = any(_DIFF_HUNK.match(line) for line in lines)
    if not (has_hunk and has_file_header):
        return None
    parsed: list[DiffLine] = []
    for line in lines:
        if line.startswith("diff --git ") or line.startswith("+++") or line.startswith("---"):
            parsed.append(DiffLine("header", line))
        elif line.startswith("@@"):
            parsed.append(DiffLine("hunk", line))
        elif line.startswith("+"):
            parsed.append(DiffLine("add", line))
        elif line.startswith("-"):
            parsed.append(DiffLine("del", line))
        else:
            parsed.append(DiffLine("context", line))
    return parsed


def split_text_and_diffs(body: str) -> list[any]:
    """Split assistant text into text runs and diff runs (in source order)."""
    lines = str(body or "").split("\n")
    pieces: list[Any] = []
    index = 0
    while index < len(lines):
        found = None
        for start in range(index, len(lines)):
            if _DIFF_FILE_HEADER.match(lines[start]):
                found = start
                break
            if lines[start].startswith("--- ") and start + 1 < len(lines):
                if lines[start + 1].startswith("+++ "):
                    found = start
                    break
        if found is None:
            text = "\n".join(lines[index:])
            if text:
                pieces.append(("text", text))
            break
        if found > index:
            text = "\n".join(lines[index:found])
            if text:
                pieces.append(("text", text + "\n"))
        end = found
        while end < len(lines):
            line = lines[end]
            if end > found and _DIFF_FILE_HEADER.match(line):
                break
            if end > found and line and not line.startswith(
                (" ", "+", "-", "@@", "\\", "index ", "new file mode", "deleted file mode",
                 "similarity index", "rename from", "rename to", "Binary files")
            ):
                # Prose after a diff run (an assistant explanation, a bullet)
                # ends the diff; a unified diff line always starts with a
                # marker, metadata keyword, or a hunk header.
                break
            end += 1
        diff = parse_unified_diff("\n".join(lines[found:end]))
        if diff is not None:
            pieces.append(("diff", diff))
        else:
            pieces.append(("text", "\n".join(lines[found:end])))
        index = end
    return pieces


# A Rich welcome panel is chrome, not a turn. These three markers together are
# the panel welcome_panel() draws; a real reply does not carry all of them.
_WELCOME_MARKERS = (
    "Conductor Super-agent TUI",
    "╭",
    "signal → triage → dispatch",
)
_MISSING = object()


def _is_prerendered_welcome(text: str) -> bool:
    body = str(text or "")
    return all(marker in body for marker in _WELCOME_MARKERS)


class _MutableBlock:
    """Cache generation for a block whose rendered content can change.

    ``version`` stays 0 through construction (dataclass init assigns fields
    before ``__post_init__``). Later assignments of a different value increment
    it, so a renderer can key its cache on the number. In-place list edits call
    ``self.version += 1`` themselves.
    """

    def __setattr__(self, name: str, value: object) -> None:
        if name != "version" and self.__dict__.get("_version_live"):
            previous = self.__dict__.get(name, _MISSING)
            current = self.__dict__.get("version")
            if previous != value and isinstance(current, int):
                self.__dict__["version"] = current + 1
        object.__setattr__(self, name, value)

    def __post_init__(self) -> None:
        self.__dict__["_version_live"] = True


def _steps_from_json(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _flatten_rendered(rendered: object) -> list[str]:
    """One display line per row. The source text is not appended again."""
    if isinstance(rendered, str):
        shown = plain(rendered).removesuffix("\n")
        return shown.split("\n") if shown else []
    if not isinstance(rendered, list):
        shown = plain(rendered).removesuffix("\n")
        return shown.split("\n") if shown else []
    rows: list[str] = []
    for item in rendered:
        if isinstance(item, str):
            piece = item
        elif isinstance(item, tuple) and item:
            piece = str(item[-1])
        elif isinstance(item, list):
            parts: list[str] = []
            for cell in item:
                if isinstance(cell, tuple) and cell:
                    parts.append(str(cell[-1]))
                else:
                    parts.append(str(cell))
            piece = "".join(parts)
        else:
            piece = str(item)
        shown = plain(piece)
        rows.extend(shown.split("\n") if shown else [""])
    while rows and rows[-1] == "":
        rows.pop()
    return rows


def _call_render_plain(render: object, source: str) -> object | None:
    """``render_plain(text)``, with a width when that parameter exists."""
    calls = (
        lambda: render(source, width=100),  # type: ignore[operator]
        lambda: render(source),  # type: ignore[operator]
        lambda: render(source, 100),  # type: ignore[operator]
    )
    for call in calls:
        try:
            return call()
        except TypeError:
            continue
        except (ValueError, AttributeError, RuntimeError, OSError):
            return None
    return None


def _completed_card_rows(text: str) -> list[str]:
    """Plain rows for a completed result. The body is rendered once.

    ``tui_render`` is imported here so this module still loads if that file is
    not written yet. A missing renderer keeps the stripped text; it is not
    printed a second time beside the renderer's rows.
    """
    raw = str(text or "")
    render: object | None = None
    try:
        from ptc_cli.superagent.tui_render import render_plain, strip_protocol_trailers
    except (ImportError, SyntaxError):
        from .text import strip_protocol_trailers
    else:
        render = render_plain
    cleaned = strip_protocol_trailers(raw)
    if not isinstance(cleaned, str):
        cleaned = "" if cleaned is None else str(cleaned)
    if render is not None:
        produced = _call_render_plain(render, cleaned)
        if produced is not None:
            rows = _flatten_rendered(produced)
            if rows or not cleaned.strip():
                return rows
    return _flatten_rendered(cleaned)


def _result_lines(block: EndBlock, summary: str, *, formatted: bool) -> list[str]:
    """Lines for the result text. A non-empty body replaces the summary."""
    status = str(block.status or "").strip()
    terminal = _progress_phase(status) in _TERMINAL_PROGRESS_PHASES
    body = str(block.body or "")
    body_wins = terminal and bool(body.strip())
    # Formatted cards keep the "summary:" label when that is all there is, so a
    # no-body completed card stays addressable in the Supervisor view. Plain
    # rows of a completed card always go through the result renderer.
    if status == "completed" and (body_wins or not formatted):
        source = body if body_wins else summary
        lines = _completed_card_rows(source) if source else []
    elif status == "completed" and summary:
        lines = [f"summary: {plain(summary)}"]
    elif body_wins:
        shown = plain(body)
        lines = shown.split("\n") if shown else []
    elif summary and formatted:
        lines = [f"summary: {plain(summary)}"]
    elif summary:
        lines = [plain(summary)]
    else:
        lines = []
    return lines


def _tool_output_lines(tool: ToolBlock) -> list[tuple[str, str]]:
    """Output rows. Collapsed keeps three lines and a ctrl+o trailer."""
    if tool.expanded:
        limit = MAX_EXPANDED_TOOL_LINES
        prefix = "  "
        trailer = "  … {n} more line(s)"
    else:
        limit = COLLAPSED_TOOL_LINES
        prefix = "╰ "
        trailer = "… {n} more lines (ctrl+o to expand)"
    shown = tool.lines[:limit]
    rows = [("class:tui-tool-output", f"{prefix}{plain(line)}") for line in shown]
    hidden = len(tool.lines) - len(shown)
    if hidden > 0:
        rows.append(("class:tui-meta", trailer.format(n=hidden)))
    return rows


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


@dataclass
class UserBlock:
    text: str
    attachments: list[str] = field(default_factory=list)
    kind: str = "user"

    def render_plain(self) -> list[str]:
        header = "> " + plain(self.text)
        lines = [header]
        lines.extend(f"  attached: {plain(path)}" for path in self.attachments)
        return lines


# Printed under a sealed block. It belongs to the block itself, not to a separate
# notice, so compacting the conversation or trimming old blocks can never leave
# two fragments of different stream epochs separated by blank space alone.
SEALED_BOUNDARY = "⟲ live output restarted"


@dataclass
class AssistantBlock(_MutableBlock):
    text: str = ""
    reasoning: bool = False
    agent: str = ""
    run_id: str = ""
    kind: str = "assistant"
    # A sealed block is a closed, unfinished fragment (the live output was
    # reset after it): later output must start a new block, never join it.
    sealed: bool = False
    # The server's stream epoch the text came from (0 = none sent). Text of
    # different epochs is never joined, in this run or after a session restore.
    epoch: int = 0
    # True for streamed Worker output, so the Supervisor view can hide it
    # without deleting it from the session.
    worker_detail: bool = False
    # Interface chrome (help) is stored so a session can restore it, and it
    # is excluded from the token estimate.
    chrome: bool = False
    version: int = field(default=0, compare=False, repr=False)

    def append(self, chunk: str) -> None:
        self.text += chunk

    def render_plain(self) -> list[str]:
        prefix = "  " if self.reasoning else ""
        rows: list[str] = []
        if self.agent and not self.reasoning:
            rows.append(f"▸ sub-agent {plain(self.agent)}")
        for piece_kind, value in split_text_and_diffs(plain(self.text)):
            if piece_kind == "diff":
                rows.extend(prefix + f"{line.marker:>7} {line.text}" for line in value)
            else:
                rows.extend(prefix + line for line in value.split("\n"))
        if self.sealed:
            rows.append(prefix + SEALED_BOUNDARY)
        return rows


@dataclass
class ActivityBlock:
    at: str
    event_kind: str
    summary: str
    detail: str = ""
    kind: str = "activity"

    def render_plain(self) -> list[str]:
        tail = f" — {plain(self.detail)}" if self.detail else ""
        return [f"• {plain(self.event_kind)} {plain(self.summary)}{tail}"]


@dataclass
class ToolBlock(_MutableBlock):
    """A tool the Worker announced: its name, status, duration and output lines.

    There is no parameter or diff field on purpose. The server projects tool
    names only, and a block restored from an older session file drops what it
    held, so a tool's arguments and file bodies never reach the screen.

    ``expanded`` defaults to collapsed. The preview is three lines; ctrl+o
    (``expand_tools`` / ``collapse_tools`` / ``toggle_last_tool``) still opens
    the rest. A session file that omitted the key stays expanded, which is
    what those older transcripts showed.
    """

    name: str
    status: str = "running"
    started: float = 0.0
    duration: float | None = None
    lines: list[str] = field(default_factory=list)
    expanded: bool = False
    kind: str = "tool"
    worker_detail: bool = True
    version: int = field(default=0, compare=False, repr=False)

    def finish(self, *, failed: bool, now: float) -> None:
        if self.status != "running":
            return
        self.status = "failed" if failed else "done"
        self.duration = max(0.0, now - self.started)

    def add_line(self, line: str) -> None:
        if len(self.lines) < MAX_TOOL_LINES:
            self.lines.append(plain(line))
            # The list is mutated in place, so the field assignment does not bump.
            self.version += 1

    def render_plain(self) -> list[str]:
        status = self.status if self.duration is None else (
            f"{self.status} {format_duration(self.duration)}"
        )
        head = f"⚙ {plain(self.name)} [{plain(status)}]"
        return [head, *(text for _style, text in _tool_output_lines(self))]


@dataclass
class ErrorBlock:
    text: str
    kind: str = "error"

    def render_plain(self) -> list[str]:
        return [f"error: {plain(self.text)}"]


@dataclass
class NoticeBlock:
    text: str
    kind: str = "notice"
    worker_detail: bool = False

    def render_plain(self) -> list[str]:
        return [f"· {plain(self.text)}"]


# Summary is a terminal fact. Question and error are shown for one phase each,
# so a waiting card does not keep a running summary on screen.
_TERMINAL_PROGRESS_PHASES = frozenset({"completed", "failed", "cancelled"})


def _progress_phase(phase: str) -> str:
    return {
        "blocked": "waiting",
        "archived": "cancelled",
    }.get(str(phase or "").strip(), str(phase or "").strip())


def visible_progress_fields(
    phase: str,
    summary: str = "",
    question: str = "",
    error: str = "",
) -> tuple[str, str, str]:
    """Summary, question, and error the Supervisor view may show for ``phase``.

    The stored calibration can be wider than this. ``needs you`` is waiting
    only, ``error`` is failed only, and ``summary`` is terminal only.
    ``blocked`` is waiting and ``archived`` is cancelled, matching the server
    progress projection, so a raw end frame is filtered the same way.
    """
    shown = _progress_phase(phase)
    shown_summary = summary if shown in _TERMINAL_PROGRESS_PHASES else ""
    shown_question = question if shown == "waiting" else ""
    shown_error = error if shown == "failed" else ""
    return shown_summary, shown_question, shown_error


def progress_detail_rows(
    phase: str,
    summary: str = "",
    question: str = "",
    error: str = "",
) -> list[tuple[str, str]]:
    """Visible progress lines, in the same order and wording as an EndBlock."""
    shown_summary, shown_question, shown_error = visible_progress_fields(
        phase, summary, question, error
    )
    rows: list[tuple[str, str]] = []
    if shown_summary:
        rows.append(("class:tui-agent", f"summary: {plain(shown_summary)}"))
    if shown_error:
        rows.append(("class:tui-error", f"error: {plain(shown_error)}"))
    if shown_question:
        rows.append(("class:tui-question", f"needs you: {plain(shown_question)}"))
    return rows


@dataclass
class ProgressBlock(_MutableBlock):
    ticket_id: str
    phase: str = "queued"
    stage: str = "Waiting for a Worker to claim the Ticket"
    started_at: float | None = None
    finished_at: float | None = None
    activity_kind: str = ""
    activity_summary: str = ""
    # One conductor tool step (▸/╰). Not the worker's tool block.
    tool_line: str = ""
    # Calibration text. Rendering goes through visible_progress_fields, so a
    # waiting or running phase can store a summary without showing it.
    summary: str = ""
    question: str = ""
    error: str = ""
    steps: list[object] = field(default_factory=list)
    # Identity belongs to this card. Older transcripts omit the keys.
    ticket_title: str = ""
    worker: str = ""
    branch: str = ""
    ticket_short: str = ""
    kind: str = "progress"
    version: int = field(default=0, compare=False, repr=False)

    def elapsed(self, now: float) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else now
        return max(0.0, end - self.started_at)

    def detail_rows(self) -> list[tuple[str, str]]:
        return progress_detail_rows(self.phase, self.summary, self.question, self.error)

    def render_plain(self) -> list[str]:
        lines = [
            "progress",
            f"phase: {plain(self.phase)}  elapsed: {format_duration(self.elapsed(time.time()))}",
            plain(self.stage),
        ]
        if self.activity_kind:
            lines.append(
                f"activity: {plain(self.activity_kind)} {plain(self.activity_summary)}".rstrip()
            )
        lines.extend(text for _style, text in self.detail_rows())
        return lines


@dataclass
class EndBlock(_MutableBlock):
    status: str
    summary: str = ""
    question: str = ""
    error: str = ""
    body: str = ""
    kind: str = "end"
    # Additive: older transcripts omit these and load as empty.
    steps: list[object] = field(default_factory=list)
    expanded: bool = False
    # The task's own title and worker. ``title`` stays the print-path chrome.
    ticket_title: str = ""
    worker: str = ""
    branch: str = ""
    ticket_short: str = ""
    ticket_id: str = ""
    # A restored card whose short id is not unique. Updates must not guess.
    unbound: bool = False
    version: int = field(default=0, compare=False, repr=False)

    @property
    def title(self) -> str:
        if self.status == "completed":
            return "✓ result"
        if self.status == "failed":
            return "✗ result"
        if self.status == "waiting":
            return "? result"
        return "• result"

    def render_plain(self) -> list[str]:
        lines = [f"status: {plain(self.status)}"]
        summary, question, error = visible_progress_fields(
            self.status, self.summary, self.question, self.error
        )
        lines.extend(_result_lines(self, summary, formatted=False))
        if error:
            lines.append(f"error: {plain(error)}")
        if question:
            lines.append(f"needs you: {plain(question)}")
        return lines


@dataclass
class PermissionBlock(_MutableBlock):
    action: str
    label: str
    detail: str = ""
    decision: str = "pending"
    kind: str = "permission"
    version: int = field(default=0, compare=False, repr=False)

    def render_plain(self) -> list[str]:
        lines = [
            f"permission: {plain(self.label)}",
            f"  action: {plain(self.action)}",
        ]
        if self.detail:
            lines.append(f"  {plain(self.detail)}")
        if self.decision == "pending":
            lines.append("  [a]llow once   [s]ession allow   [d]eny")
        else:
            lines.append(f"  decision: {plain(self.decision)}")
        return lines


@dataclass
class DispatchBlock:
    """A dispatch roster. Only the already-clipped lines are stored."""

    lines: list[str]
    kind: str = "dispatch"

    def render_plain(self) -> list[str]:
        return [plain(line) for line in self.lines]


Block = (
    UserBlock
    | AssistantBlock
    | ActivityBlock
    | ToolBlock
    | ErrorBlock
    | NoticeBlock
    | ProgressBlock
    | EndBlock
    | PermissionBlock
    | DispatchBlock
)


def _epoch_value(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def block_from_json(raw: dict[str, Any]) -> Block | None:
    kind = str(raw.get("kind") or "")
    def _string_list(value: Any) -> list[str]:
        return [str(item) for item in value] if isinstance(value, list) else []

    if kind == "user":
        return UserBlock(
            text=str(raw.get("text") or ""),
            attachments=_string_list(raw.get("attachments")),
        )
    if kind == "assistant":
        text = str(raw.get("text") or "")
        # A saved welcome panel is not assistant text; drop it on restore.
        if _is_prerendered_welcome(text):
            return None
        return AssistantBlock(
            text=text,
            reasoning=bool(raw.get("reasoning")),
            agent=str(raw.get("agent") or ""),
            run_id=str(raw.get("run_id") or ""),
            sealed=bool(raw.get("sealed")),
            epoch=_epoch_value(raw.get("epoch")),
            chrome=bool(raw.get("chrome")),
            worker_detail=bool(raw.get("worker_detail"))
            or bool(
                raw.get("run_id")
                or raw.get("epoch")
                or raw.get("agent")
                or raw.get("reasoning")
            ),
        )
    if kind == "activity":
        return ActivityBlock(
            at=str(raw.get("at") or ""),
            event_kind=str(raw.get("event_kind") or ""),
            summary=str(raw.get("summary") or ""),
            detail=str(raw.get("detail") or ""),
        )
    if kind == "tool":
        # A session file written before tool detail was dropped may still hold
        # "params" and "diff"; they are not read back.
        return ToolBlock(
            name=str(raw.get("name") or ""),
            status=str(raw.get("status") or "done"),
            duration=_finite_or_none(raw.get("duration")),
            lines=_string_list(raw.get("lines")),
            expanded=bool(raw.get("expanded", True)),
            worker_detail=True,
        )
    if kind == "error":
        return ErrorBlock(text=str(raw.get("text") or ""))
    if kind == "notice":
        text = str(raw.get("text") or "")
        legacy_worker_notice = text.startswith("live output restarted") or (
            "worker output was dropped" in text.lower()
        )
        return NoticeBlock(
            text=text,
            worker_detail=legacy_worker_notice or bool(raw.get("worker_detail")),
        )
    if kind == "progress":
        return ProgressBlock(
            ticket_id=str(raw.get("ticket_id") or ""),
            phase=str(raw.get("phase") or "queued"),
            stage=str(raw.get("stage") or ""),
            started_at=_finite_or_none(raw.get("started_at")),
            finished_at=_finite_or_none(raw.get("finished_at")),
            activity_kind=str(raw.get("activity_kind") or ""),
            activity_summary=str(raw.get("activity_summary") or ""),
            tool_line=str(raw.get("tool_line") or ""),
            summary=str(raw.get("summary") or ""),
            question=str(raw.get("question") or ""),
            error=str(raw.get("error") or ""),
            steps=_steps_from_json(raw.get("steps")),
            ticket_title=str(raw.get("ticket_title") or ""),
            worker=str(raw.get("worker") or ""),
            branch=str(raw.get("branch") or ""),
            ticket_short=str(raw.get("ticket_short") or ""),
        )
    if kind == "end":
        return EndBlock(
            status=str(raw.get("status") or ""),
            summary=str(raw.get("summary") or ""),
            question=str(raw.get("question") or ""),
            error=str(raw.get("error") or ""),
            body=str(raw.get("body") or ""),
            steps=_steps_from_json(raw.get("steps")),
            expanded=bool(raw.get("expanded")),
            ticket_title=str(raw.get("ticket_title") or ""),
            worker=str(raw.get("worker") or ""),
            branch=str(raw.get("branch") or ""),
            ticket_short=str(raw.get("ticket_short") or ""),
            ticket_id=str(raw.get("ticket_id") or ""),
            unbound=bool(raw.get("unbound")),
        )
    if kind == "permission":
        return PermissionBlock(
            action=str(raw.get("action") or ""),
            label=str(raw.get("label") or ""),
            detail=str(raw.get("detail") or ""),
            decision=str(raw.get("decision") or "done"),
        )
    if kind == "dispatch":
        return DispatchBlock(lines=_string_list(raw.get("lines")))
    return None


class Conversation:
    """Ordered conversation blocks plus the streaming/in-flight state."""

    def __init__(self, blocks: Iterable[Block] | None = None) -> None:
        self.blocks: list[Block] = [
            block for block in list(blocks or []) if not _is_welcome_block(block)
        ]
        self._run_id = ""

    # -- block helpers -----------------------------------------------------

    def add(self, block: Block) -> Block:
        if _is_welcome_block(block):
            return block
        self.blocks.append(block)
        if len(self.blocks) > MAX_BLOCKS:
            # Drop the oldest activity/notice lines first; keep user/assistant
            # and the final outcome.
            removable = [
                index
                for index, item in enumerate(self.blocks)
                if isinstance(item, (ActivityBlock, NoticeBlock))
            ]
            for index in reversed(removable[: len(self.blocks) - MAX_BLOCKS]):
                del self.blocks[index]
        return block

    def remove(self, block: Block) -> None:
        with_suppress = getattr(self.blocks, "remove", None)
        if with_suppress is None:
            return
        try:
            self.blocks.remove(block)
        except ValueError:
            return

    def tool_blocks(self) -> list[ToolBlock]:
        return [item for item in self.blocks if isinstance(item, ToolBlock)]

    def last_tool(self) -> ToolBlock | None:
        tools = self.tool_blocks()
        return tools[-1] if tools else None

    def toggle_last_tool(self) -> ToolBlock | None:
        tool = self.last_tool()
        if tool is not None:
            tool.expanded = not tool.expanded
        return tool

    def collapse_tools(self) -> int:
        changed = 0
        for tool in self.tool_blocks():
            if tool.expanded:
                tool.expanded = False
                changed += 1
        return changed

    def expand_tools(self) -> int:
        changed = 0
        for tool in self.tool_blocks():
            if not tool.expanded:
                tool.expanded = True
                changed += 1
        return changed

    def token_estimate(self) -> int:
        total = 0
        for block in self.blocks:
            if isinstance(block, UserBlock):
                total += estimate_tokens(block.text)
            elif (
                isinstance(block, AssistantBlock)
                and not block.reasoning
                and not block.chrome
            ):
                total += estimate_tokens(block.text)
            elif isinstance(block, ToolBlock):
                total += estimate_tokens(block.name) + sum(
                    estimate_tokens(line) for line in block.lines
                )
        return total

    def transcript_text(self) -> str:
        return "\n".join(self.plain_lines())

    def clear(self) -> None:
        self.blocks.clear()
        self._run_id = ""

    def compact(self) -> int:
        """Drop activity/notice noise, keeping the decisions and outcomes."""
        keep = [
            block
            for block in self.blocks
            if not isinstance(block, NoticeBlock)
        ]
        # Deduplicate consecutive activity rows that say the same thing.
        deduped: list[Block] = []
        last_key: tuple[str, str, str] | None = None
        for block in keep:
            if isinstance(block, ActivityBlock):
                key = (block.event_kind, block.summary, block.detail)
                if key == last_key:
                    continue
                last_key = key
            else:
                last_key = None
            deduped.append(block)
        removed = len(self.blocks) - len(deduped)
        self.blocks = deduped
        return removed

    # -- event reducer -----------------------------------------------------

    def feed(
        self,
        event: object,
        *,
        now: Callable[[], float] = time.monotonic,
        show_reasoning: bool = True,
        show_worker_details: bool = True,
    ) -> Block | None:
        """Apply one TUI event; returns the block that changed, if any.

        ``show_worker_details=False`` is the Supervisor view: the product
        surfaces the Super-agent's progress and result, not a Worker's token
        stream or tool names. The events are still consumed; only their
        rendering is suppressed.
        """
        if isinstance(event, (OutputReset, Gap)):
            # The display toggle never changes continuity state: a boundary
            # still seals whatever streamed text came before it.
            self.seal_live_output()
            self._finish_running_tools(now=now, failed=False)
            if not show_worker_details:
                return None
        if isinstance(event, Activity):
            return self._end_tools_and_add(
                ActivityBlock(
                    at=event.at,
                    event_kind=event.kind,
                    summary=event.summary,
                    detail=event.detail,
                ),
                now=now,
            )
        if isinstance(event, OutputChunk):
            if event.kind == "reasoning" and not show_reasoning:
                return None
            if event.run_id and self._run_id and event.run_id != self._run_id:
                # A new worker generation: close the previous output line.
                self._run_id = event.run_id
            self._run_id = self._run_id or event.run_id
            latest = self._last_streamed_block()
            if latest is not None and (
                latest.run_id != event.run_id or latest.epoch != event.epoch
            ):
                # Another source or generation with no explicit reset (a
                # rewatch, a restored session, cloud <-> daemon, a new run): the
                # earlier fragment is closed for good.
                self.seal_live_output()
            last = self.blocks[-1] if self.blocks else None
            wants_reasoning = event.kind == "reasoning"
            if (
                isinstance(last, AssistantBlock)
                and not last.sealed
                and last.epoch == event.epoch
                and last.reasoning == wants_reasoning
                and last.agent == event.agent
                and last.run_id == event.run_id
            ):
                last.append(event.text)
                return last
            block = AssistantBlock(
                reasoning=wants_reasoning,
                agent=event.agent,
                run_id=event.run_id,
                epoch=event.epoch,
                worker_detail=True,
            )
            block.append(event.text)
            self.blocks.append(block)
            return block
        if isinstance(event, OutputReset):
            self.seal_live_output()
            self._finish_running_tools(now=now, failed=False)
            return self.add(
                NoticeBlock(
                    text=output_reset_notice(event.reason),
                    worker_detail=True,
                )
            )
        if isinstance(event, ToolCall):
            changed: Block | None = None
            self._finish_running_tools(now=now, failed=False)
            for name in event.names:
                block = ToolBlock(name=name, started=now(), worker_detail=True)
                self.blocks.append(block)
                changed = block
            return changed
        if isinstance(event, OutputError):
            self._finish_running_tools(now=now, failed=True)
            return self.add(ErrorBlock(text=event.text))
        if isinstance(event, Gap):
            # Dropped output is a discontinuity like a reset: the text on either
            # side must never read as one string, and the notice that stood
            # between them does not survive /compact or trimming.
            self.seal_live_output()
            self._finish_running_tools(now=now, failed=False)
            return self.add(NoticeBlock(text=event.text, worker_detail=True))
        if isinstance(event, (Status, TimedOut)):
            self._finish_running_tools(now=now, failed=False)
            return None
        if isinstance(event, StreamNotice):
            return self.add(NoticeBlock(text=event.text))
        if isinstance(event, End):
            self._finish_running_tools(now=now, failed=event.status == "failed")
            return self.add(
                EndBlock(
                    status=event.status,
                    summary=event.summary,
                    question=event.question,
                    error=event.error,
                )
            )
        return None

    def _last_streamed_block(self) -> AssistantBlock | None:
        """The newest assistant block that carries streamed text (a run or epoch)."""
        for block in reversed(self.blocks):
            if isinstance(block, AssistantBlock) and (block.run_id or block.epoch):
                return block
        return None

    def seal_live_output(self) -> None:
        """Close the newest streamed fragment, wherever it sits.

        A new watch (another Ticket, or the same one again) opens a fresh stream
        that replays from its start: whatever this conversation already shows is
        a closed fragment of an earlier attachment and must not be joined by it.
        """
        self._run_id = ""
        for block in reversed(self.blocks):
            if isinstance(block, AssistantBlock):
                # Only streamed text can be joined by a replay; a panel the TUI
                # itself wrote (welcome, help) carries neither a run nor an epoch.
                if block.run_id or block.epoch:
                    block.sealed = True
                return

    def _end_tools_and_add(self, block: Block, *, now: Callable[[], float]) -> Block:
        # An activity row means the previous tool step is over.
        self._finish_running_tools(now=now, failed=False)
        return self.add(block)

    def _finish_running_tools(
        self, *, now: Callable[[], float], failed: bool
    ) -> None:
        moment = now()
        for tool in self.tool_blocks():
            if tool.status == "running":
                tool.finish(failed=failed, now=moment)

    # -- persistence -------------------------------------------------------

    def to_json(self) -> list[dict[str, Any]]:
        return [
            _block_to_json(block)
            for block in self.blocks
            if not _is_welcome_block(block)
        ]

    @classmethod
    def from_json(cls, rows: Iterable[dict[str, Any]] | None) -> "Conversation":
        blocks: list[Block] = []
        for raw in rows or []:
            if not isinstance(raw, dict):
                continue
            try:
                block = block_from_json(raw)
            except (TypeError, ValueError, KeyError):
                continue
            if block is not None:
                blocks.append(block)
        bind_restored_cards(blocks)
        return cls(blocks)

    # -- rendering ---------------------------------------------------------

    def plain_lines(self) -> list[str]:
        lines: list[str] = []
        for block in self.blocks:
            if _is_welcome_block(block):
                continue
            rendered = block.render_plain()
            if lines and rendered:
                lines.append("")
            lines.extend(rendered)
        return lines

    def formatted_lines(
        self,
        *,
        max_content_width: int = 100,
        show_meta: bool = True,
        show_worker_details: bool = True,
        now: Callable[[], float] = time.time,
    ) -> list[tuple[str, str]]:
        """prompt_toolkit formatted-text lines: ``(style, text)`` per row.

        The contract is one logical line per tuple: callers (the scroll marker)
        index rows against rendered lines, so a block with embedded newlines is
        split here rather than becoming a single over-tall row.
        """
        visible = [
            block
            for block in self.blocks
            if not _is_welcome_block(block)
            and (
                show_worker_details
                or not (
                    isinstance(block, ActivityBlock)
                    or (
                        isinstance(block, (AssistantBlock, ToolBlock, NoticeBlock))
                        and block.worker_detail
                    )
                )
            )
        ]
        rows: list[tuple[str, str]] = []
        for block_index, block in enumerate(visible):
            # A quiet blank row between blocks gives the transcript its
            # breathing room; consecutive streamed chunks stay in one block.
            if rows and block_index:
                rows.append(("", ""))
            if isinstance(block, UserBlock):
                rows.append(("class:tui-user", f"> {plain(block.text)}"))
                for path in block.attachments:
                    rows.append(("class:tui-meta", f"  attached: {plain(path)}"))
            elif isinstance(block, AssistantBlock):
                style = "class:tui-reasoning" if block.reasoning else "class:tui-agent"
                prefix = "  " if block.reasoning else ""
                if block.agent and not block.reasoning:
                    rows.append(("class:tui-subagent", f"▸ sub-agent {plain(block.agent)}"))
                for piece_kind, value in split_text_and_diffs(plain(block.text)):
                    if piece_kind == "diff":
                        for line in value:
                            diff_style = {
                                "add": "class:tui-diff-add",
                                "del": "class:tui-diff-del",
                                "hunk": "class:tui-diff-hunk",
                                "header": "class:tui-diff-header",
                            }.get(line.marker, "class:tui-diff-context")
                            rows.append((diff_style, prefix + line.text))
                    else:
                        for line in value.split("\n"):
                            stripped = line.lstrip()
                            if stripped.startswith("#"):
                                line_style = "class:tui-heading"
                            elif stripped.startswith(("- ", "* ")):
                                line_style = "class:tui-bullet"
                            else:
                                line_style = style
                            rows.append((line_style, prefix + line))
                if block.sealed:
                    rows.append(("class:tui-meta", prefix + SEALED_BOUNDARY))
            elif isinstance(block, ActivityBlock):
                tail = f" — {plain(block.detail)}" if block.detail else ""
                rows.append(
                    (
                        "class:tui-meta",
                        f"• {plain(block.event_kind)} {plain(block.summary)}{tail}",
                    )
                )
            elif isinstance(block, ToolBlock):
                rows.extend(_tool_formatted(block, show_meta=show_meta))
            elif isinstance(block, ErrorBlock):
                rows.append(("class:tui-error", f"error: {plain(block.text)}"))
            elif isinstance(block, NoticeBlock):
                rows.append(("class:tui-meta", f"· {plain(block.text)}"))
            elif isinstance(block, ProgressBlock):
                rows.append(("class:tui-heading", "progress"))
                rows.append(
                    (
                        "class:tui-meta",
                        f"phase: {plain(block.phase)}  "
                        f"elapsed: {format_duration(block.elapsed(now()))}",
                    )
                )
                if block.stage:
                    rows.append(("class:tui-agent", plain(block.stage)))
                if block.activity_kind:
                    rows.append(
                        (
                            "class:tui-meta",
                            f"activity: {plain(block.activity_kind)} "
                            f"{plain(block.activity_summary)}",
                        )
                    )
                rows.extend(block.detail_rows())
            elif isinstance(block, EndBlock):
                rows.append(("class:tui-result-title", block.title))
                rows.append(("class:tui-end", f"status: {plain(block.status)}"))
                summary, question, error = visible_progress_fields(
                    block.status, block.summary, block.question, block.error
                )
                rows.extend(
                    ("class:tui-agent", line)
                    for line in _result_lines(block, summary, formatted=True)
                )
                if error:
                    rows.append(("class:tui-error", f"error: {plain(error)}"))
                if question:
                    rows.append(("class:tui-question", f"needs you: {plain(question)}"))
            elif isinstance(block, DispatchBlock):
                for line in block.lines:
                    rows.append(("class:tui-meta", plain(line)))
            elif isinstance(block, PermissionBlock):
                rows.append(("class:tui-warn", f"permission: {plain(block.label)}"))
                if block.decision == "pending":
                    rows.append(
                        (
                            "class:tui-warn",
                            "  [a] allow once   [s] session allow   [d] deny",
                        )
                    )
                else:
                    rows.append(("class:tui-meta", f"  decision: {plain(block.decision)}"))
        return _split_multiline_rows(rows)


def _finite_or_none(value: Any) -> float | None:
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    if number is None or not math.isfinite(number) or number < 0:
        return None
    return number


def _split_multiline_rows(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """One tuple per logical line; the TUI indexes rows as rendered lines."""
    split: list[tuple[str, str]] = []
    for style, text in rows:
        for part in str(text).split("\n"):
            split.append((style, part))
    return split


def _tool_formatted(tool: ToolBlock, *, show_meta: bool) -> list[tuple[str, str]]:
    del show_meta  # output follows expanded/collapsed; the head is always drawn
    status = tool.status if tool.duration is None else (
        f"{tool.status} {format_duration(tool.duration)}"
    )
    rows: list[tuple[str, str]] = [
        ("class:tui-tool", f"⚙ {plain(tool.name)} [{plain(status)}]")
    ]
    rows.extend(_tool_output_lines(tool))
    return rows


def _is_welcome_block(block: Block) -> bool:
    return isinstance(block, AssistantBlock) and _is_prerendered_welcome(block.text)


def _put_card_identity(row: dict[str, Any], block: Any) -> None:
    """Write card identity only when set, so an old transcript stays the same shape."""
    for key in ("ticket_id", "ticket_title", "worker", "branch", "ticket_short"):
        value = str(getattr(block, key, "") or "")
        if value:
            row[key] = value
    if getattr(block, "unbound", False):
        row["unbound"] = True


def bind_restored_cards(blocks: list[Block]) -> None:
    """Bind a legacy card by short id only when that short id is unique.

    A card with no full id, or a short id shared by two Tickets, is ``unbound``
    and later updates skip it.
    """
    full_ids: list[str] = []
    for block in blocks:
        ticket_id = str(getattr(block, "ticket_id", "") or "")
        if ticket_id and ticket_id not in full_ids:
            full_ids.append(ticket_id)
    by_short: dict[str, list[str]] = {}
    for ticket_id in full_ids:
        by_short.setdefault(ticket_id[:8], []).append(ticket_id)
    pending = [
        block
        for block in blocks
        if isinstance(block, EndBlock) and not block.ticket_id and block.ticket_short
    ]
    shared: dict[str, int] = {}
    for block in pending:
        shared[block.ticket_short] = shared.get(block.ticket_short, 0) + 1
    for block in blocks:
        if not isinstance(block, EndBlock) or block.ticket_id:
            continue
        short = block.ticket_short
        matches = by_short.get(short, []) if short else []
        if short and len(matches) == 1 and shared.get(short, 0) == 1:
            block.ticket_id = matches[0]
            block.unbound = False
        else:
            block.unbound = True


def _block_to_json(block: Block) -> dict[str, Any]:
    if isinstance(block, UserBlock):
        return {
            "kind": "user",
            "text": block.text,
            "attachments": list(block.attachments),
        }
    if isinstance(block, AssistantBlock):
        row: dict[str, Any] = {
            "kind": "assistant",
            "text": block.text,
            "reasoning": block.reasoning,
            "agent": block.agent,
            "run_id": block.run_id,
        }
        if block.worker_detail:
            row["worker_detail"] = True
        if block.sealed:
            # Additive key: older readers ignore it; absent means unsealed.
            row["sealed"] = True
        if block.epoch:
            row["epoch"] = block.epoch
        if block.chrome:
            row["chrome"] = True
        return row
    if isinstance(block, ActivityBlock):
        return {
            "kind": "activity",
            "at": block.at,
            "event_kind": block.event_kind,
            "summary": block.summary,
            "detail": block.detail,
        }
    if isinstance(block, ToolBlock):
        return {
            "kind": "tool",
            "name": block.name,
            "status": block.status,
            "duration": block.duration,
            "lines": list(block.lines),
            "expanded": block.expanded,
        }
    if isinstance(block, ErrorBlock):
        return {"kind": "error", "text": block.text}
    if isinstance(block, NoticeBlock):
        row: dict[str, Any] = {"kind": "notice", "text": block.text}
        if block.worker_detail:
            row["worker_detail"] = True
        return row
    if isinstance(block, ProgressBlock):
        row = {
            "kind": "progress",
            "ticket_id": block.ticket_id,
            "phase": block.phase,
            "stage": block.stage,
            "started_at": block.started_at,
            "finished_at": block.finished_at,
            "activity_kind": block.activity_kind,
            "activity_summary": block.activity_summary,
        }
        if block.tool_line:
            row["tool_line"] = block.tool_line
        # Additive keys: older sessions omit them and restore as empty.
        if block.summary:
            row["summary"] = block.summary
        if block.question:
            row["question"] = block.question
        if block.error:
            row["error"] = block.error
        steps = block.steps if isinstance(block.steps, list) else []
        if steps:
            row["steps"] = list(steps)
        _put_card_identity(row, block)
        return row
    if isinstance(block, EndBlock):
        row = {
            "kind": "end",
            "status": block.status,
            "summary": block.summary,
            "question": block.question,
            "error": block.error,
        }
        # Additive keys. Absent on older transcripts; empty values stay absent
        # so a block that never gained a body still round-trips as it did.
        if block.body:
            row["body"] = block.body
        steps = block.steps if isinstance(block.steps, list) else []
        if steps:
            row["steps"] = list(steps)
        if block.expanded:
            row["expanded"] = True
        _put_card_identity(row, block)
        return row
    if isinstance(block, DispatchBlock):
        return {"kind": "dispatch", "lines": list(block.lines)}
    if isinstance(block, PermissionBlock):
        return {
            "kind": "permission",
            "action": block.action,
            "label": block.label,
            "detail": block.detail,
            "decision": block.decision,
        }
    return {"kind": "notice", "text": ""}


def collect_mentions(body: str) -> list[str]:
    """The ``@path`` mentions in one input line, in order, unique."""
    seen: list[str] = []
    for match in _MENTION_RE.finditer(str(body or "")):
        value = match.group(1).rstrip(".,:;)]}")
        if value and value not in seen:
            seen.append(value)
    return seen


__all__ = [
    "ActivityBlock",
    "AssistantBlock",
    "Block",
    "Conversation",
    "DiffLine",
    "EndBlock",
    "ErrorBlock",
    "MAX_BLOCKS",
    "SEALED_BOUNDARY",
    "MAX_EXPANDED_TOOL_LINES",
    "NoticeBlock",
    "PermissionBlock",
    "ProgressBlock",
    "DispatchBlock",
    "ToolBlock",
    "UserBlock",
    "block_from_json",
    "collect_mentions",
    "estimate_tokens",
    "format_duration",
    "parse_unified_diff",
    "split_text_and_diffs",
]
