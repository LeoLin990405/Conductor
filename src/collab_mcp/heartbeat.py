"""Idle-aware automatic heartbeat for the leases one MCP server process holds.

N80 section 7: the lease is 15 minutes and the heartbeat every 60 seconds.
The MCP process heartbeats on the app's behalf only while the app is in use:
once no tool call has arrived for 30 minutes it stops, so an app left open by
someone who walked away lets its lease expire and the task goes back to the
Router. The automatic heartbeat itself never counts as activity -- otherwise
it would keep itself alive for ever.

The class is synchronous and driven by an injected clock, so the idle rule is
tested with a fake clock and no sleeping; :meth:`AutoHeartbeat.run` is the thin
async loop the server starts beside its transport.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import anyio

logger = logging.getLogger(__name__)

#: Stop heartbeating after this long without a tool call (N80 7).
IDLE_CUTOFF_SECONDS = 30 * 60
#: Default heartbeat interval when the claim did not name one (N80 7).
DEFAULT_HEARTBEAT_SECONDS = 60
#: How often the async loop wakes up to look for a due lease.
TICK_SECONDS = 5.0

#: Codes after which a lease is no longer ours to keep alive.
LEASE_ENDING_CODES = frozenset({"lease_lost", "lease_expired", "not_found", "unauthorized"})

#: Tools whose successful call by the holder already counts as a heartbeat on
#: the server ("any task_* call by the holder is a heartbeat").
_HOLDER_TOOLS = frozenset(
    {"task_heartbeat", "task_report", "handoff_request", "review_request"}
)

#: ``send(lease_id) -> envelope`` for one heartbeat.
Sender = Callable[[str], Mapping[str, Any]]


@dataclass(slots=True)
class _Lease:
    every: float
    due: float


class AutoHeartbeat:
    """Track held leases and send due heartbeats while the app is active."""

    def __init__(
        self,
        send: Sender,
        *,
        clock: Callable[[], float] = time.monotonic,
        idle_cutoff: float = IDLE_CUTOFF_SECONDS,
        default_interval: float = DEFAULT_HEARTBEAT_SECONDS,
    ) -> None:
        self._send = send
        self._clock = clock
        self._idle_cutoff = float(idle_cutoff)
        self._default_interval = float(default_interval)
        self._leases: dict[str, _Lease] = {}
        self._last_activity: float | None = None

    # -- state ---------------------------------------------------------------

    @property
    def leases(self) -> tuple[str, ...]:
        return tuple(sorted(self._leases))

    def is_idle(self) -> bool:
        if self._last_activity is None:
            return True
        return self._clock() - self._last_activity >= self._idle_cutoff

    def note_activity(self) -> None:
        """A tool call from the app (never the automatic heartbeat)."""
        self._last_activity = self._clock()

    def track(self, lease_id: str, every_seconds: Any = None) -> None:
        every = self._interval(every_seconds)
        self._leases[str(lease_id)] = _Lease(every=every, due=self._clock() + every)

    def forget(self, lease_id: str) -> None:
        self._leases.pop(str(lease_id), None)

    def touched(self, lease_id: str) -> None:
        """The holder just called a lease tool: the server counts it as a beat."""
        lease = self._leases.get(str(lease_id))
        if lease is not None:
            lease.due = self._clock() + lease.every

    def _interval(self, value: Any) -> float:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return self._default_interval
        # A server can shorten the interval, never make it silly.
        return seconds if 5.0 <= seconds <= 3600.0 else self._default_interval

    # -- bookkeeping from tool results --------------------------------------

    def observe(
        self,
        tool: str,
        arguments: Mapping[str, Any] | None,
        envelope: Mapping[str, Any],
    ) -> None:
        """Update the held-lease set from one tool call and its envelope."""
        args = arguments or {}
        lease_id = args.get("lease_id")
        ok = bool(envelope.get("ok")) and envelope.get("code") == "ok"
        data = envelope.get("data") if isinstance(envelope.get("data"), Mapping) else {}
        if not ok:
            if isinstance(lease_id, str) and envelope.get("code") in LEASE_ENDING_CODES:
                self.forget(lease_id)
            return
        if tool in {"task_claim", "handoff_accept"}:
            new_lease = data.get("lease_id")
            if isinstance(new_lease, str) and new_lease:
                self.track(new_lease, data.get("heartbeat_every_seconds"))
            return
        if not isinstance(lease_id, str):
            return
        if tool == "task_release":
            self.forget(lease_id)
            return
        if tool == "task_report":
            report = args.get("report") if isinstance(args.get("report"), Mapping) else {}
            reconciled = data.get("reconciled") if isinstance(data.get("reconciled"), Mapping) else {}
            if report.get("status") == "done" and not reconciled.get("vetoed"):
                # A done report that passed reconciliation ends the lease.
                self.forget(lease_id)
                return
        if tool in _HOLDER_TOOLS:
            self.touched(lease_id)

    # -- the beat -------------------------------------------------------------

    def tick(self) -> list[str]:
        """Send every due heartbeat unless idle; returns the lease ids sent."""
        if self.is_idle() or not self._leases:
            return []
        now = self._clock()
        sent: list[str] = []
        for lease_id, lease in list(self._leases.items()):
            if lease.due > now:
                continue
            try:
                envelope = self._send(lease_id)
            except Exception as exc:  # unreachable server: try again next interval
                logger.warning("collab heartbeat failed: %s", type(exc).__name__)
                lease.due = now + lease.every
                continue
            sent.append(lease_id)
            code = envelope.get("code") if isinstance(envelope, Mapping) else None
            if code in LEASE_ENDING_CODES:
                self.forget(lease_id)
            else:
                lease.due = now + lease.every
        return sent

    async def run(self, *, interval: float = TICK_SECONDS) -> None:
        """Tick until cancelled; the blocking HTTP call runs in a worker thread."""
        while True:
            await anyio.sleep(interval)
            if self.is_idle() or not self._leases:
                continue
            await anyio.to_thread.run_sync(self.tick)


__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "IDLE_CUTOFF_SECONDS",
    "LEASE_ENDING_CODES",
    "TICK_SECONDS",
    "AutoHeartbeat",
]
