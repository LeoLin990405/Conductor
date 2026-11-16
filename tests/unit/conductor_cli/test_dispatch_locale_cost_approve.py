"""Wave 2026-11-14 z/fixes: approve output, status locale, dev-mode cost outcome."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conductor_cli import cli
from conductor_cli.dispatch import _dispatch, render_status
from conductor_cli.dispatch_timeline import display_lang, render_timeline_lines, timeline_of
from src.tools.auto_dispatch import cost
from src.tools.auto_dispatch.types import Dispatch, DispatchState, Line, LineStatus

FIXTURE = Path(__file__).resolve().parents[3] / "tests/fixtures/dispatch_gates_v1.json"
CJK = re.compile(r"[一-鿿]")
T0 = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class _Client:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, object]] = []

    def get(self, path: str, *, params=None):
        self.calls.append(("GET", path, params))
        return self.payload

    def post(self, path: str, json=None):
        self.calls.append(("POST", path, json))
        return self.payload

    def close(self) -> None:
        return None


def _set_locale(monkeypatch, value: str) -> None:
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", value)


# (b) approve ---------------------------------------------------------------


def _approved() -> dict:
    payload = _fixture()
    payload["state"] = "MERGE"
    return payload


def test_approve_human_view_is_a_summary_not_a_repr(monkeypatch) -> None:
    _set_locale(monkeypatch, "en_US.UTF-8")
    payload = _approved()
    ns = argparse.Namespace(args=["approve", "d3adbeef"], note=None, force=False, json=False)
    result = _dispatch(ns, _Client(payload))
    text = "\n".join(result["rendered"])
    assert text.splitlines()[0] == (
        "approved d3adbeef: state MERGE, 1 line(s), branch integrate/auto-dispatch-d3adbeef"
    )
    assert "{'" not in text and "': " not in text
    assert "tok-1" not in text


def test_approve_json_is_the_server_object_unchanged() -> None:
    payload = _approved()
    ns = argparse.Namespace(args=["approve", "d3adbeef"], note=None, force=False, json=True)
    assert _dispatch(ns, _Client(payload)) is payload


def test_approve_cli_prints_json_with_flag_and_summary_without(monkeypatch, capsys) -> None:
    _set_locale(monkeypatch, "en_US.UTF-8")
    payload = _approved()

    class _API(_Client):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__(payload)

    monkeypatch.setattr(cli, "APIClient", _API)
    assert cli.main(["dispatch", "approve", "d3adbeef"]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("approved d3adbeef: state MERGE")
    assert "{'" not in printed

    assert cli.main(["--json", "dispatch", "approve", "d3adbeef"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["data"]["id"] == "d3adbeef"
    assert envelope["data"]["state"] == "MERGE"
    assert "rendered" not in envelope["data"]


# (c) status follows the locale ----------------------------------------------


def test_display_lang_precedence() -> None:
    assert display_lang({}) == "en"
    assert display_lang({"LANG": "en_US.UTF-8"}) == "en"
    assert display_lang({"LANG": "zh_CN.UTF-8"}) == "zh"
    assert display_lang({"LC_ALL": "C", "LANG": "zh_CN.UTF-8"}) == "en"
    assert display_lang({"LC_ALL": "", "LC_MESSAGES": "zh_TW", "LANG": "en_US"}) == "zh"


def test_english_locale_status_has_no_chinese_labels(monkeypatch) -> None:
    _set_locale(monkeypatch, "en_US.UTF-8")
    text = "\n".join(render_status(_fixture()))
    assert not CJK.search(text), [line for line in text.splitlines() if CJK.search(line)]
    assert "primary review  pass  review_merged" in text
    assert "secondary review  pass  review_merged" in text
    assert "G6 human  pending approval  awaiting_human  current" in text


def test_english_locale_unreached_and_labels(monkeypatch) -> None:
    _set_locale(monkeypatch, "en_US.UTF-8")
    lines = render_timeline_lines({"phase": "spec_review"})
    assert lines[0] == "    G0 SPEC  current"
    assert sum("unreached" in line for line in lines) == 7
    assert [slot["label"] for slot in timeline_of({"phase": "spec_review"})][2:4] == [
        "implementing",
        "G2 focused tests",
    ]


def test_zh_locale_keeps_chinese_labels(monkeypatch) -> None:
    _set_locale(monkeypatch, "zh_CN.UTF-8")
    text = "\n".join(render_status(_fixture()))
    assert "G6 人工  待审批  awaiting_human  当前" in text


def test_explicit_lang_overrides_environment(monkeypatch) -> None:
    _set_locale(monkeypatch, "zh_CN.UTF-8")
    assert not CJK.search("\n".join(render_timeline_lines({"phase": "g2"}, lang="en")))


# (g) cost outcome of a dev-mode COMPLETE dispatch -----------------------------


def _dev_mode_complete() -> Dispatch:
    """Dev mode: lines PASSED, dispatch COMPLETE, no integration merge recorded."""
    gates: dict = {"schema_version": "dispatch_gates_v1", "lines": {}}
    cost.record_attempt_start(
        gates, "line-0", run_id="r0", attempt=1, model="kimi", kind="work",
        fix_round=0, started_at=T0,
    )
    cost.record_attempt_end(
        gates, "line-0", run_id="r0", ended_at=T0 + timedelta(seconds=60),
        result="ok", failure_class=None, reason_code=None, tokens_est=600,
    )
    lines = [
        Line(id="line-0", name="line-0", task="t", assigned_model="kimi",
             status=LineStatus.PASSED),
        Line(id="line-1", name="line-1", task="t", assigned_model="glm",
             status=LineStatus.PASSED),
    ]
    return Dispatch(id="dev1", goal="g", user_id="u", created_at="", lines=lines,
                    state=DispatchState.COMPLETE, gates=gates)


def test_passed_line_of_complete_dispatch_is_passed_not_open() -> None:
    report = cost.dispatch_cost_report(_dev_mode_complete())
    assert [row["outcome"] for row in report["lines"]] == ["passed", "passed"]
    assert report["counts"]["passed"] == 2 and report["counts"]["open"] == 0
    kimi = report["attempts"]["line-0"]
    assert [(row["use"], row["waste_class"]) for row in kimi] == [("useful", None)]


def test_passed_line_of_running_dispatch_is_still_open() -> None:
    dispatch = _dev_mode_complete()
    dispatch.state = DispatchState.MONITORING
    assert cost.line_outcome(dispatch, dispatch.lines[0]) == "open"


def test_cost_cli_renders_passed_outcome() -> None:
    report = cost.dispatch_cost_report(_dev_mode_complete())
    ns = argparse.Namespace(args=["cost", "dev1"], note=None, force=False, json=False)
    rendered = _dispatch(ns, _Client(report))["rendered"]
    assert "passed 2" in rendered[0]
    assert rendered[2].split()[:2] == ["line-0", "passed"]
    assert not any(row.split()[1:2] == ["open"] for row in rendered[2:4])


@pytest.mark.parametrize("status", [LineStatus.BLOCKED, LineStatus.FAILED])
def test_complete_dispatch_keeps_blocked_and_failed(status) -> None:
    dispatch = _dev_mode_complete()
    dispatch.lines[1].status = status
    assert cost.line_outcome(dispatch, dispatch.lines[1]) == status.value.lower()
