"""Deterministic assignment of a task DAG to the dynamic developer pool.

This is the scheduling half of the role policy in :mod:`roles`: the policy says
who *may* take a role; this module decides who does, in dependency order, while
respecting capability, capacity and the reserved planner/reviewer roles.

A task whose dependency could not be assigned is not silently scheduled; it is
reported as unassigned with a stable reason so the Supervisor can show a
breakage instead of pretending the plan is complete.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Iterable, Mapping

from src.server.services.autonomy.roles import (
    DEFAULT_ESTIMATED_SIZE,
    AgentState,
    RolePolicy,
)

if TYPE_CHECKING:
    from src.tools.auto_dispatch.selection import HeadroomPort


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """One unit of developer work in a plan."""

    task_id: str
    capabilities: frozenset[str] = frozenset()
    depends_on: tuple[str, ...] = ()
    write_scope: tuple[str, ...] = ()
    # Higher priority ready tasks are considered first; dependencies always
    # win, so a high-priority child never jumps an unassigned prerequisite.
    priority: int = 0
    # small | medium | large. A large estimate prefers the provider with the
    # most HeadroomPort remaining among eligible developers.
    estimated_size: str = DEFAULT_ESTIMATED_SIZE


@dataclass(frozen=True, slots=True)
class Assignment:
    """One task's assignment (or the stable reason it could not be assigned)."""

    task_id: str
    provider: str = ""
    status: str = "assigned"
    reason: str = ""

    @property
    def assigned(self) -> bool:
        return self.status == "assigned" and bool(self.provider)


@dataclass(frozen=True, slots=True)
class AssignmentPlan:
    assignments: tuple[Assignment, ...]
    unassigned: tuple[Assignment, ...]

    def by_task(self) -> dict[str, Assignment]:
        return {item.task_id: item for item in self.assignments}

    def provider_of(self, task_id: str) -> str:
        item = self.by_task().get(task_id)
        return item.provider if item is not None and item.assigned else ""

    def to_dict(self) -> dict[str, object]:
        """Audit-friendly shape; no Ticket or Worker content is included."""
        return {
            "assignments": [
                {
                    "task_id": item.task_id,
                    "provider": item.provider,
                    "status": item.status,
                }
                for item in self.assignments
            ],
            "unassigned": [
                {
                    "task_id": item.task_id,
                    "reason": item.reason,
                }
                for item in self.unassigned
            ],
        }


def _topological_order(tasks: Iterable[TaskSpec]) -> tuple[list[TaskSpec], str | None]:
    """Stable dependency order, or a reason the graph cannot be planned."""
    ordered = list(tasks)
    by_id: dict[str, TaskSpec] = {}
    for task in ordered:
        if not task.task_id or task.task_id in by_id:
            return [], "duplicate_task_id"
        by_id[task.task_id] = task
    for task in ordered:
        for dependency in task.depends_on:
            if dependency not in by_id:
                return [], "missing_dependency"
    result: list[TaskSpec] = []
    remaining = {task.task_id: task for task in ordered}
    while remaining:
        ready = [
            task
            for task in ordered
            if task.task_id in remaining
            and all(dep not in remaining for dep in task.depends_on)
        ]
        ready.sort(key=lambda task: -task.priority)
        if not ready:
            return [], "dependency_cycle"
        for task in ready:
            result.append(task)
            remaining.pop(task.task_id, None)
    return result, None


def _assignment_refusal_reason(
    policy: RolePolicy, *, write_scope: tuple[str, ...]
) -> str:
    """Stable unassigned reason for a chooser that returned None."""
    refusal = getattr(policy, "assignment_refusal", None)
    if refusal == "unknown_worker":
        return "unknown_worker"
    if refusal == "quota_exhausted":
        return "quota_exhausted"
    if refusal == "text_only_worker" and write_scope:
        return "no_eligible_worker"
    return "no_capacity"


async def read_headroom(
    providers: Iterable[str],
    headroom: "HeadroomPort",
) -> dict[str, float]:
    """Read-only remaining fractions from the same HeadroomPort auto-dispatch uses.

    Providers are canonicalized before the port is asked, matching
    ``QuotaAwareModelSelector`` (``zcode`` reads as ``glm``). Each spelling
    that was requested is stored under both the alias and the canonical name
    so later lookups through either form agree.
    """
    from src.server.services.autonomy.roles import canonical_provider

    readings: dict[str, float] = {}
    for name in providers:
        key = str(name or "").strip()
        if not key:
            continue
        canon = canonical_provider(key)
        if canon not in readings:
            readings[canon] = float(await headroom.remaining(canon))
        readings[key] = readings[canon]
    return readings


