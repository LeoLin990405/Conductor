#!/usr/bin/env python3
"""Read-only MCP tool for the private QMD notes library.

This is deliberately separate from memory federation: notes are evidence, not
memory proposals and never become Markdown memory automatically.

``memory_related`` speaks the MemFW HTTP contract v2 (``memfw-http/2``). The
contract is the only source of truth for that client:

* section 1 -- every response carries ``contract``; before its first use the
  client probes ``GET /health`` and caches the answer for five minutes per
  endpoint. A *failed* probe is not cached, so the next call tries again.
* section 4 -- ``/health`` and ``/related`` get one second; ``limit`` is 1..50.
* section 6 -- errors are ``{"error": {"code": ...}}`` and the code is passed
  through instead of being collapsed into an exception class name.
* section 7 -- ``mode``, ``degraded`` and ``reasons`` are passed through;
  ``reasons`` may only use the contract's vocabulary.

Every request goes through one private opener that refuses a redirect at the
status, before the ``Location`` header is read at all: a 3xx is the stable
failure ``redirect_refused``, no request is made to the ``Location`` target,
and the read token is never forwarded to a host the configuration did not
name. The process-wide opener is left alone.

The script runs standalone (``python scripts/memfw_notes_mcp.py``), so the
package under ``src`` is not importable here. The pieces it needs from that
package -- the contract vocabulary, the status-code table, the token resolution
and the endpoint validation boundary -- are mirrored locally, and
``tests/unit/scripts/test_memfw_notes_mcp.py`` proves the mirrors still agree
with the shared modules.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, NamedTuple
from urllib.parse import urlsplit

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP("memfw-notes")
_ALLOWED_SOURCES = {"qmd", "groove", "local", "auto"}

# --- MemFW HTTP contract v2 (memfw-http/2) ---------------------------------

_CONTRACT_NAME = "memfw-http/2"
_CONTRACT_MAJOR = 2
#: Contract section 4: ``/health`` and ``/related`` get one second.
_REQUEST_TIMEOUT_SECONDS = 1.0
#: Contract section 1: the probe answer is cached for five minutes.
_HEALTH_CACHE_SECONDS = 300.0
#: The one fixed path the probe requests. The request line is rebuilt from the
#: validated base plus this path, so the configured string -- with whatever
#: path, query or fragment it carried -- never reaches urllib.
_HEALTH_PATH = "/health"
#: The one fixed path the related request targets. Every request is the
#: validated base plus the fixed path of its own call and nothing else, so this
#: constant is the only place ``/related`` is spelled.
_RELATED_PATH = "/related"
#: The stable reason a configured endpoint is refused before any request: its
#: URL was not a root address -- it carried a userinfo, a query, a fragment or
#: a path -- so it cannot be reduced to a bare peer and must not be sent
#: anywhere.
_REASON_BAD_CONFIG = "bad_config"
#: The stable reason a request answered with a 3xx is refused: this client does
#: not follow redirects, so the ``Location`` target is never requested and the
#: read token is never forwarded to it. A probe that got one is a failure like
#: any other, so it is not cached.
_REASON_REDIRECT_REFUSED = "redirect_refused"
#: Contract section 4: ``limit`` is 1..50.
_LIMIT_MIN = 1
_LIMIT_MAX = 50
#: Contract section 6: HTTP status -> error code.
_ERROR_CODES_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "too_large",
    500: "internal",
}
#: The codes a client produces locally; the server never sends them.
_LOCAL_ERROR_CODES = frozenset(
    {"timeout", "unreachable", "invalid_response", "write_token_missing"}
)
#: Every code this client accepts from the server or reports on its own.
_KNOWN_ERROR_CODES = frozenset(_ERROR_CODES_BY_STATUS.values()) | _LOCAL_ERROR_CODES
#: Contract section 7: the only reasons a result may carry.
_REASONS_VOCABULARY = frozenset(
    {
        "link",
        "related_field",
        "shared_tag",
        "shared_course",
        "shared_entity",
        "lexical",
        "semantic",
        "section",
    }
)


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    return "" if value == f"${{{name}}}" else value


@mcp.tool()
def notes_search(
    query: str,
    limit: int = 5,
    source: Literal["qmd", "groove", "local", "auto"] = "qmd",
) -> dict[str, object]:
    """Search the private notes library (read-only); returns paths and snippets."""
    if not query.strip():
        return {"ok": False, "error": "query required", "results": []}
    if source not in _ALLOWED_SOURCES:
        return {"ok": False, "error": "invalid source", "results": []}
    bounded = max(1, min(int(limit), 20))
    try:
        completed = subprocess.run(
            [
                "memfw-fleet",
                "notes",
                "search",
                query[:512],
                "--limit",
                str(bounded),
                "--source",
                source,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": type(exc).__name__, "results": []}
    if completed.returncode != 0:
        return {"ok": False, "error": "notes search failed", "results": []}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid notes response", "results": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return {"ok": False, "error": "invalid notes response", "results": []}
    return {
        "ok": bool(payload.get("ok")),
        "query": query,
        "source": source,
        "results": payload["results"][:bounded],
        "errors": payload.get("errors", []),
    }


@mcp.tool()
def notes_related(path: str, limit: int = 10) -> dict[str, object]:
    """Read derived related-note edges; read-only, does not write notes."""
    if not path.strip():
        return {"ok": False, "error": "path required", "related": []}
    cli = _env("MEMFW_CLI")
    related_db = _env("MEMFW_RELATED_DB")
    if not cli:
        return {"ok": False, "error": "MEMFW_CLI missing", "related": []}
    if not related_db:
        return {"ok": False, "error": "MEMFW_RELATED_DB missing", "related": []}
    bounded = max(1, min(int(limit), 20))
    try:
        completed = subprocess.run(
            [
                cli,
                "notes",
                "related",
                path[:512],
                "--related-db",
                related_db,
                "--limit",
                str(bounded),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": type(exc).__name__, "related": []}
    if completed.returncode != 0:
        return {"ok": False, "error": "related search failed", "related": []}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid related response", "related": []}
    return {
        "ok": True,
        "path": path,
        "related": payload.get("related", []),
        "limit": bounded,
    }


def _resolve_readonly_token() -> str:
    """The read token: env first, then a token file; empty when neither works.

    This mirrors ``remote.resolve_memfw_token("READONLY")`` exactly -- the
    script runs standalone, so ``src`` is not importable and the logic has to
    be local. ``tests/unit/scripts/test_memfw_notes_mcp.py`` asserts that the
    two keep behaving the same.
    """
    token = os.environ.get("MEMFW_READONLY_TOKEN", "").strip()
    if token:
        return token
    token_file = os.environ.get("MEMFW_READONLY_TOKEN_FILE", "").strip()
    if not token_file:
        return ""
    try:
        return Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _contract_major(contract: Any) -> int | None:
    """The major version of a ``memfw-http/<major>`` contract string.

    ``None`` for anything that is not that shape, including a missing value.
    """
    if not isinstance(contract, str):
        return None
    name, _, tail = contract.strip().partition("/")
    if name != "memfw-http":
        return None
    try:
        return int(tail)
    except ValueError:
        return None


def _error_code_for_status(status_code: int) -> str:
    """The contract's code for a status; anything unmapped is ``internal``."""
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        return "internal"
    return _ERROR_CODES_BY_STATUS.get(status, "internal")


