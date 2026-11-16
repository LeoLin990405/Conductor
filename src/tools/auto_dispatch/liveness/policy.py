"""The frozen N76 recovery table and its local tightening rules.

PRD n76 §4.2 describes recovery as data: every parking reason belongs to one
group, and each group is an ordered list of bounded steps. The sweep never
invents an action for a reason; it asks this module for the next row. Keeping
the table here (instead of a branch per call site) is what lets a new parking
reason be added with a test rather than another operator instruction.

A step is a :class:`Step` tuple of ``(action, budget)``. ``budget`` is the
step's own nonnegative allowance, interpreted per action: one attempt for an
immediate ``reassign``/``split``, the backoff in seconds before a ``review``
retry, or the 24-hour ceiling for a ``wait``. The per-line cap (``max_steps``)
is a separate count: it bounds how many rows a line may consume before it
becomes ``needs_human``.

``~/.conductor/liveness_policy.yaml`` may only *tighten* the frozen table:
drop steps, lower a step budget, lower ``max_steps``, or turn a group/reason
into ``HUMAN``. Anything that could add an automatic action is refused, so a
local file can never widen what the PRD approved. A refused file leaves the
immutable defaults untouched and raises :class:`PolicyError`; callers that
want to keep dispatching can fall back to :func:`default_policy`.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, NamedTuple

import yaml

#: Environment variable for the per-line automatic-step cap.
MAX_STEPS_ENV = "CONDUCTOR_DISPATCH_LIVENESS_MAX_STEPS"
DEFAULT_MAX_STEPS = 4

#: The override file, relative to the operator's home directory.
POLICY_PATH = Path(".conductor") / "liveness_policy.yaml"

#: Group names exactly as PRD §4.2 writes them.
R_EXIT = "R-EXIT"
R_REVIEW = "R-REVIEW"
R_QUOTA = "R-QUOTA"
R_REPLAN = "R-REPLAN"
R_SPLIT = "R-SPLIT"
HUMAN = "HUMAN"

#: Backwards-compatible spellings used by early N76 callers.
R_HUMAN = HUMAN
HUMAN_GROUP = HUMAN

#: The verbs the recovery executor knows. A policy step names one of these.
REASSIGN = "reassign"
REVIEW = "review"
WAIT = "wait"
SPLIT = "split"

#: The 24-hour ceiling on a R-QUOTA wait (also expressed by the wait Step's
#: budget; exported because an executor may need the value before it selects
#: a step).
QUOTA_WAIT_SECONDS = 24 * 60 * 60


class PolicyError(ValueError):
    """The local policy file is malformed or would loosen the frozen table."""


class Step(NamedTuple):
    """One recovery action and its own allowance.

    ``budget`` is nonnegative. For ``review`` it is the backoff in seconds
    before that retry; for ``wait`` it is the longest wait in seconds; an
    immediate action carries its single-attempt allowance of ``1``. A local
    override may only lower it.
    """

    action: str
    budget: int | float = 0


#: The frozen PRD §4.2 table. Tuples, not lists: a caller cannot append an
#: automatic action by mutating the table it was given.
DEFAULT_GROUPS: Mapping[str, tuple[Step, ...]] = MappingProxyType(
    {
        # A worker exited without delivering, a watchdog fired, a fix never
        # started, or G2 exhausted its focused-test fixes. Replace the author
        # twice, then split the line out so the rest of the round can finish.
        R_EXIT: (
            Step(REASSIGN, 1),
            Step(REASSIGN, 1),
            Step(SPLIT, 1),
        ),
        # The review could not run, could not be parsed, or no reviewer was
        # eligible. Retry the same change after 60s, try a different reviewer
        # after 300s, then make one last attempt after 15 minutes.
        R_REVIEW: (
            Step(REVIEW, 60),
            Step(REVIEW, 300),
            Step(REVIEW, 900),
        ),
        # Reviewer or worker quota is exhausted. Wait for CodexBar's
        # ``resets_at``/paid headroom (at most a day), then resume the review.
        R_QUOTA: (
            Step(WAIT, QUOTA_WAIT_SECONDS),
            Step(REVIEW, 1),
        ),
        # The one replan decision is spent or unusable. Replace the developer
        # once; if that still does not work, split the line off.
        R_REPLAN: (
            Step(REASSIGN, 1),
            Step(SPLIT, 1),
        ),
        # Partial integration: remove this line and its DAG dependants from
        # this round. The remaining passed lines still go to G5/approval.
        R_SPLIT: (Step(SPLIT, 1),),
        # A person owns these reasons. No automatic step may be added here.
        HUMAN: (),
    }
)

#: The map is deliberately explicit: PRD §3 names each reason, and a reason
#: missing here would otherwise fall back to HUMAN silently. The last row of
#: §3 sends the ex-ERROR reasons to R-SPLIT; §4.4's special cases are noted
#: where they differ (G5 configuration stays human).
DEFAULT_REASON_GROUPS: Mapping[str, str] = MappingProxyType(
    {
        # R-EXIT: worker exit without delivery, watchdog, G2 fix exhaustion,
        # or a fix launch that never started.
        "no_new_commit": R_EXIT,
        "uncommitted_changes": R_EXIT,
        "watchdog_timeout": R_EXIT,
        "g2_no_commands_risk_bearing": R_EXIT,
        "g2_revision_moved": R_EXIT,
        "focused_tests_exhausted": R_EXIT,
        "fix_launch_failed": R_EXIT,
        # N80 L4 / N76 §7: an app lease lapsed or was released -> owner none ->
        # a new author chosen by the selector (N73).
        "app_lease_expired": R_EXIT,
        "app_lease_released": R_EXIT,
        # P0: the Worker produced no model output (no-output timeout or an
        # empty final answer); enforce reassigns it like a timed-out run.
        "worker_no_output": R_EXIT,
        # R-REVIEW: the review could not run, parse, or find a reviewer.
        "review_unavailable": R_REVIEW,
        "review_unparseable": R_REVIEW,
        "no_eligible_reviewer": R_REVIEW,
        # R-QUOTA: capacity returns on a timer.
        "reviewers_quota_exhausted": R_QUOTA,
        # R-REPLAN: the planner path ran out.
        "replan_parked": R_REPLAN,
        "replan_uncombined": R_REPLAN,
        "replan_base_unresolved": R_REPLAN,
        "replan_exhausted": R_REPLAN,
        # HUMAN: a person must diagnose or adjudicate.
        "handoff_incomplete": HUMAN,
        "reviewer_conflict_scope": HUMAN,
        "reviewer_conflict_security": HUMAN,
        "liveness_inconsistent": HUMAN,
        "g5_not_configured": HUMAN,
        "g5_full_suite_failed": HUMAN,
        "g5_timeout": HUMAN,
        "g5_worktree_dirty": HUMAN,
        "planner_failed": HUMAN,
        "resume_spec_missing": HUMAN,
        "review_blocked": HUMAN,
        # N86: a human reassign pinned a worker that cannot run now. No
        # automatic step may move the line to anyone else.
        "pinned_worker_unusable": HUMAN,
        "preexisting_failure": HUMAN,
        "review_inconclusive": HUMAN,
        # The PRD §3 last row: these used to enter ERROR. §4.4 keeps the
        # reviewer/worker-unavailable cases as capacity waits (the pool may
        # come back), so they use the same timer as exhausted quota instead of
        # immediately splitting a line that a retry could still finish.
        "no_secondary_reviewer": R_QUOTA,
        "no_eligible_worker": R_QUOTA,
        # Missing git evidence is not a transient capacity problem; there is
        # nothing to wait for, so the line is split out of this round.
        "git_evidence_unavailable": R_SPLIT,
        # The existing worker retry engine owns this class (PRD §3, "keep").
        # Mapping it to HUMAN means the liveness sweep adds no competing
        # automatic step; the worker recovery path keeps its Retry-After.
        "author_quota_exhausted": HUMAN,
        "worker_auth_failed": HUMAN,
        "worker_quota_exhausted": HUMAN,
        "worker_context_exhausted": HUMAN,
        "worker_confirmation_required": HUMAN,
        "worker_turn_budget_exhausted": HUMAN,
        "worker_rate_limited": HUMAN,
        "network_retries_exhausted": HUMAN,
        "rate_limited_retries_exhausted": HUMAN,
        "overloaded_retries_exhausted": HUMAN,
        "timeout_retries_exhausted": HUMAN,
        "context_exhausted_retries_exhausted": HUMAN,
        # The Worker was continued in its own session and still ran out of
        # turns: the line is too big for one run, so it is split out of this
        # round rather than waiting for a person (2026-11-13, grok exit 77).
        "turn_budget_exhausted_retries_exhausted": R_SPLIT,
    }
)

#: Public aliases: the table is data, so callers may read it under any of the
#: names used in the N76 design/repair tickets.
RECOVERY_TABLE = DEFAULT_GROUPS
RECOVERY_GROUPS = DEFAULT_GROUPS
GROUPS = DEFAULT_GROUPS
STEP_TABLE = DEFAULT_GROUPS
REASON_GROUPS = DEFAULT_REASON_GROUPS
REASON_TO_GROUP = DEFAULT_REASON_GROUPS
FAILURE_REASON_GROUPS = DEFAULT_REASON_GROUPS
FAILURE_REASON_TO_GROUP = DEFAULT_REASON_GROUPS
MAX_STEPS = DEFAULT_MAX_STEPS


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number(value: object, *, field: str) -> int | float:
    """A finite, nonnegative budget; integral values stay ints for YAML/JSON."""
    if not _is_number(value):
        raise PolicyError(f"{field} must be a nonnegative number")
    number = float(value)  # type: ignore[arg-type]
    if not math.isfinite(number) or number < 0:
        raise PolicyError(f"{field} must be a finite, nonnegative number")
    return int(number) if number.is_integer() else number


def _cap(value: object, *, base: int | None = None, field: str = "max_steps") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"{field} must be an integer")
    if value < 0:
        raise PolicyError(f"{field} must be nonnegative")
    if base is not None and value > base:
        raise PolicyError(f"{field}={value} would loosen the default of {base}")
    return value


def _resolve_max_steps(environ: Mapping[str, str] | None) -> int:
    """The cap from the environment, defaulting on absent/invalid/nonpositive."""
    env = os.environ if environ is None else environ
    raw = env.get(MAX_STEPS_ENV)
    if raw is None or isinstance(raw, bool):
        return DEFAULT_MAX_STEPS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_MAX_STEPS
    return value if value > 0 else DEFAULT_MAX_STEPS


def max_steps(
    environ: Mapping[str, str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    """The PRD per-line cap; non-integer, empty, or nonpositive means 4.

    ``environ`` is injectable so tests do not have to mutate the process
    environment. ``env`` is accepted as an alias for callers that name the
    mapping that way.
    """
    source = environ if environ is not None else env
    return _resolve_max_steps(source)


@dataclass(frozen=True)
class Policy:
    """A recovery table plus its reason map and per-line cap."""

    groups: Mapping[str, tuple[Step, ...]]
    reason_groups: Mapping[str, str]
    max_steps: int

    @property
    def table(self) -> Mapping[str, tuple[Step, ...]]:
        return self.groups

    @property
    def reasons(self) -> Mapping[str, str]:
        return self.reason_groups

    @property
    def recovery_table(self) -> Mapping[str, tuple[Step, ...]]:
        return self.groups

    @property
    def failure_reasons(self) -> Mapping[str, str]:
        return self.reason_groups

    def group_for(self, reason: str | None) -> str:
        """The group for ``reason``; an unknown reason is human, never invented."""
        return self.reason_groups.get(str(reason or ""), HUMAN)

    def steps_for(
        self, reason: str | None, *, capped: bool = False
    ) -> tuple[Step, ...]:
        """The group's steps, optionally truncated to the per-line cap."""
        steps = self.groups.get(self.group_for(reason), ())
        return steps[: self.max_steps] if capped else steps

    def plan_for(self, reason: str | None) -> tuple[Step, ...]:
        """The steps a line may still consume for ``reason``."""
        return self.steps_for(reason, capped=True)

    def __getitem__(self, group: str) -> Any:
        # Group lookup is the common case; the metadata keys make the policy
        # convenient to introspect without importing the dataclass fields.
        if group == "max_steps":
            return self.max_steps
        if group == "groups":
            return self.groups
        if group in {"reasons", "reason_groups"}:
            return self.reason_groups
        return self.groups[group]

    def get(
        self, group: str, default: tuple[Step, ...] | None = None
    ) -> tuple[Step, ...] | None:
        return self.groups.get(group, default)

    def __iter__(self) -> Iterator[str]:
        return iter(self.groups)

    def __len__(self) -> int:
        return len(self.groups)

    def __contains__(self, group: object) -> bool:
        return group in self.groups

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "groups": {
                name: [{"action": step.action, "budget": step.budget} for step in steps]
                for name, steps in self.groups.items()
            },
            "reasons": dict(self.reason_groups),
        }


