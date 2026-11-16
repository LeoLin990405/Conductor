from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from src.conductor_cli.cli import build_parser
from src.conductor_cli.client import (
    APIError,
    APIClient,
    CLIError,
    ServerError,
    read_token,
    write_token,
)
from src.conductor_cli.shim import install_shim


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object, object]] = []

    def get(self, path: str, *, params: dict | None = None):
        self.calls.append(("GET", path, params, None))
        return {"ok": True}

    def post(self, path: str, json: dict | None = None):
        self.calls.append(("POST", path, None, json))
        return {"ok": True}

    def patch(self, path: str, json: dict | None = None):
        self.calls.append(("PATCH", path, None, json))
        return {"ok": True}


def test_ticket_create_uses_existing_manual_task_route_and_fields() -> None:
    args = build_parser().parse_args(
        ["tickets", "create", "--workspace", "workspace-1", "--prompt", "do work", "--provider", "codex", "--model", "m", "--daemon", "d"]
    )
    client = FakeClient()
    args.handler(args, client)
    assert client.calls == [
        (
            "POST",
            "/api/v1/autonomy/tasks",
            None,
            {
                "workspace_id": "workspace-1",
                "prompt": "do work",
                "worker_provider": "codex",
                "worker_model": "m",
                "worker_daemon_id": "d",
            },
        )
    ]


