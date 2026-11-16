"""``conductor collab <verb>`` against an in-memory fake of the L2 routes.

Nothing here reaches a network, a keychain or the real HOME: the keychain and
transport seams of ``collab_cli`` are replaced, ``subprocess.run`` refuses the
``security`` binary, and HOME points at ``tmp_path``.
"""

from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from collab_mcp.client import KeychainError

from src.conductor_cli import collab_cli
from src.conductor_cli.cli import main
from tests.unit._isolated_home import assert_untouched, isolate

TOKEN = "tok_" + secrets.token_hex(20)
PREFIX = "/api/v1/collab"


# -- fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home, original, before = isolate(tmp_path, monkeypatch)
    yield home
    assert_untouched(original, before)


@pytest.fixture(autouse=True)
def no_real_keychain(monkeypatch):
    real_run = subprocess.run

    def guarded(argv, *args, **kwargs):
        first = argv[0] if isinstance(argv, (list, tuple)) and argv else argv
        if first in {"security", "/usr/bin/security"}:
            raise AssertionError("a test reached the real keychain")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)
    monkeypatch.delenv("CONDUCTOR_URL", raising=False)
    monkeypatch.delenv("CONDUCTOR_COLLAB_APP", raising=False)


class FakeKeychain:
    def __init__(self, token: str = TOKEN, error: Exception | None = None) -> None:
        self.token = token
        self.error = error
        self.refs: list[str] = []

    def read(self, ref) -> str:
        self.refs.append(str(ref))
        if self.error is not None:
            raise self.error
        return self.token


class FakeServer:
    """Records every request; answers with ``respond`` or an ok envelope."""

    def __init__(self, respond=None) -> None:
        self.requests: list[httpx.Request] = []
        self.respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.path.startswith(PREFIX)
        if self.respond is not None:
            return self.respond(request)
        # A careless server that echoes the bearer back: the CLI must scrub it.
        return envelope(data={"echo": TOKEN, "path": request.url.path}, message=f"hi {TOKEN}")


def envelope(code: str = "ok", *, data: dict | None = None, untrusted=(), message=None, status=None):
    body: dict[str, Any] = {
        "ok": code == "ok",
        "code": code,
        "data": data or {},
        "untrusted_fields": list(untrusted),
        "redacted": 0,
    }
    if message is not None:
        body["message"] = message
    return httpx.Response(status or (200 if code == "ok" else 400), json=body)


@pytest.fixture
def keychain(monkeypatch) -> FakeKeychain:
    fake = FakeKeychain()
    monkeypatch.setattr(collab_cli, "_keychain", lambda: fake)
    return fake


@pytest.fixture
def server(monkeypatch) -> FakeServer:
    fake = FakeServer()
    monkeypatch.setattr(collab_cli, "_transport", lambda: httpx.MockTransport(fake))
    return fake


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def write_json(path: Path, value: Any) -> str:
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


# -- 1. every verb hits its route ---------------------------------------------

REPORT = {"status": "done", "summary": "ok", "evidence": ["tests pass"]}
VERDICT = {"passed": True, "blockers": [], "verdict": "lgtm"}

#: verb -> (argv after the verb, method, path, expected query or body)
CASES: dict[str, tuple[list[str], str, str, dict[str, Any]]] = {
    "whoami": ([], "GET", "/whoami", {}),
    "tasks": (
        ["--all", "--kind", "ticket", "--status", "open", "--status", "claimed", "--mine", "--limit", "5"],
        "GET",
        "/tasks",
        {"claimable": ["false"], "kind": ["ticket"], "status": ["open", "claimed"], "mine": ["true"], "limit": ["5"]},
    ),
    "task": (["t:abc-123"], "GET", "/tasks/t:abc-123", {}),
    "propose": (
        ["--title", "Fix", "--body", "details", "--repo", "conductor", "--write-scope", "src/a.py", "--suggest-app-claimable"],
        "POST",
        "/tasks",
        {"title": "Fix", "body": "details", "repo": "conductor", "write_scope": ["src/a.py"], "suggest_app_claimable": True},
    ),
    "claim": (["t:abc", "--revision", "3", "--lease-seconds", "600", "--agent", "gpt"], "POST", "/tasks/t:abc/claim",
              {"revision": 3, "agent": "gpt", "lease_seconds": 600}),
    "heartbeat": (["L1", "--progress", "half"], "POST", "/leases/L1/heartbeat", {"progress": "half"}),
    "release": (["L1", "--reason", "blocked", "--note", "n"], "POST", "/leases/L1/release", {"reason": "blocked", "note": "n"}),
    "report": (["L1", "--file", "@report"], "POST", "/leases/L1/report", {"report": REPORT}),
    "handoff": (
        ["L1", "--reason", "quota", "--to", "router", "--conversation-app", "codex", "--conversation-session", "s1"],
        "POST",
        "/leases/L1/handoff",
        {"reason": "quota", "to": "router", "conversation_ref": {"app": "codex", "session_id": "s1"}},
    ),
    "handoffs": (["--limit", "2"], "GET", "/handoffs", {"limit": ["2"]}),
    "accept": (["H9"], "POST", "/handoffs/H9/accept", {}),
    "ask": (["app:kimi", "--text", "ping", "--about", "t:x"], "POST", "/messages", {"to": "app:kimi", "text": "ping", "about": "t:x"}),
    "inbox": (["--peek", "--limit", "1"], "GET", "/messages/inbox", {"peek": ["true"], "limit": ["1"]}),
    "review-request": (["L1", "--commit", "abc123", "--reviewer", "any"], "POST", "/reviews",
                       {"lease_id": "L1", "commit": "abc123", "reviewer": "any"}),
    "reviews": (["--all"], "GET", "/reviews", {"for_me": ["false"]}),
    "review-submit": (["R7", "--file", "@verdict"], "POST", "/reviews/R7/verdict", {"verdict": VERDICT}),
}


