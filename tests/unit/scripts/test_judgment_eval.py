"""The judgment eval computes known rates on synthetic samples (N61 6.6)."""

from __future__ import annotations

import json

import pytest

from scripts.judgment_eval import evaluate, expected_calibration_error, main, percentile

USE = "completion_evidence"


def _rec(subject, status, answer, p, latency, *, reason="", usage=None, use=USE, qs=None):
    return {
        "use": use,
        "subject": subject,
        "status": status,
        "reason": reason,
        "question_status": {"ok": qs or status},
        "answers": {"ok": answer},
        "probabilities": {} if p is None else {"ok": {"true": p}},
        "latency_ms": latency,
        "usage": usage,
    }


RECORDS = [
    _rec("a", "pass", True, 0.9, 10, usage={"total_tokens": 100}),
    _rec("b", "pass", True, 0.9, 20),
    _rec("c", "fail", False, 0.1, 30),
    _rec("d", "fail", False, 0.1, 40),
    _rec("e", "inconclusive", None, 0.5, 50),
    _rec("f", "error", None, None, 60, reason="timeout"),
    _rec("g", "inconclusive", None, None, 0, reason="mode_off"),
    _rec("", "pass", True, 0.9, 70),  # no join key: unlabelled
]
SKIPS = [
    _rec("h", "inconclusive", None, None, 0, reason="no_user"),
    _rec("i", "inconclusive", None, None, 0, reason="not_sampled"),
    _rec("j", "inconclusive", None, None, 0, reason="mode_off"),
]
OUTCOMES = [
    {"use": USE, "subject": s, "truth": {"ok": t}}
    for s, t in [("a", True), ("b", False), ("c", "true"), ("d", False), ("e", True), ("f", True), ("g", True)]
]


def test_known_rates_on_synthetic_samples():
    m = evaluate(RECORDS, OUTCOMES)[USE]
    assert (m["records"], m["skipped"], m["judged"]) == (8, 1, 7)
    assert (m["pass"], m["fail"], m["abstained"], m["error"]) == (3, 2, 1, 1)
    assert m["false_pass_rate"] == 0.5 and m["false_pass"] == 1
    assert m["false_block_rate"] == 0.5 and m["false_block"] == 1
    assert m["inconclusive_rate"] == pytest.approx(1 / 7)
    assert m["error_rate"] == pytest.approx(1 / 7)
    assert (m["latency_p50_ms"], m["latency_p95_ms"]) == (40, 70)
    assert (m["llm_calls_saveable"], m["llm_calls_saveable_correct"]) == (3, 1)
    # bins: four answers at confidence 0.9 with accuracy 0.5, one at 0.5 wrong
    assert m["ece_samples"] == 5
    assert m["ece"] == pytest.approx(0.8 * 0.4 + 0.2 * 0.5)


def test_missing_usage_is_never_zero_cost():
    m = evaluate(RECORDS, OUTCOMES)[USE]
    assert m["usage_missing"] == 6
    assert m["judgment_tokens"] is None
    full = evaluate([_rec("a", "pass", True, 0.9, 5, usage={"total_tokens": 7})], [])[USE]
    assert full["judgment_tokens"] == 7 and full["usage_missing"] == 0


def test_choice_false_pass_and_perfect_calibration():
    def choice(subject, answer, conf):
        return {
            "use": "router_preselect", "subject": subject, "status": "pass",
            "question_status": {"pick": "pass"}, "answers": {"pick": answer},
            "probabilities": {"pick": {answer: conf, "other": 1 - conf}}, "latency_ms": 1,
        }

    records = [choice("1", "glm", 1.0), choice("2", "kimi", 1.0)]
    outcomes = [
        {"use": "router_preselect", "subject": "1", "truth": {"pick": "glm"}},
        {"use": "router_preselect", "subject": "2", "truth": {"pick": "kimi"}},
    ]
    m = evaluate(records, outcomes)["router_preselect"]
    assert m["false_pass_rate"] == 0.0 and m["ece"] == 0.0
    assert m["false_block_rate"] is None  # nothing blocked: no rate, not zero
    outcomes[1]["truth"]["pick"] = "grok"
    m = evaluate(records, outcomes)["router_preselect"]
    assert m["false_pass_rate"] == 0.5 and m["ece"] == pytest.approx(0.5)


def test_helpers():
    assert percentile([], 50) is None
    assert percentile([3, 1, 2], 50) == 2
    assert expected_calibration_error([]) is None
    assert expected_calibration_error([(1.0, True), (0.05, False)]) == pytest.approx(0.025)


def test_main_reads_jsonl_and_json_list(tmp_path, capsys):
    records = tmp_path / "records.jsonl"
    records.write_text("\n".join(json.dumps(r) for r in RECORDS) + "\nnot json\n")
    outcomes = tmp_path / "outcomes.json"
    outcomes.write_text(json.dumps(OUTCOMES))
    before = records.read_text()
    assert main(["--records", str(records), "--outcomes", str(outcomes)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["unreadable_records"] == 1
    assert report["uses"][USE]["false_pass_rate"] == 0.5
    assert records.read_text() == before  # read-only


def test_records_written_by_the_port_evaluate(tmp_path):
    import asyncio

    from src.server.services.judgment import (
        BoolQuestion, JsonlRecorder, JudgmentPort, JudgmentRequest,
    )

    class Backend:
        name = "fake"

        async def answer(self, request):
            return {"ok": {"true": 0.95}}

    path = tmp_path / "r.jsonl"
    env = {"CONDUCTOR_JUDGMENT_COMPLETION_EVIDENCE_SAMPLE": "1"}
    port = JudgmentPort(Backend(), recorder=JsonlRecorder(path), environ=env)
    req = JudgmentRequest(USE, "1", {"x": "y"}, {"ok": BoolQuestion("ok?")}, user_id="u1")

    async def run():
        await port.judge(req, subject="L1")
        await port.judge(
            JudgmentRequest(USE, "1", {"x": "y"}, {"ok": BoolQuestion("ok?")}), subject="L2"
        )
        await port.drain()

    asyncio.run(run())
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["reason"] for row in rows] == ["", "no_user"]
    m = evaluate(rows, [{"use": USE, "subject": "L1", "truth": {"ok": False}}])[USE]
    assert m["false_pass_rate"] == 1.0 and m["judgment_tokens"] is None
    assert (m["skipped"], m["judged"], m["error"]) == (1, 1, 0)


def test_skip_reasons_are_skipped_not_error_or_judged():
    m = evaluate(RECORDS + SKIPS, OUTCOMES)[USE]
    assert (m["records"], m["skipped"], m["judged"]) == (11, 4, 7)
    assert (m["error"], m["abstained"]) == (1, 1)
    assert m["usage_missing"] == 6  # skips never count as missing usage
