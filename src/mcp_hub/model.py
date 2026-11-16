"""Registry records. Env values are references, never literals.

A server stored here can be rendered into an agent config. Plaintext env
values are rejected at construction so a later apply cannot leak them into
an agent file by accident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .errors import McpHubError

# Bare identifier. Dots, slashes, quotes and whitespace are rejected so a
# name cannot close a TOML table or a JSON string and start a new one.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VAR_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_KEYCHAIN_RE = re.compile(r"^keychain:([^/\s]+)/([^/\s]+)$")
_TRANSPORTS = frozenset({"stdio", "http", "sse"})
_OVERRIDE_KEYS = frozenset(
    {
        "command",
        "args",
        "url",
        "env",
        "transport",
        "enabled",
        "startup_timeout_sec",
        "name_in_file",
    }
)

AGENT_IDS: tuple[str, ...] = (
    "claude",
    "codex",
    "cursor",
    "kimi",
    "grok",
    "stepfun",
    "minimax",
    "zcode",
    "opencode",
    "dsh",
    "claude-desktop",
    "gemini",
    "antigravity",
    "qwen",
    "agents",
)
_AGENT_SET = frozenset(AGENT_IDS)


def require_name(name: object) -> str:
    """Reject names that could inject TOML/JSON or escape a path."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise McpHubError(
            "invalid server name: expected 1-64 letters, digits, '_' or '-'"
        )
    if name in {".", ".."} or name.startswith("."):
        raise McpHubError("invalid server name")
    return name


def require_env_key(key: object) -> str:
    if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key):
        raise McpHubError("invalid env name: expected an identifier")
    return key


def classify_env_value(value: object) -> str:
    """Return ``var``, ``keychain`` or ``plain``. Plain is never stored."""
    if not isinstance(value, str):
        return "plain"
    if _VAR_REF_RE.fullmatch(value):
        return "var"
    if _KEYCHAIN_RE.fullmatch(value):
        return "keychain"
    return "plain"


def require_env_map(env: object) -> dict[str, str]:
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise McpHubError("env must be a mapping of references")
    out: dict[str, str] = {}
    for key, value in env.items():
        require_env_key(key)
        kind = classify_env_value(value)
        if kind == "plain":
            raise McpHubError(
                f"env {key} must be ${{{key}}} or a keychain:service/account reference"
            )
        out[str(key)] = str(value)
    return out


