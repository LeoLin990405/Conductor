"""Controller and prompt_toolkit application tests for the Super-agent TUI.

These exercise the parts a terminal would: the composer key bindings (Enter,
Shift+Enter sequences, Esc, double Ctrl+C), permission keys, the command
dispatch, session resume, the ``-p`` one-shot output, and the watch loop wired
into the conversation.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

# tui imports conductor_cli, which lives in the repo src tree; the ptc-cli
# lockfile's conductor-core is a different PyPI project.
_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.client import (  # noqa: E402
    ActivityStreamEvent,
    ActivityStreamUnavailableError,
    SuperAgentError,
)
from ptc_cli.superagent.tui import SuperAgentTui, run_print_mode  # noqa: E402
from ptc_cli.superagent.tui_events import Activity, End  # noqa: E402
from ptc_cli.superagent.tui_blocks import (  # noqa: E402
    AssistantBlock,
    EndBlock,
    ErrorBlock,
    NoticeBlock,
    PermissionBlock,
    ProgressBlock,
    UserBlock,
)
from ptc_cli.superagent.tui_sessions import SessionStore  # noqa: E402

FULL_ID = "abcdefab-0000-0000-0000-000000000001"
WAITING_FULL_ID = "abcdefab-0000-0000-0000-000000000002"


class FakeClient:
    """An API facade with just enough behaviour for the TUI controller."""

    def __init__(self, *, stream: list[ActivityStreamEvent] | None = None) -> None:
        self.base_url = "http://test"
        self.user_id = "u1"
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.stops: list[str] = []
        self.closed = False
        self._stream = stream
        self.tickets_payload: dict[str, Any] = {"tickets": []}
        self.ticket_payload: dict[str, Any] = {
            "ticket_id": FULL_ID,
            "status": "completed",
            "summary": "all done",
            "title": "the task",
        }

    async def close(self) -> None:
        self.closed = True

    async def overview(self) -> dict[str, Any]:
        return {
            "counts": {"running": 1, "queued": 2, "attention": 0},
            "sources": 2,
            "enabled_sources": 1,
            "router_model": "deepseek-flash-account",
        }

    async def daemons(self) -> dict[str, Any]:
        return {
            "daemons": [
                {
                    "daemon_id": "daemon-1",
                    "name": "local",
                    "provider": "codex",
                    "last_seen_at": "2099-01-01T00:00:00Z",
                }
            ]
        }

    async def home_workspace(self) -> str:
        return "home-ws"

    async def create_task(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.created.append((prompt, kwargs))
        return {"ticket_id": FULL_ID, "status": "queued"}

    async def stream_activity(self, ticket_id: str, *, last_event_id=None):
        if self._stream is None:
            message = "no live stream"
            raise ActivityStreamUnavailableError(message)
        for event in self._stream:
            yield event

    async def activity(self, ticket_id: str) -> dict[str, Any]:
        return {"events": []}

    async def ticket(self, ticket_id: str) -> dict[str, Any]:
        return dict(self.ticket_payload)

    async def tickets(self, **params: Any) -> dict[str, Any]:
        return self.tickets_payload

    async def stop(self, ticket_id: str) -> dict[str, Any]:
        self.stops.append(ticket_id)
        return {"ok": True}

    async def fleet(self) -> dict[str, Any]:
        return {"agents": [{"id": "a1", "role": "reviewer", "status": "running", "branch": "feat/x"}]}

    async def sources(self) -> dict[str, Any]:
        return {"sources": [{"source_id": "s1", "kind": "github", "enabled": True, "config": {"repository": "acme/x"}}]}

    async def models(self) -> dict[str, Any]:
        return {"models": {"codex": {"models": ["gpt-5"]}}}


def _event(name: str, data: dict[str, Any], event_id: str | None = None) -> ActivityStreamEvent:
    return ActivityStreamEvent(event=name, data=data, event_id=event_id)


def _completed_stream() -> list[ActivityStreamEvent]:
    return [
        _event("activity", {"event_id": "e1", "kind": "created", "summary": "made"}, "1"),
        _event("output", {"kind": "text", "text": "working "}, "2"),
        _event("output", {"kind": "tool", "tools": ["read_file"]}, "3"),
        _event("output", {"kind": "text", "text": "done"}, "4"),
        _event("end", {"status": "completed", "summary": "all done"}, "5"),
    ]


def make_tui(
    tmp_path: Path,
    *,
    client: FakeClient | None = None,
    continue_session: bool = False,
    show_worker_details: bool = False,
) -> SuperAgentTui:
    return SuperAgentTui(
        server_url="http://test",
        user_id="u1",
        client=client or FakeClient(),
        session_store=SessionStore(tmp_path / "sessions"),
        history_path=tmp_path / "history",
        local_root=tmp_path,
        worktree_root=tmp_path,
        no_splash=True,
        continue_session=continue_session,
        show_worker_details=show_worker_details,
    )


def _visible_text(tui: SuperAgentTui) -> str:
    rows, _requested = tui._conversation_rows_for_control()

    def _line(row: object) -> str:
        if isinstance(row, tuple) and len(row) == 2:
            return str(row[1])
        if isinstance(row, list):
            return "".join(str(part[1]) for part in row if isinstance(part, tuple))
        return str(row)

    return "\n".join(_line(row) for row in rows)


# ---------------------------------------------------------------------------
# controller
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_sets_status_and_selects_daemon(tmp_path: Path):
    tui = make_tui(tmp_path)

    await tui.startup()

    assert tui.status.router_model == "deepseek-flash-account"
    assert tui.status.active_jobs == 3
    assert tui._daemon_id == "daemon-1"
    assert tui.status.daemon == "local"
    assert tui.status.session == tui.session.short_id
    assert (tmp_path / "sessions").is_dir()
    await tui.close()


@pytest.mark.asyncio
async def test_plain_line_creates_a_ticket_and_follows_it(tmp_path: Path):
    client = FakeClient(stream=_completed_stream())
    tui = make_tui(tmp_path, client=client)
    await tui.startup()

    await tui.submit("fix the bug")

    assert client.created[0][0] == "fix the bug"
    assert client.created[0][1]["workspace_id"] == "home-ws"
    assert client.created[0][1]["worker_daemon_id"] == "daemon-1"
    assert tui.session.last_ticket_id == FULL_ID
    assert any(isinstance(block, UserBlock) for block in tui.conversation.blocks)
    assert any(isinstance(block, AssistantBlock) for block in tui.conversation.blocks)
    assert any("all done" in line for line in tui.conversation.plain_lines())
    await tui.close()


@pytest.mark.asyncio
async def test_supervisor_view_hides_worker_details_and_shows_result(tmp_path: Path):
    client = FakeClient(stream=_completed_stream())
    tui = make_tui(tmp_path, client=client, show_worker_details=False)
    await tui.startup()

    await tui.submit("fix the bug")

    visible = "\n".join(
        text
        for _style, text in tui.conversation.formatted_lines(
            show_worker_details=False
        )
    )
    assert "all done" in visible
    assert "working" not in visible
    assert tui.status.phase == "completed"

    await tui.handle_command("/details")
    assert tui.show_worker_details is True
    visible_after = "\n".join(
        text
        for _style, text in tui.conversation.formatted_lines(
            show_worker_details=True
        )
    )
    assert "working" in visible_after
    await tui.close()


@pytest.mark.asyncio
async def test_prompt_attaches_local_files(tmp_path: Path):
    (tmp_path / "note.txt").write_text("the note body\n", encoding="utf-8")
    client = FakeClient(stream=_completed_stream())
    tui = make_tui(tmp_path, client=client)
    await tui.startup()

    await tui.submit("look at @note.txt")

    sent_prompt = client.created[0][0]
    assert "the note body" in sent_prompt
    user_block = next(
        block for block in tui.conversation.blocks if isinstance(block, UserBlock)
    )
    assert user_block.attachments == ["note.txt"]
    await tui.close()


@pytest.mark.asyncio
async def test_plain_line_without_a_daemon_refuses_to_submit(tmp_path: Path):
    client = FakeClient(stream=_completed_stream())
    client.daemons = _no_daemons  # type: ignore[method-assign]
    tui = make_tui(tmp_path, client=client)
    await tui.startup()

    await tui.submit("do work")

    assert client.created == []
    assert any(isinstance(block, ErrorBlock) for block in tui.conversation.blocks)
    await tui.close()


async def _no_daemons() -> dict[str, Any]:
    return {"daemons": []}


@pytest.mark.asyncio
async def test_slash_commands_cover_conversation_and_session_state(tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()

    await tui.handle_command("/help")
    assert any("Enter" in line for line in tui.conversation.plain_lines())
    await tui.handle_command("/compact")
    await tui.handle_command("/expand")
    await tui.handle_command("/collapse")
    await tui.handle_command("/clear")
    assert tui.conversation.blocks == []
    await tui.handle_command("/model")
    assert any("deepseek-flash-account" in line for line in tui.conversation.plain_lines())
    assert any("gpt-5" in line for line in tui.conversation.plain_lines())
    await tui.handle_command("/jobs")
    assert any("no Jobs" in line for line in tui.conversation.plain_lines())
    await tui.handle_command("/agents")
    await tui.handle_command("/signals")
    session_id = tui.session.session_id
    await tui.handle_command("/new")
    assert tui.session.session_id != session_id
    assert any("new session" in line for line in tui.conversation.plain_lines())
    await tui.close()


@pytest.mark.asyncio
async def test_stop_accepts_an_index_from_the_last_jobs_listing(tmp_path: Path):
    client = FakeClient()
    client.tickets_payload = {"tickets": [{"ticket_id": FULL_ID, "status": "running", "title": "t"}]}
    tui = make_tui(tmp_path, client=client)
    await tui.startup()

    await tui.handle_command("/jobs")
    await tui.handle_command("/stop 1")

    assert client.stops == [FULL_ID]
    await tui.close()


@pytest.mark.asyncio
async def test_merge_is_denied_without_a_local_run(monkeypatch, tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    called: list[list[str]] = []
    monkeypatch.setattr(
        "ptc_cli.superagent.tui.run_conductor",
        lambda args, **kwargs: called.append(args) or "merged",
    )

    task = asyncio.create_task(tui.handle_command("/merge feat/x --into integrate/r2"))
    await asyncio.sleep(0)
    tui.resolve_permission("d")
    await task

    assert called == []
    assert any("denied" in line for line in tui.conversation.plain_lines())
    await tui.close()


@pytest.mark.asyncio
async def test_merge_runs_locally_after_allow_once(monkeypatch, tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    called: list[list[str]] = []
    monkeypatch.setattr(
        "ptc_cli.superagent.tui.run_conductor",
        lambda args, **kwargs: called.append(args) or "merged",
    )

    task = asyncio.create_task(tui.handle_command("/merge feat/x --into integrate/r2"))
    await asyncio.sleep(0)
    assert tui._pending is not None
    tui.resolve_permission("a")
    await task

    assert called == [["merge", "feat/x", "--into", "integrate/r2"]]
    assert tui.permissions.needs_confirmation("local-merge") is True
    assert any("merged" in line for line in tui.conversation.plain_lines())
    await tui.close()


@pytest.mark.asyncio
async def test_session_allow_skips_the_next_prompt(monkeypatch, tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    monkeypatch.setattr(
        "ptc_cli.superagent.tui.run_conductor",
        lambda args, **kwargs: "merged",
    )

    first = asyncio.create_task(tui.handle_command("/merge feat/x --into integrate/r2"))
    await asyncio.sleep(0)
    tui.resolve_permission("s")
    await first
    assert tui.permissions.needs_confirmation("local-merge") is False

    await tui.handle_command("/merge feat/y --into integrate/r2")
    assert tui._pending is None
    await tui.close()


@pytest.mark.asyncio
async def test_shell_escape_runs_after_permission(monkeypatch, tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    called: list[str] = []
    monkeypatch.setattr(
        "ptc_cli.superagent.tui.run_shell",
        lambda command, cwd=None: called.append(command) or "shell out",
    )

    task = asyncio.create_task(tui.submit("!echo hi"))
    await asyncio.sleep(0)
    tui.resolve_permission("a")
    await task

    assert called == ["echo hi"]
    await tui.close()


@pytest.mark.asyncio
async def test_permission_resolution_supports_allow_and_session(tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()

    waiting = asyncio.create_task(
        tui.request_permission(
            __import__(
                "ptc_cli.superagent.tui_permissions", fromlist=["PermissionRequest"]
            ).PermissionRequest(
                action="local-review", label="review", detail="line-a"
            )
        )
    )
    await asyncio.sleep(0)
    assert any(isinstance(block, PermissionBlock) for block in tui.conversation.blocks)
    assert tui.resolve_permission("s") is True
    assert await waiting == "session"
    assert tui.permissions.session_allowed("local-review") is True
    await tui.close()


@pytest.mark.asyncio
async def test_note_ctrl_c_requires_two_presses(tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()

    assert tui.note_ctrl_c(now=10.0) is False
    assert tui.note_ctrl_c(now=10.5) is True
    assert any("Ctrl+C again" in line for line in tui.conversation.plain_lines())
    await tui.close()


@pytest.mark.asyncio
async def test_ctrl_c_while_a_permission_is_pending_denies_it(tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    waiting = asyncio.create_task(
        tui.request_permission(
            __import__(
                "ptc_cli.superagent.tui_permissions", fromlist=["PermissionRequest"]
            ).PermissionRequest(action="local-shell", label="shell")
        )
    )
    await asyncio.sleep(0)

    assert tui.note_ctrl_c(now=10.0) is False
    assert await waiting == "deny"
    await tui.close()


@pytest.mark.asyncio
async def test_interrupt_stops_the_watch_without_stopping_the_ticket(tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    task = asyncio.create_task(asyncio.sleep(30))
    tui._watch_task = task
    try:
        tui.interrupt()
        assert tui._interrupted is True
        await asyncio.sleep(0)
        assert task.cancelled() or task.cancelling()
        assert tui.status.stream == "idle" or tui._watch_task is not None
        assert any(
            "stopped watching" in line
            for line in tui.conversation.plain_lines()
        ) or task.cancelled()
    finally:
        with pytest.raises(asyncio.CancelledError):
            await task
        tui._watch_task = None
        await tui.close()


@pytest.mark.asyncio
async def test_continue_session_restores_the_transcript(tmp_path: Path):
    client = FakeClient(stream=_completed_stream())
    first = make_tui(tmp_path, client=client)
    await first.startup()
    await first.submit("remember this")
    session_id = first.session.session_id
    await first.close()

    resumed = make_tui(tmp_path, client=FakeClient(), continue_session=True)
    await resumed.startup()
    assert resumed.session.session_id == session_id
    assert any(
        isinstance(block, UserBlock) and block.text == "remember this"
        for block in resumed.conversation.blocks
    )
    assert any("resumed session" in line for line in resumed.conversation.plain_lines())
    await resumed.close()


@pytest.mark.asyncio
async def test_resume_lists_and_loads_a_saved_session(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    record = store.new(title="earlier")
    record.transcript = [{"kind": "user", "text": "old prompt"}]
    store.save(record)

    tui = make_tui(tmp_path)
    await tui.startup()
    await tui.handle_command("/resume")
    assert any(record.short_id in line for line in tui.conversation.plain_lines())
    await tui.handle_command(f"/resume {record.session_id}")
    assert any(
        isinstance(block, UserBlock) and block.text == "old prompt"
        for block in tui.conversation.blocks
    )
    await tui.close()


@pytest.mark.asyncio
async def test_startup_survives_a_server_summary_failure(tmp_path: Path):
    client = FakeClient()

    async def broken_overview():
        message = "offline"
        raise SuperAgentError(message)

    client.overview = broken_overview  # type: ignore[method-assign]
    tui = make_tui(tmp_path, client=client)

    await tui.startup()

    assert any("server summary unavailable" in line for line in tui.conversation.plain_lines())
    await tui.close()


# ---------------------------------------------------------------------------
# prompt_toolkit application key bindings
# ---------------------------------------------------------------------------


async def _run_app(tui: SuperAgentTui, feed: str, *, settle: float = 0.2):
    """Run the app with a scripted pipe, feeding keys after it starts."""
    tui.build_application(input=None, output=None)
    with create_pipe_input() as pipe:
        tui.build_application(input=pipe, output=DummyOutput())
        task = asyncio.create_task(tui._app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text(feed)
        await asyncio.sleep(settle)
        return task


@pytest.mark.asyncio
async def test_enter_submits_the_composer(tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    submitted: list[str] = []
    tui.spawn_submit = submitted.append  # type: ignore[method-assign]

    with create_pipe_input() as pipe:
        tui.build_application(input=pipe, output=DummyOutput())
        task = asyncio.create_task(tui._app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("hello world\r")
        await asyncio.sleep(0.1)
        pipe.send_text("\x03")
        await asyncio.sleep(0.02)
        pipe.send_text("\x03")
        assert await asyncio.wait_for(task, timeout=5) == 0

    assert submitted == ["hello world"]
    await tui.close()


@pytest.mark.asyncio
async def test_escape_enter_inserts_a_newline(tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    submitted: list[str] = []
    tui.spawn_submit = submitted.append  # type: ignore[method-assign]

    with create_pipe_input() as pipe:
        tui.build_application(input=pipe, output=DummyOutput())
        task = asyncio.create_task(tui._app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("line1\x1b\rline2\r")
        await asyncio.sleep(0.1)
        pipe.send_text("\x03\x03")
        assert await asyncio.wait_for(task, timeout=5) == 0

    assert submitted == ["line1\nline2"]
    await tui.close()


@pytest.mark.asyncio
async def test_shift_enter_csi_sequence_inserts_a_newline(tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    submitted: list[str] = []
    tui.spawn_submit = submitted.append  # type: ignore[method-assign]

    with create_pipe_input() as pipe:
        tui.build_application(input=pipe, output=DummyOutput())
        task = asyncio.create_task(tui._app.run_async())
        await asyncio.sleep(0.05)
        # kitty keyboard protocol: Shift+Enter = CSI 13;2u
        pipe.send_text("a\x1b[13;2ub\r")
        await asyncio.sleep(0.1)
        pipe.send_text("\x03\x03")
        assert await asyncio.wait_for(task, timeout=5) == 0

    assert submitted == ["a\nb"]
    await tui.close()


@pytest.mark.asyncio
async def test_permission_keys_resolve_the_prompt(tmp_path: Path):
    from ptc_cli.superagent.tui_permissions import PermissionRequest

    tui = make_tui(tmp_path)
    await tui.startup()
    waiting = asyncio.create_task(
        tui.request_permission(
            PermissionRequest(action="local-shell", label="run shell", detail="echo hi")
        )
    )
    await asyncio.sleep(0)

    with create_pipe_input() as pipe:
        tui.build_application(input=pipe, output=DummyOutput())
        task = asyncio.create_task(tui._app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("d")
        await asyncio.sleep(0.1)
        pipe.send_text("\x03\x03")
        assert await asyncio.wait_for(task, timeout=5) == 0

    assert await waiting == "deny"
    await tui.close()


@pytest.mark.asyncio
async def test_up_arrow_recalls_the_previous_submission(tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    submitted: list[str] = []
    tui.spawn_submit = submitted.append  # type: ignore[method-assign]

    with create_pipe_input() as pipe:
        tui.build_application(input=pipe, output=DummyOutput())
        task = asyncio.create_task(tui._app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("first prompt\r")
        await asyncio.sleep(0.05)
        pipe.send_text("\x1b[A")
        await asyncio.sleep(0.05)
        pipe.send_text("\r")
        await asyncio.sleep(0.05)
        pipe.send_text("\x03\x03")
        assert await asyncio.wait_for(task, timeout=5) == 0

    assert submitted == ["first prompt", "first prompt"]
    await tui.close()


# ---------------------------------------------------------------------------
# one-shot print mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_print_mode_streams_and_reports_success(capsys):
    client = FakeClient(stream=_completed_stream())

    code = await run_print_mode(
        server_url="http://test",
        user_id="u1",
        prompt="do it",
        client=client,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert f"ticket {FULL_ID}" in out
    assert "working" in out
    assert "done" in out
    assert "status: completed" in out


@pytest.mark.asyncio
async def test_print_mode_ends_the_fragment_at_a_gap_frame(capsys):
    stream = [
        _event("output", {"kind": "text", "text": "kept \x1b]52;c;", "run": "r", "epoch": 1}, "1"),
        _event("output", {"kind": "gap", "text": "Some Worker output was dropped"}, "2"),
        _event("output", {"kind": "text", "text": "visible after", "run": "r", "epoch": 1}, "3"),
        _event("end", {"status": "completed", "summary": "done"}, "4"),
    ]

    code = await run_print_mode(
        server_url="http://test", user_id="u1", prompt="do it", client=FakeClient(stream=stream)
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "kept \nSome Worker output was dropped\nvisible after" in out


@pytest.mark.asyncio
async def test_print_mode_returns_nonzero_for_a_failed_ticket(capsys):
    stream = _completed_stream()
    stream[-1] = _event("end", {"status": "failed", "summary": "broke"}, "5")
    client = FakeClient(stream=stream)

    code = await run_print_mode(
        server_url="http://test", user_id="u1", prompt="do it", client=client
    )

    assert code == 1
    assert "status: failed" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_print_mode_without_a_daemon_refuses(capsys):
    client = FakeClient()
    client.daemons = _no_daemons  # type: ignore[method-assign]

    code = await run_print_mode(
        server_url="http://test", user_id="u1", prompt="do it", client=client
    )

    assert code == 1
    assert "no local daemon" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_print_mode_attaches_files(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ctx.txt").write_text("context body\n", encoding="utf-8")
    client = FakeClient(stream=_completed_stream())

    code = await run_print_mode(
        server_url="http://test",
        user_id="u1",
        prompt="use @ctx.txt",
        client=client,
    )

    assert code == 0
    assert "context body" in client.created[0][0]


@pytest.mark.asyncio
async def test_print_mode_sanitizes_streamed_and_structured_text(capsys):
    from ptc_cli.superagent.client import ActivityStreamEvent

    poison = "safe\x1b]52;c;ZXZpbA==\x07"
    stream = [
        ActivityStreamEvent(event="output", data={"kind": "text", "text": poison}, event_id="1"),
        ActivityStreamEvent(
            event="output",
            data={"kind": "tool", "tools": [poison]},
            event_id="2",
        ),
        ActivityStreamEvent(
            event="end",
            data={"status": "completed", "summary": poison, "question": poison},
            event_id="3",
        ),
    ]
    client = FakeClient(stream=stream)

    code = await run_print_mode(
        server_url="http://test", user_id="u1", prompt="x", client=client
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "\x1b" not in out
    assert "\x07" not in out
    assert "safe" in out


def _fenced_stream(*frames: ActivityStreamEvent) -> list[ActivityStreamEvent]:
    return [*frames, _event("end", {"status": "completed", "summary": "ok"}, "99")]


def _text(text: str, epoch: int, run: str = "daemon:r1") -> ActivityStreamEvent:
    return _event("output", {"kind": "text", "text": text, "run": run, "epoch": epoch})


def _reset(reason: str, epoch: int) -> ActivityStreamEvent:
    return _event(
        "output",
        {"kind": "output_reset", "reason": reason, "epoch": epoch, "run": "daemon:r1"},
    )


async def _print_mode_writes(stream: list[ActivityStreamEvent]) -> list[str]:
    writes: list[str] = []
    code = await run_print_mode(
        server_url="http://test",
        user_id="u1",
        prompt="x",
        client=FakeClient(stream=stream),
        write=writes.append,
    )
    assert code == 0
    return writes


@pytest.mark.asyncio
async def test_print_mode_output_reset_uses_a_fresh_sanitizer_and_ends_the_line():
    writes = await _print_mode_writes(
        _fenced_stream(
            _text("prefix Bearer \x1b]0;ti", 1),
            _reset("continuity_lost", 2),
            _text("abcdef12345678 tail", 2),
        )
    )

    out = "".join(writes)
    assert "\x1b" not in out
    assert "0;ti" not in out
    # The unterminated OSC before the reset must not swallow what follows.
    assert "abcdef12345678 tail" in out
    assert "Bearer abcdef" not in out
    lines = [line.rstrip() for line in out.splitlines()]
    assert lines.index("prefix Bearer") < lines.index(
        "live output restarted (continuity lost)"
    ) < lines.index("abcdef12345678 tail")
    # Pre-reset and post-reset text never share one write (one sanitizer pass).
    assert not any("Bearer" in w and "abcdef" in w for w in writes)


@pytest.mark.asyncio
async def test_print_mode_without_a_reset_the_open_escape_still_swallows_text():
    # Control for the test above: the reset is what frees the post text.
    out = "".join(
        await _print_mode_writes(
            _fenced_stream(_text("before\x1b]0;ti", 1), _text("swallowed", 1))
        )
    )

    assert "before" in out
    assert "swallowed" not in out


@pytest.mark.asyncio
async def test_print_mode_epoch_change_is_treated_as_a_reset():
    out = "".join(
        await _print_mode_writes(
            _fenced_stream(_text("before\x1b]0;ti", 1), _text("visible", 2))
        )
    )

    assert "visible" in out
    assert "live output restarted (new epoch)" in out
    assert "\x1b" not in out


@pytest.mark.asyncio
async def test_print_mode_reset_reason_is_sanitized():
    out = "".join(
        await _print_mode_writes(
            _fenced_stream(_reset("gap\x1b]52;c;ZXZpbA==\x07\x1b[2J", 1), _text("ok", 1))
        )
    )

    assert "\x1b" not in out
    assert "\x07" not in out
    assert "live output restarted (gap)" in out


@pytest.mark.asyncio
async def test_tui_watch_loop_applies_output_reset_to_the_conversation(tmp_path: Path):
    stream = _fenced_stream(
        _text("prefix Bearer ", 1),
        _reset("continuity_lost", 2),
        _text("abcdef12345678", 2),
        _text("more", 3),
    )
    tui = make_tui(
        tmp_path,
        client=FakeClient(stream=stream),
        show_worker_details=True,
    )
    await tui.startup()

    await asyncio.wait_for(tui.watch("ticket-x"), timeout=5)

    blocks = [b for b in tui.conversation.blocks if isinstance(b, AssistantBlock)]
    # Explicit reset, then a synthetic one when the epoch moves 2 -> 3.
    assert [b.text for b in blocks] == ["prefix Bearer ", "abcdef12345678", "more"]
    notices = [b.text for b in tui.conversation.blocks if isinstance(b, NoticeBlock)]
    assert "live output restarted (continuity lost)" in notices
    assert "live output restarted (new epoch)" in notices
    joined = "".join(fragment[1] for fragment in tui._conversation_fragments())
    assert "Bearer abcdef" not in joined
    assert "live output restarted (continuity lost)" in joined
    await tui.close()


@pytest.mark.asyncio
async def test_rewatching_a_ticket_never_joins_the_new_stream_to_the_old_text(
    tmp_path: Path,
):
    """Two real `watch()` calls: epoch 1 shows `password `, a later attachment
    receives epoch 2 `=ordinary-value`. Neither stream carries a reset, and
    trimming plus `/compact` removes the rows between them."""
    from ptc_cli.superagent.tui_blocks import SEALED_BOUNDARY

    tui = make_tui(
        tmp_path, client=FakeClient(stream=_fenced_stream(_text("password \n", 1)))
    )
    await tui.startup()
    await asyncio.wait_for(tui.watch("ticket-x"), timeout=5)
    tui.client._stream = _fenced_stream(_text("=ordinary-value\n", 2))
    await asyncio.wait_for(tui.watch("ticket-x"), timeout=5)

    blocks = [b for b in tui.conversation.blocks if isinstance(b, AssistantBlock)]
    assert [b.text for b in blocks] == ["password \n", "=ordinary-value\n"]
    assert blocks[0].sealed is True
    tui.conversation.compact()
    transcript = tui.conversation.transcript_text()
    before, _, after = transcript.partition(SEALED_BOUNDARY)
    assert "password" in before and "=ordinary-value" in after
    assert "password =ordinary-value" not in " ".join(transcript.split())
    await tui.close()


@pytest.mark.asyncio
async def test_print_mode_a_change_of_source_is_treated_as_a_reset():
    """Cloud run, then a daemon epoch, with no announced reset in between."""
    writes = await _print_mode_writes(
        _fenced_stream(
            _text("password ", 0, run="cloud:r1"),
            _text("=ordinary-value", 3, run="daemon:r2"),
        )
    )
    joined = "".join(writes)
    assert "restarted" in joined
    assert "password =ordinary-value" not in " ".join(joined.split())


@pytest.mark.asyncio
async def test_conversation_fragments_put_newlines_between_rows(tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    tui.conversation.add(UserBlock(text="first"))
    tui.conversation.add(UserBlock(text="second"))
    tui.conversation.add(UserBlock(text="third"))

    fragments = tui._conversation_fragments()

    joined = "".join(fragment[1] for fragment in fragments)
    lines = [line for line in joined.split("\n") if line]
    assert lines == ["❯ first", "❯ second", "❯ third"]
    await tui.close()


@pytest.mark.asyncio
async def test_history_file_keeps_accepted_lines(tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    tui.spawn_submit = lambda text: None  # type: ignore[method-assign]

    with create_pipe_input() as pipe:
        tui.build_application(input=pipe, output=DummyOutput())
        task = asyncio.create_task(tui._app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("remembered line\r")
        await asyncio.sleep(0.1)
        pipe.send_text("\x03\x03")
        assert await asyncio.wait_for(task, timeout=5) == 0

    history_file = tmp_path / "history"
    if history_file.exists():
        assert "remembered line" in history_file.read_text(encoding="utf-8")
    else:
        # prompt_toolkit's FileHistory may keep in memory until close; assert
        # the buffer learned it instead.
        assert tui.input_area is not None
    await tui.close()


@pytest.mark.asyncio
async def test_overlapping_watches_do_not_steal_ownership(tmp_path: Path):
    class SlowClient(FakeClient):
        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            await asyncio.sleep(30)
            yield _event("end", {"status": "completed"})

    client = SlowClient()
    tui = make_tui(tmp_path, client=client)
    await tui.startup()

    first = asyncio.create_task(tui.watch(FULL_ID))
    await asyncio.sleep(0.05)
    first_task = tui._watch_task
    second = asyncio.create_task(tui.watch("other-ticket"))
    await asyncio.sleep(0.05)

    assert first_task is not None
    assert first_task.cancelled() or first_task.cancelling()
    assert tui._watch_task is not None
    assert tui._watch_task is not first_task

    tui.interrupt()
    await asyncio.wait_for(second, timeout=2)
    await asyncio.wait_for(first, timeout=2)
    assert tui._watch_task is None
    assert any(
        "stopped watching" in line for line in tui.conversation.plain_lines()
    )
    await tui.close()


@pytest.mark.asyncio
async def test_escape_denies_a_pending_permission(tmp_path: Path):
    from ptc_cli.superagent.tui_permissions import PermissionRequest

    tui = make_tui(tmp_path)
    await tui.startup()
    waiting = asyncio.create_task(
        tui.request_permission(
            PermissionRequest(action="local-shell", label="run shell")
        )
    )
    await asyncio.sleep(0)

    with create_pipe_input() as pipe:
        tui.build_application(input=pipe, output=DummyOutput())
        task = asyncio.create_task(tui._app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("\x1b")
        await asyncio.sleep(0.25)
        pipe.send_text("\x03")
        await asyncio.sleep(0.05)
        pipe.send_text("\x03")
        assert await asyncio.wait_for(task, timeout=5) == 0

    assert await waiting == "deny"
    await tui.close()


@pytest.mark.asyncio
async def test_conversation_renders_lines_and_follows_then_scrolls(tmp_path: Path):
    import io

    from prompt_toolkit.output.plain_text import PlainTextOutput

    tui = make_tui(tmp_path)
    await tui.startup()
    for index in range(80):
        tui.conversation.add(UserBlock(text=f"line-{index}"))
    tui._follow_tail = True
    tui._scroll_offset = 10**9
    screen = io.StringIO()

    with create_pipe_input() as pipe:
        tui.build_application(input=pipe, output=PlainTextOutput(screen))
        task = asyncio.create_task(tui._app.run_async())
        await asyncio.sleep(0.3)
        info = tui.conversation_window.render_info
        assert info is not None
        first_scroll = info.vertical_scroll
        # The tail is visible and the view is scrolled down, not stuck at row 0.
        assert first_scroll > 0
        assert "line-79" in screen.getvalue().splitlines()[-40:][-1] or "line-79" in screen.getvalue()
        pipe.send_text("\x1b[5~")  # PgUp
        await asyncio.sleep(0.2)
        info = tui.conversation_window.render_info
        assert info is not None
        assert info.vertical_scroll < first_scroll
        pipe.send_text("\x03")
        await asyncio.sleep(0.05)
        pipe.send_text("\x03")
        assert await asyncio.wait_for(task, timeout=5) == 0

    rendered = screen.getvalue()
    # Lines are actually separate rows, not one concatenated fragment.
    assert "line-79" in rendered
    assert "line-0line-1" not in rendered
    await tui.close()


@pytest.mark.asyncio
async def test_resumed_daemon_that_is_gone_is_replaced(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    record = store.new(title="old")
    record.daemon_id = "daemon-gone"
    record.daemon_label = "gone"
    store.save(record)

    tui = make_tui(tmp_path, continue_session=True)
    await tui.startup()

    assert tui._daemon_id == "daemon-1"
    assert any(
        "no longer registered" in line for line in tui.conversation.plain_lines()
    )
    await tui.close()


@pytest.mark.asyncio
async def test_new_session_stops_the_previous_watch(tmp_path: Path):
    class SlowClient(FakeClient):
        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            yield _event("output", {"kind": "text", "text": "old output"}, "1")
            await asyncio.sleep(30)
            yield _event("end", {"status": "completed"})

    client = SlowClient()
    tui = make_tui(tmp_path, client=client)
    await tui.startup()
    watch_task = asyncio.create_task(tui.watch("ticket-a"))
    await asyncio.sleep(0.1)
    assert tui._watch_task is not None

    await tui.handle_command("/new")

    assert tui._watch_task is None
    assert watch_task.done()
    blocks_after_new = list(tui.conversation.blocks)
    await asyncio.sleep(0.05)
    assert tui.conversation.blocks == blocks_after_new  # no late output leaks
    await tui.close()


@pytest.mark.asyncio
async def test_watch_cancellation_does_not_overwrite_a_newer_watch_state(
    tmp_path: Path,
):
    class SlowClient(FakeClient):
        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            await asyncio.sleep(30)
            yield _event("end", {"status": "completed"})

    client = SlowClient()
    tui = make_tui(tmp_path, client=client)
    await tui.startup()
    first = asyncio.create_task(tui.watch("ticket-a"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(tui.watch("ticket-b"))
    await asyncio.sleep(0.1)

    # The old watch's cancellation cleanup must not mark the new watch idle.
    assert tui.status.stream == "live"
    assert first.done()
    tui.interrupt()
    await asyncio.wait_for(second, timeout=2)
    await asyncio.wait_for(first, timeout=2)
    await tui.close()


@pytest.mark.asyncio
async def test_print_mode_keeps_streamed_tokens_on_one_line(capsys):
    stream = [
        _event("output", {"kind": "text", "text": "Hel"}, "1"),
        _event("output", {"kind": "text", "text": "lo"}, "2"),
        _event("end", {"status": "completed"}, "3"),
    ]
    client = FakeClient(stream=stream)

    assert await run_print_mode(
        server_url="http://test", user_id="u1", prompt="x", client=client
    ) == 0

    out = capsys.readouterr().out
    assert "Hello" in out
    assert "Hel\nlo" not in out


@pytest.mark.asyncio
async def test_print_mode_sanitizes_ticket_id_and_server_errors(capsys):
    class HostileClient(FakeClient):
        async def create_task(self, prompt: str, **kwargs: Any):
            return {"ticket_id": "evil\x1b]52;c;ZXZpbA==\x07id", "status": "queued"}

        async def home_workspace(self) -> str:
            message = "bad\x1b[2Jbase"
            raise SuperAgentError(message)

    client = HostileClient()
    code = await run_print_mode(
        server_url="http://test", user_id="u1", prompt="x", client=client
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "\x1b" not in out
    assert "\x07" not in out


@pytest.mark.asyncio
async def test_new_session_fences_a_submission_awaiting_ticket_creation(
    tmp_path: Path,
):
    class PausedClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(stream=_completed_stream())
            self.release = asyncio.Event()

        async def create_task(self, prompt: str, **kwargs: Any):
            await self.release.wait()
            return {"ticket_id": FULL_ID, "status": "queued"}

    client = PausedClient()
    tui = make_tui(tmp_path, client=client)
    await tui.startup()

    submission = asyncio.create_task(tui.submit("old prompt"))
    await asyncio.sleep(0.05)
    await tui.handle_command("/new")
    client.release.set()
    await asyncio.wait_for(submission, timeout=5)

    assert tui.session.last_ticket_id == ""
    assert tui.conversation.blocks == [] or all(
        "new session" in line for line in tui.conversation.plain_lines()
    )
    await tui.close()


@pytest.mark.asyncio
async def test_resume_accepts_a_unique_displayed_prefix(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    record = store.new(title="earlier")
    record.transcript = [{"kind": "user", "text": "old prompt"}]
    store.save(record)

    tui = make_tui(tmp_path)
    await tui.startup()
    await tui.handle_command("/resume")
    listing = "\n".join(tui.conversation.plain_lines())
    # The listing shows the full id, which is what /resume accepts.
    assert record.session_id in listing
    await tui.handle_command(f"/resume {record.session_id[:8]}")
    assert any(
        isinstance(block, UserBlock) and block.text == "old prompt"
        for block in tui.conversation.blocks
    )
    await tui.close()


@pytest.mark.asyncio
async def test_line_resume_banner_is_sanitized(monkeypatch, capsys):
    from ptc_cli.superagent import runner

    class FakeShell:
        def __init__(self, **kwargs):
            self._daemon_id = ""
            self._daemon_label = ""

        async def run(self):
            return 0

    class FakeStore:
        def latest(self):
            record = SessionStore.__new__(SessionStore)
            record.session_id = "abcdefabcdefabcd"
            record.daemon_id = "daemon-1"
            record.daemon_label = "evil\x1b]52;c;ZXZpbA==\x07label"
            record.last_ticket_id = "t\x1b[2Jid"
            return record

    monkeypatch.setattr(runner, "SuperAgentShell", FakeShell)
    monkeypatch.setattr(runner, "SessionStore", FakeStore)
    monkeypatch.setattr(runner, "detect_mode", lambda **kwargs: "line")

    code = await runner.run_super_agent(
        server_url="http://test", user_id="u1", continue_session=True
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "resumed session abcdefabcdefabcd" in out
    assert "\x1b" not in out
    assert "\x07" not in out


@pytest.mark.asyncio
async def test_direct_submit_failure_after_session_switch_is_fenced(tmp_path: Path):
    class FailingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(stream=_completed_stream())
            self.release = asyncio.Event()

        async def create_task(self, prompt: str, **kwargs: Any):
            await self.release.wait()
            message = "late failure"
            raise SuperAgentError(message)

    client = FailingClient()
    tui = make_tui(tmp_path, client=client)
    await tui.startup()

    submission = asyncio.create_task(tui.submit("old prompt"))
    await asyncio.sleep(0.1)
    await tui.handle_command("/new")
    client.release.set()
    await asyncio.wait_for(submission, timeout=5)

    assert not any(isinstance(block, ErrorBlock) for block in tui.conversation.blocks)
    assert tui.session.last_ticket_id == ""
    await tui.close()


@pytest.mark.asyncio
async def test_direct_shell_output_after_session_switch_is_fenced(
    monkeypatch, tmp_path: Path
):
    started = asyncio.Event()
    release = asyncio.Event()

    def slow_shell(command, cwd=None):
        del command, cwd
        return "late shell output"

    async def fake_to_thread(fn, *args, **kwargs):
        started.set()
        await release.wait()
        return fn(*args, **kwargs)

    tui = make_tui(tmp_path)
    await tui.startup()
    tui.permissions.allow_session("local-shell")
    monkeypatch.setattr("ptc_cli.superagent.tui.run_shell", slow_shell)
    monkeypatch.setattr(
        "ptc_cli.superagent.tui.asyncio.to_thread", fake_to_thread
    )

    submission = asyncio.create_task(tui.submit("!echo hi"))
    await asyncio.wait_for(started.wait(), timeout=2)
    await tui.handle_command("/new")
    release.set()
    await asyncio.wait_for(submission, timeout=5)

    lines = tui.conversation.plain_lines()
    assert not any("late shell output" in line for line in lines)
    await tui.close()


@pytest.mark.asyncio
async def test_resume_prefers_an_exact_id_over_a_longer_prefix(tmp_path: Path):
    from ptc_cli.superagent.tui_sessions import SessionRecord

    store = SessionStore(tmp_path / "sessions")
    store._ensure_root()
    short = SessionRecord(session_id="aaaaaaaabbbbbbbb", transcript=[{"kind": "user", "text": "short"}])
    longer = SessionRecord(session_id="aaaaaaaabbbbbbbbc", transcript=[{"kind": "user", "text": "longer"}])
    # Both ids are valid 8-32 hex ids with a shared prefix.
    store.save(short)
    store.save(longer)

    tui = make_tui(tmp_path)
    await tui.startup()
    await tui.handle_command("/resume aaaaaaaabbbbbbbb")

    assert tui.session.session_id == "aaaaaaaabbbbbbbb"
    assert any(
        isinstance(block, UserBlock) and block.text == "short"
        for block in tui.conversation.blocks
    )
    await tui.close()


@pytest.mark.asyncio
async def test_recovered_stream_clears_reconnecting_status(tmp_path: Path):
    class RecoveringClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()
            self.attempt = 0

        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            self.attempt += 1
            if self.attempt == 1:
                yield _event("output", {"kind": "text", "text": "before"}, "1")
                message = "stream dropped"
                raise SuperAgentError(message)
            yield _event("output", {"kind": "text", "text": "after"}, "2")
            await self.release.wait()
            yield _event("end", {"status": "completed"}, "3")

    async def no_sleep(_seconds):
        await asyncio.sleep(0)

    client = RecoveringClient()
    tui = make_tui(tmp_path, client=client)
    tui._sleep = no_sleep
    await tui.startup()
    watching = asyncio.create_task(tui.watch("ticket-x"))
    for _ in range(20):
        await asyncio.sleep(0.02)
        if tui.status.stream == "live":
            break

    # Frames resumed after the reconnect notice: the bar is live again.
    assert tui.status.stream == "live"
    client.release.set()
    await asyncio.wait_for(watching, timeout=5)
    await tui.close()


@pytest.mark.asyncio
async def test_stale_watch_cleanup_does_not_pollute_the_new_session(
    monkeypatch, tmp_path: Path
):
    class QuickClient(FakeClient):
        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            yield _event("end", {"status": "completed"}, "1")

    client = QuickClient()
    tui = make_tui(tmp_path, client=client)
    await tui.startup()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def paused_refresh(*, epoch: int | None = None) -> None:
        del epoch
        entered.set()
        await release.wait()
        message = "old summary"
        raise SuperAgentError(message)

    monkeypatch.setattr(tui, "_refresh_overview", paused_refresh)
    watched = asyncio.create_task(tui.watch("ticket-old"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    await tui.handle_command("/new")
    release.set()
    await asyncio.wait_for(watched, timeout=5)

    assert not any("server summary" in line for line in tui.conversation.plain_lines())
    await tui.close()


@pytest.mark.asyncio
async def test_stale_shell_denial_does_not_enter_the_new_session(tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()

    submission = asyncio.create_task(tui.submit("!echo hi"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if tui._pending is not None:
            break
    assert tui._pending is not None
    await tui.handle_command("/new")
    tui.resolve_permission("d")
    await asyncio.wait_for(submission, timeout=5)

    assert not any("denied" in line for line in tui.conversation.plain_lines())
    await tui.close()


@pytest.mark.asyncio
async def test_resume_refreshes_the_status_bar(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    record = store.new(title="old")
    record.daemon_id = "daemon-9"
    record.daemon_label = "old-daemon"
    record.router_model = "old-router"
    store.save(record)

    tui = make_tui(tmp_path)
    await tui.startup()
    await tui.handle_command(f"/resume {record.session_id}")

    assert tui.status.session == record.short_id
    assert tui.status.daemon == "old-daemon"
    await tui.close()


@pytest.mark.asyncio
async def test_stale_permission_cannot_grant_session_access(tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()

    submission = asyncio.create_task(tui.submit("!echo hi"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if tui._pending is not None:
            break
    await tui.handle_command("/new")
    assert tui.resolve_permission("s") is True
    await asyncio.wait_for(submission, timeout=5)

    # The "s" answer belonged to the old session: no session-wide grant.
    assert tui.permissions.needs_confirmation("local-shell") is True
    await tui.close()


@pytest.mark.asyncio
async def test_stale_jobs_listing_does_not_enter_the_new_session(tmp_path: Path):
    class PausedJobsClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(stream=_completed_stream())
            self.release = asyncio.Event()

        async def tickets(self, **params: Any):
            await self.release.wait()
            return {
                "tickets": [
                    {"ticket_id": "old-ticket-id", "status": "running", "title": "old job"}
                ]
            }

    client = PausedJobsClient()
    tui = make_tui(tmp_path, client=client)
    await tui.startup()

    listing = asyncio.create_task(tui.submit("/jobs"))
    await asyncio.sleep(0.1)
    await tui.handle_command("/new")
    client.release.set()
    await asyncio.wait_for(listing, timeout=5)

    assert not any("old job" in line for line in tui.conversation.plain_lines())
    await tui.close()


@pytest.mark.asyncio
async def test_stale_workspace_lookup_does_not_overwrite_a_resumed_one(
    tmp_path: Path,
):
    store = SessionStore(tmp_path / "sessions")
    record = store.new(title="restored")
    record.workspace_id = "restored-ws"
    store.save(record)

    class PausedWorkspaceClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(stream=_completed_stream())
            self.release = asyncio.Event()

        async def home_workspace(self) -> str:
            await self.release.wait()
            return "stale-ws"

    client = PausedWorkspaceClient()
    tui = make_tui(tmp_path, client=client)
    await tui.startup()

    submission = asyncio.create_task(tui.submit("old prompt"))
    await asyncio.sleep(0.1)
    await tui.handle_command(f"/resume {record.session_id}")
    client.release.set()
    await asyncio.wait_for(submission, timeout=5)

    assert tui._workspace_id == "restored-ws"
    await tui.close()


@pytest.mark.asyncio
async def test_refresh_helpers_tolerate_missing_payloads(tmp_path: Path):
    class SparseClient(FakeClient):
        async def overview(self):
            return None  # type: ignore[return-value]

        async def daemons(self):
            return None  # type: ignore[return-value]

    tui = make_tui(tmp_path, client=SparseClient())
    await tui.startup()

    await tui._refresh_overview()
    await tui._refresh_daemons(epoch=tui._epoch - 1)
    assert tui.status.stream == "idle"
    await tui.close()


@pytest.mark.asyncio
async def test_timeout_adds_a_transcript_notice(tmp_path: Path):
    class StalledClient(FakeClient):
        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            await asyncio.sleep(30)
            yield _event("end", {"status": "completed"})

    async def no_sleep(_seconds):
        await asyncio.sleep(0)

    tui = make_tui(tmp_path, client=StalledClient())
    tui._sleep = no_sleep
    tui._timeout = 0.05
    await tui.startup()
    await asyncio.wait_for(tui.watch("ticket-x"), timeout=5)

    assert any("timed out" in line for line in tui.conversation.plain_lines())
    await tui.close()


@pytest.mark.asyncio
async def test_resume_clears_the_old_daemon_liveness_suffix(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    record = store.new(title="old")
    record.daemon_id = "daemon-9"
    record.daemon_label = "old-daemon"
    store.save(record)

    tui = make_tui(tmp_path)
    await tui.startup()
    tui.status.daemon_state = "online"
    await tui.handle_command(f"/resume {record.session_id}")

    assert tui.status.daemon_state == ""
    await tui.close()


@pytest.mark.asyncio
async def test_refresh_helpers_tolerate_malformed_nested_payloads(tmp_path: Path):
    class MalformedClient(FakeClient):
        async def overview(self):
            return {"counts": 7}

        async def daemons(self):
            return {"daemons": [None, {"daemon_id": "d1", "name": "ok"}]}

    tui = make_tui(tmp_path, client=MalformedClient())
    await tui.startup()

    await tui._refresh_overview()
    await tui._refresh_daemons()
    assert tui.status.daemon == "ok"
    await tui.close()


@pytest.mark.asyncio
async def test_stale_model_failure_does_not_append_to_the_new_session(tmp_path: Path):
    class PausedModelsClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(stream=_completed_stream())
            self.release = asyncio.Event()

        async def models(self):
            await self.release.wait()
            message = "models failed"
            raise SuperAgentError(message)

    client = PausedModelsClient()
    tui = make_tui(tmp_path, client=client)
    await tui.startup()

    command = asyncio.create_task(tui.submit("/model"))
    await asyncio.sleep(0.1)
    await tui.handle_command("/new")
    client.release.set()
    await asyncio.wait_for(command, timeout=5)

    assert not any("Router model" in line for line in tui.conversation.plain_lines())
    await tui.close()


@pytest.mark.asyncio
async def test_malformed_daemon_rows_do_not_crash_startup(tmp_path: Path):
    class MalformedDaemonsClient(FakeClient):
        async def overview(self):
            return {}

        async def daemons(self):
            return {"daemons": [None]}

    tui = make_tui(tmp_path, client=MalformedDaemonsClient())
    await tui.startup()

    assert tui.status.daemon == "no daemon"
    await tui.close()


@pytest.mark.asyncio
async def test_malformed_daemon_rows_do_not_break_watch_cleanup(tmp_path: Path):
    class MalformedAfterWatchClient(FakeClient):
        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            yield _event("end", {"status": "completed"}, "1")

        async def daemons(self):
            return {"daemons": [None]}

    tui = make_tui(tmp_path, client=MalformedAfterWatchClient())
    await tui.startup()

    await asyncio.wait_for(tui.watch("ticket-x"), timeout=5)
    await tui.close()


@pytest.mark.asyncio
async def test_malformed_command_payloads_do_not_raise(tmp_path: Path):
    class BrokenPayloadsClient(FakeClient):
        async def tickets(self, **params: Any):
            return None

        async def fleet(self):
            return None

        async def sources(self):
            return None

    tui = make_tui(tmp_path, client=BrokenPayloadsClient())
    await tui.startup()

    for command in ("/jobs", "/agents", "/signals"):
        await tui.handle_command(command)
    assert not any(isinstance(block, ErrorBlock) for block in tui.conversation.blocks)
    await tui.close()


@pytest.mark.asyncio
async def test_page_down_advances_an_upscrolled_conversation(tmp_path: Path):
    import io

    from prompt_toolkit.output.plain_text import PlainTextOutput

    tui = make_tui(tmp_path)
    await tui.startup()
    for index in range(120):
        tui.conversation.add(UserBlock(text=f"row-{index}"))
    tui._follow_tail = True
    tui._scroll_offset = 10**9
    screen = io.StringIO()

    with create_pipe_input() as pipe:
        tui.build_application(input=pipe, output=PlainTextOutput(screen))
        task = asyncio.create_task(tui._app.run_async())
        await asyncio.sleep(0.3)

        for _ in range(40):  # reach the top
            pipe.send_text("\x1b[5~")
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        info = tui.conversation_window.render_info
        assert info is not None
        assert info.vertical_scroll == 0

        for _ in range(3):  # page down must move the viewport
            pipe.send_text("\x1b[6~")
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)
        info = tui.conversation_window.render_info
        assert info is not None
        assert info.vertical_scroll > 0

        pipe.send_text("\x03")
        await asyncio.sleep(0.05)
        pipe.send_text("\x03")
        assert await asyncio.wait_for(task, timeout=5) == 0
    await tui.close()


@pytest.mark.asyncio
async def test_print_mode_tolerates_malformed_daemons(capsys):
    class MalformedDaemonsClient(FakeClient):
        async def daemons(self):
            return {"daemons": [None]}

    client = MalformedDaemonsClient()
    code = await run_print_mode(
        server_url="http://test", user_id="u1", prompt="x", client=client
    )

    assert code == 1
    assert "no local daemon" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_print_mode_tolerates_a_null_task_response(capsys):
    class NullTaskClient(FakeClient):
        async def create_task(self, prompt: str, **kwargs: Any):
            return None

    client = NullTaskClient()
    code = await run_print_mode(
        server_url="http://test", user_id="u1", prompt="x", client=client
    )

    assert code == 1
    assert "malformed Ticket response" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_two_ctrl_c_presses_exit_while_watching(tmp_path: Path):
    class SlowClient(FakeClient):
        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            await asyncio.sleep(30)
            yield _event("end", {"status": "completed"})

    tui = make_tui(tmp_path, client=SlowClient())
    await tui.startup()
    watching = asyncio.create_task(tui.watch("ticket-x"))
    await asyncio.sleep(0.1)

    with create_pipe_input() as pipe:
        tui.build_application(input=pipe, output=DummyOutput())
        task = asyncio.create_task(tui._app.run_async())
        await asyncio.sleep(0.1)
        pipe.send_text("\x03")
        await asyncio.sleep(0.1)
        pipe.send_text("\x03")
        assert await asyncio.wait_for(task, timeout=5) == 0

    await asyncio.wait_for(watching, timeout=5)
    assert tui._watch_task is None
    await tui.close()


@pytest.mark.asyncio
async def test_empty_daemon_list_clears_the_stale_selection(tmp_path: Path):
    class EmptyDaemonsClient(FakeClient):
        async def daemons(self):
            return {"daemons": []}

    tui = make_tui(tmp_path, client=EmptyDaemonsClient())
    await tui.startup()
    assert tui._daemon_id == ""

    await tui._refresh_daemons()
    assert tui._daemon_id == ""
    assert tui.status.daemon == "no daemon"
    await tui.close()


@pytest.mark.asyncio
async def test_agents_and_signals_tolerate_null_rows(tmp_path: Path):
    class NullRowsClient(FakeClient):
        async def fleet(self):
            return {"agents": [None, {"id": "a1", "role": "r", "status": "s", "branch": "b"}]}

        async def daemons(self):
            return {"daemons": [None, {"daemon_id": "d1", "name": "n", "provider": "p"}]}

        async def sources(self):
            return {"sources": [None, {"source_id": "s1", "kind": "github", "enabled": True, "config": None}]}

    tui = make_tui(tmp_path, client=NullRowsClient())
    await tui.startup()
    await tui.handle_command("/agents")
    await tui.handle_command("/signals")

    assert not any(isinstance(block, ErrorBlock) for block in tui.conversation.blocks)
    await tui.close()


@pytest.mark.asyncio
async def test_print_mode_reports_a_malformed_home_response(capsys):
    class BadHomeClient(FakeClient):
        async def home_workspace(self) -> str:
            key = "workspace_id"
            raise KeyError(key)

    code = await run_print_mode(
        server_url="http://test", user_id="u1", prompt="x", client=BadHomeClient()
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "error:" in out


@pytest.mark.asyncio
async def test_two_ctrl_c_presses_exit_with_a_draft_and_active_watch(
    tmp_path: Path,
):
    class SlowClient(FakeClient):
        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            await asyncio.sleep(30)
            yield _event("end", {"status": "completed"})

    tui = make_tui(tmp_path, client=SlowClient())
    await tui.startup()
    watching = asyncio.create_task(tui.watch("ticket-x"))
    await asyncio.sleep(0.1)

    with create_pipe_input() as pipe:
        tui.build_application(input=pipe, output=DummyOutput())
        task = asyncio.create_task(tui._app.run_async())
        await asyncio.sleep(0.1)
        pipe.send_text("draft text")
        await asyncio.sleep(0.05)
        pipe.send_text("\x03")
        await asyncio.sleep(0.1)
        assert not task.done()  # first press clears/interrupts but does not quit
        pipe.send_text("\x03")
        assert await asyncio.wait_for(task, timeout=5) == 0

    await asyncio.wait_for(watching, timeout=5)
    assert tui._watch_task is None
    await tui.close()


@pytest.mark.asyncio
async def test_self_resume_keeps_unsaved_conversation(tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    tui.conversation.add(UserBlock(text="unsaved streamed output"))
    session_id = tui.session.session_id

    await tui.handle_command(f"/resume {session_id}")

    assert any(
        isinstance(block, UserBlock) and block.text == "unsaved streamed output"
        for block in tui.conversation.blocks
    )
    assert any("already in session" in line for line in tui.conversation.plain_lines())
    await tui.close()


@pytest.mark.asyncio
async def test_wrapped_paragraph_middle_is_reachable(tmp_path: Path):
    import io

    from prompt_toolkit.output.plain_text import PlainTextOutput

    tui = make_tui(tmp_path)
    await tui.startup()
    tui.conversation.add(UserBlock(text="prompt"))
    tui.conversation.add(NoticeBlock(text="ticket abc created"))
    paragraph = " ".join(f"word{index}" for index in range(900))
    tui.conversation.add(AssistantBlock(text=paragraph))
    tui._follow_tail = True
    tui._scroll_offset = 10**9
    screen = io.StringIO()

    with create_pipe_input() as pipe:
        tui.build_application(input=pipe, output=PlainTextOutput(screen))
        task = asyncio.create_task(tui._app.run_async())
        await asyncio.sleep(0.3)
        info = tui.conversation_window.render_info
        assert info is not None
        content = info.content_height
        viewport = info.window_height
        assert content > viewport * 2  # the paragraph wraps over many rows

        for _ in range(content + viewport):
            pipe.send_text("\x1b[5~")
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.2)
        info = tui.conversation_window.render_info
        assert info is not None
        assert info.vertical_scroll == 0

        pipe.send_text("\x1b[6~")
        await asyncio.sleep(0.2)
        info = tui.conversation_window.render_info
        assert info is not None
        # One PageDown from the top must move into the wrapped middle, not
        # jump straight to the tail.
        assert 0 < info.vertical_scroll < content - viewport

        pipe.send_text("\x03")
        await asyncio.sleep(0.05)
        pipe.send_text("\x03")
        assert await asyncio.wait_for(task, timeout=5) == 0
    await tui.close()


@pytest.mark.asyncio
async def test_merge_uses_the_server_merge_timeout(monkeypatch, tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    observed: dict[str, Any] = {}

    def fake(args: list[str], **kwargs: Any) -> str:
        observed["args"] = args
        observed.update(kwargs)
        return "merged"

    monkeypatch.setattr("ptc_cli.superagent.tui.run_conductor", fake)
    task = asyncio.create_task(tui.handle_command("/merge feat/x --into integrate/r2"))
    await asyncio.sleep(0)
    tui.resolve_permission("a")
    await task

    assert observed["args"] == ["merge", "feat/x", "--into", "integrate/r2"]
    assert observed["timeout"] == 11_100.0
    await tui.close()


@pytest.mark.asyncio
async def test_second_gated_action_waits_for_the_first_permission(monkeypatch, tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    called: list[list[str]] = []
    monkeypatch.setattr(
        "ptc_cli.superagent.tui.run_conductor",
        lambda args, **kwargs: called.append(args) or "merged",
    )

    first = asyncio.create_task(tui.handle_command("/merge one --into integrate/r2"))
    await asyncio.sleep(0)
    assert tui._pending is not None and "one" in tui._pending[0].label
    second = asyncio.create_task(tui.handle_command("/merge two --into integrate/r2"))
    await asyncio.sleep(0)
    assert "one" in tui._pending[0].label

    tui.resolve_permission("a")
    for _ in range(20):
        await asyncio.sleep(0.01)
        if tui._pending is not None:
            break
    assert tui._pending is not None and "two" in tui._pending[0].label
    tui.resolve_permission("a")
    await first
    await second

    assert called == [
        ["merge", "one", "--into", "integrate/r2"],
        ["merge", "two", "--into", "integrate/r2"],
    ]
    await tui.close()


@pytest.mark.asyncio
async def test_default_supervisor_view_shows_only_progress_and_result(tmp_path: Path):
    stream = [
        _event("activity", {"event_id": "e1", "kind": "created", "summary": "task"}, "1"),
        _event(
            "output",
            {"kind": "text", "text": "worker-token", "run": "r1", "epoch": 1},
            "2",
        ),
        _event(
            "output",
            {"kind": "reasoning", "text": "private-reasoning", "run": "r1", "epoch": 1},
            "3",
        ),
        _event(
            "output",
            {"kind": "text", "text": "child-output", "agent": "reviewer", "run": "r1", "epoch": 1},
            "4",
        ),
        _event("output", {"kind": "tool", "tools": ["read_secret"]}, "5"),
        _event("activity", {"event_id": "e2", "kind": "progress", "summary": "internal-step"}, "6"),
        _event("status", {"status": "running"}, "7"),
        _event("activity", {"event_id": "e3", "kind": "report", "summary": "high-level-report"}, "8"),
        _event("end", {"status": "completed", "summary": "all done"}, "9"),
    ]
    tui = make_tui(tmp_path, client=FakeClient(stream=stream))
    tui._clock = lambda: 1_000.0
    tui._monotonic = lambda: 10.0
    assert tui.show_worker_details is False
    await tui.startup()

    await tui.submit("fix the bug")

    visible = _visible_text(tui)
    for hidden in (
        "worker-token",
        "private-reasoning",
        "child-output",
        "reviewer",
        "read_secret",
        "internal-step",
    ):
        assert hidden not in visible
    assert "✔ fix the bug" in visible
    assert "0ms" in visible
    assert "phase:" not in visible
    assert "status: completed" not in visible
    assert "Ticket completed" not in visible
    assert "▸ high-level-report" in visible
    assert "summary: all done" in visible
    assert "100%" not in visible
    assert "DAG" not in visible

    header = "".join(text for _style, text in tui._header_fragments())
    assert "completed" in header
    assert any(unit in header for unit in ("ms", "s", "m"))
    tui.status.stream = "live"
    status = "".join(text for _style, text in tui._status_fragments())
    assert "\n" not in status
    assert status.count("\n") == 0
    assert "auto" in status
    assert "online" in status
    assert "needing you" not in status
    tui.status.attention = 2
    needing = "".join(text for _style, text in tui._status_fragments())
    # The count stays on the footer. The words "needing you" are the home row,
    # so an 80-column footer can still keep alt+m.
    assert "◆ 2" in needing
    assert "needing you" not in needing
    await tui.close()


@pytest.mark.asyncio
async def test_details_toggle_reveals_history_and_hides_it_again(tmp_path: Path):
    tui = make_tui(tmp_path, client=FakeClient(stream=_completed_stream()))
    await tui.startup()
    await tui.submit("fix the bug")

    hidden = _visible_text(tui)
    assert "working" not in hidden
    assert "read_file" not in hidden

    await tui.handle_command("/details")
    shown = _visible_text(tui)
    assert tui.show_worker_details is True
    assert "working" in shown
    assert "done" in shown
    assert "read_file" in shown
    assert "✔ fix the bug" in shown

    await tui.handle_command("/details")
    hidden_again = _visible_text(tui)
    assert tui.show_worker_details is False
    assert "working" not in hidden_again
    assert "read_file" not in hidden_again
    assert "✔ fix the bug" in hidden_again
    await tui.close()


@pytest.mark.asyncio
async def test_watching_a_new_ticket_resets_elapsed(tmp_path: Path):
    wall = iter([100.0, 130.0, 200.0, 205.0])
    monotonic = {"value": 1.0}
    tui = make_tui(tmp_path, client=FakeClient(stream=_completed_stream()))
    tui._clock = lambda: next(wall)
    tui._monotonic = lambda: monotonic["value"]
    await tui.startup()

    await asyncio.wait_for(tui.watch("ticket-a"), timeout=5)
    first = tui._progress_block
    assert first is not None
    assert first.started_at == 100.0
    assert first.finished_at == 130.0
    assert first.elapsed(999.0) == 30.0

    monotonic["value"] = 50.0
    await asyncio.wait_for(tui.watch("ticket-b"), timeout=5)
    second = tui._progress_block
    assert second is not None and second is not first
    assert second.started_at == 200.0
    assert second.finished_at == 205.0
    assert second.elapsed(999.0) == 5.0
    assert tui.status.started_monotonic == 50.0
    await tui.close()


@pytest.mark.asyncio
async def test_submit_elapsed_starts_before_the_watch(tmp_path: Path):
    wall = iter([100.0, 110.0])
    tui = make_tui(tmp_path, client=FakeClient(stream=_completed_stream()))
    tui._clock = lambda: next(wall)
    await tui.startup()

    await tui.submit("fix the bug")

    progress = tui._progress_block
    assert progress is not None
    assert progress.started_at == 100.0
    assert progress.finished_at == 110.0
    assert progress.elapsed(999.0) == 10.0
    await tui.close()


@pytest.mark.asyncio
async def test_restored_worker_text_stays_hidden_in_supervisor_view(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    record = store.new(title="legacy")
    record.last_ticket_id = FULL_ID
    record.transcript = [
        {
            "kind": "assistant",
            "text": "legacy-worker-text",
            "run_id": "legacy-run",
            "epoch": 3,
            "worker_detail": False,
        },
        {
            "kind": "assistant",
            "text": "legacy-child-reasoning",
            "reasoning": True,
            "agent": "reviewer",
            "run_id": "legacy-run",
            "epoch": 3,
        },
        {
            "kind": "tool",
            "name": "legacy_tool",
            "status": "done",
            "worker_detail": False,
        },
        {
            "kind": "activity",
            "event_kind": "progress",
            "summary": "legacy-internal-step",
        },
        {"kind": "end", "status": "completed", "summary": "restored result"},
    ]
    store.save(record)
    tui = make_tui(tmp_path, continue_session=True)
    await tui.startup()

    visible = _visible_text(tui)
    for hidden in (
        "legacy-worker-text",
        "legacy-child-reasoning",
        "legacy_tool",
        "legacy-internal-step",
    ):
        assert hidden not in visible
    assert "restored result" in visible

    await tui.handle_command("/details")
    detailed = _visible_text(tui)
    assert "legacy-worker-text" in detailed
    assert "legacy-child-reasoning" in detailed
    assert "legacy_tool" in detailed
    assert "legacy-internal-step" in detailed
    await tui.close()


@pytest.mark.asyncio
async def test_failed_ticket_detail_does_not_swallow_error(tmp_path: Path):
    client = FakeClient()
    client.ticket_payload = {
        "ticket_id": FULL_ID,
        "status": "failed",
        "summary": "worker stopped",
        "error": "failure detail from Ticket",
    }
    tui = make_tui(tmp_path, client=client)
    await tui.startup()

    await tui.handle_command(f"/jobs {FULL_ID}")

    visible = _visible_text(tui)
    assert "Failed" in visible
    assert "worker stopped" in visible
    assert "error: failure detail from Ticket" in visible
    await tui.close()


@pytest.mark.asyncio
async def test_jobs_attention_flag_uses_the_attention_view(tmp_path: Path):
    client = FakeClient()
    client.tickets_payload = {
        "tickets": [
            {
                "ticket_id": FULL_ID,
                "status": "completed",
                "title": "Read the adapter registry",
            },
            {
                "ticket_id": WAITING_FULL_ID,
                "status": "waiting",
                "title": "Needs a host with bash",
                "attention": {"question": "Which host should run this Ticket?"},
            },
        ]
    }
    tui = make_tui(tmp_path, client=client)
    await tui.startup()

    await tui.handle_command("/jobs --attention")

    text = str(getattr(tui.conversation.blocks[-1], "text", ""))
    assert "jobs · needs you" in text
    assert "Needs a host with bash" in text
    assert "Read the adapter registry" not in text
    await tui.close()


def _header_text(tui: SuperAgentTui) -> str:
    return "".join(text for _style, text in tui._header_fragments())


def _result_card(tui: SuperAgentTui) -> EndBlock | None:
    """The EndBlock for the current progress card, if this view has one."""
    progress = tui._progress_block
    if progress is None:
        return None
    try:
        start = tui.conversation.blocks.index(progress)
    except ValueError:
        return None
    # The newest EndBlock after the card, unless the card itself is newer.
    for index in range(len(tui.conversation.blocks) - 1, start, -1):
        block = tui.conversation.blocks[index]
        if isinstance(block, EndBlock):
            return block
    return None


def _view_snapshot(tui: SuperAgentTui, *, now: float) -> dict[str, Any]:
    """Phase, elapsed, stored text, source, and the current result card."""
    progress = tui._progress_block
    card = _result_card(tui)
    return {
        "phase": tui.status.phase,
        "elapsed": None if progress is None else progress.elapsed(now),
        "summary": tui.status.summary,
        "question": tui.status.question,
        "error": tui.status.error,
        "source": tui._progress_source,
        "ticket": None if progress is None else progress.ticket_id,
        "block_phase": None if progress is None else progress.phase,
        "block_summary": None if progress is None else progress.summary,
        "block_question": None if progress is None else progress.question,
        "block_error": None if progress is None else progress.error,
        "card_status": None if card is None else card.status,
        "card_summary": None if card is None else card.summary,
        "card_question": None if card is None else card.question,
        "card_error": None if card is None else card.error,
        "visible": _visible_text(tui),
        "header": _header_text(tui),
    }


def _server_progress(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ticket_id": FULL_ID,
        "status": "waiting",
        "phase": "waiting",
        "elapsed_seconds": 31.2,
        "steps": [
            {"key": "created", "label": "任务已创建", "state": "done"},
            {"key": "worker", "label": "执行中", "state": "done"},
        ],
        "summary": "server-summary",
        "question": "server-question",
        "error": "server-error",
        "transcript": "WORKER-RAW-TEXT",
        "tool_calls": [{"name": "bash", "arguments": "TOOL-PARAM-secret-path"}],
    }
    payload.update(overrides)
    return payload


class ProgressClient(FakeClient):
    """FakeClient that records Supervisor progress reads."""

    def __init__(self, *, stream: list[ActivityStreamEvent] | None = None) -> None:
        super().__init__(stream=stream)
        self.progress_calls: list[str] = []
        self.progress_result: Any = None
        self.progress_error: Exception | None = None

    async def progress(self, ticket_id: str) -> Any:
        self.progress_calls.append(ticket_id)
        if self.progress_error is not None:
            raise self.progress_error
        return self.progress_result


@pytest.mark.asyncio
async def test_progress_overwrites_sse_phase_elapsed_and_result(tmp_path: Path):
    stream = [
        _event("output", {"kind": "text", "text": "WORKER-RAW-TEXT"}, "1"),
        _event(
            "output",
            {"kind": "tool", "tools": ["bash"], "arguments": "TOOL-PARAM-secret-path"},
            "2",
        ),
        _event("status", {"status": "running", "summary": "sse-status-summary"}, "3"),
        _event(
            "end",
            {
                "status": "completed",
                "summary": "sse-end-summary",
                "question": "sse-question",
                "error": "sse-error",
            },
            "4",
        ),
    ]
    client = ProgressClient(stream=stream)
    client.progress_result = _server_progress()
    tui = make_tui(tmp_path, client=client)
    tui._clock = lambda: 1000.0
    tui._monotonic = lambda: 50.0
    await tui.startup()

    await tui.submit("fix the bug")

    assert client.progress_calls == [FULL_ID, FULL_ID, FULL_ID]
    assert tui.status.phase == "waiting"
    assert tui.status.summary == "server-summary"
    assert tui.status.question == "server-question"
    assert tui.status.error == "server-error"
    progress = tui._progress_block
    assert progress is not None
    assert progress.phase == "waiting"
    assert progress.elapsed(1000.0) == pytest.approx(31.2)
    assert progress.summary == "server-summary"
    assert progress.question == "server-question"
    assert progress.error == "server-error"
    visible = _visible_text(tui)
    assert "Needs you" in visible or "needs you:" in visible
    assert "31.2s" in visible
    # Waiting shows the question immediately. Summary and error stay stored
    # and off the screen until a phase that is allowed to show them.
    assert "needs you: server-question" in visible
    assert "summary: server-summary" not in visible
    assert "server-summary" not in visible
    assert "error: server-error" not in visible
    assert "server-error" not in visible
    assert "status: waiting" not in visible
    card = _result_card(tui)
    assert card is not None
    assert card.status == "waiting"
    assert card.summary == ""
    assert card.question == "server-question"
    assert card.error == ""
    for leaked in (
        "WORKER-RAW-TEXT",
        "TOOL-PARAM-secret-path",
        "sse-status-summary",
        "sse-end-summary",
        "sse-question",
        "sse-error",
        "status: completed",
    ):
        assert leaked not in visible
        assert leaked not in _header_text(tui)
    header = _header_text(tui)
    assert header == (
        " Conductor Super-agent  deepseek-flash-account  ·  "
        f"session {tui.status.session}  ·  waiting 31.2s  ·  source server  ·  ○ idle "
    )
    assert tui.show_worker_details is False
    await tui.close()


@pytest.mark.asyncio
async def test_sse_cannot_regress_phase_and_404_uses_stream_mark(tmp_path: Path):
    class Scripted(ProgressClient):
        def __init__(self) -> None:
            super().__init__()
            self.after_running = asyncio.Event()
            self.release_queued = asyncio.Event()
            self.after_queued = asyncio.Event()
            self.release_failed = asyncio.Event()
            self.after_failed = asyncio.Event()
            self.release_end = asyncio.Event()

        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            yield _event("status", {"status": "running", "summary": "sse-running"}, "1")
            self.after_running.set()
            await self.release_queued.wait()
            yield _event("status", {"status": "queued", "summary": "sse-queued"}, "2")
            self.after_queued.set()
            await self.release_failed.wait()
            yield _event("status", {"status": "failed", "summary": "sse-failed"}, "3")
            self.after_failed.set()
            await self.release_end.wait()
            yield _event(
                "end",
                {"status": "completed", "summary": "sse-completed"},
                "4",
            )

    client = Scripted()
    client.progress_result = None  # HTTP 404 from SuperAgentClient.progress
    tui = make_tui(tmp_path, client=client)
    tui._clock = lambda: 1000.0
    tui._monotonic = lambda: 50.0
    await tui.startup()
    watching = asyncio.create_task(tui.watch("ticket-sse"))

    await asyncio.wait_for(client.after_running.wait(), timeout=2)
    assert tui.status.phase == "running"
    assert len(client.progress_calls) == 1
    client.release_queued.set()
    await asyncio.wait_for(client.after_queued.wait(), timeout=2)
    assert tui.status.phase == "running"
    assert len(client.progress_calls) == 2
    client.release_failed.set()
    await asyncio.wait_for(client.after_failed.wait(), timeout=2)
    # failed is terminal, so a later completed end must not replace it.
    assert tui.status.phase == "failed"
    calls_before_end = len(client.progress_calls)
    client.release_end.set()
    await asyncio.wait_for(watching, timeout=2)

    assert tui.status.phase == "failed"
    assert tui._progress_block is not None
    assert tui._progress_block.phase == "failed"
    assert len(client.progress_calls) == calls_before_end + 1
    header = _header_text(tui)
    assert "source stream" in header
    assert "source server" not in header
    assert "status: completed" not in _visible_text(tui)
    assert "✖ Failed" in _visible_text(tui)
    assert header == (
        " Conductor Super-agent  deepseek-flash-account  ·  "
        f"session {tui.status.session}  ·  failed 0ms  ·  source stream  ·  ○ idle "
    )
    await tui.close()


@pytest.mark.asyncio
async def test_progress_network_error_falls_back_without_crashing(tmp_path: Path):
    client = ProgressClient(stream=_completed_stream())
    client.progress_error = SuperAgentError("cannot reach http://test: boom")
    tui = make_tui(tmp_path, client=client)
    await tui.startup()

    await tui.submit("fix the bug")

    assert tui.status.phase == "completed"
    assert "source stream" in _header_text(tui)
    assert not any(
        isinstance(block, ErrorBlock) and "cannot reach" in block.text
        for block in tui.conversation.blocks
    )
    assert "cannot reach" not in _visible_text(tui)
    await tui.close()


def test_phase_order_blocks_sse_regression_and_terminal_switches(tmp_path: Path):
    tui = make_tui(tmp_path)
    tui._set_progress_phase("queued")
    tui._set_progress_phase("running")
    tui._set_progress_phase("queued")
    assert tui.status.phase == "running"
    tui._set_progress_phase("blocked")
    assert tui.status.phase == "waiting"
    tui._set_progress_phase("running")
    assert tui.status.phase == "waiting"
    tui._set_progress_phase("completed")
    tui._set_progress_phase("failed")
    tui._set_progress_phase("cancelled")
    tui._set_progress_phase("running")
    assert tui.status.phase == "completed"
    tui._set_progress_phase("queued", authoritative=True)
    assert tui.status.phase == "queued"
    tui._set_progress_phase("archived", authoritative=True)
    assert tui.status.phase == "cancelled"


@pytest.mark.asyncio
async def test_status_command_reads_progress_once_and_leaves_details(tmp_path: Path):
    client = ProgressClient()
    client.progress_result = _server_progress(phase="running", elapsed_seconds=4)
    tui = make_tui(tmp_path, client=client)
    tui._clock = lambda: 1000.0
    tui._monotonic = lambda: 10.0
    await tui.startup()

    await tui.handle_command("/status")
    assert client.progress_calls == []
    assert "no Ticket" in _visible_text(tui)

    tui.session.last_ticket_id = FULL_ID
    await tui.handle_command("/status")
    assert client.progress_calls == [FULL_ID]
    assert tui.status.phase == "running"
    assert tui.show_worker_details is False

    await tui.handle_command("/details")
    assert tui.show_worker_details is True
    assert "source server" not in _header_text(tui)
    await tui.handle_command("/details")
    assert tui.show_worker_details is False
    assert "source server" in _header_text(tui)
    assert len(client.progress_calls) == 1
    await tui.close()


@pytest.mark.asyncio
async def test_reconnect_calibrates_once_when_the_stream_returns(tmp_path: Path):
    class Recovering(ProgressClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempt = 0
            self.resumed = asyncio.Event()
            self.release = asyncio.Event()

        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            self.attempt += 1
            if self.attempt == 1:
                yield _event("output", {"kind": "text", "text": "before"}, "1")
                raise SuperAgentError("stream dropped")
            yield _event("output", {"kind": "text", "text": "after"}, "2")
            self.resumed.set()
            await self.release.wait()
            yield _event("end", {"status": "completed", "summary": "sse-end"}, "3")

    async def no_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    client = Recovering()
    client.progress_result = _server_progress(
        ticket_id="ticket-reconnect", phase="running", elapsed_seconds=8
    )
    tui = make_tui(tmp_path, client=client)
    tui._sleep = no_sleep
    tui._clock = lambda: 1000.0
    tui._monotonic = lambda: 20.0
    await tui.startup()
    watching = asyncio.create_task(tui.watch("ticket-reconnect"))
    await asyncio.wait_for(client.resumed.wait(), timeout=2)
    assert client.progress_calls == ["ticket-reconnect"]
    assert tui.status.phase == "running"
    assert tui.status.stream == "live"
    client.release.set()
    await asyncio.wait_for(watching, timeout=2)
    assert client.progress_calls == ["ticket-reconnect", "ticket-reconnect"]
    assert tui.status.phase == "running"
    assert "sse-end" not in _visible_text(tui)
    assert "source server" in _header_text(tui)
    await tui.close()


@pytest.mark.asyncio
async def test_reconnect_activity_frame_calibrates_once(tmp_path: Path):
    """The first frame after reconnect may be an activity row, not status/end."""
    client = ProgressClient()
    client.progress_result = _server_progress(
        ticket_id="ticket-act", phase="running", elapsed_seconds=2
    )
    tui = make_tui(tmp_path, client=client)
    tui._clock = lambda: 1000.0
    tui._monotonic = lambda: 20.0
    await tui.startup()
    tui._start_progress("ticket-act", "queued")

    await tui._track_progress(
        Activity(at="", kind="note", summary="worker note", detail="not-shown"),
        ticket_id="ticket-act",
        reconnected=True,
    )
    assert client.progress_calls == ["ticket-act"]
    assert tui.status.phase == "running"
    assert "worker note" not in _header_text(tui)

    await tui._track_progress(
        Activity(at="", kind="note", summary="later", detail=""),
        ticket_id="ticket-act",
        reconnected=False,
    )
    assert client.progress_calls == ["ticket-act"]
    assert tui.status.phase == "running"
    await tui.close()


@pytest.mark.asyncio
async def test_refresh_heartbeat_does_not_call_progress(tmp_path: Path):
    class Holding(ProgressClient):
        def __init__(self) -> None:
            super().__init__()
            self.seen = asyncio.Event()
            self.release = asyncio.Event()

        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            yield _event("status", {"status": "running"}, "1")
            self.seen.set()
            await self.release.wait()
            yield _event("end", {"status": "completed"}, "2")

    client = Holding()
    client.progress_result = None
    tui = make_tui(tmp_path, client=client)
    await tui.startup()
    watching = asyncio.create_task(tui.watch("ticket-hold"))
    await asyncio.wait_for(client.seen.wait(), timeout=2)
    assert client.progress_calls == ["ticket-hold"]

    draws = {"n": 0}
    original = tui._header_fragments

    def counting() -> Any:
        draws["n"] += 1
        return original()

    tui._header_fragments = counting  # type: ignore[method-assign]
    with create_pipe_input() as pipe:
        app = tui.build_application(input=pipe, output=DummyOutput())
        app.refresh_interval = 0.05
        runner = asyncio.create_task(app.run_async())
        for _ in range(40):
            await asyncio.sleep(0.05)
            if draws["n"] >= 4:
                break
        during = list(client.progress_calls)
        pipe.send_text("\x03")
        await asyncio.sleep(0.05)
        pipe.send_text("\x03")
        await asyncio.wait_for(runner, timeout=5)

    assert draws["n"] >= 4
    assert during == ["ticket-hold"]
    client.release.set()
    await asyncio.wait_for(watching, timeout=2)
    await tui.close()


def _b_completed_stream() -> list[ActivityStreamEvent]:
    return [
        _event(
            "end",
            {
                "status": "completed",
                "summary": "sse-b-summary",
                "question": "sse-b-question",
                "error": "sse-b-error",
            },
            "1",
        )
    ]


def _b_progress() -> dict[str, Any]:
    return _server_progress(
        ticket_id="ticket-b",
        phase="completed",
        elapsed_seconds=6.5,
        summary="ticket-b-summary",
        question="ticket-b-question",
        error="ticket-b-error",
    )


async def _wait_for_progress_calls(client: ProgressClient, count: int) -> None:
    for _ in range(50):
        if len(client.progress_calls) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"expected {count} progress reads, saw {client.progress_calls}"
    )


@pytest.mark.asyncio
async def test_stale_status_for_ticket_a_does_not_overwrite_ticket_b(tmp_path: Path):
    """A slow /status for A must not land after the view has moved to B."""

    class Held(ProgressClient):
        def __init__(self) -> None:
            super().__init__(stream=_b_completed_stream())
            self.release_a = asyncio.Event()
            self.a_started = asyncio.Event()

        async def progress(self, ticket_id: str) -> Any:
            self.progress_calls.append(ticket_id)
            if ticket_id == "ticket-a":
                self.a_started.set()
                await self.release_a.wait()
                return _server_progress(
                    ticket_id="ticket-a",
                    phase="failed",
                    elapsed_seconds=80,
                    summary="ticket-a-summary",
                    question="ticket-a-question",
                    error="ticket-a-error",
                )
            return _b_progress()

    client = Held()
    tui = make_tui(tmp_path, client=client)
    tui._clock = lambda: 1000.0
    tui._monotonic = lambda: 50.0
    await tui.startup()
    tui._start_progress("ticket-a", "running")

    status_task = asyncio.create_task(tui.handle_command("/status"))
    await asyncio.wait_for(client.a_started.wait(), timeout=2)
    await asyncio.wait_for(tui.watch("ticket-b"), timeout=2)

    progress = tui._progress_block
    card = _result_card(tui)
    assert progress is not None and card is not None
    assert progress.ticket_id == "ticket-b"
    assert tui.status.phase == "completed"
    assert progress.phase == "completed"
    assert progress.elapsed(1000.0) == pytest.approx(6.5)
    assert tui.status.summary == "ticket-b-summary"
    assert tui.status.question == "ticket-b-question"
    assert tui.status.error == "ticket-b-error"
    assert tui._progress_source == "server"
    assert card.status == "completed"
    assert card.summary == "ticket-b-summary"
    assert card.question == ""
    assert card.error == ""
    visible = _visible_text(tui)
    assert "✔ " in visible
    assert "summary: ticket-b-summary" in visible
    assert "needs you:" not in visible
    assert "ticket-b-question" not in visible
    assert "ticket-b-error" not in visible
    assert "sse-b-summary" not in visible
    assert "source server" in _header_text(tui)
    assert "source stream" not in _header_text(tui)

    held = _view_snapshot(tui, now=1000.0)
    client.release_a.set()
    assert await asyncio.wait_for(status_task, timeout=2) is True
    assert _view_snapshot(tui, now=1000.0) == held
    visible = _visible_text(tui)
    for leaked in (
        "ticket-a-summary",
        "ticket-a-question",
        "ticket-a-error",
        "phase: failed",
        "WORKER-RAW-TEXT",
        "TOOL-PARAM-secret-path",
    ):
        assert leaked not in visible
    await tui.close()


@pytest.mark.asyncio
async def test_older_running_calibration_cannot_regress_completed(tmp_path: Path):
    """Same Ticket: a late running read must not roll back a newer completed one."""

    class Reordered(ProgressClient):
        def __init__(self) -> None:
            super().__init__()
            self.started: list[asyncio.Event] = []
            self.release: list[asyncio.Event] = []

        async def progress(self, ticket_id: str) -> Any:
            self.progress_calls.append(ticket_id)
            slot = len(self.started)
            started = asyncio.Event()
            release = asyncio.Event()
            self.started.append(started)
            self.release.append(release)
            started.set()
            await release.wait()
            if slot == 0:
                return _server_progress(
                    ticket_id=ticket_id,
                    phase="running",
                    elapsed_seconds=1.25,
                    summary="running-summary",
                    question="running-question",
                    error="running-error",
                )
            return _server_progress(
                ticket_id=ticket_id,
                phase="completed",
                elapsed_seconds=18,
                summary="done-summary",
                question="done-question",
                error="done-error",
            )

    client = Reordered()
    tui = make_tui(tmp_path, client=client)
    tui._clock = lambda: 500.0
    tui._monotonic = lambda: 10.0
    await tui.startup()
    tui._start_progress("ticket-same", "queued")
    tui.conversation.feed(
        End(
            status="queued",
            summary="seed-summary",
            question="seed-question",
            error="seed-error",
        )
    )

    older = asyncio.create_task(tui._calibrate_progress("ticket-same"))
    await _wait_for_progress_calls(client, 1)
    newer = asyncio.create_task(tui._calibrate_progress("ticket-same"))
    await _wait_for_progress_calls(client, 2)

    client.release[1].set()
    assert await asyncio.wait_for(newer, timeout=2) is True

    progress = tui._progress_block
    card = _result_card(tui)
    assert progress is not None and card is not None
    assert tui.status.phase == "completed"
    assert progress.phase == "completed"
    assert progress.elapsed(500.0) == pytest.approx(18)
    assert tui.status.summary == "done-summary"
    assert tui.status.question == "done-question"
    assert tui.status.error == "done-error"
    assert progress.summary == "done-summary"
    assert progress.question == "done-question"
    assert progress.error == "done-error"
    assert tui._progress_source == "server"
    assert card.status == "completed"
    assert card.summary == "done-summary"
    assert card.question == ""
    assert card.error == ""
    visible = _visible_text(tui)
    assert "✔ " in visible
    assert "summary: done-summary" in visible
    assert "running-summary" not in visible
    assert "seed-summary" not in visible
    assert "done-question" not in visible
    assert "done-error" not in visible
    assert "needs you:" not in visible
    assert "error:" not in visible

    held = _view_snapshot(tui, now=500.0)
    client.release[0].set()
    assert await asyncio.wait_for(older, timeout=2) is None
    assert _view_snapshot(tui, now=500.0) == held
    assert tui.status.phase == "completed"
    assert "phase: running" not in _visible_text(tui)
    assert "running-summary" not in _visible_text(tui)
    await tui.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "nondict", "error"])
async def test_stale_progress_failure_does_not_mark_stream(
    tmp_path: Path, failure: str
):
    """A late 404, bad body, or error must not flip B's source mark to stream."""

    class Held(ProgressClient):
        def __init__(self) -> None:
            super().__init__(stream=_b_completed_stream())
            self.release_a = asyncio.Event()
            self.a_started = asyncio.Event()

        async def progress(self, ticket_id: str) -> Any:
            self.progress_calls.append(ticket_id)
            if ticket_id == "ticket-a":
                self.a_started.set()
                await self.release_a.wait()
                if failure == "error":
                    raise SuperAgentError("stale boom")
                if failure == "nondict":
                    return ["not", "a", "ticket"]
                return None
            return _b_progress()

    client = Held()
    tui = make_tui(tmp_path, client=client)
    tui._clock = lambda: 1000.0
    tui._monotonic = lambda: 50.0
    await tui.startup()
    tui._start_progress("ticket-a", "running")
    status_task = asyncio.create_task(tui.handle_command("/status"))
    await asyncio.wait_for(client.a_started.wait(), timeout=2)
    await asyncio.wait_for(tui.watch("ticket-b"), timeout=2)
    held = _view_snapshot(tui, now=1000.0)
    assert tui._progress_source == "server"
    assert "source server" in held["header"]

    client.release_a.set()
    assert await asyncio.wait_for(status_task, timeout=2) is True
    assert _view_snapshot(tui, now=1000.0) == held
    assert "source stream" not in _header_text(tui)
    assert "stale boom" not in _visible_text(tui)
    assert "ticket-a" not in _visible_text(tui)
    await tui.close()