def plan_developer_assignments(
    tasks: Iterable[TaskSpec],
    agents: Iterable[AgentState],
    *,
    policy: RolePolicy | None = None,
    exclude: Iterable[str] = (),
    exclude_by_task: Mapping[str, Iterable[str]] | None = None,
    headroom: Mapping[str, float] | None = None,
) -> AssignmentPlan:
    """Assign developer tasks in dependency order with simulated capacity.

    A task is assigned only when all of its dependencies were assigned and the
    chosen developer had a free place at that point in the plan. Capacity is
    simulated, not mutated: callers decide how to apply the plan to live
    Tickets.

    ``headroom`` is the remaining-fraction map from :func:`read_headroom` (or
    any other read of a ``HeadroomPort``). Exhausted providers are never
    chosen; the refusal is recorded as ``quota_exhausted``.
    """
    # Materialize before any traversal: a generator would be exhausted by the
    # first pass and a graph error would then silently drop every task.
    tasks = tuple(tasks)
    policy = policy or RolePolicy.default()
    excluded = frozenset(str(item) for item in exclude if str(item))
    per_task = {
        str(key): frozenset(str(item) for item in value if str(item))
        for key, value in (exclude_by_task or {}).items()
    }
    ordered, graph_error = _topological_order(tasks)
    if graph_error is not None:
        return AssignmentPlan(
            assignments=(),
            unassigned=tuple(
                Assignment(task.task_id, status="unassigned", reason=graph_error)
                for task in tasks
            ),
        )

    base_agents = list(agents)
    assigned: list[Assignment] = []
    unassigned: list[Assignment] = []
    used: dict[str, int] = {}
    assigned_ids: set[str] = set()
    used_scopes: dict[str, str] = {}

    for task in ordered:
        missing = [dep for dep in task.depends_on if dep not in assigned_ids]
        if missing:
            unassigned.append(
                Assignment(
                    task.task_id,
                    status="unassigned",
                    reason=f"dependency_unassigned:{missing[0]}",
                )
            )
            continue

        # Report the first overlapping pair deterministically: the task's own
        # scopes in order, then the claimed scopes in insertion order.
        conflict_owner = ""
        for scope in task.write_scope:
            for claimed_scope, owner in used_scopes.items():
                if scopes_overlap(scope, claimed_scope):
                    conflict_owner = owner
                    break
            if conflict_owner:
                break
        if conflict_owner:
            unassigned.append(
                Assignment(
                    task.task_id,
                    status="unassigned",
                    reason=f"write_scope_conflict:{conflict_owner}",
                )
            )
            continue

        adjusted = [
            replace(agent, active=agent.active + used.get(agent.provider, 0))
            for agent in base_agents
        ]
        chosen = policy.choose_developer(
            adjusted,
            capabilities=task.capabilities,
            exclude=excluded | per_task.get(task.task_id, frozenset()),
            line=task,
            headroom=headroom,
            estimated_size=task.estimated_size,
        )
        if chosen is None:
            # no_capacity is the dry-pool code. A write line whose only
            # remaining developers were text-only uses no_eligible_worker;
            # the filter itself is text_only_worker on the policy.
            # quota_exhausted is recorded when every remaining candidate was
            # out of HeadroomPort remaining.
            reason = _assignment_refusal_reason(policy, write_scope=task.write_scope)
            unassigned.append(
                Assignment(task.task_id, status="unassigned", reason=reason)
            )
            continue
        used[chosen.provider] = used.get(chosen.provider, 0) + 1
        assigned_ids.add(task.task_id)
        for scope in task.write_scope:
            # The normalised spelling is the claim, so a root scope is recorded
            # too: otherwise nothing would ever compare against it.
            claim = normalize_scope(scope)
            if claim:
                used_scopes[claim] = task.task_id
        assigned.append(Assignment(task.task_id, provider=chosen.provider))

    return AssignmentPlan(assignments=tuple(assigned), unassigned=tuple(unassigned))


