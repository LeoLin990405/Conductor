"""Ticket activity SSE: live record, Worker tail, resume cursor, ownership."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app

USER_ID = "test-user-123"
TICKET_ID = "66666666-6666-4666-8666-666666666666"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
URL = f"/api/v1/autonomy/tickets/{TICKET_ID}/activity/stream"
MODULE = "src.server.app.ticket_activity_stream"


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


def _ticket(**overrides) -> dict:
    ticket = {
        "ticket_id": TICKET_ID,
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "Fix the save button",
        "scope": "Change the label",
        "status": "running",
        "mandate": {},
        "origin_trigger_id": None,
        "origin_thread_key": "manual:task",
        "router_thread_id": None,
        "worker_thread_id": None,
        "worker_run_id": None,
        "worker_provider": "",
        "worker_model": "",
        "worker_daemon_id": None,
        "attempts": 1,
        "attention": None,
        "summary": "",
        "error": "",
        "created_at": NOW - timedelta(minutes=5),
        "updated_at": NOW,
        "started_at": NOW - timedelta(minutes=4),
        "ended_at": None,
        "closed_at": None,
        "close_reason": "",
    }
    ticket.update(overrides)
    return ticket


def _parse_block(block: str):
    """One SSE frame -> (id, event, data); comments-only frames are skipped."""
    event = ""
    event_id = None
    data_lines: list[str] = []
    for line in block.splitlines():
        if not line or line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "id":
            event_id = value.strip()
        elif field == "event":
            event = value.strip()
        elif field == "data":
            data_lines.append(value)
    if not data_lines:
        return None
    text = "\n".join(data_lines)
    return (event_id, event, json.loads(text) if text else None)


async def _collect(
    client: AsyncClient,
    url: str = URL,
    *,
    headers: dict[str, str] | None = None,
    stop_events: tuple[str, ...] = ("end", "timeout"),
    timeout: float = 5.0,
):
    frames: list[tuple[str | None, str, dict]] = []

    async def read() -> None:
        async with client.stream("GET", url, headers=headers) as response:
            assert response.status_code == 200, await response.aread()
            buffer = ""
            async for chunk in response.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    frame = _parse_block(block)
                    if frame is None:
                        continue
                    frames.append(frame)
                    if frame[1] in stop_events:
                        return

    try:
        await asyncio.wait_for(read(), timeout)
    except asyncio.TimeoutError:
        pass
    return frames


def _activity_patches(
    *,
    tickets: list[dict],
    inputs: list[dict] | None = None,
    reports: list[dict] | None = None,
):
    """Patch the feature gate, ticket reads and the stream's poll pace."""
    rows = iter(tickets)
    last = tickets[-1] if tickets else None

    async def fake_get(_ticket_id: str):
        return next(rows, last)

    return (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(f"{MODULE}.tickets_db.get_ticket", new=fake_get),
        patch(
            f"{MODULE}.tickets_db.list_inputs",
            new=AsyncMock(return_value=inputs or []),
        ),
        patch(
            f"{MODULE}.tickets_db.list_reports",
            new=AsyncMock(return_value=reports or []),
        ),
        patch(f"{MODULE}.ACTIVITY_STREAM_POLL_S", 0.01),
        patch(f"{MODULE}.ACTIVITY_STREAM_PING_S", 60.0),
    )


def _enter(patches):
    for item in patches:
        item.start()
    return patches


def _exit(patches):
    for item in patches:
        item.stop()


@pytest.mark.asyncio
async def test_activity_stream_emits_the_record_then_terminal_end(client):
    running = _ticket(status="running")
    completed = _ticket(status="completed", summary="done")
    patches = _activity_patches(
        tickets=[running, running, completed],
        inputs=[
            {
                "input_id": "11111111-1111-4111-8111-111111111111",
                "kind": "instruction",
                "text": "use Save",
                "created_at": NOW - timedelta(minutes=3),
            }
        ],
    )
    _enter(patches)
    try:
        frames = await _collect(client)
    finally:
        _exit(patches)

    activity = [data for _, event, data in frames if event == "activity"]
    statuses = [data for _, event, data in frames if event == "status"]
    ends = [data for _, event, data in frames if event == "end"]

    assert [row["kind"] for row in activity] == ["created", "instruction"]
    assert [row["status"] for row in statuses] == ["running", "completed"]
    assert ends and ends[-1]["status"] == "completed"
    # The saved id resumes both sources: activity event id, run id, run seq.
    first_id = frames[0][0]
    assert first_id is not None and first_id.count("~") == 3


@pytest.mark.asyncio
async def test_activity_stream_replays_activity_as_an_idempotent_snapshot(client):
    """Inputs and reports are read by separate queries, so their timestamps
    do not prove a total order: a row committed between the two reads must not
    be skipped even though it sorts before the cursor. Every attach therefore
    replays the full snapshot and the client deduplicates by event_id."""
    completed = _ticket(status="completed", summary="done")
    patches = _activity_patches(
        tickets=[completed],
        inputs=[
            {
                "input_id": "11111111-1111-4111-8111-111111111111",
                "kind": "instruction",
                "text": "use Save",
                "created_at": NOW - timedelta(minutes=3),
            }
        ],
    )
    cursor = f"created:{TICKET_ID}~~0"
    _enter(patches)
    try:
        frames = await _collect(client, headers={"Last-Event-ID": cursor})
        queried = await _collect(client, f"{URL}?last_event_id=created:{TICKET_ID}~~0")
    finally:
        _exit(patches)

    activity = [frame[2] for frame in frames if frame[1] == "activity"]
    assert [row["kind"] for row in activity] == ["created", "instruction"]
    queried_activity = [frame[2] for frame in queried if frame[1] == "activity"]
    assert [row["kind"] for row in queried_activity] == ["created", "instruction"]


@pytest.mark.asyncio
async def test_activity_stream_forwards_worker_tokens_and_run_sequence(client):
    running = _ticket(
        status="running", worker_thread_id="thread-1", worker_run_id="run-1"
    )
    completed = _ticket(
        status="completed",
        worker_thread_id="thread-1",
        worker_run_id="run-1",
        summary="done",
    )
    calls: list[tuple[str, str, int]] = []

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        calls.append((thread_id, run_id, last_event_id))

        async def frames():
            yield (
                "id: 1\nevent: message_chunk\n"
                'data: {"content": "Hel", "content_type": "text", '
                '"agent": "main", "thread_id": "thread-1"}\n\n'
            )
            yield (
                "id: 2\nevent: message_chunk\n"
                'data: {"content": "lo", "content_type": "text", '
                '"agent": "main", "thread_id": "thread-1"}\n\n'
            )
            yield 'event: run_end\ndata: {"outcome": "completed"}\n\n'

        return frames()

    patches = _activity_patches(tickets=[running, running, completed])
    _enter(patches)
    try:
        with patch(f"{MODULE}._worker_run_frames", new=fake_run_frames):
            frames = await _collect(client)
    finally:
        _exit(patches)

    outputs = [data for _, event, data in frames if event == "output"]
    assert [row["text"] for row in outputs] == ["Hel", "lo"]
    assert all(row["kind"] == "text" for row in outputs)
    # Output frames carry the run generation: a watcher uses a change of run
    # id to reset half-parsed terminal sequences from the previous run.
    assert all(row["run"] == "run-1" for row in outputs)
    assert calls == [("thread-1", "run-1", 0)]
    output_ids = [frame[0] for frame in frames if frame[1] == "output"]
    assert output_ids == [
        f"created:{TICKET_ID}~run-1~1~",
        f"created:{TICKET_ID}~run-1~2~",
    ]


