"""Hard-delete a memory and its explicitly linked local copies."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Mapping, Sequence

from src.server.services.store_backends.langgraph_store import store_label
from src.server.database.refactor import memory as db_memory
from src.server.database.refactor import memory_revisions
from src.server.database.refactor import memory_tool_policy as db_tool_policy
from src.server.services.refactor.memory_projection import (
    MEMORY_INDEX_KEY,
    normalize_key,
)
from src.server.services.refactor.memory_dual_write import memory_tier_for_namespace
from src.server.utils.pg_sanitize import normalize_uuid, refactor_owner_uuid

MAX_FORGET_ROWS = 256
#: The statuses a provider notice may carry. ``unsupported`` says that the
#: provider does not offer the operation at all -- the remote MemFW of contract
#: v2 has no forget endpoint -- which is not the same as an outage, and not
#: the same as a provider that is merely read-only.
_FORGET_NOTICE_STATUSES = frozenset(
    {"deleted", "read_only", "unavailable", "not_configured", "unsupported"}
)
#: A provider may explain its status in a note of its own. The note belongs to
#: the record of the provider it explains; it is not a provider of its own and
#: is never counted or shown as one.
_REASON_SUFFIX = "_reason"
_DETAIL_REVISION_SUFFIX = re.compile(
    r"--r[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_POINTER_START_RE = re.compile(r"^\s*-\s*\[")
_POINTER_TARGET_RE = re.compile(r"\]\((?P<path>[^)\s]+)\)")


class ForgetReason(StrEnum):
    USER_REQUEST = "user_request"
    CORRECTION = "correction"
    DUPLICATE = "duplicate"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ForgetResult:
    entry_id: str
    status: Literal["completed", "partial", "not_found"]
    carriers: Mapping[str, str]
    #: The provider notices this forget left in storage, read back once the
    #: write is behind us. The carriers report what the federation said while
    #: it was still talking; these report what the row kept -- ``provider``,
    #: ``status`` and ``memfw_reason`` -- which is what the API answers with.
    #: It is empty where the read-back could not be made, and the carriers
    #: then remain the only evidence the caller gets.
    provider_notices: Sequence[Mapping[str, Any]] = ()


def _namespace(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value or not all(
        isinstance(part, str) and part for part in value
    ):
        raise ValueError("memory row has an invalid store namespace")
    return tuple(value)


def _proposal_key(entry_key: str) -> str:
    key = entry_key.removeprefix("proposed/")
    key = key.removesuffix(".md")
    return _DETAIL_REVISION_SUFFIX.sub("", key)


def _content(item: Any) -> str:
    value = getattr(item, "value", None) if item is not None else None
    body = value.get("content") if isinstance(value, Mapping) else None
    if isinstance(body, str):
        return body
    if isinstance(body, list) and all(isinstance(line, str) for line in body):
        return "\n".join(body)
    return ""


def _without_pointers(content: str, keys: set[str]) -> str:
    kept: list[str] = []
    for line in (content or "").splitlines():
        targets = _pointer_targets(line)
        if any(target in keys for target in targets):
            continue
        kept.append(line)
    return ("\n".join(kept).strip() + "\n") if kept else ""


def _pointer_targets(line: str) -> set[str]:
    if not _POINTER_START_RE.match(line):
        return set()
    return {
        normalize_key(match.group("path"))
        for match in _POINTER_TARGET_RE.finditer(line)
    }


async def _retry_store_cleanup(
    *,
    user_id: str,
    root_entry_id: str,
    store: Any,
    tombstones: Sequence[Mapping[str, Any]],
) -> bool:
    """Prove tombstone locators from a bounded owner scan before retrying cleanup."""
    if not tombstones or len(tombstones) > MAX_FORGET_ROWS:
        return False
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return False
    scanned = await asyncio.wait_for(
        store.asearch([store_label(user_id)], limit=MAX_FORGET_ROWS + 1, offset=0),
        timeout=5.0,
    )
    if not isinstance(scanned, (list, tuple)):
        return False
    if len(scanned) > MAX_FORGET_ROWS:
        return False

    indexes: list[tuple[tuple[str, ...], str]] = []
    candidates: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}
    for item in scanned:
        namespace = _namespace(getattr(item, "namespace", None))
        scope = memory_tier_for_namespace(namespace)
        if scope is None:
            continue
        if refactor_owner_uuid(scope[1]) != owner:
            return False
        key = str(getattr(item, "key", "") or "")
        if not key:
            return False
        if any(part in ("", ".", "..") for part in key.split("/")):
            return False
        if key == MEMORY_INDEX_KEY:
            value = getattr(item, "value", None)
            raw_content = value.get("content") if isinstance(value, Mapping) else None
            content = _content(item)
            if not isinstance(raw_content, str) and not (
                isinstance(raw_content, list)
                and all(isinstance(line, str) for line in raw_content)
            ):
                return False
            indexes.append((namespace, content))
        else:
            candidates[(namespace, key)] = {
                "tier": scope[0],
                "namespace": list(namespace),
                "entry_key": key,
            }
            if key.startswith("proposed/"):
                base_key = f"{_proposal_key(key)}.md"
                candidates.setdefault((namespace, base_key), {
                    "tier": scope[0],
                    "namespace": list(namespace),
                    "entry_key": base_key,
                })

    # Index references are locators too: a deleted detail can still leave a
    # stale pointer, and the retry must prove and remove that carrier.
    for namespace, content in indexes:
        for line in content.splitlines():
            scope = memory_tier_for_namespace(namespace)
            assert scope is not None
            targets = _pointer_targets(line)
            if _POINTER_START_RE.match(line) and not targets:
                return False
            for key in targets:
                if (
                    key.startswith(("http://", "https://", "#"))
                    or any(part in ("", ".", "..") for part in key.split("/"))
                ):
                    return False
                candidate_keys = {key, f"proposed/{_proposal_key(key)}.md"}
                if key.startswith("proposed/"):
                    candidate_keys.add(f"{_proposal_key(key)}.md")
                for candidate_key in sorted(candidate_keys):
                    candidates.setdefault((namespace, candidate_key), {
                        "tier": scope[0],
                        "namespace": list(namespace),
                        "entry_key": candidate_key,
                    })
    candidate_rows = list(candidates.values())
    if len(candidate_rows) > 1024:
        return False
    matches: list[dict[str, Any]] = []
    for offset in range(0, len(candidate_rows), 256):
        matches.extend(
            await db_memory.match_forget_tombstone_locators(
                user_id=user_id,
                root_entry_id=root_entry_id,
                candidates=candidate_rows[offset : offset + 256],
            )
        )
    rows = [
        {"namespace": match["namespace"], "entry_key": match["entry_key"]}
        for match in matches
    ]
    if rows:
        await _erase_store_rows(store, rows)
    return True


async def _erase_store_rows(store: Any, rows: Sequence[Mapping[str, Any]]) -> None:
    by_namespace: dict[tuple[str, ...], set[str]] = {}
    for row in rows:
        namespace = _namespace(row.get("namespace"))
        key = str(row.get("entry_key") or "")
        if not key:
            raise ValueError("memory row has an empty store key")
        by_namespace.setdefault(namespace, set()).update((key, f"proposed/{_proposal_key(key)}.md"))

    # Remove references first so a failed detail delete cannot leave a live pointer.
    for namespace, keys in by_namespace.items():
        index_item = await asyncio.wait_for(
            store.aget(namespace, MEMORY_INDEX_KEY), timeout=2.0
        )
        if index_item is not None:
            index_value = getattr(index_item, "value", None)
            raw_content = (
                index_value.get("content")
                if isinstance(index_value, Mapping)
                else None
            )
            if not isinstance(raw_content, str) and not (
                isinstance(raw_content, list)
                and all(isinstance(line, str) for line in raw_content)
            ):
                raise ValueError("memory index content has an unsupported shape")
        index_content = _content(index_item)
        cleaned = _without_pointers(index_content, keys)
        if cleaned != index_content:
            value = dict(index_item.value) if isinstance(getattr(index_item, "value", None), Mapping) else {}
            value["content"] = cleaned
            await asyncio.wait_for(
                store.aput(namespace, MEMORY_INDEX_KEY, value), timeout=2.0
            )

    for row in rows:
        namespace = _namespace(row.get("namespace"))
        key = str(row.get("entry_key") or "")
        await asyncio.wait_for(store.adelete(namespace, key), timeout=2.0)
        legacy_key = f"proposed/{_proposal_key(key)}.md"
        if legacy_key != key:
            await asyncio.wait_for(store.adelete(namespace, legacy_key), timeout=2.0)


async def _notify_federation(
    *, user_id: str, entry_id: str, content_hash: str
) -> Mapping[str, str]:
    try:
        from src.server.services.refactor.memory_federation.integration import (
            notify_configured_forget,
        )

        return await notify_configured_forget(
            user_id=user_id, entry_id=entry_id, content_hash=content_hash
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return {"configured_providers": "unavailable"}


async def _notify_rows(user_id: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    notices: list[dict[str, str]] = []
    carriers: dict[str, str] = {}
    for row in rows:
        entry_id = str(row.get("entry_id") or "")
        statuses = await _notify_federation(
            user_id=user_id,
            entry_id=entry_id,
            content_hash=str(row.get("content_hash") or ""),
        )
        # A note about a provider belongs to that provider's record. It is
        # never a provider of its own: it gets no carrier of its own and no
        # notice of its own, so it is neither counted nor shown as one.
        reasons = {
            key[: -len(_REASON_SUFFIX)]: str(value)
            for key, value in statuses.items()
            if key.endswith(_REASON_SUFFIX) and isinstance(value, str)
        }
        for provider, status in statuses.items():
            if provider.endswith(_REASON_SUFFIX):
                continue
            # ``unsupported`` is a status of its own: the provider does not
            # offer the operation at all, which is not an outage and not a
            # read-only provider. It is kept as it is.
            normalized = status if status in _FORGET_NOTICE_STATUSES else "unavailable"
            carriers[f"federation:{provider}:{entry_id}"] = normalized
            reason = reasons.get(provider)
            notice: dict[str, str] = {
                "entry_id": entry_id,
                "provider": provider,
                "status": normalized,
            }
            if reason:
                # The note rides with the provider's own record, one key
                # deeper, so that it reads as an explanation of that status
                # and not as a status of its own.
                carriers[f"federation:{provider}:{entry_id}:reason"] = reason[:200]
                # ... and it rides with the notice as well: the ruling has to
                # reach the row, or a read-back says what happened without
                # saying why. It goes over in the federation's own words --
                # which of the three forget codes it is is the persistence
                # layer's business, not this layer's.
                notice["reason"] = reason[:200]
            notices.append(notice)
    if notices:
        try:
            await db_memory.record_forget_provider_notices(user_id=user_id, notices=notices)
        except asyncio.CancelledError:
            raise
        except Exception:
            carriers["federation_audit"] = "unavailable"
    return carriers


async def forget_provider_notices(
    *, user_id: str, entry_id: str
) -> list[dict[str, Any]]:
    """Read the notices one forget wrote, with the reason each row kept.

    The forget result reports what the federation said while it was still
    talking, in the remote's own words. This is the read-back: what the row
    kept -- one of the three forget codes, or ``None`` where the ruling was
    outside that vocabulary -- beside the status it explains. It is how a
    caller says *why* after the fact, and it is owner-scoped all the way down
    to the SQL.
    """
    return await db_memory.get_forget_provider_notices(
        user_id=user_id, entry_id=entry_id
    )


async def _stored_provider_notices(
    user_id: str, entry_id: str
) -> list[dict[str, Any]]:
    """The notices one forget persisted, or nothing where they cannot be read.

    Called once the write is behind us, so the answer the API gives is the one
    the row holds rather than the one the federation gave on the way through.
    """
    try:
        notices = await forget_provider_notices(user_id=user_id, entry_id=entry_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        # A caller with no reachable database in this code path keeps the
        # immediate result: the carriers already carry what the federation
        # said, and a read-back that cannot be made is no reason to fail a
        # delete that itself succeeded.
        return []
    return [
        dict(notice) for notice in notices or () if isinstance(notice, Mapping)
    ]


async def forget_memory_entry(
    *,
    user_id: str,
    entry_id: str,
    store: Any,
    reason: ForgetReason,
) -> ForgetResult:
    """Serialize forget with approvals/reverts before tombstoning and erasing."""
    async with memory_revisions.owner_memory_lock(user_id):
        return await _forget_memory_entry_locked(
            user_id=user_id,
            entry_id=entry_id,
            store=store,
            reason=reason,
        )


async def _forget_memory_entry_locked(
    *,
    user_id: str,
    entry_id: str,
    store: Any,
    reason: ForgetReason,
) -> ForgetResult:
    """Tombstone first, erase canonical store copies, then purge the projection."""
    try:
        reason = ForgetReason(reason)
    except (TypeError, ValueError) as exc:
        raise ValueError("memory forget reason must be a supported reason code") from exc
    root_entry_id = normalize_uuid(entry_id)
    if root_entry_id is None:
        raise ValueError("memory entry id must be a UUID")
    rows = await db_memory.get_forget_closure(user_id=user_id, entry_id=root_entry_id)
    if not rows:
        tombstone = await db_memory.get_forget_tombstone(
            user_id=user_id, entry_id=root_entry_id
        )
        if tombstone is None:
            # Nothing was written, so there is no notice in storage to read
            # back: this path is the 404 the route already answers with.
            return ForgetResult(root_entry_id, "not_found", {"store": "not_found", "projection": "not_found"})
        group = await db_memory.get_forget_tombstone_group(
            user_id=user_id,
            root_entry_id=str(tombstone.get("root_entry_id") or root_entry_id),
        )
        carriers = {
            "store": "failed",
            "projection": "pending",
            "proposals": "pending",
        }
        try:
            if not group:
                raise RuntimeError("forgotten store locators are unavailable")
            if await _retry_store_cleanup(
                user_id=user_id,
                root_entry_id=str(tombstone.get("root_entry_id") or root_entry_id),
                store=store,
                tombstones=group,
            ):
                carriers["store"] = "deleted"
        except asyncio.CancelledError:
            raise
        except Exception:
            carriers["store"] = "failed"
        # A retry after a failed projection purge must finish that cleanup, or
        # it would report completion while the plaintext projection remains.
        try:
            group_ids = [str(row.get("entry_id") or "") for row in group or (tombstone,)]
            live_rows = await db_memory.get_entries_by_ids(
                user_id=user_id, entry_ids=group_ids
            )
            if live_rows:
                proposal_keys = sorted(
                    {_proposal_key(str(row.get("entry_key") or "")) for row in live_rows}
                )
                deleted = await db_memory.forget_entry_rows(
                    user_id=user_id, rows=live_rows, proposal_keys=proposal_keys
                )
                if deleted["entries"] != len(live_rows):
                    raise RuntimeError("projection retry deletion was incomplete")
                carriers["proposals"] = "deleted" if deleted["proposals"] else "absent"
            else:
                carriers["proposals"] = "absent"
            carriers["projection"] = "deleted"
        except asyncio.CancelledError:
            raise
        except Exception:
            carriers["projection"] = "failed"
            carriers["proposals"] = "failed"
        try:
            if not group:
                raise RuntimeError("forgotten policy closure is unavailable")
            await db_tool_policy.purge_forgotten_denials(
                user_id=user_id,
                entry_ids=tuple(str(row.get("entry_id") or "") for row in group),
            )
            carriers["tool_policy"] = "deleted"
        except asyncio.CancelledError:
            raise
        except Exception:
            # The committed tombstones already hide authority. Retaining the
            # carrier makes cleanup retryable and reports a partial forget.
            carriers["tool_policy"] = "failed"
        federation = await _notify_rows(user_id, group or (tombstone,))
        carriers.update(federation)
        status = (
            "completed"
            if set(carriers.values()) <= {"deleted", "absent", "not_configured"}
            else "partial"
        )
        # The retry wrote its notices too, so the response is built from what
        # the rows kept, exactly as a first forget is.
        return ForgetResult(  # type: ignore[arg-type]
            root_entry_id,
            status,
            carriers,
            await _stored_provider_notices(user_id, root_entry_id),
        )
    if len(rows) > MAX_FORGET_ROWS:
        raise ValueError("memory forget lineage exceeds the safe bound")
    expected_owner = refactor_owner_uuid(user_id)
    for row in rows:
        scope = memory_tier_for_namespace(_namespace(row.get("namespace")))
        if scope is None or refactor_owner_uuid(scope[1]) != expected_owner:
            raise ValueError("memory row is outside the authenticated owner's namespace")

    target = next(
        (row for row in rows if normalize_uuid(row.get("entry_id")) == root_entry_id),
        None,
    )
    if target is None:
        raise ValueError("memory forget closure omitted the requested entry")
        # Durable lineage is written before activation (approval/revert now fail
        # closed when their projection write fails), so the database closure is
        # the complete, bounded membership of this forget; no mutable-Store scan
        # is needed.
    await db_memory.record_forget_tombstones(
        user_id=user_id,
        rows=rows,
        root_entry_id=root_entry_id,
        reason=reason.value,
    )
    carriers: dict[str, str] = {
        "store": "failed",
        "projection": "pending",
        "proposals": "pending",
    }
    try:
        await db_tool_policy.purge_forgotten_denials(
            user_id=user_id,
            entry_ids=tuple(str(row.get("entry_id") or "") for row in rows),
        )
        carriers["tool_policy"] = "deleted"
    except asyncio.CancelledError:
        raise
    except Exception:
        # Tombstones are committed before this cleanup, so the policy remains
        # hidden even when its frozen identifiers could not be purged yet.
        carriers["tool_policy"] = "failed"
    try:
        await _erase_store_rows(store, rows)
        carriers["store"] = "deleted"
    except asyncio.CancelledError:
        raise
    except Exception:
        carriers["store"] = "failed"

    if carriers["store"] == "deleted":
        try:
            proposal_keys = sorted(
                {_proposal_key(str(row.get("entry_key") or "")) for row in rows}
            )
            deleted = await db_memory.forget_entry_rows(
                user_id=user_id, rows=rows, proposal_keys=proposal_keys
            )
            if deleted["entries"] != len(rows):
                raise RuntimeError("projection deletion was incomplete")
            carriers["projection"] = "deleted"
            carriers["proposals"] = "deleted" if deleted["proposals"] else "absent"
        except asyncio.CancelledError:
            raise
        except Exception:
            carriers["projection"] = "failed"
            carriers["proposals"] = "failed"

    federation = await _notify_rows(user_id, rows)
    carriers.update(federation)
    statuses = set(carriers.values())
    status: Literal["completed", "partial", "not_found"] = (
        "completed"
        if statuses and statuses <= {"deleted", "absent", "not_configured"}
        else "partial"
    )
    # The tombstones, the store copies and the notices are all written by now,
    # so the response says what storage kept rather than what the federation
    # said while it was talking.
    return ForgetResult(
        str(target.get("entry_id") or entry_id),
        status,
        carriers,
        await _stored_provider_notices(user_id, root_entry_id),
    )
