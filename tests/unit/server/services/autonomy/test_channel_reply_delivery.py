"""One reply, one channel: the form it takes and the road it travels.

The channel model is only worth anything if the reply path obeys it. These
tests follow one reply from the Router's tool call to the transport that
carries it, and they check the three decisions the model makes: the reply's
form, whether it may be delivered at all, and whether a person has to read it
first.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy.effects import PermanentEffectError
from src.server.services.autonomy.host import AutonomyToolHost
from src.server.services.autonomy.replies import reply_effect, reply_marker


def _effect(outbox_id: str = "outbox-1"):
    return type("Effect", (), {"payload": {"outbox_id": outbox_id}})()


def _ticket(source_id: str = "source-1", origin_thread_key: str = "thread-1") -> dict:
    return {
        "ticket_id": "t-1",
        "user_id": "u-1",
        "title": "Fix the build",
        "status": "running",
        "origin_thread_key": origin_thread_key,
        "mandate": {"source_id": source_id},
    }


def _source(kind: str, config: dict | None = None) -> dict:
    return {
        "source_id": "source-1",
        "user_id": "u-1",
        "kind": kind,
        "enabled": True,
        "config": config if config is not None else {"workspace_id": "w-1"},
    }


def _outbox(**overrides) -> dict:
    row = {
        "outbox_id": "outbox-1",
        "status": "pending",
        "source_id": "source-1",
        "user_id": "u-1",
        "ticket_id": "t-1",
        "thread_key": "thread-1",
        "body": "Fixed the build.",
        "reply_kind": "result",
        "idempotency_key": "autonomy:reply:t-1:result:18:Fixed the build.",
    }
    row.update(overrides)
    return row


# --- the Router's tool call ------------------------------------------------


@pytest.mark.asyncio
async def test_a_progress_update_is_not_delivered_on_an_im_channel():
    """IM channels notify only for a decision, a question or a failure."""
    enqueue_outbox = AsyncMock()
    enqueue_effect = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.host.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.services.autonomy.host.source_db.get_source_any_user",
            AsyncMock(return_value=_source("feishu")),
        ),
        patch(
            "src.server.services.autonomy.host.outbox_db.enqueue_outbox", enqueue_outbox
        ),
        patch("src.server.services.autonomy.host.enqueue_effect", enqueue_effect),
    ):
        result = await AutonomyToolHost(user_id="u-1", thread_id="router").reply(
            ticket_id="t-1", text="Still working on it", kind="progress"
        )

    assert result["status"] == "suppressed"
    assert result["channel"] == "feishu"
    enqueue_outbox.assert_not_awaited()
    enqueue_effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_result_on_an_im_channel_is_queued_and_shaped_for_that_channel():
    enqueue_outbox = AsyncMock(return_value={"outbox_id": "outbox-1"})
    enqueue_effect = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.host.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.services.autonomy.host.source_db.get_source_any_user",
            AsyncMock(return_value=_source("feishu")),
        ),
        patch(
            "src.server.services.autonomy.host.outbox_db.enqueue_outbox", enqueue_outbox
        ),
        patch("src.server.services.autonomy.host.enqueue_effect", enqueue_effect),
    ):
        result = await AutonomyToolHost(user_id="u-1", thread_id="router").reply(
            ticket_id="t-1", text="word " * 130, kind="result"
        )

    assert result["status"] == "pending"
    assert result["channel"] == "feishu"
    kwargs = enqueue_outbox.await_args.kwargs
    assert kwargs["status"] == "pending"
    assert kwargs["channel"] == "feishu"
    assert kwargs["reply_kind"] == "result"
    assert len(kwargs["body"].split()) == 100
    enqueue_effect.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_email_draft_is_held_for_a_person_instead_of_being_sent():
    enqueue_outbox = AsyncMock(return_value={"outbox_id": "outbox-1"})
    enqueue_effect = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.host.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.services.autonomy.host.source_db.get_source_any_user",
            AsyncMock(return_value=_source("email")),
        ),
        patch(
            "src.server.services.autonomy.host.outbox_db.enqueue_outbox", enqueue_outbox
        ),
        patch("src.server.services.autonomy.host.enqueue_effect", enqueue_effect),
    ):
        result = await AutonomyToolHost(user_id="u-1", thread_id="router").reply(
            ticket_id="t-1", text="Fixed the build."
        )

    assert result["status"] == "pending_approval"
    assert result["channel"] == "email"
    kwargs = enqueue_outbox.await_args.kwargs
    assert kwargs["status"] == "pending_approval"
    assert kwargs["body"].startswith("Subject: Conductor update: Fix the build")
    assert "\n\nSummary:\nFixed the build." in kwargs["body"]
    enqueue_effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_replies_of_different_kinds_are_two_replies():
    """A progress update and a result are not the same message."""
    enqueue_outbox = AsyncMock(return_value={"outbox_id": "outbox-1"})
    keys: list[str] = []

    async def record(**kwargs):
        keys.append(kwargs["idempotency_key"])
        return {"outbox_id": "outbox-1"}

    enqueue_outbox.side_effect = record
    with (
        patch(
            "src.server.services.autonomy.host.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.services.autonomy.host.source_db.get_source_any_user",
            AsyncMock(return_value=_source("github")),
        ),
        patch(
            "src.server.services.autonomy.host.outbox_db.enqueue_outbox", enqueue_outbox
        ),
        patch("src.server.services.autonomy.host.enqueue_effect", AsyncMock()),
    ):
        host = AutonomyToolHost(user_id="u-1", thread_id="router")
        await host.reply(ticket_id="t-1", text="Same text", kind="progress")
        await host.reply(ticket_id="t-1", text="Same text", kind="result")

    assert len(set(keys)) == 2


@pytest.mark.asyncio
async def test_an_unknown_reply_kind_is_refused_before_anything_is_written():
    enqueue_outbox = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.host.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.services.autonomy.host.source_db.get_source_any_user",
            AsyncMock(return_value=_source("github")),
        ),
        patch(
            "src.server.services.autonomy.host.outbox_db.enqueue_outbox", enqueue_outbox
        ),
    ):
        with pytest.raises(ValueError, match="reply kind must be one of"):
            await AutonomyToolHost(user_id="u-1", thread_id="router").reply(
                ticket_id="t-1", text="Update", kind="chatty"
            )

    enqueue_outbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_empty_reply_is_refused():
    enqueue_outbox = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.host.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.services.autonomy.host.source_db.get_source_any_user",
            AsyncMock(return_value=_source("github")),
        ),
        patch(
            "src.server.services.autonomy.host.outbox_db.enqueue_outbox", enqueue_outbox
        ),
    ):
        with pytest.raises(ValueError, match="reply text must not be empty"):
            await AutonomyToolHost(user_id="u-1", thread_id="router").reply(
                ticket_id="t-1", text="   "
            )

    enqueue_outbox.assert_not_awaited()


# --- the delivery effect ---------------------------------------------------


@pytest.mark.asyncio
async def test_an_email_reply_is_held_when_it_reaches_the_delivery_path():
    """A reply that was queued before the rule was known is held too."""
    send = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(return_value=_outbox()),
        ),
        patch(
            "src.server.services.autonomy.replies.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            AsyncMock(return_value=_source("email")),
        ),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_pending_approval",
            AsyncMock(),
        ) as hold,
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
        patch("src.server.services.autonomy.replies.send_mail_message", send),
    ):
        await reply_effect(_effect())

    hold.assert_awaited_once()
    sent.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_approved_email_reply_is_sent():
    send = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(
                return_value=_outbox(
                    status="pending",
                    approved_at="2026-01-01T00:00:00+00:00",
                    approved_by="u-1",
                )
            ),
        ),
        patch(
            "src.server.services.autonomy.replies.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            AsyncMock(return_value=_source("email")),
        ),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
        patch("src.server.services.autonomy.replies.send_mail_message", send) as send_mail,
    ):
        await reply_effect(_effect())

    mail = send_mail.await_args.args[2]
    assert mail.to == ""
    sent.assert_awaited_once_with("outbox-1")


@pytest.mark.asyncio
async def test_an_email_reply_is_sent_through_the_email_transport():
    send = AsyncMock()
    config = {
        "from_address": "conductor@example.com",
        "to_address": "ops@example.com",
        "workspace_id": "w-1",
    }
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(
                return_value=_outbox(
                    body="Subject: Conductor update\n\nSummary:\nFixed the build.",
                    approved_at="2026-01-01T00:00:00+00:00",
                )
            ),
        ),
        patch(
            "src.server.services.autonomy.replies.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            AsyncMock(return_value=_source("email", config)),
        ),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
        patch("src.server.services.autonomy.replies.send_mail_message", send) as send_mail,
    ):
        await reply_effect(_effect())

    mail = send_mail.await_args.args[2]
    assert mail.to == "ops@example.com"
    assert mail.subject == "Conductor update"
    assert "Fixed the build." in mail.body
    sent.assert_awaited_once_with("outbox-1")


@pytest.mark.asyncio
async def test_a_manual_reply_is_recorded_and_not_sent_anywhere():
    """A manual Ticket has no external channel; the reply stays in the inbox."""
    send = AsyncMock()
    post = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(return_value=_outbox()),
        ),
        patch(
            "src.server.services.autonomy.replies.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            AsyncMock(return_value=_source("manual")),
        ),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
        patch("src.server.services.autonomy.replies.send_feishu_message", send),
        patch("src.server.services.autonomy.replies.post_github_comment", post),
    ):
        await reply_effect(_effect())

    sent.assert_awaited_once_with("outbox-1")
    send.assert_not_awaited()
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_github_reply_is_still_posted_as_a_comment_with_its_marker():
    post = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(return_value=_outbox()),
        ),
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(
                return_value=_outbox(thread_key="github:acme/widgets#42")
            ),
        ),
        patch(
            "src.server.services.autonomy.replies.ticket_db.get_ticket",
            AsyncMock(
                return_value=_ticket(origin_thread_key="github:acme/widgets#42")
            ),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            AsyncMock(return_value=_source("github")),
        ),
        patch(
            "src.server.services.autonomy.replies.find_marker_comment",
            AsyncMock(return_value=False),
        ),
        patch(
            "src.server.services.autonomy.replies.post_github_comment", post
        ) as post_comment,
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ),
    ):
        await reply_effect(_effect())

    assert post_comment.await_args.kwargs["body"] == (
        "### Summary\nFixed the build.\n\n"
        + reply_marker("autonomy:reply:t-1:result:18:Fixed the build.")
    )


@pytest.mark.asyncio
async def test_a_slack_reply_goes_through_the_slack_transport():
    send = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(return_value=_outbox()),
        ),
        patch(
            "src.server.services.autonomy.replies.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            AsyncMock(return_value=_source("slack")),
        ),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
        patch("src.server.services.autonomy.replies.send_slack_message", send) as send_slack,
    ):
        await reply_effect(_effect())

    assert send_slack.await_args.args[2] == "Fixed the build."
    sent.assert_awaited_once_with("outbox-1")


# --- the outbound authorization ---------------------------------------------


@pytest.mark.asyncio
async def test_a_reply_to_another_source_is_denied_as_cross_source():
    """The outbox row cannot point the reply at a Source the Ticket didn't come from."""
    find = AsyncMock(return_value=False)
    post = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(return_value=_outbox()),
        ),
        patch(
            "src.server.services.autonomy.replies.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket(source_id="source-2")),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            AsyncMock(return_value=_source("github")),
        ),
        patch(
            "src.server.services.autonomy.replies.find_marker_comment", find
        ),
        patch("src.server.services.autonomy.replies.post_github_comment", post),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_dropped",
            AsyncMock(),
        ) as dropped,
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
    ):
        with pytest.raises(
            PermanentEffectError, match="outbound reply denied: cross_source"
        ):
            await reply_effect(_effect())

    dropped.assert_awaited_once_with(
        "outbox-1", "outbound reply denied: cross_source"
    )
    find.assert_not_awaited()
    post.assert_not_awaited()
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_reply_off_the_tickets_origin_thread_is_denied():
    post = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(return_value=_outbox(thread_key="github:acme/repo#42")),
        ),
        patch(
            "src.server.services.autonomy.replies.ticket_db.get_ticket",
            AsyncMock(
                return_value=_ticket(origin_thread_key="github:acme/other#7")
            ),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            AsyncMock(return_value=_source("github")),
        ),
        patch(
            "src.server.services.autonomy.replies.find_marker_comment",
            AsyncMock(return_value=False),
        ),
        patch("src.server.services.autonomy.replies.post_github_comment", post),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_dropped",
            AsyncMock(),
        ) as dropped,
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
    ):
        with pytest.raises(
            PermanentEffectError, match="outbound reply denied: thread_mismatch"
        ):
            await reply_effect(_effect())

    dropped.assert_awaited_once_with(
        "outbox-1", "outbound reply denied: thread_mismatch"
    )
    post.assert_not_awaited()
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_email_reply_cannot_override_the_configured_recipient():
    send = AsyncMock()
    config = {
        "from_address": "conductor@example.com",
        "to_address": "ops@example.com",
        "workspace_id": "w-1",
    }
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(
                return_value=_outbox(
                    approved_at="2026-01-01T00:00:00+00:00",
                    to_address="attacker@example.com",
                )
            ),
        ),
        patch(
            "src.server.services.autonomy.replies.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            AsyncMock(return_value=_source("email", config)),
        ),
        patch("src.server.services.autonomy.replies.send_mail_message", send),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_dropped",
            AsyncMock(),
        ) as dropped,
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
    ):
        with pytest.raises(
            PermanentEffectError, match="outbound reply denied: recipient_mismatch"
        ):
            await reply_effect(_effect())

    dropped.assert_awaited_once_with(
        "outbox-1", "outbound reply denied: recipient_mismatch"
    )
    send.assert_not_awaited()
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_github_reply_without_an_origin_thread_is_dropped_before_transport():
    """B5: no Ticket origin thread, no reconcile, no post — fail closed."""
    post = AsyncMock()
    find = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(return_value=_outbox()),
        ),
        patch(
            "src.server.services.autonomy.replies.ticket_db.get_ticket",
            AsyncMock(
                return_value={
                    "ticket_id": "t-1",
                    "user_id": "u-1",
                    "origin_thread_key": "",
                    "mandate": {"source_id": "source-1"},
                }
            ),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            AsyncMock(return_value=_source("github")),
        ),
        patch(
            "src.server.services.autonomy.replies.find_marker_comment", find
        ),
        patch("src.server.services.autonomy.replies.post_github_comment", post),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_dropped",
            AsyncMock(),
        ) as drop,
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
    ):
        with pytest.raises(
            PermanentEffectError, match="outbound reply denied: origin_thread_missing"
        ):
            await reply_effect(_effect())

    find.assert_not_awaited()
    post.assert_not_awaited()
    sent.assert_not_awaited()
    drop.assert_awaited_once_with(
        "outbox-1", "outbound reply denied: origin_thread_missing"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["missing", "", "#0", "garbage"])
