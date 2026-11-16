"""N60: completion is verified evidence on HEAD and write_scope; a claim is advisory."""

from __future__ import annotations

import json

from src.tools.auto_dispatch.completion_evidence import (
    COMPLETION_EVIDENCE,
    COMPLETION_MODE_ENV,
    WORKER_CLAIM,
    claim_from_output,
    note_claim,
    record_evidence,
    verify_completion,
)
from src.tools.auto_dispatch.machine import REVIEWED_HEADS

HEAD = "a" * 40
OTHER = "b" * 40


def _snap(result: str = "pass", revision: str = HEAD, codes=(0,)) -> dict:
    return {
        "gates": {
            "G2": {
                "result": result,
                "revision": revision,
                "commands": [{"index": i, "exit_code": c} for i, c in enumerate(codes)],
            }
        }
    }


def test_text_claims_are_read_from_the_tail() -> None:
    claim = claim_from_output("ran it\n98 passed in 3.2s\ncommitted abc1234")
    assert claim["source"] == "text"
    assert claim["tests_passed"] is True
    assert claim["passed_count"] == 98
    assert claim["says_committed"] is True
    assert claim_from_output("3 passed, 1 failed")["tests_passed"] is False
    assert claim_from_output("")["source"] == "none"
    snap: dict = {WORKER_CLAIM: {"old": True}}
    note_claim(snap, "", head=HEAD, start_head=OTHER, environ={})
    assert WORKER_CLAIM not in snap


def test_a_completion_report_wins_over_text() -> None:
    report = {
        "status": "done",
        "summary": "did it",
        "artifacts": {"commits": ["abc1234"], "files": ["a.py"], "pr": None},
        "verification": [{"ran": "pytest -q", "result": "passed"}],
        "next": "",
        "handbacks": [],
    }
    claim = claim_from_output("notes\n" + json.dumps(report))
    assert claim["source"] == "completion_report"
    assert claim["tests_passed"] is True
    assert claim["commits"] == ["abc1234"]


def test_n49_claim_without_a_new_commit_is_a_mismatch() -> None:
    snap: dict = {}
    claim = note_claim(snap, "98 passed", head=HEAD, start_head=HEAD, environ={})
    assert claim is snap[WORKER_CLAIM]
    assert claim["mismatch"] == ["claims_tests_passed_without_new_commit"]


def test_verified_needs_g2_on_head_zero_exits_and_diff_in_scope() -> None:
    entry = verify_completion(_snap(), head=HEAD, write_scope=["src/**"], changed=["src/a.py"], environ={})
    assert entry["verified"] is True and entry["failed"] == []
    assert entry["mode"] == "shadow"


def test_each_missing_piece_of_evidence_is_named() -> None:
    entry = verify_completion(
        _snap(revision=OTHER, codes=(0, 1)),
        head=HEAD,
        write_scope=["src/**"],
        changed=["src/a.py", "docs/x.md"],
        environ={},
    )
    assert entry["verified"] is False
    assert entry["failed"] == ["g2_not_on_head", "g2_nonzero_exit", "diff_outside_write_scope"]
    assert entry["outside_scope"] == ["docs/x.md"]
    unknown = verify_completion(_snap(), head=HEAD, write_scope=[], changed=None, environ={})
    assert unknown["failed"] == ["diff_unknown"]
    empty = verify_completion({}, head=HEAD, write_scope=[], changed=[], environ={})
    assert empty["failed"] == ["g2_missing", "g2_not_on_head", "g2_no_commands", "diff_empty"]


def test_a_claim_that_g2_refutes_is_recorded_as_a_mismatch() -> None:
    claim = {"source": "text", "tests_passed": True, "says_committed": True, "head": OTHER}
    entry = verify_completion(
        _snap(result="fail", codes=(1,)), head=HEAD, write_scope=[], changed=[],
        claim=claim, environ={},
    )
    assert entry["mismatch"] == [
        "claim_for_other_head",
        "claims_tests_passed_g2_disagrees",
        "claims_commit_diff_empty",
    ]
    assert "would_refuse" not in entry


def test_enforce_only_says_what_it_would_refuse() -> None:
    entry = verify_completion(
        _snap(result="fail"), head=HEAD, write_scope=[], changed=["a"],
        environ={COMPLETION_MODE_ENV: "enforce"},
    )
    assert entry["would_refuse"] == "completion_unverified"


def test_off_records_nothing() -> None:
    off = {COMPLETION_MODE_ENV: "off"}
    assert verify_completion(_snap(), head=HEAD, write_scope=[], changed=["a"], environ=off) is None
    snap: dict = {}
    assert note_claim(snap, "1 passed", head=HEAD, start_head=OTHER, environ=off) is None
    assert snap == {}


def test_the_reviewed_heads_ledger_gets_evidence_by_head_and_keeps_its_keys() -> None:
    ledger = {"complete": True, "heads": [HEAD]}
    root = {REVIEWED_HEADS: {"a": {"br": ledger}}}
    snap: dict = {}
    entry = verify_completion(_snap(), head=HEAD, write_scope=[], changed=["x"], environ={})
    snap[COMPLETION_EVIDENCE] = {"stale": True}
    record_evidence(root, snap, entry, line_id="a", branch="br", at="now")
    # Verified and no mismatch: the ledger only, and a stale flag is cleared.
    assert COMPLETION_EVIDENCE not in snap
    assert ledger["evidence"][HEAD]["at"] == "now"
    flagged = dict(entry, verified=False, failed=["g2_fail"])
    record_evidence(root, snap, flagged, line_id="a", branch="br", at="later")
    assert snap[COMPLETION_EVIDENCE]["failed"] == ["g2_fail"]
    record_evidence(root, snap, entry, line_id="a", branch="br", at="now")
    assert ledger["complete"] is True and ledger["heads"] == [HEAD]
    assert ledger["evidence"][HEAD]["verified"] is True
    for index in range(30):
        record_evidence(root, None, {"head": f"{index:040d}", "verified": False}, line_id="a", branch="br")
    assert len(ledger["evidence"]) == 20
    # No ledger for the branch: nothing is opened on the evidence's behalf.
    record_evidence(root, None, entry, line_id="b", branch="br")
    assert "b" not in root[REVIEWED_HEADS]
