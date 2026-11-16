"""Silent-worker detection: ``off``, ``shadow`` or ``enforce``.

A dispatch Worker can fail without saying anything: its run log holds the
launcher's own lines and the prompt it was given, and not one word from the
model. Such a run either finishes "successfully" with an empty answer, or sits
there until a timeout fires. Neither is a failure the existing failure
classifier sees, because there is no diagnostic to classify.

This module is the pure detector the state machine calls. It reads the run
log text it is handed, decides whether the model produced any output at all,
and records a finding on the line's ``silent_worker`` cell. It does no I/O,
never sleeps and never reads a clock: the caller passes ``now`` and the
elapsed time.

It ships in ``shadow`` first: a finding is recorded, and nothing else changes.
``enforce`` also lets the caller act on it (fail the run as
``worker_no_output`` and reassign); ``off`` neither records nor acts. The
mode is read from ``CONDUCTOR_DISPATCH_SILENT_WORKER_MODE`` with the exact
spelling rules of the liveness sweep (``liveness.modes.normalize_mode``): a
typo falls back to ``shadow``.

Detection is deliberately conservative. A false "silent" alarm reassigns
work a Worker was really doing, so any line this module cannot place as
launcher noise, an echoed prompt or a known non-assistant event counts as
model output.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.tools.auto_dispatch.liveness.modes import ENFORCE, OFF, normalize_mode

#: The environment variable that switches silent-worker detection.
MODE_ENV = "CONDUCTOR_DISPATCH_SILENT_WORKER_MODE"
#: Minutes without any model output before a running Worker counts as silent.
MINUTES_ENV = "CONDUCTOR_DISPATCH_SILENT_WORKER_MINUTES"
DEFAULT_MINUTES = 15

#: The failure reason (and failure class) for a silent Worker.
WORKER_NO_OUTPUT = "worker_no_output"
#: The run is still going and has produced nothing for too long.
KIND_NO_OUTPUT_TIMEOUT = "no_output_timeout"
#: The run finished cleanly and produced nothing.
KIND_EMPTY_FINAL = "empty_final_answer"
#: The snapshot key that holds the findings.
RECORD_KEY = "silent_worker"

RECORD_VERSION = 1
MAX_RUNS = 16
_UNKNOWN_RUN = "unknown"


# ------------------------------------------------------------------ modes


def silent_mode(environ: Mapping[str, str] | None = None) -> str:
    """The configured mode; ``shadow`` when the variable is unset or invalid."""
    env = os.environ if environ is None else environ
    return normalize_mode(env.get(MODE_ENV, ""), source=MODE_ENV)


def _active(mode: str | None, environ: Mapping[str, str] | None) -> str:
    return (
        silent_mode(environ) if mode is None else normalize_mode(mode, source=MODE_ENV)
    )


def should_record(
    mode: str | None = None, *, environ: Mapping[str, str] | None = None
) -> bool:
    """Whether a finding may be written (``shadow`` and ``enforce``)."""
    return _active(mode, environ) != OFF


def should_act(
    mode: str | None = None, *, environ: Mapping[str, str] | None = None
) -> bool:
    """Whether the caller may fail and reassign the run (``enforce`` only)."""
    return _active(mode, environ) == ENFORCE


def silent_after_seconds(environ: Mapping[str, str] | None = None) -> float:
    """Seconds without model output before a running Worker is silent.

    Read from ``CONDUCTOR_DISPATCH_SILENT_WORKER_MINUTES``; integers and
    decimals are accepted. An absent, non-numeric, non-positive or non-finite
    value falls back to ``DEFAULT_MINUTES``.
    """
    env = os.environ if environ is None else environ
    raw = str(env.get(MINUTES_ENV, "") or "").strip()
    minutes = float(DEFAULT_MINUTES)
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = math.nan
        if math.isfinite(value) and value > 0:
            minutes = value
    return minutes * 60.0


# ------------------------------------------------------------- detection

_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL / ST
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b[@-Z\\-_]"  # two-byte escapes
)

#: Prefixes of the lines ``scripts/dispatch_launch.sh`` writes into the log
#: itself (``deepseek_note``, ``deepseek_marker``, the watchdog), plus the
#: shell's own job messages about the launcher.
_LAUNCHER_PREFIXES = (
    "[launcher]",
    "[watchdog]",
    "dispatch_launch:",
    "dispatch_launch.sh:",
)
_LAUNCHER_CONTAINS = ("dispatch_launch.sh: line ",)
_SHELL_NOISE = frozenset({"terminated", "killed", "hangup"})

#: Roles that never carry model output.
_NON_ASSISTANT_ROLES = frozenset(
    {"user", "system", "developer", "meta", "tool", "function", "human"}
)

#: Event types that carry no model output: session/init/system/user events,
#: usage and turn bookkeeping, keepalives, errors.
_NON_OUTPUT_TYPES = frozenset(
    {
        "system",
        "init",
        "session",
        "user",
        "user_message",
        "human",
        "prompt",
        "input",
        "meta",
        "usage",
        "status",
        "ping",
        "pong",
        "heartbeat",
        "keepalive",
        "progress",
        "end",
        "done",
        "error",
        "rate_limit",
        "rate_limit_event",
        "tool_result",
        "function_call_output",
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "step_start",
        "step_end",
        "message_start",
        "message_stop",
        "content_block_stop",
        "exec.started",
    }
)
_NON_OUTPUT_PREFIXES = ("session.", "system.", "user.", "meta.", "usage.", "thread.")

#: Event types that carry the user's or the system's own text.
_USER_TYPES = frozenset(
    {
        "user",
        "user_message",
        "human",
        "prompt",
        "input",
        "system",
        "init",
        "meta",
        "tool_result",
        "function_call_output",
    }
)
#: Top-level keys that hold a final answer.
_RESULT_KEYS = ("result", "output", "response", "answer")

#: Content blocks / item types that mean the model acted (a tool call).
_ACTION_TYPES = frozenset(
    {
        "tool_use",
        "server_tool_use",
        "tool_call",
        "tool_call_update",
        "function_call",
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "web_search",
        "todo_list",
    }
)

#: Keys whose string value is model text.
_TEXT_KEYS = frozenset(
    {
        "text",
        "content",
        "delta",
        "result",
        "output",
        "response",
        "answer",
        "message",
        "thinking",
        "reasoning",
        "summary",
        "partial_json",
    }
)

#: Subtrees that never hold model output.
_SKIP_KEYS = frozenset(
    {
        "usage",
        "error",
        "errors",
        "metadata",
        "input",
        "prompt",
        "arguments",
        "parameters",
        "stats",
        "modelUsage",
        "model_usage",
    }
)

#: Keys that, on an untyped JSON object, are bookkeeping only.
_METADATA_KEYS = frozenset(
    {
        "session_id",
        "sessionId",
        "thread_id",
        "threadId",
        "id",
        "uuid",
        "usage",
        "model",
        "cwd",
        "timestamp",
        "ts",
        "time",
        "duration_ms",
        "duration_api_ms",
        "num_turns",
        "total_cost_usd",
        "cost_usd",
        "status",
        "phase",
        "error",
        "is_error",
        "subtype",
        "version",
        "tools",
        "permissionMode",
        "apiKeySource",
    }
)

_MAX_DEPTH = 8


def _lower(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _is_launcher_line(line: str) -> bool:
    if line.startswith(_LAUNCHER_PREFIXES):
        return True
    if any(marker in line for marker in _LAUNCHER_CONTAINS):
        return True
    return line.lower() in _SHELL_NOISE


def _has_text(value: Any, depth: int = 0) -> bool:
    """Whether ``value`` holds non-empty model text or a model action."""
    if depth > _MAX_DEPTH:
        return True
    if isinstance(value, list):
        return any(_has_text(item, depth + 1) for item in value)
    if not isinstance(value, Mapping):
        return False
    if _lower(value.get("role")) in _NON_ASSISTANT_ROLES:
        return False
    block_type = _lower(value.get("type"))
    if block_type in _ACTION_TYPES:
        return True
    if block_type in _NON_OUTPUT_TYPES:
        return False
    for key, item in value.items():
        if key in _SKIP_KEYS:
            continue
        if isinstance(item, str):
            if key in _TEXT_KEYS and item.strip():
                return True
        elif isinstance(item, (Mapping, list)) and _has_text(item, depth + 1):
            return True
    return False


def _event_has_output(event: Mapping[str, Any], depth: int = 0) -> bool:
    """Whether one JSON event carries model output; unknown shapes count."""
    if depth > _MAX_DEPTH:
        return True
    role = _lower(event.get("role"))
    message = event.get("message")
    message_role = _lower(message.get("role")) if isinstance(message, Mapping) else ""
    if role in _NON_ASSISTANT_ROLES or message_role in _NON_ASSISTANT_ROLES:
        return False
    raw_type = event.get("type")
    etype = _lower(raw_type) if isinstance(raw_type, str) else ""
    if etype not in _USER_TYPES and any(
        isinstance(event.get(key), str) and event[key].strip() for key in _RESULT_KEYS
    ):
        # A final answer on any event that is not the user's own.
        return True
    if etype in _NON_OUTPUT_TYPES or etype.startswith(_NON_OUTPUT_PREFIXES):
        return False

    # Final result envelopes: claude/cursor ``result``, minimax ``exec.result``.
    if etype in {"result", "exec.result"}:
        return _has_text(event.get("message"))

    # A wrapper around a nested event: claude ``stream_event``.
    nested = event.get("event")
    if isinstance(nested, Mapping):
        return _event_has_output(nested, depth + 1)

    # Codex ``item.started`` / ``item.updated`` / ``item.completed``.
    item = event.get("item")
    if etype.startswith("item.") and isinstance(item, Mapping):
        item_type = _lower(item.get("type"))
        if (
            item_type in _NON_OUTPUT_TYPES
            or _lower(item.get("role")) in _NON_ASSISTANT_ROLES
        ):
            return False
        if item_type in {"agent_message", "reasoning", "message", "assistant_message"}:
            return _has_text(item)
        # A tool action, or an item kind this module does not know.
        return True

    if _has_text(event):
        return True
    if etype in _ACTION_TYPES:
        return True
    if role == "assistant" or etype in {
        "assistant",
        "text",
        "thought",
        "thinking",
        "final",
        "message_end",
        "turn_end",
        "content_block_delta",
        "content_block_start",
        "message_delta",
    }:
        # A known assistant event that came out empty.
        return False
    if not etype:
        # An untyped object: bookkeeping keys and empty text keys mean no
        # output (an arkcli envelope with an empty ``content``).
        rest = {
            key
            for key, value in event.items()
            if key not in _METADATA_KEYS
            and not (key in _TEXT_KEYS and value in (None, "", [], {}))
            and not (key in _TEXT_KEYS and isinstance(value, str) and not value.strip())
        }
        return bool(rest)
    # A typed event this module does not know: count it (conservative).
    return True


def _json_line_has_output(payload: Any) -> bool:
    if isinstance(payload, Mapping):
        return _event_has_output(payload)
    if isinstance(payload, list):
        return any(_json_line_has_output(item) for item in payload)
    if isinstance(payload, str):
        return bool(payload.strip())
    # A bare number, boolean or null.
    return False


def _prompt_lines(prompt: str | None) -> frozenset[str]:
    if not prompt:
        return frozenset()
    return frozenset(
        line.strip() for line in _strip_ansi(prompt).splitlines() if line.strip()
    )


def has_assistant_output(text: str, *, prompt: str | None = None) -> bool:
    """True when the run log carries any model output.

    ANSI codes and blank lines are ignored, and so are the lines the launcher
    writes itself and, when ``prompt`` is given, lines that echo it. A JSON
    event line counts only when it carries non-empty assistant text or a tool
    call; any other plain text line counts.
    """
    echoed = _prompt_lines(prompt)
    lines = [
        line
        for line in (
            raw.strip()
            for raw in _strip_ansi(text or "").replace("\r", "\n").splitlines()
        )
        if line and not _is_launcher_line(line) and line not in echoed
    ]
    if len(lines) > 1 and lines[0][0] in "{[":
        # One pretty-printed JSON document (arkcli, mcode).
        try:
            document = json.loads("\n".join(lines))
        except (ValueError, RecursionError):
            pass
        else:
            return _json_line_has_output(document)
    for line in lines:
        if line[0] in "{[":
            try:
                payload = json.loads(line)
            except (ValueError, RecursionError):
                return True
            if _json_line_has_output(payload):
                return True
            continue
        return True
    return False


# ---------------------------------------------------------------- findings


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class SilentFinding:
    """A run whose Worker produced no model output."""

    kind: str
    reason: str = WORKER_NO_OUTPUT
    elapsed_seconds: float | None = None
    threshold_seconds: float | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "elapsed_seconds": _finite(self.elapsed_seconds),
            "threshold_seconds": _finite(self.threshold_seconds),
        }


def classify_run(
    output: str,
    *,
    complete: bool,
    exit_code: int | None,
    elapsed_seconds: float | None,
    environ: Mapping[str, str] | None = None,
    prompt: str | None = None,
) -> SilentFinding | None:
    """The silent-worker finding for a run, or ``None``.

    A non-zero exit belongs to the existing failure classifier. A finished
    clean run with no output is ``empty_final_answer``; an unfinished run with
    no output for at least the threshold is ``no_output_timeout``.
    """
    if exit_code not in (None, 0):
        return None
    if has_assistant_output(output, prompt=prompt):
        return None
    threshold = silent_after_seconds(environ)
    elapsed = _finite(elapsed_seconds)
    if complete:
        return SilentFinding(
            kind=KIND_EMPTY_FINAL,
            elapsed_seconds=elapsed,
            threshold_seconds=threshold,
        )
    if elapsed is not None and elapsed >= threshold:
        return SilentFinding(
            kind=KIND_NO_OUTPUT_TIMEOUT,
            elapsed_seconds=elapsed,
            threshold_seconds=threshold,
        )
    return None


def _iso(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat()


def _run_key(run_id: str | None) -> str:
    key = str(run_id or "").strip()
    return key or _UNKNOWN_RUN


def _cell(snapshot: dict[str, Any]) -> dict[str, Any]:
    cell = snapshot.get(RECORD_KEY)
    if (
        not isinstance(cell, dict)
        or cell.get("version") != RECORD_VERSION
        or not isinstance(cell.get("runs"), dict)
    ):
        cell = {"version": RECORD_VERSION, "runs": {}, "last": None}
        snapshot[RECORD_KEY] = cell
    return cell


def _trim(runs: dict[str, Any], keep: str) -> None:
    if len(runs) <= MAX_RUNS:
        return

    def recency(key: str) -> tuple[str, str]:
        entry = runs.get(key)
        seen = entry.get("last_seen") if isinstance(entry, Mapping) else None
        return (str(seen or ""), key)

    others = sorted((key for key in runs if key != keep), key=recency, reverse=True)
    for key in others[MAX_RUNS - 1 :]:
        runs.pop(key, None)


def record_finding(
    snapshot: dict[str, Any],
    finding: SilentFinding,
    *,
    run_id: str | None,
    attempt: int,
    author: str,
    mode: str,
    now: datetime,
) -> dict[str, Any]:
    """Record ``finding`` on ``snapshot[RECORD_KEY]`` and return the run's record.

    Idempotent per run: a repeat keeps ``first_seen`` and ``alerted``, bumps
    ``count`` and refreshes the rest. Only the newest ``MAX_RUNS`` runs are
    kept. Everything written is JSON-safe.
    """
    cell = _cell(snapshot)
    runs: dict[str, Any] = cell["runs"]
    key = _run_key(run_id)
    stamp = _iso(now)
    previous = runs.get(key)
    if not isinstance(previous, dict):
        previous = {}
    try:
        count = int(previous.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    record = {
        **finding.to_record(),
        "author": str(author or ""),
        "attempt": int(attempt),
        "mode": normalize_mode(mode, source=MODE_ENV),
        "first_seen": str(previous.get("first_seen") or stamp),
        "last_seen": stamp,
        "count": count + 1,
        "alerted": bool(previous.get("alerted", False)),
    }
    runs[key] = record
    cell["last"] = key
    _trim(runs, key)
    return record


def should_alert(snapshot: Mapping[str, Any], run_id: str | None) -> bool:
    """True when the run has a finding that has not been alerted yet."""
    cell = snapshot.get(RECORD_KEY) if isinstance(snapshot, Mapping) else None
    runs = cell.get("runs") if isinstance(cell, Mapping) else None
    entry = runs.get(_run_key(run_id)) if isinstance(runs, Mapping) else None
    return isinstance(entry, Mapping) and not entry.get("alerted", False)


def mark_alerted(snapshot: dict[str, Any], run_id: str | None) -> bool:
    """Mark the run's finding as alerted; ``False`` when there is none."""
    cell = snapshot.get(RECORD_KEY) if isinstance(snapshot, dict) else None
    runs = cell.get("runs") if isinstance(cell, dict) else None
    entry = runs.get(_run_key(run_id)) if isinstance(runs, dict) else None
    if not isinstance(entry, dict):
        return False
    entry["alerted"] = True
    return True


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _label(value: Any, limit: int = 80) -> str:
    text = _CONTROL_RE.sub(" ", str(value if value is not None else "")).strip()
    return (text[: limit - 1] + "…") if len(text) > limit else (text or "?")