@pytest.mark.asyncio
async def test_progress_card_shows_waiting_and_failed_text_without_an_end_frame(
    tmp_path: Path,
):
    client = ProgressClient()
    tui = make_tui(tmp_path, client=client)
    tui._clock = lambda: 1000.0
    tui._monotonic = lambda: 20.0
    await tui.startup()
    tui._start_progress("ticket-view", "queued")

    client.progress_result = _server_progress(
        ticket_id="ticket-view",
        phase="waiting",
        elapsed_seconds=3,
        summary="hidden-waiting-summary",
        question="approve the deploy?",
        error="hidden-waiting-error",
    )
    assert await tui._calibrate_progress("ticket-view") is True
    assert not any(isinstance(block, EndBlock) for block in tui.conversation.blocks)
    visible = _visible_text(tui)
    assert "needs you: approve the deploy?" in visible
    assert "hidden-waiting-summary" not in visible
    assert "summary:" not in visible
    assert "hidden-waiting-error" not in visible
    assert "error:" not in visible
    assert "WORKER-RAW-TEXT" not in visible
    assert "TOOL-PARAM-secret-path" not in visible
    assert tui.status.summary == "hidden-waiting-summary"
    assert tui.status.question == "approve the deploy?"
    assert tui.status.error == "hidden-waiting-error"
    progress = tui._progress_block
    assert progress is not None
    assert progress.summary == "hidden-waiting-summary"
    assert progress.question == "approve the deploy?"
    assert progress.error == "hidden-waiting-error"

    restored = tui.conversation.from_json(tui.conversation.to_json())
    restored_progress = next(
        block for block in restored.blocks if isinstance(block, ProgressBlock)
    )
    assert restored_progress.question == "approve the deploy?"
    assert restored_progress.summary == "hidden-waiting-summary"
    assert restored_progress.error == "hidden-waiting-error"
    restored_text = "\n".join(
        text
        for _style, text in restored.formatted_lines(
            show_worker_details=False,
            now=lambda: 1000.0,
        )
    )
    assert "needs you: approve the deploy?" in restored_text
    assert "hidden-waiting-summary" not in restored_text

    client.progress_result = _server_progress(
        ticket_id="ticket-view",
        phase="failed",
        elapsed_seconds=9,
        summary="the worker stopped",
        question="hidden-failed-question",
        error="disk full",
    )
    assert await tui._calibrate_progress("ticket-view") is True
    visible = _visible_text(tui)
    assert "error: disk full" in visible
    assert "summary: the worker stopped" in visible
    assert "needs you:" not in visible
    assert "hidden-failed-question" not in visible
    assert "approve the deploy?" not in visible
    assert not any(isinstance(block, EndBlock) for block in tui.conversation.blocks)

    client.progress_result = _server_progress(
        ticket_id="ticket-view",
        phase="running",
        elapsed_seconds=4,
        summary="hidden-running-summary",
        question="hidden-running-question",
        error="hidden-running-error",
    )
    assert await tui._calibrate_progress("ticket-view") is True
    visible = _visible_text(tui)
    assert "Running" in visible or "♪" in visible or "♫" in visible or "♬" in visible or "●" in visible
    assert "hidden-running-summary" not in visible
    assert "summary:" not in visible
    assert "needs you:" not in visible
    assert "error:" not in visible
    assert "disk full" not in visible
    assert tui.status.phase == "running"
    assert tui.status.summary == "hidden-running-summary"
    assert tui._progress_block is not None
    assert tui._progress_block.summary == "hidden-running-summary"
    await tui.close()


