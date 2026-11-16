"""The model pool a dispatch may draw from, and the rules that shrink it.

Live assignment comes only from ``RolePolicy`` via ``eligible_models``.
``DEV_MODEL_POOL`` is the old round-robin order used by ``RoundRobinSelector``
and by the read-only adapter tests. ``MODEL_POOL`` is a deprecated alias of
that same tuple so existing imports keep working; it is not the live roster.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

# Dev ``RoundRobinSelector`` walks this order and applies no role rules.
# The read-only adapter tests also parametrize this exact tuple.
DEV_MODEL_POOL: tuple[str, ...] = (
    "codex",
    "claude",
    "kimi",
    "deepseek",
    "glm",
    "minimax",
    "stepfun",
    "doubao",
)
# Deprecated alias of ``DEV_MODEL_POOL``. Not the live roster.
MODEL_POOL = DEV_MODEL_POOL

# Dispatch names whose CLI has a forced read-only mode. A test locks this set
# to ``get_read_only_adapter``. grok and cursor are absent until v2.
READ_ONLY_CAPABLE = frozenset({"codex", "claude", "stepfun", "doubao"})

# Eligible only where the role is RESEARCH: never a work lane.
RESEARCH_ONLY_MODELS = frozenset({"manus"})

# Never eligible for any role.
EXCLUDED_MODELS = frozenset({"agy"})

# Never eligible to REVIEW, each with why. A review runs the model's CLI read-only
# (``get_read_only_adapter`` in ``src/llms/native_cli/registry.py``), and these CLIs
# have no read-only mode an adapter can select and that holds. They stay in the pool for
# work lanes, which launch the CLI with its full tool set. The registry refuses the same
# models, and a test keeps the two lists equal, so this package never imports an adapter.
REVIEW_INELIGIBLE: Mapping[str, str] = MappingProxyType(
    {
        "kimi": "one-shot --prompt cannot be combined with --plan and runs under the auto permission policy",
        "deepseek": "the dsh headless profile has no read-only switch; the profile decides its tools",
        "glm": "its one candidate, --permission-mode plan, is the setting Claude Code's same-named mode showed to be advice rather than a boundary, and zcode-cli is not installed here to try a write",
        "minimax": "mcode exec offers only the smart, full and off permission policies; none is documented as read-only",
    }
)

# A model whose remaining quota is at or below this is treated as exhausted.
MIN_HEADROOM = 0.0


def eligible_models(role: str, policy: Any = None) -> tuple[str, ...]:
    """The policy roster filtered for one role, in roster order.

    ``policy`` defaults to ``load_role_policy()`` (the built-in v1 roster, or
    ``CONDUCTOR_ROLE_POLICY_FILE``). Work is the developer pool, then the work
    tail. The reserved planner and primary reviewer are omitted unless
    ``allow_reserved_code`` is set. Review is the primary reviewer, then the
    secondary pool, and only names that can run a read-only review and are
    not in ``REVIEW_INELIGIBLE``. Research is the
    work roster plus the reserved models plus the research-only names.
    """
    if policy is None:
        from src.server.services.autonomy.roles import load_role_policy

        policy = load_role_policy()
    if role == "review":
        return _review_models(policy)
    work = _work_models(policy)
    if role == "research":
        return _research_models(policy, work)
    return work


def _canon(name: str) -> str:
    from src.server.services.autonomy.roles import canonical_provider

    return canonical_provider(name)


def _work_models(policy: Any) -> tuple[str, ...]:
    reserved = {_canon(name) for name in policy.reserved_providers()}
    ordered: list[str] = []
    for name in (*policy.developer_pool, *policy.work_tail):
        canon = _canon(name)
        if canon in EXCLUDED_MODELS or canon in ordered:
            continue
        if canon in reserved and not policy.allow_reserved_code:
            continue
        ordered.append(canon)
    if policy.allow_reserved_code:
        for name in (*policy.planner, *policy.primary_reviewer):
            canon = _canon(name)
            if canon not in ordered and canon not in EXCLUDED_MODELS:
                ordered.append(canon)
    return tuple(ordered)


def _review_models(policy: Any) -> tuple[str, ...]:
    ordered: list[str] = []
    for name in (*policy.primary_reviewer, *policy.secondary_reviewer_pool):
        canon = _canon(name)
        if canon in REVIEW_INELIGIBLE or canon in EXCLUDED_MODELS or canon in ordered:
            continue
        if canon not in READ_ONLY_CAPABLE:
            continue
        ordered.append(canon)
    return tuple(ordered)


def _research_models(policy: Any, work: tuple[str, ...]) -> tuple[str, ...]:
    ordered = list(work)
    for name in (*policy.planner, *policy.primary_reviewer, *tuple(sorted(RESEARCH_ONLY_MODELS))):
        canon = _canon(name)
        if canon not in ordered and canon not in EXCLUDED_MODELS:
            ordered.append(canon)
    return tuple(ordered)


def review_roster_gaps(policy: Any = None) -> dict[str, str]:
    """Each secondary-pool member that is not on the review roster, with why (N63-1).

    A risk-bearing line needs a secondary review from this pool. A name the
    policy lists that ``eligible_models("review")`` drops is silently no
    reviewer at all, so a pool that looks like three reviewers can be one.
    Keys are canonical names, in pool order.
    """
    if policy is None:
        from src.server.services.autonomy.roles import load_role_policy

        policy = load_role_policy()
    roster = set(_review_models(policy))
    gaps: dict[str, str] = {}
    for name in policy.secondary_reviewer_pool:
        canon = _canon(name)
        if canon in roster or canon in gaps:
            continue
        if canon in EXCLUDED_MODELS:
            gaps[canon] = "excluded from every role"
        elif canon in REVIEW_INELIGIBLE:
            gaps[canon] = f"review-ineligible: {REVIEW_INELIGIBLE[canon]}"
        elif canon not in READ_ONLY_CAPABLE:
            gaps[canon] = "its CLI has no read-only mode"
        else:
            gaps[canon] = "not on the review roster"
    return gaps


def review_roster_check(policy: Any = None) -> dict[str, object]:
    """A setup check in the doctor's shape: ``{"name", "ok", "detail"}``.

    Not ok when a secondary-pool member is not on the review roster, or when
    no roster member other than the primary reviewer is left for a
    risk-bearing line's secondary review. A policy that cannot be loaded is
    not ok either; the detail names the error type only.
    """
    try:
        if policy is None:
            from src.server.services.autonomy.roles import load_role_policy

            policy = load_role_policy()
        gaps = review_roster_gaps(policy)
        primary = {_canon(name) for name in policy.primary_reviewer}
        secondaries = [name for name in _review_models(policy) if name not in primary]
    except Exception as exc:  # a setup check reports, it does not raise
        return {
            "name": "review_roster",
            "ok": False,
            "detail": f"role policy could not be read ({type(exc).__name__})",
        }
    problems = [f"{name}: {why}" for name, why in gaps.items()]
    if not secondaries:
        problems.append("no secondary reviewer besides the primary on the review roster")
    if problems:
        return {
            "name": "review_roster",
            "ok": False,
            "detail": "secondary review pool: " + "; ".join(problems),
        }
    return {
        "name": "review_roster",
        "ok": True,
        "detail": "secondary reviewers: " + ", ".join(secondaries),
    }
