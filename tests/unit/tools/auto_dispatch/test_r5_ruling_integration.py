"""R5 integration: fix-round rulings meet the run log, R5 L1 reports and N79.

Line-2 of dispatch d3205b52 checks a fix round's report against its ruling.
On main the report reaches the machine through the monitor's run log, which
may be a stream-json log, may echo the prompt, or may hold one R5 L1
completion-report object. These tests pin how each of those is read, that a
ruling-check BLOCK re-issues the same ruling instead of nesting it, that a
passing review settles the ruling, and that the LogMonitor hands the check a
tail long enough to hold every confirmation.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from src.tools.auto_dispatch.execution import REPORT_TAIL_CHARS, TAIL_CHARS, LogMonitor
from src.tools.auto_dispatch.machine import _fix_task, _remember_review
from src.server.services.autonomy.stage_gate import Handoff
from src.tools.auto_dispatch.rulings import (
    FIX_RULING,
    RULING_CHECK,
    build_ruling,
    check_confirmation,
    only_unconfirmed,
    reissue_ruling,
    unconfirmed_blockers,
)


def _ruling():
    return build_ruling("line-0", ["src/a.py: add the backoff"], fix_round=1)


def _confirm_all(ruling) -> str:
    return "\n".join(f"RULING {item.id}: confirmed - done it" for item in ruling.items)


def test_a_stream_json_log_confirms_from_inside_the_json_string() -> None:
    ruling = _ruling()
    event = {"type": "result", "result": "Summary of the fix.\n" + _confirm_all(ruling)}
    log = "{\"type\":\"system\"}\n" + json.dumps(event) + "\n"

    check = check_confirmation(log, ruling)

    assert check.complete
    assert set(check.confirmed) == {item.id for item in ruling.items}


def test_an_echoed_prompt_confirms_nothing() -> None:
    ruling = build_ruling("line-0", ["the verified flag is ignored"], fix_round=1)
    # The prompt's own lines: the bracketed items and the format example.
    echo = ruling.render()

    check = check_confirmation(echo, ruling)

    assert not check.complete
    assert set(check.missing) == {item.id for item in ruling.items}


def test_the_last_mention_of_an_item_wins() -> None:
    ruling = _ruling()
    report = "RULING M1: not confirmed yet\n" + _confirm_all(ruling)

    assert check_confirmation(report, ruling).complete


def test_a_completion_report_confirms_through_its_verification_rows() -> None:
    ruling = _ruling()
    report = json.dumps(
        {
            "status": "done",
            "summary": "Fixed the backoff.",
            "artifacts": {"commits": ["abc123"], "files": ["src/a.py"], "pr": None},
            "verification": [
                {"ran": f"RULING {item.id}", "result": "confirmed - checked"}
                for item in ruling.items
            ]
            + [{"ran": "pytest tests/unit", "result": "passed"}],
            "next": "",
            "handbacks": [],
        }
    )

    check = check_confirmation(report, ruling)

    assert check.complete


def test_a_completion_report_without_the_items_is_not_a_confirmation() -> None:
    ruling = _ruling()
    report = json.dumps(
        {
            "status": "done",
            "summary": "All done.",
            "artifacts": {"commits": ["abc123"], "files": [], "pr": None},
            "verification": [{"ran": "pytest", "result": "passed"}],
            "next": "",
            "handbacks": [],
        }
    )

    assert not check_confirmation(report, ruling).complete


def test_a_ruling_check_block_reissues_the_same_ruling() -> None:
    ruling = _ruling()
    blockers = unconfirmed_blockers(ruling, None)

    assert only_unconfirmed(blockers)
    assert not only_unconfirmed(["src/a.py: still broken", *blockers])
    again = reissue_ruling(ruling, fix_round=2)

    assert [item.text for item in again.items] == [item.text for item in ruling.items]
    assert again.fix_round == 2
    assert not any("not confirmed" in item.text for item in again.items)


def test_a_passing_review_settles_the_ruling() -> None:
    ruling = _ruling()
    snap: dict[str, object] = {FIX_RULING: ruling.to_dict(), RULING_CHECK: {"complete": True}}

    _remember_review(
        snap,
        round_n=1,
        gate="review",
        result="merge",
        reviewer="codex",
        blocker_count=0,
        reason="ok",
        at="now",
    )

    assert FIX_RULING not in snap
    assert RULING_CHECK not in snap


def test_a_blocking_review_keeps_the_ruling() -> None:
    ruling = _ruling()
    snap: dict[str, object] = {FIX_RULING: ruling.to_dict()}

    _remember_review(
        snap,
        round_n=1,
        gate="review",
        result="block",
        reviewer="codex",
        blocker_count=1,
        reason="blocked",
        at="now",
        blockers=["x"],
    )

    assert FIX_RULING in snap


def test_the_ruling_survives_the_n79_task_first_cut() -> None:
    ruling = _ruling()
    handoff = Handoff(
        branch="b", commit="c", files=(), test_command="t", results="r",
        known_limitations="none",
    )
    task = "x" * 200_000

    body = _fix_task(task, handoff, ["finding"], keep_blockers=True, ruling=ruling)

    assert "[task truncated]" in body
    assert "RULING M1: confirmed - <what you changed>" in body


def test_the_log_monitor_hands_the_check_a_long_report_tail(tmp_path: Path) -> None:
    run = "run-1"
    report = "\n".join(f"RULING M{index}: confirmed - item {index}" for index in range(1, 21))
    (tmp_path / f"{run}.log").write_text("noise\n" * 2_000 + report, encoding="utf-8")
    (tmp_path / f"{run}.exit").write_text("0", encoding="utf-8")

    status = asyncio.run(LogMonitor(tmp_path).poll(run))

    assert status.complete
    assert len(status.output) <= TAIL_CHARS
    assert status.report.endswith(report)
    assert len(status.report) <= REPORT_TAIL_CHARS


def test_a_must_not_is_confirmed_with_a_negation_in_its_body() -> None:
    ruling = _ruling()
    report = "\n".join(
        [
            "RULING M1: confirmed - added the backoff",
            "RULING N1: confirmed - I did not patch only the edge case",
            "RULING N2: confirmed - no check was weakened or skipped",
            "RULING N3: done, no tests were changed",
        ]
    )

    check = check_confirmation(report, ruling)

    assert check.complete, check.missing


def test_a_leading_negative_status_still_refutes() -> None:
    ruling = _ruling()
    report = "\n".join(
        [
            "RULING M1: not confirmed - ran out of time",
            "RULING N1: missing",
            "RULING N2: confirmed? not yet",
            "RULING N3: pending, fixed later",
        ]
    )

    check = check_confirmation(report, ruling)

    assert set(check.missing) == {"M1", "N1", "N2", "N3"}


def _completion_json(ruling) -> str:
    return json.dumps(
        {
            "status": "done",
            "summary": "Fixed.",
            "artifacts": {"commits": ["abc123"], "files": [], "pr": None},
            "verification": [
                {"ran": f"RULING {item.id}", "result": "confirmed - checked"}
                for item in ruling.items
            ],
            "next": "",
            "handbacks": [],
        }
    )


def test_a_completion_report_inside_a_log_tail_confirms() -> None:
    ruling = _ruling()
    plain = "codex exec\nprompt...\n" + _completion_json(ruling) + "\ntokens used: 1234\n"
    stream = json.dumps({"type": "result", "result": _completion_json(ruling)})

    assert check_confirmation(plain, ruling).complete
    assert check_confirmation(stream, ruling).complete


def test_an_echoed_prompt_inside_a_json_string_confirms_nothing() -> None:
    ruling = build_ruling("line-0", ['Fixed handler must log the id "x"'], fix_round=1)
    echo = json.dumps({"type": "user", "content": ruling.render()})

    assert set(check_confirmation(echo, ruling).missing) == {item.id for item in ruling.items}


def test_a_loose_opener_with_a_caveat_is_not_a_confirmation() -> None:
    ruling = _ruling()
    report = "\n".join(
        [
            "RULING M1: done except the edge case",
            "RULING N1: OK, but still pending",
            "RULING N2: implemented but tests failed",
            "RULING N3: confirmed - did not change tests",
        ]
    )

    assert set(check_confirmation(report, ruling).missing) == {"M1", "N1", "N2"}


def test_the_reviewer_prompt_carries_unconfirmed_rulings_within_budget() -> None:
    from src.tools.auto_dispatch.execution import CrossReviewer

    prompts: list[str] = []

    class _Backend:
        async def review(self, model: str, prompt: str) -> str:
            prompts.append(prompt)
            return '{"passed": true, "blockers": [], "verdict": "ok"}'

    notes = "Unconfirmed rulings: ...\n- RULING M1: not confirmed - add the backoff"
    reviewer = CrossReviewer(_Backend(), lambda line_id: "diff", max_prompt_bytes=100_000)
    asyncio.run(reviewer.review_line("line-0", "codex", notes=notes))
    assert "RULING M1: not confirmed" in prompts[-1]

    tight = CrossReviewer(_Backend(), lambda line_id: "diff", max_prompt_bytes=len(prompts[-1]) - 10)
    asyncio.run(tight.review_line("line-0", "codex", notes=notes))
    assert "RULING M1" not in prompts[-1]
