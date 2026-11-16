"""N63-1 follow-ups: plan-status memo, review-failure tail, ungated unblock, roster check."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.llms.native_cli.base import NativeCliError
from src.llms.native_cli.failure import FailureClass, classify_failure
from src.server.services.autonomy.roles import RolePolicy
from src.tools.auto_dispatch import LineStatus
from src.tools.auto_dispatch.headroom_codexbar import PlanStatusCache
from src.tools.auto_dispatch.pool import review_roster_check, review_roster_gaps
from src.tools.auto_dispatch.review_failure_tail import (
    review_failure_text,
    strip_adapter_prefix,
)

# ------------------------------------------------------- per-drive plan status


class _Port:
    def __init__(self, answers: list[object]) -> None:
        self.answers = list(answers)
        self.calls = 0

    async def remaining(self, model: str) -> float:
        return 0.5

    async def plan_status(self, model: str):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_one_drive_reads_a_models_plan_status_once_within_the_window() -> None:
    full = {"plan_full": True, "credits": True, "resets_at": None}
    port = _Port([full, {"plan_full": False, "credits": False, "resets_at": None}])
    clock = _Clock()
    cache = PlanStatusCache(port, ttl_seconds=60, clock=clock)
    first = await cache.plan_status("codex")
    first["plan_full"] = False  # a caller's edit does not reach the memo
    assert await cache.plan_status("Codex") == full
    assert port.calls == 1
    assert await cache.remaining("codex") == 0.5
    clock.now = 61
    assert (await cache.plan_status("codex"))["plan_full"] is False
    assert port.calls == 2


@pytest.mark.asyncio
async def test_none_is_kept_and_an_error_is_not() -> None:
    port = _Port([RuntimeError("boom"), None])
    cache = PlanStatusCache(port, clock=_Clock())
    with pytest.raises(RuntimeError):
        await cache.plan_status("kimi")
    assert await cache.plan_status("kimi") is None
    assert await cache.plan_status("kimi") is None
    assert port.calls == 2


@pytest.mark.asyncio
async def test_a_port_without_plan_status_answers_none() -> None:
    class Bare:
        ports = ("x",)

        async def remaining(self, model: str) -> float:
            return 1.0

    cache = PlanStatusCache(Bare())
    assert await cache.plan_status("codex") is None
    assert cache.ports == ("x",)


# ------------------------------------------------------ review-failure tail


def test_only_known_adapter_prefixes_are_stripped() -> None:
    assert strip_adapter_prefix("native CLI exited 1: You've hit your usage limit") == (
        "You've hit your usage limit"
    )
    assert strip_adapter_prefix("codex produced no final answer: usage limit") == "usage limit"
    # Not an adapter prefix: the provider's own word stays.
    assert strip_adapter_prefix("insufficient_quota: the account is out") == (
        "insufficient_quota: the account is out"
    )
    # Nothing after the prefix: the line is kept as it was.
    assert strip_adapter_prefix("codex produced no final answer:") == (
        "codex produced no final answer:"
    )


def test_a_provider_word_before_a_colon_is_classified() -> None:
    """The old rule cut a one-line message at its first colon and lost the class."""
    text = review_failure_text("insufficient_quota: the account has no credit left")
    assert classify_failure("codex", text).failure_class is FailureClass.QUOTA_EXHAUSTED


def test_the_prefix_is_stripped_from_the_first_line_of_a_short_message() -> None:
    text = review_failure_text("native CLI exited 2: warming up\nYou've hit your usage limit.")
    assert text.splitlines()[0] == "warming up"
    # A long message's first line is outside the window and is left alone.
    long = "codex produced no final answer: a\nb\nc\nd"
    assert review_failure_text(long) == "b\nc\nd"


def test_a_footer_of_three_lines_pushes_the_error_out_documented_limit() -> None:
    text = review_failure_text(
        "native CLI exited 1: You've hit your usage limit.\nhint: run --help\nlog: /tmp/x\nbye"
    )
    assert "usage limit" not in text


def test_the_machine_classifier_reads_the_same_text() -> None:
    """Needs the integrator's hook in machine._classify_review_failure."""
    from src.tools.auto_dispatch.machine import _classify_review_failure

    exc = NativeCliError("insufficient_quota: the account has no credit left")
    assert _classify_review_failure("codex", exc).failure_class == "quota_exhausted"


