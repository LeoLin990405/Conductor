"""Review of fix/reviewer-quota-fallback: the escape, and the should-fixes."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.llms.native_cli.base import NativeCliError
from src.server.services.autonomy.roles import RolePolicy
from src.tools.auto_dispatch import LineStatus, Verdict
from src.tools.auto_dispatch.machine import (
    PROVIDER_SKIPS,
    REVIEWERS_QUOTA_EXHAUSTED,
    _classify_review_failure,
    _ReviewersQuotaExhausted,
    _ReviewersSkipped,
)
from src.tools.auto_dispatch.operator import apply_unblock
from src.tools.auto_dispatch.selection import QuotaAwareModelSelector, UsageSnapshotHeadroom
from tests.unit.tools.auto_dispatch.test_parallel_lines_n31 import (
    FailFirst,
    _assigned,
    _drive,
    _setup,
)


@pytest.fixture(autouse=True)
def _n73_paid_credits_off(monkeypatch):
    """N73 S1 turned paid credits on by default; these tests pin the off rule."""
    monkeypatch.setenv("CONDUCTOR_DISPATCH_ALLOW_PAID_CREDITS", "0")

pytestmark = pytest.mark.asyncio

WEEKLY = "2026-11-16T01:58:24Z"


class Reviews:
    def __init__(self, generic=(), quota=()) -> None:
        self.generic = set(generic)
        self.quota = set(quota)
        self.calls: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.calls.append((line_id, model))
        if model in self.generic:
            raise NativeCliError(f"{model} exited with code 1: unexpected EOF")
        if model in self.quota:
            raise NativeCliError("codex produced no final answer: You've hit your usage limit.")
        return Verdict.ok("ok")


def _selector(policy: RolePolicy, plan: dict | None = None) -> QuotaAwareModelSelector:
    return QuotaAwareModelSelector(UsageSnapshotHeadroom(states={}, plan=plan or {}), policy=policy)


POLICY = RolePolicy(
    developer_pool=("stepfun", "kimi"),
    primary_reviewer=("codex",),
    secondary_reviewer_pool=("claude",),
)


async def test_quota_blockers_in_the_retry_fallback_do_not_crash_the_drive(tmp_path: Path) -> None:
    """codex fails generically; the only other reviewer's plan is full."""
    reviews = Reviews(generic={"codex"})
    store, machine, _g, service, row = await _setup(
        tmp_path, [_assigned("a", "stepfun", ("src/a/",))], launcher=FailFirst({}), reviewer=reviews, parallel=1
    )
    machine._c.selector = _selector(
        POLICY, {"claude": {"plan_full": True, "credits": True, "resets_at": None}}
    )
    await _drive(service, row.id)
    after = await store.get(row.id)
    line = after.line("a")
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "review_unavailable"
    assert "review_retry" not in after.gates["lines"]["a"]


async def test_a_developer_only_short_skip_does_not_postpone_the_park(tmp_path: Path) -> None:
    _store, machine, _g, _service, row = await _setup(
        tmp_path, [_assigned("a", "stepfun", ("src/a/",))], launcher=FailFirst({}), parallel=1
    )
    machine._c.selector = _selector(
        RolePolicy(
            developer_pool=("stepfun", "kimi"),
            primary_reviewer=("codex",),
            secondary_reviewer_pool=("stepfun",),
        )
    )
    now = machine._wall()
    row.gates[PROVIDER_SKIPS] = {
        "codex": {"reason": "quota_exhausted", "until": now + 3600, "source": "backoff", "at": "x"},
        "kimi": {"reason": "rate_limited", "until": now + 60, "source": "backoff", "at": "x"},
    }
    with pytest.raises(_ReviewersQuotaExhausted) as raised:
        await machine._select_reviewer(row, row.line("a"), exclude=["stepfun"])
    assert set(raised.value.blockers) == {"codex"}


async def test_the_reported_reset_is_the_later_of_skip_and_plan(tmp_path: Path) -> None:
    _store, machine, _g, _service, row = await _setup(
        tmp_path, [_assigned("a", "stepfun", ("src/a/",))], launcher=FailFirst({}), parallel=1
    )
    machine._c.selector = _selector(
        RolePolicy(developer_pool=("stepfun",), primary_reviewer=("codex",), secondary_reviewer_pool=()),
        {"codex": {"plan_full": True, "credits": True, "resets_at": WEEKLY}},
    )
    now = machine._wall()
    row.gates[PROVIDER_SKIPS] = {
        "codex": {"reason": "quota_exhausted", "until": now + 1800, "source": "backoff", "at": "x"}
    }
    blockers = await machine._review_quota_blockers(row, row.line("a"), ["stepfun"])
    assert blockers["codex"]["resets_at"] == WEEKLY


async def test_a_primary_that_only_holds_the_line_is_no_fallback(tmp_path: Path) -> None:
    reviews = Reviews()
    store, machine, gates, service, row = await _setup(
        tmp_path, [_assigned("a", "codex", ("src/a/",))], launcher=FailFirst({}), reviewer=reviews, parallel=1
    )
    machine._c.selector = _selector(
        RolePolicy(
            developer_pool=("codex", "stepfun"),
            primary_reviewer=("codex",),
            secondary_reviewer_pool=("stepfun",),
            allow_reserved_code=True,
        )
    )
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert after.line("a").status is LineStatus.PASSED
    audit = after.gates.get("scheduler", {}).get("audit", [])
    assert not [item for item in audit if item.get("kind") == "reviewer_fallback"]


