"""The whole live-output chain: daemon sink -> HTTP endpoint -> store -> SSE.

The sink is the real daemon class, the endpoint and the activity stream are the
real routes, and the database is the in-memory stand-in with row-lock
semantics. The consumer applies the rule a watcher must follow: join text
within an epoch, drop everything on ``output_reset``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from scripts import conductor_local_daemon as daemon
from tests.unit.server import test_autonomy_activity_stream as _activity_stream
from tests.unit.server.fake_output_db import FakeOutputDb
from tests.unit.server.test_autonomy_activity_stream import (
    TICKET_ID,
    _activity_patches,
    _collect,
    _daemon_ticket,
    _enter,
    _exit,
)

# The activity-stream module's ``client`` fixture, re-exported here so the
# tests in this module can request it by name.
client = _activity_stream.client

DAEMON_ID = "55555555-5555-4555-8555-555555555555"
LEASE_ID = "77777777-7777-4777-8777-777777777777"
OUTPUT_URL = f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/output"
HEADERS = {"X-Daemon-Token": "a" * 43}
STORE = "src.server.services.autonomy.daemon_output"


class HttpApi:
    """The daemon ``Api.output`` surface, backed by the real HTTP endpoint."""

    def __init__(self, client, loop, *, fail_after: int | None = None):
        self.client = client
        self.loop = loop
        self.posts: list[dict] = []
        self.fail_after = fail_after

    def output(
        self, ticket_id, lease_id, run_id, events, *, epoch=None, batch_seq=1, timeout=None
    ):
        body = {
            "lease_id": lease_id,
            "run_id": run_id,
            "batch_seq": batch_seq,
            "events": events,
        }
        if epoch is not None:
            body["epoch"] = epoch
        self.posts.append(body)
        future = asyncio.run_coroutine_threadsafe(
            self.client.post(OUTPUT_URL, headers=HEADERS, json=body), self.loop
        )
        response = future.result(timeout=10)
        if response.status_code != 200:
            raise daemon.ApiError(f"HTTP {response.status_code}", response.status_code)
        return response.json()


@pytest.fixture
def fake_db():
    fake = FakeOutputDb()
    fake.allow(DAEMON_ID, TICKET_ID, LEASE_ID)
    return fake


def _auth():
    from unittest.mock import AsyncMock

    return patch(
        "src.server.app.autonomy._require_daemon",
        new=AsyncMock(return_value={"daemon_id": DAEMON_ID, "user_id": "u1"}),
    )


def consume(frames) -> tuple[str, list[str]]:
    """What a correct watcher shows: text joined per epoch, cleared on reset."""
    shown = ""
    resets: list[str] = []
    epoch = None
    for _cursor, event, data in frames:
        if event != "output":
            continue
        kind = data.get("kind")
        if kind == "output_reset":
            shown = ""
            epoch = None
            resets.append(data["reason"])
            continue
        if kind in {"text", "reasoning"}:
            if epoch is not None and data.get("epoch") != epoch:
                shown = ""
                resets.append("implicit")
            epoch = data.get("epoch")
            shown += data["text"]
    return shown, resets


async def _stream(client, fake_db, loop, messages, *, tools=()):
    api = HttpApi(client, loop)
    sink = daemon._DaemonOutputStream(api, TICKET_ID, LEASE_ID, LEASE_ID)
    for message in messages:
        await sink.add({"kind": "text", "text": message})
    for name in tools:
        await sink.add({"kind": "tool", "tools": [name]})
    await sink._flush_all()
    return sink, api


async def _watch(client, cursor=None):
    ticket = _daemon_ticket(status="completed", summary="done")
    patches = _activity_patches(tickets=[ticket])
    _enter(patches)
    try:
        headers = {"Last-Event-ID": cursor} if cursor else None
        return await _collect(client, headers=headers, timeout=8.0)
    finally:
        _exit(patches)


@pytest.mark.asyncio
async def test_normal_run_reaches_the_watcher_in_order_with_its_epoch(client, fake_db):
    with _auth(), patch(f"{STORE}.output_db", new=fake_db):
        sink, api = await _stream(
            client,
            fake_db,
            asyncio.get_running_loop(),
            ["Reading the config.", "Found three files.", "Done."],
            tools=("bash",),
        )
        frames = await _watch(client)
    assert sink.text_disabled is False and sink.epoch == 1
    shown, resets = consume(frames)
    assert shown == "Reading the config.\nFound three files.\nDone.\n"
    assert resets == []
    outputs = [d for _, e, d in frames if e == "output"]
    assert all(d.get("epoch") == 1 for d in outputs)
    assert {"kind": "tool", "tools": ["bash"], "run": f"daemon:{LEASE_ID}", "epoch": 1} in outputs


@pytest.mark.asyncio
async def test_bearer_prefix_token_tail_never_reaches_a_watcher_through_the_sink(
    client, fake_db
):
    with _auth(), patch(f"{STORE}.output_db", new=fake_db):
        sink, api = await _stream(
            client,
            fake_db,
            asyncio.get_running_loop(),
            ["prefix Bearer ", "abcdef0123456789abcdef", " tail"],
        )
        frames = await _watch(client)
    shown, _ = consume(frames)
    assert "abcdef0123456789abcdef" not in shown and "tail" not in shown
    assert sink.text_disabled is True
    assert not any("abcdef0123" in str(post) for post in api.posts)


@pytest.mark.asyncio
async def test_a_misbehaving_daemon_cannot_get_the_continuation_past_the_server(
    client, fake_db
):
    """Bypass the daemon-side guard: the server alone must refuse and signal."""
    loop = asyncio.get_running_loop()
    api = HttpApi(client, loop)

    def post(events, epoch=None, seq=1):
        return asyncio.to_thread(
            api.output, TICKET_ID, LEASE_ID, LEASE_ID, events, epoch=epoch, batch_seq=seq
        )

    with _auth(), patch(f"{STORE}.output_db", new=fake_db):
        first = await post([{"kind": "text", "text": "prefix Bearer \n"}])
        token = await post(
            [{"kind": "text", "text": "abcdef0123456789abcdef\n"}], epoch=first["epoch"], seq=2
        )
        tail = await post(
            [{"kind": "text", "text": " tail\n"}], epoch=first["epoch"], seq=3
        )
        frames = await _watch(client)
    assert token["stored"] is False and token["tainted"] is True
    assert tail["stored"] is False
    shown, resets = consume(frames)
    # The watcher saw the prefix, then a reset: it never holds the continuation.
    assert "abcdef" not in shown
    assert resets == ["continuity_lost"]


@pytest.mark.asyncio
async def test_epoch_switch_resets_a_watcher_that_holds_the_old_prefix(client, fake_db):
    loop = asyncio.get_running_loop()
    with _auth(), patch(f"{STORE}.output_db", new=fake_db):
        sink, _api = await _stream(client, fake_db, loop, ["prefix Bearer "])
        frames = await _watch(client)
        shown, _ = consume(frames)
        assert shown == "prefix Bearer "
        cursor = [c for c, e, _ in frames if e == "output"][-1]
        # A retry/new run allocates a fresh epoch and starts with the token.
        api2 = HttpApi(client, loop)
        fresh = daemon._DaemonOutputStream(api2, TICKET_ID, LEASE_ID, "run-2")
        await fresh.add({"kind": "text", "text": "abcdef0123 tail"})
        await fresh._flush_all()
        assert fresh.epoch == 2
        later = await _watch(client, cursor)
    outputs = [d for _, e, d in later if e == "output"]
    assert outputs[0]["kind"] == "output_reset" and outputs[0]["epoch"] == 2
    assert outputs[1]["kind"] == "text" and outputs[1]["epoch"] == 2
    joined, resets = consume(frames + later)
    assert joined == "abcdef0123 tail\n"
    assert resets == ["new_epoch"]


@pytest.mark.asyncio
async def test_database_outage_stops_the_sink_and_stores_nothing(client, fake_db):
    from tests.unit.server.fake_output_db import FakeStoreError

    fake_db.fail_before_commit = FakeStoreError("db down")
    with _auth(), patch(f"{STORE}.output_db", new=fake_db):
        sink, api = await _stream(
            client, fake_db, asyncio.get_running_loop(), ["hello", "world"]
        )
    assert sink.disabled is True and sink.stop_reason == "not_stored"
    assert fake_db.events == {} or not any(fake_db.events.values())
    assert len(api.posts) == 1


@pytest.mark.asyncio
async def test_stale_epoch_from_a_replaced_run_stops_that_sink(client, fake_db):
    loop = asyncio.get_running_loop()
    with _auth(), patch(f"{STORE}.output_db", new=fake_db):
        old_sink, old_api = await _stream(client, fake_db, loop, ["old run line"])
        newer = daemon._DaemonOutputStream(
            HttpApi(client, loop), TICKET_ID, LEASE_ID, "run-2"
        )
        await newer.add({"kind": "text", "text": "new run line"})
        await newer._flush_all()
        assert newer.epoch == 2
        await old_sink.add({"kind": "text", "text": "late write from the old run"})
        await old_sink._flush_all()
    assert old_sink.disabled is True
    assert "late write" not in fake_db.stored_text(TICKET_ID)
    assert fake_db.stored_text(TICKET_ID) == "new run line\n"


@pytest.mark.asyncio
async def test_a_local_refusal_reaches_watchers_as_a_reset_marker(client, fake_db):
    """The daemon refuses a credential key locally; the server is told, taints
    the epoch, and the watcher gets an explicit reset instead of a silent end."""
    with _auth(), patch(f"{STORE}.output_db", new=fake_db):
        sink, _api = await _stream(
            client,
            fake_db,
            asyncio.get_running_loop(),
            ["Looking at the settings.", "Authorization: Bearer"],
            tools=("bash",),
        )
        frames = await _watch(client)
    assert sink.text_disabled is True
    shown, resets = consume(frames)
    assert shown == ""  # cleared by the reset; nothing after it was stored
    assert resets == ["continuity_lost"]
    kinds = [d["kind"] for _, e, d in frames if e == "output"]
    assert kinds == ["text", "output_reset"]  # nothing, tool names included, follows


@pytest.mark.asyncio
async def test_a_tool_named_bearer_followed_by_a_token_never_composes_a_credential(
    client, fake_db
):
    """Reviewer repro, through the whole chain: a misbehaving daemon posts a tool
    named `Bearer` and then the token as text. Both renderers print tool names
    on their own line, so the pair reads `tool: Bearer / abcdefgh`."""
    loop = asyncio.get_running_loop()
    api = HttpApi(client, loop)
    with _auth(), patch(f"{STORE}.output_db", new=fake_db):
        first = await asyncio.to_thread(
            api.output, TICKET_ID, LEASE_ID, LEASE_ID, [{"kind": "tool", "tools": ["Bearer"]}]
        )
        second = await asyncio.to_thread(
            api.output,
            TICKET_ID,
            LEASE_ID,
            LEASE_ID,
            [{"kind": "text", "text": "abcdefgh\n"}],
            epoch=first["epoch"],
            batch_seq=2,
        )
        frames = await _watch(client)
    assert second["stored"] is False and second["tainted"] is True
    kinds = [d["kind"] for _, e, d in frames if e == "output"]
    assert kinds == ["tool", "output_reset"]
    shown, _ = consume(frames)
    assert "abcdefgh" not in shown


@pytest.mark.asyncio
async def test_a_jwt_split_over_two_messages_never_reaches_a_watcher(client, fake_db):
    """Reviewer repro, whole chain: `eyJ` ends one model message and the rest of
    the token starts the next. The daemon's display newline between them must
    not hide the continuation from either scanner."""
    with _auth(), patch(f"{STORE}.output_db", new=fake_db):
        sink, api = await _stream(
            client,
            fake_db,
            asyncio.get_running_loop(),
            ["eyJ", "hbGciOiJIUzI1NiJ9", "tail"],
        )
        frames = await _watch(client)
    shown, resets = consume(frames)
    assert "hbGci" not in shown and "tail" not in shown
    assert sink.text_disabled is True
    assert not any("hbGci" in str(post) for post in api.posts)
    assert resets == ["continuity_lost"]

    # And a server that is handed the split directly (a misbehaving daemon)
    # refuses it on its own.
    fresh = FakeOutputDb()
    fresh.allow(DAEMON_ID, TICKET_ID, LEASE_ID)
    http = HttpApi(client, asyncio.get_running_loop())
    with _auth(), patch(f"{STORE}.output_db", new=fresh):
        first = await asyncio.to_thread(
            http.output, TICKET_ID, LEASE_ID, LEASE_ID,
            [{"kind": "text", "text": "eyJ\n", "join": True}],
        )
        second = await asyncio.to_thread(
            http.output, TICKET_ID, LEASE_ID, LEASE_ID,
            [{"kind": "text", "text": "hbGciOiJIUzI1NiJ9\n", "join": True}],
            epoch=first["epoch"], batch_seq=2,
        )
    assert second["stored"] is False and second["tainted"] is True
    assert "hbGci" not in fresh.stored_text(TICKET_ID)
