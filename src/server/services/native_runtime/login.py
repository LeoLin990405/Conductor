"""Official CLI sign-in for native agent providers.

The only login implementation here is the vendor's own CLI. Conductor starts the
CLI's login command, relays the URL it prints, writes a pasted authorization
code back to its stdin, and then asks the same CLI for its auth status. No OAuth
token is minted, parsed or stored by this module.

PTY sessions are used because the Claude login prompt is interactive; Cursor
prints a browser URL and finishes by itself.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_LOGIN_TTL_SECONDS = 15 * 60
_PROBE_INTERVAL_SECONDS = 2.0
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_URL_START_RE = re.compile(r"https?://[^\s<>\"']+")
_URL_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "-._~:/?#[]@!$&'()*+,;=%"
)


def _extract_url(text: str) -> str | None:
    """Find an auth URL, joining terminal-wrapped segments.

    A TUI may wrap its auth URL into consecutive lines that each start with
    one space and otherwise contain URL characters only; joining those lines
    is safe. A line with prose after the URL stops the join.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        pos = line.find("http")
        if pos < 0:
            continue
        candidate = line[pos:]
        end = 0
        while end < len(candidate) and candidate[end] in _URL_CHARS:
            end += 1
        parts = [candidate[:end]]
        if end == len(candidate):
            for following in lines[index + 1:]:
                stripped = following.strip()
                if not stripped:
                    break
                if all(char in _URL_CHARS for char in stripped):
                    parts.append(stripped)
                else:
                    break
        return "".join(parts).rstrip(".,);]") or None
    return None


class NativeLoginError(RuntimeError):
    """A login cannot be started or continued."""


@dataclass
class _LoginSpec:
    command: tuple[str, ...]
    #: Probe returns True when this CLI already has a usable account.
    probe: Callable[[str], bool | None]
    env: dict[str, str] = field(default_factory=dict)
    #: Text that means the CLI is asking for a pasted authorization code.
    input_markers: tuple[str, ...] = ()
    #: Text that means a TUI selection is waiting; Enter accepts the default.
    auto_enter_marker: str | None = None


@dataclass
class NativeLoginSession:
    session_id: str
    provider: str
    status: str = "waiting"
    auth_url: str | None = None
    requires_input: bool = False
    instruction: str = ""
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # internals, not serialized
    _binary: str = ""
    _master_fd: int = -1
    _process: subprocess.Popen[bytes] | None = None
    _thread: threading.Thread | None = None
    _cwd: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "provider": self.provider,
            "status": self.status,
            "auth_url": self.auth_url,
            "requires_input": self.requires_input,
            "instruction": self.instruction,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _binary_for(provider: str) -> str | None:
    from src.server.services.native_runtime.registry import status_for

    try:
        return status_for(provider).binary
    except Exception:
        return None


