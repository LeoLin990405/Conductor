"""Registry and managed-sidecar files under ``<home>/.conductor/mcp``.

Reads use tomllib. Writes use a small encoder (no new dependency) and an
atomic replace. A missing registry is empty; reading it does not create the
directory, so a dry run stays read-only.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Collection

from .agents import assert_no_escape, hub_dir
from .atomic import atomic_write, file_mode
from .editing import toml_key, toml_string, toml_value
from .errors import McpHubError
from .model import AGENT_IDS, Registry, Server
from .render import _home

_MANAGED = "managed.toml"
_REGISTRY = "servers.toml"


def registry_path(home: Path | str) -> Path:
    return _home(home) / ".conductor" / "mcp" / _REGISTRY


def managed_path(home: Path | str) -> Path:
    return _home(home) / ".conductor" / "mcp" / _MANAGED


def load_registry(home: Path | str) -> Registry:
    path = registry_path(home)
    text = _read_optional(_home(home), path)
    if text is None or not text.strip():
        return Registry()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise McpHubError(f"servers.toml is not valid TOML: {exc}") from exc
    raw = data.get("servers") or {}
    if not isinstance(raw, dict):
        raise McpHubError("servers.toml 'servers' must be a table")
    servers: dict[str, Server] = {}
    for name, table in raw.items():
        if not isinstance(table, dict):
            raise McpHubError(f"server {name!r} is not a table")
        servers[str(name)] = _server_from_table(str(name), table)
    return Registry(servers=servers)


def save_registry(home: Path | str, registry: Registry) -> Path:
    root = _home(home)
    checked: dict[str, Server] = {}
    for name, server in registry.servers.items():
        # Re-build so a mutated plaintext env cannot be written later.
        checked[name] = Server(**server.to_dict())
    payload = dump_registry(Registry(servers=checked)).encode("utf-8")
    path = registry_path(root)
    assert_no_escape(root, path)
    hub_dir(root)
    mode = file_mode(path, force_private=False)
    atomic_write(path, payload, mode=mode, home=root)
    return path


def plan_release(
    registry: Registry, agent: str, owned: Collection[str]
) -> tuple[set[str], set[str]]:
    """Owned keys to delete or disable, decided from the registry alone.

    Returns ``(delete, disable)``. ``delete`` holds owned keys no live server
    still writes under for this agent: the server was removed, or it no longer
    targets this agent. ``disable`` holds owned keys whose server is now
    disabled. A key whose server is still enabled is left alone, whether it
    renders or is skipped, so a representation hiccup never drops a key. Only
    names in ``owned`` are considered, so a hand-written key the hub does not
    own is never scheduled for release.
    """
    live: set[str] = set()
    disabled: set[str] = set()
    for server in registry.servers.values():
        if not server.targets_agent(agent):
            continue
        try:
            key = server.file_key(agent)
        except McpHubError:
            continue
        live.add(key)
        if not server.effective(agent)["enabled"]:
            disabled.add(key)
    delete: set[str] = set()
    disable: set[str] = set()
    for key in owned:
        if key not in live:
            delete.add(key)
        elif key in disabled:
            disable.add(key)
    return delete, disable


def load_managed(home: Path | str) -> dict[str, set[str]]:
    """Agent id -> server names the hub already owns in that agent's file."""
    data = _load_managed_document(home)
    return _managed_from_document(data)


def load_published(home: Path | str) -> dict[str, str]:
    """Agent id -> sha256 of the bytes last published into that agent's file."""
    data = _load_managed_document(home)
    raw = data.get("published") or {}
    if not isinstance(raw, dict):
        return {}
    published: dict[str, str] = {}
    for agent, digest in raw.items():
        if agent not in AGENT_IDS or not isinstance(digest, str) or not digest:
            continue
        published[str(agent)] = digest
    return published


def managed_from_toml(text: str) -> dict[str, set[str]]:
    """Parse a managed-sidecar document. Unknown sections are ignored."""
    if not text.strip():
        return {agent: set() for agent in AGENT_IDS}
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise McpHubError(f"managed.toml is not valid TOML: {exc}") from exc
    if not isinstance(data, dict):
        raise McpHubError("managed.toml is not valid TOML")
    return _managed_from_document(data)


def save_managed(
    home: Path | str,
    managed: dict[str, set[str]],
    *,
    published: dict[str, str] | None = None,
) -> Path:
    root = _home(home)
    path = managed_path(root)
    assert_no_escape(root, path)
    hub_dir(root)
    if published is None:
        published = load_published(root)
    mode = file_mode(path, force_private=False)
    payload = dump_managed(managed, published).encode("utf-8")
    atomic_write(path, payload, mode=mode, home=root)
    return path


def _load_managed_document(home: Path | str) -> dict[str, Any]:
    root = _home(home)
    text = _read_optional(root, managed_path(root))
    if text is None or not text.strip():
        return {}
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise McpHubError(f"managed.toml is not valid TOML: {exc}") from exc
    if not isinstance(data, dict):
        raise McpHubError("managed.toml is not valid TOML")
    return data


