"""Append the N73 assignment record onto a line (PRD 4.5).

The entry lives at ``gates.lines[line_id]["assignment"]``, keyed by ``run_id``.
A reassign or a fix-round relaunch appends another entry. Each entry keeps
``seq`` so the oldest can be dropped after a JSON object loses key order.
A line holds at most 16 entries. The first entry (lowest ``seq``) is never the
one dropped: it is the first author launch, which the §6.2 pass rate and the
feasible-first-try rate are measured on, so the oldest of the others goes.

``append_assignment`` returns a new dict. It does not mutate ``gates``, the
choice mappings, the feasibility attempts, or the input bundle. Every stored
value is plain JSON (``str``, numbers, ``bool``, ``None``, lists, dicts), so
the in-memory snapshot equals the one reloaded from the database.

``choice`` and ``selector_choice`` accept a mapping, a ``LineChoice``, or any
object with those attributes. ``cli_effort`` is not a ``LineChoice`` field;
it is resolved from the PRD 4.2 table (``effort.resolve_effort``), the single
source of CLI tokens, and a caller's ``cli_effort`` that disagrees with the
table is refused. A choice that names a model but no effort (today's selector)
is recorded at ``default``. ``n/a`` is stored as ``n/a``; ``None`` means the
provider has a flag and today's command is used, or the model/effort is not
in the table.

``feasibility`` accepts ``FeasibilityResult`` values, their ``to_dict()``
form (``status``), or the older ``result`` / ``ok`` mappings. Each attempt is
stored as ``{"attempt", "result", "reason_code"}``.

``fallback_reason`` is free text, or an ``ErrorResult``, stored as its kind
(``call_error``, ``timeout``, ``bad_format``) so a report can count ERROR
apart from an infeasible choice. :data:`FALLBACK_INFEASIBLE_TWICE` names the
other fallback.

``inputs_digest`` is a sha256 of the inputs plus the numeric success-rate and
headroom readings. Task text stays in the hash preimage only. The free-text
fields that may quote the task (``reason``, ``model_id``, ``fallback_reason``
and feasibility reason codes) are stored with that echo replaced, so the
record JSON does not contain the raw task. Closed-vocabulary fields (model,
effort, cli_effort, runtime, confidence) are never rewritten, and task strings
shorter than :data:`MIN_REDACTED_TASK_LENGTH` are not treated as echoes, so a
short task such as ``"high"`` cannot corrupt a model name or an effort.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as _dt
import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import Enum
from math import isfinite
from typing import Any

from pydantic import BaseModel

from src.conductor_cli.rollout_modes import MODES, OFF
from src.tools.auto_dispatch.superagent_assign.effort import resolve_effort
from src.tools.auto_dispatch.superagent_assign.types import (
    ASSIGNMENT_VERSION,
    Effort,
    ErrorResult,
    FeasibilityResult,
    Source,
)

MAX_ASSIGNMENT_ENTRIES = 16
#: Task strings shorter than this are not searched for in free text. A
#: one-word task would otherwise rewrite every field that shares a letter.
MIN_REDACTED_TASK_LENGTH = 8

SOURCE_SUPERAGENT = Source.SUPERAGENT.value
SOURCE_SUPERAGENT_REASK = Source.SUPERAGENT_REASK.value
SOURCE_SELECTOR_FALLBACK = Source.SELECTOR_FALLBACK.value
SOURCE_SELECTOR = Source.SELECTOR.value
SOURCES = frozenset(member.value for member in Source)

#: ``fallback_reason`` when the choice was infeasible on both attempts.
FALLBACK_INFEASIBLE_TWICE = "infeasible_twice"

CHOICE_FIELDS = (
    "model",
    "model_id",
    "effort",
    "cli_effort",
    "runtime",
    "reason",
    "confidence",
)
# Free text that may echo the task. Everything else in a choice is a token
# from a closed vocabulary and is stored as given.
_REDACTED_CHOICE_FIELDS = frozenset({"model_id", "reason"})

_RESULT_OK = "ok"
_RESULT_INFEASIBLE = "infeasible"
_OK_RESULTS = frozenset({_RESULT_OK, "feasible"})
_INFEASIBLE_RESULTS = frozenset({_RESULT_INFEASIBLE})

_RAW_TASK_KEYS = frozenset(
    {
        "task",
        "task_text",
        "goal",
        "prompt",
        "description",
        "body",
        "text",
        "content",
        "title",
        "reason",
        "message",
        "instruction",
        "instructions",
    }
)
_SUCCESS_KEYS = frozenset(
    {
        "success_rate",
        "success_rates",
        "success_by_provider",
        "success_rate_by_provider",
        "success_rate_by_area",
    }
)
# Success-rate tables that are row lists (context.ProviderRate / AreaRate).
# Each keeps its own key so one table does not overwrite another.
_SUCCESS_TABLE_KEYS = frozenset(
    {"success_by_provider", "success_rate_by_provider", "success_rate_by_area"}
)
# String fields a reading row keeps, so a stored count still says whose it
# is. They are roster names and area buckets, still checked for task echoes.
_READING_LABEL_KEYS = frozenset({"model", "provider", "area"})
_HEADROOM_KEYS = frozenset({"headroom", "quota", "quotas"})
# Untrusted task text. ``reason`` is not one of these: it is the rationale we
# keep after the task echo has been removed.
_TASK_ECHO_KEYS = _RAW_TASK_KEYS - {"reason"}
_TASK_REDACTION = "[redacted]"

__all__ = [
    "ASSIGNMENT_VERSION",
    "CHOICE_FIELDS",
    "FALLBACK_INFEASIBLE_TWICE",
    "MAX_ASSIGNMENT_ENTRIES",
    "MIN_REDACTED_TASK_LENGTH",
    "SOURCE_SELECTOR",
    "SOURCE_SELECTOR_FALLBACK",
    "SOURCE_SUPERAGENT",
    "SOURCE_SUPERAGENT_REASK",
    "SOURCES",
    "append_assignment",
    "assignment_entries",
    "assignment_ref",
    "build_assignment_entry",
    "inputs_digest",
]

FeasibilityInput = (
    Sequence[Mapping[str, Any] | FeasibilityResult]
    | Mapping[str, Any]
    | FeasibilityResult
    | None
)


def build_assignment_entry(
    *,
    run_id: str,
    mode: str,
    source: str,
    choice: Mapping[str, Any] | object | None = None,
    selector_choice: Mapping[str, Any] | object | None = None,
    feasibility: FeasibilityInput = None,
    reask: bool = False,
    fallback_reason: str | ErrorResult | None = None,
    inputs: Mapping[str, Any] | object | None = None,
    latency_ms: float | None = None,
    llm_tokens: float | None = None,
) -> dict[str, Any]:
    """One PRD 4.5 record. ``selector_choice`` is always present, even when empty.

    ``seq`` is assigned by :func:`append_assignment` when the entry is placed.
    ``mode`` must be off, shadow or enforce: a record does not guess. ``off``
    never calls the Super-agent, so its source is ``selector`` with no
    feasibility attempts and no re-ask. ``selector_fallback`` names its
    ``fallback_reason``. A
    ``superagent_reask`` source means the re-ask happened, and a
    ``superagent`` source means it did not.
    """
    inputs = _inputs_mapping(inputs)
    stored_mode = _mode(mode)
    stored_source = _source(source)
    flag = _flag(reask, "reask")
    attempts = _feasibility(feasibility, ())
    reason = _fallback_reason(fallback_reason)
    _check_consistent(stored_mode, stored_source, flag, attempts, reason)
    secrets = _task_secrets(inputs)
    return {
        "version": ASSIGNMENT_VERSION,
        "run_id": _required_id(run_id, "run_id"),
        "mode": stored_mode,
        "source": stored_source,
        "choice": _choice(choice, secrets),
        "selector_choice": _choice(selector_choice, secrets),
        "feasibility": _feasibility(feasibility, secrets),
        "reask": flag,
        "fallback_reason": _redact(_fallback_reason(fallback_reason), secrets),
        "inputs_digest": inputs_digest(inputs),
        "latency_ms": _optional_number(latency_ms, "latency_ms"),
        "llm_tokens": _optional_number(llm_tokens, "llm_tokens"),
    }


def append_assignment(
    gates: Mapping[str, Any],
    *,
    line_id: str,
    run_id: str,
    mode: str,
    source: str,
    choice: Mapping[str, Any] | object | None = None,
    selector_choice: Mapping[str, Any] | object | None = None,
    feasibility: FeasibilityInput = None,
    reask: bool = False,
    fallback_reason: str | ErrorResult | None = None,
    inputs: Mapping[str, Any] | object | None = None,
    latency_ms: float | None = None,
    llm_tokens: float | None = None,
) -> dict[str, Any]:
    """Return a new gates dict with this run's entry appended. ``gates`` is not modified.

    The same ``run_id`` replaces its entry and keeps its ``seq``: a run is one
    launch, and every relaunch reserves a new ``run_id``. A new run past 16
    drops the smallest ``seq`` other than the line's first entry, not
    whichever key a JSON object happens to list first.
    """
    if not isinstance(gates, Mapping):
        raise TypeError("gates must be a mapping")
    line = _required_id(line_id, "line_id")
    entry = build_assignment_entry(
        run_id=run_id,
        mode=mode,
        source=source,
        choice=choice,
        selector_choice=selector_choice,
        feasibility=feasibility,
        reask=reask,
        fallback_reason=fallback_reason,
        inputs=inputs,
        latency_ms=latency_ms,
        llm_tokens=llm_tokens,
    )
    updated = _thaw(gates)
    lines = updated.get("lines")
    if lines is None:
        lines = {}
        updated["lines"] = lines
    elif not isinstance(lines, dict):
        raise TypeError("gates.lines must be a mapping")
    current = lines.get(line)
    if current is None:
        current = {}
        lines[line] = current
    elif not isinstance(current, dict):
        raise TypeError("gates.lines[line_id] must be a mapping")
    stored = _coerce_assignment(current.get("assignment"))
    run = entry["run_id"]
    entry["seq"] = _next_seq(stored, run)
    stored[run] = entry
    _trim(stored, MAX_ASSIGNMENT_ENTRIES)
    current["assignment"] = stored
    return updated


def assignment_entries(assignment: object) -> list[dict[str, Any]]:
    """Entries for one line, oldest ``seq`` first.

    Reads the same shapes :func:`append_assignment` accepts: the mapping keyed
    by ``run_id`` and the older list of entries. ``None`` is no entries.
    """
    stored = _coerce_assignment(assignment)
    return sorted(
        stored.values(),
        key=lambda item: (_seq(item), str(item.get("run_id") or "")),
    )


def assignment_ref(line_id: str, run_id: str) -> dict[str, str]:
    """What N30's cost ledger stores as ``assignment_ref`` for one attempt."""
    return {
        "line_id": _required_id(line_id, "line_id"),
        "run_id": _required_id(run_id, "run_id"),
    }


