"""N25a against the live QuotaAwareModelSelector (review of cc3084c76).

The live selector ignores ``used_models`` for work picks, so a skip passed
only that way never moved a line: the audit said "kimi skipped -> kimi" and
the line launched on the limited provider. The selector now takes an
explicit ``exclude``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.server.services.autonomy.roles import RolePolicy
from src.tools.auto_dispatch import LineStatus
from src.tools.auto_dispatch.machine import PROVIDER_SKIPS
from src.tools.auto_dispatch.protocols import ModelRole
from src.tools.auto_dispatch.selection import QuotaAwareModelSelector
from src.tools.auto_dispatch.types import Line
from tests.unit.tools.auto_dispatch.test_parallel_lines_n31 import (
    FailFirst,
    _assigned,
    _drive,
    _limited,
    _setup,
)

pytestmark = pytest.mark.asyncio

POLICY = RolePolicy(developer_pool=("kimi", "glm", "stepfun"))


def _live(machine) -> QuotaAwareModelSelector:
    selector = QuotaAwareModelSelector(policy=POLICY)
    machine._c.selector = selector
    return selector


async def test_the_live_selector_honours_exclude_for_work_and_review() -> None:
    selector = QuotaAwareModelSelector(policy=POLICY)
    line = Line(id="L", name="n", task="t", write_scope=("src/a",))
    first = await QuotaAwareModelSelector(policy=POLICY).select(line, [], role=ModelRole.WORK)
    again = await selector.select(line, [first], role=ModelRole.WORK, exclude=(first,))
    assert again is not None and again != first
    reviewer = await QuotaAwareModelSelector(policy=POLICY).select(line, [], role=ModelRole.REVIEW)
    other = await selector.select(line, [], role=ModelRole.REVIEW, exclude=(reviewer,))
    assert other != reviewer


async def test_a_skipped_provider_moves_an_unrun_line_with_the_live_selector(tmp_path: Path) -> None:
    launcher = FailFirst({"a": _limited})
    store, machine, _gates, service, row = await _setup(
        tmp_path,
        [
            _assigned("a", "kimi", ("src/a/",)),
            _assigned("b", "kimi", ("src/b/",), depends_on=("a",)),
        ],
        launcher=launcher,
        parallel=1,
    )
    _live(machine)
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert after.gates[PROVIDER_SKIPS]["kimi"]["reason"] == "rate_limited"
    b_models = [model for line_id, model in launcher.calls if line_id == "b"]
    assert b_models and b_models[0] != "kimi"
    audit = [i for i in after.gates["scheduler"]["audit"] if i.get("kind") == "provider_skipped"]
    assert all(item["replacement"] != item["provider"] for item in audit)
    assert {line.status for line in after.lines} == {LineStatus.PASSED}


async def test_the_limited_line_itself_moves_to_the_next_developer(tmp_path: Path) -> None:
    """N25(a): the line that hit the limit moves instead of retrying and parking."""
    launcher = FailFirst({"a": _limited})
    store, machine, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "kimi", ("src/a/",))], launcher=launcher, parallel=1
    )
    _live(machine)
    await _drive(service, row.id)
    after = await store.get(row.id)
    line = after.line("a")
    models = [model for line_id, model in launcher.calls if line_id == "a"]
    assert models[0] == "kimi" and len(models) == 2 and models[1] != "kimi"
    assert line.status is LineStatus.PASSED
    assert line.assigned_model == models[1]
    # The work stays on the line's branch (not a fresh -aN branch), and the
    # new model is told why it took over.
    assert line.branch == "feature/auto-dispatch-par/line-a"
    assert "Previous attempt stopped with" in launcher.tasks[-1]
    moved = [
        item
        for item in after.gates["scheduler"]["audit"]
        if item.get("kind") == "provider_skipped" and item.get("moved")
    ]
    assert moved and moved[-1]["line_id"] == "a" and moved[-1]["replacement"] == models[1]


async def test_with_nobody_else_eligible_the_same_provider_retries(tmp_path: Path) -> None:
    launcher = FailFirst({"a": _limited})
    store, machine, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "kimi", ("src/a/",))], launcher=launcher, parallel=1
    )
    machine._c.selector = QuotaAwareModelSelector(policy=RolePolicy(developer_pool=("kimi",)))
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert [model for _line, model in launcher.calls] == ["kimi", "kimi"]
    assert after.line("a").status is LineStatus.PASSED


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize("live", [False, True])
async def test_a_rate_limited_reviewer_is_a_wait_not_an_error(tmp_path: Path, live: bool) -> None:
    """Every eligible reviewer skipped: review again at the skip's expiry."""
    launcher = FailFirst({})
    store, machine, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "kimi", ("src/a/",))], launcher=launcher, parallel=1
    )
    clock = Clock(1_000_000.0)
    machine._wall = clock
    if live:
        policy = RolePolicy(
            developer_pool=("kimi",),
            primary_reviewer=("codex",),
            secondary_reviewer_pool=(),
        )
        machine._c.selector = QuotaAwareModelSelector(policy=policy)
        reviewer_name = "codex"
    else:
        machine._c.selector._pool = ("kimi", "glm")
        reviewer_name = "glm"
    until = clock.now + 600

    def skip(r):
        r.gates[PROVIDER_SKIPS] = {
            reviewer_name: {"reason": "rate_limited", "until": until, "source": "backoff", "at": "x"}
        }
        return r

    await service.apply_mutation(row.id, skip)

    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.now += seconds  # the wait passes; the skip expires

    machine._sleep = sleep
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert after.state.value != "ERROR", after.message
    assert after.line("a").status is LineStatus.PASSED
    # The drive waited until the skip expired, then the review ran.
    assert clock.now >= until
    assert "review_retry" not in after.gates["lines"]["a"]


