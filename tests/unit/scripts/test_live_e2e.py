"""Live E2E runner: the real-stack gate must fail loudly, not silently pass."""

from __future__ import annotations

from scripts import live_e2e


def test_native_inventory_fails_when_a_provider_is_not_live(monkeypatch):
    payload = {
        "runtimes": [
            {"provider": "codex", "live_verified": True},
            {"provider": "grok", "live_verified": False},
        ]
    }
    monkeypatch.setattr(live_e2e, "api", lambda base, path: (200, payload))
    report = live_e2e.Report()
    live = live_e2e.check_native_inventory(report, "http://x")
    assert live == ["codex"]
    assert report.checks[-1].status == "fail"
    assert "grok" in report.checks[-1].detail


def test_cn_market_requires_at_least_one_healthy_source(monkeypatch):
    responses = iter([
        (200, {"sources": [{"key": "cn_breadth", "status": "unavailable"}]}),
        (200, {"insights": []}),
        (200, {"articles": []}),
    ])
    monkeypatch.setattr(live_e2e, "api", lambda base, path: next(responses))
    report = live_e2e.Report()
    live_e2e.check_cn_market(report, "http://x")
    by_name = {c.name: c for c in report.checks}
    assert by_name["cn market pulse"].status == "fail"
    # Pulse is the required CN gate; insight/news are evidence and degrade to
    # warnings when their upstream is between generations.
    assert by_name["cn market insight"].status == "warn"
    assert by_name["cn news"].status == "warn"


def test_cn_market_passes_with_one_ok_source(monkeypatch):
    responses = iter([
        (200, {"sources": [{"key": "cn_turnover", "status": "ok"}]}),
        (200, {"insights": [{"headline": "x"}]}),
        (200, {"articles": [{"title": "x"}]}),
    ])
    monkeypatch.setattr(live_e2e, "api", lambda base, path: next(responses))
    report = live_e2e.Report()
    live_e2e.check_cn_market(report, "http://x")
    assert not report.failed
    assert "cn_turnover" in {c.name: c for c in report.checks}["cn market pulse"].detail


def test_render_fails_when_a_check_fails():
    report = live_e2e.Report()
    report.add("backend health", "fail", "HTTP None")
    text = live_e2e.render(report, color=False)
    assert "LIVE E2E FAIL" in text
    assert "✗" in text
