"""``conductor mcp`` — central MCP registry for the agent fleet.

Reads and writes stay under one HOME: ``--home <dir>`` when given (to rehearse
on a copy or a worker HOME), else the process home. Env values
are never accepted on the command line and never printed: ``--env KEY`` is
stored as ``${KEY}``. Apply-style writes are dry-runs until ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from mcp_hub import (
    McpHubError,
    Server,
    apply_all,
    check_server,
    diff_all,
    import_existing,
    list_status,
    load_registry,
    recover,
    rollback,
    save_registry,
)
from mcp_hub.check import probe_local_server
from mcp_hub.drift import drift_report
from mcp_hub.agents import known_agents

from .client import CLIError
from .output import build_envelope, command_label, envelope_json

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_REF = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_KEYCHAIN_REF = re.compile(r"^keychain:[^/\s]+/[^/\s]+$")
_TRANSPORTS = ("stdio", "http", "sse")
_CHECK_FIELDS = ("name", "status", "tool_count", "elapsed_ms", "reason")


class _Usage(Exception):
    """Validation failure. The message must not contain a secret value."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def register_parser(subparsers: argparse._SubParsersAction) -> None:
    mcp = subparsers.add_parser("mcp", help="manage the central MCP server registry")
    mcp.set_defaults(handler=_handle, local=True)
    commands = mcp.add_subparsers(dest="mcp_command", required=True)

    listing = commands.add_parser("list", help="list servers in the registry")
    _json_option(listing)

    status = commands.add_parser(
        "status", help="show registry, ownership, and agent files"
    )
    _json_option(status)

    importing = commands.add_parser(
        "import",
        help="import servers from agent configs (dry-run unless --apply)",
    )
    importing.add_argument(
        "--apply",
        action="store_true",
        help="write the registry (default is a dry-run)",
    )
    _json_option(importing)

    add = commands.add_parser(
        "add",
        help="add one server (dry-run unless --apply)",
    )
    add.add_argument("--name", help="server name")
    add.add_argument("--transport", help="stdio, http, or sse (default: stdio)")
    # dest must not be "command": that is the top-level subparser destination,
    # and a default of None would wipe it and fall through to the REPL.
    add.add_argument("--command", dest="server_command", help="stdio command")
    add.add_argument("--arg", action="append", default=None, help="command argument")
    add.add_argument("--url", help="http or sse url")
    add.add_argument(
        "--env",
        action="append",
        default=None,
        help="env name stored as ${NAME}; KEY=value is rejected",
    )
    add.add_argument(
        "--target",
        action="append",
        default=None,
        help="agent id (repeatable; default: all)",
    )
    add.add_argument("--apply", action="store_true", help="write the registry")
    _json_option(add)

    remove = commands.add_parser(
        "remove",
        help=(
            "remove one server; writes the registry only "
            "(dry-run unless --apply); run 'mcp apply --apply' to release the "
            "owned agent-config entries"
        ),
    )
    remove.add_argument("name", nargs="?", help="server name")
    remove.add_argument("--apply", action="store_true", help="write the registry")
    _json_option(remove)

    enable = commands.add_parser(
        "enable", help="enable one server (dry-run unless --apply)"
    )
    enable.add_argument("name", nargs="?", help="server name")
    enable.add_argument("--apply", action="store_true", help="write the registry")
    _json_option(enable)

    disable = commands.add_parser(
        "disable",
        help=(
            "disable one server; writes the registry only "
            "(dry-run unless --apply); run 'mcp apply --apply' to release the "
            "owned agent-config entries"
        ),
    )
    disable.add_argument("name", nargs="?", help="server name")
    disable.add_argument("--apply", action="store_true", help="write the registry")
    _json_option(disable)

    diff = commands.add_parser("diff", help="show the per-agent diff without writing")
    _json_option(diff)

    apply_cmd = commands.add_parser(
        "apply",
        help=(
            "show the per-agent diff, or write it with --apply; "
            "also deletes or disables owned keys the registry no longer wants"
        ),
    )
    apply_cmd.add_argument(
        "--apply",
        action="store_true",
        help="write agent configs (default is a dry-run)",
    )
    _json_option(apply_cmd)

    rolling = commands.add_parser(
        "rollback",
        help="preview an apply snapshot, or restore it with --apply",
    )
    rolling.add_argument("snapshot_id", nargs="?", help="snapshot id (default: newest)")
    rolling.add_argument(
        "--apply",
        action="store_true",
        help="restore the snapshot (default is a preview)",
    )
    _json_option(rolling)

    recovering = commands.add_parser(
        "recover",
        help="finish registration for targets left published but unregistered",
    )
    _json_option(recovering)

    check = commands.add_parser(
        "check", help="report tool count and elapsed for one server"
    )
    check.add_argument("name", nargs="?", help="server name")
    _json_option(check)

    drifting = commands.add_parser(
        "drift",
        help=(
            "read-only: per agent, catalog servers missing/different/extra, "
            "fragile (draft/tmp) commands, and secret-looking literals (key paths only)"
        ),
    )
    _json_option(drifting)


