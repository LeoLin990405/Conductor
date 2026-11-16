"""Assignment inputs are assembled from lines and an in-memory outcome port.

No database and no network. The port is a fake that only implements the
MemoryPort methods the reader is allowed to call.
"""

from __future__ import annotations

import pytest

from src.tools.auto_dispatch.outcomes import (
    COLLECTION,
    InMemoryMemoryPort,
    MemoryP0OutcomeStore,
)
from src.tools.auto_dispatch.superagent_assign.context import (
    AreaRate,
    ProviderRate,
    acceptance_command_count,
    area_bucket,
    area_rates,
    assemble_inputs,
    bound_task_text,
    dependency_depths,
    success_rate_by_area,
)
from src.tools.auto_dispatch.types import Line, Outcome

_SECRET = "super-secret-token-value"


class RecordingPort:
    """In-memory MemoryPort. ``put`` fails the test if the reader writes."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = list(rows or [])
        self.puts = 0
        self.searches: list[tuple[str, int]] = []

    async def put(self, collection: str, key: str, value: dict) -> None:
        self.puts += 1
        raise AssertionError("outcome rates are read-only")

    async def get(self, collection: str, key: str) -> dict | None:
        raise AssertionError("outcome rates do not fetch one row")

    async def search(self, collection: str, *, limit: int = 500) -> list[object]:
        self.searches.append((collection, limit))
        copied: list[object] = []
        for row in self.rows[:limit]:
            # Non-dicts are handed through so the reader has to skip them.
            copied.append(dict(row) if isinstance(row, dict) else row)
        return copied


class BoomPort(RecordingPort):
    async def search(self, collection: str, *, limit: int = 500) -> list[object]:
        raise RuntimeError("outcome store unavailable")


def _line(line_id: str, **kwargs: object) -> Line:
    fields: dict[str, object] = {"id": line_id, "name": line_id, "task": "do the work"}
    fields.update(kwargs)
    return Line(**fields)  # type: ignore[arg-type]


def _outcome(line_id: str, model: str, passed: bool) -> Outcome:
    return Outcome(
        line_id=line_id,
        model=model,
        duration_secs=12,
        tokens=100,
        passed=passed,
        reviewer_verdict="pass" if passed else "blocked",
    )


def test_area_buckets_follow_the_tree() -> None:
    assert (
        area_bucket("src/tools/auto_dispatch/machine.py") == "src/tools/auto_dispatch"
    )
    assert area_bucket("src/tools/auto_dispatch/**") == "src/tools/auto_dispatch"
    assert area_bucket("src/llms/native_cli/isolation.py") == "src/llms/native_cli"
    assert area_bucket("web/src/App.tsx") == "web/"
    assert area_bucket("web/**") == "web/"
    assert area_bucket("./web/src/App.tsx") == "web/"
    assert area_bucket("scripts/dispatch_launch.sh") == "scripts/"
    assert area_bucket("migrations/versions/121_dispatch.py") == "migrations/"
    assert area_bucket("ｗｅｂ/src/App.tsx") == "web/"
    assert area_bucket("web/\u200bsrc/App.tsx") == "web/"
    assert area_bucket("**") == ""
    assert area_bucket("../outside") == ""
    assert area_bucket("") == ""
    # Redaction runs on the raw scope, before the slash split. The value
    # swallows the following segment, and the redacted tail is not a directory.
    secret_area = area_bucket(f"src/api_key={_SECRET}/machine.py")
    assert _SECRET not in secret_area
    assert secret_area == "src"


def test_task_text_is_redacted_before_it_is_cut() -> None:
    password = _SECRET
    # The cut falls inside the password when truncation happens first, so
    # the userinfo regex would no longer see the '@' and a fragment would
    # remain. Redaction has to win.
    prefix = "see https://user:"
    task = prefix + password + "@example.com/" + ("x" * 80)
    limit = len(prefix) + 8
    text, truncated = bound_task_text(task, limit=limit)
    assert truncated
    assert password not in text
    # Eight characters of the password sit inside the cut. They must not
    # survive: redaction removes the whole userinfo before the cut.
    assert password[:8] not in text
    assert text.startswith("see https://")
    assert len(text.encode("utf-8")) <= limit


def test_a_whole_secret_inside_the_kept_prefix_is_redacted() -> None:
    task = f"Bearer {_SECRET} and then the work"
    text, truncated = bound_task_text(task, limit=8_000)
    assert truncated is False
    assert _SECRET not in text
    assert "[redacted]" in text


def test_fullwidth_bearer_is_normalized_before_redaction() -> None:
    text, truncated = bound_task_text(f"Ｂｅａｒｅｒ {_SECRET}")
    assert truncated is False
    assert _SECRET not in text
    assert "[redacted]" in text


@pytest.mark.asyncio
async def test_assemble_redacts_truncates_and_buckets_each_line() -> None:
    long_task = f"Bearer {_SECRET}\n" + ("implement the feature " * 500)
    lines = [
        _line(
            "line-0",
            task=long_task,
            write_scope=(
                "src/tools/auto_dispatch/machine.py",
                "src/tools/auto_dispatch/backend.py",
                "web/src/App.tsx",
                f"web/src/Bearer {_SECRET}.tsx",
                "  ",
            ),
            acceptance=(
                "cmd: pytest tests/unit/a.py",
                "reviewer reads the diff",
                "cmd:",
                "cmd: pytest tests/unit/b.py",
            ),
            depends_on=("line-root",),
            risk_bearing=True,
            estimated_size="Large",
        )
    ]
    inputs = await assemble_inputs(lines, RecordingPort())
    assert len(inputs.lines) == 1
    line = inputs.lines[0]
    assert _SECRET not in line.task_text
    assert "[redacted]" in line.task_text
    assert line.task_truncated is True
    assert len(line.task_text.encode("utf-8")) <= 8_000
    assert line.areas == ("src/tools/auto_dispatch", "web/")
    assert line.write_scope[:3] == (
        "src/tools/auto_dispatch/machine.py",
        "src/tools/auto_dispatch/backend.py",
        "web/src/App.tsx",
    )
    assert line.write_scope[3].startswith("web/src/Bearer [redacted]")
    assert _SECRET not in " ".join(line.write_scope)
    assert line.acceptance_count == 2
    assert line.depends_on == ("line-root",)
    assert line.difficulty.scope_entries == 4
    assert line.difficulty.directories == ("src/tools/auto_dispatch", "web/src")
    assert line.difficulty.risk_bearing is True
    assert line.difficulty.planner_large is True
    assert "\n" not in " ".join(line.write_scope)


@pytest.mark.asyncio
async def test_fullwidth_scope_secret_is_redacted_before_storage() -> None:
    lines = [_line("line-0", write_scope=(f"web/Ｂｅａｒｅｒ {_SECRET}.tsx",))]
    inputs = await assemble_inputs(lines, RecordingPort())
    stored = inputs.lines[0].write_scope[0]
    assert _SECRET not in stored
    assert stored.startswith("web/Bearer [redacted]")


@pytest.mark.asyncio
async def test_difficulty_counts_scope_entries_and_dag_depth() -> None:
    lines = [
        _line("a", write_scope=("src/a.py",), estimated_size="small"),
        _line("b", write_scope=("src/b.py", "**"), depends_on=("a",)),
        _line("c", write_scope=("web/c.tsx",), depends_on=("b",), risk_bearing=False),
        _line(
            "d", write_scope=("src/server/services/dispatch.py",), depends_on=("a", "c")
        ),
    ]
    inputs = await assemble_inputs(lines, RecordingPort())
    by_id = {line.line_id: line for line in inputs.lines}
    assert by_id["a"].difficulty.dag_depth == 0
    assert by_id["b"].difficulty.dag_depth == 1
    assert by_id["c"].difficulty.dag_depth == 2
    assert by_id["d"].difficulty.dag_depth == 3
    assert by_id["b"].difficulty.scope_entries == 2
    assert by_id["b"].areas == ("src",)
    assert by_id["a"].difficulty.planner_large is False
    assert by_id["d"].areas == ("src/server/services",)
    assert by_id["d"].difficulty.directories == ("src/server/services",)


def test_a_dependency_cycle_adds_nothing() -> None:
    lines = [
        _line("a", depends_on=("b",)),
        _line("b", depends_on=("a",)),
    ]
    assert dependency_depths(lines) == {"a": 0, "b": 0}
    # Entering the cycle is one hop. The cycle itself does not add another,
    # and the two members do not get different depths.
    fed = [
        _line("root"),
        _line("left", depends_on=("root", "right")),
        _line("right", depends_on=("left",)),
    ]
    assert dependency_depths(fed) == {"root": 0, "left": 1, "right": 1}


def test_a_missing_dependency_is_one_hop() -> None:
    assert dependency_depths([_line("a", depends_on=("gone",))]) == {"a": 1}
    assert acceptance_command_count(("cmd: pytest -q", "looks fine", "cmd:")) == 1


@pytest.mark.asyncio
async def test_success_rates_read_the_same_outcome_rows_and_nothing_else() -> None:
    rows = [
        {
            **_outcome("l0", "stepfun", True).to_dict(),
            "write_scope": ["web/src/App.tsx"],
        },
        {
            **_outcome("l1", "stepfun", True).to_dict(),
            "write_scope": ["web/src/Other.tsx"],
        },
        {
            **_outcome("l2", "stepfun", False).to_dict(),
            "write_scope": ["web/src/Bad.tsx"],
        },
        {
            **_outcome("l3", "grok", True).to_dict(),
            "write_scope": ["src/llms/native_cli/isolation.py"],
        },
        # A plain outcome row has no area. It still counts for the provider.
        _outcome("l4", "kimi", True).to_dict(),
        # One line that touched two areas counts once per area, once overall.
        {
            **_outcome("l5", "glm", True).to_dict(),
            "write_scope": [
                "web/src/App.tsx",
                "src/llms/native_cli/x.py",
                "web/src/Again.tsx",
            ],
        },
        # The rollup row the outcome store writes has no line outcome shape.
        {"stepfun": {"runs": 99, "passed": 99}},
        "not-a-row",
    ]
    port = RecordingPort(rows)
    inputs = await assemble_inputs([_line("line-0")], port)

    assert port.puts == 0
    assert port.searches == [(COLLECTION, 500)]
    assert inputs.success_by_provider == (
        ProviderRate("glm", 1, 1),
        ProviderRate("grok", 1, 1),
        ProviderRate("kimi", 1, 1),
        ProviderRate("stepfun", 3, 2),
    )
    assert inputs.success_rate_by_area == (
        AreaRate("glm", "src/llms/native_cli", 1, 1),
        AreaRate("glm", "web/", 1, 1),
        AreaRate("grok", "src/llms/native_cli", 1, 1),
        AreaRate("stepfun", "web/", 3, 2),
    )
    stepfun_web = inputs.success_rate_by_area[-1]
    assert stepfun_web.rate == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_in_memory_port_matches_the_outcome_store_and_skips_the_rollup() -> None:
    port = InMemoryMemoryPort()
    store = MemoryP0OutcomeStore(port)
    await store.record(
        "d1",
        {
            "line-0": _outcome("line-0", "stepfun", True),
            "line-1": _outcome("line-1", "stepfun", False),
        },
    )
    # A later recorder can attach a scope without a new table. The reader
    # only searches; this put is the test arranging that row.
    saved = await port.get(COLLECTION, "d1/line-0")
    assert saved is not None
    saved["write_scope"] = ["src/tools/auto_dispatch/machine.py"]
    await port.put(COLLECTION, "d1/line-0", saved)

    rates = await success_rate_by_area(port)
    inputs = await assemble_inputs([], port)

    assert rates == (AreaRate("stepfun", "src/tools/auto_dispatch", 1, 1),)
    assert inputs.success_rate_by_area == rates
    assert inputs.success_by_provider == (ProviderRate("stepfun", 2, 1),)
    assert await store.success_rate("stepfun") == 0.5
    assert inputs.lines == ()


@pytest.mark.asyncio
async def test_a_secret_directory_does_not_become_an_area_label() -> None:
    port = RecordingPort(
        [
            {
                **_outcome("l0", "stepfun", True).to_dict(),
                "write_scope": [f"src/api_key={_SECRET}/machine.py"],
            },
            {
                **_outcome("l1", f"api_key={_SECRET}", False).to_dict(),
                "area": "web/src/App.tsx",
            },
        ]
    )
    rates = await success_rate_by_area(port)
    rendered = " ".join(f"{item.model} {item.area}" for item in rates)
    assert _SECRET not in rendered
    assert rates == (
        AreaRate("api_key=[redacted]", "web/", 1, 0),
        AreaRate("stepfun", "src", 1, 1),
    )


def test_a_credential_url_is_redacted_before_the_scope_is_split() -> None:
    secret = "canary-password"
    scope = f"src/https://alice:{secret}@example.com/x.py"
    rates = area_rates([{"model": "stepfun", "passed": True, "write_scope": [scope]}])
    assert secret not in repr(rates)
    assert rates == (AreaRate("stepfun", "src/https:/[redacted]@example.com", 1, 1),)


@pytest.mark.asyncio
async def test_explicit_area_wins_over_write_scope() -> None:
    port = RecordingPort(
        [
            {
                **_outcome("l0", "stepfun", True).to_dict(),
                "area": "web/src/App.tsx",
                "write_scope": ["src/llms/native_cli/x.py"],
            }
        ]
    )
    rates = await success_rate_by_area(port)
    assert rates == (AreaRate("stepfun", "web/", 1, 1),)


@pytest.mark.asyncio
async def test_a_failed_search_is_not_an_empty_history() -> None:
    with pytest.raises(RuntimeError, match="outcome store unavailable"):
        await success_rate_by_area(BoomPort())
    with pytest.raises(RuntimeError, match="outcome store unavailable"):
        await assemble_inputs([_line("a")], BoomPort())


@pytest.mark.asyncio
async def test_search_limit_is_honoured() -> None:
    rows = [_outcome(f"l{i}", "kimi", True).to_dict() for i in range(600)]
    port = RecordingPort(rows)
    inputs = await assemble_inputs([], port)
    assert inputs.success_by_provider == (ProviderRate("kimi", 500, 500),)
    assert inputs.success_rate_by_area == ()