#: Alias used by the N76 design text and by early consumers.
LivenessPolicy = Policy


def default_policy(max_steps: int | None = None) -> Policy:
    """The frozen table; ``max_steps`` defaults to the environment cap."""
    limit = _resolve_max_steps(None) if max_steps is None else _cap(max_steps)
    return Policy(
        groups=DEFAULT_GROUPS,
        reason_groups=DEFAULT_REASON_GROUPS,
        max_steps=limit,
    )


#: The environment-independent frozen policy, convenient for type checks.
DEFAULT_POLICY = Policy(
    groups=DEFAULT_GROUPS,
    reason_groups=DEFAULT_REASON_GROUPS,
    max_steps=DEFAULT_MAX_STEPS,
)


def _action(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"{field} must be a non-empty action name")
    return value.strip()


def _step_item(item: object, *, field: str) -> tuple[str, int | float | None]:
    """Normalise one YAML step item to ``(action, budget-or-None)``."""
    if isinstance(item, str):
        return _action(item, field=field), None
    if isinstance(item, Mapping):
        if "action" in item:
            unknown = set(item) - {"action", "budget"}
            if unknown:
                raise PolicyError(
                    f"{field}: unknown step key(s) {', '.join(sorted(map(str, unknown)))}"
                )
            action = _action(item["action"], field=field)
            budget = item.get("budget")
        elif len(item) == 1:
            action_value, budget = next(iter(item.items()))
            action = _action(action_value, field=field)
        else:
            raise PolicyError(f"{field}: a step needs an action and optional budget")
        return action, None if budget is None else _number(budget, field=field)
    if isinstance(item, (list, tuple)) and len(item) == 2:
        action = _action(item[0], field=field)
        return action, _number(item[1], field=field)
    raise PolicyError(f"{field}: a step must be an action or an [action, budget] pair")


