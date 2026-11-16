"""Recording what a dispatch cost, and feeding it back into model choice.

The destination is memory-p0, which is not merged yet, so the store writes
through a ``MemoryPort`` — the same boundary the WP12 memory work draws at
``propose``/``decide``/``recall``. ``InMemoryMemoryPort`` stands in until the
real adapter lands; nothing else in this package knows the difference.

``DispatchRowMemoryPort`` also reads outcomes that the dispatch row already
stores in ``dispatches.outcomes``. That column is the durable copy, so a
restart can still compute a success rate without a new table. The read is
capped at the store's list limit (500 newest rows). A dedicated rollup past
that cap is v2. Writes stay in the process until ``record`` puts them; a
later search unions those writes with the saved rows and keeps one row per
dispatch line.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from .task_category import line_category
from .types import Outcome

logger = logging.getLogger(__name__)

COLLECTION = "auto_dispatch_outcomes"

#: A line that ended on one of these was never judged: the reviewer's output
#: was unusable. It is neither a pass nor a failed run of the author's model.
INCONCLUSIVE_REASONS = frozenset({"review_inconclusive", "review_unparseable"})


def _exclusion_on() -> bool:
    """Inconclusive rows are set apart only in strict-review ``enforce``.

    ``off`` and ``shadow`` count every row exactly as before P0, so the
    selector's recorded rate does not move until enforce is switched on.
    """
    from .review_strict import strict_mode

    return strict_mode() == "enforce"


def _inconclusive(row: dict, *, exclusion: bool | None = None) -> bool:
    if exclusion is None:
        exclusion = _exclusion_on()
    return (
        exclusion
        and not row.get("passed")
        and str(row.get("failure_reason") or "") in INCONCLUSIVE_REASONS
    )


def _counted(row: dict, *, exclusion: bool) -> bool:
    """A row that is a judged run: it has ``passed`` and was not inconclusive."""
    return "passed" in row and not _inconclusive(row, exclusion=exclusion)


class MemoryPort(Protocol):
    """The WP12 memory boundary, narrowed to what outcome recording needs."""

    async def put(self, collection: str, key: str, value: dict) -> None: ...

    async def get(self, collection: str, key: str) -> dict | None: ...

    async def search(self, collection: str, *, limit: int = 500) -> list[dict]: ...


class InMemoryMemoryPort:
    """A stand-in for memory-p0: same shape, no durability, no network."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, dict]] = {}

    async def put(self, collection: str, key: str, value: dict) -> None:
        self._rows.setdefault(collection, {})[key] = dict(value)

    async def get(self, collection: str, key: str) -> dict | None:
        row = self._rows.get(collection, {}).get(key)
        return dict(row) if row is not None else None

    async def search(self, collection: str, *, limit: int = 500) -> list[dict]:
        rows = list(self._rows.get(collection, {}).values())
        return [dict(row) for row in rows[:limit]]


