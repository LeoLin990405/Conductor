"""Render the registry into one agent's native MCP config, without writing.

JSON edits replace only the server-map value. TOML edits replace only managed
``[mcp_servers.NAME]`` tables. A server the file already has under a name the
hub does not own is left untouched when it differs, and reported as skipped.
Ownership is the journal replay passed in, or read from the journal when the
caller does not pass it. This module does not read ``managed.toml``.
A key the hub owns but the registry no longer wants is released: it is deleted
when its server was removed, or disabled when its server is off and the agent
can hold an ``enabled`` field. A release is only reported once the bytes move
and the key is gone from them. An owned key the edit could not reach stays
owned and is listed under ``orphaned`` instead, so the next apply retries it
rather than leaving an active key nobody owns. ``orphaned`` also lists owned
keys left in place because their server is still enabled but could not be
rendered.
"""

from __future__ import annotations

import difflib
import tomllib
from pathlib import Path
from typing import Any, Collection

from .agents import SPECS, agent_path, assert_no_escape
from .editing import (
    _subset_equal,
    edit_json_container,
    edit_toml_servers,
    json_servers,
    merge_server_object,
)
from .errors import McpHubError
from .model import Registry, Server, classify_env_value

MAX_CONFIG_BYTES = 2 * 1024 * 1024

_NO_ENV = frozenset({"opencode"})


def render_agent(
    home: Path | str,
    agent: str,
    registry: Registry | None = None,
    *,
    allow_inline_secret: Collection[str] = (),
    owned: Collection[str] | None = None,
) -> dict[str, Any]:
    """Describe the file ``agent`` would have. Does not write.

    ``skipped`` lists servers left untouched and why. ``unified_diff`` is
    empty when nothing would change. Env values are never copied into the
    diff from anywhere except the references already stored in the registry.
    """
    root = _home(home)
    if agent not in SPECS:
        raise McpHubError(f"unknown agent {agent!r}")
    if registry is None:
        from .store import load_registry

        registry = load_registry(root)
    if owned is None:
        from .journal import ownership_from_journal

        owned_names = set(ownership_from_journal(root)[0].get(agent, ()))
    else:
        owned_names = {str(name) for name in owned}

    path = agent_path(root, agent)
    allow = {str(name) for name in allow_inline_secret}
    original_bytes = _read_config_bytes(root, path)
    original = _decode_config(path, original_bytes)
    owned = owned_names
    desired: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, str]] = []
    inline_names: set[str] = set()
    key_owner: dict[str, Server] = {}

    for server in registry.servers.values():
        if not server.targets_agent(agent):
            continue
        key = server.file_key(agent)
        effective = server.effective(agent)
        if not effective["enabled"]:
            continue
        native, reason = project_server(server, agent, allow=allow)
        if reason is not None or native is None:
            skipped.append(
                {
                    "server": server.name,
                    "reason": reason or f"{server.name}: nothing to render for {agent}",
                }
            )
            continue
        desired[key] = native
        key_owner[key] = server
        if _needs_inline(effective):
            inline_names.add(server.name)

    existing = _existing_servers(agent, original)
    for key in list(desired):
        # Ordinary apply must not acquire a hand-written entry, even when the
        # shape matches. Import is what records ownership.
        if key in existing and key not in owned:
            skipped.append(
                {
                    "server": key_owner[key].name,
                    "reason": (
                        f"{key}: unmanaged server already in {agent}; refusing to claim"
                    ),
                }
            )
            desired.pop(key)
            inline_names.discard(key_owner[key].name)

    # Release owned keys the registry no longer wants enabled. plan_release
    # decides delete vs disable from the registry and the owned set alone. A
    # disabled key is flipped to ``enabled = false`` on the bytes already in
    # the file when the agent can hold that field; otherwise, or when the key
    # is already gone, the owned key is deleted. Only owned keys are scheduled.
    from .store import plan_release

    release_delete, release_disable = plan_release(registry, agent, owned)
    disable_hook = SPECS[agent].disable
    for key in list(release_disable):
        body = existing.get(key)
        if isinstance(body, dict) and disable_hook is not None:
            desired[key] = disable_hook(dict(body))
        elif isinstance(body, dict) and "enabled" in body:
            disabled = dict(body)
            disabled["enabled"] = False
            desired[key] = disabled
        else:
            release_delete.add(key)
            release_disable.discard(key)
    scheduled = set(release_delete) | set(release_disable)
    remove = set(release_delete)
    desired_keys = set(desired)
    new_text, changed = _apply_edit(agent, original, desired, remove)
    if changed and new_text is not None:
        rendered: str | None = new_text
    else:
        rendered = None
        changed = False
        new_text = original
    # A release only stands once the key is really gone from the bytes this
    # apply writes, or already gone from the bytes it leaves alone. A
    # declaration the editor cannot reach, such as a dotted
    # ``mcp_servers.NAME = {...}`` key, keeps its ownership and is listed under
    # ``orphaned`` instead, so the next apply retries it rather than leaving an
    # active key nobody owns.
    _unrelease_unreached(agent, new_text, release_delete, release_disable)
    unreached = scheduled - release_delete - release_disable
    force_private = bool(inline_names & allow) and changed
    mode = 0o600
    if path.is_file() and not path.is_symlink():
        mode = path.lstat().st_mode & 0o777
    if force_private or not path.exists():
        mode = 0o600
    own = sorted((set(owned) | set(desired_keys)) - release_delete)
    orphaned = sorted(
        (set(owned) - set(desired_keys) - release_delete) | unreached
    )
    released = [
        {"key": key, "action": "delete"} for key in sorted(release_delete)
    ] + [{"key": key, "action": "disable"} for key in sorted(release_disable)]
    return {
        "agent": agent,
        "path": str(path),
        "relpath": path.relative_to(root).as_posix(),
        "unified_diff": "" if not changed else _unified(path, original, rendered),
        "changed": changed,
        "skipped": skipped,
        "rendered": rendered,
        "original": original_bytes,
        "existed": original is not None,
        "mode": mode,
        "force_private": force_private,
        "own": own,
        "orphaned": orphaned,
        "released": released,
    }


