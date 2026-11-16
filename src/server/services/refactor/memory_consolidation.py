"""Create review-only proposals from explicit, deterministic memory groups."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import yaml

from src.server.contracts.memory_policy_metadata import (
    normalize_memory_policy_metadata,
    normalize_memory_policy_metadata_layers,
)
from src.server.contracts.memory_validity import is_current
from src.server.database.refactor import memory_consolidation as db_consolidation
from src.server.services.refactor import memory_runtime_controls
from src.server.services.refactor.memory_projection import content_hash
from src.server.services.refactor_flags import refactor_flag_enabled
from src.server.utils.pg_sanitize import refactor_owner_uuid

logger = logging.getLogger(__name__)

MAX_OPTED_IN_USERS_PER_PASS = 1000
MAX_OWNERS_PER_PASS = 50
PASS_BUDGET_SECONDS = 2.0
OWNER_BUDGET_SECONDS = 1.0
MAX_CHOICES_PER_CONFLICT = 4
MAX_SOURCE_ENTRIES = 16


async def automatic_memory_enabled(user_id: str) -> bool:
    """Whether the per-user memory gate is open for this user.

    The gate is looked up on the module that defines it, at call time, rather
    than bound here when this module is first imported. A consumer that froze
    the binding would consult whichever version of the gate happened to be
    installed at import time, so the same code could open or close automatic
    memory depending on which module was imported first.
    """
    return await memory_runtime_controls.automatic_memory_enabled(user_id)


FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<metadata>.*?)\r?\n---[ \t]*(?:\r?\n)?(?P<body>.*)\Z",
    re.DOTALL,
)


def _body_and_policy(content: str) -> tuple[str, dict[str, Any]] | None:
    """Split only valid frontmatter; preserve the remaining bytes exactly."""
    match = FRONTMATTER_RE.match(content)
    if match is None:
        return content, normalize_memory_policy_metadata({})
    try:
        raw = yaml.safe_load(match.group("metadata")) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(raw, Mapping):
        return None
    return match.group("body"), normalize_memory_policy_metadata_layers(raw)


def _entry_content(item: Any) -> str | None:
    value = getattr(item, "value", None) if item is not None else None
    content = value.get("content") if isinstance(value, Mapping) else None
    return content if isinstance(content, str) else None


def _policy_from_row(value: Any) -> dict[str, Any]:
    return normalize_memory_policy_metadata_layers(
        value if isinstance(value, Mapping) else None
    )


def _preserved_memory_metadata(value: Any) -> dict[str, Any] | None:
    """Keep bounded retrieval metadata without copying source/evidence claims."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    kind = value.get("kind")
    if kind is not None:
        if not isinstance(kind, str) or len(kind.strip()) > 64:
            return None
        if kind.strip():
            result["kind"] = kind.strip()
    importance = value.get("importance")
    if importance is not None:
        if (
            isinstance(importance, bool)
            or not isinstance(importance, (int, float))
            or not math.isfinite(float(importance))
            or not 0 <= float(importance) <= 1
        ):
            return None
        result["importance"] = float(importance)
    for field in ("valid_from", "valid_until"):
        raw = value.get(field)
        if raw is not None:
            if not isinstance(raw, str) or len(raw) > 64:
                return None
            try:
                datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
            result[field] = raw
    for field in ("tags", "entities"):
        raw = value.get(field)
        if raw is not None:
            if (
                not isinstance(raw, (list, tuple))
                or len(raw) > 24
                or any(not isinstance(item, str) or len(item.strip()) > 100 for item in raw)
            ):
                return None
            normalized = [item.strip() for item in raw if item.strip()]
            if normalized:
                result[field] = normalized
    return result