@pytest.mark.asyncio
async def test_activity_stream_resumes_the_worker_run_sequence(client):
    completed = _ticket(
        status="completed",
        worker_thread_id="thread-1",
        worker_run_id="run-1",
        summary="done",
    )
    calls: list[int] = []

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        calls.append(last_event_id)

        async def frames():
            yield (
                f"id: {last_event_id + 1}\nevent: message_chunk\n"
                'data: {"content": "tail", "content_type": "text"}\n\n'
            )
            yield "event: run_end\ndata: {}\n\n"

        return frames()

    cursor = f"created:{TICKET_ID}~run-1~2"
    patches = _activity_patches(tickets=[completed])
    _enter(patches)
    try:
        with patch(f"{MODULE}._worker_run_frames", new=fake_run_frames):
            frames = await _collect(client, headers={"Last-Event-ID": cursor})
    finally:
        _exit(patches)

    assert calls == [2]
    outputs = [data for _, event, data in frames if event == "output"]
    assert [row["text"] for row in outputs] == ["tail"]


@pytest.mark.asyncio
async def test_activity_stream_does_not_tail_a_worker_run_that_is_absent(client):
    # A daemon/native Ticket has no Worker thread or run: activity only.
    completed = _ticket(status="completed", worker_daemon_id="daemon-1")
    fake = MagicMock()
    patches = _activity_patches(tickets=[completed])
    _enter(patches)
    try:
        with patch(f"{MODULE}._worker_run_frames", new=fake):
            frames = await _collect(client)
    finally:
        _exit(patches)

    assert fake.call_count == 0
    assert any(event == "end" for _, event, _ in frames)


@pytest.mark.asyncio
async def test_activity_stream_rejects_another_users_ticket(client):
    other = _ticket(user_id="someone-else")
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=other),
        ),
    ):
        response = await client.get(URL)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_activity_stream_reports_a_poll_failure_as_an_error_frame(client):
    running = _ticket(status="running")

    async def flaky_get(_ticket_id: str):
        if not calls:
            calls.append(True)
            return running
        raise RuntimeError("db down")

    calls: list[bool] = []
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(f"{MODULE}.tickets_db.get_ticket", new=flaky_get),
        patch(f"{MODULE}.tickets_db.list_inputs", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.tickets_db.list_reports", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.ACTIVITY_STREAM_POLL_S", 0.01),
    ):
        frames = await _collect(client, stop_events=("error",))

    errors = [data for _, event, data in frames if event == "error"]
    assert errors and "unavailable" in errors[0]["message"].lower()


def test_activity_cursor_round_trips_both_sources():
    from src.server.app.ticket_activity_stream import (
        parse_ticket_activity_cursor,
        ticket_activity_cursor,
    )

    cursor = ticket_activity_cursor("report:abc-123", "run-9", 17, "1699999999-0")
    assert cursor == "report:abc-123~run-9~17~1699999999-0"
    assert parse_ticket_activity_cursor(cursor) == (
        "report:abc-123",
        "run-9",
        17,
        "1699999999-0",
    )
    # A pre-daemon-stream client's three-part cursor still parses.
    assert parse_ticket_activity_cursor("report:abc-123~run-9~17") == (
        "report:abc-123",
        "run-9",
        17,
        "",
    )
    assert parse_ticket_activity_cursor(None) == ("", "", 0, "")
    assert parse_ticket_activity_cursor("garbage") == ("garbage", "", 0, "")
    assert parse_ticket_activity_cursor("~run-1~not-a-number") == (
        "",
        "run-1",
        0,
        "",
    )


def test_worker_output_keeps_text_and_drops_tool_arguments():
    from src.server.app.ticket_activity_stream import _worker_output_payload

    chunk = _worker_output_payload(
        "message_chunk", {"content": "token", "content_type": "text", "agent": "main"}
    )
    assert chunk == {"kind": "text", "text": "token", "agent": "main"}

    reasoning = _worker_output_payload(
        "message_chunk", {"content": "why", "content_type": "reasoning"}
    )
    assert reasoning == {"kind": "reasoning", "text": "why"}

    assert (
        _worker_output_payload(
            "message_chunk", {"content": "start", "content_type": "reasoning_signal"}
        )
        is None
    )

    tool = _worker_output_payload(
        "tool_calls",
        {
            "tool_calls": [
                {"name": "bash", "args": {"command": "cat ~/.ssh/id_rsa"}},
                {"name": "read_file", "args": {"path": "secret.env"}},
            ]
        },
    )
    assert tool == {"kind": "tool", "tools": ["bash", "read_file"]}
    assert "secret" not in json.dumps(tool)

    # Tool results can carry file contents; they are not printed by a watch.
    assert _worker_output_payload("tool_call_result", {"content": "secret"}) is None
    assert _worker_output_payload("run_end", {"outcome": "completed"}) is None


def test_worker_run_frame_parser_reads_the_stored_wire_format():
    from src.server.app.ticket_activity_stream import _parse_run_frame

    seq, event, data = _parse_run_frame(
        'id: 42\nevent: message_chunk\ndata: {"content": "hi", "content_type": "text"}'
    )
    assert (seq, event, data) == (
        42,
        "message_chunk",
        {"content": "hi", "content_type": "text"},
    )
    # The caught_up/boundary frames carry no id: they must not move the cursor.
    assert _parse_run_frame("event: caught_up\ndata: {}") == (None, "caught_up", {})
    assert _parse_run_frame(":keepalive") == (None, "", {})


@pytest.mark.asyncio
async def test_activity_stream_waits_for_the_run_tail_after_terminal(client):
    """A Worker can persist its report (flipping the Ticket terminal) and keep
    streaming model text inside the same run; the watch must drain to run_end
    instead of closing on the status frame."""
    running = _ticket(
        status="running", worker_thread_id="thread-1", worker_run_id="run-1"
    )
    completed = _ticket(
        status="completed",
        worker_thread_id="thread-1",
        worker_run_id="run-1",
        summary="done",
    )

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        async def frames():
            yield (
                "id: 1\nevent: message_chunk\n"
                'data: {"content": "early", "content_type": "text"}\n\n'
            )
            # Longer than the old fixed two-second terminal grace: the stream
            # must wait for run_end, not for a timer.
            await asyncio.sleep(2.2)
            yield (
                "id: 2\nevent: message_chunk\n"
                'data: {"content": "late", "content_type": "text"}\n\n'
            )
            yield "event: run_end\ndata: {}\n\n"

        return frames()

    patches = _activity_patches(tickets=[running, running, completed])
    _enter(patches)
    try:
        with patch(f"{MODULE}._worker_run_frames", new=fake_run_frames):
            frames = await _collect(client, timeout=8.0)
    finally:
        _exit(patches)

    outputs = [data["text"] for _, event, data in frames if event == "output"]
    assert outputs == ["early", "late"]
    end_index = next(index for index, frame in enumerate(frames) if frame[1] == "end")
    assert all(
        frame[1] != "output" for frame in frames[end_index + 1 :]
    ), "the stream must not emit output after its end frame"


