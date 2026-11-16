"""Detect CLIs, write ``agents.toml``, and hand official login to the CLI itself.

Never reads token values, never edits a CLI's own config, never calls a model.
``agents.toml`` holds no secret values. Writes are atomic, mode 600, and backed up
first. Symlinks are refused.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ops_cli_update import load_specs
from .ops_support import OpsError, RealSystem, Settings

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_MODES = frozenset({"official", "custom"})

# Candidate official login/status argv tails. Codex worker login stays on Task 2.
LOGIN_COMMAND: dict[str, tuple[str, ...]] = {
    "claude": ("auth", "login"),
    "codex": ("login",),
    "kimi": ("login",),
    "grok": ("login",),
    "cursor-agent": ("login",),
    "mcode": ("login",),
    "step": ("login",),
    "opencode": ("auth", "login"),
}
STATUS_COMMAND: dict[str, tuple[str, ...]] = {
    "claude": ("auth", "status"),
    "codex": ("login", "status"),
    "kimi": ("login", "status"),
    "grok": ("login", "status"),
    "cursor-agent": ("login", "status"),
    "mcode": ("login", "status"),
    "step": ("login", "status"),
    "opencode": ("auth", "status"),
}
WORKER_ENV: dict[str, str] = {
    "claude": "CLAUDE_CONFIG_DIR",
    "kimi": "KIMI_CODE_HOME",
    "grok": "GROK_HOME",
    "cursor-agent": "CURSOR_CONFIG_DIR",
    "mcode": "MINIMAX_DATA_DIR",
    "step": "STEP_CODING_AGENT_DIR",
    "opencode": "OPENCODE_CONFIG_DIR",
    "zcode": "HOME",
    "dsh": "DSH_HOME",
}


@dataclass
class AgentRow:
    name: str
    enabled: bool = False
    mode: str = "official"
    backend_note: str = ""
    backend_vendor: str = ""
    passthrough_env: list[str] = field(default_factory=list)


def agents_path(settings: Settings) -> Path:
    return settings.conductor_home / "agents.toml"


def detect(system: RealSystem, settings: Settings) -> list[dict[str, object]]:
    """Installed? version? logged in? Uses ``--version`` and status argv only."""
    specs, _interval = load_specs(settings)
    overlay = _clis_overlay(settings)
    rows: list[dict[str, object]] = []
    for spec in specs:
        login = _argv_field(overlay, spec.name, "login_command", LOGIN_COMMAND.get(spec.name, ()))
        status = _argv_field(overlay, spec.name, "status_command", STATUS_COMMAND.get(spec.name, ()))
        version = _probe(system, [spec.binary, "--version"])
        logged_in = None
        if status and version["installed"]:
            logged_in = _probe(system, [spec.binary, *status])["ok"]
        rows.append(
            {
                "name": spec.name,
                "binary": spec.binary,
                "installed": version["installed"],
                "version": version["text"],
                "logged_in": logged_in,
                "login_command": list(login),
                "status_command": list(status),
            }
        )
    return rows


def load_agents(settings: Settings) -> dict[str, AgentRow]:
    path = agents_path(settings)
    if not path.is_file() or path.is_symlink():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    agents = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(agents, dict):
        return {}
    rows: dict[str, AgentRow] = {}
    for name, raw in agents.items():
        if not isinstance(raw, dict):
            continue
        rows[str(name)] = _row_from_dict(str(name), raw)
    return rows


def write_agents(settings: Settings, rows: dict[str, AgentRow]) -> Path:
    """Atomically write mode 600. Refuse symlinks. No secret values."""
    path = agents_path(settings)
    backup = path.with_name("agents.toml.bak")
    body = _render_toml(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    _refuse_symlink(path)
    _refuse_symlink(backup)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".agents.toml.")
    tmp = Path(tmp_name)
    try:
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, body.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        _validate_toml(body)
        _backup_regular(path, backup)
        _refuse_symlink(path)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return path


def upsert(
    settings: Settings,
    name: str,
    *,
    enabled: bool | None = None,
    mode: str | None = None,
    backend_note: str | None = None,
    backend_vendor: str | None = None,
    passthrough_env: list[str] | None = None,
) -> dict[str, AgentRow]:
    rows = load_agents(settings)
    current = rows.get(name) or AgentRow(name=name)
    if enabled is not None:
        current.enabled = enabled
    if mode is not None:
        if mode not in _MODES:
            raise OpsError("mode must be official or custom")
        current.mode = mode
    if backend_note is not None:
        current.backend_note = backend_note.strip()
    if backend_vendor is not None:
        current.backend_vendor = backend_vendor.strip()
    if passthrough_env is not None:
        current.passthrough_env = _env_names(passthrough_env)
    rows[name] = current
    write_agents(settings, rows)
    return rows


def setup_from_detect(
    system: RealSystem,
    settings: Settings,
    *,
    force: bool = False,
) -> dict[str, object]:
    """Write installed CLIs as official. An existing file needs ``force`` and is not merged."""
    path = agents_path(settings)
    if _lstat_mode(path) is not None and not force:
        raise OpsError("agents.toml already exists; pass --force")
    found = detect(system, settings)
    rows: dict[str, AgentRow] = {}
    enabled: list[str] = []
    for item in found:
        if not item["installed"]:
            continue
        name = str(item["name"])
        rows[name] = AgentRow(name=name, enabled=True, mode="official")
        enabled.append(name)
    written = write_agents(settings, rows)
    return {"path": str(written), "enabled": enabled, "detected": found}


def login(
    system: RealSystem,
    settings: Settings,
    name: str,
    *,
    for_worker: bool = False,
    worker_home: Path | None = None,
) -> dict[str, object]:
    """Hand the terminal to the CLI login, then confirm with the status command."""
    if name == "codex" and for_worker:
        raise OpsError("codex worker login stays on the Task 2 developer_instructions path")
    specs, _interval = load_specs(settings)
    spec = next((item for item in specs if item.name == name), None)
    if spec is None:
        raise OpsError(f"unknown cli {name}")
    overlay = _clis_overlay(settings)
    argv_tail = _argv_field(overlay, name, "login_command", LOGIN_COMMAND.get(name, ()))
    status_tail = _argv_field(overlay, name, "status_command", STATUS_COMMAND.get(name, ()))
    if not argv_tail:
        raise OpsError(f"{name} has no official login command")
    if not status_tail:
        raise OpsError(f"{name} has no official status command")
    env = {key: value for key, value in os.environ.items() if key in _SAFE_ENV}
    env["HOME"] = str(Path.home())
    if for_worker:
        isolated = worker_home
        if isolated is None:
            raise OpsError("--for-worker needs --worker-home")
        isolated = isolated.expanduser().resolve()
        if isolated == Path.home().resolve():
            raise OpsError("worker home must not be the operator HOME")
        isolated.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(isolated)
        key = WORKER_ENV.get(name)
        if key and key != "HOME":
            env[key] = str(isolated)
    argv = [spec.binary, *argv_tail]
    status_argv = [spec.binary, *status_tail]
    system.run_terminal(argv, env=env, cwd=None, timeout=300.0)
    status = system.run_terminal(status_argv, env=env, cwd=None, timeout=300.0)
    return {
        "cli": name,
        "argv": argv,
        "for_worker": for_worker,
        "home": env["HOME"],
        "code": status.code,
        "ok": status.code == 0,
    }


_SAFE_ENV = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "TEMP",
        "TMP",
        "USER",
        "LOGNAME",
        "SHELL",
    }
)


def _row_from_dict(name: str, raw: dict[str, Any]) -> AgentRow:
    env_raw = raw.get("passthrough_env") or []
    names = _env_names([str(item) for item in env_raw]) if isinstance(env_raw, list) else []
    mode = str(raw.get("mode") or "official")
    if mode not in _MODES:
        mode = "official"
    return AgentRow(
        name=name,
        enabled=bool(raw.get("enabled")),
        mode=mode,
        backend_note=str(raw.get("backend_note") or ""),
        backend_vendor=str(raw.get("backend_vendor") or ""),
        passthrough_env=names,
    )


def _env_names(values: list[str]) -> list[str]:
    names: list[str] = []
    for item in values:
        text = item.strip()
        if not text or "=" in text or not _ENV_NAME.match(text):
            raise OpsError("passthrough_env entries must be variable names")
        names.append(text)
    return names


def _render_toml(rows: dict[str, AgentRow]) -> str:
    lines = ["# Conductor agent roster. No secret values.", ""]
    for name in sorted(rows):
        row = rows[name]
        lines.append(f"[agents.{name}]")
        lines.append(f"enabled = {'true' if row.enabled else 'false'}")
        lines.append(f"mode = {_toml_string(row.mode)}")
        if row.backend_note:
            lines.append(f"backend_note = {_toml_string(row.backend_note)}")
        if row.backend_vendor:
            lines.append(f"backend_vendor = {_toml_string(row.backend_vendor)}")
        if row.passthrough_env:
            joined = ", ".join(_toml_string(item) for item in row.passthrough_env)
            lines.append(f"passthrough_env = [{joined}]")
        lines.append("")
    return "\n".join(lines)


def _toml_string(value: str) -> str:
    """A JSON string is a valid TOML basic string."""
    return json.dumps(value, ensure_ascii=False)


def _validate_toml(body: str) -> None:
    valid = True
    try:
        tomllib.loads(body)
    except Exception:
        valid = False
    if not valid:
        # Outside the handler so the decode error, which quotes the rejected
        # text, is not chained onto this OpsError.
        raise OpsError("agents.toml text is not valid TOML")


def _lstat_mode(path: Path) -> int | None:
    try:
        return os.lstat(path).st_mode
    except FileNotFoundError:
        return None


def _refuse_symlink(path: Path) -> None:
    mode = _lstat_mode(path)
    if mode is not None and stat.S_ISLNK(mode):
        raise OpsError(f"{path.name} is a symlink")


def _backup_regular(path: Path, backup: Path) -> None:
    """Copy a regular ``agents.toml`` to ``agents.toml.bak`` before replace."""
    _refuse_symlink(path)
    _refuse_symlink(backup)
    mode = _lstat_mode(path)
    if mode is None:
        return
    if not stat.S_ISREG(mode):
        raise OpsError(f"{path.name} is not a regular file")
    _copy_private(path, backup)


def _copy_private(src: Path, dst: Path) -> None:
    data = _read_nofollow(src)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(dst, flags, 0o600)
    except OSError:
        if _lstat_mode(dst) is not None and stat.S_ISLNK(_lstat_mode(dst) or 0):
            raise OpsError(f"{dst.name} is a symlink") from None
        raise
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_nofollow(path: Path) -> bytes:
    mode = _lstat_mode(path)
    if mode is not None and stat.S_ISLNK(mode):
        raise OpsError(f"{path.name} is a symlink")
    if mode is None or not stat.S_ISREG(mode):
        raise OpsError(f"{path.name} is not a regular file")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        if _lstat_mode(path) is not None and stat.S_ISLNK(_lstat_mode(path) or 0):
            raise OpsError(f"{path.name} is a symlink") from None
        raise
    try:
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OpsError("could not write agents.toml")
        view = view[written:]


def _clis_overlay(settings: Settings) -> dict[str, Any]:
    path = settings.conductor_home / "clis.toml"
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _argv_field(
    overlay: dict[str, Any],
    name: str,
    key: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    row = overlay.get(name)
    if not isinstance(row, dict) or key not in row:
        return default
    raw = row.get(key)
    if isinstance(raw, list) and all(isinstance(item, str) and item.strip() for item in raw):
        return tuple(str(item) for item in raw)
    if isinstance(raw, str) and raw.strip():
        return tuple(raw.split())
    return default


def _probe(system: RealSystem, argv: list[str]) -> dict[str, object]:
    try:
        completed = system.run(argv, timeout=8.0)
    except OpsError:
        return {"installed": False, "ok": False, "text": ""}
    text = (completed.stdout or completed.stderr or "").strip().splitlines()
    version = text[0][:80] if text else ""
    return {
        "installed": completed.code == 0,
        "ok": completed.code == 0,
        "text": version,
    }
