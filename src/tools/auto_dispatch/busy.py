"""Busy markers around the dispatch work a drained restart must wait for (N64).

``cond restart --drain`` holds new Worker launches, then waits for the work
already under way: G2 and G5 (the gate runner holds its own marker), reviewer
CLI calls and merges. The reviewer and the merger live in ``execution.py``,
which this round does not change, so :class:`BusyProxy` wraps them where
``DispatchService`` builds them: each coroutine method runs under a marker,
and every other attribute passes straight through.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
from collections.abc import Callable
from typing import Any

BusyFactory = Callable[[str, str], contextlib.AbstractContextManager[object]]


class BusyProxy:
    """Delegate to ``inner``; run each coroutine method under ``busy(kind, name)``."""

    def __init__(self, inner: Any, kind: str, busy: BusyFactory) -> None:
        self._inner = inner
        self._kind = kind
        self._busy = busy

    @property
    def inner(self) -> Any:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or name in ("_inner", "_kind", "_busy"):
            raise AttributeError(name)
        value = getattr(self._inner, name)
        if not inspect.iscoroutinefunction(value):
            return value

        @functools.wraps(value)
        async def marked(*args: Any, **kwargs: Any) -> Any:
            with self._busy(self._kind, name):
                return await value(*args, **kwargs)

        # The machine probes keywords on the callables it is handed.
        with contextlib.suppress(TypeError, ValueError):
            marked.__signature__ = inspect.signature(value)  # type: ignore[attr-defined]
        return marked
