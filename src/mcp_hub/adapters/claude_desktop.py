"""Claude desktop: ``Library/Application Support/Claude/claude_desktop_config.json``.

Format:

* ``mcpServers`` maps a name to ``{command: str, args?: [str], env?: {str: str}}``
  (plus an app-owned ``extensionId``). The validator requires ``command`` to be
  a string, so a remote ``{url}`` entry is not valid here; remote servers are
  added as connectors in the app UI and stored elsewhere.
* The stdio spawn path merges ``env`` over the inherited environment as
  literal strings. The only substitution is ``${PATH}`` inside ``PATH``. The
  general ``${VAR}`` expander in the bundle is applied to plugin ``.mcp.json``
  configs, not to this file.

So a ``${TOKEN}`` reference written here would reach the server as the literal
text ``${TOKEN}`` and break it silently. Env references are refused unless the
server is named in ``allow_inline_secret`` (the operator accepts that the value
lands in the file as written). Keychain references are refused by the generic
env checks before this module runs.

The rest of the file (``preferences`` and account-keyed siblings the app
rewrites) is left byte for byte by the surgical JSON editor.
"""

from __future__ import annotations

from typing import Any

from ..model import classify_env_value
from .base import is_remote, json_adapter, stdio_object


def native(effective: dict[str, Any]) -> dict[str, Any]:
    return stdio_object(effective)


def cannot(name: str, effective: dict[str, Any], allow: set[str]) -> str | None:
    if is_remote(effective) or not effective["command"]:
        return (
            f"{name}: claude-desktop config only holds stdio servers; "
            "add remote servers as connectors in the Claude app"
        )
    if name not in allow:
        for key, value in effective["env"].items():
            if classify_env_value(value) == "var":
                return (
                    f"{name}: claude-desktop passes env {key} literally "
                    f"(no ${{VAR}} expansion); pass allow_inline_secret to write it"
                )
    return None


SPEC = json_adapter(
    "claude-desktop",
    "Library",
    "Application Support",
    "Claude",
    "claude_desktop_config.json",
    native=native,
    cannot=cannot,
)
