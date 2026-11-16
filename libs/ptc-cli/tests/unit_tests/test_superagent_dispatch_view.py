"""TUI dispatch roster. The view is fetched on command, not on the heartbeat."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

# dispatch_view imports conductor_cli, which lives in the repo src tree. The
# ptc-cli lockfile's conductor-core package is a different PyPI project.
_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent import dispatch_view as dispatch_view_module  # noqa: E402
from ptc_cli.superagent.client import SuperAgentClient  # noqa: E402
from ptc_cli.superagent.dispatch_view import STRINGS, render_dispatch_lines, render_dispatch_view  # noqa: E402
from ptc_cli.superagent.tui_completion import SLASH_COMMANDS  # noqa: E402
from ptc_cli.superagent.tui_i18n import check_parity  # noqa: E402
from tests.unit_tests.tui_layout_contract import check_layout  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[4] / "tests/fixtures/dispatch_gates_v1.json"
FORBIDDEN = ("Add GET /healthz", "tok-1", "gates/d3adbeef/line-0/g2-2-0.log", "primary_verdict")


def test_slash_command_is_advertised():
    assert "/dispatch" in SLASH_COMMANDS


def test_fixture_matches_the_cli_roster(monkeypatch):
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")  # the labels follow the locale
    summary = json.loads(FIXTURE.read_text(encoding="utf-8"))
    text = "\n".join(render_dispatch_lines(summary))
    assert "line-0  PASSED  kimi  awaiting_human  1  MERGE  —" in text
    assert "主审  pass  review_merged  review_blocked → fix_resubmitted → primary_review_passed" in text
    assert "副审  pass  review_merged" in text
    assert "G6 人工  待审批  awaiting_human  当前" in text
    assert "G1" not in text and "G3" not in text and "G4" not in text  # noqa: PT018
    assert "L0: line-0" in text
    assert "token issued" in text
    assert "conductor dispatch approve d3adbeef" in text
    for secret in FORBIDDEN:
        assert secret not in text


def test_cycle_and_missing_dep_do_not_raise():
    missing = {"lines": [{"id": "line-0", "depends_on": ["ghost"]}]}
    assert "⚠ missing dep line-0 ← ghost" in "\n".join(render_dispatch_lines(missing))
    cycle = {"lines": [{"id": "line-0", "depends_on": ["line-0"]}]}
    assert "⚠ cycle line-0" in "\n".join(render_dispatch_lines(cycle))


def _client(handler) -> SuperAgentClient:
    client = SuperAgentClient("http://test", "u1")
    client._client = httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_dispatch_404_is_empty_and_active_404_is_an_empty_list():
    async def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "nope"})

    client = _client(missing)
    assert await client.dispatch("missing") is None
    assert await client.active_dispatches() == []
    await client.close()


@pytest.mark.asyncio
async def test_active_reads_the_dispatches_envelope():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status/active"):
            return httpx.Response(200, json={"dispatches": [{"id": "d1"}]})
        return httpx.Response(200, json={"id": "d1", "state": "RUNNING"})

    client = _client(handler)
    assert await client.active_dispatches() == [{"id": "d1"}]
    assert (await client.dispatch("d1"))["state"] == "RUNNING"
    await client.close()


# --- terminal-native /dispatch (SPEC §7.3) ------------------------------------

_WIDTHS = (40, 80, 120)
_LOCALES = ("en", "zh-CN")
_FRAMES = Path(__file__).resolve().parents[4] / "docs/design/terminal-native/tui"
_DISPATCH_ID = "7f3a91c2"
_STRING_KEYS = (
    "title",
    "roster",
    "dag",
    "unassigned",
    "col.line",
    "col.status",
    "col.model",
    "col.gate",
    "col.fix",
    "col.verdict",
    "col.depends",
    "state.SUBMITTED",
    "state.PLANNING",
    "state.ASSIGNED",
    "state.LAUNCHING",
    "state.MONITORING",
    "state.REVIEW",
    "state.APPROVAL",
    "state.MERGE",
    "state.COMPLETE",
    "state.ABORT",
    "state.ERROR",
    "waiting",
    "gate.planning",
    "gate.spec_review",
    "gate.reviewed",
    "gate.build",
    "gate.assigned",
    "gate.pending",
    "gate.dag_ready",
    "gate.focused_tests",
    "gate.primary_review",
    "gate.fixing",
    "gate.secondary_review",
    "gate.integration_checks",
    "gate.awaiting_adjudication",
    "gate.merged",
    "gate.replanning",
    "gate.blocked",
    "gate.cancelled",
    "gate.passed",
    "gate.failed",
    "gate.running",
    "gate.merging",
    "gate.none",
    "verdict.MERGE",
    "verdict.BLOCK",
    "verdict.none",
    "empty",
    "unassigned.pool",
)
# Words the two locales must not share. A copied English table still has parity.
_TRANSLATED = (
    "title",
    "roster",
    "dag",
    "unassigned",
    "col.line",
    "col.status",
    "col.model",
    "col.gate",
    "col.fix",
    "col.verdict",
    "col.depends",
    "state.SUBMITTED",
    "state.PLANNING",
    "state.ASSIGNED",
    "state.LAUNCHING",
    "state.MONITORING",
    "state.REVIEW",
    "state.APPROVAL",
    "state.MERGE",
    "state.COMPLETE",
    "state.ABORT",
    "state.ERROR",
    "waiting",
    "gate.planning",
    "gate.spec_review",
    "gate.reviewed",
    "gate.build",
    "gate.assigned",
    "gate.pending",
    "gate.dag_ready",
    "gate.focused_tests",
    "gate.primary_review",
    "gate.fixing",
    "gate.secondary_review",
    "gate.integration_checks",
    "gate.awaiting_adjudication",
    "gate.merged",
    "gate.replanning",
    "gate.blocked",
    "gate.cancelled",
    "gate.passed",
    "gate.failed",
    "gate.running",
    "gate.merging",
    "verdict.MERGE",
    "verdict.BLOCK",
    "unassigned.pool",
)
_LOOKUPS = (
    "title",
    "roster",
    "dag",
    "unassigned",
    "col.line",
    "col.status",
    "col.model",
    "col.gate",
    "col.fix",
    "col.verdict",
    "col.depends",
    "state.APPROVAL",
    "waiting",
    "gate.reviewed",
    "gate.build",
    "verdict.MERGE",
    "verdict.BLOCK",
    "unassigned.pool",
)

_FRAME_PAYLOAD = {
    "id": _DISPATCH_ID,
    "state": "APPROVAL",
    "progress_pct": 62,
    "integrate_branch": "integrate/r2",
    "goal": {
        "en": "Ship the terminal-native review queue screen",
        "zh-CN": "把终端原生的评审队列屏幕上线",
    },
    "human_gate": "G6",
    "lines": [
        {
            "id": "L1",
            "status": "completed",
            "assigned_model": "kimi",
            "gate": "reviewed",
            "fix_round": 1,
            "verdict": "MERGE",
            "depends_on": [],
        },
        {
            "id": "L2",
            "status": "running",
            "assigned_model": "deepseek",
            "gate": "build",
            "fix_round": 0,
            "verdict": "—",
            "depends_on": ["L1"],
        },
        {
            "id": "L3",
            "status": "blocked",
            "assigned_model": "cursor",
            "gate": "—",
            "fix_round": 0,
            "verdict": "BLOCK",
            "depends_on": ["L2"],
        },
        {
            "id": "L4",
            "status": "queued",
            "assigned_model": "minimax",
            "gate": "—",
            "fix_round": 0,
            "verdict": "—",
            "depends_on": ["L2"],
        },
    ],
    # The design frames draw L4 beside L2 even though its depends cell is L2.
    "layers": [["L1"], ["L2", "L4"], ["L3"]],
    "unassigned": [{"id": "L4", "reason": "pool"}],
}

_ACTIVE = {
    "id": _DISPATCH_ID,
    "state": "RUNNING",
    "progress_pct": 62,
    "integrate_branch": "integrate/r2",
    "goal": "Ship the terminal-native review queue screen",
    "lines": [
        {
            "id": "L1",
            "status": "completed",
            "provider": "kimi",
            "model": "moonshot-v1",
            "gate": "reviewed",
            "fix_round": 1,
            "verdict": "MERGE",
            "depends_on": [],
        },
        {
            "id": "L2",
            "status": "running",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "gate": "build",
            "fix_round": 0,
            "verdict": "—",
            "depends_on": ["L1"],
        },
        {
            "id": "L3",
            "status": "blocked",
            "provider": "cursor",
            "model": "composer",
            "gate": "—",
            "fix_round": 0,
            "verdict": "BLOCK",
            "depends_on": ["L2"],
        },
        {
            "id": "L4",
            "status": "queued",
            "provider": "minimax",
            "model": "MiniMax-M2",
            "gate": "—",
            "fix_round": 0,
            "verdict": "—",
            "depends_on": ["L1"],
        },
    ],
    "unassigned": [{"id": "L4", "reason": "pool-down"}],
}

_AWAITING = {
    **_ACTIVE,
    "state": "awaiting_human",
    "phase": "awaiting_human",
    "human_gate": "G6",
}


def _render(payload):
    def render(width, locale):
        return render_dispatch_view(payload, width, locale)

    return render


def test_active_dispatch_holds_the_layout_contract():
    check_layout(
        _render(_ACTIVE),
        widths=_WIDTHS,
        locales=_LOCALES,
        keep=(_DISPATCH_ID, "integrate/r2", "L0: L1", "L1: L2, L4", "L2: L3", "pool-down"),
        states=("completed", "running", "blocked", "queued"),
        columns=("kimi", "moonshot-v1", "deepseek-chat", "cursor", "composer", "minimax", "MiniMax-M2"),
    )
    english = "\n".join(render_dispatch_view(_ACTIVE, 80, "en"))
    chinese = "\n".join(render_dispatch_view(_ACTIVE, 80, "zh-CN"))
    assert "✔ MERGE" in english
    assert "✖ BLOCK" in english
    assert "reviewed" in english
    assert "build" in english
    assert "✔ 合并" in chinese
    assert "✖ 阻断" in chinese
    assert "已评审" in chinese
    assert "构建中" in chinese
    assert "MERGE" not in chinese
    assert "BLOCK" not in chinese
    assert f"approve {_DISPATCH_ID}" not in english


def test_awaiting_human_dispatch_holds_the_layout_contract():
    approve = f"approve {_DISPATCH_ID}"
    reject = f"reject {_DISPATCH_ID}"
    check_layout(
        _render(_AWAITING),
        widths=_WIDTHS,
        locales=_LOCALES,
        keep=(approve, reject, _DISPATCH_ID, "L0: L1"),
        states=("completed", "running", "blocked", "queued"),
        columns=(approve, reject, "kimi", "moonshot-v1", "deepseek-chat", "MiniMax-M2"),
    )
    for locale in _LOCALES:
        for width in _WIDTHS:
            text = "\n".join(render_dispatch_view(_AWAITING, width, locale))
            assert approve in text
            assert reject in text
            assert "批准" not in text
            assert "驳回" not in text


def test_empty_dispatch_holds_the_layout_contract():
    check_layout(
        _render({}),
        widths=_WIDTHS,
        locales=_LOCALES,
        keep=("—",),
        states=(),
        columns=("—",),
    )
    english = "\n".join(render_dispatch_view({}, 80, "en"))
    chinese = "\n".join(render_dispatch_view({}, 40, "zh-CN"))
    # The header is also full of dashes. These rows are the empty roster and
    # the empty dag; removing either one leaves the header dashes behind.
    assert "  roster\n  —\n  dag\n  L0: —" in english
    assert "  名册\n  —\n  依赖图\n  L0: —" in chinese
    assert "dispatch" in english
    assert "L1" not in english
    assert "approve" not in english
    assert "unassigned" not in english
    assert "派发" in chinese
    assert "L1" not in chinese
    assert "approve" not in chinese
    assert "未分配" not in chinese


def test_dispatch_strings_parity(monkeypatch):
    assert check_parity(STRINGS) is None
    for key in _STRING_KEYS:
        assert key in STRINGS["en"]
        assert key in STRINGS["zh-CN"]
    for key in _TRANSLATED:
        assert STRINGS["en"][key] != STRINGS["zh-CN"][key]
    seen: list[str] = []
    real = dispatch_view_module.t

    def spy(table, key, locale=None):
        seen.append(key)
        return real(table, key, locale)

    monkeypatch.setattr(dispatch_view_module, "t", spy)
    render_dispatch_view(_FRAME_PAYLOAD, 80, "zh-CN")
    missing = [key for key in _LOOKUPS if key not in seen]
    assert missing == []


def _transcript(path: Path) -> list[str]:
    """The dispatch body of a design frame, without chrome or the command region."""
    rows = path.read_text(encoding="utf-8").splitlines()
    body = rows[1:]
    while body and not body[0].strip():
        body.pop(0)
    transcript: list[str] = []
    for row in body:
        if row.strip() in {"commands", "命令"}:
            break
        transcript.append(row)
    return transcript


def test_real_dispatch_id_decision_fits():
    ident = "0" * 32
    approve = f"approve {ident}"
    reject = f"reject {ident}"
    check_layout(
        _render({"id": ident, "state": "awaiting_human"}),
        widths=_WIDTHS,
        locales=_LOCALES,
        keep=(approve, reject),
    )


def test_api_line_states_use_canonical_words():
    payload = {
        "lines": [
            {"id": "L1", "status": "ASSIGNED"},
            {"id": "L2", "status": "REVIEWING"},
        ]
    }
    check_layout(_render(payload), widths=_WIDTHS, locales=_LOCALES, states=("queued", "running"))
    chinese = "\n".join(render_dispatch_view(payload, 80, "zh-CN"))
    assert "排队中" in chinese
    assert "运行中" in chinese
    assert "ASSIGNED" not in chinese
    assert "REVIEWING" not in chinese


def test_multiline_goal_keeps_every_character():
    goal = "first line\nsecond line"
    check_layout(_render({"goal": goal}), widths=_WIDTHS, locales=_LOCALES, prose=(goal,))


def test_approval_metadata_is_not_a_waiting_hint():
    for state, result in (("MONITORING", "pending"), ("RUNNING", "pending"), ("COMPLETE", "pass")):
        payload = {"id": "d1", "state": state, "gates": {"approval": {"gate": "G6", "result": result}}}
        check_layout(_render(payload), widths=_WIDTHS, locales=_LOCALES)
        for locale in _LOCALES:
            text = "\n".join(render_dispatch_view(payload, 80, locale))
            assert "waiting on a human" not in text
            assert "正在等待人工决定" not in text
    waiting = {"id": "d1", "state": "APPROVAL", "gates": {"approval": {"gate": "G6", "result": "pending"}}}
    assert "waiting on a human for G6" in "\n".join(render_dispatch_view(waiting, 80, "en"))


def test_approval_state_has_decision_commands():
    payload = {"id": "d1", "state": "APPROVAL", "gates": {"approval": {"gate": "G6", "result": "pending"}}}
    check_layout(
        _render(payload),
        widths=_WIDTHS,
        locales=_LOCALES,
        keep=("approve d1", "reject d1"),
        columns=("approve d1", "reject d1"),
    )


_DISPATCH_STATES = (
    ("SUBMITTED", "已提交"),
    ("PLANNING", "规划中"),
    ("ASSIGNED", "已分配"),
    ("LAUNCHING", "启动中"),
    ("MONITORING", "监控中"),
    ("REVIEW", "评审中"),
    ("APPROVAL", "待审批"),
    ("MERGE", "合并中"),
    ("COMPLETE", "已完成"),
    ("ABORT", "已中止"),
    ("ERROR", "错误"),
)


def test_api_dispatch_states_use_localized_labels():
    for state, zh_word in _DISPATCH_STATES:
        for width in _WIDTHS:
            english = "\n".join(render_dispatch_view({"state": state}, width, "en"))
            chinese = "\n".join(render_dispatch_view({"state": state}, width, "zh-CN"))
            assert state in english
            assert zh_word in chinese
            assert state not in chinese


_API_PHASES = (
    ("planning", "plan", "规划中"),
    ("spec_review", "spec", "规格评审"),
    ("dag_ready", "ready", "就绪"),
    ("assigned", "assigned", "已分配"),
    ("implementing", "build", "构建中"),
    ("focused_tests", "tests", "测试中"),
    ("primary_review", "review 1", "主审"),
    ("fixing", "fix", "修复中"),
    ("secondary_review", "review 2", "副审"),
    ("integration_checks", "checks", "检查中"),
    ("awaiting_human", "pending", "待定"),
    ("awaiting_adjudication", "decision", "待裁决"),
    ("merged", "merged", "已合并"),
    ("replanning", "replan", "重规划中"),
    ("blocked", "blocked", "已阻塞"),
    ("cancelled", "canceled", "已取消"),
    ("pending", "pending", "待定"),
    ("passed", "passed", "已通过"),
    ("failed", "failed", "失败"),
    ("running", "running", "运行中"),
    ("merging", "merging", "合并中"),
)


def test_api_gate_phases_use_localized_labels():
    summary = json.loads(FIXTURE.read_text(encoding="utf-8"))
    chinese = "\n".join(render_dispatch_view(summary, 40, "zh-CN"))
    english = "\n".join(render_dispatch_view(summary, 80, "en"))
    assert "awaiting_human" not in chinese
    assert "待定" in chinese
    assert "awaitin" not in english
    assert "pending" in english
    for phase, en_word, zh_word in _API_PHASES:
        payload = {"lines": [{"id": "L1"}], "gates": {"lines": {"L1": {"phase": phase}}}}
        for width in _WIDTHS:
            en_text = "\n".join(render_dispatch_view(payload, width, "en"))
            zh_text = "\n".join(render_dispatch_view(payload, width, "zh-CN"))
            assert en_word in en_text
            assert zh_word in zh_text
            assert phase not in zh_text
            if en_word != phase:
                assert phase not in en_text


def test_api_fixture_names_the_waiting_gate():
    summary = json.loads(FIXTURE.read_text(encoding="utf-8"))
    check_layout(_render(summary), widths=_WIDTHS, locales=_LOCALES)
    english = "\n".join(render_dispatch_view(summary, 80, "en"))
    chinese = "\n".join(render_dispatch_view(summary, 80, "zh-CN"))
    assert "waiting on a human for G6" in english
    assert "G6 正在等待人工决定" in chinese


def test_dispatch_frame_structure_matches_design_artefacts():
    for locale in _LOCALES:
        for width in _WIDTHS:
            path = _FRAMES / f"tui-dispatch-{locale}-w{width}.txt"
            assert path.is_file(), path
            expected = _transcript(path)
            roster = "roster" if locale == "en" else "名册"
            # The static frames predate the required dispatch-level decision row.
            expected.insert(expected.index(f"  {roster}"), f"  approve {_DISPATCH_ID}  reject {_DISPATCH_ID}")
            rendered = render_dispatch_view(_FRAME_PAYLOAD, width, locale)
            assert rendered == expected
