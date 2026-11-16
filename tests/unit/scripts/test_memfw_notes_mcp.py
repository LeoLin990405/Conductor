import asyncio
import importlib.util
import io
import json
import logging
import runpy
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.config.runtime.core import MCPServerConfig
from src.config.runtime.file_utils import load_yaml_config
from src.server.services.mcp_core.registry import MCPServerConnector

from src.server.services.refactor.memory_federation import errors as shared_errors
from src.server.services.refactor.memory_federation import health as shared_health
from src.server.services.refactor.memory_federation import remote as shared_remote

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "memfw_contract_v2"
_ENDPOINT = "http://memfw:18080"
_TOKEN = "read-only-token"
#: The token the audit uses to prove that a value reached a log line.
_SENTINEL = "sentinel-audit-token"


def _load_module():
    script = _REPO_ROOT / "scripts" / "memfw_notes_mcp.py"
    spec = importlib.util.spec_from_file_location("memfw_notes_mcp_test", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _fixture(name: str) -> dict:
    """The canonical contract sample for one exchange."""
    return json.loads((_FIXTURES / f"{name}.response.json").read_text(encoding="utf-8"))


def _configure_env(monkeypatch, token: str = _TOKEN) -> None:
    monkeypatch.setenv("MEMFW_READONLY_URL", f"{_ENDPOINT}/")
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", token)
    monkeypatch.delenv("MEMFW_READONLY_TOKEN_FILE", raising=False)


class _FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeMemfw:
    """A fake MemFW service that answers ``/health`` and ``/related``."""

    def __init__(
        self,
        *,
        health: dict | None = None,
        related: dict | None = None,
        health_status: int = 200,
        related_status: int = 200,
    ) -> None:
        self.health = _fixture("health.ok") if health is None else health
        self.related = _fixture("related.edges_ok") if related is None else related
        self.health_status = health_status
        self.related_status = related_status
        self.health_error: BaseException | None = None
        self.related_error: BaseException | None = None
        self.calls: list[dict] = []

    def __call__(self, request, timeout):
        self.calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "timeout": timeout,
                "token": request.get_header("X-memfw-token"),
                "payload": json.loads(request.data) if request.data else None,
            }
        )
        if request.full_url.endswith("/health"):
            if self.health_error is not None:
                raise self.health_error
            return self.health_status, self.health
        if self.related_error is not None:
            raise self.related_error
        return self.related_status, self.related

    @property
    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]


def _install_http(monkeypatch, module, service: _FakeMemfw) -> _FakeMemfw:
    """Route the script's own opener to the fake service; no host is reached.

    The seam is the private opener's ``open`` rather than
    ``urllib.request.urlopen``: the script builds a local opener that refuses
    redirects, so it never calls the module-level ``urlopen`` the earlier tests
    patched. Patching the opener keeps every handler above the transport real
    -- which is what lets the redirect tests exercise the actual redirect
    handler -- while the transport itself is still the fake.
    """

    def fake_urlopen(request, timeout):
        status, payload = service(request, timeout)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        if status < 200 or status >= 300:
            raise urllib.error.HTTPError(
                request.full_url, status, "error", {}, io.BytesIO(raw)
            )
        return _FakeResponse(raw, status)

    monkeypatch.setattr(module._OPENER, "open", fake_urlopen)
    return service

class _FakeResponse:
    """What ``urlopen`` hands back for a successful call."""

    def __init__(self, raw: bytes, status: int = 200) -> None:
        self._raw = raw
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._raw


@pytest.mark.asyncio
async def test_all_notes_tools_are_registered_on_import():
    module = _load_module()

    for name in ("notes_search", "notes_related", "memory_related"):
        assert await module.mcp.get_tool(name) is not None


def test_main_runs_after_all_tools_are_registered(monkeypatch):
    module = _load_module()
    script = _REPO_ROOT / "scripts" / "memfw_notes_mcp.py"
    captured = {}

    def fake_run(mcp, transport):
        captured["transport"] = transport
        captured["tools"] = {
            name: asyncio.run(mcp.get_tool(name)) is not None
            for name in ("notes_search", "notes_related", "memory_related")
        }

    monkeypatch.setattr(module.FastMCP, "run", fake_run)
    runpy.run_path(script, run_name="__main__")

    assert captured["transport"] == "stdio"
    assert captured["tools"] == {
        "notes_search": True,
        "notes_related": True,
        "memory_related": True,
    }


def test_notes_related_reports_missing_environment(monkeypatch):
    module = _load_module()
    monkeypatch.delenv("MEMFW_CLI", raising=False)
    monkeypatch.delenv("MEMFW_RELATED_DB", raising=False)

    assert module.notes_related("notes/a.md")["error"] == "MEMFW_CLI missing"

    monkeypatch.setenv("MEMFW_CLI", "/opt/memfw/bin/memfw")
    assert module.notes_related("notes/a.md")["error"] == "MEMFW_RELATED_DB missing"


def test_notes_related_uses_configured_cli_and_database(monkeypatch):
    module = _load_module()
    monkeypatch.setenv("MEMFW_CLI", "/opt/memfw/bin/memfw")
    monkeypatch.setenv("MEMFW_RELATED_DB", "/srv/memfw/related.sqlite")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"related": ["b.md"]}))

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    result = module.notes_related("notes/a.md", limit=7)

    assert result == {
        "ok": True,
        "path": "notes/a.md",
        "related": ["b.md"],
        "limit": 7,
    }
    assert calls[0][0] == [
        "/opt/memfw/bin/memfw",
        "notes",
        "related",
        "notes/a.md",
        "--related-db",
        "/srv/memfw/related.sqlite",
        "--limit",
        "7",
    ]


def test_memory_related_requires_explicit_url(monkeypatch):
    module = _load_module()
    monkeypatch.delenv("MEMFW_READONLY_URL", raising=False)

    result = module.memory_related("memory-1")

    assert result == {
        "ok": False,
        "error": "MEMFW_READONLY_URL missing",
        "related": [],
    }


def test_notes_search_inherits_fleet_host_environment(monkeypatch):
    module = _load_module()
    monkeypatch.setenv("FLEET_MEMORY_HOST", "notes-host")

    def fake_run(command, **kwargs):
        assert module.os.environ["FLEET_MEMORY_HOST"] == "notes-host"
        assert "env" not in kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ok": True, "results": [], "errors": []}),
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.notes_search("query")["ok"] is True


# --- memory_related against the MemFW HTTP contract v2 ----------------------


def test_memory_related_probes_health_before_the_first_related_call(monkeypatch):
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_http(monkeypatch, module, _FakeMemfw())

    result = module.memory_related("memory-1", limit=4)

    assert result["ok"] is True
    assert service.urls == [f"{_ENDPOINT}/health", f"{_ENDPOINT}/related"]
    assert service.calls[0]["method"] == "GET"
    assert service.calls[0]["token"] is None  # /health needs no authentication
    assert service.calls[1]["method"] == "POST"
    assert service.calls[1]["token"] == _TOKEN


def test_memory_related_caches_the_health_answer_for_five_minutes(monkeypatch):
    module = _load_module()
    clock = _FakeClock()
    monkeypatch.setattr(module, "_HEALTH", module._HealthCache(clock=clock))
    _configure_env(monkeypatch)
    service = _install_http(monkeypatch, module, _FakeMemfw())

    module.memory_related("memory-1")
    clock.advance(299)
    module.memory_related("memory-1")

    assert service.urls.count(f"{_ENDPOINT}/health") == 1

    clock.advance(2)  # 301s after the first probe: the window is over
    module.memory_related("memory-1")

    assert service.urls.count(f"{_ENDPOINT}/health") == 2