def _is_contract_envelope(body: Any) -> bool:
    """Whether ``body`` is an envelope of the contract this client speaks.

    Contract section 6: the envelope carries the contract marker. A body
    without one, or with another major version, is not read as a contract
    answer.
    """
    if not isinstance(body, dict):
        return False
    return _contract_major(body.get("contract")) == _CONTRACT_MAJOR


def _parse_error_code(body: Any, status_code: int) -> str:
    """Read ``error.code`` out of a contract error body.

    Only a **valid v2 envelope** may pass its code through: the body has to
    carry the contract marker of this contract (``memfw-http/2``). A body with
    no contract, with a v1 or a v3 contract, or with a code outside the
    contract's vocabulary falls back to the status-code table. A client must
    never adopt a code from a service that does not speak this contract, and
    it must never invent a code the server did not send.
    """
    if _is_contract_envelope(body):
        error = body.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            if isinstance(code, str) and code.strip():
                candidate = code.strip()
                if candidate in _KNOWN_ERROR_CODES:
                    return candidate
    return _error_code_for_status(status_code)


def _safe_endpoint(endpoint: str) -> str:
    """The endpoint reduced to scheme, host and port.

    A URL may carry a password, a path, a query string with a token, or a
    fragment. A log line needs enough to identify the peer and nothing else,
    so everything but the scheme, the host and the port is dropped. An
    endpoint that cannot be read at all becomes a fixed placeholder rather
    than an echo of whatever was configured.
    """
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "<endpoint>"
    if not parts.scheme or not host:
        return "<endpoint>"
    return f"{parts.scheme}://{host}:{port}" if port else f"{parts.scheme}://{host}"