def _steps_override(name: str, base: tuple[Step, ...], raw: object) -> tuple[Step, ...]:
    """Parse a tightening-only step list for one group.

    A requested action must match a remaining default step in order; this
    permits dropping any steps but rejects reordering, renaming, and adding.
    The requested budget must not exceed the matched default step's budget.
    When the same action repeats, a requested budget may skip earlier
    occurrences whose default budget is too small, which is how a valid
    suffix such as ``review@300, review@900`` drops the leading
    ``review@60`` step.
    """
    if raw is None:
        return ()
    if isinstance(raw, bool) or not isinstance(raw, (int, str, Mapping, list, tuple)):
        raise PolicyError(f"{name}: steps must be a list, mapping, or count")
    if isinstance(raw, int):
        if raw < 0 or raw > len(base):
            raise PolicyError(f"{name}: step count must be within the default table")
        return base[:raw]
    if isinstance(raw, Mapping):
        if "action" in raw:
            raw = [raw]
        else:
            raw = [{"action": key, "budget": value} for key, value in raw.items()]
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise PolicyError(f"{name}: steps must be a list of actions")

    steps: list[Step] = []
    next_index = 0
    for position, item in enumerate(raw):
        action, budget = _step_item(item, field=f"{name} step {position + 1}")
        candidates = [
            index
            for index in range(next_index, len(base))
            if base[index].action == action
        ]
        if not candidates:
            raise PolicyError(
                f"{name}: cannot add or reorder step {action!r}; "
                f"the default order is {[step.action for step in base]!r}"
            )
        match = next(
            (
                index
                for index in candidates
                if budget is None or budget <= base[index].budget
            ),
            None,
        )
        if match is None:
            largest = max(base[index].budget for index in candidates)
            raise PolicyError(
                f"{name}: step {action!r} budget {budget} would raise the "
                f"largest remaining default of {largest}"
            )
        limit = base[match].budget
        if budget is None:
            budget = limit
        steps.append(Step(action, budget))
        next_index = match + 1
    return tuple(steps)