def test_memory_related_does_not_cache_a_failed_health_probe(monkeypatch):
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_http(monkeypatch, module, _FakeMemfw())
    service.health_error = urllib.error.URLError("connection refused")

    first = module.memory_related("memory-1")
    second = module.memory_related("memory-1")

    assert first == {"ok": False, "error_code": "unreachable", "related": []}
    assert second == {"ok": False, "error_code": "unreachable", "related": []}
    assert service.urls == [f"{_ENDPOINT}/health", f"{_ENDPOINT}/health"]

    service.health_error = None
    third = module.memory_related("memory-1")

    assert third["ok"] is True
    assert service.urls.count(f"{_ENDPOINT}/health") == 3


def test_memory_related_reports_unreachable_when_the_probe_fails(monkeypatch):
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_http(monkeypatch, module, _FakeMemfw(health_status=503))

    result = module.memory_related("memory-1")

    assert result == {"ok": False, "error_code": "unreachable", "related": []}
    assert service.urls == [f"{_ENDPOINT}/health"]


@pytest.mark.parametrize(
    "health",
    [
        {"ok": True, "contract": "memfw-http/3"},
        {"ok": True, "contract": "memfw-http/1"},
        {"ok": True},
        {"ok": True, "contract": None},
        {"ok": True, "contract": "something-else/2"},
    ],
)
def test_memory_related_rejects_a_contract_major_other_than_two(monkeypatch, health):
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_http(monkeypatch, module, _FakeMemfw(health=health))

    result = module.memory_related("memory-1")

    assert result == {"ok": False, "error_code": "invalid_response", "related": []}
    assert service.urls == [f"{_ENDPOINT}/health"]


@pytest.mark.parametrize(
    "related",
    [
        {"contract": "memfw-http/1", "mode": "edges", "degraded": False, "results": []},
        {"contract": "memfw-http/3", "mode": "edges", "degraded": False, "results": []},
        {"mode": "edges", "degraded": False, "results": []},
        {"contract": None, "mode": "edges", "degraded": False, "results": []},
        {"contract": "something-else/2", "mode": "edges", "degraded": False, "results": []},
    ],
)
def test_memory_related_rejects_a_response_body_from_another_contract(
    monkeypatch, related
):
    """Contract section 1: every response body carries the contract marker.

    The health probe already negotiated the version of the endpoint, but the
    body that comes back is checked on its own: a body from another version of
    the service must not be read as related memories.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_http(monkeypatch, module, _FakeMemfw(related=related))

    result = module.memory_related("memory-1")

    assert result == {"ok": False, "error_code": "invalid_response", "related": []}
    assert service.urls == [f"{_ENDPOINT}/health", f"{_ENDPOINT}/related"]


@pytest.mark.parametrize(("requested", "expected"), [(0, 1), (5, 5), (100, 50)])
def test_memory_related_clamps_the_limit_to_the_contract_range(
    monkeypatch, requested, expected
):
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_http(monkeypatch, module, _FakeMemfw())

    module.memory_related("memory-1", limit=requested)

    related = [call for call in service.calls if call["url"].endswith("/related")]
    assert related[0]["payload"] == {"memory_id": "memory-1", "limit": expected}


def test_memory_related_uses_the_contract_timeouts(monkeypatch):
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_http(monkeypatch, module, _FakeMemfw())

    module.memory_related("memory-1")

    assert [call["timeout"] for call in service.calls] == [1.0, 1.0]


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, _fixture("search.unauthorized"), "unauthorized"),
        (413, _fixture("search.too_large"), "too_large"),
        (404, _fixture("related.not_found"), "not_found"),
        (400, {"error": "not a contract body"}, "bad_request"),
        (401, "<html>proxy error</html>", "unauthorized"),
        (403, {}, "forbidden"),
        (409, {}, "conflict"),
        (418, {}, "internal"),
        (500, {}, "internal"),
    ],
)
def test_memory_related_passes_the_contract_error_code_through(
    monkeypatch, status, body, expected
):
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_http(
        monkeypatch, module, _FakeMemfw(related_status=status, related=body)
    )

    result = module.memory_related("memory-1")

    assert result == {"ok": False, "error_code": expected, "related": []}
    assert service.urls == [f"{_ENDPOINT}/health", f"{_ENDPOINT}/related"]


def test_memory_related_passes_through_mode_degraded_and_reasons(monkeypatch):
    module = _load_module()
    _configure_env(monkeypatch)
    _install_http(
        monkeypatch,
        module,
        _FakeMemfw(
            related={
                "contract": "memfw-http/2",
                "mode": "edges",
                "degraded": False,
                "memory_id": "memory-1",
                "results": [
                    {
                        "id": "mem-beta",
                        "title": "Beta",
                        "score": 0.7,
                        "reasons": ["shared_tag", "link"],
                    },
                    {
                        "id": "mem-gamma",
                        "title": "Gamma",
                        "score": 0.1,
                        "reasons": ["not_a_contract_reason", "lexical"],
                    },
                ],
            }
        ),
    )

    result = module.memory_related("memory-1")

    assert result["ok"] is True
    assert result["mode"] == "edges"
    assert result["degraded"] is False
    assert result["related"][0]["reasons"] == ["shared_tag", "link"]
    assert result["related"][1]["reasons"] == ["lexical"]


def test_memory_related_passes_through_a_degraded_fts_answer(monkeypatch):
    module = _load_module()
    _configure_env(monkeypatch)
    _install_http(
        monkeypatch, module, _FakeMemfw(related=_fixture("related.fts_degraded"))
    )

    result = module.memory_related("memory-1")

    assert result["ok"] is True
    assert result["mode"] == "fts"
    assert result["degraded"] is True
    assert result["related"][0]["reasons"] == ["lexical"]


def test_memory_related_never_leaks_the_token(monkeypatch, caplog):
    module = _load_module()
    _configure_env(monkeypatch, token="sup3r-secret-token")
    service = _install_http(monkeypatch, module, _FakeMemfw())

    with caplog.at_level(logging.DEBUG):
        result = module.memory_related("memory-1")

    assert service.calls[1]["token"] == "sup3r-secret-token"
    assert "sup3r-secret-token" not in repr(result)
    assert "sup3r-secret-token" not in caplog.text


def test_memory_related_never_leaks_the_token_in_the_version_warning(
    monkeypatch, caplog
):
    module = _load_module()
    _configure_env(monkeypatch, token="sup3r-secret-token")
    _install_http(
        monkeypatch, module, _FakeMemfw(health={"ok": True, "contract": "memfw-http/3"})
    )

    with caplog.at_level(logging.DEBUG):
        result = module.memory_related("memory-1")

    assert result["error_code"] == "invalid_response"
    assert "speaks contract" in caplog.text  # the warning really was logged
    assert "sup3r-secret-token" not in repr(result)
    assert "sup3r-secret-token" not in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(f"http://memfw:18080/v1?token={_SENTINEL}", id="query"),
        pytest.param(f"http://memfw:18080/v1#{_SENTINEL}", id="fragment"),
        pytest.param(f"http://user:{_SENTINEL}@memfw:18080/v1", id="userinfo"),
    ],
)
@pytest.mark.parametrize("outcome", ["ok", "http_error", "raises"])
def test_memory_related_refuses_a_secret_url_without_a_request(
    monkeypatch, caplog, url, outcome
):
    """A configured URL that carries a userinfo, a query or a fragment is refused.

    The refusal happens before any request, so the fake service is never called
    and the sentinel cannot reach a request line or a log line. The service is
    armed to succeed, fail or raise, yet none of that can matter: a refused
    configuration sends nothing. The audit runs over the whole captured text at
    DEBUG -- every logger.
    """
    module = _load_module()
    monkeypatch.setenv("MEMFW_READONLY_URL", url)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", _TOKEN)
    monkeypatch.delenv("MEMFW_READONLY_TOKEN_FILE", raising=False)
    service = _FakeMemfw()
    if outcome == "http_error":
        service.health_status = 503
    elif outcome == "raises":
        service.health_error = urllib.error.URLError("connection refused")
    _install_http(monkeypatch, module, service)

    caplog.set_level(logging.DEBUG)
    result = module.memory_related("memory-1")

    assert result == {"ok": False, "error_code": "bad_config", "related": []}
    assert service.calls == [], "a refused config must not send a request"
    assert _SENTINEL not in caplog.text


@pytest.mark.parametrize("outcome", ["ok", "http_error", "raises"])
def test_memory_related_requests_the_fixed_paths_from_the_validated_base(
    monkeypatch, caplog, outcome
):
    """After a v2 probe the related request rides the validated base.

    The reviewer's note on this test: the health body used to answer contract
    v3, so the probe never succeeded and the successful path was never checked.
    With a v2 body the probe succeeds and the related request is made from the
    same validated base, so both URLs the service received are asserted
    exactly -- ``<normalized_base>/health`` then ``<normalized_base>/related``
    -- and the successful answer is asserted, not just the error paths.

    The configured value carries a trailing slash, so this also pins the
    normalisation: one separator between the base and the fixed path, never
    two. The audit runs over the whole captured log at DEBUG, every logger.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    service = _FakeMemfw()
    if outcome == "http_error":
        service.related_status = 503
    elif outcome == "raises":
        service.related_error = urllib.error.URLError("connection reset")
    _install_http(monkeypatch, module, service)

    caplog.set_level(logging.DEBUG)
    result = module.memory_related("memory-1", limit=4)

    assert service.urls == [f"{_ENDPOINT}/health", f"{_ENDPOINT}/related"]
    assert service.calls[0]["method"] == "GET"
    assert service.calls[1]["method"] == "POST"
    assert service.calls[1]["payload"] == {"memory_id": "memory-1", "limit": 4}
    assert service.calls[1]["token"] == _TOKEN
    if outcome == "ok":
        assert result["ok"] is True
        assert result["related"] == _fixture("related.edges_ok")["results"]
        assert result["mode"] == "edges"
    elif outcome == "http_error":
        assert result == {"ok": False, "error_code": "internal", "related": []}
    else:
        assert result == {"ok": False, "error_code": "unreachable", "related": []}
    assert _SENTINEL not in caplog.text


