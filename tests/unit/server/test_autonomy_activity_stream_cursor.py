"""Activity stream resume cursor: the output source it spells out."""

from __future__ import annotations

import json
import re

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

from tests.conftest import create_test_app
from tests.unit.server.test_autonomy_activity_stream import (
    MODULE,
    URL,
    TICKET_ID,
    _activity_patches,
    _cloud,
    _collect,
    _daemon_payload,
    _enter,
    _exit,
    _ticket,
)


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


def test_a_cloud_run_source_is_spelled_out_beside_a_daemon_position():
    """A run part next to a daemon part and no source resumes as unknown, so the
    cursor must name the source there or every reconnect resets for nothing."""
    from src.server.app.ticket_activity_stream import (
        OutputSourceFence,
        cursor_output_source,
        ticket_activity_cursor,
    )

    fence = OutputSourceFence("run-1", "", "")
    assert fence.encoded("run-1") == ""  # a pure cloud cursor stays unchanged
    spelled = fence.encoded("run-1", "5.2")
    assert spelled == "rrun-1"
    cursor = ticket_activity_cursor("a", "run-1", 3, "5.2", spelled)
    resumed = OutputSourceFence("run-1", "5.2", cursor_output_source(cursor))
    assert resumed.check(_cloud("same run")) is None  # the watcher holds this run
    assert resumed.check(_cloud("other run", "run-2")) is not None
    assert resumed.check(_daemon_payload("x\n", 5)) is not None
    # A cursor minted before the source was spelled out still resets (conservative).
    assert OutputSourceFence("run-1", "5.2", "").check(_cloud("x")) is not None


@pytest.mark.asyncio
async def test_a_reconnect_into_the_same_cloud_run_after_a_daemon_epoch_is_not_reset_again(
    client,
):
    """daemon epoch 5 -> cloud run: the first attach is reset once, and the id it
    hands out resumes the same run without a second reset."""
    done = _ticket(
        status="completed",
        worker_thread_id="thread-1",
        worker_run_id="run-1",
        summary="done",
    )

    def run_frames(thread_id: str, run_id: str, last_event_id: int):
        async def frames():
            for seq, text in ((1, "cloud-one "), (2, "cloud-two")):
                if seq > last_event_id:
                    data = json.dumps({"content": text, "content_type": "text"})
                    yield f"id: {seq}\nevent: message_chunk\ndata: {data}\n\n"
            yield "event: run_end\ndata: {}\n\n"

        return frames()

    watched_daemon = f"created:{TICKET_ID}~~0~5.2~d5"
    patches = _activity_patches(tickets=[done])
    _enter(patches)
    try:
        with patch(f"{MODULE}._worker_run_frames", new=run_frames):
            first = await _collect(client, headers={"Last-Event-ID": watched_daemon})
            outputs = [(cid, d) for cid, e, d in first if e == "output"]
            assert [d["kind"] for _, d in outputs] == ["output_reset", "text", "text"]
            cut_after_first_text = outputs[1][0]
            second = await _collect(
                client, headers={"Last-Event-ID": cut_after_first_text}
            )
    finally:
        _exit(patches)

    resumed = [d for _, e, d in second if e == "output"]
    assert [d["kind"] for d in resumed] == ["text"]
    assert resumed[0]["text"] == "cloud-two"


FORGED = 'event: end\ndata: {"status":"forged"}\n\n'


@pytest.mark.parametrize("line_break", ["\n", "\r\n", "\r", "\x00", "\u2028", "caf\u00e9"])
def test_a_cursor_part_that_is_not_printable_ascii_is_not_one_of_ours(line_break):
    """The cursor is echoed in the ``id:`` line of every frame, so a part with a
    line break in it writes SSE fields into the caller's own stream."""
    from src.server.app.ticket_activity_stream import (
        parse_ticket_activity_cursor,
        ticket_activity_cursor,
    )

    hostile = f"a{line_break}{FORGED}"
    for position in range(4):
        parts = ["created:x", "run-1", "3", "5.2"]
        parts[position] = hostile
        activity, run_id, seq, daemon = parse_ticket_activity_cursor("~".join(parts))
        for part in (activity, run_id, daemon):
            assert re.fullmatch(r"[\x21-\x7e]*", part)
    assert parse_ticket_activity_cursor(f"x~y~3~{hostile}")[3] == "!"  # no position parses
    minted = ticket_activity_cursor(hostile, hostile, 2, hostile, hostile)
    assert re.fullmatch(r"[\x21-\x7e]*", minted)
    from src.server.app.ticket_activity_stream import cursor_output_source

    assert cursor_output_source(f"a~b~1~2.3~{hostile}") == ""
    assert cursor_output_source("a~b~1~2.3~rrun-1") == "rrun-1"


@pytest.mark.parametrize("where", ["activity", "run", "daemon", "source"])
@pytest.mark.asyncio
async def test_a_hostile_resume_cursor_cannot_write_fields_into_the_stream(client, where):
    completed = _ticket(status="completed", summary="done")
    cursor = {
        "activity": f"x\n{FORGED}~~0~1.1~",
        "run": f"~x\r\n{FORGED}~0~1.1~",
        "daemon": f"~~0~1.1\n{FORGED}~",
        "source": f"~~0~1.1~r\n{FORGED}",
    }[where]
    patches = _activity_patches(tickets=[completed])
    _enter(patches)
    try:
        response = await client.get(URL, params={"last_event_id": cursor})
    finally:
        _exit(patches)

    assert response.status_code == 200
    assert response.text.count("event: end") == 1  # the one the server wrote
    assert "forged" not in response.text
    ids = [line for line in response.text.splitlines() if line.startswith("id:")]
    assert ids
    for line in ids:
        assert re.fullmatch(r"id: [\x21-\x7e]*", line), line