def _proposal_for_body(
    *,
    body: str,
    source_ids: Sequence[str],
    source_keys: Sequence[str],
    policy: Mapping[str, Any],
    memory_metadata: Mapping[str, Any],
    group_kind: str,
    group_ref: str,
    source_signature: str,
    source_body_hashes: Sequence[str],
    choice_index: int,
    choice_count: int,
) -> dict[str, Any]:
    digest = hashlib.sha256(
        f"{group_kind}\0{group_ref}\0{body}".encode("utf-8")
    ).hexdigest()
    reason = (
        "Exact stored memory bodies are repeated across active entries; review whether "
        "one approved revision should replace all listed sources."
        if group_kind == "duplicate"
        else "Active entries share an explicit source-entry lineage but contain different "
        "bodies; this proposal preserves one observed candidate for human selection."
    )
    return {
        "proposal_type": "user",
        "proposal_key": f"memory-consolidation-{digest[:40]}",
        "text": body,
        "text_hash": digest,
        "origin": "assistant_inference",
        "confidence": 0.5,
        "evidence_refs": [],
        "change_kind": "correction",
        "replaces_entry_key": str(source_keys[0]),
        "reason": reason,
        "metadata": dict(policy),
        "source_entry_ids": list(source_ids),
        "producer_metadata": {
            "kind": group_kind,
            "group_ref": str(group_ref),
            "source_signature": source_signature,
            "source_entry_ids": list(source_ids),
            "source_entry_keys": list(source_keys),
            "source_content_hashes": [
                pair.split(":", 1)[1] for pair in source_signature.split(",")
            ],
            "source_body_hashes": list(source_body_hashes),
            "memory_metadata": dict(memory_metadata),
            "choice_index": choice_index,
            "choice_count": choice_count,
            "source_count": len(source_ids),
            "conflict_reason": reason if group_kind == "conflict" else None,
        },
    }


async def _proposals_for_group(*, store: Any, group: Mapping[str, Any]) -> list[dict[str, Any]]:
    namespace = group.get("namespace")
    if (
        not isinstance(namespace, (list, tuple))
        or len(namespace) != 2
        or str(namespace[1]) != "memory"
        or refactor_owner_uuid(namespace[0])
        != str(group.get("user_id") or "")
    ):
        return []
    source_ids = [str(value) for value in group.get("source_ids", ())]
    source_keys = [str(value) for value in group.get("source_keys", ())]
    source_metadata = list(group.get("source_metadata", ()))
    if (
        len(source_ids) < 2
        or len(source_ids) > MAX_SOURCE_ENTRIES
        or len(source_ids) != len(source_keys)
        or len(source_ids) != len(source_metadata)
        or len(set(source_keys)) != len(source_keys)
    ):
        return []
    if any(
        not key.endswith(".md") or "/" in key or "\\" in key
        for key in source_keys
    ):
        return []

    policies = [_policy_from_row(value) for value in source_metadata]
    preserved_metadata = [_preserved_memory_metadata(value) for value in source_metadata]
    if any(
        not is_current(value if isinstance(value, Mapping) else None)
        for value in source_metadata
    ):
        return []
    if any(policy.get("sensitivity") == "secret" for policy in policies):
        return []
    if any(
        isinstance(value, Mapping)
        and (value.get("kind") == "constraint" or "denied_tools" in value)
        for value in source_metadata
    ):
        return []
    if any(policy != policies[0] for policy in policies[1:]):
        # Consolidation must not erase a source condition, exception, quote
        # pointer, or sensitivity distinction.
        return []
    if any(value is None for value in preserved_metadata):
        return []
    if any(value != preserved_metadata[0] for value in preserved_metadata[1:]):
        return []

    canonical: list[tuple[str, str]] = []
    expected_hashes = group.get("source_hashes")
    for index, key in enumerate(source_keys):
        try:
            item = await store.aget(tuple(str(part) for part in namespace), key)
        except Exception:  # noqa: BLE001 - one unavailable source makes this group ineligible
            return []
        content = _entry_content(item)
        if content is None:
            return []
        if expected_hashes is not None and content_hash(content) != str(expected_hashes[index]):
            return []
        split = _body_and_policy(content)
        if split is None:
            return []
        body, policy = split
        if policy != policies[index]:
            return []
        if policy.get("sensitivity") == "secret":
            return []
        canonical.append((body, content_hash(content)))

    group_kind = str(group.get("group_kind") or "")
    group_ref = str(group.get("group_ref") or "")
    source_signature = str(group.get("source_signature") or "")
    if not source_signature:
        return []
    if group_kind == "duplicate":
        if any(body != canonical[0][0] for body, _ in canonical[1:]):
            return []
        if not canonical[0][0] or len(canonical[0][0]) > 1000 or "\x00" in canonical[0][0]:
            return []
        return [
            _proposal_for_body(
                body=canonical[0][0],
                source_ids=source_ids,
                source_keys=source_keys,
                policy=policies[0],
                memory_metadata=preserved_metadata[0] or {},
                group_kind=group_kind,
                group_ref=group_ref,
                source_signature=source_signature,
                source_body_hashes=[
                    hashlib.sha256(body.encode("utf-8")).hexdigest()
                    for body, _ in canonical
                ],
                choice_index=0,
                choice_count=1,
            )
        ]
    if group_kind != "conflict":
        return []
    body_choices: list[str] = []
    for body, _ in canonical:
        if body not in body_choices:
            body_choices.append(body)
    if len(body_choices) < 2 or len(body_choices) > MAX_CHOICES_PER_CONFLICT:
        return []
    if any(not body or len(body) > 1000 or "\x00" in body for body in body_choices):
        return []
    return [
        _proposal_for_body(
            body=body,
            source_ids=source_ids,
            source_keys=source_keys,
            policy=policies[0],
            memory_metadata=preserved_metadata[0] or {},
            group_kind=group_kind,
            group_ref=group_ref,
            source_signature=source_signature,
            source_body_hashes=[
                hashlib.sha256(source_body.encode("utf-8")).hexdigest()
                for source_body, _ in canonical
            ],
            choice_index=index,
            choice_count=len(body_choices),
        )
        for index, body in enumerate(body_choices)
    ]


