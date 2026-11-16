"""``cond restart daemons`` and ``cond smoke`` against injected systems.

The fakes never open a socket and never start a real process: restart-daemons
is seeded through the ops runtime, and smoke answers the loopback API in
memory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

from src.conductor_cli.ops_cli import main
from src.conductor_cli.ops_fleet import load_fleet
from src.conductor_cli.ops_runtime import (
    _require_this_backend,
    start_backend,
    start_daemon,
    start_web,
)
from src.conductor_cli.ops_support import Completed, load_settings

PORT = 54321
WEB_PORT = 54322


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("CONDUCTOR_FORBID_PORT_8000", "1")
    monkeypatch.delenv("CONDUCTOR_SMOKE_TIMEOUT", raising=False)


def _layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    daemons: tuple[tuple[str, str], ...] = (
        ("codex", "daemon-1"),
        ("cursor", "daemon-2"),
    ),
) -> Path:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    (repo / "server.py").write_text("print('server')\n", encoding="utf-8")
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    conductor = home / ".conductor"
    conductor.mkdir(parents=True)
    rows = []
    for provider, ident in daemons:
        rows.append(f'[[daemon]]\nprovider = "{provider}"\nid = "{ident}"\n')
    (conductor / "daemons.toml").write_text(
        f"""
[backend]
port = {PORT}

[web]
port = {WEB_PORT}
mode = "preview"

""".strip()
        + "\n"
        + "\n".join(rows),
        encoding="utf-8",
    )
    policy = home / ".conductor-daemon" / "workspaces-cli.json"
    policy.parent.mkdir(parents=True)
    # Smoke Tickets need a scratch cwd under both the worktree root and the
    # Home workspace's policy root.
    worktrees = home / ".conductor" / "worktrees"
    (worktrees / "home-ws").mkdir(parents=True)
    policy.write_text(
        json.dumps({"ws-home": {"root": str(worktrees / "home-ws")}}) + "\n",
        encoding="utf-8",
    )
    os.chmod(policy, 0o600)
    monkeypatch.setenv("CONDUCTOR_WORKTREE_ROOT", str(worktrees))
    for _provider, ident in daemons:
        token = home / ".config" / "conductor" / "daemons" / f"{ident}.token"
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(f"token-for-{ident}\n", encoding="utf-8")
        os.chmod(token, 0o600)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CONDUCTOR_HOME", str(conductor))
    monkeypatch.setenv("CONDUCTOR_REPO_ROOT", str(repo))
    monkeypatch.setenv("CONDUCTOR_BACKEND_PORT", str(PORT))
    monkeypatch.setenv("CONDUCTOR_WEB_PORT", str(WEB_PORT))
    return home


class RestartSystem:
    """Process table and HTTP probes for a daemon-only rolling restart."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.popen_calls: list[tuple[int, list[str]]] = []
        self.alive_pids: set[int] = set()
        self.procs: dict[int, dict[str, object]] = {}
        self.events: list[tuple[str, object]] = []
        self.processes_calls = 0
        self.next_pid = 41000
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def run(self, argv, *, cwd=None, env=None, timeout=None) -> Completed:
        self.commands.append(list(argv))
        return Completed(0, "", "")

    def popen(
        self, argv, *, cwd, env, log_path: Path, max_bytes: int, backups: int
    ) -> int:
        self.commands.append(list(argv))
        pid = self.next_pid
        self.next_pid += 1
        self.procs[pid] = {"argv": list(argv), "cwd": str(cwd)}
        self.alive_pids.add(pid)
        self.popen_calls.append((pid, list(argv)))
        self.events.append(("popen", list(argv)))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return pid

    def alive(self, pid: int) -> bool:
        return pid in self.alive_pids

    def command(self, pid: int) -> str:
        return " ".join(str(part) for part in self.procs.get(pid, {}).get("argv", []))

    def cwd(self, pid: int) -> str:
        return str(self.procs.get(pid, {}).get("cwd") or "")

    def terminate(self, pid: int) -> None:
        self.alive_pids.discard(pid)
        self.events.append(("terminate", pid))

    def kill(self, pid: int) -> None:
        self.alive_pids.discard(pid)

    def processes(self) -> str:
        self.processes_calls += 1
        return ""

    def port_open(self, host: str, port: int, timeout: float = 0.3) -> bool:
        return False

    def http_get(self, url: str, timeout: float) -> tuple[int, str]:
        path = urlparse(url).path
        if path == "/ready":
            return 200, '{"status":"ready"}'
        if path == "/health":
            return 200, '{"status":"healthy"}'
        return 404, "{}"


