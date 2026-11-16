"""Daemon fleet config, secret loading, and v1 migration.

Secret values are returned only to the caller that builds a child environment.
They are never written into daemons.toml and never included in errors.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .ops_support import OpsError, Settings

# Names a provider is allowed to receive. Anything else in a secrets file is ignored.
PROVIDER_ENV: dict[str, frozenset[str]] = {
    "cursor": frozenset({"CURSOR_API_KEY"}),
    "deepseek-dsh": frozenset({"DEEPSEEK_API_KEY", "DSH_HOME"}),
    "deepseek": frozenset({"DEEPSEEK_API_KEY", "DSH_HOME"}),
    "kimi": frozenset({"KIMI_CODE_HOME", "KIMI_API_KEY"}),
    "codex": frozenset({"CODEX_HOME"}),
    "grok": frozenset({"XAI_API_KEY", "GROK_API_KEY"}),
    "zcode": frozenset({"ZCODE_HOME"}),
    "stepfun": frozenset({"STEPFUN_API_KEY", "STEPFUN_PLAN_API_KEY"}),
    "minimax": frozenset({"MINIMAX_API_KEY"}),
    "claude": frozenset({"CLAUDE_HOME"}),
    "mimo": frozenset({"MIMO_API_KEY"}),
    "doubao": frozenset({"ARK_API_KEY"}),
}

HOME_DEFAULTS = {
    "CODEX_HOME": ".codex",
    "KIMI_CODE_HOME": ".kimi-code",
    "DSH_HOME": ".dsh",
    "ZCODE_HOME": ".zcode",
    "CLAUDE_HOME": ".claude",
}

PROVIDER_ENV_FILES: dict[str, tuple[str, ...]] = {
    "deepseek-dsh": ("~/.config/deepseek/env",),
    "deepseek": ("~/.config/deepseek/env",),
}

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PROVIDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")


@dataclass
class DaemonSpec:
    provider: str
    ident: str
    env: list[str] = field(default_factory=list)
    env_files: list[str] = field(default_factory=list)
    keychain: dict[str, str] = field(default_factory=dict)


@dataclass
class FleetFile:
    backend_port: int
    web_port: int
    web_mode: str
    daemons: list[DaemonSpec]


def allowlist_for(provider: str) -> frozenset[str]:
    return PROVIDER_ENV.get(provider, frozenset())


def _mode_is_600(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return False
    return stat.S_IMODE(info.st_mode) == 0o600


def assert_secrets_file(path: Path) -> None:
    """Refuse to open a secrets file that is missing, linked, or not mode 600."""
    if path.name == ".zshenv" or ".zshenv" in path.parts:
        raise OpsError("refusing to read zshenv")
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        raise OpsError("secrets file must be a regular file")
    if not _mode_is_600(path):
        raise OpsError("secrets file must be mode 600")


def parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise OpsError(f"secrets file has an invalid line ({lineno})")
        key, value = line.split("=", 1)
        key = key.strip()
        if not _NAME.fullmatch(key):
            raise OpsError(f"secrets file has an invalid name ({lineno})")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def read_secrets(path: Path) -> dict[str, str]:
    assert_secrets_file(path)
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OpsError(f"could not read secrets file: {type(exc).__name__}") from exc
    return parse_env_text(text)


def load_fleet(settings: Settings) -> FleetFile:
    path = settings.daemons_file
    backend_port = settings.backend_port
    web_port = settings.web_port
    web_mode = settings.web_mode
    daemons: list[DaemonSpec] = []
    if path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise OpsError(f"could not read daemons.toml: {type(exc).__name__}") from exc
        if not isinstance(data, dict):
            raise OpsError("daemons.toml must be a table")
        backend = data.get("backend") or {}
        web = data.get("web") or {}
        if "CONDUCTOR_BACKEND_PORT" not in os.environ and isinstance(backend, dict):
            if isinstance(backend.get("port"), int):
                backend_port = int(backend["port"])
        if "CONDUCTOR_WEB_PORT" not in os.environ and isinstance(web, dict):
            if isinstance(web.get("port"), int):
                web_port = int(web["port"])
        if "CONDUCTOR_WEB_MODE" not in os.environ and isinstance(web, dict):
            mode = web.get("mode")
            # The file fills the unset-env default ("preview"). A caller that
            # already set static, before this load, keeps it. Env still wins above.
            if isinstance(mode, str) and mode in {"preview", "static"} and web_mode != "static":
                web_mode = mode
        rows = data.get("daemon") or []
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise OpsError("daemons.toml daemon entries must be a list")
        for row in rows:
            if not isinstance(row, dict):
                raise OpsError("daemon entry must be a table")
            provider = str(row.get("provider") or "")
            ident = str(row.get("id") or "")
            if not _PROVIDER.fullmatch(provider) or not _ID.fullmatch(ident):
                raise OpsError("daemon provider or id is invalid")
            env_names = [str(item) for item in row.get("env") or [] if isinstance(item, str)]
            env_files = [
                str(item) for item in row.get("env_files") or [] if isinstance(item, str)
            ]
            keychain_raw = row.get("keychain") or {}
            keychain = (
                {str(key): str(value) for key, value in keychain_raw.items()}
                if isinstance(keychain_raw, dict)
                else {}
            )
            daemons.append(
                DaemonSpec(provider, ident, env_names, env_files, keychain)
            )
    settings.backend_port = backend_port
    settings.web_port = web_port
    settings.web_mode = web_mode
    return FleetFile(backend_port, web_port, web_mode, daemons)


def render_fleet_toml(fleet: FleetFile) -> str:
    lines = [
        "[backend]",
        'host = "127.0.0.1"',
        f"port = {fleet.backend_port}",
        "",
        "[web]",
        'host = "127.0.0.1"',
        f"port = {fleet.web_port}",
        f'mode = "{fleet.web_mode}"',
        "",
    ]
    for daemon in fleet.daemons:
        lines.append("[[daemon]]")
        lines.append(f"provider = {json.dumps(daemon.provider)}")
        lines.append(f"id = {json.dumps(daemon.ident)}")
        if daemon.env:
            lines.append("env = [" + ", ".join(json.dumps(name) for name in daemon.env) + "]")
        if daemon.env_files:
            lines.append(
                "env_files = [" + ", ".join(json.dumps(name) for name in daemon.env_files) + "]"
            )
        if daemon.keychain:
            pairs = ", ".join(
                f"{json.dumps(key)} = {json.dumps(value)}"
                for key, value in sorted(daemon.keychain.items())
            )
            lines.append(f"keychain = {{ {pairs} }}")
        lines.append("")
    return "\n".join(lines)


def migrate_v1(
    script: Path, listing: Path, output: Path | None = None, *, web_port: int = 5170
) -> FleetFile:
    """Import v1-daemon.sh and v1-daemons.txt. The script is parsed, never executed.

    Pass ``output`` only to write. The CLI dry-run leaves it None and prints
    the rendered fleet instead.
    """
    try:
        script_text = script.read_text(encoding="utf-8", errors="replace")
        list_text = listing.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise OpsError(f"could not read migration input: {type(exc).__name__}") from exc
    daemons: list[DaemonSpec] = []
    for raw in list_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise OpsError("daemon list lines must be 'provider id'")
        provider, ident = parts
        if not _PROVIDER.fullmatch(provider) or not _ID.fullmatch(ident):
            raise OpsError("daemon list has an invalid provider or id")
        allowed = allowlist_for(provider)
        env_names: list[str] = []
        env_files: list[str] = []
        for name in ("CURSOR_API_KEY", "DEEPSEEK_API_KEY", "CODEX_HOME", "KIMI_CODE_HOME", "DSH_HOME"):
            if name in script_text and name in allowed and name not in env_names:
                env_names.append(name)
        for candidate in PROVIDER_ENV_FILES.get(provider, ()):
            marker = candidate.replace("~/", "")
            if marker in script_text or marker in script_text.replace("$HOME/", ""):
                env_files.append(candidate)
        daemons.append(DaemonSpec(provider, ident, env_names, env_files))
    fleet = FleetFile(8000, web_port, "preview", daemons)
    if output is not None:
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        output.write_text(render_fleet_toml(fleet), encoding="utf-8")
        os.chmod(output, 0o600)
    return fleet


def _expanded_env_file(raw: str, home: Path) -> Path:
    if raw.startswith("~/"):
        return home / raw[2:]
    return Path(raw).expanduser()


def provider_environment(
    settings: Settings,
    daemon: DaemonSpec,
    *,
    base_env: dict[str, str] | None = None,
    keychain_lookup=None,
) -> dict[str, str]:
    """Build a child environment. Values stay in the mapping and out of errors."""
    allowed = set(allowlist_for(daemon.provider))
    allowed.update(name for name in daemon.env if name in allowlist_for(daemon.provider) or name in HOME_DEFAULTS)
    # Configured names still have to be on the provider allow-list.
    allowed = {name for name in (set(daemon.env) | allowlist_for(daemon.provider)) if name in allowlist_for(daemon.provider)}
    source = base_env if base_env is not None else os.environ
    home = Path(source.get("HOME", str(Path.home()))).expanduser()
    child: dict[str, str] = {}
    for name in ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM"):
        if source.get(name):
            child[name] = source[name]
    child["HOME"] = str(home)
    child.setdefault("LANG", "en_US.UTF-8")
    child.setdefault("LC_ALL", "en_US.UTF-8")
    secrets = read_secrets(settings.secrets_file)
    for name, value in secrets.items():
        if name in allowed and value:
            child[name] = value
            if name.endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")):
                settings.sensitive.append(value)
    for raw in list(PROVIDER_ENV_FILES.get(daemon.provider, ())) + list(daemon.env_files):
        path = _expanded_env_file(raw, home)
        if path.name == ".zshenv" or ".zshenv" in path.parts or "zshenv" in path.parts:
            raise OpsError("refusing to read zshenv")
        if not path.exists():
            continue
        if not _mode_is_600(path):
            raise OpsError("provider env file must be mode 600")
        try:
            parsed = parse_env_text(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise OpsError(f"could not read provider env file: {type(exc).__name__}") from exc
        for name, value in parsed.items():
            if name in allowed and value:
                child[name] = value
                if name.endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")):
                    settings.sensitive.append(value)
    if keychain_lookup is not None:
        for name, reference in daemon.keychain.items():
            if name not in allowed:
                continue
            service, sep, account = reference.partition(":")
            if not sep or not service or not account or "\n" in reference:
                raise OpsError(f"keychain reference for {name} is invalid")
            value = keychain_lookup(service, account)
            if value:
                child[name] = value
                settings.sensitive.append(value)
    for name, folder in HOME_DEFAULTS.items():
        if name in allowed and name not in child:
            child[name] = str(home / folder)
    return child