def _reduced_base(endpoint: str) -> str | None:
    """The endpoint reduced to ``scheme://host[:port]``, or ``None`` to refuse.

    This is the validation boundary, mirroring ``health._validated_base``. A
    configured endpoint may only be a ROOT address: ``scheme://host[:port]``
    with an empty path or ``/``. A configured URL may also carry a password in
    its userinfo, a query string holding a token, or a fragment, and none of
    those may ride along on a request line. A URL that carries any of them, or
    a path of its own, is **refused** (``None``): the caller treats it as a bad
    configuration and sends nothing at all. A URL that cannot be read into a
    scheme, a host and a port is refused for the same reason.

    The reduced base is the one normalized base every request is rebuilt from
    -- the health probe and the related call each append their own fixed path
    to it -- so a URL that merely carries a path is not quietly rewritten into
    a request against some other path: it is refused. Unlike ``_safe_endpoint``
    this is not a logging helper: it decides whether a request may be made at
    all, so it fails closed rather than echoing a placeholder. An IPv6 literal
    keeps its square brackets (``http://[::1]:8765``), because a host without
    them is not a valid authority.
    """
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return None
    if "@" in (parts.netloc or "") or parts.query or parts.fragment:
        return None
    # A configured endpoint may only be a root address. Every request is this
    # base plus one fixed contract path, so a configured path has nowhere to
    # go: dropping it would silently retarget the request, and keeping it would
    # ask a URL the service does not serve. It is refused instead.
    if parts.path not in ("", "/"):
        return None
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not host:
        return None
    # ``hostname`` reports an IPv6 literal without its square brackets (``::1``
    # for ``[::1]``), so the rebuild wraps it back: ``http://[::1]:8765`` is a
    # valid authority, ``http://::1:8765`` is not. A hostname or an IPv4 literal
    # carries no colon and is left exactly as it is.
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}:{port}" if port else f"{parts.scheme}://{host}"