def _unrelease_unreached(
    agent: str,
    rendered: str | None,
    release_delete: set[str],
    release_disable: set[str],
) -> None:
    """Take back a release whose key the bytes did not reach.

    The editor copies through any representation it does not recognise, so a
    drop can leave the key in place. A deleted key must be absent from the
    servers the bytes declare and a disabled key must carry
    ``enabled = false`` there; anything else keeps its ownership. A file with
    no bytes at all declares nothing, so every scheduled key is gone from it.
    Bytes that will not parse prove nothing, so no release stands on them.
    """
    if rendered is None:
        return
    try:
        # Neutral names, so an app whose off switch is ``disabled: true``
        # still reads as ``enabled = false`` here.
        after = generic_servers(agent, rendered)
    except McpHubError:
        release_delete.clear()
        release_disable.clear()
        return
    for key in sorted(release_delete):
        if key in after:
            release_delete.discard(key)
    for key in sorted(release_disable):
        body = after.get(key)
        if not isinstance(body, dict) or body.get("enabled") is not False:
            release_disable.discard(key)


def project_server(
    server: Server,
    agent: str,
    *,
    allow: Collection[str] = (),
) -> tuple[dict[str, Any] | None, str | None]:
    """Native object for ``agent``, or ``(None, reason)`` when it cannot be written."""
    if agent not in SPECS:
        raise McpHubError(f"unknown agent {agent!r}")
    effective = server.effective(agent)
    if not effective["enabled"]:
        return None, None
    reason = _cannot_represent(server.name, agent, effective, set(allow))
    if reason:
        return None, reason
    pattern = SPECS[agent].key_pattern
    key = server.file_key(agent)
    if pattern is not None and not pattern.fullmatch(key):
        return None, f"{server.name}: {agent} cannot use {key!r} as a server key"
    return _native(agent, effective), None


def parse_agent_text(agent: str, text: str) -> dict[str, dict[str, Any]]:
    spec = SPECS[agent]
    if spec.fmt == "codec":
        return spec.codec.parse(text)
    if spec.fmt == "json":
        return json_servers(text, spec.container)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise McpHubError(f"{agent} config is not TOML: {exc}") from exc
    raw = data.get("mcp_servers") or {}
    if not isinstance(raw, dict):
        raise McpHubError(f"{agent} mcp_servers is not a table")
    out: dict[str, dict[str, Any]] = {}
    for name, table in raw.items():
        if isinstance(name, str) and isinstance(table, dict):
            out[name] = table
    return out


