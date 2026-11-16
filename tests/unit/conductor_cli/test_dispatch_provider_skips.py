"""N25a: ``dispatch status`` shows providers the drive skips for new launches."""

from __future__ import annotations

from datetime import UTC, datetime

from conductor_cli import dispatch
from conductor_cli.dispatch import render_status

NOW = datetime(2026, 11, 11, 13, 0, tzinfo=UTC).timestamp()
LATER = datetime(2026, 11, 11, 14, 5, tzinfo=UTC).timestamp()
TOMORROW = datetime(2026, 11, 12, 9, 30, tzinfo=UTC).timestamp()
EARLIER = datetime(2026, 11, 11, 12, 0, tzinfo=UTC).timestamp()


def _summary(gates: dict) -> dict:
    return {"id": "d1", "state": "MONITORING", "lines": [], "gates": gates}


def _text(summary: dict, now: float = NOW) -> str:
    return "\n".join(render_status(summary, now=now))


def _skip(reason="rate_limited", until=LATER, source="retry_after") -> dict:
    return {"reason": reason, "until": until, "source": source, "at": "2026-11-11T13:00:00Z"}


def test_active_skip_shown_with_formatted_until():
    text = _text(_summary({"provider_skips": {"glm": _skip()}}))
    assert "provider skips" in text
    assert "skipping glm for new launches until 14:05Z (rate_limited, retry_after)" in text


def test_skip_on_another_day_carries_the_date():
    text = _text(_summary({"provider_skips": {"kimi": _skip("quota_exhausted", TOMORROW, "backoff")}}))
    assert "skipping kimi for new launches until 2026-11-12 09:30Z (quota_exhausted, backoff)" in text


def test_expired_skip_not_shown_as_active():
    text = _text(_summary({"provider_skips": {"glm": _skip(until=EARLIER), "kimi": _skip()}}))
    assert "skipping glm" not in text
    assert "skipping kimi for new launches until 14:05Z" in text


def test_only_expired_skips_print_nothing():
    text = _text(_summary({"provider_skips": {"glm": _skip(until=EARLIER)}}))
    assert "provider skips" not in text
    assert "skipping" not in text


def test_null_until_is_unknown():
    entry = _skip("zcode_plan_quota_exhausted", None, "unknown")
    text = _text(_summary({"provider_skips": {"zcode": entry}}))
    assert (
        "skipping zcode for new launches until unknown (zcode_plan_quota_exhausted, unknown)"
        in text
    )


def test_default_clock_is_injectable(monkeypatch):
    monkeypatch.setattr(dispatch, "_now", lambda: LATER + 1)
    text = "\n".join(render_status(_summary({"provider_skips": {"glm": _skip()}})))
    assert "skipping glm" not in text
    monkeypatch.setattr(dispatch, "_now", lambda: NOW)
    text = "\n".join(render_status(_summary({"provider_skips": {"glm": _skip()}})))
    assert "skipping glm for new launches until 14:05Z" in text


def _audit(index: int, replacement: str = "kimi") -> dict:
    return {
        "kind": "provider_skipped",
        "line_id": f"line-{index}",
        "provider": "glm",
        "replacement": replacement,
        "reason": "rate_limited",
        "until": LATER,
        "at": "2026-11-11T13:00:00Z",
    }


def test_audit_entries_shown_and_capped_to_most_recent_five():
    audit = [_audit(i) for i in range(7)]
    audit.insert(3, {"kind": "line_launched", "line_id": "line-x", "provider": "glm"})
    audit[-1] = _audit(6, replacement="")
    text = _text(_summary({"scheduler": {"audit": audit}}))
    assert "provider skips" in text
    assert "line-0:" not in text and "line-1:" not in text
    assert "line-x" not in text
    for i in range(2, 6):
        assert f"line-{i}: glm skipped → kimi (rate_limited)" in text
    assert "line-6: glm skipped → none eligible (rate_limited)" in text
    order = [text.index(f"line-{i}:") for i in range(2, 7)]
    assert order == sorted(order)


def test_escape_sequences_sanitised():
    evil = "\x1b]8;;http://x\x07glm\x1b[31m\x9b"
    gates = {
        "provider_skips": {evil: _skip(reason="rate\x1b[2Klimited\nINJECT", source="retry\x07_after")},
        "scheduler": {
            "audit": [
                {
                    "kind": "provider_skipped",
                    "line_id": "line\x1b[1A-2",
                    "provider": evil,
                    "replacement": "ki\x1b[0mmi",
                    "reason": "rate_limited\r\x1b[2J",
                }
            ],
            "parallel": {"limit": 3, "active": ["a\x1b[31m", "b"]},
        },
    }
    text = _text(_summary(gates))
    assert "\x1b" not in text and "\x07" not in text and "\x9b" not in text
    assert "INJECT" not in text
    assert "skipping glm for new launches until 14:05Z (ratelimited, retry_after)" in text
    assert "line-2: glm skipped → kimi (rate_limited)" in text
    assert "parallel: 2 of 3 lines active (a, b)" in text


def test_parallel_line_shown():
    gates = {"scheduler": {"parallel": {"limit": 3, "active": ["line-0", "line-2"]}}}
    assert "parallel: 2 of 3 lines active (line-0, line-2)" in _text(_summary(gates))


def test_parallel_with_no_active_lines():
    gates = {"scheduler": {"parallel": {"limit": 2, "active": []}}}
    assert "parallel: 0 of 2 lines active" in _text(_summary(gates))


def test_nothing_printed_when_absent():
    for summary in (
        {"id": "d1", "state": "MONITORING", "lines": []},
        _summary({}),
        _summary({"provider_skips": {}, "scheduler": {"audit": []}}),
        _summary({"provider_skips": "bogus", "scheduler": {"audit": "bogus", "parallel": None}}),
    ):
        text = _text(summary)
        assert "provider skips" not in text
        assert "skipping" not in text
        assert "parallel:" not in text
