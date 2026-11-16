"""Human rendering for ``conductor dispatch status``. ``--json`` stays raw."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conductor_cli import cli
from conductor_cli.dispatch import _active, _status, render_status


@pytest.fixture(autouse=True)
def _zh_locale(monkeypatch):
    """These vectors assert the zh labels; en labels: test_dispatch_locale_cost_approve."""
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")


FIXTURE = Path(__file__).resolve().parents[3] / "tests/fixtures/dispatch_gates_v1.json"
FORBIDDEN = (
    "Add GET /healthz",
    "tok-1",
    "gates/d3adbeef/line-0/g2-2-0.log",
    "primary_verdict",
)


class _Client:
    def __init__(self, payload):
        self.payload = payload

    def get(self, path):
        return self.payload


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _text(summary: dict) -> str:
    return "\n".join(render_status(summary))


def test_json_status_is_the_server_object_unchanged():
    payload = _fixture()
    assert _status("d3adbeef", _Client(payload), as_json=True) is payload


def test_json_active_is_the_server_object_unchanged():
    payload = {"dispatches": [_fixture()]}
    assert _active(_Client(payload), as_json=True) is payload


def test_fixture_roster_dag_and_outer_gate_fields():
    text = _text(_fixture())
    assert "d3adbeef  APPROVAL  75%  integrate/auto-dispatch-d3adbeef" in text
    assert "line-0  PASSED  kimi  awaiting_human  1  MERGE  —" in text
    assert "L0: line-0" in text
    assert "token issued" in text
    assert "conductor dispatch approve d3adbeef" in text
    for secret in FORBIDDEN:
        assert secret not in text
    assert "specs/9f3a1c7e5b2d4086" not in text
    assert "G1" not in text and "G3" not in text and "G4" not in text
    assert "G0 SPEC  pass  planner_output_valid  spec_submitted → spec_accepted" in text
    assert "主审  pass  review_merged  review_blocked → fix_resubmitted → primary_review_passed" in text
    assert "副审  pass  review_merged" in text
    assert "G6 人工  待审批  awaiting_human  当前" in text


def test_phase_only_marks_one_named_position():
    text = _text({"lines": [{"id": "line-0"}], "gates": {"lines": {"line-0": {"phase": "spec_review"}}}})
    assert "G0 SPEC  当前" in text
    assert text.count("未到达") == 7
    assert "G1" not in text and "G3" not in text and "G4" not in text


def test_lowercase_verdicts_display_as_merge_or_block():
    summary = _fixture()
    summary["gates"]["lines"]["line-0"]["gates"]["primary_review"]["verdict"] = "block"
    roster = [line for line in _text(summary).splitlines() if line.startswith("line-0")]
    assert roster == ["line-0  PASSED  kimi  awaiting_human  1  BLOCK  —"]
    assert "approve" not in roster[0]


def test_missing_gates_render_as_dash():
    summary = _fixture()
    summary["gates"] = {}
    summary.pop("approval_token", None)
    text = _text(summary)
    assert "line-0  PASSED  kimi  —  —  —  —" in text
    assert "token issued" not in text


def test_unassigned_falls_back_to_failure_reason():
    summary = _fixture()
    summary["lines"][0]["assigned_model"] = ""
    summary["lines"][0]["failure_reason"] = "pool empty\nsecond line"
    text = _text(summary)
    assert "unassigned" in text
    assert "line-0  pool empty" in text
    assert "second line" not in text


def test_missing_dependency_and_cycle_do_not_raise():
    missing = _fixture()
    missing["lines"][0]["depends_on"] = ["ghost"]
    assert "⚠ missing dep line-0 ← ghost" in _text(missing)
    cycle = _fixture()
    cycle["lines"][0]["depends_on"] = ["line-0"]
    assert "⚠ cycle line-0" in _text(cycle)


def test_local_fake_record_is_labelled():
    text = _text({"id": "local", "fake": True, "state": "SUBMITTED", "lines": []})
    assert text.startswith("(local fake record)")


def test_active_is_one_line_per_dispatch():
    payload = {"active": [{"id": "d3adbeef", "state": "APPROVAL", "progress_pct": 75, "goal": "Add a /healthz endpoint with tests"}]}
    lines = _active(_Client(payload), as_json=False)["lines"]
    assert lines == ["d3adbeef  APPROVAL  75%  Add a /healthz endpoint with tests"]


def test_failure_reason_strips_terminal_controls():
    summary = _fixture()
    summary["lines"][0]["assigned_model"] = None
    summary["lines"][0]["failure_reason"] = "\x1b[31mbad reason"
    assert "bad reason" in _text(summary)
    assert "\x1b" not in _text(summary)


def test_non_json_status_keeps_every_server_key_and_prints_the_view(monkeypatch, capsys) -> None:
    """Human mode keeps the pre-W5 record and prints only the rendered lines."""
    payload = _fixture()

    class _API(_Client):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__(payload)

        def close(self) -> None:
            return None

    monkeypatch.setattr(cli, "APIClient", _API)
    assert cli.main(["dispatch", "status", payload["id"]]) == 0
    printed = capsys.readouterr().out
    assert "G6 人工  待审批  awaiting_human  当前" in printed
    assert "tok-1" not in printed
    assert "Add GET /healthz" not in printed

    args = cli.build_parser().parse_args(["dispatch", "status", payload["id"]])
    shown = args.handler(args, _Client(payload))
    assert set(payload) <= set(shown)
    assert "rendered" in shown
    for key, value in payload.items():
        assert shown[key] == value
    assert all(isinstance(line, str) for line in shown["rendered"])


def test_human_status_returns_string_lines_for_output():
    payload = _fixture()
    rendered = _status("d3adbeef", _Client(payload), as_json=False)
    assert set(payload) <= set(rendered)
    for key, value in payload.items():
        assert rendered[key] == value
    assert isinstance(rendered["rendered"], list)
    assert all(isinstance(line, str) for line in rendered["rendered"])
    assert "approval_token" not in "\n".join(rendered["rendered"])
