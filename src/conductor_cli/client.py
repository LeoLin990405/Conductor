"""Small authenticated REST client shared by the Conductor command line."""

from __future__ import annotations

import json as json_module
import os
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
CONFIG_DIR = Path.home() / ".config" / "conductor"
TOKEN_PATH = CONFIG_DIR / "token"


class CLIError(RuntimeError):
    """An actionable CLI or API error safe to show in a terminal."""


class APIError(CLIError):
    """The server answered with a 4xx status; exit code 1.

    ``code``/``remediation``/``detail`` are carried through when the server
    answered with its structured error shape (``{"detail": {...}}``), so a
    review/merge refusal reaches the operator with the server's remediation
    instead of a bare status line.
    """

    def __init__(
        self,
        message: str,
        status_code: int,
        *,
        code: str | None = None,
        remediation: str | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.remediation = remediation
        self.detail = detail


class ServerError(CLIError):
    """The server is unreachable or answered 5xx; exit code 3."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        code: str | None = None,
        remediation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.remediation = remediation


_TERMINAL_OSC = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
_TERMINAL_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_TERMINAL_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_JSON_C1 = re.compile(r"[\x7f-\x9f]")


def sanitize_terminal(text: str) -> str:
    """Remove escape sequences before printing untrusted server/log text."""
    value = _TERMINAL_OSC.sub("", text)
    value = _TERMINAL_CSI.sub("", value)
    return _TERMINAL_CTRL.sub("", value)


def json_dumps_safe(value: Any, *, indent: int | None = 2) -> str:
    """Serialise JSON without emitting raw C0/C1 controls.

    ``ensure_ascii=False`` keeps CJK readable, but also emits bytes such as
    U+009B directly. Escaping just those characters keeps the output valid JSON
    and safe for a terminal that interprets C1 controls.
    """
    text = json_module.dumps(value, ensure_ascii=False, indent=indent, default=str)
    return _JSON_C1.sub(lambda match: f"\\u{ord(match.group(0)):04x}", text)


def _classified_5xx(response: httpx.Response) -> tuple[str, str, str] | None:
    """The server's complete classified error shape for a 5xx, or None.

    Only a body carrying all three fields (code, message, remediation) is
    trusted: a partial envelope still counts as untrusted internals and is
    never echoed to a terminal.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    raw = body.get("detail") if isinstance(body, dict) else None
    if not isinstance(raw, dict):
        return None
    code = str(raw.get("code") or "").strip()
    message = str(raw.get("message") or "").strip()
    remediation = str(raw.get("remediation") or "").strip()
    if not (code and message and remediation):
        return None
    return f"{message}\nremediation: {remediation}", code, remediation


def _validation_reasons(entries: list[Any]) -> str:
    """The server's validation refusals as one line: where, and what is wrong.

    FastAPI answers a refused body with ``detail`` as a list of pydantic
    errors. The field and the message are shown; the echoed input is not,
    because it is the very value the operator sent.
    """
    parts: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        loc = entry.get("loc")
        where = (
            ".".join(str(item) for item in loc)
            if isinstance(loc, (list, tuple))
            else ""
        )
        message = str(entry.get("msg") or entry.get("type") or "").strip()
        if not message:
            continue
        parts.append(f"{where}: {message}" if where else message)
    return "; ".join(parts)


def _server_error_detail(
    response: httpx.Response,
) -> tuple[str, str | None, str | None, Any]:
    """Read the server's structured error envelope, falling back to the status.

    Review and merge refusals carry ``{"detail": {"code", "message",
    "remediation", "detail"}}``; the CLI shows the message and remediation so
    an operator does not have to open the backend log to learn the next step.

    A refused body carries ``{"detail": [...]}`` instead: one entry per
    failed field, which is what a 422 answers with. Those entries are shown
    too, so a refused request says which field the server refused rather than
    only that it refused one.
    """
    status = response.status_code
    fallback = f"HTTP {status} from Conductor"
    code: str | None = None
    remediation: str | None = None
    detail: Any = None
    message = fallback
    try:
        body = response.json()
    except ValueError:
        return fallback, None, None, None
    raw = body.get("detail") if isinstance(body, dict) else None
    if isinstance(raw, dict):
        candidate = str(raw.get("message") or "").strip()
        code = str(raw.get("code") or "").strip() or None
        remediation = str(raw.get("remediation") or "").strip() or None
        detail = raw.get("detail")
        if candidate:
            message = candidate
    elif isinstance(raw, str) and raw.strip():
        message = raw.strip()
    elif isinstance(raw, list) and raw:
        detail = raw
        reasons = _validation_reasons(raw)
        if reasons:
            message = f"{fallback}: {reasons}"
    if remediation and remediation not in message:
        message = f"{message}\nremediation: {remediation}"
    return message, code, remediation, detail


def _check_private_file(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CLIError(f"Cannot safely open credential file: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    _validate_private_metadata(metadata, path)


def _validate_private_metadata(metadata: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise CLIError(f"Refusing non-regular credential file: {path}")
    if metadata.st_uid != os.getuid():
        raise CLIError(f"Credential file is owned by another user: {path}")
    if metadata.st_mode & 0o077:
        raise CLIError(f"Credential file permissions are too loose: {path} (run chmod 600 {path})")


def ensure_private_directory(directory: Path, *, create: bool = True) -> None:
    """Create private per-user directories while refusing symlink components."""
    home = Path.home().resolve()
    target = directory.expanduser()
    try:
        relative = target.relative_to(home)
    except ValueError as exc:
        raise CLIError(f"Private data directory must be inside the home directory: {target}") from exc
    current = home
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if not create:
                return
            current.mkdir(mode=0o700)
            metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CLIError(f"Refusing non-directory or symlink in private path: {current}")
        if metadata.st_uid != os.getuid():
            raise CLIError(f"Private directory is owned by another user: {current}")
        if metadata.st_mode & 0o022:
            raise CLIError(f"Private directory is writable by other users: {current}")


def read_token(path: Path = TOKEN_PATH) -> str | None:
    """Read a token only when its file is private and is not a symlink."""
    ensure_private_directory(path.parent, create=False)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CLIError(f"Cannot safely open credential file: {path}") from exc
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        _validate_private_metadata(os.fstat(stream.fileno()), path)
        token = stream.read(65537)
    if len(token) > 65536:
        raise CLIError("Credential file is unexpectedly large")
    token = token.strip()
    if not token:
        raise CLIError("Credential file is empty")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in token):
        raise CLIError("Credential contains invalid control characters")
    return token


def write_token(token: str, path: Path = TOKEN_PATH) -> None:
    """Create a token file without replacing an existing credential."""
    value = token.strip()
    if not value or any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        raise CLIError("Token must be a non-empty single line")
    _check_private_file(path)
    ensure_private_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise CLIError(f"Credential file already exists: {path}") from exc
    except OSError as exc:
        raise CLIError(f"Could not create credential file: {exc}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value + "\n")
    except Exception:
        path.unlink(missing_ok=True)
        raise


class APIClient:
    """Synchronous JSON client that follows the server's Bearer auth contract."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        configured_url = (base_url or os.getenv("CONDUCTOR_URL") or DEFAULT_BASE_URL).strip()
        parsed = urlsplit(configured_url)
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise CLIError("CONDUCTOR_URL must be an HTTP(S) origin or base path without credentials, query, or fragment")
        if parsed.scheme != "https" and parsed.hostname not in local_hosts:
            raise CLIError("Conductor URLs outside loopback must use HTTPS")
        self.base_url = configured_url.rstrip("/")
        self.token = token if token is not None else read_token()
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._http = httpx.Client(timeout=timeout, headers=headers, transport=transport, follow_redirects=False)

    def close(self) -> None:
        self._http.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | list[Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        if not path.startswith("/") or path.startswith("//"):
            raise CLIError("API paths must be absolute paths on the configured Conductor server")
        target = f"{self.base_url}/{path.lstrip('/')}"
        # Only pass a timeout when one was requested: ``timeout=None`` means
        # "disable the timeout" to httpx and would override the client default
        # for every ordinary command.
        request_kwargs: dict[str, Any] = {"params": params, "json": json}
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        try:
            response = self._http.request(method, target, **request_kwargs)
        except httpx.HTTPError as exc:
            raise ServerError(f"Could not reach Conductor at {self.base_url}: {exc}") from exc
        if not response.is_success:
            if response.status_code >= 500:
                # A 5xx body is untrusted server-internal text, so it is only
                # surfaced when it carries the server's complete classified
                # error shape (code + message + remediation); anything else
                # keeps the stable status line instead of echoing internals.
                classified = _classified_5xx(response)
                if classified is None:
                    raise ServerError(
                        f"HTTP {response.status_code} from Conductor",
                        response.status_code,
                    )
                message, code, remediation = classified
                raise ServerError(
                    message,
                    response.status_code,
                    code=code,
                    remediation=remediation,
                )
            message, code, remediation, detail = _server_error_detail(response)
            raise APIError(
                message,
                response.status_code,
                code=code,
                remediation=remediation,
                detail=detail,
            )
        if response.status_code == 204 or not response.content:
            return None
        try:
            body = response.json()
        except json_module.JSONDecodeError as exc:
            raise CLIError("Conductor returned an invalid JSON response") from exc
        return body

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        return self.request("GET", path, params=params, timeout=timeout)

    def post(
        self,
        path: str,
        json: dict[str, Any] | list[Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        return self.request("POST", path, json=json, timeout=timeout)

    def patch(self, path: str, json: dict[str, Any] | list[Any] | None = None) -> Any:
        return self.request("PATCH", path, json=json)

    def delete(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self.request("DELETE", path, params=params)
