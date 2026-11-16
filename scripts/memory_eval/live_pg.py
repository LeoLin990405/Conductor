"""Live Postgres retrieval-quality probe for the memory projection (Phase 2).

The fake-store eval measures the SELECTION policy; this probe measures the
actual FTS retrieval the projection will use, on a real Postgres instance. It
builds a transaction-scoped temp copy of ``memory_entries`` (latest DDL,
generated tsvector + GIN), inserts the retrieval corpus and runs the exact
production query (``websearch_to_tsquery('simple', ...)`` + ``ts_rank``) per
case, then reports recall/precision/MRR split by language.

The SQL is emitted for piping into psql on the host that owns the database; the
run is wrapped in BEGIN/ROLLBACK so it leaves no rows behind.

Usage:
    python -m scripts.memory_eval.live_pg --sql > /tmp/retrieval.sql
    ssh <host> 'docker compose exec -T postgres psql -U postgres -d postgres \
        -A -t -F "|" -v ON_ERROR_STOP=1' < /tmp/retrieval.sql > /tmp/retrieval.out
    python -m scripts.memory_eval.live_pg --results /tmp/retrieval.out --json report.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

CASES_PATH = Path(__file__).with_name("retrieval_cases.json")
LIMIT = 20
PROBE_DATABASE = "refactor_probe"

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")

DDL = """
CREATE TEMP TABLE memory_entries (
    entry_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Probe-only column: each case seeds its own corpus, so one case's rows
    -- cannot rank inside another case's query (production has no such column).
    case_id TEXT NOT NULL,
    user_id UUID NOT NULL,
    tier TEXT NOT NULL
        CHECK (tier IN ('user', 'project', 'workspace', 'session', 'task')),
    namespace TEXT[] NOT NULL DEFAULT '{}',
    entry_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    index_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'proposed', 'rejected')),
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_thread_id UUID,
    source_response_id UUID,
    search TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple'::regconfig, entry_key || ' ' || index_text)
    ) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON memory_entries USING gin (search);
