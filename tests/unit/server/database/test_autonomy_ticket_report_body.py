import time
from contextlib import asynccontextmanager

import pytest

from src.server.database.autonomy import tickets
from src.server.database.autonomy.tickets import _report_body


def test_report_body_scrubs_before_byte_capping_and_preserves_null():
    assert _report_body(None) is None
    assert _report_body("ordinary report") == "ordinary report"
    assert "[REDACTED]" in _report_body("token=ghp_" "123456789012345678901234567890")


def test_report_body_cap_is_utf8_safe_and_includes_marker():
    value = _report_body("界" * (64 * 1024))
    assert value is not None
    assert len(value.encode("utf-8")) <= 64 * 1024
    assert value.endswith("[… report truncated …]")


def test_report_body_replaces_lone_surrogates_before_utf8_bounding():
    value = _report_body("before\ud800after")
    assert value == "before?after"


def test_report_body_bounds_input_before_sanitizing(monkeypatch):
    seen = []

    def capture(value, *, limit=0):
        seen.append(value)
        return "small result"

    monkeypatch.setattr(tickets, "sanitize_report_text", capture)
    result = _report_body("界" * (80 * 1024))
    assert len(seen[0].encode("utf-8")) <= 64 * 1024
    assert result.endswith("[… report truncated …]")


@pytest.mark.parametrize(
    "body",
    [
        "a=" * 131_072,
        "https://x/?" + "ab=12&" * 43_690,
        "token:" * 43_690,
        "session_id=" * 23_830,
    ],
)
def test_report_body_256k_adversarial_inputs_are_bounded_and_fast(body):
    started = time.perf_counter()
    result = _report_body(body)
    elapsed = time.perf_counter() - started
    assert result is not None
    assert len(result.encode("utf-8")) <= 64 * 1024
    assert elapsed <= 0.5


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["add_report", "record_worker_report"])
async def test_report_sanitizing_finishes_before_database_entry(monkeypatch, method):
    events = []

    def sanitize(body):
        events.append("sanitize")
        return "clean body"

    class EntryReached(Exception):
        pass

    @asynccontextmanager
    async def connection():
        events.append("connection")
        raise EntryReached
        yield None

    monkeypatch.setattr(tickets, "_report_body", sanitize)
    monkeypatch.setattr(tickets, "get_db_connection", connection)
    if method == "add_report":
        call = tickets.add_report(
            ticket_id="ticket", state="ready", summary="summary", body="raw"
        )
    else:
        call = tickets.record_worker_report(
            ticket_id="ticket",
            expected_thread_id="thread",
            state="ready",
            summary="summary",
            body="raw",
            ticket_fields={"status": "completed"},
        )
    with pytest.raises(EntryReached):
        await call
    assert events == ["sanitize", "connection"]
