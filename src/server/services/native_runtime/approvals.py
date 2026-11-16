"""Host-side approval broker for native agent runtimes.

A vendor CLI that runs in a subprocess cannot receive a human decision through
Conductor's ordinary request body while its turn is paused.  The runtime parks
the live process here, the graph raises a LangGraph ``interrupt`` so the
existing HITL pipeline pauses the run, and the resume turn sends the user's
decision back through this broker.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass
class PendingNativeApproval:
    """One paused native turn waiting for a human decision."""

    thread_id: str
    runtime: Any
    request_id: int
    approval_id: str
    kind: str
    interrupt_payload: dict[str, Any]
    text_parts: list[str] = field(default_factory=list)

    def decision_for(self, response: Any) -> str:
        """Map Conductor's HITL decision shape to Codex's decision enum."""

        value = response
        if isinstance(value, Mapping):
            # The resume payload is keyed by the LangGraph interrupt id; the
            # broker only ever parks one approval per native thread, so the
            # first entry is this approval.
            entries = list(value.values())
            if entries and isinstance(entries[0], Mapping):
                value = entries[0]
        if isinstance(value, Mapping):
            decisions = value.get("decisions")
            if isinstance(decisions, list) and decisions:
                first = decisions[0]
                if isinstance(first, Mapping):
                    verdict = str(first.get("type") or "").strip().lower()
                    if verdict == "approve":
                        return "accept"
                    if verdict in {"reject", "deny"}:
                        return "decline"
                    return "cancel"
        return "cancel"


class NativeApprovalBroker:
    """Thread-scoped pending approvals; one native turn pauses at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, PendingNativeApproval] = {}

    def park(self, pending: PendingNativeApproval) -> None:
        with self._lock:
            self._pending[pending.thread_id] = pending

    def get(self, thread_id: str | None) -> PendingNativeApproval | None:
        if not thread_id:
            return None
        with self._lock:
            return self._pending.get(thread_id)

    def clear(self, thread_id: str | None) -> PendingNativeApproval | None:
        if not thread_id:
            return None
        with self._lock:
            return self._pending.pop(thread_id, None)


_BROKER = NativeApprovalBroker()


def get_approval_broker() -> NativeApprovalBroker:
    return _BROKER