@pytest.mark.parametrize("outcome", ["ok", "http_error", "raises"])
def test_memory_related_refuses_a_non_root_path_without_a_request(
    monkeypatch, caplog, outcome
):
    """A MemFW endpoint may only be a root address, so a path is refused.

    The Planner's ruling: the configured endpoint is ``scheme://host[:port]``
    and a non-root path is refused with ``bad_config`` and **no request**. The
    refusal happens before anything is sent, so the fake service is never
    called and the path token cannot reach a request line or a log line. The
    service is armed to succeed, fail or raise, and none of that can matter: a
    refused configuration sends nothing. The audit runs over the whole
    captured text at DEBUG, every logger, with no filtering and no slicing of
    ``caplog.text``.
    """
    module = _load_module()
    monkeypatch.setenv("MEMFW_READONLY_URL", f"http://memfw:18080/{_SENTINEL}")
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", _TOKEN)
    monkeypatch.delenv("MEMFW_READONLY_TOKEN_FILE", raising=False)
    service = _FakeMemfw()
    if outcome == "http_error":
        service.health_status = 503
    elif outcome == "raises":
        service.health_error = urllib.error.URLError("connection refused")
    _install_http(monkeypatch, module, service)

    caplog.set_level(logging.DEBUG)
    result = module.memory_related("memory-1")

    assert result == {"ok": False, "error_code": "bad_config", "related": []}
    assert service.calls == [], "a non-root path must not send a request"
    assert _SENTINEL not in caplog.text
    # The tool boundary answers the code to its caller instead of logging it, so
    # the capture above is a guard rather than a proof that something was
    # logged. The cache's own refusal point does log, and it names the reduced
    # peer only: that is what keeps the audit from passing by silence.
    caplog.clear()
    state = module._HEALTH.check(f"http://memfw:18080/{_SENTINEL}")

    assert state.reason == "bad_config"
    assert "configuration refused" in caplog.text
    assert _SENTINEL not in caplog.text


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        pytest.param(
            "http://[::1]:8765",
            ["http://[::1]:8765/health", "http://[::1]:8765/related"],
            id="ipv6-with-port",
        ),
        pytest.param(
            "http://[::1]",
            ["http://[::1]/health", "http://[::1]/related"],
            id="ipv6-without-port",
        ),
    ],
)
def test_memory_related_keeps_ipv6_brackets_in_the_related_url(
    monkeypatch, base, expected
):
    """``http://[::1]:8765`` is requested verbatim for health and related.

    An IPv6 literal is a valid host, so the validated base keeps the square
    brackets the authority needs: ``http://[::1]:8765/related`` is requested
    rather than being mangled into ``http://::1:8765/related`` and refused. The
    URLs the fake service received are asserted exactly, both of them.
    """
    module = _load_module()
    monkeypatch.setenv("MEMFW_READONLY_URL", base)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", _TOKEN)
    monkeypatch.delenv("MEMFW_READONLY_TOKEN_FILE", raising=False)
    service = _install_http(monkeypatch, module, _FakeMemfw())

    result = module.memory_related("memory-1")

    assert result["ok"] is True
    assert service.urls == expected


@pytest.mark.parametrize(
    ("base", "expected_health"),
    [
        pytest.param("http://[::1]:8765", "http://[::1]:8765/health", id="ipv6-port"),
        pytest.param("http://[::1]", "http://[::1]/health", id="ipv6-no-port"),
        pytest.param("http://127.0.0.1:8765", "http://127.0.0.1:8765/health", id="ipv4"),
    ],
)
def test_memory_related_keeps_ipv6_brackets_in_the_health_url(
    monkeypatch, base, expected_health
):
    """The rebuilt health line keeps IPv6 brackets; IPv4 is unchanged.

    The script's local mirror of the shared rebuild wraps an IPv6 host back in
    brackets, so ``http://[::1]:8765`` is requested verbatim instead of being
    mangled into ``http://::1:8765``. The URL the fake service received is
    asserted exactly.
    """
    module = _load_module()
    monkeypatch.setenv("MEMFW_READONLY_URL", base)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", _TOKEN)
    monkeypatch.delenv("MEMFW_READONLY_TOKEN_FILE", raising=False)
    service = _install_http(monkeypatch, module, _FakeMemfw())

    module.memory_related("memory-1")

    assert service.urls[0] == expected_health


