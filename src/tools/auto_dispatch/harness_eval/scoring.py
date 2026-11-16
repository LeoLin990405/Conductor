"""Relative scores. There is no ground truth.

G2 and review each account for half of a pass. A G2 failure, or a worker that
produced no commit, scores 0. Infrastructure failures stay out of the average.
A variant is a proposal only when at least two samples clear the baseline
noise floor, none loses by more than half a point, and the mean paired
difference also clears that floor.
"""

from __future__ import annotations

from collections import defaultdict

from .types import (
    PROPOSAL_MAX_LOSS,
    PROPOSAL_MIN_SAMPLES,
    SOURCE_TAG,
    EvalReport,
    G2Status,
    NoiseFloor,
    PairResult,
    Proposal,
    ReviewStatus,
    RunRecord,
    ScoreCell,
    VariantSummary,
    WorkerStatus,
)

_ZERO_WORK = frozenset(
    {
        WorkerStatus.NO_NEW_COMMIT.value,
        WorkerStatus.UNCOMMITTED_CHANGES.value,
        WorkerStatus.WORKER_FAILED.value,
    }
)
_INFRA_WORKER = {
    WorkerStatus.TIMEOUT.value: "timeout",
    WorkerStatus.RATE_LIMITED.value: "rate_limited",
    WorkerStatus.BUDGET_SKIPPED.value: "budget_skipped",
    WorkerStatus.LEAK_CHECK_FAILED.value: "leak_check_failed",
    WorkerStatus.ENV_ERROR.value: "env_error",
    WorkerStatus.QUOTA_EXHAUSTED.value: "quota_exhausted",
    WorkerStatus.AUTH_FAILED.value: "auth_failed",
    WorkerStatus.CONFIRMATION_REQUIRED.value: "confirmation_required",
    WorkerStatus.NETWORK.value: "network",
    WorkerStatus.OVERLOADED.value: "overloaded",
    WorkerStatus.SETUP_FAILED.value: "setup_failed",
}


def score_run(
    *,
    worker_status: str,
    g2_status: str,
    review_status: str,
    preexisting_failure: bool,
    base_g2_status: str = G2Status.NOT_RUN.value,
) -> tuple[float | None, str | None]:
    """Return ``(score, unscored_reason)``. ``score`` is None when unscored.

    A base G2 ``env_error`` means the focused tests could not start, so a
    committed worker is left unscored instead of being credited for G2.
    """
    if worker_status in _INFRA_WORKER:
        return None, _INFRA_WORKER[worker_status]
    # A focused-test timeout is infrastructure. Exit 124 must not become a
    # scored G2 failure of 0.
    if g2_status == G2Status.TIMEOUT.value:
        return None, "timeout"
    if review_status == ReviewStatus.UNPARSEABLE.value:
        return None, "review_unparseable"
    if (
        base_g2_status == G2Status.ENV_ERROR.value
        and worker_status == WorkerStatus.COMMITTED.value
    ):
        return None, "env_error"
    if g2_status == G2Status.ENV_ERROR.value and not preexisting_failure:
        return None, "env_error"
    if worker_status in _ZERO_WORK:
        return 0.0, None
    if preexisting_failure:
        if review_status == ReviewStatus.PASSED.value:
            return 0.5, None
        if review_status == ReviewStatus.BLOCKED.value:
            return 0.0, None
        return None, "review_unparseable"
    if g2_status == G2Status.FAIL.value:
        return 0.0, None
    if g2_status == G2Status.PASS.value and review_status == ReviewStatus.PASSED.value:
        return 1.0, None
    if g2_status == G2Status.PASS.value and review_status == ReviewStatus.BLOCKED.value:
        return 0.5, None
    if g2_status == G2Status.SKIPPED.value and review_status == ReviewStatus.PASSED.value:
        return 0.5, None
    if g2_status == G2Status.SKIPPED.value and review_status == ReviewStatus.BLOCKED.value:
        return 0.0, None
    if g2_status == G2Status.ENV_ERROR.value:
        return None, "env_error"
    return None, "review_unparseable"


