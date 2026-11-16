"""The daemon sink withholds a URL password before it leaves the machine."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts import conductor_local_daemon as daemon


class _Api:
    """A server stand-in that accepts every batch."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def output(self, ticket_id, lease_id, run_id, events, **kwargs):
        self.events.extend(events)
        return {"stored": True, "epoch": 7, "tainted": False}

    def texts(self) -> str:
        return "".join(
            str(event.get("text") or "")
            for event in self.events
            if event.get("kind") in {"text", "reasoning"}
        )


async def _stream(monkeypatch, messages):
    api = _Api()

    async def fake_stream(invocation, **kwargs):
        for text in messages:
            yield json.dumps(
                {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
            )

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())
    sink = daemon._DaemonOutputStream(api, "ticket-1", "lease-1", "run-1")
    await daemon._run_codex_streaming(adapter, "prompt", "/work", sink)
    await sink.finalize()
    return sink, api


@pytest.mark.asyncio
async def test_a_url_password_never_leaves_the_daemon(monkeypatch):
    sink, api = await _stream(
        monkeypatch,
        ["Looking at the config.", "dsn postgres://u:hunter2pw@db/app", "later text"],
    )

    assert "hunter2pw" not in api.texts()
    assert api.texts() == "Looking at the config.\n"
    assert sink.text_disabled is True
    assert sink.stop_reason == "guard:url_credential"
    # What was already shown is followed by a stop, so watchers see a reset.
    assert [e["kind"] for e in api.events][-1] == "stop"


@pytest.mark.asyncio
async def test_a_url_password_split_across_messages_never_leaves_the_daemon(
    monkeypatch,
):
    sink, api = await _stream(
        monkeypatch, ["Reading it.", "dsn postgres://u:hunt", "er2pw@db/app", "later"]
    )

    assert "hunt" not in api.texts()
    assert sink.stop_reason == "guard:url_credential"