async def run_due_memory_consolidations(*, store: Any | None) -> dict[str, int]:
    """Produce bounded proposals for opted-in users; never changes memory state."""
    stats = {"owners_claimed": 0, "groups_checked": 0, "proposals_created": 0}
    if store is None:
        return stats
    if not refactor_flag_enabled("memory_index"):
        return stats
    from src.config.features import FeatureGate, system_flag

    flag = system_flag("memory_consolidation")
    if not flag.enabled or flag.gate is not FeatureGate.OPT_IN:
        return stats
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PASS_BUDGET_SECONDS
    today = await asyncio.wait_for(db_consolidation.due_run_date(), timeout=0.25)
    if today is None:
        return stats
    users = await asyncio.wait_for(
        db_consolidation.list_opted_in_users(limit=MAX_OPTED_IN_USERS_PER_PASS),
        timeout=min(0.5, max(0.01, deadline - loop.time())),
    )
    visited_owners: set[str] = set()
    for user_id in users:
        if loop.time() >= deadline:
            break
        run = None
        owner_deadline = min(deadline, loop.time() + OWNER_BUDGET_SECONDS)
        try:
            owner = refactor_owner_uuid(user_id)
            if owner is None or owner in visited_owners:
                continue
            visited_owners.add(owner)
            if not await asyncio.wait_for(
                automatic_memory_enabled(user_id),
                timeout=max(0.01, min(0.2, owner_deadline - loop.time())),
            ):
                continue
            if not await asyncio.wait_for(
                db_consolidation.is_user_opted_in(user_id=user_id),
                timeout=max(0.01, min(0.2, owner_deadline - loop.time())),
            ):
                continue
            run = await asyncio.wait_for(
                db_consolidation.claim_daily_run(user_id=user_id, run_date=today),
                timeout=max(0.01, min(0.2, owner_deadline - loop.time())),
            )
            if run is None:
                continue
            stats["owners_claimed"] += 1
            await asyncio.wait_for(
                _run_owner(
                    store=store,
                    run=run,
                    stats=stats,
                    deadline=owner_deadline,
                ),
                timeout=max(0.01, owner_deadline - loop.time()),
            )
            if stats["owners_claimed"] >= MAX_OWNERS_PER_PASS:
                break
        except Exception as exc:  # noqa: BLE001 - isolate one owner's failure
            run_id = (run or {}).get("run_id") if isinstance(run, Mapping) else None
            if run_id:
                try:
                    await asyncio.wait_for(
                        db_consolidation.fail_run(
                            run_id=str(run_id), user_id=user_id,
                            attempt=int(run.get("attempts") or 0),
                            error_code=type(exc).__name__,
                        ),
                        timeout=0.2,
                    )
                except Exception:  # noqa: BLE001 - report by class, never body text
                    logger.warning("[memory-consolidation] could not release owner lease")
            logger.warning(
                "[memory-consolidation] owner pass failed (%s)",
                type(exc).__name__,
            )
    return stats


