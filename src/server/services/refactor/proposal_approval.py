"""Store-writing approval for a memory proposal (D5).

Approval must change the **source of truth** (the LangGraph store), not just the
review queue: it writes one detail memory file and appends its pointer line to
``memory.md``, then the projection follows through the same dual-write mapping a
live memory write would use. Rejection is status-only.

Each approved proposal gets an immutable detail key derived from its proposal
identity and id. Retries reuse that revision, and the owner index remains the
single pointer to the active revision.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import hashlib
import re
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Mapping

import yaml

from src.server.contracts.memory_origin import normalize_memory_metadata
from src.server.contracts.memory_policy_metadata import (
    normalize_memory_policy_metadata_layers,
)
from src.server.contracts.memory_tool_policy import (
    normalize_memory_tool_policy_metadata,
)
from src.server.services.refactor.memory_projection import parse_memory_index

logger = logging.getLogger(__name__)

_MISSING_STORE_VALUE = object()


async def _snapshot_store_values(store, namespace, keys):
    """Exact pre-activation Store values for the keys this approval mutates.

    Projection is the durable lineage forget walks; if it fails after the Store
    has been materialized, the activation must be undone rather than leave an
    active revision outside the database closure.
    """
    snapshot = {}
    for key in keys:
        if not key:
            continue
        # A read failure aborts before any mutation: treating it as "missing"
        # would let the rollback delete a key that still exists.
        item = await store.aget(tuple(namespace), key)
        snapshot[key] = item.value if item is not None else _MISSING_STORE_VALUE
    return snapshot


async def _restore_store_values(store, namespace, snapshot):
    for key, value in snapshot.items():
        try:
            if value is _MISSING_STORE_VALUE:
                await store.adelete(tuple(namespace), key)
            else:
                await store.aput(tuple(namespace), key, value)
        except Exception:
            logger.error(
                "[refactor] could not roll back Store value after failed "
                "activation projection key=%s",
                key,
                exc_info=True,
            )
            raise RuntimeError(
                "activation rollback failed after projection error"
            ) from None


@asynccontextmanager
async def _activation_guard(
    store,
    namespace,
    *,
    user_id,
    store_snapshot,
    projection_keys,
    projection_snapshot,
    projected_entry_ids,
    policy_snapshot=None,
):
    """Compensate any activation failure, including cancellation, then re-raise."""
    try:
        yield
    except BaseException:
        # Any failure after the activation started (including ordinary Store
        # errors and cancellation) must leave no half-materialized revision.
        await _restore_store_values(store, namespace, store_snapshot)
        await _rollback_projection(
            user_id=user_id,
            namespace=namespace,
            projection_keys=projection_keys,
            projection_snapshot=projection_snapshot,
            projected_entry_ids=projected_entry_ids,
            policy_snapshot=policy_snapshot,
        )
        raise


async def _rollback_projection(
    user_id: str,
    namespace,
    projection_keys,
    projection_snapshot,
    projected_entry_ids,
    policy_snapshot=None,
) -> None:
    """Restore projection and committed policy authority to the snapshots."""
    from src.server.database.refactor import memory as db_memory
    from src.server.database.refactor import memory_tool_policy as db_policy
    from src.server.services.refactor.memory_dual_write import (
        memory_tier_for_namespace,
    )

    if policy_snapshot is not None:
        policy_entry_ids = {
            str(row.get("entry_id"))
            for row in policy_snapshot
            if row.get("entry_id")
        }
        policy_entry_ids.update(str(value) for value in projected_entry_ids)
        await db_policy.restore_denials_snapshot(
            user_id=user_id,
            rows=list(policy_snapshot),
            entry_ids=sorted(policy_entry_ids),
        )
    tier = memory_tier_for_namespace(tuple(namespace))
    if tier is None:
        return
    await db_memory.restore_projection_rows(
        user_id=user_id,
        tier=tier[0],
        entry_keys=list(projection_keys),
        rows=list(projection_snapshot),
        remove_entry_ids=list(projected_entry_ids),
    )


MEMORY_INDEX_KEY = "memory.md"
PROPOSED_PREFIX = "proposed/"
MAX_TITLE_CHARS = 60
MAX_DESCRIPTION_CHARS = 150
_DETAIL_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<metadata>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(?P<body>.*)\Z",
    re.DOTALL,
)
_SAFE_KEY = re.compile(r"[^A-Za-z0-9._-]+")


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def _base_key(proposal: Mapping[str, Any]) -> str:
    """A safe memory-root filename for an approved proposal.

    The old flow wrote approvals under ``proposed/``; approved memories now move
    to the root so retrieval and injection can tell them apart from candidates.
    """
    key = str(proposal.get("proposal_key") or "").strip()
    if key.startswith(PROPOSED_PREFIX):
        key = key[len(PROPOSED_PREFIX):]
    key = key.removesuffix(".md")
    key = _SAFE_KEY.sub("-", key).strip(".-")
    if not key:
        # Never fatal: a keyless row still gets a deterministic-enough identity.
        key = f"proposal-{str(proposal.get('proposal_id') or 'x')[:8]}"
    return key


def _detail_key(proposal: Mapping[str, Any]) -> str:
    suffix = _SAFE_KEY.sub("-", str(proposal.get("proposal_id") or "revision"))[:40]
    return f"{_base_key(proposal)}--r{suffix}.md"


def _legacy_detail_key(proposal: Mapping[str, Any]) -> str:
    return f"{PROPOSED_PREFIX}{_base_key(proposal)}.md"


def _detail_content(
    proposal: Mapping[str, Any],
    *,
    now: str,
    source_entry_id: str | None = None,
    lifecycle: str = "active",
    preserve_body: bool = False,
    supersedes_entry_keys: Sequence[str] = (),
    supersedes_entry_ids: Sequence[str] = (),
    retrieval_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    raw_text = proposal.get("text")
    text = str(raw_text or "") if preserve_body else str(raw_text or "").strip()
    description = _one_line(text)[:MAX_DESCRIPTION_CHARS]
    proposal_type = str(proposal.get("proposal_type") or "user")
    metadata = normalize_memory_metadata(proposal, trust_evidence_refs=False)
    policy = normalize_memory_policy_metadata_layers(proposal.get("metadata", {}))
    tool_policy = normalize_memory_tool_policy_metadata(proposal.get("metadata", {}))
    frontmatter = {
        "description": description,
        "type": proposal_type,
        "origin": metadata["origin"],
        "confidence": metadata["confidence"],
        "evidence_refs": metadata["evidence_refs"],
        "evidence_count": metadata["evidence_count"],
        "counts_as_evidence": metadata["counts_as_evidence"],
        "lifecycle": lifecycle,
        "change_kind": str(proposal.get("change_kind") or "long_term"),
    }
    frontmatter.update(policy)
    if isinstance(retrieval_metadata, Mapping):
        for field in ("kind", "importance", "valid_from", "valid_until", "tags", "entities"):
            if field in retrieval_metadata:
                frontmatter[field] = retrieval_metadata[field]
    # The reviewed proposal's typed restriction and frozen window outrank
    # incidental retrieval metadata. Conditions/exceptions remain descriptive
    # and never narrow a denied tool's scope.
    frontmatter.update(tool_policy)
    replaces = str(proposal.get("replaces_entry_key") or "").strip()
    if replaces:
        frontmatter["replaces_entry_key"] = replaces
        frontmatter["supersedes"] = [replaces]
    reason = str(proposal.get("reason") or "").strip()
    if reason:
        frontmatter["reason"] = reason[:1000]
    if source_entry_id:
        frontmatter["source_entry_id"] = source_entry_id
    if supersedes_entry_ids:
        frontmatter["supersedes_entry_ids"] = list(supersedes_entry_ids)
    if supersedes_entry_keys:
        frontmatter["supersedes"] = list(supersedes_entry_keys)
    try:
        source_thread_id = str(uuid.UUID(str(proposal.get("thread_id"))))
    except (ValueError, TypeError, AttributeError):
        source_thread_id = None
    if source_thread_id:
        frontmatter["source_thread_id"] = source_thread_id
    try:
        source_response_id = str(uuid.UUID(str(proposal.get("source_response_id"))))
    except (ValueError, TypeError, AttributeError):
        source_response_id = None
    if source_response_id:
        frontmatter["source_response_id"] = source_response_id
    try:
        source_task_run_id = str(uuid.UUID(str(proposal.get("task_run_id"))))
    except (ValueError, TypeError, AttributeError):
        source_task_run_id = None
    if source_task_run_id:
        frontmatter["source_task_run_id"] = source_task_run_id
    yaml_text = yaml.safe_dump(
        frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).rstrip()
    separator = "---\n" if preserve_body else "---\n\n"
    body = text if preserve_body else f"{text}\n"
    content = f"---\n{yaml_text}\n{separator}{body}"
    return {
        "content": content,
        "encoding": "utf-8",
        "created_at": now,
        "modified_at": now,
    }


def _matches_proposal(content: str, expected_content: str) -> bool:
    """Recognize a retry's own active or staged revision without trusting the key alone."""
    def parts(value: str) -> tuple[dict[str, Any], str] | None:
        match = _DETAIL_FRONTMATTER_RE.match(value)
        if not match:
            return None
        try:
            metadata = yaml.safe_load(match.group("metadata")) or {}
        except yaml.YAMLError:
            return None
        return (
            (dict(metadata), match.group("body"))
            if isinstance(metadata, Mapping)
            else None
        )

    actual = parts(content)
    expected = parts(expected_content)
    if actual is None or expected is None:
        return False
    actual_meta, actual_body = actual
    expected_meta, expected_body = expected
    if actual_meta.get("lifecycle") not in ("pending", "active"):
        return False
    actual_meta.pop("lifecycle", None)
    expected_meta.pop("lifecycle", None)
    return actual_meta == expected_meta and actual_body == expected_body


