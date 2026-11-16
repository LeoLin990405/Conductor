"""N31: live dispatches run independent lines in parallel; dev drills stay serial."""

from __future__ import annotations

from src.server.services.dispatch import DispatchService, DispatchSettings
from src.tools.auto_dispatch import Dispatch
from src.tools.auto_dispatch.dev import InMemoryDispatchStore


def test_max_parallel_lines_comes_from_the_environment() -> None:
    assert DispatchSettings.from_env({}).max_parallel_lines == 4
    env = {"CONDUCTOR_DISPATCH_MAX_PARALLEL_LINES": "2"}
    assert DispatchSettings.from_env(env).max_parallel_lines == 2
    for bad in ("0", "-1", "many", " "):
        settings = DispatchSettings.from_env({"CONDUCTOR_DISPATCH_MAX_PARALLEL_LINES": bad})
        assert settings.max_parallel_lines == 4


def _machine(tmp_path, adapters: str):
    (tmp_path / "repo").mkdir(exist_ok=True)
    settings = DispatchSettings(
        adapters=adapters,
        runs_dir=tmp_path / "runs",
        repo_root=tmp_path / "repo",
        proposed_dir=tmp_path / "proposed",
        max_parallel_lines=3,
        gates_mode="off",
    )
    service = DispatchService(settings, store=InMemoryDispatchStore())
    dispatch = Dispatch(id="d", goal="g", user_id="u", created_at="now")
    return service._machine(dispatch)


def test_live_machines_get_the_parallel_limit_and_dev_stays_serial(tmp_path) -> None:
    assert _machine(tmp_path, "live")._config.max_parallel_lines == 3
    assert _machine(tmp_path, "dev")._config.max_parallel_lines == 1
