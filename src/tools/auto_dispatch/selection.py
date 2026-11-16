"""Quota-aware model assignment for auto-dispatch lines."""

from __future__ import annotations

import logging
import random
from contextlib import AbstractAsyncContextManager, nullcontext
import os
from collections.abc import Callable
from collections.abc import Collection, Iterable, Mapping
from typing import Any, Protocol

from src.server.services.autonomy.role_plan import (
    Assignment,
    AssignmentPlan,
    TaskSpec,
    plan_developer_assignments,
)
from src.server.services.autonomy.roles import AgentState, RolePolicy

from .path_ownership import (
    ALLOW,
    PATH_OWNERSHIP_CONTEXT_REQUIRED,
    OwnershipDecision,
    PathOwnership,
)
from .human_pin import PINNED_WORKER_UNUSABLE, work_pick
from . import soft_select
from .pool import MIN_HEADROOM, eligible_models
from .protocols import ModelRole
from .task_category import line_category
from .types import Dispatch, Line, LineStatus

logger = logging.getLogger(__name__)

# Stable reason when review has no usable primary and no usable secondary.
NO_SECONDARY_REVIEWER = "no_secondary_reviewer"


def _default_policy() -> Any:
    from src.server.services.autonomy.roles import load_role_policy

    return load_role_policy()


#: L2: the workers one dispatch may use, kept on its row under this gates key.
#: Absent or empty means every worker the role policy allows.
ALLOWED_WORKERS = "allowed_workers"
#: Why a reassign or a planned line was refused by that restriction.
WORKER_NOT_ALLOWED = "worker_not_allowed"


def normalise_workers(values: Iterable[object] | str | None) -> tuple[str, ...]:
    """Canonical provider names, in the order given, without repeats or blanks.

    A comma-separated string is accepted so the CLI and the API share one
    spelling (``--workers kimi,glm``).
    """
    from src.server.services.autonomy.roles import canonical_provider

    if values is None:
        return ()
    items = values.split(",") if isinstance(values, str) else list(values)
    seen: list[str] = []
    for item in items:
        name = canonical_provider(str(item or "").strip().lower())
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


def dispatch_allowed_workers(dispatch: Dispatch | None) -> frozenset[str]:
    """The restriction stored on a dispatch row; empty means unrestricted."""
    gates = getattr(dispatch, "gates", None)
    if not isinstance(gates, Mapping):
        return frozenset()
    return frozenset(normalise_workers(gates.get(ALLOWED_WORKERS) or ()))


def worker_allowed(model: str | None, allowed: Collection[str]) -> bool:
    """Whether ``model`` may take work under ``allowed`` (empty allows all)."""
    if not allowed:
        return True
    from src.server.services.autonomy.roles import canonical_provider

    return canonical_provider(str(model or "").strip().lower()) in set(allowed)


class HeadroomPort(Protocol):
    """How much of a model's quota is left, as a fraction in [0, 1]."""

    async def remaining(self, model: str) -> float: ...


class UnlimitedHeadroom:
    """The default when no quota reader is wired: every model looks wide open."""

    async def remaining(self, model: str) -> float:
        return 1.0

    async def plan_status(self, model: str) -> dict | None:
        return None


# Set to 1/true/yes/on to let a reviewer whose plan is full review on paid
# credits. Read at call time, so an operator can flip it without a restart.
ALLOW_PAID_CREDITS_ENV = "CONDUCTOR_DISPATCH_ALLOW_PAID_CREDITS"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})

# ``review_blockers`` reasons.
PLAN_EXHAUSTED = "plan_exhausted"
NO_HEADROOM = "no_headroom"


def paid_credits_allowed() -> bool:
    """Whether review may spend paid credits once a plan is full (N63)."""
    # N73 S1: on by default; ``=0`` (or false/no/off) turns it off.
    return os.environ.get(ALLOW_PAID_CREDITS_ENV, "").strip().lower() not in _FALSY


class SuccessStatsPort(Protocol):
    """A model's recorded pass rate, or None when nothing is recorded yet."""

    async def success_rate(self, model: str) -> float | None: ...


class CostStatsPort(Protocol):
    """A model's cost per merged line in wall seconds (N30), or None.

    None means not enough merged lines to say; such a model is not adjusted.
    """

    async def cost_per_merged_line(self, model: str) -> float | None: ...


#: N30: the largest share the cost input may move a success rate by.
MAX_COST_WEIGHT = 0.5


def cost_penalties(costs: Mapping[str, float | None], weight: float) -> dict[str, float]:
    """``weight * clamp(log2(cost / median), -1, 1)`` per model that has data.

    The median is taken over the candidates that have a cost, so the input
    is relative to who else could take the line. A model without a cost, a
    non-positive cost or a zero weight gets no entry: it is not adjusted.
    """
    import math
    import statistics

    known = {
        model: float(value)
        for model, value in costs.items()
        if isinstance(value, (int, float)) and math.isfinite(value) and value > 0
    }
    if weight <= 0 or not known:
        return {}
    median = statistics.median(known.values())
    if median <= 0:
        return {}
    return {
        model: weight * max(-1.0, min(1.0, math.log2(value / median)))
        for model, value in known.items()
    }


