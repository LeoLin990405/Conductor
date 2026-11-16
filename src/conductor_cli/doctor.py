"""Local doctor: web port, daemon processes, sandbox policy files, orphaned workers, schema drift."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

from .build_info import build_info, is_sha
from .client import sanitize_terminal
from .ops_fleet import load_fleet
from .ops_runtime import MARKER, Component, _owned, _read_pid, pid_path, prepare
from .ops_support import OpsError, RealSystem, Settings


def _file_check(path: Path, label: str) -> dict[str, object]:
    if path.name == ".zshenv" or ".zshenv" in path.parts:
        return {"name": label, "ok": False, "detail": "refusing zshenv", "path": str(path)}
    if not path.exists():
        return {"name": label, "ok": False, "detail": "missing", "path": str(path)}
    if path.is_symlink() or not path.is_file():
        return {"name": label, "ok": False, "detail": "not a regular file", "path": str(path)}
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return {"name": label, "ok": False, "detail": "mode is too open", "path": str(path)}
    return {"name": label, "ok": True, "detail": "ok", "path": str(path)}


def _grant_verifier_check(settings: Settings) -> dict[str, object]:
    """A daemon policy without a buildable grant verifier refuses every Ticket.

    The verifier is built from the trusted server keys. When a policy file is
    present those keys must load, or doctor FAILs before the daemon starts
    refusing every claim.
    """
    policy = settings.daemon_policy
    check: dict[str, object] = {
        "name": "grant_verifier",
        "path": str(settings.trusted_keys),
    }
    if not policy.exists():
        return {**check, "ok": True, "detail": "no daemon policy"}
    from .daemon_capability import grant_verifier_mode
    from .daemon_grant import GrantError, TrustedKeys

    from .ops_runtime import daemon_rollout_env

    # N58: only an enforcing verifier refuses Tickets, so only then is a
    # verifier that cannot be built a FAIL; shadow and off say so and pass.
    # The mode is the daemons' (what ``cond`` passes them), not this shell's.
    mode = grant_verifier_mode(daemon_rollout_env(settings))
    check["mode"] = mode
    keys_path = settings.trusted_keys
    problem = ""
    if not keys_path.exists():
        problem = "daemon policy is present but no grant verifier can be built"
    else:
        try:
            TrustedKeys.load(keys_path)
        except (GrantError, UnicodeError):
            # Non-UTF-8 key files raise UnicodeDecodeError from the owned-file
            # reader before GrantError; treat them like any other unusable file.
            problem = "daemon policy is present but no grant verifier can be built"
    if not problem:
        return {**check, "ok": True, "detail": "ok"}
    if mode == "enforce":
        return {**check, "ok": False, "detail": problem}
    return {
        **check,
        "ok": True,
        "detail": f"{problem} (verifier mode {mode}: Tickets are not refused)",
    }


# Path substring that marks a snapshot job as still pointing at the operator's
# primary checkout rather than this deployment. Empty by default so the
# repository carries no personal workspace name; set
# CONDUCTOR_MAIN_CLONE_MARKER to activate the check.
_MAIN_CLONE = ""


def _main_clone_marker() -> str:
    return os.environ.get("CONDUCTOR_MAIN_CLONE_MARKER", _MAIN_CLONE).strip()


def _codexbar_snapshot_check(settings: Settings) -> dict[str, object]:
    """The snapshot job has to run from this checkout, not the main clone."""
    path = settings.launchd_dir / "com.conductor.codexbar-snapshot.plist"
    expected_python = str(settings.python)
    expected_script = str(settings.repo / "scripts" / "codexbar_snapshot.py")
    if path.is_symlink() or not path.is_file():
        return {
            "name": "codexbar_snapshot",
            "ok": False,
            "detail": "missing",
            "path": str(path),
        }
    try:
        payload = plistlib.loads(path.read_bytes())
        argv = payload.get("ProgramArguments") if isinstance(payload, dict) else None
    except (OSError, plistlib.InvalidFileException, ExpatError, ValueError):
        # A truncated XML plist raises ExpatError, not InvalidFileException.
        argv = None
    if not isinstance(argv, list) or len(argv) < 2:
        return {
            "name": "codexbar_snapshot",
            "ok": False,
            "detail": "unreadable",
            "path": str(path),
        }
    python = str(argv[0])
    script = str(argv[1])
    marker = _main_clone_marker()
    if marker and (marker in python or marker in script):
        return {
            "name": "codexbar_snapshot",
            "ok": False,
            "detail": "main clone",
            "path": str(path),
            "python": python,
        }
    if python != expected_python or script != expected_script:
        return {
            "name": "codexbar_snapshot",
            "ok": False,
            "detail": "not the deployment checkout",
            "path": str(path),
            "python": python,
        }
    return {
        "name": "codexbar_snapshot",
        "ok": True,
        "detail": "deployment",
        "path": str(path),
    }


def _codexbar_rules_check(settings: Settings) -> dict[str, object]:
    """Are the CodexBar parsing rules still valid for the CodexBar that ran?

    Reads the latest snapshot (names and versions only). A CodexBar release other
    than the validated one, or output keys the rules do not know, means the
    upstream format may have changed: re-validate src/conductor_cli/codexbar_snapshot.py
    and refresh tests/fixtures/codexbar/ (docs/CODEXBAR.md).
    """
    from .codexbar_snapshot import VALIDATED_CODEXBAR_VERSION

    path = settings.conductor_home / "usage" / "codexbar-snapshot.json"
    check: dict[str, object] = {"name": "codexbar_rules", "path": str(path)}
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
            return {**check, "ok": False, "detail": "snapshot missing"}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {**check, "ok": False, "detail": "snapshot unreadable"}
    if not isinstance(payload, dict):
        return {**check, "ok": False, "detail": "snapshot unreadable"}
    running = payload.get("codexbar_version")
    if not isinstance(running, str) or not running:
        return {**check, "ok": False, "detail": "codexbar version unknown (old snapshot writer)"}
    drift: dict[str, list[str]] = {}
    rows = payload.get("providers")
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("schema_drift"), list):
            names = [str(name) for name in row["schema_drift"][:20]]
            if names:
                drift[str(row.get("provider"))[:40]] = names
    if running != VALIDATED_CODEXBAR_VERSION:
        return {
            **check,
            "ok": False,
            "detail": f"CodexBar {running} differs from validated {VALIDATED_CODEXBAR_VERSION}",
            "drift": drift,
        }
    if drift:
        return {**check, "ok": False, "detail": "unknown CodexBar output keys", "drift": drift}
    return {**check, "ok": True, "detail": f"CodexBar {running} matches the validated rules"}


# The default base every daemon Ticket without its own base is created from
# (src/server/services/autonomy/ticket_worktree.resolve_base_sha): origin/main,
# falling back to main.
_DEFAULT_BASE_REFS = ("origin/main", "main")
_TICKET_BASE_GIT_TIMEOUT = 10.0

# Where the running server says which tree it answers from.
_HEALTH_PATH = "/health"
_HEALTH_TIMEOUT = 5.0
_UNKNOWN_SHA = "unknown"


def _server_sha(system: RealSystem, settings: Settings) -> str:
    """The tree SHA the running server reports on ``/health``.

    Empty when the server cannot say: it is down, it answers with
    something else, it is too old to report a tree, or it reports
    something that is not a tree SHA at all.

    The status gates readiness, not identity: /health answers 503 when
    MCP discovery failed, and that body names the same tree a healthy
    answer names. A mismatch hidden behind a 503 is a mismatch the
    operator cannot see, so the body is read whatever the status is.

    The answer crosses a process boundary and is about to be printed, so
    it is validated before it is believed: a server that answers with
    escape sequences, a newline or any other byte must not put them in
    the operator's terminal.
    """
    try:
        _status, body = system.http_get(
            f"{settings.base_url}{_HEALTH_PATH}", _HEALTH_TIMEOUT
        )
    except (OpsError, OSError):
        # The server could not be reached at all. A doctor that cannot
        # answer the question is no use, so it says "cannot say" instead
        # of raising: an unreadable identity is not a mismatch either.
        return ""
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    sha = payload.get("sha")
    if not isinstance(sha, str):
        return ""
    sha = sha.strip()
    return sha if is_sha(sha) else ""


def _build_identity_check(system: RealSystem, settings: Settings) -> dict[str, object]:
    """The checkout and the running server must be the same tree.

    A server started yesterday answers with the tree it was started from
    while the checkout has moved on: the operator then edits code the server
    does not run. A mismatch is a warning, not a failure -- an operator may
    deliberately run another tree -- but it has to be visible.
    """
    checkout = build_info(settings.repo)
    server = _server_sha(system, settings)
    check: dict[str, object] = {
        "name": "build_identity",
        "ok": True,
        "checkout_sha": checkout.sha,
        "server_sha": server or _UNKNOWN_SHA,
        "checkout_dirty": checkout.dirty,
    }
    checkout_state = f"{checkout.sha} (dirty)" if checkout.dirty else checkout.sha
    if not server or server == _UNKNOWN_SHA:
        # A server that cannot name its tree is not a mismatch: it says
        # nothing about the checkout, and a warning here would cry wolf on
        # every deployment that ships without a checkout.
        check["detail"] = f"checkout {checkout_state}; the server reports no tree"
        return check
    if checkout.sha != _UNKNOWN_SHA and server != checkout.sha:
        check["warn"] = True
        check["detail"] = (
            f"checkout {checkout_state} server {server}; "
            "the server is not running this checkout"
        )
        return check
    check["detail"] = f"checkout {checkout_state} server {server}"
    return check


def _ticket_base_check(dispatch_repo: Path | None) -> dict[str, object]:
    """FAIL when the dispatch repository has no default Ticket base.

    The server resolves every daemon Ticket's base from
    ``CONDUCTOR_DISPATCH_REPO``. Without ``origin/main`` or ``main`` there,
    each such Ticket stays queued (a server-wide outage, never the Ticket's
    fault), so the operator sees it here rather than only in the server log.
    """
    name = "ticket_base"
    if dispatch_repo is None:
        return {"name": name, "ok": True, "detail": "no dispatch repository configured"}
    repo = Path(dispatch_repo).expanduser()
    if not (repo / ".git").exists():
        return {
            "name": name,
            "ok": False,
            "detail": f"dispatch repository {repo} is not a git checkout",
        }
    # conductor_cli ships without ``src``, so the server's shared hardened Git
    # builder is not importable here. ``rev-parse`` reads refs only (no
    # checkout, no filter), so this minimal copy is enough: scrubbed
    # environment, no system/global config, no fsmonitor, no hooks.
    git = shutil.which("git", path=os.defpath)
    if git is None:
        return {
            "name": name,
            "ok": False,
            "detail": f"could not check the default base in {repo} (git unavailable)",
        }
    env = {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for ref in _DEFAULT_BASE_REFS:
        try:
            result = subprocess.run(
                [
                    git,
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"{ref}^{{commit}}",
                ],
                cwd=str(repo),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_TICKET_BASE_GIT_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {
                "name": name,
                "ok": False,
                "detail": f"could not check the default base in {repo} (git did not run)",
            }
        if result.returncode == 0 and result.stdout.strip():
            return {"name": name, "ok": True, "detail": f"{ref} in {repo}"}
    return {
        "name": name,
        "ok": False,
        "detail": (
            f"dispatch repository {repo} has neither origin/main nor main; daemon "
            "Tickets without their own base stay queued until one exists"
        ),
    }


def doctor_report(
    system: RealSystem,
    settings: Settings,
    *,
    schema_connect: Callable[[str], Any] | None = None,
) -> dict[str, object]:
    fleet = load_fleet(settings)
    checks: list[dict[str, object]] = []
    web_open = system.port_open("127.0.0.1", settings.web_port)
    checks.append(
        {
            "name": "web_port",
            "ok": web_open,
            "detail": "open" if web_open else "closed",
            "port": settings.web_port,
        }
    )
    # The tree the server answers from, next to the tree of this checkout.
    checks.append(_build_identity_check(system, settings))
    if not fleet.daemons:
        checks.append({"name": "daemons", "ok": True, "detail": "none configured"})
    for daemon in fleet.daemons:
        component = Component("daemon", daemon.provider, daemon.ident)
        pid = _read_pid(pid_path(settings, component))
        running = pid is not None and _owned(system, pid, settings, MARKER["daemon"])
        checks.append(
            {
                "name": "daemon",
                "ok": running,
                "provider": daemon.provider,
                "id": daemon.ident,
                "detail": "running" if running else "down",
            }
        )
    # The device policy is optional (it stays parked until promoted), so a
    # missing one is not a failure; a present one must still be sound.
    if settings.daemon_policy.exists() or settings.daemon_policy.is_symlink():
        checks.append(_file_check(settings.daemon_policy, "daemon_policy"))
    else:
        checks.append(
            {
                "name": "daemon_policy",
                "ok": True,
                "detail": "absent (optional)",
                "path": str(settings.daemon_policy),
            }
        )
    for label, path in (
        ("trusted_keys", settings.trusted_keys),
        ("workspace_policy", settings.workspace_policy),
    ):
        checks.append(_file_check(path, label))
    checks.append(_grant_verifier_check(settings))
    checks.append(_codexbar_snapshot_check(settings))
    checks.append(_codexbar_rules_check(settings))
    checks.append(_ticket_base_check(settings.dispatch_repo))
    from .ops_dispatch import orphan_check

    # A warning only: ok stays true, and nothing is signalled.
    checks.append(orphan_check(system, settings))
    from .ops_cli_update import latest_results, persisted_health

    # Current health and the last attempt stay side by side. A skip, pin, or
    # dry-run must not hide a broken CLI, and a broken CLI must not hide the skip.
    health = persisted_health(settings)
    latest: dict[str, dict[str, object]] = {}
    for row in latest_results(settings):
        name = row.get("name")
        if isinstance(name, str):
            latest[name] = row
    for name in dict.fromkeys([*health, *latest]):
        state = health.get(name, "")
        attempt = str(latest.get(name, {}).get("result") or "unchecked")
        if state != "broken" and attempt != "broken":
            continue
        checks.append(
            {
                "name": "cli",
                "ok": False,
                "detail": "broken",
                "cli": name,
                "health": state or "unchecked",
                "attempt": attempt,
            }
        )
    from .schema_drift import schema_checks

    # The connector comes from the system so a fake system never reaches a
    # database. Without one the check is a WARN line, not a live call.
    connect = schema_connect or getattr(system, "connect_database", None)
    checks.extend(schema_checks(settings.repo, connect, sensitive=settings.sensitive))
    ok = all(bool(item.get("ok")) for item in checks)
    return {"ok": ok, "checks": checks}


def _render_line(text: str) -> str:
    """One line that is safe to print.

    Doctor prints text that came from other processes -- a server answer,
    a process command line, a path -- and a terminal renders whatever it
    is given. Control characters in that text would move the cursor,
    clear the screen or fake another line, so every rendered line goes
    through the same scrubber the rest of the CLI uses, and a line
    break in the text cannot become a line of its own.
    """
    return sanitize_terminal(str(text)).replace("\r", " ").replace("\n", " ")


def format_doctor(report: dict[str, object]) -> str:
    lines = []
    checks = report.get("checks")
    if isinstance(checks, list):
        for item in checks:
            if not isinstance(item, dict):
                continue
            state = "ok" if item.get("ok") else "FAIL"
            if item.get("ok") and item.get("warn"):
                state = "WARN"
            name = str(item.get("name"))
            extra = ""
            if item.get("provider"):
                extra = f" {item.get('provider')}/{item.get('id')}"
            if item.get("cli"):
                extra = f" {item.get('cli')}"
                if item.get("health") or item.get("attempt"):
                    extra += f" health={item.get('health')} attempt={item.get('attempt')}"
            if item.get("port"):
                extra = f" {item.get('port')}"
            lines.append(f"{state}: {name}{extra} ({item.get('detail')})")
            processes = item.get("processes")
            if item.get("warn") and isinstance(processes, list):
                for proc in processes:
                    if isinstance(proc, dict):
                        lines.append(
                            f"  pid={proc.get('pid')} age={proc.get('age')} "
                            f"kind={proc.get('kind')} cwd={proc.get('cwd') or 'unknown'}"
                        )
    lines.append("doctor: " + ("ok" if report.get("ok") else "failed"))
    return "\n".join(_render_line(line) for line in lines) + "\n"


def run_doctor(system: RealSystem | None = None, *, json_mode: bool = False) -> tuple[int, str]:
    try:
        system, settings, _fleet = prepare(system)
        report = doctor_report(system, settings)
    except OpsError as exc:
        report = {"ok": False, "checks": [{"name": "doctor", "ok": False, "detail": str(exc)}]}
    text = json.dumps(report, indent=2) + "\n" if json_mode else format_doctor(report)
    return (0 if report.get("ok") else 1), text


def register_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    doctor = commands.add_parser("doctor", help="check the web port, daemons, sandbox policy files, orphaned workers, and schema drift")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=_handler)


def _handler(args: argparse.Namespace) -> int:
    code, text = run_doctor(json_mode=bool(args.json))
    print(text, end="")
    return code
