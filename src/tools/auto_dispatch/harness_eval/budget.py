"""Append-only ledger that reserves a run or a review before it starts.

A reserve that never settles still counts. The next process, reading the same
file, cannot spend that slot again. Reviewer models are not counted toward
``max_models``; that cap is the worker pool.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from . import safe_io
from .types import BudgetSpec, BudgetState, Reserve


class Ledger:
    """``<output>/ledger.jsonl``. ``reserve`` writes nothing when it refuses."""

    def __init__(
        self,
        path: Path,
        budget: BudgetSpec,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.path = Path(path)
        self.budget = budget
        self._clock = clock or time.time
        self.events: list[dict[str, Any]] = []
        if self.path.is_file():
            self._load()

    def state(self) -> BudgetState:
        runs = self._reserves("run")
        reviews = self._reserves("review")
        started = [float(item["at"]) for item in runs + reviews if "at" in item]
        return BudgetState(
            runs_reserved=len(runs),
            reviews_reserved=len(reviews),
            models=frozenset(str(item.get("model") or "") for item in runs),
            wall_started=min(started) if started else None,
            max_runs=self.budget.max_runs,
            max_reviews=self.budget.max_reviews,
            max_models=self.budget.max_models,
            max_wall_minutes=self.budget.max_wall_minutes,
        )

    def reserve(self, kind: str, run8: str, model: str) -> Reserve:
        """Record a spend, or return a refusal without appending a line.

        The decision and the append happen under an exclusive lock on the
        ledger, after re-reading the file, so a second process using the
        same ``output_dir`` cannot spend a slot this one already took.
        """
        if kind not in {"run", "review"}:
            raise ValueError(f"unknown ledger kind {kind}")
        with self._locked():
            self._reload()
            return self._reserve_locked(kind, run8, model)

    def _reserve_locked(self, kind: str, run8: str, model: str) -> Reserve:
        # A run can last up to worker_timeout_seconds once launched, so it is
        # admitted only when that worst case still fits in the wall budget.
        worst_case = self.budget.worker_timeout_seconds if kind == "run" else 0
        if self._wall_exceeded(worst_case):
            return Reserve(False, "max_wall_minutes")
        if kind == "run":
            runs = self._reserves("run")
            if len(runs) >= self.budget.max_runs:
                return Reserve(False, "max_runs")
            models = {str(item.get("model") or "") for item in runs}
            models.add(model)
            models.discard("")
            if len(models) > self.budget.max_models:
                return Reserve(False, "max_models")
        else:
            if len(self._reserves("review")) >= self.budget.max_reviews:
                return Reserve(False, "max_reviews")
        self._append(
            {
                "event": "reserve",
                "kind": kind,
                "run8": run8,
                "model": model,
                "at": self._clock(),
            }
        )
        return Reserve(True, None)

    def settle(
        self,
        kind: str,
        run8: str,
        *,
        result: str,
        tokens: int,
        duration_secs: float,
    ) -> None:
        with self._locked():
            self._reload()
            self._append_unlocked(
                {
                    "event": "settle",
                    "kind": kind,
                    "run8": run8,
                    "result": result,
                    "tokens": int(tokens),
                    "duration_secs": float(duration_secs),
                    "at": self._clock(),
                }
            )

    def _reserves(self, kind: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self.events
            if item.get("event") == "reserve" and item.get("kind") == kind
        ]

    def _wall_exceeded(self, worst_case_seconds: float = 0) -> bool:
        times = [
            float(item["at"])
            for item in self.events
            if item.get("event") == "reserve" and "at" in item
        ]
        limit = self.budget.max_wall_minutes * 60
        if not times:
            return worst_case_seconds > limit
        elapsed = self._clock() - min(times)
        if elapsed >= limit:
            return True
        return elapsed + worst_case_seconds > limit

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.path.with_name(self.path.name + ".lock")
        fd = safe_io.open_lock(self.path.parent, lock_path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _reload(self) -> None:
        self.events = []
        if self.path.is_file():
            self._load()

    def _load(self) -> None:
        # A torn last line (a crash mid-append) is skipped, not fatal: a
        # reserve is written before its launch, so a torn reserve never ran.
        rows, _skipped = safe_io.parse_jsonl(self.path.read_text(encoding="utf-8"))
        self.events.extend(item for item in rows if isinstance(item, dict))

    def _append(self, event: dict[str, Any]) -> None:
        self._append_unlocked(event)

    def _append_unlocked(self, event: dict[str, Any]) -> None:
        safe_io.append_line(self.path.parent, self.path, json.dumps(event, sort_keys=True))
        self.events.append(event)