# CodexBar snapshot states from ``load_usage_headroom``, as a fraction of
# remaining quota. Unknown and missing names are not evidence of exhaustion.
_SNAPSHOT_FRACTION = {
    "available": 1.0,
    "low": 0.25,
    "exhausted": 0.0,
    "unknown": 1.0,
}


def _snapshot_keys(model: str) -> tuple[str, ...]:
    """Every spelling of the same CLI, so an alias reads its own quota.

    A Worker, a policy file and the snapshot may each spell one vendor CLI
    differently (glm / zcode / zhipu / zai are one CLI). The spellings come
    from ``roles.provider_alias_spellings`` -- the one alias table -- so
    ``remaining`` finds the state whichever spelling the snapshot used and
    whichever spelling the caller passed, and an exhausted CLI is never
    reported open just because the two sides spelled it apart.
    """
    from src.server.services.autonomy.roles import provider_alias_spellings

    return provider_alias_spellings(model)


class UsageSnapshotHeadroom:
    """``HeadroomPort`` over the CodexBar snapshot native routing already reads.

    The numbers come from ``src.server.services.native_runtime.usage_headroom.load_usage_headroom``
    (the ``usage_headroom`` snapshot: provider, window used-percent, freshness).
    A disabled, empty or unreadable snapshot answers 1.0 for every model, the
    same figure ``UnlimitedHeadroom`` returns. ``exhausted`` is 0 and is skipped
    by the selector; ``low`` stays eligible.

    ``plan_status`` answers from ``load_usage_plan_status`` (plan full, paid
    credits left, reset time), loaded lazily once. ``plan_loader`` injects it
    for tests; when ``loader`` is injected without ``plan_loader`` there is no
    plan data, so a test never reads the real snapshot by accident.
    """

    source = "native_runtime.usage_headroom.load_usage_headroom"

    def __init__(
        self,
        states: dict[str, str] | None = None,
        *,
        loader: Callable[[], Any] | None = None,
        plan: dict[str, dict] | None = None,
        plan_loader: Callable[[], Any] | None = None,
    ) -> None:
        self._states = dict(states) if states is not None else None
        self._loader = loader
        if plan is None and plan_loader is None and (states is not None or loader is not None):
            plan = {}
        self._plan = dict(plan) if plan is not None else None
        self._plan_loader = plan_loader

    async def remaining(self, model: str) -> float:
        states = await self._ensure_states()
        for key in _snapshot_keys(model):
            state = states.get(key)
            if state is not None:
                return _SNAPSHOT_FRACTION.get(state, 1.0)
        return 1.0

    async def plan_status(self, model: str) -> dict | None:
        """``{"plan_full", "credits", "resets_at"}`` for this model, or None."""
        plan = await self._ensure_plan()
        for key in _snapshot_keys(model):
            status = plan.get(key)
            if isinstance(status, dict):
                return dict(status)
        return None

    async def _ensure_plan(self) -> dict[str, dict]:
        if self._plan is not None:
            return self._plan
        self._plan = await self._read_plan()
        return self._plan

    async def _read_plan(self) -> dict[str, dict]:
        if self._plan_loader is not None:
            loaded = self._plan_loader()
            if hasattr(loaded, "__await__"):
                loaded = await loaded
            return dict(loaded or {})
        try:
            from src.server.services.native_runtime.usage_headroom import (
                load_usage_plan_status,
            )

            loaded = await load_usage_plan_status()
        except Exception:
            logger.warning("usage plan status could not be read", exc_info=True)
            return {}
        return dict(loaded or {})

    async def _ensure_states(self) -> dict[str, str]:
        if self._states is not None:
            return self._states
        self._states = await self._read()
        return self._states

    async def _read(self) -> dict[str, str]:
        if self._loader is not None:
            loaded = self._loader()
            if hasattr(loaded, "__await__"):
                loaded = await loaded
            return dict(loaded or {})
        try:
            from src.server.services.native_runtime.usage_headroom import load_usage_headroom

            loaded = await load_usage_headroom()
        except Exception:
            logger.warning("usage headroom snapshot could not be read", exc_info=True)
            return {}
        return dict(loaded or {})