def _single_line(value: object, field_name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise McpHubError(f"{field_name} must be a string")
    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise McpHubError(f"{field_name} contains a control character")
    return value


def _require_args(args: object) -> tuple[str, ...]:
    if args is None:
        return ()
    if isinstance(args, str) or not isinstance(args, (list, tuple)):
        raise McpHubError("args must be a list of strings")
    out: list[str] = []
    for item in args:
        out.append(_single_line(item, "arg"))
    return tuple(out)


def _require_targets(targets: object) -> str | tuple[str, ...]:
    if targets is None or targets == "all":
        return "all"
    if isinstance(targets, str):
        raise McpHubError("targets must be 'all' or a list of agent ids")
    if not isinstance(targets, (list, tuple)) or not targets:
        raise McpHubError("targets must be 'all' or a list of agent ids")
    seen: list[str] = []
    for agent in targets:
        if not isinstance(agent, str) or agent not in _AGENT_SET:
            raise McpHubError(f"unknown agent {agent!r}")
        if agent not in seen:
            seen.append(agent)
    return tuple(seen)


def _require_overrides(overrides: object) -> dict[str, dict[str, Any]]:
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise McpHubError("overrides must be a mapping of agent ids")
    out: dict[str, dict[str, Any]] = {}
    for agent, raw in overrides.items():
        if agent not in _AGENT_SET:
            raise McpHubError(f"unknown agent {agent!r} in overrides")
        if not isinstance(raw, dict):
            raise McpHubError(f"overrides for {agent} must be a mapping")
        cleaned: dict[str, Any] = {}
        for key, value in raw.items():
            if key not in _OVERRIDE_KEYS:
                raise McpHubError(f"unknown override {key!r}")
            if key == "env":
                cleaned[key] = require_env_map(value)
            elif key == "args":
                cleaned[key] = list(_require_args(value))
            elif key in {"command", "url", "name_in_file", "transport"}:
                text = _single_line(value, key)
                if key == "transport" and text not in _TRANSPORTS:
                    raise McpHubError("transport must be stdio, http or sse")
                if key == "name_in_file":
                    require_name(text)
                cleaned[key] = text
            elif key == "enabled":
                if not isinstance(value, bool):
                    raise McpHubError("enabled override must be a bool")
                cleaned[key] = value
            elif key == "startup_timeout_sec":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise McpHubError("startup_timeout_sec must be an int")
                if value < 0:
                    raise McpHubError("startup_timeout_sec must be >= 0")
                cleaned[key] = value
        out[str(agent)] = cleaned
    return out


def _require_url(url: str) -> str:
    text = _single_line(url, "url")
    if not text:
        return ""
    # user:password@host would land in the agent file as a credential.
    if "://" in text:
        rest = text.split("://", 1)[1]
        host = rest.split("/", 1)[0]
        if "@" in host:
            raise McpHubError("url must not embed credentials")
    return text


@dataclass
class Server:
    """One MCP server in the central registry.

    ``env`` values are ``${NAME}`` or ``keychain:service/account`` only.
    ``targets`` is ``"all"`` or a tuple of agent ids.
    """

    name: str
    transport: str = "stdio"
    command: str = ""
    args: tuple[str, ...] = ()
    url: str = ""
    env: dict[str, str] = field(default_factory=dict)
    targets: str | tuple[str, ...] = "all"
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    enabled: bool = True

    def __post_init__(self) -> None:
        self.name = require_name(self.name)
        if self.transport not in _TRANSPORTS:
            raise McpHubError("transport must be stdio, http or sse")
        self.command = _single_line(self.command, "command")
        self.args = _require_args(self.args)
        self.url = _require_url(self.url)
        self.env = require_env_map(self.env)
        self.targets = _require_targets(self.targets)
        self.overrides = _require_overrides(self.overrides)
        if not isinstance(self.enabled, bool):
            raise McpHubError("enabled must be a bool")
        if self.transport == "stdio" and not self.command and not self.url:
            raise McpHubError(f"server {self.name} needs a command or a url")
        if self.transport in {"http", "sse"} and not self.url:
            raise McpHubError(f"server {self.name} needs a url")

    def targets_agent(self, agent: str) -> bool:
        if self.targets == "all":
            return agent in _AGENT_SET
        return agent in self.targets

    def file_key(self, agent: str) -> str:
        override = self.overrides.get(agent) or {}
        key = override.get("name_in_file") or self.name
        return require_name(str(key))

    def effective(self, agent: str) -> dict[str, Any]:
        """Registry fields with this agent's overrides applied."""
        override = self.overrides.get(agent) or {}
        env = dict(self.env)
        extra_env = override.get("env")
        if isinstance(extra_env, dict):
            env.update(extra_env)
        args = override.get("args", self.args)
        return {
            "transport": override.get("transport", self.transport),
            "command": override.get("command", self.command),
            "args": list(args),
            "url": override.get("url", self.url),
            "env": env,
            "enabled": override.get("enabled", self.enabled),
            "startup_timeout_sec": override.get("startup_timeout_sec"),
        }

    def to_dict(self) -> dict[str, Any]:
        targets: str | list[str]
        if self.targets == "all":
            targets = "all"
        else:
            targets = list(self.targets)
        return {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "url": self.url,
            "env": dict(self.env),
            "targets": targets,
            "overrides": {k: dict(v) for k, v in self.overrides.items()},
            "enabled": self.enabled,
        }


@dataclass
class Registry:
    """The servers the hub wants on disk. Not a live view of agent files."""

    servers: dict[str, Server] = field(default_factory=dict)

    def __post_init__(self) -> None:
        cleaned: dict[str, Server] = {}
        for name, server in self.servers.items():
            if not isinstance(server, Server):
                raise McpHubError(f"registry entry {name!r} is not a Server")
            if server.name != name:
                raise McpHubError(
                    f"registry key {name!r} does not match server name {server.name!r}"
                )
            require_name(name)
            cleaned[name] = server
        self.servers = cleaned

    def to_dict(self) -> dict[str, Any]:
        return {name: self.servers[name].to_dict() for name in sorted(self.servers)}
