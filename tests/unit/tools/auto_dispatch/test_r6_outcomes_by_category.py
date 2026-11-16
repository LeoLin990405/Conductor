"""R6 L4: success per category x model x version; old rows keep their shape."""

from __future__ import annotations

import pytest

from src.server.services.autonomy.role_types import (
    ModelRef,
    RoleEntry,
    RoleRegistry,
    fallback_registry,
    pinned_version,
)
from src.tools.auto_dispatch.outcomes import InMemoryMemoryPort, MemoryP0OutcomeStore
from src.tools.auto_dispatch.types import Outcome



def _o(line: str, model: str, passed: bool, category: str = "", version: str = "") -> Outcome:
    return Outcome(line, model, 1, 0, passed, category=category, model_version=version)


def test_outcome_dict_shape_is_unchanged_without_category() -> None:
    data = Outcome("l", "kimi", 1, 0, True).to_dict()
    assert "category" not in data and "model_version" not in data
    full = _o("l", "kimi", True, "web", "2.5").to_dict()
    assert Outcome.from_dict(full).category == "web"
    assert Outcome.from_dict(full).model_version == "2.5"


@pytest.mark.asyncio
async def test_counts_split_by_category_and_version() -> None:
    store = MemoryP0OutcomeStore(InMemoryMemoryPort())
    await store.record(
        "d1",
        {
            "a": _o("a", "kimi", True, "web"),
            "b": _o("b", "kimi", False, "docs"),
            "c": _o("c", "kimi", True, "web", "2.5"),
            "d": _o("d", "glm", True),
        },
    )
    assert await store.category_counts("kimi") == (2, 3)
    assert await store.category_counts("kimi", category="web") == (2, 2)
    assert await store.category_counts("kimi", category="web", version="2.5") == (1, 1)
    assert await store.category_counts("kimi", version="latest") == (1, 2)
    assert await store.success_rate("kimi") == 2 / 3
    table = await store.category_table("web")
    assert table["category"] == {"kimi": (2, 2)}
    assert table["all"]["glm"] == (1, 1)


def test_pinned_version_reads_registry() -> None:
    base = fallback_registry({}, router_model="r")
    worker = base.entry("worker")
    pinned = RoleEntry(
        "worker",
        (ModelRef.parse("kimi@2.5"), *worker.candidates[1:]),
        ModelRef.parse("kimi@2.5"),
        worker.constraints,
    )
    registry: RoleRegistry = base.with_entry(pinned)
    assert pinned_version("worker", "kimi", registry) == "2.5"
    assert pinned_version("worker", "glm", registry) == ""
    assert pinned_version("worker", "kimi", base) == ""


class _Store:
    def __init__(self, dispatches):
        self._dispatches = dispatches

    async def list(self, limit: int = 500):
        return self._dispatches[:limit]


@pytest.mark.asyncio
async def test_saved_rows_get_their_category_from_the_line() -> None:
    from src.tools.auto_dispatch.outcomes import DispatchRowMemoryPort
    from src.tools.auto_dispatch.types import Dispatch, Line

    line = Line(id="a", name="a", task="t", write_scope=("web/src/x.ts",))
    dispatch = Dispatch(id="d1", goal="g", user_id="u", created_at="now", lines=[line])
    dispatch.outcomes["a"] = Outcome("a", "kimi", 1, 0, True)
    # The stored row keeps main's shape.
    assert "category" not in dispatch.outcomes["a"].to_dict()
    store = MemoryP0OutcomeStore(DispatchRowMemoryPort(_Store([dispatch])))
    assert await store.category_counts("kimi", category="web") == (1, 1)
    # A process write for the same key keeps the saved line's category.
    await store.record("d1", {"a": Outcome("a", "kimi", 1, 0, False)})
    assert await store.category_counts("kimi", category="web") == (0, 1)
