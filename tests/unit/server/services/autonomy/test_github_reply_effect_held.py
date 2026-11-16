"""A GitHub Source that asks for approval holds every reply.

``require_approval`` is a Source config flag, and ``decide_delivery`` reads it
straight off the Source config: the GitHub channel posts by default, so the
flag is the only thing that turns that off. ``reply_effect`` is the one place
that acts on the decision — a held reply is marked ``pending_approval`` and
nothing is written to GitHub: no marker read, no comment posted. A reply a
person has already read is delivered anyway, because the approval is recorded
on the row itself.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy.replies import reply_effect, reply_marker

THREAD = "github:acme/widgets#42"
KEY = "autonomy:reply:t-1:result:18:Fixed the build."


def _effect(outbox_id: str = "outbox-1"):
    return type("Effect", (), {"payload": {"outbox_id": outbox_id}})()


def _source(config: dict | None = None) -> dict:
    return {
        "source_id": "source-1",
        "user_id": "u-1",
        "kind": "github",
        "enabled": True,
        "config": config
        if config is not None
        else {"workspace_id": "w-1", "repository": "acme/widgets"},
    }


def _ticket() -> dict:
    return {
        "ticket_id": "t-1",
        "user_id": "u-1",
        "title": "Fix the build",
        "status": "running",
        "origin_thread_key": THREAD,
        "mandate": {"source_id": "source-1"},
    }


def _outbox(**overrides) -> dict:
    row = {
        "outbox_id": "outbox-1",
        "status": "pending",
        "source_id": "source-1",
        "user_id": "u-1",
        "ticket_id": "t-1",
        "thread_key": THREAD,
        "body": "Fixed the build.",
        "reply_kind": "result",
        "idempotency_key": KEY,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_a_require_approval_github_reply_is_held_and_nothing_is_posted():
    post = AsyncMock()
    find = AsyncMock(return_value=False)
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
                return_value=_source(
                    {
                        "workspace_id": "w-1",
                        "repository": "acme/widgets",
                        "require_approval": True,
                    }
                )
            ),
        ),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_pending_approval",
            AsyncMock(),
        ) as hold,
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_dropped",
            AsyncMock(),
        ) as dropped,
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
        patch("src.server.services.autonomy.replies.find_marker_comment", find),
        patch("src.server.services.autonomy.replies.post_github_comment", post),
    ):
        await reply_effect(_effect())

    hold.assert_awaited_once()
    assert hold.await_args.args[0] == "outbox-1"
    # It says why a person is needed, the way the approval queue shows it.
    assert hold.await_args.args[1]
    sent.assert_not_awaited()
    dropped.assert_not_awaited()
    find.assert_not_awaited()
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_github_reply_is_posted_when_the_source_does_not_ask_for_approval():
    post = AsyncMock()
    find = AsyncMock(return_value=False)
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
            AsyncMock(return_value=_source({"require_approval": False})),
        ),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_pending_approval",
            AsyncMock(),
        ) as hold,
        patch("src.server.services.autonomy.replies.find_marker_comment", find),
        patch("src.server.services.autonomy.replies.post_github_comment", post),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
    ):
        await reply_effect(_effect())

    hold.assert_not_awaited()
    sent.assert_awaited_once_with("outbox-1")
    assert post.await_args.kwargs["body"] == (
        f"### Summary\nFixed the build.\n\n{reply_marker(KEY)}"
    )


@pytest.mark.asyncio
async def test_an_approved_github_reply_is_posted_even_when_the_source_asks_for_approval():
    """The approval is recorded on the row, so the hold does not apply again."""
    post = AsyncMock()
    find = AsyncMock(return_value=False)
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(
                return_value=_outbox(
                    approved_at="2026-01-01T00:00:00+00:00", approved_by="u-1"
                )
            ),
        ),
        patch(
            "src.server.services.autonomy.replies.ticket_db.get_ticket",
            AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            AsyncMock(
                return_value=_source(
                    {
                        "workspace_id": "w-1",
                        "repository": "acme/widgets",
                        "require_approval": True,
                    }
                )
            ),
        ),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_pending_approval",
            AsyncMock(),
        ) as hold,
        patch("src.server.services.autonomy.replies.find_marker_comment", find),
        patch("src.server.services.autonomy.replies.post_github_comment", post),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent", AsyncMock()
        ) as sent,
    ):
        await reply_effect(_effect())

    hold.assert_not_awaited()
    sent.assert_awaited_once_with("outbox-1")
    post.assert_awaited_once()