def _handle(args: argparse.Namespace) -> dict[str, Any] | int:
    try:
        home = resolve_home(getattr(args, "hub_home", None))
    except _Usage as exc:
        return _usage_exit(args, exc.message)
    try:
        return _dispatch(args, home)
    except _Usage as exc:
        return _usage_exit(args, exc.message)
    except McpHubError as exc:
        message = _redact_text(str(exc), _literal_env_values(home))
        raise CLIError(message) from exc


def _dispatch(args: argparse.Namespace, home: Path) -> dict[str, Any]:
    command = args.mcp_command
    if command == "list":
        return _present(_list(home), home=home)
    if command == "status":
        return _present(list_status(home), home=home)
    if command == "import":
        return _present(_import(home, apply=args.apply), home=home)
    if command == "add":
        return _present(_add(home, args), home=home)
    if command == "remove":
        return _present(
            _set_removed(home, _require_name(args, "remove"), args.apply), home=home
        )
    if command == "enable":
        return _present(
            _set_enabled(home, _require_name(args, "enable"), True, args.apply),
            home=home,
        )
    if command == "disable":
        return _present(
            _set_enabled(home, _require_name(args, "disable"), False, args.apply),
            home=home,
        )
    if command == "diff":
        return _present(diff_all(home), home=home)
    if command == "apply":
        # Literals have to be captured before the write. Afterwards the file
        # no longer has the old secret, and the diff would print it.
        secrets = _literal_env_values(home)
        return _present(apply_all(home, apply=args.apply), secrets=secrets, home=home)
    if command == "rollback":
        return _present(rollback(home, args.snapshot_id, apply=args.apply), home=home)
    if command == "recover":
        return _present(recover(home), home=home)
    if command == "check":
        return _check(home, _require_name(args, "check"))
    if command == "drift":
        return _present(drift_report(home), home=home)
    raise CLIError(f"unknown mcp command {command!r}")


def _json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument(
        "--home",
        dest="hub_home",
        default=None,
        help=(
            "HOME whose agent configs and ~/.conductor/mcp are used "
            "(default: the real HOME); rehearse on a copy with this"
        ),
    )


def resolve_home(raw: str | None) -> Path:
    """``--home`` as an absolute, existing directory, or the process HOME."""
    if raw is None or not str(raw).strip():
        return Path.home()
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_dir():
        raise _Usage("--home must be an existing directory")
    return path


def _usage_exit(args: argparse.Namespace, message: str) -> int:
    envelope = build_envelope(
        command_label(args),
        data=None,
        errors=[{"code": "usage", "message": message}],
        exit_code=2,
    )
    print(envelope_json(envelope), file=sys.stderr)
    return 2


def _require_name(args: argparse.Namespace, command: str) -> str:
    name = (getattr(args, "name", None) or "").strip()
    if not name:
        raise _Usage(f"{command} requires a server name")
    return name


def _list(home: Path) -> dict[str, Any]:
    registry = load_registry(home)
    servers = [registry.servers[name].to_dict() for name in sorted(registry.servers)]
    return {"servers": servers}


def _add(home: Path, args: argparse.Namespace) -> dict[str, Any]:
    name = (args.name or "").strip()
    if not name:
        raise _Usage("add requires --name")
    transport = (args.transport or "stdio").strip()
    if transport not in _TRANSPORTS:
        raise _Usage("transport must be stdio, http, or sse")
    targets = args.target or None
    if targets is not None:
        known = set(known_agents())
        unknown = [agent for agent in targets if agent not in known]
        if unknown:
            raise _Usage(f"unknown agent {unknown[0]!r}")
    server = Server(
        name=name,
        transport=transport,
        command=args.server_command or "",
        args=tuple(args.arg or ()),
        url=args.url or "",
        env=_env_map(args.env or []),
        targets=tuple(targets) if targets else "all",
    )
    registry = load_registry(home)
    if server.name in registry.servers:
        raise McpHubError(f"server {server.name!r} already exists")
    body: dict[str, Any] = {"dry_run": not args.apply, "server": server.to_dict()}
    if not args.apply:
        return body
    registry.servers[server.name] = server
    save_registry(home, registry)
    return body


