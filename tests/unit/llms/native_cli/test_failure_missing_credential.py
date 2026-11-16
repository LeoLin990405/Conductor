"""The DSH CLI's ``MISSING_CREDENTIAL`` line is a credential failure (N30/N26).

DSH prints ``MISSING_CREDENTIAL: no API key`` when the provider route carries
no key. Before N30 that line fell through to ``crash`` (with an exit code) or
``unknown``, so the recovery loop retried a launch only a human could fix and
the cost ledger charged it to the model. It is ``auth_failed`` with the stable
code ``missing_credential``; the model quoting the word mid-sentence is not.
"""

from __future__ import annotations

import pytest

from src.llms.native_cli.failure import FailureClass, classify_failure

SENTINEL = "SENTINEL-7f3a-no-text"

MARKER_LINES = [
    "MISSING_CREDENTIAL: no API key",
    "error: MISSING_CREDENTIAL",
    "Error: missing_credential (provider route deepseek)",
    "\x1b[31mMISSING_CREDENTIAL: no API key\x1b[0m",
    "[dsh] MISSING_CREDENTIAL: no API key",
    f"starting worker\nMISSING_CREDENTIAL: no API key {SENTINEL}\n",
]


@pytest.mark.parametrize("provider", ["deepseek", "dsh", "glm", "kimi"])
@pytest.mark.parametrize("text", MARKER_LINES)
def test_missing_credential_is_auth_failed(provider: str, text: str) -> None:
    result = classify_failure(provider, text, exit_code=1)
    assert result.failure_class is FailureClass.AUTH_FAILED
    assert result.reason_code == "missing_credential"


def test_missing_credential_without_exit_code() -> None:
    result = classify_failure("deepseek", "MISSING_CREDENTIAL: no API key")
    assert result.failure_class is FailureClass.AUTH_FAILED


def test_reason_code_carries_no_text() -> None:
    result = classify_failure("deepseek", MARKER_LINES[-1], exit_code=1)
    assert SENTINEL not in repr(result)


@pytest.mark.parametrize(
    "text",
    [
        "the model wrote: we should handle MISSING_CREDENTIAL gracefully",
        "MISSING_CREDENTIALS_HANDLER registered",
    ],
)
def test_mid_sentence_or_longer_word_is_not_the_marker(text: str) -> None:
    result = classify_failure("deepseek", text, exit_code=1)
    assert result.failure_class is not FailureClass.AUTH_FAILED


def test_zhipu_rate_limit_unchanged() -> None:
    result = classify_failure("glm", "API error [1302] rate limit reached", exit_code=1)
    assert result.failure_class is FailureClass.RATE_LIMITED


def test_existing_auth_status_code_unchanged() -> None:
    result = classify_failure("glm", "HTTP status 401 unauthorized", exit_code=1)
    assert result.failure_class is FailureClass.AUTH_FAILED
    assert result.reason_code == "worker_auth_failed"
