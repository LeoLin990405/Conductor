"""Drain guard: hold every new Worker process while ``cond drain`` is set.

Once lines run in parallel no quiet window ever comes, so a deploy asks the
dispatch side to stop starting work instead. The guard wraps the real
launcher (``ScriptLauncher``) where ``DispatchService`` builds it; the machine
and the launcher are unchanged.

Why the guard *waits* rather than declining: the machine has no "not now"
answer a launcher can give. A ``LaunchError`` is retried ``launch_retries``
times and then gives the launch up, and a classified ``WorkerLaunchFailure``
spends recovery budget or moves the line off its provider. A slow launch, on
the other hand, is normal: ``launch`` / ``launch_worker`` run the Worker to
completion, and the line stays ASSIGNED with its reserved run id until the call
returns. So a drained launch is a launch that has not reached its spawn yet:
no process, no Worker log, no attempt spent, and it continues by itself after
``cond undrain``. If the backend restarts first, the resumed drive sees what it
sees after a restart during worktree preparation: a launch that never spawned.

Two points are held: the top of the call (before the worktree is prepared) and
the machine's ``before_spawn`` hook (a drain set during preparation still
stops the spawn; the hook's epoch fence runs after the hold, so a drive that
lost ownership while it waited does not spawn). A launch that was held at the
top runs that hook once more before it reaches the inner launcher, so a drive
that lost ownership while held does not even prepare a worktree. Fix rounds go
through the same
``launch_worker`` call and are covered. The proactive self-report asks a CLI
turn of its own and is held the same way.

Monitoring, G2, review and merge do not call these methods and keep running;
``cancel`` and every other attribute pass straight through.

While a ``launch`` / ``launch_worker`` call is past the hold it holds an
``activity.busy(LAUNCH)`` marker, which ``cond restart --drain`` waits for. The
marker is never held while the call is held (top of call, or the spawn hold),
or the restart would wait on launches that wait on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from src.conductor_cli import activity
from src.conductor_cli.drain_owner import drained_live

logger = logging.getLogger(__name__)

DRAIN_POLL_SECONDS = 5.0

#: Launcher methods that start a Worker CLI process.
_HELD_METHODS = ("launch", "launch_worker", "request_self_report")
#: Of those, the calls that hold a busy marker while they run.
_MARKED_METHODS = frozenset({"launch", "launch_worker"})

BusyFactory = Callable[[str, str], contextlib.AbstractContextManager[Any]]


class _Marker:
    """A busy marker that can be dropped for a hold and taken again."""

    def __init__(self, factory: BusyFactory | None, detail: str) -> None:
        self._factory = factory
        self._detail = detail
        self._cm: contextlib.AbstractContextManager[Any] | None = None

    def acquire(self) -> None:
        if self._factory is None or self._cm is not None:
            return
        cm = self._factory(activity.LAUNCH, self._detail)
        cm.__enter__()
        self._cm = cm

    def release(self) -> None:
        cm, self._cm = self._cm, None
        if cm is not None:
            cm.__exit__(None, None, None)


class DrainAwareLauncher:
    """Delegate to ``inner``; hold process starts while the drain flag is set."""

    def __init__(
        self,
        inner: Any,
        *,
        home: Path | None = None,
        drained: Callable[[], bool] | None = None,
        poll_seconds: float = DRAIN_POLL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        busy: BusyFactory | None = None,
    ) -> None:
        self._inner = inner
        self._drained = drained or (lambda: drained_live(home))
        self._busy: BusyFactory = busy or (
            lambda kind, detail: activity.busy(kind, detail, home=home)
        )
        self._poll = max(0.01, float(poll_seconds))
        self._sleep = sleep or asyncio.sleep
        # Only the methods the inner launcher has: the machine probes
        # ``launch_worker`` / ``request_self_report`` with getattr, and the
        # signature is copied because it probes keywords such as
        # ``before_spawn`` on the callable it is handed.
        for name in _HELD_METHODS:
            method = getattr(inner, name, None)
            if callable(method):
                setattr(self, name, self._held(name, method))

    @property
    def inner(self) -> Any:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not set above: cancel, capacity_for,
        # cleanup_worktrees, pin_base_commit, ...
        if name.startswith("__") or name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)

    async def wait_until_undrained(self, line: Any = None) -> bool:
        """Return at once when not drained; otherwise poll until undrained.

        True when it actually held (waited at least once).
        """
        noted = False
        while self._drained():
            if not noted:
                logger.info(
                    "Drain is set: line %s waits before starting a Worker",
                    getattr(line, "id", "?"),
                )
                noted = True
            await self._sleep(self._poll)
        if noted:
            logger.info("Drain cleared: line %s continues", getattr(line, "id", "?"))
        return noted

    def _held(self, name: str, method: Callable[..., Any]) -> Callable[..., Any]:
        marked = name in _MARKED_METHODS

        async def held(line: Any, *args: Any, **kwargs: Any) -> Any:
            waited = await self.wait_until_undrained(line)
            hook = kwargs.get("before_spawn")
            marker = _Marker(
                self._busy if marked else None, str(getattr(line, "id", "") or "")
            )
            if callable(hook):

                async def guarded_spawn() -> None:
                    if self._drained():
                        # Held at the spawn: drop the marker for the hold.
                        marker.release()
                        try:
                            await self.wait_until_undrained(line)
                        finally:
                            marker.acquire()
                    await hook()

                kwargs["before_spawn"] = guarded_spawn
            marker.acquire()
            try:
                if waited and callable(hook):
                    # The epoch fence, before the worktree is prepared: a
                    # drive that lost ownership while held stops here.
                    await hook()
                return await method(line, *args, **kwargs)
            finally:
                marker.release()

        held.__name__ = name
        held.__qualname__ = f"DrainAwareLauncher.{name}"
        try:
            held.__signature__ = inspect.signature(method)  # type: ignore[attr-defined]
        except (TypeError, ValueError):
            pass
        return held


def drain_aware(inner: Any, **kwargs: Any) -> DrainAwareLauncher:
    """Wrap ``inner`` once; an already wrapped launcher is returned as is."""
    if isinstance(inner, DrainAwareLauncher):
        return inner
    return DrainAwareLauncher(inner, **kwargs)
