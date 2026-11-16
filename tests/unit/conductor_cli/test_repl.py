from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.conductor_cli import cli as cli_module
from src.conductor_cli import repl as repl_module
from src.conductor_cli.client import CLIError
from src.conductor_cli.cli import build_parser, main, task_create_body
from src.conductor_cli.repl import (
    Repl,
    follow_ticket,
    merge_commands,
    resolve_ticket_id,
)


FULL_ID = "abcdefab-0000-0000-0000-000000000000"


def make_ticket(ticket_id: str = FULL_ID, **overrides):
    ticket = {
        "ticket_id": ticket_id,
        "workspace_id": "home-ws",
        "title": "a task",
        "scope": "manual",
        "status": "queued",
        "worker_provider": "",
        "worker_model": "",
        "summary": "",
        "error": "",
        "attention": None,
        "reports": [],
    }
    ticket.update(overrides)
    return ticket


def codex_daemon(daemon_id: str = "daemon-codex", **overrides) -> dict:
    daemon = {
        "daemon_id": daemon_id,
        "name": "local",
        "provider": "codex",
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    }
    daemon.update(overrides)
    return daemon


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.home: object = {"workspace_id": "home-ws"}
        self.workspaces = {"workspaces": [{"workspace_id": "ws-1", "name": "alpha"}]}
        self.tickets_list: dict = {"tickets": []}
        self.ticket_details: dict[str, dict] = {}
        self.ticket_sequences: dict[str, list[dict]] = {}
        self.daemons: list[dict] = []
        self.models: dict = {"models": {}}
        self.health = {"status": "healthy", "service": "ptc-agent", "version": "0.1.0"}
        self.created: list[dict] = []

    def get(self, path: str, *, params: dict | None = None):
        self.calls.append(("GET", path, params))
        if path == "/health":
            return self.health
        if path == "/api/v1/workspaces":
            return self.workspaces
        if path == "/api/v1/autonomy/tickets":
            return self.tickets_list
        if path == "/api/v1/autonomy/daemons":
            return self.daemons
        if path == "/api/v1/models":
            return self.models
        if path.endswith("/clarification"):
            check_id = path.split("/")[-2]
            return {
                "clarification_id": "clar-1",
                "ticket_id": check_id,
                "status": "active",
                "clarity_score": 0.5,
                "original_prompt": "do work",
                "expanded_request": "do the work in the repo",
                "assumptions": ["repo is AlphaCN"],
                "open_questions": [],
                "confirmed_text": None,
                "created_at": "2026-11-04T00:00:00Z",
            }
        if path.startswith("/api/v1/autonomy/tickets/"):
            ticket_id = path.removeprefix("/api/v1/autonomy/tickets/")
            sequence = self.ticket_sequences.get(ticket_id)
            if sequence is not None:
                return sequence[0] if len(sequence) == 1 else sequence.pop(0)
            detail = self.ticket_details.get(ticket_id)
            if detail is None:
                raise CLIError("HTTP 404 from Conductor")
            return detail
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, json: dict | None = None):
        self.calls.append(("POST", path, json))
        if path == "/api/v1/workspaces/home":
            if isinstance(self.home, Exception):
                raise self.home
            return self.home
        if path == "/api/v1/autonomy/tasks":
            body = dict(json or {})
            self.created.append(body)
            ticket_id = f"00000000-0000-0000-0000-{len(self.created):012d}"
            return {"ticket_id": ticket_id}
        if path.startswith("/api/v1/autonomy/tickets/") and path.endswith("/stop"):
            return {"ok": True}
        if path.startswith("/api/v1/autonomy/tickets/") and path.endswith("/reply"):
            return {"ok": True}
        if path.startswith("/api/v1/autonomy/tickets/") and path.endswith(
            "/clarification/confirm"
        ):
            return {"ok": True}
        raise AssertionError(f"unexpected POST {path}")


def scripted(lines: list[str]):
    queue = list(lines)

    def read() -> str | None:
        return queue.pop(0) if queue else None

    return read


