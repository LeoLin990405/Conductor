"""Completion claims are strict, sanitized, bounded and reconciled by facts."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from src.server.services.autonomy import completion
from src.server.services.autonomy.completion import (
    COMPLETION_ARTIFACT_FIELDS,
    COMPLETION_REPORT_FIELDS,
    COMPLETION_REPORT_MAX_BYTES,
    COMPLETION_REPORT_MAX_ITEM_CHARS,
    COMPLETION_REPORT_MAX_ITEMS,
    COMPLETION_REPORT_PROMPT,
    COMPLETION_REPORT_STATUSES,
    COMPLETION_VERIFICATION_FIELDS,
    completion_report_from_text,
    reconcile_completion,
)

SECRET = "ghp_" + "a1B2c3D4e5" * 4


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "done",
        "summary": "implemented the completion contract",
        "artifacts": {
            "commits": ["abc1234"],
            "files": ["src/server/services/autonomy/completion.py"],
            "pr": None,
        },
        "verification": [
            {
                "ran": "pytest tests/unit/.../test_completion_contract.py",
                "result": "passed",
            }
        ],
        "next": "send the Ticket to review",
        "handbacks": [],
    }
    payload.update(overrides)
    return payload


def _text(payload: dict[str, object] | None = None, **overrides: object) -> str:
    return json.dumps(payload or _payload(**overrides), ensure_ascii=False)


def test_a_matching_report_is_sanitized_and_kept_with_its_original_size() -> None:
    text = _text(summary=f"completed without using {SECRET}")
    report = completion_report_from_text(text)
    assert report is not None
    assert report.status == "done"
    assert report.summary == "[REDACTED]"
    assert report.artifacts.commits == ("abc1234",)
    assert report.artifacts.pr is None
    assert report.verification[0].ran.startswith("pytest ")
    assert report.handbacks == ()
    assert report.byte_count == len(text.encode("utf-8"))
    assert SECRET not in repr(report)


@pytest.mark.parametrize(
    "key", COMPLETION_REPORT_FIELDS, ids=lambda key: f"missing-{key}"
)
def test_every_top_level_key_is_required(key: str) -> None:
    payload = _payload()
    del payload[key]
    assert completion_report_from_text(_text(payload)) is None


@pytest.mark.parametrize(
    "payload",
    [
        _payload(extra="not allowed"),
        _payload(status="finished"),
        _payload(status=["done"]),
        _payload(status=None),
        _payload(summary=1),
        _payload(next=[]),
        _payload(artifacts="abc1234"),
        _payload(artifacts={"commits": [], "files": []}),
        _payload(artifacts={"commits": [], "files": [], "pr": None, "branch": "x"}),
        _payload(artifacts={"commits": "abc1234", "files": [], "pr": None}),
        _payload(artifacts={"commits": [], "files": {}, "pr": None}),
        _payload(artifacts={"commits": [], "files": [], "pr": 42}),
        _payload(verification={}),
        _payload(verification=[{"ran": "pytest"}]),
        _payload(verification=[{"ran": "pytest", "result": "passed", "exit": 0}]),
        _payload(verification=[{"ran": None, "result": "passed"}]),
        _payload(verification=[{"ran": "pytest", "result": True}]),
        _payload(handbacks="router"),
        _payload(handbacks=[{"ticket": "child"}]),
    ],
    ids=[
        "extra-top-level-key",
        "unknown-status",
        "list-status",
        "null-status",
        "numeric-summary",
        "list-next",
        "string-artifacts",
        "missing-artifact-key",
        "extra-artifact-key",
        "string-commits",
        "object-files",
        "numeric-pr",
        "object-verification",
        "missing-verification-key",
        "extra-verification-key",
        "null-ran",
        "boolean-result",
        "string-handbacks",
        "object-handback",
    ],
)
def test_any_schema_deviation_is_discarded_not_repaired(
    payload: dict[str, object],
) -> None:
    assert completion_report_from_text(_text(payload)) is None


@pytest.mark.parametrize("field", ["commits", "files", "handbacks", "verification"])
def test_list_item_limits_are_inclusive(field: str) -> None:
    if field == "verification":
        value = [{"ran": "pytest", "result": "passed"}] * COMPLETION_REPORT_MAX_ITEMS
        assert completion_report_from_text(_text(verification=value)) is not None
        assert (
            completion_report_from_text(_text(verification=[*value, value[0]])) is None
        )
        return
    value = ["x"] * COMPLETION_REPORT_MAX_ITEMS
    artifacts = {"commits": value, "files": value, "pr": None}
    kwargs = (
        {"artifacts": artifacts}
        if field in COMPLETION_ARTIFACT_FIELDS
        else {field: value}
    )
    assert completion_report_from_text(_text(**kwargs)) is not None
    value.append("x")
    assert completion_report_from_text(_text(**kwargs)) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"summary": "x" * (COMPLETION_REPORT_MAX_ITEM_CHARS + 1)},
        {"next": "x" * (COMPLETION_REPORT_MAX_ITEM_CHARS + 1)},
        {"artifacts": {"commits": ["x" * 201], "files": [], "pr": None}},
        {"artifacts": {"commits": [], "files": [], "pr": "x" * 201}},
        {"verification": [{"ran": "x" * 201, "result": "passed"}]},
        {"verification": [{"ran": "pytest", "result": "x" * 201}]},
        {"handbacks": ["x" * 201]},
    ],
    ids=["summary", "next", "commit", "pr", "ran", "result", "handback"],
)
def test_oversize_strings_are_discarded(overrides: dict[str, object]) -> None:
    assert completion_report_from_text(_text(**overrides)) is None


def test_string_item_limit_is_inclusive() -> None:
    text = "x" * COMPLETION_REPORT_MAX_ITEM_CHARS
    assert completion_report_from_text(_text(summary=text, next=text)) is not None


def test_oversize_utf8_input_is_rejected_before_sanitization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_run(value: object) -> str:
        raise AssertionError("sanitizer received an oversize report")

    monkeypatch.setattr(completion, "sanitize_report_text", must_not_run)
    text = _text(
        artifacts={
            "commits": ["é" * COMPLETION_REPORT_MAX_ITEM_CHARS]
            * COMPLETION_REPORT_MAX_ITEMS,
            "files": ["é" * COMPLETION_REPORT_MAX_ITEM_CHARS]
            * COMPLETION_REPORT_MAX_ITEMS,
            "pr": None,
        }
    )
    assert len(text.encode("utf-8")) > COMPLETION_REPORT_MAX_BYTES
    assert completion_report_from_text(text) is None


def test_unencodable_input_is_rejected_before_sanitization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_run(value: object) -> str:
        raise AssertionError("sanitizer received an unencodable report")

    monkeypatch.setattr(completion, "sanitize_report_text", must_not_run)
    text = _text(summary="\ud800")
    with pytest.raises(UnicodeEncodeError):
        text.encode("utf-8")
    assert completion_report_from_text(text) is None


@pytest.mark.parametrize(
    "verification", [[], "wrong type"], ids=["valid", "wrong-type"]
)
def test_unencodable_sanitized_output_is_discarded(verification: object) -> None:
    text = json.dumps(
        _payload(summary="\ud800", next=SECRET, verification=verification)
    )
    assert "\\ud800" in text
    assert completion_report_from_text(text) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"summary": "\ud800"},
        {"next": "\ud800"},
        {"artifacts": {"commits": ["\ud800"], "files": [], "pr": None}},
        {"artifacts": {"commits": [], "files": ["\ud800"], "pr": None}},
        {"artifacts": {"commits": [], "files": [], "pr": "\ud800"}},
        {"verification": [{"ran": "\ud800", "result": "passed"}]},
        {"verification": [{"ran": "pytest", "result": "\ud800"}]},
        {"handbacks": ["\ud800"]},
    ],
    ids=["summary", "next", "commit", "file", "pr", "ran", "result", "handback"],
)
def test_unencodable_decoded_strings_are_discarded(
    overrides: dict[str, object],
) -> None:
    text = json.dumps(_payload(**overrides))
    assert "\\ud800" in text
    assert completion.sanitize_report_text(text) == text
    assert completion_report_from_text(text) is None


def test_sanitized_output_also_has_a_size_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        completion,
        "sanitize_report_text",
        lambda value: str(value) + " " * COMPLETION_REPORT_MAX_BYTES,
    )
    assert completion_report_from_text(_text()) is None


@pytest.mark.parametrize(
    "text", ["", "not json", "[]", '"report"', "{", "```json\n{}\n```"]
)
def test_text_that_is_not_one_complete_object_is_discarded(text: str) -> None:
    assert completion_report_from_text(text) is None


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("done", "ready"),
        ("partial", "waiting"),
        ("blocked", "blocked"),
        ("needs_review", "waiting"),
        ("needs_split", "waiting"),
    ],
)
def test_status_maps_to_a_deterministic_ticket_state(status: str, state: str) -> None:
    report = completion_report_from_text(_text(status=status))
    assert report is not None
    assert reconcile_completion(report, git_delivered=True, tests_passed=True) == (
        completion.ReconciledCompletion(state, False, None)
    )


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("done", "dropped"),
        ("partial", "waiting"),
        ("blocked", "blocked"),
        ("needs_review", "waiting"),
        ("needs_split", "waiting"),
    ],
)
def test_speculative_status_map_never_completes_work(status: str, state: str) -> None:
    report = completion_report_from_text(_text(status=status))
    assert report is not None
    result = reconcile_completion(
        report, git_delivered=False, tests_passed=False, speculative=True
    )
    assert result == completion.ReconciledCompletion(state, False, None)


def test_missing_git_delivery_vetoes_ready_and_marks_disagreement() -> None:
    report = completion_report_from_text(_text(status="done"))
    assert report is not None
    result = reconcile_completion(report, git_delivered=False, tests_passed=True)
    assert result.state == "blocked"
    assert result.disagreement is True
    assert result.disagreement_reason == "git_not_delivered"


def test_failed_tests_veto_ready_and_mark_disagreement() -> None:
    report = completion_report_from_text(_text(status="done"))
    assert report is not None
    result = reconcile_completion(report, git_delivered=True, tests_passed=False)
    assert result.state == "blocked"
    assert result.disagreement is True
    assert result.disagreement_reason == "tests_failed"


@pytest.mark.parametrize("git_delivered", [True, None])
def test_checks_that_did_not_run_neither_confirm_nor_refute(
    git_delivered: bool | None,
) -> None:
    report = completion_report_from_text(_text(status="done", verification=[]))
    assert report is not None
    result = reconcile_completion(
        report, git_delivered=git_delivered, tests_passed=None
    )
    assert result == completion.ReconciledCompletion("ready", False, None)


@pytest.mark.parametrize("status", ["partial", "blocked", "needs_split", "needs_review"])
def test_system_checks_do_not_turn_an_incomplete_claim_into_ready(status: str) -> None:
    report = completion_report_from_text(
        _text(
            status=status,
            artifacts={"commits": [], "files": [], "pr": None},
            verification=[],
        )
    )
    assert report is not None
    expected = "blocked" if status == "blocked" else "waiting"
    result = reconcile_completion(report, git_delivered=False, tests_passed=False)
    assert result == completion.ReconciledCompletion(expected, False, None)


@pytest.mark.parametrize("status", ["partial", "needs_review"])
def test_refuted_artifact_claims_are_flagged_without_inventing_a_state(
    status: str,
) -> None:
    report = completion_report_from_text(_text(status=status))
    assert report is not None
    assert reconcile_completion(
        report, git_delivered=False, tests_passed=None
    ) == completion.ReconciledCompletion("waiting", True, "git_not_delivered")
    assert reconcile_completion(
        report, git_delivered=True, tests_passed=False
    ) == completion.ReconciledCompletion("waiting", True, "tests_failed")


@pytest.mark.parametrize(
    ("git_summary", "commit_delivery", "expected"),
    [
        ("", "delivered", True),
        ("", "missing", False),
        ("", "unknown", False),
        ("", "not_checked", None),
        (None, "not_checked", None),
        (None, "missing", False),
        ("Uncommitted git changes detected.", "delivered", False),
        ("Git workspace status is unknown because it could not be checked safely.", "delivered", False),
    ],
)
def test_git_delivery_verdict_combines_workspace_and_commit_checks(
    git_summary: str | None, commit_delivery: str, expected: bool | None
) -> None:
    assert (
        completion.git_delivered_from_checks(git_summary, commit_delivery) is expected
    )


def test_unknown_commit_delivery_verdict_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        completion.git_delivered_from_checks("", "maybe")


def test_done_without_listed_commits_is_still_vetoed_by_missing_delivery() -> None:
    """Regression: the no-delivery verdict must not depend on artifacts.commits."""
    text = _text(
        status="done",
        artifacts={"commits": [], "files": ["src/a.py"], "pr": None},
        verification=[],
    )
    outcome = completion.settle_native_completion(
        text, git_summary="", commit_delivery="missing"
    )
    assert outcome["state"] == "blocked"
    assert outcome["reconciled"] == completion.ReconciledCompletion(
        "blocked", True, "git_not_delivered"
    )
    assert outcome["question"] == completion.GIT_NOT_DELIVERED_QUESTION
    assert json.loads(outcome["body"])["disagreement"] is True


def test_settled_ready_report_carries_no_question_and_a_canonical_body() -> None:
    outcome = completion.settle_native_completion(
        _text(), git_summary="", commit_delivery="delivered"
    )
    assert outcome["state"] == "ready"
    assert outcome["question"] is None
    assert outcome["attention_reason"] is None
    body = json.loads(outcome["body"])
    assert body["status"] == "done"
    assert body["reconciled_state"] == "ready"
    assert body["disagreement"] is False


def test_settled_uncommitted_workspace_takes_the_shared_git_attention() -> None:
    git_summary = "Uncommitted git changes detected.\ngit status --short:\n M a.py"
    outcome = completion.settle_native_completion(
        _text(), git_summary=git_summary, commit_delivery="delivered"
    )
    assert outcome["state"] == "blocked"
    assert outcome["attention_reason"] == "uncommitted_changes"
    assert outcome["reconciled"].disagreement_reason == "git_not_delivered"
    assert outcome["body"].startswith(git_summary)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "Implemented and verified.",
        "Implemented.\nAUTONOMY_STATE: ready",
        "AUTONOMY_STATE: blocked\nAUTONOMY_QUESTION: which file?",
        "```json\n" + json.dumps(_payload()) + "\n```",
    ],
)
def test_missing_or_invalid_report_settles_as_waiting_never_success(
    text: str | None,
) -> None:
    outcome = completion.settle_native_completion(
        text, git_summary="", commit_delivery="delivered"
    )
    assert outcome["state"] == "waiting"
    assert outcome["attention_reason"] == completion.COMPLETION_REPORT_MISSING
    assert outcome["summary"] == completion.MISSING_REPORT_SUMMARY
    assert outcome["question"] == completion.MISSING_REPORT_QUESTION
    assert outcome["completion"] is None


def test_blocked_report_asks_its_next_question() -> None:
    outcome = completion.settle_native_completion(
        _text(status="blocked", next="Which branch should I use?")
    )
    assert outcome["state"] == "blocked"
    assert outcome["question"] == "Which branch should I use?"


def test_speculative_settlement_skips_git_checks() -> None:
    outcome = completion.settle_native_completion(
        _text(status="needs_review", summary="PLAN: patch the parser"),
        git_summary="Uncommitted git changes detected.",
        commit_delivery="missing",
        speculative=True,
    )
    assert outcome["state"] == "waiting"
    assert outcome["attention_reason"] is None
    assert outcome["summary"] == "PLAN: patch the parser"


def test_reconciled_completion_is_frozen() -> None:
    result = completion.ReconciledCompletion("ready", False, None)
    with pytest.raises(FrozenInstanceError):
        result.state = "blocked"  # type: ignore[misc]


def test_the_prompt_names_the_exact_schema_and_limits() -> None:
    for key in (
        *COMPLETION_REPORT_FIELDS,
        *COMPLETION_ARTIFACT_FIELDS,
        *COMPLETION_VERIFICATION_FIELDS,
    ):
        assert f'"{key}"' in COMPLETION_REPORT_PROMPT
    for status in COMPLETION_REPORT_STATUSES:
        assert f'"{status}"' in COMPLETION_REPORT_PROMPT
    assert "JSON object and nothing else" in COMPLETION_REPORT_PROMPT
    assert f"{COMPLETION_REPORT_MAX_ITEMS} items" in COMPLETION_REPORT_PROMPT
    assert f"{COMPLETION_REPORT_MAX_ITEM_CHARS} characters" in COMPLETION_REPORT_PROMPT
    assert f"{COMPLETION_REPORT_MAX_BYTES} bytes" in COMPLETION_REPORT_PROMPT


def test_module_has_no_database_or_dispatcher_imports() -> None:
    path = Path(completion.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = ("src.server.database", "src.server.services.projects")
    assert not [name for name in imported if name.startswith(forbidden)]
    assert imported == {
        "__future__",
        "json",
        "dataclasses",
        "typing",
        "src.llms.native_cli.worker_report",
        "src.server.utils.sanitize",
        "src.server.services.autonomy.git_delivery",
    }


def test_settling_a_report_loads_no_database_or_dispatcher_module() -> None:
    code = (
        "import json, sys\n"
        "from src.server.services.autonomy import completion\n"
        "completion.settle_native_completion(json.dumps({'status': 'done', "
        "'summary': 's', 'artifacts': {'commits': [], 'files': [], 'pr': None}, "
        "'verification': [], 'next': '', 'handbacks': []}), git_summary='x')\n"
        "print([m for m in sys.modules if m.startswith(("
        "'src.server.database', 'src.server.services.projects'))])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[5],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert result.stdout.strip().splitlines()[-1] == "[]"


def test_a_repeated_key_is_ambiguous_and_discarded() -> None:
    text = _text()
    repeated = text[:-1] + ', "status": "blocked"}'
    assert json.loads(repeated)["status"] == "blocked"
    assert completion_report_from_text(repeated) is None


def test_report_strings_cover_every_decoded_field() -> None:
    report = completion_report_from_text(
        _text(
            artifacts={"commits": ["c1"], "files": ["f1"], "pr": "p1"},
            verification=[{"ran": "r1", "result": "ok1"}],
            handbacks=["h1"],
            next="n1",
            summary="s1",
        )
    )
    assert report is not None
    assert sorted(completion.completion_report_strings(report)) == sorted(
        ["s1", "n1", "c1", "f1", "p1", "r1", "ok1", "h1"]
    )


def test_done_with_no_change_at_all_waits_for_a_decision() -> None:
    text = _text(
        status="done",
        artifacts={"commits": [], "files": [], "pr": None},
        verification=[],
    )
    outcome = completion.settle_native_completion(
        text, git_summary="", commit_delivery="missing", baseline_clean=True
    )
    assert outcome["state"] == "waiting"
    assert outcome["attention_reason"] == completion.COMPLETION_NO_CHANGE
    assert outcome["question"] == completion.NO_CHANGE_QUESTION
    assert "Commit the work" not in outcome["question"]
    assert json.loads(outcome["body"])["reconciled_state"] == "waiting"


@pytest.mark.parametrize(
    ("git_summary", "commit_delivery", "state"),
    [
        ("", "delivered", "ready"),
        ("", "unknown", "blocked"),
        ("Uncommitted git changes detected.", "missing", "blocked"),
        (None, "not_checked", "ready"),
    ],
)
def test_no_change_waiting_needs_a_clean_checked_workspace(
    git_summary: str | None, commit_delivery: str, state: str
) -> None:
    text = _text(
        status="done",
        artifacts={"commits": [], "files": [], "pr": None},
        verification=[],
    )
    outcome = completion.settle_native_completion(
        text,
        git_summary=git_summary,
        commit_delivery=commit_delivery,
        baseline_clean=True,
    )
    assert outcome["state"] == state
    assert outcome["attention_reason"] != completion.COMPLETION_NO_CHANGE


def test_no_change_needs_a_baseline_without_uncommitted_work() -> None:
    """Dirt an earlier run left inside a re-taken baseline must not be closed."""
    text = _text(
        status="done",
        artifacts={"commits": [], "files": [], "pr": None},
        verification=[],
    )
    outcome = completion.settle_native_completion(
        text, git_summary="", commit_delivery="missing", baseline_clean=False
    )
    assert outcome["state"] == "blocked"
    assert outcome["attention_reason"] != completion.COMPLETION_NO_CHANGE


def test_no_change_attention_reaches_the_ticket() -> None:
    from src.server.services.autonomy.host import worker_report_transition

    transition = worker_report_transition(
        ticket_id="t",
        ticket={"mandate": {}},
        state="waiting",
        summary="s",
        question=completion.NO_CHANGE_QUESTION,
        attention_reason=completion.COMPLETION_NO_CHANGE,
    )
    assert transition["attention"] == {
        "kind": "needs_action",
        "reason": completion.COMPLETION_NO_CHANGE,
        "question": completion.NO_CHANGE_QUESTION,
    }


@pytest.mark.parametrize(
    ("commits", "files", "state", "reason"),
    [
        ([], [], "waiting", "completion_unverified"),
        ([], ["src/a.py"], "waiting", "completion_unverified"),
        (["abc1234"], ["src/a.py"], "ready", None),
    ],
)
def test_an_unverified_commit_check_never_readies_a_done_without_commits(
    commits: list[str], files: list[str], state: str, reason: str | None
) -> None:
    """Regression: an unknown delivery origin must not let a bare done pass."""
    text = _text(
        status="done",
        artifacts={"commits": commits, "files": files, "pr": None},
        verification=[],
    )
    outcome = completion.settle_native_completion(
        text, git_summary="", commit_delivery="unverified", baseline_clean=True
    )
    assert outcome["state"] == state
    assert outcome["attention_reason"] == reason
    if reason:
        assert outcome["question"] == completion.UNVERIFIED_QUESTION


def test_an_unverified_check_keeps_a_non_ready_claim() -> None:
    outcome = completion.settle_native_completion(
        _text(status="partial"), git_summary="", commit_delivery="unverified"
    )
    assert outcome["state"] == "waiting"
    assert outcome["attention_reason"] is None