def _managed_from_document(data: dict[str, Any]) -> dict[str, set[str]]:
    owned = {agent: set() for agent in AGENT_IDS}
    raw = data.get("managed") or {}
    if not isinstance(raw, dict):
        raise McpHubError("managed.toml 'managed' must be a table")
    for agent, names in raw.items():
        if agent not in owned:
            raise McpHubError(f"unknown agent {agent!r} in managed.toml")
        if not isinstance(names, list):
            raise McpHubError(f"managed.{agent} must be a list of server names")
        for name in names:
            owned[str(agent)].add(_owned_name(name))
    return owned


def dump_registry(registry: Registry) -> str:
    chunks: list[str] = []
    for name in sorted(registry.servers):
        server = registry.servers[name]
        lines = [
            f"[servers.{name}]",
            f"transport = {toml_string(server.transport)}",
            f"command = {toml_string(server.command)}",
            f"args = {toml_value(list(server.args))}",
            f"url = {toml_string(server.url)}",
            f"enabled = {toml_value(server.enabled)}",
        ]
        if server.targets == "all":
            lines.append('targets = "all"')
        else:
            lines.append(f"targets = {toml_value(list(server.targets))}")
        chunks.append("\n".join(lines))
        if server.env:
            env_lines = [f"[servers.{name}.env]"]
            for key in sorted(server.env):
                env_lines.append(f"{key} = {toml_string(server.env[key])}")
            chunks.append("\n".join(env_lines))
        for agent in sorted(server.overrides):
            override = server.overrides[agent]
            if not override:
                continue
            chunks.extend(_override_tables(name, agent, override))
    if not chunks:
        return ""
    return "\n\n".join(chunks) + "\n"


def _override_tables(name: str, agent: str, override: dict[str, Any]) -> list[str]:
    """Scalar override keys, then one nested table per dict-valued key.

    TOML closes a table once a subtable is opened, so env maps cannot be
    inlined after the fact. Empty maps stay inline so they still round-trip.
    """
    scalars = [f"[servers.{name}.overrides.{agent}]"]
    nested: list[tuple[str, dict[str, Any]]] = []
    wrote_scalar = False
    for key in sorted(override):
        value = override[key]
        if isinstance(value, dict) and value:
            nested.append((key, value))
            continue
        scalars.append(f"{key} = {toml_value(value)}")
        wrote_scalar = True
    chunks: list[str] = []
    if wrote_scalar or not nested:
        chunks.append("\n".join(scalars))
    for key, value in nested:
        lines = [f"[servers.{name}.overrides.{agent}.{key}]"]
        for child in sorted(value):
            lines.append(f"{toml_key(str(child))} = {toml_value(value[child])}")
        chunks.append("\n".join(lines))
    return chunks


def dump_managed(
    managed: dict[str, set[str]], published: dict[str, str] | None = None
) -> str:
    lines = ["[managed]"]
    empty = True
    for agent in AGENT_IDS:
        names = sorted(managed.get(agent, ()))
        if not names:
            continue
        empty = False
        lines.append(f"{agent} = {toml_value(names)}")
    body = "[managed]\n" if empty else "\n".join(lines) + "\n"
    pub_lines = ["[published]"]
    for agent in AGENT_IDS:
        digest = (published or {}).get(agent)
        if not isinstance(digest, str) or not digest:
            continue
        pub_lines.append(f"{agent} = {toml_string(digest)}")
    if len(pub_lines) == 1:
        return body
    return body + "\n" + "\n".join(pub_lines) + "\n"


def _server_from_table(name: str, table: dict[str, Any]) -> Server:
    env = table.get("env") or {}
    overrides = table.get("overrides") or {}
    if not isinstance(env, dict):
        raise McpHubError(f"server {name}: env must be a table")
    if not isinstance(overrides, dict):
        raise McpHubError(f"server {name}: overrides must be a table")
    targets = table.get("targets", "all")
    return Server(
        name=name,
        transport=str(table.get("transport") or "stdio"),
        command=str(table.get("command") or ""),
        args=tuple(table.get("args") or ()),
        url=str(table.get("url") or ""),
        env={str(k): str(v) for k, v in env.items()},
        targets=targets,
        overrides={str(agent): dict(values) for agent, values in overrides.items()},
        enabled=table.get("enabled", True)
        if isinstance(table.get("enabled", True), bool)
        else True,
    )


def _owned_name(name: object) -> str:
    from .model import require_name

    return require_name(name)


def _read_optional(home: Path, path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    assert_no_escape(home, path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise McpHubError(f"could not read {path.name}: {exc.strerror or exc}") from exc
    if len(data) > 2 * 1024 * 1024:
        raise McpHubError(f"{path.name} is too large")
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise McpHubError(f"{path.name} is not utf-8") from exc
