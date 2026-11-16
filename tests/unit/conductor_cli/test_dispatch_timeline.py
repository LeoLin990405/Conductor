"""Shared timeline vectors. CLI and TUI both render ``timeline_of``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conductor_cli.dispatch import render_status
from conductor_cli.dispatch_timeline import current_position, render_timeline_lines, timeline_of
from ptc_cli.superagent.dispatch_view import render_dispatch_lines


@pytest.fixture(autouse=True)
def _zh_locale(monkeypatch):
    """These vectors assert the zh labels; en labels: test_dispatch_locale_cost_approve."""
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")


VECTORS = Path(__file__).resolve().parents[3] / "tests/fixtures/dispatch_timeline_vectors_v1.json"


def _vectors() -> list[dict]:
    return json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]


def test_shared_vectors_agree_on_position_and_status():
    for vector in _vectors():
        record = vector["record"]
        slots = timeline_of(record)
        assert current_position(record)[0] == vector["current"], vector["id"]
        for expected in vector["slots"]:
            got = next(slot for slot in slots if slot["id"] == expected["id"])
            assert got["status"] == expected["status"], (vector["id"], expected["id"], got["status"])
            assert got["current"] is expected["current"], (vector["id"], expected["id"])
        for slot_id, notes in vector.get("notes", {}).items():
            got = next(slot for slot in slots if slot["id"] == slot_id)
            assert got["notes"] == notes, vector["id"]
        assert all(slot["id"] not in {"g1", "g3", "g4"} for slot in slots)


def test_human_rejection_without_a_stage_event_stops_on_g6():
    """The record ``mark_rejected`` writes: G6 fail, phase blocked, no blocked event."""
    record = {
        "phase": "blocked",
        "reason": "human_rejected",
        "machine": {"stage": "blocked", "return_stage": None},
        "gates": {
            "G0": {"result": "pass", "reason": "planner_output_valid"},
            "D1": {"result": "pass", "reason": "planner_output_valid"},
            "G2": {"result": "skipped", "reason": "g2_no_commands"},
            "G5": {"result": "pass", "reason": "g5_passed"},
            "G6": {"result": "fail", "reason": "human_rejected"},
        },
        "history": [],
        "events": [
            {
                "from_stage": "awaiting_human",
                "to_stage": "awaiting_human",
                "gate": "G6",
                "result": "pending",
                "reason": "awaiting_human",
            }
        ],
    }
    assert current_position(record) == ("g6", "blocked")
    g6 = next(line for line in render_timeline_lines(record) if "G6" in line)
    assert "未到达" not in g6
    assert "阻塞" in g6 or "拒绝" in g6 or "已取消" in g6


def test_cancel_stays_on_g6_in_cli_and_tui():
    record = next(item["record"] for item in _vectors() if item["id"] == "w0-cancel")
    summary = {"id": "d3adbeef", "lines": [{"id": "line-0"}], "gates": {"lines": {"line-0": record}}}
    lines = render_timeline_lines(record)
    for text in ("\n".join(render_status(summary)), "\n".join(render_dispatch_lines(summary))):
        for line in lines:
            assert line in text
        primary = next(line for line in text.splitlines() if "主审" in line)
        assert "当前" not in primary
        assert "G6 人工  已取消" in text
        assert "当前" in next(line for line in text.splitlines() if "G6 人工" in line)
