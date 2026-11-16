"""Closed memory-origin and evidence rules shared by memory writers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, get_args

from src.server.contracts.memory_validity import confidence_value

MemoryOrigin = Literal["user", "tool", "web", "assistant_inference"]
MEMORY_ORIGINS: tuple[MemoryOrigin, ...] = get_args(MemoryOrigin)

_EVIDENCE_KINDS = frozenset({"user_message", "tool_call", "web_source"})
_MAX_EVIDENCE_REFS = 64
_MAX_EVIDENCE_ID = 256


def normalize_memory_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    default_origin: str = "assistant_inference",
    trust_evidence_refs: bool = False,
) -> dict[str, Any]:
    """Normalize origin claims and derive user evidence without trusting counts.

    Callers that cannot verify a source keep its ID but it remains explicitly
    unverified. Only a verified user-message reference corroborates a user-origin
    fact; tool/web provenance and approval never add user evidence.
    """
    normalized = dict(metadata or {}) if isinstance(metadata, Mapping) else {}
    fallback = default_origin if default_origin in MEMORY_ORIGINS else "assistant_inference"
    raw_origin = normalized.get("origin")
    origin: MemoryOrigin = (
        raw_origin if raw_origin in MEMORY_ORIGINS else fallback  # type: ignore[assignment]
    )

    confidence = confidence_value(normalized)
    if confidence is None:
        confidence = 0.5
    if origin == "assistant_inference":
        confidence = min(confidence, 0.5)

    raw_refs = normalized.get("evidence_refs")
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    if isinstance(raw_refs, Sequence) and not isinstance(raw_refs, (str, bytes)):
        for raw_ref in raw_refs[:_MAX_EVIDENCE_REFS]:
            if not isinstance(raw_ref, Mapping):
                continue
            kind = str(raw_ref.get("kind") or "").strip()
            source_id = str(raw_ref.get("id") or "").strip()
            if kind not in _EVIDENCE_KINDS or not source_id or len(source_id) > _MAX_EVIDENCE_ID:
                continue
            identity = (kind, source_id)
            if identity in seen:
                continue
            seen.add(identity)
            verified = bool(trust_evidence_refs and raw_ref.get("verified") is True)
            refs.append({"kind": kind, "id": source_id, "verified": verified})

    evidence_count = (
        sum(1 for ref in refs if ref["kind"] == "user_message" and ref["verified"])
        if origin == "user"
        else 0
    )
    normalized.update(
        {
            "origin": origin,
            "confidence": confidence,
            "evidence_refs": refs,
            "evidence_count": evidence_count,
            "counts_as_evidence": evidence_count > 0,
        }
    )
    return normalized
