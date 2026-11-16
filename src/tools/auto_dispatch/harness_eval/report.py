"""Write ``plan.json``, ``runs.jsonl``, ``report.json``, and ``report.md``.

The markdown report is a proposal for a person to read. It does not contain
worker output, blocker text, or an instruction to apply a change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import safe_io
from .spec import selected_inputs_fingerprint
from .types import (
    SOURCE_TAG,
    EvalReport,
    Experiment,
    PlannedRun,
    RunRecord,
    Sample,
    Skip,
)

_PROPOSAL_BANNER = (
    "本报告只是提案。任何改 AGENTS.md、skills 或路由的动作，都要另开 Ticket，并经人批准。"
)
_LIMITATIONS = (
    "局限：没有 ground truth；reviewer 有噪声；样本很少；模型采样没有固定种子；"
    "live 模式下网络缺口和线上相同，不能称为网络隔离。"
)
ISOLATION_NOTE = (
    "live 启动路径没有 OS sandbox：worker 以同一用户运行，能直接读到 output_dir 里"
    "早先运行的 worker.diff、各运行的仓库和 plan.json；基线重复之间也可能互相 fetch "
    "分支，使噪声底线偏低。每个运行有自己的仓库只是 git 层面的隔离，不是文件系统隔离。"
)


def write_plan(
    output_dir: Path,
    experiment: Experiment,
    samples: tuple[Sample, ...],
    skips: tuple[Skip, ...],
    runs: tuple[PlannedRun, ...],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": experiment.schema,
        "experiment_sha256": experiment.canonical_sha256,
        "inputs_sha256": selected_inputs_fingerprint(samples),
        "experiment": experiment.canonical,
        "samples": [item.public_dict() for item in samples],
        "skipped": [{"key": item.key, "reason": item.reason} for item in skips],
        "runs": [item.to_dict() for item in runs],
        "budget": {
            "max_runs": experiment.budget.max_runs,
            "max_models": experiment.budget.max_models,
            "max_reviews": experiment.budget.max_reviews,
            "max_wall_minutes": experiment.budget.max_wall_minutes,
            "worker_timeout_seconds": experiment.budget.worker_timeout_seconds,
        },
        "planned_runs": len(runs),
        "planned_reviews": len(runs),
        "skills_unknown": experiment.skills_unknown,
    }
    path = output_dir / "plan.json"
    safe_io.write_text(output_dir, path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def read_plan(output_dir: Path) -> dict[str, Any]:
    path = output_dir / "plan.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("plan.json must be an object")
    return payload


def append_record(output_dir: Path, record: RunRecord) -> None:
    path = output_dir / "runs.jsonl"
    safe_io.append_line(output_dir, path, json.dumps(record.to_dict(), sort_keys=True))


def load_records(output_dir: Path) -> list[RunRecord]:
    """Last record for each ``run8``. Earlier lines stay in the file."""
    path = output_dir / "runs.jsonl"
    if not path.is_file():
        return []
    latest: dict[str, RunRecord] = {}
    order: list[str] = []
    # A torn last line (a crash mid-append) is skipped; that run is simply
    # not recorded yet and a resume runs it again.
    rows, _skipped = safe_io.parse_jsonl(path.read_text(encoding="utf-8"))
    for payload in rows:
        if not isinstance(payload, dict) or "run8" not in payload:
            continue
        record = RunRecord.from_dict(payload)
        if record.run8 not in latest:
            order.append(record.run8)
        latest[record.run8] = record
    return [latest[key] for key in order]


def write_reports(output_dir: Path, report: EvalReport) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "report.json"
    md_path = output_dir / "report.md"
    safe_io.write_text(
        output_dir, json_path, json.dumps(_report_json(report), indent=2, sort_keys=True) + "\n"
    )
    safe_io.write_text(output_dir, md_path, _report_md(report))
    return json_path, md_path


def format_dry_run(
    experiment: Experiment,
    samples: tuple[Sample, ...],
    skips: tuple[Skip, ...],
    run_count: int,
) -> str:
    """Stdout of a dry run: counts, models, samples, and skip reasons."""
    lines = [
        f"planned_runs: {run_count}",
        f"planned_reviews: {run_count}",
        "models: " + ",".join(experiment.models),
        "samples: " + ",".join(item.key for item in samples),
    ]
    for skip in skips:
        lines.append(f"skipped: {skip.key} {skip.reason}")
    if experiment.skills_unknown:
        lines.append("skills_unknown: true")
    return "\n".join(lines) + "\n"


def _report_json(report: EvalReport) -> dict[str, Any]:
    return {
        "source": SOURCE_TAG,
        "proposal_only": True,
        "isolation": {"os_sandbox": False, "note": ISOLATION_NOTE},
        "baseline": report.baseline,
        "incomplete": report.incomplete,
        "skills_unknown": report.skills_unknown,
        "variants": [
            {
                "variant_id": item.variant_id,
                "n_scored": item.n_scored,
                "n_unscored": item.n_unscored,
                "mean_score": item.mean_score,
                "both_passed_rate": item.both_passed_rate,
                "mean_tokens": item.mean_tokens,
                "mean_duration_secs": item.mean_duration_secs,
                "out_of_scope_runs": item.out_of_scope_runs,
            }
            for item in report.variants
        ],
        "pairs": [
            {
                "variant_id": item.variant_id,
                "sample_key": item.sample_key,
                "model": item.model,
                "repeat": item.repeat,
                "baseline_score": item.baseline_score,
                "variant_score": item.variant_score,
                "diff": item.diff,
                "result": item.result,
                "variant_run8": item.variant_run8,
                "baseline_run8": item.baseline_run8,
            }
            for item in report.pairs
        ],
        "noise_floor": {
            "disagreement_rate": report.noise.disagreement_rate,
            "mean_absolute_diff": report.noise.mean_absolute_diff,
            "pairs": report.noise.pairs,
        },
        "proposals": [_proposal_json(item) for item in report.proposals],
        "observations": [_proposal_json(item) for item in report.observations],
        "unscored": [
            {"run8": run8, "reason": reason} for run8, reason in report.unscored
        ],
        "warnings": list(report.warnings),
        "matrix": [
            {
                "sample_key": cell.sample_key,
                "variant_id": cell.variant_id,
                "scores": list(cell.scores),
            }
            for cell in report.cells
        ],
    }


def _proposal_json(item: Any) -> dict[str, Any]:
    return {
        "variant_id": item.variant_id,
        "status": item.status,
        "wording": item.wording,
        "samples": list(item.samples),
        "run8s": list(item.run8s),
        "mean_paired_diff": item.mean_paired_diff,
        "reason": item.reason,
    }


def _report_md(report: EvalReport) -> str:
    lines = [
        _PROPOSAL_BANNER,
        "",
        _conclusion(report),
        "",
        "## 变体",
        "",
        "| 变体 | n_scored | n_unscored | 平均分 | 两项都通过 | 平均 tokens | 平均耗时(s) | 越界运行 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report.variants:
        lines.append(
            "| {id} | {n} | {u} | {mean} | {both} | {tokens} | {duration} | {scope} |".format(
                id=item.variant_id,
                n=item.n_scored,
                u=item.n_unscored,
                mean=_num(item.mean_score),
                both=_num(item.both_passed_rate),
                tokens=_num(item.mean_tokens),
                duration=_num(item.mean_duration_secs),
                scope=item.out_of_scope_runs,
            )
        )
    lines.extend(["", "## 样本 × 变体", ""])
    lines.extend(_matrix(report))
    lines.extend(
        [
            "",
            "## 噪声底线",
            "",
            f"- 基线重复不一致比例: {report.noise.disagreement_rate:.3f}",
            f"- 平均绝对差: {report.noise.mean_absolute_diff:.3f}",
            f"- 配对数: {report.noise.pairs}",
            "",
            "## 提案",
            "",
        ]
    )
    if not report.proposals:
        lines.append("没有达到门槛的变体。")
    for item in report.proposals:
        evidence = ", ".join(item.run8s) if item.run8s else "(无 run8)"
        lines.append(
            f"- {item.wording} `{item.variant_id}`。"
            f"样本: {', '.join(item.samples) or '(无)'}。证据 run8: {evidence}。"
        )
    lines.extend(["", "## 观察", ""])
    if not report.observations:
        lines.append("没有额外观察。")
    for item in report.observations:
        lines.append(f"- `{item.variant_id}`: {item.reason}")
    lines.extend(["", "## 未评分", ""])
    if not report.unscored:
        lines.append("没有未评分的运行。")
    for run8, reason in report.unscored:
        lines.append(f"- `{run8}`: {reason}")
    if report.warnings:
        lines.extend(["", "## 警告", ""])
        for warning in report.warnings:
            lines.append(f"- {warning}")
    lines.extend(["", "## 局限", "", _LIMITATIONS, "", ISOLATION_NOTE, ""])
    return "\n".join(lines)


def _conclusion(report: EvalReport) -> str:
    if report.proposals:
        names = ", ".join(item.variant_id for item in report.proposals)
        return f"建议人审考虑这些变体: {names}。"
    return "没有变体同时在至少 2 个样本上超过噪声底线，本次只有观察，没有提案。"


def _matrix(report: EvalReport) -> list[str]:
    """Sample by variant means from every run record, not only paired rows.

    ``report.baseline`` names the baseline column. A second variant with no
    pairs must not hide that column, and a baseline repeat that has no
    matching variant run still contributes to the cell.
    """
    variants = [item.variant_id for item in report.variants]
    for cell in report.cells:
        if cell.variant_id not in variants:
            variants.append(cell.variant_id)
    if not variants:
        return ["(没有变体)"]
    samples: list[str] = []
    cells: dict[tuple[str, str], tuple[float, ...]] = {}
    for cell in report.cells:
        if cell.sample_key not in samples:
            samples.append(cell.sample_key)
        cells[(cell.sample_key, cell.variant_id)] = cell.scores
    lines = [f"基线: {report.baseline or '(未标明)'}"]
    if not samples:
        lines.append("(没有运行记录)")
        return lines
    titles = [
        f"{variant} (基线)" if variant == report.baseline else variant for variant in variants
    ]
    header = "| 样本 | " + " | ".join(titles) + " |"
    rule = "|---|" + "|".join("---:" for _ in variants) + "|"
    body = [header, rule]
    for sample in samples:
        row = [sample]
        for variant in variants:
            values = cells.get((sample, variant))
            row.append(_num(sum(values) / len(values) if values else None))
        body.append("| " + " | ".join(row) + " |")
    lines.extend(body)
    return lines


def _num(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}"