def build_report(
    records: list[RunRecord],
    *,
    baseline: str,
    variant_ids: tuple[str, ...],
    incomplete: bool,
    skills_unknown: bool,
) -> EvalReport:
    """Summaries, pairs, the noise floor, and proposal versus observation."""
    noise = noise_floor(records, baseline)
    pairs = paired(records, baseline)
    summaries = tuple(
        _summary(records, variant_id) for variant_id in variant_ids
    )
    proposals, observations = _classify(pairs, noise, records, baseline, variant_ids)
    unscored = tuple(
        (item.run8, item.unscored_reason or "unscored")
        for item in records
        if item.score is None
    )
    warnings: list[str] = []
    if any(item.out_of_scope_files > 0 for item in records):
        warnings.append(
            "有运行改了 write_scope 之外的文件。out_of_scope_files 单独列出，不因此扣分。"
        )
    if skills_unknown:
        warnings.append(
            "skills_unknown：实验文件没有写明 keep 变体的 skills 列表，本次按空列表重放。"
        )
    return EvalReport(
        source=SOURCE_TAG,
        proposal_only=True,
        incomplete=incomplete,
        variants=summaries,
        pairs=tuple(pairs),
        noise=noise,
        proposals=tuple(proposals),
        observations=tuple(observations),
        unscored=unscored,
        skills_unknown=skills_unknown,
        warnings=tuple(warnings),
        baseline=baseline,
        cells=score_cells(records),
    )


def score_cells(records: list[RunRecord]) -> tuple[ScoreCell, ...]:
    """One cell per sample and variant that has a run record.

    Scored repeats are all kept, including baseline repeats with no paired
    variant and variant runs whose baseline repeat was not scored. Unscored
    runs still occupy a cell so the matrix can show a dash instead of
    dropping the row.
    """
    scores: dict[tuple[str, str], list[float]] = {}
    order: list[tuple[str, str]] = []
    for item in records:
        key = (item.sample_key, item.variant_id)
        if key not in scores:
            scores[key] = []
            order.append(key)
        if item.score is not None:
            scores[key].append(float(item.score))
    return tuple(
        ScoreCell(sample_key=sample, variant_id=variant, scores=tuple(scores[(sample, variant)]))
        for sample, variant in order
    )


def noise_floor(records: list[RunRecord], baseline: str) -> NoiseFloor:
    """Disagreement and mean absolute gap between baseline repeats."""
    grouped: dict[tuple[str, str], list[RunRecord]] = defaultdict(list)
    for item in records:
        if item.variant_id != baseline or item.score is None:
            continue
        grouped[(item.sample_key, item.model)].append(item)
    gaps: list[float] = []
    disagree = 0
    for bucket in grouped.values():
        ordered = sorted(bucket, key=lambda record: record.repeat)
        scores = [float(item.score) for item in ordered if item.score is not None]
        for left in range(len(scores)):
            for right in range(left + 1, len(scores)):
                gap = abs(scores[left] - scores[right])
                gaps.append(gap)
                if gap > 0:
                    disagree += 1
    if not gaps:
        return NoiseFloor(disagreement_rate=0.0, mean_absolute_diff=0.0, pairs=0)
    return NoiseFloor(
        disagreement_rate=disagree / len(gaps),
        mean_absolute_diff=sum(gaps) / len(gaps),
        pairs=len(gaps),
    )


def paired(records: list[RunRecord], baseline: str) -> list[PairResult]:
    """One row per (sample, model, repeat) where both sides were scored."""
    index = {
        (item.sample_key, item.variant_id, item.model, item.repeat): item
        for item in records
    }
    rows: list[PairResult] = []
    for item in records:
        if item.variant_id == baseline or item.score is None:
            continue
        base = index.get((item.sample_key, baseline, item.model, item.repeat))
        if base is None or base.score is None:
            continue
        diff = float(item.score) - float(base.score)
        if diff > 0:
            result = "win"
        elif diff < 0:
            result = "loss"
        else:
            result = "tie"
        rows.append(
            PairResult(
                variant_id=item.variant_id,
                sample_key=item.sample_key,
                model=item.model,
                repeat=item.repeat,
                baseline_score=float(base.score),
                variant_score=float(item.score),
                diff=diff,
                result=result,
                variant_run8=item.run8,
                baseline_run8=base.run8,
            )
        )
    return rows


