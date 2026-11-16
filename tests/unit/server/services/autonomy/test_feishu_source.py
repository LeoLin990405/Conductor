"""Feishu Source parsing and first-poll cursor behavior."""

from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy import feishu


def _message(message_id: str, created: str, text: str, *, root: str = "", sender: str = "ou_alice", mentions=None):
    body = {"content": __import__("json").dumps({"text": text})}
    row = {
        "message_id": message_id,
        "create_time": created,
        "sender": {"id": sender, "sender_type": "user"},
        "body": body,
    }
    if root:
        row["root_id"] = root
    if mentions:
        row["mentions"] = mentions
    return row


def test_drafts_are_oldest_first_and_follow_the_root_thread():
    drafts, cursor = feishu.feishu_drafts(
        [
            _message("m2", "2000", "second", root="m1"),
            _message("m1", "1000", "first"),
            _message("m3", "3000", "third", root="m1"),
        ],
        chat_id="oc_1",
        agent_open_id="ou_bot",
        cursor=1500,
    )
    assert cursor == 3000
    assert [d.external_id for d in drafts] == ["m2", "m3"]
    assert drafts[0].thread_key == "feishu:oc_1:m1"
    assert drafts[0].actor_login == "ou_alice"
    assert drafts[0].addressed is False


def test_a_mention_marks_the_message_addressed():
    drafts, _ = feishu.feishu_drafts(
        [
            _message(
                "m1",
                "1000",
                "please check",
                mentions=[{"id": {"open_id": "ou_bot"}}],
            )
        ],
        chat_id="oc_1",
        agent_open_id="ou_bot",
        cursor=0,
    )
    assert drafts[0].addressed is True


def test_feishu_message_secret_is_removed_before_trigger_storage():
    token = "ghp_" + "b" * 36
    drafts, _ = feishu.feishu_drafts(
        [_message("m1", "1000", f"please review {token}")],
        chat_id="oc_1",
        agent_open_id="ou_bot",
        cursor=0,
    )
    assert token not in drafts[0].text
    assert "[REDACTED]" in drafts[0].text


@pytest.mark.asyncio
async def test_first_poll_only_sets_the_cursor_and_next_poll_reads_new_messages():
    payload = '{"data": {"items": [%s, %s]}}' % (
        __import__("json").dumps(_message("m2", "2000", "second")),
        __import__("json").dumps(_message("m1", "1000", "first")),
    )
    with patch(
        "src.server.services.autonomy.feishu._run_lark",
        AsyncMock(return_value=(0, payload, "")),
    ):
        drafts, state = await feishu.poll_feishu_messages(
            source_id="s1",
            user_id="u1",
            config={"chat_id": "oc_1", "agent_open_id": "ou_bot"},
            state={},
        )
    assert drafts == []
    assert state["cursor"] == "2000"

    with patch(
        "src.server.services.autonomy.feishu._run_lark",
        AsyncMock(return_value=(0, payload, "")),
    ):
        drafts, state = await feishu.poll_feishu_messages(
            source_id="s1",
            user_id="u1",
            config={"chat_id": "oc_1", "agent_open_id": "ou_bot"},
            state=state,
        )
    assert drafts == []


@pytest.mark.asyncio
async def test_a_cli_failure_is_a_source_error():
    with patch(
        "src.server.services.autonomy.feishu._run_lark",
        AsyncMock(return_value=(1, "", "not authenticated")),
    ):
        with pytest.raises(feishu.FeishuSourceError):
            await feishu.check_feishu_source({"chat_id": "oc_1"})


