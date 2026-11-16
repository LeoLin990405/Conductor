"""Durable outbound replies and Worker report relays.

Every reply goes through the channel model. The Source decides the channel,
the channel decides the reply's form and its limits, and the channel's
approval rules decide whether the reply may leave the system now — or has to
wait for a person to read it first.

Delivery is reconcile-before-retry. Each outbound side effect carries the
outbox row's idempotency key to the destination — as a marker comment
(GitHub), a marker line (Feishu), or a deterministic ``Message-ID`` (email) —
and a retry first asks the destination whether the marker is already there.
A send that committed remotely but lost its local acknowledgement then
settles as ``sent`` without a duplicate post. Providers without a read-back
(the Slack webhook is write-only; SMTP has no mailbox scan here) stay
at-least-once, narrowed only by the key they carry.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping

from src.server.database.autonomy import outbox as outbox_db
from src.server.database.autonomy import sources as source_db
from src.server.database.autonomy import tickets as ticket_db
from src.server.services.autonomy.channels import (
    ACTION_HOLD,
    ACTION_SUPPRESS,
    KIND_RESULT,
    TRANSPORT_EMAIL,
    TRANSPORT_FEISHU,
    TRANSPORT_GITHUB,
    TRANSPORT_LOCAL,
    TRANSPORT_SLACK,
    decide_delivery,
    resolve_channel,
    shape_reply,
    transport_for,
)
from src.server.services.autonomy.effects import PermanentEffectError
from src.server.services.autonomy.email import (
    build_mail_message,
    message_id_for_key,
    send_mail_message,
)
from src.server.services.autonomy.feishu import (
    find_marker_message,
    send_feishu_message,
)
from src.server.services.autonomy.github import (
    find_marker_comment,
    post_github_comment,
)
from src.server.services.autonomy.router import dispatch_report
from src.server.services.autonomy.slack import send_slack_message

logger = logging.getLogger(__name__)


def reply_marker(idempotency_key: str) -> str:
    """The destination-visible marker for one logical reply.

    Derived from the outbox idempotency key, not the row id, so the same
    reply reconciles against destinations even if its row is ever re-created.
    The digest keeps reply text out of the marker and stays inside the
    HTML-comment grammar (hex, no ``--``).
    """
    digest = hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()[:32]
    return f"<!-- conductor-autonomy:{digest} -->"


# --- outbound endpoint authorization ----------------------------------------
#
# This layer is independent of any prompt instruction: it reads only the
# Source configuration, the outbox row's identity fields and the Ticket's
# origin, so a Worker (or a Router) whose text was injected cannot steer a
# reply to a destination the Source and the Ticket origin do not name. The
# reply *content* is out of scope here — injection that changes what a reply
# says is a content-layer risk, documented in docs/ports/credential-broker.md.

#: Stable reason codes for ``OutboundDecision``; documented in
#: docs/ports/credential-broker.md and asserted by tests.
OUTBOUND_OK = "ok"
OUTBOUND_MISSING_TICKET_ORIGIN = "missing_ticket_origin"
OUTBOUND_CROSS_SOURCE = "cross_source"
OUTBOUND_THREAD_MISMATCH = "thread_mismatch"
OUTBOUND_ORIGIN_THREAD_MISSING = "origin_thread_missing"
OUTBOUND_DESTINATION_NOT_FROM_SOURCE = "destination_not_from_source"
OUTBOUND_RECIPIENT_MISMATCH = "recipient_mismatch"
OUTBOUND_UNCHECKED_TRANSPORT = "unchecked_transport"

_GITHUB_THREAD_RE = re.compile(r"^github:(?P<repository>.+)#(?P<number>\d+)$")
# A Ticket origin thread must name an owner, a repository and a positive
# issue number: `#0` and free-form keys are not GitHub threads.
_TICKET_ORIGIN_THREAD_RE = re.compile(r"^github:[^/\s]+/[^/\s]+#[1-9][0-9]*$")


@dataclass(frozen=True, slots=True)
class OutboundDecision:
    """One outbound delivery authorization.

    ``reason_code`` is one of the stable ``OUTBOUND_*`` constants;
    ``detail`` is a short, log-safe summary that never carries reply text.
    """

    allowed: bool
    reason_code: str
    detail: str = ""


def _allowed_sources(ticket_origin: Mapping[str, Any]) -> frozenset[str]:
    origin = str(ticket_origin.get("source_id") or "").strip()
    reply_sources = {
        str(item).strip()
        for item in (ticket_origin.get("reply_source_ids") or ())
        if str(item).strip()
    }
    return frozenset({origin, *reply_sources}) - {""}


def authorize_outbound(
    transport: str,
    source_config: Mapping[str, Any],
    outbox_row: Mapping[str, Any],
    ticket_origin: Mapping[str, Any] | None,
) -> OutboundDecision:
    """Decide, independently of any prompt, whether this reply may be sent.

    Called by :func:`reply_effect` before any transport work. The rules:

    * the reply may only leave for the Ticket's origin Source, or a Source the
      user explicitly configured as a reply destination (``reply_source_ids``
      on the Ticket mandate); any other destination is a cross-Source delivery
      and is denied;
    * every destination field (repository, issue number, channel, recipient)
      must come from the Source configuration or the Ticket origin — never
      from Worker- or Router-generated text, which reaches this layer only as
      the reply ``body`` and is ignored here;
    * a GitHub reply's thread (issue/PR) must be the Ticket's origin thread;
    * an email recipient must equal the Source configuration's ``to_address``;
      an outbox row cannot override it.

    ``ticket_origin`` carries ``source_id`` and ``thread_key`` (the Ticket's
    origin) plus the optional explicit ``reply_source_ids``. An unknown or
    incomplete origin denies external delivery — the check fails closed; the
    ``local`` transport has no external destination and is always allowed. A
    transport this layer does not know is left to the existing fallback in
    :func:`reply_effect` (``unchecked_transport``): nothing is sent either
    way.
    """
    transport_text = str(transport or "").strip().lower()
    row_source = str(outbox_row.get("source_id") or "").strip()
    if transport_text == TRANSPORT_LOCAL:
        return OutboundDecision(True, OUTBOUND_OK)
    if transport_text not in {
        TRANSPORT_GITHUB,
        TRANSPORT_SLACK,
        TRANSPORT_EMAIL,
        TRANSPORT_FEISHU,
    }:
        return OutboundDecision(True, OUTBOUND_UNCHECKED_TRANSPORT)
    origin_source = str((ticket_origin or {}).get("source_id") or "").strip()
    if not origin_source:
        return OutboundDecision(
            False,
            OUTBOUND_MISSING_TICKET_ORIGIN,
            "the ticket origin Source is unknown",
        )
    allowed_sources = _allowed_sources(ticket_origin or {})
    if row_source not in allowed_sources:
        return OutboundDecision(
            False,
            OUTBOUND_CROSS_SOURCE,
            "the reply destination is not the ticket origin Source or a "
            "configured reply Source",
        )
    if transport_text == TRANSPORT_GITHUB:
        row_thread = str(outbox_row.get("thread_key") or "").strip()
        origin_thread = str((ticket_origin or {}).get("thread_key") or "").strip()
        # Fail closed on a missing or malformed origin thread (`#0`, free-form
        # keys included): there is no GitHub thread to bind the reply to, so a
        # GitHub reply never leaves on the strength of the row alone.
        if not origin_thread or _TICKET_ORIGIN_THREAD_RE.fullmatch(origin_thread) is None:
            return OutboundDecision(
                False,
                OUTBOUND_ORIGIN_THREAD_MISSING,
                "the ticket's GitHub origin thread is missing or malformed",
            )
        if row_thread != origin_thread:
            return OutboundDecision(
                False,
                OUTBOUND_THREAD_MISMATCH,
                "the reply thread is not the ticket's origin thread",
            )
        repository = str(source_config.get("repository") or "").strip()
        if repository:
            match = _GITHUB_THREAD_RE.fullmatch(row_thread)
            if match is None or match.group("repository").lower() != repository.lower():
                return OutboundDecision(
                    False,
                    OUTBOUND_DESTINATION_NOT_FROM_SOURCE,
                    "the reply thread does not name the Source's repository",
                )
    if transport_text == TRANSPORT_EMAIL:
        configured = str(source_config.get("to_address") or "").strip().lower()
        override = str(outbox_row.get("to_address") or "").strip().lower()
        if override and override != configured:
            return OutboundDecision(
                False,
                OUTBOUND_RECIPIENT_MISMATCH,
                "the outbox row may not override the Source's recipient",
            )
    return OutboundDecision(True, OUTBOUND_OK)


def shape_channel_reply(kind: str, text: str) -> str:
    """Render one reply in the channel's own form.

    Kept as the kind-based entry point; the channel model owns the rules.
    """
    return shape_reply(resolve_channel(kind), text)


async def reply_effect(effect) -> None:
    outbox_id = str(effect.payload.get("outbox_id") or "")
    if not outbox_id:
        raise ValueError("reply effect has no outbox_id")
    outbox = await outbox_db.get_outbox(outbox_id)
    if outbox is None or outbox["status"] != "pending":
        return
    source_id = str(outbox.get("source_id") or "").strip()
    if not source_id:
        error = "reply outbox has no Source id"
        await outbox_db.mark_dropped(outbox_id, error)
        raise PermanentEffectError(error)
    source = await source_db.get_source_any_user(source_id)
    if source is None or not source.get("enabled", True):
        error = "reply Source is not found or is disabled"
        await outbox_db.mark_dropped(outbox_id, error)
        raise PermanentEffectError(error)
    kind = str(source.get("kind") or "")
    config = dict(source.get("config") or {})
    spec = resolve_channel(kind, config)
    reply_kind = str(outbox.get("reply_kind") or KIND_RESULT)
    decision = decide_delivery(spec, kind=reply_kind, config=config)
    if decision.action == ACTION_SUPPRESS:
        await outbox_db.mark_dropped(outbox_id, decision.reason)
        logger.info(
            "[autonomy] reply suppressed outbox=%s channel=%s reason=%s",
            outbox_id,
            spec.channel,
            decision.reason,
        )
        return
    # A reply a person has already read is delivered even when the channel
    # asks for approval: the approval is recorded on the row.
    approved = bool(outbox.get("approved_at"))
    if decision.action == ACTION_HOLD and not approved:
        await outbox_db.mark_pending_approval(outbox_id, decision.reason)
        logger.info(
            "[autonomy] reply held for approval outbox=%s channel=%s",
            outbox_id,
            spec.channel,
        )
        return
    body = shape_reply(spec, str(outbox["body"]))
    transport = transport_for(spec, kind)
    # Outbound authorization, before any transport work — including the GitHub
    # and Feishu reconcile reads. This check is independent of prompt
    # instructions: an injected model cannot change the destination, which
    # comes only from the Source configuration and the Ticket origin.
    ticket_id = str(outbox.get("ticket_id") or "")
    ticket = await ticket_db.get_ticket(ticket_id) if ticket_id else None
    mandate = dict((ticket or {}).get("mandate") or {})
    ticket_origin = {
        "source_id": str(mandate.get("source_id") or ""),
        "thread_key": str((ticket or {}).get("origin_thread_key") or ""),
        "reply_source_ids": tuple(
            str(s) for s in (mandate.get("reply_source_ids") or ()) if str(s).strip()
        ),
    }
    authz = authorize_outbound(transport, config, outbox, ticket_origin)
    if not authz.allowed:
        error = f"outbound reply denied: {authz.reason_code}"
        await outbox_db.mark_dropped(outbox_id, error)
        logger.info(
            "[autonomy] outbound reply denied outbox=%s transport=%s reason=%s",
            outbox_id,
            transport,
            authz.reason_code,
        )
        raise PermanentEffectError(error)
    user_id = str(outbox["user_id"])
    idempotency_key = str(outbox.get("idempotency_key") or "") or outbox_id
    marker = reply_marker(idempotency_key)
    if transport == TRANSPORT_FEISHU:
        # Reconcile before sending: a previous attempt may have committed the
        # message while its local acknowledgement was lost.
        if await find_marker_message(config=config, marker=marker):
            await outbox_db.mark_sent(outbox_id)
            logger.info(
                "[autonomy] feishu reply already present outbox=%s", outbox_id
            )
            return
        await send_feishu_message(config, f"{body}\n\n{marker}")
        await outbox_db.mark_sent(outbox_id)
        logger.info("[autonomy] feishu reply sent outbox=%s", outbox_id)
        return
    if transport == TRANSPORT_SLACK:
        # The incoming webhook is write-only: there is nothing to reconcile
        # against, so this transport stays at-least-once by API behavior.
        await send_slack_message(user_id, config, body)
        await outbox_db.mark_sent(outbox_id)
        logger.info("[autonomy] slack reply sent outbox=%s", outbox_id)
        return
    if transport == TRANSPORT_EMAIL:
        mail = build_mail_message(
            to=str(config.get("to_address") or ""),
            text=body,
            subject=spec.default_subject,
            headers={
                "X-Conductor-Outbox": outbox_id,
                # Deterministic per idempotency key: a retried send renders
                # the same Message-ID, so duplicate-aware receivers (and a
                # future Sent-folder reconcile) can recognise the resend.
                "Message-ID": message_id_for_key(
                    idempotency_key,
                    from_address=str(config.get("from_address") or ""),
                ),
            },
        )
        await send_mail_message(user_id, config, mail)
        await outbox_db.mark_sent(outbox_id)
        logger.info("[autonomy] email reply sent outbox=%s", outbox_id)
        return
    if transport == TRANSPORT_GITHUB:
        if await find_marker_comment(
            user_id=user_id,
            config=config,
            thread_key=str(outbox["thread_key"]),
            marker=marker,
        ):
            await outbox_db.mark_sent(outbox_id)
            logger.info(
                "[autonomy] reply already present outbox=%s ticket=%s",
                outbox_id,
                outbox.get("ticket_id"),
            )
            return
        await post_github_comment(
            user_id=user_id,
            config=config,
            thread_key=str(outbox["thread_key"]),
            body=f"{body}\n\n{marker}",
        )
        await outbox_db.mark_sent(outbox_id)
        logger.info("[autonomy] reply sent outbox=%s ticket=%s", outbox_id, outbox.get("ticket_id"))
        return
    if transport == TRANSPORT_LOCAL:
        # The reply is recorded in the Conductor inbox. There is no external
        # channel to deliver it to, so the row is settled without a transport.
        await outbox_db.mark_sent(outbox_id)
        logger.info("[autonomy] local reply recorded outbox=%s", outbox_id)
        return
    error = f"no delivery transport for channel {spec.channel!r}"
    await outbox_db.mark_dropped(outbox_id, error)
    raise PermanentEffectError(error)


async def relay_report_effect(effect) -> None:
    """Send a Worker report to the Router so it can decide the human-facing reply."""
    ticket_id = str(effect.payload.get("ticket_id") or "")
    if not ticket_id:
        raise ValueError("relay effect has no ticket_id")
    ticket = await ticket_db.get_ticket(ticket_id)
    report = await ticket_db.latest_report(ticket_id)
    if ticket is None or report is None:
        return
    await dispatch_report(ticket=ticket, report=report)
