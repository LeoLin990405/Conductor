"""Shared SuperAgent prototype contract.

Implementations live in sibling packages (``reference``, ``reference_bridge``,
``p_python``, ``n_pi``, ``c_native``) and are compared by
``tests/prototypes/superagent``.

Names resolve lazily so a runtime child can import
``prototypes.superagent.bridge`` without loading the product tool registry.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "CREDIT_STOP_TEXT",
    "FAKE_TOOL_HANDLERS",
    "SHELL_OR_FILE_TOOLS",
    "CreditDecision",
    "EventSubscriber",
    "FakeCreditGate",
    "FakeMemoryRecall",
    "FakePostgresStore",
    "MemoryHit",
    "RuntimeContext",
    "ScriptedChunk",
    "ScriptedFakeChatModel",
    "SessionRecord",
    "StreamEvent",
    "SuperAgentAdapter",
    "Transcript",
    "TranscriptEntry",
    "autonomy_tool_names",
    "canonical_role",
    "content_texts",
    "is_shell_or_file_tool",
    "message_texts",
    "normalize_tool_name",
    "offered_tool_name",
    "scripted_tool_call_text",
    "scripted_tool_result",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        return getattr(importlib.import_module("prototypes.superagent.contract"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