def alert_text(line_id: Any, author: Any, finding: SilentFinding) -> str:
    """A short alert line; never includes log content."""
    if finding.kind == KIND_NO_OUTPUT_TIMEOUT:
        elapsed = _finite(finding.elapsed_seconds)
        detail = (
            f"no model output for {elapsed / 60:.0f} min"
            if elapsed is not None
            else "no model output before the timeout"
        )
    elif finding.kind == KIND_EMPTY_FINAL:
        detail = "finished with no model output"
    else:
        detail = _label(finding.kind, 40)
    return (
        f"Dispatch line {_label(line_id)}: worker {_label(author, 40)} {detail} "
        f"({_label(finding.reason, 40)})."
    )


__all__ = [
    "DEFAULT_MINUTES",
    "KIND_EMPTY_FINAL",
    "KIND_NO_OUTPUT_TIMEOUT",
    "MAX_RUNS",
    "MINUTES_ENV",
    "MODE_ENV",
    "RECORD_KEY",
    "WORKER_NO_OUTPUT",
    "SilentFinding",
    "alert_text",
    "classify_run",
    "has_assistant_output",
    "mark_alerted",
    "record_finding",
    "should_act",
    "should_alert",
    "should_record",
    "silent_after_seconds",
    "silent_mode",
]
