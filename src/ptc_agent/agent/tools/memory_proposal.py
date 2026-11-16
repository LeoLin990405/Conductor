"""A subagent's proposal channel for durable memory.

The tool records nothing: it marks an intent that the server extracts from the
task transcript after the run, so a subagent can suggest a memory without
gaining a write path into the user's memory (docs/refactor/0008 D5).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from langchain_core.tools import tool

#: Mirrors ``server.contracts.refactor.MEMORY_PROPOSAL_TYPES``; a cross-layer
#: test pins the two in sync (the agent layer must not import the server package).
PROPOSAL_TYPES = ("user", "feedback", "project", "reference")
class _NamedToolDenial(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool"]
    name: str


class _MCPToolDenial(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["mcp"]
    server: str
    tool: str


DeniedTool = Annotated[
    _NamedToolDenial | _MCPToolDenial,
    Field(discriminator="type"),
]


@tool("ProposeMemory", parse_docstring=True)
def propose_memory(
    memory_type: str,
    text: str,
    change_kind: Literal["once", "long_term", "correction"] = "long_term",
    replaces_entry_key: str | None = None,
    reason: str | None = None,
    origin: Literal["user", "tool", "web", "assistant_inference"] = "assistant_inference",
    evidence_refs: list[dict[str, str]] | None = None,
    sensitivity: Literal["public", "private", "secret"] = "private",
    applies_when: str | None = None,
    exceptions: str | None = None,
    evidence_quote_ref: dict[str, Any] | None = None,
    kind: Literal[
        "preference", "constraint", "thesis", "decision", "fact", "entity", "event"
    ] | None = None,
    denied_tools: list[DeniedTool] | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
) -> str:
    """Keep a task-only instruction in this conversation or offer a durable memory for review.

    This never writes or reads memory. Use once for task-only instructions; they
    stay in this conversation and are not queued. Use long_term for durable
    changes, and correction when an existing memory is wrong. Durable kinds
    require manual approval before being saved.

    Args:
        memory_type: One of user, feedback, project, reference.
        text: One self-contained durable statement (at most 1000 characters).
        change_kind: once, long_term, or correction; defaults to long_term.
        replaces_entry_key: Existing owner memory filename to replace; required for correction.
        reason: Why the old entry is superseded or wrong; required for correction.
        origin: Where the proposed fact came from; omitted values mean assistant_inference.
        evidence_refs: Optional source IDs only; the server treats them as unverified.
        sensitivity: public, private, or secret; secret memories are excluded from recall.
        applies_when: Optional bounded condition for when the memory applies.
        exceptions: Optional bounded exception that limits when the memory applies.
        evidence_quote_ref: Optional message ID and Unicode codepoint offsets; it points to source text but does not include it.
        kind: Optional memory kind. Set constraint only when the owner should review an explicit tool restriction.
        denied_tools: Exact typed identifiers blocked for the full approved validity window; each item is either {type: tool, name: ExactName} or {type: mcp, server: ExactServer, tool: ExactTool}. Applies_when and exceptions are descriptive and do not narrow these denials.
        valid_from: Optional ISO date or timestamp when an approved restriction starts.
        valid_until: Optional ISO date or timestamp when an approved restriction expires.
    """
    if change_kind == "once":
        return f"Temporary task instruction noted for this conversation only: {text}"
    decision = memory_type if memory_type in PROPOSAL_TYPES else "user"
    del origin, replaces_entry_key, reason, evidence_refs, sensitivity
    del applies_when, exceptions, evidence_quote_ref, kind, denied_tools
    del valid_from, valid_until
    return f"Memory proposal recorded ({decision}); it will be reviewed before it is saved."
