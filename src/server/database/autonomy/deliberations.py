"""Postgres store for deliberation aggregates.

A connection is checked out only when a method runs, never at import.
"""

from __future__ import annotations

from typing import Any

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection
from src.server.services.deliberation.errors import DeliberationError

_DELIBERATION_PK = "autonomy_deliberations_pkey"
_ACTIVE_TICKET = "uq_autonomy_deliberations_active_ticket"

_DELIBERATION_COLUMNS = """
    deliberation_id, ticket_id, user_id, objective, status, max_rounds,
    round_index, consensus_plan, decision_reason, thread_key,
    planner_agent_key, parent_deliberation_id, created_at, updated_at
"""
_PARTICIPANT_COLUMNS = """
    participant_id, deliberation_id, agent_key, role, adapter, daemon_id,
    display_name
"""
_ROUND_COLUMNS = """
    round_id, deliberation_id, round_index, kind, provider_groups, status,
    created_at, closed_at
"""
_MESSAGE_COLUMNS = """
    message_id, deliberation_id, round_id, participant_id, round_index,
    agent_key, kind, body, plan, created_at
"""
_VOTE_COLUMNS = """
    vote_id, deliberation_id, round_id, participant_id, round_index,
    agent_key, ballot, comment, created_at
"""
_DISSENT_COLUMNS = """
    dissent_id, deliberation_id, participant_id, round_id, round_index,
    agent_key, reason, created_at
"""
_STEP_COLUMNS = """
    step_id, deliberation_id, step_key, title, prompt, assigned_agent,
    depends_on, status, daemon_id, child_ticket_id
"""

_DELIBERATION_FIELDS = (
    "deliberation_id",
    "ticket_id",
    "user_id",
    "objective",
    "status",
    "max_rounds",
    "round_index",
    "consensus_plan",
    "decision_reason",
    "thread_key",
    "planner_agent_key",
    "parent_deliberation_id",
    "created_at",
    "updated_at",
)
_PARTICIPANT_FIELDS = (
    "participant_id",
    "deliberation_id",
    "agent_key",
    "role",
    "adapter",
    "daemon_id",
    "display_name",
)
_ROUND_FIELDS = (
    "round_id",
    "deliberation_id",
    "round_index",
    "kind",
    "provider_groups",
    "status",
    "created_at",
    "closed_at",
)
_MESSAGE_FIELDS = (
    "message_id",
    "deliberation_id",
    "round_id",
    "participant_id",
    "round_index",
    "agent_key",
    "kind",
    "body",
    "plan",
    "created_at",
)
_VOTE_FIELDS = (
    "vote_id",
    "deliberation_id",
    "round_id",
    "participant_id",
    "round_index",
    "agent_key",
    "ballot",
    "comment",
    "created_at",
)
_DISSENT_FIELDS = (
    "dissent_id",
    "deliberation_id",
    "participant_id",
    "round_id",
    "round_index",
    "agent_key",
    "reason",
    "created_at",
)
_STEP_FIELDS = (
    "step_id",
    "step_key",
    "title",
    "prompt",
    "assigned_agent",
    "depends_on",
    "status",
    "daemon_id",
    "child_ticket_id",
)
_UPDATABLE = frozenset(
    {
        "status",
        "round_index",
        "consensus_plan",
        "decision_reason",
        "thread_key",
        "planner_agent_key",
        "updated_at",
        "max_rounds",
        "objective",
        "parent_deliberation_id",
    }
)
_CHILD_KEYS = (
    "participants",
    "rounds",
    "messages",
    "votes",
    "dissents",
    "steps",
)
_JSON_OR_NULL = frozenset({"consensus_plan", "plan"})


def _constraint_name(exc: UniqueViolation) -> str | None:
    return getattr(getattr(exc, "diag", None), "constraint_name", None)