class SnapshotOrUnlimitedHeadroom:
    """Real snapshot headroom when the snapshot has rows, else unlimited.

    Constructing this does no I/O. The first ``remaining`` call loads the
    snapshot; an empty result is kept as ``UnlimitedHeadroom``.
    """

    def __init__(
        self,
        loader: Callable[[], Any] | None = None,
        *,
        plan_loader: Callable[[], Any] | None = None,
    ) -> None:
        self._loader = loader
        self._plan_loader = plan_loader
        self._inner: HeadroomPort | None = None

    async def _resolve(self) -> HeadroomPort:
        if self._inner is None:
            probe = UsageSnapshotHeadroom(loader=self._loader, plan_loader=self._plan_loader)
            states = await probe._read()
            if states:
                probe._states = states
                self._inner = probe
            else:
                self._inner = UnlimitedHeadroom()
        return self._inner

    async def remaining(self, model: str) -> float:
        return await (await self._resolve()).remaining(model)

    async def plan_status(self, model: str) -> dict | None:
        """The snapshot's plan status, or None when the snapshot had no rows."""
        inner = await self._resolve()
        reader = getattr(inner, "plan_status", None)
        return await reader(model) if reader is not None else None

    def resolved(self) -> HeadroomPort | None:
        """The port chosen after the first read, if a read has happened."""
        return self._inner


class MostConstrainedHeadroom:
    """The smallest remaining fraction any of several ports reports.

    The CodexBar window file and the native-runtime usage snapshot see
    different things (window percentages; DeepSeek's balance), so the live
    default asks both and believes the more exhausted one. A port that raises
    is skipped for that read rather than stopping a dispatch: "could not read"
    is not "spent", the same answer every port gives a Worker it does not know.
    """

    def __init__(self, *ports: HeadroomPort) -> None:
        self.ports = tuple(ports)

    async def remaining(self, model: str) -> float:
        lowest = 1.0
        for port in self.ports:
            try:
                value = float(await port.remaining(model))
            except Exception:
                logger.warning("headroom read failed for %s", model, exc_info=True)
                continue
            if value == value:  # NaN is not a reading
                lowest = min(lowest, value)
        return lowest

    async def plan_status(self, model: str) -> dict | None:
        """Every port's plan status merged, most constrained first; None when none answers.

        Merged rather than passed through to the first port that has the
        method: the ports read the same quota with different freshness limits
        and different row filters, so the first port can have a not-full (or
        no) row while the second sees the plan full. Passing the first answer
        through would let review spend paid credits with the flag off, which
        is the opposite of ``remaining``'s rule that the more exhausted reading
        wins. So: the plan is full when any port says so; credits count only
        when every port that saw the plan full also saw credits (otherwise
        whether a full plan was seen at all); ``resets_at`` is the latest reset
        among the full readings. A port that raises or has no ``plan_status``
        is skipped, as in ``remaining``.
        """
        statuses: list[dict] = []
        for port in self.ports:
            reader = getattr(port, "plan_status", None)
            if reader is None:
                continue
            try:
                status = await reader(model)
            except Exception:
                logger.warning("plan status read failed for %s", model, exc_info=True)
                continue
            if isinstance(status, dict):
                statuses.append(status)
        if not statuses:
            return None
        full = [status for status in statuses if status.get("plan_full")]
        if full:
            credits = all(bool(status.get("credits")) for status in full)
        else:
            credits = any(bool(status.get("credits")) for status in statuses)
        return {
            "plan_full": bool(full),
            "credits": credits,
            "resets_at": _latest_reset(status.get("resets_at") for status in full),
        }


def _latest_reset(values: Iterable[Any]) -> str | None:
    """The latest of several ISO reset times, compared as instants, not strings."""
    from datetime import datetime, timezone

    latest: tuple[datetime, str] | None = None
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if latest is None or parsed > latest[0]:
            latest = (parsed, value)
    return latest[1] if latest is not None else None


_SOFT_AUDIT_MAX = 32


