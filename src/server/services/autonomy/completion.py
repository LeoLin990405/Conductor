"""Strict, sanitized completion claims and deterministic Ticket reconciliation.

A native Worker cannot call the report tool, so its final assistant message is
one JSON object: the completion report. The object reuses the self-report
field name ``next`` and the limits of ``src.llms.native_cli.worker_report`` and
adds the completion contract: status, summary, artifacts, verification and
handbacks.

Validation is strict and ordered: the size bound, then ``sanitize_report_text``,
then the schema. A payload that fails any step is discarded, never repaired.

A Worker's report is a claim, not delivery evidence. Git delivery and test
checks are facts the Worker does not own: this module reconciles the claim with
the facts its caller supplies, the facts win, and a disagreement is flagged.
Persistence, git inspection, test execution and Ticket writes stay outside this
boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.llms.native_cli.worker_report import (
    WORKER_REPORT_MAX_BYTES,
    WORKER_REPORT_MAX_ITEM_CHARS,
    WORKER_REPORT_MAX_ITEMS,
)
from src.server.utils.sanitize import sanitize_report_text

COMPLETION_REPORT_FIELDS = (
    "status",
    "summary",
    "artifacts",
    "verification",
    "next",
    "handbacks",
)
COMPLETION_ARTIFACT_FIELDS = ("commits", "files", "pr")
COMPLETION_VERIFICATION_FIELDS = ("ran", "result")
COMPLETION_REPORT_STATUSES = frozenset(
    {"done", "partial", "blocked", "needs_review", "needs_split"}
)
COMPLETION_REPORT_MAX_ITEMS = WORKER_REPORT_MAX_ITEMS
COMPLETION_REPORT_MAX_ITEM_CHARS = WORKER_REPORT_MAX_ITEM_CHARS
COMPLETION_REPORT_MAX_BYTES = WORKER_REPORT_MAX_BYTES

#: The deterministic decision table from a claimed status to a Worker state.
#: Only ``done`` can complete a Ticket; a review request waits for a person.
COMPLETION_STATUS_TO_TICKET_STATE = {
    "done": "ready",
    "partial": "waiting",
    "blocked": "blocked",
    "needs_review": "waiting",
    "needs_split": "waiting",
}
#: A speculative plan run never completes work: a plan waits for approval and
#: ``done`` means nothing needs doing.
SPECULATIVE_STATUS_TO_TICKET_STATE = {
    "done": "dropped",
    "partial": "waiting",
    "blocked": "blocked",
    "needs_review": "waiting",
    "needs_split": "waiting",
}

#: The values a caller passes as ``commit_delivery`` (see git_delivery).
#: ``unverified``: a Git checkout was checked but the Ticket's delivery origin
#: is unknown, so an earlier run's commits cannot be told from this one's.
COMMIT_DELIVERY_VERDICTS = (
    "delivered",
    "missing",
    "unknown",
    "not_checked",
    "unverified",
)

COMPLETION_REPORT_MISSING = "completion_report_missing"
COMPLETION_NO_CHANGE = "completion_no_change"
COMPLETION_UNVERIFIED = "completion_unverified"
UNVERIFIED_SUMMARY_SUFFIX = "System check: delivery could not be verified."
UNVERIFIED_QUESTION = (
    "The Worker reported done without listing a commit, and the system could "
    "not verify delivery for this Ticket. Review the run, then close or retry."
)
NO_CHANGE_SUMMARY_SUFFIX = "System check: no commit and no changed file."
NO_CHANGE_QUESTION = (
    "The Worker reported done without committing or changing anything. "
    "Review the run, then close the Ticket or retry it."
)
MISSING_REPORT_SUMMARY = "No valid completion report was received."
MISSING_REPORT_QUESTION = (
    "No valid completion report was received. "
    "Review the raw run, then reply or retry."
)
GIT_NOT_DELIVERED_QUESTION = (
    "The system could not confirm a delivered commit for this Ticket. "
    "Commit the work, then retry."
)
TESTS_FAILED_QUESTION = "The system test check failed. Fix the tests, then retry."

_DISAGREEMENT_TEXT = {
    "git_not_delivered": "System check: Git delivery was not confirmed.",
    "tests_failed": "System check: tests failed.",
}
_PASSING_RESULTS = frozenset({"pass", "passed", "ok", "success", "succeeded"})


def completion_report_prompt(*, speculative: bool = False) -> str:
    """Closing instructions that name the exact schema and its limits."""
    schema = (
        "Reply with one JSON object and nothing else: no prose and no code fence. "
        'Use exactly these keys: "status", "summary", "artifacts", "verification", '
        '"next", "handbacks". Status is one of "done", "partial", "blocked", '
        '"needs_review", "needs_split". Artifacts is an object with exactly '
        '"commits", "files", "pr"; commits and files are lists of strings, and pr '
        "is a string or null. Verification is a list of objects with exactly "
        '"ran", "result", both strings. Next is a string and handbacks is a list of '
        f"strings. Each list has at most {COMPLETION_REPORT_MAX_ITEMS} items, each "
        f"string is at most {COMPLETION_REPORT_MAX_ITEM_CHARS} characters, and the "
        f"whole object is at most {COMPLETION_REPORT_MAX_BYTES} bytes. Do not include "
        "credentials, tokens or file contents. For that final reporting step, do not "
        "run tools or change files."
    )
    if speculative:
        return (
            "This is a SPECULATIVE PLAN run. Do not modify files, run mutating "
            "commands, or push. Investigate with read-only evidence. If you confirm "
            "the issue and find a simple, actionable fix, end with status "
            '"needs_review" and a summary that starts with "PLAN: " for a person to '
            'approve. When no work is needed, end with status "done" and stay quiet. '
            "Your final message is the completion report. " + schema
        )
    return (
        "Because this Worker runs a vendor CLI with no autonomy report tool, your "
        "final message is the completion report, sent after completing and "
        'verifying the Ticket. Use "done" only when the work is finished and '
        'committed, "partial" when it is unfinished, "blocked" when a person must '
        'answer the question you put in "next", "needs_review" when a person must '
        'review the result, and "needs_split" when part of the work should become '
        'another Ticket named in "handbacks". The system checks Git delivery after '
        "this report; when the checks disagree with it, the checks decide. " + schema
    )


#: The one completion turn sent to a Worker. It names the exact schema and limits.
COMPLETION_REPORT_PROMPT = completion_report_prompt()


@dataclass(frozen=True)
class CompletionArtifacts:
    commits: tuple[str, ...]
    files: tuple[str, ...]
    pr: str | None


@dataclass(frozen=True)
class CompletionVerification:
    ran: str
    result: str


@dataclass(frozen=True)
class CompletionReport:
    """A sanitized self-report that matched the exact schema."""

    status: str
    summary: str
    artifacts: CompletionArtifacts
    verification: tuple[CompletionVerification, ...]
    next: str
    handbacks: tuple[str, ...]
    byte_count: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "artifacts": {
                "commits": list(self.artifacts.commits),
                "files": list(self.artifacts.files),
                "pr": self.artifacts.pr,
            },
            "verification": [
                {"ran": item.ran, "result": item.result} for item in self.verification
            ],
            "next": self.next,
            "handbacks": list(self.handbacks),
        }


@dataclass(frozen=True)
class ReconciledCompletion:
    state: str
    disagreement: bool
    disagreement_reason: str | None


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object whose keys repeat is ambiguous; reject it, never pick one."""
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key in completion report")
    return dict(pairs)


