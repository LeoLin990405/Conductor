"""Super-agent P-lane runtime core.

Public names only. Sibling modules import langchain_core and the stdlib;
this package init does not import any server module.
"""

from __future__ import annotations

__all__ = [
    "SHELL_OR_FILE_TOOLS",
    "SUPERVISOR_SYSTEM_PROMPT",
    "WATCHDOG_SYSTEM_PROMPT",
    "CreditPort",
    "CreditVerdict",
    "EventSink",
    "LoopEvent",
    "RecallPort",
    "ToolPort",
    "TurnInput",
    "TurnResult",
    "is_shell_or_file_tool",
    "normalize_tool_name",
    "run_turn",
]


def __getattr__(name: str):
    if name in {
        "CreditPort",
        "CreditVerdict",
        "EventSink",
        "LoopEvent",
        "RecallPort",
        "ToolPort",
        "TurnInput",
        "TurnResult",
    }:
        from . import ports as _ports

        return getattr(_ports, name)
    if name in {
        "SHELL_OR_FILE_TOOLS",
        "is_shell_or_file_tool",
        "normalize_tool_name",
    }:
        from . import tools as _tools

        return getattr(_tools, name)
    if name == "run_turn":
        from .loop import run_turn

        return run_turn
    if name in {"SUPERVISOR_SYSTEM_PROMPT", "WATCHDOG_SYSTEM_PROMPT"}:
        from . import prompts as _prompts

        return getattr(_prompts, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