async def plan_developer_assignments_with_headroom(
    tasks: Iterable[TaskSpec],
    agents: Iterable[AgentState],
    headroom_port: "HeadroomPort",
    *,
    policy: RolePolicy | None = None,
    exclude: Iterable[str] = (),
    exclude_by_task: Mapping[str, Iterable[str]] | None = None,
) -> AssignmentPlan:
    """Resolve a HeadroomPort once, then plan with those read-only readings."""
    agent_rows = list(agents)
    readings = await read_headroom(
        (agent.provider for agent in agent_rows),
        headroom_port,
    )
    return plan_developer_assignments(
        tasks,
        agent_rows,
        policy=policy,
        exclude=exclude,
        exclude_by_task=exclude_by_task,
        headroom=readings,
    )


async def _headroom_readings_for_agents(
    agents: Iterable[AgentState],
    headroom_port: "HeadroomPort",
) -> dict[str, float]:
    return await read_headroom((agent.provider for agent in agents), headroom_port)


#: The root scope: a claim with no directory part covers the whole repository.
_ROOT_SCOPE = "."

#: The glob metacharacters a scope segment may carry.
_WILDCARDS = "*?["


def normalize_scope(scope: str) -> str:
    """Canonical spelling of a validated scope.

    Collapses repeated separators, drops every ``.`` segment (so ``././``
    becomes the root scope) and a trailing separator, and reads an empty result
    or a bare ``**`` as the root scope. Callers validate the *raw* scope first
    (see :func:`planning.validate_write_scope`), so this never has to decide
    whether an escaping path was meant.
    """
    text = str(scope or "").strip().replace("\\", "/")
    if not text:
        return ""
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or parts == ["**"]:
        return _ROOT_SCOPE
    return "/".join(parts)


def _has_wildcard(segment: str) -> bool:
    return any(marker in segment for marker in _WILDCARDS)


def overlaps(a: str, b: str) -> bool:
    """Whether two write scopes can touch the same file.

    The scopes are walked segment by segment from the root:

    * a ``**`` segment matches anything below it, so the walk stops in an
      overlap;
    * two literal segments that differ cannot both match one path, so the walk
      stops in "no overlap";
    * a literal against a wildcard segment is decided by ``fnmatchcase``;
    * two wildcard segments may still intersect, so the walk continues;
    * when one side runs out of segments first it covers the other's subtree,
      which is an overlap;
    * both running out together is an overlap.

    The rule is conservative by construction: whenever the two sides cannot be
    proven disjoint, the answer is "overlap", because a false conflict costs one
    lane while a missed one puts two developers in the same file.
    """
    left = normalize_scope(a)
    right = normalize_scope(b)
    if not left or not right:
        return False
    if left == right or _ROOT_SCOPE in (left, right):
        return True
    return _walk(left.split("/"), right.split("/"))


def _walk(left: list[str], right: list[str]) -> bool:
    for index, (first, second) in enumerate(zip(left, right)):
        if "**" in (first, second):
            return True
        first_wild = _has_wildcard(first)
        second_wild = _has_wildcard(second)
        if first_wild and second_wild:
            continue
        if first_wild:
            if not fnmatch.fnmatchcase(second, first):
                return False
            continue
        if second_wild:
            if not fnmatch.fnmatchcase(first, second):
                return False
            continue
        if first != second:
            return False
    # The shorter scope covers the longer one's subtree, and two scopes of the
    # same length that agreed on every segment are the same claim.
    return True


#: The scope test the planner validator and the assignment stage share.
scopes_overlap = overlaps


@dataclass(frozen=True, slots=True)
class SupervisorPlan:
    """A planner choice plus the developer assignment plan for one mission."""

    planner: str
    assignments: AssignmentPlan
    planner_reason: str = ""

    @property
    def planned(self) -> bool:
        return bool(self.planner)

    def to_dict(self) -> dict[str, object]:
        return {
            "planner": self.planner,
            "planner_reason": self.planner_reason,
            **self.assignments.to_dict(),
        }


def plan_supervisor_mission(
    tasks: Iterable[TaskSpec],
    agents: Iterable[AgentState],
    *,
    policy: RolePolicy | None = None,
    headroom: Mapping[str, float] | None = None,
) -> SupervisorPlan:
    """Choose the reserved planner and plan developer work for one mission.

    The planner is selected before work; it is never part of the developer
    pool. A missing planner is returned explicitly instead of falling back to
    a developer model. ``headroom`` is the same remaining-fraction map a
    ``HeadroomPort`` answers; without it, every developer looks fully open.
    """
    policy = policy or RolePolicy.default()
    agent_rows = list(agents)
    planner = policy.choose_planner(agent_rows)
    if planner is None:
        return SupervisorPlan(
            planner="",
            assignments=plan_developer_assignments(
                tasks, agent_rows, policy=policy, headroom=headroom
            ),
            planner_reason="no_planner",
        )
    return SupervisorPlan(
        planner=planner.provider,
        assignments=plan_developer_assignments(
            tasks, agent_rows, policy=policy, headroom=headroom
        ),
    )


