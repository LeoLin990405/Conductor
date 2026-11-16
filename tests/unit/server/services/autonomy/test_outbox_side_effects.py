"""Partial completion and external side effects: reconcile before retry.

A reply effect can fail after its remote post committed but before the local
acknowledgement (``mark_sent``) landed. On the next attempt the delivery path
must not post again. These tests pin the idempotency contract per transport:
the outbox row's idempotency key is surfaced at the destination (marker
comment, marker line, or deterministic Message-ID) and the retry reconciles
against the destination before sending. All transports are faked; no network.
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy.effects import PermanentEffectError
from src.server.services.autonomy.feishu import FeishuSourceError
from src.server.services.autonomy.replies import reply_effect, reply_marker

KEY = "autonomy:reply:t-1:result:18:Fixed the build."


def _effect(outbox_id: str = "outbox-1"):
    return type("Effect", (), {"payload": {"outbox_id": outbox_id}})()


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
        "idempotency_key": KEY,
    }
    row.update(overrides)
    return row


def _ticket(thread_key: str = "thread-1") -> dict:
    """The Ticket origin the outbound authorization checks the row against."""
    return {
        "ticket_id": "t-1",
        "user_id": "u-1",
        "origin_thread_key": thread_key,
        "mandate": {"source_id": "source-1"},
    }


# --- the marker itself ------------------------------------------------------


def test_the_marker_is_a_hash_of_the_idempotency_key_not_the_row():
    """Re-enqueueing the same logical reply must reconcile at the destination."""
    one = reply_marker("autonomy:reply:t-1:result:18:Fixed the build.")
    same = reply_marker("autonomy:reply:t-1:result:18:Fixed the build.")
    other = reply_marker("autonomy:reply:t-1:result:19:Fixed the builds.")

    assert one == same
    assert one != other
    assert "outbox-1" not in one
    # The reply text must not leak into the destination-visible marker.
    assert "Fixed the build" not in one
    assert one.startswith("<!-- conductor-autonomy:")
    assert one.endswith(" -->")


# --- GitHub: remote commit, local ack lost ----------------------------------


@pytest.mark.asyncio
async def test_a_github_reply_whose_marker_is_already_there_is_not_posted_again():
    """The remote post committed; only the local acknowledgement was lost."""
    post = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(return_value=_outbox(thread_key="github:acme/repo#7")),
        ),
        patch(
            "src.server.services.autonomy.replies.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket("github:acme/repo#7")),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            AsyncMock(return_value=_source("github")),
        ),
        patch(
            "src.server.services.autonomy.replies.find_marker_comment",
            AsyncMock(return_value=True),
        ) as find_marker,
        patch("src.server.services.autonomy.replies.post_github_comment", post),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
    ):
        await reply_effect(_effect())

    assert find_marker.await_args.kwargs["marker"] == reply_marker(KEY)
    post.assert_not_awaited()
    sent.assert_awaited_once_with("outbox-1")


@pytest.mark.asyncio
async def test_a_github_reply_posts_with_the_marker_on_its_first_attempt():
    post = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(return_value=_outbox(thread_key="github:acme/repo#7")),
        ),
        patch(
            "src.server.services.autonomy.replies.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket("github:acme/repo#7")),
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
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
    ):
        await reply_effect(_effect())

    assert post.await_args.kwargs["body"].endswith(f"\n\n{reply_marker(KEY)}")
    sent.assert_awaited_once_with("outbox-1")


# --- Feishu: remote commit, local ack lost -----------------------------------


@pytest.mark.asyncio
async def test_a_feishu_reply_whose_marker_is_already_there_is_not_sent_again():
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
            AsyncMock(
                return_value=_source("feishu", {"chat_id": "oc_1", "workspace_id": "w-1"})
            ),
        ),
        patch(
            "src.server.services.autonomy.replies.find_marker_message",
            AsyncMock(return_value=True),
        ) as find_marker,
        patch("src.server.services.autonomy.replies.send_feishu_message", send),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
    ):
        await reply_effect(_effect())

    assert find_marker.await_args.kwargs["marker"] == reply_marker(KEY)
    send.assert_not_awaited()
    sent.assert_awaited_once_with("outbox-1")


@pytest.mark.asyncio
async def test_a_feishu_reply_carries_the_marker_on_its_first_attempt():
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
            AsyncMock(
                return_value=_source("feishu", {"chat_id": "oc_1", "workspace_id": "w-1"})
            ),
        ),
        patch(
            "src.server.services.autonomy.replies.find_marker_message",
            AsyncMock(return_value=False),
        ),
        patch("src.server.services.autonomy.replies.send_feishu_message", send),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
    ):
        await reply_effect(_effect())

    assert send.await_args.args[1].endswith(f"\n\n{reply_marker(KEY)}")
    sent.assert_awaited_once_with("outbox-1")


@pytest.mark.asyncio
async def test_a_feishu_reply_is_not_sent_when_the_destination_cannot_be_read():
    """An unreadable destination must retry the effect, not risk a duplicate."""
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
            AsyncMock(
                return_value=_source("feishu", {"chat_id": "oc_1", "workspace_id": "w-1"})
            ),
        ),
        patch(
            "src.server.services.autonomy.replies.find_marker_message",
            AsyncMock(side_effect=FeishuSourceError("cannot reconcile Feishu replies: cli down")),
        ),
        patch("src.server.services.autonomy.replies.send_feishu_message", send),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
    ):
        with pytest.raises(FeishuSourceError, match="cannot reconcile"):
            await reply_effect(_effect())

    send.assert_not_awaited()
    sent.assert_not_awaited()


# --- email: the same send renders the same Message-ID ------------------------


@pytest.mark.asyncio
async def test_an_email_reply_carries_a_deterministic_message_id():
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
        patch("src.server.services.autonomy.replies.send_mail_message", send),
    ):
        await reply_effect(_effect())

    mail = send.await_args.args[2]
    headers = dict(mail.headers)
    assert headers["Message-ID"] == (
        "<conductor-" + hashlib.sha256(KEY.encode("utf-8")).hexdigest()[:32] + "@example.com>"
    )
    assert headers["X-Conductor-Outbox"] == "outbox-1"
    sent.assert_awaited_once_with("outbox-1")


# --- a settled row is never re-delivered --------------------------------------


@pytest.mark.asyncio
async def test_an_outbox_row_that_is_no_longer_pending_is_not_touched():
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(return_value=_outbox(status="sent")),
        ),
        patch(
            "src.server.services.autonomy.replies.find_marker_message",
            new_callable=AsyncMock,
        ) as find_marker,
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent",
            new_callable=AsyncMock,
        ) as sent,
    ):
        await reply_effect(_effect())

    find_marker.assert_not_awaited()
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_reply_without_a_source_is_still_dropped_as_permanent():
    """Reconcile-before-retry narrows retries, not permanent failures."""
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(return_value=_outbox(source_id=None)),
        ),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_dropped",
            AsyncMock(),
        ) as drop,
    ):
        with pytest.raises(PermanentEffectError, match="no Source id"):
            await reply_effect(_effect())

    drop.assert_awaited_once_with("outbox-1", "reply outbox has no Source id")