def _bounded_string(value: object) -> bool:
    if not isinstance(value, str) or len(value) > COMPLETION_REPORT_MAX_ITEM_CHARS:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _string_list(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or len(value) > COMPLETION_REPORT_MAX_ITEMS:
        return None
    if not all(_bounded_string(item) for item in value):
        return None
    return tuple(value)


def _artifacts_from_payload(value: object) -> CompletionArtifacts | None:
    if not isinstance(value, dict) or set(value) != set(COMPLETION_ARTIFACT_FIELDS):
        return None
    commits = _string_list(value["commits"])
    files = _string_list(value["files"])
    pr = value["pr"]
    if commits is None or files is None or pr is not None and not _bounded_string(pr):
        return None
    return CompletionArtifacts(commits=commits, files=files, pr=pr)


def _verification_from_payload(
    value: object,
) -> tuple[CompletionVerification, ...] | None:
    if not isinstance(value, list) or len(value) > COMPLETION_REPORT_MAX_ITEMS:
        return None
    entries: list[CompletionVerification] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != set(
            COMPLETION_VERIFICATION_FIELDS
        ):
            return None
        ran = item["ran"]
        result = item["result"]
        if not _bounded_string(ran) or not _bounded_string(result):
            return None
        entries.append(CompletionVerification(ran=ran, result=result))
    return tuple(entries)


def completion_report_from_text(text: str | None) -> CompletionReport | None:
    """Return exactly one valid sanitized report, or None without repairing it."""
    if not isinstance(text, str):
        return None
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if size > COMPLETION_REPORT_MAX_BYTES:
        return None
    # The sanitizer re-serializes JSON and would silently keep the last of a
    # repeated key, so an ambiguous object is refused before it runs.
    try:
        json.loads(text, object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError):
        return None
    cleaned = sanitize_report_text(text)
    try:
        cleaned_size = len(cleaned.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if cleaned_size > COMPLETION_REPORT_MAX_BYTES:
        return None
    try:
        payload = json.loads(cleaned, object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or set(payload) != set(COMPLETION_REPORT_FIELDS):
        return None

    status = payload["status"]
    summary = payload["summary"]
    next_step = payload["next"]
    if not isinstance(status, str) or status not in COMPLETION_REPORT_STATUSES:
        return None
    if not _bounded_string(summary) or not _bounded_string(next_step):
        return None
    artifacts = _artifacts_from_payload(payload["artifacts"])
    verification = _verification_from_payload(payload["verification"])
    handbacks = _string_list(payload["handbacks"])
    if artifacts is None or verification is None or handbacks is None:
        return None
    return CompletionReport(
        status=status,
        summary=summary,
        artifacts=artifacts,
        verification=verification,
        next=next_step,
        handbacks=handbacks,
        byte_count=size,
    )


def git_delivered_from_checks(
    git_summary: str | None, commit_delivery: str = "not_checked"
) -> bool | None:
    """The system's Git delivery verdict: True, False, or None when unchecked.

    ``git_summary`` is the shared workspace check (empty when clean, text for
    uncommitted or unknown state, None when nothing was checked) and
    ``commit_delivery`` says whether a new commit exists since the baseline.
    Either refuting fact is a no-delivery verdict, whatever the Worker lists.
    """
    if commit_delivery not in COMMIT_DELIVERY_VERDICTS:
        raise ValueError(f"unknown commit delivery verdict: {commit_delivery!r}")
    if git_summary:
        return False
    if commit_delivery in {"missing", "unknown"}:
        return False
    if commit_delivery == "delivered":
        return True
    return None


def _claims_passing_tests(report: CompletionReport) -> bool:
    return any(
        item.result.strip().lower() in _PASSING_RESULTS for item in report.verification
    )


def reconcile_completion(
    report: CompletionReport,
    *,
    git_delivered: bool | None,
    tests_passed: bool | None,
    speculative: bool = False,
) -> ReconciledCompletion:
    """Apply system facts to the status mapping without inventing completion.

    ``None`` means a check did not run; it neither confirms nor refutes the
    claim. A refuting check vetoes a ready claim (the Ticket is blocked) and is
    flagged as a disagreement. It is also flagged, without a state change, when
    the Worker lists commits or passing tests the check refutes. Checks cannot
    prove that a partial, blocked, review or split request is complete.
    """
    if speculative:
        return ReconciledCompletion(
            SPECULATIVE_STATUS_TO_TICKET_STATE[report.status], False, None
        )
    mapped_state = COMPLETION_STATUS_TO_TICKET_STATE[report.status]
    claims_ready = mapped_state == "ready"
    reason = None
    if git_delivered is False and (claims_ready or report.artifacts.commits):
        reason = "git_not_delivered"
    elif tests_passed is False and (claims_ready or _claims_passing_tests(report)):
        reason = "tests_failed"
    if reason is None:
        return ReconciledCompletion(mapped_state, False, None)
    return ReconciledCompletion("blocked" if claims_ready else mapped_state, True, reason)


def completion_question(
    report: CompletionReport, reconciled: ReconciledCompletion
) -> str | None:
    """The one question a non-ready outcome shows; ready never carries one."""
    if reconciled.state not in {"blocked", "waiting"}:
        return None
    if reconciled.disagreement and reconciled.state == "blocked" and (
        COMPLETION_STATUS_TO_TICKET_STATE[report.status] == "ready"
    ):
        if reconciled.disagreement_reason == "git_not_delivered":
            return GIT_NOT_DELIVERED_QUESTION
        return TESTS_FAILED_QUESTION
    if reconciled.state == "blocked":
        return report.next or report.summary
    return None


def completion_summary(
    report: CompletionReport, reconciled: ReconciledCompletion
) -> str:
    text = _DISAGREEMENT_TEXT.get(reconciled.disagreement_reason or "")
    return f"{report.summary} {text}".strip() if text else report.summary


def completion_report_body(
    report: CompletionReport, reconciled: ReconciledCompletion
) -> str:
    """The stored report body: the validated claim and the system's verdict."""
    payload = report.to_payload()
    payload["reconciled_state"] = reconciled.state
    payload["disagreement"] = reconciled.disagreement
    payload["disagreement_reason"] = reconciled.disagreement_reason
    return sanitize_report_text(json.dumps(payload, ensure_ascii=False), limit=0)


def completion_report_strings(report: CompletionReport) -> list[str]:
    """Every decoded string of a report, for callers that screen reported text."""
    values = [report.summary, report.next, *report.artifacts.commits]
    values.extend(report.artifacts.files)
    if report.artifacts.pr is not None:
        values.append(report.artifacts.pr)
    for item in report.verification:
        values.extend((item.ran, item.result))
    values.extend(report.handbacks)
    return [value for value in values if value]


def settle_native_completion(
    text: str | None,
    *,
    git_summary: str | None = None,
    commit_delivery: str = "not_checked",
    tests_passed: bool | None = None,
    speculative: bool = False,
    raw_text: str | None = None,
    baseline_clean: bool = False,
) -> dict[str, Any]:
    """Settle a successful native turn from its final message and the checks.

    Shared by the server executor, the Docker runtime and the local daemon. A
    missing or invalid report settles as ``waiting`` with the
    ``completion_report_missing`` attention; it is never read as success.
    The returned dict has ``state``, ``summary``, ``question``, ``body``,
    ``attention_reason``, ``completion`` and ``reconciled``. ``raw_text`` is
    the whole turn output when ``text`` is only its final message; a missing
    report keeps it as the body a person reviews. ``baseline_clean`` says the
    run started from a workspace with no uncommitted work, so an unchanged
    clean workspace really holds nothing undelivered.
    """
    from src.server.services.autonomy.git_delivery import apply_git_delivery_guard

    report = completion_report_from_text(text)
    if report is None:
        outcome: dict[str, Any] = {
            "state": "waiting",
            "summary": MISSING_REPORT_SUMMARY,
            "question": MISSING_REPORT_QUESTION,
            "body": raw_text
            if isinstance(raw_text, str)
            else text
            if isinstance(text, str)
            else "",
            "attention_reason": COMPLETION_REPORT_MISSING,
            "completion": None,
            "reconciled": None,
        }
        if git_summary and not speculative:
            # The guard leaves a waiting state alone and only prefixes the body.
            apply_git_delivery_guard(outcome, git_summary)
        return outcome
    if speculative:
        git_delivered = None
    else:
        git_delivered = git_delivered_from_checks(git_summary, commit_delivery)
    reconciled = reconcile_completion(
        report,
        git_delivered=git_delivered,
        tests_passed=tests_passed,
        speculative=speculative,
    )
    if (
        not speculative
        and report.status == "done"
        and not report.artifacts.commits
        and not report.artifacts.files
        and git_summary == ""
        and commit_delivery == "missing"
        and baseline_clean
    ):
        # Done with nothing to deliver: the Worker claims no change and the
        # checks agree. That may be right (no change was needed) or not, so a
        # person or the Router decides; it is neither ready nor blocked.
        reconciled = ReconciledCompletion("waiting", False, None)
        return {
            "state": "waiting",
            "summary": f"{report.summary} {NO_CHANGE_SUMMARY_SUFFIX}".strip(),
            "question": NO_CHANGE_QUESTION,
            "body": completion_report_body(report, reconciled),
            "attention_reason": COMPLETION_NO_CHANGE,
            "completion": report,
            "reconciled": reconciled,
        }
    if (
        not speculative
        and reconciled.state == "ready"
        and report.status == "done"
        and not report.artifacts.commits
        and commit_delivery == "unverified"
    ):
        # Done, no commit listed, and the check cannot tell whether one was
        # made: a person or the Router decides. A claim that lists commits
        # keeps the workspace-only verdict.
        reconciled = ReconciledCompletion("waiting", False, None)
        return {
            "state": "waiting",
            "summary": f"{report.summary} {UNVERIFIED_SUMMARY_SUFFIX}".strip(),
            "question": UNVERIFIED_QUESTION,
            "body": completion_report_body(report, reconciled),
            "attention_reason": COMPLETION_UNVERIFIED,
            "completion": report,
            "reconciled": reconciled,
        }
    outcome = {
        "state": reconciled.state,
        "summary": completion_summary(report, reconciled),
        "question": completion_question(report, reconciled),
        "body": completion_report_body(report, reconciled),
        "attention_reason": None,
        "completion": report,
        "reconciled": reconciled,
    }
    if git_summary and not speculative:
        claimed = COMPLETION_STATUS_TO_TICKET_STATE[report.status]
        guarded = apply_git_delivery_guard(
            {
                # A ready claim refuted by workspace state takes the shared
                # uncommitted/unknown attention and wording instead of the
                # generic delivery question.
                "state": "ready" if claimed == "ready" else reconciled.state,
                "summary": report.summary,
                "body": outcome["body"],
                "question": outcome["question"],
            },
            git_summary,
        )
        outcome["body"] = guarded["body"]
        if guarded.get("attention_reason"):
            outcome["attention_reason"] = guarded["attention_reason"]
            outcome["question"] = guarded["question"]
            outcome["summary"] = guarded["summary"]
    return outcome
