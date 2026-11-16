"""Native MCP bridge for Conductor's shipped/builtin servers.

Only secret-less servers are bridged: stdio with no env, or HTTP with no
headers/OAuth binding. Builtin entry points are resolved to absolute paths;
HTTP/OAuth servers that carry credentials remain outside this bridge because
their secrets must not ride argv or a generated profile file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping
from typing import Any

from src.config.runtime.core import MCPServerConfig
from src.config.runtime.plugins import BUNDLES_DIR
from src.server.services.agent_config_state import get_agent_config
from src.server.services.mcp_config import Origin, State, resolve_mcp_config

REPO_ROOT = BUNDLES_DIR.parent
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class NativeMCPServer:
    name: str
    command: str = ""
    args: tuple[str, ...] = ()
    url: str | None = None
    env: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "transport": self.transport}
        if self.url:
            out["url"] = self.url
            if self.headers:
                out["headers"] = dict(self.headers)
        else:
            out["command"] = self.command
            out["args"] = list(self.args)
            if self.env:
                out["env"] = dict(self.env)
        return out


def _absolute_builtin_args(config: MCPServerConfig) -> tuple[str, ...]:
    """Make a bundled server's relative script path independent of native cwd."""
    args = [str(arg) for arg in (config.args or ())]
    if config.command == "uv" and len(args) >= 3 and args[:2] == ["run", "python"]:
        return (
            "run",
            "--directory",
            str(REPO_ROOT),
            "python",
            str(REPO_ROOT / args[2]),
            *args[3:],
        )
    normalized: list[str] = []
    for arg in args:
        if not arg.startswith(("/", "-")) and arg.startswith("plugins/"):
            normalized.append(str(REPO_ROOT / arg))
        else:
            normalized.append(arg)
    return tuple(normalized)


def _bridgeable(
    config: MCPServerConfig, *, origin: Origin, oauth_bound: bool
) -> bool:
    if _BARE_KEY.match(config.name) is None:
        return False
    if config.transport == "http":
        return bool(config.url) and not config.command and not config.args
    if oauth_bound:
        return False
    if config.transport != "stdio" or not config.command:
        return False
    if config.env or config.headers:
        return False
    # User commands are already host-absolute or on PATH; only the shipped
    # builtin tree uses repository-relative entrypoints.
    return origin is Origin.BUILTIN or origin is Origin.USER


_VAULT_REF = re.compile(r"\$\{vault:([^}]+)\}")


async def _resolved_headers(
    user_id: str, headers: Mapping[str, str] | None
) -> dict[str, str] | None:
    """Resolve ``${vault:NAME}`` header values; None if any ref is missing."""
    if not headers:
        return {}
    names = {m.group(1) for value in headers.values() for m in _VAULT_REF.finditer(str(value))}
    secrets: dict[str, str] = {}
    if names:
        from src.server.database.user_vault_secrets import get_user_secrets_decrypted

        secrets = await get_user_secrets_decrypted(user_id, names)
    out: dict[str, str] = {}
    for key, value in headers.items():
        text = str(value)
        missing: set[str] = set()

        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in secrets:
                missing.add(name)
                return ""
            return secrets[name]

        resolved = _VAULT_REF.sub(repl, text)
        if missing or "${vault:" in resolved:
            return None
        out[str(key)] = resolved
    return out


async def collect_builtin_mcp_servers(
    *, user_id: str, workspace_id: str
) -> list[NativeMCPServer]:
    """The workspace's active builtin stdio servers Codex can safely launch."""
    agent_config = get_agent_config()
    if agent_config is None:
        return []
    try:
        resolved = await resolve_mcp_config(agent_config, user_id, workspace_id)
    except Exception:
        # MCP configuration is additive; a resolve fault must not prevent the
        # native turn. The caller logs and proceeds without the bridge.
        return []
    out: list[NativeMCPServer] = []
    for entry in resolved.entries:
        if entry.state is not State.ACTIVE:
            continue
        if entry.origin not in (Origin.BUILTIN, Origin.USER):
            continue
        config = entry.config
        if not _bridgeable(
            config,
            origin=entry.origin,
            oauth_bound=bool(config.oauth_connection_id) or entry.host_side_oauth,
        ):
            continue
        if config.transport == "http":
            headers = await _resolved_headers(user_id, config.headers)
            if headers is None:
                continue
            if config.oauth_connection_id:
                try:
                    from src.server.services.mcp_oauth.lifecycle import (
                        ensure_fresh_access_token,
                    )

                    token = await ensure_fresh_access_token(config.oauth_connection_id)
                    headers["Authorization"] = token.authorization_header
                except Exception:
                    # An OAuth server that cannot refresh is not usable by the
                    # CLI; leave it out instead of sending a stale placeholder.
                    continue
            out.append(
                NativeMCPServer(
                    name=config.name,
                    url=str(config.url),
                    headers=tuple(headers.items()),
                )
            )
            continue
        args = (
            _absolute_builtin_args(config)
            if entry.origin is Origin.BUILTIN
            else tuple(str(arg) for arg in (config.args or ()))
        )
        out.append(
            NativeMCPServer(
                name=config.name,
                command=str(config.command),
                args=args,
            )
        )
    return out


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )
    return f'"{escaped}"'


def codex_mcp_overrides(servers: list[NativeMCPServer]) -> list[str]:
    """One ``-c key=TOML`` override per server field for ``codex exec``.

    ``codex`` parses each value as TOML; arrays and quoted strings are its
    documented override grammar. Names are checked against a bare-key regex
    before this function is called.
    """
    overrides: list[str] = []
    for server in servers:
        base = f"mcp_servers.{server.name}"
        if server.url:
            overrides.append(f"{base}.url={_toml_string(server.url)}")
            continue
        overrides.append(f"{base}.command={_toml_string(server.command)}")
        args = ", ".join(_toml_string(arg) for arg in server.args)
        overrides.append(f"{base}.args=[{args}]")
    return overrides
