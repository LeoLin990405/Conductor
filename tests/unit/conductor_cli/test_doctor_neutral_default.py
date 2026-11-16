"""Doctor's main-clone marker default carries no personal workspace name."""

from __future__ import annotations

from src.conductor_cli.doctor import _MAIN_CLONE, _main_clone_marker


def test_doctor_main_clone_default_contains_no_personal_workspace_name(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CONDUCTOR_MAIN_CLONE_MARKER", raising=False)
    assert "AlphaCN" not in _MAIN_CLONE
    assert "AlphaCN" not in _main_clone_marker()