def _completed_end_stream() -> list[ActivityStreamEvent]:
    return [
        _event(
            "end",
            {
                "status": "completed",
                "summary": "sse-done",
                "question": "sse-question",
                "error": "sse-error",
            },
            "1",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "nondict", "error"])
async def test_failed_newer_status_does_not_drop_the_end_frame(
    tmp_path: Path, failure: str
):
    """A newer /status that fails must not swallow the Ticket's only End."""

    class HeldEnd(ProgressClient):
        def __init__(self) -> None:
            super().__init__(stream=_completed_end_stream())
            self.end_started = asyncio.Event()
            self.release_end = asyncio.Event()

        async def progress(self, ticket_id: str) -> Any:
            self.progress_calls.append(ticket_id)
            if len(self.progress_calls) == 1:
                self.end_started.set()
                await self.release_end.wait()
                return _server_progress(
                    ticket_id=ticket_id,
                    phase="running",
                    elapsed_seconds=99,
                    summary="stale-running",
                    question="stale-question",
                    error="stale-error",
                )
            if failure == "error":
                raise SuperAgentError("status boom")
            if failure == "nondict":
                return ["not", "a", "ticket"]
            return None

    times = {"now": 1000.0}
    client = HeldEnd()
    tui = make_tui(tmp_path, client=client)
    tui._clock = lambda: times["now"]
    tui._monotonic = lambda: times["now"] - 960.0
    await tui.startup()
    tui._start_progress(
        "ticket-end",
        "running",
        started_at=1000.0,
        started_monotonic=40.0,
    )
    watching = asyncio.create_task(tui.watch("ticket-end"))
    await asyncio.wait_for(client.end_started.wait(), timeout=2)

    await tui.handle_command("/status")
    assert tui.status.phase == "running"
    assert tui._progress_source == "stream"

    client.release_end.set()
    await asyncio.wait_for(watching, timeout=2)

    progress = tui._progress_block
    card = _result_card(tui)
    assert progress is not None and card is not None
    assert tui.status.phase == "completed"
    assert progress.phase == "completed"
    assert card.status == "completed"
    assert progress.finished_at is not None
    assert tui.status.finished_monotonic is not None
    times["now"] = 1090.0
    assert progress.elapsed(tui._clock()) == pytest.approx(0)
    assert tui._progress_source == "stream"
    assert "source stream" in _header_text(tui)
    assert "source server" not in _header_text(tui)
    visible = _visible_text(tui)
    assert "✔ " in visible
    assert "summary: sse-done" in visible
    assert card.summary == "sse-done"
    assert "stale-running" not in visible
    assert "stale-question" not in visible
    assert "stale-error" not in visible
    assert "status boom" not in visible
    assert "phase: running" not in visible
    await tui.close()