class QuotaAwareModelSelector:
    """Picks the eligible model with the fewest assignments and the most headroom.

    Ordering is deterministic and needs no randomness: least-assigned first,
    then a model that has a recorded success rate ahead of one that has none,
    then the higher rate, then roster order. A model with no headroom is
    skipped rather than tried and failed, and the pool is only exhausted when
    every eligible model is either out of quota or already taken by a role that
    forbids reuse. A ``large`` task (``estimated_size`` or ``risk_bearing``)
    ranks by the usage-plan headroom tier first, then the least-assigned
    spread, then remaining quota, so heavy work lands on a provider with quota
    to spare rather than on whichever name comes first in the roster or holds
    one line fewer. The model a large line is being reassigned off ranks last,
    which is what still moves a live reassignment onto someone else.

    Work never takes the reserved planner or primary reviewer unless the
    policy sets ``allow_reserved_code``. That choice writes ``assignment_audit``
    on the line, which the dispatch row stores with the line. Identities are
    canonicalized first, so an alias cannot skip that rule or review its own
    work. Review tries an available primary reviewer before any secondary, and
    never a model that cannot run read-only. When no secondary remains,
    ``review_refusal`` is ``no_secondary_reviewer`` and the result is None.

    Review also treats a model whose plan is full as out of headroom even when
    paid credits keep its snapshot state ``low`` (N63): work billed to credits
    keeps running, but a reviewer on a spent plan fails its reviews. Setting
    ``CONDUCTOR_DISPATCH_ALLOW_PAID_CREDITS`` lets review spend the credits.
    ``review_blockers`` names the reviewers held back by headroom and why.

    ``review_refused`` names models the deployment will not run as a reviewer (the
    operator switched their CLI off). Skipping them here is what lets a review go to
    another model; refusing only at the CLI would end every dispatch the pick reached.
    """

    def __init__(
        self,
        headroom: HeadroomPort | None = None,
        stats: SuccessStatsPort | None = None,
        *,
        min_headroom: float = MIN_HEADROOM,
        review_refused: Callable[[str], bool] | None = None,
        policy: Any = None,
        path_ownership: PathOwnership | None = None,
        allowed_workers: Collection[str] | None = None,
        cost_stats: CostStatsPort | None = None,
        cost_weight: float = 0.0,
        rng: random.Random | None = None,
    ) -> None:
        self._headroom = headroom or UnlimitedHeadroom()
        #: N30: a soft input on the WORK path, off unless both are set.
        self._cost_stats = cost_stats
        self._cost_weight = max(0.0, min(MAX_COST_WEIGHT, float(cost_weight or 0.0)))
        #: R6 L4: soft-weighted sampling (``CONDUCTOR_DISPATCH_SELECTOR_MODE``).
        self._rng = rng or random.Random()
        #: Owner preference from the role registry, set by the machine per line.
        self.owner_preference: tuple[str, ...] = ()
        #: Last soft decision and a bounded audit of them (shadow and enforce).
        self.last_soft_choice: dict[str, Any] | None = None
        self.soft_audit: list[dict[str, Any]] = []
        #: L2: the dispatch's own worker restriction. It narrows work picks
        #: only; reviewers are chosen by the review rules as before.
        self._allowed_workers = frozenset(normalise_workers(allowed_workers or ()))
        self._stats = stats
        self._min_headroom = min_headroom
        self._review_refused = review_refused or (lambda model: False)
        self._policy = policy if policy is not None else _default_policy()
        self._path_ownership = path_ownership
        self._assignments: dict[str, int] = {}
        self.review_refusal: str | None = None
        # Set when a write line dropped a text-only Worker and nobody remained.
        self.work_refusal: str | None = None
        self.work_decision: str | None = None
        self.path_ownership_decision: OwnershipDecision | None = None

    def path_claim_guard(self) -> AbstractAsyncContextManager[object]:
        """The lock the machine holds across path checks and the assign save."""
        if self._path_ownership is None:
            return nullcontext()
        return self._path_ownership.guard()

    async def check_path_ownership(
        self,
        line: Line,
        *,
        dispatch: Dispatch | None = None,
        dispatch_id: str | None = None,
    ) -> OwnershipDecision:
        """Arbitrate one work line before either planner or live assignment."""
        if self._path_ownership is None:
            return OwnershipDecision(ALLOW)
        previous = self.path_ownership_decision
        owner_id = dispatch.id if dispatch is not None else dispatch_id
        if not owner_id:
            decision = OwnershipDecision(
                "refuse", f"{PATH_OWNERSHIP_CONTEXT_REQUIRED}:{line.id}"
            )
            self._path_ownership.decisions.append(decision)
            line.failure_reason = decision.reason
        else:
            decision = await self._path_ownership.decide(owner_id, line, dispatch)
        self.path_ownership_decision = decision
        self.work_decision = decision.action
        if decision.allowed:
            if previous is not None and not previous.allowed:
                self.work_refusal = None
        else:
            self.work_refusal = decision.reason
            logger.info("path ownership %s: %s", decision.action, decision.reason)
        return decision

    async def select(
        self,
        line: Line,
        used_models: list[str],
        *,
        role: ModelRole = ModelRole.WORK,
        exclude: Collection[str] = (),
        dispatch: Dispatch | None = None,
        dispatch_id: str | None = None,
    ) -> str | None:
        from src.server.services.autonomy.roles import (
            can_take_line,
            canonical_provider,
            headroom_tier,
        )

        # A hard exclusion for every role (N25a): a provider skipped for new
        # launches, or the model a line is moving off, is never returned.
        # ``used_models`` stays a review-only rule; work may share a model.
        excluded = {canonical_provider(str(model)) for model in exclude if model}
        if role is ModelRole.REVIEW:
            self.work_refusal = None
            return await self._select_review(line, [*used_models, *excluded])
        # Only a line that has not started yet asks for its paths. A line that
        # is already active (a reassign, a fix author's replacement) holds its
        # claim; re-arbitrating it would queue or refuse work in flight.
        if line.status is LineStatus.PENDING:
            ownership = await self.check_path_ownership(
                line, dispatch=dispatch, dispatch_id=dispatch_id
            )
            if not ownership.allowed:
                return None
        else:
            self.work_decision = None
            self.path_ownership_decision = None
        # N86: a line a person reassigned keeps that worker. No automatic
        # path (a move off a limited provider, a liveness reassign) may seat
        # anyone else; an excluded pin answers nobody (human_pin.work_pick).
        pin = work_pick(dispatch, line, excluded)
        if pin.handled:
            self.work_refusal = None if pin.model else PINNED_WORKER_UNUSABLE
            return pin.model

        reserved = self._policy.canonical_reserved()
        skipped_text_only = False
        # A large task ranks by quota tier before the least-assigned spread, so
        # heavy work is not seated on a nearly spent provider just because a
        # healthier one already holds another line; a smaller task keeps the
        # round-robin order and uses headroom only as a gate.
        prefer_headroom = _estimated_size_for_line(line) == "large"
        # The model this line is moving off (a live reassignment keeps the old
        # author on the line until the new one is installed). Least-assigned
        # used to be what moved a reassignment off it; once quota leads for a
        # large task, the seat is demoted explicitly instead, and it stays a
        # last resort rather than a hard exclusion.
        seated = canonical_provider(str(getattr(line, "assigned_model", "") or ""))
        candidates: list[tuple[tuple[float, ...], str]] = []
        # N30: where -rate sits in each candidate's key, so the soft cost input
        # can adjust it after the median over all candidates is known.
        rate_slots: list[tuple[int, float]] = []
        headroom_seen: dict[str, float] = {}
        restricted = False
        for index, model in enumerate(eligible_models(role.value, self._policy)):
            canon = canonical_provider(model)
            if canon in excluded:
                continue
            if self._allowed_workers and canon not in self._allowed_workers:
                restricted = True
                continue
            if (
                role is ModelRole.WORK
                and canon in reserved
                and not self._policy.allow_reserved_code
            ):
                continue
            # One capability decision for every path: can_take_line folds the
            # identity and checks a non-empty write scope against the adapter.
            # Doubao stays eligible for an empty scope and for review; its
            # capability flags are not flipped to make this pass.
            ok, reason = can_take_line(model, line)
            if not ok:
                if reason == "text_only_worker":
                    skipped_text_only = True
                logger.info("work filter: %s %s", reason, canon)
                continue
            remaining = await self._headroom.remaining(canon)
            if remaining <= self._min_headroom:
                logger.debug("Model %s skipped: headroom %s", canon, remaining)
                continue
            headroom_seen[canon] = remaining
            recorded = await self._recorded_rate(canon)
            has_data = 0 if recorded is not None else 1
            rate = 0.0 if recorded is None else recorded
            assignments = self._assignments.get(canon, 0)
            if prefer_headroom:
                # Heavy work: the usage-plan tier (high/medium/low) leads, so
                # kimi at 0.05 never takes a large line cursor at 0.95 could;
                # inside a tier the least-assigned spread and then the widest
                # remaining quota decide.
                moving_off = 1 if seated and canon == seated else 0
                key = (
                    moving_off,
                    -headroom_tier(remaining),
                    assignments,
                    -remaining,
                    has_data,
                    -rate,
                    index,
                )
                rate_slots.append((5, rate))
            else:
                key = (assignments, has_data, -rate, index)
                rate_slots.append((2, rate))
            candidates.append((key, canon))
        if not candidates:
            self.work_refusal = (
                "text_only_worker"
                if skipped_text_only
                else WORKER_NOT_ALLOWED
                if restricted
                else None
            )
            return None
        if self._cost_stats is not None and self._cost_weight > 0:
            candidates = await self._apply_cost(candidates, rate_slots)
        candidates.sort(key=lambda item: item[0])
        chosen = candidates[0][1]
        chosen = await self._soft_override(
            line, [name for _key, name in candidates], headroom_seen, chosen
        )
        self._take(chosen)
        self.review_refusal = None
        self.work_refusal = None
        self._record_reserved_audit(line, role, chosen)
        return chosen

    async def _soft_override(
        self,
        line: Line,
        ordered: list[str],
        headroom_seen: dict[str, float],
        chosen: str,
    ) -> str:
        """R6 L4: record the soft-weighted pick; return it only in enforce mode.

        Runs after every hard filter, so the soft pick is always one of the
        eligible candidates. ``off`` returns ``chosen`` without computing.
        """
        mode = soft_select.selector_mode()
        if mode == "off" or not ordered:
            return chosen
        category = line_category(line)
        table = await self._category_table(category)
        pool: list[soft_select.Candidate] = []
        for name in ordered:
            passed, runs = table["category"].get(name) or table["all"].get(name) or (0, 0)
            pool.append(
                soft_select.Candidate(
                    name=name, passed=passed, runs=runs, headroom=headroom_seen.get(name, 1.0)
                )
            )
        result = soft_select.weighted_pick(
            pool,
            rng=self._rng,
            temp=soft_select.temperature(),
            preference=self.owner_preference,
        )
        picked = result.choice or chosen
        record = {
            "line_id": line.id,
            "mode": mode,
            "category": category,
            "ordered_choice": chosen,
            "soft_choice": picked,
            **result.to_dict(),
        }
        self.last_soft_choice = record
        self.soft_audit.append(record)
        del self.soft_audit[:-_SOFT_AUDIT_MAX]
        return picked if mode == "enforce" else chosen

    async def _category_table(self, category: str) -> dict[str, dict[str, tuple[int, int]]]:
        """Per-category and overall ``(passed, runs)``; empty when stats cannot say."""
        reader = getattr(self._stats, "category_table", None)
        if reader is None:
            return {"category": {}, "all": {}}
        try:
            table = await reader(category)
        except Exception:  # stats are advisory; never fail a selection
            logger.debug("category stats unavailable", exc_info=True)
            return {"category": {}, "all": {}}
        if not isinstance(table, dict):
            return {"category": {}, "all": {}}
        return {"category": dict(table.get("category") or {}), "all": dict(table.get("all") or {})}

    async def _select_review(self, line: Line, used_models: list[str]) -> str | None:
        """Primary reviewer first. Secondaries are ranked only after that."""
        from src.server.services.autonomy.roles import canonical_provider

        taken = {canonical_provider(model) for model in used_models}
        roster = eligible_models(ModelRole.REVIEW.value, self._policy)
        primaries = {canonical_provider(name) for name in self._policy.primary_reviewer}
        for model in roster:
            if model not in primaries:
                continue
            if await self._review_blocked(model, taken):
                continue
            self._take(model)
            self.review_refusal = None
            return model
        candidates: list[tuple[int, int, float, int, str]] = []
        for index, model in enumerate(roster):
            if model in primaries or await self._review_blocked(model, taken):
                continue
            recorded = await self._recorded_rate(model)
            has_data = 0 if recorded is not None else 1
            rate = 0.0 if recorded is None else recorded
            candidates.append((self._assignments.get(model, 0), has_data, -rate, index, model))
        if not candidates:
            self.review_refusal = NO_SECONDARY_REVIEWER
            logger.info("review refusal: %s", NO_SECONDARY_REVIEWER)
            return None
        candidates.sort()
        chosen = candidates[0][4]
        self._take(chosen)
        self.review_refusal = None
        return chosen

    async def _review_blocked(self, model: str, taken: set[str]) -> bool:
        if model in taken or self._review_refused(model):
            return True
        return not await self._has_review_headroom(model)

    async def _plan_status(self, model: str) -> dict | None:
        reader = getattr(self._headroom, "plan_status", None)
        if reader is None:
            return None
        status = await reader(model)
        return status if isinstance(status, dict) else None

    async def _has_review_headroom(self, model: str) -> bool:
        if not await self._has_headroom(model):
            # A plan at 0% with paid credits left reads as no headroom, but
            # with the flag on review may run on those credits (N63-1). Only
            # an explicit plan_full + credits status counts; unknown stays out.
            if paid_credits_allowed():
                status = await self._plan_status(model)
                if (
                    status is not None
                    and status.get("plan_full")
                    and status.get("credits")
                ):
                    logger.debug("Reviewer %s on paid credits: plan full", model)
                    return True
            return False
        if paid_credits_allowed():
            return True
        status = await self._plan_status(model)
        if status is not None and status.get("plan_full"):
            logger.debug("Reviewer %s skipped: plan full, credits not allowed", model)
            return False
        return True

    async def review_blockers(
        self,
        line: Line,
        used_models: list[str],
        *,
        exclude: Collection[str] = (),
    ) -> dict[str, dict]:
        """Reviewers this line could have had but for headroom, with the reason.

        For each review-roster model not used by the line, not excluded and not
        ``review_refused``: ``{"reason": "plan_exhausted" | "no_headroom",
        "resets_at": str | None}``. Read-only: assignments and
        ``review_refusal`` are not touched.
        """
        from src.server.services.autonomy.roles import canonical_provider

        taken = {canonical_provider(str(model)) for model in [*used_models, *exclude] if model}
        blockers: dict[str, dict] = {}
        for model in eligible_models(ModelRole.REVIEW.value, self._policy):
            if model in taken or self._review_refused(model):
                continue
            if await self._has_review_headroom(model):
                continue
            status = await self._plan_status(model) or {}
            blockers[model] = {
                "reason": PLAN_EXHAUSTED if status.get("plan_full") else NO_HEADROOM,
                "resets_at": status.get("resets_at"),
            }
        return blockers

    async def _has_headroom(self, model: str) -> bool:
        remaining = await self._headroom.remaining(model)
        if remaining <= self._min_headroom:
            logger.debug("Model %s skipped: headroom %s", model, remaining)
            return False
        return True

    def _take(self, model: str) -> None:
        self._assignments[model] = self._assignments.get(model, 0) + 1

    def _record_reserved_audit(self, line: Line, role: ModelRole, chosen: str) -> None:
        if role is not ModelRole.WORK or not hasattr(line, "assignment_audit"):
            return
        if chosen not in self._policy.canonical_reserved():
            line.assignment_audit = None
            return
        from src.server.services.autonomy.roles import reserved_work_audit

        reason = reserved_work_audit(chosen)
        line.assignment_audit = reason
        logger.info("dispatch assignment audit: %s", reason)

    async def _apply_cost(
        self,
        candidates: list[tuple[tuple[float, ...], str]],
        rate_slots: list[tuple[int, float]],
    ) -> list[tuple[tuple[float, ...], str]]:
        """Swap ``-rate`` for ``-(rate - penalty)`` (N30, soft, WORK only).

        Assignments and every key ahead of the rate still lead, so the cost
        only reorders models tied on them, and it never drops a candidate. A
        cost port that fails is treated as having no data.
        """
        costs: dict[str, float | None] = {}
        for _, canon in candidates:
            try:
                costs[canon] = await self._cost_stats.cost_per_merged_line(canon)
            except Exception:  # noqa: BLE001 - a soft input must not stop a pick
                logger.debug("cost stats unavailable for %s", canon, exc_info=True)
                costs[canon] = None
        penalties = cost_penalties(costs, self._cost_weight)
        if not penalties:
            return candidates
        adjusted: list[tuple[tuple[float, ...], str]] = []
        for (key, canon), (slot, rate) in zip(candidates, rate_slots, strict=True):
            penalty = penalties.get(canon)
            if penalty is not None:
                key = (*key[:slot], -(rate - penalty), *key[slot + 1 :])
            adjusted.append((key, canon))
        return adjusted

    async def _recorded_rate(self, model: str) -> float | None:
        """The stored rate, or None when this model has no recorded run."""
        if self._stats is None:
            return None
        return await self._stats.success_rate(model)

    def release(self, model: str | None) -> None:
        """Give back an assignment when a line is abandoned rather than run."""
        if model and self._assignments.get(model):
            self._assignments[model] -= 1


