"""N64 drain hardening: signals, damaged flags, grace scan, busy wait, status.

Every system here is a fake: no process is started or signalled except the
test's own SIGTERM to itself (caught by the handler under test), no socket is
opened, and every file lives under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.conductor_cli import activity, ops_cli, ops_runtime
from src.conductor_cli.drain import (
    clear_drain,
    drain_path,
    is_drained,
    read_drain,
    set_drain,
)
from src.conductor_cli.ops_support import OpsError, load_settings

UNREADABLE = {"set_at": "", "reason": "unreadable drain flag"}


# --- fakes ------------------------------------------------------------------


class Clock:
    """``monotonic`` / ``sleep`` / ``http_get`` for the injected system."""

    def __init__(self, ops: tuple[int, str] | None = None) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.on_sleep: list = []
        self.ops = ops
        self.gets: list[str] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        for hook in list(self.on_sleep):
            hook(self)

    def http_get(self, url: str, timeout: float) -> tuple[int, str]:
        self.gets.append(url)
        if url.endswith(ops_runtime.OPS_STATUS_PATH) and self.ops is not None:
            return self.ops
        if url.endswith("/health"):
            return 200, '{"status":"healthy"}'
        return 0, "URLError"


def _settings(tmp_path: Path, wait: float = 600.0):
    home = tmp_path / "conductor-home"
    home.mkdir(exist_ok=True)
    return SimpleNamespace(
        conductor_home=home,
        dispatch_wait_seconds=wait,
        base_url="http://127.0.0.1:54321",
        sensitive=[],
    )


@pytest.fixture
def restart_env(tmp_path, monkeypatch):
    """``cond restart --drain`` with the launch wait and restart recorded."""
    settings = _settings(tmp_path)
    events: list[tuple] = []
    state = {"settle": None}

    def settle(system, s, action, options):
        events.append(("wait", is_drained(s.conductor_home), (options or {}).get("wait_timeout")))
        if state["settle"] is not None:
            state["settle"](system)

    def restart(system, s, fleet):
        events.append(("restart", is_drained(s.conductor_home)))

    monkeypatch.setattr(ops_cli, "_fleet_or_raise", lambda system, s: object())
    monkeypatch.setattr(ops_cli, "_settle_dispatch", settle)
    monkeypatch.setattr(ops_cli, "restart_all", restart)
    monkeypatch.setattr(
        ops_cli,
        "status_map",
        lambda system, s, fleet: {"backend": {}, "web": {}, "daemons": []},
    )
    monkeypatch.setattr(ops_cli, "format_status", lambda report: "")
    monkeypatch.setattr(ops_cli, "status_ok", lambda report: True)
    monkeypatch.setattr(ops_cli, "prepare", lambda system: (system, settings, None))
    return SimpleNamespace(settings=settings, events=events, state=state)


# --- B1: SIGTERM / SIGHUP during restart --drain -----------------------------


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGHUP"])
def test_a_signal_during_the_wait_clears_the_drain_and_restores_handlers(
    restart_env, signame, capsys
) -> None:
    signum = getattr(signal, signame)
    seen: list[object] = []

    def previous(sig, frame):  # the handler that was there before
        seen.append(sig)

    old = signal.signal(signum, previous)
    try:

        def send_signal(system) -> None:
            # The operator's SSH session drops while cond waits.
            os.kill(os.getpid(), signum)
            raise AssertionError("the signal handler did not interrupt the wait")

        restart_env.state["settle"] = send_signal
        code = ops_cli.main(["restart", "--drain"], system=Clock())

        assert code == 1
        assert f"interrupted by {signame}" in capsys.readouterr().err
        assert not is_drained(restart_env.settings.conductor_home)
        # Nothing was restarted.
        assert [e[0] for e in restart_env.events] == ["wait"]
        # The previous handler is back.
        assert signal.getsignal(signum) is previous
    finally:
        signal.signal(signum, old)
    assert seen == []


def test_handlers_are_restored_after_a_normal_restart(restart_env) -> None:
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    assert ops_cli.main(["restart", "--drain"], system=Clock()) == 0
    assert {s: signal.getsignal(s) for s in before} == before


def test_handlers_are_restored_after_an_error(restart_env) -> None:
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}

    def fail(system) -> None:
        raise OpsError("still running")

    restart_env.state["settle"] = fail
    assert ops_cli.main(["restart", "--drain"], system=Clock()) == 1
    assert {s: signal.getsignal(s) for s in before} == before
    assert not is_drained(restart_env.settings.conductor_home)


# --- SF2: grace period and the second launch scan ----------------------------


def test_launches_are_scanned_before_and_after_a_grace_period(restart_env) -> None:
    clock = Clock()
    assert (
        ops_cli.main(["restart", "--drain", "--wait-timeout", "30"], system=clock) == 0
    )
    events = restart_env.events
    # Two scans, both drained, then the restart; the grace is 2 s of the
    # injected clock, and the second scan gets what is left of the budget.
    assert events == [
        ("wait", True, 30.0),
        ("wait", True, 28.0),
        ("restart", True),
    ]
    assert clock.sleeps == [ops_cli.DRAIN_GRACE_SECONDS]


def test_a_launch_that_appears_during_grace_blocks_the_restart(restart_env) -> None:
    clock = Clock()
    scans = {"n": 0}

    def settle(system) -> None:
        scans["n"] += 1
        if scans["n"] == 2:
            # A launch that passed its check just before the drain spawned
            # during the grace period: the second scan finds it.
            raise OpsError("dispatch launch(es) still running after 28s: 4242")

    restart_env.state["settle"] = settle
    assert (
        ops_cli.main(["restart", "--drain", "--wait-timeout", "30"], system=clock) == 1
    )
    assert [e[0] for e in restart_env.events] == ["wait", "wait"]
    assert not is_drained(restart_env.settings.conductor_home)


# --- SF4: busy markers --------------------------------------------------------


def test_restart_waits_for_busy_markers_then_restarts(restart_env, capsys) -> None:
    home = restart_env.settings.conductor_home
    clock = Clock()
    marker = activity.busy(activity.G2, "line-1", home=home)
    marker.__enter__()
    released = {"at": None}

    def end_g2(system) -> None:
        if system.now >= 6 and released["at"] is None:
            marker.__exit__(None, None, None)
            released["at"] = system.now

    clock.on_sleep.append(end_g2)
    try:
        assert ops_cli.main(["restart", "--drain", "--wait-timeout", "30"], system=clock) == 0
    finally:
        if released["at"] is None:
            marker.__exit__(None, None, None)
    assert restart_env.events[-1] == ("restart", True)
    assert released["at"] is not None
    assert "waiting for dispatch work: g2=1" in capsys.readouterr().err
    assert not is_drained(home)


def test_busy_timeout_restarts_nothing_and_names_the_kinds(restart_env, capsys) -> None:
    home = restart_env.settings.conductor_home
    clock = Clock()
    with activity.busy(activity.REVIEW, "line-2", home=home), activity.busy(
        activity.MERGE, "line-3", home=home
    ):
        code = ops_cli.main(["restart", "--drain", "--wait-timeout", "10"], system=clock)
    err = capsys.readouterr().err
    assert code == 1
    assert "still busy: merge=1, review=1" in err
    assert [e[0] for e in restart_env.events] == ["wait", "wait"]
    assert not is_drained(home)
    # The busy wait stayed inside the --wait-timeout budget.
    assert clock.now <= 10.0 + 1e-9


# --- B2 / SF5: damaged flag files ---------------------------------------------


def test_a_non_utf8_flag_counts_as_drained(tmp_path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    drain_path(home).write_bytes(b"\xff\xfe\x00garbage")
    assert read_drain(home) == UNREADABLE
    assert is_drained(home)


def test_cond_drain_and_status_survive_a_non_utf8_flag(tmp_path, capsys) -> None:
    settings = _settings(tmp_path)
    drain_path(settings.conductor_home).write_bytes(b"\xff\xfe")
    assert ops_cli.cmd_drain(settings, "deploy") == 0
    assert "drain: on" in capsys.readouterr().out
    assert ops_runtime._drain_status(settings) == {"drained": True, **UNREADABLE}


def test_a_dangling_symlink_counts_as_drained(tmp_path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    os.symlink(home / "missing.json", drain_path(home))
    assert read_drain(home) == UNREADABLE
    assert is_drained(home)
    # undrain removes the link itself.
    assert clear_drain(home) is True
    assert not is_drained(home)


def test_a_missing_flag_is_not_drained(tmp_path) -> None:
    assert read_drain(tmp_path) is None


def test_a_directory_at_the_flag_path_counts_as_drained(tmp_path) -> None:
    home = tmp_path / "h"
    drain_path(home).mkdir(parents=True)
    assert read_drain(home) == UNREADABLE
    assert is_drained(home)


def test_undrain_on_a_directory_is_a_clean_error(tmp_path, capsys) -> None:
    settings = _settings(tmp_path)
    drain_path(settings.conductor_home).mkdir()
    with pytest.raises(OpsError, match="cannot clear the drain flag"):
        ops_cli.cmd_undrain(settings)


def test_undrain_on_a_directory_exits_1_without_traceback(
    tmp_path, monkeypatch, capsys
) -> None:
    settings = _settings(tmp_path)
    drain_path(settings.conductor_home).mkdir()
    monkeypatch.setattr(ops_cli, "prepare", lambda system: (system, settings, None))
    assert ops_cli.main(["undrain"], system=Clock()) == 1
    err = capsys.readouterr().err
    assert "[cond] error: cannot clear the drain flag" in err
    assert "Traceback" not in err


# --- SF3: the backend's view in cond status ------------------------------------


def _status_settings(tmp_path: Path) -> object:
    home = tmp_path / "home"
    conductor = home / ".conductor"
    conductor.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    return load_settings(
        {
            "HOME": str(home),
            "CONDUCTOR_HOME": str(conductor),
            "CONDUCTOR_REPO_ROOT": str(repo),
            "CONDUCTOR_BACKEND_PORT": "54321",
            "CONDUCTOR_WEB_PORT": "54322",
        }
    )


@pytest.fixture
def status_env(tmp_path, monkeypatch):
    settings = _status_settings(tmp_path)
    monkeypatch.setattr(ops_cli, "_fleet_or_raise", lambda system, s: SimpleNamespace(daemons=[]))
    monkeypatch.setattr(ops_runtime, "_cli_status_rows", lambda s: [])
    # The backend and the web console are running (a healthy stack).
    pids = {"backend.pid": 4242, "web.pid": 4243}
    monkeypatch.setattr(ops_runtime, "_read_pid", lambda path: pids.get(Path(path).name))
    monkeypatch.setattr(
        ops_runtime, "_owned", lambda system, pid, s, marker: pid in (4242, 4243)
    )
    return settings


def _ops_body(path: str, drained: bool, busy: dict[str, int] | None = None) -> str:
    return json.dumps(
        {
            "conductor_home": str(Path(path).parent),
            "drain": {"drained": drained, "path": path, "set_at": "", "reason": ""},
            "busy": busy or {},
        }
    )


def _status(settings, system, json_mode: bool = False) -> tuple[int, str]:
    import io
    from contextlib import redirect_stdout

    out = io.StringIO()
    with redirect_stdout(out):
        code = ops_cli.cmd_status(system, settings, json_mode=json_mode)
    return code, out.getvalue()


def test_status_shows_the_backends_drain(status_env) -> None:
    path = str(drain_path(status_env.conductor_home))
    system = Clock(ops=(200, _ops_body(path, True, {"g2": 1})))

    code, text = _status(status_env, system)
    assert code == 0  # drain does not change the exit code
    assert "drain (backend): on\n" in text
    assert "busy (backend): g2=1\n" in text
    assert "mismatch" not in text
    assert status_env.base_url + ops_runtime.OPS_STATUS_PATH in system.gets

    code, text = _status(status_env, system, json_mode=True)
    view = json.loads(text)["drain_backend"]
    assert code == 0
    assert view["reachable"] is True
    assert view["drained"] is True
    assert view["home_mismatch"] is False
    assert view["busy"] == {"g2": 1}


def test_status_warns_on_a_conductor_home_mismatch(status_env, tmp_path) -> None:
    other = str(tmp_path / "elsewhere" / "drain.json")
    system = Clock(ops=(200, _ops_body(other, False)))

    code, text = _status(status_env, system)
    assert code == 0
    assert "drain (backend): off\n" in text
    cli = drain_path(status_env.conductor_home)
    assert f"warning: CONDUCTOR_HOME mismatch: cli={cli} backend={other}" in text
    view = json.loads(_status(status_env, system, json_mode=True)[1])["drain_backend"]
    assert view["home_mismatch"] is True


@pytest.mark.parametrize(
    "ops",
    [None, (404, '{"detail":"Not Found"}'), (200, "not json"), (200, '{"drain": {}}')],
)
def test_status_backend_drain_unknown_when_it_does_not_answer(status_env, ops) -> None:
    set_drain(status_env.conductor_home, "deploy")
    code, text = _status(status_env, Clock(ops=ops))
    assert code == 0
    assert "drain: on" in text
    assert "drain (backend): unknown\n" in text
    assert "mismatch" not in text


def test_status_does_not_ask_a_backend_that_is_down(status_env, monkeypatch) -> None:
    monkeypatch.setattr(ops_runtime, "_owned", lambda system, pid, s, marker: False)
    system = Clock(ops=(200, _ops_body("/x/drain.json", True)))
    code, text = _status(status_env, system)
    assert "drain (backend): unknown\n" in text
    assert system.gets == []
