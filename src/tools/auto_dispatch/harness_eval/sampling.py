"""Choose a fixed, replayable set of historical lines.

The input is a local export (or a ``DispatchStore`` in tests). A line without
a commit that exists in ``repo``, without a parseable ``cmd:`` acceptance, or
inside a G2 risk glob is skipped with a reason code. The same input always
yields the same selection.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from src.tools.auto_dispatch.gates import (
    DEFAULT_RISK_GLOBS,
    acceptance_commands,
    line_is_risk_bearing,
)
from src.tools.auto_dispatch.types import Dispatch, Line

from .spec import SpecError
from .types import Experiment, Sample, Skip
from .variants import resolve_commit

_FALLBACK_RISK = ("migrations/**",)


async def select_samples(
    experiment: Experiment,
    *,
    store: Any | None = None,
    risk_globs: tuple[str, ...] | None = None,
) -> tuple[tuple[Sample, ...], tuple[Skip, ...]]:
    """Return the selected samples and every line that was skipped."""
    documents = await _documents(experiment, store)
    globs = risk_globs if risk_globs is not None else DEFAULT_RISK_GLOBS
    if not globs:
        globs = _FALLBACK_RISK
    repo = experiment.repo
    if not (repo / ".git").exists():
        raise SpecError("repo", "is not a git repository")
    candidates, skips = _candidates(documents, experiment, repo, globs)
    if experiment.samples.select == "explicit":
        chosen, extra = _explicit(candidates, experiment, {item.key for item in skips})
    else:
        chosen, extra = _seeded(candidates, experiment)
    skips.extend(extra)
    samples = tuple(
        Sample(
            key=item.key,
            index=index,
            dispatch_id=item.dispatch_id,
            line_id=item.line_id,
            goal=item.goal,
            task=item.task,
            acceptance=item.acceptance,
            write_scope=item.write_scope,
            base_commit=item.base_commit,
            risk_bearing=item.risk_bearing,
            forbidden=item.forbidden,
        )
        for index, item in enumerate(chosen)
    )
    return samples, tuple(skips)


async def _documents(experiment: Experiment, store: Any | None) -> list[dict[str, Any]]:
    if store is not None:
        rows = await store.list(limit=10_000)
        documents: list[dict[str, Any]] = []
        for dispatch in rows:
            if isinstance(dispatch, Dispatch):
                raw = dispatch.to_dict()
                raw["dispatch_id"] = dispatch.id
            elif isinstance(dispatch, dict):
                raw = dict(dispatch)
            else:
                continue
            documents.append(raw)
        return documents
    path = experiment.experiment_dir / experiment.samples.source
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SpecError("samples.source", f"cannot read export ({exc})") from exc
    if not isinstance(payload, list):
        raise SpecError("samples.source", "must be a JSON array of dispatches")
    return [item for item in payload if isinstance(item, dict)]


def _candidates(
    documents: list[dict[str, Any]],
    experiment: Experiment,
    repo: Path,
    globs: tuple[str, ...],
) -> tuple[dict[str, Sample], list[Skip]]:
    found: dict[str, Sample] = {}
    skips: list[Skip] = []
    for document in documents:
        dispatch_id = str(document.get("dispatch_id") or document.get("id") or "")
        goal = str(document.get("goal") or "")
        gates = document.get("gates") if isinstance(document.get("gates"), dict) else {}
        gate_base = str(gates.get("base_commit") or "") if isinstance(gates, dict) else ""
        forbidden = _forbidden(dispatch_id, document.get("outcomes"))
        lines = document.get("lines") or []
        if not isinstance(lines, list):
            continue
        for raw_line in lines:
            if not isinstance(raw_line, dict) or "id" not in raw_line:
                key = f"{dispatch_id}:?"
                skips.append(Skip(key, "malformed"))
                continue
            try:
                line = Line.from_dict(raw_line)
            except (KeyError, TypeError, ValueError):
                skips.append(Skip(f"{dispatch_id}:{raw_line.get('id')}", "malformed"))
                continue
            key = f"{dispatch_id}:{line.id}"
            reason = _reject_line(
                line,
                gate_base,
                repo,
                experiment,
                globs,
            )
            if reason is not None:
                skips.append(Skip(key, reason))
                continue
            base = line.base_commit or gate_base
            resolved = resolve_commit(repo, base)
            if resolved is None:
                skips.append(Skip(key, "base_missing"))
                continue
            found[key] = Sample(
                key=key,
                index=0,
                dispatch_id=dispatch_id,
                line_id=line.id,
                goal=goal,
                task=line.task,
                acceptance=line.acceptance,
                write_scope=line.write_scope,
                base_commit=resolved,
                risk_bearing=line.risk_bearing,
                forbidden=forbidden,
            )
    return found, skips


def _reject_line(
    line: Line,
    gate_base: str,
    repo: Path,
    experiment: Experiment,
    globs: tuple[str, ...],
) -> str | None:
    if not line.task.strip():
        return "empty_task"
    base = line.base_commit or gate_base
    if not base:
        return "no_base_commit"
    if resolve_commit(repo, base) is None:
        return "base_missing"
    if experiment.samples.require_cmd_acceptance:
        commands = acceptance_commands(line, None)
        saw_cmd = any(
            isinstance(item, str) and item.startswith("cmd:") for item in line.acceptance
        )
        if commands is None:
            return "acceptance_unparseable"
        if not commands or not saw_cmd:
            return "no_cmd_acceptance"
    elif acceptance_commands(line, None) is None:
        return "acceptance_unparseable"
    if experiment.samples.exclude_risk_bearing and line_is_risk_bearing(line, globs):
        return "risk_bearing"
    return None


def _explicit(
    candidates: dict[str, Sample],
    experiment: Experiment,
    already: set[str],
) -> tuple[list[Sample], list[Skip]]:
    chosen: list[Sample] = []
    skips: list[Skip] = []
    seen: set[str] = set()
    for key in experiment.samples.lines:
        if key in seen:
            skips.append(Skip(key, "duplicate"))
            continue
        seen.add(key)
        sample = candidates.get(key)
        if sample is None:
            if key not in already:
                skips.append(Skip(key, "not_found"))
            continue
        if len(chosen) >= experiment.samples.max:
            skips.append(Skip(key, "over_max"))
            continue
        chosen.append(sample)
    return chosen, skips


def _seeded(
    candidates: dict[str, Sample], experiment: Experiment
) -> tuple[list[Sample], list[Skip]]:
    seed = experiment.samples.seed

    def sort_key(sample: Sample) -> str:
        material = f"{seed}:{sample.dispatch_id}:{sample.line_id}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    ordered = sorted(candidates.values(), key=sort_key)
    return ordered[: experiment.samples.max], []


def _forbidden(dispatch_id: str, outcomes: Any) -> tuple[str, ...]:
    texts = [dispatch_id] if dispatch_id else []
    if isinstance(outcomes, dict) and outcomes:
        dumped = json.dumps(outcomes, sort_keys=True, ensure_ascii=False)
        if len(dumped) >= 12:
            texts.append(dumped)
        for value in outcomes.values():
            if isinstance(value, dict):
                verdict = str(value.get("reviewer_verdict") or "")
                if verdict:
                    texts.append(verdict)
    return tuple(texts)
