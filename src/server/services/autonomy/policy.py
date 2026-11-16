"""Autonomy policy: which GitHub events may enter an Eval.

The default is explicit requests from configured GitHub logins. ``observe``
also lets the Router run proactive observations through a read-only,
plan-only Worker before a person approves; ``auto`` runs the real work. The
admission table maps a receiving sender/mention cell to explicit, inferred or
ignore. The policy is a versioned row so every Ticket records the rules it was
accepted under.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection
from src.server.services.autonomy.routing import source_allows_candidate, source_mode


DEFAULT_ADMISSION = {
    "listed_addressed": "explicit",
    "listed_unaddressed": "inferred",
    "unlisted_addressed": "ignore",
    "unlisted_unaddressed": "ignore",
    "bot": "ignore",
}
_ADMISSION_CELLS = {
    "listed_addressed": {"explicit", "inferred", "ignore"},
    "listed_unaddressed": {"inferred", "ignore"},
    "unlisted_addressed": {"explicit", "inferred", "ignore"},
    "unlisted_unaddressed": {"inferred", "ignore"},
    "bot": {"inferred", "ignore"},
}


def admission_table(raw: dict[str, Any] | None) -> dict[str, str]:
    table = dict(DEFAULT_ADMISSION)
    for key, value in (raw or {}).items():
        if key in _ADMISSION_CELLS and value in _ADMISSION_CELLS[key]:
            table[key] = str(value)
    return table


def effective_admission(
    policy_admission: dict[str, Any] | None, config: dict[str, Any]
) -> dict[str, str]:
    """Merge a Source's optional admission overrides over the user policy."""
    overrides = config.get("admission")
    if not isinstance(overrides, dict):
        overrides = {}
    return admission_table({**admission_table(policy_admission), **overrides})


def version_of(
    rules: list[dict[str, Any]],
    accept_mode: str = "explicit",
    admission: dict[str, Any] | None = None,
) -> str:
    payload = json.dumps(
        {
            "rules": rules,
            "accept_mode": accept_mode,
            "admission": admission_table(admission),
        },
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


async def current_policy(user_id: str) -> dict[str, Any]:
    """Return the current policy, creating the V1 explicit-only row if absent."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT policy_id, user_id, version, rules, accept_mode, admission
                FROM autonomy_policies
                WHERE user_id = %s AND is_current
                LIMIT 1
                """,
                (user_id,),
            )
            row = await cur.fetchone()
            if row is not None:
                return {
                    **dict(row),
                    "policy_id": str(row["policy_id"]),
                    "rules": list(row.get("rules") or []),
                    "admission": admission_table(row.get("admission")),
                }
            version = version_of([])
            await cur.execute(
                """
                INSERT INTO autonomy_policies (user_id, version, rules, accept_mode, is_current)
                VALUES (%s, %s, %s, 'explicit', TRUE)
                ON CONFLICT (user_id, version) DO NOTHING
                RETURNING policy_id, user_id, version, rules, accept_mode, admission
                """,
                (user_id, version, Json([])),
            )
            created = await cur.fetchone()
            if created is None:
                await cur.execute(
                    """
                    SELECT policy_id, user_id, version, rules, accept_mode, admission
                    FROM autonomy_policies
                    WHERE user_id = %s AND is_current
                    LIMIT 1
                    """,
                    (user_id,),
                )
                created = await cur.fetchone()
            return {
                **dict(created or {}),
                "policy_id": str((created or {}).get("policy_id") or ""),
                "rules": list((created or {}).get("rules") or []),
                "admission": admission_table((created or {}).get("admission")),
            }


def admit(
    trigger: dict[str, Any],
    config: dict[str, Any],
    admission: dict[str, Any] | None = None,
) -> str | None:
    """Return the Trigger's admission strength, or None when ignored."""
    table = admission_table(admission)
    login = str(trigger.get("actor_login") or "").lower()
    allowed = {
        str(item).strip().lower()
        for item in (config.get("allowed_logins") or [])
        if str(item).strip()
    }
    if trigger.get("actor_is_bot"):
        bots = {
            str(item).strip().lower()
            for item in (config.get("bot_logins") or [])
            if str(item).strip()
        }
        if login not in bots:
            return None
        cell = "bot"
    elif login and login in allowed:
        cell = "listed_addressed" if trigger.get("addressed") else "listed_unaddressed"
    else:
        cell = "unlisted_addressed" if trigger.get("addressed") else "unlisted_unaddressed"
    strength = table.get(cell, "ignore")
    return None if strength == "ignore" else strength


def is_candidate(
    trigger: dict[str, Any],
    config: dict[str, Any],
    accept_mode: str = "explicit",
    admission: dict[str, Any] | None = None,
) -> bool:
    """Whether an event may enter an Eval under this policy mode."""
    strength = admit(trigger, config, admission)
    if strength == "explicit":
        return True
    return strength == "inferred" and accept_mode in {"observe", "auto"}


def is_source_candidate(
    trigger: dict[str, Any],
    config: dict[str, Any],
    accept_mode: str = "explicit",
    admission: dict[str, Any] | None = None,
) -> bool:
    """Apply per-Source routing mode without weakening admission policy."""
    return source_allows_candidate(trigger, config, accept_mode, admission)


