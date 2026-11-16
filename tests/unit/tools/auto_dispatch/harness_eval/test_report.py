"""Reports stay proposals and do not carry worker or blocker text."""

from __future__ import annotations

import json

from src.tools.auto_dispatch.harness_eval.report import format_dry_run, write_reports
from src.tools.auto_dispatch.harness_eval.scoring import build_report
from src.tools.auto_dispatch.harness_eval.types import Sample, Skip

from harness_eval_fakes import BLOCKER_TOKEN, MODEL, VERDICT_TOKEN, WORKER_TOKEN, make_record


def test_report_is_a_proposal_and_hides_secrets(tmp_path) -> None:
    records = [
        make_record(run8="b0", sample_key="s-a", repeat=0, score=0.5),
        make_record(run8="b1", sample_key="s-a", repeat=1, score=0.5),
        make_record(run8="b2", sample_key="s-b", repeat=0, score=0.5),
        make_record(run8="b3", sample_key="s-b", repeat=1, score=0.5),
        make_record(
            run8="v0",
            sample_key="s-a",
            variant_id="short",
            score=1.0,
            out_of_scope_files=1,
        ),
        make_record(run8="v1", sample_key="s-b", variant_id="short", score=1.0),
    ]
    report = build_report(
        records,
        baseline="baseline",
        variant_ids=("baseline", "short"),
        incomplete=False,
        skills_unknown=True,
    )
    json_path, md_path = write_reports(tmp_path, report)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = md_path.read_text(encoding="utf-8")
    assert payload["source"] == "offline-harness-eval"
    assert payload["proposal_only"] is True
    assert payload["proposals"][0]["wording"] == "建议人审考虑"
    assert markdown.startswith(
        "本报告只是提案。任何改 AGENTS.md、skills 或路由的动作，都要另开 Ticket，并经人批准。"
    )
    assert "不能称为网络隔离" in markdown
    assert "skills_unknown" in markdown
    for secret in (BLOCKER_TOKEN, WORKER_TOKEN, VERDICT_TOKEN):
        assert secret not in markdown
        assert secret not in json_path.read_text(encoding="utf-8")


def test_matrix_uses_every_scored_run_and_names_the_baseline(tmp_path) -> None:
    records = [
        make_record(run8="b0", sample_key="s-a", repeat=0, score=0.5),
        make_record(run8="b1", sample_key="s-a", repeat=1, score=1.0),
        make_record(run8="s0", sample_key="s-a", variant_id="short", repeat=0, score=1.0),
        make_record(run8="l0", sample_key="s-b", variant_id="lonely", repeat=0, score=0.5),
        make_record(
            run8="q0",
            sample_key="s-a",
            variant_id="quiet",
            repeat=0,
            score=None,
            unscored_reason="timeout",
            worker_status="timeout",
        ),
    ]
    report = build_report(
        records,
        baseline="baseline",
        variant_ids=("baseline", "short", "lonely", "quiet"),
        incomplete=False,
        skills_unknown=False,
    )
    assert report.baseline == "baseline"
    _json, md_path = write_reports(tmp_path, report)
    payload = json.loads(_json.read_text(encoding="utf-8"))
    markdown = md_path.read_text(encoding="utf-8")
    assert payload["baseline"] == "baseline"
    by_cell = {
        (item["sample_key"], item["variant_id"]): item["scores"] for item in payload["matrix"]
    }
    assert by_cell[("s-a", "baseline")] == [0.5, 1.0]
    assert by_cell[("s-a", "short")] == [1.0]
    assert by_cell[("s-b", "lonely")] == [0.5]
    assert "基线: baseline" in markdown
    assert "baseline (基线)" in markdown
    assert "| s-a | 0.750 | 1.000 | - | - |" in markdown
    assert "| s-b | - | - | 0.500 | - |" in markdown


def test_dry_run_text_lists_counts_and_skips() -> None:
    class _Experiment:
        models = (MODEL,)
        skills_unknown = True

    sample = Sample(
        key="zzhist9k:line-0",
        index=0,
        dispatch_id="zzhist9k",
        line_id="line-0",
        goal="Ship the note.",
        task="Add note.txt",
        acceptance=("cmd: true",),
        write_scope=("note.txt",),
        base_commit="a" * 40,
        forbidden=(VERDICT_TOKEN,),
    )
    text = format_dry_run(
        _Experiment(),  # type: ignore[arg-type]
        (sample,),
        (Skip("zzhist9k:line-1", "no_base_commit"),),
        2,
    )
    assert "planned_runs: 2" in text
    assert "planned_reviews: 2" in text
    assert f"models: {MODEL}" in text
    assert "samples: zzhist9k:line-0" in text
    assert "skipped: zzhist9k:line-1 no_base_commit" in text
    assert "skills_unknown: true" in text
    assert VERDICT_TOKEN not in text
