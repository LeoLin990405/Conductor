"""``cond smoke`` hardening: scratch cwd, cleanup, exact READY, loopback only.

Everything runs against an in-memory API fake under ``tmp_path``; no socket is
opened (``RealSystem`` HTTP tests patch ``urlopen`` and assert it is never
reached for a refused URL).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

from src.conductor_cli import ops_support
from src.conductor_cli.ops_cli import main
from src.conductor_cli.ops_runtime import SMOKE_READY, _smoke_outcome
from src.conductor_cli.ops_fleet import DaemonSpec
from src.conductor_cli.ops_support import OpsError, RealSystem, require_loopback_url

PORT = 54331
WEB_PORT = 54332


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("CONDUCTOR_FORBID_PORT_8000", "1")
    monkeypatch.delenv("CONDUCTOR_SMOKE_TIMEOUT", raising=False)


def _layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: dict[str, object] | None = None,
) -> dict[str, Path]:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    (repo / "server.py").write_text("print('server')\n", encoding="utf-8")
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    conductor = home / ".conductor"
    conductor.mkdir(parents=True)
    (conductor / "daemons.toml").write_text(
        f"[backend]\nport = {PORT}\n\n[web]\nport = {WEB_PORT}\nmode = \"preview\"\n\n"
        '[[daemon]]\nprovider = "codex"\nid = "daemon-1"\n\n'
        '[[daemon]]\nprovider = "cursor"\nid = "daemon-2"\n',
        encoding="utf-8",
    )
    worktrees = home / ".conductor" / "worktrees"
    workspace_root = worktrees / "home-ws"
    workspace_root.mkdir(parents=True)
    policy_file = home / ".conductor-daemon" / "workspaces-cli.json"
    policy_file.parent.mkdir(parents=True)
    if policy is None:
        policy = {"ws-home": {"root": str(workspace_root)}}
    policy_file.write_text(json.dumps(policy) + "\n", encoding="utf-8")
    os.chmod(policy_file, 0o600)
    for ident in ("daemon-1", "daemon-2"):
        token = home / ".config" / "conductor" / "daemons" / f"{ident}.token"
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(f"token-for-{ident}\n", encoding="utf-8")
        os.chmod(token, 0o600)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CONDUCTOR_HOME", str(conductor))
    monkeypatch.setenv("CONDUCTOR_REPO_ROOT", str(repo))
    monkeypatch.setenv("CONDUCTOR_BACKEND_PORT", str(PORT))
    monkeypatch.setenv("CONDUCTOR_WEB_PORT", str(WEB_PORT))
    monkeypatch.setenv("CONDUCTOR_WORKTREE_ROOT", str(worktrees))
    return {"repo": repo, "worktrees": worktrees, "workspace_root": workspace_root}


class SmokeApi:
    """The loopback API smoke uses. ``interrupt_after`` raises Ctrl-C mid-poll."""

    def __init__(
        self,
        outcomes: dict[str, object] | None = None,
        *,
        interrupt_after: int | None = None,
        progress_error: type[BaseException] | None = None,
    ) -> None:
        self.outcomes = outcomes or {}
        self.interrupt_after = interrupt_after
        self.progress_error = progress_error
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.tickets: dict[str, str] = {}
        self.cwds_seen: list[tuple[str, bool]] = []
        self.progress_calls = 0
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def http_post(self, url: str, payload, timeout: float) -> tuple[int, str]:
        path = urlparse(url).path
        self.posts.append((path, dict(payload)))
        if path == "/api/v1/workspaces/home":
            return 200, '{"workspace_id":"ws-home"}'
        if path == "/api/v1/autonomy/tasks":
            cwd = str(payload.get("cwd") or "")
            self.cwds_seen.append((cwd, bool(cwd) and Path(cwd).is_dir()))
            ticket_id = f"ticket-{len(self.tickets) + 1}"
            self.tickets[ticket_id] = str(payload.get("worker_daemon_id") or "")
            return 201, json.dumps({"ticket_id": ticket_id})
        if path.endswith("/stop") or path.endswith("/archive"):
            return 200, "{}"
        return 404, '{"detail":"not found"}'

    def http_get(self, url: str, timeout: float) -> tuple[int, str]:
        path = urlparse(url).path
        self.progress_calls += 1
        if self.interrupt_after is not None and self.progress_calls > self.interrupt_after:
            raise KeyboardInterrupt
        if self.progress_error is not None:
            raise self.progress_error("boom")
        ticket_id = path.removeprefix("/api/v1/autonomy/tickets/").removesuffix(
            "/progress"
        )
        outcome = self.outcomes.get(self.tickets.get(ticket_id, ""), "running")
        if outcome == "running":
            return 200, '{"status":"running"}'
        return 200, json.dumps(outcome)

    def paths(self, suffix: str) -> list[str]:
        return [path for path, _ in self.posts if path.endswith(suffix)]

    def tasks(self) -> list[dict[str, object]]:
        return [p for path, p in self.posts if path == "/api/v1/autonomy/tasks"]


READY_BOTH = {
    "daemon-1": {"status": "completed", "summary": SMOKE_READY},
    "daemon-2": {"status": "completed", "summary": SMOKE_READY},
}


# 1. Scratch cwd ---------------------------------------------------------------


def test_smoke_tickets_run_in_a_fresh_scratch_cwd_that_is_removed(
    tmp_path: Path, monkeypatch
) -> None:
    roots = _layout(tmp_path, monkeypatch)
    fake = SmokeApi(READY_BOTH)

    assert main(["smoke"], system=fake) == 0

    assert len(fake.cwds_seen) == 2
    cwds = {cwd for cwd, _ in fake.cwds_seen}
    assert len(cwds) == 1
    scratch = Path(cwds.pop())
    # It existed while the Tickets were created, as a daemon requires...
    assert all(existed for _, existed in fake.cwds_seen)
    # ...under both the worktree root and the Home policy root, and is not
    # the repo or the workspace root itself.
    assert scratch.is_relative_to(roots["workspace_root"].resolve())
    assert scratch.is_relative_to(roots["worktrees"].resolve())
    assert scratch != roots["workspace_root"].resolve()
    assert not scratch.is_relative_to(roots["repo"].resolve())
    assert scratch.parent.name == ".cond-smoke"
    # ...and nothing is left behind.
    assert not scratch.exists()
    assert not (roots["workspace_root"] / ".cond-smoke").exists()


def test_smoke_fails_every_daemon_without_creating_tickets_when_no_policy_row(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _layout(tmp_path, monkeypatch, policy={})
    fake = SmokeApi(READY_BOTH)

    assert main(["smoke"], system=fake) == 1
    out = capsys.readouterr().out

    assert fake.tasks() == []
    assert out.count("no smoke cwd: ") == 2
    assert "no entry for the Home workspace" in out


def test_smoke_refuses_when_worktree_and_workspace_roots_do_not_overlap(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _layout(tmp_path, monkeypatch, policy={"ws-home": {"root": str(elsewhere)}})
    fake = SmokeApi(READY_BOTH)

    assert main(["smoke"], system=fake) == 1
    assert fake.tasks() == []
    out = capsys.readouterr().out
    assert "do not overlap" in out
    assert "CONDUCTOR_SMOKE_CWD" in out
    assert list(elsewhere.iterdir()) == []


# N70: the daemons' worktree root is not this shell's, so the roots this shell
# sees do not overlap; the operator names the smoke directory instead.


def test_configured_smoke_cwd_is_used_when_the_roots_do_not_overlap(
    tmp_path: Path, monkeypatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    smoke_base = elsewhere / "smoke"
    smoke_base.mkdir(parents=True)
    _layout(tmp_path, monkeypatch, policy={"ws-home": {"root": str(elsewhere)}})
    monkeypatch.setenv("CONDUCTOR_SMOKE_CWD", str(smoke_base))
    fake = SmokeApi(READY_BOTH)

    assert main(["smoke"], system=fake) == 0

    assert len(fake.cwds_seen) == 2
    assert all(existed for _, existed in fake.cwds_seen)
    scratch = Path(fake.cwds_seen[0][0])
    assert scratch.parent == smoke_base.resolve() / ".cond-smoke"
    assert not scratch.exists()
    assert list(smoke_base.iterdir()) == []


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("relative/dir", "must be an absolute path"),
        ("{tmp}/missing", "does not exist"),
        ("{tmp}/outside", "is not inside the Home workspace root"),
        ("{tmp}/elsewhere/link", "not a symlink"),
    ],
)
def test_configured_smoke_cwd_is_refused_unless_a_daemon_could_accept_it(
    tmp_path: Path, monkeypatch, capsys, value: str, reason: str
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "outside").mkdir()
    (elsewhere / "real").mkdir()
    (elsewhere / "link").symlink_to(elsewhere / "real")
    _layout(tmp_path, monkeypatch, policy={"ws-home": {"root": str(elsewhere)}})
    monkeypatch.setenv("CONDUCTOR_SMOKE_CWD", value.format(tmp=tmp_path))
    fake = SmokeApi(READY_BOTH)

    assert main(["smoke"], system=fake) == 1
    out = capsys.readouterr().out
    assert fake.tasks() == []
    assert out.count("no smoke cwd: ") == 2
    assert reason in out


# 2. Cleanup on Ctrl-C and on errors ------------------------------------------


def test_ctrl_c_stops_and_archives_every_running_smoke_ticket(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    roots = _layout(tmp_path, monkeypatch)
    fake = SmokeApi({"daemon-1": "running", "daemon-2": "running"}, interrupt_after=2)

    assert main(["smoke"], system=fake) == 130

    assert sorted(fake.paths("/stop")) == [
        "/api/v1/autonomy/tickets/ticket-1/stop",
        "/api/v1/autonomy/tickets/ticket-2/stop",
    ]
    assert len(fake.paths("/archive")) == 2
    assert "interrupted" in capsys.readouterr().err
    assert not (roots["workspace_root"] / ".cond-smoke").exists()


def test_unexpected_error_still_stops_running_tickets_and_propagates(
    tmp_path: Path, monkeypatch
) -> None:
    roots = _layout(tmp_path, monkeypatch)
    fake = SmokeApi(progress_error=RuntimeError)

    with pytest.raises(RuntimeError):
        main(["smoke"], system=fake)

    assert len(fake.paths("/stop")) == 2
    assert len(fake.paths("/archive")) == 2
    assert not (roots["workspace_root"] / ".cond-smoke").exists()


def test_ctrl_c_only_stops_tickets_still_running(
    tmp_path: Path, monkeypatch
) -> None:
    _layout(tmp_path, monkeypatch)
    # daemon-1 finishes on the first poll; Ctrl-C lands on the next round.
    fake = SmokeApi(
        {"daemon-1": {"status": "completed", "summary": SMOKE_READY}},
        interrupt_after=2,
    )

    assert main(["smoke"], system=fake) == 130
    assert fake.paths("/stop") == ["/api/v1/autonomy/tickets/ticket-2/stop"]


# 3. Exact READY -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("summary", "ready"),
    [
        (SMOKE_READY, True),
        (f"  {SMOKE_READY}\n", True),
        (f"NOT {SMOKE_READY}", False),
        (f"{SMOKE_READY}.", False),
        (SMOKE_READY.lower(), False),
        (f"I am {SMOKE_READY} now", False),
        ("NOT READY", False),
        ("READY.", False),
        ("ready", False),
        ("  READY\n", False),
    ],
)
def test_ready_requires_the_whole_trimmed_reply_to_be_the_token(
    summary: str, ready: bool
) -> None:
    daemon = DaemonSpec("codex", "daemon-1")
    outcome = _smoke_outcome(daemon, "t-1", {"status": "completed", "summary": summary})
    assert outcome is not None
    assert outcome.ready is ready


# 4. Marked and archived ----------------------------------------------------------


def test_smoke_tickets_are_titled_smoke_and_archived_when_done(
    tmp_path: Path, monkeypatch
) -> None:
    _layout(tmp_path, monkeypatch)
    fake = SmokeApi(
        {
            "daemon-1": {"status": "completed", "summary": SMOKE_READY},
            "daemon-2": {"status": "failed", "error": "quota"},
        }
    )

    assert main(["smoke"], system=fake) == 1

    titles = [str(task["title"]) for task in fake.tasks()]
    assert titles == ["[smoke] codex/daemon-1", "[smoke] cursor/daemon-2"]
    assert sorted(fake.paths("/archive")) == [
        "/api/v1/autonomy/tickets/ticket-1/archive",
        "/api/v1/autonomy/tickets/ticket-2/archive",
    ]
    # Finished Tickets are not stopped, only archived.
    assert fake.paths("/stop") == []


# 5. Loopback only ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:5/x",
        "http://127.8.9.10:5/x",
        "http://localhost:5/x",
        "https://LOCALHOST/x",
        "http://[::1]:5/x",
    ],
)
def test_loopback_urls_are_allowed(url: str) -> None:
    require_loopback_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x",
        "http://10.0.0.1/x",
        "http://0.0.0.0:5/x",
        "http://128.0.0.1/x",
        "http://localhost.example.com/x",
        "http://127.0.0.1.nip.io/x",
        "http://user@evil.test/x",
        "file:///etc/passwd",
        "ftp://127.0.0.1/x",
        "http:///x",
        "http://127.0.0.1:notaport/x",
    ],
)
@pytest.mark.parametrize("method", ["get", "post"])
def test_real_system_refuses_non_loopback_before_connecting(
    url: str, method: str, monkeypatch
) -> None:
    def no_connect(*args, **kwargs):
        raise AssertionError("urlopen must not be reached")

    monkeypatch.setattr(ops_support, "urlopen", no_connect)
    system = RealSystem()
    with pytest.raises(OpsError, match="refusing"):
        if method == "get":
            system.http_get(url, timeout=1)
        else:
            system.http_post(url, {"a": 1}, timeout=1)
