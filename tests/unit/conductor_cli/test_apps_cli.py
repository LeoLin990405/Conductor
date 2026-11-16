"""N80 L3: ``conductor apps <verb>`` and the ``cond apps`` forwarder.

Everything runs against an in-memory fake of the L2 operator routes
(``httpx.MockTransport``), a fake keychain and a fake operator token. HOME is
a temp directory, ``security`` is a tripwire, and no token is ever printed.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.conductor_cli import apps_cli
from src.conductor_cli.cli import main
from tests.unit._isolated_home import assert_untouched, isolate

ADMIN = "/api/v1/collab/admin/apps"
OPERATOR = "op_" + "a" * 40
SERVICE = "svc_" + "b" * 40
OPERATOR_USER = "user-1"


def fake_token() -> str:
    return "tok_" + secrets.token_hex(20)


def envelope(data: Any = None, *, ok: bool = True, code: str = "ok", **extra: Any) -> dict[str, Any]:
    body = {"ok": ok, "code": code, "data": data if data is not None else {}, "untrusted_fields": [], "redacted": 0}
    body.update(extra)
    return body


class FakeKeychain:
    def __init__(self, fail: Exception | None = None) -> None:
        self.writes: list[tuple[Any, str]] = []
        self.fail = fail

    def write(self, ref: Any, secret: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.writes.append((ref, secret))


class FakeServer:
    """The L2 operator routes, in memory."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.issued: list[str] = []
        self.omit_token = False
        self.refuse_with_token = False
        self.raise_connect = False

    def body(self, request: httpx.Request) -> dict[str, Any]:
        return json.loads(request.content) if request.content else {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_connect:
            raise httpx.ConnectError("refused", request=request)
        path, method = request.url.path, request.method
        if self.refuse_with_token:
            token = fake_token()
            self.issued.append(token)
            return httpx.Response(409, json=envelope({"token": token}, ok=False, code="conflict", message="exists"))
        if path == ADMIN and method == "POST":
            body = self.body(request)
            data: dict[str, Any] = {"app_id": body["app_id"], "scopes": body["scopes"]}
            if not self.omit_token:
                token = fake_token()
                self.issued.append(token)
                data["token"] = token
            return httpx.Response(201, json=envelope(data))
        if path == ADMIN and method == "GET":
            return httpx.Response(200, json=envelope({"apps": [{"app_id": "codex", "token_prefix": "ab12"}]}))
        parts = path[len(ADMIN) + 1 :].split("/")
        app_id, action = parts[0], parts[1]
        if action == "rotate":
            token = fake_token()
            self.issued.append(token)
            return httpx.Response(200, json=envelope({"app_id": app_id, "token": token}))
        if action == "revoke":
            return httpx.Response(200, json=envelope({"app_id": app_id, "revoked": True, "released": 0}))
        if action == "scopes":
            return httpx.Response(200, json=envelope({"app_id": app_id, **self.body(request)}))
        if action == "audit":
            return httpx.Response(200, json=envelope({"app_id": app_id, "rows": []}))
        return httpx.Response(404, json=envelope(ok=False, code="not_found", message="nope"))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home, original, before = isolate(tmp_path, monkeypatch)
    monkeypatch.delenv("CONDUCTOR_URL", raising=False)
    real_run = subprocess.run

    def tripwire(argv, *args, **kwargs):
        first = argv[0] if isinstance(argv, (list, tuple)) and argv else argv
        if first in {"security", "/usr/bin/security"}:
            raise AssertionError("a test reached the real keychain")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", tripwire)
    yield home
    assert_untouched(original, before)


@pytest.fixture
def server(monkeypatch) -> FakeServer:
    fake = FakeServer()
    monkeypatch.setattr(apps_cli, "_transport", lambda: httpx.MockTransport(fake))
    monkeypatch.setattr(apps_cli, "_operator_token", lambda: OPERATOR)
    monkeypatch.setattr(apps_cli, "_service_credential", lambda: (SERVICE, OPERATOR_USER))
    return fake


@pytest.fixture
def keychain(monkeypatch) -> FakeKeychain:
    fake = FakeKeychain()
    monkeypatch.setattr(apps_cli, "_keychain", lambda: fake)
    return fake


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def json_data(text: str) -> dict[str, Any]:
    return json.loads(text)["data"]["data"]


def walk(home: Path) -> set[str]:
    return {str(p.relative_to(home)) for p in home.rglob("*")}


# 1 ---------------------------------------------------------------------------


@pytest.mark.parametrize("json_mode", [True, False])
def test_register_stores_token_in_keychain_and_never_prints_it(capsys, server, keychain, json_mode):
    argv = ["apps", "register", "claude-code", "--scopes", "review,read,claim", "--can-write", "--max-leases", "2"]
    code, out, err = run(capsys, *argv, *(["--json"] if json_mode else []))
    assert code == 0, err

    (request,) = server.requests
    assert request.method == "POST" and request.url.path == ADMIN
    assert request.headers["authorization"] == f"Bearer {OPERATOR}"
    assert json.loads(request.content) == {
        "app_id": "claude-code",
        "scopes": ["claim", "read", "review"],
        "can_write": True,
        "max_leases": 2,
    }

    (token,) = server.issued
    ((ref, secret),) = keychain.writes
    assert secret == token
    assert (ref.service, ref.account) == ("conductor-collab", "claude-code")
    assert str(ref) == "keychain:conductor-collab/claude-code"
    assert token not in out and token not in err

    data = json_data(out) if json_mode else json.loads(out)
    assert "token" not in data
    assert data["token_stored_at"] == "keychain:conductor-collab/claude-code"
    hub = data["mcp_hub"]
    assert hub["applied"] is False
    assert hub["token_ref"] == "keychain:conductor-collab/claude-code"
    assert hub["target"] == "claude"
    assert hub["conductor_mcp_add"].endswith("--target claude")
    assert token not in json.dumps(hub)


def test_register_default_body_is_read_only(capsys, server, keychain):
    code, _, err = run(capsys, "apps", "register", "codex", "--json")
    assert code == 0, err
    assert json.loads(server.requests[0].content) == {"app_id": "codex", "scopes": ["read"]}


# 2 ---------------------------------------------------------------------------


def _forbid_apply(monkeypatch):
    import mcp_hub
    import mcp_hub.apply

    def boom(*_a, **_k):
        raise AssertionError("mcp_hub.apply_all must never be called")

    monkeypatch.setattr(mcp_hub, "apply_all", boom)
    monkeypatch.setattr(mcp_hub.apply, "apply_all", boom)


def test_register_snippet_is_printed_never_applied(capsys, server, keychain, monkeypatch, isolated_home):
    _forbid_apply(monkeypatch)
    before = walk(isolated_home)
    code, _, err = run(capsys, "apps", "register", "codex", "--scopes", "read,claim", "--json")
    assert code == 0, err
    assert walk(isolated_home) == before
    assert not (isolated_home / ".conductor" / "mcp" / "servers.toml").exists()


def test_snippet_verb_makes_no_http_call(capsys, server, monkeypatch, isolated_home):
    _forbid_apply(monkeypatch)
    before = walk(isolated_home)
    code, out, err = run(capsys, "apps", "snippet", "codex", "--json")
    assert code == 0, err
    assert server.requests == []
    assert walk(isolated_home) == before
    hub = json_data(out)["mcp_hub"]
    assert hub["applied"] is False and hub["token_ref"] == "keychain:conductor-collab/codex"
    assert "--target codex" in hub["conductor_mcp_add"]


# 3 ---------------------------------------------------------------------------


def test_rotate_stores_token_and_never_prints_it(capsys, server, keychain):
    code, out, err = run(capsys, "apps", "rotate", "codex", "--json")
    assert code == 0, err
    request = server.requests[0]
    assert (request.method, request.url.path) == ("POST", f"{ADMIN}/codex/rotate")
    (token,) = server.issued
    assert keychain.writes[0][1] == token
    assert token not in out and token not in err
    data = json_data(out)
    assert "token" not in data and data["token_stored_at"] == "keychain:conductor-collab/codex"
    assert "mcp_hub" not in data


@pytest.mark.parametrize("json_mode", [True, False])
def test_rotate_keychain_failure_exits_1_without_token(capsys, server, monkeypatch, json_mode):
    from collab_mcp.client import KeychainError

    monkeypatch.setattr(apps_cli, "_keychain", lambda: FakeKeychain(fail=KeychainError("locked")))
    code, out, err = run(capsys, "apps", "rotate", "codex", *(["--json"] if json_mode else []))
    assert code == 1
    (token,) = server.issued
    assert token not in out and token not in err
    assert "conductor apps rotate codex" in err


# 4 ---------------------------------------------------------------------------


def test_register_without_token_from_server_exits_1(capsys, server, keychain):
    server.omit_token = True
    code, _, err = run(capsys, "apps", "register", "codex", "--json")
    assert code == 1
    assert keychain.writes == []
    assert "did not return a token" in err


# 5 ---------------------------------------------------------------------------


def test_list(capsys, server):
    code, out, err = run(capsys, "apps", "list", "--json")
    assert code == 0, err
    request = server.requests[0]
    assert (request.method, request.url.path) == ("GET", ADMIN)
    assert request.headers["authorization"] == f"Bearer {OPERATOR}"
    assert json_data(out)["apps"][0]["app_id"] == "codex"


def test_revoke(capsys, server):
    code, out, err = run(capsys, "apps", "revoke", "codex", "--json")
    assert code == 0, err
    request = server.requests[0]
    assert (request.method, request.url.path) == ("POST", f"{ADMIN}/codex/revoke")
    assert json_data(out)["revoked"] is True


def test_audit_query_params(capsys, server):
    code, _, err = run(capsys, "apps", "audit", "codex", "--since", "2026-11-01T00:00:00Z", "--limit", "5", "--json")
    assert code == 0, err
    request = server.requests[0]
    assert (request.method, request.url.path) == ("GET", f"{ADMIN}/codex/audit")
    assert dict(request.url.params) == {"since": "2026-11-01T00:00:00Z", "limit": "5"}


def test_scopes_add_remove_body(capsys, server):
    code, _, err = run(
        capsys, "apps", "scopes", "codex", "--add", "report", "--add", "claim", "--remove", "ask", "--json"
    )
    assert code == 0, err
    request = server.requests[0]
    assert (request.method, request.url.path) == ("POST", f"{ADMIN}/codex/scopes")
    assert json.loads(request.content) == {"add": ["claim", "report"], "remove": ["ask"]}


def test_scopes_without_add_or_remove_exits_2(capsys, server):
    code, _, _ = run(capsys, "apps", "scopes", "codex", "--json")
    assert code == 2
    assert server.requests == []


def test_scopes_same_scope_added_and_removed_exits_2(capsys, server):
    code, _, _ = run(capsys, "apps", "scopes", "codex", "--add", "claim", "--remove", "claim", "--json")
    assert code == 2
    assert server.requests == []


# 6 ---------------------------------------------------------------------------


@pytest.mark.parametrize("json_mode", [True, False])
def test_refusal_carrying_token_never_prints_it(capsys, server, keychain, json_mode):
    server.refuse_with_token = True
    code, out, err = run(capsys, "apps", "register", "codex", *(["--json"] if json_mode else []))
    assert code == 1
    (token,) = server.issued
    assert token not in out and token not in err
    assert keychain.writes == []


# 7 ---------------------------------------------------------------------------


@pytest.mark.parametrize("app_id", ["Bad_App", "../x", "1abc", "a" * 33])
def test_invalid_app_id_exits_2_without_http(capsys, server, keychain, app_id):
    code, _, _ = run(capsys, "apps", "register", app_id, "--json")
    assert code == 2
    assert server.requests == []
    assert keychain.writes == []


def test_unknown_scope_is_argparse_error(capsys, server):
    with pytest.raises(SystemExit) as exc:
        main(["apps", "register", "codex", "--scopes", "read,admin"])
    assert exc.value.code == 2
    assert server.requests == []


# 8 ---------------------------------------------------------------------------


def test_scopes_mirror_server_scopes():
    from src.collab.scopes import SCOPES

    assert apps_cli.SCOPES == SCOPES


# 9 ---------------------------------------------------------------------------


def test_cond_apps_forwards_without_prepare(capsys, server, monkeypatch):
    from src.conductor_cli import ops_cli

    def no_prepare(*_a, **_k):
        raise AssertionError("cond apps must not run ops prepare")

    monkeypatch.setattr(ops_cli, "prepare", no_prepare)
    code = ops_cli.main(["apps", "list", "--json"])
    out = capsys.readouterr().out
    assert code == 0
    assert [(r.method, r.url.path) for r in server.requests] == [("GET", ADMIN)]
    assert json_data(out)["apps"][0]["app_id"] == "codex"


def test_apps_is_not_an_ops_parser_verb():
    import argparse

    from src.conductor_cli.ops_cli import build_parser

    choices: set[str] = set()
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            choices |= set(action.choices)
    assert choices, "ops parser should expose subcommands"
    assert "apps" not in choices


# 10 --------------------------------------------------------------------------


def test_non_loopback_url_exits_2_without_http(capsys, server):
    code, _, _ = run(capsys, "--url", "http://conductor.example.com:8000", "apps", "list", "--json")
    assert code == 2
    assert server.requests == []


def test_connect_error_exits_3(capsys, server):
    server.raise_connect = True
    code, _, err = run(capsys, "apps", "list", "--json")
    assert code == 3
    assert "unreachable" in err


def test_no_real_token_file_needed(capsys, server, monkeypatch):
    # Operator routes on a local OSS Conductor may run without a token.
    monkeypatch.setattr(apps_cli, "_operator_token", lambda: None)
    code, _, err = run(capsys, "apps", "list", "--json")
    assert code == 0, err
    assert "authorization" not in server.requests[0].headers
    assert os.environ.get("CONDUCTOR_URL") is None


# 11 --------------------------------------------------------------------------
# The operator credential: the internal service token, as X-Service-Token.


@pytest.mark.parametrize(
    "argv",
    [
        ("apps", "list"),
        ("apps", "register", "codex"),
        ("apps", "scopes", "codex", "--add", "review"),
        ("apps", "rotate", "codex"),
        ("apps", "revoke", "codex"),
        ("apps", "audit", "codex"),
    ],
)
def test_every_admin_call_sends_the_operator_credential(capsys, server, keychain, argv):
    code, out, err = run(capsys, *argv, "--json")
    assert code == 0, err
    (request,) = server.requests
    assert request.headers["x-service-token"] == SERVICE
    assert request.headers["x-user-id"] == OPERATOR_USER
    assert SERVICE not in out and SERVICE not in err


def test_missing_operator_credential_exits_1_without_http(capsys, server, monkeypatch):
    monkeypatch.setattr(apps_cli, "_service_credential", lambda: (None, OPERATOR_USER))
    code, _, err = run(capsys, "apps", "list", "--json")
    assert code == 1
    assert server.requests == []
    assert "INTERNAL_SERVICE_TOKEN" in err
    assert json.loads(err)["errors"][0]["code"] == "unauthorized"


def test_an_echoed_service_token_is_never_printed(capsys, server, monkeypatch):
    def echo(request: httpx.Request) -> httpx.Response:
        server.requests.append(request)
        sent = request.headers["x-service-token"]
        return httpx.Response(401, json=envelope({"seen": sent}, ok=False, code="unauthorized", message=sent))

    monkeypatch.setattr(apps_cli, "_transport", lambda: httpx.MockTransport(echo))
    for json_mode in (True, False):
        code, out, err = run(capsys, "apps", "list", *(["--json"] if json_mode else []))
        assert code == 1
        assert SERVICE not in out and SERVICE not in err


def test_service_credential_prefers_the_environment(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("INTERNAL_SERVICE_TOKEN=from-file\nAUTH_USER_ID=file-user\n", encoding="utf-8")
    monkeypatch.setenv("CONDUCTOR_REPO_ROOT", str(repo))
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "from-env")
    monkeypatch.setenv("AUTH_USER_ID", "env-user")
    assert apps_cli._service_credential() == ("from-env", "env-user")


def test_service_credential_falls_back_to_the_checkout_env(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("INTERNAL_SERVICE_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setenv("CONDUCTOR_REPO_ROOT", str(repo))
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("AUTH_USER_ID", raising=False)
    assert apps_cli._service_credential() == ("from-file", apps_cli.DEFAULT_USER_ID)


def test_service_credential_is_none_without_a_source(monkeypatch, tmp_path):
    monkeypatch.setenv("CONDUCTOR_REPO_ROOT", str(tmp_path / "missing"))
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("AUTH_USER_ID", raising=False)
    assert apps_cli._service_credential() == (None, apps_cli.DEFAULT_USER_ID)


def test_collab_client_sends_the_service_token_on_admin_routes_only():
    from src.collab_mcp.client import CollabClient

    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=envelope({}))

    client = CollabClient(
        "tok_" + "c" * 40,
        transport=httpx.MockTransport(answer),
        service_token=SERVICE,
        user_id=OPERATOR_USER,
    )
    with client:
        client.admin("apps_list")
        client.call("whoami")
        assert SERVICE not in repr(client)
        assert client.scrub({"x": f"a{SERVICE}b"}) == {"x": "a[redacted]b"}
    admin, tool = seen
    assert admin.headers["x-service-token"] == SERVICE
    assert admin.headers["x-user-id"] == OPERATOR_USER
    assert "x-service-token" not in tool.headers and "x-user-id" not in tool.headers


# BYOK_ENCRYPTION_KEY unset on the server -------------------------------------


def _byok_missing(fake: FakeServer):
    def answer(request: httpx.Request) -> httpx.Response:
        fake.requests.append(request)
        return httpx.Response(
            503,
            json=envelope(
                {"missing_setting": "BYOK_ENCRYPTION_KEY"},
                ok=False,
                code="byok_key_missing",
                message="BYOK_ENCRYPTION_KEY is not set on the server",
            ),
        )

    return answer


@pytest.mark.parametrize("verb", ["register", "rotate"])
def test_byok_key_missing_prints_a_hint(capsys, server, keychain, monkeypatch, verb):
    monkeypatch.setattr(apps_cli, "_transport", lambda: httpx.MockTransport(_byok_missing(server)))
    code, out, err = run(capsys, "apps", verb, "codex")
    assert code == apps_cli.EXIT_REFUSED
    assert "byok_key_missing" in err
    assert "BYOK_ENCRYPTION_KEY" in err
    assert "hint:" in err and "openssl rand -hex 32" in err and ".env" in err
    assert f"conductor apps {verb}" in err
    assert keychain.writes == []
    assert out == ""


def test_byok_key_missing_hint_in_json_mode(capsys, server, keychain, monkeypatch):
    monkeypatch.setattr(apps_cli, "_transport", lambda: httpx.MockTransport(_byok_missing(server)))
    code, out, err = run(capsys, "apps", "register", "codex", "--json")
    assert code == apps_cli.EXIT_REFUSED
    (error,) = json.loads(err)["errors"]
    assert error["code"] == "byok_key_missing"
    assert "hint:" in error["message"] and "BYOK_ENCRYPTION_KEY" in error["message"]
