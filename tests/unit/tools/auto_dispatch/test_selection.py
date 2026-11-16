"""Model selection: role rules, quota headroom and round-robin spread."""

from __future__ import annotations

import pytest

from src.server.services.autonomy.roles import ROLE_POLICY_ENV
from src.tools.auto_dispatch.pool import (
    DEV_MODEL_POOL,
    MODEL_POOL,
    READ_ONLY_CAPABLE,
    REVIEW_INELIGIBLE,
    eligible_models,
)
from src.tools.auto_dispatch.protocols import ModelRole
from src.tools.auto_dispatch.selection import QuotaAwareModelSelector
from src.tools.auto_dispatch.types import Line

LINE = Line(id="line-0", name="survey", task="read the code")


@pytest.fixture(autouse=True)
def _default_role_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ROLE_POLICY_ENV, raising=False)


class Headroom:
    def __init__(self, remaining: dict[str, float]) -> None:
        self.remaining_map = remaining

    async def remaining(self, model: str) -> float:
        return self.remaining_map.get(model, 1.0)


class Stats:
    def __init__(self, rates: dict[str, float]) -> None:
        self.rates = rates

    async def success_rate(self, model: str) -> float | None:
        return self.rates.get(model)


async def test_agy_is_never_eligible_and_manus_is_research_only() -> None:
    assert "agy" not in eligible_models("work")
    assert "agy" not in eligible_models("review")
    assert "agy" not in eligible_models("research")
    assert "manus" not in eligible_models("work")
    assert "manus" in eligible_models("research")


async def test_round_robin_spreads_lines_across_the_work_roster() -> None:
    selector = QuotaAwareModelSelector()
    roster = eligible_models("work")
    chosen = [await selector.select(LINE, []) for _ in range(len(roster))]
    assert chosen == list(roster)


async def test_a_model_out_of_quota_is_skipped() -> None:
    headroom = Headroom({"kimi": 0.0, "glm": 0.0})
    selector = QuotaAwareModelSelector(headroom)
    assert await selector.select(LINE, []) == "grok"


async def test_an_exhausted_pool_returns_none() -> None:
    roster = eligible_models("work")
    selector = QuotaAwareModelSelector(Headroom({model: 0.0 for model in roster}))
    assert await selector.select(LINE, []) is None


async def test_a_reviewer_is_never_the_author() -> None:
    selector = QuotaAwareModelSelector()
    author = await selector.select(LINE, [], role=ModelRole.WORK)
    reviewer = await selector.select(LINE, [author], role=ModelRole.REVIEW)
    assert author is not None
    assert reviewer is not None
    assert reviewer != author


async def test_review_is_the_primary_reviewer_then_the_read_only_secondary_pool() -> None:
    from src.server.services.autonomy.roles import RolePolicy

    policy = RolePolicy.default()
    reviewers = eligible_models("review", policy)
    expected = tuple(
        name
        for name in (*policy.primary_reviewer, *policy.secondary_reviewer_pool)
        if name not in REVIEW_INELIGIBLE and name in READ_ONLY_CAPABLE
    )
    assert reviewers == expected
    assert reviewers == ("codex", "stepfun")
    assert "grok" not in reviewers
    assert "cursor" not in reviewers
    assert not set(reviewers) & set(REVIEW_INELIGIBLE)
    assert set(REVIEW_INELIGIBLE) <= set(eligible_models("work", policy))
    assert all(REVIEW_INELIGIBLE.values()), "every exclusion carries its reason"
    assert DEV_MODEL_POOL == (
        "codex",
        "claude",
        "kimi",
        "deepseek",
        "glm",
        "minimax",
        "stepfun",
        "doubao",
    )
    assert MODEL_POOL is DEV_MODEL_POOL


@pytest.mark.parametrize("model", ("codex", "grok", "cursor", "stepfun", "kimi", "claude", "doubao"))
async def test_the_review_role_returns_only_the_primary_or_secondary_pool(model: str) -> None:
    roster = eligible_models("review")
    others = {other: 0.0 for other in (*roster, model) if other != model}
    selector = QuotaAwareModelSelector(Headroom(others))

    picked = await selector.select(LINE, [], role=ModelRole.REVIEW)

    assert picked == (model if model in roster else None)


async def test_a_model_the_operator_switched_off_never_reviews_but_can_still_work() -> None:
    from src.tools.auto_dispatch.selection import NO_SECONDARY_REVIEWER

    def refuse_stepfun(model: str) -> bool:
        return model == "stepfun"

    unrestricted = QuotaAwareModelSelector()
    assert await unrestricted.select(LINE, ["codex"], role=ModelRole.REVIEW) == "stepfun"
    restricted = QuotaAwareModelSelector(review_refused=refuse_stepfun)
    picked = await restricted.select(LINE, ["codex"], role=ModelRole.REVIEW)
    assert picked is None
    assert restricted.review_refusal == NO_SECONDARY_REVIEWER
    work = [await restricted.select(LINE, []) for _ in range(len(eligible_models("work")))]
    assert "grok" in work
    assert "cursor" in work
    assert "claude" not in work
    assert "codex" not in work