# ------------------------------------------- ungated reviewers_quota_exhausted


@pytest.mark.asyncio
async def test_unblock_of_an_ungated_quota_park_is_refused_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ungated path parks reviewers_quota_exhausted with no Stage-Gate
    snapshot; unblock has nothing to resume and refuses cleanly."""
    # N73 S1 turned paid credits on by default; this park needs the off rule.
    monkeypatch.setenv("CONDUCTOR_DISPATCH_ALLOW_PAID_CREDITS", "0")
    import copy

    from src.tools.auto_dispatch import Dispatch
    from src.tools.auto_dispatch.machine import REVIEWERS_QUOTA_EXHAUSTED
    from src.tools.auto_dispatch.operator import (
        apply_decision,
        apply_unblock,
        decision_refusal,
        unblock_refusal,
    )
    from src.tools.auto_dispatch.selection import (
        QuotaAwareModelSelector,
        UsageSnapshotHeadroom,
    )
    from tests.unit.tools.auto_dispatch.test_n63_review_followups import WEEKLY, Reviews
    from tests.unit.tools.auto_dispatch.test_review_unparseable import (
        _collaborators,
        _dispatch,
        _machine,
    )

    collaborators = _collaborators(tmp_path, reviewer=Reviews())
    assert collaborators.gates is None
    collaborators.selector = QuotaAwareModelSelector(
        UsageSnapshotHeadroom(
            states={}, plan={"codex": {"plan_full": True, "credits": True, "resets_at": WEEKLY}}
        ),
        policy=RolePolicy(
            developer_pool=("kimi", "glm", "grok"),
            primary_reviewer=("codex",),
            secondary_reviewer_pool=(),
        ),
    )
    machine, _recorder = _machine(collaborators)
    parked = await machine.run(await collaborators.store.create(_dispatch()))
    assert isinstance(parked, Dispatch)
    line = parked.lines[0]
    assert line.failure_reason == REVIEWERS_QUOTA_EXHAUSTED
    assert line.status is LineStatus.BLOCKED
    before = copy.deepcopy((parked.gates, repr(parked.lines)))

    assert unblock_refusal(parked, line) == "not_gated"
    assert apply_unblock(parked, line) == "not_gated"
    request = {"kind": "unblock", "note": ""}
    assert decision_refusal(parked, line, request) == "not_gated"
    assert apply_decision(parked, line, request) == "not_gated"
    assert (parked.gates, repr(parked.lines)) == before


# ----------------------------------------------------------- roster check


def test_a_secondary_pool_member_off_the_review_roster_fails_the_check() -> None:
    policy = RolePolicy(
        developer_pool=("stepfun", "kimi", "grok"),
        primary_reviewer=("codex",),
        secondary_reviewer_pool=("stepfun", "kimi", "grok", "step"),
    )
    gaps = review_roster_gaps(policy)
    assert list(gaps) == ["kimi", "grok"]
    assert gaps["kimi"].startswith("review-ineligible")
    assert gaps["grok"] == "its CLI has no read-only mode"
    check = review_roster_check(policy)
    assert check["ok"] is False and check["name"] == "review_roster"
    assert "kimi" in check["detail"] and "grok" in check["detail"]


def test_a_pool_with_only_the_primary_fails_and_a_clean_pool_passes() -> None:
    only_primary = RolePolicy(
        developer_pool=("stepfun",), primary_reviewer=("codex",), secondary_reviewer_pool=("codex",)
    )
    assert review_roster_gaps(only_primary) == {}
    assert review_roster_check(only_primary)["ok"] is False
    clean = RolePolicy.default()
    assert review_roster_check(clean) == {
        "name": "review_roster",
        "ok": True,
        "detail": "secondary reviewers: stepfun",
    }


def test_an_unreadable_policy_is_a_failed_check_not_an_error(monkeypatch, tmp_path) -> None:
    bad = tmp_path / "policy.yaml"
    bad.write_text("- not a mapping\n", encoding="utf-8")
    monkeypatch.setenv("CONDUCTOR_ROLE_POLICY_FILE", str(bad))
    check = review_roster_check()
    assert check["ok"] is False
    assert "RolePolicyError" in check["detail"]