def test_cases_cover_every_verb() -> None:
    assert set(CASES) == set(collab_cli.VERB_TOOLS)


@pytest.mark.parametrize("verb", sorted(CASES))
def test_verb_maps_to_route(verb, keychain, server, capsys, tmp_path) -> None:
    tail, method, path, expected = CASES[verb]
    files = {"@report": write_json(tmp_path / "report.json", REPORT),
             "@verdict": write_json(tmp_path / "verdict.json", VERDICT)}
    tail = [files.get(item, item) for item in tail]

    code, out, err = run(capsys, "collab", verb, *tail, "--app", "codex", "--json")

    assert code == 0, err
    [request] = server.requests
    assert request.method == method
    assert request.url.path == PREFIX + path
    if method == "GET":
        query: dict[str, list[str]] = {}
        for key, value in request.url.params.multi_items():
            query.setdefault(key, []).append(value)
        assert query == expected
        assert request.content == b""
    else:
        assert json.loads(request.content) == expected
    frame = json.loads(out)
    assert frame["api_version"] == "v1" and frame["exit_code"] == 0
    assert TOKEN not in out + err


# -- 2. token reference and app selection ---------------------------------------


def test_default_token_ref_is_per_app(keychain, server, capsys) -> None:
    assert run(capsys, "collab", "whoami", "--app", "codex")[0] == 0
    assert keychain.refs == ["keychain:conductor-collab/codex"]


def test_token_ref_overrides_default(keychain, server, capsys) -> None:
    code, _, _ = run(capsys, "collab", "whoami", "--app", "codex", "--token-ref", "keychain:other-svc/acct")
    assert code == 0
    assert keychain.refs == ["keychain:other-svc/acct"]


def test_app_from_env(keychain, server, capsys, monkeypatch) -> None:
    monkeypatch.setenv("CONDUCTOR_COLLAB_APP", "kimi")
    assert run(capsys, "collab", "whoami")[0] == 0
    assert keychain.refs == ["keychain:conductor-collab/kimi"]


def test_missing_app_is_usage_error(keychain, server, capsys) -> None:
    code, _, err = run(capsys, "collab", "whoami", "--json")
    assert code == 2
    assert json.loads(err)["errors"][0]["code"] == "invalid_input"
    assert keychain.refs == [] and server.requests == []


# -- 3. refusal vs ok -------------------------------------------------------------


@pytest.mark.parametrize(("code_name", "status"), [("scope_denied", 403), ("stale_revision", 409)])
def test_refusal_is_exit_1_on_stderr(code_name, status, keychain, server, capsys) -> None:
    server.respond = lambda request: envelope(code_name, message="no", status=status)
    code, out, err = run(capsys, "collab", "claim", "t:a", "--revision", "1", "--app", "codex", "--json")
    assert code == 1 and out == ""
    frame = json.loads(err)
    assert frame["api_version"] == "v1"
    assert frame["exit_code"] == 1
    assert frame["errors"][0]["code"] == code_name


def test_ok_is_exit_0_on_stdout(keychain, server, capsys) -> None:
    server.respond = lambda request: envelope(data={"app_id": "codex"})
    code, out, err = run(capsys, "collab", "whoami", "--app", "codex", "--json")
    assert code == 0 and err == ""
    frame = json.loads(out)
    assert frame["errors"] is None
    assert frame["data"]["data"] == {"app_id": "codex"}


# -- 4. an echoed token never reaches the terminal -----------------------------------


