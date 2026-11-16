"""Antigravity: ``~/.gemini/config/mcp_config.json``, servers under ``mcpServers``.

Shape, from the documentation and config struct shipped in the app's
language server (``Antigravity.app/Contents/Resources/bin/language_server``):

* stdio: ``{"command", "args"?, "env"?}`` (``cwd`` also exists; the hub does
  not write it).
* remote: ``{"url"}`` over Streamable HTTP. ``serverUrl`` is a legacy alias
  the app still reads; the hub writes ``url``. Legacy HTTP+SSE is not
  supported by the app, so an ``sse`` server is refused.
* other per-server keys the app reads and the hub never writes: ``headers``,
  ``disabled``, ``disabledTools``, ``enabledTools``, ``timeoutSeconds``,
  ``oauth``. A hand-set key survives an owned rewrite (JSON edits merge).

There is no ``type`` key; files in the wild sometimes carry ``"type":
"stdio"`` and the app ignores it, so the hub neither writes nor needs it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..model import classify_env_value
from .base import json_adapter, stdio_object


def _remote(effective: dict[str, Any]) -> bool:
    return str(effective["transport"]) in {"http", "sse"} and bool(effective["url"])


def native(effective: dict[str, Any]) -> dict[str, Any]:
    if _remote(effective):
        return {"url": str(effective["url"])}
    return stdio_object(effective)


def cannot(name: str, effective: dict[str, Any], allow: set[str]) -> str | None:
    for key, value in effective["env"].items():
        if classify_env_value(value) == "keychain" and name not in allow:
            return f"{name}: antigravity cannot represent a keychain reference for {key}"
    if _remote(effective):
        if str(effective["transport"]) == "sse":
            return f"{name}: antigravity does not support legacy SSE; use http"
        if effective["env"]:
            return f"{name}: antigravity remote servers have no env field"
        if effective["args"]:
            return f"{name}: antigravity remote servers have no args field"
        return None
    if not effective["command"]:
        return f"{name}: antigravity needs a command or an http url"
    return None


def generic(body: dict[str, Any]) -> dict[str, Any]:
    """One Antigravity entry in neutral field names (for import and drift).

    ``serverUrl`` is the legacy spelling of ``url``; ``disabled: true`` is the
    app's off switch.
    """
    out: dict[str, Any] = {}
    url = body.get("url") or body.get("serverUrl")
    if isinstance(url, str) and url:
        out.update(transport="http", url=url)
    else:
        out["transport"] = "stdio"
        for key in ("command", "args", "env"):
            if key in body:
                out[key] = body[key]
    if isinstance(body.get("disabled"), bool):
        out["enabled"] = not body["disabled"]
    return out


def disable(body: dict[str, Any]) -> dict[str, Any]:
    """Antigravity's off switch is ``disabled: true``, not ``enabled``."""
    out = dict(body)
    out["disabled"] = True
    return out


SPEC = json_adapter(
    "antigravity",
    ".gemini",
    "config",
    "mcp_config.json",
    native=native,
    cannot=cannot,
    generic=generic,
)
SPEC = replace(SPEC, disable=disable)
