"""Goal decomposition: the one stage that needs a model before anything runs.

The planner asks a model for a SPEC and a task DAG, then holds the reply to a
strict schema before anything downstream sees it. A malformed plan is a plan
failure, not a dispatch failure: the caller is told to refine the goal rather
than being handed a guess, and a line the role policy cannot place is reported
instead of being quietly reassigned by the quota-aware selector.

Nothing here reads the clock. The only thing written to disk is the SPEC
archive, which holds the validated plan and never the model's raw reply, the
prompt, any reasoning text or token usage.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import tempfile
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol

from .planner_prompt import (
    PLANNER_PROMPT_VERSION,
    atomic_plans_enabled,
    build_planner_prompt,
    planner_system_prompt,
)
from . import app_lines
from .soft_gates import quota_advisory, record_only
from .check_modes import (
    ENFORCE,
    OFF,
    REQUIRE_TESTS_ENV,
    SHADOW,
    check_mode,
    normalize_mode,
)
from .gates import acceptance_command_body
from .planner_reason import (
    ACCEPTANCE_TEST_DIRECTORY,
    EXCEEDS_DEVELOPER_POOL,
    GOAL_TOO_LARGE,
    INVALID_JSON,
    NO_JSON,
    UNASSIGNED,
    PlannerFailure,
)
from .protocols import DispatchError
from .selection import (
    WORKER_NOT_ALLOWED,
    _blocked_by_filtered,
    assign_dag,
    dag_agents_from_policy,
    normalise_workers,
)
from .types import Line
from src.server.services.autonomy.role_plan import Assignment, AssignmentPlan, scopes_overlap
from src.server.services.autonomy.roles import (
    AgentState,
    ESTIMATED_SIZES,
    RolePolicy,
    can_take_line,
    canonical_provider,
    reserved_work_audit,
)

logger = logging.getLogger(__name__)

# An injected completion callable: (prompt, system) -> raw text. The server
# wires this to LLMService.complete; tests wire it to a fixture.
Complete = Callable[[str, str], Awaitable[str]]

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# What a planner line may be, in UTF-8 bytes. A task becomes one argv string of
# the launch script (Linux refuses a process whose single argument is 131,072
# bytes or more, and a model wrote it) and the reviewer's prompt cuts a task at
# 8,000 bytes and a name at 256, so a line the planner accepts is a line every
# later stage takes whole. A plan with a longer one is a plan failure, like a
# malformed one: cutting a task would change what the work lane is asked.
MAX_LINE_NAME_BYTES = 256
MAX_LINE_TASK_BYTES = 8_000
# The goal shares the task's ceiling: it becomes the planner's user turn, and a
# goal past it is refused before the model is called at all.
MAX_GOAL_BYTES = 8_000
# One write-scope or acceptance entry is a path or a command, not a paragraph.
MAX_SCOPE_ENTRY_BYTES = 512
MAX_WRITE_SCOPE_ENTRIES = 32
MAX_ACCEPTANCE_ENTRIES = 16
MAX_SPEC_TITLE_BYTES = 256
MAX_SPEC_BODY_BYTES = 16_000

#: The planner output schema this module validates.
PLANNER_SCHEMA_VERSION = 1

_TOP_FIELDS = frozenset({"schema_version", "spec", "lines"})
_LINE_FIELDS = frozenset(
    {
        "name",
        "task",
        "depends_on",
        "write_scope",
        "declared_paths",
        "acceptance",
        "risk_bearing",
        "estimated_size",
    }
)
# N76: the planner may mark lines that must be integrated together or not at
# all; R-SPLIT refuses to remove one member of the group. The field exists
# only under LIVENESS_MODE=enforce: off and shadow reject a plan naming it
# exactly as main does (``unknown_field``).
_ATOMIC_LINE_FIELDS = _LINE_FIELDS | {"atomic_with"}
# N80 L4: the planner may mark a line an interactive app may claim. The field
# exists only under CONDUCTOR_COLLAB_MODE=full; off, shadow and docs reject a
# plan naming it exactly as main does (``unknown_field``).
_APP_LINE_FIELD = "app_claimable"

# A scope the planner may declare: repository-relative, no escape hatch. The
# check runs on the raw spelling, before any normalisation, so a ``..`` segment
# cannot hide behind a ``./`` that a normaliser would fold away.
# A leading ``~`` is a home-directory reference, not a repository path. The
# replan spec lists only ``/`` and a drive letter, so this is the one check
# kept from the earlier rounds rather than invented here.
_ABSOLUTE_SCOPE = re.compile(r"^(?:/|~|[A-Za-z]:)")
_DETAIL_LIMIT = 64


@dataclass(frozen=True, slots=True)
class Spec:
    """The mission the lines exist to serve: a title and a markdown body."""

    title: str
    body: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "body": self.body}


@dataclass(frozen=True, slots=True)
class PlanRejection:
    """Why a planner payload is not a plan. ``code`` is the stable reason."""

    reason: str
    detail: str = ""

    @property
    def code(self) -> str:
        return f"{self.reason}:{self.detail}" if self.detail else self.reason


@dataclass(frozen=True, slots=True)
class PlanResult:
    """A validated plan: the SPEC, the lines, and who the policy gave them to."""

    schema_version: int
    spec: Spec
    lines: tuple[Line, ...]
    assignment: AssignmentPlan
    risk_bearing: frozenset[str]
    prompt_version: str = PLANNER_PROMPT_VERSION
    spec_ref: str = ""

    def unassigned_reasons(self) -> tuple[str, ...]:
        """``line_id:reason`` for every line the policy could not place."""
        return tuple(
            f"{item.task_id}:{item.reason}" for item in self.assignment.unassigned
        )


@dataclass(frozen=True, slots=True)
class SpecRecord:
    """What an archive is handed: the validated plan, and nothing else."""

    spec: Spec
    lines: tuple[Line, ...]
    assignment: AssignmentPlan
    risk_bearing: frozenset[str]
    schema_version: int
    prompt_version: str = PLANNER_PROMPT_VERSION

    def plan_payload(self) -> dict[str, Any]:
        """The archived ``plan.json`` body: a fixed field whitelist."""
        return {
            "schema_version": self.schema_version,
            "prompt_version": self.prompt_version,
            "spec": self.spec.to_dict(),
            "lines": [
                {
                    "id": line.id,
                    "name": line.name,
                    "task": line.task,
                    "depends_on": list(line.depends_on),
                    "write_scope": list(line.write_scope),
                    "declared_paths": list(line.declared_paths),
                    "acceptance": list(line.acceptance),
                    "risk_bearing": line.risk_bearing,
                    "estimated_size": line.estimated_size,
                    "assigned_model": line.assigned_model,
                    # Why a reserved model was allowed onto a work line, when
                    # the emergency override put it there. Empty otherwise.
                    "assignment_audit": line.assignment_audit,
                    # N80 L4: only a marked line carries the key, so a plan
                    # without one archives byte for byte as before.
                    **({_APP_LINE_FIELD: True} if line.app_claimable else {}),
                }
                for line in self.lines
            ],
            "assignment": self.assignment.to_dict(),
            "risk_bearing": sorted(self.risk_bearing),
        }


class SpecArchive(Protocol):
    """Where a validated plan is kept. The reference comes back as a string."""

    async def save(self, record: SpecRecord) -> str:
        ...


class FileSpecArchive:
    """One directory per plan under ``root``, named by the plan's own hash.

    The id is the first 16 hex characters of the sha256 of the canonical
    ``plan.json`` body, so archiving the same plan twice is the same directory
    and the same reference. What lands there is the SPEC and the validated plan
    only: no model reply, no prompt, no goal, no reasoning text, no token count.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def spec_id(self, record: SpecRecord) -> str:
        payload = record.plan_payload()
        return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:16]

    async def save(self, record: SpecRecord) -> str:
        spec_id = self.spec_id(record)
        target = self._root / spec_id
        try:
            # Same content, same id: an existing archive is left as it stands.
            if not (target / "SPEC.md").exists():
                _atomic_write(target / "SPEC.md", _spec_markdown(record.spec))
            if not (target / "plan.json").exists():
                _atomic_write(
                    target / "plan.json",
                    json.dumps(record.plan_payload(), indent=2, sort_keys=True) + "\n",
                )
        except OSError as exc:
            raise DispatchError(
                f"spec_archive_failed: {exc.__class__.__name__}"
            ) from exc
        return f"spec:{spec_id}"

    async def load(self, spec_ref: str) -> list[Line] | None:
        """The archived lines behind ``spec:<id>``, or None when there are none.

        Only a well-formed reference is read: the id is 16 hex characters, so
        it cannot name a path outside ``root``. A missing or unreadable
        ``plan.json`` is None, never an exception, so a restart that finds no
        archive falls back to planning again rather than failing.
        """
        spec_id = spec_ref_id(spec_ref)
        if spec_id is None:
            return None
        try:
            payload = json.loads((self._root / spec_id / "plan.json").read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return archived_lines(payload)


_SPEC_REF = re.compile(r"spec:([0-9a-f]{16})")


def spec_ref_id(spec_ref: object) -> str | None:
    """The archive id of a ``spec:<16 hex>`` reference, or None."""
    match = _SPEC_REF.fullmatch(str(spec_ref or "").strip())
    return match.group(1) if match else None


def archived_lines(payload: object) -> list[Line] | None:
    """Rebuild the planned lines from an archived ``plan.json`` body."""
    if not isinstance(payload, Mapping):
        return None
    entries = payload.get("lines")
    if not isinstance(entries, list) or not entries:
        return None
    lines: list[Line] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str):
            return None
        lines.append(
            Line(
                id=entry["id"],
                name=str(entry.get("name") or entry["id"]),
                task=str(entry.get("task") or ""),
                assigned_model=entry.get("assigned_model") or None,
                assignment_audit=entry.get("assignment_audit") or None,
                depends_on=_string_list(entry.get("depends_on")),
                write_scope=_string_list(entry.get("write_scope")),
                declared_paths=_string_list(entry.get("declared_paths")),
                acceptance=_string_list(entry.get("acceptance")),
                risk_bearing=bool(entry.get("risk_bearing")),
                estimated_size=str(entry.get("estimated_size") or ""),
                app_claimable=entry.get(_APP_LINE_FIELD) is True,
            )
        )
    return lines


