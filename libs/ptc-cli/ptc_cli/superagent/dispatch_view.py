"""Dispatch roster for the TUI.

``render_dispatch_lines`` is the CLI roster the shipped screen still prints.
``render_dispatch_view`` is the terminal-native transcript of ``tui/SPEC.md``
section 7.3. Copy is read through :func:`tui_i18n.t` so a missing zh-CN key
fails in tests instead of shipping half-translated.
"""

from __future__ import annotations

from typing import Any

from conductor_cli.dispatch_timeline import render_timeline_lines
from rich.cells import cell_len

from .text import plain
from .tui_i18n import normalize_locale, t
from .tui_theme import status_glyph, status_word


def _clip(value: object, limit: int) -> str:
    text = plain(value).split("\n", 1)[0].strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    raw = summary.get("lines")
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _line_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("name") or "")


def _depends_on(row: dict[str, Any]) -> list[str]:
    raw = row.get("depends_on")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item or "").strip()]


def dag_layers(summary: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Keep ``←`` edges on the CLI roster, including a missing id or a cycle."""
    order: list[str] = []
    deps: dict[str, list[str]] = {}
    for row in _rows(summary):
        ident = _line_id(row)
        if not ident or ident in deps:
            continue
        order.append(ident)
        deps[ident] = _depends_on(row)
    known = set(order)
    warnings: list[str] = []
    for ident in order:
        missing = [item for item in deps[ident] if item not in known]
        if missing:
            warnings.append(f"⚠ missing dep {ident} ← {', '.join(missing)}")
    remaining = list(order)
    layers: list[list[str]] = []
    while remaining:
        ready = [
            ident
            for ident in remaining
            if all(item not in remaining for item in deps[ident] if item in known)
        ]
        if not ready:
            warnings.append("⚠ cycle " + ", ".join(remaining))
            layers.append(list(remaining))
            break
        layers.append(ready)
        remaining = [ident for ident in remaining if ident not in set(ready)]
    rendered: list[str] = []
    for index, layer in enumerate(layers):
        labels: list[str] = []
        for ident in layer:
            present = [item for item in deps[ident] if item in known]
            labels.append(f"{ident} ← {', '.join(present)}" if present else ident)
        rendered.append(f"L{index}: {', '.join(labels)}")
    if not rendered:
        rendered.append("L0: —")
    return rendered, warnings


def _gate_record(summary: dict[str, Any], line_id: str) -> dict[str, Any] | None:
    gates = summary.get("gates")
    if not isinstance(gates, dict) or not gates:
        return None
    bucket = gates.get("lines")
    if not isinstance(bucket, dict):
        return None
    row = bucket.get(line_id)
    return row if isinstance(row, dict) else None


def _verdict_label(value: object) -> str:
    text = _clip(value, 40)
    if not text:
        return "—"
    lowered = text.lower()
    if lowered == "merge":
        return "MERGE"
    if lowered == "block":
        return "BLOCK"
    return text


def _verdict(record: dict[str, Any] | None) -> str:
    if not isinstance(record, dict):
        return "—"
    inner = record.get("gates")
    if isinstance(inner, dict):
        primary = inner.get("primary_review")
        if isinstance(primary, dict) and primary.get("verdict"):
            return _verdict_label(primary.get("verdict"))
    history = record.get("history")
    if isinstance(history, list):
        for item in reversed(history):
            if isinstance(item, dict) and item.get("verdict"):
                return _verdict_label(item.get("verdict"))
            if isinstance(item, dict) and item.get("result"):
                return _verdict_label(item.get("result"))
    return "—"


def _token_issued(summary: dict[str, Any]) -> bool:
    if summary.get("approval_token"):
        return True
    gates = summary.get("gates")
    if isinstance(gates, dict):
        approval = gates.get("approval")
        if isinstance(approval, dict) and approval.get("token_present"):
            return True
    return False



def render_dispatch_lines(summary: dict[str, Any], *, listing: bool = False) -> list[str]:
    """Same roster the CLI prints. The block stores these lines, not the payload."""
    if listing:
        percent = summary.get("progress_pct")
        percent_text = (
            f"{percent}%"
            if isinstance(percent, (int, float)) and not isinstance(percent, bool)
            else "—"
        )
        return [
            "  ".join(
                [
                    _clip(summary.get("id"), 80) or "—",
                    _clip(summary.get("state"), 40) or "—",
                    percent_text,
                    _clip(summary.get("goal"), 200),
                ]
            ).rstrip()
        ]
    lines: list[str] = []
    if summary.get("fake"):
        lines.append("(local fake record)")
    ident = _clip(summary.get("id"), 80) or "—"
    state = _clip(summary.get("state"), 40) or "—"
    percent = summary.get("progress_pct")
    percent_text = (
        f"{percent}%"
        if isinstance(percent, (int, float)) and not isinstance(percent, bool)
        else "—"
    )
    branch = _clip(summary.get("integrate_branch"), 120) or "—"
    lines.append(f"{ident}  {state}  {percent_text}  {branch}")
    goal = _clip(summary.get("goal"), 200)
    if goal:
        lines.append(goal)
    message = _clip(summary.get("message"), 200)
    if message:
        lines.append(message)
    if state == "APPROVAL":
        lines.append(f"approval pending — conductor dispatch approve {ident}")
    if _token_issued(summary):
        lines.append("token issued")
    lines.append("")
    lines.append("roster")
    rows = _rows(summary)
    if not rows:
        lines.append("—")
    for row in rows:
        line_id = _line_id(row) or "—"
        record = _gate_record(summary, line_id)
        gate = _clip(record.get("phase"), 40) if record else "—"
        fix = record.get("fix_round") if record and record.get("fix_round") is not None else "—"
        depends = _depends_on(row)
        lines.append(
            "  ".join(
                [
                    line_id,
                    _clip(row.get("status"), 40) or "—",
                    _clip(row.get("assigned_model"), 40) or "—",
                    gate or "—",
                    str(fix),
                    _verdict(record),
                    ", ".join(depends) if depends else "—",
                ]
            )
        )
        if record:
            lines.extend(render_timeline_lines(record))
    lines.append("")
    lines.append("dag")
    layers, warnings = dag_layers(summary)
    lines.extend(layers)
    lines.extend(warnings)
    unassigned: list[str] = []
    for row in rows:
        if row.get("assigned_model"):
            continue
        reason = row.get("failure_reason")
        if not reason:
            blockers = row.get("blockers")
            if isinstance(blockers, list) and blockers:
                reason = blockers[0]
        unassigned.append(f"{_line_id(row) or '—'}  {_clip(reason, 120) or '—'}")
    if unassigned:
        lines.append("")
        lines.append("unassigned")
        lines.extend(unassigned)
    return lines


# Section 7.3. Widths are terminal cells (a CJK character counts as two). At
# 40 and below the roster stacks; above that a column that does not fit is
# dropped, lowest priority first. ``render_dispatch_lines`` does not read this.
_INSET = 2
_GAP = 2
_STACK_AT = 40
_DETAIL_INDENT = 2
_DASH = "\u2014"
_ELLIPSIS = "\u2026"
_META_SEP = " \u00b7 "
_WIDE_CELL = 2
_CLOSERS = frozenset("\u3001\u3002\uff0c\uff1b\uff1a\uff1f\uff01")
# (key, width, priority). A higher priority is kept longer. line/glyph/status
# are pinned: the state word is never the column that gets dropped.
_ROSTER: tuple[tuple[str, int, int], ...] = (
    ("line", 4, 100),
    ("glyph", 1, 100),
    ("status", 9, 95),
    ("model", 8, 70),
    ("gate", 8, 60),
    ("fix", 3, 50),
    ("verdict", 7, 80),
    ("depends", 7, 40),
)
_PINNED = frozenset({"line", "glyph", "status"})
_IDENTITY = ("line", "glyph", "status")
_META = ("model", "gate", "fix", "verdict", "depends")
_WIDTH = {key: width for key, width, _priority in _ROSTER}
_STATUS_IDS = {
    "completed": "completed",
    "complete": "completed",
    "passed": "completed",
    "pass": "completed",
    "merged": "completed",
    "done": "completed",
    "running": "running",
    "implementing": "running",
    "in_progress": "running",
    "blocked": "blocked",
    "failed": "failed",
    "fail": "failed",
    "error": "failed",
    "queued": "queued",
    "pending": "queued",
    # LineStatus.ASSIGNED is waiting to launch, not one of the seven Ticket states.
    "assigned": "queued",
    # LineStatus.REVIEWING is a reviewer at work.
    "reviewing": "running",
    "needs-you": "needs-you",
    "needs_you": "needs-you",
    "awaiting_human": "needs-you",
    "archived": "archived",
    "cancelled": "archived",
    "canceled": "archived",
}
_STATE_KEYS = {
    "submitted": "state.SUBMITTED",
    "planning": "state.PLANNING",
    "assigned": "state.ASSIGNED",
    "launching": "state.LAUNCHING",
    "monitoring": "state.MONITORING",
    "review": "state.REVIEW",
    "approval": "state.APPROVAL",
    "awaiting_human": "state.APPROVAL",
    "merge": "state.MERGE",
    "complete": "state.COMPLETE",
    "abort": "state.ABORT",
    "error": "state.ERROR",
}
# The gate column is 8 cells (SPEC 4.2). Backend phases are longer than that,
# so each one maps to a short word instead of the raw token.
_GATE_KEYS = {
    "planning": "gate.planning",
    "spec_review": "gate.spec_review",
    "reviewed": "gate.reviewed",
    "build": "gate.build",
    "building": "gate.build",
    "assigned": "gate.assigned",
    "implementing": "gate.build",
    "awaiting_human": "gate.pending",
    "pending": "gate.pending",
    "dag_ready": "gate.dag_ready",
    "focused_tests": "gate.focused_tests",
    "primary_review": "gate.primary_review",
    "fixing": "gate.fixing",
    "secondary_review": "gate.secondary_review",
    "integration_checks": "gate.integration_checks",
    "awaiting_adjudication": "gate.awaiting_adjudication",
    "merged": "gate.merged",
    "replanning": "gate.replanning",
    "blocked": "gate.blocked",
    "cancelled": "gate.cancelled",
    "passed": "gate.passed",
    "failed": "gate.failed",
    "running": "gate.running",
    "merging": "gate.merging",
}
# gates.approval is filled in at dispatch start and kept after COMPLETE.
_WAITING_STATES = frozenset({"approval", "awaiting_human"})
_REASON_KEYS = {
    "pool": "unassigned.pool",
    "worker_pool_exhausted": "unassigned.pool",
    "worker pool exhausted": "unassigned.pool",
}

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "title": "dispatch",
        "roster": "roster",
        "dag": "dag",
        "unassigned": "unassigned",
        "col.line": "line",
        "col.status": "status",
        "col.model": "model",
        "col.gate": "gate",
        "col.fix": "fix",
        "col.verdict": "verdict",
        "col.depends": "depends",
        "state.SUBMITTED": "SUBMITTED",
        "state.PLANNING": "PLANNING",
        "state.ASSIGNED": "ASSIGNED",
        "state.LAUNCHING": "LAUNCHING",
        "state.MONITORING": "MONITORING",
        "state.REVIEW": "REVIEW",
        "state.APPROVAL": "APPROVAL",
        "state.MERGE": "MERGE",
        "state.COMPLETE": "COMPLETE",
        "state.ABORT": "ABORT",
        "state.ERROR": "ERROR",
        "waiting": "waiting on a human for {gate}",
        "gate.planning": "plan",
        "gate.spec_review": "spec",
        "gate.reviewed": "reviewed",
        "gate.build": "build",
        "gate.assigned": "assigned",
        "gate.pending": "pending",
        "gate.dag_ready": "ready",
        "gate.focused_tests": "tests",
        "gate.primary_review": "review 1",
        "gate.fixing": "fix",
        "gate.secondary_review": "review 2",
        "gate.integration_checks": "checks",
        "gate.awaiting_adjudication": "decision",
        "gate.merged": "merged",
        "gate.replanning": "replan",
        "gate.blocked": "blocked",
        "gate.cancelled": "canceled",
        "gate.passed": "passed",
        "gate.failed": "failed",
        "gate.running": "running",
        "gate.merging": "merging",
        "gate.none": _DASH,
        "verdict.MERGE": "MERGE",
        "verdict.BLOCK": "BLOCK",
        "verdict.none": _DASH,
        "empty": _DASH,
        "unassigned.pool": "worker pool exhausted",
    },
    "zh-CN": {
        "title": "派发",
        "roster": "名册",
        "dag": "依赖图",
        "unassigned": "未分配",
        "col.line": "线",
        "col.status": "状态",
        "col.model": "模型",
        "col.gate": "闸门",
        "col.fix": "修复",
        "col.verdict": "结论",
        "col.depends": "依赖",
        "state.SUBMITTED": "已提交",
        "state.PLANNING": "规划中",
        "state.ASSIGNED": "已分配",
        "state.LAUNCHING": "启动中",
        "state.MONITORING": "监控中",
        "state.REVIEW": "评审中",
        "state.APPROVAL": "待审批",
        "state.MERGE": "合并中",
        "state.COMPLETE": "已完成",
        "state.ABORT": "已中止",
        "state.ERROR": "错误",
        "waiting": "{gate} 正在等待人工决定",
        "gate.planning": "规划中",
        "gate.spec_review": "规格评审",
        "gate.reviewed": "已评审",
        "gate.build": "构建中",
        "gate.assigned": "已分配",
        "gate.pending": "待定",
        "gate.dag_ready": "就绪",
        "gate.focused_tests": "测试中",
        "gate.primary_review": "主审",
        "gate.fixing": "修复中",
        "gate.secondary_review": "副审",
        "gate.integration_checks": "检查中",
        "gate.awaiting_adjudication": "待裁决",
        "gate.merged": "已合并",
        "gate.replanning": "重规划中",
        "gate.blocked": "已阻塞",
        "gate.cancelled": "已取消",
        "gate.passed": "已通过",
        "gate.failed": "失败",
        "gate.running": "运行中",
        "gate.merging": "合并中",
        "gate.none": _DASH,
        "verdict.MERGE": "合并",
        "verdict.BLOCK": "阻断",
        "verdict.none": _DASH,
        "empty": _DASH,
        "unassigned.pool": "执行者池已耗尽",
    },
}


def _say(key: str, locale: str) -> str:
    return t(STRINGS, key, locale)


def _dash(locale: str) -> str:
    return _say("empty", locale)


def _shown(value: object) -> str:
    # ``plain`` drops a numeric zero (``str(value or "")``). A fix round of 0
    # is a real roster cell, so numbers are stringified before that filter.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return plain(str(value))
    return plain(value)


def _one_line(value: object) -> str:
    return _shown(value).split("\n", 1)[0].strip()


def _text(value: object) -> str:
    """Keep every line. Dispatch goals are stored with their newlines."""
    return _shown(value).strip()


def _take(text: str, limit: int) -> str:
    """The longest prefix of ``text`` that fits in ``limit`` cells."""
    if limit <= 0:
        return ""
    out: list[str] = []
    used = 0
    for char in text:
        width = cell_len(char)
        if used + width > limit:
            break
        out.append(char)
        used += width
    return "".join(out)


def _clip_cells(text: str, width: int) -> str:
    """Shorten ``text`` to ``width`` cells, marking a cut with an ellipsis."""
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    if width == 1:
        return _ELLIPSIS
    return _take(text, width - cell_len(_ELLIPSIS)) + _ELLIPSIS


def _pad(text: str, width: int) -> str:
    gap = width - cell_len(text)
    if gap <= 0:
        return text
    return text + (" " * gap)


def _hard_wrap(text: str, room: int) -> list[str]:
    """Keep every character, filling each line before the next one starts."""
    room = max(1, room)
    if cell_len(text) <= room:
        return [text]
    lines: list[str] = []
    rest = text
    while rest:
        if cell_len(rest) <= room:
            lines.append(rest)
            break
        head = _take(rest, room) or rest[0]
        rest = rest[len(head) :]
        if rest and rest[0] in _CLOSERS and len(head) > 1:
            head, rest = head[:-1], head[-1] + rest
        lines.append(head)
    return lines or [""]


def _wrap_words(text: str, room: int) -> list[str]:
    """Wrap prose on spaces. A CJK run, which has none, fills the line."""
    room = max(1, room)
    if cell_len(text) <= room:
        return [text]
    if " " not in text:
        return _hard_wrap(text, room)
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        piece = word
        candidate = piece if not current else f"{current} {piece}"
        if cell_len(candidate) <= room:
            current = candidate
            continue
        wide = bool(piece) and cell_len(piece[0]) == _WIDE_CELL
        if current and (cell_len(piece) > room or wide):
            left = room - cell_len(current) - 1
            head = _take(piece, left) if left > 0 else ""
            rest = piece[len(head) :]
            if head and rest and rest[0] in _CLOSERS and len(head) > 1:
                head, rest = head[:-1], head[-1] + rest
            if head:
                lines.append(f"{current} {head}")
                piece = rest
                current = ""
        if current:
            lines.append(current)
            current = ""
        while cell_len(piece) > room:
            head = _take(piece, room) or piece[0]
            rest = piece[len(head) :]
            if rest and rest[0] in _CLOSERS and len(head) > 1:
                lines.append(head[:-1])
                piece = head[-1] + rest
                continue
            lines.append(head)
            piece = rest
        current = piece
    if current:
        lines.append(current)
    return lines or [""]


def _prose(text: str, width: int) -> list[str]:
    """Wrap prose to ``width`` cells, keeping a stored newline as a row break."""
    room = max(1, width - _INSET)
    blocks = text.splitlines() or [""]
    rows: list[str] = []
    for block in blocks:
        if not block and len(blocks) > 1:
            continue
        rows.extend((" " * _INSET + line).rstrip() for line in _wrap_words(block, room))
    return rows


def _compose(keys: list[str], texts: dict[str, str]) -> str:
    parts: list[str] = []
    for key in keys:
        raw = texts.get(key, "")
        column = _WIDTH[key]
        if key in _PINNED and cell_len(raw) > column:
            parts.append(raw)
            continue
        parts.append(_pad(_clip_cells(raw, column), column))
    return (" " * _INSET + (" " * _GAP).join(parts)).rstrip()


def _fit_keys(texts: dict[str, str], width: int) -> list[str]:
    """Drop the lowest-priority unpinned column until the row fits."""
    kept = [key for key, _width, _priority in _ROSTER]
    while True:
        if cell_len(_compose(kept, texts)) <= width:
            return kept
        victim = min(
            (key for key in kept if key not in _PINNED),
            default="",
            key=lambda key: next(priority for name, _width, priority in _ROSTER if name == key),
        )
        if not victim:
            return kept
        kept.remove(victim)


def _stack_parts(parts: list[str], room: int) -> list[str]:
    """Join meta cells with `` · `` and wrap. Nothing is dropped."""
    lines: list[str] = []
    current = ""
    for text in parts:
        if not current:
            if cell_len(text) <= room:
                current = text
                continue
            chunks = _hard_wrap(text, room)
            lines.extend(chunks[:-1])
            current = chunks[-1]
            continue
        addition = f"{_META_SEP}{text}"
        if cell_len(current) + cell_len(addition) <= room:
            current += addition
            continue
        lines.append(current)
        if cell_len(text) <= room:
            current = text
            continue
        chunks = _hard_wrap(text, room)
        lines.extend(chunks[:-1])
        current = chunks[-1]
    if current:
        lines.append(current)
    return lines


def _detail_lines(parts: list[str], width: int) -> list[str]:
    indent = _INSET + _DETAIL_INDENT
    room = max(1, width - indent)
    return [(" " * indent + line).rstrip() for line in _stack_parts(parts, room)]


def _roster_lines(texts: dict[str, str], width: int, *, meta: bool) -> list[str]:
    if width <= _STACK_AT:
        rows = [_compose(list(_IDENTITY), texts)]
        if meta:
            rows.extend(_detail_lines([texts[key] for key in _META], width))
        return rows
    return [_compose(_fit_keys(texts, width), texts)]


def _header_texts(locale: str) -> dict[str, str]:
    return {
        "line": _say("col.line", locale),
        "glyph": "",
        "status": _say("col.status", locale),
        "model": _say("col.model", locale),
        "gate": _say("col.gate", locale),
        "fix": _say("col.fix", locale),
        "verdict": _say("col.verdict", locale),
        "depends": _say("col.depends", locale),
    }


def _localized(value: object, locale: str) -> str:
    if isinstance(value, dict):
        picked = value.get(locale) or value.get("en") or ""
        return _text(picked)
    return _text(value)


def _goal(payload: dict[str, Any], locale: str) -> str:
    for key in ("goal", "subject", "title"):
        if key not in payload:
            continue
        text = _localized(payload.get(key), locale)
        if text:
            return text
    return ""


def _state_label(payload: dict[str, Any], locale: str) -> str:
    raw = _one_line(payload.get("state") or payload.get("phase"))
    key = _STATE_KEYS.get(raw.lower())
    if key:
        return _say(key, locale)
    return raw or _dash(locale)


def _percent(value: object, locale: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _dash(locale)
    return f"{value:g}%"


def _header_facts(payload: dict[str, Any], locale: str) -> str:
    ident = _one_line(payload.get("id")) or _dash(locale)
    state = _state_label(payload, locale)
    percent = _percent(payload.get("progress_pct"), locale)
    branch = _one_line(payload.get("integrate_branch") or payload.get("branch")) or _dash(locale)
    return f"{ident}  {state}  {percent}  {branch}"


def _human_gate(payload: dict[str, Any]) -> str:
    """The gate a person must answer. The API stores it on ``gates.approval``."""
    for key in ("human_gate", "waiting_gate"):
        text = _one_line(payload.get(key))
        if text:
            return text
    gates = payload.get("gates")
    if isinstance(gates, dict):
        approval = gates.get("approval")
        if isinstance(approval, dict):
            return _one_line(approval.get("gate"))
    return ""


def _dispatch_awaits_human(payload: dict[str, Any]) -> bool:
    """True only while this dispatch is the human gate, not merely carrying its record."""
    state = _one_line(payload.get("state") or payload.get("phase")).lower()
    if state in _WAITING_STATES:
        return True
    # A named in-flight or finished state keeps approval metadata. That record
    # is not itself a wait. With no state, a line still in awaiting_human is.
    if state:
        return False
    return _awaiting_human(payload)


def _awaiting_human(payload: dict[str, Any]) -> bool:
    for key in ("phase", "state"):
        if _one_line(payload.get(key)).lower() == "awaiting_human":
            return True
    gates = payload.get("gates")
    if not isinstance(gates, dict):
        return False
    lines = gates.get("lines")
    if not isinstance(lines, dict):
        return False
    return any(isinstance(record, dict) and _one_line(record.get("phase")).lower() == "awaiting_human" for record in lines.values())


def _fit_command(command: str, width: int) -> list[str]:
    """Place a typed command in ``width`` cells without cutting its id.

    A dispatch id is 32 hex characters. ``approve`` plus that id is 40 cells,
    so the usual two-cell inset is kept only when the command still fits.
    """
    indented = " " * _INSET + command
    if cell_len(indented) <= width:
        return [indented.rstrip()]
    if cell_len(command) <= width:
        return [command]
    return _hard_wrap(command, max(1, width))


def _decision_lines(ident: str, width: int) -> list[str]:
    """The commands the operator types. They stay literal in both languages."""
    approve = f"approve {ident}"
    reject = f"reject {ident}"
    combined = " " * _INSET + f"{approve}  {reject}"
    if cell_len(combined) <= width:
        return [combined.rstrip()]
    rows: list[str] = []
    for command in (approve, reject):
        rows.extend(_fit_command(command, width))
    return rows


def _status_cells(raw: object, locale: str) -> tuple[str, str]:
    text = _one_line(raw)
    status_id = _STATUS_IDS.get(text.lower())
    if status_id is None and text:
        try:
            return status_glyph(text), status_word(text, locale)
        except KeyError:
            return "", text
    if status_id is None:
        return "", _dash(locale)
    return status_glyph(status_id), status_word(status_id, locale)


def _gate_word(raw: object, locale: str) -> str:
    text = _one_line(raw)
    if not text or text in {_DASH, "-"}:
        return _say("gate.none", locale)
    key = _GATE_KEYS.get(text.lower())
    if key:
        return _say(key, locale)
    return text


def _verdict_cell(raw: object, locale: str) -> str:
    label = _verdict_label(raw)
    if label == "MERGE":
        return f"{status_glyph('completed')} {_say('verdict.MERGE', locale)}"
    if label == "BLOCK":
        return f"{status_glyph('failed')} {_say('verdict.BLOCK', locale)}"
    return _say("verdict.none", locale)


def _line_gate(summary: dict[str, Any], row: dict[str, Any]) -> object:
    if row.get("gate") not in (None, ""):
        return row.get("gate")
    record = _gate_record(summary, _line_id(row))
    if record and record.get("phase"):
        return record.get("phase")
    return ""


def _line_fix(summary: dict[str, Any], row: dict[str, Any], locale: str) -> str:
    if "fix_round" in row and row.get("fix_round") is not None:
        return _one_line(row.get("fix_round"))
    if "fix" in row and row.get("fix") is not None:
        return _one_line(row.get("fix"))
    record = _gate_record(summary, _line_id(row))
    if record and record.get("fix_round") is not None:
        return _one_line(record.get("fix_round"))
    return _dash(locale)


def _line_verdict(summary: dict[str, Any], row: dict[str, Any]) -> object:
    if "verdict" in row and row.get("verdict") is not None:
        return row.get("verdict")
    return _verdict(_gate_record(summary, _line_id(row)))


def _provider_model(row: dict[str, Any], locale: str) -> str:
    provider = _one_line(row.get("provider") or row.get("assigned_provider") or row.get("worker_provider"))
    model = _one_line(row.get("model") or row.get("assigned_model") or row.get("worker_model"))
    if provider and model and provider != model:
        return f"{provider}/{model}"
    return model or provider or _dash(locale)


def _depends_cell(row: dict[str, Any], locale: str) -> str:
    deps = _depends_on(row)
    if not deps:
        raw = row.get("depends")
        if isinstance(raw, str):
            deps = [item.strip() for item in raw.split(",") if item.strip()]
        elif isinstance(raw, list):
            deps = [str(item) for item in raw if str(item or "").strip()]
    if not deps or deps == [_DASH]:
        return _dash(locale)
    return ", ".join(deps)


def _line_texts(summary: dict[str, Any], row: dict[str, Any], locale: str) -> dict[str, str]:
    glyph, word = _status_cells(row.get("status"), locale)
    return {
        "line": _line_id(row) or _dash(locale),
        "glyph": glyph,
        "status": word,
        "model": _provider_model(row, locale),
        "gate": _gate_word(_line_gate(summary, row), locale),
        "fix": _line_fix(summary, row, locale),
        "verdict": _verdict_cell(_line_verdict(summary, row), locale),
        "depends": _depends_cell(row, locale),
    }


def _reason(value: object, locale: str) -> str:
    if isinstance(value, dict):
        return _localized(value, locale) or _dash(locale)
    text = _one_line(value)
    key = _REASON_KEYS.get(text)
    if key:
        return _say(key, locale)
    return text or _dash(locale)


def _unassigned_lines(payload: dict[str, Any], rows: list[dict[str, Any]], locale: str) -> list[str]:
    raw = payload.get("unassigned")
    rendered: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                ident = _one_line(item.get("id") or item.get("line")) or _dash(locale)
                rendered.append(f"{ident}  {_reason(item.get('reason'), locale)}")
            else:
                rendered.append(_one_line(item) or _dash(locale))
        return rendered
    for row in rows:
        if _provider_model(row, locale) != _dash(locale):
            continue
        reason = row.get("failure_reason")
        if not reason:
            blockers = row.get("blockers")
            if isinstance(blockers, list) and blockers:
                reason = blockers[0]
        rendered.append(f"{_line_id(row) or _dash(locale)}  {_reason(reason, locale)}")
    return rendered


def _explicit_layers(payload: dict[str, Any]) -> list[list[str]] | None:
    """Layers the payload already drew. The design transcript's DAG is not the depends column."""
    raw = payload.get("layers")
    if not isinstance(raw, list) or not raw:
        return None
    layers: list[list[str]] = []
    for item in raw:
        if not isinstance(item, list):
            return None
        layers.append([_one_line(part) for part in item if _one_line(part)])
    return layers


def _view_dag(rows: list[dict[str, Any]], locale: str) -> list[str]:
    """Layer ids only. The CLI roster still prints the ``←`` form."""
    order: list[str] = []
    deps: dict[str, list[str]] = {}
    for row in rows:
        ident = _line_id(row)
        if not ident or ident in deps:
            continue
        order.append(ident)
        deps[ident] = _depends_on(row)
    if not order:
        return [f"L0: {_dash(locale)}"]
    known = set(order)
    remaining = list(order)
    layers: list[list[str]] = []
    while remaining:
        ready = [ident for ident in remaining if all(item not in remaining for item in deps[ident] if item in known)]
        if not ready:
            layers.append(list(remaining))
            break
        layers.append(ready)
        ready_ids = set(ready)
        remaining = [ident for ident in remaining if ident not in ready_ids]
    return [f"L{index}: {', '.join(layer)}" for index, layer in enumerate(layers)]


def render_dispatch_view(payload: dict[str, Any], width: int, locale: str) -> list[str]:
    """The ``/dispatch`` transcript of ``tui/SPEC.md`` section 7.3.

    Cells are measured with :func:`rich.cells.cell_len`. At ``width`` <= 40 the
    roster stacks each line's model, gate, fix round, verdict and deps onto a
    detail row instead of dropping them. ``awaiting_human`` adds one decision
    row whose commands are the literal ``approve <id>`` and ``reject <id>``.
    """
    summary = payload if isinstance(payload, dict) else {}
    resolved = normalize_locale(locale)
    columns = width if isinstance(width, int) and not isinstance(width, bool) and width > 0 else 1
    rows = _rows(summary)
    lines = [
        *_prose(_say("title", resolved), columns),
        *_prose(_header_facts(summary, resolved), columns),
    ]
    goal = _goal(summary, resolved)
    if goal:
        lines.extend(_prose(goal, columns))
    if _dispatch_awaits_human(summary):
        gate = _human_gate(summary)
        if gate:
            lines.extend(_prose(_say("waiting", resolved).replace("{gate}", gate), columns))
    if _dispatch_awaits_human(summary):
        ident = _one_line(summary.get("id")) or _dash(resolved)
        lines.extend(_decision_lines(ident, columns))
    lines.extend(_prose(_say("roster", resolved), columns))
    if rows:
        lines.extend(_roster_lines(_header_texts(resolved), columns, meta=False))
        for row in rows:
            lines.extend(_roster_lines(_line_texts(summary, row, resolved), columns, meta=True))
    else:
        lines.extend(_prose(_dash(resolved), columns))
    lines.extend(_prose(_say("dag", resolved), columns))
    drawn = _explicit_layers(summary)
    dag = (
        [f"L{index}: {', '.join(layer) if layer else _dash(resolved)}" for index, layer in enumerate(drawn)]
        if drawn is not None
        else _view_dag(rows, resolved)
    )
    for layer in dag:
        lines.extend(_prose(layer, columns))
    unassigned = _unassigned_lines(summary, rows, resolved)
    if unassigned:
        lines.extend(_prose(_say("unassigned", resolved), columns))
        for item in unassigned:
            lines.extend(_prose(item, columns))
    return lines
