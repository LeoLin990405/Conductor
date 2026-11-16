#!/usr/bin/env python3
"""Conductor pre-flight doctor.

Checks the things that decide whether this checkout can run: env files, the
Postgres/Redis dependencies, Docker availability, the API/web ports, backend
health, LLM providers, native CLI providers and MCP servers.

The script uses only the Python standard library so it also works before
``make install``. Exit code is 1 only when a required check fails; warnings
describe optional pieces (Docker daemon, dev web server, native CLIs, lazily
spawned MCP processes).

Examples:
    make doctor
    make doctor ARGS="--json"
    python3 scripts/doctor.py --strict
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"

SYMBOLS = {"ok": "✓", "warn": "!", "fail": "✗", "skip": "-"}


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | skip
    detail: str
    fix: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Doctor:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, fix: str | None = None, **data: Any) -> None:
        self.checks.append(Check(name, status, detail, fix, data))

    @property
    def failed(self) -> bool:
        return any(c.status == "fail" for c in self.checks)

    def render(self, color: bool = True, strict: bool = False) -> str:
        lines: list[str] = []
        c = {k: (v if color else "") for k, v in {"ok": GREEN, "warn": YELLOW, "fail": RED, "skip": DIM}.items()}
        lines.append("Conductor doctor")
        lines.append(f"repo: {ROOT}")
        lines.append("")
        for check in self.checks:
            symbol = SYMBOLS.get(check.status, "?")
            prefix = f"{c.get(check.status, '')}{symbol}{RESET if color else ''}"
            lines.append(f"  {prefix} {check.name:<19} {check.detail}")
            if check.fix and check.status in {"fail", "warn"}:
                lines.append(f"      {c['skip'] if color else ''}fix: {check.fix}{RESET if color else ''}")
        ok = sum(c.status == "ok" for c in self.checks)
        warn = sum(c.status == "warn" for c in self.checks)
        fail = sum(c.status == "fail" for c in self.checks)
        skip = sum(c.status == "skip" for c in self.checks)
        lines.append("")
        lines.append(f"Summary: {ok} ok, {warn} warn, {fail} fail, {skip} skipped")
        if fail:
            lines.append("Result: NOT READY — fix the failed checks above.")
        elif strict and warn:
            lines.append("Result: NOT READY — strict mode promotes warnings to failures.")
        elif warn:
            lines.append("Result: READY with warnings — optional pieces are unavailable.")
        else:
            lines.append("Result: READY")
        return "\n".join(lines)


def load_dotenv(path: Path) -> dict[str, str]:
    """Parse a simple .env without executing it or overriding real env vars."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def get(name: str, dotenv: dict[str, str], default: str = "") -> str:
    return os.environ.get(name, dotenv.get(name, default))


def env_int(name: str, dotenv: dict[str, str], default: int) -> int:
    try:
        return int(get(name, dotenv, str(default)))
    except ValueError:
        return default


