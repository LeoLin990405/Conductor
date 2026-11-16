"""Gemini CLI: ``~/.gemini/settings.json``, servers under ``mcpServers``.

Shape (gemini-cli 0.65 ``SETTINGS_SCHEMA_DEFINITIONS.MCPServerConfig``,
``additionalProperties: false``): ``command``, ``args``, ``env``, ``cwd``,
``url`` + ``type`` (``"http"`` streamable or ``"sse"``), deprecated
``httpUrl``, ``headers``, ``tcp``, ``timeout`` (milliseconds; used as the
connect and per-request timeout), ``trust``, ``description``,
``includeTools``/``excludeTools``, ``oauth``, ``authProviderType`` and the
Google auth fields. There is no per-server ``enabled`` flag: disabling is the
top-level ``mcp.excluded`` list, which the hub does not manage, so releasing a
disabled server deletes the key. ``$VAR``/``${VAR}`` strings are expanded both
when settings load and again for a stdio ``env`` value.

The projection:

* stdio -> ``{"command", "args", "env"?}`` (no ``type``, so hand entries
  written without one still match under subset semantics);
* http / sse -> ``{"type": transport, "url"}`` (the non-deprecated form;
  ``httpUrl`` is never written);
* ``startup_timeout_sec`` -> ``timeout`` in milliseconds.

Gemini reads ``url`` before ``command`` and only passes ``env`` to a stdio
process, so a remote server with env refs is refused rather than written with
env Gemini would silently ignore. Plaintext env and keychain references are
refused by the generic checks in ``render`` before ``cannot`` runs.
"""

from __future__ import annotations

from typing import Any

from .base import is_remote, json_adapter, stdio_object


def _timeout_ms(effective: dict[str, Any]) -> int | None:
    timeout = effective.get("startup_timeout_sec")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        return None
    return timeout * 1000


def native(effective: dict[str, Any]) -> dict[str, Any]:
    if is_remote(effective):
        obj: dict[str, Any] = {
            "type": str(effective["transport"]),
            "url": str(effective["url"]),
        }
    else:
        obj = stdio_object(effective)
    timeout = _timeout_ms(effective)
    if timeout is not None:
        obj["timeout"] = timeout
    return obj


def cannot(name: str, effective: dict[str, Any], allow: set[str]) -> str | None:
    transport = str(effective["transport"])
    if transport in {"http", "sse"}:
        if not effective["url"]:
            return f"{name}: gemini {transport} server needs a url"
        if effective["env"]:
            return f"{name}: gemini only passes env to stdio servers"
        return None
    if not effective["command"]:
        return f"{name}: gemini stdio server needs a command"
    return None


def generic(body: dict[str, Any]) -> dict[str, Any]:
    """One Gemini entry in neutral field names (for import and drift).

    Gemini's precedence: ``httpUrl`` (streamable http, deprecated) > ``url``
    (``type`` http or sse; sse when unset) > ``command``. ``timeout`` is in
    milliseconds.
    """
    out: dict[str, Any] = {}
    http_url = body.get("httpUrl")
    url = body.get("url")
    kind = body.get("type")
    if isinstance(http_url, str) and http_url:
        out.update(transport="http", url=http_url)
    elif isinstance(url, str) and url:
        out.update(transport=kind if kind in {"http", "sse"} else "sse", url=url)
    else:
        out["transport"] = "stdio"
        for key in ("command", "args", "env"):
            if key in body:
                out[key] = body[key]
    timeout = body.get("timeout")
    if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
        out["startup_timeout_sec"] = timeout // 1000
    return out


SPEC = json_adapter(
    "gemini",
    ".gemini",
    "settings.json",
    native=native,
    cannot=cannot,
    generic=generic,
)
