#!/usr/bin/env python3
"""Offline evaluation of fast-judgment records (N61 section 4.4). Read-only.

    python scripts/judgment_eval.py --records records.jsonl --outcomes outcomes.jsonl

Records are the JSONL lines ``JsonlRecorder`` writes. Outcomes are what
actually happened, as JSONL or a JSON list, one row per judged subject::

    {"use": "completion_evidence", "subject": "line-42",
     "truth": {"report_supported": false, "in_scope": true}}

A record joins its outcome on ``(use, subject)``. ``truth`` holds the right
answer per question id: an option/level for a choice or score question, a
bool (or "true"/"false") for a bool question.

Per use the report gives:

- ``false_pass_rate``: adopted (PASS) answers that were wrong / labelled PASS
  answers. ``false_block_rate``: FAIL answers whose truth was true / labelled
  FAIL answers. ``None`` when there is nothing to divide by.
- ``inconclusive_rate`` and ``error_rate`` over judged records; skipped
  (mode off, not sampled, no user id), abstained (INCONCLUSIVE) and error records are
  counted separately.
- latency P50/P95 (nearest rank) over judged records.
- ``llm_calls_saveable``: PASS records, the System Two calls an enforce mode
  could skip; ``_correct`` counts those whose labelled answers were all right.
- ``judgment_tokens``: the backend's token usage. Records without usage are
  counted in ``usage_missing`` and make the total ``None``: missing usage is
  never zero cost.
- ``ece``: expected calibration error of the top answer over 10 equal bins.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SKIP_REASONS = frozenset({"mode_off", "not_sampled", "no_user", "no_user_credentials"})
ECE_BINS = 10


def _rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Rows of a JSONL file or a JSON list; second item counts unreadable rows."""
    text = Path(path).read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return [], 1
        rows = [row for row in data if isinstance(row, dict)]
        return rows, len(data) - len(rows)
    rows: list[dict[str, Any]] = []
    bad = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    return rows, bad


def _norm(value: Any) -> str | None:
    """Answers and truths compared as text; bools as "true"/"false"."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    return text.casefold() if text.casefold() in {"true", "false"} else text


def percentile(values: Iterable[float], q: float) -> float | None:
    """Nearest-rank percentile; ``None`` for no values."""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def expected_calibration_error(
    samples: Iterable[tuple[float, bool]], bins: int = ECE_BINS
) -> float | None:
    """ECE over ``bins`` equal-width bins of (confidence, correct)."""
    counts = [0] * bins
    conf_sum = [0.0] * bins
    hits = [0] * bins
    total = 0
    for confidence, correct in samples:
        index = min(bins - 1, max(0, int(confidence * bins)))
        counts[index] += 1
        conf_sum[index] += confidence
        hits[index] += 1 if correct else 0
        total += 1
    if not total:
        return None
    return sum(
        counts[i] / total * abs(hits[i] / counts[i] - conf_sum[i] / counts[i])
        for i in range(bins)
        if counts[i]
    )


def _top(dist: Mapping[str, Any]) -> tuple[str, float] | None:
    clean = {
        str(k): float(v)
        for k, v in dist.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    if not clean:
        return None
    if set(clean) == {"true"}:
        clean = {"true": clean["true"], "false": 1.0 - clean["true"]}
    option = max(sorted(clean), key=lambda k: clean[k])
    return option, clean[option]


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def evaluate(
    records: Iterable[Mapping[str, Any]], outcomes: Iterable[Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Per-use metrics. Pure: reads nothing, writes nothing."""
    truth: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in outcomes:
        subject = str(row.get("subject", "") or "")
        if subject and isinstance(row.get("truth"), Mapping):
            truth[(str(row.get("use", "")), subject)] = row["truth"]

    acc: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "records": 0,
            "skipped": 0,
            "judged": 0,
            "pass": 0,
            "fail": 0,
            "abstained": 0,
            "error": 0,
            "labelled": 0,
            "pass_decisions": 0,
            "false_pass": 0,
            "fail_decisions": 0,
            "false_block": 0,
            "llm_calls_saveable": 0,
            "llm_calls_saveable_correct": 0,
            "usage_missing": 0,
            "_tokens": 0,
            "_latency": [],
            "_ece": [],
        }
    )
    for record in records:
        use = str(record.get("use", ""))
        m = acc[use]
        m["records"] += 1
        status = str(record.get("status", ""))
        if str(record.get("reason", "")) in SKIP_REASONS:
            m["skipped"] += 1
            continue
        m["judged"] += 1
        if status == "pass":
            m["pass"] += 1
        elif status == "fail":
            m["fail"] += 1
        elif status == "inconclusive":
            m["abstained"] += 1
        else:
            m["error"] += 1
        latency = record.get("latency_ms")
        if isinstance(latency, (int, float)) and not isinstance(latency, bool):
            m["_latency"].append(float(latency))
        usage = record.get("usage")
        tokens = usage.get("total_tokens") if isinstance(usage, Mapping) else None
        if isinstance(tokens, (int, float)) and not isinstance(tokens, bool):
            m["_tokens"] += tokens
        else:
            m["usage_missing"] += 1

        labels = truth.get((use, str(record.get("subject", "") or "")))
        if status == "pass":
            m["llm_calls_saveable"] += 1
        if labels is None:
            continue
        m["labelled"] += 1
        q_status = record.get("question_status") or {}
        answers = record.get("answers") or {}
        probabilities = record.get("probabilities") or {}
        all_right = True
        any_label = False
        for qid, raw_truth in labels.items():
            expected = _norm(raw_truth)
            if expected is None:
                continue
            qs = str(q_status.get(qid, ""))
            answer = _norm(answers.get(qid))
            if qs == "pass":
                any_label = True
                m["pass_decisions"] += 1
                if answer != expected:
                    m["false_pass"] += 1
                    all_right = False
            elif qs == "fail":
                m["fail_decisions"] += 1
                if expected == "true":
                    m["false_block"] += 1
            dist = probabilities.get(qid)
            if isinstance(dist, Mapping) and qs != "error":
                top = _top(dist)
                if top is not None:
                    m["_ece"].append((top[1], top[0] == expected))
        if status == "pass" and any_label and all_right:
            m["llm_calls_saveable_correct"] += 1

    report: dict[str, dict[str, Any]] = {}
    for use, m in sorted(acc.items()):
        latency = m.pop("_latency")
        ece_samples = m.pop("_ece")
        tokens = m.pop("_tokens")
        out = dict(m)
        out["false_pass_rate"] = _rate(m["false_pass"], m["pass_decisions"])
        out["false_block_rate"] = _rate(m["false_block"], m["fail_decisions"])
        out["inconclusive_rate"] = _rate(m["abstained"], m["judged"])
        out["error_rate"] = _rate(m["error"], m["judged"])
        out["latency_p50_ms"] = percentile(latency, 50)
        out["latency_p95_ms"] = percentile(latency, 95)
        out["judgment_tokens"] = tokens if m["usage_missing"] == 0 and m["judged"] else None
        out["ece"] = expected_calibration_error(ece_samples)
        out["ece_samples"] = len(ece_samples)
        report[use] = out
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--records", required=True, type=Path)
    parser.add_argument("--outcomes", type=Path, default=None)
    args = parser.parse_args(argv)
    records, bad_records = _rows(args.records)
    outcomes: list[dict[str, Any]] = []
    bad_outcomes = 0
    if args.outcomes is not None:
        outcomes, bad_outcomes = _rows(args.outcomes)
    report = {
        "uses": evaluate(records, outcomes),
        "unreadable_records": bad_records,
        "unreadable_outcomes": bad_outcomes,
    }
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