def _apply_budgets(
    name: str, steps: tuple[Step, ...], budgets: object
) -> tuple[Step, ...]:
    """Lower step budgets in place, one entry per step and never above it."""
    if not isinstance(budgets, (list, tuple)):
        raise PolicyError(f"{name}: budgets must be a list")
    if len(budgets) != len(steps):
        raise PolicyError(f"{name}: budgets must match the number of steps")
    tightened: list[Step] = []
    for index, (step, budget) in enumerate(zip(steps, budgets, strict=True)):
        limit = step.budget
        value = _number(budget, field=f"{name} budget {index + 1}")
        if value > limit:
            raise PolicyError(
                f"{name}: step {step.action!r} budget {value} would "
                f"raise the default of {limit}"
            )
        tightened.append(Step(step.action, value))
    return tuple(tightened)


def _group_override(name: str, base: tuple[Step, ...], raw: object) -> tuple[Step, ...]:
    """Apply one group override, allowing only reductions and humanising."""
    if raw is None:
        return ()
    if isinstance(raw, bool):
        return () if raw else base
    if isinstance(raw, str):
        if raw.strip().casefold() == HUMAN.casefold():
            return ()
        steps = _steps_override(name, base, raw)
    elif isinstance(raw, (int, list, tuple)):
        steps = _steps_override(name, base, raw)
    elif isinstance(raw, Mapping):
        if "action" in raw:
            # ``R-EXIT: {action: reassign, budget: 0}`` is a one-step group.
            steps = _steps_override(name, base, [raw])
        else:
            unknown = set(raw) - {"steps", "human", "budgets"}
            if unknown:
                raise PolicyError(
                    f"{name}: unknown override key(s) "
                    f"{', '.join(sorted(map(str, unknown)))}"
                )
            human = raw.get("human", False)
            if not isinstance(human, bool):
                raise PolicyError(f"{name}: human must be a boolean")
            if "steps" in raw:
                steps = _steps_override(name, base, raw["steps"])
            elif "budgets" in raw:
                # ``budgets`` alone tightens every default step, so it must
                # still be validated instead of silently falling back to base.
                steps = base
            else:
                return () if human else base
            if "budgets" in raw:
                steps = _apply_budgets(name, steps, raw["budgets"])
            if human and steps:
                raise PolicyError(f"{name}: a human group cannot have automatic steps")
    else:
        raise PolicyError(f"{name}: override must be a mapping, list, or count")
    if name == HUMAN and steps:
        raise PolicyError("HUMAN: automatic steps are forbidden")
    return steps