def inputs_digest(inputs: Mapping[str, Any] | object | None) -> dict[str, Any]:
    """Hash of ``inputs`` plus numeric success rates and headroom. No task text.

    The hash covers the whole bundle, including the task, so two lines with the
    same rates and different tasks do not collide. Datetimes hash as ISO text,
    enums by value, pydantic models and dataclasses by their fields; any other
    object is a ``TypeError`` rather than a hash that ignores its value. The
    returned object copies only numbers found under success-rate and headroom
    (or quota) keys, plus the ``model`` / ``provider`` / ``area`` label of
    each row so a count still says whose it is. ``success_by_provider`` and
    ``success_rate_by_area`` (the ``context.AssignmentInputs`` tables) each
    keep their own key. Other strings under those keys, every task, goal,
    prompt or description subtree, and any key or label that quotes a task
    are left out. ``inputs`` may be a mapping, or a dataclass or pydantic
    bundle such as ``context.AssignmentInputs``.
    """
    mapped = _inputs_mapping(inputs)
    bundle: Mapping[str, Any] = mapped if mapped is not None else {}
    secrets = _task_secrets(bundle)
    success: dict[str, Any] = {}
    headroom: dict[str, Any] = {}
    _collect(bundle, (), success, headroom, secrets)
    return {
        "hash": hashlib.sha256(_canonical(bundle).encode("utf-8")).hexdigest(),
        "success_rates": success,
        "headroom": headroom,
    }


