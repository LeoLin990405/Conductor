"""Unhandled failures stay safe to return and possible to find.

The HTTP 500 carries a fixed sentence and a correlation id, never any text
of the exception. The local daemon's failure report carries the exception
type, a scrubbed sentence, and one correlation id. The traceback is only in
the log, next to that same id.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import traceback
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.server.app.error_handlers import (
    PUBLIC_MESSAGE_LIMIT,
    unhandled_exception_handler,
)

SECRET = "sk-" + "abcDEF1234567890"
URL_PASSWORD = "hunter2-secret"
PASTED_TRACE = (
    "Traceback (most recent call last):\n"
    '  File "/tmp/secret.py", line 3, in boom\n'
    "    raise RuntimeError\n"
)


def _boom_text() -> str:
    return (
        f"adapter blew up token={SECRET} "
        f"postgresql://app:{URL_PASSWORD}@db.internal/app "
        f"{PASTED_TRACE}"
    )


class _TraceMiddleware:
    """Stands in for RequestIDMiddleware's scope state."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope["state"] = {"trace_id": "trace-fixed-id"}
        await self.app(scope, receive, send)


def _client_app(*, trace: bool = False) -> FastAPI:
    app = FastAPI()
    if trace:
        app.add_middleware(_TraceMiddleware)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    @app.get("/boom")
    def boom():
        raise RuntimeError(_boom_text())

    @app.get("/long")
    def long():
        raise RuntimeError("m" * (PUBLIC_MESSAGE_LIMIT + 50))

    @app.get("/empty")
    def empty():
        raise RuntimeError()

    @app.get("/missing")
    def missing():
        raise HTTPException(status_code=404, detail="missing-thing")

    @app.get("/split-secret")
    def split_secret():
        raise RuntimeError(_SPLIT_SECRET)

    @app.get("/delimited-secret")
    def delimited_secret():
        raise RuntimeError(_DELIMITED_SECRET)

    return app


# The value sits on the next line and contains a comma. Joining the lines
# before redaction ends the value at that comma and leaves the sentinel.
_SPLIT_SECRET = (
    "password=\nprefix,FAKE_SECRET_SENTINEL_42\n"
    "api_key=\nleft,FAKE_TOKEN_SENTINEL_99"
)

# Same-line values. A cut at the comma, the semicolon, or the ampersand
# leaves the sentinel in the sentence. A value that begins with one of
# those characters has no prefix for that cut to replace.
_DELIMITED_SECRET = (
    "password=prefix,FAKE_SECRET_SENTINEL_42 "
    "session_key=aaa;FAKE_SEMI_SENTINEL_7 "
    "client_secret=bbb&FAKE_AMP_SENTINEL_8 "
    "token=,FAKE_LEAD_SENTINEL_3"
)
_DELIMITED_FRAGMENTS = (
    "FAKE_SECRET_SENTINEL_42",
    "FAKE_SEMI_SENTINEL_7",
    "FAKE_AMP_SENTINEL_8",
    "FAKE_LEAD_SENTINEL_3",
    "prefix,",
    "aaa;",
    "bbb&",
)


def _logged_traceback(caplog: pytest.LogCaptureFixture) -> tuple[str, str]:
    """Return ``(correlation id, rendered traceback)`` from the failure log."""
    for record in caplog.records:
        message = record.getMessage()
        if "correlation_id=" not in message or record.exc_info is None:
            continue
        correlation_id = message.split("correlation_id=", 1)[1].split()[0]
        rendered = "".join(traceback.format_exception(*record.exc_info))
        return correlation_id, rendered
    raise AssertionError("no failure log carried a correlation id and a traceback")


def test_unhandled_exception_returns_a_fixed_message_and_logs_the_traceback(caplog):
    app = _client_app()
    with caplog.at_level(logging.ERROR):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/boom")
    body = response.json()
    assert response.status_code == 500
    assert body["correlation_id"]
    assert response.headers["x-correlation-id"] == body["correlation_id"]
    assert response.headers["x-trace-id"] == body["correlation_id"]
    detail = body["detail"]
    assert SECRET not in response.text
    assert URL_PASSWORD not in response.text
    assert "Traceback" not in detail
    assert "secret.py" not in detail
    assert "File \"" not in detail
    assert detail == "Internal server error"
    assert "adapter blew up" not in response.text
    assert set(body) == {"detail", "correlation_id"}
    correlation_id, rendered = _logged_traceback(caplog)
    assert correlation_id == body["correlation_id"]
    assert "Traceback (most recent call last)" in rendered
    assert SECRET in rendered
    assert all(SECRET not in record.getMessage() for record in caplog.records)


def test_unhandled_exception_reuses_the_request_trace_id(caplog):
    app = _client_app(trace=True)
    with caplog.at_level(logging.ERROR):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/boom")
    assert response.json()["correlation_id"] == "trace-fixed-id"
    assert response.headers["x-correlation-id"] == "trace-fixed-id"
    correlation_id, _rendered = _logged_traceback(caplog)
    assert correlation_id == "trace-fixed-id"