def run_session(
    client: FakeClient, lines: list[str], **kwargs
) -> tuple[int, list[str]]:
    emits: list[str] = []
    session = Repl(
        client,
        read_line=scripted(lines),
        emit=lambda text="", style=None: emits.append(text),
        **kwargs,
    )
    exit_code = session.run()
    return exit_code, emits


# -- task creation body -----------------------------------------------------


def test_task_create_body_matches_tickets_create_shape() -> None:
    assert task_create_body(
        "ws-1", "do work", provider="codex", model="m", daemon="d", title="t"
    ) == {
        "workspace_id": "ws-1",
        "prompt": "do work",
        "worker_provider": "codex",
        "worker_model": "m",
        "worker_daemon_id": "d",
        "title": "t",
    }


def test_repl_plain_line_creates_task_in_home_workspace_and_follows() -> None:
    client = FakeClient()
    client.daemons = [codex_daemon()]
    created_id = "00000000-0000-0000-0000-000000000001"
    client.ticket_sequences[created_id] = [
        make_ticket(created_id, status="queued"),
        make_ticket(created_id, status="completed", summary="done"),
    ]
    exit_code, emits = run_session(client, ["fix the bug", "/exit"])

    assert exit_code == 0
    workspace_call = next(
        call
        for call in client.calls
        if call[0] == "POST" and call[1] == "/api/v1/workspaces/home"
    )
    assert workspace_call is not None
    assert client.created == [
        {
            "workspace_id": "home-ws",
            "prompt": "fix the bug",
            "worker_provider": "codex",
            "worker_model": "",
            "worker_daemon_id": "daemon-codex",
            "title": "fix the bug",
        }
    ]
    assert "[00000000] queued" in emits
    assert "[00000000] completed" in emits
    assert emits[-1] == "bye"


def test_repl_worktree_resolution_uses_configured_root(tmp_path) -> None:
    root = tmp_path / "wt"
    selected = root / "repo"
    selected.mkdir(parents=True)
    client = FakeClient()
    client.daemons = [codex_daemon()]
    created_id = "00000000-0000-0000-0000-000000000001"
    client.ticket_sequences[created_id] = [make_ticket(created_id, status="completed")]

    run_session(client, ["work", "/exit"], cwd=str(selected), worktree_root=str(root))

    assert client.created[0]["cwd"] == str(selected.resolve())


def test_home_workspace_404_falls_back_to_first_workspace() -> None:
    client = FakeClient()
    client.daemons = [codex_daemon()]
    client.home = CLIError("HTTP 404 from Conductor")
    created_id = "00000000-0000-0000-0000-000000000001"
    client.ticket_sequences[created_id] = [make_ticket(created_id, status="completed")]

    run_session(client, ["hi", "/exit"])

    assert client.created[0]["workspace_id"] == "ws-1"
    assert ("GET", "/api/v1/workspaces", None) in client.calls


# -- follow loop -------------------------------------------------------------


def test_follow_prints_transitions_reports_and_summary_deltas() -> None:
    client = FakeClient()
    sleeps: list[float] = []
    emits: list[str] = []
    client.ticket_sequences[FULL_ID] = [
        make_ticket(status="queued"),
        make_ticket(
            status="running",
            worker_provider="codex",
            worker_model="gpt-5",
            summary="doing",
        ),
        make_ticket(
            status="running",
            summary="doing stuff",
            reports=[{"report_id": "r1", "summary": "report one"}],
        ),
        make_ticket(
            status="completed",
            summary="doing stuff",
            reports=[
                {"report_id": "r1", "summary": "report one"},
                {"report_id": "r2", "summary": "report two"},
            ],
        ),
    ]

    final = follow_ticket(
        client,
        FULL_ID,
        poll=2.0,
        sleep=lambda seconds: sleeps.append(seconds),
        emit=lambda text="", style=None: emits.append(text),
    )

    assert final["status"] == "completed"
    assert emits == [
        "[abcdefab] queued",
        "doing",
        "[abcdefab] running (codex/gpt-5)",
        "report one",
        " stuff",
        "report two",
        "[abcdefab] completed",
    ]
    assert sleeps == [2.0, 2.0, 2.0]