def run(cmd: list[str], timeout: int = 6, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def port_listening(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def listening_pid(port: int) -> int | None:
    """PID owning a local listening port, when the platform tools expose it."""
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    try:
        result = run([lsof, "-tiTCP:%d" % port, "-sTCP:LISTEN"], timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def process_env(pid: int) -> dict[str, str]:
    """Best-effort environment of a same-user process via `ps eww`."""
    try:
        result = run(["ps", "eww", "-p", str(pid)], timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    values: dict[str, str] = {}
    for token in result.stdout.split():
        if "=" in token:
            key, value = token.split("=", 1)
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                values[key] = value
    return values


def http_json(url: str, timeout: float = 5.0) -> tuple[int | None, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None, None


def check_env(doctor: Doctor, dotenv: dict[str, str]) -> None:
    if ENV_FILE.is_file():
        doctor.add("env file", "ok", str(ENV_FILE), data={"keys": len(dotenv)})
    else:
        doctor.add(
            "env file",
            "warn",
            f"{ENV_FILE} not found",
            "cp .env.example .env && make config",
        )
    for name in ("config.yaml", "agent_config.yaml"):
        path = ROOT / name
        doctor.add("config file", "ok" if path.is_file() else "fail", str(path), f"restore {name} from the repository")


def check_docker(doctor: Doctor) -> None:
    docker = shutil.which("docker")
    if not docker:
        doctor.add("docker", "warn", "not installed (host DB/Redis mode)", "install Docker Desktop, or keep using host Postgres/Redis")
        return
    try:
        result = run([docker, "info", "--format", "{{.ServerVersion}}"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        doctor.add("docker", "warn", f"daemon check failed: {exc}", "start Docker Desktop")
        return
    if result.returncode == 0 and result.stdout.strip():
        doctor.add("docker", "ok", f"daemon {result.stdout.strip()}", data={"server_version": result.stdout.strip()})
    else:
        detail = (result.stderr or result.stdout or "daemon not running").strip().splitlines()[0]
        doctor.add("docker", "warn", f"not running: {detail}", "start Docker Desktop, or use host Postgres/Redis")


def check_postgres(doctor: Doctor, dotenv: dict[str, str]) -> None:
    host = get("DB_HOST", dotenv, "localhost")
    port = env_int("DB_PORT", dotenv, 5432)
    user = get("DB_USER", dotenv, "postgres")
    db = get("DB_NAME", dotenv, "postgres")
    password = get("DB_PASSWORD", dotenv)
    if not port_listening(host, port):
        doctor.add(
            "postgres",
            "fail",
            f"{host}:{port} not reachable",
            "make setup-db  (Docker) or start your local PostgreSQL",
        )
        return
    if not shutil.which("psql"):
        doctor.add("postgres", "ok", f"{host}:{port} accepting connections (psql not installed)")
        return
    env = dict(os.environ)
    env["PGPASSWORD"] = password
    try:
        result = run(
            ["psql", "-h", host, "-p", str(port), "-U", user, "-d", db, "-tAc", "select 1"],
            timeout=8,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        doctor.add("postgres", "fail", f"query failed: {exc}", "check DB_HOST/DB_USER/DB_PASSWORD")
        return
    if result.returncode == 0 and result.stdout.strip() == "1":
        doctor.add("postgres", "ok", f"{host}:{port}/{db} as {user}", data={"host": host, "port": port, "database": db})
    else:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1] if (result.stderr or result.stdout).strip() else "query failed"
        doctor.add("postgres", "fail", f"{host}:{port} rejected: {detail}", "check .env DB_* or run make setup-db")
    # Schema readiness is what the backend actually needs; the query above only
    # proves credentials. Ask the table count without requiring Alembic state.
    try:
        result = run(
            ["psql", "-h", host, "-p", str(port), "-U", user, "-d", db, "-tAc", "select count(*) from information_schema.tables where table_schema='public'"],
            timeout=8,
            env=env,
        )
        count = int((result.stdout or "0").strip() or 0)
        if count > 0:
            doctor.add("database schema", "ok", f"{count} public tables")
        else:
            doctor.add("database schema", "fail", "no public tables", "make migrate")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        doctor.add("database schema", "skip", "could not count tables")


def check_redis(doctor: Doctor, dotenv: dict[str, str]) -> None:
    raw = get("REDIS_URL", dotenv, "redis://localhost:6379/0")
    try:
        parsed = urllib.parse.urlsplit(raw)
        host = parsed.hostname or "localhost"
        port = parsed.port or 6379
        password = parsed.password or get("REDIS_PASSWORD", dotenv)
    except ValueError:
        doctor.add("redis", "fail", f"invalid REDIS_URL: {raw}", "set REDIS_URL=redis://localhost:6379/0")
        return
    if not port_listening(host, port):
        doctor.add("redis", "fail", f"{host}:{port} not reachable", "make setup-db  (Docker) or start your local Redis")
        return
    try:
        with socket.create_connection((host, port), timeout=2) as sock:
            sock.settimeout(2)
            if password:
                sock.sendall(f"AUTH {password}\r\n".encode())
                auth = sock.recv(256).decode("latin-1", "replace")
                if not auth.startswith("+OK"):
                    raise OSError(auth.strip() or "AUTH failed")
            sock.sendall(b"PING\r\n")
            reply = sock.recv(256).decode("latin-1", "replace").strip()
        if reply.startswith("+PONG"):
            doctor.add("redis", "ok", f"{host}:{port}", data={"host": host, "port": port})
        else:
            doctor.add("redis", "fail", f"{host}:{port} unexpected reply: {reply}", "check REDIS_URL/REDIS_PASSWORD")
    except OSError as exc:
        doctor.add("redis", "fail", f"{host}:{port} ping failed: {exc}", "check REDIS_URL/REDIS_PASSWORD")


def check_ports(doctor: Doctor, dotenv: dict[str, str]) -> tuple[bool, bool]:
    backend_port = env_int("BACKEND_PORT", dotenv, 8000)
    web_port = env_int("WEB_PORT", dotenv, 5170)
    backend_up = port_listening("127.0.0.1", backend_port)
    web_up = port_listening("127.0.0.1", web_port)
    doctor.add("port backend", "ok" if backend_up else "fail", f"127.0.0.1:{backend_port} {'listening' if backend_up else 'free'}", None if backend_up else "make dev")
    doctor.add("port web", "ok" if web_up else "warn", f"127.0.0.1:{web_port} {'listening' if web_up else 'free'}", None if web_up else "make dev-web")
    return backend_up, web_up


def check_backend(doctor: Doctor, dotenv: dict[str, str], backend_up: bool) -> bool:
    port = env_int("BACKEND_PORT", dotenv, 8000)
    base = f"http://127.0.0.1:{port}"
    if not backend_up:
        doctor.add("backend health", "skip", "backend is not listening")
        return False
    status, body = http_json(f"{base}/health")
    if status != 200 or not isinstance(body, dict) or body.get("status") != "healthy":
        detail = f"HTTP {status}" if status else "no response"
        doctor.add("backend health", "fail", detail, f"curl {base}/health && tail -50 ~/.conductor/logs/backend.log")
        return False
    checkpointer = body.get("checkpointer") or {}
    cp_status = checkpointer.get("status") if isinstance(checkpointer, dict) else None
    if cp_status and cp_status != "healthy":
        doctor.add("backend health", "fail", f"healthy, checkpointer={cp_status}", "restart backend and inspect its log")
        return False
    doctor.add("backend health", "ok", f"{body.get('service', 'backend')} {body.get('version', '')} @ {base}".strip(), data={"version": body.get("version")})
    return True


def check_llm_providers(doctor: Doctor, dotenv: dict[str, str]) -> None:
    port = env_int("BACKEND_PORT", dotenv, 8000)
    status, body = http_json(f"http://127.0.0.1:{port}/api/v1/models")
    if status != 200 or not isinstance(body, dict):
        doctor.add("llm providers", "skip", "models endpoint unavailable")
        return
    models = body.get("models") or {}
    providers = [key for key in models if key != "__auto__"] if isinstance(models, dict) else []
    if providers:
        doctor.add("llm providers", "ok", f"{len(providers)} configured: {', '.join(providers[:8])}", data={"providers": providers})
    else:
        doctor.add("llm providers", "fail", "no model providers configured", "make config")


def check_native_providers(doctor: Doctor, dotenv: dict[str, str]) -> None:
    port = env_int("BACKEND_PORT", dotenv, 8000)
    status, body = http_json(f"http://127.0.0.1:{port}/api/v1/native-agents")
    if status != 200 or not isinstance(body, dict):
        doctor.add("native providers", "skip", "native-agents endpoint unavailable")
        return
    runtimes = body.get("runtimes") or []
    live = [r.get("provider") for r in runtimes if isinstance(r, dict) and r.get("live_verified")]
    installed = [r.get("provider") for r in runtimes if isinstance(r, dict) and r.get("installed")]
    doctor.add(
        "native providers",
        "ok" if live else "warn",
        f"{len(live)} live / {len(installed)} installed"
        + (f": {', '.join(live)}" if live else ""),
        None if live else "install and log in a native CLI, or use the PTC runtime",
        data={"live": live, "installed": installed},
    )
    acp = get("NATIVE_CLI_ACP", dotenv)
    source = ".env"
    if not acp:
        pid = listening_pid(env_int("BACKEND_PORT", dotenv, 8000))
        if pid:
            backend_env = process_env(pid)
            acp = backend_env.get("NATIVE_CLI_ACP", "")
            source = f"backend pid {pid}"
    if acp:
        doctor.add("native acp env", "ok", f"{acp} ({source})")
    else:
        doctor.add(
            "native acp env",
            "warn",
            "NATIVE_CLI_ACP is unset; ACP lanes fall back to their defaults",
            "set NATIVE_CLI_ACP in .env or the launch environment",
        )


def check_desktop(doctor: Doctor) -> None:
    """Surface the installed desktop app when it drifts from the repo version."""
    try:
        # doctor may run as `python3 scripts/doctor.py`, which puts scripts/ —
        # not the repo root — on sys.path. Import the sibling release helper as
        # a package from the root, then restore the path.
        sys.path.insert(0, str(ROOT))
        try:
            from scripts import desktop_release
        finally:
            sys.path.pop(0)
        status = desktop_release.collect_status()
    except Exception as exc:  # noqa: BLE001 - desktop is optional; never break doctor
        doctor.add("desktop app", "skip", f"could not inspect desktop app: {exc}")
        return
    if not status.installed:
        doctor.add("desktop app", "warn", f"not installed ({desktop_release.INSTALL_TARGET})", "make desktop-dist && make desktop-install")
    elif status.installed.version != status.repo_version:
        doctor.add(
            "desktop app",
            "warn",
            f"installed {status.installed.version or '?'} != repo {status.repo_version}",
            "make desktop-dist && make desktop-install",
            installed=status.installed.path,
        )
    elif status.duplicates:
        doctor.add(
            "desktop app",
            "warn",
            f"{len(status.duplicates)} duplicate bundle(s) besides {status.installed.path}",
            "make desktop-install  (removes duplicate build bundles)",
        )
    else:
        doctor.add(
            "desktop app",
            "ok",
            f"{status.installed.version} at {status.installed.path} ({status.installed.signature or 'signature unknown'})",
        )


def check_mcp(doctor: Doctor, dotenv: dict[str, str]) -> None:
    port = env_int("BACKEND_PORT", dotenv, 8000)
    configured: list[str] = []
    status, body = http_json(f"http://127.0.0.1:{port}/api/v1/mcp/servers")
    if status == 200 and isinstance(body, dict):
        configured = [s.get("name", "?") for s in body.get("servers", []) if isinstance(s, dict)]
    builtin = sorted(p.name for p in (ROOT / "mcp_servers").glob("*_mcp_server.py")) if (ROOT / "mcp_servers").is_dir() else []
    plugin = sorted(p.name for p in (ROOT / "plugins").glob("*/*_mcp_server.py")) if (ROOT / "plugins").is_dir() else []
    try:
        result = run(["ps", "-axo", "args="], timeout=5)
        processes = [
            Path(line.split()[0]).name
            for line in result.stdout.splitlines()
            if "_mcp_server.py" in line and str(ROOT) in line
        ]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        processes = []
    detail = f"{len(configured)} configured, {len(builtin) + len(plugin)} definitions"
    if processes:
        detail += f", {len(processes)} running"
    doctor.add(
        "mcp servers",
        "warn" if not processes else "ok",
        detail,
        None if processes else "MCP stdio servers spawn on first use; run a turn to start them",
        data={"configured": configured, "builtin": builtin, "plugin": plugin, "running": processes},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Conductor pre-flight doctor")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("--no-network", action="store_true", help="only check local files/ports/processes")
    args = parser.parse_args()

    dotenv = load_dotenv(ENV_FILE)
    doctor = Doctor()
    check_env(doctor, dotenv)
    check_docker(doctor)
    if not args.no_network:
        check_postgres(doctor, dotenv)
        check_redis(doctor, dotenv)
        backend_up, _ = check_ports(doctor, dotenv)
    else:
        backend_up = port_listening("127.0.0.1", env_int("BACKEND_PORT", dotenv, 8000))
        doctor.add("ports", "skip", "network checks disabled")
    if not args.no_network:
        healthy = check_backend(doctor, dotenv, backend_up)
        if healthy:
            check_llm_providers(doctor, dotenv)
            check_native_providers(doctor, dotenv)
            check_mcp(doctor, dotenv)
            check_desktop(doctor)
        else:
            for name in ("llm providers", "native providers", "mcp servers"):
                doctor.add(name, "skip", "backend unavailable")

    if args.json:
        payload = {
            "repo": str(ROOT),
            "ready": not doctor.failed and not (args.strict and any(c.status == "warn" for c in doctor.checks)),
            "checks": [asdict(c) for c in doctor.checks],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(doctor.render(color=sys.stdout.isatty(), strict=args.strict))

    failed = doctor.failed or (args.strict and any(c.status == "warn" for c in doctor.checks))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