CREATE INDEX ON memory_entries (user_id, tier, status);
"""


class ProbeError(ValueError):
    """Malformed cases file or psql result stream."""


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _case_predicate(case: dict[str, Any], *, cjk_path: bool) -> tuple[str, str]:
    """The WHERE predicate and ORDER BY expression the production path uses."""
    query = _sql_literal(str(case["query"]))
    combined = "(entry_key || ' ' || index_text)"
    if cjk_path and _has_cjk(str(case["query"])):
        terms = str(case["query"]).split()
        if len(terms) > 1:
            predicate = " AND ".join(
                f"{combined} ILIKE {_sql_literal(_like_pattern(term))} ESCAPE '\\'"
                for term in terms
            )
        else:
            # Production uses the word-similarity operator with a 0.45 session
            # floor (see _WORD_SIMILARITY_THRESHOLD in the SQL layer).
            predicate = f"{query} <% {combined}"
        return predicate, f"word_similarity({query}, {combined})"
    return (
        f"search @@ websearch_to_tsquery('simple', {query})",
        f"ts_rank(search, websearch_to_tsquery('simple', {query}))",
    )


def load_cases(path: Path = CASES_PATH) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeError(f"cannot read retrieval cases: {exc}") from exc
    if not raw.get("cases"):
        raise ProbeError("retrieval cases file is empty")
    seen: set[str] = set()
    for case in raw["cases"]:
        case_id = str(case.get("id") or "")
        if not case_id or case_id in seen:
            raise ProbeError(f"case id missing or duplicated: {case_id!r}")
        seen.add(case_id)
        if not case.get("query"):
            raise ProbeError(f"{case_id}: query is empty")
        if not case.get("expected"):
            raise ProbeError(f"{case_id}: expected must not be empty")
        for entry in case.get("entries") or ():
            if entry.get("tier") not in ("user", "project", "workspace", "session", "task"):
                raise ProbeError(f"{case_id}: unknown tier {entry.get('tier')!r}")
    return raw


def build_sql(
    cases: dict[str, Any], *, cjk_path: bool = False, scratch_db: bool = False
) -> str:
    user = str(cases.get("user") or "")
    if not user:
        raise ProbeError("cases file needs a user uuid")
    lines: list[str] = []
    if scratch_db:
        # Use when the target database must not be touched (e.g. the app DB has
        # not had migration 092 applied): create a scratch database, measure and
        # drop it. With ``cjk_path`` the extension is created only there.
        lines += [
            f"DROP DATABASE IF EXISTS {PROBE_DATABASE};",
            f"CREATE DATABASE {PROBE_DATABASE};",
            f"\\connect {PROBE_DATABASE}",
        ]
        if cjk_path:
            lines.append("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    lines += ["BEGIN;", DDL.strip()]
    if cjk_path:
        lines.append("SET LOCAL pg_trgm.word_similarity_threshold = 0.45;")
    lines.append("")
    for case in cases["cases"]:
        case_id = str(case["id"])
        for index, entry in enumerate(case.get("entries") or ()):
            key = str(entry["key"])
            text = str(entry.get("index_text") or "")
            status = str(entry.get("status") or "active")
            tier = str(entry["tier"])
            hash_ = f"{case_id}:{index}"
            lines.append(
                "INSERT INTO memory_entries"
                " (case_id, user_id, tier, entry_key, content_hash, index_text, status)"
                f" VALUES ({_sql_literal(case_id)}, {_sql_literal(user)},"
                f" {_sql_literal(tier)}, {_sql_literal(key)},"
                f" {_sql_literal(hash_)}, {_sql_literal(text)}, {_sql_literal(status)});"
            )
        lines.append("")
    for case in cases["cases"]:
        case_id = str(case["id"]).replace("'", "''")
        tiers = sorted({str(entry["tier"]) for entry in case.get("entries") or ()})
        tier_list = ", ".join(_sql_literal(tier) for tier in tiers)
        predicate, order = _case_predicate(case, cjk_path=cjk_path)
        lines += [
            "SELECT "
            f"{_sql_literal(case_id)} || '|' || tier || '|' || entry_key"
            " FROM memory_entries"
            f" WHERE case_id = {_sql_literal(case_id)}"
            f"   AND user_id = {_sql_literal(user)}"
            "   AND status = 'active'"
            f"   AND tier IN ({tier_list})"
            f"   AND {predicate}"
            f" ORDER BY {order} DESC, updated_at DESC"
            f" LIMIT {LIMIT};",
        ]
    lines += ["", "ROLLBACK;"]
    if scratch_db:
        lines += ["\\connect postgres", f"DROP DATABASE IF EXISTS {PROBE_DATABASE};"]
    return "\n".join(lines) + "\n"


def parse_results(text: str) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            # psql status lines and \connect notices carry no result row.
            continue
        parts = line.split("|")
        if len(parts) != 3:
            raise ProbeError(f"unexpected result row: {line!r}")
        case_id, tier, key = parts
        rows.setdefault(case_id, []).append(f"{tier}:{key}")
    return rows


@dataclass
class CaseScore:
    id: str
    lang: str
    query: str
    expected: list[str]
    retrieved: list[str] = field(default_factory=list)
    recall: float = 0.0
    precision: float = 0.0
    mrr: float = 0.0

    @property
    def ok(self) -> bool:
        return self.recall == 1.0


def score_cases(cases: dict[str, Any], results: dict[str, list[str]]) -> list[CaseScore]:
    scores: list[CaseScore] = []
    for case in cases["cases"]:
        case_id = str(case["id"])
        expected = [str(key) for key in case["expected"]]
        retrieved = results.get(case_id, [])
        expected_set = set(expected)
        hits = [key for key in retrieved if key in expected_set]
        recall = len(set(hits)) / len(expected_set) if expected_set else 1.0
        precision = len(hits) / len(retrieved) if retrieved else 0.0
        mrr = 0.0
        for rank, key in enumerate(retrieved, start=1):
            if key in expected_set:
                mrr = 1.0 / rank
                break
        scores.append(
            CaseScore(
                id=case_id,
                lang=str(case.get("lang") or "en"),
                query=str(case["query"]),
                expected=expected,
                retrieved=retrieved,
                recall=recall,
                precision=precision,
                mrr=mrr,
            )
        )
    return scores


def render_report(scores: Sequence[CaseScore]) -> str:
    lines = [
        "| case | lang | query | recall | precision | mrr | retrieved |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for score in scores:
        lines.append(
            f"| {score.id} | {score.lang} | {score.query} | {score.recall:.2f} | "
            f"{score.precision:.2f} | {score.mrr:.2f} | {', '.join(score.retrieved) or '-'} |"
        )
    for lang in ("en", "zh"):
        subset = [score for score in scores if score.lang == lang]
        if not subset:
            continue
        recall = sum(score.recall for score in subset) / len(subset)
        precision = sum(score.precision for score in subset) / len(subset)
        hits = sum(1 for score in subset if score.ok)
        lines.append("")
        lines.append(
            f"{lang}: recall {recall:.2f} · precision {precision:.2f} · "
            f"cases fully retrieved {hits}/{len(subset)}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory-live-pg", description=__doc__)
    parser.add_argument("--cases", default=str(CASES_PATH))
    parser.add_argument("--sql", action="store_true", help="emit SQL for psql")
    parser.add_argument(
        "--cjk-path",
        action="store_true",
        help="measure the trigram path (needs pg_trgm; requires migration 092 "
        "on the target database, or --scratch-db)",
    )
    parser.add_argument(
        "--scratch-db",
        action="store_true",
        help="run in a throwaway scratch database (zero residue, no migration needed)",
    )
    parser.add_argument("--results", help="psql -A -t -F '|' output to score")
    parser.add_argument("--json", help="write the machine-readable report here")
    args = parser.parse_args(argv)
    try:
        cases = load_cases(Path(args.cases))
        if args.sql:
            sys.stdout.write(build_sql(cases, cjk_path=args.cjk_path, scratch_db=args.scratch_db))
            return 0
        if not args.results:
            parser.error("one of --sql/--results is required")
        text = Path(args.results).read_text(encoding="utf-8")
        scores = score_cases(cases, parse_results(text))
    except (ProbeError, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    print(render_report(scores))
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "id": score.id,
                            "lang": score.lang,
                            "query": score.query,
                            "expected": score.expected,
                            "retrieved": score.retrieved,
                            "recall": round(score.recall, 4),
                            "precision": round(score.precision, 4),
                            "mrr": round(score.mrr, 4),
                        }
                        for score in scores
                    ]
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