def test_follow_stops_when_attention_is_set() -> None:
    client = FakeClient()
    client.ticket_sequences[FULL_ID] = [
        make_ticket(status="running", attention={"kind": "question"})
    ]
    emits: list[str] = []

    final = follow_ticket(
        client,
        FULL_ID,
        sleep=lambda seconds: pytest.fail("should not sleep once terminal"),
        emit=lambda text="", style=None: emits.append(text),
    )

    assert final["attention"] == {"kind": "question"}
    assert emits[0] == "[abcdefab] running"
    assert any("waiting for input" in line for line in emits)


def test_follow_prints_error_on_failure() -> None:
    client = FakeClient()
    client.ticket_sequences[FULL_ID] = [make_ticket(status="failed", error="boom")]
    emits: list[str] = []

    final = follow_ticket(
        client, FULL_ID, emit=lambda text="", style=None: emits.append(text)
    )

    assert final["status"] == "failed"
    assert emits == ["[abcdefab] failed", "error: boom"]


def test_ctrl_c_while_following_detaches_and_keeps_prompting() -> None:
    client = FakeClient()
    client.daemons = [codex_daemon()]
    created_id = "00000000-0000-0000-0000-000000000001"
    client.ticket_sequences[created_id] = [make_ticket(created_id, status="running")]
    interrupted = {"once": False}

    def sleepy(seconds: float) -> None:
        if not interrupted["once"]:
            interrupted["once"] = True
            raise KeyboardInterrupt

    exit_code, emits = run_session(client, ["long task", "/exit"], sleep=sleepy)

    assert exit_code == 0
    assert any("stopped following" in line for line in emits)
    assert emits[-1] == "bye"


# -- slash commands ----------------------------------------------------------


def test_slash_parsing_help_jobs_and_unknown_command() -> None:
    client = FakeClient()
    client.tickets_list = {
        "tickets": [make_ticket(status="completed", title="hello world")]
    }
    exit_code, emits = run_session(client, ["/help", "/jobs 5", "/bogus", "/exit"])

    assert exit_code == 0
    assert any("/review <id>" in line for line in emits)
    assert ("GET", "/api/v1/autonomy/tickets", {"limit": 5}) in client.calls
    assert any("unknown command /bogus" in line for line in emits)


def test_jobs_table_truncates_title_to_fit() -> None:
    client = FakeClient()
    client.tickets_list = {
        "tickets": [
            make_ticket(status="running", worker_provider="codex", title="x" * 100)
        ]
    }
    _, emits = run_session(client, ["/jobs", "/exit"])

    row = next(line for line in emits if line.startswith("abcdefab"))
    assert len(row) <= 80
    assert "..." in row


def test_show_prints_summary_and_error() -> None:
    client = FakeClient()
    client.ticket_details[FULL_ID] = make_ticket(
        status="failed", error="kaput", summary="partial result"
    )
    exit_code, emits = run_session(client, [f"/show {FULL_ID}", "/exit"])

    assert exit_code == 0
    assert any("partial result" in line for line in emits)
    assert any("error: kaput" in line for line in emits)


def test_id_prefix_resolution_against_recent_jobs() -> None:
    first = make_ticket("aaaaaaa1-0000-0000-0000-000000000000")
    second = make_ticket("aaaaaaa2-0000-0000-0000-000000000000")
    recent = [first, second]

    assert resolve_ticket_id(recent, "aaaaaaa1") == first["ticket_id"]
    assert resolve_ticket_id(recent, "aaaaaaa1-0000") == first["ticket_id"]
    with pytest.raises(CLIError, match="ambiguous"):
        resolve_ticket_id(recent, "aaaa")
    assert resolve_ticket_id(recent, "zzzz") == "zzzz"
    with pytest.raises(CLIError, match="required"):
        resolve_ticket_id(recent, "")


def test_follow_command_resolves_short_prefix() -> None:
    client = FakeClient()
    client.tickets_list = {"tickets": [make_ticket()]}
    exit_code, _ = run_session(client, ["/jobs", "/follow abcdefab", "/exit"])

    assert exit_code == 0
    assert ("GET", f"/api/v1/autonomy/tickets/{FULL_ID}", None) in client.calls


