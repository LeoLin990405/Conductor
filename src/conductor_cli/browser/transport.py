"""Private per-agent profile paths and local owner-process transport."""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..client import CLIError, ensure_private_directory

MAX_MESSAGE_BYTES = 300_000
AGENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def validate_agent(value: str) -> str:
    if not isinstance(value, str) or not AGENT_PATTERN.fullmatch(value):
        raise CLIError("Agent name must use 1 to 64 letters, numbers, underscores, or hyphens")
    return value


def profile_for_agent(agent: str) -> Path:
    name = validate_agent(agent)
    path = Path.home() / ".conductor" / "agent-browser" / name
    ensure_private_directory(path)
    for component in (path.parent.parent, path.parent, path):
        metadata = component.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise CLIError(f"Browser profile directory must be private: {component}")
    return path


def _socket_path(profile: Path) -> Path:
    path = profile / "control.sock"
    if len(os.fsencode(path)) >= 100:
        raise CLIError("Browser control socket path is too long for this system")
    return path


def _connect(path: Path, timeout: float) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
    except OSError:
        client.close()
        raise
    return client


def _request(profile: Path, request: dict[str, Any], timeout: float = 70.0) -> dict[str, Any]:
    encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise CLIError("Browser request is too large")
    try:
        with _connect(_socket_path(profile), timeout) as client:
            client.sendall(encoded)
            chunks = bytearray()
            while len(chunks) <= MAX_MESSAGE_BYTES:
                chunk = client.recv(8192)
                if not chunk:
                    break
                chunks.extend(chunk)
                if b"\n" in chunk:
                    break
    except OSError as exc:
        raise CLIError("Managed browser is unavailable; run `conductor browser open`") from exc
    if not chunks or len(chunks) > MAX_MESSAGE_BYTES:
        raise CLIError("Managed browser returned an invalid response")
    try:
        response = json.loads(bytes(chunks).split(b"\n", 1)[0])
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CLIError("Managed browser returned an invalid response") from exc
    if not isinstance(response, dict):
        raise CLIError("Managed browser returned an invalid response")
    # New public browser commands use an explicit result envelope. Preserve it
    # for the CLI so it can decide whether an allowed browser-failure fallback
    # applies. Legacy owner commands still return raw data and string errors.
    if "ok" in response and isinstance(response.get("ok"), bool):
        return response
    if response.get("error") is not None:
        message = response["error"]
        raise CLIError(message if isinstance(message, str) and len(message) <= 200 else "Browser command failed")
    return response


def _minimal_env(profile: Path) -> dict[str, str]:
    allowed = {"PATH", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment["HOME"] = str(Path.home())
    environment["CONDUCTOR_BROWSER_PROFILE"] = str(profile)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    return environment


def ensure_owner(
    profile: Path,
    *,
    startup_timeout: float = 40.0,
    popen_factory: Any = subprocess.Popen,
) -> dict[str, Any]:
    path = _socket_path(profile)
    try:
        return _request(profile, {"command": "ping"}, timeout=0.3)
    except CLIError:
        pass
    if path.exists() and not stat.S_ISSOCK(path.lstat().st_mode):
        raise CLIError("Refusing an unexpected browser control path")
    module = "conductor_cli.browser.owner"
    process: Any = None
    try:
        process = popen_factory(
            [sys.executable, "-m", module, "--profile", str(profile)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            cwd=str(Path(__file__).resolve().parents[3]),
            env=_minimal_env(profile),
        )
    except OSError as exc:
        raise CLIError("Could not start the managed browser owner") from exc
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise CLIError("Managed browser owner exited during startup")
        try:
            return _request(profile, {"command": "ping"}, timeout=0.3)
        except CLIError:
            time.sleep(0.1)
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    raise CLIError("Managed browser did not become ready; install its Chromium runtime and try again")


def browser_command(agent: str, request: dict[str, Any], *, start: bool = False, timeout: float = 70.0) -> dict[str, Any]:
    profile = profile_for_agent(agent)
    if start:
        ensure_owner(profile)
    return _request(profile, request, timeout=timeout)


def close_owner(agent: str, *, timeout: float = 10.0) -> dict[str, Any]:
    profile = profile_for_agent(agent)
    result = _request(profile, {"command": "close"})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _request(profile, {"command": "ping"}, timeout=0.2)
        except CLIError:
            return result
        time.sleep(0.1)
    raise CLIError("Managed browser owner did not close in time")
