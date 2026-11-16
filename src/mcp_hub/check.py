"""Health check hook and status. Neither returns env values.

``check_server`` without a runner does not spawn anything. ``probe_local_server``
is the CLI runner: it speaks initialize and tools/list to a local stdio
process, or to a configured HTTP/SSE endpoint, with a short timeout. Stdio
children receive PATH, locale, and the server's declared ``${VAR}`` references
only. A redirect onto another host is refused so credentials are not sent
there. Public hosts are not banned for being public.
"""

from __future__ import annotations

import http.client
import json
import os
import queue
import signal
import socket
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .agents import agent_path
from .errors import McpHubError
from .journal import ownership_from_journal
from .model import AGENT_IDS, Server, classify_env_value
from .render import _home
from .store import load_registry


def check_server(
    home: Path | str,
    name: str,
    *,
    runner: Callable[[Server], Any] | None = None,
) -> dict[str, Any]:
    """Report ``tool_count`` and ``elapsed_ms`` without echoing env.

    With no ``runner``, the status is ``not-run``. The runner may see the
    server object; only an allow-listed summary is returned.
    """
    root = _home(home)
    registry = load_registry(root)
    server = registry.servers.get(name)
    if server is None:
        raise McpHubError(f"unknown server {name!r}")
    if not server.enabled:
        return {
            "name": name,
            "status": "not-run",
            "tool_count": None,
            "elapsed_ms": None,
            "reason": "disabled",
        }
    if runner is None:
        return {
            "name": name,
            "status": "not-run",
            "tool_count": None,
            "elapsed_ms": None,
        }
    started = time.perf_counter()
    try:
        result = runner(server)
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        return {
            "name": name,
            "status": "error",
            "tool_count": None,
            "elapsed_ms": elapsed,
            "reason": _redact(str(exc), server),
        }
    elapsed = int((time.perf_counter() - started) * 1000)
    tool_count: int | None = None
    status = "ok"
    reason: str | None = None
    if isinstance(result, dict):
        count = result.get("tool_count")
        if isinstance(count, int) and not isinstance(count, bool):
            tool_count = count
        reported = result.get("elapsed_ms")
        if isinstance(reported, int) and not isinstance(reported, bool):
            elapsed = reported
        reported_status = result.get("status")
        if reported_status in {"ok", "error", "not-run"}:
            status = str(reported_status)
        if isinstance(result.get("reason"), str):
            reason = _redact(result["reason"], server)
    elif isinstance(result, int) and not isinstance(result, bool):
        tool_count = result
    out: dict[str, Any] = {
        "name": name,
        "status": status,
        "tool_count": tool_count,
        "elapsed_ms": elapsed,
    }
    if reason:
        out["reason"] = reason
    return out


def list_status(home: Path | str) -> dict[str, Any]:
    """JSON-ready registry, ownership and which agent files exist."""
    root = _home(home)
    registry = load_registry(root)
    managed, _published = ownership_from_journal(root)
    agents: dict[str, Any] = {}
    for agent in AGENT_IDS:
        path = agent_path(root, agent)
        agents[agent] = {
            "path": str(path),
            "present": path.is_file() and not path.is_symlink(),
        }
    return {
        "servers": [
            registry.servers[name].to_dict() for name in sorted(registry.servers)
        ],
        "managed": {agent: sorted(names) for agent, names in managed.items() if names},
        "agents": agents,
    }


def _redact(text: str, server: Server) -> str:
    redacted = text
    for value in server.env.values():
        if value:
            redacted = redacted.replace(value, "[redacted]")
    return redacted


_PROBE_TIMEOUT_SEC = 2.0
_PROBE_MAX_MESSAGES = 20
_PROBE_MAX_LINE = 1024 * 1024
_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "COMSPEC")


