"""One timeline for the CLI and the TUI.

This module ships with the core CLI. Display text goes through
``terminal_text``, which imports only the standard library.

The eight positions are G0 SPEC, D1 DAG, implementing, G2, primary review,
secondary review, G5 and G6. G1, G3 and G4 are not positions. ``machine`` is
read only for ``stage`` and ``return_stage``.
"""

from __future__ import annotations

import os
from typing import Any

from .terminal_text import plain

_AXIS = (
    ("g0", "G0 SPEC", ("planning", "spec_review"), ("g0",)),
    ("d1", "D1 DAG", ("dag_ready",), ("d1",)),
    ("implementing", "实现中", ("assigned", "implementing"), ()),
    ("g2", "G2 聚焦测试", ("focused_tests", "g2"), ("g2",)),
    ("primary", "主审", ("primary_review",), ("primary_review",)),
    ("secondary", "副审", ("secondary_review",), ("secondary_review",)),
    ("g5", "G5 集成", ("integration_checks", "g5"), ("g5",)),
    ("g6", "G6 人工", ("awaiting_human", "approval", "g6"), ("g6",)),
)
# English labels for a non-zh locale; the zh text above stays for zh_*.
_LABEL_EN = {
    "g0": "G0 SPEC",
    "d1": "D1 DAG",
    "implementing": "implementing",
    "g2": "G2 focused tests",
    "primary": "primary review",
    "secondary": "secondary review",
    "g5": "G5 integration",
    "g6": "G6 human",
}
_INDEX = {slot_id: index for index, (slot_id, *_rest) in enumerate(_AXIS)}
_PRIMARY_REASONS = {"review_blocked", "fix_resubmitted", "primary_review_passed"}
_STOPS = {"cancelled", "blocked", "replanning"}
_GATE_STOP = {"g0": "g0", "d1": "d1", "g2": "g2", "g5": "g5", "g6": "g6"}
_FAIL_RESULTS = frozenset({"fail", "block"})
_STATUS_TEXT = {
    "unreached": "未到达",
    "pending-approval": "待审批",
    "current": "当前",
    "not-applicable": "不适用",
    "fixing": "修复中",
    "merged": "已合并",
    "cancelled": "已取消",
    "blocked": "阻塞",
    "replanning": "重新规划",
}
_STATUS_TEXT_EN = {
    "unreached": "unreached",
    "pending-approval": "pending approval",
    "current": "current",
    "not-applicable": "n/a",
    "fixing": "fixing",
    "merged": "merged",
    "cancelled": "cancelled",
    "blocked": "blocked",
    "replanning": "replanning",
}
_CURRENT_TEXT = {"zh": "当前", "en": "current"}


def display_lang(environ: Any = None) -> str:
    """``zh`` or ``en`` from the POSIX locale (``LC_ALL``, ``LC_MESSAGES``, ``LANG``).

    The first non-empty variable wins, as in POSIX; ``zh*`` is Chinese and
    anything else (including unset or ``C``) is English.
    """
    env = os.environ if environ is None else environ
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = str(env.get(name) or "").strip()
        if value:
            return "zh" if value.lower().startswith("zh") else "en"
    return "en"


def _resolve_lang(lang: str | None) -> str:
    return lang if lang in ("zh", "en") else display_lang()


def _clip(value: Any, limit: int) -> str:
    text = plain(value).split("\n", 1)[0].strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _token(value: Any) -> str:
    return _clip(value, 80).lower()


def _slot_id(value: Any) -> str | None:
    key = _token(value)
    if not key:
        return None
    for slot_id, _label, phases, gates in _AXIS:
        if key in phases or key in gates:
            return slot_id
    return None


