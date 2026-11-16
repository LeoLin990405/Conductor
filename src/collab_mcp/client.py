"""The one HTTP client the collab MCP server and ``conductor collab``/``apps`` share.

N80 L3 talks to the L2 routes under ``/api/v1/collab`` only through this
module, so the wire contract lives in one table (:data:`ENDPOINTS` for the
sixteen protocol tools, :data:`ADMIN_ENDPOINTS` for app registration). The
shapes come from ``src/collab/schema`` and the envelope from
``src/collab/envelope.py``:

* a tool's path parameters (``ref``, ``lease_id``, ``handoff_id``,
  ``review_id``) go in the URL and are removed from the rest of the input;
* a ``GET`` sends the rest of the input as query parameters (booleans as
  ``true``/``false``, arrays as repeated keys); a ``POST`` sends it as the JSON
  body. The server rebuilds the tool input as ``{**body_or_query, **path}``
  and validates it with ``validate_tool_input``;
* every answer is the collab envelope ``{ok, code, data, untrusted_fields,
  redacted[, message, retry_after]}`` with the HTTP status from
  ``ERROR_CODES``. A body that is not an envelope is never echoed.

Auth: the tool routes take only ``Authorization: Bearer <app token>``; a
tool call never sends ``X-Service-Token`` or ``X-User-Id``. ``X-Collab-Agent``
is optional and audit-only. The operator routes (:data:`ADMIN_ENDPOINTS`,
``conductor apps``) require the operator credential instead: the internal
service token in ``X-Service-Token`` plus ``X-User-Id``, which
:meth:`CollabClient.admin` sends only when the client was built with
``service_token`` (the MCP server never is):

* ``POST /admin/apps`` body ``{app_id, scopes: [..sorted], can_write?: true,
  max_leases?: 1..3}`` -> ``data {app_id, scopes, ..., token}``;
* ``GET /admin/apps`` -> ``data {apps: [{app_id, scopes, token_prefix,
  last_used_at, enabled, max_leases, can_write}]}`` (never a token);
* ``POST /admin/apps/{app_id}/scopes`` body ``{add: [..], remove: [..]}``;
* ``POST /admin/apps/{app_id}/rotate`` (no body) -> ``data {..., token}``;
* ``POST /admin/apps/{app_id}/revoke`` (no body) -> releases every lease;
* ``GET /admin/apps/{app_id}/audit`` query ``since?``, ``limit?``.

``data.token`` is the only place a plaintext token ever crosses the wire; the
CLI moves it into the keychain and drops it from everything it prints.

This module imports nothing from ``src``: ``conductor_cli`` imports it, and the
wheel that ships ``conductor`` carries ``collab_mcp`` but not ``src``.

Secrets: the bearer token is held in a private attribute, never in ``repr``,
an exception message or a log line, and :meth:`CollabClient.scrub` replaces it
in anything a caller is about to print, so even a server that echoed it back
could not make a front end show it.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

API_PREFIX = "/api/v1/collab"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: Optional, audit-only: the agent/model name the app reports (N80 3.1). It
#: never takes part in authorization.
AGENT_HEADER = "X-Collab-Agent"
#: The operator credential on the admin routes, as every service caller sends it.
SERVICE_TOKEN_HEADER = "X-Service-Token"
USER_ID_HEADER = "X-User-Id"

#: Keychain service every app token lives under (``conductor-collab/<app_id>``).
KEYCHAIN_SERVICE = "conductor-collab"

#: Lowercase short app names, fixed at registration (N80 3.1).
APP_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

_KEYCHAIN_REF = re.compile(r"^keychain:([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)$")
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9._~+/=-]{16,512}$")
_REDACTED = "[redacted]"


class CollabClientError(RuntimeError):
    """A local failure safe to show: never carries a token or a raw body."""


class CollabUnavailable(CollabClientError):
    """Conductor could not be reached."""


class KeychainError(CollabClientError):
    """The keychain reference could not be read or written."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One route: method, path template under :data:`API_PREFIX`, path params."""

    method: str
    path: str
    path_params: tuple[str, ...] = ()


