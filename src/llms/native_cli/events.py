"""NDJSON event parsing for the DSH headless machine interface."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from src.llms.native_cli.base import NativeCliError, NativeCliUsage


@dataclass
class ParsedEventStream:
    session_id: str | None = None
    final_text: str = ""
    text_chunks: list[str] = field(default_factory=list)
    thinking_chunks: list[str] = field(default_factory=list)
    usage: NativeCliUsage = field(default_factory=NativeCliUsage)
    events: list[Mapping[str, Any]] = field(default_factory=list)

    @property
    def text(self) -> str:
        if self.final_text:
            return self.final_text
        return "".join(self.text_chunks)


def parse_ndjson(stdout: str) -> ParsedEventStream:
    """Parse one event per line, tolerating a trailing partial/noisy line.

    DSH writes progress to stderr and the run events to stdout, so stdout is
    expected to be strict NDJSON. A malformed line is skipped rather than
    losing a completed answer, but a stream with no usable event raises: a
    silent empty answer would be worse than a loud CLI failure.
    """
    parsed = ParsedEventStream()
    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        parsed.events.append(event)
        kind = event.get("type")
        if kind == "session":
            parsed.session_id = str(event.get("sessionId") or "") or None
        elif kind == "text":
            parsed.text_chunks.append(str(event.get("text") or ""))
        elif kind == "thinking":
            parsed.thinking_chunks.append(str(event.get("text") or ""))
        elif kind == "final":
            parsed.final_text = str(event.get("text") or "")
        elif kind == "status" and event.get("phase") == "step_end":
            usage = event.get("usage")
            if isinstance(usage, dict):
                parsed.usage = NativeCliUsage.from_mapping(usage)
    if not parsed.events:
        raise NativeCliError("native CLI returned no machine-readable events")
    return parsed
