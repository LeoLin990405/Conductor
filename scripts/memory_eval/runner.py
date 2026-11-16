"""Memory-context evaluation: selected vs required, budget, leakage, staleness.

This measures the **selection policy** (quota, dedupe, budget) against a
deterministic in-memory store — retrieval quality (Postgres FTS) is measured
separately, so a policy regression cannot hide behind a retrieval difference.

Each case declares its memory corpus, the entries retrieval would return (in
order), the required/relevant ids and the ids that must never be injected. The
runner is the Phase-2 gate from docs/refactor/0005-memory-eval.md: required
recall ≥ 0.90, precision ≥ 0.70, zero leakage, zero stale entries, every case
inside its token budget.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.server.services.refactor.memory_context import (
    MemoryContext,
    TIER_ORDER,
    select_memory_context,
    selected_ids,
)
from src.server.contracts.memory_validity import is_current

DEFAULT_MIN_RECALL = 0.90
DEFAULT_MIN_PRECISION = 0.70
CASES_PATH = Path(__file__).with_name("cases.json")


class EvalError(ValueError):
    """A case file or case shape the runner cannot evaluate."""


@dataclass(frozen=True)
class Case:
    id: str
    user_id: str
    query: str
    budget_tokens: int
    entries: tuple[dict[str, Any], ...]
    required: tuple[str, ...]
    relevant: tuple[str, ...]
    must_not_include: tuple[str, ...]
    source_refs_required: tuple[str, ...] = ()
    condition_fields_required: tuple[str, ...] = ()
    expected_origins: Mapping[str, str] = field(default_factory=dict)
    must_not_count_as_evidence: tuple[str, ...] = ()
    thread_id: str | None = None


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalError(f"cannot read cases: {exc}") from exc
    cases: list[Case] = []
    seen: set[str] = set()
    for item in raw.get("cases") or []:
        case_id = str(item.get("id") or "")
        if not case_id or case_id in seen:
            raise EvalError(f"case id missing or duplicated: {case_id!r}")
        seen.add(case_id)
        required = tuple(str(key) for key in item.get("required") or ())
        if not required:
            raise EvalError(f"{case_id}: required must not be empty")
        entries = tuple(item.get("entries") or ())
        for entry in entries:
            if entry.get("tier") not in TIER_ORDER:
                raise EvalError(f"{case_id}: unknown tier {entry.get('tier')!r}")
            if not entry.get("key") or not entry.get("index_text"):
                raise EvalError(f"{case_id}: entry needs key and index_text")
        cases.append(
            Case(
                id=case_id,
                user_id=str(item.get("user") or "u1"),
                query=str(item.get("query") or ""),
                budget_tokens=int(item.get("budget_tokens") or 0),
                entries=entries,
                required=required,
                relevant=tuple(str(key) for key in item.get("relevant") or required),
                must_not_include=tuple(
                    str(key) for key in item.get("must_not_include") or ()
                ),
                source_refs_required=tuple(
                    str(key) for key in item.get("source_refs_required") or ()
                ),
                condition_fields_required=tuple(
                    str(key) for key in item.get("condition_fields_required") or ()
                ),
                expected_origins={
                    str(key): str(origin)
                    for key, origin in (item.get("expected_origins") or {}).items()
                },
                must_not_count_as_evidence=tuple(
                    str(key)
                    for key in item.get("must_not_count_as_evidence") or ()
                ),
                thread_id=(str(item["thread_id"]) if item.get("thread_id") else None),
            )
        )
    if not cases:
        raise EvalError("cases file is empty")
    return cases


class FakeMemoryStore:
    """Deterministic retrieval: each tier returns the case's entries in order."""

    def __init__(self, entries: Sequence[Mapping[str, Any]]) -> None:
        self.entries = list(entries)
        self.queries: list[tuple[str, str, str, int, str | None, str | None]] = []

    async def __call__(
        self,
        user_id: str,
        tier: str,
        query: str,
        limit: int,
        thread_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        self.queries.append((user_id, tier, query, limit, thread_id, workspace_id))
        rows = []
        for entry in self.entries:
            if str(entry.get("user") or "") != user_id:
                continue
            if str(entry.get("tier") or "") != tier:
                continue
            if tier in {"session", "task"} and thread_id is not None:
                if str(entry.get("thread_id") or "") != thread_id:
                    continue
            rows.append(
                {
                    **entry,
                    # The search contract names the identity ``entry_key``; a
                    # case file may use the shorter ``key``.
                    "entry_key": entry.get("key"),
                    "status": entry.get("status") or "active",
                    "content_hash": entry.get("content_hash")
                    or f"{tier}:{entry.get('key')}",
                }
            )
        return rows[:limit]


@dataclass(frozen=True)
class CaseResult:
    id: str
    selected: tuple[str, ...]
    missing: tuple[str, ...]
    recall: float
    precision: float
    budget_ok: bool
    leakage: tuple[str, ...]
    stale: tuple[str, ...]
    tokens: int
    dropped: int
    latency_ms: float = 0.0
    source_reference_errors: tuple[str, ...] = ()
    condition_fields_missing: tuple[str, ...] = ()
    attribution_errors: tuple[str, ...] = ()
    unsupported_checks: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return (
            self.recall >= DEFAULT_MIN_RECALL
            and self.precision >= 0.0
            and self.budget_ok
            and not self.leakage
            and not self.stale
            and not self.source_reference_errors
            and not self.condition_fields_missing
            and not self.attribution_errors
            and not self.unsupported_checks
        )


def _valid_quote_ref(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    message_id = value.get("message_id")
    start, end = value.get("start"), value.get("end")
    return (
        isinstance(message_id, str)
        and bool(message_id.strip())
        and value.get("unit") == "unicode_codepoint"
        and isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and start >= 0
        and end > start
    )


def _entry_output(entry: Any) -> tuple[dict[str, Any], str]:
    """Read only the selected context entry's published fields and rendering."""
    raw = entry.to_dict() if callable(getattr(entry, "to_dict", None)) else {}
    result = dict(raw) if isinstance(raw, Mapping) else {}
    provenance = result.get("provenance")
    if isinstance(provenance, Mapping):
        result = {**result, **provenance}
    metadata = result.get("metadata")
    if isinstance(metadata, Mapping):
        result = {**result, **metadata}
    return result, entry.render() if callable(getattr(entry, "render", None)) else ""


def evaluate_case(
    case: Case, context: MemoryContext, *, now=None, latency_ms: float = 0.0
) -> CaseResult:
    selected = set(selected_ids(context))
    required = set(case.required)
    relevant = set(case.relevant or case.required)
    missing = tuple(sorted(required - selected))
    recall = (len(required) - len(missing)) / len(required)
    precision = len(selected & relevant) / len(selected) if selected else 1.0
    leakage = tuple(sorted(selected & set(case.must_not_include)))
    status_by_id = {
        f"{entry['tier']}:{entry['key']}": str(entry.get("status") or "active")
        for entry in case.entries
    }
    metadata_by_id = {
        f"{entry['tier']}:{entry['key']}": entry.get("metadata")
        for entry in case.entries
    }
    stale = tuple(sorted(
        key for key in selected
        if status_by_id.get(key, "active") != "active"
        or not is_current(metadata_by_id.get(key), now=now)
    ))
    context_by_id = {
        f"{entry.tier}:{entry.entry_key}": entry for entry in context.entries
    }
    source_reference_errors: list[str] = []
    condition_fields_missing: list[str] = []
    unsupported_checks: list[str] = []
    for key in case.source_refs_required:
        if key not in selected:
            source_reference_errors.append(key)
            continue
        payload, rendered = _entry_output(context_by_id[key])
        ref = payload.get("evidence_quote_ref")
        if "evidence_quote_ref" not in payload:
            if "evidence_quote_ref" not in rendered:
                unsupported_checks.append(f"{key}:evidence_quote_ref")
            else:
                source_reference_errors.append(key)
        elif not _valid_quote_ref(ref):
            source_reference_errors.append(key)
    for key in case.condition_fields_required:
        if key not in selected:
            condition_fields_missing.append(key)
            continue
        payload, rendered = _entry_output(context_by_id[key])
        has_any = any(field in payload for field in ("applies_when", "exceptions"))
        if not has_any:
            if "applies_when" not in rendered or "exceptions" not in rendered:
                unsupported_checks.append(f"{key}:conditional_metadata")
            else:
                condition_fields_missing.append(key)
        elif not all(
            isinstance(payload.get(field), str) and bool(payload[field].strip())
            for field in ("applies_when", "exceptions")
        ):
            condition_fields_missing.append(key)
    attribution_errors = tuple(
        sorted(
            key
            for key, expected_origin in case.expected_origins.items()
            if key not in selected
            or (
                context_by_id[key].origin != expected_origin
                or (
                    expected_origin == "assistant_inference"
                    and "[inference; low confidence; not evidence]"
                    not in context_by_id[key].render()
                )
            )
        )
        + sorted(
            key
            for key in case.must_not_count_as_evidence
            if key in selected and context_by_id[key].counts_as_evidence
        )
    )
    return CaseResult(
        id=case.id,
        selected=tuple(sorted(selected)),
        missing=missing,
        recall=recall,
        precision=precision,
        budget_ok=context.tokens_used <= case.budget_tokens,
        leakage=leakage,
        stale=stale,
        tokens=context.tokens_used,
        dropped=context.dropped,
        latency_ms=max(0.0, float(latency_ms)),
        source_reference_errors=tuple(sorted(source_reference_errors)),
        condition_fields_missing=tuple(sorted(condition_fields_missing)),
        attribution_errors=attribution_errors,
        unsupported_checks=tuple(sorted(unsupported_checks)),
    )


@dataclass(frozen=True)
class EvalReport:
    results: tuple[CaseResult, ...]
    elapsed_ms: float

    @property
    def mean_recall(self) -> float:
        return sum(result.recall for result in self.results) / len(self.results)

    @property
    def mean_precision(self) -> float:
        return sum(result.precision for result in self.results) / len(self.results)

    @property
    def leakage(self) -> tuple[str, ...]:
        return tuple(key for result in self.results for key in result.leakage)

    @property
    def stale(self) -> tuple[str, ...]:
        return tuple(key for result in self.results for key in result.stale)

    @property
    def budget_failures(self) -> tuple[str, ...]:
        return tuple(result.id for result in self.results if not result.budget_ok)

    @property
    def source_reference_errors(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (result.id, key)
            for result in self.results
            for key in result.source_reference_errors
        )

    @property
    def condition_failures(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (result.id, key)
            for result in self.results
            for key in result.condition_fields_missing
        )

    @property
    def attribution_failures(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (result.id, key)
            for result in self.results
            for key in result.attribution_errors
        )

    @property
    def unsupported_checks(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (result.id, check)
            for result in self.results
            for check in result.unsupported_checks
        )

    def ok(self, *, min_recall: float, min_precision: float) -> bool:
        return (
            self.mean_recall >= min_recall
            and self.mean_precision >= min_precision
            and all(result.recall >= min_recall for result in self.results)
            and not self.leakage
            and not self.stale
            and not self.budget_failures
            and not self.source_reference_errors
            and not self.condition_failures
            and not self.attribution_failures
            and not self.unsupported_checks
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok(min_recall=DEFAULT_MIN_RECALL, min_precision=DEFAULT_MIN_PRECISION),
            "mean_recall": round(self.mean_recall, 4),
            "mean_precision": round(self.mean_precision, 4),
            "leakage": list(self.leakage),
            "stale": list(self.stale),
            "budget_failures": list(self.budget_failures),
            "source_reference_errors": [list(item) for item in self.source_reference_errors],
            "condition_failures": [list(item) for item in self.condition_failures],
            "attribution_failures": [list(item) for item in self.attribution_failures],
            "unsupported_checks": [list(item) for item in self.unsupported_checks],
            "elapsed_ms": round(self.elapsed_ms, 2),
            "cases": [
                {
                    "id": result.id,
                    "recall": round(result.recall, 4),
                    "precision": round(result.precision, 4),
                    "tokens": result.tokens,
                    "budget_ok": result.budget_ok,
                    "leakage": list(result.leakage),
                    "stale": list(result.stale),
                    "missing": list(result.missing),
                    "dropped": result.dropped,
                    "latency_ms": round(result.latency_ms, 3),
                    "source_reference_errors": list(result.source_reference_errors),
                    "condition_fields_missing": list(result.condition_fields_missing),
                    "attribution_errors": list(result.attribution_errors),
                    "unsupported_checks": list(result.unsupported_checks),
                }
                for result in self.results
            ],
        }

    def render_table(self) -> str:
        lines = [
            "| case | recall | precision | tokens | latency ms | budget | leakage | stale |",
            "| --- | --- | --- | --- | ---: | --- | --- | --- |",
        ]
        for result in self.results:
            lines.append(
                f"| {result.id} | {result.recall:.2f} | {result.precision:.2f} | "
                f"{result.tokens} | {result.latency_ms:.3f} | "
                f"{'ok' if result.budget_ok else 'OVER'} | "
                f"{','.join(result.leakage) or '-'} | {','.join(result.stale) or '-'} |"
            )
        lines += [
            "",
            f"mean recall {self.mean_recall:.2f} · mean precision "
            f"{self.mean_precision:.2f} · leakage {len(self.leakage)} · "
            f"stale {len(self.stale)} · budget failures {len(self.budget_failures)} · "
            f"source-ref errors {len(self.source_reference_errors)} · "
            f"condition failures {len(self.condition_failures)} · "
            f"attribution failures {len(self.attribution_failures)} · "
            f"unsupported checks {len(self.unsupported_checks)} · "
            f"{self.elapsed_ms:.0f} ms",
        ]
        return "\n".join(lines)


async def run_cases(cases: Sequence[Case]) -> EvalReport:
    started = time.perf_counter()
    results: list[CaseResult] = []
    for case in cases:
        store = FakeMemoryStore(case.entries)
        case_started = time.perf_counter()
        context = await select_memory_context(
            user_id=case.user_id,
            query=case.query,
            budget_tokens=case.budget_tokens,
            search=store,
            thread_id=case.thread_id,
        )
        case_latency_ms = (time.perf_counter() - case_started) * 1000
        results.append(evaluate_case(case, context, latency_ms=case_latency_ms))
    return EvalReport(
        results=tuple(results),
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory-eval", description=__doc__)
    parser.add_argument("--cases", default=str(CASES_PATH))
    parser.add_argument("--json", help="write the machine-readable report here")
    parser.add_argument("--min-recall", type=float, default=DEFAULT_MIN_RECALL)
    parser.add_argument("--min-precision", type=float, default=DEFAULT_MIN_PRECISION)
    args = parser.parse_args(argv)
    try:
        cases = load_cases(Path(args.cases))
        report = asyncio.run(run_cases(cases))
    except EvalError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    print(report.render_table())
    if args.json:
        Path(args.json).write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    ok = report.ok(min_recall=args.min_recall, min_precision=args.min_precision)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