def test_stop_and_reply_post_to_ticket_routes() -> None:
    client = FakeClient()
    client.tickets_list = {"tickets": [make_ticket()]}
    exit_code, emits = run_session(
        client, ["/jobs", "/stop abcdefab", "/reply abcdefab ship it", "/exit"]
    )

    assert exit_code == 0
    assert ("POST", f"/api/v1/autonomy/tickets/{FULL_ID}/stop", None) in client.calls
    assert (
        "POST",
        f"/api/v1/autonomy/tickets/{FULL_ID}/reply",
        {"text": "ship it"},
    ) in client.calls


def test_agents_marks_recent_daemons_online() -> None:
    now = datetime.now(timezone.utc)
    client = FakeClient()
    client.daemons = [
        {
            "name": "fresh",
            "provider": "codex",
            "last_seen_at": (now - timedelta(seconds=30)).isoformat(),
        },
        {
            "name": "stale",
            "provider": "codex",
            "last_seen_at": (now - timedelta(seconds=400)).isoformat(),
        },
        {"name": "never", "provider": "claude", "last_seen_at": None},
    ]
    exit_code, emits = run_session(client, ["/agents", "/exit"])

    assert exit_code == 0
    assert any("fresh" in line and "online" in line for line in emits)
    assert any("stale" in line and "offline" in line for line in emits)
    assert any("never" in line and "never seen" in line for line in emits)


def test_models_lists_provider_models() -> None:
    client = FakeClient()
    client.models = {"models": {"codex": {"models": ["gpt-5", "gpt-5.1"]}}}
    exit_code, emits = run_session(client, ["/models", "/exit"])

    assert exit_code == 0
    assert "codex:" in emits
    assert "  gpt-5" in emits


def test_use_changes_default_worker_for_next_task() -> None:
    client = FakeClient()
    client.daemons = [codex_daemon(), codex_daemon("daemon-claude", provider="claude")]
    created_id = "00000000-0000-0000-0000-000000000001"
    client.ticket_sequences[created_id] = [make_ticket(created_id, status="completed")]
    exit_code, emits = run_session(client, ["/use claude/opus", "do thing", "/exit"])

    assert exit_code == 0
    assert any("claude/opus" in line for line in emits)
    assert client.created[0]["worker_provider"] == "claude"
    assert client.created[0]["worker_model"] == "opus"
    assert client.created[0]["worker_daemon_id"] == "daemon-claude"


def test_review_creates_codex_ticket_with_context_and_follows() -> None:
    client = FakeClient()
    client.daemons = [codex_daemon()]
    client.ticket_details[FULL_ID] = make_ticket(
        status="completed", title="Add widget", summary="added the widget"
    )
    created_id = "00000000-0000-0000-0000-000000000001"
    client.ticket_sequences[created_id] = [make_ticket(created_id, status="completed")]
    exit_code, emits = run_session(client, [f"/review {FULL_ID}", "/exit"])

    assert exit_code == 0
    assert len(client.created) == 1
    body = client.created[0]
    assert body["worker_provider"] == "codex"
    assert body["worker_daemon_id"] == "daemon-codex"
    assert body["workspace_id"] == "home-ws"
    assert "deadbeef" in body["prompt"] or "abcdefab" in body["prompt"]
    assert "Add widget" in body["prompt"]
    assert "added the widget" in body["prompt"]
    assert "read-only" in body["prompt"]
    assert ("GET", f"/api/v1/autonomy/tickets/{created_id}", None) in client.calls


def test_merge_prints_commands_without_running_anything() -> None:
    client = FakeClient()
    exit_code, emits = run_session(client, ["/merge feature/x", "/exit"])

    assert exit_code == 0
    assert client.calls == []
    script = "\n".join(emits)
    assert "nothing has been executed or pushed" in script
    assert "git branch -- backup/pre-merge-feature-x-" in script
    assert "git merge --no-ff feature/x" in script
    assert "make lint" in script
    assert "uv run pytest tests/unit/ -q" in script


def test_merge_commands_helper_builds_backup_merge_and_checks() -> None:
    commands = merge_commands("feature/x", stamp="20261103120000")
    assert commands[0] == "git branch -- backup/pre-merge-feature-x-20261103120000"
    assert commands[1] == "git merge --no-ff feature/x"