def test_public_message_is_bounded_and_empty_text_is_generic():
    from src.server.public_errors import scrub_public_message

    detail = scrub_public_message("m" * (PUBLIC_MESSAGE_LIMIT + 50))
    assert len(detail) == PUBLIC_MESSAGE_LIMIT
    assert detail.endswith("…")
    assert "m" * (PUBLIC_MESSAGE_LIMIT) not in detail
    assert scrub_public_message("") == "Internal server error"
    assert scrub_public_message("", fallback="no message") == "no message"
    app = _client_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        long = client.get("/long")
        empty = client.get("/empty")
    assert long.json()["detail"] == "Internal server error"
    assert "m" * 20 not in long.text
    assert empty.json()["detail"] == "Internal server error"
    assert "Traceback" not in empty.text


def test_a_secret_on_the_next_line_stays_out_of_the_response(caplog):
    app = _client_app()
    with caplog.at_level(logging.ERROR):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/split-secret")
    body = response.json()
    assert response.status_code == 500
    assert "FAKE_SECRET_SENTINEL_42" not in response.text
    assert "FAKE_TOKEN_SENTINEL_99" not in response.text
    assert "prefix," not in body["detail"]
    correlation_id, rendered = _logged_traceback(caplog)
    assert correlation_id == body["correlation_id"]
    assert "FAKE_SECRET_SENTINEL_42" in rendered


def test_a_same_line_secret_keeps_no_suffix_after_a_delimiter(caplog):
    app = _client_app()
    with caplog.at_level(logging.ERROR):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/delimited-secret")
    body = response.json()
    assert response.status_code == 500
    detail = body["detail"]
    for fragment in _DELIMITED_FRAGMENTS:
        assert fragment not in response.text
        assert fragment not in detail
    assert "password=[redacted],FAKE_SECRET_SENTINEL_42" not in detail
    correlation_id, rendered = _logged_traceback(caplog)
    assert correlation_id == body["correlation_id"]
    for fragment in (
        "FAKE_SECRET_SENTINEL_42",
        "FAKE_SEMI_SENTINEL_7",
        "FAKE_AMP_SENTINEL_8",
        "FAKE_LEAD_SENTINEL_3",
    ):
        assert fragment in rendered


def test_http_exception_is_left_to_its_own_handler():
    app = _client_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/missing")
    assert response.status_code == 404
    assert response.json()["detail"] == "missing-thing"
    assert "correlation_id" not in response.json()


def test_the_app_answers_unhandled_exceptions_with_the_safe_handler():
    from src.server.app import setup

    assert setup.app.exception_handlers[Exception] is unhandled_exception_handler
    # Debug mode replaces this handler with an HTML traceback page.
    assert setup.app.debug is False


@pytest.fixture
def daemon_isolation(tmp_path, monkeypatch):
    from src.llms.native_cli import isolation

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = tmp_path / "workspace-policy.json"
    policy.write_text("{}")
    os.chmod(policy, 0o600)
    monkeypatch.setenv("CONDUCTOR_WORKSPACE_POLICY", str(policy))
    monkeypatch.setenv("CONDUCTOR_ISOLATION_ROOT", str(tmp_path / "worker-state"))
    monkeypatch.setattr(
        isolation,
        "read_workspace_policy",
        lambda *_args: {"root": str(workspace), "env": set(), "runtime_read_paths": ()},
    )
    return workspace


def _ticket(ticket_id: str) -> dict:
    return {
        "workspace_id": "test-workspace",
        "ticket_id": ticket_id,
        "lease_id": "11111111-1111-4111-8111-111111111112",
        "lease_duration_seconds": 60,
        "worker_provider": "codex",
        "speculative": False,
    }


@pytest.mark.asyncio
async def test_daemon_failure_summary_is_scrubbed_bounded_and_correlated(
    daemon_isolation, caplog
):
    from scripts import conductor_local_daemon as daemon

    api = Mock()
    manager = daemon.LeaseManager(api)
    secret = SECRET
    text = secret + " " + ("m" * 4000) + " " + secret + " " + PASTED_TRACE
    with caplog.at_level(logging.ERROR, logger="scripts.conductor_local_daemon"):
        with patch(
            "src.llms.native_cli.registry.get_adapter",
            side_effect=RuntimeError(text),
        ):
            assert await daemon.run_claimed(
                api, "codex", "/tmp", 30, {"ticket": _ticket("t-secret")}, manager
            ) is True
    summary = api.report.call_args.args[2]["summary"]
    assert api.report.call_args.args[2]["state"] == "failed"
    assert summary.startswith("Local daemon codex failed:")
    assert secret not in summary
    assert "Traceback" not in summary
    assert "secret.py" not in summary
    assert summary.startswith("Local daemon codex failed: RuntimeError: ")
    assert "m" * 501 not in summary
    correlation_id, rendered = _logged_traceback(caplog)
    assert f"correlation_id={correlation_id}" in summary
    assert "Traceback (most recent call last)" in rendered
    assert secret in rendered
    assert secret not in caplog.records[-1].getMessage()