class MemoryP0OutcomeStore:
    """Writes each dispatch's outcomes and derives per-model success rates.

    The rollup is what makes the next dispatch smarter: a model that keeps
    passing review outranks an untried one with equal quota headroom.
    """

    def __init__(self, port: MemoryPort, *, collection: str = COLLECTION) -> None:
        self._port = port
        self._collection = collection

    async def record(self, dispatch_id: str, outcomes: dict[str, Outcome]) -> None:
        for line_id, outcome in outcomes.items():
            await self._port.put(
                self._collection, f"{dispatch_id}/{line_id}", outcome.to_dict()
            )
        await self._refresh_rollup()

    async def success_rate(self, model: str) -> float | None:
        rows = await self._port.search(self._collection)
        exclusion = _exclusion_on()
        runs = [
            row
            for row in rows
            if row.get("model") == model and _counted(row, exclusion=exclusion)
        ]
        if not runs:
            return None
        return sum(1 for row in runs if row.get("passed")) / len(runs)

    async def category_counts(
        self, model: str, *, category: str | None = None, version: str | None = None
    ) -> tuple[int, int]:
        """``(passed, runs)`` for ``model``, optionally on one category / version.

        R6 L4 (N5): success is counted per task category x model, and per
        model version when the registry pins one. ``version`` ``None`` counts
        every version; ``"latest"`` (or ``""``) counts rows with no recorded
        version. Rows written before R6 have no category and count only in
        the all-categories total.
        """
        rows = await self._port.search(self._collection)
        passed = runs = 0
        want_version = None if version is None else ("" if version == "latest" else version)
        exclusion = _exclusion_on()
        for row in rows:
            if row.get("model") != model or not _counted(row, exclusion=exclusion):
                continue
            if category is not None and str(row.get("category") or "") != category:
                continue
            if want_version is not None and str(row.get("model_version") or "") != want_version:
                continue
            runs += 1
            passed += 1 if row.get("passed") else 0
        return passed, runs

    async def category_table(
        self, category: str | None = None
    ) -> dict[str, dict[str, tuple[int, int]]]:
        """One read: ``{"category": {model: (passed, runs)}, "all": {...}}``.

        The selector asks once per selection instead of once per candidate.
        """
        rows = await self._port.search(self._collection)
        table: dict[str, dict[str, tuple[int, int]]] = {"category": {}, "all": {}}
        exclusion = _exclusion_on()
        for row in rows:
            model = str(row.get("model") or "")
            if not model or not _counted(row, exclusion=exclusion):
                continue
            ok = 1 if row.get("passed") else 0
            passed, runs = table["all"].get(model, (0, 0))
            table["all"][model] = (passed + ok, runs + 1)
            if category is not None and str(row.get("category") or "") == category:
                passed, runs = table["category"].get(model, (0, 0))
                table["category"][model] = (passed + ok, runs + 1)
        return table

    async def _refresh_rollup(self) -> None:
        rows = await self._port.search(self._collection)
        stats: dict[str, dict[str, int]] = {}
        exclusion = _exclusion_on()
        for row in rows:
            model = str(row.get("model") or "")
            if not model or "passed" not in row:
                continue
            bucket = stats.setdefault(model, {"runs": 0, "passed": 0})
            if _inconclusive(row, exclusion=exclusion):
                # Counted apart: never a run, a pass or a failure.
                bucket["inconclusive"] = bucket.get("inconclusive", 0) + 1
                continue
            bucket["runs"] += 1
            bucket["passed"] += 1 if row.get("passed") else 0
            category = str(row.get("category") or "")
            if category:
                # Additive per-category rollup; the per-model keys are unchanged.
                by_cat = bucket.setdefault("by_category", {}).setdefault(
                    category, {"runs": 0, "passed": 0}
                )
                by_cat["runs"] += 1
                by_cat["passed"] += 1 if row.get("passed") else 0
        await self._port.put(self._collection, "_models", stats)


class DispatchRowMemoryPort:
    """MemoryPort that unions process writes with saved dispatch outcomes.

    ``store`` is a ``DispatchStore``. ``list`` supplies the rows already
    written to ``dispatches.outcomes`` (or the in-memory stand-in). No
    migration is added. ``get`` and ``put`` stay process-local except that
    ``get`` can also read one saved dispatch by the ``{id}/{line}`` key.
    """

    def __init__(self, store: Any, *, limit: int = 500) -> None:
        self._store = store
        self._rows: dict[str, dict] = {}
        self._limit = limit

    async def put(self, collection: str, key: str, value: dict) -> None:
        self._rows[key] = dict(value)

    async def get(self, collection: str, key: str) -> dict | None:
        row = self._rows.get(key)
        if row is not None:
            return dict(row)
        dispatch_id, separator, line_id = key.partition("/")
        if not separator or not hasattr(self._store, "get"):
            return None
        dispatch = await self._store.get(dispatch_id)
        if dispatch is None:
            return None
        outcome = dispatch.outcomes.get(line_id)
        return outcome.to_dict() if outcome is not None else None

    async def search(self, collection: str, *, limit: int = 500) -> list[dict]:
        merged: dict[str, dict] = {}
        cap = max(1, min(limit, self._limit))
        for dispatch in await self._store.list(limit=cap):
            for line_id, outcome in dispatch.outcomes.items():
                to_dict = getattr(outcome, "to_dict", None)
                if to_dict is None:
                    continue
                row = to_dict()
                # R6 L4: the category is derived from the saved line, so the
                # stored outcome keeps main's shape.
                line = dispatch.line(line_id) if hasattr(dispatch, "line") else None
                if line is not None and not row.get("category"):
                    row["category"] = line_category(line)
                merged[f"{dispatch.id}/{line_id}"] = row
        # A record() of a row the store already holds overwrites that key,
        # so the same line is not counted twice; a category the saved line
        # supplied is kept.
        for key, value in self._rows.items():
            saved = merged.get(key) or {}
            row = dict(value)
            if not row.get("category") and saved.get("category"):
                row["category"] = saved["category"]
            merged[key] = row
        return list(merged.values())[:limit]


def outcome_models(outcomes: dict[str, Outcome]) -> dict[str, int]:
    """Per-model attempt counts, for the outcome summary a human reads."""
    counts: dict[str, int] = {}
    for outcome in outcomes.values():
        counts[outcome.model] = counts.get(outcome.model, 0) + 1
    return counts
