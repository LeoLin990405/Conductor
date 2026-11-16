"""The ``/roles`` command (R6 L1): show and edit the role registry from the TUI.

``/roles`` prints the four roles; ``/roles set <role> <field> <value> reason
<text>`` changes one field (``default``, ``candidates`` as a comma list) with
the reason the server stores in its audit. The server validates every change.
"""

from __future__ import annotations

from typing import Any

ROLES = ("router", "planner", "reviewer", "worker")
FIELDS = ("default", "candidates")
_MIN_SET_WORDS = 6  # set <role> <field> <value> reason <text>
USAGE = "usage: /roles | /roles set <role> default|candidates <value> reason <text>"


class RolesUsageError(ValueError):
    """The command line does not parse; the message is the usage line."""


def parse(rest: str) -> tuple[str, str | None, dict[str, Any] | None]:
    """``("show", None, None)`` or ``("set", role, body)``."""
    words = rest.split()
    if not words or words[0] == "show":
        return "show", None, None
    if words[0] != "set" or len(words) < _MIN_SET_WORDS or words[4] != "reason":
        raise RolesUsageError(USAGE)
    role, field, value = words[1], words[2], words[3]
    if role not in ROLES or field not in FIELDS:
        raise RolesUsageError(USAGE)
    reason = " ".join(words[5:]).strip()
    if not reason:
        raise RolesUsageError(USAGE)
    body: dict[str, Any] = {"reason": reason}
    if field == "candidates":
        body["candidates"] = [name for name in value.split(",") if name]
    else:
        body["default"] = value
    return "set", role, body


def render(payload: dict[str, Any] | None) -> list[str]:
    """One line per role plus the hard rules."""
    if not isinstance(payload, dict):
        return ["role registry unavailable"]
    head = f"role registry ({payload.get('source', '?')}, revision {payload.get('revision', 0)})"
    lines = [head]
    roles = payload.get("roles") if isinstance(payload.get("roles"), dict) else {}
    for role in ROLES:
        entry = roles.get(role) or {}
        constraints = entry.get("constraints") or {}
        flags = [
            name
            for name in ("read_only", "may_write", "may_execute", "requires_session_resume")
            if constraints.get(name)
        ]
        candidates = ", ".join(entry.get("candidates") or []) or "-"
        lines.append(
            f"  {role:<9} default {entry.get('default') or '-'}; candidates {candidates}"
            + (f"; {' '.join(flags)}" if flags else "")
        )
    rules = payload.get("hard_rules") or []
    if rules:
        lines.append("  hard rules: " + ", ".join(str(rule) for rule in rules))
    return lines