def generic_servers(agent: str, text: str) -> dict[str, dict[str, Any]]:
    """Servers in the hub's neutral field names, for import and drift.

    JSON and TOML agents already use those names. A codec agent maps its own
    shape (for example dsh's ``config.serverName``) through ``codec.generic``.
    """
    spec = SPECS[agent]
    servers = parse_agent_text(agent, text)
    if spec.fmt == "codec":
        return {name: spec.codec.generic(body) for name, body in servers.items()}
    if spec.generic is not None:
        return {name: spec.generic(body) for name, body in servers.items()}
    return servers


def _home(home: Path | str) -> Path:
    path = Path(home)
    if not path.is_absolute():
        raise McpHubError("home must be an absolute path")
    return path


def _read_config_bytes(home: Path, path: Path) -> bytes | None:
    if not path.exists() and not path.is_symlink():
        # Still refuse a parent symlink before a later create would follow it.
        assert_no_escape(home, path)
        return None
    assert_no_escape(home, path)
    try:
        size = path.lstat().st_size
    except OSError as exc:
        raise McpHubError(f"could not stat {path.name}: {exc.strerror or exc}") from exc
    if size > MAX_CONFIG_BYTES:
        raise McpHubError(f"{path.name} exceeds {MAX_CONFIG_BYTES} bytes")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise McpHubError(f"could not read {path.name}: {exc.strerror or exc}") from exc