def test_memory_related_never_leaks_a_token_from_the_contract_string(monkeypatch, caplog):
    """The contract string comes from the service and is not trusted."""
    module = _load_module()
    sentinel = "sentinel-audit-token"
    _configure_env(monkeypatch)
    _install_http(
        monkeypatch,
        module,
        _FakeMemfw(health={"ok": True, "contract": "memfw-http/3/" + sentinel}),
    )

    with caplog.at_level(logging.DEBUG):
        result = module.memory_related("memory-1")

    assert result["error_code"] == "invalid_response"
    assert "speaks contract" in caplog.text  # the warning really was logged
    assert sentinel not in caplog.text


def test_memory_related_never_leaks_the_token_when_the_endpoint_is_down(
    monkeypatch, caplog
):
    module = _load_module()
    _configure_env(monkeypatch, token="sup3r-secret-token")
    service = _install_http(monkeypatch, module, _FakeMemfw())
    service.health_error = urllib.error.URLError("connection refused")

    with caplog.at_level(logging.DEBUG):
        result = module.memory_related("memory-1")

    assert result["error_code"] == "unreachable"
    assert "health probe" in caplog.text  # the failure really was logged
    assert "sup3r-secret-token" not in repr(result)
    assert "sup3r-secret-token" not in caplog.text


def test_memory_related_reports_unreachable_when_the_related_call_fails(monkeypatch):
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_http(monkeypatch, module, _FakeMemfw())
    service.related_error = urllib.error.URLError("connection reset")

    result = module.memory_related("memory-1")

    assert result == {"ok": False, "error_code": "unreachable", "related": []}
    assert service.urls == [f"{_ENDPOINT}/health", f"{_ENDPOINT}/related"]


# --- the local mirrors of the shared modules --------------------------------


@pytest.mark.parametrize(
    ("env_token", "set_file_env", "file_text", "expected"),
    [
        ("env-token", False, None, "env-token"),
        (None, True, "file-token\n", "file-token"),
        ("env-token", True, "file-token\n", "env-token"),
        (None, True, None, ""),
        ("  env-token  ", False, None, "env-token"),
        (None, True, "  file-token  \n", "file-token"),
    ],
)
def test_local_token_resolution_matches_the_shared_resolver(
    monkeypatch, tmp_path, env_token, set_file_env, file_text, expected
):
    module = _load_module()
    monkeypatch.delenv("MEMFW_READONLY_TOKEN", raising=False)
    monkeypatch.delenv("MEMFW_READONLY_TOKEN_FILE", raising=False)
    token_file = tmp_path / "read-token"
    if file_text is not None:
        token_file.write_text(file_text, encoding="utf-8")
    if set_file_env:
        monkeypatch.setenv("MEMFW_READONLY_TOKEN_FILE", str(token_file))
    if env_token is not None:
        monkeypatch.setenv("MEMFW_READONLY_TOKEN", env_token)

    assert module._resolve_readonly_token() == expected
    assert shared_remote.resolve_memfw_token("READONLY") == expected
    assert module._resolve_readonly_token() == shared_remote.resolve_memfw_token(
        "READONLY"
    )