def _seed_running_stack(system: RestartSystem) -> tuple[int, int, dict[str, int]]:
    settings = load_settings()
    fleet = load_fleet(settings)
    backend = start_backend(system, settings)
    _require_this_backend(system, settings, backend)
    web = start_web(system, settings)
    daemons = {
        f"{daemon.provider}/{daemon.ident}": start_daemon(system, settings, daemon)
        for daemon in fleet.daemons
    }
    return backend, web, daemons


def test_restart_daemons_is_rolling_and_keeps_backend_and_web(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _layout(tmp_path, monkeypatch)
    fake = RestartSystem()
    backend, web, daemons = _seed_running_stack(fake)
    popen_before = len(fake.popen_calls)

    assert main(["restart", "daemons"], system=fake) == 0
    out = capsys.readouterr().out

    assert backend in fake.alive_pids
    assert web in fake.alive_pids
    assert fake.processes_calls == 0
    assert sum(
        1 for _pid, argv in fake.popen_calls if "server.py" in " ".join(argv)
    ) == 1
    new_popen = fake.popen_calls[popen_before:]
    daemon_argv = [
        argv
        for _pid, argv in new_popen
        if "conductor_local_daemon.py" in " ".join(argv)
    ]
    assert len(daemon_argv) == 2
    assert all(_pid not in fake.alive_pids for _pid in daemons.values())
    assert "daemon: codex/daemon-1 up" in out
    assert "daemon: cursor/daemon-2 up" in out
    # Each daemon is stopped before its own replacement starts, and the two
    # replacements do not interleave: this is a rolling pass, not stop-all.
    events = fake.events
    first_stop = events.index(("terminate", daemons["codex/daemon-1"]))
    first_start = next(
        index
        for index, event in enumerate(events)
        if index > first_stop
        and event[0] == "popen"
        and "conductor_local_daemon.py" in " ".join(event[1])  # type: ignore[arg-type]
    )
    second_stop = events.index(("terminate", daemons["cursor/daemon-2"]))
    assert first_stop < first_start < second_stop


def test_restart_daemons_refuses_launch_wait_options(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _layout(tmp_path, monkeypatch)
    fake = RestartSystem()
    _seed_running_stack(fake)
    for extra in (
        ["--force"],
        ["--no-wait"],
        ["--wait-timeout", "0"],
    ):
        assert main(["restart", "daemons", *extra], system=fake) == 1
        assert "apply only to a full restart" in capsys.readouterr().err
    assert fake.processes_calls == 0


class SmokeSystem:
    """The loopback API the smoke command uses, with per-daemon outcomes."""

    def __init__(self, outcomes: dict[str, object] | None = None,
                 create_errors: dict[str, tuple[int, str]] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.create_errors = create_errors or {}
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.gets: list[str] = []
        self.tickets: dict[str, str] = {}
        self.next_ticket = 0
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
            daemon_id = str(payload.get("worker_daemon_id") or "")
            error = self.create_errors.get(daemon_id)
            if error is not None:
                return error
            self.next_ticket += 1
            ticket_id = f"ticket-{self.next_ticket}"
            self.tickets[ticket_id] = daemon_id
            return 201, json.dumps({"ticket_id": ticket_id})
        if path.endswith("/stop"):
            return 200, "{}"
        return 404, '{"detail":"not found"}'

    def http_get(self, url: str, timeout: float) -> tuple[int, str]:
        path = urlparse(url).path
        self.gets.append(path)
        prefix = "/api/v1/autonomy/tickets/"
        if not path.startswith(prefix) or not path.endswith("/progress"):
            return 404, '{"detail":"not found"}'
        ticket_id = path[len(prefix) : -len("/progress")]
        daemon_id = self.tickets.get(ticket_id)
        if daemon_id is None:
            return 404, '{"detail":"Ticket not found"}'
        outcome = self.outcomes.get(daemon_id, "running")
        if outcome == "running":
            return 200, '{"status":"running"}'
        assert isinstance(outcome, dict)
        return 200, json.dumps(outcome)


def _task_posts(fake: SmokeSystem) -> list[dict[str, object]]:
    return [
        payload
        for path, payload in fake.posts
        if path == "/api/v1/autonomy/tasks"
    ]


def test_smoke_creates_one_read_only_ticket_per_daemon(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _layout(tmp_path, monkeypatch)
    fake = SmokeSystem(
        {
            "daemon-1": {"status": "completed", "summary": "SMOKE-READY"},
            "daemon-2": {"status": "completed", "summary": "SMOKE-READY"},
        }
    )

    assert main(["smoke"], system=fake) == 0
    out = capsys.readouterr().out

    tasks = _task_posts(fake)
    assert len(tasks) == 2
    assert {task["worker_daemon_id"] for task in tasks} == {"daemon-1", "daemon-2"}
    assert {task["worker_provider"] for task in tasks} == {"codex", "cursor"}
    assert all(task["workspace_id"] == "ws-home" for task in tasks)
    assert all("SMOKE-READY" in str(task["prompt"]) for task in tasks)
    assert all("read-only" in str(task["prompt"]) for task in tasks)
    assert "daemon: codex/daemon-1 READY" in out
    assert "daemon: cursor/daemon-2 READY" in out
    assert "smoke: 2/2 ready" in out


def test_smoke_reports_failure_reason_and_exits_nonzero(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _layout(tmp_path, monkeypatch)
    fake = SmokeSystem(
        {
            "daemon-1": {"status": "completed", "summary": "SMOKE-READY"},
            "daemon-2": {
                "status": "failed",
                "summary": "worker exited",
                "error": "zcode quota exhausted",
            },
        }
    )

    assert main(["smoke"], system=fake) == 1
    out = capsys.readouterr().out
    assert "daemon: codex/daemon-1 READY" in out
    assert "daemon: cursor/daemon-2 FAILED: zcode quota exhausted" in out
    assert "smoke: 1/2 ready" in out


def test_smoke_keeps_checking_other_daemons_when_one_cannot_be_bound(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _layout(tmp_path, monkeypatch)
    fake = SmokeSystem(
        {"daemon-2": {"status": "completed", "summary": "SMOKE-READY"}},
        create_errors={"daemon-1": (404, '{"detail":"Daemon not found"}')},
    )

    assert main(["smoke"], system=fake) == 1
    out = capsys.readouterr().out
    assert "daemon: codex/daemon-1 FAILED: Daemon not found" in out
    assert "daemon: cursor/daemon-2 READY" in out
    assert len(_task_posts(fake)) == 2


def test_smoke_stops_a_ticket_waiting_for_input(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _layout(tmp_path, monkeypatch)
    fake = SmokeSystem(
        {
            "daemon-1": {"status": "waiting", "summary": "needs an answer"},
            "daemon-2": {"status": "completed", "summary": "SMOKE-READY"},
        }
    )

    assert main(["smoke"], system=fake) == 1
    out = capsys.readouterr().out
    assert "daemon: codex/daemon-1 FAILED: needs an answer" in out
    assert "daemon: cursor/daemon-2 READY" in out
    stops = [path for path, _payload in fake.posts if path.endswith("/stop")]
    assert stops == ["/api/v1/autonomy/tickets/ticket-1/stop"]


def test_smoke_timeout_fails_and_stops_unfinished_tickets(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _layout(tmp_path, monkeypatch)
    fake = SmokeSystem({"daemon-1": "running", "daemon-2": "running"})

    assert main(["smoke", "--timeout", "3"], system=fake) == 1
    out = capsys.readouterr().out
    assert out.count("timed out after 3s") == 2
    assert "smoke: 0/2 ready" in out
    assert fake.now == pytest.approx(3.0)
    stops = [path for path, _payload in fake.posts if path.endswith("/stop")]
    assert len(stops) == 2


def test_smoke_timeout_comes_from_the_environment(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _layout(tmp_path, monkeypatch)
    monkeypatch.setenv("CONDUCTOR_SMOKE_TIMEOUT", "2")
    fake = SmokeSystem({"daemon-1": "running", "daemon-2": "running"})

    assert main(["smoke"], system=fake) == 1
    assert fake.now == pytest.approx(2.0)
    assert capsys.readouterr().out.count("timed out after 2s") == 2


def test_smoke_with_no_configured_daemons_is_a_clean_noop(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _layout(tmp_path, monkeypatch, daemons=())
    fake = SmokeSystem()

    assert main(["smoke"], system=fake) == 0
    assert "daemon: none configured" in capsys.readouterr().out
    assert fake.posts == []


def test_smoke_json_is_machine_readable(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _layout(tmp_path, monkeypatch)
    fake = SmokeSystem(
        {
            "daemon-1": {"status": "completed", "summary": "SMOKE-READY"},
            "daemon-2": {"status": "completed", "summary": "SMOKE-READY"},
        }
    )

    assert main(["smoke", "--json"], system=fake) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert [row["id"] for row in payload["daemons"]] == ["daemon-1", "daemon-2"]
    assert all(row["state"] == "ready" for row in payload["daemons"])


def test_restart_daemons_never_waits_for_dispatch_launches(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from src.conductor_cli import ops_cli

    _layout(tmp_path, monkeypatch)
    fake = RestartSystem()
    backend, web, _daemons = _seed_running_stack(fake)

    def no_settle(*args, **kwargs):
        raise AssertionError("restart daemons must not wait for launches")

    monkeypatch.setattr(ops_cli, "_settle_dispatch", no_settle)
    assert main(["restart", "daemons"], system=fake) == 0
    assert ("terminate", backend) not in fake.events
    assert ("terminate", web) not in fake.events


def test_restart_daemons_stops_the_pass_when_one_daemon_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from src.conductor_cli import ops_runtime
    from src.conductor_cli.ops_support import OpsError

    _layout(tmp_path, monkeypatch)
    fake = RestartSystem()
    _backend, _web, daemons = _seed_running_stack(fake)
    real_start = ops_runtime.start_daemon

    def failing_start(system, settings, daemon):
        if daemon.ident == "daemon-1":
            raise OpsError("daemon-1 did not start")
        return real_start(system, settings, daemon)

    monkeypatch.setattr(ops_runtime, "start_daemon", failing_start)
    assert main(["restart", "daemons"], system=fake) == 1
    assert "daemon-1 did not start" in capsys.readouterr().err
    # Rolling: the second daemon was never touched.
    assert daemons["cursor/daemon-2"] in fake.alive_pids


# --- N64: drain -------------------------------------------------------------


def _drain_file(home: Path) -> Path:
    return home / ".conductor" / "drain.json"


def test_drain_and_undrain_set_and_clear_the_flag(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = _layout(tmp_path, monkeypatch)
    fake = RestartSystem()

    assert main(["drain", "--reason", "deploy  v2"], system=fake) == 0
    out = capsys.readouterr().out
    payload = json.loads(_drain_file(home).read_text(encoding="utf-8"))
    assert payload["reason"] == "deploy v2"
    assert payload["set_at"]
    assert out.startswith("drain: on")
    assert "reason=deploy v2" in out

    # Draining again keeps the first flag.
    assert main(["drain", "--reason", "other"], system=fake) == 0
    capsys.readouterr()
    assert json.loads(_drain_file(home).read_text())["reason"] == "deploy v2"

    assert main(["undrain"], system=fake) == 0
    assert capsys.readouterr().out == "drain: off\n"
    assert not _drain_file(home).exists()
    assert main(["undrain"], system=fake) == 0
    assert "was not set" in capsys.readouterr().out


def test_status_shows_the_drain_state(tmp_path: Path, monkeypatch, capsys) -> None:
    from src.conductor_cli.drain import set_drain

    home = _layout(tmp_path, monkeypatch)
    fake = RestartSystem()
    _seed_running_stack(fake)

    assert main(["status"], system=fake) == 0
    assert "drain: off\n" in capsys.readouterr().out

    set_drain(home / ".conductor", "release 12")
    # Drained is not "down": the exit code does not change.
    assert main(["status"], system=fake) == 0
    line = next(
        text
        for text in capsys.readouterr().out.splitlines()
        if text.startswith("drain:")
    )
    assert line.startswith("drain: on (no new dispatch launches) since=")
    assert line.endswith("reason=release 12")

    assert main(["status", "--json"], system=fake) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["drain"]["drained"] is True
    assert report["drain"]["reason"] == "release 12"


def test_an_unparsable_flag_still_counts_as_drained(tmp_path: Path) -> None:
    from src.conductor_cli.drain import is_drained, read_drain

    home = tmp_path / "c"
    home.mkdir()
    (home / "drain.json").write_text("{half", encoding="utf-8")
    assert is_drained(home)
    assert read_drain(home) == {"set_at": "", "reason": "unparsable drain flag"}


def _record_restart(monkeypatch, home: Path, *, settle_error: str | None = None):
    from src.conductor_cli import ops_cli
    from src.conductor_cli.ops_support import OpsError

    events: list[tuple[str, bool]] = []

    def drained() -> bool:
        return _drain_file(home).exists()

    def settle(system, settings, action, options):
        events.append(("wait", drained()))
        if settle_error:
            raise OpsError(settle_error)

    def restart(system, settings, fleet):
        events.append(("restart", drained()))
        return {}

    monkeypatch.setattr(ops_cli, "_settle_dispatch", settle)
    monkeypatch.setattr(ops_cli, "restart_all", restart)
    return events


def test_restart_drain_drains_waits_restarts_then_undrains(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = _layout(tmp_path, monkeypatch)
    fake = RestartSystem()
    _seed_running_stack(fake)
    events = _record_restart(monkeypatch, home)

    assert main(["restart", "--drain", "--wait-timeout", "30"], system=fake) == 0
    err = capsys.readouterr().err

    # The waits (before and after the grace period) and the restart all ran
    # with the drain set; it is gone after.
    assert events == [("wait", True), ("wait", True), ("restart", True)]
    assert not _drain_file(home).exists()
    assert err.index("drain: on") < err.index("drain: off")


def test_restart_drain_timeout_undrains_and_does_not_restart(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = _layout(tmp_path, monkeypatch)
    fake = RestartSystem()
    _seed_running_stack(fake)
    events = _record_restart(
        monkeypatch, home, settle_error="dispatch launch(es) still running"
    )

    assert main(["restart", "--drain", "--wait-timeout", "1"], system=fake) == 1
    err = capsys.readouterr().err
    assert events == [("wait", True)]
    assert "still running" in err
    assert not _drain_file(home).exists()


def test_restart_drain_keeps_an_operator_drain(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from src.conductor_cli.drain import set_drain

    home = _layout(tmp_path, monkeypatch)
    fake = RestartSystem()
    _seed_running_stack(fake)
    set_drain(home / ".conductor", "manual")
    events = _record_restart(monkeypatch, home)

    assert main(["restart", "--drain"], system=fake) == 0
    assert events == [("wait", True), ("wait", True), ("restart", True)]
    assert json.loads(_drain_file(home).read_text())["reason"] == "manual"
    assert "stays on" in capsys.readouterr().err


def test_restart_drain_refuses_daemons_and_no_wait(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = _layout(tmp_path, monkeypatch)
    fake = RestartSystem()
    _seed_running_stack(fake)
    events = _record_restart(monkeypatch, home)

    assert main(["restart", "daemons", "--drain"], system=fake) == 1
    assert "only to a full restart" in capsys.readouterr().err
    assert main(["restart", "--drain", "--no-wait"], system=fake) == 1
    assert "cannot use --no-wait" in capsys.readouterr().err
    assert events == []
    assert not _drain_file(home).exists()