def _probe_claude(binary: str) -> bool | None:
    try:
        out = subprocess.run(
            [binary, "auth", "status", "--json"],
            capture_output=True, text=True, timeout=12,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return bool(json.loads(out.stdout or "{}").get("loggedIn"))
    except (json.JSONDecodeError, AttributeError):
        return None


def _probe_cursor(binary: str) -> bool | None:
    try:
        out = subprocess.run(
            [binary, "status", "--format", "json"],
            capture_output=True, text=True, timeout=12,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        payload = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return None
    return bool(payload.get("isAuthenticated"))


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _probe_codex(binary: str) -> bool | None:
    try:
        out = subprocess.run(
            [binary, "login", "status"], capture_output=True, text=True, timeout=12,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = f"{out.stdout}\n{out.stderr}".strip().lower()
    if "not logged in" in text or "logged out" in text:
        return False
    return out.returncode == 0 and "logged in" in text


def _probe_kimi(binary: str) -> bool | None:
    del binary
    path = Path(os.environ.get("HOME") or str(Path.home())) / ".kimi-code" / "credentials" / "kimi-code.json"
    payload = _read_json_file(path)
    if payload is None:
        return False
    return bool(payload.get("access_token") or payload.get("refresh_token"))


def _probe_zcode(binary: str) -> bool | None:
    del binary
    path = Path(os.environ.get("HOME") or str(Path.home())) / ".zcode" / "v2" / "credentials.json"
    payload = _read_json_file(path)
    if payload is None:
        return False
    for key, value in payload.items():
        folded = str(key).lower()
        if ("access_token" in folded or folded == "zcodejwttoken") and str(value or "").strip():
            return True
    return False


_SPECS: dict[str, _LoginSpec] = {
    "claude-code": _LoginSpec(
        command=("auth", "login", "--claudeai"),
        input_markers=("paste code here", "paste code"),
        probe=_probe_claude,
    ),
    "cursor": _LoginSpec(
        command=("login",),
        env={"NO_OPEN_BROWSER": "1"},
        probe=_probe_cursor,
    ),
    "codex": _LoginSpec(
        command=("login",),
        probe=_probe_codex,
    ),
    "kimi": _LoginSpec(
        command=("login", "--region", "mainland-cn"),
        probe=_probe_kimi,
    ),
    "zcode": _LoginSpec(
        command=("login", "bigmodel", "--no-browser"),
        env={
            "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": "/Applications/ZCode.app/Contents/Resources/config/provider/zcode-builtin.json",
        },
        probe=_probe_zcode,
    ),
}


class NativeLoginManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, NativeLoginSession] = {}
        self._active: dict[str, str] = {}

    def providers(self) -> list[str]:
        return list(_SPECS)

    def start(self, provider: str) -> dict[str, Any]:
        spec = _SPECS.get(provider)
        if spec is None:
            raise NativeLoginError("that provider has no official login")
        binary = _binary_for(provider) or shutil.which(provider)
        if not binary:
            raise NativeLoginError(f"{provider} CLI is not installed")
        # Already signed in: do not run the vendor login again just to show a button.
        already = spec.probe(binary)
        if already:
            return NativeLoginSession(
                session_id="",
                provider=provider,
                status="authenticated",
                instruction="Already signed in.",
            ).to_dict()
        with self._lock:
            current_id = self._active.get(provider)
            current = self._sessions.get(current_id or "")
            if current is not None and current.status in {"waiting", "input_required"}:
                return current.to_dict()
        session = NativeLoginSession(
            session_id=uuid.uuid4().hex,
            provider=provider,
            _binary=binary,
            instruction=(
                "Open the sign-in URL, finish the browser flow, then paste the "
                "authorization code here."
                if spec.input_markers else
                "Open the sign-in URL and finish the browser flow."
            ),
        )
        with self._lock:
            self._sessions[session.session_id] = session
            self._active[provider] = session.session_id
        try:
            self._spawn(session, spec)
        except Exception as exc:
            with self._lock:
                session.status = "failed"
                session.error = f"{type(exc).__name__}: {exc}"
                self._active.pop(provider, None)
            raise NativeLoginError(session.error) from exc
        session._thread = threading.Thread(
            target=self._pump, args=(session, spec), daemon=True,
            name=f"native-login-{provider}",
        )
        session._thread.start()
        return session.to_dict()

    def _spawn(self, session: NativeLoginSession, spec: _LoginSpec) -> None:
        cwd = tempfile.mkdtemp(prefix="conductor-native-login-")
        env = os.environ.copy()
        env.update(spec.env)
        # A login prompt is interactive even when the server has no TTY.
        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 2000, 0, 0))
            attrs = termios.tcgetattr(slave)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            cmd = (session._binary, *spec.command)
            proc = subprocess.Popen(
                cmd,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        except Exception:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        session._master_fd = master
        session._process = proc
        session._cwd = cwd

    def get(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise NativeLoginError("login session not found")
            return session.to_dict()

    def submit_code(self, session_id: str, code: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise NativeLoginError("login session not found")
            if session.status not in {"waiting", "input_required"}:
                raise NativeLoginError("this login is no longer waiting for input")
            if session._master_fd < 0:
                raise NativeLoginError("login process is not running")
            payload = (code.strip() + "\n").encode()
            try:
                os.write(session._master_fd, payload)
            except OSError as exc:
                raise NativeLoginError(f"could not write to the login process: {exc}") from exc
            session.requires_input = False
            session.instruction = "Checking the code with the official CLI…"
            session.updated_at = time.time()
            return session.to_dict()

    def cancel(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise NativeLoginError("login session not found")
            session.status = "cancelled"
            session.updated_at = time.time()
            self._active.pop(session.provider, None)
            self._terminate(session)
            return session.to_dict()

    def _terminate(self, session: NativeLoginSession) -> None:
        proc = session._process
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                try:
                    proc.terminate()
                except OSError:
                    pass
        fd = session._master_fd
        session._master_fd = -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    def _pump(self, session: NativeLoginSession, spec: _LoginSpec) -> None:
        proc = session._process
        assert proc is not None
        buffer = ""
        sent_enter = False
        last_probe = 0.0
        deadline = session.created_at + _LOGIN_TTL_SECONDS
        try:
            while True:
                if session.status in {"cancelled", "authenticated", "failed", "expired"}:
                    break
                if time.time() > deadline:
                    session.status = "expired"
                    session.error = "login timed out"
                    break
                fd = session._master_fd
                if fd < 0:
                    break
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(fd, 4096)
                    except OSError:
                        chunk = b""
                    if chunk:
                        text = chunk.decode("utf-8", "replace")
                        buffer = (buffer + text)[-20000:]
                        clean = _ANSI_RE.sub("", buffer)
                        clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", clean)
                        url = _extract_url(clean)
                        if url and not session.auth_url:
                            session.auth_url = url
                            session.updated_at = time.time()
                        lowered = clean.lower()
                        if any(marker in lowered for marker in spec.input_markers):
                            session.requires_input = True
                            session.status = "input_required"
                            session.updated_at = time.time()
                        if spec.auto_enter_marker and not sent_enter:
                            if spec.auto_enter_marker.lower() in lowered:
                                sent_enter = True
                                try:
                                    os.write(fd, b"\r")
                                except OSError:
                                    pass
                now = time.time()
                if now - last_probe >= _PROBE_INTERVAL_SECONDS:
                    last_probe = now
                    result = spec.probe(session._binary)
                    if result:
                        session.status = "authenticated"
                        session.requires_input = False
                        session.instruction = "Signed in."
                        session.updated_at = time.time()
                        break
                elif proc.poll() is not None:
                    result = spec.probe(session._binary)
                    session.status = "authenticated" if result else "failed"
                    if not result:
                        session.error = "the CLI exited before authentication completed"
                    session.updated_at = time.time()
                    break
        finally:
            with self._lock:
                self._active.pop(session.provider, None)
                self._terminate(session)
                shutil.rmtree(session._cwd, ignore_errors=True)
                if session.status in {"waiting", "input_required"}:
                    session.status = "failed"
                    session.error = session.error or "login session ended"
                session.updated_at = time.time()


login_manager = NativeLoginManager()


def auth_status(provider: str) -> dict[str, Any]:
    """Official CLI auth status for the providers that have a login flow."""
    spec = _SPECS.get(provider)
    if spec is None:
        return {"provider": provider, "supported": False, "installed": False, "authenticated": None}
    binary = _binary_for(provider) or shutil.which(provider)
    if not binary:
        return {"provider": provider, "supported": True, "installed": False, "authenticated": False}
    authenticated = spec.probe(binary)
    return {
        "provider": provider,
        "supported": True,
        "installed": True,
        "authenticated": bool(authenticated) if authenticated is not None else None,
    }