@pytest.mark.parametrize(
    "contract",
    ["memfw-http/2", "memfw-http/3", "memfw-http/2.1", "memfw-http/", "", None, 2],
)
def test_local_contract_major_matches_the_shared_helper(contract):
    module = _load_module()

    assert module._contract_major(contract) == shared_health.contract_major(contract)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://memfw:18080",
        "https://memfw",
        "http://memfw:18080/v1/search",
        "http://memfw:18080/v1?token=secret",
        "http://memfw:18080/v1#token=secret",
        "http://user:secret@memfw:18080/v1",
        "not a url at all",
        "",
    ],
)
def test_local_safe_endpoint_matches_the_shared_helper(endpoint):
    module = _load_module()

    assert module._safe_endpoint(endpoint) == shared_health.safe_endpoint(endpoint)


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        pytest.param("http://127.0.0.1:8765", "http://127.0.0.1:8765", id="ipv4"),
        pytest.param("http://[::1]:8765", "http://[::1]:8765", id="ipv6-with-port"),
        pytest.param("http://[::1]", "http://[::1]", id="ipv6-without-port"),
        pytest.param("http://memfw:18080", "http://memfw:18080", id="hostname"),
        pytest.param("https://memfw", "https://memfw", id="hostname-without-port"),
        # A non-root path is refused: a configured endpoint may only be a root
        # address, and every request is that base plus one fixed contract path.
        # (A trailing slash is the only spelling of "root with a separator" and
        # is normalised away rather than refused.)
        pytest.param(
            "http://memfw:18080/path-marker", None, id="non-root-path-refused"
        ),
        # ... and a URL that cannot be reduced to a bare peer is refused.
        pytest.param(
            "http://user:secret@memfw:18080/v1", None, id="userinfo-refused"
        ),
        pytest.param(
            "http://memfw:18080/v1?token=secret", None, id="query-refused"
        ),
        pytest.param(
            "http://memfw:18080/v1#token=secret", None, id="fragment-refused"
        ),
        pytest.param("not a url at all", None, id="unreadable"),
        pytest.param("", None, id="empty"),
    ],
)
def test_local_reduced_base_matches_the_shared_validated_base(endpoint, expected):
    """The script's mirror of the shared rebuild keeps agreeing with the server.

    The standalone script cannot import the server package, so it keeps a local
    copy of the validation boundary -- the same reason ``_safe_endpoint`` is
    mirrored. One table drives both: the script's ``_reduced_base`` and the
    server's shared ``health._validated_base`` answer the same vectors the same
    way, IPv6 brackets included, and each answer is the one the vector names.
    Pinning the expected outcome as well as the agreement is what keeps a drift
    from passing by being wrong in both copies at once.
    """
    module = _load_module()

    mirror = module._reduced_base(endpoint)
    shared = shared_health._validated_base(endpoint)

    assert mirror == shared
    assert mirror == expected


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"error": {"code": "unauthorized"}}, 401),
        ({"error": {"code": "not_found", "message": "memory not found"}}, 404),
        ({"error": {"code": "made_up"}}, 400),
        ({"error": "not a contract body"}, 400),
        ({}, 413),
        ({"contract": "memfw-http/2"}, 500),
        (None, 500),
        ("<html>proxy error</html>", 502),
    ],
)
def test_local_error_code_parsing_matches_the_shared_helper(body, status):
    module = _load_module()

    assert module._parse_error_code(body, status) == shared_errors.parse_error_code(
        body, status
    )


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param({}, id="contract-missing"),
        pytest.param({"contract": "memfw-http/1"}, id="contract-v1"),
        pytest.param({"contract": "memfw-http/3"}, id="contract-v3"),
    ],
)
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        pytest.param(401, "unauthorized", id="401"),
        pytest.param(403, "forbidden", id="403"),
        pytest.param(404, "not_found", id="404"),
        pytest.param(409, "conflict", id="409"),
        pytest.param(500, "internal", id="500"),
    ],
)
def test_a_foreign_envelope_falls_back_to_the_status_table(
    monkeypatch, envelope, status, expected
):
    """Only a v2 envelope may pass its code through.

    The mirror in the standalone script has the same rule as the shared
    parser: a body that does not carry this contract's marker cannot name the
    client's failure, so the status table decides.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    body = {**envelope, "error": {"code": "conflict", "message": "not a v2 envelope"}}
    _install_http(monkeypatch, module, _FakeMemfw(related_status=status, related=body))

    result = module.memory_related("memory-1")

    assert result == {"ok": False, "error_code": expected, "related": []}
    assert module._parse_error_code(body, status) == expected
    assert module._parse_error_code(body, status) == shared_errors.parse_error_code(
        body, status
    )


def test_a_v2_envelope_passes_its_code_through(monkeypatch):
    module = _load_module()
    _configure_env(monkeypatch)
    body = {"contract": "memfw-http/2", "error": {"code": "not_found"}}
    _install_http(monkeypatch, module, _FakeMemfw(related_status=404, related=body))

    result = module.memory_related("memory-1")

    assert result == {"ok": False, "error_code": "not_found", "related": []}


# --- the tracked MCP server configuration -----------------------------------


def test_memory_related_uses_token_file_and_mocked_http(monkeypatch, tmp_path):
    module = _load_module()
    token_file = tmp_path / "read-token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.delenv("MEMFW_READONLY_TOKEN", raising=False)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("MEMFW_READONLY_URL", "http://memfw:18080/")
    service = _install_http(monkeypatch, module, _FakeMemfw())

    result = module.memory_related("memory-1", limit=4)

    assert result["ok"] is True
    assert result["related"] == _fixture("related.edges_ok")["results"]
    assert service.urls == [f"{_ENDPOINT}/health", f"{_ENDPOINT}/related"]
    assert service.calls[0]["token"] is None
    assert service.calls[1]["token"] == "file-token"
    assert service.calls[1]["payload"] == {"memory_id": "memory-1", "limit": 4}
    assert [call["timeout"] for call in service.calls] == [1.0, 1.0]


def test_tracked_config_assembles_notes_subprocess_with_token_file_only(
    monkeypatch, tmp_path
):
    token_file = tmp_path / "notes-token"
    token_file.write_text("notes-file-token\n", encoding="utf-8")
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", "backend-only-token")
    monkeypatch.setenv("MEMFW_READONLY_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("MEMFW_READONLY_URL", "http://notes-mock.invalid:18080/")

    agent_config = load_yaml_config(
        str(_REPO_ROOT / "agent_config.yaml"), use_cache=False
    )
    notes_config = next(
        server
        for server in agent_config["mcp"]["servers"]
        if server["name"] == "memfw-notes"
    )
    connector = MCPServerConnector(MCPServerConfig(**notes_config))
    subprocess_env = connector._prepare_env()

    assert "MEMFW_READONLY_TOKEN" not in notes_config["env"]
    assert "MEMFW_READONLY_TOKEN" not in subprocess_env
    assert subprocess_env["MEMFW_READONLY_TOKEN_FILE"] == str(token_file)
    assert subprocess_env["MEMFW_READONLY_URL"] == "http://notes-mock.invalid:18080/"

    module = _load_module()
    service = _FakeMemfw()

    def fake_urlopen(request, timeout):
        status, payload = service(request, timeout)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return _FakeResponse(raw, status)

    with patch.dict(module.os.environ, subprocess_env, clear=True):
        monkeypatch.setattr(module._OPENER, "open", fake_urlopen)
        result = module.memory_related("memory-1", limit=4)

    assert result["ok"] is True
    assert service.urls == [
        "http://notes-mock.invalid:18080/health",
        "http://notes-mock.invalid:18080/related",
    ]
    assert service.calls[0]["token"] is None
    assert service.calls[1]["token"] == "notes-file-token"
    assert service.calls[1]["payload"] == {"memory_id": "memory-1", "limit": 4}
    assert [call["timeout"] for call in service.calls] == [1.0, 1.0]


# --- a 3xx is refused, never followed (redirect audit) ----------------------
#
# The audit's finding: ``urlopen``'s default opener follows a redirect on the
# caller's behalf, and urllib's rebuild copies every header but the content
# ones -- the read token included -- onto the request it makes against whatever
# host the ``Location`` names. A peer that answers ``/related`` with a
# cross-host 302 therefore receives the read token, and the answer that comes
# back from that host is reported as a success.
#
# The fix is a private opener built with ``urllib.request.build_opener`` where
# ``HTTPRedirectHandler`` is replaced by a subclass whose ``redirect_request``
# answers ``None``: nothing is installed globally, and no second request is
# ever built. A 3xx becomes the stable reason code ``redirect_refused``, a
# redirected health probe is a failure that is not cached, and the httpx
# clients say ``follow_redirects=False`` at their own construction sites.
#
# The fake transport below is patched in at ``AbstractHTTPHandler.do_open`` --
# the one place urllib hands a request to ``http.client`` -- so everything above
# it stays the real thing: ``HTTPErrorProcessor`` and whichever redirect handler
# the script installed. A 3xx is decided by the real handler, not by the fake
# deciding whether to follow it. Patching ``urlopen`` instead would sit above
# the redirect handler and could not tell a refusal from a bypass.

import email.message  # noqa: E402

import httpx  # noqa: E402

#: The host a redirect points at: another peer, never the configured one.
_REDIRECT_HOST = "http://unconfigured.invalid"


class _TransportResponse:
    """What ``http.client`` hands back, as the urllib chain reads it.

    ``code``, ``msg`` and ``info()`` are what ``HTTPErrorProcessor`` reads to
    decide whether a status is an error, and ``read()`` is what both the success
    path and ``HTTPError``'s body read go through. Nothing here decides
    anything: the handlers above it do.
    """

    def __init__(self, status: int, headers, body: bytes) -> None:
        self.status = status
        self.code = status
        self.msg = "OK"
        self._headers = headers
        self._body = body

    def info(self):
        return self._headers

    def read(self, amount: int = -1) -> bytes:
        body, self._body = self._body, b""
        return body

    def close(self) -> None:
        """The transport holds no socket, so there is nothing to close."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


class _Transport:
    """A fake transport for ``do_open``: one canned answer per fixed path.

    The answers are keyed by the fixed contract paths, the way the fake service
    above keys them, so a redirect is answered by naming the status and the
    ``Location`` the peer sent. Every request that reaches the transport is
    recorded -- URL, method, timeout and the token header -- because the count
    of those requests is the proof that a refused redirect was not followed.
    """

    def __init__(
        self,
        *,
        health: dict | None = None,
        related: dict | None = None,
    ) -> None:
        self.health = (
            {"status": 200, "body": _fixture("health.ok")}
            if health is None
            else health
        )
        self.related = (
            {"status": 200, "body": _fixture("related.edges_ok")}
            if related is None
            else related
        )
        self.calls: list[dict] = []

    def __call__(self, request) -> _TransportResponse:
        answer = self.health if request.full_url.endswith("/health") else self.related
        self.calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "timeout": request.timeout,
                "headers": dict(request.headers),
                "token": request.get_header("X-memfw-token"),
            }
        )
        headers = email.message.Message()
        location = answer.get("location")
        if location:
            headers["Location"] = location
        body = answer.get("body")
        raw = b"" if body is None else (
            body if isinstance(body, bytes) else json.dumps(body).encode()
        )
        return _TransportResponse(int(answer["status"]), headers, raw)

    @property
    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]

    @property
    def tokens(self) -> list[str | None]:
        """The read token every request carried, in the order they were made."""
        return [call["token"] for call in self.calls]


def _install_transport(monkeypatch, module, service: _Transport) -> _Transport:
    """Route the transport to the fake service; no real host is ever reached.

    ``do_open`` is patched on the class rather than on one handler instance, so
    the opener the script built -- and the global one ``urlopen`` uses -- both
    pick the fake up, and the redirect handler still runs on what it returns.
    """

    def fake_do_open(_handler, _http_class, request, **_http_conn_args):
        return service(request)

    monkeypatch.setattr(
        module.urllib.request.AbstractHTTPHandler, "do_open", fake_do_open
    )
    return service


