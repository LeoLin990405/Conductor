"""Load an experiment file and refuse it whole when any rule fails.

Paths other than ``repo`` and ``output_dir`` stay inside the experiment
directory. Hard caps are constants: the file can only tighten them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from src.tools.auto_dispatch.pool import READ_ONLY_CAPABLE, REVIEW_INELIGIBLE

from .types import (
    HARD_CAP_BASELINE_REPEATS,
    HARD_CAP_MAX_MODELS,
    HARD_CAP_MAX_RUNS,
    HARD_CAP_REPEATS,
    HARD_CAP_SAMPLES,
    HARD_CAP_VARIANTS,
    HARD_CAP_WALL_MINUTES,
    HARD_CAP_WORKER_TIMEOUT_SECONDS,
    LEAK_MARKERS,
    MAX_AGENTS_MD_BYTES,
    MAX_SKILL_NAMES,
    SCHEMA,
    AgentsMdSpec,
    BudgetSpec,
    Experiment,
    ReviewerSpec,
    Sample,
    SamplesSpec,
    SkillsSpec,
    VariantSpec,
)

# Directories the runner creates under output_dir. ``--purge`` deletes the
# first three. A source repo that overlaps any of them is deleted or written
# through when the eval runs.
GENERATED_SUBTREES = ("repos", "worktrees", "g2-worktrees", "runs", "launch-logs")
PURGED_SUBTREES = ("repos", "worktrees", "g2-worktrees")

_EXPERIMENT_ID = re.compile(r"[a-z0-9-]{1,48}\Z")
_SKILL_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_MODEL_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")

_TOP_FIELDS = frozenset(
    {
        "schema",
        "id",
        "repo",
        "samples",
        "variants",
        "baseline",
        "models",
        "repeats",
        "baseline_repeats",
        "reviewer",
        "budget",
        "output_dir",
    }
)
_SAMPLE_FIELDS = frozenset(
    {
        "source",
        "select",
        "lines",
        "max",
        "seed",
        "require_cmd_acceptance",
        "exclude_risk_bearing",
    }
)
_VARIANT_FIELDS = frozenset({"id", "agents_md", "skills"})
_AGENTS_FIELDS = frozenset({"mode", "file"})
_SKILLS_FIELDS = frozenset({"mode", "names"})
_REVIEWER_FIELDS = frozenset({"models", "fixed_per_sample"})
_BUDGET_FIELDS = frozenset(
    {
        "max_runs",
        "max_models",
        "max_reviews",
        "max_wall_minutes",
        "worker_timeout_seconds",
    }
)
_AGENTS_MODES = frozenset({"keep", "replace", "append", "remove"})
_SKILL_MODES = frozenset({"keep", "set", "none"})
_SELECT_MODES = frozenset({"explicit", "seeded"})


class SpecError(ValueError):
    """The experiment file is rejected. ``field`` names the offending key."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(f"{field}: {message}")