def test_status_uses_health_endpoint() -> None:
    client = FakeClient()
    exit_code, emits = run_session(client, ["/status", "/exit"])

    assert exit_code == 0
    assert ("GET", "/health", None) in client.calls
    assert any("status: healthy" in line for line in emits)


# -- review context, daemon selection, input parking, merge hardening --------


def test_no_daemon_refuses_to_submit_instead_of_creating_a_failing_ticket() -> None:
    client = FakeClient()
    exit_code, emits = run_session(client, ["do work", "/exit"])

    assert exit_code == 0
    assert client.created == []
    assert any("No local daemon" in line for line in emits)


def test_provider_without_a_matching_daemon_is_refused() -> None:
    client = FakeClient()
    client.daemons = [codex_daemon("daemon-claude", provider="claude")]
    exit_code, emits = run_session(client, ["do work", "/exit"])

    assert exit_code == 0
    assert client.created == []
    assert any("No codex daemon is registered" in line for line in emits)
    assert any("claude" in line for line in emits)


def test_daemon_selection_prefers_a_recently_seen_daemon() -> None:
    now = datetime.now(timezone.utc)
    client = FakeClient()
    client.daemons = [
        codex_daemon("stale", last_seen_at=(now - timedelta(seconds=900)).isoformat()),
        codex_daemon("fresh", last_seen_at=(now - timedelta(seconds=10)).isoformat()),
    ]
    created_id = "00000000-0000-0000-0000-000000000001"
    client.ticket_sequences[created_id] = [make_ticket(created_id, status="completed")]

    run_session(client, ["hi", "/exit"])

    assert client.created[0]["worker_daemon_id"] == "fresh"


def test_stale_daemon_is_used_with_a_warning() -> None:
    now = datetime.now(timezone.utc)
    client = FakeClient()
    client.daemons = [
        codex_daemon("old", last_seen_at=(now - timedelta(seconds=900)).isoformat())
    ]
    created_id = "00000000-0000-0000-0000-000000000001"
    client.ticket_sequences[created_id] = [make_ticket(created_id, status="completed")]

    _, emits = run_session(client, ["hi", "/exit"])

    assert client.created[0]["worker_daemon_id"] == "old"
    assert any("may stay queued" in line for line in emits)


def test_review_preserves_target_workspace_worktree_and_avoids_foreign_worker(
    tmp_path,
) -> None:
    root = tmp_path / "wt"
    selected = root / "target-repo"
    selected.mkdir(parents=True)
    client = FakeClient()
    client.daemons = [codex_daemon()]
    client.ticket_details[FULL_ID] = make_ticket(
        status="completed",
        title="Add widget",
        summary="added the widget",
        workspace_id="ws-b",
        worker_provider="claude",
        worker_model="opus",
        worker_daemon_id="daemon-claude",
        mandate={"cwd": str(selected)},
    )
    created_id = "00000000-0000-0000-0000-000000000001"
    client.ticket_sequences[created_id] = [make_ticket(created_id, status="completed")]

    exit_code, _ = run_session(
        client, [f"/review {FULL_ID}", "/exit"], worktree_root=str(root)
    )

    assert exit_code == 0
    body = client.created[0]
    assert body["workspace_id"] == "ws-b"
    assert body["cwd"] == str(selected.resolve())
    assert body["worker_provider"] == "codex"
    assert body["worker_model"] == ""  # a claude model must not run under codex
    assert body["worker_daemon_id"] == "daemon-codex"  # foreign daemon not reused


def test_review_reuses_target_daemon_and_model_when_provider_matches() -> None:
    client = FakeClient()
    client.daemons = [codex_daemon("daemon-other")]
    client.ticket_details[FULL_ID] = make_ticket(
        status="completed",
        worker_provider="codex",
        worker_model="gpt-5",
        worker_daemon_id="daemon-target",
    )
    created_id = "00000000-0000-0000-0000-000000000001"
    client.ticket_sequences[created_id] = [make_ticket(created_id, status="completed")]

    run_session(client, [f"/review {FULL_ID}", "/exit"])

    body = client.created[0]
    assert body["worker_daemon_id"] == "daemon-target"
    assert body["worker_model"] == "gpt-5"


