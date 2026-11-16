"""Deep-refactor wire contracts: goals, plans, runs, permissions, memory tiers.

Phase 1 of the refactor makes state that used to be implicit (goal, plan,
permission mode, subagent result, memory tier, lifecycle event) explicit. The
values below are the one spelling shared by three ends that cannot import each
other: the migration CHECK constraints, the Python services that write rows, and
the TypeScript interface. The migrations spell the sets out in SQL instead of
referencing this module (they must stay runnable without the application stack),
so ``tests/unit/migrations/test_refactor_migrations.py`` reads the SQL back and
asserts the two ends still agree — the same pattern ``contracts/status.py`` uses
for the run ledger.

Nothing here reads the environment, the database or a model. Import-safe from
the agent library and the server.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from src.server.contracts.memory_origin import (
    MEMORY_ORIGINS,
    MemoryOrigin,
    normalize_memory_metadata,
)
from src.server.contracts.memory_policy_metadata import (
    normalize_memory_policy_metadata_layers,
)
from src.server.contracts.memory_tool_policy import (
    MemoryToolPolicyError,
    normalize_memory_tool_policy_metadata,
)

# A subagent result is persisted as JSON, read by the parent and shipped to the
# UI. Bumping this is a data-format change: the parent merge must accept both
# versions until every recorded run is either migrated or read-only.
SUBAGENT_CONTRACT_VERSION = 1

# Lifecycle vocabularies. Terminal states are deliberately separate from live
# ones so a "completed" goal can never be moved back by a late start event; a
# transition helper below is the only place a legal move is decided.
GoalStatus = Literal["active", "paused", "completed", "cancelled", "blocked"]
GOAL_STATUSES: tuple[GoalStatus, ...] = get_args(GoalStatus)
GOAL_TERMINAL_STATUSES = frozenset({"completed", "cancelled"})
GOAL_TRANSITIONS: Mapping[GoalStatus, frozenset[GoalStatus]] = {
    "active": frozenset({"paused", "completed", "cancelled", "blocked"}),
    "paused": frozenset({"active", "cancelled"}),
    "blocked": frozenset({"active", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}

PlanStatus = Literal["draft", "awaiting_approval", "approved", "rejected", "superseded"]
PLAN_STATUSES: tuple[PlanStatus, ...] = get_args(PlanStatus)
PLAN_TRANSITIONS: Mapping[PlanStatus, frozenset[PlanStatus]] = {
    "draft": frozenset({"awaiting_approval", "superseded"}),
    "awaiting_approval": frozenset({"approved", "rejected", "superseded"}),
    "approved": frozenset({"superseded"}),
    "rejected": frozenset({"superseded"}),
    "superseded": frozenset(),
}

# How much the agent may do without asking. "manual" asks before every
# state-changing action; "auto" allows read-only actions and asks for writes;
# "skip" runs without prompts. The mode is per goal/thread and enforced by the
# policy layer, never by the prompt alone.
PermissionMode = Literal["manual", "auto", "skip"]
PERMISSION_MODES: tuple[PermissionMode, ...] = get_args(PermissionMode)

# The verifier answers one question: did the run satisfy the goal's stop
# conditions? "unknown" is a valid answer (model down, evidence unreadable) and
# must not be treated as success.
VerifierVerdict = Literal["satisfied", "unsatisfied", "unknown"]
VERIFIER_VERDICTS: tuple[VerifierVerdict, ...] = get_args(VerifierVerdict)

# Five memory tiers. The storage namespaces live in backends/langgraph_store.py;
# "project" is resolved as the workspace's project metadata in Phase 2 unless a
# first-class project object lands (see docs/refactor/0004-data-model.md).
MemoryTier = Literal["user", "project", "workspace", "session", "task"]
MEMORY_TIERS: tuple[MemoryTier, ...] = get_args(MemoryTier)

MemoryEntryStatus = Literal["active", "superseded", "proposed", "rejected"]
MEMORY_ENTRY_STATUSES: tuple[MemoryEntryStatus, ...] = get_args(MemoryEntryStatus)

# Subagent-derived memory proposals keep the subagent prompt's own vocabulary.
# A proposal is never written to a tier directly: the parent decides.
MemoryProposalType = Literal["user", "feedback", "project", "reference"]
MEMORY_PROPOSAL_TYPES: tuple[MemoryProposalType, ...] = get_args(MemoryProposalType)

# Task tree vocabulary. The tree is persisted in ``agent_tasks``; a todo row
# comes from the main agent's TodoWrite middleware, a subagent mirror row from a
# delegated task (linked by ``subagent_task_ref``, not a duplicate of its state).
TaskStatus = Literal["pending", "in_progress", "completed", "blocked", "cancelled"]
TASK_STATUSES: tuple[TaskStatus, ...] = get_args(TaskStatus)
TaskSource = Literal["todo", "subagent"]
TASK_SOURCES: tuple[TaskSource, ...] = get_args(TaskSource)

# Append-only event vocabulary. Phase 1 writes only the subset the new paths
# produce; the tuple is the allowed set so the table cannot grow by typo. Event
# payloads are redacted at write time and never carry prompts, keys or full
# tool results (store ids/hashes instead).
EVENT_TYPES: tuple[str, ...] = (
    "goal.created",
    "goal.status_changed",
    "goal.verdict",
    "plan.created",
    "plan.decided",
    "run.contract",
    "permission.mode_changed",
    "permission.decision",
    "memory.injected",
    "memory.proposed",
    "memory.approved",
    "memory.rejected",
    "subagent.spawned",
    "subagent.status_changed",
    "subagent.result",
    "task.created",
    "task.status_changed",
)
EVENT_TYPE_SET = frozenset(EVENT_TYPES)


class ContractError(ValueError):
    """A contract value or transition the rest of the system may not accept."""


def ensure_goal_transition(current: str, target: str) -> None:
    """Raise unless ``current -> target`` is a legal goal status move.

    Same-status writes are idempotent and pass: event retries happen, and
    rejecting a duplicate would force every writer to read-before-write.
    """
    _ensure_known("goal status", current, GOAL_STATUSES)
    _ensure_known("goal status", target, GOAL_STATUSES)
    if current == target:
        return
    allowed = GOAL_TRANSITIONS[current]  # type: ignore[index]
    if target not in allowed:
        raise ContractError(
            f"illegal goal transition {current} -> {target}; "
            f"allowed: {sorted(allowed)}"
        )


def ensure_plan_transition(current: str, target: str) -> None:
    """Raise unless ``current -> target`` is a legal plan status move."""
    _ensure_known("plan status", current, PLAN_STATUSES)
    _ensure_known("plan status", target, PLAN_STATUSES)
    if current == target:
        return
    allowed = PLAN_TRANSITIONS[current]  # type: ignore[index]
    if target not in allowed:
        raise ContractError(
            f"illegal plan transition {current} -> {target}; "
            f"allowed: {sorted(allowed)}"
        )


def _ensure_known(kind: str, value: str, known: Iterable[str]) -> None:
    if value not in known:
        raise ContractError(f"unknown {kind} {value!r}; known: {sorted(known)}")


def ensure_event_type(event_type: str) -> None:
    """Raise for an event type outside the append-only vocabulary."""
    if event_type not in EVENT_TYPE_SET:
        raise ContractError(f"unknown event type {event_type!r}")


@dataclass(frozen=True, slots=True)
class GoalSpec:
    """What a goal means: an objective plus how anyone can tell it is done.

    ``stop_conditions`` are natural-language conditions the verifier evaluates
    against evidence; they are required because a goal without them has no
    completion test and would fall back to trusting the agent's last message.
    """

    title: str
    objective: str
    stop_conditions: tuple[str, ...]
    budget: Mapping[str, Any] = field(default_factory=dict)
    verifier_config: Mapping[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "objective": self.objective,
            "stop_conditions": list(self.stop_conditions),
            "budget": dict(self.budget),
            "verifier_config": dict(self.verifier_config),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> GoalSpec:
        title = str(value.get("title") or "").strip()
        objective = str(value.get("objective") or "").strip()
        stop_conditions = tuple(value.get("stop_conditions") or ())
        if not title:
            raise ContractError("goal title must not be empty")
        if not objective:
            raise ContractError("goal objective must not be empty")
        if not stop_conditions:
            raise ContractError("goal needs at least one stop condition")
        for condition in stop_conditions:
            if not isinstance(condition, str) or not condition.strip():
                raise ContractError("stop conditions must be non-empty strings")
        budget = value.get("budget")
        verifier_config = value.get("verifier_config")
        budget = {} if budget is None else budget
        verifier_config = {} if verifier_config is None else verifier_config
        if not isinstance(budget, Mapping):
            raise ContractError("goal budget must be a mapping")
        if not isinstance(verifier_config, Mapping):
            raise ContractError("goal verifier_config must be a mapping")
        return cls(
            title=title,
            objective=objective,
            stop_conditions=stop_conditions,
            budget=dict(budget),
            verifier_config=dict(verifier_config),
        )


@dataclass(frozen=True, slots=True)
class ProposedMemory:
    """A durable fact a subagent offers to the parent's memory."""

    type: MemoryProposalType
    text: str
    change_kind: str = "long_term"
    replaces_entry_key: str | None = None
    reason: str | None = None
    origin: MemoryOrigin = "assistant_inference"
    confidence: float = 0.5
    evidence_refs: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in MEMORY_PROPOSAL_TYPES:
            raise ContractError(
                f"unknown memory proposal type {self.type!r}; "
                f"known: {list(MEMORY_PROPOSAL_TYPES)}"
            )
        if not self.text.strip():
            raise ContractError("memory proposal text must not be empty")
        if len(self.text) > 4000:
            raise ContractError("memory proposal text is too long (max 4000 chars)")
        if self.change_kind not in ("long_term", "correction"):
            raise ContractError("durable memory proposals must be long_term or correction")
        replacement_key = (self.replaces_entry_key or "").strip() or None
        reason = (self.reason or "").strip() or None
        if replacement_key is not None and (
            len(replacement_key) > 512
            or "/" in replacement_key
            or "\\" in replacement_key
            or replacement_key in (".", "..")
        ):
            raise ContractError("replacement key must be a bounded owner-memory filename")
        if self.change_kind == "correction" and (not replacement_key or not reason):
            raise ContractError("correction needs a replacement key and reason")
        if reason is not None and len(reason) > 1000:
            raise ContractError("memory proposal reason is too long (max 1000 chars)")
        object.__setattr__(self, "replaces_entry_key", replacement_key)
        object.__setattr__(self, "reason", reason)
        normalized = normalize_memory_metadata(
            {
                "origin": self.origin,
                "confidence": self.confidence,
                "evidence_refs": self.evidence_refs,
            }
        )
        if normalized["origin"] not in MEMORY_ORIGINS:
            raise ContractError("memory proposal has an invalid origin")
        object.__setattr__(self, "origin", normalized["origin"])
        object.__setattr__(self, "confidence", normalized["confidence"])
        object.__setattr__(self, "evidence_refs", tuple(normalized["evidence_refs"]))
        try:
            policy_metadata = normalize_memory_policy_metadata_layers(self.metadata)
            tool_policy_metadata = normalize_memory_tool_policy_metadata(self.metadata)
        except MemoryToolPolicyError as exc:
            raise ContractError(str(exc)) from exc
        object.__setattr__(
            self, "metadata", {**policy_metadata, **tool_policy_metadata}
        )

    def to_record(self) -> dict[str, Any]:
        normalized = normalize_memory_metadata(
            {
                "origin": self.origin,
                "confidence": self.confidence,
                "evidence_refs": self.evidence_refs,
            }
        )
        return {
            "type": self.type,
            "text": self.text,
            "change_kind": self.change_kind,
            "replaces_entry_key": self.replaces_entry_key,
            "reason": self.reason,
            "origin": normalized["origin"],
            "confidence": normalized["confidence"],
            "evidence_refs": normalized["evidence_refs"],
            "evidence_count": normalized["evidence_count"],
            "counts_as_evidence": normalized["counts_as_evidence"],
            "metadata": {
                **normalize_memory_policy_metadata_layers(self.metadata),
                **normalize_memory_tool_policy_metadata(self.metadata),
            },
        }