#: Tool -> route, exactly the table in N80 section 4.1.
ENDPOINTS: dict[str, Endpoint] = {
    "whoami": Endpoint("GET", "/whoami"),
    "task_list": Endpoint("GET", "/tasks"),
    "task_get": Endpoint("GET", "/tasks/{ref}", ("ref",)),
    "task_create": Endpoint("POST", "/tasks"),
    "task_claim": Endpoint("POST", "/tasks/{ref}/claim", ("ref",)),
    "task_heartbeat": Endpoint("POST", "/leases/{lease_id}/heartbeat", ("lease_id",)),
    "task_release": Endpoint("POST", "/leases/{lease_id}/release", ("lease_id",)),
    "task_report": Endpoint("POST", "/leases/{lease_id}/report", ("lease_id",)),
    "handoff_request": Endpoint("POST", "/leases/{lease_id}/handoff", ("lease_id",)),
    "handoff_list": Endpoint("GET", "/handoffs"),
    "handoff_accept": Endpoint("POST", "/handoffs/{handoff_id}/accept", ("handoff_id",)),
    "agent_ask": Endpoint("POST", "/messages"),
    "agent_inbox": Endpoint("GET", "/messages/inbox"),
    "review_request": Endpoint("POST", "/reviews"),
    "review_list": Endpoint("GET", "/reviews"),
    "review_submit": Endpoint("POST", "/reviews/{review_id}/verdict", ("review_id",)),
}

#: Operator routes behind ``conductor apps``. They take the operator's normal
#: Conductor bearer token, never an app token; ``register`` and ``rotate``
#: answer the new plaintext app token exactly once in ``data.token``.
ADMIN_ENDPOINTS: dict[str, Endpoint] = {
    "apps_register": Endpoint("POST", "/admin/apps"),
    "apps_list": Endpoint("GET", "/admin/apps"),
    "apps_scopes": Endpoint("POST", "/admin/apps/{app_id}/scopes", ("app_id",)),
    "apps_rotate": Endpoint("POST", "/admin/apps/{app_id}/rotate", ("app_id",)),
    "apps_revoke": Endpoint("POST", "/admin/apps/{app_id}/revoke", ("app_id",)),
    "apps_audit": Endpoint("GET", "/admin/apps/{app_id}/audit", ("app_id",)),
}

#: Fallback codes when a refusal arrives without an envelope body.
_STATUS_CODES = {401: "unauthorized", 404: "not_found", 413: "too_large", 429: "rate_limited"}


def is_loopback_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "http" and (parsed.hostname or "") in LOOPBACK_HOSTS


def validate_base_url(url: str | None) -> str:
    """The collab routes are loopback-only (N80 4.4), so the client is too."""
    value = (url or DEFAULT_BASE_URL).strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or (parsed.hostname or "") not in LOOPBACK_HOSTS
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise CollabClientError(
            "the collab client only talks to a loopback http:// Conductor URL "
            "(127.0.0.1, localhost or ::1) without credentials, query or fragment"
        )
    return value.rstrip("/")


def validate_app_id(app_id: str) -> str:
    value = str(app_id or "").strip()
    if not APP_ID_PATTERN.match(value):
        raise CollabClientError(
            "app id must be a lowercase short name: a letter, then letters, digits or '-' (max 32)"
        )
    return value


def _query_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return [_query_value(item) for item in value]
    return value


def build_request(
    endpoint: Endpoint, arguments: Mapping[str, Any] | None
) -> tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]:
    """``(method, path, query, body)`` for one call; pure, so tests pin the wire."""
    rest = dict(arguments or {})
    values: dict[str, str] = {}
    for name in endpoint.path_params:
        raw = rest.pop(name, None)
        if not isinstance(raw, str) or not raw.strip():
            raise CollabClientError(f"{name} is required")
        # ':' stays literal so a task ref reads as t:<uuid> in the access log;
        # '/' and everything else that could change the route is escaped.
        values[name] = quote(raw, safe=":")
    path = API_PREFIX + endpoint.path.format(**values)
    if endpoint.method == "GET":
        query = {key: _query_value(value) for key, value in rest.items() if value is not None}
        return endpoint.method, path, query or None, None
    return endpoint.method, path, None, rest


def _error_envelope(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "code": code,
        "data": {},
        "untrusted_fields": [],
        "redacted": 0,
        "message": message,
    }


def _is_envelope(body: Any) -> bool:
    return (
        isinstance(body, dict)
        and isinstance(body.get("ok"), bool)
        and isinstance(body.get("code"), str)
        and isinstance(body.get("data", {}), dict)
    )


@dataclass(frozen=True, slots=True)
class CollabResponse:
    """One answer: the HTTP status and the collab envelope."""

    status: int
    envelope: dict[str, Any]

    @property
    def ok(self) -> bool:
        return bool(self.envelope.get("ok")) and self.envelope.get("code") == "ok"

    @property
    def code(self) -> str:
        return str(self.envelope.get("code") or "internal_error")

    @property
    def data(self) -> dict[str, Any]:
        data = self.envelope.get("data")
        return data if isinstance(data, dict) else {}


