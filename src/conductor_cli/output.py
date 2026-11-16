"""Stable --json envelope, shell completion scripts, and CLI version lookup."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

from .build_info import package_version
from .build_info import version_line as identity_line
from .client import json_dumps_safe, sanitize_terminal

API_VERSION = "v1"


def cli_version() -> str:
    """Return the installed distribution version, or a visible fallback.

    The identity of a build -- its version *and* the tree it was built from
    -- lives in :mod:`build_info`. This stays the plain version string, so
    a caller that wants to compare versions does not pay for a git call.
    """
    return package_version()


def version_line(name: str = "conductor") -> str:
    """The line ``--version`` prints: ``conductor 0.1.0 (1a2b3c4d, dirty)``.

    The format lives in :mod:`build_info`, which ``cond --version`` can run
    without this module's imports.
    """
    return identity_line(name)


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def command_label(args: argparse.Namespace) -> str:
    """Flatten the parsed command path (``skills preset save``) for the envelope."""
    parts = [str(getattr(args, "command", "") or "")]
    for dest in _SUBCOMMAND_DESTS:
        value = getattr(args, dest, None)
        if value:
            parts.append(str(value))
    if getattr(args, "command", None) == "dispatch":
        # dispatch has no subparsers: the literal goal/status word is the split.
        words = getattr(args, "args", None) or []
        first = words[0] if words else getattr(args, "target", None)
        if first == "status":
            parts.append("status")
    return " ".join(part for part in parts if part)


_SUBCOMMAND_DESTS = (
    "auth_command",
    "tickets_command",
    "sources_command",
    "daemons_command",
    "agents_command",
    "models_command",
    "deliberation_command",
    "memory_command",
    "logs_command",
    "github_command",
    "skills_command",
    "mcp_command",
    "instructions_command",
    "preset_command",
    "project_command",
    "deploy_command",
    "lines_command",
    "review_command",
    "workspace_command",
)


def build_envelope(
    command: str,
    *,
    data: Any = None,
    errors: list[dict[str, str]] | None = None,
    exit_code: int = 0,
) -> dict[str, Any]:
    return {
        "api_version": API_VERSION,
        "command": command,
        "timestamp": utc_timestamp(),
        "data": data,
        "errors": errors,
        "exit_code": exit_code,
    }


def envelope_json(envelope: dict[str, Any]) -> str:
    # Both the command path and the server-provided payload can carry control
    # characters; keep them escaped rather than emitted to a terminal.
    return json_dumps_safe(envelope)


def human_render(value: Any) -> str:
    """Render a result for a terminal; mirrors the pre-envelope behaviour.

    Only a non-empty ``lines`` list is treated as log text: an empty list
    (e.g. a fake dispatch record) must still print the rest of the payload
    instead of exiting 0 with no output. Untrusted text is sanitized at this
    output boundary so server-controlled escape sequences cannot reach the
    terminal.
    """
    if (
        isinstance(value, dict)
        and isinstance(value.get("lines"), list)
        and value["lines"]
    ):
        return "\n".join(sanitize_terminal(str(line)) for line in value["lines"])
    if value is None:
        return "OK"
    if isinstance(value, (dict, list)):
        return json_dumps_safe(value)
    return sanitize_terminal(str(value))


# ---------------------------------------------------------------------------
# Shell completion
# ---------------------------------------------------------------------------


def _parser_tree(parser: argparse.ArgumentParser) -> dict[str, list[str]]:
    tree: dict[str, list[str]] = {}
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public walk
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, sub in action.choices.items():
            subs: set[str] = set()
            for sub_action in sub._actions:  # noqa: SLF001
                if isinstance(sub_action, argparse._SubParsersAction):
                    subs.update(sub_action.choices.keys())
            existing = set(tree.get(name, ()))
            tree[name] = sorted(existing | subs)
    return dict(sorted(tree.items()))


def build_completion(shell: str, parser: argparse.ArgumentParser) -> str:
    tree = _parser_tree(parser)
    commands = sorted(tree)
    if shell == "bash":
        return _bash_completion(commands, tree)
    if shell == "zsh":
        return _zsh_completion(commands, tree)
    if shell == "fish":
        return _fish_completion(commands, tree)
    raise ValueError(f"unsupported shell: {shell}")


def _bash_completion(commands: list[str], tree: dict[str, list[str]]) -> str:
    lines = [
        "_conductor() {",
        '    local cur="${COMP_WORDS[COMP_CWORD]}"',
        '    local cmd="${COMP_WORDS[1]}"',
        "    if [ \"$COMP_CWORD\" -eq 1 ]; then",
        f'        COMPREPLY=( $(compgen -W "{" ".join(commands)}" -- "$cur") )',
        "        return 0",
        "    fi",
        '    case "$cmd" in',
    ]
    for name, subs in tree.items():
        if not subs:
            continue
        lines.append(f"        {name})")
        lines.append(f'            COMPREPLY=( $(compgen -W "{" ".join(subs)}" -- "$cur") )')
        lines.append("            ;;")
    lines.extend(
        [
            "    esac",
            "    return 0",
            "}",
            'complete -F _conductor conductor',
            "",
        ]
    )
    return "\n".join(lines)


def _zsh_completion(commands: list[str], tree: dict[str, list[str]]) -> str:
    lines = ["#compdef conductor", "", "_conductor() {", "    local cur=${words[CURRENT]}", "    if (( CURRENT == 2 )); then"]
    quoted = " ".join(commands)
    lines.append(f"        _values 'conductor command' {quoted}")
    lines.append("        return")
    lines.append("    fi")
    lines.append('    case "${words[2]}" in')
    for name, subs in tree.items():
        if not subs:
            continue
        lines.append(f"        {name})")
        lines.append(f"            _values '{name} subcommand' {' '.join(subs)}")
        lines.append("            ;;")
    lines.extend(["    esac", "}", "", '_conductor "$@"', ""])
    return "\n".join(lines)


def _fish_completion(commands: list[str], tree: dict[str, list[str]]) -> str:
    lines = [
        "# conductor fish completion",
        "complete -c conductor -f -n '__fish_use_subcommand' -a 'auth' -h 'manage CLI authentication'",
    ]
    # Full command list with generic help; help text is cosmetic for fish.
    for name in commands:
        if name == "auth":
            continue
        lines.append(
            f"complete -c conductor -f -n '__fish_use_subcommand' -a '{name}'"
        )
    for name, subs in tree.items():
        if not subs:
            continue
        for sub in subs:
            lines.append(
                f"complete -c conductor -f -n '__fish_seen_subcommand_from {name}' -a '{sub}'"
            )
    lines.append("")
    return "\n".join(lines)