async def test_a_stored_reviewer_that_is_skipped_is_not_reused(tmp_path: Path) -> None:
    from src.server.services.autonomy.stage_gate import StageGateState

    _store, machine, _g, _service, row = await _setup(
        tmp_path, [_assigned("a", "stepfun", ("src/a/",))], launcher=FailFirst({}), parallel=1
    )
    machine._c.selector = _selector(
        RolePolicy(
            developer_pool=("stepfun",),
            primary_reviewer=("codex",),
            secondary_reviewer_pool=("claude",),
        )
    )
    row.gates[PROVIDER_SKIPS] = {
        "codex": {"reason": "quota_exhausted", "until": machine._wall() + 3600, "source": "backoff", "at": "x"}
    }
    state = StageGateState(task_id="a", primary_reviewer="codex")
    picked = await machine._pick_primary(row, row.line("a"), state)
    assert picked == "claude"


async def test_the_ungated_path_parks_instead_of_error(tmp_path: Path) -> None:
    from src.tools.auto_dispatch import Dispatch, DispatchState
    from tests.unit.tools.auto_dispatch.test_review_unparseable import _collaborators, _dispatch, _machine

    collaborators = _collaborators(tmp_path, reviewer=Reviews())
    collaborators.selector = _selector(
        RolePolicy(developer_pool=("kimi", "glm", "grok"), primary_reviewer=("codex",), secondary_reviewer_pool=()),
        {"codex": {"plan_full": True, "credits": True, "resets_at": WEEKLY}},
    )
    machine, _recorder = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())
    parked = await machine.run(dispatch)
    assert isinstance(parked, Dispatch)
    assert parked.state is not DispatchState.ERROR, parked.message
    assert {line.failure_reason for line in parked.lines} == {REVIEWERS_QUOTA_EXHAUSTED}
    assert WEEKLY in parked.lines[0].blockers[0]


async def test_unblock_of_a_quota_park_audits_its_reason_and_clears_the_record(tmp_path: Path) -> None:
    store, machine, _g, service, row = await _setup(
        tmp_path, [_assigned("a", "stepfun", ("src/a/",))], launcher=FailFirst({}), reviewer=Reviews(), parallel=1
    )
    machine._c.selector = _selector(
        RolePolicy(developer_pool=("stepfun",), primary_reviewer=("codex",), secondary_reviewer_pool=("stepfun",)),
        {"codex": {"plan_full": True, "credits": True, "resets_at": WEEKLY}},
    )
    await _drive(service, row.id)
    parked = await store.get(row.id)
    line = parked.line("a")
    assert line.failure_reason == REVIEWERS_QUOTA_EXHAUSTED
    assert apply_unblock(parked, line) is None
    assert REVIEWERS_QUOTA_EXHAUSTED not in parked.gates["lines"]["a"]
    audit = [i for i in parked.gates["scheduler"]["audit"] if i.get("kind") == "operator_unblock"]
    assert audit[-1]["failure_reason"] == REVIEWERS_QUOTA_EXHAUSTED


def test_only_the_trailing_error_lines_are_classified() -> None:
    echoed = (
        "codex produced no final answer: reading src/x.py\n"
        "    raise QuotaError(\"You've hit your usage limit\")  # in the code under review\n"
        "line 2\nline 3\nerror: stream disconnected before completion"
    )
    assert _classify_review_failure("codex", NativeCliError(echoed)).failure_class != "quota_exhausted"
    trailing = "codex produced no final answer: warming up\nYou've hit your usage limit. Try later."
    assert _classify_review_failure("codex", NativeCliError(trailing)).failure_class == "quota_exhausted"


async def test_paid_credits_allowed_lets_codex_review(tmp_path: Path, monkeypatch) -> None:
    """Prod sets CONDUCTOR_DISPATCH_ALLOW_PAID_CREDITS=1: codex reviews on credits."""
    monkeypatch.setenv("CONDUCTOR_DISPATCH_ALLOW_PAID_CREDITS", "1")
    reviews = Reviews()
    store, machine, _g, service, row = await _setup(
        tmp_path, [_assigned("a", "stepfun", ("src/a/",))], launcher=FailFirst({}), reviewer=reviews, parallel=1
    )
    machine._c.selector = _selector(
        POLICY, {"codex": {"plan_full": True, "credits": True, "resets_at": WEEKLY}}
    )
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert after.line("a").status is LineStatus.PASSED
    assert [model for _l, model in reviews.calls] == ["codex"]


async def test_skip_wait_still_used_for_a_short_reviewer_skip(tmp_path: Path) -> None:
    _store, machine, _g, _service, row = await _setup(
        tmp_path, [_assigned("a", "stepfun", ("src/a/",))], launcher=FailFirst({}), parallel=1
    )
    machine._c.selector = _selector(
        RolePolicy(developer_pool=("stepfun",), primary_reviewer=("codex",), secondary_reviewer_pool=())
    )
    row.gates[PROVIDER_SKIPS] = {
        "codex": {"reason": "rate_limited", "until": machine._wall() + 120, "source": "backoff", "at": "x"}
    }
    with pytest.raises(_ReviewersSkipped):
        await machine._select_reviewer(row, row.line("a"), exclude=["stepfun"])