def _uuid(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _iso(value: Any) -> Any:
    """Keep Postgres timestamps JSON-safe in aggregates copied into run payloads."""
    return value.isoformat() if hasattr(value, "isoformat") else value


def _object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    return None


def _array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    return []


def _bound(key: str, value: Any) -> Any:
    if key in {"depends_on", "provider_groups"}:
        return Json([] if value is None else value)
    if key in _JSON_OR_NULL:
        return None if value is None else Json(value)
    return value


def _columns(row: dict[str, Any], fields: tuple[str, ...]) -> tuple[list[str], list[Any]]:
    cols: list[str] = []
    params: list[Any] = []
    for key in fields:
        if key not in row:
            continue
        cols.append(key)
        params.append(_bound(key, row[key]))
    return cols, params


def _deliberation(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["deliberation_id"] = str(out["deliberation_id"])
    out["ticket_id"] = str(out["ticket_id"])
    out["parent_deliberation_id"] = _uuid(out.get("parent_deliberation_id"))
    out["consensus_plan"] = _object(out.get("consensus_plan"))
    for key in ("created_at", "updated_at"):
        out[key] = _iso(out.get(key))
    return out


def _participant(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["participant_id"] = str(out["participant_id"])
    out["deliberation_id"] = str(out["deliberation_id"])
    out["daemon_id"] = _uuid(out.get("daemon_id"))
    return out


def _round(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["round_id"] = str(out["round_id"])
    out["deliberation_id"] = str(out["deliberation_id"])
    out["provider_groups"] = _array(out.get("provider_groups"))
    for key in ("created_at", "closed_at"):
        out[key] = _iso(out.get(key))
    return out


def _message(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("message_id", "deliberation_id", "round_id", "participant_id"):
        out[key] = str(out[key])
    out["plan"] = _object(out.get("plan"))
    out["created_at"] = _iso(out.get("created_at"))
    return out


def _vote(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("vote_id", "deliberation_id", "round_id", "participant_id"):
        out[key] = str(out[key])
    out["created_at"] = _iso(out.get("created_at"))
    return out


def _dissent(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["dissent_id"] = str(out["dissent_id"])
    out["deliberation_id"] = str(out["deliberation_id"])
    out["participant_id"] = str(out["participant_id"])
    out["round_id"] = _uuid(out.get("round_id"))
    out["created_at"] = _iso(out.get("created_at"))
    return out


def _step(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["step_id"] = str(out["step_id"])
    out["deliberation_id"] = str(out["deliberation_id"])
    out["depends_on"] = _array(out.get("depends_on"))
    out["daemon_id"] = _uuid(out.get("daemon_id"))
    out["child_ticket_id"] = _uuid(out.get("child_ticket_id"))
    return out


async def _insert(
    table: str,
    row: dict[str, Any],
    fields: tuple[str, ...],
    returning: str,
    normalize: Any,
) -> dict[str, Any]:
    cols, params = _columns(row, fields)
    placeholders = ", ".join(["%s"] * len(cols))
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) "
                f"VALUES ({placeholders}) RETURNING {returning}",
                params,
            )
            return normalize(await cur.fetchone())


async def _attach_children(cur: Any, agg: dict[str, Any]) -> None:
    deliberation_id = agg["deliberation_id"]
    # Participants have no created_at; ctid keeps insertion order.
    await cur.execute(
        f"""
        SELECT {_PARTICIPANT_COLUMNS}
        FROM autonomy_deliberation_participants
        WHERE deliberation_id = %s
        ORDER BY ctid
        """,
        (deliberation_id,),
    )
    agg["participants"] = [_participant(row) for row in await cur.fetchall()]
    await cur.execute(
        f"""
        SELECT {_ROUND_COLUMNS}
        FROM autonomy_deliberation_rounds
        WHERE deliberation_id = %s
        ORDER BY created_at, ctid
        """,
        (deliberation_id,),
    )
    agg["rounds"] = [_round(row) for row in await cur.fetchall()]
    await cur.execute(
        f"""
        SELECT {_MESSAGE_COLUMNS}
        FROM autonomy_deliberation_messages
        WHERE deliberation_id = %s
        ORDER BY created_at, ctid
        """,
        (deliberation_id,),
    )
    agg["messages"] = [_message(row) for row in await cur.fetchall()]
    await cur.execute(
        f"""
        SELECT {_VOTE_COLUMNS}
        FROM autonomy_deliberation_votes
        WHERE deliberation_id = %s
        ORDER BY created_at, ctid
        """,
        (deliberation_id,),
    )
    agg["votes"] = [_vote(row) for row in await cur.fetchall()]
    await cur.execute(
        f"""
        SELECT {_DISSENT_COLUMNS}
        FROM autonomy_deliberation_dissents
        WHERE deliberation_id = %s
        ORDER BY created_at, ctid
        """,
        (deliberation_id,),
    )
    agg["dissents"] = [_dissent(row) for row in await cur.fetchall()]
    # Steps have no created_at. ctid keeps insertion order; step_key does not.
    await cur.execute(
        f"""
        SELECT {_STEP_COLUMNS}
        FROM autonomy_deliberation_steps
        WHERE deliberation_id = %s
        ORDER BY ctid
        """,
        (deliberation_id,),
    )
    agg["steps"] = [_step(row) for row in await cur.fetchall()]


class PostgresDeliberationStore:
    """Async deliberation aggregate backed by autonomy_deliberation_* tables."""

    async def create(self, row: dict[str, Any]) -> dict[str, Any]:
        cols, params = _columns(row, _DELIBERATION_FIELDS)
        placeholders = ", ".join(["%s"] * len(cols))
        try:
            async with get_db_connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"INSERT INTO autonomy_deliberations ({', '.join(cols)}) "
                        f"VALUES ({placeholders}) RETURNING {_DELIBERATION_COLUMNS}",
                        params,
                    )
                    created = _deliberation(await cur.fetchone())
        except UniqueViolation as exc:
            constraint = _constraint_name(exc)
            if constraint == _DELIBERATION_PK:
                raise DeliberationError(
                    "already_exists",
                    f"deliberation {row.get('deliberation_id')} already exists",
                ) from exc
            if constraint == _ACTIVE_TICKET:
                raise DeliberationError(
                    "already_exists",
                    "an active deliberation already exists for this ticket",
                ) from exc
            raise
        for key in _CHILD_KEYS:
            created[key] = []
        return created

    async def save_participant(self, row: dict[str, Any]) -> dict[str, Any]:
        return await _insert(
            "autonomy_deliberation_participants",
            row,
            _PARTICIPANT_FIELDS,
            _PARTICIPANT_COLUMNS,
            _participant,
        )

    async def save_round(self, row: dict[str, Any]) -> dict[str, Any]:
        return await _insert(
            "autonomy_deliberation_rounds",
            row,
            _ROUND_FIELDS,
            _ROUND_COLUMNS,
            _round,
        )

    async def mark_round_closed(self, round_id: str) -> None:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    UPDATE autonomy_deliberation_rounds
                    SET status = 'closed',
                        closed_at = COALESCE(closed_at, NOW())
                    WHERE round_id = %s
                    RETURNING round_id
                    """,
                    (round_id,),
                )
                if await cur.fetchone() is None:
                    raise DeliberationError(
                        "not_found",
                        f"round {round_id} not found",
                    )

    async def save_message(self, row: dict[str, Any]) -> dict[str, Any]:
        return await _insert(
            "autonomy_deliberation_messages",
            row,
            _MESSAGE_FIELDS,
            _MESSAGE_COLUMNS,
            _message,
        )

    async def save_vote(self, row: dict[str, Any]) -> dict[str, Any]:
        return await _insert(
            "autonomy_deliberation_votes",
            row,
            _VOTE_FIELDS,
            _VOTE_COLUMNS,
            _vote,
        )

    async def save_dissent(self, row: dict[str, Any]) -> dict[str, Any]:
        return await _insert(
            "autonomy_deliberation_dissents",
            row,
            _DISSENT_FIELDS,
            _DISSENT_COLUMNS,
            _dissent,
        )

    async def save_steps(self, deliberation_id: str, steps: list[dict[str, Any]]) -> None:
        async with get_db_connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM autonomy_deliberation_steps WHERE deliberation_id = %s",
                        (deliberation_id,),
                    )
                    for step in steps:
                        record = {key: step[key] for key in _STEP_FIELDS if key in step}
                        record["deliberation_id"] = deliberation_id
                        cols, params = _columns(record, ("deliberation_id",) + _STEP_FIELDS)
                        placeholders = ", ".join(["%s"] * len(cols))
                        await cur.execute(
                            f"INSERT INTO autonomy_deliberation_steps "
                            f"({', '.join(cols)}) VALUES ({placeholders})",
                            params,
                        )

    async def update(self, deliberation_id: str, **fields: Any) -> dict[str, Any]:
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in _UPDATABLE or key == "updated_at":
                continue
            assignments.append(f"{key} = %s")
            params.append(_bound(key, value))
        if "updated_at" in fields:
            assignments.append("updated_at = %s")
            params.append(fields["updated_at"])
        else:
            assignments.append("updated_at = NOW()")
        params.append(deliberation_id)
        async with get_db_connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"""
                        UPDATE autonomy_deliberations
                        SET {", ".join(assignments)}
                        WHERE deliberation_id = %s
                        RETURNING {_DELIBERATION_COLUMNS}
                        """,
                        params,
                    )
                    row = await cur.fetchone()
                    if row is None:
                        raise DeliberationError(
                            "not_found",
                            f"deliberation {deliberation_id} not found",
                        )
                    agg = _deliberation(row)
                    await _attach_children(cur, agg)
                    return agg

    async def claim_start(self, deliberation_id: str) -> bool:
        """Allow one process to start a draft; running work is never silently replayed."""
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE autonomy_deliberations SET status='running', round_index=0, "
                    "updated_at=NOW() WHERE deliberation_id=%s AND status='draft'",
                    (deliberation_id,),
                )
                return cur.rowcount == 1

    async def request_cancel(self, deliberation_id: str) -> bool:
        """Cancel draft/running work atomically so finalization cannot overwrite it."""
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE autonomy_deliberations SET status='cancelled', updated_at=NOW() "
                    "WHERE deliberation_id=%s AND status IN ('draft', 'running')",
                    (deliberation_id,),
                )
                return cur.rowcount == 1

    async def finalize(
        self,
        deliberation_id: str,
        *,
        status: str,
        round_index: int,
        consensus_plan: dict[str, Any],
        decision_reason: str,
        steps: list[dict[str, Any]],
    ) -> bool:
        """Commit the decision and DAG only while this worker still owns running state."""
        async with get_db_connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE autonomy_deliberations SET status=%s, round_index=%s, "
                        "consensus_plan=%s, decision_reason=%s, updated_at=NOW() "
                        "WHERE deliberation_id=%s AND status='running'",
                        (
                            status,
                            round_index,
                            Json(consensus_plan),
                            decision_reason,
                            deliberation_id,
                        ),
                    )
                    if cur.rowcount != 1:
                        return False
                    await cur.execute(
                        "DELETE FROM autonomy_deliberation_steps WHERE deliberation_id=%s",
                        (deliberation_id,),
                    )
                    for step in steps:
                        record = {key: step[key] for key in _STEP_FIELDS if key in step}
                        record["deliberation_id"] = deliberation_id
                        cols, params = _columns(record, ("deliberation_id",) + _STEP_FIELDS)
                        placeholders = ", ".join(["%s"] * len(cols))
                        await cur.execute(
                            f"INSERT INTO autonomy_deliberation_steps ({', '.join(cols)}) "
                            f"VALUES ({placeholders})",
                            params,
                        )
                return True

    async def fail_running(self, deliberation_id: str, reason: str) -> bool:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE autonomy_deliberations SET status='failed', decision_reason=%s, "
                    "updated_at=NOW() WHERE deliberation_id=%s AND status='running'",
                    (reason[:500], deliberation_id),
                )
                return cur.rowcount == 1

    async def get_status(self, deliberation_id: str) -> str | None:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT status FROM autonomy_deliberations WHERE deliberation_id=%s",
                    (deliberation_id,),
                )
                row = await cur.fetchone()
                return None if row is None else str(row[0])

    async def touch_running(self, deliberation_id: str) -> bool:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE autonomy_deliberations SET updated_at=NOW() "
                    "WHERE deliberation_id=%s AND status='running'",
                    (deliberation_id,),
                )
                return cur.rowcount == 1

    async def load(self, deliberation_id: str) -> dict[str, Any] | None:
        async with get_db_connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"""
                        SELECT {_DELIBERATION_COLUMNS}
                        FROM autonomy_deliberations
                        WHERE deliberation_id = %s
                        """,
                        (deliberation_id,),
                    )
                    row = await cur.fetchone()
                    if row is None:
                        return None
                    agg = _deliberation(row)
                    await _attach_children(cur, agg)
                    return agg

    async def load_for_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        async with get_db_connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"SELECT {_DELIBERATION_COLUMNS} FROM autonomy_deliberations "
                        "WHERE ticket_id=%s ORDER BY created_at DESC LIMIT 1",
                        (ticket_id,),
                    )
                    row = await cur.fetchone()
                    if row is None:
                        return None
                    agg = _deliberation(row)
                    await _attach_children(cur, agg)
                    return agg

    async def fail_stale_running(self, deliberation_id: str, stale_seconds: int = 1800) -> bool:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE autonomy_deliberations SET status='failed', "
                    "decision_reason='worker lease expired; retry creates a new deliberation', updated_at=NOW() "
                    "WHERE deliberation_id=%s AND status='running' "
                    "AND updated_at < NOW() - (%s * INTERVAL '1 second')",
                    (deliberation_id, stale_seconds),
                )
                return cur.rowcount == 1