def _pointer_line(proposal: Mapping[str, Any], key: str) -> str:
    title = _one_line(str(proposal.get("text") or ""))[:MAX_TITLE_CHARS]
    hook = _one_line(str(proposal.get("text") or ""))[:MAX_DESCRIPTION_CHARS]
    return f"- [{title}]({key}) — {hook}"


def _drop_pointer(content: str, key: str) -> str:
    """Remove a pointer line that names ``key`` (the pre-promotion proposed path)."""
    kept = [
        line
        for line in (content or "").splitlines()
        if not (line.strip().startswith("- [") and f"]({key})" in line)
    ]
    return ("\n".join(kept).strip() + "\n") if kept else ""


def _content_of(item: Any) -> str:
    value = getattr(item, "value", None) if item is not None else None
    content = value.get("content") if isinstance(value, Mapping) else None
    return content if isinstance(content, str) else ""


def _annotate_lifecycle(content: str, *, lifecycle: str, reason: str | None) -> str:
    """Retain the exact old body while recording why it left the active index."""
    match = _DETAIL_FRONTMATTER_RE.match(content)
    if match:
        raw = yaml.safe_load(match.group("metadata")) or {}
        metadata = dict(raw) if isinstance(raw, Mapping) else {}
        body = match.group("body")
    else:
        metadata = {}
        body = content
    metadata["lifecycle"] = lifecycle
    if reason:
        metadata["reason"] = reason[:1000]
    yaml_text = yaml.safe_dump(
        metadata, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).rstrip()
    return f"---\n{yaml_text}\n---\n{body}"


