"""Shared fakes for the auto-dispatch state machine tests.

Everything here is in-process and deterministic: no clock, no subprocess, no
socket. The state machine takes its sleep and clock as callables, so a whole
dispatch runs in microseconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.tools.auto_dispatch.protocols import LaunchError
from src.tools.auto_dispatch.types import Line, RunStatus, Verdict


@dataclass
class Recorder:
    """A sleep that records what it was asked to wait for."""

    waits: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


@dataclass
class Clock:
    """A clock the test advances by hand."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FailingLauncher:
    """Raises for the first ``fail_times`` launches of each line, then works."""

    def __init__(self, *, fail_times: int = 1, always: bool = False) -> None:
        self.fail_times = fail_times
        self.always = always
        self.attempts: dict[str, int] = {}
        self.launches: list[tuple[str, str]] = []

    async def launch(self, line: Line, model: str, skills: list[str]) -> str:
        seen = self.attempts.get(line.id, 0)
        self.attempts[line.id] = seen + 1
        self.launches.append((line.id, model))
        if self.always or seen < self.fail_times:
            raise LaunchError(f"{model} could not start line {line.id}")
        return f"run-{line.id}-{seen + 1}"


class BlockingReviewer:
    """Fails the lines it is told to fail, the first time it sees each one.

    A second review of the same line passes, so a human reassignment can be
    driven to the gate without a reviewer that blocks forever.
    """

    def __init__(self, blocked: dict[str, list[str]] | None = None) -> None:
        self.blocked = blocked or {}
        self.reviewed: list[tuple[str, str]] = []
        self._seen: dict[str, int] = {}

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.reviewed.append((line_id, model))
        seen = self._seen.get(line_id, 0)
        self._seen[line_id] = seen + 1
        blockers = self.blocked.get(line_id) if seen == 0 else None
        if blockers:
            return Verdict.blocked(blockers)
        return Verdict.ok(f"reviewed by {model}")


def stalled(run_id: str) -> dict[str, list[RunStatus]]:
    """A monitor script that stalls a run once, then lets it finish."""
    return {run_id: [RunStatus(output="", stalled=True)]}
