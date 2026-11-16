from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy.effects import PermanentEffectError
from src.server.services.autonomy.github import GitHubSourceError
from src.server.services.autonomy.replies import reply_effect


def _effect():
    return type("Effect", (), {"payload": {"outbox_id": "outbox-1"}})()


def _outbox(source_id="source-1"):
    return {
        "outbox_id": "outbox-1",
        "status": "pending",
        "source_id": source_id,
        "user_id": "user-1",
        "ticket_id": "t-1",
        "thread_key": "github:acme/repo#42",
        "body": "Reply text",
    }


@pytest.mark.asyncio
async def test_reply_without_source_id_is_dropped_without_source_lookup():
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(return_value=_outbox(None)),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            new_callable=AsyncMock,
        ) as get_source,
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_dropped",
            new_callable=AsyncMock,
        ) as drop,
    ):
        with pytest.raises(PermanentEffectError, match="no Source id"):
            await reply_effect(_effect())

    get_source.assert_not_awaited()
    drop.assert_awaited_once_with("outbox-1", "reply outbox has no Source id")


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, {"enabled": False}])
async def test_reply_to_missing_or_disabled_source_is_dropped(source):
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(return_value=_outbox()),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            AsyncMock(return_value=source),
        ),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_dropped",
            new_callable=AsyncMock,
        ) as drop,
    ):
        with pytest.raises(PermanentEffectError, match="not found or is disabled"):
            await reply_effect(_effect())

    drop.assert_awaited_once_with(
        "outbox-1", "reply Source is not found or is disabled"
    )


@pytest.mark.asyncio
async def test_transient_github_delivery_error_stays_retryable():
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
                    "origin_thread_key": "github:acme/repo#42",
                    "mandate": {"source_id": "source-1"},
                }
            ),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            AsyncMock(
                return_value={
                    "enabled": True,
                    "kind": "github",
                    "config": {"repository": "acme/repo"},
                }
            ),
        ),
        patch(
            "src.server.services.autonomy.replies.find_marker_comment",
            AsyncMock(return_value=False),
        ),
        patch(
            "src.server.services.autonomy.replies.post_github_comment",
            AsyncMock(side_effect=GitHubSourceError("temporary upstream failure")),
        ),
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_dropped",
            new_callable=AsyncMock,
        ) as drop,
    ):
        with pytest.raises(GitHubSourceError, match="temporary upstream failure"):
            await reply_effect(_effect())

    drop.assert_not_awaited()