def _annotate_consolidation_lifecycle(content: str, *, lifecycle: str) -> str:
    """Mark a source revision while retaining its original reason for retries."""
    parsed = _detail_frontmatter(content)
    if parsed is None:
        raise ValueError("consolidation source frontmatter is malformed")
    metadata, body = parsed
    metadata["lifecycle"] = lifecycle
    metadata["lifecycle_reason"] = "consolidated_duplicate_or_conflict"
    yaml_text = yaml.safe_dump(
        metadata, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).rstrip()
    return f"---\n{yaml_text}\n---\n{body}"


def _replacement_key(proposal: Mapping[str, Any]) -> str | None:
    key = str(proposal.get("replaces_entry_key") or "").strip()
    if not key:
        return None
    if (
        len(key) > 512
        or key in (".", "..", MEMORY_INDEX_KEY)
        or "/" in key
        or "\\" in key
        or key.startswith(PROPOSED_PREFIX)
        or not key.endswith(".md")
    ):
        raise ValueError("replacement target must be an owner memory detail filename")
    return key


def _detail_frontmatter(content: str) -> tuple[dict[str, Any], str] | None:
    match = _DETAIL_FRONTMATTER_RE.match(content)
    if match is None:
        return None
    try:
        raw = yaml.safe_load(match.group("metadata")) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(raw, Mapping):
        return None
    return dict(raw), match.group("body")


def _valid_consolidation_key(value: object) -> str | None:
    key = str(value or "")
    if (
        not key
        or len(key) > 512
        or not key.endswith(".md")
        or key in (".", "..", MEMORY_INDEX_KEY)
        or "/" in key
        or "\\" in key
    ):
        return None
    return key


