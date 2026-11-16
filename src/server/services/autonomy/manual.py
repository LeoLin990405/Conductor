"""Manual Super-agent tasks: the user's own request becomes one Ticket.

A manual request is explicit by definition. It does not need a Router decision
or a GitHub Source; it gets a synthetic manual Source row, a Trigger/Thread for
audit and subscriptions, and a queued Ticket whose Worker starts once the
prompt passes the clarification gate (or immediately when the gate is off) and,
when the prompt refiner is on, once the user has confirmed its rewrite (a
``/raw`` prefix or ``no_refine`` runs the request as written instead).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from src.server.database.autonomy import sources as source_db
from src.server.database.autonomy import tickets as ticket_db
from src.server.database.autonomy import triggers as trigger_db
from src.server.database.autonomy.effects import enqueue_effect
from src.server.services.autonomy import policy, prompt_refiner
from src.server.services.autonomy.clarify import clarify_enabled, gate
from src.server.services.autonomy.github import stable_trigger_id
from src.server.services.autonomy.models import EFFECT_LAUNCH_WORKER
from src.server.services.autonomy.prompt_refiner import REFINE_OFF

logger = logging.getLogger(__name__)


async def _manual_source(user_id: str, workspace_id: str) -> dict[str, Any]:
    sources = await source_db.list_sources(user_id)
    for source in sources:
        if source.get("kind") == "manual":
            return source
    return await source_db.create_source(
        user_id=user_id,
        kind="manual",
        config={
            "workspace_id": workspace_id,
            "allowed_logins": [user_id],
            "poll_seconds": 0,
        },
    )


async def create_manual_task(
    *,
    user_id: str,
    workspace_id: str,
    prompt: str,
    title: str = "",
    worker_provider: str = "",
    worker_model: str = "",
    worker_daemon_id: str | None = None,
    cwd: str | None = None,
    external_key: str | None = None,
    schedule_id: str | None = None,
    no_refine: bool = False,
    hold_for_confirmation: bool = False,
) -> dict[str, Any]:
    # Off (the default) leaves the prompt exactly as before. With the refiner
    # on, a /raw prefix or an explicit no_refine flag runs the request as
    # written, and the control prefix is not part of the request.
    refine = prompt_refiner.refine_mode()
    request = prompt
    if refine != REFINE_OFF:
        request, raw_requested = prompt_refiner.strip_raw_prefix(prompt)
        if raw_requested and not request.strip():
            raise ValueError("usage: /raw <request>")
        # An unattended entry (a due schedule) is always held: /raw and
        # no_refine skip the refiner, never the confirmation hold.
        if (no_refine or raw_requested) and not hold_for_confirmation:
            refine = REFINE_OFF
    source = await _manual_source(user_id, workspace_id)
    task_key = external_key or uuid4().hex
    thread_key = f"manual:{user_id}:{task_key}"
    trigger_id = stable_trigger_id(str(source["source_id"]), f"manual:{task_key}")
    inserted = await trigger_db.insert_trigger(
        trigger_id=trigger_id,
        source_id=str(source["source_id"]),
        user_id=user_id,
        external_id=f"manual:{task_key}",
        kind="manual",
        thread_key=thread_key,
        actor_login=user_id,
        actor_is_bot=False,
        addressed=True,
        text=prompt[:20000],
        url="",
        payload={"manual": True},
    )
    if inserted is None:
        existing = await ticket_db.get_ticket_by_origin_trigger(trigger_id)
        if existing is not None:
            return existing
    await trigger_db.ensure_thread(
        thread_key=thread_key,
        user_id=user_id,
        source_id=str(source["source_id"]),
        meta={"manual": True},
    )
    current = await policy.current_policy(user_id)
    gate_enabled = clarify_enabled()
    ticket = await ticket_db.create_ticket(
        user_id=user_id,
        workspace_id=workspace_id,
        title=(title.strip() or request.strip().splitlines()[0])[:120],
        scope=request[:20000],
        mandate={
            "strength": "explicit",
            "source": "manual",
            "source_id": str(source["source_id"]),
            "trigger_id": trigger_id,
            "scope": request[:1500],
            **({"cwd": cwd} if cwd is not None else {}),
            **({"schedule_id": schedule_id} if schedule_id else {}),
        },
        policy_version=str(current.get("version") or ""),
        origin_trigger_id=trigger_id,
        origin_thread_key=thread_key,
        worker_provider=worker_provider,
        worker_model=worker_model,
        worker_daemon_id=worker_daemon_id,
        initial_status="awaiting_clarification" if gate_enabled else "queued",
    )
    await ticket_db.add_subscription(str(ticket["ticket_id"]), thread_key, kind="own")
    outcome = await gate(ticket=ticket, enabled=gate_enabled)
    if outcome == "passed" and refine != REFINE_OFF:
        # An unattended entry (a due schedule) has nobody to confirm a
        # rewrite, so its request is held instead of being refined.
        if hold_for_confirmation:
            outcome = await prompt_refiner.hold_gate(ticket=ticket, mode=refine)
        else:
            outcome = await prompt_refiner.refine_gate(ticket=ticket, mode=refine)
    if outcome == "passed":
        await enqueue_effect(
            user_id=user_id,
            kind=EFFECT_LAUNCH_WORKER,
            idempotency_key=f"autonomy:launch:{ticket['ticket_id']}",
            payload={"ticket_id": str(ticket["ticket_id"])},
        )
    ticket = await ticket_db.get_ticket(str(ticket["ticket_id"])) or ticket
    logger.info("[autonomy] manual ticket=%s thread=%s", ticket["ticket_id"], thread_key)
    return ticket