def load_experiment(path: Path, policy: Any | None = None) -> Experiment:
    """Parse ``path`` and return it, or raise ``SpecError`` naming the field."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SpecError("schema", f"experiment file is not UTF-8 JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise SpecError("schema", "experiment file must be a JSON object")
    _reject_unknown(raw, _TOP_FIELDS, "")
    schema = raw.get("schema")
    if schema != SCHEMA:
        raise SpecError("schema", f"must equal {SCHEMA}")
    experiment_id = _as_str(raw.get("id"), "id")
    if not _EXPERIMENT_ID.fullmatch(experiment_id):
        raise SpecError("id", "must match [a-z0-9-]{1,48}")
    experiment_dir = path.parent.resolve()
    repo = _location(raw.get("repo"), "repo", experiment_dir)
    output_dir = _location(raw.get("output_dir"), "output_dir", experiment_dir)
    assert_output_contained(repo, output_dir)
    samples = _samples(raw.get("samples"), experiment_dir)
    variants = _variants(raw.get("variants"), experiment_dir)
    baseline = _as_str(raw.get("baseline"), "baseline")
    matched = [item for item in variants if item.id == baseline]
    if not matched:
        raise SpecError("baseline", "must name one of variants")
    base_variant = matched[0]
    if base_variant.agents_md.mode != "keep" or base_variant.skills.mode != "keep":
        raise SpecError(
            "baseline",
            "baseline variant must keep AGENTS.md and keep skills",
        )
    variants = _inherit_baseline_skills(variants, base_variant)
    models = _names(raw.get("models"), "models", minimum=1, maximum=HARD_CAP_MAX_MODELS)
    repeats = _as_int(raw.get("repeats"), "repeats", minimum=1, maximum=HARD_CAP_REPEATS)
    baseline_repeats = _as_int(
        raw.get("baseline_repeats"),
        "baseline_repeats",
        minimum=1,
        maximum=HARD_CAP_BASELINE_REPEATS,
    )
    reviewer = _reviewer(raw.get("reviewer"), models, policy)
    budget = _budget(raw.get("budget"), model_count=len(models))
    experiment = Experiment(
        schema=SCHEMA,
        id=experiment_id,
        repo=repo,
        samples=samples,
        variants=variants,
        baseline=baseline,
        models=models,
        repeats=repeats,
        baseline_repeats=baseline_repeats,
        reviewer=reviewer,
        budget=budget,
        output_dir=output_dir,
        experiment_dir=experiment_dir,
        canonical={},
        canonical_sha256="",
    )
    canonical = canonical_document(experiment)
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()
    return Experiment(
        schema=experiment.schema,
        id=experiment.id,
        repo=experiment.repo,
        samples=experiment.samples,
        variants=experiment.variants,
        baseline=experiment.baseline,
        models=experiment.models,
        repeats=experiment.repeats,
        baseline_repeats=experiment.baseline_repeats,
        reviewer=experiment.reviewer,
        budget=experiment.budget,
        output_dir=experiment.output_dir,
        experiment_dir=experiment.experiment_dir,
        canonical=canonical,
        canonical_sha256=digest,
    )


def canonical_document(experiment: Experiment) -> dict[str, Any]:
    """Stable document of the experiment file.

    The sha of this document does not cover exported line text or the commit
    a base name resolves to. ``selected_inputs_fingerprint`` covers those, and
    a resume has to match both.
    """
    return {
        "schema": experiment.schema,
        "id": experiment.id,
        "repo": experiment.repo.as_posix(),
        "samples": {
            "source": experiment.samples.source,
            "select": experiment.samples.select,
            "lines": list(experiment.samples.lines),
            "max": experiment.samples.max,
            "seed": experiment.samples.seed,
            "require_cmd_acceptance": experiment.samples.require_cmd_acceptance,
            "exclude_risk_bearing": experiment.samples.exclude_risk_bearing,
        },
        "variants": [
            {
                "id": item.id,
                "agents_md": {
                    "mode": item.agents_md.mode,
                    "file": item.agents_md.file,
                },
                "skills": {
                    "mode": item.skills.mode,
                    "names": None if item.skills.names is None else list(item.skills.names),
                },
                "files_sha256": _variant_file_sha256(experiment, item),
            }
            for item in experiment.variants
        ],
        "baseline": experiment.baseline,
        "models": list(experiment.models),
        "repeats": experiment.repeats,
        "baseline_repeats": experiment.baseline_repeats,
        "reviewer": {
            "models": list(experiment.reviewer.models),
            "fixed_per_sample": experiment.reviewer.fixed_per_sample,
        },
        "budget": {
            "max_runs": experiment.budget.max_runs,
            "max_models": experiment.budget.max_models,
            "max_reviews": experiment.budget.max_reviews,
            "max_wall_minutes": experiment.budget.max_wall_minutes,
            "worker_timeout_seconds": experiment.budget.worker_timeout_seconds,
        },
        "output_dir": experiment.output_dir.as_posix(),
    }


def selected_inputs_fingerprint(samples: tuple[Sample, ...] | list[Sample]) -> str:
    """Hash the replay inputs and the resolved base of each selected line.

    The sample key alone is not an identity. A later export can keep
    ``dispatch:line`` and change the task, the acceptance commands, or the
    commit those commands run against. Resume must reject that plan.
    """
    body = [
        {
            "key": sample.key,
            "dispatch_id": sample.dispatch_id,
            "line_id": sample.line_id,
            "goal": sample.goal,
            "task": sample.task,
            "acceptance": list(sample.acceptance),
            "write_scope": list(sample.write_scope),
            "base_commit": sample.base_commit,
        }
        for sample in samples
    ]
    encoded = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def planned_run_count(experiment: Experiment, sample_count: int) -> int:
    """Runs implied by the selected samples. This count is never truncated."""
    per_sample = (len(experiment.variants) - 1) * experiment.repeats
    per_sample += experiment.baseline_repeats
    return sample_count * per_sample * len(experiment.models)


def ensure_within_budget(experiment: Experiment, sample_count: int) -> int:
    """Reject when the matrix is larger than ``max_runs``. Returns the count."""
    count = planned_run_count(experiment, sample_count)
    if count > experiment.budget.max_runs:
        raise SpecError(
            "budget.max_runs",
            f"planned runs {count} exceed max_runs {experiment.budget.max_runs}",
        )
    return count


def find_marker(text: str, *extra: str) -> str | None:
    """Return the first scoring marker in ``text``, if any."""
    for marker in (*LEAK_MARKERS, *extra):
        if marker and marker in text:
            return marker
    return None


def _variant_file_sha256(experiment: Experiment, variant: VariantSpec) -> str:
    """Include variant bytes in the rerun fingerprint, not only the JSON path."""
    if variant.agents_md.file:
        data = (experiment.experiment_dir / variant.agents_md.file).read_bytes()
    else:
        data = b""
    return hashlib.sha256(data).hexdigest()


def _samples(raw: Any, experiment_dir: Path) -> SamplesSpec:
    if not isinstance(raw, dict):
        raise SpecError("samples", "must be an object")
    _reject_unknown(raw, _SAMPLE_FIELDS, "samples")
    source = _as_str(raw.get("source"), "samples.source")
    source_path = _inside(experiment_dir, source, "samples.source")
    if not source_path.is_file():
        raise SpecError("samples.source", "file does not exist")
    select = _as_str(raw.get("select"), "samples.select")
    if select not in _SELECT_MODES:
        raise SpecError("samples.select", "must be explicit or seeded")
    maximum = _as_int(
        raw.get("max"), "samples.max", minimum=1, maximum=HARD_CAP_SAMPLES
    )
    seed = 0
    if "seed" in raw:
        seed = _as_int(raw.get("seed"), "samples.seed", minimum=0, maximum=2**31 - 1)
    require_cmd = True
    if "require_cmd_acceptance" in raw:
        require_cmd = _as_bool(raw.get("require_cmd_acceptance"), "samples.require_cmd_acceptance")
    exclude_risk = True
    if "exclude_risk_bearing" in raw:
        exclude_risk = _as_bool(raw.get("exclude_risk_bearing"), "samples.exclude_risk_bearing")
    lines: tuple[str, ...] = ()
    if "lines" in raw:
        lines = _string_tuple(raw.get("lines"), "samples.lines")
    if select == "explicit" and not lines:
        raise SpecError("samples.lines", "explicit select needs at least one line")
    for item in lines:
        if ":" not in item:
            raise SpecError("samples.lines", f"{item!r} must be dispatch_id:line_id")
    relative = source_path.relative_to(experiment_dir).as_posix()
    return SamplesSpec(
        source=relative,
        select=select,
        max=maximum,
        lines=lines,
        seed=seed,
        require_cmd_acceptance=require_cmd,
        exclude_risk_bearing=exclude_risk,
    )


def _variants(raw: Any, experiment_dir: Path) -> tuple[VariantSpec, ...]:
    if not isinstance(raw, list) or not raw:
        raise SpecError("variants", "must be a non-empty list")
    if len(raw) > HARD_CAP_VARIANTS:
        raise SpecError("variants", f"must contain at most {HARD_CAP_VARIANTS} variants")
    variants: list[VariantSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        field = f"variants[{index}]"
        if not isinstance(item, dict):
            raise SpecError(field, "must be an object")
        _reject_unknown(item, _VARIANT_FIELDS, field)
        variant_id = _as_str(item.get("id"), f"{field}.id")
        if not _EXPERIMENT_ID.fullmatch(variant_id):
            raise SpecError(f"{field}.id", "must match [a-z0-9-]{1,48}")
        if variant_id in seen:
            raise SpecError(f"{field}.id", "duplicate variant id")
        seen.add(variant_id)
        variants.append(
            VariantSpec(
                id=variant_id,
                agents_md=_agents(item.get("agents_md"), experiment_dir, f"{field}.agents_md"),
                skills=_skills(item.get("skills"), f"{field}.skills"),
            )
        )
    return tuple(variants)


def _inherit_baseline_skills(
    variants: tuple[VariantSpec, ...], baseline: VariantSpec
) -> tuple[VariantSpec, ...]:
    """Give every ``keep`` variant the baseline's declared skills list.

    ``keep`` means the original dispatch's value, and the operator states it
    once, on the baseline. A ``keep`` variant that left ``names`` unset uses
    that list, so the skills dimension does not differ by accident when only
    AGENTS.md is under test. A ``keep`` variant with a different list is a
    ``set`` in disguise and is rejected.
    """
    declared = baseline.skills.names
    resolved: list[VariantSpec] = []
    for index, item in enumerate(variants):
        if item.skills.mode != "keep" or item.id == baseline.id:
            resolved.append(item)
            continue
        if item.skills.names is None:
            resolved.append(
                VariantSpec(
                    id=item.id,
                    agents_md=item.agents_md,
                    skills=SkillsSpec(mode="keep", names=declared),
                )
            )
            continue
        if item.skills.names != declared:
            raise SpecError(
                f"variants[{index}].skills.names",
                "keep must match the baseline's skills list; use set for a different list",
            )
        resolved.append(item)
    return tuple(resolved)


def _agents(raw: Any, experiment_dir: Path, field: str) -> AgentsMdSpec:
    if not isinstance(raw, dict):
        raise SpecError(field, "must be an object")
    _reject_unknown(raw, _AGENTS_FIELDS, field)
    mode = _as_str(raw.get("mode"), f"{field}.mode")
    if mode not in _AGENTS_MODES:
        raise SpecError(f"{field}.mode", "must be keep, replace, append, or remove")
    file_name: str | None = None
    if "file" in raw and raw.get("file") is not None:
        file_name = _as_str(raw.get("file"), f"{field}.file")
    if mode in {"replace", "append"}:
        if not file_name:
            raise SpecError(f"{field}.file", "required when mode is replace or append")
        path = _inside(experiment_dir, file_name, f"{field}.file")
        if not path.is_file():
            raise SpecError(f"{field}.file", "file does not exist")
        data = path.read_bytes()
        if len(data) > MAX_AGENTS_MD_BYTES:
            raise SpecError(f"{field}.file", "file is larger than 32 KiB")
        try:
            text = data.decode("utf-8")
        except UnicodeError as exc:
            raise SpecError(f"{field}.file", "file must be UTF-8") from exc
        marker = find_marker(text)
        if marker is not None:
            raise SpecError(f"{field}.file", f"contains scoring marker {marker!r}")
        file_name = path.relative_to(experiment_dir).as_posix()
    elif file_name is not None:
        raise SpecError(f"{field}.file", "only replace and append take a file")
    return AgentsMdSpec(mode=mode, file=file_name)


def _skills(raw: Any, field: str) -> SkillsSpec:
    if not isinstance(raw, dict):
        raise SpecError(field, "must be an object")
    _reject_unknown(raw, _SKILLS_FIELDS, field)
    mode = _as_str(raw.get("mode"), f"{field}.mode")
    if mode not in _SKILL_MODES:
        raise SpecError(f"{field}.mode", "must be keep, set, or none")
    if "names" not in raw or raw.get("names") is None:
        if mode == "set":
            raise SpecError(f"{field}.names", "required when mode is set")
        if mode == "none" and "names" in raw:
            raise SpecError(f"{field}.names", "none does not take names")
        return SkillsSpec(mode=mode, names=None)
    if mode == "none":
        raise SpecError(f"{field}.names", "none does not take names")
    names = _string_tuple(raw.get("names"), f"{field}.names")
    if len(names) > MAX_SKILL_NAMES:
        raise SpecError(f"{field}.names", f"at most {MAX_SKILL_NAMES} names")
    for name in names:
        if not _SKILL_NAME.fullmatch(name):
            raise SpecError(f"{field}.names", f"{name!r} must match [A-Za-z0-9._-]{{1,64}}")
        marker = find_marker(name)
        if marker is not None:
            raise SpecError(f"{field}.names", f"{name!r} contains scoring marker {marker!r}")
    return SkillsSpec(mode=mode, names=names)


def _reviewer(raw: Any, authors: tuple[str, ...], policy: Any | None) -> ReviewerSpec:
    if not isinstance(raw, dict):
        raise SpecError("reviewer", "must be an object")
    _reject_unknown(raw, _REVIEWER_FIELDS, "reviewer")
    models = _names(raw.get("models"), "reviewer.models", minimum=1, maximum=8)
    author_keys = {item.lower() for item in authors}
    allowed = _allowed_reviewers(policy)
    for name in models:
        if name.lower() in author_keys:
            raise SpecError(
                "reviewer.models",
                f"author model {name} cannot review its own runs",
            )
        if allowed is None:
            if name.lower() in REVIEW_INELIGIBLE:
                raise SpecError(
                    "reviewer.models",
                    f"{name} is not eligible to review",
                )
            continue
        if name.lower() not in allowed:
            raise SpecError(
                "reviewer.models",
                f"{name} is not in the read-only review roster",
            )
    fixed = True
    if "fixed_per_sample" in raw:
        fixed = _as_bool(raw.get("fixed_per_sample"), "reviewer.fixed_per_sample")
    return ReviewerSpec(models=models, fixed_per_sample=fixed)


def _allowed_reviewers(policy: Any | None) -> set[str] | None:
    """Review names from a RolePolicy-shaped object, without importing it.

    ``None`` means no policy was passed, so only the N2 denylist applies.
    """
    if policy is None:
        return None
    primary = tuple(getattr(policy, "primary_reviewer", ()) or ())
    secondary = tuple(getattr(policy, "secondary_reviewer_pool", ()) or ())
    allowed: set[str] = set()
    for name in (*primary, *secondary):
        canon = str(name).strip().lower()
        if not canon or canon in REVIEW_INELIGIBLE:
            continue
        if canon not in READ_ONLY_CAPABLE:
            continue
        allowed.add(canon)
    return allowed


def _budget(raw: Any, *, model_count: int) -> BudgetSpec:
    if not isinstance(raw, dict):
        raise SpecError("budget", "must be an object")
    _reject_unknown(raw, _BUDGET_FIELDS, "budget")
    max_runs = _as_int(
        raw.get("max_runs"), "budget.max_runs", minimum=1, maximum=HARD_CAP_MAX_RUNS
    )
    max_models = _as_int(
        raw.get("max_models"),
        "budget.max_models",
        minimum=1,
        maximum=HARD_CAP_MAX_MODELS,
    )
    if model_count > max_models:
        raise SpecError(
            "models",
            f"{model_count} models exceed budget.max_models {max_models}",
        )
    max_reviews = _as_int(
        raw.get("max_reviews"), "budget.max_reviews", minimum=0, maximum=10_000
    )
    max_wall = _as_int(
        raw.get("max_wall_minutes"),
        "budget.max_wall_minutes",
        minimum=1,
        maximum=HARD_CAP_WALL_MINUTES,
    )
    timeout = _as_int(
        raw.get("worker_timeout_seconds"),
        "budget.worker_timeout_seconds",
        minimum=1,
        maximum=HARD_CAP_WORKER_TIMEOUT_SECONDS,
    )
    return BudgetSpec(
        max_runs=max_runs,
        max_models=max_models,
        max_reviews=max_reviews,
        max_wall_minutes=max_wall,
        worker_timeout_seconds=timeout,
    )


def _names(raw: Any, field: str, *, minimum: int, maximum: int) -> tuple[str, ...]:
    values = _string_tuple(raw, field)
    if len(values) < minimum or len(values) > maximum:
        raise SpecError(field, f"must contain {minimum} to {maximum} names")
    seen: set[str] = set()
    for name in values:
        if not _MODEL_NAME.fullmatch(name):
            raise SpecError(field, f"{name!r} must match [A-Za-z0-9._-]{{1,64}}")
        key = name.lower()
        if key in seen:
            raise SpecError(field, f"duplicate model {name}")
        seen.add(key)
    return values


def _string_tuple(raw: Any, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise SpecError(field, "must be a list of strings")
    return tuple(raw)


def _location(raw: Any, field: str, experiment_dir: Path) -> Path:
    text = _as_str(raw, field)
    expanded = Path(text).expanduser()
    if not expanded.is_absolute():
        expanded = experiment_dir / expanded
    return expanded.resolve()


def _inside(experiment_dir: Path, raw: str, field: str) -> Path:
    if raw.startswith("~") or Path(raw).is_absolute():
        raise SpecError(field, "must be a relative path inside the experiment directory")
    path = (experiment_dir / raw).resolve()
    try:
        path.relative_to(experiment_dir)
    except ValueError as exc:
        raise SpecError(field, "path escapes the experiment directory") from exc
    return path


def assert_output_contained(repo: Path, output_dir: Path) -> None:
    """Reject an output directory that can delete the source or live dispatch state.

    ``output_dir`` must sit outside ``repo``. ``repo`` must also sit outside
    every subtree the eval creates or ``--purge`` removes: otherwise
    ``repo=/tmp/eval/repos`` with ``output_dir=/tmp/eval`` validates, and
    purge deletes the source. The same check refuses the live runs directory,
    its ``dispatch-worktrees`` sibling, and the launch-log directory.
    """
    repo_resolved = repo.resolve()
    output_resolved = output_dir.resolve()
    if output_resolved == repo_resolved or _is_inside(output_resolved, repo_resolved):
        raise SpecError("output_dir", "must not be inside repo")
    for name in GENERATED_SUBTREES:
        generated = (output_resolved / name).resolve()
        if _overlaps(repo_resolved, generated):
            raise SpecError(
                "output_dir",
                f"repo overlaps {name}, which the eval creates and purge may delete",
            )
    for protected in live_dispatch_directories():
        if _overlaps(output_resolved, protected):
            raise SpecError(
                "output_dir",
                "must not overlap a live dispatch directory",
            )


def assert_purge_target(repo: Path, output_dir: Path, target: Path) -> None:
    """Raise before a purge deletes ``target`` when the path is not contained.

    Call this on the real directory or symlink that is about to be removed.
    A link from ``output_dir/repos`` to the source repository resolves onto
    the source and is refused with the tree still in place.
    """
    assert_output_contained(repo, output_dir)
    if not target.exists() and not target.is_symlink():
        return
    resolved = target.resolve()
    root = output_dir.resolve()
    repo_resolved = repo.resolve()
    name = target.name
    if _overlaps(resolved, repo_resolved) or resolved == root or not _is_inside(resolved, root):
        raise SpecError("output_dir", f"{name} is not a contained eval subtree")


def live_dispatch_directories() -> tuple[Path, ...]:
    """Runs, worktrees, and launch logs that belong to a live dispatch."""
    home = Path.home()
    conductor = Path(os.environ.get("CONDUCTOR_HOME") or (home / ".conductor")).expanduser()
    roots = [
        home / ".conductor" / "dispatch" / "runs",
        home / ".conductor" / "dispatch" / "dispatch-worktrees",
        conductor / "dispatch" / "runs",
        conductor / "dispatch" / "dispatch-worktrees",
        _state_home() / "conductor" / "dispatch-launch",
    ]
    runs = os.environ.get("CONDUCTOR_DISPATCH_RUNS_DIR")
    if runs:
        runs_path = Path(runs).expanduser()
        roots.append(runs_path)
        roots.append(runs_path.parent / "dispatch-worktrees")
    for key in ("CONDUCTOR_DISPATCH_PROPOSED_DIR", "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR"):
        raw = os.environ.get(key)
        if raw:
            roots.append(Path(raw).expanduser())
    unique: list[Path] = []
    seen: set[Path] = set()
    home_resolved = home.resolve()
    for path in roots:
        resolved = _safe_resolve(path)
        if resolved is None or resolved in seen:
            continue
        if resolved == home_resolved or resolved == Path(resolved.anchor):
            continue
        seen.add(resolved)
        unique.append(resolved)
    return tuple(unique)


def _state_home() -> Path:
    raw = os.environ.get("XDG_STATE_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".local" / "state"


def _safe_resolve(path: Path) -> Path | None:
    try:
        return path.expanduser().resolve()
    except OSError:
        return None


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or _is_inside(left, right) or _is_inside(right, left)


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _reject_unknown(raw: dict[str, Any], allowed: frozenset[str], prefix: str) -> None:
    extra = sorted(set(raw) - allowed)
    if not extra:
        return
    name = extra[0]
    field = f"{prefix}.{name}" if prefix else name
    raise SpecError(field, "unknown field")


def _as_str(raw: Any, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise SpecError(field, "must be a non-empty string")
    return raw.strip()


def _as_bool(raw: Any, field: str) -> bool:
    if not isinstance(raw, bool):
        raise SpecError(field, "must be a boolean")
    return raw


def _as_int(raw: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise SpecError(field, "must be an integer")
    if raw < minimum or raw > maximum:
        raise SpecError(field, f"must be between {minimum} and {maximum}")
    return raw
