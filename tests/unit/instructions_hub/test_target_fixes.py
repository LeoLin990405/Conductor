"""MiniMax and StepFun global instruction files, with legacy detection."""

from __future__ import annotations

from pathlib import Path

from src.instructions_hub.store import status
from src.instructions_hub.targets import get_target, target_path


def test_mcode_reads_minimax_data_dir(home: Path) -> None:
    target = get_target("minimax")
    # mcode reads $MINIMAX_DATA_DIR/AGENTS.md (default ~/.minimax).
    assert target_path(target, home, environ={}) == (home / ".minimax" / "AGENTS.md").resolve()
    assert (".mcode", "AGENTS.md") in target.legacy


def test_step_reads_stepcode_agent_dir(home: Path) -> None:
    target = get_target("stepfun")
    expected = (home / ".stepcode" / "agent" / "AGENTS.md").resolve()
    assert target_path(target, home, environ={}) == expected
    moved = home / "custom-step"
    assert target_path(
        target, home, environ={"STEP_CODING_AGENT_DIR": str(moved)}
    ) == (moved / "AGENTS.md").resolve()
    assert (".step", "AGENTS.md") in target.legacy


def test_status_reports_a_legacy_file(home: Path) -> None:
    old = home / ".mcode" / "AGENTS.md"
    old.parent.mkdir(parents=True)
    old.write_text("old\n", encoding="utf-8")
    report = status(home, targets=["mcode", "step"], environ={})
    rows = {row["target"]: row for row in report["targets"]}
    assert rows["mcode"]["legacy"] == [str(home.resolve() / ".mcode" / "AGENTS.md")]
    assert "legacy" not in rows["step"]