def is_explicit(trigger: dict[str, Any], config: dict[str, Any]) -> bool:
    """V1 accepts only an addressed comment from an allowed login."""
    login = str(trigger.get("actor_login") or "").lower()
    allowed = {
        str(item).strip().lower()
        for item in (config.get("allowed_logins") or [])
        if str(item).strip()
    }
    if trigger.get("actor_is_bot"):
        return False
    if not allowed or login not in allowed:
        return False
    return bool(trigger.get("addressed"))


async def _replace_current(
    user_id: str,
    *,
    accept_mode: str,
    admission: dict[str, Any],
) -> dict[str, Any]:
    current = await current_policy(user_id)
    rules = list(current.get("rules") or [])
    version = version_of(rules, accept_mode, admission)
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "UPDATE autonomy_policies SET is_current = FALSE "
                    "WHERE user_id = %s AND is_current",
                    (user_id,),
                )
                await cur.execute(
                    """
                    INSERT INTO autonomy_policies
                        (user_id, version, rules, accept_mode, admission, is_current)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (user_id, version)
                    DO UPDATE SET is_current = TRUE
                    RETURNING policy_id, user_id, version, rules, accept_mode, admission
                    """,
                    (user_id, version, Json(rules), accept_mode, Json(admission)),
                )
                row = await cur.fetchone()
    return {
        **dict(row or {}),
        "policy_id": str((row or {}).get("policy_id") or ""),
        "rules": list((row or {}).get("rules") or []),
        "admission": admission_table((row or {}).get("admission")),
    }


async def set_accept_mode(user_id: str, accept_mode: str) -> dict[str, Any]:
    current = await current_policy(user_id)
    if str(current.get("accept_mode") or "explicit") == accept_mode:
        return current
    return await _replace_current(
        user_id,
        accept_mode=accept_mode,
        admission=dict(current.get("admission") or DEFAULT_ADMISSION),
    )


async def set_admission(user_id: str, admission: dict[str, Any]) -> dict[str, Any]:
    current = await current_policy(user_id)
    table = admission_table({**dict(current.get("admission") or {}), **admission})
    if dict(current.get("admission") or DEFAULT_ADMISSION) == table:
        return current
    return await _replace_current(
        user_id,
        accept_mode=str(current.get("accept_mode") or "explicit"),
        admission=table,
    )


async def policy_changed(user_id: str, *, reason: str = "policy changed") -> int:
    """Discard active Evals so their Triggers re-enter under the new Policy."""
    from src.server.database.autonomy import evals as eval_db

    return await eval_db.discard_evals_for_user(user_id, reason=reason)


def revalidate(
    trigger: dict[str, Any],
    config: dict[str, Any],
    admission: dict[str, Any] | None,
    *,
    strength: str,
    accept_mode: str,
) -> tuple[bool, str]:
    """Whether a Ticket's current Mandate still holds for a retry.

    A weaker current strength revokes an explicit Mandate; an inferred Mandate
    also needs an accept mode that still permits inferred work. A person's
    approve of a plan-only observe Ticket is intentionally not re-litigated
    here; the retry is the explicit human action.
    """
    mode = source_mode(config)
    if mode == "ignore":
        return False, "this Source is now ignored"
    if mode == "respond-only-explicit" and strength != "explicit":
        return False, "this Source now accepts only explicit requests"
    current = admit(trigger, config, admission)
    if current is None:
        return False, "the sender is no longer admitted by this Source"
    if strength == "explicit" and current != "explicit":
        return False, "the sender no longer has explicit strength"
    if strength == "inferred":
        if current not in {"explicit", "inferred"}:
            return False, "the sender is no longer admitted"
        if accept_mode not in {"observe", "auto"}:
            return False, "inferred work is no longer accepted"
    return True, ""


async def notify_policy_change(
    user_id: str, *, before: dict[str, Any], after: dict[str, Any]
) -> int:
    """Give each open Ticket an Input describing the Policy change.

    A running Worker gets the Input delivered as a follow-up. A Ticket without
    a Worker only stores the Input; the next Worker turn sees it in its report
    history and a person sees it in the Ticket detail.
    """
    from src.server.database.autonomy import tickets as ticket_db
    from src.server.database.autonomy.effects import enqueue_effect
    from src.server.services.autonomy.models import EFFECT_DELIVER_INPUT

    tickets = await ticket_db.list_open_for_user(user_id)
    notice = (
        f"Policy changed from {before.get('version') or 'unknown'} to "
        f"{after.get('version') or 'unknown'} "
        f"(mode {after.get('accept_mode') or 'explicit'}). "
        "Re-check this Ticket's scope before continuing."
    )
    notified = 0
    for ticket in tickets:
        ticket_id = str(ticket.get("ticket_id") or "")
        if not ticket_id:
            continue
        input_row = await ticket_db.add_input(
            ticket_id=ticket_id, trigger_id=None, kind="policy", text=notice
        )
        if ticket.get("status") == "running" and ticket.get("worker_thread_id"):
            await enqueue_effect(
                user_id=user_id,
                kind=EFFECT_DELIVER_INPUT,
                idempotency_key=f"autonomy:policy-input:{ticket_id}:{after.get('version')}",
                payload={
                    "ticket_id": ticket_id,
                    "input_id": input_row["input_id"],
                },
            )
        notified += 1
    return notified