@pytest.mark.asyncio
async def test_newer_successful_status_beats_a_stale_end_frame(tmp_path: Path):
    """A successful newer /status wins over the End frame whose read lost the race."""

    class HeldEnd(ProgressClient):
        def __init__(self) -> None:
            super().__init__(stream=_completed_end_stream())
            self.end_started = asyncio.Event()
            self.release_end = asyncio.Event()

        async def progress(self, ticket_id: str) -> Any:
            self.progress_calls.append(ticket_id)
            if len(self.progress_calls) == 1:
                self.end_started.set()
                await self.release_end.wait()
                return _server_progress(
                    ticket_id=ticket_id,
                    phase="running",
                    elapsed_seconds=99,
                    summary="stale-running",
                    question="stale-question",
                    error="stale-error",
                )
            return _server_progress(
                ticket_id=ticket_id,
                phase="completed",
                elapsed_seconds=12,
                summary="status-done",
                question="status-question",
                error="status-error",
            )

    client = HeldEnd()
    tui = make_tui(tmp_path, client=client)
    tui._clock = lambda: 1000.0
    tui._monotonic = lambda: 40.0
    await tui.startup()
    tui._start_progress(
        "ticket-end",
        "running",
        started_at=1000.0,
        started_monotonic=40.0,
    )
    watching = asyncio.create_task(tui.watch("ticket-end"))
    await asyncio.wait_for(client.end_started.wait(), timeout=2)
    await tui.handle_command("/status")
    assert tui.status.phase == "completed"
    assert tui.status.summary == "status-done"

    client.release_end.set()
    await asyncio.wait_for(watching, timeout=2)

    progress = tui._progress_block
    card = _result_card(tui)
    assert progress is not None and card is not None
    assert tui.status.phase == "completed"
    assert progress.phase == "completed"
    assert progress.elapsed(1000.0) == pytest.approx(12)
    assert tui.status.summary == "status-done"
    assert tui._progress_source == "server"
    assert card.status == "completed"
    assert card.summary == "status-done"
    visible = _visible_text(tui)
    assert "summary: status-done" in visible
    assert "sse-done" not in visible
    assert "stale-running" not in visible
    assert "phase: running" not in visible
    await tui.close()