def test_follow_stops_at_waiting_and_prints_the_report_question() -> None:
    client = FakeClient()
    client.ticket_sequences[FULL_ID] = [
        make_ticket(
            status="waiting",
            reports=[
                {
                    "report_id": "r1",
                    "summary": "needs a decision",
                    "question": "which database?",
                }
            ],
        )
    ]
    emits: list[str] = []

    final = follow_ticket(
        client,
        FULL_ID,
        sleep=lambda seconds: pytest.fail("should not poll once input is needed"),
        emit=lambda text="", style=None: emits.append(text),
    )

    assert final["status"] == "waiting"
    assert "question: which database?" in emits
    assert any("waiting for input" in line for line in emits)
    assert "needs a decision" in emits


def test_show_renders_attention_question() -> None:
    client = FakeClient()
    client.ticket_details[FULL_ID] = make_ticket(
        status="waiting", attention={"question": "which database?"}
    )
    exit_code, emits = run_session(client, [f"/show {FULL_ID}", "/exit"])

    assert exit_code == 0
    assert any(line == "question: which database?" for line in emits)


def test_reply_keeps_apostrophes_and_quotes_as_literal_text() -> None:
    client = FakeClient()
    client.tickets_list = {"tickets": [make_ticket()]}
    run_session(client, ["/jobs", "/reply abcdefab don't change \"the plan\"", "/exit"])

    assert (
        "POST",
        f"/api/v1/autonomy/tickets/{FULL_ID}/reply",
        {"text": 'don\'t change "the plan"'},
    ) in client.calls


def test_home_server_errors_do_not_silently_fall_back_to_a_workspace() -> None:
    client = FakeClient()
    client.daemons = [codex_daemon()]
    client.home = CLIError("HTTP 403 from Conductor")

    exit_code, emits = run_session(client, ["do work", "/exit"])

    assert exit_code == 0
    assert client.created == []
    assert ("GET", "/api/v1/workspaces", None) not in client.calls
    assert any("HTTP 403" in line for line in emits)


def test_merge_commands_quote_shell_metacharacter_refs() -> None:
    dollar = merge_commands("feature/$HOME", stamp="20261103120000")
    assert dollar[1] == "git merge --no-ff 'feature/$HOME'"

    semicolon = merge_commands("feature/x;echo", stamp="20261103120000")
    assert semicolon[1] == "git merge --no-ff 'feature/x;echo'"


def test_merge_commands_reject_invalid_refs() -> None:
    for branch in ("--upload-pack=evil", "bad ref", "a..b", "feature/.hidden"):
        with pytest.raises(CLIError, match="not a valid Git branch"):
            merge_commands(branch, stamp="20261103120000")


def test_plain_emit_sanitizes_terminal_controls(capsys) -> None:
    repl_module._plain_emit("\x1b]52;c;ZXZpbA==\x07safe")

    assert capsys.readouterr().out == "safe\n"


# -- CLI wiring ---------------------------------------------------------------


def test_repl_subcommand_defaults() -> None:
    args = build_parser().parse_args(["repl"])
    assert args.command == "repl"
    assert args.provider == "codex"
    assert args.poll == 2.0
    assert args.worktree_root == "~/.conductor/worktrees"
    assert args.handler is repl_module.run_repl


def test_parser_without_command_defaults_to_repl_namespace() -> None:
    args = build_parser().parse_args([])
    assert args.command is None


def test_bare_conductor_starts_repl(monkeypatch) -> None:
    class DummyClient:
        def close(self) -> None:
            pass

    monkeypatch.setattr(cli_module, "APIClient", lambda **kwargs: DummyClient())
    monkeypatch.setattr(repl_module, "run_repl", lambda args, client: 7)
    assert main([]) == 7


def test_run_repl_wraps_session(monkeypatch) -> None:
    monkeypatch.setattr(repl_module.Repl, "run", lambda self: 0)
    assert repl_module.run_repl(object(), object()) == 0


# -- second review round: explicit overrides, prefix cache, parking, output ---


