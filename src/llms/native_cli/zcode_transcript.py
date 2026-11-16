"""Shared parsing for ZCode-family CLI transcripts.

The ZCode CLIs (``zcode`` and the clean-room ``zcode-cli``) can print their
headless result as a stream of newline-delimited events. The transcript ends
with a terminal result object, but the process exit code does not separate a
failed turn from a turn budget that ran out: both exit non-zero, and the CLI
reports the exhausted budget as its own ``resultType``.

That distinction matters. A long headless run that simply needed more turns
must not be reported as a generic failure, so the transcript is the source of
truth and the exit code is only a fallback.
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Mapping, Sequence

from src.llms.native_cli.base import NativeCliError

#: ``resultType`` the CLI reports when the turn budget ran out.
MAX_TURNS_RESULT_TYPE = "error_max_turns"

#: ``stopReason`` the CLI reports when the turn budget ran out.
MAX_TURNS_STOP_REASON = "max_turns"


class ZCodeMaxTurnsError(NativeCliError):
    """The CLI hit its turn budget before it produced a final answer.

    This is a budget problem, not a provider failure: the run is resumable
    with a larger turn budget.
    """

    code = "max_turns"


def iter_json_objects(stdout: str) -> Iterator[Mapping[str, Any]]:
    """Yield every JSON object in a transcript, ignoring non-JSON lines."""
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            yield payload


def final_result(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The terminal result object of a transcript.

    ``--output-format stream-json`` marks it with ``type: "result"``; the
    single-object ``json`` shape carries the same fields without the marker.
    """
    for event in reversed(events):
        if str(event.get("type") or "") == "result":
            return event
    for event in reversed(events):
        if "response" in event or "usage" in event or "sessionId" in event:
            return event
    return {}


def turn_result_type(events: Sequence[Mapping[str, Any]]) -> str:
    """The ``resultType`` the CLI reported for the turn, if any."""
    for event in events:
        if str(event.get("type") or "") != "turn.completed":
            continue
        payload = event.get("payload")
        if isinstance(payload, Mapping):
            value = str(payload.get("resultType") or "").strip()
            if value:
                return value
    return ""


def turn_failure(events: Sequence[Mapping[str, Any]]) -> str | None:
    """The diagnostic of a failed turn, if the transcript reports one."""
    for event in events:
        if str(event.get("type") or "") != "turn.failed":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        error = payload.get("error")
        if isinstance(error, Mapping):
            return " ".join(
                str(error.get(key) or "") for key in ("name", "message")
            ).strip()
        if error:
            return str(error)
    return None


def exhausted_turn_budget(
    events: Sequence[Mapping[str, Any]], payload: Mapping[str, Any]
) -> bool:
    """Whether the transcript shows the turn budget running out."""
    if turn_result_type(events) == MAX_TURNS_RESULT_TYPE:
        return True
    return str(payload.get("stopReason") or "") == MAX_TURNS_STOP_REASON