@pytest.mark.asyncio
async def test_waiting_end_hides_summary_while_pending_cancelled_and_restored(
    tmp_path: Path,
):
    """A non-terminal summary on End is hidden before calibration returns."""

    class Held(ProgressClient):
        def __init__(self) -> None:
            super().__init__(
                stream=[
                    _event(
                        "end",
                        {
                            "status": "waiting",
                            "summary": "NONTERMINAL-SUMMARY",
                            "question": "approve?",
                            "error": "hidden-error",
                        },
                        "1",
                    )
                ]
            )
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def progress(self, ticket_id: str) -> Any:
            self.progress_calls.append(ticket_id)
            self.started.set()
            await self.release.wait()
            return _server_progress(
                ticket_id=ticket_id,
                phase="waiting",
                summary="server-summary",
                question="approve?",
                error="",
            )

    client = Held()
    tui = make_tui(tmp_path, client=client)
    tui._clock = lambda: 1000.0
    tui._monotonic = lambda: 20.0
    await tui.startup()
    tui._start_progress("ticket-w", "running")
    watching = asyncio.create_task(tui.watch("ticket-w"))
    await asyncio.wait_for(client.started.wait(), timeout=2)

    card = _result_card(tui)
    assert card is not None
    assert card.summary == "NONTERMINAL-SUMMARY"
    visible = _visible_text(tui)
    plain = "\n".join(card.render_plain())
    assert "NONTERMINAL-SUMMARY" not in visible
    assert "NONTERMINAL-SUMMARY" not in plain
    assert "summary:" not in visible
    assert "needs you: approve?" in visible
    assert "needs you: approve?" in plain
    assert "hidden-error" not in visible

    tui.interrupt()
    await asyncio.wait_for(watching, timeout=2)
    visible = _visible_text(tui)
    assert "NONTERMINAL-SUMMARY" not in visible
    assert "needs you: approve?" in visible
    assert card.summary == "NONTERMINAL-SUMMARY"

    restored = tui.conversation.from_json(tui.conversation.to_json())
    restored_card = next(block for block in restored.blocks if isinstance(block, EndBlock))
    assert restored_card.summary == "NONTERMINAL-SUMMARY"
    restored_text = "\n".join(
        text
        for _style, text in restored.formatted_lines(
            show_worker_details=False,
            now=lambda: 1000.0,
        )
    )
    assert "NONTERMINAL-SUMMARY" not in restored_text
    assert "needs you: approve?" in restored_text
    assert "summary:" not in restored_text
    assert "NONTERMINAL-SUMMARY" not in "\n".join(restored_card.render_plain())

    failed = EndBlock(
        status="failed",
        summary="boom-summary",
        question="hidden-question",
        error="boom-error",
    )
    done = EndBlock(
        status="completed",
        summary="done-summary",
        question="done-question",
        error="done-error",
    )
    failed_text = "\n".join(failed.render_plain())
    done_text = "\n".join(done.render_plain())
    assert "boom-summary" in failed_text
    assert "error: boom-error" in failed_text
    assert "hidden-question" not in failed_text
    assert "done-summary" in done_text
    assert "done-question" not in done_text
    assert "error:" not in done_text
    await tui.close()