@pytest.mark.asyncio
async def test_worker_run_pump_discards_a_stale_run_generation(monkeypatch):
    """A replaced run must not leak frames (and its sequence) into the new run:
    otherwise a reconnect cursor carries the old run's seq for the new run."""
    import time

    from src.server.app import ticket_activity_stream as stream_module

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        async def frames():
            if run_id == "run-1":
                yield (
                    "id: 100\nevent: message_chunk\n"
                    'data: {"content": "old", "content_type": "text"}\n\n'
                )
                await asyncio.sleep(5)
            else:
                yield (
                    "id: 1\nevent: message_chunk\n"
                    'data: {"content": "new", "content_type": "text"}\n\n'
                )
                yield "event: run_end\ndata: {}\n\n"

        return frames()

    monkeypatch.setattr(
        stream_module, "_worker_run_frames", fake_run_frames, raising=True
    )
    pump = stream_module._RunPump()
    pump.ensure("thread-1", "run-1", seq=0, ticket_terminal=False)
    # Let the first generation queue its frame before the run switches.
    await asyncio.sleep(0.02)
    pump.ensure("thread-1", "run-2", seq=0, ticket_terminal=False)
    state = {"run_id": "run-2", "run_seq": 0}
    payloads: list[str] = []
    deadline = time.monotonic() + 2.0
    try:
        while time.monotonic() < deadline:
            present, item = pump.get_nowait()
            if not present:
                present, item = await pump.wait(0.05)
                if not present:
                    continue
            if item is None:
                break
            payload = stream_module._consume_worker_item(item, state)
            if payload:
                payloads.append(payload["text"])
    finally:
        await pump.close()

    assert payloads == ["new"]
    assert state["run_seq"] == 1
    assert pump.run_done is True


def test_worker_run_frame_parser_keeps_unicode_line_separators():
    """U+2028/U+2029/NEL are legal JSON string content and must not be read as
    SSE line endings (``str.splitlines`` would corrupt the payload)."""
    from src.server.app.ticket_activity_stream import (
        _parse_run_frame,
        _worker_output_payload,
    )

    content = "hello\u2028world\u2029!\x85end"
    frame = (
        "id: 7\nevent: message_chunk\ndata: "
        + json.dumps({"content": content, "content_type": "text"}, ensure_ascii=False)
        + "\n\n"
    )
    seq, event, data = _parse_run_frame(frame)
    assert seq == 7 and event == "message_chunk"
    assert data["content"] == content
    assert _worker_output_payload(event, data) == {"kind": "text", "text": content}


def test_consume_worker_item_rejects_frames_from_another_generation():
    from src.server.app.ticket_activity_stream import _consume_worker_item

    stale = {"run_id": "run-1", "seq": 100, "payload": {"kind": "text", "text": "x"}}
    # Another run's frame never moves the current run's cursor.
    state = {"run_id": "run-2", "run_seq": 0}
    assert _consume_worker_item(stale, state) is None
    assert state["run_seq"] == 0
    # Nor does it when the Ticket cleared its Worker ids (empty current run).
    cleared = {"run_id": "", "run_seq": 0}
    assert _consume_worker_item(stale, cleared) is None
    assert cleared["run_seq"] == 0
    # The current generation is accepted and advances the cursor.
    current = {"run_id": "run-2", "seq": 3, "payload": {"kind": "text", "text": "ok"}}
    assert _consume_worker_item(current, state) == {"kind": "text", "text": "ok"}
    assert state["run_seq"] == 3


@pytest.mark.asyncio
async def test_worker_run_pump_retires_when_the_ticket_clears_its_ids():
    from src.server.app import ticket_activity_stream as stream_module

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        async def frames():
            yield (
                "id: 100\nevent: message_chunk\n"
                'data: {"content": "stale", "content_type": "text"}\n\n'
            )
            await asyncio.sleep(5)

        return frames()

    with patch(f"{MODULE}._worker_run_frames", new=fake_run_frames):
        pump = stream_module._RunPump()
        pump.ensure("thread-1", "run-1", seq=0, ticket_terminal=False)
        await asyncio.sleep(0.02)
        pump.ensure("", "", seq=0, ticket_terminal=False)
        assert pump.task is None
        await asyncio.sleep(0.01)
        present, item = pump.get_nowait()
        assert (present, item) == (False, None)
        await pump.close()


@pytest.mark.asyncio
async def test_activity_stream_discards_a_stale_run_after_ids_are_cleared(client):
    """A retry can clear worker_thread_id/worker_run_id while an old tail is
    still attached; its frames must not be emitted or leak into the cursor."""
    with_run = _ticket(
        status="running", worker_thread_id="thread-1", worker_run_id="run-1"
    )
    cleared = _ticket(status="running")
    cleared_done = _ticket(status="completed", summary="done")

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        async def frames():
            await asyncio.sleep(0.1)
            yield (
                "id: 100\nevent: message_chunk\n"
                'data: {"content": "STALE_OLD_RUN", "content_type": "text"}\n\n'
            )

        return frames()

    patches = _activity_patches(tickets=[with_run, with_run, cleared, cleared_done])
    _enter(patches)
    try:
        with patch(f"{MODULE}._worker_run_frames", new=fake_run_frames):
            frames = await _collect(client, timeout=8.0)
    finally:
        _exit(patches)

    outputs = [data for _, event, data in frames if event == "output"]
    assert outputs == []
    ids = [frame[0] or "" for frame in frames]
    assert not any("~100" in event_id for event_id in ids)


@pytest.mark.asyncio
async def test_activity_stream_terminal_grace_is_a_hard_bound(client):
    completed = _ticket(
        status="completed",
        worker_thread_id="thread-1",
        worker_run_id="run-1",
        summary="done",
    )

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        async def frames():
            yield (
                "id: 1\nevent: message_chunk\n"
                'data: {"content": "queued", "content_type": "text"}\n\n'
            )
            await asyncio.sleep(0.05)
            yield (
                "id: 2\nevent: message_chunk\n"
                'data: {"content": "late", "content_type": "text"}\n\n'
            )
            yield "event: run_end\ndata: {}\n\n"

        return frames()

    patches = _activity_patches(tickets=[completed])
    # Separate patch entry: the grace constant is read when the terminal drain
    # starts, so it has to be overridden for this one stream.
    patches = patches + (patch(f"{MODULE}.ACTIVITY_STREAM_TERMINAL_RUN_GRACE_S", 0.02),)
    _enter(patches)
    try:
        with patch(f"{MODULE}._worker_run_frames", new=fake_run_frames):
            frames = await _collect(client, timeout=8.0)
    finally:
        _exit(patches)

    outputs = [data["text"] for _, event, data in frames if event == "output"]
    assert "late" not in outputs
    assert any(event == "end" for _, event, _ in frames)


