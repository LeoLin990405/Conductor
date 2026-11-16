"""Adapter contract shared by the per-app modules in this package.

An adapter is an ``AgentSpec`` (see ``mcp_hub.agents``). JSON apps reuse the
surgical JSON editor and only supply ``native`` (registry fields -> the app's
server object) and, when needed, ``cannot`` (why a server cannot be written).
A format that is neither JSON nor TOML supplies a :class:`Codec`.

Every function here is pure: no filesystem access, no process HOME.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..agents import AgentSpec


class Codec(Protocol):
    """Text <-> server map for one non-JSON, non-TOML config format.

    ``parse`` returns ``{file_key: native_body}`` where ``native_body`` has the
    same shape ``AgentSpec.native`` produces, so ``matches`` can compare them.
    ``generic`` maps one native body to the hub's neutral field names
    (``transport``, ``command``, ``args``, ``url``, ``env``, ``enabled``,
    ``startup_timeout_sec``) for import and drift.
    ``edit`` must copy every byte it does not manage, and raise
    ``McpHubError`` rather than produce text ``parse`` cannot read back.
    """

    def parse(self, text: str) -> dict[str, dict[str, Any]]: ...

    def generic(self, body: dict[str, Any]) -> dict[str, Any]: ...

    def matches(self, existing: dict[str, Any], desired: dict[str, Any]) -> bool: ...

    def edit(
        self,
        original: str,
        desired: dict[str, dict[str, Any]],
        remove: set[str],
    ) -> str: ...

    def fresh(self, desired: dict[str, dict[str, Any]]) -> str: ...


def stdio_object(effective: dict[str, Any]) -> dict[str, Any]:
    """``{"command", "args"?, "env"?}``: the common Claude-desktop-style shape.

    Empty ``args`` is left out, so a hand-written entry without ``args``
    matches instead of being rewritten to add ``"args": []``.
    """
    obj: dict[str, Any] = {"command": str(effective["command"])}
    if effective["args"]:
        obj["args"] = list(effective["args"])
    env = dict(effective["env"])
    if env:
        obj["env"] = env
    return obj


def is_remote(effective: dict[str, Any]) -> bool:
    return str(effective["transport"]) in {"http", "sse"} and bool(effective["url"])


def json_adapter(
    agent: str,
    *relative: str,
    native,
    cannot=None,
    container: tuple[str, ...] = ("mcpServers",),
    has_env: bool = True,
    generic=None,
) -> AgentSpec:
    return AgentSpec(
        agent=agent,
        relative=tuple(relative),
        fmt="json",
        container=container,
        style=agent,
        native=native,
        cannot=cannot,
        has_env=has_env,
        generic=generic,
    )
