"""Conductor's PostgreSQL memory projection as a federation provider."""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from typing import Any

from src.server.contracts.refactor import MEMORY_PROPOSAL_TYPES, MEMORY_TIERS
from src.server.contracts.memory_policy_metadata import (
    normalize_memory_policy_metadata_layers,
)
from src.server.database.refactor import memory as db_memory
from src.server.database.refactor import proposals as db_proposals
from src.server.services.refactor.memory_projection import is_current
from src.server.services.refactor.memory_dual_write import memory_tier_for_namespace
from src.server.services.refactor.proposals import proposal_identity
from src.server.services.refactor_flags import refactor_flag_enabled
from src.server.utils.pg_sanitize import normalize_uuid, refactor_owner_uuid

from .models import MemoryEntry, MemoryHit
from .provider import DEFAULT_PROVIDER_TIMEOUT_SECONDS

#: Share of the federation's per-provider budget a tier's hybrid recall may
#: spend. The rest is margin for the lexical path and fusion, so a slow
#: embedding round-trip degrades recall instead of discarding the rows already
#: retrieved or overrunning the provider timeout.
_RECALL_BUDGET_RATIO = 0.75


class ConductorPgMemory:
    """Read active projection rows without triggering embedding writes."""

    def __init__(
        self,
        *,
        recall_budget_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        if (
            not isinstance(recall_budget_seconds, (int, float))
            or isinstance(recall_budget_seconds, bool)
            or not math.isfinite(recall_budget_seconds)
            or recall_budget_seconds <= 0
        ):
            raise ValueError("recall budget must be positive")
        self._recall_deadline_seconds = recall_budget_seconds * _RECALL_BUDGET_RATIO

    async def recall(
        self,
        user_id: str,
        query: str,
        k: int,
        tiers: Sequence[str],
        workspace_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[MemoryHit]:
        if k < 1 or not query.strip():
            return []
        normalized_owner = refactor_owner_uuid(user_id)
        if normalized_owner is None:
            return []
        # Lazy import keeps the SQL/embedding modules out of this adapter's import
        # cycle while still routing every tier through the semantic path.
        from src.server.services.refactor.memory_embeddings import hybrid_search

        deadline = time.monotonic() + self._recall_deadline_seconds
        hits: list[MemoryHit] = []
        for tier in dict.fromkeys(str(value) for value in tiers):
            if tier not in MEMORY_TIERS:
                continue
            if tier in {"session", "task"} and not thread_id:
                continue
            scoped_thread = thread_id if tier in {"session", "task"} else None
            # ``workspace`` and ``project`` share a namespace; passing the id lets
            # the SQL layer scope before LIMIT. With no workspace (Chief-of-Staff),
            # the tiers stay unscoped like the baseline rather than disappearing.
            scoped_workspace = workspace_id if tier in {"workspace", "project"} else None
            # Use the hybrid path so opting into federation keeps Conductor's
            # semantic retrieval and its existing embeddings. Pass the one
            # absolute deadline for the whole recall: hybrid_search charges
            # each tier's lexical query to it and still returns that tier's
            # lexical rows when the optional stage runs out.
            rows = await hybrid_search(
                user_id=user_id,
                query=query,
                tier=tier,
                thread_id=scoped_thread,
                workspace_id=scoped_workspace,
                limit=min(k, 100),
                semantic_deadline=deadline,
            )
            rank = 0
            for row in rows:
                if row.get("user_id") is not None and str(row["user_id"]) != normalized_owner:
                    continue
                if row.get("tier") is not None and str(row["tier"]) != tier:
                    continue
                namespace = row.get("namespace")
                if not isinstance(namespace, (list, tuple)):
                    continue
                scoped = memory_tier_for_namespace(namespace)
                if scoped is None:
                    continue
                namespace_tier, namespace_owner, namespace_workspace = scoped
                if refactor_owner_uuid(namespace_owner) != normalized_owner:
                    continue
                if tier == "user" and namespace_tier != "user":
                    continue
                if tier in {"session", "task"} and (
                    namespace_tier != "session"
                    or len(namespace) < 3
                    or normalize_uuid(namespace[2]) != normalize_uuid(thread_id)
                ):
                    continue
                if tier in {"workspace", "project"}:
                    # Project memories currently share the workspace namespace.
                    if namespace_tier != "workspace":
                        continue
                    if workspace_id is not None:
                        expected_workspace = normalize_uuid(workspace_id)
                        if (
                            expected_workspace is None
                            or normalize_uuid(namespace_workspace) != expected_workspace
                        ):
                            continue
                if row.get("status") != "active":
                    continue
                metadata = row.get("metadata")
                if metadata is not None and not isinstance(metadata, dict):
                    continue
                row_provenance = row.get("provenance")
                if row_provenance is not None and not isinstance(row_provenance, dict):
                    continue
                if not is_current(metadata if isinstance(metadata, dict) else None):
                    continue
                policy_layers = [
                    normalize_memory_policy_metadata_layers(value)
                    for value in (metadata, row_provenance)
                    if isinstance(value, dict)
                ]
                if any(layer["sensitivity"] == "secret" for layer in policy_layers):
                    continue
                key = str(row.get("entry_key") or "").strip()
                if not key or key.startswith("proposed/"):
                    continue
                text = str(row.get("index_text") or "").strip()
                if not text:
                    continue
                provenance: dict[str, Any] = (
                    dict(row_provenance) if isinstance(row_provenance, dict) else {}
                )
                if isinstance(metadata, dict):
                    provenance["memory_metadata"] = dict(metadata)
                if row.get("content_hash"):
                    provenance["content_hash"] = str(row["content_hash"])
                if row.get("entry_id"):
                    provenance["entry_id"] = str(row["entry_id"])
                hits.append(
                    MemoryHit(
                        source="conductor_pg",
                        key=key,
                        text=text,
                        score=1.0 / (rank + 1),
                        provenance=provenance,
                        tier=tier,
                        workspace_id=scoped_workspace,
                    )
                )
                rank += 1
        tier_order = {tier: index for index, tier in enumerate(MEMORY_TIERS)}
        hits.sort(
            key=lambda hit: (
                tier_order.get(hit.tier or "", len(tier_order)),
                -hit.score,
                hit.key,
            )
        )
        return hits[:k]

    async def propose(self, user_id: str, entry: MemoryEntry) -> str:
        """Queue a typed candidate for review; approval writes user memory."""
        if not refactor_flag_enabled("memory_index"):
            raise RuntimeError("Conductor memory proposals are disabled")
        proposal_type = str(entry.tier or "").strip()
        if proposal_type not in MEMORY_PROPOSAL_TYPES:
            raise ValueError(
                "proposal tier must be one of user, feedback, project, reference"
            )
        if entry.workspace_id is not None:
            raise ValueError(
                "Conductor proposal approval currently targets user memory only"
            )
        if not entry.thread_id or not entry.task_run_id:
            raise ValueError("Conductor proposals require thread_id and task_run_id")
        text = entry.text.strip()
        if not text or len(text) > 1000:
            raise ValueError("proposal text must contain 1 to 1000 characters")
        policy_metadata = normalize_memory_policy_metadata_layers(entry.metadata)
        text_hash, proposal_key = proposal_identity(
            proposal_type, text, entry.origin, policy_metadata=policy_metadata
        )
        proposal_id = await db_proposals.insert_proposal_for_run(
            user_id=user_id,
            thread_id=entry.thread_id,
            task_run_id=entry.task_run_id,
            proposal_type=proposal_type,
            proposal_key=proposal_key,
            text=text,
            text_hash=text_hash,
            origin=entry.origin,
            confidence=entry.confidence,
            evidence_refs=entry.evidence_refs,
            metadata=policy_metadata,
        )
        if proposal_id is None:
            raise ValueError(
                "proposal run is invalid, incomplete, or owned by another user"
            )
        return proposal_id

    async def notify_forget(
        self, *, user_id: str, entry_id: str, content_hash: str
    ) -> str:
        """Confirm the local projection row is gone after forget orchestration."""
        del content_hash
        row = await db_memory.get_entry_for_forget(
            user_id=user_id, entry_id=entry_id
        )
        return "deleted" if row is None else "unavailable"
