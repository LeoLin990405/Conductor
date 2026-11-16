"""Every score row, plus the two-sample proposal rule."""

from __future__ import annotations

import pytest

from src.tools.auto_dispatch.harness_eval.scoring import build_report, score_run
from src.tools.auto_dispatch.harness_eval.types import G2Status, ReviewStatus, WorkerStatus

from harness_eval_fakes import make_record


@pytest.mark.parametrize(
    ("worker", "g2", "review", "preexisting", "base", "score", "reason"),
    [
        (WorkerStatus.COMMITTED, G2Status.PASS, ReviewStatus.PASSED, False, G2Status.PASS, 1.0, None),
        (WorkerStatus.COMMITTED, G2Status.PASS, ReviewStatus.BLOCKED, False, G2Status.PASS, 0.5, None),
        (WorkerStatus.COMMITTED, G2Status.SKIPPED, ReviewStatus.PASSED, False, G2Status.SKIPPED, 0.5, None),
        (WorkerStatus.COMMITTED, G2Status.FAIL, ReviewStatus.PASSED, True, G2Status.FAIL, 0.5, None),
        (WorkerStatus.COMMITTED, G2Status.FAIL, ReviewStatus.BLOCKED, True, G2Status.FAIL, 0.0, None),
        (WorkerStatus.COMMITTED, G2Status.FAIL, ReviewStatus.NOT_RUN, False, G2Status.PASS, 0.0, None),
        (WorkerStatus.NO_NEW_COMMIT, G2Status.NOT_RUN, ReviewStatus.NOT_RUN, False, G2Status.PASS, 0.0, None),
        (
            WorkerStatus.UNCOMMITTED_CHANGES,
            G2Status.NOT_RUN,
            ReviewStatus.NOT_RUN,
            False,
            G2Status.PASS,
            0.0,
            None,
        ),
        (WorkerStatus.WORKER_FAILED, G2Status.NOT_RUN, ReviewStatus.NOT_RUN, False, G2Status.PASS, 0.0, None),
        (
            WorkerStatus.COMMITTED,
            G2Status.PASS,
            ReviewStatus.UNPARSEABLE,
            False,
            G2Status.PASS,
            None,
            "review_unparseable",
        ),
        (WorkerStatus.RATE_LIMITED, G2Status.NOT_RUN, ReviewStatus.NOT_RUN, False, G2Status.NOT_RUN, None, "rate_limited"),
        (WorkerStatus.COMMITTED, G2Status.ENV_ERROR, ReviewStatus.NOT_RUN, False, G2Status.PASS, None, "env_error"),
        (WorkerStatus.COMMITTED, G2Status.TIMEOUT, ReviewStatus.NOT_RUN, False, G2Status.PASS, None, "timeout"),
        (WorkerStatus.COMMITTED, G2Status.TIMEOUT, ReviewStatus.PASSED, False, G2Status.FAIL, None, "timeout"),
        (WorkerStatus.TIMEOUT, G2Status.NOT_RUN, ReviewStatus.NOT_RUN, False, G2Status.NOT_RUN, None, "timeout"),
        (WorkerStatus.ENV_ERROR, G2Status.NOT_RUN, ReviewStatus.NOT_RUN, False, G2Status.NOT_RUN, None, "env_error"),
        (
            WorkerStatus.BUDGET_SKIPPED,
            G2Status.NOT_RUN,
            ReviewStatus.NOT_RUN,
            False,
            G2Status.NOT_RUN,
            None,
            "budget_skipped",
        ),
        (
            WorkerStatus.LEAK_CHECK_FAILED,
            G2Status.NOT_RUN,
            ReviewStatus.NOT_RUN,
            False,
            G2Status.NOT_RUN,
            None,
            "leak_check_failed",
        ),
        (WorkerStatus.COMMITTED, G2Status.PASS, ReviewStatus.NOT_RUN, False, G2Status.ENV_ERROR, None, "env_error"),
        (WorkerStatus.COMMITTED, G2Status.SKIPPED, ReviewStatus.BLOCKED, False, G2Status.SKIPPED, 0.0, None),
    ],
)
def test_score_table(worker, g2, review, preexisting, base, score, reason) -> None:
    got, why = score_run(
        worker_status=worker.value,
        g2_status=g2.value,
        review_status=review.value,
        preexisting_failure=preexisting,
        base_g2_status=base.value,
    )
    assert got == score
    assert why == reason


