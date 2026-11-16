"""Owner-scoped source inspection and explicit memory revision restore."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import hashlib
import logging
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import yaml

from src.server.contracts.memory_validity import is_current
from src.server.database.refactor import memory_revisions
from src.server.services.refactor.memory_projection import parse_memory_index

logger = logging.getLogger(__name__)

_MISSING_STORE_VALUE = object()


async def _snapshot_store_values(store, namespace, keys):
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
                "[memory] could not roll back Store value after failed revert "
                "projection key=%s",
                key,
                exc_info=True,
            )
            raise RuntimeError("revert rollback failed after projection error") from None


async def _restore_projections(store, namespace, snapshot, keys):
    """Undo revert projections that committed before another projection failed."""
    from src.server.services.refactor.memory_dual_write import project_memory_write

    failures = []
    for key in keys:
        value = snapshot.get(key)
        try:
            if value is _MISSING_STORE_VALUE:
                await project_memory_write(
                    store=store, namespace=namespace, key=key, deleted=True
                )
            else:
                await project_memory_write(
                    store=store, namespace=namespace, key=key, value=value
                )
        except Exception:
            logger.error(
                "[memory] could not roll back revert projection key=%s",
                key,
                exc_info=True,
            )
            failures.append(key)
    if failures:
        raise RuntimeError(
            "revert projection rollback failed "
            f"({', '.join(sorted(failures))})"
        )

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
    """Compensate any revert-activation failure, including cancellation."""
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
    *,
    user_id,
    namespace,
    projection_keys,
    projection_snapshot,
    projected_entry_ids,
    policy_snapshot=None,
):
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


_FRONTMATTER = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z",
    re.DOTALL,
)


class MemoryRevisionError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _namespace(user_id: str) -> tuple[str, ...]:
    from src.server.services.store_backends.langgraph_store import user_memory_namespace

    return user_memory_namespace(user_id)


def _item_content(item: Any) -> str:
    value = getattr(item, "value", None)
    content = value.get("content") if isinstance(value, Mapping) else None
    return content if isinstance(content, str) else ""


def _updated_value(item: Any, content: str, now: str) -> dict[str, Any]:
    value = getattr(item, "value", None)
    updated = dict(value) if isinstance(value, Mapping) else {"encoding": "utf-8"}
    updated["content"] = content
    updated["modified_at"] = now
    return updated


def _frontmatter(content: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER.match(content or "")
    if match is None:
        return {}, content or ""
    try:
        raw = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise MemoryRevisionError("malformed_revision_metadata") from exc
    if not isinstance(raw, Mapping):
        raise MemoryRevisionError("malformed_revision_metadata")
    return dict(raw), match.group(2)


def _render(metadata: Mapping[str, Any], body: str) -> str:
    header = yaml.safe_dump(
        dict(metadata), allow_unicode=True, sort_keys=False, default_flow_style=False
    ).rstrip()
    # Keep the body byte-for-byte, including indentation and trailing spaces.
    return f"---\n{header}\n---\n{body}"


def _safe_detail_key(value: object) -> str:
    key = str(value or "")
    if (
        not key.endswith(".md")
        or key in ("memory.md", ".md")
        or "/" in key
        or "\\" in key
        or key in (".", "..")
        or len(key) > 512
    ):
        raise MemoryRevisionError("invalid_revision_key")
    return key


async def _lookup_memory_source(*, user_id: str, entry_id: str) -> dict[str, Any]:
    """Return only source references that resolve to exact canonical message text.

    Current memory rows often carry no message/offset reference. In that case
    the API reports unavailability instead of inferring a quote from a run id.
    """
    row = await memory_revisions.get_revision(user_id=user_id, entry_id=entry_id)
    if row is None:
        return {"status": "not_found", "entry_id": entry_id}
    metadata = row.get("metadata")
    quote_ref = metadata.get("evidence_quote_ref") if isinstance(metadata, Mapping) else None
    if (
        not isinstance(quote_ref, Mapping)
        or set(quote_ref) != {"message_id", "start", "end", "unit"}
        or not isinstance(quote_ref.get("message_id"), str)
        or type(quote_ref.get("start")) is not int
        or type(quote_ref.get("end")) is not int
        or quote_ref.get("start", -1) < 0
        or quote_ref.get("end", 0) <= quote_ref.get("start", 0)
        or quote_ref.get("end", 0) > 1_000_000_000
        or quote_ref.get("unit") != "unicode_codepoint"
        or not (row.get("source_thread_id") or (metadata or {}).get("source_thread_id"))
    ):
        return {
            "status": "unavailable",
            "entry_id": str(row["entry_id"]),
            "reason": "exact_message_offset_reference_unavailable",
        }
    from src.server.database import conversation as conv_db
    from src.server.utils.pg_sanitize import refactor_owner_uuid

    thread_id = str(row.get("source_thread_id") or metadata.get("source_thread_id"))
    response_id = row.get("source_response_id") or metadata.get("source_response_id")
    if not response_id and metadata.get("source_task_run_id"):
        response_id = await memory_revisions.get_parent_response_id(
            thread_id=thread_id,
            task_run_id=str(metadata["source_task_run_id"]),
        )
    if not response_id:
        return {
            "status": "unavailable",
            "entry_id": str(row["entry_id"]),
            "reason": "source_response_unavailable",
        }
    response_id = str(response_id)
    thread_meta = await conv_db.get_thread_auth_meta(thread_id)
    if (
        thread_meta is None
        or refactor_owner_uuid(thread_meta.get("user_id"))
        != refactor_owner_uuid(user_id)
    ):
        return {
            "status": "unavailable",
            "entry_id": str(row["entry_id"]),
            "reason": "source_thread_unavailable",
        }
    source_slice = await memory_revisions.get_source_turn_slice(
        thread_id=thread_id, response_id=response_id
    )
    if source_slice is None:
        return {
            "status": "unavailable",
            "entry_id": str(row["entry_id"]),
            "reason": "source_response_unavailable",
        }
    if (
        str(source_slice.get("thread_id")) != thread_id
        or str(source_slice.get("response_id")) != response_id
    ):
        return {
            "status": "unavailable",
            "entry_id": str(row["entry_id"]),
            "reason": "source_response_unavailable",
        }
    try:
        from src.server.services.history import slices
        from src.server.services.history.reader import CheckpointHistoryReader

        decoded = slices.decode(
            CheckpointHistoryReader.get_instance().serde,
            str(source_slice["slice_codec"]),
            bytes(source_slice["slice"]),
        )
    except Exception:
        return {
            "status": "unavailable",
            "entry_id": str(row["entry_id"]),
            "reason": "source_slice_unavailable",
        }
    source_message = next(
        (
            message
            for message in decoded.messages
            if str(getattr(message, "id", "")) == quote_ref["message_id"]
            and getattr(message, "type", None) in ("human", "ai", "tool")
        ),
        None,
    )
    content = getattr(source_message, "content", None)
    start, end = quote_ref["start"], quote_ref["end"]
    if end - start > 4000:
        return {
            "status": "unavailable",
            "entry_id": str(row["entry_id"]),
            "reason": "source_span_too_large",
        }
    if not isinstance(content, str) or start < 0 or end <= start or end > len(content):
        return {
            "status": "unavailable",
            "entry_id": str(row["entry_id"]),
            "reason": "source_offsets_unavailable",
        }
    return {
        "status": "available",
        "entry_id": str(row["entry_id"]),
        "source": {
            "thread_id": thread_id,
            "response_id": response_id,
            "message_id": str(source_message.id),
            "role": str(source_message.type),
            "origin": str(metadata.get("origin") or "assistant_inference"),
            "supports_user_evidence": (
                metadata.get("origin") == "user" and source_message.type == "human"
            ),
            "start": start,
            "end": end,
            "quote": content[start:end],
            "unit": "unicode_codepoint",
        },
    }


async def lookup_memory_source(*, user_id: str, entry_id: str) -> dict[str, Any]:
    try:
        return await _lookup_memory_source(user_id=user_id, entry_id=entry_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        return {
            "status": "unavailable",
            "entry_id": entry_id,
            "reason": "source_data_unavailable",
        }


async def revert_memory_revision(
    *, user_id: str, entry_id: str, store: Any, reason: str
) -> dict[str, Any]:
    """Restore the immediate predecessor as a new immutable active revision."""
    restore_reason = " ".join(str(reason or "").split())
    if not restore_reason or len(restore_reason) > 1000:
        raise MemoryRevisionError("restore_reason_required")

    async with memory_revisions.owner_memory_lock(user_id):
        current = await memory_revisions.get_revision(user_id=user_id, entry_id=entry_id)
        if current is None:
            raise MemoryRevisionError("entry_not_found")
        current_key = _safe_detail_key(current.get("entry_key"))
        predecessor_id = current.get("source_entry_id")
        if not predecessor_id:
            raise MemoryRevisionError("predecessor_unavailable")
        predecessor = await memory_revisions.get_revision(
            user_id=user_id, entry_id=str(predecessor_id)
        )
        if predecessor is None:
            raise MemoryRevisionError("predecessor_unavailable")
        namespace = _namespace(user_id)
        if (
            current.get("tier") != "user"
            or predecessor.get("tier") != "user"
            or tuple(current.get("namespace") or ()) != namespace
            or tuple(predecessor.get("namespace") or ()) != namespace
        ):
            raise MemoryRevisionError("entry_outside_user_namespace")
        if current.get("status") not in ("active", "superseded"):
            raise MemoryRevisionError("entry_not_current")
        current_validity = dict(current.get("metadata") or {})
        current_validity["lifecycle"] = "active"
        if not is_current(current_validity):
            raise MemoryRevisionError("entry_outside_validity_window")
        predecessor_metadata = predecessor.get("metadata")
        lifecycle = (
            predecessor_metadata.get("lifecycle")
            if isinstance(predecessor_metadata, Mapping)
            else None
        )
        if lifecycle not in ("wrong", "superseded"):
            raise MemoryRevisionError("predecessor_not_restorable")
        predecessor_key = _safe_detail_key(predecessor.get("entry_key"))
        valid_metadata = dict(predecessor_metadata or {})
        valid_metadata["lifecycle"] = "active"
        if not is_current(valid_metadata):
            raise MemoryRevisionError("predecessor_outside_validity_window")

        from src.server.app._store_helpers import aget, aput
        from src.server.services.refactor.memory_dual_write import project_memory_write

        index_item = await aget(store, namespace, "memory.md")
        index_body = _item_content(index_item)
        pointers = parse_memory_index(index_body)
        current_item = await aget(store, namespace, current_key)
        predecessor_item = await aget(store, namespace, predecessor_key)
        current_content = _item_content(current_item)
        predecessor_content = _item_content(predecessor_item)
        if not current_content or not predecessor_content:
            raise MemoryRevisionError("revision_body_unavailable")
        current_frontmatter, current_body = _frontmatter(current_content)
        predecessor_frontmatter, predecessor_body = _frontmatter(predecessor_content)
        if predecessor_frontmatter.get("lifecycle") not in ("wrong", "superseded"):
            raise MemoryRevisionError("predecessor_not_restorable")
        store_validity = dict(predecessor_frontmatter)
        store_validity["lifecycle"] = "active"
        if not is_current(store_validity):
            raise MemoryRevisionError("predecessor_outside_validity_window")

        restore_identity = hashlib.sha256(
            f"{current['entry_id']}\0{predecessor['entry_id']}\0{restore_reason}".encode()
        ).hexdigest()[:24]
        new_key = (
            f"{predecessor_key[:-3][:120]}--restore-"
            f"{str(predecessor['entry_id'])[:8]}-{restore_identity}.md"
        )
        index_has_current = current_key in pointers
        index_has_new = new_key in pointers
        retrying = (
            not index_has_current
            and index_has_new
            and current_frontmatter.get("lifecycle") in ("active", "superseded")
            and (
                current_frontmatter.get("lifecycle") == "active"
                or current_frontmatter.get("reverted_to_entry_key") == new_key
            )
        )
        if predecessor_key in pointers or not (
            (index_has_current and not index_has_new and current_frontmatter.get("lifecycle") == "active")
            or retrying
        ):
            raise MemoryRevisionError("active_index_changed")
        from src.server.services.refactor.memory_projection import content_hash

        current_hash_matches = current.get("content_hash") == content_hash(current_content)
        predecessor_hash_matches = predecessor.get("content_hash") == content_hash(predecessor_content)
        if not predecessor_hash_matches or (not retrying and not current_hash_matches):
            raise MemoryRevisionError("store_projection_mismatch")
        now = datetime.now(timezone.utc).isoformat()
        restored_metadata = dict(predecessor_frontmatter)
        restored_metadata.pop("lifecycle", None)
        restored_metadata["lifecycle"] = "active"
        restored_metadata["restores_entry_key"] = predecessor_key
        restored_metadata["restores_entry_id"] = str(predecessor["entry_id"])
        restored_metadata["restore_reason"] = restore_reason
        restored_metadata["supersedes"] = [current_key]
        restored_metadata["source_entry_id"] = str(current["entry_id"])
        restored_metadata["restore_revision_id"] = restore_identity
        active_restored_content = _render(restored_metadata, predecessor_body)
        pending_metadata = dict(restored_metadata)
        pending_metadata["lifecycle"] = "pending"
        pending_restored_content = _render(pending_metadata, predecessor_body)

        revert_snapshot = await _snapshot_store_values(
            store, namespace, [new_key, current_key, "memory.md"]
        )
        from src.server.database.refactor import memory as db_memory
        from src.server.services.refactor.memory_dual_write import (
            memory_tier_for_namespace,
        )

        from src.server.database.refactor import memory_tool_policy as db_policy

        _revert_tier = memory_tier_for_namespace(tuple(namespace))
        projection_keys = [new_key, current_key]
        projection_snapshot = (
            await db_memory.get_projection_rows(
                user_id=user_id,
                tier=_revert_tier[0],
                entry_keys=projection_keys,
            )
            if _revert_tier is not None
            else []
        )
        # The authority copy can commit before connection cleanup returns; a
        # cancellation there must roll the committed denial rows back too.
        policy_snapshot = await db_policy.get_denials_snapshot(user_id=user_id)
        existing_restore = await aget(store, namespace, new_key)
        existing_restore_content = _item_content(existing_restore)
        projected_entry_ids: list[str] = []
        async with _activation_guard(
            store,
            namespace,
            user_id=user_id,
            store_snapshot=revert_snapshot,
            projection_keys=projection_keys,
            projection_snapshot=projection_snapshot,
            projected_entry_ids=projected_entry_ids,
            policy_snapshot=policy_snapshot,
        ):
            if existing_restore_content:
                existing_metadata, existing_body = _frontmatter(existing_restore_content)
                if (
                    existing_metadata.get("restore_revision_id") != restore_identity
                    or existing_body != predecessor_body
                    or existing_metadata.get("lifecycle") not in ("pending", "active")
                ):
                    raise MemoryRevisionError("restore_revision_key_conflict")
            else:
                await aput(
                    store,
                    namespace,
                    new_key,
                    {"content": pending_restored_content, "encoding": "utf-8", "created_at": now, "modified_at": now},
                )
            next_lines = [
                line
                for line in index_body.splitlines()
                if f"]({current_key})" not in line and f"]({new_key})" not in line
            ]
            title = predecessor_key[:-3]
            hook = str(predecessor.get("index_text") or title).replace("\n", " ")[:500]
            next_lines.append(f"- [{title}]({new_key}) — {hook}")
            next_index = "\n".join(next_lines).strip() + "\n"
            index_value = _updated_value(index_item, next_index, now)
            if not index_has_new:
                await aput(store, namespace, "memory.md", index_value)
            updated_current = current_content
            if current_frontmatter.get("lifecycle") != "superseded":
                current_frontmatter["lifecycle"] = "superseded"
                current_frontmatter["lifecycle_reason"] = "owner_revert"
                current_frontmatter["reverted_to_entry_key"] = new_key
                updated_current = _render(current_frontmatter, current_body)
                await aput(store, namespace, current_key, _updated_value(current_item, updated_current, now))
            await aput(
                store,
                namespace,
                new_key,
                {"content": active_restored_content, "encoding": "utf-8", "created_at": now, "modified_at": now},
            )


        projection_failures: list[str] = []
        projected_ok: list[str] = []
        for key, value in ((new_key, {"content": active_restored_content}), (current_key, {"content": updated_current}), ("memory.md", index_value)):
            try:
                projection_result = await project_memory_write(
                    store=store,
                    namespace=namespace,
                    key=key,
                    value=value,
                )
                if projection_result is None:
                    projection_failures.append(key)
                else:
                    projected_ok.append(key)
                    entry_id = ((projection_result or {}).get("row") or {}).get(
                        "entry_id"
                    )
                    if entry_id:
                        projected_entry_ids.append(str(entry_id))
            except BaseException as exc:
                logger.warning(
                    "[memory] projection after source revert failed for %s",
                    key,
                    exc_info=True,
                )
                if isinstance(exc, asyncio.CancelledError):
                    await _restore_store_values(store, namespace, revert_snapshot)
                    await _rollback_projection(
                        user_id=user_id,
                        namespace=namespace,
                        projection_keys=projection_keys,
                        projection_snapshot=projection_snapshot,
                        projected_entry_ids=projected_entry_ids,
                        policy_snapshot=policy_snapshot,
                    )
                    raise
                projection_failures.append(key)
        if projection_failures:
            # Durable lineage is what forget walks; a revert that cannot project
            # would leave the restored revision outside the projection (and the
            # database lineage), so it fails closed and restores the Store to
            # its pre-revert state. The revision key is deterministic, so a
            # retry completes it idempotently.
            await _restore_store_values(store, namespace, revert_snapshot)
            await _rollback_projection(
                user_id=user_id,
                namespace=namespace,
                projection_keys=projection_keys,
                projection_snapshot=projection_snapshot,
                projected_entry_ids=projected_entry_ids,
                policy_snapshot=None,
            )
            raise MemoryRevisionError("projection_failed")
        projection_status = "complete"
        # An explicit owner revert is the only path that may copy frozen
        # predecessor authority onto the new revision. Always run it, including
        # non-constraint restores, so any current denial is retired.
        try:
            from src.server.services.refactor.memory_tool_policy_approval import (
                sync_reverted_memory_policy,
            )

            await sync_reverted_memory_policy(
                user_id=user_id,
                restored_entry_key=new_key,
                restored_from_entry_id=str(predecessor["entry_id"]),
                replaced_entry_id=str(current["entry_id"]),
            )
        except BaseException as exc:
            # The restored revision is already active and indexed; a policy
            # copy failure (including request cancellation) would leave it
            # active without the frozen authority it must carry. Roll the whole
            # activation back, like a projection failure, before surfacing the
            # error. CancelledError must be re-raised unchanged.
            logger.warning(
                "[memory] revert policy sync failed; rolling activation back",
                exc_info=True,
            )
            await _restore_store_values(store, namespace, revert_snapshot)
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
            raise MemoryRevisionError("tool_policy_sync_failed") from exc
        return {
            "status": projection_status,
            "entry_id": str(current["entry_id"]),
            "restored_from_entry_id": str(predecessor["entry_id"]),
            "new_entry_key": new_key,
            "restore_reason": restore_reason,
        }
