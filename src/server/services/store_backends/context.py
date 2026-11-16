"""Who a call runs for, as the store-backed routes read it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CallContext:
    """Who a command runs for. The mount serves the whole computer, so a save
    through it names only the call, and a file whose meaning depends on the
    conversation (a new automation's defaults and ``"current"`` thread)
    reads the rest from here."""

    workspace_id: str | None = None
    thread_id: str | None = None
    # None reads the user's stored zone, when a file needs one.
    timezone: str | None = None


__all__ = ["CallContext"]
