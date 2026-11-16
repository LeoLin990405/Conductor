"""Dispatch/review de-duplication: keys, modes, ledger bookkeeping."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.tools.auto_dispatch import dedup

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def test_mode_defaults_to_shadow_and_reads_the_env():
    assert dedup.MODE_ENV == "CONDUCTOR_DISPATCH_DEDUP_MODE"
    assert dedup.dedup_mode({}) == "shadow"
    assert dedup.dedup_mode({dedup.MODE_ENV: "enforce"}) == "enforce"
    assert dedup.dedup_mode({dedup.MODE_ENV: "off"}) == "off"
    assert dedup.dedup_mode({dedup.MODE_ENV: " ENFORCE "}) == "shadow"


@pytest.mark.parametrize(
    "ref",
    ["#59", "issue:59", "Issue #59", "  issue 59 ", "ISSUE:#59", "#059"],
)
def test_bare_issue_spellings_share_one_key(ref):
    assert dedup.dispatch_key(ref, "L1", "ABCDEF1") == "dispatch|issue:59|l1|abcdef1"


@pytest.mark.parametrize(
    "ref", ["Owner/Repo#59", "owner/repo#59", "owner/repo/issues/59", "owner/repo:59"]
)
def test_repo_qualified_issue_spellings_share_one_key(ref):
    assert dedup.normalize_ref(ref) == "owner/repo#59"


def test_repo_issue_and_bare_issue_stay_distinct():
    assert dedup.normalize_ref("a/b#59") != dedup.normalize_ref("#59")


def test_ticket_refs_are_case_folded():
    upper = "T:AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
    assert dedup.normalize_ref(upper) == dedup.normalize_ref(
        "ticket:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )


@pytest.mark.parametrize(
    ("ref", "line", "commit"),
    [("", "l", "abc"), ("#1", " ", "abc"), ("#1", "l", ""), (None, "l", "abc")],
)
def test_empty_dispatch_parts_return_none(ref, line, commit):
    assert dedup.dispatch_key(ref, line, commit) is None


def test_review_key_normalises_and_none_on_empty():
    assert dedup.review_key(" Line-A ", " DEADBEEF ") == "review|line-a|deadbeef"
    assert dedup.review_key("", "abc") is None
    assert dedup.review_key("l", None) is None


def test_decide_none_key_is_always_new():
    decision = dedup.decide({}, None, mode="enforce")
    assert decision.action == "new" and not decision.would_reuse


def _ledger_with(state: str) -> tuple[dict, str]:
    ledger: dict = {}
    key = dedup.review_key("l", "abc1234")
    dedup.claim(ledger, key, "rev-1", now=NOW)
    if state != dedup.IN_FLIGHT:
        dedup.settle(ledger, key, state, now=NOW)
    return ledger, key


@pytest.mark.parametrize("state", [dedup.IN_FLIGHT, dedup.DONE])
def test_enforce_reuses_in_flight_and_done(state):
    ledger, key = _ledger_with(state)
    decision = dedup.decide(ledger, key, mode="enforce")
    assert decision.action == "reuse"
    assert decision.would_reuse and not decision.forced
    assert decision.existing["ref"] == "rev-1"
    assert decision.mode == "enforce"


def test_enforce_force_overrides():
    ledger, key = _ledger_with(dedup.DONE)
    decision = dedup.decide(ledger, key, mode="enforce", force=True)
    assert decision.action == "new" and decision.forced and not decision.would_reuse


def test_failed_entries_are_never_reused():
    ledger, key = _ledger_with(dedup.FAILED)
    for mode in ("enforce", "shadow"):
        decision = dedup.decide(ledger, key, mode=mode)
        assert decision.action == "new" and not decision.would_reuse


def test_shadow_proceeds_but_reports_would_reuse():
    ledger, key = _ledger_with(dedup.IN_FLIGHT)
    decision = dedup.decide(ledger, key, environ={})
    assert decision.mode == "shadow"
    assert decision.action == "new" and decision.would_reuse


def test_off_never_reuses_nor_records():
    ledger, key = _ledger_with(dedup.DONE)
    decision = dedup.decide(ledger, key, environ={dedup.MODE_ENV: "off"})
    assert decision.action == "new" and not decision.would_reuse and not decision.forced
    assert dedup.note_duplicate(ledger, key, decision=decision, now=NOW) is None
    assert ledger[key]["duplicates"] == 0


def test_decide_does_not_alias_the_ledger_entry():
    ledger, key = _ledger_with(dedup.DONE)
    decision = dedup.decide(ledger, key, mode="enforce")
    decision.existing["state"] = "mutated"
    assert ledger[key]["state"] == dedup.DONE


def test_claim_counts_attempts_and_settle_records_result():
    ledger: dict = {}
    key = dedup.dispatch_key("#59", "glm", "abc1234")
    first = dedup.claim(ledger, key, "d-1", now=NOW, meta={"worker": "glm"})
    assert first["state"] == dedup.IN_FLIGHT and first["attempts"] == 1
    dedup.settle(ledger, key, dedup.FAILED, now=NOW, result={"reason": "crash"})
    again = dedup.claim(ledger, key, "d-2", now=NOW + timedelta(minutes=1))
    assert again["attempts"] == 2 and again["ref"] == "d-2"
    assert again["result"] is None and again["meta"] == {"worker": "glm"}
    done = dedup.settle(ledger, key, dedup.DONE, now=NOW, result={"sha": "f00"})
    assert done["state"] == dedup.DONE and done["result"] == {"sha": "f00"}
    json.dumps(ledger)


def test_settle_unknown_key_and_bad_state():
    assert dedup.settle({}, "nope", dedup.DONE, now=NOW) is None
    with pytest.raises(ValueError):
        dedup.settle({}, "nope", dedup.IN_FLIGHT, now=NOW)
    with pytest.raises(ValueError):
        dedup.claim({}, "", "ref", now=NOW)


def test_note_duplicate_counts_shadow_enforce_and_forced():
    ledger, key = _ledger_with(dedup.IN_FLIGHT)
    shadow = dedup.decide(ledger, key, mode="shadow")
    dedup.note_duplicate(ledger, key, decision=shadow, now=NOW)
    enforce = dedup.decide(ledger, key, mode="enforce")
    dedup.note_duplicate(ledger, key, decision=enforce, now=NOW)
    forced = dedup.decide(ledger, key, mode="enforce", force=True)
    entry = dedup.note_duplicate(ledger, key, decision=forced, now=NOW)
    assert entry["duplicates"] == 3
    assert entry["forced"] == 1
    assert entry["reused"] == 1
    assert ledger[key]["last_duplicate_mode"] == "enforce"


def test_note_duplicate_ignores_non_duplicates():
    ledger, key = _ledger_with(dedup.FAILED)
    decision = dedup.decide(ledger, key, mode="enforce")
    assert dedup.note_duplicate(ledger, key, decision=decision, now=NOW) is None


def test_ledger_is_bounded_and_evicts_settled_first():
    ledger: dict = {}
    in_flight = dedup.review_key("old", "aaaaaaa")
    dedup.claim(ledger, in_flight, "keep", now=NOW - timedelta(days=1))
    for i in range(dedup.MAX_ENTRIES):
        key = dedup.review_key(f"l{i}", "bbbbbbb")
        dedup.claim(ledger, key, f"r{i}", now=NOW + timedelta(seconds=i))
        dedup.settle(ledger, key, dedup.DONE, now=NOW + timedelta(seconds=i))
    assert len(ledger) == dedup.MAX_ENTRIES
    # The oldest entry is in flight, so the oldest settled one went instead.
    assert in_flight in ledger
    assert dedup.review_key("l0", "bbbbbbb") not in ledger
    assert dedup.review_key(f"l{dedup.MAX_ENTRIES - 1}", "bbbbbbb") in ledger
