"""user_feed outbox executor: publish is a latency hint, not a dead letter."""

import json
import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services import hook_outbox, user_feed
from src.server.services.hook_outbox import HookOutboxDrainer
from src.server.services.user_feed import (
    execute_user_feed,
    register_outbox_executors,
    user_events_channel,
)


@contextmanager
def _restored_executors():
    """Pop any prior user_feed so a different stub cannot flake a re-register."""
    saved = dict(hook_outbox._EXECUTORS)
    try:
        hook_outbox._EXECUTORS.pop("user_feed", None)
        yield
    finally:
        hook_outbox._EXECUTORS.clear()
        hook_outbox._EXECUTORS.update(saved)


class _FakeClient:
    def __init__(self, *, error: BaseException | None = None):
        self.published: list[tuple[str, str]] = []
        self._error = error

    async def publish(self, channel, payload):
        self.published.append((channel, payload))
        if self._error is not None:
            raise self._error


class _FakeCache:
    def __init__(self, *, enabled, client):
        self.enabled = enabled
        self.client = client


def _payload(**over) -> dict:
    payload = {
        "user_id": "u-1",
        "thread_id": "t-1",
        "workspace_id": "ws-1",
        "run_id": "r-1",
        "run_seq": 4,
        "status": "completed",
        "interrupt_reason": None,
    }
    payload.update(over)
    return payload


def test_registrar_binds_user_feed_executor():
    with _restored_executors():
        register_outbox_executors()
        assert hook_outbox._EXECUTORS["user_feed"] is execute_user_feed


@pytest.mark.asyncio
async def test_execute_publishes_payload_json(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(
        user_feed,
        "get_cache_client",
        lambda: _FakeCache(enabled=True, client=client),
    )
    payload = _payload()

    await execute_user_feed({"payload": payload})

    assert user_events_channel("u-1") == "user:events:u-1"
    assert client.published == [("user:events:u-1", json.dumps(payload))]


@pytest.mark.asyncio
async def test_missing_user_id_and_non_dict_payload_do_not_publish(monkeypatch):
    client = _FakeClient(error=RuntimeError("should not publish"))
    monkeypatch.setattr(
        user_feed,
        "get_cache_client",
        lambda: _FakeCache(enabled=True, client=client),
    )

    await execute_user_feed({"payload": {"thread_id": "t-1"}})
    await execute_user_feed({"payload": {"user_id": ""}})
    await execute_user_feed({"payload": {"user_id": None}})
    await execute_user_feed({"payload": "nope"})
    await execute_user_feed({"payload": None})
    await execute_user_feed({})
    await execute_user_feed({"payload": ["u-1"]})

    assert client.published == []


@pytest.mark.asyncio
async def test_redis_disabled_or_missing_client_does_not_raise(monkeypatch):
    client = _FakeClient(error=RuntimeError("should not publish"))
    monkeypatch.setattr(
        user_feed,
        "get_cache_client",
        lambda: _FakeCache(enabled=False, client=client),
    )
    await execute_user_feed({"payload": _payload()})

    monkeypatch.setattr(
        user_feed,
        "get_cache_client",
        lambda: _FakeCache(enabled=True, client=None),
    )
    await execute_user_feed({"payload": _payload()})

    assert client.published == []


@pytest.mark.asyncio
async def test_publish_raising_does_not_raise(monkeypatch, caplog):
    client = _FakeClient(error=RuntimeError("redis down"))
    monkeypatch.setattr(
        user_feed,
        "get_cache_client",
        lambda: _FakeCache(enabled=True, client=client),
    )

    with caplog.at_level(logging.DEBUG, logger="src.server.services.user_feed"):
        await execute_user_feed({"payload": _payload()})

    assert len(client.published) == 1
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


@pytest.mark.asyncio
async def test_drainer_acks_user_feed_without_unknown_type_log(monkeypatch, caplog):
    client = _FakeClient()
    monkeypatch.setattr(
        user_feed,
        "get_cache_client",
        lambda: _FakeCache(enabled=True, client=client),
    )
    ack = AsyncMock()
    nack = AsyncMock()
    job = {
        "hook_outbox_id": "job-1",
        "hook_type": "user_feed",
        "attempts": 1,
        "run_id": "r-1",
        "payload": _payload(),
    }

    with _restored_executors():
        register_outbox_executors()
        with (
            patch(
                "src.server.services.hook_outbox.outbox_db.ack_outbox_job",
                ack,
            ),
            patch(
                "src.server.services.hook_outbox.outbox_db.nack_outbox_job",
                nack,
            ),
            caplog.at_level(
                logging.DEBUG, logger="src.server.services.hook_outbox"
            ),
        ):
            await HookOutboxDrainer()._execute(job)

    ack.assert_awaited_once_with("job-1", attempts=1)
    nack.assert_not_awaited()
    assert "unknown hook_type=user_feed" not in caplog.text
    assert client.published == [("user:events:u-1", json.dumps(job["payload"]))]
