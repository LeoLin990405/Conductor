"""Auto-dispatch: a goal in, parallel work lines out, one human gate before merge.

The orchestrator is written against the protocols in ``protocols`` so it can be
built and tested before impact-map, skills-hub and memory-p0 land; ``dev``
holds the deterministic doubles the design names for that window, and the
real adapters live beside them in ``planning``, ``execution``, ``selection``,
``approval`` and ``outcomes``.
"""

from __future__ import annotations

from .approval import FileApprovalGate
from .dev import (
    AlwaysPassReviewer,
    FixedPlanner,
    InMemoryDispatchStore,
    InMemoryOutcomeStore,
    NoopMerger,
    RecordingLauncher,
    RoundRobinSelector,
    ScriptedMonitor,
)
from .escalation import Escalation, build_escalation, should_escalate
from .execution import (
    GitMerger,
    LogMonitor,
    ScriptLauncher,
    CrossReviewer,
    line_evidence,
)
from .machine import (
    Collaborators,
    DispatchLoopMismatch,
    DispatchMachine,
    MachineConfig,
    progress_pct,
)
from .outcomes import COLLECTION, InMemoryMemoryPort, MemoryP0OutcomeStore, MemoryPort
from .path_ownership import OwnershipDecision, PathClaim, PathOwnership
from .planning import LLMPlanner
from .pool import (
    EXCLUDED_MODELS,
    MODEL_POOL,
    RESEARCH_ONLY_MODELS,
    REVIEW_INELIGIBLE,
    eligible_models,
)
from .protocols import (
    ApprovalDecision,
    ApprovalGate,
    DispatchError,
    DispatchStore,
    GateNotApprovedError,
    LaunchError,
    Launcher,
    MergeConflictError,
    ModelRole,
    ModelSelector,
    Monitor,
    Merger,
    OutcomeStore,
    Planner,
    Reviewer,
)
from .rulings import (
    Ruling,
    RulingCheck,
    RulingInput,
    build_ruling,
    check_confirmation,
    deliver_ruling,
    unconfirmed_blockers,
)
from .selection import HeadroomPort, QuotaAwareModelSelector, SuccessStatsPort, UnlimitedHeadroom
from .types import (
    TERMINAL_STATES,
    Dispatch,
    DispatchState,
    Line,
    LineStatus,
    Outcome,
    RunStatus,
    SplitOffOutcome,
    Verdict,
)

__all__ = [
    "COLLECTION",
    "EXCLUDED_MODELS",
    "MODEL_POOL",
    "RESEARCH_ONLY_MODELS",
    "REVIEW_INELIGIBLE",
    "TERMINAL_STATES",
    "AlwaysPassReviewer",
    "ApprovalDecision",
    "ApprovalGate",
    "Collaborators",
    "CrossReviewer",
    "Dispatch",
    "DispatchError",
    "DispatchLoopMismatch",
    "DispatchMachine",
    "DispatchState",
    "Escalation",
    "DispatchStore",
    "FileApprovalGate",
    "FixedPlanner",
    "GateNotApprovedError",
    "GitMerger",
    "HeadroomPort",
    "InMemoryDispatchStore",
    "InMemoryMemoryPort",
    "InMemoryOutcomeStore",
    "LLMPlanner",
    "LaunchError",
    "Launcher",
    "Line",
    "LineStatus",
    "LogMonitor",
    "MachineConfig",
    "MemoryP0OutcomeStore",
    "MemoryPort",
    "MergeConflictError",
    "ModelRole",
    "ModelSelector",
    "Monitor",
    "Merger",
    "NoopMerger",
    "Outcome",
    "OutcomeStore",
    "OwnershipDecision",
    "PathClaim",
    "PathOwnership",
    "Planner",
    "QuotaAwareModelSelector",
    "RecordingLauncher",
    "Reviewer",
    "RoundRobinSelector",
    "Ruling",
    "RulingCheck",
    "RulingInput",
    "RunStatus",
    "SplitOffOutcome",
    "ScriptLauncher",
    "ScriptedMonitor",
    "SuccessStatsPort",
    "UnlimitedHeadroom",
    "Verdict",
    "build_escalation",
    "build_ruling",
    "check_confirmation",
    "deliver_ruling",
    "eligible_models",
    "line_evidence",
    "progress_pct",
    "should_escalate",
    "unconfirmed_blockers",
]
