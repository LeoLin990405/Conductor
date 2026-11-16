"""Follow-up redaction checks (N55): linear time and raw URL passwords.

The timing tests feed 1 MB (or 2 MB) adversarial texts to
``redact_credentials`` and require each to finish in under a second. A
SIGALRM guard stops a regressed, super-linear pass after a few seconds so
the test fails instead of hanging for hours.
"""

from __future__ import annotations

import signal
import time
from collections.abc import Callable, Iterator

import pytest

from src.llms.native_cli.failure import redact_credentials

_MB = 1_000_000
# Well above the ~0.1-0.3 s these take locally, still under one second.
_BOUND_SECONDS = 0.95
_GUARD_SECONDS = 5


class _TooSlowError(Exception):
    pass


@pytest.fixture
def alarm_guard() -> Iterator[None]:
    if not hasattr(signal, "setitimer"):
        yield
        return

    def _raise(signum: int, frame: object) -> None:
        raise _TooSlowError(f"redaction ran longer than {_GUARD_SECONDS}s")

    previous = signal.signal(signal.SIGALRM, _raise)
    signal.setitimer(signal.ITIMER_REAL, _GUARD_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _repeat(unit: str, size: int = _MB) -> str:
    return (unit * (size // len(unit) + 1))[:size]


_ADVERSARIAL: dict[str, Callable[[], str]] = {
    "word-chars": lambda: "x" * _MB,
    "key-chars-dot-dash": lambda: _repeat("a.b-c_"),
    "key-name-run": lambda: _repeat("api_key"),
    "dots": lambda: "." * _MB,
    "dashes": lambda: "-" * _MB,
    "shape-prefix": lambda: "sk-" + "a" * (_MB - 3),
    "plus-diff-lines": lambda: _repeat("\n+"),
    "plus": lambda: "+" * _MB,
    "at-signs": lambda: "@" * _MB,
    "colons": lambda: ":" * _MB,
    "slashes": lambda: "/" * _MB,
    "spaces": lambda: " " * _MB,
    "quotes": lambda: '"' * _MB,
    "equals": lambda: "=" * _MB,
    "scheme-run": lambda: _repeat("a://"),
    "userinfo-at-run": lambda: "a://b:" + "@" * (_MB - 6),
    "userinfo-pairs": lambda: _repeat("x:a@"),
    "url-spill-hash": lambda: _repeat("a://b:c#"),
    "url-spill-space": lambda: _repeat("a://u:p w "),
    "url-spill-ats": lambda: "a://b:c#" + "@" * (_MB - 8),
    "bearer-run": lambda: _repeat("Bearer "),
    "authorization-run": lambda: _repeat("authorization:"),
    "account-run": lambda: _repeat("uid="),
    "spaced-key-run": lambda: _repeat("api key "),
    "pem-begin-run": lambda: _repeat("-----BEGIN "),
    "pair-run": lambda: _repeat("api_key=x "),
    "diff-pair-run": lambda: _repeat("password\n+x\n"),
}


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL))
def test_adversarial_megabyte_redacts_in_under_a_second(
    name: str, alarm_guard: None
) -> None:
    text = _ADVERSARIAL[name]()
    started = time.perf_counter()
    redact_credentials(text)
    elapsed = time.perf_counter() - started
    assert elapsed < _BOUND_SECONDS, f"{name}: {elapsed:.3f}s"


def test_long_word_is_linear_regression_b3(alarm_guard: None) -> None:
    # 40 kB of word characters took 32 s before the pair key was anchored.
    started = time.perf_counter()
    assert redact_credentials("x" * 40_000) == "x" * 40_000
    assert time.perf_counter() - started < _BOUND_SECONDS


def test_many_matches_do_not_copy_the_tail(alarm_guard: None) -> None:
    # Each match used to slice the rest of the text to look for the marker,
    # which is quadratic in the number of matches.
    text = _repeat("Bearer a ", 2 * _MB)
    started = time.perf_counter()
    redacted = redact_credentials(text)
    assert time.perf_counter() - started < _BOUND_SECONDS
    assert "Bearer a" not in redacted


@pytest.mark.parametrize(
    "text",
    [
        "x api_key=s3cret",
        "feature/api_key=s3cret",
        '{"api_key": "s3cret"}',
        "a.b-c.api_key=s3cret",
    ],
)
def test_anchored_pair_key_still_matches_at_token_start(text: str) -> None:
    assert "s3cret" not in redact_credentials(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("postgres://u:p#ss@db/app", "postgres://[redacted]@db/app"),
        ('postgres://u:p"w@db', "postgres://[redacted]@db"),
        ("postgres://u:p[w]@db:5432/app", "postgres://[redacted]@db:5432/app"),
        ("postgres://u:p]w@db", "postgres://[redacted]@db"),
        ("postgres://u:p'w@db", "postgres://[redacted]@db"),
        ("postgres://u:p w@db", "postgres://[redacted]@db"),
        ("postgres://u:p\tw@db", "postgres://[redacted]@db"),
        (
            "failed: postgres://u:p w@db/app (retry)",
            "failed: postgres://[redacted]@db/app (retry)",
        ),
        ("postgres://u:p w@x@db", "postgres://[redacted]@db"),
        ('postgres://u:a#b c"d[e]@db.internal', "postgres://[redacted]@db.internal"),
        ("redis://:p#ss@localhost:6379/0", "redis://[redacted]@localhost:6379/0"),
    ],
)
def test_raw_delimiter_in_url_password_is_redacted(text: str, expected: str) -> None:
    assert redact_credentials(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "https://host:8080 as bob@example.com",
        "https://example.com:8080#frag@x",
        "http://127.0.0.1:8080 then mail a@b.com",
        '{"url":"https://example.com","email":"a@b.com"}',
        '{"url":"https://example.com:8080","email":"a@b.com"}',
        "https://example.com#top",
        "https://example.com/a b@c",
        "see http://h:p w\nmail a@b.com",
        "https://host:p# no host after @ ",
        "https://example.com:8080,next",
    ],
)
def test_credential_free_urls_stay_intact(text: str) -> None:
    assert redact_credentials(text) == text


def test_spill_stops_before_the_next_url() -> None:
    text = "a://u:p#x https://alice:secret@host/path"
    assert redact_credentials(text) == "a://u:p#x https://[redacted]@host/path"