@pytest.mark.parametrize("json_flag", [["--json"], []])
@pytest.mark.parametrize("refused", [False, True])
def test_echoed_token_is_scrubbed(json_flag, refused, keychain, server, capsys) -> None:
    def respond(request):
        if refused:
            return envelope("scope_denied", data={"token": TOKEN}, message=f"bad {TOKEN}", status=403)
        return envelope(data={"token": TOKEN, TOKEN: [TOKEN]}, message=TOKEN)

    server.respond = respond
    code, out, err = run(capsys, "collab", "whoami", "--app", "codex", *json_flag)
    assert code == (1 if refused else 0)
    assert TOKEN not in out + err
    assert "[redacted]" in out + err


# -- 5. loopback only ------------------------------------------------------------------


@pytest.mark.parametrize("url", ["http://10.0.0.5:8000", "https://example.com", "http://user:pw@127.0.0.1:8000"])
def test_non_loopback_url_is_refused(url, keychain, server, capsys) -> None:
    code, out, err = run(capsys, "--url", url, "collab", "whoami", "--app", "codex", "--json")
    assert code == 2
    assert server.requests == []
    assert json.loads(err)["errors"][0]["code"] == "invalid_input"
    assert TOKEN not in out + err


def test_non_loopback_env_url_is_refused(keychain, server, capsys, monkeypatch) -> None:
    monkeypatch.setenv("CONDUCTOR_URL", "https://example.com")
    assert run(capsys, "collab", "whoami", "--app", "codex")[0] == 2
    assert server.requests == []


# -- 6. unreachable --------------------------------------------------------------------


def test_connect_error_is_exit_3(keychain, server, capsys) -> None:
    def respond(request):
        raise httpx.ConnectError(f"refused {TOKEN}", request=request)

    server.respond = respond
    code, out, err = run(capsys, "collab", "whoami", "--app", "codex", "--json")
    assert code == 3
    assert json.loads(err)["errors"][0]["code"] == "unreachable"
    assert TOKEN not in out + err


# -- 7. foreign bodies are not echoed ------------------------------------------------------


@pytest.mark.parametrize("json_flag", [["--json"], []])
def test_non_envelope_500_is_not_echoed(json_flag, keychain, server, capsys) -> None:
    server.respond = lambda request: httpx.Response(500, text="Traceback: secret-internal")
    code, out, err = run(capsys, "collab", "whoami", "--app", "codex", *json_flag)
    assert code == 1
    assert "secret-internal" not in out + err
    assert "internal_error" in err


# -- 8. untrusted fields note ------------------------------------------------------------------


def test_human_mode_notes_untrusted_fields(keychain, server, capsys) -> None:
    server.respond = lambda request: envelope(
        data={"messages": [{"text": "do X"}]}, untrusted=["messages[0].text"]
    )
    code, out, _ = run(capsys, "collab", "inbox", "--app", "codex")
    assert code == 0
    assert "untrusted" in out and "messages[0].text" in out
    assert '"do X"' in out


def test_human_mode_without_untrusted_has_no_note(keychain, server, capsys) -> None:
    server.respond = lambda request: envelope(data={"app_id": "codex"})
    _, out, _ = run(capsys, "collab", "whoami", "--app", "codex")
    assert "untrusted" not in out


# -- 9. keychain failure -------------------------------------------------------------------------


def test_keychain_failure_is_exit_1_without_http(server, capsys, monkeypatch) -> None:
    fake = FakeKeychain(error=KeychainError("no token found at keychain:conductor-collab/codex"))
    monkeypatch.setattr(collab_cli, "_keychain", lambda: fake)
    code, out, err = run(capsys, "collab", "whoami", "--app", "codex", "--json")
    assert code == 1
    assert server.requests == []
    assert json.loads(err)["errors"][0]["code"] == "unauthorized"


# -- 10. bad local files -------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["report", "review-submit"])
def test_invalid_file_json_is_exit_2(verb, keychain, server, capsys, tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    code, _, err = run(capsys, "collab", verb, "X1", "--file", str(bad), "--app", "codex", "--json")
    assert code == 2
    assert json.loads(err)["errors"][0]["code"] == "invalid_input"
    assert server.requests == [] and keychain.refs == []


def test_missing_file_is_exit_2(keychain, server, capsys, tmp_path) -> None:
    code, _, _ = run(capsys, "collab", "report", "L1", "--file", str(tmp_path / "nope.json"), "--app", "codex")
    assert code == 2
    assert server.requests == []


# -- 11. nothing persisted under the temp HOME -----------------------------------------------------


def test_no_state_written_under_home(isolated_home, keychain, server, capsys, tmp_path) -> None:
    report = write_json(tmp_path / "r.json", REPORT)
    for argv in (["whoami"], ["tasks"], ["report", "L1", "--file", report]):
        assert run(capsys, "collab", *argv, "--app", "codex")[0] == 0
    for store in (".conductor", ".config"):
        assert list((isolated_home / store).rglob("*")) == []