@dataclass(frozen=True, slots=True)
class SubagentResult:
    """The result a subagent run hands back to its parent.

    ``summary`` is what the parent reads first; ``findings``/``artifacts``/
    ``evidence`` are the machine-checkable body; ``proposed_memories`` are the
    only write the subagent may propose. The parent merge rejects a result whose
    status is not terminal or whose summary is empty, so a run cannot look
    successful while handing back nothing.
    """

    contract_version: int
    task_id: str
    status: str
    summary: str
    findings: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    proposed_memories: tuple[ProposedMemory, ...] = ()
    open_questions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.contract_version != SUBAGENT_CONTRACT_VERSION:
            raise ContractError(
                f"unsupported subagent contract version {self.contract_version}; "
                f"expected {SUBAGENT_CONTRACT_VERSION}"
            )
        if not self.task_id.strip():
            raise ContractError("subagent result needs a task id")
        _ensure_known(
            "subagent run status",
            self.status,
            ("completed", "interrupted", "error", "cancelled"),
        )
        if not self.summary.strip():
            raise ContractError("subagent result needs a non-empty summary")
        for name, values in (
            ("findings", self.findings),
            ("artifact_ids", self.artifact_ids),
            ("evidence", self.evidence),
            ("open_questions", self.open_questions),
        ):
            _ensure_strings(name, values)

    def to_record(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "task_id": self.task_id,
            "status": self.status,
            "summary": self.summary,
            "findings": list(self.findings),
            "artifact_ids": list(self.artifact_ids),
            "evidence": list(self.evidence),
            "proposed_memories": [m.to_record() for m in self.proposed_memories],
            "open_questions": list(self.open_questions),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SubagentResult:
        proposals = tuple(
            ProposedMemory(
                type=item.get("type"),
                text=item.get("text"),
                change_kind=item.get("change_kind", "long_term"),
                replaces_entry_key=item.get("replaces_entry_key"),
                reason=item.get("reason"),
                origin=item.get("origin", "assistant_inference"),
                confidence=item.get("confidence", 0.5),
                evidence_refs=tuple(
                    ref
                    for ref in (item.get("evidence_refs") or ())
                    if isinstance(ref, Mapping)
                ),
                metadata=item.get("metadata", {}),
            )
            for item in (value.get("proposed_memories") or ())
            if isinstance(item, Mapping)
            and item.get("change_kind", "long_term") != "once"
        )
        return cls(
            contract_version=int(value.get("contract_version") or 0),
            task_id=str(value.get("task_id") or ""),
            status=str(value.get("status") or ""),
            summary=str(value.get("summary") or ""),
            findings=_string_sequence("findings", value.get("findings")),
            artifact_ids=_string_sequence("artifact_ids", value.get("artifact_ids")),
            evidence=_string_sequence("evidence", value.get("evidence")),
            proposed_memories=proposals,
            open_questions=_string_sequence("open_questions", value.get("open_questions")),
        )


def _string_sequence(name: str, raw: Any) -> tuple[str, ...]:
    """A JSON list of non-empty strings; None/absent means empty.

    A bare string is rejected rather than iterated into characters: a result
    that serialized ``"findings": "one"`` should fail loudly, not silently
    become four one-character findings.
    """
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, Iterable):
        raise ContractError(f"{name} must be a list of strings")
    values = tuple(raw)
    _ensure_strings(name, values)
    return values


def _ensure_strings(name: str, values: Iterable[Any]) -> None:
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ContractError(f"{name} entries must be non-empty strings")