def _summary(records: list[RunRecord], variant_id: str) -> VariantSummary:
    rows = [item for item in records if item.variant_id == variant_id]
    scored = [item for item in rows if item.score is not None]
    unscored = len(rows) - len(scored)
    if not scored:
        return VariantSummary(
            variant_id=variant_id,
            n_scored=0,
            n_unscored=unscored,
            mean_score=None,
            both_passed_rate=None,
            mean_tokens=None,
            mean_duration_secs=None,
            out_of_scope_runs=sum(1 for item in rows if item.out_of_scope_files > 0),
        )
    scores = [float(item.score) for item in scored if item.score is not None]
    return VariantSummary(
        variant_id=variant_id,
        n_scored=len(scored),
        n_unscored=unscored,
        mean_score=sum(scores) / len(scores),
        both_passed_rate=sum(1 for score in scores if score == 1.0) / len(scores),
        mean_tokens=sum(item.tokens for item in scored) / len(scored),
        mean_duration_secs=sum(item.duration_secs for item in scored) / len(scored),
        out_of_scope_runs=sum(1 for item in rows if item.out_of_scope_files > 0),
    )


def _classify(
    pairs: list[PairResult],
    noise: NoiseFloor,
    records: list[RunRecord],
    baseline: str,
    variant_ids: tuple[str, ...],
) -> tuple[list[Proposal], list[Proposal]]:
    proposals: list[Proposal] = []
    observations: list[Proposal] = []
    floor = noise.mean_absolute_diff
    for variant_id in variant_ids:
        if variant_id == baseline:
            continue
        rows = [item for item in pairs if item.variant_id == variant_id]
        by_sample: dict[str, list[PairResult]] = defaultdict(list)
        for item in rows:
            by_sample[item.sample_key].append(item)
        sample_means = {
            key: sum(item.diff for item in bucket) / len(bucket)
            for key, bucket in by_sample.items()
        }
        above = sorted(key for key, mean in sample_means.items() if mean > floor)
        mean_diff = (
            sum(item.diff for item in rows) / len(rows) if rows else None
        )
        worst = min(sample_means.values()) if sample_means else 0.0
        run8s = tuple(
            item.run8
            for item in records
            if item.variant_id == variant_id and item.sample_key in above and item.score is not None
        )
        wording = "建议人审考虑"
        reason = _proposal_reason(len(above), worst, mean_diff, floor, noise.pairs)
        proposal = Proposal(
            variant_id=variant_id,
            status="proposal" if reason is None else "observation",
            wording=wording if reason is None else "观察",
            samples=tuple(above),
            run8s=run8s,
            mean_paired_diff=mean_diff,
            reason=reason or "至少 2 个样本的配对差高于噪声底线",
        )
        if reason is None:
            proposals.append(proposal)
        else:
            observations.append(proposal)
    return proposals, observations


def _proposal_reason(
    above: int,
    worst: float,
    mean_diff: float | None,
    floor: float,
    noise_pairs: int,
) -> str | None:
    # Without at least one scored pair of baseline repeats the floor is not
    # measured: 0.0 would let any positive difference through.
    if noise_pairs < 1:
        return "没有测到噪声底线（基线重复未成对评分），只记为观察"
    if above < PROPOSAL_MIN_SAMPLES:
        return f"只有 {above} 个样本高于噪声底线，提案至少需要 {PROPOSAL_MIN_SAMPLES} 个"
    if worst < -PROPOSAL_MAX_LOSS:
        return f"有样本相对基线下降超过 {PROPOSAL_MAX_LOSS}"
    if mean_diff is None or mean_diff <= floor:
        return "平均配对差没有高于噪声底线"
    return None