def test_review_never_inherits_the_session_worker_or_checkout(tmp_path) -> None:
    session_root = tmp_path / "session-wt"
    session_dir = session_root / "session-repo"
    session_dir.mkdir(parents=True)
    client = FakeClient()
    client.daemons = [
        codex_daemon("daemon-codex"),
        codex_daemon("daemon-session", provider="claude"),
    ]
    client.ticket_details[FULL_ID] = make_ticket(
        status="completed",
        workspace_id="ws-target",
        worker_provider="claude",
        worker_model="opus",
        worker_daemon_id="daemon-claude-target",
    )
    created_id = "00000000-0000-0000-0000-000000000001"
    client.ticket_sequences[created_id] = [make_ticket(created_id, status="completed")]

    run_session(
        client,
        [f"/review {FULL_ID}", "/exit"],
        daemon="daemon-session",
        model="session-model",
        cwd=str(session_dir),
        worktree_root=str(session_root),
    )

    body = client.created[0]
    assert body["workspace_id"] == "ws-target"
    assert body["worker_daemon_id"] == "daemon-codex"  # not the session daemon
    assert body["worker_model"] == ""  # not the session model
    assert "cwd" not in body  # not the session checkout


def test_reply_hint_short_id_resolves_right_after_creation() -> None:
    client = FakeClient()
    client.daemons = [codex_daemon()]
    created_id = "00000000-0000-0000-0000-000000000001"
    client.ticket_sequences[created_id] = [
        make_ticket(created_id, status="waiting", attention={"question": "which db?"})
    ]

    exit_code, emits = run_session(client, ["do work", "/reply 00000000 use sqlite", "/exit"])

    assert exit_code == 0
    assert any("waiting for input" in line for line in emits)
    assert (
        "POST",
        f"/api/v1/autonomy/tickets/{created_id}/reply",
        {"text": "use sqlite"},
    ) in client.calls


def test_reply_short_prefix_resolves_in_a_fresh_session() -> None:
    client = FakeClient()
    client.tickets_list = {"tickets": [make_ticket()]}

    run_session(client, ["/reply abcdefab done", "/exit"])

    assert (
        "POST",
        f"/api/v1/autonomy/tickets/{FULL_ID}/reply",
        {"text": "done"},
    ) in client.calls


def test_follow_ignores_answered_report_questions_and_keeps_polling() -> None:
    client = FakeClient()
    client.ticket_sequences[FULL_ID] = [
        make_ticket(
            status="running",
            reports=[{"report_id": "r1", "summary": "asked", "question": "old?"}],
        ),
        make_ticket(
            status="running",
            reports=[{"report_id": "r1", "summary": "asked", "question": "old?"}],
        ),
        make_ticket(status="completed"),
    ]
    sleeps: list[float] = []
    emits: list[str] = []

    final = follow_ticket(
        client,
        FULL_ID,
        poll=1.0,
        sleep=lambda seconds: sleeps.append(seconds),
        emit=lambda text="", style=None: emits.append(text),
    )

    assert final["status"] == "completed"
    assert sleeps == [1.0, 1.0]
    assert not any("waiting for input" in line for line in emits)


def test_output_boundary_drops_carriage_returns(capsys) -> None:
    repl_module._plain_emit("report: complete\rFORGED")

    assert capsys.readouterr().out == "report: completeFORGED\n"


def test_awaiting_clarification_points_at_confirm() -> None:
    client = FakeClient()
    client.ticket_sequences[FULL_ID] = [
        make_ticket(status="awaiting_clarification", attention={"question": "scope?"})
    ]
    emits: list[str] = []

    follow_ticket(
        client,
        FULL_ID,
        sleep=lambda seconds: pytest.fail("should not poll while awaiting input"),
        emit=lambda text="", style=None: emits.append(text),
    )

    assert any("question: scope?" in line for line in emits)
    assert any("/confirm abcdefab" in line for line in emits)