def test_memory_related_refuses_a_cross_host_redirect_without_following_it(
    monkeypatch,
):
    """A 302 that names another host is one request and ``redirect_refused``.

    The default opener would rebuild the request against the ``Location`` and
    copy the read token onto it, so the answer from a host that was never
    configured would be reported as a success. The probe is answered normally
    and the related call with the redirect, and both the URLs the transport
    received and the token each of them carried are asserted exactly: the
    probe, the related call, and nothing else.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_transport(
        monkeypatch,
        module,
        _Transport(
            related={
                "status": 302,
                "location": f"{_REDIRECT_HOST}/path-marker",
                "body": None,
            }
        ),
    )

    result = module.memory_related("memory-1", limit=4)

    assert result == {"ok": False, "error_code": "redirect_refused", "related": []}
    assert service.urls == [f"{_ENDPOINT}/health", f"{_ENDPOINT}/related"]
    assert service.tokens == [None, _TOKEN]
    assert _REDIRECT_HOST not in " ".join(service.urls)


def test_memory_related_refuses_a_same_host_redirect_to_a_non_fixed_path(
    monkeypatch,
):
    """A 302 that stays on the same host is refused just the same.

    The ``Location`` points at a path this client never requests -- a path the
    contract does not define -- so following it would ask a URL the service was
    never meant to answer, on the same host, carrying the same token. The
    assertions are the cross-host ones: no follow-up request, no forwarding, and
    the stable reason code.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_transport(
        monkeypatch,
        module,
        _Transport(
            related={
                "status": 302,
                "location": f"{_ENDPOINT}/some-other-path",
                "body": None,
            }
        ),
    )

    result = module.memory_related("memory-1", limit=4)

    assert result == {"ok": False, "error_code": "redirect_refused", "related": []}
    assert service.urls == [f"{_ENDPOINT}/health", f"{_ENDPOINT}/related"]
    assert service.tokens == [None, _TOKEN]


def test_memory_related_does_not_cache_a_redirected_health_probe(monkeypatch):
    """A redirected probe is a failure, and it is not cached.

    ``/health`` is probed once per endpoint per five-minute window, so caching a
    redirected probe would freeze the failure for the whole window: the second
    call has to probe again rather than answer from the cache. And because the
    probe never negotiated the contract, the related request is not made at all
    -- the health probe is the only line the transport ever receives.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_transport(
        monkeypatch,
        module,
        _Transport(
            health={
                "status": 302,
                "location": f"{_REDIRECT_HOST}/path-marker",
                "body": None,
            }
        ),
    )

    first = module.memory_related("memory-1")
    second = module.memory_related("memory-1")

    assert first["ok"] is False
    assert first["related"] == []
    assert first == second
    assert service.urls == [f"{_ENDPOINT}/health", f"{_ENDPOINT}/health"]
    assert f"{_ENDPOINT}/related" not in service.urls
    assert _REDIRECT_HOST not in " ".join(service.urls)


def test_memory_related_still_succeeds_on_a_normal_answer(monkeypatch):
    """The control: a 200 answer is untouched by the redirect refusal.

    The refusal has to be narrow. A probe and a related call that answer
    normally still succeed, through the same transport fake, so a client that
    refused every answer would not pass this suite.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_transport(monkeypatch, module, _Transport())

    result = module.memory_related("memory-1", limit=4)

    assert result["ok"] is True
    assert result["related"] == _fixture("related.edges_ok")["results"]
    assert result["mode"] == "edges"
    assert service.urls == [f"{_ENDPOINT}/health", f"{_ENDPOINT}/related"]
    assert service.tokens == [None, _TOKEN]


def test_memory_related_still_passes_a_403_code_through(monkeypatch):
    """The control: a 403 is the server's own code, not the redirect one.

    A refusal that answered ``redirect_refused`` for every failure would be
    worse than the defect: the contract's error codes have to keep reaching the
    caller, and the probe still has to run before the related call.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_transport(
        monkeypatch,
        module,
        _Transport(
            related={"status": 403, "body": {"error": {"code": "forbidden"}}}
        ),
    )

    result = module.memory_related("memory-1")

    assert result == {"ok": False, "error_code": "forbidden", "related": []}
    assert service.urls == [f"{_ENDPOINT}/health", f"{_ENDPOINT}/related"]


def test_memory_related_refuses_a_redirect_that_carries_the_token_in_its_target(
    monkeypatch, caplog
):
    """A redirect whose ``Location`` carries a token in the URL is refused.

    The sentinel sits in the ``Location`` the peer chose, so it may reach
    neither a second request line nor a log line: a follow-up would put it in
    the URL of a request to another host, and a log line that named the target
    would put it in every log shipper the fleet runs. The audit runs over the
    whole captured text at DEBUG, every logger, with no filtering and no slicing
    of ``caplog.text``. The health probe is answered normally, so the redirect
    is the only thing that can go wrong and the related call is the only request
    that carries the read token.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    target = f"{_REDIRECT_HOST}/path-marker?token={_SENTINEL}"
    service = _install_transport(
        monkeypatch,
        module,
        _Transport(related={"status": 302, "location": target, "body": None}),
    )

    caplog.set_level(logging.DEBUG)
    result = module.memory_related("memory-1", limit=4)

    assert result == {"ok": False, "error_code": "redirect_refused", "related": []}
    assert service.urls == [f"{_ENDPOINT}/health", f"{_ENDPOINT}/related"]
    assert service.tokens == [None, _TOKEN]
    assert _SENTINEL not in " ".join(service.urls)
    assert _SENTINEL not in repr(result)
    assert _SENTINEL not in caplog.text


@pytest.mark.asyncio
async def test_the_remote_client_refuses_a_redirect_without_following_it(monkeypatch):
    """The shared httpx client answers a 302 with one request and a failure.

    ``RemoteMemfwMemory`` builds its own client when the caller does not hand
    one in, and that construction site is where ``follow_redirects=False`` has
    to be written: a client left at httpx's default must not turn a 302 into a
    second, token-carrying request to another host. The wrapper below keeps
    every keyword the construction passed and swaps the mock transport in, so
    the flag is read back off the real construction and the request count off
    the real transport. A ``302`` is a failure for this client either way -- the
    contract has no success in the 3xx range -- so what the test pins is that
    the failure costs exactly one request.
    """
    calls: list[httpx.Request] = []
    construction: list[dict] = []
    target = f"{_REDIRECT_HOST}/path-marker?token={_SENTINEL}"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"Location": target}, request=request)

    class _RecordingClient(httpx.AsyncClient):
        """The client the provider builds, with the mock transport injected."""

        def __init__(self, *args, **kwargs) -> None:
            construction.append(kwargs)
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    provider = shared_remote.RemoteMemfwMemory(
        "http://memfw:18080", owner_id="user-a", token="test-token"
    )

    with pytest.raises(shared_errors.MemfwContractError):
        await provider.recall("user-a", "query", 5, ["user"])

    assert construction, "the provider builds its own client here"
    assert construction[0].get("follow_redirects") is False
    assert len(calls) == 1, "a refused redirect must not be followed"
    assert str(calls[0].url) == "http://memfw:18080/search"
    assert calls[0].headers["X-Memfw-Token"] == "test-token"
    assert _SENTINEL not in " ".join(str(call.url) for call in calls)