@pytest.mark.asyncio
async def test_a_feishu_outbox_reply_reconciles_then_sends_with_its_marker():
    from src.server.services.autonomy import replies

    outbox = {
        "outbox_id": "o1",
        "status": "pending",
        "source_id": "s1",
        "user_id": "u1",
        "ticket_id": "t1",
        "thread_key": "feishu:oc_1:m1",
        "body": "done",
        "idempotency_key": "autonomy:reply:t1:result:4:done",
    }
    with (
        patch(
            "src.server.services.autonomy.replies.outbox_db.get_outbox",
            AsyncMock(return_value=outbox),
        ),
        patch(
            "src.server.services.autonomy.replies.ticket_db.get_ticket",
            AsyncMock(
                return_value={
                    "ticket_id": "t1",
                    "user_id": "u1",
                    "origin_thread_key": "feishu:oc_1:m1",
                    "mandate": {"source_id": "s1"},
                }
            ),
        ),
        patch(
            "src.server.services.autonomy.replies.source_db.get_source_any_user",
            AsyncMock(return_value={"kind": "feishu", "config": {"chat_id": "oc_1"}}),
        ),
        patch(
            "src.server.services.autonomy.replies.find_marker_message",
            AsyncMock(return_value=False),
        ) as find_marker,
        patch(
            "src.server.services.autonomy.replies.send_feishu_message",
            AsyncMock(return_value=None),
        ) as send,
        patch(
            "src.server.services.autonomy.replies.outbox_db.mark_sent",
            AsyncMock(return_value=True),
        ) as mark,
    ):
        await replies.reply_effect(type("E", (), {"payload": {"outbox_id": "o1"}})())

    assert find_marker.await_args.kwargs["marker"] == replies.reply_marker(
        "autonomy:reply:t1:result:4:done"
    )
    body = send.await_args.args[1]
    assert body.startswith("done")
    assert body.endswith(replies.reply_marker("autonomy:reply:t1:result:4:done"))
    mark.assert_awaited_once_with("o1")


# --- reconcile-before-retry read-back ----------------------------------------


def _read_payload(*texts: str) -> str:
    return '{"data": {"items": [%s]}}' % ", ".join(
        __import__("json").dumps(_message(f"m{i}", str(1000 + i), text))
        for i, text in enumerate(texts)
    )


@pytest.mark.asyncio
async def test_the_marker_read_back_finds_a_committed_reply():
    marker = "<!-- conductor-autonomy:abc123 -->"
    with patch(
        "src.server.services.autonomy.feishu._run_lark",
        AsyncMock(return_value=(0, _read_payload("hello", f"done\n\n{marker}"), "")),
    ) as run:
        found = await feishu.find_marker_message(
            config={"chat_id": "oc_1"}, marker=marker
        )

    assert found is True
    assert "+chat-messages-list" in run.await_args.args[0]


@pytest.mark.asyncio
async def test_the_marker_read_back_reports_an_absent_marker():
    with patch(
        "src.server.services.autonomy.feishu._run_lark",
        AsyncMock(return_value=(0, _read_payload("hello", "bye"), "")),
    ):
        found = await feishu.find_marker_message(
            config={"chat_id": "oc_1"}, marker="<!-- conductor-autonomy:abc123 -->"
        )

    assert found is False


@pytest.mark.asyncio
async def test_a_failed_read_back_raises_instead_of_guessing():
    """An unreadable destination must not be treated as "marker absent"."""
    with patch(
        "src.server.services.autonomy.feishu._run_lark",
        AsyncMock(return_value=(1, "", "not authenticated")),
    ):
        with pytest.raises(
            feishu.FeishuSourceError, match="cannot reconcile Feishu replies"
        ):
            await feishu.find_marker_message(
                config={"chat_id": "oc_1"}, marker="<!-- conductor-autonomy:abc123 -->"
            )


@pytest.mark.asyncio
async def test_a_non_json_read_back_raises_instead_of_guessing():
    with patch(
        "src.server.services.autonomy.feishu._run_lark",
        AsyncMock(return_value=(0, "not json", "")),
    ):
        with pytest.raises(
            feishu.FeishuSourceError, match="cannot reconcile Feishu replies"
        ):
            await feishu.find_marker_message(
                config={"chat_id": "oc_1"}, marker="<!-- conductor-autonomy:abc123 -->"
            )