def _mode(value: object) -> str:
    raw = value.value if isinstance(value, Enum) else value
    if not isinstance(raw, str):
        raise TypeError("mode must be a string")
    name = raw.strip().casefold()
    if name not in MODES:
        raise ValueError("mode must be off, shadow, or enforce")
    return name


def _source(value: object) -> str:
    raw = value.value if isinstance(value, Enum) else value
    name = str(raw or "").strip().casefold()
    if name not in SOURCES:
        raise ValueError(
            "source must be superagent, superagent_reask, selector_fallback, or selector"
        )
    return name


def _check_consistent(
    mode: str,
    source: str,
    reask: bool,
    attempts: Sequence[Mapping[str, Any]],
    fallback_reason: str | None,
) -> None:
    if mode == OFF:
        if source != SOURCE_SELECTOR:
            raise ValueError(
                "mode off does not call the super-agent; source is selector"
            )
        if reask or attempts:
            raise ValueError("mode off has no feasibility attempts and no re-ask")
    if source == SOURCE_SELECTOR_FALLBACK and fallback_reason is None:
        raise ValueError("source selector_fallback needs a fallback_reason")
    if source == SOURCE_SUPERAGENT_REASK and not reask:
        raise ValueError("source superagent_reask requires reask")
    if source == SOURCE_SUPERAGENT and reask:
        raise ValueError("a re-asked choice is source superagent_reask")