async def _approve_consolidation_proposal(
    *, proposal: Mapping[str, Any], store: Any, user_id: str
) -> dict[str, Any]:
    """Replace a verified source group only inside an explicit manual approval."""
    from src.server.services.store_backends.langgraph_store import user_memory_namespace
    from src.server.app._store_helpers import aget, aput
    from src.server.contracts.memory_policy_metadata import normalize_memory_policy_metadata_layers
    from src.server.contracts.memory_validity import is_current
    from src.server.database.refactor import memory_consolidation as db_consolidation
    from src.server.database.refactor import memory_tool_policy as db_tool_policy
    from src.server.services.refactor.memory_consolidation import (
        _policy_from_row,
        _preserved_memory_metadata,
    )
    from src.server.services.refactor.memory_projection import (
        content_hash,
        parse_memory_metadata,
    )
    from src.server.services.refactor.memory_dual_write import project_memory_write

    source_ids_raw = proposal.get("source_entry_ids")
    if not isinstance(source_ids_raw, (list, tuple)) or not 2 <= len(source_ids_raw) <= 16:
        raise ValueError("consolidation source set is invalid")
    source_ids = [str(uuid.UUID(str(value))) for value in source_ids_raw]
    if len(set(source_ids)) != len(source_ids) or source_ids != sorted(source_ids):
        raise ValueError("consolidation source set contains duplicates")
    if await db_tool_policy.has_active_denials(
        user_id=user_id, entry_ids=source_ids
    ):
        raise ValueError("consolidation cannot replace an owner-approved tool constraint")
    audit = proposal.get("producer_metadata")
    if proposal.get("producer_kind") != "memory_consolidation" or not isinstance(audit, Mapping):
        raise ValueError("consolidation audit metadata is missing")
    group_kind = audit.get("kind")
    if group_kind not in ("duplicate", "conflict"):
        raise ValueError("consolidation group kind is invalid")
    source_signature = str(audit.get("source_signature") or "")
    source_body_hashes_raw = audit.get("source_body_hashes")
    if not isinstance(source_body_hashes_raw, (list, tuple)) or len(source_body_hashes_raw) != len(source_ids):
        raise ValueError("consolidation source body hashes are missing")
    source_body_hashes = [str(value) for value in source_body_hashes_raw]
    if any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in source_body_hashes):
        raise ValueError("consolidation source body hashes are invalid")
    body_hash_by_id = dict(zip(source_ids, source_body_hashes, strict=True))
    rows = await db_consolidation.get_source_entries_for_retry(
        user_id=user_id, source_entry_ids=source_ids
    )
    rows.sort(key=lambda item: str(item.get("entry_id") or ""))
    if len(rows) != len(source_ids):
        raise ValueError("consolidation source set is stale")
    expected_hashes: dict[str, str] = {}
    try:
        for item in source_signature.split(","):
            entry_id, digest = item.split(":", 1)
            normalized_id = str(uuid.UUID(entry_id))
            if not digest or normalized_id in expected_hashes:
                raise ValueError
            expected_hashes[normalized_id] = digest
    except (ValueError, AttributeError):
        raise ValueError("consolidation source signature is invalid") from None
    if set(source_ids) != {str(row.get("entry_id")) for row in rows} or set(expected_hashes) != set(source_ids):
        raise ValueError("consolidation source version changed")

    namespace = tuple(user_memory_namespace(user_id))
    keys: list[str] = []
    canonical: list[tuple[dict[str, Any], str, str]] = []
    expected_policy = normalize_memory_policy_metadata_layers(proposal.get("metadata"))
    expected_memory_metadata = audit.get("memory_metadata")
    if not isinstance(expected_memory_metadata, Mapping):
        raise ValueError("consolidation retrieval metadata is missing")
    audited_keys_raw = audit.get("source_entry_keys")
    if not isinstance(audited_keys_raw, (list, tuple)) or len(audited_keys_raw) != len(source_ids):
        raise ValueError("consolidation source keys are missing")
    audited_keys = [_valid_consolidation_key(value) for value in audited_keys_raw]
    if any(key is None for key in audited_keys) or len(set(audited_keys)) != len(audited_keys):
        raise ValueError("consolidation source keys are invalid")
    keys = [str(key) for key in audited_keys]
    detail_key = _detail_key(proposal)
    if detail_key in keys:
        raise ValueError("consolidation revision must use a new key")
    now = datetime.now(timezone.utc).isoformat()
    detail_args = {
        "proposal": proposal,
        "now": now,
        "source_entry_id": source_ids[0],
        "lifecycle": "pending",
        "preserve_body": True,
        "supersedes_entry_keys": keys,
        "supersedes_entry_ids": source_ids,
        "retrieval_metadata": dict(expected_memory_metadata),
    }
    staged_detail = _detail_content(**detail_args)
    active_detail = _detail_content(**{**detail_args, "lifecycle": "active"})
    existing = await aget(store, namespace, detail_key)
    existing_content = _content_of(existing)
    if existing_content and not (
        _matches_proposal(existing_content, staged_detail["content"])
        or _matches_proposal(existing_content, active_detail["content"])
    ):
        raise ValueError("immutable consolidation revision already has different content")
    index_item = await aget(store, namespace, MEMORY_INDEX_KEY)
    index_content = _content_of(index_item)
    pointer_map = parse_memory_index(index_content)
    revision_is_indexed = detail_key in pointer_map
    retry_proven = bool(
        revision_is_indexed
        and existing_content
        and (
            _matches_proposal(existing_content, staged_detail["content"])
            or _matches_proposal(existing_content, active_detail["content"])
        )
    )
    if revision_is_indexed and not retry_proven:
        raise ValueError("consolidation retry revision is not in its own staged state")
    for row in rows:
        key = _valid_consolidation_key(row.get("entry_key"))
        if key is None:
            raise ValueError("consolidation source key is invalid")
        if key != keys[len(canonical)]:
            raise ValueError("consolidation source key set changed")
        if tuple(str(part) for part in row.get("namespace") or ()) != namespace:
            raise ValueError("consolidation sources cross a memory namespace")
        row_status = str(row.get("status") or "")
        if row_status not in ("active", "superseded"):
            raise ValueError("consolidation source status changed")
        row_metadata = dict(row.get("metadata")) if isinstance(row.get("metadata"), Mapping) else {}
        row_lifecycle = row_metadata.pop("lifecycle", None)
        if row_status == "active" and row_lifecycle not in (None, "active"):
            raise ValueError("consolidation source status changed")
        if row_status == "superseded" and row_lifecycle not in ("superseded", "wrong"):
            raise ValueError("consolidation source status changed")
        if not is_current(row_metadata):
            raise ValueError("consolidation source validity expired")
        if row_metadata.get("kind") == "constraint" or "denied_tools" in row_metadata:
            raise ValueError("consolidation cannot replace tool constraints")
        row_policy = _policy_from_row(row.get("metadata"))
        row_memory_metadata = _preserved_memory_metadata(row.get("metadata"))
        if row_policy != expected_policy or row_memory_metadata != dict(expected_memory_metadata):
            raise ValueError("consolidation source metadata changed")
        item = await aget(store, namespace, key)
        content = _content_of(item)
        if not content:
            raise ValueError("consolidation source content changed")
        parsed = _detail_frontmatter(content)
        if parsed is None:
            raise ValueError("consolidation source frontmatter is malformed")
        frontmatter, body = parsed
        if hashlib.sha256(body.encode("utf-8")).hexdigest() != body_hash_by_id.get(
            str(row.get("entry_id"))
        ):
            raise ValueError("consolidation source body changed")
        lifecycle = frontmatter.get("lifecycle")
        already_retired = lifecycle in ("superseded", "wrong")
        if already_retired:
            if (
                not retry_proven
                or row_status not in ("active", "superseded")
                or frontmatter.get("lifecycle_reason")
                != "consolidated_duplicate_or_conflict"
            ):
                raise ValueError("consolidation source is no longer active")
            if row_status == "superseded" and content_hash(content) != str(
                row.get("content_hash") or ""
            ):
                raise ValueError("consolidation source projection changed")
        elif lifecycle not in (None, "active"):
            raise ValueError("consolidation source is no longer active")
        elif (
            row_status != "active"
            or content_hash(content) != expected_hashes.get(str(row.get("entry_id")))
        ):
            raise ValueError("consolidation source version changed")
        actual_policy = _policy_from_row(parse_memory_metadata(content))
        actual_memory_metadata = _preserved_memory_metadata(parse_memory_metadata(content))
        if actual_policy != expected_policy or actual_memory_metadata != dict(expected_memory_metadata):
            raise ValueError("consolidation source frontmatter changed")
        canonical.append((frontmatter, body, content))

    bodies = [item[1] for item in canonical]
    selected_body = str(proposal.get("text") or "")
    if not selected_body or len(selected_body) > 1000 or "\x00" in selected_body:
        raise ValueError("consolidation candidate body is invalid")
    if selected_body not in bodies:
        raise ValueError("consolidation candidate was not observed in its sources")
    if group_kind == "duplicate" and any(body != bodies[0] for body in bodies[1:]):
        raise ValueError("duplicate source bodies no longer match")
    if group_kind == "conflict" and len(set(bodies)) < 2:
        raise ValueError("conflict source bodies no longer conflict")
    if len(set(keys)) != len(keys):
        raise ValueError("consolidation source keys are duplicated")

    for key, row in zip(keys, rows, strict=True):
        if revision_is_indexed:
            if key in pointer_map:
                raise ValueError("consolidation source index changed")
        elif str(row.get("status")) == "active" and key not in pointer_map:
            raise ValueError("consolidation source index changed")
        elif str(row.get("status")) == "superseded" and key in pointer_map:
            raise ValueError("consolidation source index changed")
    consolidation_rollback_keys = [detail_key, *keys, MEMORY_INDEX_KEY]
    consolidation_snapshot = await _snapshot_store_values(
        store, namespace, consolidation_rollback_keys
    )
    consolidation_projection_keys = [
        key for key in consolidation_rollback_keys if key and key != MEMORY_INDEX_KEY
    ]
    from src.server.database.refactor import memory as db_memory
    from src.server.services.refactor.memory_dual_write import (
        memory_tier_for_namespace,
    )

    _consolidation_tier = memory_tier_for_namespace(tuple(namespace))
    consolidation_projection_snapshot = (
        await db_memory.get_projection_rows(
            user_id=str(proposal.get("user_id") or ""),
            tier=_consolidation_tier[0],
            entry_keys=consolidation_projection_keys,
        )
        if _consolidation_tier is not None
        else []
    )
    projected_entry_ids: list[str] = []
    async with _activation_guard(
        store,
        namespace,
        user_id=str(proposal.get("user_id") or ""),
        store_snapshot=consolidation_snapshot,
        projection_keys=consolidation_projection_keys,
        projection_snapshot=consolidation_projection_snapshot,
        projected_entry_ids=projected_entry_ids,
    ):
        if not existing_content:
            await aput(store, namespace, detail_key, staged_detail)

        next_index = index_content
        for key in keys:
            next_index = _drop_pointer(next_index, key)
        pointer = _pointer_line(proposal, detail_key)
        if pointer not in next_index:
            next_index = f"{next_index.rstrip()}\n{pointer}\n" if next_index.strip() else f"{pointer}\n"
        index_value = {
            "content": next_index,
            "encoding": "utf-8",
            "created_at": now,
            "modified_at": now,
        }
        await aput(store, namespace, MEMORY_INDEX_KEY, index_value)

        retired_values: list[tuple[str, dict[str, Any]]] = []
        for row, (frontmatter, _body, content) in zip(rows, canonical, strict=True):
            source_key = str(row["entry_key"])
            chosen_body = _detail_frontmatter(content)[1]
            lifecycle = (
                "superseded"
                if group_kind == "duplicate" or chosen_body == selected_body
                else "wrong"
            )
            if frontmatter.get("lifecycle") == lifecycle and (
                frontmatter.get("lifecycle_reason") == "consolidated_duplicate_or_conflict"
            ):
                retired_content = content
            else:
                retired_content = _annotate_consolidation_lifecycle(
                    content, lifecycle=lifecycle
                )
            value = {"content": retired_content, "encoding": "utf-8", "modified_at": now}
            retired_values.append((source_key, value))
        for key, value in retired_values:
            await aput(store, namespace, key, value)
        await aput(store, namespace, detail_key, active_detail)


    projected = 0
    projection_failures: list[str] = []
    projected_ok: list[str] = []
    for key, value in [(detail_key, active_detail), *retired_values, (MEMORY_INDEX_KEY, index_value)]:
        try:
            result = await project_memory_write(
                store=store, namespace=namespace, key=key, value=value
            )
        except BaseException as exc:
            logger.warning(
                f"[refactor] projection after consolidation approval failed for {key}",
                exc_info=True,
            )
            if isinstance(exc, asyncio.CancelledError):
                await _restore_store_values(store, namespace, consolidation_snapshot)
                await _rollback_projection(
                    user_id=str(proposal.get("user_id") or ""),
                    namespace=namespace,
                    projection_keys=consolidation_projection_keys,
                    projection_snapshot=consolidation_projection_snapshot,
                    projected_entry_ids=projected_entry_ids,
                )
                raise
            result = None
        if result is None:
            projection_failures.append(key)
        else:
            projected += 1
            projected_ok.append(key)
            entry_id = ((result or {}).get("row") or {}).get("entry_id")
            if entry_id:
                projected_entry_ids.append(str(entry_id))
    if projection_failures:
        await _restore_store_values(store, namespace, consolidation_snapshot)
        await _rollback_projection(
            user_id=str(proposal.get("user_id") or ""),
            namespace=namespace,
            projection_keys=consolidation_projection_keys,
            projection_snapshot=consolidation_projection_snapshot,
            projected_entry_ids=projected_entry_ids,
        )
        raise RuntimeError(
            "consolidation approval projection failed; retry to complete the "
            f"revision ({', '.join(sorted(projection_failures))})"
        )
    return {
        "detail_key": detail_key,
        "index_key": MEMORY_INDEX_KEY,
        "replaced_entry_keys": keys,
        "projected": projected,
        "projection_status": "complete",
    }