async def test_a_model_with_recorded_success_ranks_before_a_model_with_no_data() -> None:
    # glm's recorded 0.0 still outranks kimi, which has no row, despite roster order.
    selector = QuotaAwareModelSelector(stats=Stats({"glm": 0.0}))
    assert await selector.select(LINE, []) == "glm"


async def test_a_proven_model_outranks_an_untried_one() -> None:
    selector = QuotaAwareModelSelector(stats=Stats({"deepseek": 1.0}))
    assert await selector.select(LINE, []) == "deepseek"


async def test_release_gives_an_assignment_back() -> None:
    selector = QuotaAwareModelSelector()
    first = await selector.select(LINE, [])
    selector.release(first)
    second = await selector.select(LINE, [])
    assert first == second


@pytest.mark.parametrize("role", [ModelRole.WORK, ModelRole.REVIEW, ModelRole.RESEARCH])
async def test_excluded_models_are_never_returned(role: ModelRole) -> None:
    selector = QuotaAwareModelSelector()
    for _ in range(len(eligible_models(role.value)) + 1):
        assert await selector.select(LINE, [], role=role) not in ("agy",)


async def test_work_never_selects_the_reserved_planner_or_primary_reviewer() -> None:
    selector = QuotaAwareModelSelector()
    roster = eligible_models("work")
    chosen = [await selector.select(LINE, []) for _ in range(len(roster) * 2)]
    assert "claude" not in chosen
    assert "codex" not in chosen
    assert "cursor" in chosen
    assert "grok" in chosen


async def test_allow_reserved_code_can_select_a_reserved_model_and_audits_it() -> None:
    from src.server.services.autonomy.roles import RolePolicy

    policy = RolePolicy(allow_reserved_code=True)
    roster = eligible_models("work", policy)
    blocked = {model: 0.0 for model in roster if model not in policy.reserved_providers()}
    line = Line(id="line-1", name="implement", task="write it")
    selector = QuotaAwareModelSelector(Headroom(blocked), policy=policy)

    chosen = await selector.select(line, [])

    assert chosen == "claude"
    assert line.assignment_audit == (
        "allow_reserved_code: claude is reserved for planning or review "
        "and was selected for work"
    )


async def test_review_prefers_codex_then_falls_back_and_refuses_the_author() -> None:
    from src.tools.auto_dispatch.selection import NO_SECONDARY_REVIEWER

    selector = QuotaAwareModelSelector()
    assert await selector.select(LINE, [], role=ModelRole.REVIEW) == "codex"
    assert await selector.select(LINE, ["codex"], role=ModelRole.REVIEW) == "stepfun"
    # step is the same CLI as stepfun, so it cannot review that author's line.
    blocked = QuotaAwareModelSelector(Headroom({"codex": 0.0}))
    assert await blocked.select(LINE, ["step"], role=ModelRole.REVIEW) is None
    assert blocked.review_refusal == NO_SECONDARY_REVIEWER
    for name in REVIEW_INELIGIBLE:
        assert await selector.select(LINE, [], role=ModelRole.REVIEW) != name


async def test_review_keeps_the_primary_ahead_of_a_higher_secondary_rate() -> None:
    selector = QuotaAwareModelSelector(stats=Stats({"codex": 0.1, "stepfun": 1.0, "grok": 1.0}))
    assert await selector.select(LINE, [], role=ModelRole.REVIEW) == "codex"


async def test_repeated_reviews_do_not_leave_the_read_only_primary() -> None:
    selector = QuotaAwareModelSelector()
    assert await selector.select(LINE, ["kimi"], role=ModelRole.REVIEW) == "codex"
    assert await selector.select(LINE, ["kimi"], role=ModelRole.REVIEW) == "codex"


async def test_review_falls_back_when_the_primary_is_disabled_exhausted_or_the_author() -> None:
    disabled = QuotaAwareModelSelector(review_refused=lambda model: model == "codex")
    assert await disabled.select(LINE, [], role=ModelRole.REVIEW) == "stepfun"
    exhausted = QuotaAwareModelSelector(Headroom({"codex": 0.0}))
    assert await exhausted.select(LINE, [], role=ModelRole.REVIEW) == "stepfun"
    authored = QuotaAwareModelSelector()
    assert await authored.select(LINE, ["codex"], role=ModelRole.REVIEW) == "stepfun"