# --- a malformed ``Location`` is a refusal, not an outage -------------------
#
# The audit's follow-up. The refusal above decides on the *status*, but the
# handler that ships in the standard library does not:
# ``HTTPRedirectHandler.http_error_302`` calls ``urlparse(newurl)`` to "fix a
# possible malformed URL" *before* it ever asks ``redirect_request``. A peer
# that answers a 3xx with ``Location: http://[broken`` therefore makes the
# opener raise ``ValueError: Invalid IPv6 URL``. That exception is not an
# ``HTTPError``, so it escapes the call as a transport failure and the caller
# classifies it ``unreachable`` -- an outage -- for a peer that is up, talking,
# and redirecting. ``Location: http://[::1`` fails the same way.
#
# The fix is a ``urllib.request.BaseHandler`` subclass with
# ``handler_order = 400``, ahead of the library's ``HTTPRedirectHandler`` at
# 500, whose ``http_error_301/302/303/307/308`` raise a local ``_RedirectRefused``
# without reading or parsing the ``Location`` header at all; the call sites turn
# that into the stable reason code ``redirect_refused``. The tests below pin the
# observable contract rather than that implementation -- they do not care
# whether a refusal arrives as an ``HTTPError`` carrying the 3xx status or as a
# local exception -- and they run offline through the same ``do_open`` seam the
# redirect audit uses, so the real handler chain above the transport is what
# decides.

#: The six ``Location`` vectors this audit pins. Two are URLs
#: ``urllib.parse`` cannot read at all -- an unterminated bracket and a
#: truncated IPv6 literal -- which is exactly where the old classification
#: broke. One is the empty header and one is a header that is missing
#: altogether: two different malformed answers that must land on the same
#: reason code. The last two are well formed, so the refusal is not merely a
#: parse failure: a cross-host target, and a same-host target at a path this
#: client never requests.
_LOCATION_VECTORS = [
    pytest.param("http://[broken", id="unterminated-bracket"),
    pytest.param("http://[::1", id="truncated-ipv6"),
    pytest.param("", id="empty-header"),
    pytest.param(None, id="header-missing"),
    pytest.param(f"{_REDIRECT_HOST}/path-marker", id="cross-host"),
    pytest.param(f"{_ENDPOINT}/some-other-path", id="same-host-other-path"),
]


class _LocationTransport:
    """A fake transport that answers one fixed path with a chosen 3xx.

    ``answered_on`` names the fixed path that gets the 3xx -- ``/health`` or
    ``/related`` -- and every other path gets the normal 2xx contract answer,
    so the redirect is the only thing that can go wrong. ``location`` is the
    value the peer puts in the header: ``None`` omits the header altogether and
    ``""`` sends an empty one, which are two different malformed answers and
    are kept apart on purpose.

    Every request that reaches the transport is recorded -- URL, method,
    timeout and token header -- because the count of those requests is the
    proof that a refused redirect was not followed.
    """

    def __init__(
        self,
        *,
        location: str | None,
        status: int = 302,
        answered_on: str = "/related",
    ) -> None:
        self._location = location
        self._status = status
        self._answered_on = answered_on
        self.calls: list[dict] = []

    def __call__(self, request) -> _TransportResponse:
        self.calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "timeout": request.timeout,
                "token": request.get_header("X-memfw-token"),
            }
        )
        if request.full_url.endswith(self._answered_on):
            status, body = self._status, b""
        elif request.full_url.endswith("/health"):
            status, body = 200, json.dumps(_fixture("health.ok")).encode()
        else:
            status, body = 200, json.dumps(_fixture("related.edges_ok")).encode()
        headers = email.message.Message()
        if self._location is not None:
            headers["Location"] = self._location
        return _TransportResponse(status, headers, body)

    @property
    def urls(self) -> list[str]:
        """Every request line the transport received, in order."""
        return [call["url"] for call in self.calls]

    @property
    def tokens(self) -> list[str | None]:
        """The read token every request carried, in the order they were made."""
        return [call["token"] for call in self.calls]


def _assert_refused_without_a_follow_up(service: _LocationTransport, result) -> None:
    """The three assertions every redirect vector has to satisfy.

    The reason code is the stable one, the request list is *exactly* the fixed
    contract paths -- one probe, one related call, nothing else -- and every
    request line still ends at one of those two paths, which is what rules out
    a second request to whatever the ``Location`` named. The audit runs over
    the whole captured log at DEBUG, every logger, with no filtering and no
    slicing of ``caplog.text``.
    """
    assert result == {"ok": False, "error_code": "redirect_refused", "related": []}
    assert service.urls == [f"{_ENDPOINT}/health", f"{_ENDPOINT}/related"]
    assert all(url.endswith(("/health", "/related")) for url in service.urls)
    assert _REDIRECT_HOST not in " ".join(service.urls)


@pytest.mark.parametrize("location", _LOCATION_VECTORS)
def test_memory_related_refuses_a_malformed_location_on_the_related_call(
    monkeypatch, caplog, location
):
    """The related call: a 3xx is ``redirect_refused`` whatever it carries.

    The probe is answered normally, so the redirect is the only thing that can
    go wrong and the related call is the only request that carries the read
    token. The token is forwarded to the configured peer and to nowhere else:
    there is no second request, so the ``Location`` target is never contacted
    and never receives it. The private opener is also never installed
    process-wide -- the guard the reviewer's probe pins too.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_transport(
        monkeypatch,
        module,
        _LocationTransport(location=location, answered_on="/related"),
    )
    global_opener = module.urllib.request._opener

    caplog.set_level(logging.DEBUG)
    result = module.memory_related("memory-1", limit=4)

    _assert_refused_without_a_follow_up(service, result)
    assert service.tokens == [None, _TOKEN]
    assert module.urllib.request._opener is global_opener
    assert _TOKEN not in " ".join(service.urls)
    assert _TOKEN not in repr(result)
    assert _TOKEN not in caplog.text


@pytest.mark.parametrize("location", _LOCATION_VECTORS)
def test_memory_related_refuses_a_malformed_location_on_the_health_probe(
    monkeypatch, caplog, location
):
    """The health probe: a 3xx is ``redirect_refused`` whatever it carries.

    ``/health`` needs no authentication, so the probe is the one request the
    transport ever receives and it carries no token at all: a refused redirect
    on the probe cannot forward one anywhere, and the probe never becomes the
    endpoint's answer.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_transport(
        monkeypatch,
        module,
        _LocationTransport(location=location, answered_on="/health"),
    )
    global_opener = module.urllib.request._opener

    caplog.set_level(logging.DEBUG)
    result = module.memory_related("memory-1")

    assert result == {"ok": False, "error_code": "redirect_refused", "related": []}
    assert service.urls == [f"{_ENDPOINT}/health"]
    assert all(url.endswith("/health") for url in service.urls)
    assert service.tokens == [None]
    assert module.urllib.request._opener is global_opener
    assert _REDIRECT_HOST not in " ".join(service.urls)
    assert _TOKEN not in " ".join(service.urls)
    assert _TOKEN not in repr(result)
    assert _TOKEN not in caplog.text


