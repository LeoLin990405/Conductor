"""The failure classifier: a fixed vocabulary, a fixed priority, and no diagnostic.

The dispatcher branches on the class names and nothing else, and the
diagnostic must not travel with the answer: a Worker's error text carries
API keys, account ids and e-mail addresses. So these tests pin the
vocabulary, the order between the classes, the Retry-After cap, and -- the
one that matters most -- that a sentinel planted in the diagnostic appears
nowhere in the result, in its ``repr``, or in the log the module writes.
``timeout`` is the local process and read deadline; transport errors stay
``network``. The split itself is pinned in ``test_failure_timeout_class``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from src.llms.native_cli.failure import (
    FailureClass,
    FailureClassification,
    classify_failure,
    failure_rules,
    register_failure_rule,
)

REPO_ROOT = Path(__file__).resolve().parents[4]

# The token the PRD's acceptance asks for: it stands in for whatever the
# provider leaked into its error text.
SENTINEL = "sentinel-secret-token-abc123"

# One diagnostic per class, chosen so that it matches exactly one rule.
REACHABLE = [
    (FailureClass.RATE_LIMITED, "HTTP 429 Too Many Requests", None),
    (FailureClass.OVERLOADED, "HTTP 503 Service Unavailable", None),
    (FailureClass.NETWORK, "connect ECONNREFUSED 127.0.0.1:443", None),
    (FailureClass.QUOTA_EXHAUSTED, "insufficient_quota: no credit left", None),
    (FailureClass.AUTH_FAILED, "Error: not logged in. Run the login flow.", None),
    (FailureClass.CONTEXT_EXHAUSTED, "maximum context length exceeded", None),
    (FailureClass.CRASH, "the worker stopped unexpectedly", 1),
    (FailureClass.UNKNOWN, "something happened", None),
]

# Two classes in one diagnostic; the PRD's priority decides which one wins.
PRIORITY = [
    (
        FailureClass.AUTH_FAILED,
        "not logged in; insufficient_quota",
    ),
    (
        FailureClass.QUOTA_EXHAUSTED,
        "insufficient_quota; maximum context length exceeded",
    ),
    (
        FailureClass.CONTEXT_EXHAUSTED,
        "maximum context length exceeded; HTTP 429 Too Many Requests",
    ),
    (
        FailureClass.RATE_LIMITED,
        "HTTP 429 Too Many Requests; HTTP 503 Service Unavailable",
    ),
    (
        FailureClass.OVERLOADED,
        "HTTP 503 Service Unavailable; connection refused",
    ),
    (
        FailureClass.NETWORK,
        "connection refused by peer",
    ),
    (
        FailureClass.AUTH_FAILED,
        "not logged in; HTTP 429 Too Many Requests",
    ),
    (
        # The shape a spent quota really arrives in: a 429 whose body says
        # the quota ran out. Rate limiting would retry it forever.
        FailureClass.QUOTA_EXHAUSTED,
        "insufficient_quota; HTTP 429 Too Many Requests",
    ),
]


def _run_entry_point(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run the module the way the dispatch script does."""
    return subprocess.run(
        [sys.executable, "-m", "src.llms.native_cli.failure_cli", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        input=stdin,
        timeout=120,
    )


def test_the_vocabulary_is_exactly_the_fixed_classes() -> None:
    assert {member.value for member in FailureClass} == {
        "rate_limited",
        "overloaded",
        "network",
        "timeout",
        "quota_exhausted",
        "auth_failed",
        "context_exhausted",
        "turn_budget_exhausted",
        "confirmation_required",
        "tool_blocked",
        "crash",
        "unknown",
    }
    # The class crosses into JSON and into a shell exit table as a plain
    # string, so the comparison has to hold.
    assert FailureClass.RATE_LIMITED == "rate_limited"


@pytest.mark.parametrize(
    ("expected", "diagnostic", "exit_code"),
    REACHABLE,
    ids=[row[0].value for row in REACHABLE],
)
def test_every_class_is_reachable(expected, diagnostic, exit_code) -> None:
    classification = classify_failure("kimi", diagnostic, exit_code=exit_code)
    assert classification.failure_class is expected


@pytest.mark.parametrize(
    ("expected", "diagnostic"),
    PRIORITY,
    ids=[row[0].value for row in PRIORITY],
)
def test_the_fixed_priority_decides_when_two_classes_match(expected, diagnostic) -> None:
    classification = classify_failure("kimi", diagnostic, exit_code=1)
    assert classification.failure_class is expected


def test_the_priority_outranks_the_exit_code() -> None:
    """A dying process does not turn a quota failure into a crash."""
    classification = classify_failure(
        "kimi", "insufficient_quota: no credit left", exit_code=2
    )
    assert classification.failure_class is FailureClass.QUOTA_EXHAUSTED


def test_retry_after_is_read_as_seconds() -> None:
    classification = classify_failure(
        "kimi", "HTTP 429 Too Many Requests\nRetry-After: 120"
    )
    assert classification.retry_after_seconds == 120


def test_retry_after_is_capped_at_ten_minutes() -> None:
    classification = classify_failure(
        "kimi", "HTTP 429 Too Many Requests\nRetry-After: 99999"
    )
    assert classification.retry_after_seconds == 600


def test_an_absent_retry_after_is_none() -> None:
    classification = classify_failure("kimi", "HTTP 429 Too Many Requests")
    assert classification.retry_after_seconds is None


def test_an_unreadable_retry_after_is_none() -> None:
    classification = classify_failure(
        "kimi", "HTTP 429 Too Many Requests\nRetry-After: not-a-number"
    )
    assert classification.retry_after_seconds is None


def test_a_payload_retry_after_is_read() -> None:
    classification = classify_failure(
        "kimi",
        {"error": {"message": "Too Many Requests"}, "retry_after": 45},
    )
    assert classification.failure_class is FailureClass.RATE_LIMITED
    assert classification.retry_after_seconds == 45


def test_a_non_zero_exit_code_without_a_signal_is_a_crash() -> None:
    classification = classify_failure("kimi", "the worker stopped", exit_code=1)
    assert classification.failure_class is FailureClass.CRASH
    assert classification.reason_code == "worker_crash"


def test_no_exit_code_and_no_signal_is_unknown() -> None:
    classification = classify_failure("kimi", "the worker stopped")
    assert classification.failure_class is FailureClass.UNKNOWN
    assert classification.reason_code == "worker_unknown"


def test_a_successful_exit_code_is_not_a_crash() -> None:
    classification = classify_failure("kimi", "the worker stopped", exit_code=0)
    assert classification.failure_class is FailureClass.UNKNOWN


def test_the_reason_code_is_stable_and_free_of_free_text() -> None:
    first = classify_failure("kimi", "HTTP 429 Too Many Requests")
    second = classify_failure("kimi", "HTTP 429 Too Many Requests for another line")
    assert first.reason_code == "worker_rate_limited"
    assert first.reason_code == second.reason_code
    assert first.reason_code.replace("_", "").isalnum()


def test_the_result_has_exactly_three_fields() -> None:
    classification = classify_failure("kimi", "HTTP 429 Too Many Requests")
    assert {field.name for field in dataclasses.fields(classification)} == {
        "failure_class",
        "reason_code",
        "retry_after_seconds",
    }
    assert isinstance(classification, FailureClassification)


def test_the_result_is_frozen() -> None:
    classification = classify_failure("kimi", "HTTP 429 Too Many Requests")
    with pytest.raises(dataclasses.FrozenInstanceError):
        classification.reason_code = "worker_something_else"


def test_the_diagnostic_appears_nowhere_in_the_result(caplog) -> None:
    """The sentinel test: a leak into the result is a credential leak."""
    diagnostic = f"HTTP 429 Too Many Requests (token={SENTINEL})"
    with caplog.at_level(logging.DEBUG):
        classification = classify_failure("kimi", diagnostic)
    assert SENTINEL not in repr(classification)
    assert SENTINEL not in str(classification)
    assert SENTINEL not in repr(vars(classification))
    assert SENTINEL not in caplog.text
    assert classification.failure_class is FailureClass.RATE_LIMITED


def test_the_diagnostic_appears_nowhere_in_the_log(caplog) -> None:
    diagnostic = f"not logged in (token={SENTINEL})"
    with caplog.at_level(logging.DEBUG):
        classification = classify_failure("kimi", diagnostic)
    assert classification.failure_class is FailureClass.AUTH_FAILED
    assert SENTINEL not in caplog.text
    # The line that is there names the provider, the class and the code.
    assert "kimi" in caplog.text
    assert "auth_failed" in caplog.text


def test_a_provider_rule_is_consulted() -> None:
    register_failure_rule(
        "test-provider-alpha",
        FailureClass.CONTEXT_EXHAUSTED,
        patterns=("alphaprovidercontextsignal",),
    )
    classification = classify_failure("test-provider-alpha", "alphaprovidercontextsignal")
    assert classification.failure_class is FailureClass.CONTEXT_EXHAUSTED


def test_a_provider_rule_stays_with_its_provider() -> None:
    register_failure_rule(
        "test-provider-beta",
        FailureClass.NETWORK,
        patterns=("betasignal",),
    )
    classification = classify_failure("test-provider-gamma", "betasignal")
    assert classification.failure_class is FailureClass.UNKNOWN


def test_the_priority_governs_a_provider_rule_too() -> None:
    """A provider's own rule does not outrank the fixed order."""
    register_failure_rule(
        "test-provider-delta",
        FailureClass.OVERLOADED,
        patterns=("deltasignal",),
    )
    classification = classify_failure(
        "test-provider-delta", "deltasignal; HTTP 429 Too Many Requests"
    )
    assert classification.failure_class is FailureClass.RATE_LIMITED


def test_a_provider_rule_may_name_its_own_reason_code() -> None:
    register_failure_rule(
        "test-provider-epsilon",
        FailureClass.QUOTA_EXHAUSTED,
        reason_code="worker_epsilon_credit_spent",
        patterns=("epsilonquota",),
    )
    classification = classify_failure("test-provider-epsilon", "epsilonquota")
    assert classification.failure_class is FailureClass.QUOTA_EXHAUSTED
    assert classification.reason_code == "worker_epsilon_credit_spent"


def test_a_provider_rule_may_name_status_codes() -> None:
    register_failure_rule(
        "test-provider-zeta",
        FailureClass.OVERLOADED,
        status_codes=(599,),
    )
    classification = classify_failure("test-provider-zeta", {"status_code": 599})
    assert classification.failure_class is FailureClass.OVERLOADED


def test_failure_rules_lists_the_generic_rules_and_the_providers_own() -> None:
    register_failure_rule(
        "test-provider-eta",
        FailureClass.NETWORK,
        patterns=("etasignal",),
    )
    rules = failure_rules("test-provider-eta")
    assert any(rule.provider == "test-provider-eta" for rule in rules)
    assert any(rule.generic for rule in rules)
    assert FailureClass.AUTH_FAILED in {rule.failure_class for rule in rules}


def test_a_class_outside_the_vocabulary_is_refused() -> None:
    with pytest.raises(ValueError):
        register_failure_rule("test-provider-theta", "not_a_class")


def test_a_reason_code_must_be_a_stable_token() -> None:
    with pytest.raises(ValueError) as excinfo:
        register_failure_rule(
            "test-provider-iota",
            FailureClass.NETWORK,
            reason_code="the network went sideways",
        )
    # The refusal does not repeat the sentence it refused.
    assert "went sideways" not in str(excinfo.value)


def test_the_entry_point_prints_one_json_line() -> None:
    result = _run_entry_point(
        "--provider",
        "kimi",
        "--text",
        "HTTP 429 Too Many Requests\nRetry-After: 120",
    )
    assert result.returncode == 0
    assert result.stdout.count("\n") == 1
    assert json.loads(result.stdout) == {
        "failure_class": "rate_limited",
        "reason_code": "worker_rate_limited",
        "retry_after_seconds": 120,
    }


def test_the_entry_point_reads_a_diagnostic_file(tmp_path) -> None:
    path = tmp_path / "worker.stderr"
    path.write_text("HTTP 503 Service Unavailable\n", encoding="utf-8")
    result = _run_entry_point("--provider", "kimi", "--diagnostic-file", str(path))
    assert result.returncode == 0
    assert json.loads(result.stdout)["failure_class"] == "overloaded"


def test_the_entry_point_reads_stdin() -> None:
    result = _run_entry_point(
        "--provider",
        "kimi",
        "--diagnostic-file",
        "-",
        stdin="HTTP 429 Too Many Requests",
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["failure_class"] == "rate_limited"


def test_the_entry_point_never_prints_the_diagnostic() -> None:
    result = _run_entry_point(
        "--provider", "kimi", "--text", f"HTTP 429 Too Many Requests ({SENTINEL})"
    )
    assert result.returncode == 0
    assert SENTINEL not in result.stdout
    assert SENTINEL not in result.stderr
    assert set(json.loads(result.stdout)) == {
        "failure_class",
        "reason_code",
        "retry_after_seconds",
    }


def test_the_entry_point_reports_a_crash_from_the_exit_code() -> None:
    result = _run_entry_point(
        "--provider",
        "kimi",
        "--text",
        "the worker stopped",
        "--exit-code",
        "1",
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["failure_class"] == "crash"


def test_the_entry_point_needs_one_diagnostic_source() -> None:
    result = _run_entry_point("--provider", "kimi")
    assert result.returncode == 2
    assert result.stdout == ""


def test_the_entry_point_refuses_two_diagnostic_sources() -> None:
    result = _run_entry_point(
        "--provider", "kimi", "--text", "boom", "--diagnostic-file", "-"
    )
    assert result.returncode == 2
    assert result.stdout == ""
