"""Waker contract: one switch per source, isolation, and redacted payloads."""

from src.server.services.autonomy.wakers.base import (
    WakeEvent,
    Waker,
    dispatch_wakers,
    redact_wake_payload,
    wake_idempotency_key,
)
from src.server.services.autonomy.wakers.codexbar import CodexBarWaker

_TOKEN = "ghp_" + ("a" * 24)


class _Waker:
    def __init__(self, name, *, enabled=True, events=None, error=None):
        self.name = name
        self._enabled = enabled
        self._events = list(events or [])
        self._error = error
        self.calls = 0

    def enabled(self):
        return self._enabled

    def collect(self, raw):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._events)


def test_codexbar_satisfies_the_waker_protocol():
    waker = CodexBarWaker(enabled=True)
    assert isinstance(waker, Waker)
    assert waker.enabled() is True
    assert waker.name == "codexbar"


def test_disabled_waker_is_not_collected():
    disabled = _Waker("codexbar", enabled=False, error=RuntimeError("must not run"))
    report = dispatch_wakers([disabled], {"codexbar": {"kind": "quota_exhausted"}})
    assert report.events == ()
    assert report.skipped == ("codexbar",)
    assert report.failures == ()
    assert disabled.calls == 0


def test_one_waker_failing_does_not_stop_the_next():
    event = WakeEvent(source="repobar", kind="pull_request", idempotency_key="k")
    boom = _Waker("codexbar", error=RuntimeError(f"snapshot {_TOKEN}"))
    other = _Waker("repobar", events=[event])
    report = dispatch_wakers([boom, other], {"codexbar": {}, "repobar": {}})
    assert report.events == (event,)
    assert len(report.failures) == 1
    assert report.failures[0].waker == "codexbar"
    assert _TOKEN not in report.failures[0].error
    assert "[REDACTED]" in report.failures[0].error


def test_redacts_secret_keys_and_token_text():
    cleaned = redact_wake_payload(
        {
            "api_key": "super-secret",
            "name": f"build {_TOKEN}",
            "nested": {"authorization": "Bearer abcdefghijklmnop"},
            "ok": True,
        }
    )
    text = str(cleaned)
    assert "super-secret" not in text
    assert _TOKEN not in text
    assert cleaned["api_key"] == "[REDACTED]"
    assert cleaned["nested"]["authorization"] == "[REDACTED]"
    assert "[REDACTED]" in cleaned["name"]
    assert cleaned["ok"] is True


def test_long_or_multiline_idempotency_key_stays_bounded():
    long_key = wake_idempotency_key("repobar", "ci_failure", "acme/app", "x" * 300)
    multiline = wake_idempotency_key("codexbar", "quota_exhausted", "glm", "a\nb")
    assert len(long_key) <= 80
    assert long_key.startswith("waker:repobar:ci_failure:")
    assert "\n" not in multiline
    assert multiline.startswith("waker:codexbar:quota_exhausted:")
