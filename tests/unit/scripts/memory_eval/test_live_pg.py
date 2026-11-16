"""Live-Postgres retrieval probe: SQL generation, result parsing and scoring.

The probe is measurement, not the policy gate (that stays in runner.py): these
tests pin the parts that would silently misreport the measurement — SQL that
does not match production's FTS predicate, parsing that drops/misreads rows, or
scoring that counts a miss as recall.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.memory_eval import live_pg

REPO = Path(__file__).resolve().parents[4]
CASES = REPO / "scripts" / "memory_eval" / "retrieval_cases.json"

pytestmark = pytest.mark.asyncio


class TestCases:
    def test_committed_retrieval_cases_load(self):
        cases = live_pg.load_cases(CASES)
        assert len(cases["cases"]) == 10
        assert len({case["id"] for case in cases["cases"]}) == 10
        langs = {case["lang"] for case in cases["cases"]}
        assert langs == {"en", "zh"}

    def test_bad_cases_rejected(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"cases": []}))
        with pytest.raises(live_pg.ProbeError, match="empty"):
            live_pg.load_cases(path)


class TestSql:
    def test_matches_production_fts_predicate(self):
        sql = live_pg.build_sql(live_pg.load_cases(CASES))
        assert sql.startswith("BEGIN;")
        assert sql.rstrip().endswith("ROLLBACK;")
        assert "CREATE TEMP TABLE memory_entries" in sql
        assert "case_id TEXT NOT NULL" in sql
        assert "WHERE case_id = " in sql
        assert "GENERATED ALWAYS AS" in sql
        assert "websearch_to_tsquery('simple'," in sql
        assert "ts_rank(" in sql
        assert "status = 'active'" in sql
        assert "LIMIT 20" in sql

    def test_quotes_apostrophes_in_seeded_text(self):
        cases = {
            "user": "11111111-1111-1111-1111-111111111111",
            "cases": [
                {
                    "id": "quote-case",
                    "query": "it's fine",
                    "entries": [
                        {"tier": "user", "key": "k", "index_text": "it's fine, isn't it"},
                    ],
                    "expected": ["user:k"],
                }
            ],
        }
        sql = live_pg.build_sql(cases)
        assert "it''s fine, isn''t it" in sql
        assert "it''s fine'" in sql  # the query literal is escaped too


class TestCjkPath:
    def test_cjk_path_uses_scratch_database_and_trigram_predicates(self):
        sql = live_pg.build_sql(
            live_pg.load_cases(CASES), cjk_path=True, scratch_db=True
        )
        assert "DROP DATABASE IF EXISTS refactor_probe;" in sql
        assert "CREATE DATABASE refactor_probe;" in sql
        assert "\\connect refactor_probe" in sql
        assert "CREATE EXTENSION IF NOT EXISTS pg_trgm;" in sql
        # Latin cases keep the production FTS predicate on the trigram run too.
        assert "search @@ websearch_to_tsquery('simple', 'prefers Chinese answers')" in sql
        # Spaced CJK: contains per term; unspaced: the trigram similarity operator.
        assert "(entry_key || ' ' || index_text) ILIKE '%股票%' ESCAPE '\\'" in sql
        assert "'我关注的股票' <% (entry_key || ' ' || index_text)" in sql
        assert "word_similarity('我关注的股票', (entry_key || ' ' || index_text))" in sql
        assert "SET LOCAL pg_trgm.word_similarity_threshold = 0.45;" in sql
        # The scratch database is dropped again: zero residue.
        assert "\\connect postgres" in sql
        assert sql.rstrip().endswith("DROP DATABASE IF EXISTS refactor_probe;")

    def test_cjk_path_without_scratch_targets_a_migrated_database(self):
        sql = live_pg.build_sql(live_pg.load_cases(CASES), cjk_path=True)
        assert "CREATE DATABASE" not in sql
        assert "DROP DATABASE" not in sql
        assert "CREATE EXTENSION" not in sql
        # The path itself is the same; the target database is expected to have
        # migration 092 applied.
        assert "SET LOCAL pg_trgm.word_similarity_threshold = 0.45;" in sql
        assert "'我关注的股票' <% (entry_key || ' ' || index_text)" in sql


class TestParsing:
    def test_parses_marked_rows_and_ignores_psql_noise(self):
        text = "BEGIN\nCREATE TABLE\nINSERT 0 1\n\ncase-a|user|prefs/lang\ncase-a|task|task/x\nROLLBACK\n"
        assert live_pg.parse_results(text) == {
            "case-a": ["user:prefs/lang", "task:task/x"]
        }

    def test_ignores_connect_notices(self):
        text = 'You are now connected to database "refactor_probe" as user "postgres".\n'
        assert live_pg.parse_results(text) == {}

    def test_rejects_malformed_rows(self):
        with pytest.raises(live_pg.ProbeError, match="unexpected result row"):
            live_pg.parse_results("case-a|user\n")


class TestScoring:
    def _cases(self):
        return {
            "user": "u",
            "cases": [
                {
                    "id": "hit",
                    "lang": "en",
                    "query": "q1",
                    "entries": [{"tier": "user", "key": "a", "index_text": "x"}],
                    "expected": ["user:a"],
                },
                {
                    "id": "miss",
                    "lang": "zh",
                    "query": "q2",
                    "entries": [{"tier": "workspace", "key": "b", "index_text": "y"}],
                    "expected": ["workspace:b"],
                },
            ],
        }

    def test_scores_recall_precision_and_mrr(self):
        scores = live_pg.score_cases(
            self._cases(),
            {"hit": ["user:noise", "user:a"], "miss": ["workspace:noise"]},
        )
        hit, miss = scores
        assert hit.recall == 1.0
        assert hit.precision == 0.5
        assert hit.mrr == 0.5
        assert hit.ok
        assert miss.recall == 0.0
        assert miss.precision == 0.0
        assert not miss.ok

    def test_missing_case_scores_zero(self):
        scores = live_pg.score_cases(self._cases(), {})
        assert [score.recall for score in scores] == [0.0, 0.0]

    def test_report_splits_languages(self):
        scores = live_pg.score_cases(
            self._cases(), {"hit": ["user:a"], "miss": []}
        )
        report = live_pg.render_report(scores)
        assert "en: recall 1.00" in report
        assert "zh: recall 0.00" in report
        assert "cases fully retrieved 1/1" in report


class TestCli:
    def test_sql_mode_emits_sql(self, capsys):
        assert live_pg.main(["--sql"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("BEGIN;")
        assert "ROLLBACK;" in out

    def test_results_mode_scores_and_writes_json(self, tmp_path, capsys):
        results = tmp_path / "out.txt"
        results.write_text("", encoding="utf-8")
        out = tmp_path / "report.json"
        assert live_pg.main(["--results", str(results), "--json", str(out)]) == 0
        data = json.loads(out.read_text())
        assert len(data["cases"]) == 10
        assert "recall" in capsys.readouterr().out

    def test_missing_results_file_fails(self, tmp_path, capsys):
        assert live_pg.main(["--results", str(tmp_path / "nope.txt")]) == 2
        assert "FAIL" in capsys.readouterr().err
