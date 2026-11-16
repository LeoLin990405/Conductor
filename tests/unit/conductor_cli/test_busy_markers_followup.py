"""B5 follow-up: a leaked busy marker must not block ``restart --drain`` for ever.

Markers are written atomically and cleaned up on a failed write, unparsable
markers are pruned once they are older than a minute, a reused pid (same pid,
different start time) is stale, ``--force`` skips the busy wait, and the
timeout names the marker files. ``restart --drain`` also refuses when the
running backend answers and does not see the drain.

Every system is a fake; files live under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.conductor_cli import activity, ops_cli, ops_runtime
from src.conductor_cli.drain import is_drained, set_drain


class Clock:
    """``monotonic`` / ``sleep`` / ``http_get`` for the injected system."""

    def __init__(self, ops: tuple[int, str] | None = None) -> None:
        self.now = 0.0
        self.ops = ops
        self.gets: list[str] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def http_get(self, url: str, timeout: float) -> tuple[int, str]:
        self.gets.append(url)
        if url.endswith(ops_runtime.OPS_STATUS_PATH) and self.ops is not None:
            return self.ops
        return 0, "URLError"


@pytest.fixture
def restart_env(tmp_path, monkeypatch):
    """``cond restart --drain`` with the launch wait and restart recorded."""
    home = tmp_path / "conductor-home"
    home.mkdir()
    settings = SimpleNamespace(
        conductor_home=home,
        dispatch_wait_seconds=600.0,
        base_url="http://127.0.0.1:54321",
        sensitive=[],
    )
    events: list[tuple] = []

    def settle(system, s, action, options):
        events.append(("wait", is_drained(s.conductor_home)))

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
    return SimpleNamespace(settings=settings, events=events)


def _dir(home: Path) -> Path:
    directory = activity.busy_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# --- atomic write -------------------------------------------------------------


def test_a_failed_write_leaves_no_marker_behind(tmp_path, monkeypatch) -> None:
    def full_disk(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(activity.os, "replace", full_disk)
    with activity.busy(activity.G2, "x", home=tmp_path) as path:
        assert path is None
    assert list(activity.busy_dir(tmp_path).iterdir()) == []
    assert activity.busy_counts(tmp_path) == {}


def test_a_failed_write_after_the_file_was_created_is_cleaned_up(
    tmp_path, monkeypatch
) -> None:
    real_fdopen = os.fdopen

    class Broken:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()
            return False

        def write(self, text):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(
        activity.os, "fdopen", lambda fd, *a, **k: Broken(real_fdopen(fd, *a, **k))
    )
    with activity.busy(activity.G2, "x", home=tmp_path) as path:
        assert path is None
    assert list(activity.busy_dir(tmp_path).iterdir()) == []


def test_the_marker_is_renamed_into_place_from_a_dot_temp(tmp_path, monkeypatch) -> None:
    seen: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append((Path(src).name, Path(dst).name))
        real_replace(src, dst)

    monkeypatch.setattr(activity.os, "replace", spy)
    with activity.busy(activity.REVIEW, "x", home=tmp_path) as path:
        assert path is not None and path.exists()
    (temp, final), = seen
    assert temp.startswith(".") and temp.endswith(".tmp")
    assert final.endswith(".json") and not final.startswith(".")


def test_a_temp_file_is_not_read_as_a_marker(tmp_path) -> None:
    directory = _dir(tmp_path)
    (directory / f".{os.getpid()}-abc.json.tmp").write_text("")
    (directory / f".{os.getpid()}-abd.json").write_text("")
    assert activity.busy_counts(tmp_path) == {}


# --- unparsable markers -------------------------------------------------------


def test_an_old_unparsable_marker_is_pruned(tmp_path) -> None:
    directory = _dir(tmp_path)
    old = directory / "4242-dead.json"
    old.write_text("")
    past = time.time() - activity.UNPARSABLE_GRACE_SECONDS - 5
    os.utime(old, (past, past))
    assert activity.busy_counts(tmp_path) == {}
    assert not old.exists()


def test_a_fresh_unparsable_marker_still_counts(tmp_path) -> None:
    directory = _dir(tmp_path)
    fresh = directory / "4242-new.json"
    fresh.write_text("{")
    entries = activity.busy_entries(tmp_path)
    assert [entry["kind"] for entry in entries] == ["unknown"]
    assert entries[0]["path"] == str(fresh)
    assert fresh.exists()


# --- pid reuse -----------------------------------------------------------------


def _marker(directory: Path, pid: int, pid_start: str | None) -> Path:
    payload: dict[str, object] = {"kind": "merge", "pid": pid}
    if pid_start is not None:
        payload["pid_start"] = pid_start
    path = directory / f"{pid}-x.json"
    path.write_text(json.dumps(payload))
    return path


def test_a_reused_pid_is_stale(tmp_path) -> None:
    # pid 1 is always alive; its start time differs from the recorded one.
    path = _marker(_dir(tmp_path), 1, "Mon Jan  1 00:00:00 2001")
    entries = activity.busy_entries(tmp_path, start_time=lambda pid: "Tue Nov 11 2026")
    assert entries == []
    assert not path.exists()


def test_the_same_start_time_is_live(tmp_path) -> None:
    _marker(_dir(tmp_path), 1, "T0")
    entries = activity.busy_entries(tmp_path, start_time=lambda pid: "T0")
    assert [entry["kind"] for entry in entries] == ["merge"]


def test_an_unknown_start_time_keeps_the_marker(tmp_path) -> None:
    _marker(_dir(tmp_path), 1, "T0")
    entries = activity.busy_entries(tmp_path, start_time=lambda pid: None)
    assert [entry["kind"] for entry in entries] == ["merge"]


def test_the_marker_records_the_writer_start_time(tmp_path) -> None:
    with activity.busy(
        activity.G5, "x", home=tmp_path, start_time=lambda pid: "T-own"
    ) as path:
        assert path is not None
        assert json.loads(path.read_text())["pid_start"] == "T-own"


def test_the_real_start_time_reader_sees_this_process(tmp_path) -> None:
    first = activity.process_start_time(os.getpid())
    assert first
    assert activity.process_start_time(os.getpid()) == first
    assert activity.process_start_time(2**22 + 12345) is None
    # A marker this process writes is live under the real reader.
    with activity.busy(activity.LAUNCH, "x", home=tmp_path):
        assert activity.busy_counts(tmp_path) == {"launch": 1}


# --- restart --drain -----------------------------------------------------------


def test_force_skips_the_busy_wait(restart_env, capsys) -> None:
    home = restart_env.settings.conductor_home
    with activity.busy(activity.G2, "line-1", home=home):
        code = ops_cli.main(
            ["restart", "--drain", "--force", "--wait-timeout", "10"], system=Clock()
        )
    err = capsys.readouterr().err
    assert code == 0
    assert restart_env.events[-1][0] == "restart"
    assert "--force: not waiting for busy markers" in err
    assert not is_drained(home)


def test_a_leaked_empty_marker_does_not_block_a_restart(restart_env) -> None:
    home = restart_env.settings.conductor_home
    leaked = _dir(home) / "4242-dead.json"
    leaked.write_text("")
    past = time.time() - 120
    os.utime(leaked, (past, past))
    assert ops_cli.main(["restart", "--drain", "--wait-timeout", "10"], system=Clock()) == 0
    assert restart_env.events[-1][0] == "restart"


def test_the_busy_timeout_names_the_marker_paths(restart_env, capsys) -> None:
    home = restart_env.settings.conductor_home
    with activity.busy(activity.MERGE, "line-3", home=home) as path:
        code = ops_cli.main(["restart", "--drain", "--wait-timeout", "5"], system=Clock())
    err = capsys.readouterr().err
    assert code == 1
    assert str(path) in err
    assert "--force skips this wait" in err


def _ops(path: Path, drained: bool) -> tuple[int, str]:
    return 200, json.dumps({"drain": {"drained": drained, "path": str(path)}, "busy": {}})


def test_restart_refuses_when_the_backend_does_not_see_the_drain(
    restart_env, tmp_path, capsys
) -> None:
    home = restart_env.settings.conductor_home
    clock = Clock(ops=_ops(tmp_path / "elsewhere" / "drain.json", drained=False))
    code = ops_cli.main(["restart", "--drain"], system=clock)
    err = capsys.readouterr().err
    assert code == 1
    assert "does not see the drain" in err
    assert "CONDUCTOR_HOME differs" in err
    assert restart_env.events == []  # no launch wait, no restart
    assert not is_drained(home)
    assert any(url.endswith(ops_runtime.OPS_STATUS_PATH) for url in clock.gets)


def test_restart_keeps_an_operator_drain_when_it_refuses(restart_env, tmp_path) -> None:
    home = restart_env.settings.conductor_home
    set_drain(home, "operator")
    clock = Clock(ops=_ops(tmp_path / "elsewhere" / "drain.json", drained=False))
    assert ops_cli.main(["restart", "--drain"], system=clock) == 1
    assert is_drained(home)


def test_restart_proceeds_when_the_backend_sees_the_drain(restart_env) -> None:
    home = restart_env.settings.conductor_home
    clock = Clock(ops=_ops(home / "drain.json", drained=True))
    assert ops_cli.main(["restart", "--drain"], system=clock) == 0
    assert restart_env.events[-1] == ("restart", True)


@pytest.mark.parametrize("ops", [None, (503, "down")])
def test_restart_proceeds_when_the_backend_does_not_answer(restart_env, ops) -> None:
    assert ops_cli.main(["restart", "--drain"], system=Clock(ops=ops)) == 0
    assert restart_env.events[-1] == ("restart", True)