async def _thread_owner(thread_id: object) -> str | None:
    from src.server.database import conversation as conv_db

    if not thread_id:
        return None
    meta = await conv_db.get_thread_auth_meta(str(thread_id))
    owner = str((meta or {}).get("user_id") or "").strip()
    return owner or None


async def approve_proposal(
    *,
    proposal: Mapping[str, Any],
    store: Any,
    owner_id: str | None = None,
    approval_source: str,
) -> dict[str, Any]:
    """Write the proposal into the user's memory and project it.

    ``owner_id`` is the **raw** store owner (what the app wrote its memory under);
    it defaults to the proposal thread's owner. The projection maps it to the
    queue's UUID owner itself, so store and projection agree.

    Returns ``{"detail_key", "index_key", "projected"}``. Raises when the store
    write fails: the CAS must only happen after the source of truth changed.
    """
    if approval_source != "manual":
        raise ValueError("proposal approval requires manual authority")
    from src.server.services.store_backends.langgraph_store import user_memory_namespace
    from src.server.app._store_helpers import aget, aput
    from src.server.services.refactor.memory_dual_write import project_memory_write

    user_id = owner_id or await _thread_owner(proposal.get("thread_id"))
    if not user_id:
        raise ValueError("proposal has no store owner")
    if proposal.get("producer_kind") == "memory_consolidation":
        return await _approve_consolidation_proposal(
            proposal=proposal, store=store, user_id=user_id
        )
    if proposal.get("producer_kind") is not None:
        raise ValueError("unsupported proposal producer")
    namespace = user_memory_namespace(user_id)
    now = datetime.now(timezone.utc).isoformat()
    detail_key = _detail_key(proposal)
    legacy_key = _legacy_detail_key(proposal)
    change_kind = str(proposal.get("change_kind") or "long_term")
    if change_kind not in ("long_term", "correction"):
        raise ValueError("only durable memory proposals can be approved")
    replacement_key = _replacement_key(proposal)
    reason = str(proposal.get("reason") or "").strip() or None
    if replacement_key and change_kind == "long_term" and not reason:
        reason = "long_term_change"
    proposal = {**proposal, "reason": reason}
    if change_kind == "correction" and (not replacement_key or not reason):
        raise ValueError("correction needs a replacement target and reason")
    if replacement_key == detail_key:
        raise ValueError("replacement must use a distinct immutable revision key")
    prior_projection = None
    index_item = await aget(store, namespace, MEMORY_INDEX_KEY)
    index_content = _content_of(index_item)
    prior_value = None
    prior_content = ""
    if replacement_key:
        prior_item = await aget(store, namespace, replacement_key)
        prior_content = _content_of(prior_item)
        if not prior_content:
            raise ValueError("replacement target is missing from this owner namespace")
        from src.server.database.refactor import memory as db_memory

        prior_projection = await db_memory.get_active_entry_for_approval(
            user_id=user_id, tier="user", entry_key=replacement_key
        )
        if prior_projection is None:
            prior_projection = await db_memory.get_entry(
                user_id=user_id, tier="user", entry_key=replacement_key, status="superseded"
            )
        if prior_projection is None:
            raise ValueError("replacement target projection is missing; repair it before approval")
        prior_value = (
            dict(prior_item.value)
            if prior_item is not None and isinstance(prior_item.value, Mapping)
            else {"content": prior_content, "encoding": "utf-8"}
        )
        prior_value["content"] = _annotate_lifecycle(
            prior_content,
            lifecycle="wrong" if change_kind == "correction" else "superseded",
            reason=reason,
        )
        prior_value["modified_at"] = now

    detail_value = _detail_content(
        proposal,
        now=now,
        source_entry_id=(str(prior_projection["entry_id"]) if prior_projection else None),
        lifecycle="pending",
    )
    active_detail_value = _detail_content(
        proposal,
        now=now,
        source_entry_id=(str(prior_projection["entry_id"]) if prior_projection else None),
        lifecycle="active",
    )
    existing_new = await aget(store, namespace, detail_key)
    existing_content = _content_of(existing_new)
    if existing_content and not (
        _matches_proposal(existing_content, detail_value["content"])
        or _matches_proposal(existing_content, active_detail_value["content"])
    ):
        raise ValueError("immutable revision key already contains different memory content")
    already_indexed = detail_key in parse_memory_index(index_content)
    target_still_active = bool(replacement_key and replacement_key in parse_memory_index(index_content))
    if replacement_key and not target_still_active and not already_indexed:
        raise ValueError("replacement target is not active in this owner namespace")

    replaced_entry_ids = (
        (str(prior_projection["entry_id"]),) if prior_projection else ()
    )
    from src.server.services.refactor.memory_tool_policy_approval import (
        validate_approved_memory_policy,
    )

    # Fail before any Store/projection mutation if the policy write this
    # approval will perform is already known to be invalid (e.g. correction
    # without a constraint kind replacing a memory with active denials).
    await validate_approved_memory_policy(
        user_id=user_id,
        proposal=proposal,
        replaced_entry_ids=replaced_entry_ids,
    )

    rollback_keys = [detail_key, replacement_key or "", MEMORY_INDEX_KEY, legacy_key]
    store_snapshot = await _snapshot_store_values(store, namespace, rollback_keys)
    from src.server.database.refactor import memory as db_memory
    from src.server.database.refactor import memory_tool_policy as db_policy
    from src.server.services.refactor.memory_dual_write import (
        memory_tier_for_namespace,
    )

    # The policy transaction can commit before its connection cleanup returns;
    # a cancellation in that window must not leave authority changed after the
    # activation itself is rolled back.
    policy_snapshot = await db_policy.get_denials_snapshot(
        user_id=str(proposal.get("user_id") or "")
    )

    _tier = memory_tier_for_namespace(tuple(namespace))
    projection_keys = [key for key in rollback_keys if key and key != MEMORY_INDEX_KEY]
    projection_snapshot = (
        await db_memory.get_projection_rows(
            user_id=str(proposal.get("user_id") or ""),
            tier=_tier[0],
            entry_keys=projection_keys,
        )
        if _tier is not None
        else []
    )

    projected_entry_ids: list[str] = []
    async with _activation_guard(
        store,
        namespace,
        user_id=user_id,
        store_snapshot=store_snapshot,
        projection_keys=projection_keys,
        projection_snapshot=projection_snapshot,
        projected_entry_ids=projected_entry_ids,
        policy_snapshot=policy_snapshot,
    ):
        if not existing_content:
            await aput(store, namespace, detail_key, detail_value)

        pointer = _pointer_line(proposal, detail_key)
        # A pre-promotion approval may have left a proposed/ pointer; remove it so
        # the index names only the canonical file. Idempotent retries keep the rest
        # untouched and do not append a second pointer.
        next_index = _drop_pointer(index_content, legacy_key)
        if replacement_key:
            next_index = _drop_pointer(next_index, replacement_key)
        if pointer not in next_index:
            next_index = f"{next_index.rstrip()}\n{pointer}\n" if next_index.strip() else f"{pointer}\n"
        if next_index != index_content:
            index_value = {
                "content": next_index,
                "encoding": "utf-8",
                "created_at": now,
                "modified_at": now,
            }
            await aput(store, namespace, MEMORY_INDEX_KEY, index_value)
        else:
            # Idempotent retry: reuse what is already in the index.
            index_value = (
                dict(index_item.value)
                if index_item is not None and isinstance(index_item.value, Mapping)
                else {"content": index_content, "encoding": "utf-8"}
            )

        if replacement_key and prior_value is not None:
            prior_value["content"] = _annotate_lifecycle(
                prior_content,
                lifecycle="wrong" if change_kind == "correction" else "superseded",
                reason=reason,
            )
            await aput(store, namespace, replacement_key, prior_value)
        if _content_of(existing_new) != active_detail_value["content"]:
            await aput(store, namespace, detail_key, active_detail_value)

        # Drop the old proposed/ detail if it exists. The store is the truth; the
        # projection follows below (a missing legacy file is fine).
        if legacy_key != detail_key:
            try:
                await store.adelete(namespace, legacy_key)
            except Exception:
                logger.debug(
                    f"[refactor] legacy proposed detail delete failed for {legacy_key}",
                    exc_info=True,
                )
            try:
                await project_memory_write(
                    store=store,
                    namespace=namespace,
                    key=legacy_key,
                    deleted=True,
                )
            except Exception:
                logger.warning(
                    f"[refactor] legacy proposed projection delete failed for {legacy_key}",
                    exc_info=True,
                )


    projected = 0
    projected_values = [(detail_key, active_detail_value)]
    if replacement_key and prior_value is not None:
        projected_values.append((replacement_key, prior_value))
    projected_values.append((MEMORY_INDEX_KEY, index_value))
    projection_failures: list[str] = []
    projected_ok: list[str] = []
    for key, value in projected_values:
        try:
            result = await project_memory_write(
                store=store, namespace=namespace, key=key, value=value
            )
            if result is None:
                projection_failures.append(key)
            else:
                projected += 1
                projected_ok.append(key)
                entry_id = ((result or {}).get("row") or {}).get("entry_id")
                if entry_id:
                    projected_entry_ids.append(str(entry_id))
        except BaseException as exc:
            # Durable lineage is the authorization record forget relies on. An
            # approval that cannot project its revision would leave a canonical
            # Store copy unknown to the projection, so it must not report
            # success: raising leaves the proposal `proposed` (the queue CAS has
            # not run) and the idempotent retry completes the same revision.
            # Cancellation gets the same compensation immediately, then is
            # re-raised unchanged.
            logger.warning(
                f"[refactor] projection after proposal approval failed for {key}",
                exc_info=True,
            )
            if isinstance(exc, asyncio.CancelledError):
                await _restore_store_values(store, namespace, store_snapshot)
                await _rollback_projection(
                    user_id=str(proposal.get("user_id") or ""),
                    namespace=namespace,
                    projection_keys=projection_keys,
                    projection_snapshot=projection_snapshot,
                    projected_entry_ids=projected_entry_ids,
                    policy_snapshot=policy_snapshot,
                )
                raise
            projection_failures.append(key)
    if projection_failures:
        # The Store has been materialized but the database lineage has not, so
        # no forget walk could find this revision. Undo the Store writes and the
        # projections that already committed; the proposal CAS has not run, so
        # the idempotent retry starts clean.
        await _restore_store_values(store, namespace, store_snapshot)
        await _rollback_projection(
            user_id=str(proposal.get("user_id") or ""),
            namespace=namespace,
            projection_keys=projection_keys,
            projection_snapshot=projection_snapshot,
            projected_entry_ids=projected_entry_ids,
            policy_snapshot=policy_snapshot,
        )
        raise RuntimeError(
            "proposal approval projection failed; retry to complete the "
            f"revision ({', '.join(sorted(projection_failures))})"
        )
    # Persist authority before the proposal queue can report approval success.
    # A failed projection or policy write leaves the queue proposed so a retry
    # can complete the same hash-bound revision idempotently.
    from src.server.services.refactor.memory_tool_policy_approval import (
        sync_approved_memory_policy,
    )

    try:
        await sync_approved_memory_policy(
            user_id=user_id,
            proposal=proposal,
            approved_entry_key=detail_key,
            replaced_entry_ids=replaced_entry_ids,
        )
    except BaseException as exc:
        # Policy authority is part of the activation. If it cannot persist
        # (including request cancellation), the Store and projection changes
        # must roll back with it, or the proposal returns to `proposed` while
        # the replacement stays active.
        logger.warning(
            "[refactor] approval policy sync failed; rolling activation back",
            exc_info=True,
        )
        await _restore_store_values(store, namespace, store_snapshot)
        await _rollback_projection(
            user_id=user_id,
            namespace=namespace,
            projection_keys=projection_keys,
            projection_snapshot=projection_snapshot,
            projected_entry_ids=projected_entry_ids,
            policy_snapshot=policy_snapshot,
        )
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise
    from src.config import settings
    from src.server.services.refactor.memory_federation.write_bridge import (
        stage_approved_proposal,
    )

    # Egress is scoped to this user's explicit URL binding. In particular, a
    # proposal tenant alone never authorizes use of a process-wide write URL.
    proposal_tenant = str(proposal.get("tenant") or "").strip()
    try:
        binding = settings.get_memory_federation_binding(user_id)
    except Exception:
        logger.debug(
            "[refactor] memory federation binding unavailable; skipping MemFW staging",
            exc_info=True,
        )
        binding = None
    binding_tenant = str(getattr(binding, "memfw_tenant", "") or "").strip()
    binding_url = str(getattr(binding, "memfw_url", "") or "").strip()
    from src.config.features import system_flag
    from src.server.services.features import user_feature_enabled

    try:
        feature_allowed = bool(
            getattr(system_flag("memory_federation"), "enabled", False)
        ) and await user_feature_enabled(user_id, "memory_federation")
    except Exception:
        logger.debug(
            "[refactor] memory federation feature state unavailable; "
            "skipping MemFW staging",
            exc_info=True,
        )
        feature_allowed = False
    policy = normalize_memory_policy_metadata_layers(proposal.get("metadata", {}))

    # The bridge is optional and additive: a staging failure must not roll back
    # the human-approved Conductor authority. It only writes a MemFW candidate.
    if not binding_url or not feature_allowed:
        memfw_staging = {
            "enabled": False,
            "staged": False,
            "reason": (
                "no_user_binding"
                if not binding_url
                else "memory_federation_disabled"
            ),
        }
    elif policy["sensitivity"] == "secret":
        memfw_staging = {
            "enabled": True,
            "staged": False,
            "error_code": "secret_forbidden",
        }
    else:
        memfw_staging = await stage_approved_proposal(
            proposal,
            content=str(active_detail_value.get("content") or ""),
            endpoint=binding_url,
            sensitivity=policy["sensitivity"],
            tags=[str(item) for item in (proposal.get("tags") or [])],
            tenant=proposal_tenant or binding_tenant,
        )
    return {
        "detail_key": detail_key,
        "index_key": MEMORY_INDEX_KEY,
        "replaced_entry_key": replacement_key,
        "projected": projected,
        "memfw_staging": memfw_staging,
    }