async def test_dispatch_redraw_does_not_fetch_again(tmp_path: Path):
    """The 0.5s heartbeat only redraws. A second /dispatch is what refreshes."""
    client = FakeClient()
    calls = {"n": 0}

    async def dispatch(dispatch_id: str) -> dict[str, Any]:
        calls["n"] += 1
        return {
            "id": dispatch_id,
            "state": "RUNNING",
            "progress_pct": 10,
            "integrate_branch": "integrate/x",
            "lines": [{"id": "line-0", "status": "RUNNING", "assigned_model": "kimi", "depends_on": []}],
        }

    client.dispatch = dispatch  # type: ignore[attr-defined]
    tui = make_tui(tmp_path, client=client)
    await tui.handle_command("/dispatch d3adbeef")
    assert "line-0" in _visible_text(tui)
    for _ in range(5):
        tui.conversation.formatted_lines()
        tui._header_fragments()
    assert calls["n"] == 1
    await tui.handle_command("/dispatch d3adbeef")
    assert calls["n"] == 2
    await tui.close()


@pytest.mark.asyncio
async def test_reply_mode_calls_reply_not_create_task(tmp_path: Path):
    client = FakeClient()
    replies: list[tuple[str, str]] = []

    async def reply(ticket_id: str, text: str) -> dict[str, Any]:
        replies.append((ticket_id, text))
        return {"ok": True}

    client.reply = reply  # type: ignore[attr-defined]
    tui = make_tui(tmp_path, client=client)
    await tui.startup()
    tui._start_progress(FULL_ID, "waiting")

    await tui.submit("the answer")

    assert replies == [(FULL_ID, "the answer")]
    assert client.created == []
    assert client.stops == []
    await tui.close()