@pytest.mark.asyncio
async def test_daemon_summary_redacts_a_secret_that_crossed_a_line(
    daemon_isolation, caplog
):
    from scripts import conductor_local_daemon as daemon

    api = Mock()
    manager = daemon.LeaseManager(api)
    with caplog.at_level(logging.ERROR, logger="scripts.conductor_local_daemon"):
        with patch(
            "src.llms.native_cli.registry.get_adapter",
            side_effect=RuntimeError(_SPLIT_SECRET),
        ):
            assert await daemon.run_claimed(
                api, "codex", "/tmp", 30, {"ticket": _ticket("t-split")}, manager
            ) is True
    summary = api.report.call_args.args[2]["summary"]
    assert summary.startswith("Local daemon codex failed:")
    assert "FAKE_SECRET_SENTINEL_42" not in summary
    assert "FAKE_TOKEN_SENTINEL_99" not in summary
    assert "prefix," not in summary
    correlation_id, rendered = _logged_traceback(caplog)
    assert f"correlation_id={correlation_id}" in summary
    assert "FAKE_SECRET_SENTINEL_42" in rendered


@pytest.mark.asyncio
async def test_daemon_summary_redacts_a_secret_split_by_a_delimiter(
    daemon_isolation, caplog
):
    from scripts import conductor_local_daemon as daemon

    api = Mock()
    manager = daemon.LeaseManager(api)
    with caplog.at_level(logging.ERROR, logger="scripts.conductor_local_daemon"):
        with patch(
            "src.llms.native_cli.registry.get_adapter",
            side_effect=RuntimeError(_DELIMITED_SECRET),
        ):
            assert await daemon.run_claimed(
                api, "codex", "/tmp", 30, {"ticket": _ticket("t-delim")}, manager
            ) is True
    summary = api.report.call_args.args[2]["summary"]
    assert summary.startswith("Local daemon codex failed:")
    for fragment in _DELIMITED_FRAGMENTS:
        assert fragment not in summary
    assert "password=[redacted],FAKE_SECRET_SENTINEL_42" not in summary
    correlation_id, rendered = _logged_traceback(caplog)
    assert f"correlation_id={correlation_id}" in summary
    for fragment in (
        "FAKE_SECRET_SENTINEL_42",
        "FAKE_SEMI_SENTINEL_7",
        "FAKE_AMP_SENTINEL_8",
        "FAKE_LEAD_SENTINEL_3",
    ):
        assert fragment in rendered


@pytest.mark.asyncio
async def test_prepare_failure_summary_is_not_type_only(tmp_path, caplog):
    from scripts import conductor_local_daemon as daemon

    secret = "sk-" + "d" * 20
    lease = daemon.ActiveLease(
        "t-prepare",
        "11111111-1111-4111-8111-111111111199",
        time.monotonic() + 60,
        60,
        asyncio.Event(),
    )

    async def boom(*_args, **_kwargs):
        raise RuntimeError(f"git failed token={secret}\n{PASTED_TRACE}")

    with caplog.at_level(logging.ERROR, logger="scripts.conductor_local_daemon"):
        with patch.object(daemon, "prepare_host_worktree", side_effect=boom):
            report = await daemon._prepare_and_bind_host_worktree(
                Mock(),
                {"ticket_id": "t-prepare", "lease_id": lease.lease_id},
                {},
                lease,
                worktree_root=str(tmp_path),
                binding={},
            )
    assert report is not None
    summary = report["summary"]
    assert report["state"] == "failed"
    assert summary.startswith(
        "Local daemon could not prepare the Ticket worktree on this host:"
    )
    assert secret not in summary
    assert "RuntimeError: git failed" in summary
    assert "Traceback" not in summary
    correlation_id, rendered = _logged_traceback(caplog)
    assert f"correlation_id={correlation_id}" in summary
    assert "Traceback (most recent call last)" in rendered
    assert secret in rendered


def test_the_daemon_side_helpers_do_not_build_the_web_app():
    """The daemon formats failure reports with these helpers.

    ``src.server.app``'s ``__init__`` imports ``setup``, which builds the
    whole FastAPI app. The shared helpers must load without it, or every
    failed Ticket would construct the server inside the daemon.
    """
    import subprocess
    import sys

    probe = (
        "import sys\n"
        "import src.server.public_errors as p\n"
        "p.public_exception_message(RuntimeError('x'))\n"
        "print('src.server.app.setup' in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert result.stdout.strip().splitlines()[-1] == "False"


def test_the_daemon_imports_the_helpers_from_outside_the_app_package():
    from pathlib import Path

    source = Path("scripts/conductor_local_daemon.py").read_text(encoding="utf-8")
    assert "src.server.app.error_handlers" not in source
    assert "from src.server.public_errors import" in source
