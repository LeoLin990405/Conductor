"""Backfill the memory projection (``memory_entries``) from the LangGraph store.

The store remains the source of truth; this job makes the relational projection
match it so Phase-2 retrieval and injection have data:
- one user tier per user (``(label(user), "memory")``);
- one workspace tier per owned, non-deleted workspace;
- one projection row per **detail** entry (``memory.md`` itself is the index and
  never a row). The mapping rules live in
  ``services.refactor.memory_projection`` and are shared with the write-path
  dual write.

Idempotent: ``upsert_entry`` is keyed on ``(user_id, tier, entry_key, status)``.
Lifecycle frontmatter controls whether a row is active or superseded, so a second
run repairs status as well as content. Dry-run is the default; ``--apply`` writes.
After writing, the job reports store-vs-projection parity (missing/extra/changed).

Owner ids are mapped through ``refactor_owner_uuid``: Supabase UUIDs pass through
and OSS/local-dev sentinels map deterministically to the UUID the projection
stores. Store namespaces are still read from the *raw* owner id, which is what
the app wrote them under, so a local-dev store is projectable too.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from src.server.database.refactor import memory as db_memory
from src.server.services.refactor.memory_projection import (
    primary_concept,
    projectable_rows,
)

MAX_NAMESPACE_ITEMS = 5000
DEFAULT_PAGE = 100


class BackfillError(RuntimeError):
    """The job cannot proceed (no store, unusable input)."""


def compare_projection(
    store_entries: Sequence[Mapping[str, Any]],
    projected_entries: Sequence[Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Parity by ``(tier, entry_key)``: what the projection is missing/extra/stale.

    Local to this script so the deployment job depends on one database module
    (the projection SQL layer), not the whole reader stack; semantics match
    ``services.refactor.readers.compare_memory_projection``.
    """

    def identity(entry: Mapping[str, Any]) -> tuple[str, str]:
        return (str(entry.get("tier") or ""), str(entry.get("entry_key") or ""))

    store = {identity(entry): entry for entry in store_entries}
    projected = {identity(entry): entry for entry in projected_entries}
    return {
        "missing": [f"{tier}:{key}" for tier, key in sorted(set(store) - set(projected))],
        "extra": [f"{tier}:{key}" for tier, key in sorted(set(projected) - set(store))],
        "changed": [
            f"{tier}:{key}"
            for tier, key in sorted(set(store) & set(projected))
            if store[(tier, key)].get("content_hash")
            != projected[(tier, key)].get("content_hash")
            or store[(tier, key)].get("index_text")
            != projected[(tier, key)].get("index_text")
        ],
    }