def _choice(value: object, secrets: Sequence[str]) -> dict[str, Any]:
    if value is None:
        raw: Mapping[str, Any] = {}
    elif isinstance(value, Mapping):
        raw = value
    elif isinstance(value, BaseModel):
        raw = value.model_dump(mode="json")
    else:
        raw = {name: getattr(value, name, None) for name in CHOICE_FIELDS}
    stored = {name: _plain(raw.get(name), name) for name in CHOICE_FIELDS}
    if stored["model"]:
        if stored["effort"] is None or stored["effort"] == "":
            # Today's selector picks a model only; it launches at default.
            stored["effort"] = Effort.DEFAULT.value
        resolved = resolve_effort(str(stored["model"]), stored["effort"])
        if resolved.supported:
            given = stored["cli_effort"]
            if given is not None and given != resolved.cli_effort:
                raise ValueError(
                    f"cli_effort {given!r} is not the table's token "
                    f"{resolved.cli_effort!r} for {stored['model']} {stored['effort']}"
                )
            stored["cli_effort"] = resolved.cli_effort
    for name in _REDACTED_CHOICE_FIELDS:
        field = stored[name]
        if isinstance(field, str):
            stored[name] = _redact(field, secrets)
    return stored


def _feasibility(
    attempts: FeasibilityInput, secrets: Sequence[str]
) -> list[dict[str, Any]]:
    if attempts is None:
        return []
    if isinstance(attempts, (Mapping, FeasibilityResult)):
        items: Sequence[object] = [attempts]
    elif isinstance(attempts, Sequence) and not isinstance(attempts, (str, bytes)):
        items = attempts
    else:
        raise TypeError("feasibility must be a sequence of attempts")
    recorded: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if isinstance(item, FeasibilityResult):
            item = item.to_dict()
        if not isinstance(item, Mapping):
            raise TypeError(
                "feasibility attempt must be a mapping or FeasibilityResult"
            )
        recorded.append(
            {
                "attempt": index,
                "result": _result(item),
                "reason_code": _reason_code(item.get("reason_code"), secrets),
            }
        )
    return recorded


def _result(item: Mapping[str, Any]) -> str:
    raw = item.get("status")
    if raw is None:
        raw = item.get("result")
    if raw is None and "ok" in item:
        flag = item.get("ok")
        if not isinstance(flag, bool):
            raise TypeError("feasibility ok must be a bool")
        raw = _RESULT_OK if flag else _RESULT_INFEASIBLE
    if isinstance(raw, bool):
        raw = _RESULT_OK if raw else _RESULT_INFEASIBLE
    if not isinstance(raw, str):
        raise ValueError("feasibility result must be ok or infeasible")
    name = raw.strip().casefold()
    if name in _OK_RESULTS:
        return _RESULT_OK
    if name in _INFEASIBLE_RESULTS:
        return _RESULT_INFEASIBLE
    raise ValueError("feasibility result must be ok or infeasible")


def _reason_code(value: object, secrets: Sequence[str]) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        value = value.value
    if not isinstance(value, str):
        raise TypeError("reason_code must be a string or None")
    text = value.strip()
    if not text:
        return None
    return _redact(text, secrets)


def _fallback_reason(value: object) -> str | None:
    if isinstance(value, ErrorResult):
        return value.kind.value
    return _optional_text(value, "fallback_reason")


def _plain(value: object, name: str) -> object:
    """A choice field as plain JSON. Enums by value; nothing else is coerced."""
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else None
    raise TypeError(f"choice.{name} must be a string, number, or None")


def _inputs_mapping(inputs: object) -> Mapping[str, Any] | None:
    """``inputs`` as a mapping. A dataclass or pydantic bundle is its fields."""
    if inputs is None:
        return None
    mapped = _as_fields(inputs)
    if not isinstance(mapped, Mapping):
        raise TypeError("inputs must be a mapping, a dataclass, a pydantic model, or None")
    return mapped