# --- DAG assignment --------------------------------------------------------
# The entry point for a planner that has already produced a validated task
# DAG: dispatch ``Line`` objects in, a deterministic ``AssignmentPlan`` out.
# Everything above picks one model for one line; this hands the whole graph to
# the role planner in :mod:`role_plan`.


def dag_agents_from_policy(policy: RolePolicy | None = None) -> tuple[AgentState, ...]:
    """One agent per developer-pool name, in roster order.

    Each member of the pool gets a single free place, so a plan built from the
    policy alone is a conservative one. A caller with a live view of the
    workers passes its own agents to :func:`assign_dag` instead.
    """
    policy = policy or RolePolicy.default()
    return tuple(
        AgentState(provider=name, capacity=1) for name in policy.developer_pool
    )


_PENDING = object()
_CLEAR = object()


def _blocked_by_filtered(lines: list[Line], filtered_ids: set[str]) -> dict[str, str]:
    """Line id to the filtered line id that blocks it.

    A line is blocked when it depends, directly or through other blocked
    lines, on a line whose write scope was refused. The named id is that
    refused line, not the intermediate child. Edges are walked in
    ``depends_on`` order and lines are resolved in a stable topological order
    (the original list breaks ties). A cycle that never reaches a refused line
    is left untouched, so the role planner still reports it.
    """
    by_id = {line.id: line for line in lines}
    blocked: dict[str, str] = {}
    finished: set[str] = set()
    pending = [line for line in lines if line.id not in filtered_ids]
    # Once a strict pass adds nothing, unfinished deps are a cycle. Skip them
    # and keep a later edge that does reach a refused line.
    skipping = False

    def verdict(line: Line) -> object:
        saw_unfinished = False
        for dependency in line.depends_on:
            if dependency in filtered_ids:
                return dependency
            ancestor = blocked.get(dependency)
            if ancestor is not None:
                return ancestor
            if dependency not in by_id or dependency in finished:
                continue
            if skipping:
                saw_unfinished = True
                continue
            return _PENDING
        if saw_unfinished:
            return _PENDING
        return _CLEAR

    while pending:
        still: list[Line] = []
        moved = False
        for line in pending:
            result = verdict(line)
            if result is _PENDING:
                still.append(line)
                continue
            if isinstance(result, str):
                blocked[line.id] = result
            finished.add(line.id)
            moved = True
        if moved:
            pending = still
            continue
        if not skipping:
            skipping = True
            continue
        break
    return blocked


