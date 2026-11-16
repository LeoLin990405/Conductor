"""Pull MCP entries out of agent configs into the registry.

Plaintext env values are not copied. A single plaintext value under a key is
stored as ``${KEY}``. Two different plaintext values under the same key stay
two servers, each with its own ``${VAR}`` reference, so apply cannot drop one.
``${VAR}`` and ``keychain:service/account`` references are kept as written.
Servers that share a transport, command, args, url and those references are
one server. A different reference is a different server. Invalid names are
skipped so a hostile key cannot enter the registry.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agents import agent_path
from .errors import McpHubError
from .journal import ownership_from_journal, snapshot_ids, write_entry
from .model import (
    AGENT_IDS,
    Registry,
    Server,
    classify_env_value,
    require_env_key,
    require_name,
)
from .render import _home, _read_config, generic_servers
from .store import load_registry, save_managed, save_registry


@dataclass(frozen=True)
class Observed:
    agent: str
    name: str
    transport: str
    command: str
    args: tuple[str, ...]
    url: str
    env: tuple[tuple[str, str], ...]
    env_spec: tuple[tuple[str, str, str], ...]
    enabled: bool
    startup_timeout_sec: int | None


def import_existing(home: Path | str, *, write: bool = True) -> dict[str, Any]:
    """Read agent files and merge what they declare into the registry.

    Returns a dict keyed by imported server name. Agent files are not modified.
    With ``write=False`` nothing at all is written: not the registry, not the
    journal, not the managed sidecar, and no hub directory is created.
    """
    root = _home(home)
    found: list[Observed] = []
    texts: dict[str, str] = {}
    for agent in AGENT_IDS:
        path = agent_path(root, agent)
        text = _read_config(root, path)
        if text is None or not text.strip():
            continue
        texts[agent] = text
        try:
            native = generic_servers(agent, text)
        except McpHubError:
            continue
        for name, body in native.items():
            observed = _observe(agent, name, body)
            if observed is not None:
                found.append(observed)
    refs = _plaintext_refs([(item.name, item.env_spec) for item in found])
    found = [
        replace(item, env=_stored_env(item.env_spec, refs)) for item in found
    ]
    registry = load_registry(root)
    before, _hashes = ownership_from_journal(root)
    claimed = {agent: set(before.get(agent, ())) for agent in AGENT_IDS}
    grouped: dict[tuple[Any, ...], list[Observed]] = {}
    for item in found:
        grouped.setdefault(_identity(item), []).append(item)
    imported: dict[str, Any] = {}
    for identity, group in grouped.items():
        natural = _pick_name(group)
        current = registry.servers.get(natural)
        if current is not None and _server_identity(current) == identity:
            server = _union(current, _server_for(group, natural))
        elif current is not None:
            server = _server_for(group, _fresh_name(natural, set(registry.servers)))
        else:
            server = _server_for(group, natural)
        registry.servers[server.name] = server
        for item in group:
            claimed[item.agent].add(item.name)
        imported[server.name] = server.to_dict()
    if imported and write:
        save_registry(root, Registry(servers=registry.servers))
        _record_import(root, before, claimed, texts)
        try:
            save_managed(root, claimed)
        except (OSError, McpHubError):
            # The journal already holds the claim. The cache is optional.
            pass
    return imported


def _record_import(
    root: Path,
    before: dict[str, set[str]],
    claimed: dict[str, set[str]],
    texts: dict[str, str],
) -> None:
    """Append one ``imported`` event per agent whose claim changed.

    Each event stores the full owned set so a later replay does not drop it.
    """
    changed = [
        agent
        for agent in AGENT_IDS
        if claimed.get(agent, set()) != before.get(agent, set())
    ]
    if not changed:
        return
    snapshot_id = _import_snapshot_id(root)
    for agent in changed:
        path = agent_path(root, agent)
        raw = texts.get(agent, "").encode("utf-8")
        write_entry(
            root,
            snapshot_id,
            {
                "agent": agent,
                "target": path.relative_to(root).as_posix(),
                "pre_hash": None,
                "post_hash": hashlib.sha256(raw).hexdigest(),
                "owned": sorted(claimed.get(agent, ())),
                "state": "imported",
            },
        )


def _import_snapshot_id(root: Path) -> str:
    taken = set(snapshot_ids(root))
    base = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    candidate = base
    counter = 0
    while candidate in taken:
        counter += 1
        candidate = f"{base}-{counter}"
    return candidate


def _identity(item: Observed) -> tuple[Any, ...]:
    return (item.transport, item.command, item.args, item.url, item.env)


def _server_identity(server: Server) -> tuple[Any, ...]:
    env = tuple(sorted(server.env.items()))
    return (server.transport, server.command, server.args, server.url, env)


def _observe(agent: str, name: str, body: dict[str, Any]) -> Observed | None:
    try:
        require_name(name)
    except McpHubError:
        return None
    transport = _transport(body)
    command = body.get("command") if isinstance(body.get("command"), str) else ""
    url = body.get("url") if isinstance(body.get("url"), str) else ""
    if _control(command) or _control(url) or _credential_url(url):
        return None
    raw_args = body.get("args") if body.get("args") is not None else []
    if not isinstance(raw_args, list) or any(
        not isinstance(item, str) for item in raw_args
    ):
        return None
    if any(_control(item) for item in raw_args):
        return None
    args = tuple(raw_args)
    if transport in {"http", "sse"} and not url:
        if command:
            transport = "stdio"
        else:
            return None
    if transport == "stdio" and not command:
        if url:
            transport = "http"
        else:
            return None
    if url and transport == "stdio" and not command:
        transport = "http"
    env_raw = body.get("env") if isinstance(body.get("env"), dict) else {}
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = True
    timeout = body.get("startup_timeout_sec")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 0:
        timeout = None
    return Observed(
        agent=agent,
        name=name,
        transport=transport,
        command=command,
        args=args,
        url=url,
        env=(),
        env_spec=_env_spec(env_raw),
        enabled=enabled,
        startup_timeout_sec=timeout,
    )


def _transport(body: dict[str, Any]) -> str:
    raw = body.get("transport") if isinstance(body.get("transport"), str) else None
    if raw is None and isinstance(body.get("type"), str):
        raw = body["type"]
    if raw in {"stdio", "http", "sse"}:
        return str(raw)
    return "stdio"


def _control(value: str) -> bool:
    return any(ch in value for ch in ("\n", "\r", "\x00"))


def _credential_url(url: str) -> bool:
    if "://" not in url:
        return False
    host = url.split("://", 1)[1].split("/", 1)[0]
    return "@" in host


def _fresh_name(name: str, used: set[str]) -> str:
    counter = 2
    while f"{name}--{counter}" in used:
        counter += 1
    fresh = f"{name}--{counter}"
    return require_name(fresh)


def _env_spec(env_raw: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """Classify env entries. A plaintext payload is a digest, never the value."""
    rows: list[tuple[str, str, str]] = []
    for key, value in env_raw.items():
        try:
            require_env_key(key)
        except McpHubError:
            continue
        if isinstance(value, str) and classify_env_value(value) in {"var", "keychain"}:
            rows.append((str(key), classify_env_value(value), value))
            continue
        raw = value if isinstance(value, str) else repr(value)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        rows.append((str(key), "plain", digest))
    return tuple(sorted(rows))


def _env_token(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in name)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"n_{cleaned}"
    return cleaned[:48]


def _plaintext_refs(
    groups: list[tuple[str, tuple[tuple[str, str, str], ...]]],
) -> dict[tuple[str, str], str]:
    """Give each distinct plaintext value its own ``${REF}``.

    One value under a key keeps ``${KEY}``. A second value, same key and
    same command, gets a different reference so the two servers do not merge.
    The digest never leaves this map.
    """
    names_for: dict[tuple[str, str], set[str]] = {}
    digests_for: dict[str, set[str]] = {}
    for name, spec in groups:
        for key, kind, payload in spec:
            if kind != "plain":
                continue
            names_for.setdefault((key, payload), set()).add(name)
            digests_for.setdefault(key, set()).add(payload)
    assigned: dict[tuple[str, str], str] = {}
    used: set[str] = set()
    for key, digest in sorted(names_for):
        if len(digests_for[key]) == 1:
            token = key
        else:
            base_name = sorted(names_for[(key, digest)])[0]
            token = f"{key}_{_env_token(base_name)}"
        candidate = token
        counter = 2
        while candidate in used:
            candidate = f"{token}_{counter}"
            counter += 1
        require_env_key(candidate)
        used.add(candidate)
        assigned[(key, digest)] = f"${{{candidate}}}"
    return assigned


def _stored_env(
    spec: tuple[tuple[str, str, str], ...],
    refs: dict[tuple[str, str], str],
) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    for key, kind, payload in spec:
        if kind == "plain":
            rows.append((key, refs[(key, payload)]))
        else:
            rows.append((key, payload))
    return tuple(sorted(rows))


def _server_for(group: list[Observed], name: str) -> Server:
    require_name(name)
    targets = tuple(
        agent for agent in AGENT_IDS if any(item.agent == agent for item in group)
    )
    overrides: dict[str, dict[str, Any]] = {}
    for item in group:
        override: dict[str, Any] = {}
        if item.name != name:
            override["name_in_file"] = item.name
        if item.startup_timeout_sec is not None and item.agent == "stepfun":
            override["startup_timeout_sec"] = item.startup_timeout_sec
        if not item.enabled:
            override["enabled"] = False
        if override:
            overrides[item.agent] = override
    enabled = all(item.enabled for item in group)
    first = group[0]
    return Server(
        name=name,
        transport=first.transport,
        command=first.command,
        args=first.args,
        url=first.url,
        env=dict(group[0].env),
        targets=targets if len(targets) != len(AGENT_IDS) else "all",
        overrides=overrides,
        enabled=enabled,
    )


def _pick_name(group: list[Observed]) -> str:
    counts = Counter(item.name for item in group)
    return sorted(counts, key=lambda item: (-counts[item], item))[0]


def _union(current: Server, new: Server) -> Server:
    if current.targets == "all" or new.targets == "all":
        targets: str | tuple[str, ...] = "all"
    else:
        targets = tuple(
            agent
            for agent in AGENT_IDS
            if agent in current.targets or agent in new.targets
        )
    env = dict(current.env)
    env.update(new.env)
    overrides = {agent: dict(values) for agent, values in current.overrides.items()}
    for agent, values in new.overrides.items():
        merged = dict(overrides.get(agent, {}))
        merged.update(values)
        overrides[agent] = merged
    return Server(
        name=current.name,
        transport=current.transport,
        command=current.command,
        args=current.args,
        url=current.url,
        env=env,
        targets=targets,
        overrides=overrides,
        enabled=current.enabled and new.enabled,
    )