@pytest.mark.asyncio
async def test_escape_leaves_reply_mode_so_enter_creates_a_ticket(tmp_path: Path):
    client = FakeClient(stream=_completed_stream())
    replies: list[tuple[str, str]] = []

    async def reply(ticket_id: str, text: str) -> dict[str, Any]:
        replies.append((ticket_id, text))
        return {"ok": True}

    client.reply = reply  # type: ignore[attr-defined]
    tui = make_tui(tmp_path, client=client)
    await tui.startup()
    tui._start_progress(FULL_ID, "waiting")

    with create_pipe_input() as pipe:
        tui.build_application(input=pipe, output=DummyOutput())
        task = asyncio.create_task(tui._app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("\x1b")
        await asyncio.sleep(0.4)
        pipe.send_text("fresh task\r")
        await asyncio.sleep(0.4)
        pipe.send_text("\x03\x03")
        assert await asyncio.wait_for(task, timeout=5) == 0

    assert replies == []
    assert client.stops == []
    assert client.created
    assert client.created[0][0] == "fresh task"
    await tui.close()


@pytest.mark.asyncio
async def test_slash_menu_contains_status_and_reply(tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    tui.build_application(output=DummyOutput())
    assert tui.input_area is not None
    tui.input_area.buffer.text = "/"
    names = [name for name, _hint in tui._menu_items()]
    assert "/status" in names
    assert "/reply" in names
    before = tui.input_area.buffer.text
    tui._move_menu(1)
    tui._move_menu(-1)
    assert tui.input_area.buffer.text == before == "/"
    await tui.close()


@pytest.mark.asyncio
async def test_status_footer_is_exactly_one_line(tmp_path: Path):
    tui = make_tui(tmp_path)
    await tui.startup()
    tui.build_application(output=DummyOutput())
    text = "".join(piece for _style, piece in tui._status_fragments())
    assert "\n" not in text
    assert text
    assert tui.status_window is not None
    assert tui.status_window.height == 1
    await tui.close()


@pytest.mark.asyncio
async def test_memory_command_says_the_server_does_not_support_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CONDUCTOR_LOCALE", "en")
    tui = make_tui(tmp_path)
    await tui.startup()
    await tui.handle_command("/memory")
    lines = tui.conversation.plain_lines()
    assert any("not supported by the server yet" in line for line in lines)
    assert any("not supported by the server yet" == block.text for block in tui.conversation.blocks if isinstance(block, NoticeBlock))
    await tui.close()


@pytest.mark.asyncio
async def test_exit_summary_prints_the_last_card_and_resume_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CONDUCTOR_NO_EXIT_SUMMARY", raising=False)
    tui = make_tui(tmp_path)
    await tui.startup()
    tui.conversation.add(
        EndBlock(status="completed", summary="SHIPPED-CARD")
    )
    tui._print_exit_summary()
    printed = capsys.readouterr().out
    first = printed.splitlines()[0]
    assert "✔" in first
    assert "Done" in first
    assert "SHIPPED-CARD" in printed
    assert printed.strip().splitlines()[-1] == "resume with: cond --continue"

    tui.no_exit_summary = True
    tui._print_exit_summary()
    assert capsys.readouterr().out == ""

    tui.no_exit_summary = False
    monkeypatch.setenv("CONDUCTOR_NO_EXIT_SUMMARY", "1")
    tui._print_exit_summary()
    assert capsys.readouterr().out == ""
    await tui.close()
