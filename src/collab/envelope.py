"""The one response envelope every collab transport returns.

The envelope's job is to keep authority separate from data: task text, peer
messages, handoff self-reports and review opinions are listed in
``untrusted_fields`` so a consuming model is told, field by field, what it may
read but must not obey. The server computes the list from the shape it is about
to return; it never trusts a caller to label its own text.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.collab.types import ERROR_CODES, CollabError

#: A field whose whole value is peer-authored, not just a scalar inside it.
#: ``last_report`` is the previous holder's completion report inside a handoff
#: pack: every part of it (including ``next``, ``handbacks`` and each
#: ``verification[].ran``/``result``) is worker-authored, so the whole object
#: is marked rather than trusting a field allow-list to stay complete.
#: ``conversation_ref`` (``{app, session_id}``) is whatever the handing-off
#: app sent with its handoff request.
_WHOLE_UNTRUSTED = frozenset(
    {"worker_reported", "verdict", "self_report", "last_report", "conversation_ref"}
)

#: Scalar (or list-of-scalar) fields whose contents are authored elsewhere.
#: A task's ``title``, ``repo`` and ``write_scope`` are app-authored too
#: (``task_create``), and ``agent`` is the label a caller chose for itself.
_UNTRUSTED_TEXT = frozenset(
    {
        "acceptance",
        "agent",
        "body",
        "focus",
        "handbacks",
        "next",
        "note",
        "open_findings",
        "ran",
        "repo",
        "result",
        "summary",
        "text",
        "title",
        "write_scope",
    }
)

#: Per-shape templates. They are documentation as much as data: a new response
#: shape should add its untrusted paths here before it is exposed.
UNTRUSTED_FIELDS: dict[str, tuple[str, ...]] = {
    "task": (
        "data.task.title",
        "data.task.body",
        "data.task.acceptance[]",
        "data.task.repo",
        "data.task.write_scope[]",
    ),
    "task_list": (
        "data.tasks[].title",
        "data.tasks[].body",
        "data.tasks[].acceptance[]",
        "data.tasks[].repo",
        "data.tasks[].write_scope[]",
    ),
    "message": ("data.message.text",),
    "messages": ("data.messages[].text",),
    "handoff_pack": (
        "data.pack.worker_reported",
        "data.pack.system.last_report",
        "data.pack.system.open_findings[]",
        "data.pack.system.task.title",
        "data.pack.system.task.body",
        "data.pack.system.task.acceptance[]",
        "data.pack.system.task.repo",
        "data.pack.system.task.write_scope[]",
        "data.pack.conversation_ref",
    ),
    "review": ("data.review.verdict", "data.review.focus"),
    "reviews": ("data.reviews[].verdict", "data.reviews[].focus"),
    "agent_ask": ("data.text",),
    "whoami": ("data.agent",),
}


def _add(paths: list[str], path: str) -> None:
    if path and path not in paths:
        paths.append(path)


def _walk(value: Any, path: str, paths: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            child = f"{path}.{key_text}" if path else key_text
            if key_text in _WHOLE_UNTRUSTED:
                _add(paths, child)
                continue
            if key_text in _UNTRUSTED_TEXT:
                if isinstance(item, (list, tuple)):
                    _add(paths, f"{child}[]")
                else:
                    _add(paths, child)
                continue
            _walk(item, child, paths)
    elif isinstance(value, (list, tuple)):
        child = f"{path}[]" if path else "[]"
        for item in value:
            _walk(item, child, paths)


def _envelope_data(value: Any) -> Mapping[str, Any] | None:
    """Return the ``data`` object when ``value`` is already an envelope."""
    if isinstance(value, Mapping) and "data" in value:
        if "ok" in value or "code" in value or "untrusted_fields" in value:
            data = value.get("data")
            return data if isinstance(data, Mapping) else {}
    return None


def untrusted_fields(
    value: Any, *, prefix: str = "data", include_prefix: bool = True
) -> list[str]:
    """Paths in ``value`` that carry text from another agent or an external source.

    The result is deterministic and sorted by discovery order, so a response is
    stable across retries. Passing a bare ``data`` object and passing a complete
    envelope both work: an envelope is unwrapped first so its paths never gain
    a second ``data.`` prefix.
    """
    paths: list[str] = []
    root = prefix if include_prefix else ""
    data = _envelope_data(value)
    _walk(data if data is not None else value, root, paths)
    return paths


def ok_envelope(
    data: Mapping[str, Any] | None = None,
    *,
    untrusted: list[str] | tuple[str, ...] | None = None,
    redacted: int = 0,
) -> dict[str, Any]:
    """A success envelope; ``untrusted_fields`` is computed unless supplied."""
    payload = dict(data or {})
    fields = list(untrusted) if untrusted is not None else untrusted_fields(payload)
    return {
        "ok": True,
        "code": "ok",
        "data": payload,
        "untrusted_fields": fields,
        "redacted": max(0, int(redacted)),
    }


def error_envelope(
    code: str,
    message: str = "",
    *,
    data: Mapping[str, Any] | None = None,
    untrusted: list[str] | tuple[str, ...] | None = None,
    redacted: int = 0,
    retry_after: float | int | None = None,
) -> dict[str, Any]:
    """A refusal envelope with the closed protocol code vocabulary."""
    resolved = str(code or "internal_error")
    if resolved == "ok" or resolved not in ERROR_CODES:
        resolved = "internal_error"
    payload = dict(data or {})
    fields = list(untrusted) if untrusted is not None else untrusted_fields(payload)
    envelope: dict[str, Any] = {
        "ok": False,
        "code": resolved,
        "data": payload,
        "untrusted_fields": fields,
        "redacted": max(0, int(redacted)),
    }
    if message:
        envelope["message"] = message
    if retry_after is not None:
        envelope["retry_after"] = max(0.0, float(retry_after))
    return envelope


def from_error(error: CollabError, *, redacted: int = 0) -> dict[str, Any]:
    """Render a ``CollabError`` without leaking its internal details."""
    return error_envelope(
        error.code,
        str(error),
        redacted=redacted,
        retry_after=error.retry_after,
    )


def is_success(envelope: Mapping[str, Any]) -> bool:
    return bool(envelope.get("ok")) and envelope.get("code") == "ok"


def envelope(
    data: Mapping[str, Any] | None = None,
    *,
    untrusted: list[str] | tuple[str, ...] | None = None,
    redacted: int = 0,
) -> dict[str, Any]:
    return ok_envelope(data, untrusted=untrusted, redacted=redacted)


#: Names other front ends use.
build_envelope = ok_envelope
build_error = error_envelope
UNTRUSTED_FIELD_PATHS = UNTRUSTED_FIELDS


__all__ = [
    "UNTRUSTED_FIELDS",
    "UNTRUSTED_FIELD_PATHS",
    "build_envelope",
    "build_error",
    "envelope",
    "error_envelope",
    "from_error",
    "is_success",
    "ok_envelope",
    "untrusted_fields",
]