def _task_secrets(inputs: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Task strings from ``inputs``, longest first, so a quote can be removed.

    Strings shorter than :data:`MIN_REDACTED_TASK_LENGTH` are skipped; they
    would match inside unrelated words.
    """
    found: list[str] = []
    if isinstance(inputs, Mapping):
        _gather_task_text(inputs, False, found)
    unique: list[str] = []
    seen: set[str] = set()
    for text in sorted(found, key=len, reverse=True):
        if text in seen or text == _TASK_REDACTION:
            continue
        if len(text) < MIN_REDACTED_TASK_LENGTH:
            continue
        seen.add(text)
        unique.append(text)
    return tuple(unique)


def _gather_task_text(node: object, in_task: bool, found: list[str]) -> None:
    # A typed line (``context.LineInput``) carries ``task_text`` as a field.
    node = _as_fields(node)
    if isinstance(node, str):
        if in_task:
            text = node.strip()
            if text:
                found.append(text)
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            nested = in_task or str(key).casefold() in _TASK_ECHO_KEYS
            _gather_task_text(value, nested, found)
        return
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for item in node:
            _gather_task_text(item, in_task, found)


def _redact(value: str | None, secrets: Sequence[str]) -> str | None:
    """Replace task echoes. A string that does not quote the task is unchanged."""
    if value is None or not secrets:
        return value
    redacted = value
    for secret in secrets:
        if not secret or secret not in redacted:
            continue
        redacted = _TASK_REDACTION.join(redacted.split(secret))
    if redacted == value:
        return value
    stripped = redacted.strip()
    return stripped or None


def _thaw(value: object) -> Any:
    """Deep copy with every mapping as a plain ``dict`` (``MappingProxyType`` too)."""
    if isinstance(value, Mapping):
        return {key: _thaw(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_thaw(inner) for inner in value]
    return copy.deepcopy(value)


def _legacy_run_id(entry: Mapping[str, Any], key: object) -> str | None:
    for candidate in (entry.get("run_id"), key):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


def _coerce_assignment(value: object) -> dict[str, dict[str, Any]]:
    """Stored entries keyed by ``run_id``, each with an ``int`` ``seq``.

    Malformed old entries (not a mapping, no usable ``run_id``) are skipped
    rather than blocking the append. Entries without a ``seq`` are numbered
    after the highest ``seq`` already present, in stored order. Two entries
    for one ``run_id`` keep the higher ``seq``.
    """
    if value is None:
        return {}
    pairs: list[tuple[object, object]]
    if isinstance(value, Mapping):
        pairs = list(value.items())
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        pairs = [(None, entry) for entry in value]
    else:
        raise TypeError(
            "assignment must be a mapping keyed by run_id or a list of entries"
        )
    items: list[dict[str, Any]] = []
    for key, entry in pairs:
        if not isinstance(entry, Mapping):
            continue
        run_id = _legacy_run_id(entry, key)
        if run_id is None:
            continue
        copied = _thaw(entry)
        copied["run_id"] = run_id
        items.append(copied)
    top = max((_seq(item) for item in items if _has_seq(item)), default=0)
    for item in items:
        if not _has_seq(item):
            top += 1
            item["seq"] = top
    stored: dict[str, dict[str, Any]] = {}
    for item in items:
        run_id = item["run_id"]
        previous = stored.get(run_id)
        if previous is None or _seq(item) >= _seq(previous):
            stored[run_id] = item
    return stored


def _next_seq(stored: Mapping[str, Mapping[str, Any]], run_id: str) -> int:
    existing = stored.get(run_id)
    if isinstance(existing, Mapping) and _has_seq(existing):
        return _seq(existing)
    best = 0
    for entry in stored.values():
        best = max(best, _seq(entry))
    return best + 1


def _trim(stored: dict[str, dict[str, Any]], limit: int) -> None:
    while len(stored) > limit:
        ordered = sorted(stored, key=lambda key: (_seq(stored[key]), key))
        # Keep the first launch (ordered[0]): the §6.2 cohort and the
        # feasible-first-try rate read it. Drop the oldest of the rest.
        del stored[ordered[1]]


def _has_seq(entry: Mapping[str, Any]) -> bool:
    seq = entry.get("seq")
    return isinstance(seq, int) and not isinstance(seq, bool)


def _seq(entry: Mapping[str, Any]) -> int:
    return entry["seq"] if _has_seq(entry) else 0


def _as_fields(node: object) -> object:
    """A dataclass or pydantic reading as a mapping of its fields."""
    if isinstance(node, BaseModel):
        return node.model_dump(mode="python")
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        return dataclasses.asdict(node)
    return node


def _quotes_task(text: str, secrets: Sequence[str]) -> bool:
    return any(secret and secret in text for secret in secrets)


def _collect(
    node: object,
    path: tuple[str, ...],
    success: dict[str, Any],
    headroom: dict[str, Any],
    secrets: Sequence[str] = (),
) -> None:
    node = _as_fields(node)
    if isinstance(node, Mapping):
        for key, value in node.items():
            name = str(key)
            folded = name.casefold()
            if folded in _RAW_TASK_KEYS:
                continue
            if _quotes_task(name, secrets):
                # A subtree keyed by the task would store the task as a path.
                continue
            if folded in _SUCCESS_KEYS:
                # Per-area and per-provider row tables stay under their own
                # key so they do not overwrite the rates at the same path.
                where = path + (name,) if folded in _SUCCESS_TABLE_KEYS else path
                _place(success, where, _numbers_only(value, secrets))
                continue
            if folded in _HEADROOM_KEYS:
                _place(headroom, path, _numbers_only(value, secrets))
                continue
            _collect(value, path + (name,), success, headroom, secrets)
        return
    if isinstance(node, (list, tuple)) and not isinstance(node, (str, bytes)):
        for index, value in enumerate(node):
            _collect(value, path + (str(index),), success, headroom, secrets)


def _place(target: dict[str, Any], path: tuple[str, ...], numbers: object) -> None:
    if numbers is None:
        return
    if not path:
        if isinstance(numbers, dict):
            target.update(numbers)
        else:
            target["value"] = numbers
        return
    cursor = target
    for part in path[:-1]:
        current = cursor.get(part)
        if not isinstance(current, dict):
            current = {}
            cursor[part] = current
        cursor = current
    cursor[path[-1]] = numbers


def _numbers_only(node: object, secrets: Sequence[str] = ()) -> object:
    """Numbers under ``node``, plus each row's model/provider/area label.

    A key or a label that quotes a task string is dropped, not stored.
    """
    node = _as_fields(node)
    if isinstance(node, bool) or node is None:
        return None
    if isinstance(node, (int, float)):
        if isinstance(node, float) and not isfinite(node):
            return None
        return node
    if isinstance(node, Mapping):
        cleaned: dict[str, Any] = {}
        labels: dict[str, str] = {}
        for key, value in node.items():
            name = str(key)
            if name.casefold() in _RAW_TASK_KEYS or _quotes_task(name, secrets):
                continue
            if name.casefold() in _READING_LABEL_KEYS and isinstance(value, str):
                label = value.strip()
                if label and not _quotes_task(label, secrets):
                    labels[name] = label
                continue
            child = _numbers_only(value, secrets)
            if child is None or child == {} or child == []:
                continue
            cleaned[name] = child
        # A label alone is not a reading; it is kept only beside a number.
        if not cleaned:
            return None
        return {**labels, **cleaned}
    if isinstance(node, (list, tuple)) and not isinstance(node, (str, bytes)):
        cleaned_list = []
        for value in node:
            child = _numbers_only(value, secrets)
            if child is None:
                continue
            cleaned_list.append(child)
        return cleaned_list or None
    return None


def _canonical(value: object) -> str:
    return json.dumps(
        _freeze(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _freeze(value: object) -> object:
    if isinstance(value, Enum):
        return _freeze(value.value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            return None
        return value
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return _freeze(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _freeze(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, inner in value.items():
            name = str(key.value if isinstance(key, Enum) else key)
            if name in frozen:
                raise TypeError(f"inputs key {name!r} appears twice after str()")
            frozen[name] = _freeze(inner)
        return frozen
    if isinstance(value, (list, tuple)):
        return [_freeze(inner) for inner in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_freeze(inner) for inner in value), key=_canonical)
    raise TypeError(
        f"inputs value of type {type(value).__name__} cannot be hashed by value"
    )


def _required_id(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if value.strip() == "":
        raise ValueError(f"{name} is required")
    return value


def _flag(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise TypeError(f"{name} must be a bool")


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        value = value.value
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string or None")
    text = value.strip()
    return text or None


def _optional_number(value: object, name: str) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number or None")
    if isinstance(value, float) and not isfinite(value):
        raise ValueError(f"{name} must be a nonnegative finite number")
    if value < 0:
        raise ValueError(f"{name} must be a nonnegative finite number")
    if isinstance(value, int):
        return value
    number = float(value)
    if number.is_integer():
        return int(number)
    return number