@pytest.mark.parametrize("location", _LOCATION_VECTORS)
def test_memory_related_probes_again_after_a_malformed_location_probe(
    monkeypatch, location
):
    """A redirected probe is a failure, and it is not cached.

    ``/health`` is probed once per endpoint per five-minute window, so caching
    a redirected probe would freeze the failure for the whole window: the
    second call has to probe again rather than answer from the cache. The URLs
    list is therefore two health probes, not one, and both answers are the
    same stable reason code.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_transport(
        monkeypatch,
        module,
        _LocationTransport(location=location, answered_on="/health"),
    )

    first = module.memory_related("memory-1")
    second = module.memory_related("memory-1")

    assert first == {"ok": False, "error_code": "redirect_refused", "related": []}
    assert first == second
    assert service.urls == [f"{_ENDPOINT}/health", f"{_ENDPOINT}/health"]
    assert service.urls.count(f"{_ENDPOINT}/health") == 2


@pytest.mark.parametrize("location", _LOCATION_VECTORS)
def test_memory_related_never_requests_related_after_a_malformed_location_probe(
    monkeypatch, location
):
    """After a redirected probe nothing downstream is requested.

    The probe never negotiated the contract, so there is no negotiated state to
    rely on and nowhere to send the read token: ``/related`` -- and with it the
    only request that would carry the token -- is not requested at all. The
    health probe is the only line the transport ever receives.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_transport(
        monkeypatch,
        module,
        _LocationTransport(location=location, answered_on="/health"),
    )

    module.memory_related("memory-1")

    assert service.urls == [f"{_ENDPOINT}/health"]
    assert f"{_ENDPOINT}/related" not in service.urls
    assert service.tokens == [None]


def test_a_normal_2xx_answer_succeeds_and_is_still_cached(monkeypatch):
    """The control: a 200 answer is untouched by the redirect refusal.

    The refusal has to be narrow. A probe and a related call that answer
    normally still succeed through the same transport fake -- so a client that
    refused every answer would not pass this suite -- and the successful probe
    is still cached for its window, which is what tells this control apart
    from the redirected one above: only the failure is re-probed.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_transport(monkeypatch, module, _Transport())

    result = module.memory_related("memory-1", limit=4)
    module.memory_related("memory-1", limit=4)

    assert result["ok"] is True
    assert result["related"] == _fixture("related.edges_ok")["results"]
    assert result["mode"] == "edges"
    assert service.urls == [
        f"{_ENDPOINT}/health",
        f"{_ENDPOINT}/related",
        f"{_ENDPOINT}/related",
    ]
    # One probe for two calls: the successful answer is cached, unlike a
    # redirected one, which is a failure and is probed again.
    assert service.urls.count(f"{_ENDPOINT}/health") == 1
    assert service.tokens == [None, _TOKEN, _TOKEN]


def test_a_403_still_passes_its_own_code_through(monkeypatch):
    """The control: a 403 is the server's own code, not the redirect one.

    A refusal that answered ``redirect_refused`` for every failure would be
    worse than the defect: the contract's error codes have to keep reaching the
    caller, and the probe still has to run before the related call.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_transport(
        monkeypatch,
        module,
        _Transport(
            related={"status": 403, "body": {"error": {"code": "forbidden"}}}
        ),
    )

    result = module.memory_related("memory-1")

    assert result == {"ok": False, "error_code": "forbidden", "related": []}
    assert result.get("error_code") != "redirect_refused"
    assert service.urls == [f"{_ENDPOINT}/health", f"{_ENDPOINT}/related"]


@pytest.mark.parametrize("location", _LOCATION_VECTORS)
def test_memory_related_never_leaks_the_token_into_a_malformed_redirect(
    monkeypatch, caplog, location
):
    """The read token reaches nowhere when the ``Location`` is malformed.

    A refused redirect is what keeps the token on the peer the configuration
    named, so the token has to be absent from every request line, from the
    result the tool returns, and from every log line. The audit runs over the
    whole captured text at DEBUG -- every logger, no filtering, no slicing of
    ``caplog.text`` -- and the token is a distinctive value that no fixture and
    no default carries, so a match cannot be an accident. The one request that
    legitimately carries it is the related call itself; the follow-up that
    would have forwarded it is never made.
    """
    module = _load_module()
    token = "sup3r-secret-token"
    _configure_env(monkeypatch, token=token)
    service = _install_transport(
        monkeypatch,
        module,
        _LocationTransport(location=location, answered_on="/related"),
    )

    caplog.set_level(logging.DEBUG)
    result = module.memory_related("memory-1", limit=4)

    _assert_refused_without_a_follow_up(service, result)
    assert service.tokens == [None, token]
    assert token not in " ".join(service.urls)
    assert token not in repr(result)
    assert token not in caplog.text


#: Two more unreadable ``Location`` shapes, taken from the reviewer's Codex
#: probe so this suite covers the same ground: a bracketed host that is not an
#: IPv6 literal at all (``urlparse`` raises ``ValueError: 'xyz' does not appear
#: to be an IPv4 or IPv6 address``), and a host with a non-ASCII character that
#: has to be percent-encoded before it can be used at all. Both are refused the
#: same way, which is the point of pinning them: the refusal is decided at the
#: status, so nothing about the target can change it.
_PROBE_LOCATION_VECTORS = [
    pytest.param("http://[xyz]/target", id="bracket-not-ipv6"),
    pytest.param("http://t\u00e9st.invalid/path", id="non-ascii-host"),
]


@pytest.mark.parametrize("location", _PROBE_LOCATION_VECTORS)
def test_memory_related_refuses_the_probe_malformed_location_on_the_related_call(
    monkeypatch, caplog, location
):
    """The related call: the probe's remaining malformed shapes are refused.

    Same assertions as the audit's own twelve combinations -- the stable reason
    code, exactly the two fixed contract paths and no follow-up request, the
    token forwarded to the configured peer and nowhere else, and the private
    opener never installed process-wide.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_transport(
        monkeypatch,
        module,
        _LocationTransport(location=location, answered_on="/related"),
    )
    global_opener = module.urllib.request._opener

    caplog.set_level(logging.DEBUG)
    result = module.memory_related("memory-1", limit=4)

    _assert_refused_without_a_follow_up(service, result)
    assert service.tokens == [None, _TOKEN]
    assert module.urllib.request._opener is global_opener
    assert _TOKEN not in " ".join(service.urls)
    assert _TOKEN not in repr(result)
    assert _TOKEN not in caplog.text


@pytest.mark.parametrize("location", _PROBE_LOCATION_VECTORS)
def test_memory_related_refuses_the_probe_malformed_location_on_the_health_probe(
    monkeypatch, caplog, location
):
    """The health probe: the probe's remaining malformed shapes are refused.

    The probe is the one request the transport receives and it carries no
    token, so a refused redirect on the probe cannot forward one anywhere.
    """
    module = _load_module()
    _configure_env(monkeypatch)
    service = _install_transport(
        monkeypatch,
        module,
        _LocationTransport(location=location, answered_on="/health"),
    )
    global_opener = module.urllib.request._opener

    caplog.set_level(logging.DEBUG)
    result = module.memory_related("memory-1")

    assert result == {"ok": False, "error_code": "redirect_refused", "related": []}
    assert service.urls == [f"{_ENDPOINT}/health"]
    assert all(url.endswith("/health") for url in service.urls)
    assert service.tokens == [None]
    assert module.urllib.request._opener is global_opener
    assert _REDIRECT_HOST not in " ".join(service.urls)
    assert _TOKEN not in " ".join(service.urls)
    assert _TOKEN not in repr(result)
    assert _TOKEN not in caplog.text