class _ProbeFailed(Exception):
    """A local probe did not finish. The message is a fixed phrase, not output."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def probe_local_server(
    server: Server, *, timeout: float = _PROBE_TIMEOUT_SEC
) -> dict[str, Any]:
    """initialize + tools/list. Stdio stays local; HTTP/SSE uses the configured URL.

    The result never includes env values, response bodies, or the URL.
    """
    remote = server.transport in {"http", "sse"} or (server.url and not server.command)
    try:
        if remote:
            count = _http_tool_count(server, timeout=timeout)
        elif server.transport != "stdio" or not server.command:
            return {
                "status": "error",
                "tool_count": None,
                "reason": "server has no local command",
            }
        else:
            count = _stdio_tool_count(server, timeout=timeout)
    except _ProbeFailed as exc:
        return {"status": "error", "tool_count": None, "reason": exc.reason}
    except Exception:
        return {"status": "error", "tool_count": None, "reason": "check failed"}
    return {"status": "ok", "tool_count": count}


def _probe_env(server: Server | None = None) -> dict[str, str]:
    """PATH, locale, and declared ``${VAR}`` values. Nothing else from the environment."""
    env: dict[str, str] = {}
    for key in _ENV_PASSTHROUGH:
        value = os.environ.get(key)
        if value:
            env[key] = value
    if server is None:
        return env
    for key, value in server.env.items():
        if classify_env_value(value) != "var":
            continue
        ref = value[2:-1]
        if ref not in os.environ:
            continue
        env[key] = os.environ[ref]
    return env


_MAX_HTTP_BYTES = 1024 * 1024
_MAX_REDIRECTS = 3


def _http_tool_count(server: Server, *, timeout: float) -> int:
    if not server.url:
        raise _ProbeFailed("check failed")
    start = _parse_endpoint(server.url)
    deadline = time.monotonic() + timeout
    _ignored, session = _http_exchange(
        start, _initialize_request(), deadline, expect_id=1
    )
    _http_exchange(
        start,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        deadline,
        expect_id=None,
        session_id=session,
    )
    result, _session = _http_exchange(
        start,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        deadline,
        expect_id=2,
        session_id=session,
    )
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise _ProbeFailed("tools/list failed")
    return len(tools)


def _parse_endpoint(url: str) -> urllib.parse.SplitResult:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise _ProbeFailed("check failed") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise _ProbeFailed("check failed")
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise _ProbeFailed("refusing credentials in a url")
    return parsed


def _same_host(left: urllib.parse.SplitResult, right: urllib.parse.SplitResult) -> bool:
    return (left.hostname or "").lower() == (right.hostname or "").lower()


def _request_target(parsed: urllib.parse.SplitResult) -> str:
    path = parsed.path or "/"
    if parsed.query:
        return f"{path}?{parsed.query}"
    return path


def _open_http(
    parsed: urllib.parse.SplitResult, timeout: float
) -> http.client.HTTPConnection:
    host = parsed.hostname
    if not host:
        raise _ProbeFailed("check failed")
    if parsed.scheme == "https":
        port = parsed.port or 443
        return http.client.HTTPSConnection(host, port, timeout=timeout)
    port = parsed.port or 80
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _http_exchange(
    start: urllib.parse.SplitResult,
    payload: dict[str, Any],
    deadline: float,
    *,
    expect_id: int | None,
    session_id: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    current = start
    redirects = 0
    session = _session_token(session_id)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _ProbeFailed("timed out")
        status, raw, location, content_type, hop_session = _http_round(
            current, body, remaining, session_id=session
        )
        if hop_session:
            session = hop_session
        if status in {301, 302, 303, 307, 308}:
            redirects += 1
            if redirects > _MAX_REDIRECTS or not location:
                raise _ProbeFailed("too many redirects" if location else "check failed")
            try:
                joined = urllib.parse.urljoin(current.geturl(), location)
                nxt = urllib.parse.urlsplit(joined)
            except ValueError as exc:
                raise _ProbeFailed("check failed") from exc
            if (
                nxt.username
                or nxt.password
                or "@" in (nxt.netloc or "")
                or nxt.scheme not in {"http", "https"}
                or not nxt.hostname
                or not _same_host(current, nxt)
            ):
                # A cross-host hop would carry the same request, including any
                # credential header a later caller adds. Refuse it outright.
                raise _ProbeFailed("refusing cross-host redirect")
            current = nxt
            continue
        if expect_id is None:
            if 200 <= status < 300:
                return {}, session
            raise _ProbeFailed("check failed")
        reason = "initialize failed" if expect_id == 1 else "tools/list failed"
        if status != 200:
            raise _ProbeFailed(reason)
        return _rpc_result(raw, content_type, expect_id, reason), session


def _session_token(value: str | None) -> str | None:
    """A single-line session id, or None. Never a header that could split the request."""
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token or len(token) > 256:
        return None
    if any(char in token for char in "\r\n\x00"):
        return None
    return token


def _http_round(
    parsed: urllib.parse.SplitResult,
    body: bytes,
    timeout: float,
    *,
    session_id: str | None = None,
) -> tuple[int, bytes, str | None, str, str | None]:
    conn = _open_http(parsed, timeout)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Content-Length": str(len(body)),
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    try:
        conn.request(
            "POST",
            _request_target(parsed),
            body=body,
            headers=headers,
        )
        resp = conn.getresponse()
        raw = resp.read(_MAX_HTTP_BYTES + 1)
        status = resp.status
        location = resp.getheader("Location")
        content_type = resp.getheader("Content-Type") or ""
        session = _session_token(resp.getheader("Mcp-Session-Id"))
    except (TimeoutError, socket.timeout) as exc:
        raise _ProbeFailed("timed out") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise _ProbeFailed("check failed") from exc
    finally:
        conn.close()
    if len(raw) > _MAX_HTTP_BYTES:
        raise _ProbeFailed("response too large")
    return status, raw, location, content_type, session


def _rpc_result(
    raw: bytes, content_type: str, expect_id: int, reason: str
) -> dict[str, Any]:
    try:
        messages = _decode_messages(raw, content_type)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _ProbeFailed(reason) from exc
    for message in messages[:_PROBE_MAX_MESSAGES]:
        if not isinstance(message, dict) or message.get("id") != expect_id:
            continue
        if "error" in message or not isinstance(message.get("result"), dict):
            raise _ProbeFailed(reason)
        return message["result"]
    raise _ProbeFailed(reason)


def _decode_messages(raw: bytes, content_type: str) -> list[Any]:
    text = raw.decode("utf-8")
    if "text/event-stream" in content_type.lower():
        messages: list[Any] = []
        for block in text.split("\n\n"):
            data_lines = [
                line[5:].lstrip()
                for line in block.splitlines()
                if line.startswith("data:")
            ]
            if not data_lines:
                continue
            messages.append(json.loads("\n".join(data_lines)))
            if len(messages) >= _PROBE_MAX_MESSAGES:
                break
        return messages
    if not text.strip():
        return []
    parsed = json.loads(text)
    return parsed if isinstance(parsed, list) else [parsed]


def _stdio_tool_count(server: Server, *, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    try:
        proc = subprocess.Popen(
            [server.command, *server.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            env=_probe_env(server),
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise _ProbeFailed("server failed to start") from exc
    except OSError as exc:
        raise _ProbeFailed("server failed to start") from exc
    assert proc.stdin is not None and proc.stdout is not None
    reader = _LineReader(proc.stdout)
    try:
        _send(proc, _initialize_request())
        _read_result(reader, deadline, expect_id=1, reason="initialize failed")
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        result = _read_result(reader, deadline, expect_id=2, reason="tools/list failed")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise _ProbeFailed("tools/list failed")
        return len(tools)
    except _ProbeFailed:
        raise
    except BrokenPipeError as exc:
        raise _ProbeFailed("server exited") from exc
    except OSError as exc:
        raise _ProbeFailed("check failed") from exc
    finally:
        _stop_process(proc)
        reader.close()


def _initialize_request() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "conductor-mcp-hub", "version": "0.1.0"},
        },
    }


def _send(proc: subprocess.Popen[bytes], payload: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise _ProbeFailed("server failed to start")
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    proc.stdin.write(raw)
    proc.stdin.flush()


def _read_result(
    reader: "_LineReader", deadline: float, *, expect_id: int, reason: str
) -> dict[str, Any]:
    for _ in range(_PROBE_MAX_MESSAGES):
        message = reader.read_json(deadline, reason=reason)
        if not isinstance(message, dict):
            raise _ProbeFailed(reason)
        if message.get("id") != expect_id:
            continue
        if "error" in message or not isinstance(message.get("result"), dict):
            raise _ProbeFailed(reason)
        return message["result"]
    raise _ProbeFailed(reason)


class _LineReader:
    """Stdout lines with a deadline. Bytes never become part of an error string."""

    def __init__(self, stdout: Any) -> None:
        self._queue: queue.Queue[bytes | BaseException | None] = queue.Queue()
        self._thread = threading.Thread(target=self._pump, args=(stdout,), daemon=True)
        self._thread.start()

    def _pump(self, stdout: Any) -> None:
        try:
            while True:
                line = stdout.readline()
                if not line:
                    self._queue.put(None)
                    return
                self._queue.put(line)
        except Exception as exc:  # noqa: BLE001 - the probe turns this into a fixed reason
            self._queue.put(exc)

    def read_json(self, deadline: float, *, reason: str) -> Any:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _ProbeFailed("timed out")
        try:
            item = self._queue.get(timeout=remaining)
        except queue.Empty as exc:
            raise _ProbeFailed("timed out") from exc
        if item is None:
            raise _ProbeFailed("server exited")
        if isinstance(item, Exception):
            raise _ProbeFailed("check failed") from item
        if len(item) > _PROBE_MAX_LINE:
            raise _ProbeFailed("response too large")
        try:
            return json.loads(item.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise _ProbeFailed(reason) from exc

    def close(self) -> None:
        self._thread.join(timeout=0.5)


def _stop_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except OSError:
            pass
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return
