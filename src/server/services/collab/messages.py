"""``agent_ask`` / ``agent_inbox``: bounded, untrusted messages (N80 6.4).

Both routes reuse the peer channel limits: at most 2000 characters per
message and 32 unread per recipient (``channel_full`` past that).

* ``to = app:<app_id>``: a ``collab_messages`` row for that app (same user
  only; another user's app is ``not_found``). Read-and-consume unless
  ``peek``.
* ``to = t:<ticket_uuid>``: addressed to the Ticket's current holder. The
  sender must hold a lease (its Ticket is the sending Ticket). A Ticket held
  by another app's lease gets the message in that app's inbox; a Ticket run
  by a server (PTC) Worker goes through ``peer_channel.send_peer_message``
  with that path's scope rules; a native CLI or daemon Worker has no peer
  tool, so the answer is ``not_deliverable``.

Every delivered message is ``untrusted: true`` at the receiver.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from src.collab.types import (
    ChannelFullError,
    InvalidSchemaError,
    NotDeliverableError,
    NotFoundError,
    TaskRef,
    normalize_app_id,
)
from src.server.database.collab import apps as app_db
from src.server.database.collab import leases as lease_db
from src.server.database.collab import messages as message_db
from src.server.services.autonomy import peer_channel
from src.server.services.collab import tickets as task_svc
from src.server.services.collab.leases import iso
from src.server.services.collab.tools import ToolCall, handler


def message_view(row: Mapping[str, Any]) -> dict[str, Any]:
    sender = str(row.get("from_app_id") or "")
    agent = str(row.get("from_agent") or "")
    view: dict[str, Any] = {
        "id": str(row.get("message_id") or ""),
        "from": f"app:{sender}" + (f"/{agent}" if agent else ""),
        "text": str(row.get("text") or "")[:2000],
        "sent_at": iso(row.get("created_at")) or datetime.now(timezone.utc).isoformat(),
        "untrusted": True,
    }
    if row.get("about_ref"):
        view["about"] = str(row["about_ref"])
    return view


async def _app_recipient(call: ToolCall, to: str) -> str:
    try:
        app_id = normalize_app_id(to[4:])
    except ValueError as exc:
        raise InvalidSchemaError("to must be app:<app_id> or t:<ticket_uuid>") from exc
    if app_id == call.caller.app_id:
        raise InvalidSchemaError("an app cannot message itself")
    app = await app_db.get_app(app_id)
    if (
        app is None
        or str(app.get("user_id") or "") != call.caller.user_id
        or app.get("revoked_at") is not None
    ):
        raise NotFoundError("not_found: no such app")
    return f"app:{app_id}"


async def _send_collab(call: ToolCall, target: str, about: str) -> dict[str, Any]:
    async with call.transaction() as conn:
        try:
            row = await message_db.send_message(
                from_app_id=call.caller.app_id,
                from_agent=call.caller.agent,
                to_target=target,
                text=str(call.arguments["text"]),
                about_ref=about,
                reply_to=str(call.arguments.get("reply_to") or "")[:200],
                channel_capacity=peer_channel.peer_channel_capacity(),
                conn=conn,
            )
        except ValueError as exc:
            if isinstance(exc, ChannelFullError):
                raise
            raise InvalidSchemaError(str(exc)) from exc
        return await call.finish(conn, {"message": message_view(row)})


async def _send_to_ticket(call: ToolCall, to: str, about: str) -> dict[str, Any]:
    receiver = await task_svc.load_ticket(to, user_id=call.caller.user_id)
    # A message to a Ticket names that task, so the docs-stage limit applies.
    call.require_task_mode(task_svc.task_facts(receiver))
    if str(receiver.get("status") or "") in task_svc.ENDED_STATUSES:
        raise NotDeliverableError("not_deliverable: the Ticket has ended")
    own = [row for row in await lease_db.list_active_leases(call.caller.app_id) if row.get("ticket_id")]
    if not own:
        raise NotDeliverableError(
            "no_lease: messaging a Ticket's holder needs a lease of your own; "
            "use review_request or wait for a handoff"
        )
    holder = await lease_db.lease_for_target(task_svc.ref_of(receiver))
    if holder is not None:
        if str(holder.get("app_id")) == call.caller.app_id:
            raise InvalidSchemaError("an app cannot message its own Ticket")
        return await _send_collab(call, f"app:{holder['app_id']}", about)
    if not (receiver.get("worker_run_id") or receiver.get("worker_daemon_id") or receiver.get("worker_lease_id")):
        raise NotDeliverableError("no_holder: nobody is working on that Ticket right now")
    if receiver.get("worker_daemon_id") or receiver.get("worker_lease_id"):
        raise NotDeliverableError(
            "native_worker: the Ticket's Worker has no peer channel; use "
            "review_request or wait for a handoff"
        )
    try:
        result = await peer_channel.send_peer_message(
            sender_ticket_id=str(own[0]["ticket_id"]),
            receiver_ticket_id=str(receiver["ticket_id"]),
            text=str(call.arguments["text"]),
        )
    except ValueError as exc:
        raise NotFoundError("not_found: no such peer") from exc
    if result.get("status") == "channel_full":
        raise ChannelFullError("channel_full: the peer's inbox is full")
    return {
        "message": {
            "id": str(result.get("message_id") or ""),
            "from": call.caller.actor,
            "text": str(call.arguments["text"])[:2000],
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "untrusted": True,
            **({"about": about} if about else {}),
        }
    }


@handler("agent_ask")
async def agent_ask(call: ToolCall) -> dict[str, Any]:
    to = str(call.arguments["to"]).strip()
    call.set_target(to)
    about = str(call.arguments.get("about") or "")
    if about:
        # In docs mode a message about a task is limited like the task (L5).
        about_ticket = await task_svc.load_ticket(about, user_id=call.caller.user_id)
        call.require_task_mode(task_svc.task_facts(about_ticket))
    if to.startswith("app:"):
        return await _send_collab(call, await _app_recipient(call, to), about)
    try:
        parsed = TaskRef.parse(to)
    except ValueError as exc:
        raise InvalidSchemaError("to must be app:<app_id> or t:<ticket_uuid>") from exc
    if not parsed.is_ticket:
        raise NotDeliverableError("not_deliverable: dispatch lines take messages after N80 L4")
    return await _send_to_ticket(call, parsed.value, about)


@handler("agent_inbox")
async def agent_inbox(call: ToolCall) -> dict[str, Any]:
    limit = int(call.arguments.get("limit") or 20)
    peek = call.arguments.get("peek") is True
    async with call.transaction() as conn:
        rows = await message_db.inbox(
            to_target=f"app:{call.caller.app_id}", limit=limit, peek=peek, conn=conn
        )
        return await call.finish(conn, {"messages": [message_view(row) for row in rows]})


__all__ = ["agent_ask", "agent_inbox", "message_view"]
