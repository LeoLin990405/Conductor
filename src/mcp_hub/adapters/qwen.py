"""Qwen Code: ``~/.qwen/settings.json``, server map under ``mcpServers``.

Shape, read from the qwen-code 0.25.1 bundle (a Gemini CLI fork):

* ``MCPServerConfig`` carries ``command``/``args``/``env``/``cwd`` (stdio),
  ``url`` (SSE), ``httpUrl`` (streamable HTTP), ``headers``, ``timeout``,
  ``trust`` and friends. ``createTransport`` picks ``httpUrl`` first, then
  ``url`` as SSE, then ``command``. A ``type`` field is not consulted for
  the transport, so http must be written as ``httpUrl`` and sse as ``url``.
* stdio ``env`` is merged over the inherited environment. Settings values go
  through ``resolveEnvVarsInObject`` (``$VAR`` and ``${VAR}``), so a
  ``${NAME}`` reference is expanded by qwen itself.
* There is no per-entry enable flag: qwen disables a server by listing it in
  the top-level ``mcp.excluded`` array. The hub therefore never writes
  ``enabled``; releasing a disabled server deletes its owned key.
* qwen reads the file with ``strip-json-comments`` and rewrites it when
  ``$version`` is missing or old. The hub's JSON editor refuses commented
  files rather than drop the comments, and keeps ``$version`` byte for byte.
"""

from __future__ import annotations

from typing import Any

from .base import is_remote, json_adapter, stdio_object


def native(effective: dict[str, Any]) -> dict[str, Any]:
    if is_remote(effective):
        url = str(effective["url"])
        if str(effective["transport"]) == "sse":
            return {"url": url}
        return {"httpUrl": url}
    return stdio_object(effective)


def cannot(name: str, effective: dict[str, Any], allow: set[str]) -> str | None:
    if is_remote(effective):
        if effective["env"]:
            return f"{name}: qwen remote servers have no env field"
        if effective["args"]:
            return f"{name}: qwen remote servers cannot carry args"
        return None
    if not effective["command"]:
        return f"{name}: qwen needs a command, or an http/sse url"
    return None


def generic(body: dict[str, Any]) -> dict[str, Any]:
    """One qwen entry in the hub's neutral field names (for import/drift).

    Mirrors qwen's own precedence: ``httpUrl`` > ``url`` (SSE) > ``command``.
    """
    out: dict[str, Any] = {}
    http_url = body.get("httpUrl")
    sse_url = body.get("url")
    if isinstance(http_url, str) and http_url:
        out["transport"] = "http"
        out["url"] = http_url
    elif isinstance(sse_url, str) and sse_url:
        out["transport"] = "sse"
        out["url"] = sse_url
    else:
        out["transport"] = "stdio"
        for key in ("command", "args", "env"):
            if key in body:
                out[key] = body[key]
    return out


SPEC = json_adapter(
    "qwen", ".qwen", "settings.json", native=native, cannot=cannot, generic=generic
)
