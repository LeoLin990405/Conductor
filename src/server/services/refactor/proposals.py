"""Owner-scoped review and approval for proposed memories."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from src.server.contracts.memory_origin import normalize_memory_metadata
from src.server.contracts.memory_policy_metadata import (
    normalize_memory_policy_metadata_layers,
)
from src.server.contracts.memory_tool_policy import (
    normalize_memory_tool_policy_metadata,
)
from src.server.contracts.refactor import ContractError
from src.server.database.refactor import proposals as db_proposals
from src.server.services.refactor import memory_runtime_controls
from src.server.services.refactor_flags import refactor_flag_enabled

MAX_PROPOSAL_PAGE = 200


async def automatic_memory_enabled(user_id: str) -> bool:
    """Whether the per-user memory gate is open for this user.

    The gate is looked up on the module that defines it, at call time, rather
    than bound here when this module is first imported. A consumer that froze
    the binding would consult whichever version of the gate happened to be
    installed at import time, so the same code could open or close automatic
    memory depending on which module was imported first.
    """
    return await memory_runtime_controls.automatic_memory_enabled(user_id)


def proposal_identity(
    proposal_type: str,
    text: str,
    origin: str = "assistant_inference",
    change_kind: str = "long_term",
    replaces_entry_key: str | None = None,
    reason: str | None = None,
    policy_metadata: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """``(text_hash, proposal_key)`` for one proposal.

    The hash is the idempotency identity; the key is a human-sized store path
    component used when an approval writes the memory file.
    """
    metadata = normalize_memory_metadata({"origin": origin})
    normalized_origin = metadata["origin"]
    identity_text = (
        text
        if normalized_origin == "assistant_inference"
        else f"{normalized_origin}\0{text}"
    )
    replacement = str(replaces_entry_key or "").strip()
    why = str(reason or "").strip()
    policy = normalize_memory_policy_metadata_layers(policy_metadata or {})
    tool_policy = normalize_memory_tool_policy_metadata(policy_metadata or {})
    if policy != {"sensitivity": "private"} or tool_policy:
        identity_text = (
            f"{identity_text}\0{json.dumps({**policy, **tool_policy}, ensure_ascii=False, sort_keys=True)}"
        )
    if change_kind != "long_term" or replacement or why:
        identity_text = (
            f"{identity_text}\0{change_kind}\0{replacement}\0{why}"
        )
    digest = hashlib.sha256(identity_text.encode("utf-8")).hexdigest()
    return digest, f"{proposal_type}-{normalized_origin}-{digest[:12]}"


async def project_proposals(
    *,
    user_id: str,
    thread_id: str,
    task_run_id: str,
    proposals: Any,
    settings_user_id: str | None = None,
) -> int:
    """Record extracted proposals in the review queue (best-effort caller).

    Flag off is a strict no-op. Idempotent per ``(task_run, type, origin-salted hash)``, so a
    retried finalize or a backfill re-extraction cannot duplicate the queue.
    """
    # ``user_id`` is the UUID owner used by projection tables. Local/OSS user
    # sentinels must remain intact for the Settings lookup that gates capture.
    if not await automatic_memory_enabled(settings_user_id or user_id):
        return 0
    if not refactor_flag_enabled("memory_index"):
        return 0
    rows: list[dict[str, Any]] = []
    for proposal in proposals or ():
        proposal_type = str(proposal.get("type") or "").strip()
        text = str(proposal.get("text") or "").strip()
        change_kind = str(proposal.get("change_kind") or "long_term").strip()
        if not text:
            continue
        if change_kind == "once":
            continue
        if change_kind not in ("long_term", "correction"):
            continue
        if change_kind == "correction" and not (
            str(proposal.get("replaces_entry_key") or "").strip()
            and str(proposal.get("reason") or "").strip()
        ):
            continue
        metadata = normalize_memory_metadata(proposal)
        raw_policy: dict[str, Any] = {
            field: proposal[field]
            for field in (
                "sensitivity",
                "applies_when",
                "exceptions",
                "evidence_quote_ref",
            )
            if field in proposal
        }
        if "metadata" in proposal:
            raw_policy["metadata"] = proposal["metadata"]
        policy_metadata = normalize_memory_policy_metadata_layers(raw_policy)
        raw_tool_policy = {
            field: proposal[field]
            for field in ("kind", "denied_tools", "valid_from", "valid_until")
            if field in proposal and proposal[field] is not None
        }
        if "metadata" in proposal:
            raw_tool_policy["metadata"] = proposal["metadata"]
        tool_policy_metadata = normalize_memory_tool_policy_metadata(
            raw_tool_policy
        )
        policy_metadata = {**policy_metadata, **tool_policy_metadata}
        replacement = str(proposal.get("replaces_entry_key") or "").strip() or None
        reason = str(proposal.get("reason") or "").strip() or None
        text_hash, proposal_key = proposal_identity(
            proposal_type,
            text,
            metadata["origin"],
            change_kind,
            replacement,
            reason,
            policy_metadata,
        )
        rows.append(
            {
                "type": proposal_type,
                "text": text,
                "text_hash": text_hash,
                "proposal_key": proposal_key,
                "change_kind": change_kind,
                "replaces_entry_key": replacement,
                "reason": reason,
                "origin": metadata["origin"],
                "confidence": metadata["confidence"],
                "evidence_refs": metadata["evidence_refs"],
                "evidence_count": metadata["evidence_count"],
                "counts_as_evidence": metadata["counts_as_evidence"],
                "metadata": policy_metadata,
            }
        )
    if not rows:
        return 0
    return await db_proposals.insert_proposals(
        user_id=user_id,
        thread_id=thread_id,
        task_run_id=task_run_id,
        proposals=rows,
    )


async def list_pending_proposals(
    *, user_id: str, limit: int = 50
) -> list[dict[str, Any]] | None:
    """Owner-scoped proposals, newest first; ``None`` when the flag is off."""
    if not refactor_flag_enabled("memory_index"):
        return None
    if limit < 1 or limit > MAX_PROPOSAL_PAGE:
        raise ContractError(
            f"proposal page size must be between 1 and {MAX_PROPOSAL_PAGE}"
        )
    rows = await db_proposals.list_proposals(
        user_id=user_id, status="proposed", limit=limit
    )
    proposals: list[dict[str, Any]] = []
    for row in rows:
        created_at = row.get("created_at")
        metadata = normalize_memory_metadata(row)
        policy_metadata = normalize_memory_policy_metadata_layers(row.get("metadata", {}))
        tool_policy_metadata = normalize_memory_tool_policy_metadata(
            row.get("metadata", {})
        )
        proposals.append(
            {
                "proposal_id": row.get("proposal_id"),
                "type": str(row.get("proposal_type") or ""),
                "text": str(row.get("text") or ""),
                "task_run_id": row.get("task_run_id"),
                "thread_id": row.get("thread_id"),
                "status": row.get("status"),
                "proposed_at": str(created_at) if created_at is not None else None,
                "origin": metadata["origin"],
                "confidence": metadata["confidence"],
                "evidence_refs": metadata["evidence_refs"],
                "evidence_count": metadata["evidence_count"],
                "counts_as_evidence": metadata["counts_as_evidence"],
                "metadata": {**policy_metadata, **tool_policy_metadata},
                "change_kind": row.get("change_kind") or "long_term",
                "replaces_entry_key": row.get("replaces_entry_key"),
                "reason": row.get("reason"),
                "consolidation": _consolidation_review_metadata(row),
            }
        )
    return proposals


def _consolidation_review_metadata(row: Mapping[str, Any]) -> dict[str, Any] | None:
    if row.get("producer_kind") != "memory_consolidation":
        return None
    raw = row.get("producer_metadata")
    if not isinstance(raw, Mapping):
        return {"kind": "unknown", "source_count": len(row.get("source_entry_ids") or ())}
    source_count_raw = raw.get("source_count")
    source_count = (
        source_count_raw
        if isinstance(source_count_raw, int) and not isinstance(source_count_raw, bool)
        else len(row.get("source_entry_ids") or ())
    )
    result = {
        "kind": raw.get("kind") if raw.get("kind") in ("duplicate", "conflict") else "unknown",
        "source_count": min(16, max(0, source_count)),
    }
    for field in ("choice_index", "choice_count"):
        value = raw.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 4:
            result[field] = value
    reason = raw.get("conflict_reason")
    if result["kind"] == "conflict" and isinstance(reason, str):
        result["conflict_reason"] = reason[:500]
    return result


async def get_proposal_for_owner(
    *, proposal_id: str, user_id: str
) -> dict[str, Any] | None:
    """The proposal when it belongs to the credential's user, else ``None``."""
    if not refactor_flag_enabled("memory_index"):
        return None
    current = await db_proposals.get_proposal(proposal_id)
    if current is None:
        return None
    from src.server.utils.pg_sanitize import refactor_owner_uuid

    if refactor_owner_uuid(user_id) != current.get("user_id"):
        return None
    return current