class LLMPlanner:
    """Decomposes a goal into a validated SPEC, a task DAG and an assignment.

    A refusal or malformed JSON is a plan failure, not a dispatch failure: the
    caller is told to refine the goal rather than being handed a guess. The last
    plan is kept on ``last_result`` so a caller can read the archive reference
    without a second call.

    When a ``HeadroomPort`` is supplied, each plan (and each replan) reads it
    once and hands the fractions to ``assign_dag``, so planner-assigned workers
    respect the same quota the selector would.
    """

    def __init__(
        self,
        complete: Complete,
        *,
        max_lines: int = 8,
        archive: SpecArchive | None = None,
        policy: RolePolicy | None = None,
        agents: Iterable[AgentState] | None = None,
        headroom: Any = None,
        default_worker: str = "",
        allowed_workers: Iterable[str] = (),
        resume_spec_ref: str = "",
        on_archived: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._complete = complete
        #: L2 restart recovery. The machine resumes a dispatch that stopped in
        #: PLANNING by calling ``plan(dispatch.spec_ref)``. Handed exactly the
        #: reference already on the row, the planner reads that archived plan
        #: back instead of asking the model again; any other text is a goal,
        #: so a goal that merely looks like a reference never loads another
        #: dispatch's plan. ``on_archived`` is told the reference of each plan
        #: it archives, so the caller can keep it on the row at once.
        self._resume_spec_ref = str(resume_spec_ref or "").strip()
        self._on_archived = on_archived
        self._max_lines = max_lines
        self._archive = archive
        self._policy = policy
        self._agents = tuple(agents) if agents is not None else None
        self._headroom = headroom
        #: L2: the workers this dispatch may use. Empty means every worker the
        #: role policy allows; otherwise the pool, the default and the final
        #: assignment are all held to it.
        self._allowed_workers = frozenset(normalise_workers(allowed_workers))
        #: The workspace's default provider, applied during assignment when the
        #: role policy has no other place for a line. It never overrides a
        #: provider the planner already placed on a line.
        self._default_worker = canonical_provider(str(default_worker or "").strip().lower())
        if self._allowed_workers and self._default_worker not in self._allowed_workers:
            self._default_worker = ""
        self.last_result: PlanResult | None = None

    @property
    def archive(self) -> SpecArchive | None:
        return self._archive

    async def plan_dag(self, goal: str) -> PlanResult:
        """Plan the goal into a SPEC, a DAG and a developer assignment."""
        goal = (goal or "").strip()
        if not goal:
            raise DispatchError("A dispatch goal is required")
        if _byte_size(goal) > MAX_GOAL_BYTES:
            # The PRD's stable reason code, so an operator can grep for it.
            raise PlannerFailure(
                GOAL_TOO_LARGE,
                f"goal_too_large: the dispatch goal is longer than "
                f"{MAX_GOAL_BYTES} bytes; refine the goal",
            )
        agents = self._resolve_agents()
        roster = {agent.provider: agent.capacity for agent in agents}
        # Quota is read before the places are counted: an exhausted developer
        # offers no place, so the planner is never told it may plan a line that
        # assign_dag would then leave unassigned as quota_exhausted.
        readings = await self._headroom_readings(agents)
        places = developer_places(
            agents,
            self._resolve_policy(),
            limit=self._max_lines,
            # N73 S3: quota is an input to the assignment, not a DAG place.
            headroom=None if quota_advisory() else readings,
        )
        app_claims = app_lines.lines_enabled()
        prompt = build_planner_prompt(
            goal, roster, developers=places, app_claims=app_claims
        )
        atomic = atomic_plans_enabled()
        outcome: PlanResult | PlanRejection | None = None
        last_error: DispatchError | None = None
        # A model reply is not deterministic: a long task string can come back
        # with a broken escape, or an empty lines list. Those are retried with a
        # short correction; a reply that parses and is rejected for its content
        # (scope, schema, roster) is final -- except the content rules a model
        # can fix on its own once told (N27, N28, N46): too many lines for the
        # pool, an acceptance command that runs a whole test directory, and an
        # acceptance list that runs no test of the line's own.
        correction = PLANNER_RETRY_NOTE
        corrected = False
        for attempt in range(PLANNER_MAX_ATTEMPTS):
            ask = prompt if attempt == 0 else prompt + correction
            correction = PLANNER_RETRY_NOTE
            raw = await self._complete(ask, planner_system_prompt(atomic=atomic))
            try:
                payload = _extract_json(raw)
            except DispatchError as exc:
                last_error = exc
                logger.warning("planner reply attempt %d unparseable", attempt + 1)
                continue
            outcome = validate_plan(
                payload,
                max_lines=self._max_lines,
                policy=self._resolve_policy(),
                agents=agents,
                headroom=readings,
                atomic=atomic,
                app_claims=app_claims,
                default_worker=self._default_worker,
                allowed_workers=self._allowed_workers,
            )
            if (
                isinstance(outcome, PlanRejection)
                and outcome.reason in _CORRECTABLE_REJECTIONS
                and not corrected
                and attempt + 1 < PLANNER_MAX_ATTEMPTS
            ):
                # Exactly one corrected retry. A second such rejection is
                # final below, under its own code.
                corrected = True
                logger.warning(
                    "planner reply attempt %d rejected: %s", attempt + 1, outcome.code
                )
                correction = _correction_note(outcome, places)
                outcome = None
                continue
            if isinstance(outcome, PlanRejection) and outcome.reason in _RETRYABLE_REJECTIONS:
                last_error = PlannerFailure(
                    INVALID_JSON,
                    f"Planner output rejected: {outcome.code}; refine the goal",
                )
                logger.warning(
                    "planner reply attempt %d rejected: %s", attempt + 1, outcome.code
                )
                outcome = None
                continue
            break
        if outcome is None:
            assert last_error is not None
            raise last_error
        if isinstance(outcome, PlanRejection):
            logger.debug("planner output rejected: %s", outcome.code)
            raise PlannerFailure(
                _failure_reason(outcome),
                f"Planner output rejected: {outcome.code}; refine the goal",
            )
        logger.debug(
            "planner produced %d line(s), %d unassigned",
            len(outcome.lines),
            len(outcome.assignment.unassigned),
        )
        return outcome

    async def plan(self, goal: str) -> list[Line]:
        """The :class:`Planner` protocol: the lines, already assigned.

        A line the role policy could not place is a plan failure rather than a
        line left for the selector: the message names every ``line_id:reason``
        so the operator sees the whole breakage at once.
        """
        if self._resume_spec_ref and str(goal or "").strip() == self._resume_spec_ref:
            return await self._archived_lines(self._resume_spec_ref)
        result = await self.plan_dag(goal)
        # Write the assignment back onto the lines before anything is archived,
        # so the archived plan and the returned lines say the same thing and the
        # spec_id is the one the caller can recompute.
        _apply_assignment(result, self._resolve_policy())
        # The plan is not allowed to leave a placeable line for the selector:
        # the dispatcher only launches a line whose assigned_model is set. A
        # workspace default fills exactly those lines the role policy could
        # not place, before the archive and before the UNASSIGNED failure, so
        # a live start uses the workspace's worker instead of planning a line
        # that can never launch.
        if self._default_worker:
            result = _apply_default_worker(
                result, self._default_worker, self._resolve_policy()
            )
        if quota_advisory():
            result = release_quota_unassigned(result)
        # The plan is kept and archived even when it cannot be placed: an
        # operator has to see the whole breakage, and the archive has to hold
        # it. Archiving first means an archive failure surfaces as
        # spec_archive_failed rather than hiding behind the assignment error.
        self.last_result = result
        if self._archive is not None:
            self.last_result = replace(
                result, spec_ref=await self._archive_plan(result)
            )
            await self._report_archived(self.last_result.spec_ref)
        if result.assignment.unassigned:
            raise PlannerFailure(
                UNASSIGNED,
                "Planner could not assign every line: "
                + ", ".join(result.unassigned_reasons())
                + "; refine the goal",
            )
        if self._allowed_workers:
            outside = [
                line.id
                for line in result.lines
                if line.assigned_model
                and canonical_provider(line.assigned_model) not in self._allowed_workers
            ]
            if outside:
                raise PlannerFailure(
                    UNASSIGNED,
                    "Planner could not assign every line: "
                    + ", ".join(f"{line_id}:{WORKER_NOT_ALLOWED}" for line_id in outside)
                    + "; refine the goal",
                )
        return list(result.lines)

    async def _archived_lines(self, spec_ref: str) -> list[Line]:
        load = getattr(self._archive, "load", None)
        lines = await load(spec_ref) if load is not None else None
        if not lines:
            raise PlannerFailure(
                INVALID_JSON,
                "The archived plan for this dispatch is missing; refine the goal",
            )
        if self._allowed_workers:
            for line in lines:
                if line.assigned_model and (
                    canonical_provider(line.assigned_model) not in self._allowed_workers
                ):
                    raise PlannerFailure(
                        UNASSIGNED,
                        f"Planner could not assign every line: {line.id}:"
                        f"{WORKER_NOT_ALLOWED}; refine the goal",
                    )
        return lines

    async def _report_archived(self, spec_ref: str) -> None:
        if not spec_ref or self._on_archived is None:
            return
        try:
            await self._on_archived(spec_ref)
        except Exception as exc:  # noqa: BLE001 - a recovery aid, never a plan failure
            logger.warning("Could not record the spec reference: %s", type(exc).__name__)

    async def _archive_plan(self, result: PlanResult) -> str:
        try:
            return await self._archive.save(_record(result))
        except OSError as exc:
            raise DispatchError(
                f"spec_archive_failed: {exc.__class__.__name__}"
            ) from exc

    def _resolve_policy(self) -> RolePolicy:
        if self._policy is None:
            from src.server.services.autonomy.roles import load_role_policy

            self._policy = load_role_policy()
        return self._policy

    def _resolve_agents(self) -> tuple[AgentState, ...]:
        agents = (
            self._agents
            if self._agents is not None
            else dag_agents_from_policy(self._resolve_policy())
        )
        if not self._allowed_workers:
            return agents
        return tuple(
            agent
            for agent in agents
            if canonical_provider(agent.provider) in self._allowed_workers
        )

    async def _headroom_readings(
        self, agents: Iterable[AgentState]
    ) -> Mapping[str, float] | None:
        """Canonical remaining fractions, or None when no HeadroomPort is wired."""
        if self._headroom is None:
            return None
        from src.server.services.autonomy.role_plan import read_headroom

        return await read_headroom(
            (agent.provider for agent in agents),
            self._headroom,
        )


def first_duplicate_task_id(ids: Iterable[str]) -> str | None:
    """The first id that repeats an earlier one, walking the input in order.

    ``validate_plan`` and ``assign_dag`` both call this, so a repeated line id
    is ``duplicate_task_id:<id>`` at both layers. A second copy of the walk
    would be free to pick a different id.
    """
    seen: set[str] = set()
    for task_id in ids:
        if task_id in seen:
            return task_id
        seen.add(task_id)
    return None


def validate_plan(
    payload: Any,
    *,
    max_lines: int = 8,
    policy: RolePolicy | None = None,
    agents: Iterable[AgentState] | None = None,
    require_tests_mode: str | None = None,
    headroom: Mapping[str, float] | None = None,
    atomic: bool | None = None,
    app_claims: bool | None = None,
    default_worker: str = "",
    allowed_workers: Iterable[str] = (),
) -> PlanResult | PlanRejection:
    """Hold a planner payload to the schema, or say exactly why it is not one.

    ``allowed_workers`` (L2) is the dispatch's own worker restriction: the
    developer pool and the workspace default are narrowed to it before any
    place is counted or assigned, so no line is placed on a worker the
    dispatch may not use. Empty keeps the whole pool.

    ``atomic`` admits the N76 ``atomic_with`` field; ``None`` reads the
    liveness mode (only enforce admits it; off and shadow keep main's schema).

    ``app_claims`` admits the N80 L4 boolean ``app_claimable`` field; ``None``
    reads ``CONDUCTOR_COLLAB_MODE`` (only ``full`` admits it).

    Pure: no clock, no IO, no model. Every failure is a :class:`PlanRejection`
    with a stable reason, and the checks run in a fixed order, so the same
    payload always yields the same first reason:

    ``schema_version`` -> ``spec`` -> ``spec`` size -> line count -> per-line
    text and byte limits -> duplicate names -> dependencies -> the dependency
    cycle -> write scopes -> acceptance (including ``acceptance_test_directory``
    and ``acceptance_missing_test``) -> field types -> unknown fields ->
    ``exceeds_developer_pool``.

    A self-dependency is reported as itself rather than as a cycle, because it
    is the more specific reason; everything else follows the order above.

    ``acceptance_missing_test`` (N46) is rolled out behind a mode (N58):
    ``require_tests_mode``, else ``CONDUCTOR_DISPATCH_G2_REQUIRE_TESTS_MODE``,
    default ``shadow``. Only ``enforce`` rejects; ``shadow`` logs the refusal
    it did not make and lets the plan through, and G2 records the same
    verdict on the line's gates snapshot; ``off`` skips the rule.

    A write-scope item failure rejects the plan only when no line survives it,
    so a plan of one illegal scope is still that same ``PlanRejection``. When
    another line is legal, the failing line is not built into a ``Line`` and
    is not archived. Its id was already checked, and the only thing kept is
    that id plus the checker's reason, on the unassigned list. Nothing else
    on that line is read. Lines that remain are checked in full. A line that
    depends on a filtered line is ``missing_dependency:<filtered line id>``
    and is not assignable, so it does not take part in the overlap check.
    Overlap among the lines that can still be assigned is still a plan
    rejection.
    """
    if atomic is None:
        atomic = atomic_plans_enabled()
    if app_claims is None:
        app_claims = app_lines.lines_enabled()
    if not isinstance(payload, Mapping):
        return PlanRejection("invalid_field", "payload")

    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return PlanRejection("schema_version_unsupported", _short(version))
    if version != PLANNER_SCHEMA_VERSION:
        return PlanRejection("schema_version_unsupported", _short(version))

    spec_payload = payload.get("spec")
    if not isinstance(spec_payload, Mapping):
        return PlanRejection("missing_spec", _short(spec_payload))
    title = spec_payload.get("title")
    body = spec_payload.get("body")
    if not isinstance(title, str) or not title.strip():
        return PlanRejection("missing_spec", "title")
    if not isinstance(body, str) or not body.strip():
        return PlanRejection("missing_spec", "body")
    # The SPEC is archived as UTF-8, so text no UTF-8 encoder can carry is a
    # rejection here rather than a failure later, when the archive writes it.
    if _uncarriable(title):
        return PlanRejection("invalid_text", "spec_title")
    if _uncarriable(body):
        return PlanRejection("invalid_text", "spec_body")
    if _byte_size(title) > MAX_SPEC_TITLE_BYTES:
        return PlanRejection("spec_too_large", "title")
    if _byte_size(body) > MAX_SPEC_BODY_BYTES:
        return PlanRejection("spec_too_large", "body")

    entries = payload.get("lines")
    if not isinstance(entries, list) or not entries:
        return PlanRejection("no_lines", _short(entries))
    if len(entries) > max_lines:
        return PlanRejection("too_many_lines", str(len(entries)))

    names: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            return PlanRejection("invalid_field", f"line:{index}")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            return PlanRejection("invalid_field", f"name:{index}")
        names.append(name.strip())

    for index, entry in enumerate(entries):
        task = entry.get("task")
        if isinstance(task, str) and not task.strip():
            return PlanRejection("invalid_field", f"task:{names[index]}")
        rejection = _check_line_size(entry)
        if rejection is not None:
            return rejection

    duplicate = first_duplicate_task_id(names)
    if duplicate is not None:
        return PlanRejection("duplicate_task_id", duplicate)
    # Membership for the dependency check. The duplicate walk itself is
    # ``first_duplicate_task_id``; this set is only "does the plan name it".
    seen = set(names)

    for index, entry in enumerate(entries):
        for dependency in _string_list(entry.get("depends_on")):
            if dependency == names[index]:
                return PlanRejection("self_dependency", names[index])
            if dependency not in seen:
                return PlanRejection("missing_dependency", dependency)
        for member in _string_list(entry.get("atomic_with")) if atomic else ():
            # Atomic groups are named like dependencies (planner names, not
            # the line-N ids) and every member must exist in this plan.
            if member not in seen:
                return PlanRejection("missing_dependency", member)

    if _has_cycle(names, entries):
        return PlanRejection("dependency_cycle")

    # A scope item failure filters that line. Its other fields are not read:
    # the id was checked already, and the reason is the checker's own. Overlap
    # is judged only among lines that can still be assigned. A child of a
    # filtered line is not assignable, so its scope overlapping a legal line
    # does not reject the plan.
    passed_scopes: list[int] = []
    scope_failures: list[tuple[int, str]] = []
    for index, entry in enumerate(entries):
        failure = ""
        for field_name in ("write_scope", "declared_paths"):
            if field_name == "declared_paths" and entry.get(field_name) is None:
                # Optional: a line without it owns its write scope (Line.__post_init__).
                continue
            _checked, reason = check_write_scope(entry.get(field_name))
            if reason is not None:
                failure = reason
                break
        if failure:
            scope_failures.append((index, failure))
            continue
        passed_scopes.append(index)
    if scope_failures and not passed_scopes:
        index, reason = scope_failures[0]
        return PlanRejection(reason, names[index])

    filtered_ids = {f"line-{index}" for index, _reason in scope_failures}
    blocked = _blocked_by_filtered(
        _survivor_lines(names, entries, passed_scopes), filtered_ids
    )
    assignable = [index for index in passed_scopes if f"line-{index}" not in blocked]
    seen_assignable: list[int] = []
    for index in assignable:
        rejection = _check_scopes(
            entries[index],
            names[index],
            index,
            names,
            entries,
            against=seen_assignable,
        )
        if rejection is not None:
            return rejection
        seen_assignable.append(index)

    # N73 S4: the deployment switch is record-only (enforce acts as shadow);
    # an explicit ``require_tests_mode`` argument still means what it says.
    mode = (
        record_only(check_mode(REQUIRE_TESTS_ENV), check="require_tests")
        if require_tests_mode is None
        else normalize_mode(require_tests_mode, source="require_tests_mode")
    )
    for index in passed_scopes:
        rejection = _check_acceptance(entries[index], names[index], mode)
        if rejection is not None:
            return rejection

    for index in passed_scopes:
        rejection = _check_line_types(
            entries[index], names[index], atomic=atomic, app_claims=app_claims
        )
        if rejection is not None:
            return rejection

    extra_top = sorted(str(key) for key in set(payload) - _TOP_FIELDS)
    if extra_top:
        return PlanRejection("unknown_field", extra_top[0])
    for index in passed_scopes:
        allowed = _ATOMIC_LINE_FIELDS if atomic else _LINE_FIELDS
        if app_claims:
            allowed = allowed | {_APP_LINE_FIELD}
        extra_line = sorted(str(key) for key in set(entries[index]) - allowed)
        if extra_line:
            return PlanRejection("unknown_field", extra_line[0])

    # N28: assign_dag gives every assignable line a developer place up front,
    # dependent lines included, so a plan with more assignable lines than the
    # pool has places always ends in no_capacity / dependency_unassigned. It
    # is refused here, with one code, before anything is assigned. A pool
    # with no place at all is left to the assignment, which names why.
    resolved_policy = policy or RolePolicy.default()
    pool = tuple(agents) if agents is not None else dag_agents_from_policy(resolved_policy)
    restriction = frozenset(normalise_workers(allowed_workers))
    if restriction:
        pool = tuple(
            agent for agent in pool if canonical_provider(agent.provider) in restriction
        )
        if canonical_provider(str(default_worker or "").strip().lower()) not in restriction:
            default_worker = ""
    # A workspace default is a developer place during assignment, so a plan
    # that asks for more lines than the role pool can staff is not rejected
    # before the default gets its turn. It is added only when the policy
    # accepts it as a code-writing developer; anything else stays a refusal.
    default = canonical_provider(str(default_worker or "").strip().lower())
    # The default is the workspace's explicit fallback for every write line.
    # It may name a worker outside the deployment's developer_pool; it may not
    # name a reserved or text-only one. The capability check uses a synthetic
    # write line, the same probe ``developer_places`` uses.
    default_ok = bool(default) and resolved_policy.may_write_code(default)
    if default_ok:
        default_ok, _reason = can_take_line(
            default, SimpleNamespace(task_id="default-probe", write_scope=("probe",))
        )
    if default_ok:
        # Give it enough places for the whole plan. Otherwise the capacity
        # check rejects the plan before assignment can use it, which is the
        # exact failure this default exists to prevent. A worker outside the
        # pool is not selectable by ``assign_dag``; the fallback in ``plan``
        # fills its lines after assignment.
        places = max(1, len(assignable))
        if default in {agent.provider for agent in pool}:
            pool = tuple(
                replace(agent, capacity=max(agent.capacity, places))
                if agent.provider == default
                else agent
                for agent in pool
            )
        else:
            pool = (*pool, AgentState(provider=default, capacity=places))
    # The quota view counts too: an exhausted developer is no place. A valid
    # workspace default is an extra fallback place for every line, so a plan
    # that merely outgrows the role pool is not rejected when it is set.
    places = developer_places(
        pool, resolved_policy, limit=len(assignable), headroom=headroom
    )
    if 0 < places < len(assignable) and not default_ok:
        return PlanRejection(EXCEEDS_DEVELOPER_POOL, f"{len(assignable)}>{places}")

    return _build_result(
        spec_payload,
        names,
        entries,
        policy,
        pool,
        passed_scopes,
        scope_failures,
        blocked,
        headroom=headroom,
    )


def _survivor_lines(
    names: list[str], entries: list[Any], passed: list[int]
) -> list[Line]:
    """Graph nodes for lines that passed the scope item check.

    Only ``depends_on`` is read. A filtered line is represented by its id in
    the caller's filtered set, not by a ``Line``, so its task, acceptance and
    risk are never touched.
    """
    by_name = {name: index for index, name in enumerate(names)}
    lines: list[Line] = []
    for index in passed:
        lines.append(
            Line(
                id=f"line-{index}",
                name=names[index],
                task="",
                depends_on=tuple(
                    f"line-{by_name[dependency]}"
                    for dependency in _string_list(entries[index].get("depends_on"))
                ),
            )
        )
    return lines


def _build_result(
    spec_payload: Mapping[str, Any],
    names: list[str],
    entries: list[Any],
    policy: RolePolicy | None,
    agents: Iterable[AgentState] | None,
    passed: list[int],
    failures: list[tuple[int, str]],
    blocked: dict[str, str],
    *,
    headroom: Mapping[str, float] | None = None,
) -> PlanResult:
    spec = Spec(title=_text(spec_payload["title"]), body=_text(spec_payload["body"]))
    by_name = {name: index for index, name in enumerate(names)}
    lines: list[Line] = []
    assignable: list[Line] = []
    held: list[Assignment] = []
    for index in passed:
        entry = entries[index]
        checked, _reason = check_write_scope(entry.get("write_scope"))
        owned = (
            check_write_scope(entry.get("declared_paths"))[0]
            if entry.get("declared_paths") is not None
            else None
        )
        line = Line(
            id=f"line-{index}",
            name=names[index],
            task=_text(entry["task"]),
            depends_on=tuple(
                f"line-{by_name[dependency]}"
                for dependency in _string_list(entry.get("depends_on"))
            ),
            write_scope=checked or (),
            declared_paths=owned or (),
            acceptance=_string_list(entry.get("acceptance")),
            risk_bearing=entry["risk_bearing"] is True,
            estimated_size=_estimated_size(entry.get("estimated_size")),
            # N76: names become line ids here, exactly like depends_on.
            atomic_with=tuple(
                f"line-{by_name[member]}"
                for member in _string_list(entry.get("atomic_with"))
                if member in by_name
            ),
            # N80 L4: admitted only under full mode; otherwise the plan was
            # already refused as unknown_field.
            app_claimable=entry.get(_APP_LINE_FIELD) is True,
        )
        lines.append(line)
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
        assignable.append(line)
    refused = tuple(
        Assignment(
            f"line-{index}",
            status="unassigned",
            reason=f"{reason}:line-{index}",
        )
        for index, reason in failures
    )
    resolved = policy or RolePolicy.default()
    chosen = tuple(agents) if agents is not None else dag_agents_from_policy(resolved)
    assignment = assign_dag(
        assignable, chosen, policy=resolved, headroom=headroom
    )
    return PlanResult(
        schema_version=PLANNER_SCHEMA_VERSION,
        spec=spec,
        lines=tuple(lines),
        assignment=AssignmentPlan(
            assignments=assignment.assignments,
            unassigned=assignment.unassigned + tuple(held) + refused,
        ),
        risk_bearing=frozenset(line.id for line in lines if line.risk_bearing),
    )


def _check_line_size(entry: Mapping[str, Any]) -> PlanRejection | None:
    """Text a process argument cannot carry, then byte limits, in field order.

    A NUL or a lone surrogate is not a size problem -- shortening the text would
    not fix it -- so it keeps its own reason rather than being folded into
    ``line_too_large``.
    """
    for field_name, limit in (
        ("name", MAX_LINE_NAME_BYTES),
        ("task", MAX_LINE_TASK_BYTES),
    ):
        value = entry.get(field_name)
        if isinstance(value, str) and _uncarriable(value):
            return PlanRejection("invalid_text", field_name)
        if isinstance(value, str) and _byte_size(value) > limit:
            return PlanRejection("line_too_large", field_name)
    # ``acceptance`` keeps its own byte and count limits here. ``write_scope``
    # does not: its whole-list check lives in check_write_scope, which decides
    # the first failing item in order, so a pre-scan here would let a later
    # item's invalid_text overtake an earlier item's reason.
    value = entry.get("acceptance")
    if isinstance(value, list):
        if len(value) > MAX_ACCEPTANCE_ENTRIES:
            return PlanRejection("line_too_large", "acceptance")
        for item in value:
            if isinstance(item, str) and _uncarriable(item):
                return PlanRejection("invalid_text", "acceptance")
            if isinstance(item, str) and _byte_size(item) > MAX_SCOPE_ENTRY_BYTES:
                return PlanRejection("line_too_large", "acceptance")
    return None


#: The result of checking a write scope: the values to store, or the stable
#: reason it was refused. Success is ``(trimmed values, None)``. A refusal is
#: ``(None, reason)``. A value is never returned alongside a reason.
ScopeCheck = tuple[tuple[str, ...], None] | tuple[None, str]


def check_write_scope(items: object) -> ScopeCheck:
    """Check a whole write scope and return the values to store, or why not.

    This is the ONE write-scope check in the dispatch path: the planner
    validator and the direct ``assign_dag`` entry both call it, so a spelling
    one refuses is refused by the other, with the same reason.

    The whole list is judged first -- missing, not a list, or empty is
    ``empty_write_scope``; more than 32 items is ``line_too_large`` -- and then
    each item in order, stopping at the first failure so the reason is always
    the first failing item's:

    1. not a string, or carrying a NUL -> ``invalid_text``;
    2. text no UTF-8 encoder can carry (a lone surrogate) -> ``invalid_text``;
    3. over 512 bytes -> ``line_too_large``;
    4. blank once trimmed -> ``empty_write_scope``;
    5. an absolute path, a drive letter, a leading ``~`` or a ``..`` segment in
       the trimmed spelling -> ``invalid_write_scope``;
    6. otherwise the trimmed value, which is what gets stored in the ``Line``,
       the archive and the assignment claim.

    The byte size is measured only after the value encodes, so a lone surrogate
    is reported as uncarriable text rather than as an oversized one. Trimming
    before the path checks is what makes the stored value the checked one.
    """
    if not isinstance(items, (list, tuple)) or len(items) == 0:
        return None, "empty_write_scope"
    if len(items) > MAX_WRITE_SCOPE_ENTRIES:
        return None, "line_too_large"
    checked: list[str] = []
    for raw in items:
        if not isinstance(raw, str) or "\x00" in raw:
            return None, "invalid_text"
        try:
            encoded = raw.encode("utf-8")
        except UnicodeEncodeError:
            return None, "invalid_text"
        if len(encoded) > MAX_SCOPE_ENTRY_BYTES:
            return None, "line_too_large"
        value = raw.strip()
        if not value:
            return None, "empty_write_scope"
        text = value.replace("\\", "/")
        if _ABSOLUTE_SCOPE.match(text) or ".." in text.split("/"):
            return None, "invalid_write_scope"
        checked.append(value)
    return tuple(checked), None


def validate_write_scope(scope: str) -> bool:
    """Whether a single write scope passes :func:`check_write_scope`.

    Kept as a boolean for callers that only need the verdict; the reason and the
    trimmed values come from ``check_write_scope``.
    """
    values, reason = check_write_scope([scope])
    return reason is None and values is not None


def _check_scopes(
    entry: Mapping[str, Any],
    name: str,
    index: int,
    names: list[str],
    entries: list[Any],
    *,
    against: list[int] | None = None,
) -> PlanRejection | None:
    """Empty, escaping or overlapping write scopes, in the PRD's order."""
    # The one scope check decides the first failing item, so the reason here is
    # the same reason the direct assign_dag entry gives for the same list.
    checked, reason = check_write_scope(entry.get("write_scope"))
    if reason:
        return PlanRejection(reason, name)
    owned: tuple[str, ...] | list[str] | None = checked
    if entry.get("declared_paths") is not None:
        owned, reason = check_write_scope(entry.get("declared_paths"))
        if reason:
            return PlanRejection(reason, name)
    others = range(index) if against is None else against
    for field_name, mine, code in (
        ("write_scope", checked or (), "write_scope_conflict"),
        ("declared_paths", owned or (), "declared_path_conflict"),
    ):
        for other_index in others:
            for theirs in _string_list(entries[other_index].get(field_name)):
                for scope in mine:
                    if scopes_overlap(scope, theirs):
                        return PlanRejection(code, f"{names[other_index]}:{name}")
    return None


def _check_acceptance(
    entry: Mapping[str, Any], name: str, require_tests_mode: str = ENFORCE
) -> PlanRejection | None:
    items = entry.get("acceptance")
    if not isinstance(items, list) or not any(
        isinstance(item, str) and item.strip() for item in items
    ):
        return PlanRejection("empty_acceptance", name)
    for item in items:
        if isinstance(item, str) and runs_test_directory(item):
            return PlanRejection(ACCEPTANCE_TEST_DIRECTORY, name)
    # N46: G2 runs what acceptance names, so a list that never runs a test
    # passes while covering nothing (R8c line-0-r1-3 was a ruff check and an
    # import check). The rule reads the line's own write_scope, which is what
    # the line actually changes, and the ``cmd:`` entries G2 would run.
    # N58: only ``enforce`` refuses; ``shadow`` says what it would have done.
    if require_tests_mode == OFF:
        return None
    refusal = _check_acceptance_runs_tests(entry, name, items)
    if refusal is not None and require_tests_mode == SHADOW:
        logger.warning("plan check %s (shadow, not enforced)", refusal.code)
        return None
    return refusal


def require_tests_refusal(line: Line) -> PlanRejection | None:
    """The N46 refusal a planned line would get in ``enforce`` mode, or None.

    G2 calls this in ``shadow`` mode to record, on the line's own gates
    snapshot, the plan rejection the validator did not make.
    """
    entry = {
        "write_scope": list(line.write_scope),
        "acceptance": list(line.acceptance),
        "task": line.task,
    }
    return _check_acceptance_runs_tests(entry, line.name or line.id, entry["acceptance"])


def _check_acceptance_runs_tests(
    entry: Mapping[str, Any], name: str, items: list[Any]
) -> PlanRejection | None:
    """N46: a code line's acceptance runs its own tests, or it is refused.

    A line whose whole write scope is documentation has no test to run, so the
    rule asks only for a test command on a code line. The test files it has to
    cover are the ones the line changes and the ones its task names: every one
    of them has to be named by at least one of those commands, because a command
    that runs some other line's tests, or none at all, leaves this line's work
    unverified while still reporting ``focused_tests_passed``.

    The detail is the line name alone when no command runs tests, and
    ``<name>:<test file>`` for the first test file no command covers, so the
    operator sees which file the plan forgot about.
    """
    scope = _string_list(entry.get("write_scope"))
    if not writes_code(scope):
        return None
    commands = [
        item for item in items if isinstance(item, str) and is_test_command(item)
    ]
    if not commands:
        return PlanRejection(ACCEPTANCE_MISSING_TEST, name)
    changed = [path for path in scope if is_test_path(path)]
    # A task that names a test file has to run it too, even when the scope is
    # the package directory the file will live in: the line is asked for the
    # regression test it describes, not only for the files it lists.
    changed.extend(
        path for path in _task_test_paths(entry.get("task")) if path not in changed
    )
    if not changed:
        return None
    named = [target for command in commands for target in test_command_targets(command)]
    for path in changed:
        if not any(_covers(target, path) for target in named):
            return PlanRejection(ACCEPTANCE_MISSING_TEST, f"{name}:{path}")
    return None


# A run of characters a task may spell a path with: everything but whitespace
# and the brackets and quotes a sentence puts around one.
_TASK_TOKEN = re.compile(r"[^\s\"'`()\[\]{}<>]+")
# A ``::selector`` or a ``:12`` line reference riding on the end of a path,
# with the sentence's punctuation still attached to it.
_TASK_PATH_SUFFIX = re.compile(r":\S*[.,;:!?]*$")


def _task_test_paths(task: Any) -> list[str]:
    """The test files a line's task names, in the order it names them.

    The task is prose a model wrote, so only a token that reads as a
    repository-relative test file counts: a bare ``test_x.py`` with no directory
    is as likely to be a word as a path, and the full stop after one belongs to
    the sentence. A URL is a reference, not a file the line changes.
    """
    if not isinstance(task, str):
        return []
    found: list[str] = []
    for token in _TASK_TOKEN.findall(task):
        candidate = _normalized_path(_TASK_PATH_SUFFIX.sub("", token).strip(".,;:!?"))
        if "/" not in candidate or "://" in candidate:
            continue
        if is_test_path(candidate) and candidate not in found:
            found.append(candidate)
    return found


# N27: an acceptance check is a line's own focused test. A command that runs a
# whole test directory (or the whole suite) checks other lines' work and
# whatever main already breaks, so it can fail for reasons the line cannot fix
# (ACCEPTANCE_TEST_DIRECTORY, also the planner failure reason).
# N28: the plan has more lines than the developer pool has places. The same
# code is the planner failure reason a parked dispatch carries
# (``planner_failed:exceeds_developer_pool``).
# N46: an acceptance list of lint, import and type checks passes G2 while
# covering nothing, so a code line has to run its own tests and, when it
# changes test files, the command has to name them.
#: N46's code. It is a plan-validation code, not a planner failure reason: a
#: plan that still breaks the rule parks as ``planner_error``, and the code
#: itself reaches the operator in the retry note and the rejection log.
ACCEPTANCE_MISSING_TEST = "acceptance_missing_test"

_PYTEST_NAMES = frozenset({"pytest", "py.test"})
_PYTHON_NAME = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
# Runners that start the program named after them: "uv run pytest ...".
_RUN_WRAPPERS = frozenset({"uv", "poetry", "pipenv", "hatch", "pdm", "rye"})
# pytest options whose value is the next argument, so the value is not read
# as a test path. A ``--name=value`` spelling is one argument and needs no entry.
_PYTEST_VALUE_OPTIONS = frozenset(
    {
        "-k", "-m", "-p", "-W", "-c", "-o", "-n", "-r",
        "--deselect", "--ignore", "--ignore-glob", "--rootdir", "--confcutdir",
        "--maxfail", "--tb", "--durations", "--durations-min", "--junitxml",
        "--junit-xml", "--junit-prefix", "--basetemp", "--log-level",
        "--log-format", "--log-date-format", "--log-cli-level",
        "--log-cli-format", "--log-cli-date-format", "--log-file",
        "--log-file-level", "--log-file-format", "--log-file-date-format",
        "--timeout", "--cov", "--cov-report", "--cov-config", "--override-ini",
        "--import-mode", "--capture", "--color", "--code-highlight", "--dist",
        "--numprocesses", "--pythonwarnings",
        "--assert", "--show-capture", "--report-log",
    }
)
# Test runners that are named after what they do, so the program position is
# enough: "vitest run", "jest", "tox". A pytest run is recognised separately,
# through ``_pytest_arguments``, because it is also spelled "python -m pytest".
_TEST_RUNNER_NAMES = frozenset(
    {"vitest", "jest", "tox", "nox", "mocha", "rspec", "phpunit", "ctest"}
)
# Options that make a runner report about itself instead of running a test:
# "pytest --version", "pytest --collect-only", "cargo test --no-run". Each
# exits zero, so an acceptance list built from one passes G2 having run
# nothing -- the hole N46 exists to close.
_NO_RUN_OPTIONS = frozenset(
    {
        "--version", "-V",
        "--help", "-h",
        "--collect-only", "--co",
        "--fixtures", "--markers", "--setup-plan",
        "--no-run", "--notest",
    }
)
# Runners that take the verb first: "go test ./...", "cargo test", "pnpm test".
_TEST_RUNNER_VERBS = frozenset(
    {
        ("go", "test"),
        ("cargo", "test"),
        ("pnpm", "test"),
        ("npm", "test"),
        ("yarn", "test"),
        ("bun", "test"),
        ("make", "test"),
        ("just", "test"),
    }
)
# Directories a test file lives under, whatever the language.
_TEST_DIRECTORY_NAMES = frozenset(
    {"test", "tests", "testing", "__tests__", "spec", "specs"}
)
# A file whose own name says it is a test: test_x.py, x_test.py, x.test.ts,
# x.spec.ts and conftest.py. Matched against the name without its extension.
_TEST_BASENAME = re.compile(r"^(?:test_.+|.+[._]test|.+[._]spec|conftest)$")
# Extensions a line writes prose in. A scope made only of these has no test
# to run, so N46 asks a code line for one and leaves a documentation line
# with the assertion it wrote.
_DOCUMENT_SUFFIXES = frozenset({".md", ".mdx", ".rst", ".adoc", ".txt"})


def _program_name(item: str) -> str:
    return PurePosixPath(item.replace("\\", "/")).name


def _program_index(argv: list[str]) -> int:
    """The index of the program in ``argv``, stepping over the preamble.

    Only the program position counts: ``VAR=value`` assignments, ``env`` and a
    runner (``uv run``, ``poetry run``, ``uvx``) are stepped over, then the
    program is ``pytest``/``py.test`` (or a path to one), or a Python
    interpreter whose options include ``-m pytest``. An argument that merely
    names a pytest path (``ruff check tests/pytest``) is not a pytest run.
    """
    index = 0
    while index < len(argv):
        item = argv[index]
        name = _program_name(item)
        if "=" in item and not item.startswith("-") and "/" not in item.split("=", 1)[0]:
            index += 1
        elif name == "env":
            index += 1
            while index < len(argv) and argv[index].startswith("-"):
                index += 1
        elif name in _RUN_WRAPPERS and index + 1 < len(argv) and argv[index + 1] == "run":
            index += 2
            while index < len(argv) and argv[index].startswith("-"):
                index += 1
        elif name == "uvx":
            index += 1
            while index < len(argv) and argv[index].startswith("-"):
                index += 1
        else:
            break
    return index


def _pytest_arguments(argv: list[str]) -> list[str] | None:
    """The arguments after ``pytest`` in ``argv``, or ``None`` if it runs no pytest.

    The program position is the one :func:`_program_index` finds.
    """
    index = _program_index(argv)
    if index >= len(argv):
        return None
    name = _program_name(argv[index])
    if name in _PYTEST_NAMES:
        return argv[index + 1:]
    if _PYTHON_NAME.match(name):
        position = index + 1
        while position < len(argv) and argv[position].startswith("-"):
            option = argv[position]
            if option == "-m":
                if position + 1 < len(argv) and argv[position + 1] in _PYTEST_NAMES:
                    return argv[position + 2:]
                return None
            if option == "-c":
                # "python -c code" runs that code; nothing after it is -m.
                return None
            # -W and -X take a value: "python -W ignore -m pytest".
            position += 2 if option in ("-W", "-X") else 1
    return None


def runs_test_directory(acceptance: str) -> bool:
    """Whether a ``cmd:`` acceptance entry runs pytest on more than named files.

    The entry is read the way G2 reads it (``gates.acceptance_command_body``)
    after the trim the plan stores it with. True for ``pytest tests/unit``,
    ``python -m pytest -q tests`` and a bare ``pytest -q`` (the whole suite);
    false for ``pytest tests/unit/test_x.py``,
    ``pytest tests/unit/test_x.py::test_one``, ``pytest --pyargs pkg.mod``,
    a non-pytest command and a human-readable assertion.
    """
    body = acceptance_command_body(acceptance.strip())
    if body is None:
        return False
    try:
        argv = shlex.split(body)
    except ValueError:
        return False
    arguments = _pytest_arguments(argv)
    if arguments is None:
        return False
    targets: list[str] = []
    skip = False
    pyargs = False
    for item in arguments:
        if skip:
            skip = False
            continue
        if item == "--":
            continue
        if item.startswith("-"):
            pyargs = pyargs or item == "--pyargs"
            skip = item in _PYTEST_VALUE_OPTIONS
            continue
        targets.append(item)
    if not targets:
        return True
    if pyargs:
        # Targets are importable names; a module or a test file is named.
        return False
    return any(not target.split("::", 1)[0].endswith(".py") for target in targets)


def _runs_no_test(arguments: list[str]) -> bool:
    """Whether a runner's arguments ask it to report rather than to run.

    The arguments are walked the way :func:`test_command_targets` walks them,
    so the value of an option that takes one is never read as a flag of its
    own. A ``--name=value`` spelling is one argument.
    """
    skip = False
    for item in arguments:
        if skip:
            skip = False
            continue
        if not item.startswith("-"):
            continue
        flag = item.split("=", 1)[0]
        if flag in _NO_RUN_OPTIONS:
            return True
        skip = flag in _PYTEST_VALUE_OPTIONS
    return False


def _test_command_arguments(argv: list[str]) -> list[str] | None:
    """The arguments of a test command in ``argv``, or ``None`` if it runs none.

    A pytest run is one, and so is a runner that is named after what it does
    (``vitest``, ``jest``, ``tox``) or takes the verb first (``go test``,
    ``pnpm test``). The program position is the one :func:`_program_index`
    finds, so ``uv run pytest`` and ``env -i python -m pytest`` are read here
    exactly as ``runs_test_directory`` reads them.

    A command that only reports about itself is not one: see
    :data:`_NO_RUN_OPTIONS`.
    """
    pytest_arguments = _pytest_arguments(argv)
    if pytest_arguments is not None:
        return None if _runs_no_test(pytest_arguments) else pytest_arguments
    index = _program_index(argv)
    if index >= len(argv):
        return None
    name = _program_name(argv[index])
    if name in _TEST_RUNNER_NAMES:
        arguments = argv[index + 1 :]
    elif index + 1 < len(argv) and (name, argv[index + 1]) in _TEST_RUNNER_VERBS:
        arguments = argv[index + 2 :]
    else:
        return None
    return None if _runs_no_test(arguments) else arguments


def _normalized_path(path: str) -> str:
    """A repository path as the plan stores it: one separator, no ``./``."""
    text = path.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/")


def is_test_path(path: str) -> bool:
    """Whether ``path`` names a test file rather than the code under test.

    A file whose own name says it is a test is one: ``test_x.py``, ``x_test.py``,
    ``x.test.ts``, ``x.spec.ts`` and ``conftest.py``. So is a file that sits
    directly in a test directory (``tests/``, ``__tests__/``, ``spec/``), which
    is where a runner collects from. A file deeper inside that tree is a helper
    those tests load rather than a test of its own -- a ``tests/unit/server/
    app/x.py`` is exercised by the tests beside it -- so it demands no command
    naming it, and neither does a directory a scope names.

    Prose is never one, wherever it sits, and the last component has to carry
    an extension, so a write scope that names a test directory (``tests/unit``)
    is not itself a test file and does not demand a command naming it.

    The spelling the plan stores is what is judged, a glob included:
    ``tests/unit/test_*.py`` is a test file a command naming
    ``tests/unit/test_x.py`` covers.
    """
    parts = [
        part for part in _normalized_path(path).split("/") if part not in ("", ".")
    ]
    if not parts:
        return False
    name = parts[-1]
    suffix = PurePosixPath(name).suffix.casefold()
    if suffix in _DOCUMENT_SUFFIXES:
        return False
    if "." not in name:
        return False
    stem = name.rsplit(".", 1)[0]
    if _TEST_BASENAME.match(stem.casefold()):
        return True
    return len(parts) > 1 and parts[-2].casefold() in _TEST_DIRECTORY_NAMES


def writes_code(scope: Iterable[str]) -> bool:
    """Whether a write scope changes code rather than only documentation.

    N46 asks for a test command on a *code* line: a line whose whole scope is
    prose has no test to run, and refusing it would refuse the documentation
    work a plan legitimately splits out. One entry that is not prose makes the
    line a code line.
    """
    entries = [entry for entry in scope if isinstance(entry, str) and entry.strip()]
    if not entries:
        return False
    return any(
        PurePosixPath(_normalized_path(entry)).suffix.casefold()
        not in _DOCUMENT_SUFFIXES
        for entry in entries
    )


def is_test_command(acceptance: str) -> bool:
    """Whether an acceptance entry is a command that runs tests.

    Only a ``cmd:`` entry can be: G2 runs those and reads everything else as
    text for a reviewer, so a human-readable assertion is not a test command
    however it is spelled.
    """
    body = acceptance_command_body(acceptance.strip())
    if body is None:
        return False
    try:
        argv = shlex.split(body)
    except ValueError:
        return False
    return _test_command_arguments(argv) is not None


def test_command_targets(acceptance: str) -> tuple[str, ...]:
    """The test files a test command names, in the order it names them.

    Only the arguments that name a test file count: an option, the value of an
    option that takes one, and a plain path that is not a test file are all
    stepped over, so ``-k`` expressions and a ``--rootdir`` are not read as
    targets. A ``::test`` selector is dropped, because the file is what the
    line changes.
    """
    body = acceptance_command_body(acceptance.strip())
    if body is None:
        return ()
    try:
        argv = shlex.split(body)
    except ValueError:
        return ()
    arguments = _test_command_arguments(argv)
    if arguments is None:
        return ()
    targets: list[str] = []
    skip = False
    for item in arguments:
        if skip:
            skip = False
            continue
        if item == "--":
            continue
        if item.startswith("-"):
            skip = item in _PYTEST_VALUE_OPTIONS
            continue
        candidate = item.split("::", 1)[0]
        if is_test_path(candidate):
            targets.append(_normalized_path(candidate))
    return tuple(targets)


def _covers(named: str, changed: str) -> bool:
    """Whether a test file a command names covers one the line changes.

    The same path is covered. A glob is matched in either direction, because a
    write-scope entry may be a glob and a command target may not be. A
    directory a command names covers what is under it, which is how a
    non-pytest runner is spelled. A ``conftest.py`` is covered by the tests
    under its own directory, which is how pytest loads it.
    """
    if named == changed:
        return True
    if fnmatchcase(named, changed) or fnmatchcase(changed, named):
        return True
    if changed.startswith(f"{named}/") or named.startswith(f"{changed}/"):
        return True
    return _loads_conftest(named, changed)


def _loads_conftest(named: str, changed: str) -> bool:
    """Whether a named test loads the fixtures a changed conftest declares.

    pytest reads a conftest for every test at or below its directory, so the
    line's command has to name one of those tests rather than the conftest:
    requiring the conftest itself would refuse a line whose fixtures its own
    tests exercise.
    """
    if PurePosixPath(changed).name != "conftest.py":
        return False
    if "/" not in changed:
        # The repository's own conftest, which every test loads.
        return True
    return named.startswith(f"{changed.rsplit('/', 1)[0]}/")


def developer_places(
    agents: Iterable[AgentState],
    policy: RolePolicy,
    *,
    limit: int,
    headroom: Mapping[str, float] | None = None,
) -> int:
    """How many write lines the pool can staff at once, counted up to ``limit``.

    The count asks the role policy itself, the way ``assign_dag`` does: one
    place at a time, each taken place counted as active, until the policy
    has no developer left or ``limit`` is reached. So a reserved model, a
    text-only Worker, an unavailable or exhausted one, and one already at
    its capacity add no place. ``headroom`` is the same remaining-fraction
    map ``assign_dag`` is given; without it every developer looks open.
    """
    base = list(agents)
    used: dict[str, int] = {}
    probe = SimpleNamespace(task_id="capacity-probe", write_scope=("capacity-probe",))
    places = 0
    while places < max(limit, 0):
        adjusted = [
            replace(agent, active=agent.active + used.get(agent.provider, 0))
            for agent in base
        ]
        chosen = policy.choose_developer(adjusted, line=probe, headroom=headroom)
        if chosen is None:
            break
        used[chosen.provider] = used.get(chosen.provider, 0) + 1
        places += 1
    return places


def _failure_reason(rejection: PlanRejection) -> str:
    """The planner failure reason for a rejection that ends the planning."""
    if rejection.reason in _CORRECTABLE_REJECTIONS:
        return rejection.reason
    return INVALID_JSON


def _correction_note(rejection: PlanRejection, places: int) -> str:
    """The retry note for a content rejection the model can fix itself."""
    if rejection.reason == EXCEEDS_DEVELOPER_POOL:
        return (
            f"\n\nYour previous plan was rejected ({rejection.code}): only {places}"
            f" developer place(s) are available, so the plan may have at most"
            f" {places} line(s). Merge small or related items into fewer lines"
            " and reply with the whole plan JSON object again."
        )
    if rejection.reason == ACCEPTANCE_MISSING_TEST:
        return (
            f"\n\nYour previous plan was rejected ({rejection.code}): every code"
            " line needs at least one acceptance command that runs tests, and it"
            " must name the test files the line changes or its task names"
            " (cmd: pytest -q tests/unit/test_x.py). A lint, import or type check"
            " on its own is not acceptance. Reply with the whole plan JSON object"
            " again."
        )
    return (
        f"\n\nYour previous plan was rejected ({rejection.code}): an acceptance"
        " command ran pytest on a whole test directory or the whole suite. Name"
        " the line's own test files instead (cmd: pytest -q tests/unit/test_x.py)"
        " and reply with the whole plan JSON object again."
    )


def _check_line_types(
    entry: Mapping[str, Any],
    name: str,
    *,
    atomic: bool = False,
    app_claims: bool = False,
) -> PlanRejection | None:
    """Field types, after the value checks that need the right type to run."""
    for field_name in ("name", "task"):
        if not isinstance(entry.get(field_name), str):
            return PlanRejection("invalid_field", f"{field_name}:{name}")
    for field_name in (
        "depends_on",
        "write_scope",
        "declared_paths",
        "acceptance",
        *(("atomic_with",) if atomic else ()),
    ):
        value = entry.get(field_name)
        if value is not None and (
            not isinstance(value, list)
            or not all(isinstance(item, str) for item in value)
        ):
            return PlanRejection("invalid_field", f"{field_name}:{name}")
    if not isinstance(entry.get("risk_bearing"), bool):
        return PlanRejection("invalid_field", f"risk_bearing:{name}")
    # estimated_size is the one optional field: absent is fine, a present value
    # must still be a string. An unrecognised size is not rejected here -- it is
    # normalised away in _build_result, so a bad hint never fails the plan.
    estimated = entry.get("estimated_size")
    if estimated is not None and not isinstance(estimated, str):
        return PlanRejection("invalid_field", f"estimated_size:{name}")
    if (
        app_claims
        and _APP_LINE_FIELD in entry
        and not isinstance(entry[_APP_LINE_FIELD], bool)
    ):
        return PlanRejection("invalid_field", f"{_APP_LINE_FIELD}:{name}")
    return None


def _has_cycle(names: list[str], entries: list[Any]) -> bool:
    graph = {
        names[index]: _string_list(entry.get("depends_on"))
        for index, entry in enumerate(entries)
    }
    visiting: set[str] = set()
    settled: set[str] = set()

    def walk(node: str) -> bool:
        if node in settled:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for dependency in graph.get(node, ()):
            if walk(dependency):
                return True
        visiting.discard(node)
        settled.add(node)
        return False

    return any(walk(name) for name in graph)


#: Assignment failures a workspace default may fill. A filtered scope or a
#: dangling dependency is a defect in the plan, not a capacity shortage, and
#: must still surface as an UNASSIGNED plan failure.
_DEFAULT_FILLABLE_REASONS = frozenset(
    {"no_capacity", "quota_exhausted", "dependency_unassigned"}
)


def _apply_default_worker(
    result: PlanResult, provider: str, policy: RolePolicy
) -> PlanResult:
    """Place the capacity leftovers on the workspace's default worker.

    The provider is used only when the role policy accepts it as a developer
    that may write code: a workspace cannot promote a reserved or text-only
    model by naming it as its default. Lines the planner placed are left
    alone, and a line refused for a scope or dependency defect keeps its
    reason so the UNASSIGNED failure below still names it.
    """
    candidate = canonical_provider(str(provider or "").strip().lower())
    if not candidate or not policy.may_write_code(candidate):
        return result
    filled: set[str] = set()
    for item in result.assignment.unassigned:
        if item.reason.split(":", 1)[0] in _DEFAULT_FILLABLE_REASONS:
            filled.add(item.task_id)
    if not filled:
        return result
    by_id = {line.id: line for line in result.lines}
    placements: list[Assignment] = []
    for task_id in sorted(filled):
        line = by_id.get(task_id)
        if line is None or line.assigned_model:
            continue
        allowed, _reason = can_take_line(candidate, line)
        if not allowed:
            continue
        line.assigned_model = candidate
        placements.append(Assignment(task_id, provider=candidate))
    if not placements:
        return result
    assigned_ids = {item.task_id for item in placements}
    result = replace(
        result,
        assignment=AssignmentPlan(
            assignments=tuple(result.assignment.assignments) + tuple(placements),
            unassigned=tuple(
                item
                for item in result.assignment.unassigned
                if item.task_id not in assigned_ids
            ),
        ),
    )
    logger.info(
        "workspace default worker %s filled %d unassigned line(s)",
        candidate,
        len(placements),
    )
    return result


def release_quota_unassigned(result: PlanResult) -> PlanResult:
    """N73 S3: a line left unplaced only for quota is not a plan failure.

    It keeps an empty ``assigned_model``, so ``_assign`` picks its author at
    launch time with the quota-aware selector (or the Super-agent's choice
    and its feasibility check). Scope and dependency defects still fail.
    """
    ids = {
        item.task_id
        for item in result.assignment.unassigned
        if item.reason.split(":", 1)[0] == "quota_exhausted"
    }
    if not ids:
        return result
    # A line unplaced only because its dependency was released goes too.
    changed = True
    while changed:
        changed = False
        for item in result.assignment.unassigned:
            code, _, dep = item.reason.partition(":")
            if item.task_id not in ids and code == "dependency_unassigned" and dep in ids:
                ids.add(item.task_id)
                changed = True
    logger.info("quota left %d line(s) for launch-time assignment", len(ids))
    return replace(
        result,
        assignment=AssignmentPlan(
            assignments=tuple(result.assignment.assignments),
            unassigned=tuple(
                item for item in result.assignment.unassigned if item.task_id not in ids
            ),
        ),
    )


def _apply_assignment(result: PlanResult, policy: RolePolicy) -> None:
    """Write each placed line's developer onto the line itself.

    A line the policy could not place keeps its empty model, so the archived
    plan shows exactly which lines were placed and which were not. When the
    emergency ``allow_reserved_code`` override put a reserved planner or reviewer
    on a work line, the reason is written onto the line and logged, exactly as
    the live selector does, so the override is visible in the archive too.
    """
    by_task = result.assignment.by_task()
    reserved = policy.canonical_reserved()
    for line in result.lines:
        placed = by_task.get(line.id)
        if placed is None or not placed.assigned:
            continue
        line.assigned_model = placed.provider
        if canonical_provider(placed.provider) in reserved:
            reason = reserved_work_audit(placed.provider)
            line.assignment_audit = reason
            logger.info("dispatch assignment audit: %s", reason)


def _record(result: PlanResult) -> SpecRecord:
    return SpecRecord(
        spec=result.spec,
        lines=result.lines,
        assignment=result.assignment,
        risk_bearing=result.risk_bearing,
        schema_version=result.schema_version,
        prompt_version=result.prompt_version,
    )


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` or not at all: a unique temp file plus a rename.

    The temporary name is unique per call, so two threads archiving the same
    plan at the same time cannot rename each other's file away.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _spec_markdown(spec: Spec) -> str:
    return f"# {spec.title}\n\n{spec.body}\n"


def _string_list(value: Any) -> tuple[str, ...]:
    """The trimmed, non-empty string items of a stored list.

    Trimming here is what makes the *stored* value, so a caller that validates
    with :func:`validate_write_scope` and then stores through this function is
    validating exactly what it stores.
    """
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _estimated_size(value: Any) -> str:
    """A valid size token, or ``""`` for a missing or unrecognised estimate.

    The planner's ``estimated_size`` is an optional hint, so an unknown value
    is normalised away rather than rejected: the line keeps its default and
    only ``risk_bearing`` can still make it ``large``.
    """
    if isinstance(value, str):
        token = value.strip().lower()
        if token in ESTIMATED_SIZES:
            return token
    return ""


def _byte_size(text: str) -> int:
    """UTF-8 size, or one past every limit for text no argument can carry."""
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return MAX_LINE_TASK_BYTES + 1


def _uncarriable(text: str) -> bool:
    """Whether ``text`` is a NUL, or a lone surrogate no argv can carry."""
    if "\x00" in text:
        return True
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _short(value: Any) -> str:
    return repr(value)[:_DETAIL_LIMIT]


PLANNER_MAX_ATTEMPTS = 3
PLANNER_RETRY_NOTE = (
    "\n\nYour previous reply could not be used as a plan (invalid JSON, or a "
    "missing spec or lines). Reply again with only the plan JSON object in the "
    "requested schema; escape every quote and backslash inside strings."
)
# Rejections that mean "the reply was unusable", not "the plan is wrong".
_RETRYABLE_REJECTIONS = frozenset({"no_lines", "invalid_field", "missing_spec"})
# Content rejections a model fixes once it is told the rule it broke.
_CORRECTABLE_REJECTIONS = frozenset(
    {
        EXCEEDS_DEVELOPER_POOL,
        ACCEPTANCE_TEST_DIRECTORY,
        ACCEPTANCE_MISSING_TEST,
    }
)


def _extract_json(raw: str) -> Any:
    """The plan object from a model reply.

    Prose around the JSON may itself contain braces (a quoted regex, a code
    sample), so slicing from the first open brace to the last close brace is
    not enough. Every open brace is tried with ``raw_decode`` and only an object
    with a ``lines`` key counts as the plan; when several do (an example in the
    prose), the longest one wins, which is the outer plan. A reply whose plan
    object does not decode (a broken escape) is a parse failure, never a nested
    object from inside it, so the caller can retry.
    """
    text = _FENCE.sub("", (raw or "").strip())
    if "{" not in text:
        raise PlannerFailure(
            NO_JSON, "Planner returned no JSON object; refine the goal"
        )
    decoder = json.JSONDecoder()
    best: Any = None
    best_span = -1
    first_error: json.JSONDecodeError | None = None
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            if first_error is None:
                first_error = exc
        else:
            if isinstance(value, dict) and "lines" in value and end - index > best_span:
                best, best_span = value, end - index
        index = text.find("{", index + 1)
    if best is not None:
        return best
    message = first_error.msg if first_error is not None else "no plan object with lines"
    raise PlannerFailure(INVALID_JSON, f"Planner returned invalid JSON: {message}")