def _env_map(items: list[str]) -> dict[str, str]:
    """Store ``--env KEY`` as ``${KEY}``. ``KEY=value`` is a usage error."""
    env: dict[str, str] = {}
    for item in items:
        if "=" in item:
            raise _Usage("secrets must be references; pass --env KEY, not KEY=value")
        if not _ENV_KEY.fullmatch(item):
            raise _Usage("env name must be an identifier")
        if item in env:
            raise _Usage(f"duplicate env name {item}")
        env[item] = "${" + item + "}"
    return env


def _set_removed(home: Path, name: str, apply: bool) -> dict[str, Any]:
    registry = load_registry(home)
    if name not in registry.servers:
        raise McpHubError(f"unknown server {name!r}")
    if not apply:
        return {"dry_run": True, "removed": name}
    del registry.servers[name]
    save_registry(home, registry)
    return {"dry_run": False, "removed": name}


def _set_enabled(home: Path, name: str, enabled: bool, apply: bool) -> dict[str, Any]:
    registry = load_registry(home)
    server = registry.servers.get(name)
    if server is None:
        raise McpHubError(f"unknown server {name!r}")
    body = {"dry_run": not apply, "name": name, "enabled": enabled}
    if not apply:
        return body
    server.enabled = enabled
    save_registry(home, registry)
    return body


def _import(home: Path, *, apply: bool) -> dict[str, Any]:
    # A dry-run runs the same merge with every write off: no registry, no
    # journal event, no sidecar, no hub directory.
    imported = import_existing(home, write=apply)
    return {"dry_run": not apply, "imported": imported}


def _check(home: Path, name: str) -> dict[str, Any]:
    result = check_server(home, name, runner=probe_local_server)
    public = {key: result[key] for key in _CHECK_FIELDS if key in result}
    count = public.get("tool_count")
    elapsed = public.get("elapsed_ms")
    public["lines"] = [f"{name}: tool_count={count} elapsed_ms={elapsed}"]
    return _present(public, home=home)


def _present(
    payload: Any, secrets: list[str] | None = None, *, home: Path
) -> Any:
    found: list[str] = []
    if secrets:
        found.extend(secrets)
    found.extend(_literal_env_values(home))
    return _scrub(payload, list(dict.fromkeys(found)))


def _is_reference(value: object) -> bool:
    return isinstance(value, str) and bool(
        _ENV_REF.fullmatch(value) or _KEYCHAIN_REF.fullmatch(value)
    )


def _literal_env_values(home: Path) -> list[str]:
    """Plaintext env values already on disk, so a diff cannot echo them."""
    from mcp_hub.agents import agent_path
    from mcp_hub.render import generic_servers

    found: list[str] = []
    for agent in known_agents():
        path = agent_path(home, agent)
        try:
            if not path.is_file() or path.is_symlink():
                continue
            servers = generic_servers(agent, path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, McpHubError):
            continue
        for body in servers.values():
            env = body.get("env") if isinstance(body, dict) else None
            if not isinstance(env, dict):
                continue
            for value in env.values():
                if isinstance(value, str) and value and not _is_reference(value):
                    found.append(value)
    return found


def _scrub(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, dict):
        scrubbed: dict[str, Any] = {}
        for key, item in value.items():
            if key == "env" and isinstance(item, dict):
                scrubbed[key] = {
                    str(env_key): (
                        env_value if _is_reference(env_value) else "[redacted]"
                    )
                    for env_key, env_value in item.items()
                }
            else:
                scrubbed[key] = _scrub(item, secrets)
        return scrubbed
    if isinstance(value, list):
        return [_scrub(item, secrets) for item in value]
    if isinstance(value, str):
        return _redact_text(value, secrets)
    return value


def _redact_text(text: str, secrets: list[str]) -> str:
    """Drop plaintext env values from text we are about to print.

    Quoted forms are always replaced. A raw substring replace is only done for
    longer values so a short token cannot punch holes in unrelated words.
    """
    redacted = text
    for secret in secrets:
        if not secret:
            continue
        quoted = json.dumps(secret)
        redacted = redacted.replace(quoted, json.dumps("[redacted]"))
        # TOML basic strings use single quotes. A short value must still
        # disappear from a diff; only the quoted form is replaced so the
        # token cannot punch a hole in an unrelated word.
        if "'" not in secret:
            redacted = redacted.replace(f"'{secret}'", "'[redacted]'")
        if len(secret) >= 8:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted
