"""Local process and read deadlines are not transport failures.

The runner reports its own deadline as "native CLI timed out after Ns", and a
socket read as "read timed out" or "ReadTimeout". Those phrases also contain
"timed out" or "timeout", which the network rule still uses. The timeout
class is the narrower match and outranks network, so the local deadline is
``worker_timeout``. A refused connection, a reset, DNS, TLS, "connection
timed out", "etimedout" and a provider line such as "Request timed out" stay
``worker_network``.
"""

from __future__ import annotations

import pytest

from src.llms.native_cli.failure import FailureClass, classify_failure


# The messages the local runner and a read of its pipes actually produce.
LOCAL_TIMEOUTS = [
    "native CLI timed out after 300s",
    "dsh CLI timed out after 90s",
    "zcode CLI timed out after 12.50s waiting for the provider lock",
    "Zhipu CLI timed out after 0.05s",
    "claude CLI timed out",
    "read timed out",
    "Read timed out. (read timeout=30)",
    "The read operation timed out",
    "ReadTimeout",
]

# Transport failures, including the ones that say "timed out" without being
# the local process or the local read.
NETWORK_ERRORS = [
    "connect ECONNREFUSED 127.0.0.1:443",
    "connection reset by peer",
    "connection timed out",
    "connection timed out after 30s",
    "etimedout",
    "name resolution failed",
    "tls handshake failed",
    "Request timed out. Check your internet connection and proxy settings",
    "PING timed out",
    "network is unreachable",
    "连接超时",
    "timed out",
    "timeout",
]


@pytest.mark.parametrize("diagnostic", LOCAL_TIMEOUTS)
def test_local_process_and_read_timeouts_are_worker_timeout(diagnostic: str) -> None:
    classification = classify_failure("kimi", diagnostic, exit_code=1)
    assert classification.failure_class is FailureClass.TIMEOUT
    assert classification.reason_code == "worker_timeout"
    assert classification.failure_class == "timeout"


@pytest.mark.parametrize("diagnostic", NETWORK_ERRORS)
def test_genuine_network_errors_stay_worker_network(diagnostic: str) -> None:
    classification = classify_failure("kimi", diagnostic)
    assert classification.failure_class is FailureClass.NETWORK
    assert classification.reason_code == "worker_network"
    assert classification.failure_class == "network"


def test_a_local_deadline_outranks_a_network_phrase_in_the_same_text() -> None:
    """The process was killed locally even if the line also says the link failed."""
    classification = classify_failure(
        "kimi",
        "native CLI timed out after 300s; connection refused",
        exit_code=1,
    )
    assert classification.failure_class is FailureClass.TIMEOUT
    assert classification.reason_code == "worker_timeout"


def test_a_higher_class_still_outranks_a_local_timeout() -> None:
    classification = classify_failure(
        "kimi",
        "quota exceeded; native CLI timed out after 300s",
    )
    assert classification.failure_class is FailureClass.QUOTA_EXHAUSTED
    assert classification.reason_code == "worker_quota_exhausted"


def test_cursor_ping_timeout_stays_a_provider_network_error() -> None:
    """A provider's own transport wording is not the runner's deadline."""
    classification = classify_failure("cursor", "PING timed out")
    assert classification.failure_class is FailureClass.NETWORK
    assert classification.reason_code == "cursor_network_error"