async def test_a_bad_github_origin_is_dropped_before_any_delivery(origin):
    ticket = _ticket(origin_thread_key="github:acme/widgets#42")
    if origin == "missing":
        del ticket["origin_thread_key"]
    else:
        ticket["origin_thread_key"] = origin
    find = AsyncMock()
    post = AsyncMock()
    send = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(return_value=_outbox(thread_key="github:acme/widgets#42")),
        ),
        patch(
            "src.server.services.autonomy.replies.ticket_db.get_ticket",
            AsyncMock(return_value=ticket),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            AsyncMock(return_value=_source("github")),
        ),
        patch("src.server.services.autonomy.replies.find_marker_comment", find),
        patch("src.server.services.autonomy.replies.post_github_comment", post),
        patch("src.server.services.autonomy.replies.send_slack_message", send),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_dropped",
            AsyncMock(),
        ) as dropped,
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
    ):
        with pytest.raises(
            PermanentEffectError, match="outbound reply denied: origin_thread_missing"
        ):
            await reply_effect(_effect())

    dropped.assert_awaited_once_with(
        "outbox-1", "outbound reply denied: origin_thread_missing"
    )
    find.assert_not_awaited()
    post.assert_not_awaited()
    send.assert_not_awaited()
    sent.assert_not_awaited()