async def test_read_only_capable_matches_the_real_adapter_and_the_selector_falls_back() -> None:
    from types import SimpleNamespace

    from src.llms.native_cli.base import ReadOnlyUnavailable
    from src.llms.native_cli.registry import get_read_only_adapter
    from src.server.services.autonomy.roles import RolePolicy, canonical_provider
    from src.server.services.dispatch import NativeReviewBackend

    names = set(DEV_MODEL_POOL) | set(RolePolicy.default().developer_pool) | {
        "grok",
        "cursor",
        "step",
        "claudedev",
        "codexrev",
    }
    capable: set[str] = set()
    for name in names:
        try:
            get_read_only_adapter(name)
        except ReadOnlyUnavailable:
            continue
        except Exception:
            continue
        capable.add(canonical_provider(name))
    assert capable == set(READ_ONLY_CAPABLE)

    class _FakeAdapter:
        async def run(self, prompt: str) -> SimpleNamespace:
            return SimpleNamespace(text="ok")

    def fake_read_only(provider: str, **kwargs: object) -> _FakeAdapter:
        if provider in {"grok", "cursor"}:
            raise ReadOnlyUnavailable(f"{provider!r} has no read-only mode")
        return _FakeAdapter()

    # The shipped selector already excluded grok and cursor. Execution of the
    # pick goes through NativeReviewBackend, which calls the read-only adapter.
    import src.llms.native_cli as native_cli

    original = native_cli.get_read_only_adapter
    native_cli.get_read_only_adapter = fake_read_only
    try:
        selector = QuotaAwareModelSelector(Headroom({"codex": 0.0}))
        picked = await selector.select(LINE, [], role=ModelRole.REVIEW)
        assert picked == "stepfun"
        assert await NativeReviewBackend().review(picked, "look") == "ok"
        try:
            await NativeReviewBackend().review("grok", "look")
        except ReadOnlyUnavailable:
            pass
        else:
            raise AssertionError("an incapable reviewer must fail at the CLI boundary")
    finally:
        native_cli.get_read_only_adapter = original


async def test_policy_work_roster_matches_eligible_models() -> None:
    from src.server.services.autonomy.roles import RolePolicy

    policy = RolePolicy.default()
    assert eligible_models("work", policy) == (
        "kimi",
        "glm",
        "grok",
        "cursor",
        "stepfun",
        "deepseek",
        "minimax",
        "doubao",
    )
    assert eligible_models("work", policy) == tuple(
        name
        for name in (*policy.developer_pool, *policy.work_tail)
        if name not in policy.reserved_providers()
    )
    assert eligible_models("review", policy) == ("codex", "stepfun")


async def test_an_empty_usage_snapshot_stays_unlimited() -> None:
    from src.tools.auto_dispatch.selection import SnapshotOrUnlimitedHeadroom, UnlimitedHeadroom

    async def empty():
        return {}

    port = SnapshotOrUnlimitedHeadroom(loader=empty)
    assert await port.remaining("kimi") == 1.0
    assert isinstance(port.resolved(), UnlimitedHeadroom)


async def test_usage_snapshot_headroom_maps_exhausted_and_missing() -> None:
    from src.tools.auto_dispatch.selection import UsageSnapshotHeadroom

    port = UsageSnapshotHeadroom({"kimi": "exhausted", "grok": "low", "deepseek-dsh": "available"})
    assert port.source.startswith("native_runtime.usage_headroom")
    assert await port.remaining("kimi") == 0.0
    assert await port.remaining("grok") == 0.25
    assert await port.remaining("deepseek") == 1.0
    assert await port.remaining("cursor") == 1.0


def _only_doubao_has_quota() -> Headroom:
    roster = eligible_models("work")
    return Headroom({model: 0.0 for model in roster if model != "doubao"})


async def test_a_write_line_is_not_given_to_text_only_doubao() -> None:
    """Other workers at quota 0 must not hand a write line to Doubao."""
    from src.server.services.native_runtime.registry import capabilities_for

    assert capabilities_for("doubao").workspace_files is False
    selector = QuotaAwareModelSelector(_only_doubao_has_quota())
    line = Line(id="line-0", name="edit", task="edit the file", write_scope=("src/a.py",))
    assert await selector.select(line, []) is None
    assert selector.work_refusal == "text_only_worker"


async def test_a_text_line_can_still_use_doubao() -> None:
    selector = QuotaAwareModelSelector(_only_doubao_has_quota())
    assert await selector.select(LINE, []) == "doubao"
    assert selector.work_refusal is None


async def test_a_review_can_still_use_doubao() -> None:
    from src.server.services.autonomy.roles import RolePolicy

    policy = RolePolicy(secondary_reviewer_pool=("doubao",))
    selector = QuotaAwareModelSelector(Headroom({"codex": 0.0}), policy=policy)
    line = Line(id="line-0", name="edit", task="edit the file", write_scope=("src/a.py",))
    assert await selector.select(line, [], role=ModelRole.REVIEW) == "doubao"
    assert selector.work_refusal is None


pytestmark = pytest.mark.asyncio

