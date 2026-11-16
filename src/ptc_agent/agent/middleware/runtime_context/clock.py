"""Human-readable durations for the runtime-context envelope."""

from __future__ import annotations

from datetime import datetime


def format_elapsed(start: datetime, end: datetime) -> str:
    seconds = max(0.0, (end - start).total_seconds())
    if seconds < 60:
        return f"{int(seconds)} seconds"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''}"


def elapsed_summary(start: datetime, end: datetime) -> tuple[str, int]:
    return format_elapsed(start, end), 0