def _transport_error_code(exc: BaseException) -> str:
    """The local error code for a call that never got an answer."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(getattr(exc, "reason", None), TimeoutError):
        return "timeout"
    return "unreachable"


def _is_redirect(status: int) -> bool:
    """Whether a status is a 3xx this client refuses to follow.

    A redirect is not an answer to the request that was sent: following it
    would make a second request to whatever host the ``Location`` header names,
    with the read token still attached to it. This client never follows one, so
    a 3xx is a stable failure wherever it is seen -- the health probe and the
    related call alike -- and it never becomes the endpoint's answer.
    """
    return 300 <= status <= 399


def _decode_json(raw: bytes | None) -> Any:
    """The decoded body, or ``None`` when it is missing or not JSON."""
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


class _RedirectRefused(Exception):
    """A 3xx this client refuses, raised before the header is ever parsed.

    The standard library's ``HTTPRedirectHandler.http_error_302`` reads and
    parses the ``Location`` header *before* it asks ``redirect_request``
    whether to follow it, so a malformed value -- ``http://[broken``,
    ``http://[::1`` -- raises inside urllib and surfaces as ``unreachable``,
    which is not the refusal this client owes. The handler below therefore
    decides the 3xx the moment the status arrives and never looks at the
    header at all: the target is not parsed, not requested and not logged. The
    refused status rides along on this exception, and the health and related
    paths convert it into the stable reason ``redirect_refused``.
    """

    def __init__(self, code: int) -> None:
        super().__init__(code)
        #: The status that was refused. It is logged and nothing else: the
        #: header that named the target is not read.
        self.code = code


class _RefuseRedirects(urllib.request.BaseHandler):
    """A redirect handler that refuses every redirect without reading it.

    Refusing means deciding at the status, not at the target: this handler
    runs ahead of the standard ``HTTPRedirectHandler`` and raises straight
    away, so ``Location`` is never parsed and no second request is ever built.
    The read token therefore never leaves the peer the configuration named,
    not even when the header is malformed -- that is the same refusal, not an
    unreachable endpoint.
    """

    handler_order = 400  # 比默认的 HTTPRedirectHandler（500）先执行

    def _refuse(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> None:
        raise _RedirectRefused(code)  # 不读取、也不解析 Location

    http_error_301 = http_error_302 = http_error_303 = http_error_307 = http_error_308 = _refuse


def _drop_redirect_handler(opener: Any) -> None:
    """Take the standard redirect handler back out of a private opener.

    ``build_opener`` replaces a default handler only when the handler it is
    handed is a subclass of it, and a ``BaseHandler`` is not an
    ``HTTPRedirectHandler``: the default one is therefore still installed,
    behind the refusal (``handler_order`` 400 against 500) and never reached.
    It is removed anyway so that refusing is the only redirect behaviour this
    opener can express, whatever a future handler order might do.
    """
    chains: list[list[Any]] = [
        *opener.handle_open.values(),
        *opener.process_request.values(),
        *opener.process_response.values(),
    ]
    for kinds in opener.handle_error.values():
        chains.extend(kinds.values())
    for chain in chains:
        chain[:] = [
            handler
            for handler in chain
            if not isinstance(handler, urllib.request.HTTPRedirectHandler)
        ]
    opener.handlers[:] = [
        handler
        for handler in opener.handlers
        if not isinstance(handler, urllib.request.HTTPRedirectHandler)
    ]


#: The one private opener every request in this script goes through: the
#: standard handlers with the redirect handler replaced by the one that
#: refuses. It is built and never installed -- ``install_opener`` would replace
#: the process-wide opener and with it the behaviour of every other HTTP user
#: in this interpreter, which is not this script's to change.
_OPENER = urllib.request.build_opener(_RefuseRedirects)
_drop_redirect_handler(_OPENER)


def _http_call(request: Any, timeout: float) -> tuple[int, Any]:
    """Perform one HTTP call and return ``(status, body)``.

    ``body`` is the decoded JSON, or ``None`` when the response carried no
    body or a body that is not JSON. An HTTP error status is returned like any
    other status: ``urllib`` raises ``HTTPError`` for it, and that response
    carries the body the contract puts its error code in. A 3xx the private
    opener refuses is the one thing that is **not** returned as a status: the
    refusing handler raises ``_RedirectRefused`` out of the handler chain,
    before the ``Location`` header is parsed, and the callers convert it into
    the stable reason ``redirect_refused``.
    """
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            return status, _decode_json(response.read())
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 - the error body is best effort
            raw = b""
        return int(getattr(exc, "code", 0) or 0), _decode_json(raw)


class _HealthState(NamedTuple):
    """What the client learned about one endpoint.

    A named tuple rather than a dataclass on purpose: this module is loaded
    with ``importlib`` in the tests, where it is never registered in
    ``sys.modules``, and a dataclass cannot resolve its string annotations in
    that situation.
    """

    compatible: bool
    reachable: bool = True
    #: A stable reason the endpoint was refused before any request, else ``None``.
    #: ``bad_config`` means the configured URL was not a root address -- it
    #: carried a userinfo, a query, a fragment or a path of its own -- so
    #: nothing was sent. ``redirect_refused`` means the probe was answered with
    #: a 3xx, which this client refuses to follow, so the answer of the
    #: ``Location`` target is not this endpoint's answer. A probe that got an
    #: ordinary answer leaves this ``None`` even when it failed.
    reason: str | None = None


class _HealthCache:
    """Per-endpoint health cache with a five-minute window.

    A successful probe is cached, including a version mismatch -- the service
    is reachable, it just does not speak this contract. A failed probe is
    **not** cached: the endpoint is treated as temporarily unavailable and the
    next call probes again. A probe that was answered with a 3xx is a failure
    for the same reason -- the redirect is not followed, so there is no answer
    of this endpoint to cache.
    """

    def __init__(
        self,
        *,
        cache_seconds: float = _HEALTH_CACHE_SECONDS,
        timeout: float = _REQUEST_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cache_seconds = cache_seconds
        self._timeout = timeout
        self._clock = clock
        self._entries: dict[str, tuple[float, _HealthState]] = {}
        self._warned: set[str] = set()

    def clear(self) -> None:
        """Forget every cached answer and every emitted warning."""
        self._entries.clear()
        self._warned.clear()

    def check(self, endpoint: str) -> _HealthState:
        """The contract state of one endpoint, probing only when needed."""
        base = _reduced_base(endpoint)
        if base is None:
            # A URL that is not a root address -- it carries a userinfo, a
            # query, a fragment or a path of its own -- is refused here, before
            # anything is sent: the request line must never be able to carry
            # it, and neither may a log line. The reason is stable and the
            # reduced peer is all that is named. A refused config is not
            # cached -- it is a local fact, cheap to re-check.
            logger.debug(
                "MemFW endpoint configuration refused (%s); "
                "treating it as unavailable",
                _safe_endpoint(endpoint),
            )
            return _HealthState(
                compatible=False, reachable=False, reason=_REASON_BAD_CONFIG
            )
        moment = self._clock()
        cached = self._entries.get(base)
        if cached is not None and moment - cached[0] < self._cache_seconds:
            return cached[1]
        state = self._probe(base)
        if state.reachable:
            self._entries[base] = (moment, state)
        return state

    def _probe(self, endpoint: str) -> _HealthState:
        # ``endpoint`` is the already-validated ``scheme://host[:port]`` base.
        # The request line is that base plus the one fixed health path, so the
        # configured string -- with whatever path, query or fragment it carried
        # -- is never handed to urllib or to a transport that logs it.
        request = urllib.request.Request(
            endpoint + _HEALTH_PATH, headers={"Accept": "application/json"}
        )
        try:
            status, body = _http_call(request, self._timeout)
        except _RedirectRefused as exc:
            # The probe was answered with a 3xx and the opener refused it
            # before parsing ``Location``: the request the header names is
            # never made and the answer of that host is not this endpoint's
            # answer. It is a failure like any other, so it is not cached and
            # the next call probes again.
            logger.debug(
                "MemFW health probe for %s was answered with HTTP %s; "
                "no redirect is followed",
                _safe_endpoint(endpoint),
                exc.code,
            )
            return _HealthState(
                compatible=False, reachable=False, reason=_REASON_REDIRECT_REFUSED
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary
            logger.debug(
                "MemFW health probe for %s failed (%s)",
                _safe_endpoint(endpoint),
                _transport_error_code(exc),
            )
            return _HealthState(compatible=False, reachable=False)
        if _is_redirect(status):
            # A 3xx is refused: the opener does not follow it, so the request
            # the ``Location`` header names is never made and the answer of
            # that host is not this endpoint's answer. It is a failure like any
            # other, so it is not cached and the next call probes again.
            logger.debug(
                "MemFW health probe for %s was answered with HTTP %s; "
                "no redirect is followed",
                _safe_endpoint(endpoint),
                status,
            )
            return _HealthState(
                compatible=False, reachable=False, reason=_REASON_REDIRECT_REFUSED
            )
        if status != 200:
            logger.debug(
                "MemFW health probe for %s returned HTTP %s",
                _safe_endpoint(endpoint),
                status,
            )
            return _HealthState(compatible=False, reachable=False)
        contract = body.get("contract") if isinstance(body, dict) else None
        major = _contract_major(contract)
        if major != _CONTRACT_MAJOR:
            self._warn_once(endpoint, major)
            return _HealthState(compatible=False, reachable=True)
        return _HealthState(compatible=True, reachable=True)

    def _warn_once(self, endpoint: str, major: int | None) -> None:
        """Warn once per endpoint that it does not speak this contract.

        The warning names the **major version** the endpoint answered with and
        nothing else. The raw contract string is not logged: it comes from the
        service, it is not validated, and a log line must not be able to carry
        a value the service chose.
        """
        if endpoint in self._warned:
            return
        self._warned.add(endpoint)
        logger.warning(
            "MemFW endpoint %s speaks contract major %s, not %s/%d",
            _safe_endpoint(endpoint),
            major if major is not None else "<unknown>",
            _CONTRACT_NAME,
            _CONTRACT_MAJOR,
        )


#: The process-wide cache. ``/health`` is per endpoint, so the same answer is
#: reused by every caller bound to that endpoint.
_HEALTH = _HealthCache()


def _related_result(row: dict[str, Any]) -> dict[str, Any]:
    """One result, with reasons outside the contract's vocabulary dropped.

    Contract section 7 fixes the vocabulary: ``link``, ``related_field``,
    ``shared_tag``, ``shared_course``, ``shared_entity``, ``lexical``,
    ``semantic`` and ``section``. The rest of the row is passed through.
    """
    normalised = dict(row)
    if "reasons" in normalised:
        reasons = row.get("reasons")
        normalised["reasons"] = (
            [
                reason
                for reason in reasons
                if isinstance(reason, str) and reason in _REASONS_VOCABULARY
            ]
            if isinstance(reasons, list)
            else []
        )
    return normalised


@mcp.tool()
def memory_related(memory_id: str, limit: int = 10) -> dict[str, object]:
    """Read related current memories from the read-only MemFW service."""
    if not memory_id.strip():
        return {"ok": False, "error": "memory_id required", "related": []}
    url = _env("MEMFW_READONLY_URL")
    if not url:
        return {"ok": False, "error": "MEMFW_READONLY_URL missing", "related": []}
    token = _resolve_readonly_token()
    if not token:
        return {"ok": False, "error": "MEMFW_READONLY_TOKEN missing", "related": []}
    # One validated normalized base per endpoint, through the same boundary the
    # probe uses: the health check below and the related request both rebuild
    # their line from it plus their own fixed path, so the configured string --
    # with whatever path it carried -- reaches neither a request nor a log line.
    # A URL that is not a root address is refused before any request at all.
    base = _reduced_base(url)
    if base is None:
        return {"ok": False, "error_code": _REASON_BAD_CONFIG, "related": []}
    state = _HEALTH.check(base)
    if state.reason == _REASON_BAD_CONFIG:
        # The configured URL was not a root address and was refused before any
        # request. This is a local configuration fault, not an outage, so it is
        # named directly rather than through the status table or the probe's
        # reachability.
        return {"ok": False, "error_code": _REASON_BAD_CONFIG, "related": []}
    if state.reason == _REASON_REDIRECT_REFUSED:
        # The probe was answered with a 3xx and the redirect was not followed.
        # The answer belongs to whatever host the ``Location`` header named, so
        # there is no negotiated contract of this endpoint to rely on and
        # nowhere to send the read token. A refused redirect is a failure, so
        # it is not cached and the next call probes again.
        return {
            "ok": False,
            "error_code": _REASON_REDIRECT_REFUSED,
            "related": [],
        }
    if not state.reachable:
        return {"ok": False, "error_code": "unreachable", "related": []}
    if not state.compatible:
        return {"ok": False, "error_code": "invalid_response", "related": []}
    bounded = max(_LIMIT_MIN, min(int(limit), _LIMIT_MAX))
    payload = json.dumps({"memory_id": memory_id[:128], "limit": bounded}).encode()
    request = urllib.request.Request(
        base + _RELATED_PATH,
        data=payload,
        headers={"Content-Type": "application/json", "X-Memfw-Token": token},
    )
    try:
        status, body = _http_call(request, _REQUEST_TIMEOUT_SECONDS)
    except _RedirectRefused as exc:
        # The related call was answered with a 3xx. The private opener refused
        # it before parsing ``Location``, so the target was never requested and
        # the read token never left the configured peer.
        logger.debug(
            "MemFW related request for %s was answered with HTTP %s; "
            "no redirect is followed",
            _safe_endpoint(url),
            exc.code,
        )
        return {
            "ok": False,
            "error_code": _REASON_REDIRECT_REFUSED,
            "related": [],
        }
    except Exception as exc:  # noqa: BLE001 - tool boundary
        code = _transport_error_code(exc)
        logger.debug(
            "MemFW related request for %s failed (%s)", _safe_endpoint(url), code
        )
        return {"ok": False, "error_code": code, "related": []}
    if _is_redirect(status):
        # The related call was answered with a 3xx. The private opener refused
        # to follow it, so the ``Location`` target was never requested and the
        # read token never left the configured peer.
        logger.debug(
            "MemFW related request for %s was answered with HTTP %s; "
            "no redirect is followed",
            _safe_endpoint(url),
            status,
        )
        return {
            "ok": False,
            "error_code": _REASON_REDIRECT_REFUSED,
            "related": [],
        }
    if status < 200 or status >= 300:
        code = _parse_error_code(body, status)
        logger.debug(
            "MemFW related request for %s returned HTTP %s (%s)",
            _safe_endpoint(url),
            status,
            code,
        )
        return {"ok": False, "error_code": code, "related": []}
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        return {"ok": False, "error_code": "invalid_response", "related": []}
    # Contract section 1: every response body carries the contract marker, so a
    # body from another version of the service is not read as related memories.
    if _contract_major(body.get("contract")) != _CONTRACT_MAJOR:
        return {"ok": False, "error_code": "invalid_response", "related": []}
    mode = body.get("mode")
    degraded = body.get("degraded")
    return {
        "ok": True,
        "memory_id": memory_id,
        "mode": mode if isinstance(mode, str) else None,
        "degraded": degraded if isinstance(degraded, bool) else False,
        "related": [
            _related_result(row) for row in body["results"] if isinstance(row, dict)
        ],
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