def test_confirm_shows_the_draft_and_posts_the_confirmation() -> None:
    client = FakeClient()
    client.tickets_list = {"tickets": [make_ticket()]}

    exit_code, emits = run_session(client, ["/confirm abcdefab use postgres", "/exit"])

    assert exit_code == 0
    assert any("do the work in the repo" in line for line in emits)
    assert (
        "POST",
        f"/api/v1/autonomy/tickets/{FULL_ID}/clarification/confirm",
        {"text": "use postgres"},
    ) in client.calls


def test_bare_confirm_refuses_to_accept_an_unseen_draft() -> None:
    client = FakeClient()
    client.tickets_list = {"tickets": [make_ticket()]}

    exit_code, emits = run_session(client, ["/confirm abcdefab", "/exit"])

    assert exit_code == 0
    assert any("do the work in the repo" in line for line in emits)
    assert any("bare confirmation would accept" in line for line in emits)
    assert not any(call[:2] == ("POST", f"/api/v1/autonomy/tickets/{FULL_ID}/clarification/confirm") for call in client.calls)


def test_reply_preserves_internal_and_leading_whitespace() -> None:
    client = FakeClient()
    client.tickets_list = {"tickets": [make_ticket()]}

    run_session(client, ["/reply abcdefab  two  spaces ", "/exit"])

    text = next(
        call[2]["text"]
        for call in client.calls
        if call[:2] == ("POST", f"/api/v1/autonomy/tickets/{FULL_ID}/reply")
    )
    assert "two  spaces" in text


def test_review_resolves_a_short_prefix_in_a_fresh_session() -> None:
    client = FakeClient()
    client.daemons = [codex_daemon()]
    target = make_ticket(status="completed")
    client.tickets_list = {"tickets": [target]}
    client.ticket_details[FULL_ID] = target
    created_id = "00000000-0000-0000-0000-000000000001"
    client.ticket_sequences[created_id] = [make_ticket(created_id, status="completed")]

    exit_code, _ = run_session(client, ["/review abcdefab", "/exit"])

    assert exit_code == 0
    assert len(client.created) == 1
    assert ("GET", f"/api/v1/autonomy/tickets/{FULL_ID}", None) in client.calls


def test_reply_preserves_extra_spaces_and_trailing_whitespace() -> None:
    client = FakeClient()
    client.tickets_list = {"tickets": [make_ticket()]}

    run_session(client, ["/reply abcdefab  answer  ", "/exit"])

    text = next(
        call[2]["text"]
        for call in client.calls
        if call[:2] == ("POST", f"/api/v1/autonomy/tickets/{FULL_ID}/reply")
    )
    assert text == " answer  "


def test_reply_accepts_a_tab_separator() -> None:
    client = FakeClient()
    client.tickets_list = {"tickets": [make_ticket()]}

    run_session(client, ["/reply abcdefab\tanswer", "/exit"])

    text = next(
        call[2]["text"]
        for call in client.calls
        if call[:2] == ("POST", f"/api/v1/autonomy/tickets/{FULL_ID}/reply")
    )
    assert text == "answer"


def test_use_unpins_a_daemon_that_does_not_serve_the_new_provider() -> None:
    client = FakeClient()
    client.daemons = [
        codex_daemon("daemon-codex"),
        codex_daemon("daemon-claude", provider="claude"),
    ]
    created_id = "00000000-0000-0000-0000-000000000001"
    client.ticket_sequences[created_id] = [make_ticket(created_id, status="completed")]

    exit_code, emits = run_session(
        client, ["/use claude/opus", "do thing", "/exit"], daemon="daemon-codex"
    )

    assert exit_code == 0
    assert any("unpinning daemon" in line for line in emits)
    assert client.created[0]["worker_daemon_id"] == "daemon-claude"


def test_use_clears_a_daemon_pin_when_the_provider_lookup_fails() -> None:
    client = FakeClient()

    def boom(path, *, params=None):
        raise CLIError("Could not reach Conductor")

    client.get = boom  # type: ignore[method-assign]
    session = Repl(client, daemon="daemon-pinned", emit=lambda *a, **k: None)

    session._cmd_use(["claude/opus"], "/use claude/opus")

    assert session.provider == "claude"
    assert session.model == "opus"
    assert session.daemon is None

def test_archived_tickets_are_terminal_for_the_repl() -> None:
    assert repl_module._terminal({"status": "archived"}) is True