def _reason_override(
    reason: str,
    target: object,
    base: Policy,
    groups: Mapping[str, tuple[Step, ...]],
) -> str:
    """Apply one reason-to-group override; only humanising is a tightening."""
    if isinstance(target, Mapping):
        target = target.get("group", target.get("to"))
    if not isinstance(target, str) or not target.strip():
        raise PolicyError(f"reason {reason!r}: group must be a non-empty name")
    group = target.strip()
    if reason not in base.reason_groups:
        raise PolicyError(f"unknown failure reason {reason!r}")
    if group not in groups:
        raise PolicyError(f"reason {reason!r}: unknown group {group!r}")
    current = base.reason_groups[reason]
    if group == current:
        return group
    if group == HUMAN or not groups[group]:
        return group
    if current == HUMAN:
        raise PolicyError(
            f"reason {reason!r} is HUMAN and cannot be moved to automatic group {group!r}"
        )
    raise PolicyError(
        f"reason {reason!r} may only be moved to HUMAN, not from {current!r} to {group!r}"
    )


def _read_yaml(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"cannot read {path}: {exc}") from exc
    if not text.strip():
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"cannot parse {path}: {exc}") from exc


def tighten(base: Policy | Mapping[str, tuple[Step, ...]], raw: object) -> Policy:
    """Apply an already-read override mapping, allowing only reductions.

    Group overrides are applied before reason overrides, so a file may first
    empty a group and then move a reason into that now-human group.
    """
    if not isinstance(base, Policy):
        base = Policy(
            groups=base,
            reason_groups=DEFAULT_REASON_GROUPS,
            max_steps=_resolve_max_steps(None),
        )
    if raw is None:
        return base
    if not isinstance(raw, Mapping):
        raise PolicyError("the policy file must be a mapping")

    reason_keys = {
        "reasons",
        "failure_reasons",
        "reason_groups",
        "failure_reason_groups",
    }
    groups: dict[str, tuple[Step, ...]] = dict(base.groups)
    reason_updates: dict[str, object] = {}
    limit = base.max_steps

    for key, value in raw.items():
        name = str(key)
        if name == "max_steps":
            # An empty YAML value is the same as not naming the key.
            if value is not None:
                limit = _cap(value, base=base.max_steps)
        elif name == "groups":
            if not isinstance(value, Mapping):
                raise PolicyError("groups must be a mapping")
            for group_name, override in value.items():
                group = str(group_name)
                if group not in base.groups:
                    raise PolicyError(f"unknown recovery group {group!r}")
                groups[group] = _group_override(group, groups[group], override)
        elif name in reason_keys:
            if not isinstance(value, Mapping):
                raise PolicyError(f"{name} must be a mapping")
            reason_updates.update(
                {str(reason): target for reason, target in value.items()}
            )
        elif name in base.groups:
            groups[name] = _group_override(name, groups[name], value)
        else:
            raise PolicyError(f"unknown policy key {name!r}")

    reasons = dict(base.reason_groups)
    for reason, target in reason_updates.items():
        reasons[reason] = _reason_override(reason, target, base, groups)

    return Policy(
        groups=MappingProxyType(groups),
        reason_groups=MappingProxyType(reasons),
        max_steps=limit,
    )


