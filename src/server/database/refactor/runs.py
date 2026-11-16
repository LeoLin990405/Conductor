"""Per-turn run-contract rows (SQL layer, migration 087).

One row per main-agent turn records the contract the turn ran under: permission
mode, model, context budget and the memory entries that were injected. Phase 1
writes it from the finalize path behind the ``events`` flag (best-effort, after
the terminal CAS), so a failure here can never fail a turn.

The JSON columns bind through ``SafeJson`` (psycopg ``Json``); assignment into a
``jsonb`` column casts implicitly on INSERT/UPDATE. No COALESCE over a raw JSON
parameter: ``COALESCE($1, col)`` needs both sides to share a type, which is why
the upgrade path stores a value or leaves the column alone per field.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Mapping

from psycopg.rows import dict_row

from src.server.contracts.refactor import SUBAGENT_CONTRACT_VERSION
from src.server.database import pool
from src.server.utils.pg_sanitize import SafeJson, normalize_uuid

_CONTRACT_COLUMNS = (
    "conversation_response_id, contract_version, permission_mode, model_id,"
    " context_budget, injected_memory, verifier_verdict, created_at"
)


def _normalized_id(value: object, field: str) -> str:
    normalized = normalize_uuid(value)
    if normalized is None:
        raise ValueError(f"{field} is not a UUID")
    return normalized


async def upsert_contract(
    *,
    conversation_response_id: str,
    permission_mode: str = "manual",
    model_id: str | None = None,
    context_budget: Mapping[str, Any] | None = None,
    injected_memory: list[Any] | None = None,
    verifier_verdict: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert or update the turn's contract row (idempotent across retries).

    A retried finalize for the same response must not duplicate the row; it
    updates the fields the later attempt knows better (model/verdict) and keeps
    an existing ``model_id`` when the retry has none.

    The memory-injection audit writes ``context_budget``/``injected_memory``
    **before** finalize (at turn start); finalize calls with neither, so empty
    defaults must not erase the audit. Preserve-on-empty is expressed in SQL:
    a default ``{}``/``[]`` keeps the stored value, a non-empty one replaces it.
    """
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO agent_run_contracts
                    (conversation_response_id, permission_mode, model_id,
                     context_budget, injected_memory, verifier_verdict)
                VALUES (
                    %s, %s, %s,
                    COALESCE(%s::jsonb, '{{}}'::jsonb),
                    COALESCE(%s::jsonb, '[]'::jsonb),
                    %s
                )
                ON CONFLICT (conversation_response_id) DO UPDATE
                   SET permission_mode = EXCLUDED.permission_mode,
                       model_id = COALESCE(EXCLUDED.model_id, agent_run_contracts.model_id),
                       context_budget = CASE
                           WHEN EXCLUDED.context_budget = '{{}}'::jsonb
                           THEN agent_run_contracts.context_budget
                           ELSE EXCLUDED.context_budget
                       END,
                       injected_memory = CASE
                           WHEN EXCLUDED.injected_memory = '[]'::jsonb
                           THEN agent_run_contracts.injected_memory
                           ELSE EXCLUDED.injected_memory
                       END,
                       verifier_verdict = COALESCE(
                           EXCLUDED.verifier_verdict,
                           agent_run_contracts.verifier_verdict
                       )
                RETURNING {_CONTRACT_COLUMNS}
                """,
                (
                    _normalized_id(conversation_response_id, "conversation_response_id"),
                    permission_mode,
                    model_id,
                    SafeJson(dict(context_budget)) if context_budget is not None else None,
                    SafeJson(list(injected_memory)) if injected_memory is not None else None,
                    SafeJson(dict(verifier_verdict)) if verifier_verdict else None,
                ),
            )
            return dict(await cur.fetchone())


async def record_subagent_contract(
    *,
    task_run_id: str,
    result: Mapping[str, Any],
    contract_version: int = SUBAGENT_CONTRACT_VERSION,
    verification: Mapping[str, Any] | None = None,
    tool_allowlist: Sequence[str] | None = None,
    context_budget: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Write one subagent run's contract columns (migration 087), idempotently.

    The ledger row already exists — it was written when the run was admitted —
    so this is an UPDATE, not an insert. It returns ``None`` when the ledger has
    no such run (a projection of a run this database never admitted), which the
    caller logs and drops.
    """
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE subagent_runs
                   SET contract_version = %s,
                       result = COALESCE(%s::jsonb, result),
                       verification = COALESCE(%s::jsonb, verification),
                       tool_allowlist = COALESCE(%s::text[], tool_allowlist),
                       context_budget = COALESCE(%s::jsonb, context_budget)
                 WHERE task_run_id = %s
                RETURNING task_run_id, contract_version, status, result
                """,
                (
                    contract_version,
                    SafeJson(dict(result)),
                    SafeJson(dict(verification)) if verification is not None else None,
                    list(tool_allowlist) if tool_allowlist is not None else None,
                    SafeJson(dict(context_budget)) if context_budget is not None else None,
                    task_run_id,
                ),
            )
            row = await cur.fetchone()
            return dict(row) if row is not None else None

