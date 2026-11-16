"""Permission gates for the local actions a Super-agent TUI can run.

The TUI itself is a thin client: the Worker runs in the daemon/cloud, and its
tool calls are approved by the deployment's own policy. What the TUI *can* do
locally is run a ``conductor`` subcommand (``/review run``, ``/merge``) or a
shell command from the composer (``!...``). Those change files on this
machine, so they go through a confirmation unless the operator allowed the
action for this session.

Decisions map onto the three affordances Claude Code-style TUIs expose:
**Allow once**, **Allow for this session**, **Deny**. The manager is plain
state so the controller can render the prompt and tests can resolve it without
a terminal. :func:`render_permission_prompt` draws the terminal-native card
and the status-bar takeover of ``tui/SPEC.md`` §7.8 so a later mount can use
them without changing the gate logic here.
"""

# ruff: noqa: RUF001, D102, D105, D107
# D1xx: the gate helpers keep their pre-S09 doc surface; only the renderer is new.

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from rich.cells import cell_len

from .text import plain
from .tui_i18n import t
from .tui_theme import status_glyph

# Actions the TUI performs locally; each may mutate the checkout or a worktree.
LOCAL_MUTATING_ACTIONS = frozenset({"local-merge", "local-review", "local-shell"})

# Card inset; hung rows sit two cells further in so a wrap keeps the hang.
INSET = 2
HANG = 4
# SPEC §3.4: a wrapped line never opens with one of these closing marks.
_CJK_BREAKS = frozenset("，。、；：？！")
_CJK_CELL_WIDTH = 2

STRINGS = {
    "en": {
        "mark": "permission: ",
        "action": "action: ",
        "keys": "[a]llow once  [s]ession allow  [d]eny",
        "escape": "esc deny",
        "allow": "a allow",
        "session": "s session",
        "deny": "d deny",
        "open": " (",
        "close": ")",
        "dash": " — ",
    },
    "zh-CN": {
        "mark": "权限：",
        "action": "动作：",
        "keys": "[a]允许一次  [s]本会话允许  [d]拒绝",
        "escape": "esc 拒绝",
        "allow": "a 允许",
        "session": "s 会话",
        "deny": "d 拒绝",
        "open": "（",
        "close": "）",
        "dash": "— ",
    },
}


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """One pending confirmation the composer/status renders."""

    action: str
    label: str
    detail: str = ""

    def render(self) -> str:
        body = f"{self.label} ({self.action})"
        return f"{body} — {self.detail}" if self.detail else body


class PermissionResolver(Protocol):
    """Async prompt the application provides; tests implement it directly."""

    async def __call__(self, request: PermissionRequest) -> str: ...