def build_namespace_rows(
    *,
    user_id: str,
    tier: str,
    namespace: Sequence[str],
    items: Iterable[tuple[str, Mapping[str, Any]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Projection rows for one namespace, via the shared mapping rules."""
    return projectable_rows(user_id=user_id, tier=tier, namespace=namespace, items=items)


async def _read_namespace(
    store: Any, namespace: Sequence[str], *, page: int = DEFAULT_PAGE
) -> tuple[list[tuple[str, Mapping[str, Any]]], list[str]]:
    items: list[tuple[str, Mapping[str, Any]]] = []
    warnings: list[str] = []
    offset = 0
    while len(items) < MAX_NAMESPACE_ITEMS:
        batch = await store.asearch(tuple(namespace), limit=page, offset=offset)
        if not batch:
            break
        for item in batch:
            if tuple(getattr(item, "namespace", ())) != tuple(namespace):
                # asearch is prefix-based on dotted labels; a sibling namespace
                # that string-prefixes this one must not leak into the tier.
                continue
            items.append((str(getattr(item, "key", "")), getattr(item, "value", {}) or {}))
        if len(batch) < page:
            break
        offset += page
    if len(items) >= MAX_NAMESPACE_ITEMS:
        warnings.append(f"namespace truncated at {MAX_NAMESPACE_ITEMS} items")
    return items, warnings


async def backfill_namespace(
    store: Any,
    *,
    user_id: str,
    tier: str,
    namespace: Sequence[str],
    apply: bool = False,
    page: int = DEFAULT_PAGE,
) -> dict[str, Any]:
    """Project one namespace, write (if apply) and report parity."""
    items, warnings = await _read_namespace(store, namespace, page=page)
    rows, skipped = build_namespace_rows(
        user_id=user_id, tier=tier, namespace=namespace, items=items
    )
    report: dict[str, Any] = {
        "user_id": user_id,
        "tier": tier,
        "namespace": ".".join(str(part) for part in namespace),
        "store_items": len(items),
        "rows": len(rows),
        "applied": 0,
        "dry_run": not apply,
        "skipped": skipped,
        "warnings": warnings,
        "failures": [],
        "parity": None,
    }
    if apply:
        from src.server.database.refactor import memory_revisions

        # The apply pass races memory forget; the owner lock serializes the
        # two, and upsert_entry additionally rejects tombstoned identities.
        async with memory_revisions.owner_memory_lock(user_id):
            for row in rows:
                try:
                    if row.get("deactivate"):
                        # This store key cannot be projected any more (or became
                        # malformed); deactivate whatever row was projected before.
                        await db_memory.supersede_entry(
                            user_id=user_id, tier=tier, entry_key=row["entry_key"]
                        )
                        report["applied"] += 1
                        continue
                    if row.get("status") != "active":
                        await db_memory.supersede_entry(
                            user_id=user_id, tier=tier, entry_key=row["entry_key"]
                        )
                    source_ids = row.get("metadata", {}).get("supersedes_entry_ids")
                    # The ontology of the memory: the class it belongs to and
                    # the concept it is about. The upsert overwrites both
                    # columns, so a backfill that left them out would wipe the
                    # classification of every annotated memory it re-projected
                    # -- including one a previous backfill had classified.
                    metadata = row.get("metadata") or {}
                    await db_memory.upsert_entry(
                        user_id=user_id,
                        tier=tier,
                        namespace=row["namespace"],
                        entry_key=row["entry_key"],
                        content_hash=row["content_hash"],
                        size=row["size"],
                        index_text=row["index_text"],
                        status=row.get("status") or "active",
                        metadata=metadata,
                        provenance=row["provenance"],
                        source_entry_id=row.get("source_entry_id"),
                        source_entry_ids=(
                            [str(value) for value in source_ids]
                            if source_ids
                            else None
                        ),
                        memory_class=metadata.get("class"),
                        about=primary_concept(metadata),
                    )
                    report["applied"] += 1
                except Exception as exc:  # one bad entry must not stop the namespace
                    report["failures"].append(f"{row['entry_key']}: {exc}")
    try:
        projected = await db_memory.list_entries(
            user_id=user_id, tier=tier, status="active", limit=500
        )
    except Exception as exc:
        report["failures"].append(f"parity read failed: {exc}")
        return report
    report["parity"] = compare_projection(
        [
            {
                "tier": tier,
                "entry_key": row["entry_key"],
                "content_hash": row["content_hash"],
                "index_text": row["index_text"],
            }
            for row in rows
            if not row.get("deactivate")
        ],
        [
            {
                "tier": row["tier"],
                "entry_key": row["entry_key"],
                "content_hash": row["content_hash"],
                "index_text": row["index_text"],
            }
            for row in projected
        ],
    )
    return report


def render_summary(reports: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "| user | tier | namespace | store | rows | applied | missing | extra | changed | failures |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for report in reports:
        parity = report.get("parity") or {}
        lines.append(
            f"| {report['user_id']} | {report['tier']} | {report['namespace']} | "
            f"{report['store_items']} | {report['rows']} | {report['applied']} | "
            f"{len(parity.get('missing') or ())} | {len(parity.get('extra') or ())} | "
            f"{len(parity.get('changed') or ())} | {len(report.get('failures') or ())} |"
        )
    lines.append("")
    lines.append(
        f"namespaces {len(reports)} · rows {sum(r['rows'] for r in reports)} · "
        f"applied {sum(r['applied'] for r in reports)} · "
        f"failures {sum(len(r.get('failures') or ()) for r in reports)}"
    )
    return "\n".join(lines)


async def _store_and_database():  # pragma: no cover - deployment plumbing
    """Open the LangGraph store and conversation DB pool from the environment."""
    from src.server.database.pool import get_or_create_pool
    from src.server.utils.checkpointer import (
        get_checkpointer,
        get_store,
        open_checkpointer_pool,
    )

    checkpointer = get_checkpointer(
        memory_type=os.getenv("MEMORY_DB_TYPE", "postgres"),
        db_host=os.getenv("DB_HOST", "localhost"),
        db_port=os.getenv("DB_PORT", "5432"),
        db_name=os.getenv("DB_NAME", "postgres"),
        db_user=os.getenv("DB_USER", "postgres"),
        db_password=os.getenv("DB_PASSWORD", "postgres"),
    )
    await open_checkpointer_pool(checkpointer)
    store = get_store(checkpointer)
    if store is None:
        raise BackfillError("LangGraph store is not available in this deployment")
    pool = get_or_create_pool()
    await pool.open()
    return store, pool


async def _list_users(limit: int, user_id: str | None) -> list[str]:
    from psycopg.rows import dict_row

    from src.server.database.pool import get_db_connection

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if user_id:
                await cur.execute(
                    "SELECT user_id FROM users WHERE user_id = %s", (user_id,)
                )
            else:
                await cur.execute(
                    "SELECT user_id FROM users ORDER BY user_id LIMIT %s", (limit,)
                )
            return [str(row["user_id"]) for row in await cur.fetchall()]


async def _list_workspaces(user_id: str) -> list[str]:
    from psycopg.rows import dict_row

    from src.server.database.pool import get_db_connection

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT workspace_id::text AS workspace_id FROM workspaces"
                " WHERE user_id = %s AND status <> 'deleted' ORDER BY workspace_id",
                (user_id,),
            )
            return [str(row["workspace_id"]) for row in await cur.fetchall()]


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="refactor-memory-backfill", description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write rows (default: dry-run)")
    parser.add_argument("--user", help="one user id (default: all users, capped)")
    parser.add_argument("--max-users", type=int, default=50)
    parser.add_argument("--json", help="write the machine-readable report here")
    args = parser.parse_args(argv)
    from ptc_agent.agent.backends import user_memory_namespace, workspace_memory_namespace
    from src.server.utils.pg_sanitize import refactor_owner_uuid

    try:
        store, pool = await _store_and_database()
    except Exception as exc:  # pragma: no cover - deployment plumbing
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    reports: list[dict[str, Any]] = []
    try:
        try:
            users = await _list_users(args.max_users, args.user)
        except Exception as exc:
            print(f"FAIL: cannot list users: {exc}", file=sys.stderr)
            return 2
        for raw_user in users:
            owner_uuid = refactor_owner_uuid(raw_user)
            if owner_uuid is None:
                reports.append(
                    {
                        "user_id": str(raw_user),
                        "tier": "-",
                        "namespace": "-",
                        "store_items": 0,
                        "rows": 0,
                        "applied": 0,
                        "failures": ["empty user id: cannot project"],
                        "parity": None,
                    }
                )
                continue
            reports.append(
                await backfill_namespace(
                    store,
                    user_id=owner_uuid,
                    tier="user",
                    namespace=user_memory_namespace(raw_user),
                    apply=args.apply,
                )
            )
            if args.apply:
                # Legacy rows written before the "candidates are not memory"
                # rule; the store's proposed files stay, the active rows do not.
                try:
                    cleaned = await db_memory.supersede_proposed_entries(
                        user_id=owner_uuid
                    )
                    reports[-1]["superseded_proposed"] = cleaned
                except Exception as exc:
                    reports[-1]["failures"].append(f"proposed cleanup: {exc}")
            try:
                workspaces = await _list_workspaces(raw_user)
            except Exception as exc:  # a user read failure must not stop the rest
                reports.append(
                    {
                        "user_id": owner_uuid,
                        "tier": "workspace",
                        "namespace": "-",
                        "store_items": 0,
                        "rows": 0,
                        "applied": 0,
                        "failures": [f"workspace list failed: {exc}"],
                        "parity": None,
                    }
                )
                continue
            for workspace_id in workspaces:
                reports.append(
                    await backfill_namespace(
                        store,
                        user_id=owner_uuid,
                        tier="workspace",
                        namespace=workspace_memory_namespace(raw_user, workspace_id),
                        apply=args.apply,
                    )
                )
    finally:
        try:
            await pool.close()
        except Exception:  # pragma: no cover - close best-effort
            pass
    print(render_summary(reports))
    if args.json:
        Path(args.json).write_text(
            json.dumps({"apply": args.apply, "reports": reports}, indent=2, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
    failures = sum(len(report.get("failures") or ()) for report in reports)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
