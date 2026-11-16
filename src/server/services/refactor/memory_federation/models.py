"""Shared, source-tagged value objects for federated memory retrieval."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from types import MappingProxyType
from typing import Any, Mapping

from src.server.contracts.memory_origin import normalize_memory_metadata
from src.server.contracts.memory_policy_metadata import (
    normalize_memory_policy_metadata_layers,
)


def _frozen_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class MemoryHit:
    source: str
    key: str
    text: str
    score: float
    provenance: Mapping[str, Any] = field(default_factory=dict)
    tier: str | None = None
    workspace_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not isinstance(self.key, str):
            raise TypeError("memory hit source and key must be strings")
        if not self.source.strip():
            raise ValueError("memory hit source must be non-empty")
        if not self.key.strip():
            raise ValueError("memory hit key must be non-empty")
        if not isinstance(self.text, str):
            raise TypeError("memory hit text must be a string")
        if self.tier is not None and not isinstance(self.tier, str):
            raise TypeError("memory hit tier must be a string or null")
        if self.workspace_id is not None and not isinstance(self.workspace_id, str):
            raise TypeError("memory hit workspace_id must be a string or null")
        score = float(self.score)
        if not isfinite(score):
            raise ValueError("memory hit score must be finite")
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "provenance", _frozen_mapping(self.provenance))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "key": self.key,
            "text": self.text,
            "score": self.score,
            "provenance": dict(self.provenance),
            "tier": self.tier,
            "workspace_id": self.workspace_id,
        }


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """A proposed memory payload; providers decide whether they can write it."""

    key: str
    text: str
    tier: str
    workspace_id: str | None = None
    thread_id: str | None = None
    task_run_id: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)
    origin: str | None = None
    confidence: float | None = None
    evidence_refs: tuple[Mapping[str, Any], ...] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not isinstance(self.tier, str):
            raise TypeError("memory entry key and tier must be strings")
        if not self.key.strip():
            raise ValueError("memory entry key must be non-empty")
        if not self.tier.strip():
            raise ValueError("memory entry tier must be non-empty")
        if not isinstance(self.text, str):
            raise TypeError("memory entry text must be a string")
        if self.workspace_id is not None and not isinstance(self.workspace_id, str):
            raise TypeError("memory entry workspace_id must be a string or null")
        if self.thread_id is not None and not isinstance(self.thread_id, str):
            raise TypeError("memory entry thread_id must be a string or null")
        if self.task_run_id is not None and not isinstance(self.task_run_id, str):
            raise TypeError("memory entry task_run_id must be a string or null")
        provenance = dict(self.provenance or {})
        metadata_input = dict(provenance)
        if self.origin is not None:
            metadata_input["origin"] = self.origin
        if self.confidence is not None:
            metadata_input["confidence"] = self.confidence
        if self.evidence_refs is not None:
            metadata_input["evidence_refs"] = self.evidence_refs
        metadata = normalize_memory_metadata(metadata_input)
        object.__setattr__(self, "origin", metadata["origin"])
        object.__setattr__(self, "confidence", metadata["confidence"])
        object.__setattr__(self, "evidence_refs", tuple(metadata["evidence_refs"]))
        object.__setattr__(self, "provenance", _frozen_mapping(metadata))
        policy_source = dict(provenance)
        policy_source["metadata"] = self.metadata
        policy_metadata = normalize_memory_policy_metadata_layers(policy_source)
        object.__setattr__(self, "metadata", _frozen_mapping(policy_metadata))


@dataclass(frozen=True, slots=True)
class MemoryProposal:
    proposal_id: str
    kind: str
    target_ids: tuple[str, ...]
    status: str
    actor: str | None = None
    created_at: datetime | None = None
    decision_at: datetime | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.proposal_id, str) or not isinstance(self.kind, str):
            raise TypeError("memory proposal id and kind must be strings")
        if not isinstance(self.status, str):
            raise TypeError("memory proposal status must be a string")
        if not all(isinstance(value, str) for value in self.target_ids):
            raise TypeError("memory proposal target_ids must contain strings")
        if not self.proposal_id.strip():
            raise ValueError("memory proposal id must be non-empty")
        if not self.kind.strip():
            raise ValueError("memory proposal kind must be non-empty")
        if not self.status.strip():
            raise ValueError("memory proposal status must be non-empty")
        object.__setattr__(self, "target_ids", tuple(self.target_ids))
        object.__setattr__(self, "provenance", _frozen_mapping(self.provenance))
