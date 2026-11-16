"""Structured subagent result derived from durable records.

The subagent contract has ``findings``/``artifact_ids``/``open_questions`` beside
the summary. This module fills them from what the run already left behind, so a
projection needs no new agent tool and no prompt change:

- ``artifact_ids``: files the run successfully wrote/edited (Write/Edit tool calls
  matched to their non-error tool messages) — machine-checkable evidence of what
  the run produced;
- ``findings``: the summary's bullet lines (the agent's own enumerated results),
  bounded;
- ``open_questions``: bullet lines under an "open questions" heading;
- ``evidence`` stays empty: artifact paths already carry the checkable evidence,
  and nothing else in the transcript is a trustworthy evidence pointer.

It is a conservative projection, not a parser that invents structure: no bullets
means no findings, and a nested tool call that failed is not an artifact.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

from src.server.contracts.memory_origin import normalize_memory_metadata
from src.server.contracts.memory_policy_metadata import (
    normalize_memory_policy_metadata,
)
from src.server.contracts.memory_tool_policy import (
    MemoryToolPolicyError,
    normalize_memory_tool_policy_metadata,
)
from src.server.contracts.refactor import MEMORY_PROPOSAL_TYPES

MAX_FINDINGS = 20
MAX_OPEN_QUESTIONS = 10
MAX_ARTIFACTS = 50
MAX_PROPOSALS = 10
MAX_ITEM_CHARS = 500
MAX_PROPOSAL_TEXT = 1000

_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.+?)\s*$")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s*(.+?)\s*$")
_OPEN_QUESTIONS_RE = re.compile(r"open\s+questions?|questions?|未决问题|待定问题", re.IGNORECASE)
_WRITE_TOOLS = frozenset({"Write", "Edit"})
_PROPOSAL_TOOL = "ProposeMemory"


@dataclass(frozen=True)
class StructuredSubagentResult:
    summary: str
    findings: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    proposed_memories: tuple[dict[str, Any], ...] = ()


def _text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part).strip()
    return ""


def _clean(item: str) -> str:
    return item.strip().strip("*_` ").strip()[:MAX_ITEM_CHARS]


def _bullets(text: str) -> list[str]:
    found: list[str] = []
    for line in (text or "").splitlines():
        match = _BULLET_RE.match(line)
        if not match:
            continue
        cleaned = _clean(match.group(1))
        if cleaned:
            found.append(cleaned)
    return found


def _findings(text: str) -> list[str]:
    """Bullets outside an open-questions section (those are not results)."""
    items: list[str] = []
    in_open_questions = False
    for line in (text or "").splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            in_open_questions = bool(_OPEN_QUESTIONS_RE.search(heading.group(1)))
            continue
        if in_open_questions:
            continue
        match = _BULLET_RE.match(line)
        if not match:
            continue
        cleaned = _clean(match.group(1))
        if cleaned:
            items.append(cleaned)
    return items


def _open_questions(text: str) -> list[str]:
    """Bullet lines under the last heading that names questions."""
    items: list[str] = []
    in_section = False
    for line in (text or "").splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            in_section = bool(_OPEN_QUESTIONS_RE.search(heading.group(1)))
            continue
        if not in_section:
            continue
        match = _BULLET_RE.match(line)
        if match:
            cleaned = _clean(match.group(1))
            if cleaned:
                items.append(cleaned)
    return items


def _successful_tool_call_ids(messages: Sequence[Any]) -> set[str]:
    succeeded: set[str] = set()
    for message in messages:
        if getattr(message, "type", None) != "tool":
            continue
        status = getattr(message, "status", None)
        if isinstance(status, str) and status.lower() == "error":
            continue
        if getattr(message, "error", None):
            continue
        call_id = getattr(message, "tool_call_id", None)
        if isinstance(call_id, str) and call_id:
            succeeded.add(call_id)
    return succeeded


def _artifact_paths(messages: Sequence[Any]) -> list[str]:
    succeeded = _successful_tool_call_ids(messages)
    paths: list[str] = []
    seen: set[str] = set()
    for message in messages:
        if getattr(message, "type", None) != "ai":
            continue
        for call in getattr(message, "tool_calls", None) or ():
            if not isinstance(call, dict):
                continue
            if call.get("name") not in _WRITE_TOOLS:
                continue
            if str(call.get("id") or "") not in succeeded:
                continue
            args = call.get("args")
            if not isinstance(args, dict):
                continue
            named = args.get("file_path") or args.get("filePath") or args.get("path") or args.get("filename")
            if not isinstance(named, str) or not named.strip():
                continue
            path = named.strip()
            if path in seen:
                continue
            seen.add(path)
            paths.append(path)
    return paths


def _proposed_memories(messages: Sequence[Any]) -> list[dict[str, Any]]:
    """Valid ``ProposeMemory`` calls from the run's transcript, newest last."""
    proposals: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for message in messages:
        if getattr(message, "type", None) != "ai":
            continue
        for call in getattr(message, "tool_calls", None) or ():
            if not isinstance(call, dict) or call.get("name") != _PROPOSAL_TOOL:
                continue
            args = call.get("args")
            if not isinstance(args, dict):
                continue
            proposal_type = str(args.get("memory_type") or args.get("type") or "").strip()
            text = str(args.get("text") or "").strip()
            change_kind = str(args.get("change_kind") or "long_term").strip()
            if change_kind == "once":
                # The tool result keeps task-only state in the live conversation.
                # Do not copy it into the durable contract or proposal queue.
                continue
            if proposal_type not in MEMORY_PROPOSAL_TYPES:
                continue
            if change_kind not in ("long_term", "correction"):
                continue
            if not text or len(text) > MAX_PROPOSAL_TEXT:
                continue
            metadata = normalize_memory_metadata(
                {
                    "origin": args.get("origin", "assistant_inference"),
                    "confidence": args.get("confidence", 0.5),
                    "evidence_refs": args.get("evidence_refs", ()),
                }
            )
            raw_policy = {"sensitivity": args.get("sensitivity", "private")}
            for field in ("applies_when", "exceptions", "evidence_quote_ref"):
                if args.get(field) is not None:
                    raw_policy[field] = args[field]
            policy_metadata = normalize_memory_policy_metadata(raw_policy)
            raw_tool_policy = {
                field: args[field]
                for field in ("kind", "denied_tools", "valid_from", "valid_until")
                if field in args and args[field] is not None
            }
            try:
                tool_policy_metadata = normalize_memory_tool_policy_metadata(
                    raw_tool_policy
                )
            except MemoryToolPolicyError:
                continue
            policy_metadata.update(tool_policy_metadata)
            replacement = str(args.get("replaces_entry_key") or "").strip()
            reason = str(args.get("reason") or "").strip()
            key = (
                proposal_type,
                text,
                metadata["origin"],
                change_kind,
                replacement,
                reason,
                json.dumps(policy_metadata, ensure_ascii=False, sort_keys=True),
            )
            if key in seen:
                continue
            seen.add(key)
            proposals.append(
                {
                    "type": proposal_type,
                    "text": text,
                    "change_kind": change_kind,
                    "replaces_entry_key": replacement or None,
                    "reason": reason or None,
                    "origin": metadata["origin"],
                    "confidence": metadata["confidence"],
                    "evidence_refs": metadata["evidence_refs"],
                    "evidence_count": metadata["evidence_count"],
                    "counts_as_evidence": metadata["counts_as_evidence"],
                    "metadata": policy_metadata,
                }
            )
    return proposals[:MAX_PROPOSALS]


def build_structured_result(
    *, summary: str, messages: Sequence[Any] = ()
) -> StructuredSubagentResult:
    """The contract fields a completed run's records support."""
    text = (summary or "").strip()
    return StructuredSubagentResult(
        summary=text,
        findings=tuple(_findings(text)[:MAX_FINDINGS]),
        evidence=(),
        artifact_ids=tuple(_artifact_paths(messages)[:MAX_ARTIFACTS]),
        open_questions=tuple(_open_questions(text)[:MAX_OPEN_QUESTIONS]),
        proposed_memories=tuple(_proposed_memories(messages)),
    )
