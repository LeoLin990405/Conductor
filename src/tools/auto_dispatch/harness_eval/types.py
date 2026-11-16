"""Shared shapes for one offline harness evaluation.

The modules behind these records are pure file and git work. Nothing here is
written to the outcome store or to live dispatch config.

Public functions live in the sibling modules:

- ``spec.load_experiment(path, policy=None) -> Experiment``
- ``spec.ensure_within_budget(experiment, sample_count) -> int``
- ``sampling.select_samples(experiment, store=None) -> tuple[Sample, ...], tuple[Skip, ...]``
- ``variants.prepare_eval_repo / make_harness_commit / assert_no_leak``
- ``variants.blinded_line / blinded_dispatch / skills_value``
- ``budget.Ledger.reserve / Ledger.settle``
- ``scoring.score_run / scoring.build_report``
- ``report.write_plan / report.write_reports / report.load_records``
- ``runner.execute / runner.regenerate``
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

SCHEMA = "conductor.harness_eval/v1"
SOURCE_TAG = "offline-harness-eval"

# Hard caps. An experiment file cannot raise these.
HARD_CAP_MAX_RUNS = 24
HARD_CAP_MAX_MODELS = 2
HARD_CAP_SAMPLES = 6
HARD_CAP_VARIANTS = 4
HARD_CAP_REPEATS = 2
HARD_CAP_BASELINE_REPEATS = 3
HARD_CAP_WORKER_TIMEOUT_SECONDS = 3600
HARD_CAP_WALL_MINUTES = 480
MAX_AGENTS_MD_BYTES = 32 * 1024
MAX_SKILL_NAMES = 16

# Backlog N2: these CLIs have no read-only review mode.
NEVER_REVIEW_MODELS = frozenset({"kimi", "deepseek", "glm", "minimax"})

# G2 and review each contribute half of a full pass. A G2 failure is 0.
G2_WEIGHT = 0.5
REVIEW_WEIGHT = 0.5
PROPOSAL_MIN_SAMPLES = 2
PROPOSAL_MAX_LOSS = 0.5

EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_BUDGET = 3
EXIT_LEAK = 4
# Every planned run failed setup: the report is written but measured nothing.
EXIT_SETUP = 5

HARNESS_AUTHOR_NAME = "conductor-harness-eval"
HARNESS_AUTHOR_EMAIL = "noreply@invalid"

LEAK_MARKERS = (
    "reviewer_verdict",
    '"passed":',
    "blockers",
    "g2",
    "G2",
    "pass_rate",
)


class WorkerStatus(StrEnum):
    COMMITTED = "committed"
    NO_NEW_COMMIT = "no_new_commit"
    UNCOMMITTED_CHANGES = "uncommitted_changes"
    WORKER_FAILED = "worker_failed"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    BUDGET_SKIPPED = "budget_skipped"
    LEAK_CHECK_FAILED = "leak_check_failed"
    ENV_ERROR = "env_error"
    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTH_FAILED = "auth_failed"
    CONFIRMATION_REQUIRED = "confirmation_required"
    NETWORK = "network"
    OVERLOADED = "overloaded"
    SETUP_FAILED = "setup_failed"


class G2Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    ENV_ERROR = "env_error"
    TIMEOUT = "timeout"
    NOT_RUN = "not_run"


class ReviewStatus(StrEnum):
    PASSED = "passed"
    BLOCKED = "blocked"
    UNPARSEABLE = "review_unparseable"
    NOT_RUN = "not_run"


UNSCORED_REASONS = frozenset(
    {
        "review_unparseable",
        "rate_limited",
        "env_error",
        "timeout",
        "budget_skipped",
        "leak_check_failed",
    }
)


@dataclass(frozen=True)
class AgentsMdSpec:
    """How one variant treats the repository ``AGENTS.md``."""

    mode: str
    file: str | None = None


@dataclass(frozen=True)
class SkillsSpec:
    """How one variant fills ``--skills``.

    ``names is None`` on ``keep`` means the experiment file left the list
    unset. The run then uses an empty list and is marked ``skills_unknown``.
    An explicit list, including an empty one, is the operator's baseline.
    """

    mode: str
    names: tuple[str, ...] | None = None


@dataclass(frozen=True)
class VariantSpec:
    id: str
    agents_md: AgentsMdSpec
    skills: SkillsSpec


@dataclass(frozen=True)
class SamplesSpec:
    source: str
    select: str
    max: int
    lines: tuple[str, ...] = ()
    seed: int = 0
    require_cmd_acceptance: bool = True
    exclude_risk_bearing: bool = True


@dataclass(frozen=True)
class ReviewerSpec:
    models: tuple[str, ...]
    fixed_per_sample: bool = True


@dataclass(frozen=True)
class BudgetSpec:
    max_runs: int
    max_models: int
    max_reviews: int
    max_wall_minutes: int
    worker_timeout_seconds: int


@dataclass(frozen=True)
class Experiment:
    """One validated experiment file.

    ``canonical`` is the normalised document whose sha256 decides whether a
    second invocation may reuse ``output_dir``.
    """

    schema: str
    id: str
    repo: Path
    samples: SamplesSpec
    variants: tuple[VariantSpec, ...]
    baseline: str
    models: tuple[str, ...]
    repeats: int
    baseline_repeats: int
    reviewer: ReviewerSpec
    budget: BudgetSpec
    output_dir: Path
    experiment_dir: Path
    canonical: dict[str, Any]
    canonical_sha256: str

    def variant(self, variant_id: str) -> VariantSpec:
        for item in self.variants:
            if item.id == variant_id:
                return item
        raise KeyError(variant_id)

    @property
    def skills_unknown(self) -> bool:
        return any(
            item.skills.mode == "keep" and item.skills.names is None
            for item in self.variants
        )


@dataclass(frozen=True)
class Skip:
    key: str
    reason: str


@dataclass(frozen=True)
class Sample:
    """One selected historical line, plus text that must not reach a worker."""

    key: str
    index: int
    dispatch_id: str
    line_id: str
    goal: str
    task: str
    acceptance: tuple[str, ...]
    write_scope: tuple[str, ...]
    base_commit: str
    risk_bearing: bool = False
    forbidden: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        """Plan-file form. ``forbidden`` stays in memory only."""
        return {
            "key": self.key,
            "index": self.index,
            "dispatch_id": self.dispatch_id,
            "line_id": self.line_id,
            "goal": self.goal,
            "task": self.task,
            "acceptance": list(self.acceptance),
            "write_scope": list(self.write_scope),
            "base_commit": self.base_commit,
            "risk_bearing": self.risk_bearing,
        }


@dataclass(frozen=True)
class PlannedRun:
    run8: str
    sample_key: str
    sample_index: int
    variant_id: str
    model: str
    repeat: int
    reviewer_model: str
    is_baseline: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CommandOutcome:
    index: int
    exit_code: int
    duration_ms: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommandOutcome:
        return cls(
            index=int(data["index"]),
            exit_code=int(data["exit_code"]),
            duration_ms=int(data["duration_ms"]),
        )


@dataclass
class RunRecord:
    """One attempted replay. Blocker text and worker output are not stored."""

    run8: str
    sample_key: str
    sample_index: int
    variant_id: str
    model: str
    repeat: int
    reviewer_model: str
    worker_status: str
    worker_exit_code: int | None = None
    g2_status: str = G2Status.NOT_RUN.value
    g2_commands: tuple[CommandOutcome, ...] = ()
    review_status: str = ReviewStatus.NOT_RUN.value
    review_passed: bool | None = None
    blocker_count: int = 0
    blocker_sha256: str = ""
    tokens: int = 0
    duration_secs: float = 0.0
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    out_of_scope_files: int = 0
    harness_commit: str = ""
    variant_files_sha256: str = ""
    skills_unknown: bool = False
    score: float | None = None
    unscored_reason: str | None = None
    preexisting_failure: bool = False
    base_g2_status: str = G2Status.NOT_RUN.value
    completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["g2_commands"] = [item.to_dict() for item in self.g2_commands]
        return raw

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        commands = tuple(
            CommandOutcome.from_dict(item) for item in data.get("g2_commands") or ()
        )
        payload = dict(data)
        payload["g2_commands"] = commands
        known = {item.name for item in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in payload.items() if key in known})


@dataclass
class BudgetState:
    """In-memory view of the ledger. Unsettled reserves count as spent."""

    runs_reserved: int
    reviews_reserved: int
    models: frozenset[str]
    wall_started: float | None
    max_runs: int
    max_reviews: int
    max_models: int
    max_wall_minutes: int


@dataclass(frozen=True)
class Reserve:
    ok: bool
    reason: str | None = None


@dataclass(frozen=True)
class HarnessCommit:
    sample_key: str
    variant_id: str
    commit: str
    variant_files_sha256: str
    skills_hub: str
    skills_unknown: bool
    ref: str


@dataclass(frozen=True)
class VariantSummary:
    variant_id: str
    n_scored: int
    n_unscored: int
    mean_score: float | None
    both_passed_rate: float | None
    mean_tokens: float | None
    mean_duration_secs: float | None
    out_of_scope_runs: int


@dataclass(frozen=True)
class PairResult:
    variant_id: str
    sample_key: str
    model: str
    repeat: int
    baseline_score: float
    variant_score: float
    diff: float
    result: str
    variant_run8: str
    baseline_run8: str


@dataclass(frozen=True)
class NoiseFloor:
    """How far baseline repeats drift from each other.

    ``mean_absolute_diff`` is the floor a proposal has to clear.
    """

    disagreement_rate: float
    mean_absolute_diff: float
    pairs: int


@dataclass(frozen=True)
class Proposal:
    variant_id: str
    status: str
    wording: str
    samples: tuple[str, ...]
    run8s: tuple[str, ...]
    mean_paired_diff: float | None
    reason: str


@dataclass(frozen=True)
class ScoreCell:
    """Every scored repeat for one sample and one variant.

    The matrix is built from these cells, including baseline repeats that
    have no pair and variant runs whose baseline repeat was not scored.
    ``baseline`` on the report names the baseline column; it is not inferred
    from which variants happen to have pairs.
    """

    sample_key: str
    variant_id: str
    scores: tuple[float, ...]


@dataclass(frozen=True)
class EvalReport:
    source: str
    proposal_only: bool
    incomplete: bool
    variants: tuple[VariantSummary, ...]
    pairs: tuple[PairResult, ...]
    noise: NoiseFloor
    proposals: tuple[Proposal, ...]
    observations: tuple[Proposal, ...]
    unscored: tuple[tuple[str, str], ...]
    skills_unknown: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)
    baseline: str = ""
    cells: tuple[ScoreCell, ...] = ()
