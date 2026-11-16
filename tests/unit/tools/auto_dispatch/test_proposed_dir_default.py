"""The pending-dispatch directory lives under the Conductor state dir, not cwd.

Regression: ``proposed/auto-dispatch`` used to default to ``Path.cwd()`` at
import time, leaving an untracked directory in whatever checkout the backend
ran from.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.server.services.dispatch import DispatchSettings
from src.tools.auto_dispatch.approval import (
    FileApprovalGate,
    conductor_state_dir,
    default_proposed_dir,
)
from src.tools.auto_dispatch.types import Dispatch

_KEYS = ("CONDUCTOR_HOME", "XDG_STATE_HOME", "CONDUCTOR_DISPATCH_PROPOSED_DIR")


@pytest.fixture
def isolated(tmp_path, monkeypatch) -> Path:
    root = tmp_path.resolve()
    home = root / "home"
    work = root / "checkout"
    home.mkdir()
    work.mkdir()
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    return root


def test_conductor_home_wins(isolated, monkeypatch) -> None:
    monkeypatch.setenv("CONDUCTOR_HOME", str(isolated / "ch"))
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated / "xdg"))
    assert default_proposed_dir() == isolated / "ch" / "proposed" / "auto-dispatch"


def test_xdg_state_home_when_no_conductor_home(isolated, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated / "xdg"))
    assert conductor_state_dir() == isolated / "xdg" / "conductor"
    assert default_proposed_dir() == isolated / "xdg" / "conductor" / "proposed" / "auto-dispatch"


def test_falls_back_to_local_state_under_home(isolated) -> None:
    expected = isolated / "home" / ".local" / "state" / "conductor" / "proposed" / "auto-dispatch"
    assert default_proposed_dir() == expected


def test_blank_values_are_treated_as_unset(isolated, monkeypatch) -> None:
    monkeypatch.setenv("CONDUCTOR_HOME", "  ")
    monkeypatch.setenv("XDG_STATE_HOME", "")
    monkeypatch.setenv("CONDUCTOR_DISPATCH_PROPOSED_DIR", "")
    assert default_proposed_dir().is_relative_to(isolated / "home")


def test_explicit_override_is_preserved(isolated, monkeypatch) -> None:
    monkeypatch.setenv("CONDUCTOR_HOME", str(isolated / "ch"))
    monkeypatch.setenv("CONDUCTOR_DISPATCH_PROPOSED_DIR", str(isolated / "mine"))
    assert default_proposed_dir() == isolated / "mine"
    assert DispatchSettings.from_env().proposed_dir == isolated / "mine"


def test_never_under_cwd(isolated) -> None:
    cwd = Path.cwd().resolve()
    assert not default_proposed_dir().is_relative_to(cwd)
    assert not DispatchSettings().proposed_dir.is_relative_to(cwd)
    assert not DispatchSettings.from_env().proposed_dir.is_relative_to(cwd)
    assert not FileApprovalGate().root.is_relative_to(cwd)


def test_resolved_lazily_after_env_change(isolated, monkeypatch) -> None:
    monkeypatch.setenv("CONDUCTOR_HOME", str(isolated / "late"))
    expected = isolated / "late" / "proposed" / "auto-dispatch"
    assert FileApprovalGate().root == expected
    assert DispatchSettings().proposed_dir == expected
    assert DispatchSettings.from_env().proposed_dir == expected


def test_from_env_uses_the_given_mapping(isolated) -> None:
    env = {"CONDUCTOR_HOME": str(isolated / "mapped")}
    settings = DispatchSettings.from_env(env)
    assert settings.proposed_dir == isolated / "mapped" / "proposed" / "auto-dispatch"


@pytest.mark.asyncio
async def test_gate_writes_nothing_into_the_checkout(isolated, monkeypatch) -> None:
    monkeypatch.setenv("CONDUCTOR_HOME", str(isolated / "ch"))
    gate = FileApprovalGate()
    dispatch = Dispatch(id="d1", goal="g", user_id="u", created_at="now")
    await gate.request_approval(dispatch)
    assert not (isolated / "checkout" / "proposed").exists()
    assert (isolated / "ch" / "proposed" / "auto-dispatch" / "d1" / "summary.md").is_file()