def test_ticket_create_validates_and_sends_selected_worktree(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    selected = root / "repo"
    selected.mkdir(parents=True)
    args = build_parser().parse_args(
        [
            "tickets", "create", "--workspace", "w", "--prompt", "do work",
            "--worktree", str(selected), "--worktree-root", str(root),
        ]
    )
    client = FakeClient()

    args.handler(args, client)

    assert client.calls[0][3]["cwd"] == str(selected.resolve())


def test_ticket_create_rejects_worktree_outside_root_before_api_call(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    args = build_parser().parse_args(
        [
            "tickets", "create", "--workspace", "w", "--prompt", "do work",
            "--cwd", str(outside), "--worktree-root", str(root),
        ]
    )
    client = FakeClient()

    with pytest.raises(CLIError, match="under the configured worktree root"):
        args.handler(args, client)

    assert client.calls == []


def test_source_add_defaults_github_secret_name() -> None:
    args = build_parser().parse_args(["sources", "add", "--config", '{"workspace_id":"w"}'])
    client = FakeClient()
    args.handler(args, client)
    assert client.calls[0][1] == "/api/v1/autonomy/sources"
    assert client.calls[0][3] == {
        "kind": "github",
        "config": {"workspace_id": "w", "secret_name": "GITHUB_TOKEN"},
        "enabled": True,
    }


def test_client_attaches_bearer_and_sanitizes_api_errors() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500, json={"detail": "private token echoed"})

    client = APIClient("http://localhost:8000", "sample-token", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CLIError, match="HTTP 500") as error:
            client.get("/api/v1/system/readiness")
        assert "private token" not in str(error.value)
        assert seen[0].headers["Authorization"] == "Bearer sample-token"
    finally:
        client.close()


def test_client_surfaces_the_structured_refusal_and_remediation() -> None:
    """A review/merge refusal reaches the operator with the server's next step."""
    detail = {
        "code": "tests_failed",
        "message": "The merge rehearsal is red; the target branch was not touched.",
        "remediation": "Fix the failing tests on the line branch, then retry.",
        "detail": {"exit_code": 1},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": detail})

    client = APIClient(
        "http://127.0.0.1:8000", "", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(APIError) as error:
            client.post("/api/v1/merges", {})
        assert error.value.status_code == 409
        assert error.value.code == "tests_failed"
        assert error.value.remediation == detail["remediation"]
        assert error.value.detail == {"exit_code": 1}
        rendered = str(error.value)
        assert "target branch was not touched" in rendered
        assert "remediation:" in rendered
        assert detail["remediation"] in rendered
    finally:
        client.close()


def test_client_keeps_the_configured_timeout_when_no_override_is_given() -> None:
    client = APIClient("http://127.0.0.1:8000", "")
    seen: list[dict] = []

    def fake_request(method, target, **kwargs):
        seen.append(kwargs)
        return httpx.Response(200, json={"ok": True})

    client._http.request = fake_request  # type: ignore[method-assign]
    try:
        client.get("/api/v1/health")
        client.get("/api/v1/health", timeout=5.0)
    finally:
        client.close()
    # timeout=None means "disable" in httpx, so it must not be passed at all.
    assert "timeout" not in seen[0]
    assert seen[1]["timeout"] == 5.0


def test_client_surfaces_a_classified_5xx_with_its_remediation() -> None:
    detail = {
        "code": "audit_unavailable",
        "message": "The merge was not attempted because its durable audit record could not be written.",
        "remediation": "Check the database connection, then retry.",
        "detail": {},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": detail})

    client = APIClient(
        "http://127.0.0.1:8000", "", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ServerError) as error:
            client.post("/api/v1/merges", {})
        assert error.value.status_code == 503
        assert error.value.code == "audit_unavailable"
        assert "durable audit record" in str(error.value)
        assert "Check the database connection" in str(error.value)
    finally:
        client.close()


def test_client_does_not_echo_a_partial_5xx_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503, json={"detail": {"code": "x", "message": "private sentinel"}}
        )

    client = APIClient(
        "http://127.0.0.1:8000", "", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ServerError, match="HTTP 503") as error:
            client.get("/api/v1/merges")
        assert "private sentinel" not in str(error.value)
        assert error.value.code is None
    finally:
        client.close()


def test_client_does_not_echo_an_unclassified_5xx_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "private token echoed"})

    client = APIClient(
        "http://127.0.0.1:8000", "", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ServerError, match="HTTP 500") as error:
            client.get("/api/v1/system/readiness")
        assert "private token" not in str(error.value)
        assert error.value.code is None
    finally:
        client.close()


def test_client_keeps_a_plain_string_error_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Ticket not found"})

    client = APIClient(
        "http://127.0.0.1:8000", "", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(APIError, match="Ticket not found") as error:
            client.get("/api/v1/autonomy/tickets/x")
        assert error.value.code is None
    finally:
        client.close()


@pytest.mark.parametrize("url", ["http://example.com", "https://user:pass@example.com", "https://example.com/?x=1"])
def test_client_refuses_untrusted_or_ambiguous_base_urls(url: str) -> None:
    with pytest.raises(CLIError):
        APIClient(url, token="")


def test_client_refuses_absolute_request_urls() -> None:
    client = APIClient("http://127.0.0.1:8000", token="")
    try:
        with pytest.raises(CLIError, match="API paths"):
            client.get("https://example.com/steal")
    finally:
        client.close()


def test_token_creation_is_private_and_never_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    path = tmp_path / "token"
    write_token("secret-value", path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_token(path) == "secret-value"
    with pytest.raises(CLIError, match="already exists"):
        write_token("replacement", path)
    assert read_token(path) == "secret-value"


def test_token_reader_refuses_loose_permissions_and_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    loose = tmp_path / "loose"
    loose.write_text("secret", encoding="utf-8")
    loose.chmod(0o644)
    with pytest.raises(CLIError, match="too loose"):
        read_token(loose)
    secret = tmp_path / "secret"
    secret.write_text("secret", encoding="utf-8")
    secret.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(secret)
    with pytest.raises(CLIError, match="safely open"):
        read_token(link)


def test_cli_json_flag_after_subcommand_and_auth_use_local_handlers(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    monkeypatch.setattr("src.conductor_cli.client.TOKEN_PATH", tmp_path / "token")
    client = APIClient("http://127.0.0.1:8000", token="", transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ready"})))
    # The parser accepts the global switch after the command when invoked through main's normalizer.
    monkeypatch.setattr("src.conductor_cli.cli.APIClient", lambda **kwargs: client)
    from src.conductor_cli.cli import main

    assert main(["status", "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["api_version"] == "v1"
    assert envelope["command"] == "status"
    assert envelope["data"]["status"] == "ready"
    assert envelope["errors"] is None
    assert envelope["exit_code"] == 0


def test_cli_keeps_operation_exit_code_out_of_json_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.conductor_cli import operations
    from src.conductor_cli.cli import main

    monkeypatch.setattr(
        operations,
        "handle_rollback",
        lambda args: {"status": "cancelled", "exit_code": 1},
    )

    assert main(["rollback", "--commit", "a" * 40, "--backup", "/tmp/backup", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    envelope = json.loads(captured.err)
    assert envelope["data"] == {"status": "cancelled"}
    assert envelope["exit_code"] == 1
    assert envelope["errors"]


def test_cli_preserves_server_exit_code_field_in_json_response(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.conductor_cli import cli

    payload = {"status": "ready", "exit_code": 42}
    client = APIClient(
        "http://127.0.0.1:8000",
        token="",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    monkeypatch.setattr(cli, "APIClient", lambda **kwargs: client)

    assert cli.main(["status", "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["data"] == payload
    assert envelope["exit_code"] == 0
    assert envelope["errors"] is None


def test_browser_cli_is_local_and_never_constructs_api_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.conductor_cli import browser, cli

    calls: list[tuple] = []

    def no_api_client(**_kwargs):
        raise AssertionError("browser command must not construct APIClient")

    monkeypatch.setattr(cli, "APIClient", no_api_client)
    monkeypatch.setattr(
        browser.commands,
        "browser_command",
        lambda agent, request, **kwargs: calls.append((agent, request, kwargs)) or {"status": "ready"},
    )

    assert cli.main(["browser", "open", "--agent", "test-agent", "--json"]) == 0

    assert calls == [("test-agent", {"command": "ping"}, {"start": True})]
    assert json.loads(capsys.readouterr().out) == {"ok": True, "data": {"status": "open", "agent": "test-agent"}, "error": None}


def test_browser_import_login_anchors_relative_state_path_to_cli_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.conductor_cli import browser, cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "APIClient", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("APIClient constructed")))
    calls: list[tuple] = []
    monkeypatch.setattr(
        browser.commands,
        "browser_command",
        lambda agent, request, **kwargs: calls.append((agent, request, kwargs)) or {"status": "imported"},
    )

    assert cli.main(
        ["browser", "import-login", "--agent", "test-agent", "--state", "selected.json", "--domain", "example.test", "--json"]
    ) == 0

    assert calls == [
        (
            "test-agent",
            {"command": "import-login", "path": str((tmp_path / "selected.json").absolute()), "domains": ["example.test"]},
            {"timeout": 20.0},
        )
    ]
    assert json.loads(capsys.readouterr().out) == {"ok": True, "data": {"status": "imported"}, "error": None}


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["tickets", "list", "--status", "blocked", "--limit", "7", "--json"],
            [("GET", "/api/v1/autonomy/tickets", {"limit": ["7"], "status": ["blocked"]}, None)],
        ),
        (
            ["tickets", "show", "11111111-2222-4333-8444-555555555555", "--json"],
            [("GET", "/api/v1/autonomy/tickets/11111111-2222-4333-8444-555555555555", {}, None)],
        ),
        (
            ["tickets", "confirm", "11111111-2222-4333-8444-555555555555", "--text", "approved", "--json"],
            [("POST", "/api/v1/autonomy/tickets/11111111-2222-4333-8444-555555555555/clarification/confirm", {}, {"text": "approved"})],
        ),
        (
            ["tickets", "cancel", "11111111-2222-4333-8444-555555555555", "--json"],
            [("POST", "/api/v1/autonomy/tickets/11111111-2222-4333-8444-555555555555/stop", {}, None)],
        ),
        (
            ["tickets", "lines", "--json"],
            [("GET", "/api/v1/autonomy/tickets/lines", {"include_archived": ["false"]}, None)],
        ),
        (
            ["tickets", "lines", "--include-archived", "--json"],
            [("GET", "/api/v1/autonomy/tickets/lines", {"include_archived": ["true"]}, None)],
        ),
        (
            ["tickets", "reply", "11111111-2222-4333-8444-555555555555", "continue", "--json"],
            [("POST", "/api/v1/autonomy/tickets/11111111-2222-4333-8444-555555555555/reply", {}, {"text": "continue"})],
        ),
        (
            ["sources", "list", "--json"],
            [("GET", "/api/v1/autonomy/sources", {}, None)],
        ),
        (
            ["sources", "sync", "source-1", "--json"],
            [("POST", "/api/v1/autonomy/sources/source-1/poll", {}, None)],
        ),
        (
            ["sources", "enable", "source-1", "--json"],
            [("PATCH", "/api/v1/autonomy/sources/source-1", {}, {"enabled": True})],
        ),
        (
            ["sources", "disable", "source-1", "--json"],
            [("PATCH", "/api/v1/autonomy/sources/source-1", {}, {"enabled": False})],
        ),
        (
            ["models", "list", "--json"],
            [("GET", "/api/v1/models", {}, None)],
        ),
        (
            ["agents", "list", "--json"],
            [
                ("GET", "/api/v1/subagents", {}, None),
                ("GET", "/api/v1/native-agents", {}, None),
            ],
        ),
    ],
)
def test_main_dispatches_supported_rest_contracts(
    argv: list[str],
    expected: list[tuple[str, str, dict[str, list[str]], dict | None]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from src.conductor_cli import cli

    seen: list[tuple[str, str, dict[str, list[str]], dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = urlsplit(str(request.url))
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, parsed.path, parse_qs(parsed.query), body))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli,
        "APIClient",
        lambda **kwargs: APIClient(
            base_url=kwargs.get("base_url"), token="", transport=transport
        ),
    )

    assert cli.main(argv) == 0
    assert seen == expected
    assert json.loads(capsys.readouterr().out) is not None


def test_memory_export_streams_jsonl_pages_with_explicit_secret_option(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.conductor_cli import cli

    class ExportClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.pages = [
                {"items": [{"key": "one", "content": "first"}], "next_cursor": "0:1"},
                {"items": [{"key": "two", "content": "second"}], "next_cursor": None},
            ]

        def get(self, path: str, *, params: dict[str, object] | None = None):
            assert path == "/api/v1/memory/export"
            self.calls.append((path, params or {}))
            return self.pages.pop(0)

        def close(self) -> None:
            pass

    client = ExportClient()
    monkeypatch.setattr(cli, "APIClient", lambda **kwargs: client)

    assert cli.main(["memory", "export", "--limit", "1", "--include-secret"]) == 0
    assert client.calls == [
        ("/api/v1/memory/export", {"limit": 1, "include_secret": True}),
        (
            "/api/v1/memory/export",
            {"limit": 1, "include_secret": True, "cursor": "0:1"},
        ),
    ]
    assert capsys.readouterr().out.splitlines() == [
        '{"key":"one","content":"first"}',
        '{"key":"two","content":"second"}',
    ]


def test_installed_shim_is_available_in_minimal_noninteractive_zsh_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("CONDUCTOR_REPO", str(Path.cwd()))
    destination = home / ".local" / "bin" / "conductor"
    installed = install_shim(str(destination))
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }
    result = subprocess.run(
        ["/bin/zsh", "-c", "command -v conductor && conductor --help"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert str(installed) in result.stdout
    assert "tickets" in result.stdout


def test_install_shim_refuses_to_replace_unrelated_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("CONDUCTOR_REPO", str(Path.cwd()))
    destination = tmp_path / "bin" / "conductor"
    destination.parent.mkdir()
    destination.write_text("#!/bin/sh\necho another command\n", encoding="utf-8")

    with pytest.raises(CLIError, match="Refusing to replace an existing command"):
        install_shim(str(destination))

    assert destination.read_text(encoding="utf-8") == "#!/bin/sh\necho another command\n"


def test_install_shim_is_idempotent_and_keeps_zsh_path_block_single(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("CONDUCTOR_REPO", str(Path.cwd()))
    destination = home / ".local" / "bin" / "conductor"

    install_shim(str(destination))
    first_config = (home / ".zshenv").read_text(encoding="utf-8")
    install_shim(str(destination))
    second_config = (home / ".zshenv").read_text(encoding="utf-8")

    assert second_config == first_config
    assert second_config.count("# >>> conductor CLI PATH >>>") == 1
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755


def test_tickets_lines_formats_a_grouped_text_dashboard() -> None:
    from src.conductor_cli import cli

    payload = {
        "lines": [
            {
                "line": {
                    "line_id": "l1",
                    "name": "ob-tickets",
                    "branch": "feat/ob-tickets",
                    "worktree_path": "/wt/ob-tickets",
                },
                "tickets": [
                    {
                        "ticket_id": "t1",
                        "title": "Fix the save button",
                        "status": "running",
                        "owner": "m5",
                        "worker_provider": "codex",
                        "attempts": 1,
                        "updated_at": "2026-11-03T00:00:00Z",
                        "last_activity_at": "2026-11-03T00:05:00Z",
                    }
                ],
                "ticket_count": 1,
                "open_count": 1,
                "last_activity_at": "2026-11-03T00:05:00Z",
            }
        ],
        "ungrouped": {
            "line": None,
            "tickets": [
                {
                    "ticket_id": "t2",
                    "title": "Loose task",
                    "status": "queued",
                    "owner": "cloud",
                    "worker_provider": "",
                    "attempts": 0,
                    "updated_at": "2026-11-03T00:00:00Z",
                    "last_activity_at": None,
                }
            ],
            "ticket_count": 1,
            "open_count": 1,
            "last_activity_at": "2026-11-03T00:00:00Z",
        },
    }

    rows = cli._format_ticket_lines(payload)

    assert rows[0].startswith("ob-tickets (feat/ob-tickets · /wt/ob-tickets)")
    assert "1/1 open" in rows[0]
    assert rows[1].startswith("  [running] Fix the save button — m5 · codex —")
    assert any(row.startswith("Ungrouped —") for row in rows)
    assert any("[queued] Loose task — cloud —" in row for row in rows)


def test_tickets_lines_empty_payload_says_so() -> None:
    from src.conductor_cli import cli

    assert cli._format_ticket_lines({"lines": [], "ungrouped": None}) == [
        "No ticket lines"
    ]


def test_tickets_switch_posts_candidates() -> None:
    client = FakeClient()
    args = build_parser().parse_args(
        ["tickets", "switch", "11111111-2222-4333-8444-555555555555", "--reason", "stall", "--candidate", "kimi/k2", "--candidate", "grok/g4"]
    )
    args.handler(args, client)
    method, path, _, body = client.calls[-1]
    assert (method, path) == ("POST", "/api/v1/autonomy/tickets/11111111-2222-4333-8444-555555555555/switch")
    assert body["reason"] == "stall"
    assert body["candidates"] == [
        {"provider": "kimi", "model": "k2"},
        {"provider": "grok", "model": "g4"},
    ]