def _estimated_size_for_line(line: Line) -> str:
    """Task size for role-plan ranking: explicit estimate, else risk → large."""
    from src.server.services.autonomy.roles import (
        DEFAULT_ESTIMATED_SIZE,
        ESTIMATED_SIZES,
    )

    explicit = getattr(line, "estimated_size", None)
    if isinstance(explicit, str):
        token = explicit.strip().lower()
        if token in ESTIMATED_SIZES:
            return token
    if getattr(line, "risk_bearing", False):
        return "large"
    return DEFAULT_ESTIMATED_SIZE


def assign_dag(
    lines: Iterable[Line],
    agents: Iterable[AgentState],
    *,
    policy: RolePolicy | None = None,
    headroom: Mapping[str, float] | None = None,
) -> AssignmentPlan:
    """Assign the lines of one plan to the developer pool, in dependency order.

    Each line becomes one developer task: the line id is the task id, and the
    planner's dependency edges and write scopes are carried over unchanged.
    Capabilities and priority stay empty, so the order the planner chose for
    its lines is the order the plan is built in.

    ``headroom`` is the remaining-fraction map from a ``HeadroomPort`` (the
    same port the selector uses). Without it, every developer looks fully
    open and an exhausted provider can still be planner-assigned. Risk-bearing
    lines are estimated ``large`` so they prefer the widest remaining quota.

    The plan is the role planner's own and is returned as it stands: no
    selector runs as a fallback, so a line the policy could not place comes
    back unassigned with a stable reason instead of being quietly reassigned
    by the quota-aware selector above.

    A line whose write scope does not validate is returned unassigned, with
    the checker's own first-failure reason and the line id
    (``<reason>:<line_id>``), and is never offered as a claim. The planner
    validator uses the same check. A plan made only of lines that fail it is
    still rejected there; a caller that builds its own lines hits this second
    check.

    A line that depends on one of those refused lines, directly or through
    other lines that do, is not passed to the role planner either. It is
    unassigned with ``missing_dependency:<filtered line id>``, where the id is
    the refused line reached by the first dependency edge in topological
    order. A line with no such dependency is planned exactly as it would be
    if the refused line and its dependents were absent. An unrelated line
    therefore does not inherit ``missing_dependency`` from a dangling parent.
    """
    # The same scope check the planner validator runs, on the same values, with
    # the same first-failure order: a caller that hands assign_dag a line it
    # built itself cannot get an empty, blank, unencodable or escaping scope
    # placed or claimed, and the reason matches the validator's for that list.
    # Dependents of a refused line are removed before the role planner sees the
    # graph. Otherwise one dangling parent makes ``_topological_order`` mark
    # every remaining task ``missing_dependency``, including a line that never
    # depended on the refused one.
    from src.tools.auto_dispatch.planning import check_write_scope, first_duplicate_task_id

    line_list = list(lines)
    # Same walk the validator uses, before any scope is claimed. A repeated id
    # rejects the whole plan; filtering the illegal twin must not hide it.
    duplicate = first_duplicate_task_id(line.id for line in line_list)
    if duplicate is not None:
        reason = f"duplicate_task_id:{duplicate}"
        return AssignmentPlan(
            assignments=(),
            unassigned=tuple(
                Assignment(line.id, status="unassigned", reason=reason)
                for line in line_list
            ),
        )
    scopes: dict[str, tuple[str, ...]] = {}
    filtered_ids: set[str] = set()
    refused: list[Assignment] = []
    for line in line_list:
        checked, reason = check_write_scope(line.write_scope)
        if reason is not None or checked is None:
            filtered_ids.add(line.id)
            refused.append(
                Assignment(line.id, status="unassigned", reason=f"{reason}:{line.id}")
            )
            continue
        scopes[line.id] = checked
    blocked = _blocked_by_filtered(line_list, filtered_ids)
    specs: list[TaskSpec] = []
    held: list[Assignment] = []
    for line in line_list:
        if line.id in filtered_ids:
            continue
        ancestor = blocked.get(line.id)
        if ancestor is not None:
            held.append(
                Assignment(
                    line.id,
                    status="unassigned",
                    reason=f"missing_dependency:{ancestor}",
                )
            )
            continue
        specs.append(
            TaskSpec(
                task_id=line.id,
                depends_on=tuple(line.depends_on),
                write_scope=scopes[line.id],
                estimated_size=_estimated_size_for_line(line),
            )
        )
    plan = plan_developer_assignments(
        specs, agents, policy=policy, headroom=headroom
    )
    return AssignmentPlan(
        assignments=plan.assignments,
        unassigned=plan.unassigned + tuple(held) + tuple(refused),
    )


async def assign_dag_with_headroom(
    lines: Iterable[Line],
    agents: Iterable[AgentState],
    headroom_port: HeadroomPort,
    *,
    policy: RolePolicy | None = None,
) -> AssignmentPlan:
    """Resolve a HeadroomPort once, then assign the DAG with those readings."""
    from src.server.services.autonomy.role_plan import read_headroom

    agent_rows = list(agents)
    readings = await read_headroom(
        (agent.provider for agent in agent_rows),
        headroom_port,
    )
    return assign_dag(lines, agent_rows, policy=policy, headroom=readings)
