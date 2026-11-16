"""What ``sanitize_error_text`` must scrub before exception text is delivered."""

from __future__ import annotations

import time

import pytest

from src.server.utils.error_sanitization import sanitize_error_text


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://svc:hunter2@db.example:5432/app",
        "redis://default:s3cret@cache.example:6379/0",
        "amqp://guest:guest@broker.example:5672/",
    ],
)
def test_a_connection_dsn_never_carries_its_password_through(dsn: str) -> None:
    """The exceptions this scrubber exists for are connection failures, and
    those quote a DSN rather than an http(s) URL."""
    scrubbed = sanitize_error_text(f"connection failed: {dsn}")

    assert "hunter2" not in scrubbed
    assert "s3cret" not in scrubbed
    assert ":guest@" not in scrubbed
    # The host survives — it is the diagnostic, and the credential is not.
    assert ".example" in scrubbed


def test_the_scheme_survives_so_the_message_still_reads() -> None:
    assert sanitize_error_text("GET https://u:p@api.example/v1 failed") == (
        "GET https://api.example/v1 failed"
    )


def test_ordinary_text_with_an_at_sign_is_left_alone() -> None:
    """Only userinfo directly after a scheme is credential-shaped; an address
    or a decorator in a traceback is not."""
    text = "raised in @retry wrapper; contact ops@example.com"

    assert sanitize_error_text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "a" * 200_000,
        "a.b+c-" * 40_000,
        "a://" * 50_000,
        "://" * 60_000,
        "x" * 200_000 + "://u@",
        "http://" + "u" * 200_000,
        "sk-" * 60_000,
        "AIza" * 50_000,
        "api_key" + " " * 200_000,
        "bearer" + " " * 200_000,
        "?key=" * 40_000,
    ],
    ids=["scheme-run", "scheme-chars", "anchors", "bare-anchor", "run-then-anchor",
         "long-userinfo", "sk", "aiza", "key-blanks", "bearer-blanks", "query-keys"],
)
def test_scrubbing_is_linear_on_text_a_caller_chooses(text: str) -> None:
    """A dict key in a 422's ``loc`` is scrubbed on the event loop: the scheme
    pattern retried every start of a long scheme-looking run to its end."""
    started = time.perf_counter()
    sanitize_error_text(text)
    assert time.perf_counter() - started < 1.0


def test_a_scheme_longer_than_the_bound_still_loses_its_userinfo() -> None:
    long_scheme = "database-connection-string-for-the-main-database-postgres"
    assert (
        sanitize_error_text(f"{long_scheme}://u:pw@db/x")
        == f"{long_scheme}://db/x"
    )
    assert sanitize_error_text("1http://u:pw@h x-http://a:b@c") == "1http://h x-http://c"