def _matrix(variant_scores: dict[str, float]) -> list:
    rows = []
    for index, sample in enumerate(("s-a", "s-b", "s-c")):
        for repeat, baseline_score in enumerate((0.5, 0.5)):
            rows.append(
                make_record(
                    run8=f"b{index}{repeat}",
                    sample_key=sample,
                    sample_index=index,
                    variant_id="baseline",
                    repeat=repeat,
                    score=baseline_score,
                )
            )
        rows.append(
            make_record(
                run8=f"v{index}",
                sample_key=sample,
                sample_index=index,
                variant_id="short",
                repeat=0,
                score=variant_scores[sample],
            )
        )
    rows.append(
        make_record(
            run8="uuuuuuuu",
            sample_key="s-a",
            variant_id="short",
            repeat=1,
            score=None,
            unscored_reason="timeout",
            worker_status="timeout",
        )
    )
    return rows


def test_one_sample_win_is_an_observation_and_two_is_a_proposal() -> None:
    one = build_report(
        _matrix({"s-a": 1.0, "s-b": 0.5, "s-c": 0.5}),
        baseline="baseline",
        variant_ids=("baseline", "short"),
        incomplete=False,
        skills_unknown=True,
    )
    assert one.noise.pairs == 3
    assert one.noise.mean_absolute_diff == 0.0
    assert one.noise.disagreement_rate == 0.0
    assert one.proposals == ()
    assert one.observations[0].variant_id == "short"
    assert one.observations[0].wording == "观察"
    short = next(item for item in one.variants if item.variant_id == "short")
    assert short.n_unscored == 1
    assert short.n_scored == 3
    assert one.skills_unknown is True

    two = build_report(
        _matrix({"s-a": 1.0, "s-b": 1.0, "s-c": 0.5}),
        baseline="baseline",
        variant_ids=("baseline", "short"),
        incomplete=False,
        skills_unknown=False,
    )
    assert len(two.proposals) == 1
    proposal = two.proposals[0]
    assert proposal.wording == "建议人审考虑"
    assert proposal.samples == ("s-a", "s-b")
    assert set(proposal.run8s) == {"v0", "v1"}
    assert "自动" not in proposal.wording


def test_a_loss_beyond_half_a_point_stays_an_observation() -> None:
    rows = []
    scores = {"s-a": 1.0, "s-b": 1.0, "s-c": 0.0}
    baselines = {"s-a": 0.5, "s-b": 0.5, "s-c": 1.0}
    for index, sample in enumerate(("s-a", "s-b", "s-c")):
        for repeat in (0, 1):
            rows.append(
                make_record(
                    run8=f"b{index}{repeat}",
                    sample_key=sample,
                    sample_index=index,
                    repeat=repeat,
                    score=baselines[sample],
                )
            )
        rows.append(
            make_record(
                run8=f"v{index}",
                sample_key=sample,
                sample_index=index,
                variant_id="short",
                score=scores[sample],
            )
        )
    report = build_report(
        rows,
        baseline="baseline",
        variant_ids=("baseline", "short"),
        incomplete=False,
        skills_unknown=False,
    )
    assert report.noise.mean_absolute_diff == 0.0
    assert report.proposals == ()
    assert report.observations[0].status == "observation"


def test_no_measured_noise_floor_is_never_a_proposal() -> None:
    # One baseline repeat per sample: the noise floor has no pairs. The
    # variant wins on every sample, but without a measured floor it is only
    # an observation.
    rows = []
    for index, sample in enumerate(("s-a", "s-b", "s-c")):
        rows.append(
            make_record(run8=f"b{index}", sample_key=sample, sample_index=index, score=0.5)
        )
        rows.append(
            make_record(
                run8=f"v{index}",
                sample_key=sample,
                sample_index=index,
                variant_id="short",
                score=1.0,
            )
        )
    report = build_report(
        rows,
        baseline="baseline",
        variant_ids=("baseline", "short"),
        incomplete=False,
        skills_unknown=False,
    )
    assert report.noise.pairs == 0
    assert report.proposals == ()
    assert report.observations[0].status == "observation"
    assert "噪声底线" in report.observations[0].reason
