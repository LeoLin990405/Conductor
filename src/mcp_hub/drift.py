"""Read-only drift report: the registry next to what each agent file holds.

For every agent the report lists:

* ``missing``: catalog servers aimed at this agent that the file lacks;
* ``different``: servers the file has under a catalog key whose fields differ
  (only the differing field names are listed, never their values);
* ``extra``: servers in the file the catalog does not aim at this agent, with
  whether the hub owns that key;
* ``unrepresentable``: catalog servers this agent cannot hold, and why;
* ``fragile``: servers whose command, args, cwd or url point into a temporary
  or draft location (a ``draft-*`` workspace dir, ``/tmp``, ``/var/folders``,
  the Trash), reported by key path only;
* ``secrets``: key paths in the config whose value looks like a literal
  secret. The value itself is never read into the report.

Nothing is written, no lock is taken, and no file outside the HOME is read.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any, Iterator

from .agents import SPECS, agent_path
from .errors import McpHubError
from .journal import ownership_from_journal
from .model import AGENT_IDS, Registry, classify_env_value
from .render import (
    _home,
    _matches,
    _read_config,
    generic_servers,
    parse_agent_text,
    project_server,
)
from .store import load_registry

MAX_SECRET_PATHS = 50

_SECRET_KEY = re.compile(
    r"(token|secret|passw(or)?d|passwd|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|credential|bearer|cookie|authorization|client[_-]?secret|"
    r"session[_-]?key|signing[_-]?key)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"^(sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\..*|"
    r"(Bearer|Basic) [A-Za-z0-9._~+/=-]{12,})$"
)
_REFERENCE = re.compile(
    r"^(\$\{[A-Za-z_][A-Za-z0-9_]*(:-[^}]*)?\}|\$[A-Za-z_][A-Za-z0-9_]*|"
    r"keychain:[^/\s]+/[^/\s]+|\{env:[A-Za-z_][A-Za-z0-9_]*\}|"
    r"process\.env\.[A-Za-z_][A-Za-z0-9_]*)$"
)
_FRAGILE_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")
_DRAFT_SEGMENT = re.compile(r"^draft-[A-Za-z0-9._-]+$")
_URL_CREDENTIAL = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@\s]+:[^/@\s]+@", re.IGNORECASE)
_URL_SECRET_QUERY = re.compile(
    r"[?&](token|key|api_key|apikey|secret|access_token|auth)=[^&\s]{8,}",
    re.IGNORECASE,
)


def drift_report(home: Path | str, registry: Registry | None = None) -> dict[str, Any]:
    root = _home(home)
    if registry is None:
        registry = load_registry(root)
    owned_by_agent, _published = ownership_from_journal(root)
    agents: dict[str, Any] = {}
    totals = {
        "missing": 0,
        "different": 0,
        "extra": 0,
        "fragile": 0,
        "secrets": 0,
        "errors": 0,
    }
    for agent in AGENT_IDS:
        row = _agent_drift(root, agent, registry, set(owned_by_agent.get(agent, ())))
        agents[agent] = row
        for key in ("missing", "different", "extra", "fragile", "secrets"):
            totals[key] += len(row.get(key, ()))
        if row.get("error"):
            totals["errors"] += 1
    return {
        "home": str(root),
        "catalog_servers": len(registry.servers),
        "totals": totals,
        "agents": agents,
        "lines": _lines(agents),
    }


def _agent_drift(
    root: Path, agent: str, registry: Registry, owned: set[str]
) -> dict[str, Any]:
    path = agent_path(root, agent)
    row: dict[str, Any] = {
        "path": str(path),
        "present": False,
        "missing": [],
        "different": [],
        "extra": [],
        "unrepresentable": [],
        "fragile": [],
        "secrets": [],
    }
    try:
        text = _read_config(root, path)
    except McpHubError as exc:
        row["error"] = str(exc)
        return row
    row["present"] = text is not None
    native: dict[str, dict[str, Any]] = {}
    generic: dict[str, dict[str, Any]] = {}
    if text is not None and text.strip():
        try:
            native = parse_agent_text(agent, text)
            generic = generic_servers(agent, text)
        except McpHubError as exc:
            row["error"] = str(exc)
            return row
    desired: dict[str, dict[str, Any]] = {}
    # Every key the catalog speaks for here, wanted on or not. An entry under
    # one of them is never "extra", even when the server is off or cannot be
    # rendered for this agent.
    catalog_keys: set[str] = set()
    for server in registry.servers.values():
        if not server.targets_agent(agent):
            continue
        key = server.file_key(agent)
        catalog_keys.add(key)
        if not server.effective(agent)["enabled"]:
            continue
        body, reason = project_server(server, agent)
        if reason is not None or body is None:
            row["unrepresentable"].append(
                {"server": server.name, "reason": reason or "nothing to render"}
            )
            continue
        desired[key] = body
    for key in sorted(desired):
        if key not in native:
            row["missing"].append(key)
        elif not _matches(agent, native[key], desired[key]):
            row["different"].append(
                {"key": key, "fields": _differing_fields(native[key], desired[key])}
            )
    for key in sorted(native):
        if key not in catalog_keys:
            row["extra"].append({"key": key, "owned": key in owned})
    for key in sorted(set(native) | set(generic)):
        # Both shapes: a generic() may drop fields (``cwd``) the app still uses.
        paths = set(_fragile_paths(native.get(key, {})))
        paths |= set(_fragile_paths(generic.get(key, {})))
        for where in sorted(paths):
            row["fragile"].append({"key": key, "path": where})
    if text is not None:
        row["secrets"] = _secret_paths(agent, text, native)
    return row


def _differing_fields(existing: dict[str, Any], desired: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    for name in sorted(desired):
        if name not in existing:
            fields.append(name)
            continue
        if not _matches_value(existing[name], desired[name]):
            fields.append(name)
    return fields


def _matches_value(existing: object, desired: object) -> bool:
    from .editing import _subset_equal

    return _subset_equal(existing, desired)


def _fragile_paths(body: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for where, value in _strings(body, ""):
        top = where.split("[", 1)[0].split(".", 1)[0]
        if top not in {"command", "args", "cwd", "url"}:
            continue
        if _is_fragile(value):
            found.append(where)
    return found


def _is_fragile(value: str) -> bool:
    text = value.strip()
    # A path anywhere in the value, including inside a ``--flag=PATH``.
    for token in [text, *re.split(r"[=\s]", text)]:
        if "/" not in token:
            continue
        if token.startswith(_FRAGILE_PREFIXES):
            return True
        parts = token.split("/")
        if any(_DRAFT_SEGMENT.match(part) for part in parts):
            return True
        if ".Trash" in parts:
            return True
    return False


def _secret_paths(
    agent: str, text: str, servers: dict[str, dict[str, Any]]
) -> list[str]:
    """Key paths whose values look like literal secrets. Values are not kept."""
    spec = SPECS[agent]
    document: object
    if spec.fmt == "json":
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            return []
    elif spec.fmt == "toml":
        try:
            document = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return []
    else:
        # Codec formats (dsh YAML with !!js tags) are scanned through the
        # parsed server map, under a ``servers.`` prefix.
        document = {"servers": servers}
    found: list[str] = []
    for where, value in _strings(document, ""):
        if _looks_secret(where, value):
            found.append(where)
            if len(found) >= MAX_SECRET_PATHS:
                break
    return found


_NAMES_A_VARIABLE = re.compile(
    r"(_env_var|_env|env_?var|_var|_name|_file|_path|file|path|name)$", re.IGNORECASE
)
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_EMBEDDED_REF = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(:-[^}]*)?\}|\{env:[A-Za-z_]")


def _looks_secret(where: str, value: str) -> bool:
    text = value.strip()
    if not text or _REFERENCE.match(text) or classify_env_value(text) != "plain":
        return False
    # ``Bearer ${TOKEN}`` or ``{env:TOKEN}`` inside a value is a reference.
    if _EMBEDDED_REF.search(text):
        return False
    if _SECRET_VALUE.match(text):
        return True
    if "://" in text and (_URL_CREDENTIAL.match(text) or _URL_SECRET_QUERY.search(text)):
        return True
    leaf = re.split(r"[.\[]", where)[-1].rstrip("]").strip('"')
    if not _SECRET_KEY.search(leaf):
        return False
    # ``bearer_token_env_var = "GITHUB_TOKEN"`` names the variable, it is not
    # the secret; so does an ALL_CAPS identifier under a secret-named key.
    if _NAMES_A_VARIABLE.search(leaf) or _ENV_NAME.fullmatch(text):
        return False
    # A secret-named key with a short or path-like value is most likely a
    # flag or a file name (``tokenFile``), not the secret itself.
    if len(text) < 8 or text.startswith(("/", "~", "./")):
        return False
    return True


def _strings(value: object, where: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            child = name if not where else f"{where}.{_segment(name)}"
            yield from _strings(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{where}[{index}]")
    elif isinstance(value, str):
        yield where, value


def _segment(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return name
    return json.dumps(name, ensure_ascii=False)


def _lines(agents: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for agent, row in agents.items():
        if row.get("error"):
            lines.append(f"{agent}: error: {row['error']}")
            continue
        if not row["present"] and not row["missing"]:
            continue
        parts = []
        for key in ("missing", "different", "extra", "fragile", "secrets"):
            if row[key]:
                parts.append(f"{key}={len(row[key])}")
        if row["unrepresentable"]:
            parts.append(f"unrepresentable={len(row['unrepresentable'])}")
        lines.append(f"{agent}: " + (", ".join(parts) if parts else "in sync"))
    return lines
