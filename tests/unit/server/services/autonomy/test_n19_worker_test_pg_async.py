"""N19 follow-up: async throwaway PG never blocks the loop; safe base paths."""

import asyncio
import threading
import time
from pathlib import Path

import pytest

from src.server.services.autonomy import worker_test_pg as wpg

REPO = Path(__file__).resolve().parents[5]


class SlowRunner:
    """Fake initdb/pg_ctl/createdb that block like the real tools do."""

    def __init__(self, delay: float = 0.15, gate: threading.Event | None = None):
        self.delay = delay
        self.gate = gate
        self.steps: list[str] = []
        self.threads: set[int] = set()

    def __call__(self, argv, *, env, timeout):
        argv = list(argv)
        tool = Path(argv[0]).name
        self.steps.append(tool + (":" + argv[-1] if tool == "pg_ctl" else ""))
        self.threads.add(threading.get_ident())
        if tool == "initdb":
            Path(argv[argv.index("-D") + 1]).mkdir(parents=True)
            if self.gate is not None:
                self.gate.wait(5)
        time.sleep(self.delay)  # blocking, as subprocess.run is
        return wpg.RunResult(0)


@pytest.fixture
def bins(tmp_path):
    bin_dir = tmp_path / "pgbin"
    bin_dir.mkdir()
    for name in ("initdb", "pg_ctl", "createdb"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 99\n")
        (bin_dir / name).chmod(0o755)
    return bin_dir


def test_heartbeat_keeps_running_while_a_slow_cluster_starts(tmp_path, bins):
    runner = SlowRunner(delay=0.15)
    base = tmp_path / "pg" / "t1"

    async def scenario():
        ticks = 0
        stop = asyncio.Event()

        async def heartbeat():
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        async with wpg.async_throwaway_pg(
            base, ticket_id="t1", runner=runner, bin_dir=bins,
            port_picker=lambda: 55432, enabled=True,
        ) as pg:
            ticks_after_start = ticks
            assert pg is not None and pg.env()["TEST_DB_PORT"] == "55432"
        stop.set()
        await beat
        return ticks_after_start, ticks

    ticks_after_start, ticks_total = asyncio.run(scenario())
    # Start blocks ~0.45 s (initdb + pg_ctl start + createdb); a blocked loop
    # would leave the heartbeat at one tick.
    assert ticks_after_start >= 10
    assert ticks_total > ticks_after_start  # stop ran off-loop too
    assert runner.steps == ["initdb", "pg_ctl:start", "createdb", "pg_ctl:stop"]
    assert threading.get_ident() not in runner.threads
    assert not base.exists()


def test_cancel_during_start_still_stops_the_cluster(tmp_path, bins):
    gate = threading.Event()
    runner = SlowRunner(delay=0.0, gate=gate)
    base = tmp_path / "pg" / "t2"

    async def scenario():
        async def run():
            async with wpg.async_throwaway_pg(
                base, ticket_id="t2", runner=runner, bin_dir=bins,
                port_picker=lambda: 55433, enabled=True,
            ):
                raise AssertionError("body must not run")

        task = asyncio.create_task(run())
        while "initdb" not in runner.steps:
            await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.02)
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert runner.steps == ["initdb", "pg_ctl:start", "createdb", "pg_ctl:stop"]
    assert not base.exists()


def test_async_disabled_yields_none_and_runs_nothing(tmp_path, bins, monkeypatch):
    monkeypatch.delenv(wpg.TEST_PG_ENV, raising=False)
    runner = SlowRunner()

    async def scenario():
        async with wpg.async_throwaway_pg(
            tmp_path / "pg", ticket_id="t", runner=runner, bin_dir=bins
        ) as pg:
            return pg

    assert asyncio.run(scenario()) is None
    assert runner.steps == []


@pytest.mark.parametrize("value", ["maybe", "enable", "2", "onn"])
def test_invalid_env_value_with_feature_off_gives_none_without_raising(
    tmp_path, bins, monkeypatch, value
):
    monkeypatch.setenv(wpg.TEST_PG_ENV, value)
    runner = SlowRunner()
    with wpg.throwaway_pg(tmp_path / "a", ticket_id="t", runner=runner, bin_dir=bins) as pg:
        assert pg is None

    async def scenario():
        async with wpg.async_throwaway_pg(
            tmp_path / "b", ticket_id="t", runner=runner, bin_dir=bins
        ) as apg:
            return apg

    assert asyncio.run(scenario()) is None
    assert runner.steps == []


@pytest.mark.parametrize(
    "ticket_id",
    ["../x/y", "../../etc", "..", ".", "/abs/path", "a\\b", "x" * 500, "", "t\x00z"],
)
def test_base_dir_stays_one_safe_component_under_root(tmp_path, ticket_id):
    root = tmp_path / "iso"
    path = wpg.throwaway_pg_base_dir(root, ticket_id)
    assert path.parent == root / "pg"
    assert path.resolve().is_relative_to((root / "pg").resolve())
    name = path.name
    assert name not in {"", ".", ".."}
    assert "/" not in name and "\\" not in name and ".." not in name
    assert all(ch.isalnum() or ch in "_-" for ch in name)
    assert len(name) <= 60


def test_base_dir_keeps_distinct_ids_distinct(tmp_path):
    a = wpg.throwaway_pg_base_dir(tmp_path, "a/b")
    b = wpg.throwaway_pg_base_dir(tmp_path, "a_b")
    assert a != b
    assert wpg.throwaway_pg_base_dir(tmp_path, "a/b") == a


def test_entered_in_async_runs_sync_manager_enter_and_exit():
    events: list[str] = []

    class Cm:
        def __enter__(self):
            events.append("enter")
            return 7

        def __exit__(self, *exc):
            events.append("exit:" + (exc[0].__name__ if exc[0] else "ok"))
            return False

    async def scenario():
        async with wpg.entered_in_async(Cm()) as value:
            assert value == 7
        with pytest.raises(KeyError):
            async with wpg.entered_in_async(Cm()):
                raise KeyError("x")

    asyncio.run(scenario())
    assert events == ["enter", "exit:ok", "enter", "exit:KeyError"]


def test_daemon_uses_the_async_manager_and_safe_path():
    source = (REPO / "scripts" / "conductor_local_daemon.py").read_text()
    assert "async with async_throwaway_pg(" in source
    assert "throwaway_pg_base_dir(isolation_root, ticket_id)" in source
    assert "with throwaway_pg(" not in source
    assert '/ "pg" / ticket_id' not in source