def load_policy(
    path: str | Path | None = None,
    *,
    home: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Policy:
    """Load defaults plus the optional local override file.

    With no ``path`` the operator file ``~/.conductor/liveness_policy.yaml``
    is read if present. An explicit ``path`` is useful in tests. A missing
    file always yields the defaults, never an error and never a write. A file
    that would loosen the table raises :class:`PolicyError`; because the
    defaults are immutable, the caller can safely fall back to them.
    """
    base = default_policy(max_steps=max_steps(environ, env=env))
    if path is not None:
        candidate = Path(path).expanduser()
        if candidate.is_dir():
            # A caller may hand us a directory: accept the conventional file
            # beside it or under its ``.conductor`` child.
            direct = candidate / "liveness_policy.yaml"
            nested = candidate / POLICY_PATH
            candidate = direct if direct.exists() else nested
    else:
        root = Path(home).expanduser() if home is not None else Path.home()
        # Only the documented ``~/.conductor/liveness_policy.yaml`` is read.
        # A stray ``~/liveness_policy.yaml`` must not shadow it: an unrelated
        # file could otherwise suppress an operator's HUMAN override.
        candidate = root / POLICY_PATH
    if not candidate.exists():
        return base
    if candidate.is_dir():
        raise PolicyError(f"{candidate} is a directory, not a policy file")
    raw = _read_yaml(candidate)
    return tighten(base, raw)


__all__ = [
    "DEFAULT_GROUPS",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_POLICY",
    "DEFAULT_REASON_GROUPS",
    "FAILURE_REASON_GROUPS",
    "FAILURE_REASON_TO_GROUP",
    "GROUPS",
    "HUMAN",
    "HUMAN_GROUP",
    "LivenessPolicy",
    "MAX_STEPS",
    "MAX_STEPS_ENV",
    "POLICY_PATH",
    "Policy",
    "PolicyError",
    "QUOTA_WAIT_SECONDS",
    "R_EXIT",
    "R_HUMAN",
    "R_QUOTA",
    "R_REPLAN",
    "R_REVIEW",
    "R_SPLIT",
    "REASON_GROUPS",
    "REASON_TO_GROUP",
    "RECOVERY_GROUPS",
    "RECOVERY_TABLE",
    "REASSIGN",
    "REVIEW",
    "SPLIT",
    "STEP_TABLE",
    "Step",
    "WAIT",
    "default_policy",
    "load_policy",
    "max_steps",
    "tighten",
]
