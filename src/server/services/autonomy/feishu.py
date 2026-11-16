"""Feishu/Lark Source: poll a chat through the official lark-cli.

The CLI owns app binding and user authorization, so this module holds no
Feishu credentials. It turns one page of chat messages into the same
TriggerDraft shape as GitHub, using the message's root/thread id as the
Thread key so follow-ups stay on one Super-agent Thread.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.server.services.autonomy.lark_cli import _build_args, _run_lark
from src.server.services.autonomy.models import TriggerDraft
from src.server.services.autonomy.security import redact_external_text

logger = logging.getLogger(__name__)


class FeishuSourceError(Exception):
    """A Feishu Source cannot be checked or polled."""


def _payload_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [row for row in data["items"] if isinstance(row, dict)]
    for key in ("items", "messages", "data"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _content_text(message: dict[str, Any]) -> str:
    raw = message.get("body", {}).get("content") if isinstance(message.get("body"), dict) else None
    if raw is None:
        raw = message.get("content") or message.get("text") or ""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(parsed, dict):
            return str(parsed.get("text") or parsed.get("content") or "")
    if isinstance(raw, dict):
        return str(raw.get("text") or raw.get("content") or "")
    return str(raw or "")


def _sender_id(message: dict[str, Any]) -> str:
    sender = message.get("sender")
    if isinstance(sender, dict):
        value = sender.get("id") or sender.get("sender_id") or sender.get("open_id")
        if value:
            return str(value)
    for key in ("sender_id", "sender_open_id", "open_id"):
        if message.get(key):
            return str(message[key])
    return ""


def _mention_ids(message: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for mention in message.get("mentions") or []:
        if not isinstance(mention, dict):
            continue
        ident = mention.get("id")
        if isinstance(ident, dict):
            found.update(str(v) for v in ident.values() if v)
        for key in ("id", "user_id", "open_id"):
            if mention.get(key):
                found.add(str(mention[key]))
    return found


def _create_time(message: dict[str, Any]) -> int:
    for key in ("create_time", "timestamp", "time"):
        value = message.get(key)
        if value is None:
            continue
        try:
            return int(str(value))
        except (TypeError, ValueError):
            continue
    return 0


def _thread_id(message: dict[str, Any], message_id: str) -> str:
    for key in ("thread_id", "root_id", "parent_id"):
        if message.get(key):
            return str(message[key])
    return message_id


def feishu_drafts(
    messages: list[dict[str, Any]],
    *,
    chat_id: str,
    agent_open_id: str,
    cursor: int,
) -> tuple[list[TriggerDraft], int]:
    """Turn newest-first CLI messages into oldest-first Trigger drafts."""
    drafts: list[TriggerDraft] = []
    max_seen = cursor
    for message in sorted(messages, key=_create_time):
        message_id = str(message.get("message_id") or message.get("id") or "")
        if not message_id:
            continue
        created = _create_time(message)
        max_seen = max(max_seen, created)
        if created <= cursor:
            continue
        text = redact_external_text(_content_text(message)).strip()
        if not text:
            continue
        sender = _sender_id(message)
        sender_type = str((message.get("sender") or {}).get("sender_type") or "")
        mentions = _mention_ids(message)
        addressed = bool(agent_open_id) and (
            agent_open_id in mentions or f'user_id="{agent_open_id}"' in text
        )
        thread = _thread_id(message, message_id)
        drafts.append(
            TriggerDraft(
                external_id=message_id,
                kind="message",
                thread_key=f"feishu:{chat_id}:{thread}",
                actor_login=sender,
                actor_is_bot=sender_type == "bot" or sender == agent_open_id,
                addressed=addressed,
                text=text,
                url="",
                payload={"chat_id": chat_id, "message_id": message_id},
            )
        )
    return drafts, max_seen


async def poll_feishu_messages(
    *, source_id: str, user_id: str, config: dict[str, Any], state: dict[str, Any]
) -> tuple[list[TriggerDraft], dict[str, Any]]:
    """Read one page of a Feishu chat; the first poll only fixes the cursor."""
    chat_id = str(config.get("chat_id") or "").strip()
    if not chat_id:
        raise FeishuSourceError("Feishu Source has no chat_id")
    args = _build_args(
        "read_messages",
        chat_id=chat_id,
        query="",
        start="",
        end="",
        limit=50,
        text="",
        identity="",
    )
    returncode, stdout, stderr = await _run_lark(args, timeout=45)
    if returncode != 0:
        detail = redact_external_text(
            stderr.strip() or stdout.strip() or "lark-cli failed"
        )[:1000]
        raise FeishuSourceError(detail)
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise FeishuSourceError("lark-cli did not return JSON") from exc
    messages = _payload_items(payload)
    cursor = int(state.get("cursor") or 0)
    drafts, max_seen = feishu_drafts(
        messages,
        chat_id=chat_id,
        agent_open_id=str(config.get("agent_open_id") or ""),
        cursor=cursor,
    )
    # A first poll establishes the high-water mark instead of replaying the
    # whole visible history into Tickets.
    if not cursor:
        drafts = []
    next_state = {
        **state,
        "cursor": str(max_seen),
        "last_poll_at": state.get("last_poll_at") or "",
        "repository": chat_id,
        "agent_login": "lark-cli",
        "last_error": "",
    }
    logger.info("[autonomy] feishu poll source=%s messages=%s drafts=%s", source_id, len(messages), len(drafts))
    return drafts, next_state


async def check_feishu_source(config: dict[str, Any]) -> dict[str, Any]:
    """Check the CLI is installed/authorized and name the chat in state."""
    chat_id = str(config.get("chat_id") or "").strip()
    if not chat_id:
        raise FeishuSourceError("Feishu Source has no chat_id")
    args = _build_args(
        "status",
        chat_id="",
        query="",
        start="",
        end="",
        limit=1,
        text="",
        identity="",
    )
    returncode, stdout, stderr = await _run_lark(args, timeout=45)
    if returncode != 0:
        detail = redact_external_text(
            stderr.strip() or stdout.strip() or "lark-cli is not ready"
        )[:1000]
        raise FeishuSourceError(detail)
    return {"agent_login": "lark-cli", "repository": chat_id, "can_push": True}


async def send_feishu_message(config: dict[str, Any], text: str) -> None:
    chat_id = str(config.get("chat_id") or "").strip()
    if not chat_id:
        raise FeishuSourceError("Feishu Source has no chat_id")
    args = _build_args(
        "send_message",
        chat_id=chat_id,
        query="",
        start="",
        end="",
        limit=1,
        text=text,
        identity="",
    )
    returncode, stdout, stderr = await _run_lark(args, timeout=45)
    if returncode != 0:
        detail = redact_external_text(
            stderr.strip() or stdout.strip() or "lark-cli send failed"
        )[:1000]
        raise FeishuSourceError(detail)


async def find_marker_message(*, config: dict[str, Any], marker: str) -> bool:
    """Whether the chat already holds a message carrying this marker.

    The outbox retries a send that may have committed remotely before its
    local acknowledgement; checking the destination is the idempotency fence.
    A read-back failure raises instead of returning ``False``: when the
    destination cannot be read the safe move is to retry the effect, not to
    risk a duplicate post.
    """
    chat_id = str(config.get("chat_id") or "").strip()
    if not chat_id:
        raise FeishuSourceError("Feishu Source has no chat_id")
    args = _build_args(
        "read_messages",
        chat_id=chat_id,
        query="",
        start="",
        end="",
        limit=50,
        text="",
        identity="",
    )
    returncode, stdout, stderr = await _run_lark(args, timeout=45)
    if returncode != 0:
        detail = redact_external_text(
            stderr.strip() or stdout.strip() or "lark-cli read failed"
        )[:1000]
        raise FeishuSourceError(f"cannot reconcile Feishu replies: {detail}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise FeishuSourceError("cannot reconcile Feishu replies: lark-cli did not return JSON") from exc
    needle = str(marker or "")
    return any(
        needle in _content_text(message) for message in _payload_items(payload)
    )