async def plan_supervisor_mission_with_headroom(
    tasks: Iterable[TaskSpec],
    agents: Iterable[AgentState],
    headroom_port: "HeadroomPort",
    *,
    policy: RolePolicy | None = None,
) -> SupervisorPlan:
    """Resolve a HeadroomPort, then plan the mission with those readings."""
    agent_rows = list(agents)
    readings = await _headroom_readings_for_agents(agent_rows, headroom_port)
    return plan_supervisor_mission(
        tasks, agent_rows, policy=policy, headroom=readings
    )


def replan_after_failure(
    previous: AssignmentPlan,
    tasks: Iterable[TaskSpec],
    agents: Iterable[AgentState],
    *,
    failed_task_id: str,
    failed_provider: str,
    policy: RolePolicy | None = None,
    headroom: Mapping[str, float] | None = None,
) -> AssignmentPlan:
    """Reassign one failed task while keeping the rest of the plan intact.

    Capacity used by the unaffected assignments is charged before the failed
    task is offered again; only that task excludes the provider that failed.
    If no agent has a free place, the task is returned as unassigned rather
    than silently dropping it or changing unrelated assignments.

    ``headroom`` must be refreshed or reused from the original plan so a
    retry cannot hand work to an exhausted provider that looked free when
    the map was omitted.
    """
    policy = policy or RolePolicy.default()
    task_by_id = {task.task_id: task for task in tasks if task.task_id}
    target = task_by_id.get(failed_task_id)
    if target is None:
        return previous
    by_task = previous.by_task()
    kept = [
        item
        for item in previous.assignments
        if item.task_id != failed_task_id
    ]
    if failed_task_id not in by_task:
        kept = list(previous.assignments)
    used: dict[str, int] = {}
    for item in kept:
        if item.assigned:
            used[item.provider] = used.get(item.provider, 0) + 1
    adjusted = [
        replace(agent, active=agent.active + used.get(agent.provider, 0))
        for agent in agents
    ]
    chosen = policy.choose_developer(
        adjusted,
        capabilities=target.capabilities,
        exclude={failed_provider},
        line=target,
        headroom=headroom,
        estimated_size=target.estimated_size,
    )
    unassigned = tuple(
        item for item in previous.unassigned if item.task_id != failed_task_id
    )
    if chosen is None:
        reason = _assignment_refusal_reason(policy, write_scope=target.write_scope)
        return AssignmentPlan(
            assignments=tuple(kept),
            unassigned=unassigned
            + (
                Assignment(
                    failed_task_id,
                    status="unassigned",
                    reason=reason,
                ),
            ),
        )
    replacement = Assignment(failed_task_id, provider=chosen.provider)
    # Keep the original task order: replace in place when it was assigned,
    # otherwise append after the untouched assignments.
    if failed_task_id in by_task and any(
        item.task_id == failed_task_id for item in previous.assignments
    ):
        assignments = [
            replacement if item.task_id == failed_task_id else item
            for item in previous.assignments
        ]
    else:
        assignments = [*kept, replacement]
    return AssignmentPlan(
        assignments=tuple(assignments),
        unassigned=unassigned,
    )


async def replan_after_failure_with_headroom(
    previous: AssignmentPlan,
    tasks: Iterable[TaskSpec],
    agents: Iterable[AgentState],
    headroom_port: "HeadroomPort",
    *,
    failed_task_id: str,
    failed_provider: str,
    policy: RolePolicy | None = None,
) -> AssignmentPlan:
    """Refresh HeadroomPort readings, then replan one failed task."""
    agent_rows = list(agents)
    readings = await _headroom_readings_for_agents(agent_rows, headroom_port)
    return replan_after_failure(
        previous,
        tasks,
        agent_rows,
        failed_task_id=failed_task_id,
        failed_provider=failed_provider,
        policy=policy,
        headroom=readings,
    )