def _decode_config(path: Path, data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise McpHubError(f"{path.name} is not utf-8") from exc


def _read_config(home: Path, path: Path) -> str | None:
    return _decode_config(path, _read_config_bytes(home, path))


def _existing_servers(agent: str, text: str | None) -> dict[str, dict[str, Any]]:
    if text is None:
        return {}
    return parse_agent_text(agent, text)


def _matches(agent: str, existing: dict[str, Any], desired: dict[str, Any]) -> bool:
    spec = SPECS[agent]
    if spec.fmt == "codec":
        return bool(spec.codec.matches(existing, desired))
    if spec.fmt == "json":
        # An app with its own field names is compared as the neutral server
        # both bodies stand for: two spellings of one server (Gemini's
        # deprecated ``httpUrl`` next to ``type`` + ``url``, Antigravity's
        # ``serverUrl``) are not drift, while an off switch the subset rule
        # would not see (``disabled: true``) is.
        if spec.generic is not None:
            return _neutral(spec.generic(existing)) == _neutral(spec.generic(desired))
        return _subset_equal(existing, desired)
    for key, value in desired.items():
        if existing.get(key) != value:
            return False
    return True


def _neutral(body: dict[str, Any]) -> dict[str, Any]:
    transport = body.get("transport") or "stdio"
    return {
        "transport": transport,
        "command": body.get("command") or "",
        "args": list(body.get("args") or []),
        "url": body.get("url") or "",
        "env": dict(body.get("env") or {}),
        "enabled": body.get("enabled", True) is not False,
        "startup_timeout_sec": body.get("startup_timeout_sec"),
    }


def _apply_edit(
    agent: str,
    original: str | None,
    desired: dict[str, dict[str, Any]],
    remove: set[str],
) -> tuple[str | None, bool]:
    spec = SPECS[agent]
    if spec.fmt == "codec":
        return _apply_codec(agent, original, desired, remove)
    if original is None:
        if not desired:
            return None, False
        if spec.fmt == "json":
            return _fresh_json(spec.container, desired), True
        return _fresh_toml(desired), True
    if spec.fmt == "json":
        existing = _existing_servers(agent, original)
        # An entry that already matches (subset or same neutral server) keeps
        # its bytes; only real drift is rewritten.
        rewrite = {
            name: body
            for name, body in desired.items()
            if not (name in existing and _matches(agent, existing[name], body))
        }

        def rebuild(spans):
            return merge_server_object(original, spans, rewrite, remove)

        updated = edit_json_container(original, spec.container, rebuild)
        if updated == original:
            return original, False
        return updated, True
    existing = _existing_servers(agent, original)
    # Matching tables stay byte for byte; only a real drift is rewritten.
    rewrite = {
        name: body
        for name, body in desired.items()
        if not (name in existing and _matches(agent, existing[name], body))
    }
    updated = edit_toml_servers(original, rewrite=rewrite, remove=remove)
    if updated == original:
        return original, False
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise McpHubError(f"refusing to write invalid TOML: {exc}") from exc
    return updated, True


def _apply_codec(
    agent: str,
    original: str | None,
    desired: dict[str, dict[str, Any]],
    remove: set[str],
) -> tuple[str | None, bool]:
    codec = SPECS[agent].codec
    if original is None:
        if not desired:
            return None, False
        updated = codec.fresh(desired)
    else:
        existing = codec.parse(original)
        rewrite = {
            name: body
            for name, body in desired.items()
            if not (name in existing and codec.matches(existing[name], body))
        }
        present_removals = {name for name in remove if name in existing}
        if not rewrite and not present_removals:
            return original, False
        updated = codec.edit(original, rewrite, present_removals)
        if updated == original:
            return original, False
    # The codec must be able to read back what it wrote.
    codec.parse(updated)
    return updated, True


def _fresh_json(container: tuple[str, ...], desired: dict[str, dict[str, Any]]) -> str:
    import json

    obj: object = {name: desired[name] for name in sorted(desired)}
    for key in reversed(container):
        obj = {key: obj}
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _fresh_toml(desired: dict[str, dict[str, Any]]) -> str:
    from .editing import render_toml_server

    parts = [render_toml_server(name, desired[name]) for name in sorted(desired)]
    return "\n".join(part.rstrip("\n") for part in parts) + "\n"


def _native(agent: str, effective: dict[str, Any]) -> dict[str, Any]:
    spec = SPECS[agent]
    if spec.native is not None:
        return spec.native(effective)
    style = spec.style
    transport = str(effective["transport"])
    command = str(effective["command"])
    args = list(effective["args"])
    url = str(effective["url"])
    env = dict(effective["env"])
    remote = transport in {"http", "sse"} and bool(url)
    if style == "claude":
        if remote:
            obj: dict[str, Any] = {"type": transport, "url": url}
        else:
            obj = {"type": "stdio", "command": command, "args": args}
        if env:
            obj["env"] = env
        return obj
    if style in {"cursor", "dsh"}:
        obj = {"command": command, "args": args}
        if url:
            obj["url"] = url
        if env:
            obj["env"] = env
        return obj
    if style == "kimi":
        obj = {"transport": transport, "command": command, "args": args}
        if url:
            obj["url"] = url
        if env:
            obj["env"] = env
        return obj
    if style == "minimax":
        if remote:
            return {"type": transport, "url": url, "enabled": True}
        return {
            "type": "stdio",
            "command": command,
            "args": args,
            "env": env,
            "enabled": True,
        }
    if style == "zcode":
        obj = {}
        if command:
            obj["command"] = command
        if args:
            obj["args"] = args
        if env:
            obj["env"] = env
        if url and not command:
            obj["url"] = url
        return obj
    if style == "opencode":
        return {"type": transport, "command": command, "enabled": True}
    fields: dict[str, Any] = {}
    if remote:
        fields["url"] = url
    else:
        fields["command"] = command
        fields["args"] = args
    if style == "grok":
        fields["enabled"] = True
    timeout = effective.get("startup_timeout_sec")
    if (
        style == "stepfun"
        and isinstance(timeout, int)
        and not isinstance(timeout, bool)
    ):
        fields["startup_timeout_sec"] = timeout
    if env:
        fields["env"] = env
    return fields


def _cannot_represent(
    name: str,
    agent: str,
    effective: dict[str, Any],
    allow: set[str],
) -> str | None:
    spec = SPECS[agent]
    style = spec.style
    has_env = style not in _NO_ENV and spec.has_env
    for key, value in effective["env"].items():
        kind = classify_env_value(value)
        if kind == "plain":
            return f"{name}: refusing plaintext env {key}"
        if kind == "keychain":
            if not has_env:
                return f"{name}: {agent} has no env field for {key}"
            if name not in allow:
                return (
                    f"{name}: {agent} cannot represent a keychain reference for {key}"
                )
        elif kind == "var" and not has_env:
            return f"{name}: {agent} has no env field for {key}"
    if style == "opencode" and effective["args"]:
        return f"{name}: opencode cannot represent args"
    if style == "opencode" and effective["url"] and not effective["command"]:
        return f"{name}: opencode cannot represent a url"
    if style == "zcode" and not effective["command"] and not effective["url"]:
        return f"{name}: zcode needs a command"
    if spec.cannot is not None:
        return spec.cannot(name, effective, allow)
    return None


def _needs_inline(effective: dict[str, Any]) -> bool:
    return any(
        classify_env_value(value) == "keychain" for value in effective["env"].values()
    )


def _unified(path: Path, old: str | None, new: str | None) -> str:
    old_text = old or ""
    new_text = new or ""
    if old_text == new_text:
        return ""
    lines = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile=str(path),
        tofile=str(path),
        lineterm="",
    )
    body = "\n".join(lines)
    if not body:
        return ""
    return body + "\n"