async def decide_proposal(
    *,
    proposal_id: str,
    user_id: str,
    approve: bool,
    approval_source: str,
    store: Any | None = None,
) -> dict[str, Any] | None:
    """Decide an owned proposal; approval can only come from a manual action.

    The source guard runs before any Store write so automated callers cannot
    approve a web/tool proposal by routing it through this service.
    """
    if approve and approval_source != "manual":
        raise ContractError("proposal approval requires manual authority")
    if not refactor_flag_enabled("memory_index"):
        return None
    target = "approved" if approve else "rejected"
    approval_result: dict[str, Any] | None = None

    async def write_to_store(current: dict[str, Any]) -> None:
        nonlocal approval_result
        if store is None:
            raise ContractError("proposal approval store is unavailable")
        from src.server.services.refactor.proposal_approval import approve_proposal

        try:
            approval_result = await approve_proposal(
                proposal=current,
                store=store,
                owner_id=user_id,
                approval_source=approval_source,
            )
        except ValueError as exc:
            raise ContractError(str(exc)) from exc

    decided = await db_proposals.decide_proposal(
        proposal_id=proposal_id,
        user_id=user_id,
        expected_status="proposed",
        target_status=target,
        approval_source=approval_source,
        before_decide=write_to_store if approve else None,
    )
    if decided is None:
        raise ContractError(f"proposal {proposal_id} not found")
    if decided.get("status") != target:
        raise ContractError(
            f"proposal is already {decided.get('status')!r}; only 'proposed' can be decided"
        )
    if (
        approve
        and decided.get("producer_kind") == "memory_consolidation"
        and approval_result is not None
    ):
        decided["approval"] = approval_result
    return decided
