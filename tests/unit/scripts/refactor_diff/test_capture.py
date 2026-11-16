"""HTTP capture: a live server is turned into a validated, redacted fixture.

The capture path is what makes F1 real rather than synthetic, so the parts worth
pinning are: the response becomes the exchange body, a non-JSON response is kept
as bounded raw text, a 404 is captured as a status (not an exception), and
nothing token-shaped survives into the fixture — the producer redacts and the
backstop validator would reject it otherwise.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.refactor_diff import cli
from scripts.refactor_diff.capture import (
    CaptureError,
    capture_http,
    parse_request,
)
from scripts.refactor_diff.normalize import assert_redacted
from scripts.refactor_diff.schema import load_fixture

# A loopback HTTP server is the point of this module; unit tests otherwise run
# socket-blocked.
pytestmark = pytest.mark.enable_socket

REPO = Path(__file__).resolve().parents[4]
FIXTURES = REPO / "scripts" / "refactor_diff" / "fixtures"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
        if self.path == "/api/v1/oauth/providers":
            self._send(200, {"providers": [{"id": "kimi-oauth", "login_supported": True}]})
        elif self.path == "/api/v1/secret":
            self._send(
                200,
                {
                    "api_key": "sk-" "live-abcdefghijklmnop",
                    "nested": [{"refresh_token": "rt-real", "ok": True}],
                },
                content_type="application/json",
            )
        elif self.path == "/html":
            self._send(200, b"<html>Bearer abcdefghijklmnopqrstuvwxyz</html>", "text/html")
        else:
            self._send(404, {"detail": "not found"})

    def _send(self, status: int, body, content_type: str = "application/json") -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args) -> None:  # silence access logs in tests
        return


@pytest.fixture()
def live_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class TestCaptureHttp:
    def test_captures_json_statuses_and_redacts(self, live_server):
        fixture = capture_http(
            flow="F1",
            revision="unit-capture",
            base_url=live_server,
            requests=[
                "GET:/api/v1/oauth/providers",
                "GET:/api/v1/secret",
                "GET:/missing",
                "GET:/html",
            ],
        )
        assert fixture.validate() is None
        assert_redacted(fixture)
        assert fixture.synthetic is False
        assert fixture.captured_at
        assert [exchange.status for exchange in fixture.http] == [200, 200, 404, 200]
        assert fixture.http[0].response["body"]["providers"][0]["id"] == "kimi-oauth"

        secret = fixture.http[1].response["body"]
        assert secret["api_key"] == "[REDACTED]"
        assert secret["nested"][0]["refresh_token"] == "[REDACTED]"
        assert secret["nested"][0]["ok"] is True

        html = fixture.http[3].response["body"]
        assert html["content_type"] == "text/html"
        assert "abc" not in html["raw"]
        assert "[REDACTED]" in html["raw"]

    def test_synthetic_flag_and_metadata(self, live_server):
        fixture = capture_http(
            flow="F1",
            revision="unit",
            base_url=live_server,
            requests=["GET:/api/v1/oauth/providers"],
            app_revision="abc123",
            synthetic=True,
        )
        assert fixture.synthetic is True
        assert fixture.app_revision == "abc123"

    def test_parse_request(self):
        assert parse_request("GET:/api/v1/x") == ("GET", "/api/v1/x")
        assert parse_request("post:/x") == ("POST", "/x")
        for bad in ("GET", ":/x", "GET:relative"):
            with pytest.raises(CaptureError, match="request spec"):
                parse_request(bad)

    def test_unreachable_server_raises_capture_error(self):
        with pytest.raises(CaptureError, match="failed"):
            capture_http(
                flow="F1",
                revision="unit",
                base_url="http://127.0.0.1:1",
                requests=["GET:/api/v1/oauth/providers"],
                timeout=0.5,
            )

    def test_no_requests_rejected(self, live_server):
        with pytest.raises(CaptureError, match="no requests"):
            capture_http(
                flow="F1",
                revision="unit",
                base_url=live_server,
                requests=[],
            )


class TestCaptureCli:
    def test_cli_capture_writes_valid_fixture(self, live_server, tmp_path):
        out = tmp_path / "login-capture.json"
        code = cli.main(
            [
                "capture",
                "--flow",
                "F1",
                "--revision",
                "cli-capture",
                "--base-url",
                live_server,
                "--request",
                "GET:/api/v1/oauth/providers",
                "--request",
                "GET:/html",
                "--out",
                str(out),
                "--synthetic",
            ]
        )
        assert code == 0
        fixture = load_fixture(out)
        assert fixture.synthetic is True
        assert len(fixture.http) == 2
        # The captured fixture must pass the same validate gate as committed ones.
        assert cli.main(["validate", str(out)]) == 0

    def test_cli_capture_failure_exit_code(self, tmp_path):
        code = cli.main(
            [
                "capture",
                "--flow",
                "F1",
                "--revision",
                "fail",
                "--base-url",
                "http://127.0.0.1:1",
                "--request",
                "GET:/x",
                "--out",
                str(tmp_path / "out.json"),
                "--timeout",
                "0.5",
            ]
        )
        assert code == 2


class TestCommittedFixturesStillValidate:
    def test_all_fixtures_validate_after_capture_addition(self):
        paths = sorted(FIXTURES.glob("*/*.json"))
        assert paths
        for path in paths:
            assert cli.main(["validate", str(path)]) == 0