@pytest.mark.asyncio
async def test_activity_stream_keeps_polling_under_a_busy_worker_tail(monkeypatch):
    """A replenishing output queue must not starve Ticket state reads or the
    stream ceiling: batches are bounded and a poll is forced when due."""
    from contextlib import suppress

    from src.server.app import ticket_activity_stream as stream_module

    row = _ticket(status="running", worker_thread_id="thread-1", worker_run_id="run-1")
    poll = AsyncMock(return_value=(row, []))

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        async def frames():
            seq = int(last_event_id or 0)
            while True:
                seq += 1
                yield (
                    f"id: {seq}\nevent: message_chunk\n"
                    'data: {"content": "tok", "content_type": "text"}\n\n'
                )
                await asyncio.sleep(0)

        return frames()

    monkeypatch.setattr(stream_module, "_worker_run_frames", fake_run_frames)
    monkeypatch.setattr(stream_module, "_poll_activity", poll)
    monkeypatch.setattr(
        stream_module.tickets_db, "list_inputs", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        stream_module.tickets_db, "list_reports", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(stream_module, "ACTIVITY_STREAM_MAX_S", 0.05)
    monkeypatch.setattr(stream_module, "ACTIVITY_STREAM_POLL_S", 0.02)

    frames: list[str] = []
    stream = stream_module.ticket_activity_events("ticket-1", initial_ticket=row)

    async def consume() -> None:
        async for frame in stream:
            frames.append(frame)
            if "event: timeout" in frame:
                return
            # Simulate a slow downstream consumer of the output frames.
            await asyncio.sleep(0.002)

    try:
        await asyncio.wait_for(consume(), timeout=5.0)
    finally:
        with suppress(Exception):
            await stream.aclose()

    assert any("event: timeout" in frame for frame in frames)
    assert poll.await_count >= 1
    assert any("event: output" in frame for frame in frames)


@pytest.mark.asyncio
async def test_activity_stream_rechecks_state_during_terminal_drain(client):
    """A cancelled Ticket can go back to queued while its old run is still
    draining; the stream must adopt the new state and not emit an obsolete
    cancelled ``end`` or stale-run output."""
    cancelled = _ticket(
        status="cancelled", worker_thread_id="thread-1", worker_run_id="run-1"
    )
    queued = _ticket(status="queued")  # retried: Worker ids cleared
    completed = _ticket(status="completed", summary="done")

    old_run_id = {"value": 0}

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        async def frames():
            sequence = int(last_event_id or 0)
            while True:
                sequence += 1
                old_run_id["value"] = sequence
                yield (
                    f"id: {sequence}\nevent: message_chunk\n"
                    'data: {"content": "old", "content_type": "text"}\n\n'
                )
                await asyncio.sleep(0.002)

        return frames()

    patches = _activity_patches(
        tickets=[cancelled, cancelled, queued, queued, completed]
    )
    _enter(patches)
    try:
        with patch(f"{MODULE}._worker_run_frames", new=fake_run_frames):
            frames = await _collect(client, timeout=8.0)
    finally:
        _exit(patches)

    statuses = [data["status"] for _, event, data in frames if event == "status"]
    assert statuses == ["cancelled", "queued", "completed"]
    ends = [data for _, event, data in frames if event == "end"]
    assert ends and ends[-1]["status"] == "completed"
    queued_index = next(
        index
        for index, (_, event, data) in enumerate(frames)
        if event == "status" and data.get("status") == "queued"
    )
    assert all(
        event != "output" for _, event, _ in frames[queued_index:]
    ), "old-run output must stop once the retry clears the Worker ids"


@pytest.mark.asyncio
async def test_worker_run_pump_bounds_its_queue(monkeypatch):
    """A paused HTTP consumer must push back, not grow memory without bound."""
    from src.server.app import ticket_activity_stream as stream_module

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        async def frames():
            for index in range(10_000):
                yield (
                    f"id: {index + 1}\nevent: message_chunk\n"
                    'data: {"content": "x", "content_type": "text"}\n\n'
                )

        return frames()

    monkeypatch.setattr(stream_module, "_worker_run_frames", fake_run_frames)
    pump = stream_module._RunPump()
    pump.ensure("thread-1", "run-1", seq=0, ticket_terminal=False)
    try:
        await asyncio.sleep(0.05)
        assert (
            pump._queue.qsize()  # noqa: SLF001 - the bound is the behaviour under test
            <= stream_module.ACTIVITY_STREAM_OUTPUT_QUEUE_MAX
        )
    finally:
        await pump.close()


@pytest.mark.asyncio
async def test_terminal_stream_repolls_before_closing_after_a_due_poll(monkeypatch):
    """A tail that closes without ``run_end`` must not end on a stale terminal
    read when the next durable poll is already due (a retry may have revived
    the Ticket)."""
    from types import SimpleNamespace

    from src.server.app import ticket_activity_stream as stream_module

    clock = SimpleNamespace(value=100.0)
    cancelled = _ticket(
        status="cancelled", worker_thread_id="thread-1", worker_run_id="run-1"
    )
    queued = _ticket(status="queued")
    current = {"row": cancelled}

    async def fake_poll(_ticket_id: str):
        return current["row"], []

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        async def frames():
            yield (
                "id: 1\nevent: message_chunk\n"
                'data: {"content": "last token", "content_type": "text"}\n\n'
            )

        return frames()

    monkeypatch.setattr(
        stream_module, "time", SimpleNamespace(monotonic=lambda: clock.value)
    )
    monkeypatch.setattr(stream_module, "_poll_activity", fake_poll)
    monkeypatch.setattr(stream_module, "_worker_run_frames", fake_run_frames)
    monkeypatch.setattr(stream_module, "project_task_activity", lambda *args: [])
    monkeypatch.setattr(
        stream_module.tickets_db, "list_inputs", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        stream_module.tickets_db, "list_reports", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(stream_module, "ACTIVITY_STREAM_POLL_S", 1.0)
    monkeypatch.setattr(stream_module, "ACTIVITY_STREAM_MAX_S", 100.0)

    frames: list[str] = []
    stream = stream_module.ticket_activity_events("ticket-1", initial_ticket=cancelled)
    try:
        while True:
            frame = await anext(stream)
            frames.append(frame)
            if "event: output" in frame:
                # A slow consumer crosses the next poll deadline, and the
                # Ticket is retried meanwhile.
                clock.value = 102.0
                current["row"] = queued
            elif "event: status" in frame and '"status": "queued"' in frame:
                # Past the stream ceiling: the timeout is the clean exit here.
                clock.value = 205.0
    except StopAsyncIteration:
        pass
    finally:
        with __import__("contextlib").suppress(Exception):
            await stream.aclose()

    statuses = [
        json.loads(frame.split("data: ", 1)[1].split("\n")[0])["status"]
        for frame in frames
        if "event: status" in frame
    ]
    assert statuses == ["cancelled", "queued"]
    assert not any(
        "event: end" in frame and '"status": "cancelled"' in frame for frame in frames
    )
    assert any("event: timeout" in frame for frame in frames)


@pytest.mark.asyncio
async def test_activity_stream_retries_a_transient_worker_tail_failure(client):
    """A transient run-reader failure must not silently stop token delivery
    while the SSE connection stays open: retry from the queued sequence."""
    running = _ticket(
        status="running", worker_thread_id="thread-1", worker_run_id="run-1"
    )
    completed = _ticket(
        status="completed",
        worker_thread_id="thread-1",
        worker_run_id="run-1",
        summary="done",
    )
    attempts: list[int] = []

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        attempts.append(last_event_id)

        async def frames():
            if len(attempts) == 1:
                raise RuntimeError("transient run-ledger failure")
            yield (
                "id: 1\nevent: message_chunk\n"
                'data: {"content": "RECOVERED", "content_type": "text"}\n\n'
            )
            yield "event: run_end\ndata: {}\n\n"

        return frames()

    patches = _activity_patches(tickets=[running, running, completed]) + (
        patch(f"{MODULE}.ACTIVITY_STREAM_TAIL_BACKOFF_S", 0.0),
    )
    _enter(patches)
    try:
        with patch(f"{MODULE}._worker_run_frames", new=fake_run_frames):
            frames = await _collect(client, timeout=8.0)
    finally:
        _exit(patches)

    outputs = [data["text"] for _, event, data in frames if event == "output"]
    assert "RECOVERED" in outputs
    assert len(attempts) >= 2
    assert not any(event == "error" for _, event, _ in frames)


@pytest.mark.asyncio
async def test_activity_stream_signals_an_exhausted_worker_tail(client):
    """When the bounded retries are spent, signal the client so it can
    reattach, instead of polling and pinging a stream that lost its tokens."""
    running = _ticket(
        status="running", worker_thread_id="thread-1", worker_run_id="run-1"
    )
    attempts: list[int] = []

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        attempts.append(last_event_id)

        async def frames():
            raise RuntimeError("run reader is down")
            yield  # pragma: no cover

        return frames()

    patches = _activity_patches(tickets=[running]) + (
        patch(f"{MODULE}.ACTIVITY_STREAM_TAIL_BACKOFF_S", 0.0),
        patch(f"{MODULE}.ACTIVITY_STREAM_TAIL_RETRIES", 2),
        patch(f"{MODULE}.ACTIVITY_STREAM_MAX_S", 5.0),
    )
    _enter(patches)
    try:
        with patch(f"{MODULE}._worker_run_frames", new=fake_run_frames):
            frames = await _collect(client, stop_events=("error", "end", "timeout"))
    finally:
        _exit(patches)

    errors = [data for _, event, data in frames if event == "error"]
    assert errors and "worker output" in errors[0]["message"].lower()
    # Initial attempt + the configured two retries.
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_activity_stream_retries_the_tail_of_a_terminal_ticket(client):
    """A failed reader must be retried even when the Ticket is already
    terminal: its stream can still hold the final tokens."""
    completed = _ticket(
        status="completed",
        worker_thread_id="thread-1",
        worker_run_id="run-1",
        summary="done",
    )
    attempts: list[int] = []

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        attempts.append(last_event_id)

        async def frames():
            if len(attempts) == 1:
                raise RuntimeError("transient run-ledger failure")
            yield (
                "id: 1\nevent: message_chunk\n"
                'data: {"content": "FINAL TOKEN", "content_type": "text"}\n\n'
            )
            yield "event: run_end\ndata: {}\n\n"

        return frames()

    patches = _activity_patches(tickets=[completed]) + (
        patch(f"{MODULE}.ACTIVITY_STREAM_TAIL_BACKOFF_S", 0.0),
    )
    _enter(patches)
    try:
        with patch(f"{MODULE}._worker_run_frames", new=fake_run_frames):
            frames = await _collect(client, timeout=8.0)
    finally:
        _exit(patches)

    outputs = [data["text"] for _, event, data in frames if event == "output"]
    assert "FINAL TOKEN" in outputs
    assert len(attempts) >= 2
    assert any(event == "end" for _, event, _ in frames)
    assert not any(event == "error" for _, event, _ in frames)


@pytest.mark.asyncio
async def test_activity_stream_retries_a_premature_empty_tail(client):
    """The run reader can return without frames when its pool is unavailable;
    on a live Ticket that is a premature EOF, not the end of token delivery."""
    running = _ticket(
        status="running", worker_thread_id="thread-1", worker_run_id="run-1"
    )
    completed = _ticket(
        status="completed",
        worker_thread_id="thread-1",
        worker_run_id="run-1",
        summary="done",
    )
    attempts: list[int] = []

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        attempts.append(last_event_id)

        async def frames():
            if len(attempts) == 1:
                return  # premature EOF: no frames, no run_end
            yield (
                "id: 1\nevent: message_chunk\n"
                'data: {"content": "RESUMED", "content_type": "text"}\n\n'
            )
            yield "event: run_end\ndata: {}\n\n"

        return frames()

    patches = _activity_patches(tickets=[running, running, running, completed]) + (
        patch(f"{MODULE}.ACTIVITY_STREAM_TAIL_BACKOFF_S", 0.0),
    )
    _enter(patches)
    try:
        with patch(f"{MODULE}._worker_run_frames", new=fake_run_frames):
            frames = await _collect(client, timeout=8.0)
    finally:
        _exit(patches)

    outputs = [data["text"] for _, event, data in frames if event == "output"]
    assert "RESUMED" in outputs
    assert len(attempts) >= 2
    assert not any(event == "error" for _, event, _ in frames)


@pytest.mark.asyncio
async def test_activity_stream_does_not_retry_a_terminal_tickets_clean_eof(client):
    """A completed Ticket whose retained stream is already gone (clean EOF, no
    run_end) must end instead of retrying a stream that will never appear."""
    completed = _ticket(
        status="completed",
        worker_thread_id="thread-1",
        worker_run_id="run-1",
        summary="done",
    )
    attempts: list[int] = []

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        attempts.append(last_event_id)

        async def frames():
            return
            yield  # pragma: no cover

        return frames()

    patches = _activity_patches(tickets=[completed]) + (
        patch(f"{MODULE}.ACTIVITY_STREAM_TAIL_BACKOFF_S", 0.0),
    )
    _enter(patches)
    try:
        with patch(f"{MODULE}._worker_run_frames", new=fake_run_frames):
            frames = await _collect(client, timeout=8.0)
    finally:
        _exit(patches)

    assert len(attempts) == 1
    assert any(event == "end" for _, event, _ in frames)
    assert not any(event == "error" for _, event, _ in frames)


@pytest.mark.asyncio
async def test_worker_tail_earns_a_fresh_retry_budget_after_sustained_health(
    monkeypatch,
):
    """A quick first frame must not reset the budget, but 60s of streaming must."""
    from types import SimpleNamespace

    from src.server.app import ticket_activity_stream as stream_module

    clock = SimpleNamespace(value=100.0)
    fake_time = SimpleNamespace(monotonic=lambda: clock.value)
    health = {"seconds": 61.0}

    def fake_run_frames(thread_id: str, run_id: str, last_event_id: int):
        async def frames():
            yield (
                "id: 1\nevent: message_chunk\n"
                'data: {"content": "A", "content_type": "text"}\n\n'
            )
            clock.value += health["seconds"]
            yield (
                "id: 2\nevent: message_chunk\n"
                'data: {"content": "B", "content_type": "text"}\n\n'
            )
            raise RuntimeError("later failure")

        return frames()

    monkeypatch.setattr(stream_module, "time", fake_time)
    monkeypatch.setattr(stream_module, "_worker_run_frames", fake_run_frames)
    pump = stream_module._RunPump()
    pump._restarts = 3  # noqa: SLF001 - the budget is the behaviour under test
    pump.ensure("thread-1", "run-1", seq=0, ticket_terminal=False)
    await pump.task
    try:
        assert pump._restarts == 0  # noqa: SLF001
        assert pump.failed is True
        assert pump.last_seq == 2
    finally:
        await pump.close()


# ---------------------------------------------------------------------------
# Local daemon/native Worker live output (database event log, epoch-fenced)
# ---------------------------------------------------------------------------


def _daemon_ticket(**overrides) -> dict:
    return _ticket(worker_daemon_id="daemon-1", **overrides)


def _entry(epoch: int, seq: int, data: dict) -> dict:
    return {"seq": seq, "cursor": f"{epoch}.{seq}", "data": data}


def _page(epoch: int, entries: list[dict], *, reset=None, cursor=None, start=None):
    from src.server.services.autonomy.daemon_output import OutputPage

    last = entries[-1]["seq"] if entries else 0
    first = entries[0]["seq"] - 1 if entries else 0
    return OutputPage(
        epoch=epoch,
        cursor=cursor or f"{epoch}.{last}",
        start=start or f"{epoch}.{first}",
        reset=reset,
        events=entries,
    )


@pytest.mark.asyncio
async def test_activity_stream_tails_daemon_output_and_resumes_by_epoch_seq(client):
    running = _daemon_ticket(status="running")
    completed = _daemon_ticket(status="completed", summary="done")
    calls: list[str | None] = []

    async def fake_read(ticket_id: str, cursor, *, count=64):
        calls.append(cursor)
        if cursor is None:
            return _page(
                2,
                [
                    _entry(2, 1, {"kind": "text", "text": "Hel", "run_id": "r1"}),
                    _entry(2, 2, {"kind": "tool", "tools": ["bash"], "run_id": "r1"}),
                ],
            )
        if cursor == "2.2":
            return _page(
                2, [_entry(2, 3, {"kind": "text", "text": "lo", "run_id": "r1"})]
            )
        return _page(2, [], cursor=cursor)

    patches = _activity_patches(tickets=[running, running, completed])
    _enter(patches)
    try:
        with patch(f"{MODULE}.daemon_output_store.read_output", new=fake_read):
            frames = await _collect(client, timeout=8.0)
    finally:
        _exit(patches)

    outputs = [data for _, event, data in frames if event == "output"]
    assert [row.get("text") for row in outputs if row.get("kind") == "text"] == [
        "Hel",
        "lo",
    ]
    tool = {"kind": "tool", "tools": ["bash"], "run": "daemon:r1", "epoch": 2}
    assert tool in outputs
    assert all(row.get("epoch") == 2 for row in outputs)
    assert all(str(row.get("run") or "").startswith("daemon:r1") for row in outputs)
    assert calls[0] is None
    assert "2.2" in calls
    last_output = [frame for frame in frames if frame[1] == "output"][-1]
    assert last_output[0].endswith("~2.3~d2")


@pytest.mark.asyncio
async def test_activity_stream_reconnect_resumes_the_daemon_cursor(client):
    completed = _daemon_ticket(status="completed", summary="done")
    seen: list[str | None] = []

    async def fake_read(ticket_id: str, cursor, *, count=64):
        seen.append(cursor)
        if cursor == "3.1":
            return _page(
                3, [_entry(3, 2, {"kind": "text", "text": "tail", "run_id": "r1"})]
            )
        return _page(3, [], cursor=cursor or "")

    resume = f"created:{TICKET_ID}~~0~3.1"
    patches = _activity_patches(tickets=[completed])
    _enter(patches)
    try:
        with patch(f"{MODULE}.daemon_output_store.read_output", new=fake_read):
            frames = await _collect(client, headers={"Last-Event-ID": resume})
    finally:
        _exit(patches)

    assert seen and seen[0] == "3.1"
    outputs = [data["text"] for _, event, data in frames if event == "output"]
    assert outputs == ["tail"]


@pytest.mark.asyncio
async def test_activity_stream_leads_with_a_reset_when_the_cursor_names_another_epoch(
    client,
):
    """A watcher that holds a prefix from epoch 1 is told to reset before the
    first byte of epoch 2; the reset payload itself carries no text."""
    completed = _daemon_ticket(status="completed", summary="done")

    async def fake_read(ticket_id: str, cursor, *, count=64):
        if cursor == "1.5":
            return _page(
                2,
                [_entry(2, 1, {"kind": "text", "text": "fresh\n", "run_id": "r2"})],
                reset="new_epoch",
            )
        return _page(2, [], cursor=cursor or "")

    resume = f"created:{TICKET_ID}~~0~1.5"
    patches = _activity_patches(tickets=[completed])
    _enter(patches)
    try:
        with patch(f"{MODULE}.daemon_output_store.read_output", new=fake_read):
            frames = await _collect(client, headers={"Last-Event-ID": resume})
    finally:
        _exit(patches)

    outputs = [(cursor, data) for cursor, event, data in frames if event == "output"]
    assert [data["kind"] for _, data in outputs] == ["output_reset", "text"]
    reset_cursor, reset = outputs[0]
    assert reset == {
        "kind": "output_reset",
        "epoch": 2,
        "reason": "new_epoch",
        "run": "daemon",
    }
    # Dropping the connection right after the reset must not skip the epoch's
    # first event, nor trigger the same reset again.
    assert reset_cursor.endswith("~2.0~d2")
    assert outputs[1][1]["text"] == "fresh\n"


@pytest.mark.asyncio
async def test_activity_stream_forwards_a_taint_marker_without_stored_detail(client):
    completed = _daemon_ticket(status="completed", summary="done")

    async def fake_read(ticket_id: str, cursor, *, count=64):
        if cursor is None:
            return _page(
                1,
                [
                    _entry(1, 1, {"kind": "text", "text": "ok\n", "run_id": "r1"}),
                    _entry(
                        1,
                        2,
                        {
                            "kind": "output_reset",
                            "reason": "credential_key",  # an unexpected detail
                            "run_id": "r1",
                        },
                    ),
                ],
            )
        return _page(1, [], cursor=cursor)

    patches = _activity_patches(tickets=[completed])
    _enter(patches)
    try:
        with patch(f"{MODULE}.daemon_output_store.read_output", new=fake_read):
            frames = await _collect(client)
    finally:
        _exit(patches)

    outputs = [data for _, event, data in frames if event == "output"]
    assert outputs[1] == {
        "kind": "output_reset",
        "epoch": 1,
        "reason": "continuity_lost",
        "run": "daemon:r1",
    }


@pytest.mark.asyncio
async def test_activity_stream_daemon_store_outage_is_a_silent_fallback(client):
    """An unavailable store must leave the activity/status stream intact."""
    completed = _daemon_ticket(status="completed", summary="done")

    async def unavailable(ticket_id: str, cursor, *, count=64):
        from src.server.services.autonomy.daemon_output import OutputPage

        return OutputPage(cursor=cursor or "")

    patches = _activity_patches(tickets=[completed])
    _enter(patches)
    try:
        with patch(f"{MODULE}.daemon_output_store.read_output", new=unavailable):
            frames = await _collect(client)
    finally:
        _exit(patches)

    assert any(event == "activity" for _, event, _ in frames)
    assert any(event == "status" for _, event, _ in frames)
    assert any(event == "end" for _, event, _ in frames)
    assert not any(event == "error" for _, event, _ in frames)


@pytest.mark.asyncio
async def test_activity_stream_does_not_read_daemon_output_for_other_workers(client):
    """A PTC Ticket must not pay a database read on every watch loop."""
    completed = _ticket(status="completed", summary="done")
    read = AsyncMock()

    patches = _activity_patches(tickets=[completed])
    _enter(patches)
    try:
        with patch(f"{MODULE}.daemon_output_store.read_output", new=read):
            frames = await _collect(client)
    finally:
        _exit(patches)

    assert any(event == "end" for _, event, _ in frames)
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_stream_drains_every_retained_daemon_event(client):
    """A terminal Ticket must drain the whole retained daemon log, not one
    final batch: a slow consumer previously lost the tail before `end`."""
    completed = _daemon_ticket(status="completed", summary="done")
    total = 300
    events = [
        _entry(1, index + 1, {"kind": "text", "text": f"t{index}", "run_id": "r1"})
        for index in range(total)
    ]

    async def fake_read(ticket_id: str, cursor, *, count=64):
        start = int(cursor.split(".")[1]) if cursor else 0
        return _page(1, events[start : start + min(count, 100)], cursor=cursor or "")

    patches = _activity_patches(tickets=[completed])
    _enter(patches)
    try:
        with patch(f"{MODULE}.daemon_output_store.read_output", new=fake_read):
            frames = await _collect(client, timeout=10.0)
    finally:
        _exit(patches)

    outputs = [data["text"] for _, event, data in frames if event == "output"]
    assert outputs == [f"t{index}" for index in range(total)]
    assert outputs[-1] == "t299"


@pytest.mark.asyncio
async def test_timeout_drain_respects_the_stream_deadline(client, monkeypatch):
    """A live store that keeps answering must not postpone `timeout` forever;
    the final drain is bounded by the same stream deadline."""
    completed = _daemon_ticket(status="completed")
    position = {"seq": 0}

    async def endless_read(ticket_id, cursor, *, count=64):
        await asyncio.sleep(0.01)
        entries = []
        for _ in range(100):
            position["seq"] += 1
            entries.append(
                _entry(
                    1,
                    position["seq"],
                    {"kind": "text", "text": "tail", "run_id": "r1"},
                )
            )
        return _page(1, entries)

    monkeypatch.setattr(f"{MODULE}.ACTIVITY_STREAM_MAX_S", 0.15)
    patches = _activity_patches(tickets=[completed])
    _enter(patches)
    try:
        with patch(f"{MODULE}.daemon_output_store.read_output", new=endless_read):
            frames = await _collect(client, timeout=3.0)
    finally:
        _exit(patches)

    events = [event for _, event, _ in frames]
    assert "timeout" in events
    assert len([event for event in events if event == "output"]) < 5000


@pytest.mark.asyncio
async def test_terminal_drain_ends_when_the_store_keeps_returning_the_same_reset(client):
    """A store that answers the same gap page forever must not turn the final
    drain into a reset loop: no cursor progress ends it."""
    completed = _daemon_ticket(status="completed", summary="done")
    calls = {"n": 0}

    async def stuck(ticket_id: str, cursor, *, count=64):
        calls["n"] += 1
        return _page(4, [], reset="gap", cursor="4.0", start="4.0")

    patches = _activity_patches(tickets=[completed])
    _enter(patches)
    try:
        with patch(f"{MODULE}.daemon_output_store.read_output", new=stuck):
            frames = await _collect(client, timeout=5.0)
    finally:
        _exit(patches)

    resets = [d for _, e, d in frames if e == "output" and d["kind"] == "output_reset"]
    assert 1 <= len(resets) <= 2
    assert any(event == "end" for _, event, _ in frames)
    assert calls["n"] < 20


@pytest.mark.asyncio
async def test_real_store_gap_over_expired_rows_emits_one_reset_then_ends(client):
    from tests.unit.server.fake_output_db import FakeOutputDb

    fake = FakeOutputDb()
    fake.allow("d", TICKET_ID, "l")
    from src.server.services.autonomy import daemon_output as store

    completed = _daemon_ticket(status="completed", summary="done")
    patches = _activity_patches(tickets=[completed])
    _enter(patches)
    try:
        with patch.object(store, "output_db", fake):
            first = await store.append_output(
                "d", TICKET_ID, "l", "r", None, 1, [{"kind": "text", "text": "a\n"}]
            )
            await store.append_output(
                "d", TICKET_ID, "l", "r", first.epoch, 2, [{"kind": "text", "text": "b\n"}]
            )
            fake.events[(TICKET_ID, first.epoch)].clear()
            resume = f"created:{TICKET_ID}~~0~{first.epoch}.1"
            frames = await _collect(client, headers={"Last-Event-ID": resume}, timeout=5.0)
    finally:
        _exit(patches)

    resets = [d for _, e, d in frames if e == "output" and d["kind"] == "output_reset"]
    assert len(resets) == 1 and resets[0]["reason"] == "gap"
    assert any(event == "end" for _, event, _ in frames)


@pytest.mark.asyncio
async def test_a_requeued_ticket_keeps_the_daemon_cursor_so_a_new_epoch_resets(client):
    """The Ticket loses its daemon (requeued), later a daemon starts epoch 2.
    The connection kept the epoch-1 cursor, so the first epoch-2 read leads with
    a reset instead of silently starting a new stream."""
    owned = _daemon_ticket(status="running")
    requeued = _ticket(status="queued", worker_daemon_id=None)
    reowned = _daemon_ticket(status="running")
    done = _daemon_ticket(status="completed", summary="done")
    seen: list[str | None] = []

    async def fake_read(ticket_id: str, cursor, *, count=64):
        seen.append(cursor)
        if cursor is None:
            return _page(
                1, [_entry(1, 1, {"kind": "text", "text": "password \n", "run_id": "r1"})]
            )
        if cursor == "1.1":
            return _page(
                2,
                [_entry(2, 1, {"kind": "text", "text": "=ordinary\n", "run_id": "r2"})],
                reset="new_epoch",
            )
        return _page(2, [], cursor=cursor)

    patches = _activity_patches(tickets=[owned, requeued, reowned, done])
    _enter(patches)
    try:
        with patch(f"{MODULE}.daemon_output_store.read_output", new=fake_read):
            frames = await _collect(client, timeout=10.0)
    finally:
        _exit(patches)

    kinds = [d["kind"] for _, e, d in frames if e == "output"]
    assert kinds == ["text", "output_reset", "text"]
    assert seen[0] is None and "1.1" in seen


# ---------------------------------------------------------------------------
# Source fence: cloud run <-> local daemon transitions reset the watcher
# ---------------------------------------------------------------------------


def _cloud(text: str, run: str = "run-1") -> dict:
    return {"kind": "text", "text": text, "run": run}


def _daemon_payload(text: str, epoch: int, run: str = "daemon:r1") -> dict:
    return {"kind": "text", "text": text, "run": run, "epoch": epoch}


def test_the_source_fence_resets_on_every_transition_in_both_directions():
    from src.server.app.ticket_activity_stream import OutputSourceFence

    fence = OutputSourceFence("", "")  # a fresh watcher has seen nothing
    assert fence.check(_cloud("password ")) is None
    reset = fence.check(_daemon_payload("=ordinary-value\n", 4))
    assert reset == {"kind": "output_reset", "epoch": 4, "reason": "new_epoch", "run": "daemon:r1"}
    assert fence.check(_daemon_payload("more\n", 4)) is None
    back = fence.check(_cloud("later", "run-2"))
    assert back["kind"] == "output_reset" and back["epoch"] == 0
    # A new cloud run id is a new generation too.
    again = fence.check(_cloud("next", "run-3"))
    assert again is not None and again["reason"] == "new_epoch"


def test_the_source_fence_does_not_double_a_reset_the_store_already_sent():
    from src.server.app.ticket_activity_stream import OutputSourceFence

    fence = OutputSourceFence("", "")
    fence.check(_daemon_payload("a\n", 1))
    explicit = {"kind": "output_reset", "epoch": 2, "reason": "new_epoch", "run": "daemon"}
    assert fence.check(explicit) is None  # the explicit reset is the boundary
    assert fence.check(_daemon_payload("b\n", 2)) is None


@pytest.mark.parametrize(
    "run_id,daemon,first,expect_reset",
    [
        ("run-1", "", _daemon_payload("x\n", 2), True),  # watcher saw a cloud run
        ("", "1.5", _cloud("x"), True),  # watcher saw a daemon epoch
        ("", "1.5", _daemon_payload("x\n", 1), False),  # same epoch: a plain resume
        ("", "1.5", _daemon_payload("x\n", 2), True),
        ("run-1", "", _cloud("x"), False),
        ("run-1", "1.5", _cloud("x"), True),  # ambiguous cursor: reset to be safe
        ("", "", _daemon_payload("x\n", 2), False),  # fresh watcher
        ("", "garbage", _daemon_payload("x\n", 2), True),
    ],
)
def test_the_source_fence_starts_from_what_the_resume_cursor_names(
    run_id, daemon, first, expect_reset
):
    from src.server.app.ticket_activity_stream import OutputSourceFence

    assert (OutputSourceFence(run_id, daemon).check(first) is not None) is expect_reset


def test_payloads_that_name_no_run_do_not_move_the_source():
    from src.server.app.ticket_activity_stream import OutputSourceFence

    fence = OutputSourceFence("run-1", "")
    assert fence.check({"kind": "gap", "text": "x", "run": ""}) is None
    assert fence.check(_cloud("same run")) is None


@pytest.mark.asyncio
async def test_a_watcher_that_saw_a_cloud_run_is_reset_before_daemon_output(client):
    """Resume cursor names a cloud run; the Ticket is now owned by a daemon."""
    completed = _daemon_ticket(status="completed", summary="done")

    async def fake_read(ticket_id: str, cursor, *, count=64):
        if cursor is None:
            return _page(
                3, [_entry(3, 1, {"kind": "text", "text": "=ordinary\n", "run_id": "r9"})]
            )
        return _page(3, [], cursor=cursor)

    resume = f"created:{TICKET_ID}~run-old~7~"
    patches = _activity_patches(tickets=[completed])
    _enter(patches)
    try:
        with patch(f"{MODULE}.daemon_output_store.read_output", new=fake_read):
            frames = await _collect(client, headers={"Last-Event-ID": resume})
    finally:
        _exit(patches)

    kinds = [d["kind"] for _, e, d in frames if e == "output"]
    assert kinds == ["output_reset", "text"]


@pytest.mark.asyncio
async def test_a_reconnect_after_only_a_status_frame_still_resets_across_a_source_change(
    client,
):
    """Reviewer repro: resume names a cloud run, the Ticket now belongs to a
    daemon. The watcher receives only a status frame and drops; its next cursor
    must still remember the cloud source so the daemon text is preceded by a
    reset."""
    running = _daemon_ticket(status="running")

    async def fake_read(ticket_id: str, cursor, *, count=64):
        if cursor is None:
            return _page(
                3, [_entry(3, 1, {"kind": "text", "text": "=ordinary\n", "run_id": "r9"})]
            )
        return _page(3, [], cursor=cursor)

    first_resume = f"created:{TICKET_ID}~run-old~7~"
    done = _daemon_ticket(status="completed", summary="done")
    quiet = AsyncMock(return_value=_page(3, [], cursor=""))
    patches = _activity_patches(tickets=[running, done])
    _enter(patches)
    try:
        # The first attachment is cut after its status frame, before any daemon
        # output could be read.
        with patch(f"{MODULE}.daemon_output_store.read_output", new=quiet):
            first = await _collect(
                client,
                headers={"Last-Event-ID": first_resume},
                stop_events=("status",),
                timeout=8.0,
            )
        status_id = [cid for cid, event, _ in first if event == "status"][-1]
        assert status_id and "rrun-old" in status_id
    finally:
        _exit(patches)
    patches = _activity_patches(tickets=[done])
    _enter(patches)
    try:
        with patch(f"{MODULE}.daemon_output_store.read_output", new=fake_read):
            second = await _collect(
                client, headers={"Last-Event-ID": status_id}, timeout=5.0
            )
    finally:
        _exit(patches)

    outputs = [d["kind"] for _, e, d in second if e == "output"]
    assert outputs[:2] == ["output_reset", "text"]


@pytest.mark.asyncio
async def test_a_synthetic_reset_carries_no_id_so_an_interrupted_watcher_rereads_the_payload(
    client,
):
    completed = _daemon_ticket(status="completed", summary="done")

    async def fake_read(ticket_id: str, cursor, *, count=64):
        if cursor is None:
            return _page(
                3, [_entry(3, 1, {"kind": "text", "text": "=ordinary\n", "run_id": "r9"})]
            )
        return _page(3, [], cursor=cursor)

    resume = f"created:{TICKET_ID}~run-old~7~"
    patches = _activity_patches(tickets=[completed])
    _enter(patches)
    try:
        with patch(f"{MODULE}.daemon_output_store.read_output", new=fake_read):
            frames = await _collect(client, headers={"Last-Event-ID": resume})
    finally:
        _exit(patches)

    outputs = [(cid, d) for cid, e, d in frames if e == "output"]
    assert [d["kind"] for _, d in outputs] == ["output_reset", "text"]
    assert outputs[0][0] is None  # no id: the client's last id stays before the text
    assert outputs[1][0] and outputs[1][0].endswith("~3.1~d3")


def test_cursor_source_round_trip_and_old_cursors():
    from src.server.app.ticket_activity_stream import (
        OutputSourceFence,
        cursor_output_source,
        ticket_activity_cursor,
    )

    cursor = ticket_activity_cursor("a", "", 0, "", "rrun-old")
    assert cursor_output_source(cursor) == "rrun-old"
    assert cursor_output_source("a~run~3~1.2") == ""  # a pre-source cursor
    assert OutputSourceFence.decode("d12") == ("daemon", 12)
    assert OutputSourceFence.decode("rabc") == ("run", "abc")
    assert OutputSourceFence.decode("garbage") is None
    fence = OutputSourceFence("", "", "d5")
    assert fence.check({"kind": "text", "text": "x", "run": "daemon:r", "epoch": 5}) is None
    assert fence.check({"kind": "text", "text": "x", "run": "daemon:r", "epoch": 6}) is not None
    assert OutputSourceFence("run-1", "", "").encoded("run-1") == ""


def test_a_malformed_source_in_a_cursor_never_raises():
    from src.server.app.ticket_activity_stream import OutputSourceFence

    for raw in ("d" + "9" * 5000, "d" + "9" * 40, "dx", "d", "r", "", "q1"):
        OutputSourceFence("", "", raw).check(
            {"kind": "text", "text": "x", "run": "daemon:r", "epoch": 1}
        )  # must not raise
    assert OutputSourceFence.decode("d" + "9" * 5000) is None