async def _run_owner(
    *, store: Any, run: Mapping[str, Any], stats: dict[str, int], deadline: float
) -> None:
    run_id = str(run["run_id"])
    user_id = str(run["user_id"])
    raw_user_id = str(run.get("raw_user_id") or user_id)
    cursor = str(run.get("cursor_key") or "")
    processed = int(run.get("groups_processed") or 0)
    remaining = db_consolidation.MAX_GROUPS_PER_DAY - processed

    async def stop_if_disabled() -> bool:
        if await automatic_memory_enabled(raw_user_id):
            return False
        await db_consolidation.fail_run(
            run_id=run_id,
            user_id=user_id,
            attempt=int(run.get("attempts") or 0),
            error_code="memory_disabled",
        )
        return True

    while remaining > 0:
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("memory_consolidation_budget_exhausted")
        if await stop_if_disabled():
            return
        page = await db_consolidation.list_candidate_groups(
            user_id=user_id,
            after_cursor=cursor,
            limit=min(db_consolidation.MAX_GROUPS_PER_PAGE + 1, remaining + 1),
        )
        has_more = len(page) > min(db_consolidation.MAX_GROUPS_PER_PAGE, remaining)
        selected = page[: min(db_consolidation.MAX_GROUPS_PER_PAGE, remaining)]
        proposals: list[dict[str, Any]] = []
        for group in selected:
            proposals.extend(await _proposals_for_group(store=store, group=group))
        from src.config.features import FeatureGate, system_flag
        flag = system_flag("memory_consolidation")

        if await stop_if_disabled():
            return

        if (
            not refactor_flag_enabled("memory_index")
            or not flag.enabled
            or flag.gate is not FeatureGate.OPT_IN
            or not await db_consolidation.is_user_opted_in(
                user_id=str(run.get("raw_user_id") or user_id)
            )
        ):
            await db_consolidation.record_page(
                run_id=run_id,
                user_id=user_id,
                attempt=int(run.get("attempts") or 0),
                expected_cursor=cursor,
                next_cursor=cursor,
                groups_processed=0,
                proposals=(),
                complete=True,
            )
            return
        if selected:
            next_cursor = str(selected[-1]["cursor_key"])
        else:
            next_cursor = cursor
        remaining -= len(selected)
        complete = not has_more or remaining <= 0
        created = await db_consolidation.record_page(
            run_id=run_id,
            user_id=user_id,
            attempt=int(run.get("attempts") or 0),
            expected_cursor=cursor,
            next_cursor=next_cursor,
            groups_processed=len(selected),
            proposals=proposals,
            complete=complete,
        )
        if created is None:
            raise RuntimeError("memory_consolidation_lease_lost")
        stats["groups_checked"] += len(selected)
        stats["proposals_created"] += created
        processed += len(selected)
        cursor = next_cursor
        if complete:
            return
        # Continue inside this owner lease; the per-day group ceiling bounds the
        # work even if the scheduler cadence changes.
