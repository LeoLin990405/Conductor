"""Live HTTP capture into a differential fixture (Phase 1, F1-capable).

The comparison half of the harness does not care where a fixture came from; this
module produces one by making the requests in a capture plan against a live app
and redacting the responses before writing. It is deliberately stdlib-only and
GET/POST-capable so it can run **on the host that reaches the app** (the M2 app
binds loopback there) without installing Playwright — browser/SSE capture lands
with the Phase 3/4 slices.

Safety: request headers are not stored beyond ``accept``; response headers are
dropped; response bodies are redacted (secret-named fields wholesale,
secret-shaped substrings replaced); the resulting fixture is validated with
``assert_redacted`` before it can be saved.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Sequence

from scripts.refactor_diff.normalize import assert_redacted, redact_for_capture
from scripts.refactor_diff.schema import Exchange, Fixture

CAPTURE_HEADERS = {"accept": "application/json", "user-agent": "conductor-refactor-capture/1"}
# Capture connects directly: it is run on a host that reaches the app itself
# (the M2 app binds loopback there). Honouring an operator HTTP(S)_PROXY would
# turn a loopback capture into a proxy error (observed: 502 from a local proxy).
_CAPTURE_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MAX_BODY_CHARS = 200_000


class CaptureError(RuntimeError):
    """The capture plan could not be executed (network/parse/schema)."""


def parse_request(spec: str) -> tuple[str, str]:
    """``GET:/api/v1/...`` -> ``("GET", "/api/v1/...")``."""
    method, sep, path = spec.partition(":")
    method = method.strip().upper()
    path = path.strip()
    if not sep or not method or not path.startswith("/"):
        raise CaptureError(f"request spec must be METHOD:/path, got {spec!r}")
    return method, path


def _fetch(
    base_url: str, method: str, path: str, *, timeout: float
) -> tuple[int, dict[str, Any]]:
    url = base_url.rstrip("/") + path
    request = urllib.request.Request(url, method=method, headers=CAPTURE_HEADERS)
    try:
        with _CAPTURE_OPENER.open(request, timeout=timeout) as response:
            status = response.status
            content_type = response.headers.get("content-type", "")
            raw = response.read(MAX_BODY_CHARS * 4)
    except urllib.error.HTTPError as exc:
        status = exc.code
        content_type = exc.headers.get("content-type", "") if exc.headers else ""
        raw = exc.read(MAX_BODY_CHARS * 4)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CaptureError(f"{method} {path} failed: {exc}") from exc

    text = raw[:MAX_BODY_CHARS].decode("utf-8", "replace")
    if "json" in content_type.lower():
        try:
            body: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CaptureError(f"{method} {path} returned invalid JSON: {exc}") from exc
    else:
        body = {"raw": text, "content_type": content_type}
    return status, {"body": redact_for_capture(body), "content_type": content_type}


def capture_http(
    *,
    flow: str,
    revision: str,
    base_url: str,
    requests: Sequence[str],
    app_revision: str = "",
    synthetic: bool = False,
    timeout: float = 15.0,
) -> Fixture:
    """Execute a capture plan and return a validated, redacted fixture."""
    if not requests:
        raise CaptureError("capture plan has no requests")
    exchanges: list[Exchange] = []
    for spec in requests:
        method, path = parse_request(spec)
        status, response = _fetch(base_url, method, path, timeout=timeout)
        exchanges.append(
            Exchange(
                method=method,
                path=path,
                status=status,
                request={"accept": CAPTURE_HEADERS["accept"]},
                response=response,
            )
        )
    fixture = Fixture(
        flow=flow,
        revision=revision,
        app_revision=app_revision,
        captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        synthetic=synthetic,
        http=tuple(exchanges),
    )
    fixture.validate()
    assert_redacted(fixture)
    return fixture
