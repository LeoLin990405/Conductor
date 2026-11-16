"""N63: a reviewer whose plan is full is not picked just because credits remain.

CodexBar reports Codex at 100% of its weekly plan with paid credits left. The
four-state headroom reader calls that ``low`` (work keeps running on credits),
so the selector kept handing Codex reviews that then failed. Review now reads
the plan status beside the headroom state. Every snapshot here is a fixture in
``tmp_path`` or an injected loader; the real home snapshot is never read.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.server.services.autonomy.roles import RolePolicy
from src.server.services.native_runtime.usage_headroom import (
    read_usage_headroom,
    read_usage_plan_status,
)
from src.tools.auto_dispatch.protocols import ModelRole
from src.tools.auto_dispatch.selection import (
    ALLOW_PAID_CREDITS_ENV,
    QuotaAwareModelSelector,
    SnapshotOrUnlimitedHeadroom,
    UnlimitedHeadroom,
    UsageSnapshotHeadroom,
)
from src.tools.auto_dispatch.types import Line

NOW = datetime(2026, 11, 11, 2, 0, tzinfo=timezone.utc)
NOW_TEXT = "2026-11-11T01:58:00Z"
RESETS = "2026-11-16T01:58:24Z"
LINE = Line(id="line-1", name="edit", task="edit it", write_scope=("src/a.py",))

CODEX_ROW = {
    "provider": "codex",
    "kind": "quota",
    "status": "ok",
    "windows": [{"used_percent": 100, "role": "secondary", "resets_at_utc": RESETS}],
    "updated_at": NOW_TEXT,
    "credits_remaining": 7932.04,
}
CODEX_PLAN = {"codex": {"plan_full": True, "credits": True, "resets_at": RESETS}}

REVIEW_POLICY = RolePolicy(
    primary_reviewer=("codex",),
    secondary_reviewer_pool=("stepfun",),
    developer_pool=("kimi", "stepfun"),
    work_tail=(),
)


def _snapshot(tmp_path: Path, rows: list[dict]) -> str:
    path = tmp_path / "codexbar-snapshot.json"
    path.write_text(json.dumps({"providers": rows}), encoding="utf-8")
    return str(path)


def _headroom(states: dict[str, str], plan: dict[str, dict]) -> UsageSnapshotHeadroom:
    async def states_loader():
        return states

    async def plan_loader():
        return plan

    return UsageSnapshotHeadroom(loader=states_loader, plan_loader=plan_loader)


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_paid_credits(monkeypatch):
    # N73 S1 turned paid credits on by default; these tests pin the off rule.
    monkeypatch.setenv(ALLOW_PAID_CREDITS_ENV, "0")


# --- read_usage_plan_status --------------------------------------------------


def test_full_plan_with_credits_is_plan_full_with_credits_and_reset(tmp_path):
    path = _snapshot(tmp_path, [CODEX_ROW])

    assert read_usage_plan_status(path, now=NOW) == {
        "codex": {"plan_full": True, "credits": True, "resets_at": RESETS}
    }
    # The four-state contract is untouched: credits still make it low.
    assert read_usage_headroom(path, now=NOW) == {"codex": "low"}


def test_latest_reset_among_full_plan_windows_wins(tmp_path):
    row = dict(CODEX_ROW, windows=[
        {"used_percent": 100, "role": "primary", "resets_at_utc": "2026-11-11T05:00:00Z"},
        {"used_percent": 100, "role": "secondary", "resets_at_utc": RESETS},
        {"used_percent": 100, "role": "tertiary", "resets_at_utc": "2026-12-01T00:00:00Z"},
    ])
    status = read_usage_plan_status(_snapshot(tmp_path, [row]), now=NOW)
    assert status["codex"]["resets_at"] == RESETS


def test_available_and_reset_windows_are_not_plan_full(tmp_path):
    rows = [
        dict(CODEX_ROW, windows=[{"used_percent": 40, "role": "secondary", "resets_at_utc": RESETS}],
             credits_remaining=0),
        {
            "provider": "kimi",
            "kind": "quota",
            "status": "ok",
            "updated_at": NOW_TEXT,
            # Full, but the window already reset: no longer evidence.
            "windows": [
                {"used_percent": 100, "role": "primary", "resets_at_utc": "2026-11-11T01:00:00Z"},
                {"used_percent": 10, "role": "secondary", "resets_at_utc": RESETS},
            ],
        },
        {
            "provider": "stepfun",
            "kind": "quota",
            "status": "ok",
            "updated_at": NOW_TEXT,
            # Only a bucket is full; the plan has room.
            "windows": [{"used_percent": 100, "role": "tertiary", "resets_at_utc": RESETS}],
        },
    ]
    assert read_usage_plan_status(_snapshot(tmp_path, rows), now=NOW) == {
        "codex": {"plan_full": False, "credits": False, "resets_at": None},
        "kimi": {"plan_full": False, "credits": False, "resets_at": None},
        "stepfun": {"plan_full": False, "credits": False, "resets_at": None},
    }


def test_stale_failed_and_unreadable_rows_are_left_out(tmp_path):
    rows = [
        dict(CODEX_ROW, updated_at="2026-11-10T00:00:00Z"),
        dict(CODEX_ROW, provider="kimi", status="error"),
    ]
    assert read_usage_plan_status(_snapshot(tmp_path, rows), now=NOW) == {}
    assert read_usage_plan_status(str(tmp_path / "missing.json"), now=NOW) == {}
    link = tmp_path / "link.json"
    link.symlink_to(_snapshot(tmp_path, [CODEX_ROW]))
    assert read_usage_plan_status(str(link), now=NOW) == {}


# --- the headroom ports -------------------------------------------------------


async def test_ports_answer_plan_status_through_snapshot_aliases():
    headroom = _headroom({"codex": "low"}, CODEX_PLAN)
    assert await headroom.plan_status("codex") == CODEX_PLAN["codex"]
    assert await headroom.plan_status("stepfun") is None
    assert await UnlimitedHeadroom().plan_status("codex") is None

    calls = []

    def plan_loader():
        calls.append(1)
        return CODEX_PLAN

    wrapped = SnapshotOrUnlimitedHeadroom(lambda: {"codex": "low"}, plan_loader=plan_loader)
    assert await wrapped.plan_status("codex") == CODEX_PLAN["codex"]
    assert await wrapped.plan_status("codex") == CODEX_PLAN["codex"]
    assert calls == [1]
    # An empty snapshot is unlimited, and unlimited has no plan data.
    assert await SnapshotOrUnlimitedHeadroom(lambda: {}, plan_loader=plan_loader).plan_status(
        "codex"
    ) is None


# --- the selector -------------------------------------------------------------


async def test_review_skips_a_full_plan_primary_for_the_secondary():
    selector = QuotaAwareModelSelector(
        _headroom({"codex": "low"}, CODEX_PLAN), policy=REVIEW_POLICY
    )
    assert await selector.select(LINE, [], role=ModelRole.REVIEW) == "stepfun"
    assert selector.review_refusal is None


async def test_paid_credits_flag_lets_review_use_the_full_plan_primary(monkeypatch):
    monkeypatch.setenv(ALLOW_PAID_CREDITS_ENV, "1")
    selector = QuotaAwareModelSelector(
        _headroom({"codex": "low"}, CODEX_PLAN), policy=REVIEW_POLICY
    )
    assert await selector.select(LINE, [], role=ModelRole.REVIEW) == "codex"
    assert await selector.review_blockers(LINE, []) == {}


async def test_work_still_runs_on_credits_when_the_plan_is_full():
    plan = {"kimi": {"plan_full": True, "credits": True, "resets_at": RESETS}}
    selector = QuotaAwareModelSelector(
        _headroom({"kimi": "low", "stepfun": "exhausted"}, plan),
        policy=RolePolicy(developer_pool=("kimi", "stepfun"), work_tail=()),
    )
    assert await selector.select(LINE, []) == "kimi"


async def test_review_blockers_name_each_blocked_reviewer_and_reset():
    selector = QuotaAwareModelSelector(
        _headroom({"codex": "low", "stepfun": "exhausted"}, CODEX_PLAN),
        policy=REVIEW_POLICY,
    )
    assert await selector.select(LINE, [], role=ModelRole.REVIEW) is None
    refusal = selector.review_refusal
    assignments = dict(selector._assignments)

    assert await selector.review_blockers(LINE, []) == {
        "codex": {"reason": "plan_exhausted", "resets_at": RESETS},
        "stepfun": {"reason": "no_headroom", "resets_at": None},
    }
    # Used/excluded models are not blockers, and nothing is mutated.
    assert await selector.review_blockers(LINE, ["kimi"], exclude=("stepfun",)) == {
        "codex": {"reason": "plan_exhausted", "resets_at": RESETS},
    }
    assert selector.review_refusal == refusal
    assert selector._assignments == assignments


async def test_review_blockers_skip_refused_reviewers():
    selector = QuotaAwareModelSelector(
        _headroom({"codex": "low"}, CODEX_PLAN),
        policy=REVIEW_POLICY,
        review_refused=lambda model: model == "codex",
    )
    assert await selector.review_blockers(LINE, []) == {}


# --- N63-1: a 0% plan with paid credits may still review when allowed --------
#
# In prod Codex sits at 0% of its weekly plan with a paid balance, so
# ``remaining`` is 0 and the headroom gate rejected it before the paid-credits
# rule ran; every primary review then fell back to stepfun.


async def test_zero_plan_reviews_on_credits_when_allowed(monkeypatch):
    monkeypatch.setenv(ALLOW_PAID_CREDITS_ENV, "1")
    selector = QuotaAwareModelSelector(
        _headroom({"codex": "exhausted"}, CODEX_PLAN), policy=REVIEW_POLICY
    )
    assert await selector.select(LINE, [], role=ModelRole.REVIEW) == "codex"
    assert await selector.review_blockers(LINE, []) == {}


async def test_zero_plan_falls_back_without_credits_flag(monkeypatch):
    # Same snapshot both ways: only the flag decides.
    monkeypatch.setenv(ALLOW_PAID_CREDITS_ENV, "1")
    allowed = QuotaAwareModelSelector(
        _headroom({"codex": "exhausted"}, CODEX_PLAN), policy=REVIEW_POLICY
    )
    assert await allowed.select(LINE, [], role=ModelRole.REVIEW) == "codex"

    monkeypatch.setenv(ALLOW_PAID_CREDITS_ENV, "0")
    selector = QuotaAwareModelSelector(
        _headroom({"codex": "exhausted"}, CODEX_PLAN), policy=REVIEW_POLICY
    )
    assert await selector.select(LINE, [], role=ModelRole.REVIEW) == "stepfun"
    assert await selector.review_blockers(LINE, []) == {
        "codex": {"reason": "plan_exhausted", "resets_at": RESETS},
    }


@pytest.mark.parametrize(
    "plan",
    [
        {},  # plan status None: unknown is not headroom
        {"codex": {"plan_full": False, "credits": True, "resets_at": None}},
        {"codex": {"plan_full": True, "credits": False, "resets_at": RESETS}},
    ],
    ids=["status-none", "not-plan-full", "no-credits"],
)
async def test_zero_plan_unknown_status_falls_back(monkeypatch, plan):
    # Flag on throughout: only the plan status differs from the codex case.
    monkeypatch.setenv(ALLOW_PAID_CREDITS_ENV, "1")
    on_credits = QuotaAwareModelSelector(
        _headroom({"codex": "exhausted"}, CODEX_PLAN), policy=REVIEW_POLICY
    )
    assert await on_credits.select(LINE, [], role=ModelRole.REVIEW) == "codex"

    selector = QuotaAwareModelSelector(
        _headroom({"codex": "exhausted"}, plan), policy=REVIEW_POLICY
    )
    assert await selector.select(LINE, [], role=ModelRole.REVIEW) == "stepfun"


async def test_zero_plan_on_credits_gets_no_work(monkeypatch):
    # The same 0%-plan-on-credits model reviews but is never handed work.
    monkeypatch.setenv(ALLOW_PAID_CREDITS_ENV, "1")
    plan = {"stepfun": {"plan_full": True, "credits": True, "resets_at": RESETS}}
    states = {"stepfun": "exhausted", "kimi": "available", "codex": "available"}
    reviewer = QuotaAwareModelSelector(
        _headroom(states, plan),
        policy=RolePolicy(
            primary_reviewer=("stepfun",),
            secondary_reviewer_pool=("codex",),
            developer_pool=("stepfun", "kimi"),
            work_tail=(),
        ),
    )
    assert await reviewer.select(LINE, [], role=ModelRole.REVIEW) == "stepfun"

    selector = QuotaAwareModelSelector(
        _headroom(states, plan),
        policy=RolePolicy(developer_pool=("stepfun", "kimi"), work_tail=()),
    )
    assert await selector.select(LINE, []) == "kimi"
    assert await selector.select(LINE, []) == "kimi"
