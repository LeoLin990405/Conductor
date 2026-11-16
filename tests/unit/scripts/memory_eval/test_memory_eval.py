"""Memory eval harness: case loading, metrics, thresholds and CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.memory_eval import runner
from src.server.services.refactor.memory_context import ContextEntry, MemoryContext

REPO = Path(__file__).resolve().parents[4]
CASES = REPO / "scripts" / "memory_eval" / "cases.json"


def _case(**overrides) -> runner.Case:
    values = {
        "id": "case",
        "user_id": "u1",
        "query": "q",
        "budget_tokens": 100,
        "entries": (
            {
                "user": "u1",
                "tier": "user",
                "key": "prefs/lang",
                "index_text": "prefers Chinese",
            },
        ),
        "required": ("user:prefs/lang",),
        "relevant": ("user:prefs/lang",),
        "must_not_include": (),
    }
    values.update(overrides)
    return runner.Case(**values)


def _context(
    *entries: tuple[str, str],
    tokens: int = 10,
    provenance: dict[str, dict] | None = None,
    metadata: dict[str, dict] | None = None,
) -> MemoryContext:
    return MemoryContext(
        query="q",
        budget_tokens=100,
        entries=tuple(
            ContextEntry(
                tier=tier,
                entry_key=key,
                index_text="index",
                content_hash=f"{tier}:{key}",
                tokens=tokens,
                rank=0,
                provenance=(provenance or {}).get(key, {}),
                metadata=(metadata or {}).get(key, {}),
            )
            for tier, key in entries
        ),
    )


class TestCases:
    def test_committed_cases_load(self):
        cases = runner.load_cases(CASES)
        assert len(cases) == 16
        assert len({case.id for case in cases}) == 16
        assert {
            "conditional-rule-preserves-new-concept",
            "long-term-conclusion-first-cross-session",
            "recall-exact-user-wording-source-reference",
            "assistant-inference-not-user-preference",
        } <= {case.id for case in cases}
        for case in cases:
            assert case.required
            assert case.budget_tokens > 0

    def test_bad_cases_rejected(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"cases": []}))
        with pytest.raises(runner.EvalError, match="empty"):
            runner.load_cases(path)
        path.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "id": "x",
                            "required": ["user:a"],
                            "entries": [
                                {"tier": "galactic", "key": "a", "index_text": "b"}
                            ],
                        }
                    ]
                }
            )
        )
        with pytest.raises(runner.EvalError, match="unknown tier"):
            runner.load_cases(path)


class TestMetrics:
    def test_full_recall_and_precision(self):
        result = runner.evaluate_case(_case(), _context(("user", "prefs/lang")))
        assert result.recall == 1.0
        assert result.precision == 1.0
        assert result.ok

    def test_missing_required_lowers_recall(self):
        result = runner.evaluate_case(_case(), _context())
        assert result.recall == 0.0
        assert result.missing == ("user:prefs/lang",)
        assert not result.ok

    def test_irrelevant_selection_lowers_precision(self):
        result = runner.evaluate_case(
            _case(), _context(("user", "prefs/lang"), ("user", "noise"))
        )
        assert result.precision == 0.5

    def test_leakage_detected(self):
        case = _case(must_not_include=("user:prefs/lang",))
        result = runner.evaluate_case(case, _context(("user", "prefs/lang")))
        assert result.leakage == ("user:prefs/lang",)
        assert not result.ok

    def test_stale_selected_detected(self):
        case = _case(
            entries=(
                {
                    "user": "u1",
                    "tier": "user",
                    "key": "prefs/lang",
                    "index_text": "old",
                    "status": "superseded",
                },
            ),
            required=("user:prefs/lang",),
        )
        result = runner.evaluate_case(case, _context(("user", "prefs/lang")))
        assert result.stale == ("user:prefs/lang",)
        assert not result.ok

    def test_expired_selection_is_stale_at_exclusive_boundary(self):
        from datetime import datetime, timezone

        case = _case(entries=({
            "user": "u1", "tier": "user", "key": "prefs/lang",
            "index_text": "expired", "metadata": {"valid_until": "2026-01-01T00:00:00Z"},
        },))
        boundary = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = runner.evaluate_case(case, _context(("user", "prefs/lang")), now=boundary)
        assert result.stale == ("user:prefs/lang",)
        assert not result.ok

    def test_budget_overrun_detected(self):
        case = _case(budget_tokens=5)
        result = runner.evaluate_case(case, _context(("user", "prefs/lang")))
        assert not result.budget_ok

    def test_report_thresholds(self):
        results = tuple(
            runner.evaluate_case(
                _case(id=f"c{index}"), _context(("user", "prefs/lang"))
            )
            for index in range(3)
        )
        report = runner.EvalReport(results=results, elapsed_ms=1.0)
        assert report.ok(min_recall=0.9, min_precision=0.7)
        assert report.to_dict()["ok"] is True
        assert "mean recall 1.00" in report.render_table()

    def test_one_wholly_missed_case_cannot_hide_in_corpus_mean(self):
        results = tuple(
            runner.evaluate_case(
                _case(id=f"c{index}"),
                _context() if index == 0 else _context(("user", "prefs/lang")),
            )
            for index in range(16)
        )
        report = runner.EvalReport(results=results, elapsed_ms=1.0)

        assert report.mean_recall == pytest.approx(0.9375)
        assert report.results[0].missing == ("user:prefs/lang",)
        assert not report.ok(min_recall=0.9, min_precision=0.7)

    def test_partial_case_recall_at_configured_threshold_passes(self):
        required = tuple(f"user:key-{index}" for index in range(10))
        case = _case(required=required, relevant=required)
        context = _context(*(("user", f"key-{index}") for index in range(9)))
        report = runner.EvalReport(
            results=(runner.evaluate_case(case, context),), elapsed_ms=1.0
        )

        assert report.mean_recall == 0.9
        assert report.ok(min_recall=0.9, min_precision=0.7)

    def test_contract_checks_fail_when_the_required_entry_is_unselected(self):
        key = "user:prefs/lang"
        case = _case(
            source_refs_required=(key,),
            condition_fields_required=(key,),
            expected_origins={key: "user"},
        )

        result = runner.evaluate_case(case, _context())
        report = runner.EvalReport(results=(result,), elapsed_ms=1.0)

        assert result.source_reference_errors == (key,)
        assert result.condition_fields_missing == (key,)
        assert result.attribution_errors == (key,)
        assert not report.ok(min_recall=0.9, min_precision=0.7)

    def test_source_quote_reference_shape_is_checked_without_resolving_source(self):
        entry = {
            "user": "u1",
            "tier": "user",
            "key": "request/original",
            "index_text": "The exact instruction is available by source reference.",
            "metadata": {
                "evidence_quote_ref": {
                    "message_id": "synthetic-message-1",
                    "start": 0,
                    "end": 12,
                    "unit": "unicode_codepoint",
                }
            },
        }
        case = _case(
            entries=(entry,),
            source_refs_required=("user:request/original",),
        )
        result = runner.evaluate_case(
            case,
            _context(
                ("user", "request/original"),
                metadata={"request/original": entry["metadata"]},
            ),
        )
        assert not result.source_reference_errors
        assert not result.unsupported_checks

        malformed = {
            **entry,
            "metadata": {
                "evidence_quote_ref": {
                    "message_id": "",
                    "start": 0,
                    "end": 12,
                    "unit": "unicode_codepoint",
                }
            },
        }
        case = _case(
            entries=(malformed,),
            source_refs_required=("user:request/original",),
        )
        result = runner.evaluate_case(
            case,
            _context(
                ("user", "request/original"),
                provenance={
                    "request/original": {
                        "evidence_quote_ref": malformed["metadata"]["evidence_quote_ref"]
                    }
                },
                metadata={"request/original": malformed["metadata"]},
            ),
        )
        assert result.source_reference_errors == ("user:request/original",)
        assert not result.ok

    def test_conditional_fields_are_required_on_selected_rule(self):
        entry = {
            "user": "u1",
            "tier": "project",
            "key": "rule/retry",
            "index_text": "Retries use a bounded budget.",
            "metadata": {
                "applies_when": "A transient error occurs.",
                "exceptions": "Do not retry non-idempotent actions.",
            },
        }
        case = _case(
            entries=(entry,),
            required=("project:rule/retry",),
            relevant=("project:rule/retry",),
            condition_fields_required=("project:rule/retry",),
        )
        result = runner.evaluate_case(
            case,
            _context(
                ("project", "rule/retry"),
                metadata={"rule/retry": entry["metadata"]},
            ),
        )
        assert not result.condition_fields_missing
        assert not result.unsupported_checks

        entry["metadata"] = {"applies_when": "A transient error occurs."}
        case = _case(
            entries=(entry,),
            required=("project:rule/retry",),
            relevant=("project:rule/retry",),
            condition_fields_required=("project:rule/retry",),
        )
        result = runner.evaluate_case(
            case,
            _context(
                ("project", "rule/retry"),
                metadata={"rule/retry": entry["metadata"]},
            ),
        )
        assert result.condition_fields_missing == ("project:rule/retry",)
        assert not result.ok


    @pytest.mark.asyncio
    async def test_missing_selected_condition_metadata_fails_the_corpus_gate(self):
        case = _case(
            entries=(
                {
                    "user": "u1",
                    "tier": "project",
                    "key": "rule/retry",
                    "index_text": "Retries use a bounded budget.",
                },
            ),
            required=("project:rule/retry",),
            relevant=("project:rule/retry",),
            condition_fields_required=("project:rule/retry",),
        )

        report = await runner.run_cases((case,))

        assert report.unsupported_checks == (
            ("case", "project:rule/retry:conditional_metadata"),
        )
        assert not report.ok(min_recall=0.9, min_precision=0.7)

    def test_inference_origin_must_remain_non_evidence(self):
        case = _case(
            expected_origins={"user:prefs/lang": "assistant_inference"},
            must_not_count_as_evidence=("user:prefs/lang",),
        )
        inferred = ContextEntry(
            tier="user",
            entry_key="prefs/lang",
            index_text="Possible preference.",
            content_hash="inference",
            tokens=10,
            rank=0,
            origin="assistant_inference",
            counts_as_evidence=False,
        )
        result = runner.evaluate_case(
            case,
            MemoryContext(query="q", budget_tokens=100, entries=(inferred,)),
        )
        assert not result.attribution_errors

        misattributed = ContextEntry(
            tier="user",
            entry_key="prefs/lang",
            index_text="Possible preference.",
            content_hash="inference",
            tokens=10,
            rank=0,
            origin="assistant_inference",
            counts_as_evidence=True,
        )
        result = runner.evaluate_case(
            case,
            MemoryContext(query="q", budget_tokens=100, entries=(misattributed,)),
        )
        assert result.attribution_errors == ("user:prefs/lang",)
        assert not result.ok

    @pytest.mark.asyncio
    async def test_run_cases_records_selection_latency_per_case(self, monkeypatch):
        case = _case()
        clock_values = iter((0.0, 0.010, 0.035, 0.050))
        monkeypatch.setattr(runner.time, "perf_counter", lambda: next(clock_values))

        report = await runner.run_cases((case,))

        assert report.results[0].latency_ms == pytest.approx(25.0)
        assert report.elapsed_ms == pytest.approx(50.0)
        assert report.to_dict()["cases"][0]["latency_ms"] == 25.0


class TestCommittedCorpus:
    def test_committed_cases_pass_thresholds(self, capsys):
        assert runner.main([]) == 0
        out = capsys.readouterr().out
        assert "PASS" in out
        assert "mean recall 1.00" in out

    def test_json_report_written(self, tmp_path):
        out = tmp_path / "report.json"
        assert runner.main(["--json", str(out)]) == 0
        data = json.loads(out.read_text())
        assert data["ok"] is True
        assert len(data["cases"]) == 16
        assert all(case["latency_ms"] >= 0 for case in data["cases"])
        assert data["unsupported_checks"] == []
        assert data["ok"] is True
        assert data["mean_recall"] >= 0.9

    def test_missing_cases_file_fails(self, tmp_path, capsys):
        assert runner.main(["--cases", str(tmp_path / "nope.json")]) == 2
        assert "FAIL" in capsys.readouterr().err
