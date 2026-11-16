"""Refusal policy for the Super-agent P lane.

Copied from ``prototypes.superagent.contract.SHELL_OR_FILE_TOOLS`` and the
refusal branch of ``prototypes.superagent.p_python.tools.handle_tool_request``
so the production loop never imports the prototype package. The loop applies
it to every call before anything reaches the ``ToolPort``; allowed names are
the role's tool objects.
"""

from __future__ import annotations

import re

# Names that must never be executed, even if a model asks for them.
# ``autonomy_read`` is a record read, not a file read, so it is not here.
# Capitalized names are built-in tools of native agent CLIs (Claude Code
# and peers): shell, file, notebook, web, and subagent access.
SHELL_OR_FILE_TOOLS = frozenset(
    {
        "apply_patch",
        "bash",
        "computer",
        "delete_file",
        "edit_file",
        "exec",
        "execute",
        "execute_code",
        "read_file",
        "run_terminal_cmd",
        "sh",
        "shell",
        "str_replace",
        "str_replace_editor",
        "write_file",
        "Bash",
        "Edit",
        "Glob",
        "Grep",
        "LS",
        "MultiEdit",
        "NotebookEdit",
        "Read",
        "Task",
        "WebFetch",
        "WebSearch",
        "Write",
    }
)

_SHELL_OR_FILE_FOLDED = frozenset(name.casefold() for name in SHELL_OR_FILE_TOOLS)

# ``mcp__<server>__<tool>`` is how MCP-backed CLIs name a server's tools.
_MCP_PREFIX = re.compile(r"^mcp__[^_].*?__(?=[^_])")


def normalize_tool_name(name: str) -> str:
    """Strip an MCP ``mcp__<server>__`` prefix. Case is kept."""

    return _MCP_PREFIX.sub("", name, count=1)


def is_shell_or_file_tool(name: str) -> bool:
    """True for a shell, file, web, or subagent tool, in any letter case.

    ``Bash``, ``BASH``, and ``mcp__fs__write_file`` all match.
    """

    return normalize_tool_name(name).casefold() in _SHELL_OR_FILE_FOLDED