@dataclass
class PermissionState:
    """What the operator chose for this session."""

    auto_approve: bool = False
    session_allowed: set[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.session_allowed is None:
            self.session_allowed = set()


class PermissionManager:
    """Decide whether a local action needs a confirmation."""

    def __init__(self, *, auto_approve: bool = False) -> None:
        self.state = PermissionState(auto_approve=auto_approve)

    def is_mutating(self, action: str) -> bool:
        return action in LOCAL_MUTATING_ACTIONS

    def needs_confirmation(self, action: str) -> bool:
        if not self.is_mutating(action):
            return False
        if self.state.auto_approve:
            return False
        return action not in self.state.session_allowed

    def allow_session(self, action: str) -> None:
        self.state.session_allowed.add(action)

    def allow_once(self, action: str) -> None:
        """Allow a single run: the caller does not remember the action."""

    def deny(self, action: str) -> None:
        """Explicit denial; no state to keep (the caller raises)."""

    def session_allowed(self, action: str) -> bool:
        return action in self.state.session_allowed


def decision_from_key(key: str) -> str:
    """Map a composer key to a decision: allow / session / deny / pending."""
    value = (key or "").lower()
    if value == "a":
        return "allow"
    if value == "s":
        return "session"
    if value == "d":
        return "deny"
    return "pending"


def _take_cells(text: str, limit: int) -> str:
    """The longest prefix of ``text`` that fits in ``limit`` cells."""
    if limit <= 0:
        return ""
    used = 0
    out: list[str] = []
    for char in text:
        char_width = cell_len(char)
        if used + char_width > limit:
            break
        out.append(char)
        used += char_width
    return "".join(out)


def _pull_closing_run(head: str, rest: str) -> tuple[str, str]:
    """Keep a continuation from opening with CJK closing marks (SPEC §3.4).

    A paired closing run moves down together with the character above it.
    Trailing closing marks already taken onto ``head`` join that same run.
    """
    if not head or not rest or rest[0] not in _CJK_BREAKS:
        return head, rest
    index = 0
    while index < len(rest) and rest[index] in _CJK_BREAKS:
        index += 1
    run = rest[:index]
    body = rest[index:]
    kept = head
    while kept and kept[-1] in _CJK_BREAKS:
        run = kept[-1] + run
        kept = kept[:-1]
    if not kept:
        return head, rest
    return kept[:-1], kept[-1] + run + body


def _wrap_to_width(text: str, width: int) -> list[str]:
    """Wrap one physical line to ``width`` cells per SPEC §3.4.

    English breaks on spaces. A long word or CJK run fills the line it starts
    on (a short head such as ``共 2`` never sits alone above it). A continuation
    never opens with a CJK closing mark — a whole closing run moves down with
    the character above it. Double spaces stay as empty ``split(" ")`` tokens.
    """
    room = max(1, int(width))
    if text == "":
        return [""]
    lines: list[str] = []
    current = ""

    def emit(line: str) -> None:
        stripped = line.rstrip()
        if stripped:
            lines.append(stripped)

    for token in text.split(" "):
        if token == "":
            if current and cell_len(current) + 1 <= room:
                current = f"{current} "
            continue
        remaining = token
        candidate = remaining if not current else f"{current} {remaining}"
        if cell_len(candidate) <= room:
            current = candidate
            continue
        # Fill the current line from a long / CJK word so a short head is not
        # stranded alone above it (SPEC §3.4).
        breakable = cell_len(remaining) > room or (
            bool(remaining) and cell_len(remaining[0]) == _CJK_CELL_WIDTH
        )
        if current and breakable:
            left = room - cell_len(current) - 1
            head = _take_cells(remaining, left) if left > 0 else ""
            rest = remaining[len(head) :]
            head, rest = _pull_closing_run(head, rest)
            if head:
                emit(f"{current} {head}")
                current = ""
                remaining = rest
            else:
                emit(current)
                current = ""
        elif current:
            emit(current)
            current = ""
        while cell_len(remaining) > room:
            head = _take_cells(remaining, room) or remaining[0]
            rest = remaining[len(head) :]
            pulled_head, pulled_rest = _pull_closing_run(head, rest)
            if pulled_head and (pulled_head != head or pulled_rest != rest):
                emit(pulled_head)
                remaining = pulled_rest
                continue
            emit(head)
            remaining = rest
        current = remaining
    emit(current)
    return lines or [""]


def _hung(text: object, width: int, *, indent: int) -> list[str]:
    """Wrap prose under a fixed indent; each physical line keeps the hang."""
    body = plain(text).rstrip()
    if not body:
        return []
    prefix = " " * max(0, indent)
    room = max(1, width - cell_len(prefix))
    rows: list[str] = []
    for paragraph in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if paragraph == "":
            rows.append(prefix)
            continue
        rows.extend(prefix + line for line in _wrap_to_width(paragraph, room))
    return rows


def _clip_label(text: str, width: int) -> str:
    """Shorten ``text`` to ``width`` cells on a word boundary, marking the cut with …."""
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    if width == 1:
        return "…"
    limit = width - 1
    kept: list[str] = []
    for piece in text.split(" "):
        candidate = piece if not kept else f"{' '.join(kept)} {piece}"
        if cell_len(candidate) > limit:
            break
        kept.append(piece)
    if kept:
        return f"{' '.join(kept)}…"
    head = _take_cells(text, limit)
    return f"{head}…"


def _flat_words(value: object) -> str:
    """Collapse every run of whitespace so a status label stays one physical row."""
    return " ".join(plain(value).split())


def _status_label(request: PermissionRequest, locale: str) -> str:
    """The status-bar label before the three keys take the rest of the row."""
    glyph = status_glyph("needs-you")
    label = _flat_words(request.label)
    action = _flat_words(request.action)
    detail = _flat_words(request.detail)
    open_mark = t(STRINGS, "open", locale)
    close_mark = t(STRINGS, "close", locale)
    body = f"{glyph} {label}{open_mark}{action}{close_mark}"
    if not detail:
        return body
    return f"{body}{t(STRINGS, 'dash', locale)}{detail}"


def _status_row(request: PermissionRequest, width: int, locale: str) -> str:
    """Status-bar takeover: the label gives way; the three keys never do."""
    legend = " · ".join(
        (
            t(STRINGS, "allow", locale),
            t(STRINGS, "session", locale),
            t(STRINGS, "deny", locale),
        )
    )
    room = max(0, width - cell_len(legend) - 3)
    return f" {_clip_label(_status_label(request, locale), room)}  {legend}"


def render_permission_prompt(
    request: PermissionRequest,
    width: int,
    locale: str,
) -> list[str]:
    """The §7.8 permission card followed by the status-bar takeover row.

    Card rows: mark (needs-you glyph), action, detail, the ``[a]``/``[s]``/``[d]``
    key line (stacking under a 4-space hang at 40 columns) and an Escape = deny
    hint. The last row is the status bar: its label is cut with ``…`` so the
    ``a allow · s session · d deny`` keys always fit.
    """
    columns = max(1, int(width))
    glyph = status_glyph("needs-you")
    label = plain(request.label).strip()
    action = plain(request.action).strip()
    detail = plain(request.detail).strip()
    mark = f"{glyph} {t(STRINGS, 'mark', locale)}{label}".rstrip()
    rows = _hung(mark, columns, indent=INSET)
    rows.extend(_hung(f"{t(STRINGS, 'action', locale)}{action}", columns, indent=HANG))
    if detail:
        rows.extend(_hung(detail, columns, indent=HANG))
    rows.extend(_hung(t(STRINGS, "keys", locale), columns, indent=HANG))
    rows.extend(_hung(t(STRINGS, "escape", locale), columns, indent=HANG))
    rows.append(_status_row(request, columns, locale))
    return rows


__all__ = [
    "LOCAL_MUTATING_ACTIONS",
    "STRINGS",
    "PermissionManager",
    "PermissionRequest",
    "PermissionResolver",
    "PermissionState",
    "decision_from_key",
    "render_permission_prompt",
]