def _event_slot(event: dict[str, Any]) -> str | None:
    if _token(event.get("gate")):
        return _slot_id(event.get("gate"))
    reason = _token(event.get("reason"))
    stages = {_token(event.get("from_stage")), _token(event.get("to_stage"))}
    if reason.startswith("secondary_") or (
        "secondary_review" in stages and reason in {"review_blocked", "fix_resubmitted"}
    ):
        return "secondary"
    if reason in _PRIMARY_REASONS:
        return "primary"
    for stage in (event.get("to_stage"), event.get("from_stage")):
        found = _slot_id(stage)
        if found:
            return found
    return None


def _history_slot(item: dict[str, Any]) -> str | None:
    found = _slot_id(item.get("gate"))
    if found:
        return found
    reason = _token(item.get("reason"))
    if reason.startswith("secondary_"):
        return "secondary"
    if reason in _PRIMARY_REASONS:
        return "primary"
    return None


def _stage(record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    machine = record.get("machine") if isinstance(record.get("machine"), dict) else {}
    stage = _token(machine.get("stage")) or _token(record.get("phase"))
    return stage, machine


def _event_stop(event: dict[str, Any]) -> str | None:
    found = _slot_id(event.get("from_stage"))
    if found:
        return found
    gate = _token(event.get("gate"))
    if gate in _GATE_STOP:
        return _GATE_STOP[gate]
    if not gate:
        origin = _token(event.get("from_stage"))
        if origin == "primary_review":
            return "primary"
        if origin == "secondary_review":
            return "secondary"
    return None


def _failed_check_slot(name: str, item: Any) -> str | None:
    if name == "checks" or not isinstance(item, dict):
        return None
    if _token(item.get("result")) not in _FAIL_RESULTS:
        return None
    return _slot_id(name)


def _last_failed_gate(record: dict[str, Any]) -> str | None:
    """The last axis slot whose gate check result is ``fail`` or ``block``.

    Line records keep those checks on ``gates``. A nested ``gates["checks"]``
    object is read the same way. Pass, skip, and pending do not place the stop.
    """
    gates = record.get("gates")
    if not isinstance(gates, dict):
        return None
    sources: list[dict[str, Any]] = [gates]
    nested = gates.get("checks")
    if isinstance(nested, dict):
        sources.append(nested)
    found: str | None = None
    for slot_id, _label, _phases, _gate_names in _AXIS:
        for source in sources:
            for name, item in source.items():
                if _failed_check_slot(str(name), item) == slot_id:
                    found = slot_id
    return found


def _stop_position(record: dict[str, Any], stage: str) -> str:
    """Where a cancelled, blocked or replanning line stopped. ``return_stage`` is ignored.

    A locating event wins. Otherwise the last failed gate check wins, then the
    last history result, then the implementing slot.
    """
    events = record.get("events")
    if isinstance(events, list):
        for event in reversed(events):
            if not isinstance(event, dict) or _token(event.get("to_stage")) != stage:
                continue
            found = _event_stop(event)
            if found:
                return found
    failed = _last_failed_gate(record)
    if failed:
        return failed
    history = record.get("history")
    if isinstance(history, list):
        for item in reversed(history):
            if not isinstance(item, dict) or not _token(item.get("result")):
                continue
            found = _history_slot(item)
            if found:
                return found
    return "implementing"


def current_position(record: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Return ``(slot id or None, marker or None)``."""
    if not isinstance(record, dict):
        return None, None
    stage, machine = _stage(record)
    if stage == "merged":
        return None, "merged"
    if stage in _STOPS:
        return _stop_position(record, stage), stage
    if stage == "fixing":
        back = _token(record.get("return_stage") or machine.get("return_stage"))
        return _slot_id(back) or "primary", "fixing"
    return _slot_id(stage), None


def timeline_of(
    record: dict[str, Any] | None, *, lang: str | None = None
) -> list[dict[str, Any]]:
    """Eight slots. Status values are stable codes; labels follow the locale."""
    zh = _resolve_lang(lang) == "zh"
    current_id, marker = current_position(record if isinstance(record, dict) else None)
    stage = ""
    if isinstance(record, dict):
        stage, _machine = _stage(record)
    notes: dict[str, list[str]] = {slot_id: [] for slot_id, *_rest in _AXIS}
    seen: dict[str, set[str]] = {slot_id: set() for slot_id, *_rest in _AXIS}
    event_result: dict[str, str] = {}
    history_result: dict[str, str] = {}

    def remember(slot: str | None, value: Any) -> None:
        if not slot:
            return
        text = _clip(value, 120)
        if not text or text in seen[slot]:
            return
        seen[slot].add(text)
        notes[slot].append(text)

    if isinstance(record, dict):
        history = record.get("history")
        if isinstance(history, list):
            for item in history:
                if not isinstance(item, dict):
                    continue
                slot = _history_slot(item)
                remember(slot, item.get("reason"))
                outcome = _clip(item.get("result"), 40)
                if slot and outcome:
                    history_result[slot] = outcome
        events = record.get("events")
        if isinstance(events, list):
            for event in events:
                if not isinstance(event, dict):
                    continue
                slot = _event_slot(event)
                remember(slot, event.get("reason"))
                outcome = _clip(event.get("result"), 40)
                if slot and outcome:
                    event_result[slot] = outcome

    risk = record.get("risk_bearing") if isinstance(record, dict) else None
    gates = record.get("gates") if isinstance(record, dict) and isinstance(record.get("gates"), dict) else {}
    current_index = _INDEX.get(current_id or "", -1)
    slots: list[dict[str, Any]] = []
    for index, (slot_id, label, _phases, _gates) in enumerate(_AXIS):
        check = None
        for name, item in gates.items():
            if isinstance(item, dict) and _slot_id(name) == slot_id:
                check = item
                break
        result = _clip(check.get("result"), 40) if check else ""
        if not result:
            result = event_result.get(slot_id) or history_result.get(slot_id, "")
        detail = _clip(check.get("reason"), 120) if check else ""
        trail = [note for note in notes[slot_id] if note != detail]
        here = slot_id == current_id
        if marker == "merged":
            status = "merged"
        elif marker in _STOPS and current_index >= 0 and index > current_index:
            status = "unreached"
            detail = ""
            trail = []
            here = False
        elif here and marker == "fixing":
            status = "fixing"
        elif here and marker in _STOPS:
            status = marker
        elif (
            here
            and slot_id == "g6"
            and stage in {"awaiting_human", "approval", "g6"}
            and (not result or result.lower() == "pending")
            and (
                not detail
                or detail.lower() == "awaiting_human"
                or stage in {"awaiting_human", "approval"}
            )
        ):
            status = "pending-approval"
        elif result:
            status = result
        elif here:
            status = "current"
        elif slot_id == "secondary" and risk is False and not detail and not trail:
            status = "not-applicable"
        elif detail or trail:
            status = "—"
        else:
            status = "unreached"
        slots.append(
            {
                "id": slot_id,
                "label": label if zh else _LABEL_EN[slot_id],
                "status": status,
                "detail": detail,
                "notes": trail,
                "current": here,
            }
        )
    return slots


def render_timeline_lines(
    record: dict[str, Any] | None, *, lang: str | None = None
) -> list[str]:
    """The CLI and TUI lines for one gate record, in the locale's language."""
    chosen = _resolve_lang(lang)
    table = _STATUS_TEXT if chosen == "zh" else _STATUS_TEXT_EN
    lines: list[str] = []
    for slot in timeline_of(record, lang=chosen):
        status = table.get(slot["status"], slot["status"])
        bits = [f"    {slot['label']}", status]
        if slot["detail"]:
            bits.append(slot["detail"])
        if slot["notes"]:
            bits.append(" → ".join(slot["notes"]))
        if slot["current"] and slot["status"] != "current":
            bits.append(_CURRENT_TEXT[chosen])
        lines.append("  ".join(bits))
    return lines