async def test_a_new_failure_never_shortens_an_active_skip(tmp_path: Path) -> None:
    from src.llms.native_cli.failure import FailureClass

    _store, machine, _gates, _service, row = await _setup(
        tmp_path, [_assigned("a", "kimi", ("src/a/",))], launcher=FailFirst({}), parallel=1
    )
    machine._wall = Clock(1_000.0)
    machine._note_provider_skip(row, "kimi", _limited("r1", retry_after=3600))
    long = row.gates[PROVIDER_SKIPS]["kimi"]["until"]
    machine._note_provider_skip(row, "kimi", _limited("r2", retry_after=60))
    assert row.gates[PROVIDER_SKIPS]["kimi"]["until"] == long
    machine._note_provider_skip(
        row,
        "kimi",
        _limited("r3", failure_class=FailureClass.QUOTA_EXHAUSTED, reason="q", retry_after=7200),
    )
    assert row.gates[PROVIDER_SKIPS]["kimi"]["until"] > long


class _SavingPlanner:
    """Plans one child; while it plans, the actor adopts a freshly saved row."""

    def __init__(self, machine_ref: list) -> None:
        self.machine_ref = machine_ref

    async def plan(self, _goal: str):
        machine, dispatch = self.machine_ref
        await machine._submit(machine._save, dispatch)
        return [Line(id="child", name="child", task="do child", write_scope=("src/c/",))]


async def test_replan_keeps_a_running_siblings_epoch_and_its_snapshot_writes(
    tmp_path: Path,
) -> None:
    from src.tools.auto_dispatch.gates import drive_epoch

    store, machine, gates, _service, row = await _setup(
        tmp_path,
        [_assigned("a", "kimi", ("src/a/",)), _assigned("b", "glm", ("src/b/",))],
        launcher=FailFirst({}),
        parallel=2,
    )
    dispatch = await store.get(row.id)
    sibling = dispatch.line("b")
    sibling.status = LineStatus.RUNNING
    sibling.current_run = "par:b:0:out"
    await store.save(dispatch)
    ref: list = []
    machine._c.planner = _SavingPlanner(ref)
    await machine._claim_drive()
    try:
        ref.extend([machine, dispatch])
        machine._epoch0 = drive_epoch(dispatch)
        before = drive_epoch(dispatch)
        await machine._replan(dispatch, dispatch.line("a"), ["x"], "two_failed_rounds")
    finally:
        machine._release_drive()
    stored = await store.get(row.id)
    # The running sibling's reservation still carries the drive's epoch.
    assert drive_epoch(stored) == before
    snap = stored.gates["lines"]["a"]
    assert snap["replans"] == 1
    assert snap["reason"] == "replanned"
    assert stored.line("a-r1-1") is not None
