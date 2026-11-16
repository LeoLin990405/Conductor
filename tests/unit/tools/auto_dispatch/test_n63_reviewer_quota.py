"""N63: a reviewer out of quota is skipped, the review falls back, or the line parks.

Prod 2026-11-11: codex had 0% of its weekly plan left (paid credits still
there), every review it got failed as an unclassified ``review_unavailable``,
the CodexBar headroom read it as ``low`` (still eligible), and a risk-bearing
line's secondary review had nobody else: every other developer of the
dispatch was left out of review picks. Tests run against the live
QuotaAwareModelSelector.
"""

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

USAGE_LIMIT = "codex produced no final answer: You've hit your usage limit. Try again later."
RESET = "2026-11-16T01:58:24Z"


class Reviews:
    """codex fails with its CLI's usage-limit wording; everyone else passes."""

    def __init__(self, down: set[str] | None = None) -> None:
        self.down = {"codex"} if down is None else down
        self.calls: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.calls.append((line_id, model))
        if model in self.down:
            raise NativeCliError(USAGE_LIMIT)
        return Verdict.ok(f"ok from {model}")


def _selector(policy: RolePolicy, *, plan: dict | None = None) -> QuotaAwareModelSelector:
    headroom = UsageSnapshotHeadroom(states={}, plan=plan or {})
    return QuotaAwareModelSelector(headroom, policy=policy)


def test_a_usage_limit_review_failure_is_classified() -> None:
    classified = _classify_review_failure("codex", NativeCliError(USAGE_LIMIT))
    assert classified.failure_class == "quota_exhausted"
    assert classified.reason_code == "codex_usage_limit_reached"


async def test_a_quota_failure_skips_the_reviewer_and_the_next_pick_excludes_it(
    tmp_path: Path,
) -> None:
    reviews = Reviews()
    store, machine, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "kimi", ("src/a/",))], launcher=FailFirst({}), reviewer=reviews, parallel=1
    )
    machine._c.selector = _selector(
        RolePolicy(
            developer_pool=("kimi", "stepfun"),
            primary_reviewer=("codex",),
            secondary_reviewer_pool=("stepfun",),
        )
    )
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert after.gates[PROVIDER_SKIPS]["codex"]["reason"] == "quota_exhausted"
    # codex is asked once; the retry goes to the next reviewer.
    assert [model for _line, model in reviews.calls] == ["codex", "stepfun"]
    assert after.line("a").status is LineStatus.PASSED
    fallback = [
        item for item in after.gates["scheduler"]["audit"] if item.get("kind") == "reviewer_fallback"
    ]
    assert fallback and fallback[-1]["reviewer"] == "stepfun"
    assert after.gates["lines"]["a"]["review_fallback"]["reviewer"] == "stepfun"


async def test_a_risk_bearing_line_falls_back_to_a_non_holder_secondary(tmp_path: Path) -> None:
    """codex is skipped; another line's author may still review this one."""
    reviews = Reviews()
    store, machine, _gates, service, row = await _setup(
        tmp_path,
        [
            _assigned("a", "grok", ("src/a/",), risk_bearing=True),
            _assigned("b", "stepfun", ("src/b/",)),
        ],
        launcher=FailFirst({}),
        reviewer=reviews,
        parallel=1,
    )
    machine._c.selector = _selector(
        RolePolicy(
            developer_pool=("grok", "stepfun"),
            primary_reviewer=("codex",),
            secondary_reviewer_pool=("stepfun", "claude"),
        )
    )

    def codex_out(r):
        r.gates[PROVIDER_SKIPS] = {
            "codex": {"reason": "quota_exhausted", "until": 9e12, "source": "backoff", "at": "x"}
        }
        return r

    await service.apply_mutation(row.id, codex_out)
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert after.state.value != "ERROR", after.message
    assert after.line("a").status is LineStatus.PASSED
    a_reviewers = [model for line_id, model in reviews.calls if line_id == "a"]
    assert "codex" not in a_reviewers
    assert len(a_reviewers) == 2 and len(set(a_reviewers)) == 2
    assert after.gates["lines"]["a"]["review_fallback"]["from"] == "codex"


async def test_no_eligible_reviewer_parks_naming_each_provider_and_reset(tmp_path: Path) -> None:
    reviews = Reviews()
    store, machine, gates, service, row = await _setup(
        tmp_path, [_assigned("a", "stepfun", ("src/a/",))], launcher=FailFirst({}), reviewer=reviews, parallel=1
    )
    machine._c.selector = _selector(
        RolePolicy(
            developer_pool=("stepfun",),
            primary_reviewer=("codex",),
            secondary_reviewer_pool=("stepfun",),
        ),
        plan={"codex": {"plan_full": True, "credits": True, "resets_at": RESET}},
    )
    await _drive(service, row.id)
    after = await store.get(row.id)
    line = after.line("a")
    assert after.state.value != "ERROR", after.message
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == REVIEWERS_QUOTA_EXHAUSTED
    assert "codex" in line.blockers[0] and RESET in line.blockers[0]
    snap = after.gates["lines"]["a"]
    assert snap[REVIEWERS_QUOTA_EXHAUSTED]["codex"]["resets_at"] == RESET
    # The headroom check kept codex away: its review was never attempted.
    assert reviews.calls == []
    # unblock resumes the review once a reviewer is back.
    assert apply_unblock(after, line) is None
    assert line.status is LineStatus.RUNNING
