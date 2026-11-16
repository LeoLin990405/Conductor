"""Gate injection and the gates field on the dispatch summary."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.server.app.dispatch import DispatchSummary
from src.server.services.dispatch import DispatchService, DispatchSettings, dispatch_summary
from src.tools.auto_dispatch import Dispatch, FileApprovalGate, InMemoryDispatchStore
from src.tools.auto_dispatch.gates import DEFAULT_RISK_GLOBS, GateRunner

pytestmark = pytest.mark.asyncio


def _service(tmp_path: Path, **settings) -> DispatchService:
    return DispatchService(
        DispatchSettings(
            proposed_dir=tmp_path / "proposed",
            runs_dir=tmp_path / "runs",
            repo_root=tmp_path / "repo",
            **settings,
        ),
        store=InMemoryDispatchStore(),
        gate=FileApprovalGate(tmp_path / "proposed"),
    )


def _dispatch() -> Dispatch:
    return Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")


async def test_dev_does_not_inject_a_gate_runner(tmp_path: Path) -> None:
    service = _service(tmp_path, adapters="dev")
    machine = service._machine(_dispatch())
    assert machine._c.gates is None


async def test_live_off_does_not_inject_a_gate_runner(tmp_path: Path) -> None:
    service = _service(tmp_path, adapters="live", gates_mode="off")
    machine = service._machine(_dispatch())
    assert machine._c.gates is None


async def test_live_injects_a_gate_runner(tmp_path: Path) -> None:
    service = _service(tmp_path, adapters="live", gates_mode="on", g5_command="pytest -q")
    machine = service._machine(_dispatch())
    assert isinstance(machine._c.gates, GateRunner)


async def test_settings_read_the_gate_environment() -> None:
    settings = DispatchSettings.from_env(
        {
            "CONDUCTOR_DISPATCH_GATES": "off",
            "CONDUCTOR_DISPATCH_G2_COMMAND": "pytest -q tests/unit",
            "CONDUCTOR_DISPATCH_G2_TIMEOUT_SECONDS": "12",
            "CONDUCTOR_DISPATCH_G5_COMMAND": "pytest -q",
            "CONDUCTOR_DISPATCH_G5_TIMEOUT_SECONDS": "30",
            "CONDUCTOR_DISPATCH_RISK_GLOBS": "migrations/**, src/server/app/**",
        }
    )
    assert settings.gates_mode == "off"
    assert settings.g2_command == "pytest -q tests/unit"
    assert settings.g2_timeout_seconds == 12.0
    assert settings.g5_command == "pytest -q"
    assert settings.g5_timeout_seconds == 30.0
    assert settings.risk_globs == ("migrations/**", "src/server/app/**")
    defaults = DispatchSettings.from_env({})
    assert defaults.gates_mode == "on"
    assert defaults.g2_command is None
    assert defaults.g5_command is None
    assert defaults.g2_timeout_seconds == 900.0
    assert defaults.g5_timeout_seconds == 3600.0
    assert defaults.risk_globs == DEFAULT_RISK_GLOBS


async def test_dispatch_summary_includes_gates_and_the_api_model_accepts_them() -> None:
    dispatch = _dispatch()
    dispatch.gates = {"schema_version": "dispatch_gates_v1", "lines": {}}
    summary = dispatch_summary(dispatch)
    assert summary["gates"]["schema_version"] == "dispatch_gates_v1"
    model = DispatchSummary(**summary)
    assert model.gates == dispatch.gates
    empty = dispatch_summary(_dispatch())
    assert empty["gates"] == {}
    assert DispatchSummary(**empty).gates == {}
