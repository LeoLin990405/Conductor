"""Doctor: the pre-flight read must not lie about readiness.

The checks themselves talk to Docker/DB/HTTP, so these tests lock the parser,
the status/warning contract, and the provider/port decisions against fakes.
"""

from __future__ import annotations

from pathlib import Path

from scripts import doctor


def test_load_dotenv_handles_export_quotes_and_comments(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n"
        "DB_HOST=localhost\n"
        "export REDIS_URL='redis://localhost:6379/0'\n"
        'EMPTY=""\n'
        "BROKEN LINE\n",
    )
    values = doctor.load_dotenv(path)
    assert values == {
        "DB_HOST": "localhost",
        "REDIS_URL": "redis://localhost:6379/0",
        "EMPTY": "",
    }


def test_render_turns_warnings_into_failures_only_under_strict():
    report = doctor.Doctor()
    report.add("one", "ok", "fine")
    report.add("two", "warn", "optional", "fix it")
    human = report.render(color=False)
    assert "Summary: 1 ok, 1 warn, 0 fail, 0 skipped" in human
    assert "Result: READY with warnings" in human
    assert "fix: fix it" in human

    strict = report.render(color=False, strict=True)
    assert "Result: NOT READY — strict mode promotes warnings to failures." in strict
    assert report.failed is False


def test_check_ports_reports_backend_as_required_and_web_as_optional(monkeypatch):
    monkeypatch.setattr(doctor, "port_listening", lambda host, port: port == 8000)
    report = doctor.Doctor()
    backend_up, web_up = doctor.check_ports(report, {})
    by_name = {check.name: check for check in report.checks}
    assert backend_up is True and web_up is False
    assert by_name["port backend"].status == "ok"
    assert by_name["port web"].status == "warn"


def test_check_native_providers_counts_live_runtimes(monkeypatch):
    payload = {
        "runtimes": [
            {"provider": "codex", "installed": True, "live_verified": True},
            {"provider": "grok", "installed": True, "live_verified": False},
        ]
    }
    monkeypatch.setattr(doctor, "http_json", lambda url, timeout=5.0: (200, payload))
    monkeypatch.setattr(doctor, "listening_pid", lambda port: None)
    report = doctor.Doctor()
    doctor.check_native_providers(report, {})
    check = {c.name: c for c in report.checks}["native providers"]
    assert check.status == "ok"
    assert "1 live / 2 installed" in check.detail


def test_check_backend_reports_checkpointer_failure(monkeypatch):
    monkeypatch.setattr(
        doctor,
        "http_json",
        lambda url, timeout=5.0: (200, {"status": "healthy", "checkpointer": {"status": "down"}}),
    )
    report = doctor.Doctor()
    assert doctor.check_backend(report, {}, True) is False
    assert report.checks[-1].status == "fail"
