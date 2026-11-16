"""CodexBar quota effect history used by the observation fence.

Kept under wakers/ so database/autonomy/effects.py stays outside this line.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

# History includes quota_low so a repeated low reading is not enqueued again.
# The claim gate does not: a low reading after an exhaust must not reopen work.
QUOTA_HISTORY_KINDS = ("quota_low", "quota_exhausted", "quota_reset")
QUOTA_GATE_KINDS = ("quota_exhausted", "quota_reset")


def collapse_provider_kinds(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Latest quota kind per worker-provider id.

    CodexBar and ticket rows spell the same provider differently (zai and glm).
    The newest observed_at across those spellings wins, so a reset stored under
    one spelling clears an exhaust stored under the other.
    """
    from src.server.services.autonomy.wakers.base import canonical_provider_id

    best: dict[str, tuple[tuple[str, str, str], str]] = {}
    for row in rows:
        provider = canonical_provider_id(row.get("provider"))
        kind = str(row.get("kind") or "")
        if not provider or not kind:
            continue
        rank = _quota_rank(row)
        current = best.get(provider)
        if current is None or rank > current[0]:
            best[provider] = (rank, kind)
    return {provider: item[1] for provider, item in best.items()}


def _quota_rank(row: dict[str, Any]) -> tuple[str, str, str]:
    created = row.get("created_at")
    if isinstance(created, datetime):
        created_text = created.isoformat()
    else:
        created_text = str(created or "")
    return (
        str(row.get("observed_at") or ""),
        created_text,
        str(row.get("effect_id") or ""),
    )


async def _latest_quota_rows(kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    """Newest CodexBar quota wake per stored provider spelling.

    Page continuations (``quota_after_ticket_id`` set) are the same observation
    as their origin wake. Including them would let a later ``created_at`` beat
    a same-timestamp reset that already outranked the origin.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT DISTINCT ON (lower(payload->>'provider'))
                       payload->>'provider' AS provider,
                       payload->>'kind' AS kind,
                       COALESCE(payload->>'observed_at', '') AS observed_at,
                       created_at,
                       effect_id
                FROM autonomy_effects
                WHERE kind = 'wake'
                  AND payload->>'source' = 'codexbar'
                  AND payload->>'kind' = ANY(%s)
                  AND COALESCE(payload->>'provider', '') <> ''
                  AND COALESCE(payload->>'quota_after_ticket_id', '') = ''
                ORDER BY lower(payload->>'provider'),
                         COALESCE(payload->>'observed_at', '') DESC,
                         created_at DESC,
                         effect_id DESC
                """,
                (list(kinds),),
            )
            return [dict(row) for row in await cur.fetchall()]


async def latest_codexbar_quota_kinds() -> dict[str, str]:
    """Latest CodexBar quota kind per provider, including quota_low."""
    return collapse_provider_kinds(await _latest_quota_rows(QUOTA_HISTORY_KINDS))


async def latest_quota_observed_at(provider: str) -> str:
    """Newest exhaust-or-reset observation for one provider.

    The rank matches :func:`collapse_provider_kinds`: observed_at, then
    created_at, then effect id. A reset stored under another spelling of the
    same provider still moves this horizon, so a delayed exhaust cannot park
    work after that reset.
    """
    from src.server.services.autonomy.wakers.base import canonical_provider_id

    target = canonical_provider_id(provider)
    if not target:
        return ""
    best: tuple[tuple[str, str, str], str] | None = None
    for row in await _latest_quota_rows(QUOTA_GATE_KINDS):
        if canonical_provider_id(row.get("provider")) != target:
            continue
        rank = _quota_rank(row)
        observed = str(row.get("observed_at") or "")
        if best is None or rank > best[0]:
            best = (rank, observed)
    return "" if best is None else best[1]


async def quota_observation_outranks_effect(
    provider: str, effect_id: str
) -> bool:
    """Whether a stored exhaust/reset ranks above this wake effect.

    Ties on ``observed_at`` break on ``created_at`` then ``effect_id``, the
    same order provider history uses. A later-created reset with the same
    timestamp must make an older exhaust stale at plan time, not only in
    the SQL write fence.
    """
    from src.server.services.autonomy.wakers.base import (
        canonical_provider_id,
        provider_spellings,
    )

    target = _quota_uuid(effect_id)
    canonical = canonical_provider_id(provider)
    spellings = list(provider_spellings(canonical))
    if target is None or not spellings:
        return False
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM autonomy_effects newer
                    JOIN autonomy_effects me ON me.effect_id = %s::uuid
                    WHERE newer.kind = 'wake'
                      AND newer.payload->>'source' = 'codexbar'
                      AND newer.payload->>'kind' = ANY(%s)
                      AND lower(COALESCE(newer.payload->>'provider', ''))
                          = ANY(%s)
                      AND COALESCE(newer.payload->>'quota_after_ticket_id', '') = ''
                      AND (
                        COALESCE(newer.payload->>'observed_at', ''),
                        newer.created_at,
                        newer.effect_id::text
                      ) > (
                        COALESCE(me.payload->>'observed_at', ''),
                        me.created_at,
                        me.effect_id::text
                      )
                )
                """,
                (target, list(QUOTA_GATE_KINDS), spellings),
            )
            row = await cur.fetchone()
            return bool(row and row[0])


def _quota_uuid(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    try:
        from uuid import UUID

        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


async def known_exhausted_providers() -> set[str]:
    """Providers whose latest exhaust-or-reset wake is still exhausted.

    quota_low is ignored here. A provider absent from the latest snapshot is
    not treated as reset; only a quota_reset wake clears the gate.
    """
    kinds = collapse_provider_kinds(await _latest_quota_rows(QUOTA_GATE_KINDS))
    return {provider for provider, kind in kinds.items() if kind == "quota_exhausted"}