def parse_response(response: httpx.Response) -> CollabResponse:
    """Turn any HTTP answer into an envelope without echoing a foreign body."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if _is_envelope(body):
        envelope = dict(body)
        envelope.setdefault("data", {})
        envelope.setdefault("untrusted_fields", [])
        envelope.setdefault("redacted", 0)
        if response.is_success and envelope.get("ok") is not True:
            envelope["ok"] = False
        if not response.is_success:
            envelope["ok"] = False
            if envelope.get("code") == "ok":
                envelope["code"] = _STATUS_CODES.get(response.status_code, "internal_error")
        return CollabResponse(response.status_code, envelope)
    code = _STATUS_CODES.get(response.status_code, "internal_error")
    if response.is_success:
        message = "Conductor answered without a collab envelope"
    else:
        message = f"HTTP {response.status_code} from Conductor"
    return CollabResponse(response.status_code, _error_envelope(code, message))


class CollabClient:
    """Synchronous client for the collab routes; one bearer, loopback only."""

    def __init__(
        self,
        token: str | None,
        *,
        base_url: str | None = None,
        agent: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        require_token: bool = True,
        service_token: str | None = None,
        user_id: str | None = None,
    ) -> None:
        value = token.strip() if isinstance(token, str) else ""
        # Sent on the operator routes only (see :meth:`admin`), never on a
        # tool route, and kept out of ``repr`` like the bearer.
        self._service_token = service_token.strip() if isinstance(service_token, str) else ""
        self._user_id = str(user_id or "").strip()[:200]
        if require_token and not value:
            raise CollabClientError("a bearer token is required")
        # ``require_token=False`` is only for the operator routes on a local
        # (OSS) Conductor that runs without an operator token; app routes
        # always take the app's bearer.
        self._token = value
        self.base_url = validate_base_url(base_url)
        headers = {"Accept": "application/json"}
        if value:
            headers["Authorization"] = f"Bearer {value}"
        if agent:
            headers[AGENT_HEADER] = str(agent)[:64]
        self._http = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    def __repr__(self) -> str:
        return f"CollabClient(base_url={self.base_url!r}, token=<hidden>)"

    def _operator_headers(self) -> dict[str, str]:
        if not self._service_token:
            return {}
        headers = {SERVICE_TOKEN_HEADER: self._service_token}
        if self._user_id:
            headers[USER_ID_HEADER] = self._user_id
        return headers

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> CollabClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def call(self, tool: str, arguments: Mapping[str, Any] | None = None) -> CollabResponse:
        try:
            endpoint = ENDPOINTS[tool]
        except KeyError as exc:
            raise CollabClientError(f"unknown collaboration tool: {tool}") from exc
        return self.send(endpoint, arguments)

    def admin(self, action: str, arguments: Mapping[str, Any] | None = None) -> CollabResponse:
        try:
            endpoint = ADMIN_ENDPOINTS[action]
        except KeyError as exc:
            raise CollabClientError(f"unknown apps action: {action}") from exc
        return self.send(endpoint, arguments, headers=self._operator_headers())

    def send(
        self,
        endpoint: Endpoint,
        arguments: Mapping[str, Any] | None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> CollabResponse:
        method, path, query, body = build_request(endpoint, arguments)
        try:
            response = self._http.request(
                method, path, params=query, json=body, headers=dict(headers or {}) or None
            )
        except httpx.HTTPError as exc:
            # The exception text names the URL only; it is still kept out of
            # the message so nothing request-derived reaches a terminal.
            raise CollabUnavailable(
                f"could not reach Conductor at {self.base_url} ({type(exc).__name__})"
            ) from None
        return parse_response(response)

    def scrub(self, value: Any) -> Any:
        """``value`` with every occurrence of the bearer or service token replaced."""
        return scrub_secret(scrub_secret(value, self._token or None), self._service_token or None)


def scrub_secret(value: Any, secret: str | None) -> Any:
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, _REDACTED)
    if isinstance(value, Mapping):
        return {scrub_secret(key, secret): scrub_secret(item, secret) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_secret(item, secret) for item in value]
    return value


# ---------------------------------------------------------------------------
# Keychain references
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KeychainRef:
    """``keychain:<service>/<account>``, the only form a token reference takes."""

    service: str
    account: str

    @classmethod
    def parse(cls, text: str) -> KeychainRef:
        match = _KEYCHAIN_REF.match(str(text or "").strip())
        if not match:
            raise KeychainError("token reference must look like keychain:<service>/<account>")
        return cls(match.group(1), match.group(2))

    @classmethod
    def for_app(cls, app_id: str) -> KeychainRef:
        return cls(KEYCHAIN_SERVICE, validate_app_id(app_id))

    def __str__(self) -> str:
        return f"keychain:{self.service}/{self.account}"


#: ``runner(argv, stdin) -> (returncode, stdout)``. Tests pass a fake; the
#: default runs ``/usr/bin/security``.
Runner = Callable[[list[str], str | None], tuple[int, str]]


def _security_runner(argv: list[str], stdin: str | None) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return completed.returncode, completed.stdout


class Keychain:
    """Read and write app tokens by reference; never logs or returns stderr."""

    def __init__(self, runner: Runner | None = None, *, executable: str = "security") -> None:
        self._run = runner or _security_runner
        self._executable = executable

    def read(self, ref: KeychainRef | str) -> str:
        target = ref if isinstance(ref, KeychainRef) else KeychainRef.parse(ref)
        code, stdout = self._run(
            [
                self._executable,
                "find-generic-password",
                "-s",
                target.service,
                "-a",
                target.account,
                "-w",
            ],
            None,
        )
        secret = (stdout or "").strip()
        if code != 0 or not secret:
            raise KeychainError(f"no token found at {target}")
        return secret

    def write(self, ref: KeychainRef | str, secret: str) -> None:
        """Store ``secret`` through ``security -i`` so it never sits in argv."""
        target = ref if isinstance(ref, KeychainRef) else KeychainRef.parse(ref)
        value = str(secret or "").strip()
        if not _TOKEN_SHAPE.match(value):
            raise KeychainError("refusing to store a token with an unexpected shape")
        command = (
            f"add-generic-password -U -s {target.service} -a {target.account} "
            f"-l conductor-collab-{target.account} -w {value}\n"
        )
        code, _ = self._run([self._executable, "-i"], command)
        if code != 0:
            raise KeychainError(f"could not write the token to {target}")


#: Collab app ids whose MCP hub agent id is spelled differently.
_HUB_TARGETS = {"claude-code": "claude"}


def mcp_hub_snippet(
    app_id: str,
    *,
    target: str | None = None,
    command: str = "conductor-collab-mcp",
) -> dict[str, Any]:
    """The MCP hub entry for one app, as text to copy; nothing is applied.

    The token is referenced, never inlined: the server reads
    ``keychain:conductor-collab/<app_id>`` itself at start-up, so the entry
    carries no env value at all. The ``--target`` is always named so a pasted
    line can never register the server for every agent at once.
    """
    app = validate_app_id(app_id)
    hub_target = (target or _HUB_TARGETS.get(app, app)).strip()
    if not re.match(r"^[a-z][a-z0-9-]{0,31}$", hub_target):
        raise CollabClientError("MCP hub target must be a lowercase agent id")
    name = f"conductor-collab-{app}"
    args = ["--app", app]
    toml = "\n".join(
        [
            f"[servers.{name}]",
            'transport = "stdio"',
            f"command = {json.dumps(command)}",
            f"args = {json.dumps(args)}",
            "",
        ]
    )
    cli = (
        f"conductor mcp add --name {name} --transport stdio --command {command} "
        + " ".join(f"--arg={arg}" for arg in args)
        + f" --target {hub_target}"
    )
    return {
        "name": name,
        "target": hub_target,
        "token_ref": str(KeychainRef.for_app(app)),
        "servers_toml": toml,
        "conductor_mcp_add": cli,
        "applied": False,
        "note": "printed only; review it, then run the conductor mcp add line yourself (dry-run unless --apply)",
    }


__all__ = [
    "ADMIN_ENDPOINTS",
    "AGENT_HEADER",
    "API_PREFIX",
    "APP_ID_PATTERN",
    "DEFAULT_BASE_URL",
    "ENDPOINTS",
    "KEYCHAIN_SERVICE",
    "LOOPBACK_HOSTS",
    "SERVICE_TOKEN_HEADER",
    "USER_ID_HEADER",
    "CollabClient",
    "CollabClientError",
    "CollabResponse",
    "CollabUnavailable",
    "Endpoint",
    "Keychain",
    "KeychainError",
    "KeychainRef",
    "build_request",
    "is_loopback_url",
    "mcp_hub_snippet",
    "parse_response",
    "scrub_secret",
    "validate_app_id",
    "validate_base_url",
]
