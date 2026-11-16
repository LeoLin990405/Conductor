"""Terminal-native jobs, Ticket detail, watch and attention rendering."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import pytest
from rich.cells import cell_len

_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.tui_i18n import check_parity  # noqa: E402
from ptc_cli.superagent.tui_jobs import (  # noqa: E402
    STRINGS,
    attention_rows,
    job_detail_rows,
    jobs_rows,
    watch_header_rows,
)
from ptc_cli.superagent.tui_theme import STATUS_IDS, resolve_theme  # noqa: E402
from tests.unit_tests.tui_layout_contract import check_layout  # noqa: E402


def _theme():
    return resolve_theme({"CONDUCTOR_THEME": "dark", "LANG": "en_US.UTF-8"})


def _ticket(
    number: int,
    status: str,
    title: str,
    worker: str,
    age: str,
    *,
    full_id: str | None = None,
) -> dict:
    return {
        "ticket_id": full_id or f"{number:08x}-aaaa-bbbb-cccc-{number:012x}",
        "status": status,
        "title": title,
        "worker_model": worker,
        "age": age,
    }


GOLDEN = [
    _ticket(1, "completed", "Read the adapter registry", "kimi", "12s", full_id="7f782add"),
    _ticket(2, "waiting", "Needs a host with bash", "deepseek", "2h05m", full_id="6bfa630f"),
    _ticket(3, "failed", "Sandbox denied", "cursor", "7h57m", full_id="4b2f7803"),
    _ticket(4, "running", "Add the review queue screen", "minimax", "4m12s", full_id="1a91c0de"),
    _ticket(5, "queued", "Audit the token contrast", "", "", full_id="0c73ab19"),
]
ALL_STATES = [_ticket(index, status, f"Ticket {status}", "kimi", "12s") for index, status in enumerate(STATUS_IDS, 1)]


def test_string_table_is_bilingual() -> None:
    check_parity(STRINGS)


def test_list_matches_the_golden_column_order_at_120() -> None:
    rows = jobs_rows(GOLDEN, _theme(), "en", 120)
    assert rows[:4] == [
        "  jobs",
        "     #    id           state      title                        worker    age     next",
        "  ▸  1    7f782add  ✔  Completed  Read the adapter registry    kimi      12s     /jobs 1",
        "     2    6bfa630f  ◆  Needs you  Needs a host with bash       deepseek  2h05m   /reply 2",
    ]
    assert rows[-1] == "  5 Tickets · 1 needs you · 1 running"


def test_fit_rule_drops_convenience_columns_at_80() -> None:
    english = jobs_rows(GOLDEN, _theme(), "en", 80)
    assert english[1] == "     id           state      title                        next"
    assert english[2] == "  ▸  7f782add  ✔  Completed  Read the adapter registry    /jobs 1"
    chinese_tickets = deepcopy(GOLDEN)
    for ticket, title in zip(
        chinese_tickets,
        ("读取适配器注册表", "需要一台带 bash 的主机", "沙箱拒绝了这次执行", "添加评审队列屏幕", "审查令牌的对比度"),
        strict=True,
    ):
        ticket["title"] = title
    chinese = jobs_rows(chinese_tickets, _theme(), "zh-CN", 80)
    assert chinese[1] == "     id           状态       标题                    执行者    下一步"
    assert chinese[2] == "  ▸  7f782add  ✔  已完成     读取适配器注册表        kimi      /jobs 1"


def test_stack_rule_keeps_every_column_and_the_full_id() -> None:
    full_id = "7f782add-aaaa-bbbb-cccc-123456789abc"
    ticket = _ticket(1, "completed", "Read the adapter registry", "kimi", "12s", full_id=full_id)
    rows = jobs_rows([ticket], _theme(), "en", 40)
    assert rows == [
        "  jobs",
        "        title",
        "  ▸  ✔  Read the adapter registry",
        "    ✔ Completed · /jobs 1",
        "    " + full_id,
        "    kimi · 12s · 1",
        "  1 Tickets · 0 needs you · 0 running",
    ]


def test_all_states_and_ascii_glyphs_pass_the_layout_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    check_layout(
        lambda width, locale: jobs_rows(ALL_STATES, _theme(), locale, width),
        states=STATUS_IDS,
        columns=("kimi", "12s", "/jobs 1", ALL_STATES[0]["ticket_id"]),
    )
    monkeypatch.setenv("PTC_ASCII", "1")
    check_layout(
        lambda width, locale: jobs_rows(ALL_STATES, None, locale, width),
        states=STATUS_IDS,
        columns=("kimi", "12s", "/jobs 1", ALL_STATES[0]["ticket_id"]),
        ascii_glyphs=True,
    )


def test_long_english_and_chinese_titles_fit_without_losing_the_state() -> None:
    titles = ["x" * 80, "读取适配器注册表并检查每一个终端单元格" * 3]
    for title in titles:
        ticket = _ticket(1, "running", title, "kimi", "12s")
        check_layout(
            lambda width, locale, ticket=ticket: jobs_rows([ticket], _theme(), locale, width),
            states=("running",),
            widths=(80, 120),
        )
        check_layout(
            lambda width, locale, ticket=ticket: jobs_rows([ticket], _theme(), locale, width),
            prose=(title,),
            widths=(40,),
        )
        wide = jobs_rows([ticket], _theme(), "en", 80)
        assert "…" in "\n".join(wide)
        narrow = jobs_rows([ticket], _theme(), "en", 40)
        assert "".join("".join(narrow).split()).count(title.replace(" ", "")) == 1


def test_status_attention_and_failed_filters() -> None:
    tickets = [*deepcopy(GOLDEN), _ticket(6, "blocked", "Blocked job", "kimi", "1m")]
    before = deepcopy(tickets)
    failed = jobs_rows(tickets, _theme(), "en", 80, failed=True)
    assert "Sandbox denied" in "\n".join(failed)
    assert "Blocked job" in "\n".join(failed)
    assert "Read the adapter registry" not in "\n".join(failed)
    queued = jobs_rows(tickets, _theme(), "en", 80, status="queued")
    assert "Audit the token contrast" in "\n".join(queued)
    assert "Sandbox denied" not in "\n".join(queued)
    attention = attention_rows(tickets, _theme(), "en", 40)
    assert "Needs a host with bash" in "\n".join(attention)
    assert "Read the adapter registry" not in "\n".join(attention)
    assert "jobs · needs you" in attention[0]
    assert tickets == before


def test_cancelled_uses_archived_state_in_list_detail_and_watch() -> None:
    ticket = _ticket(7, "cancelled", "Stopped", "kimi", "12s")
    renderers = (
        lambda width, locale: jobs_rows([ticket], _theme(), locale, width),
        lambda width, locale: job_detail_rows(ticket, _theme(), locale, width),
        lambda width, locale: watch_header_rows(ticket, _theme(), locale, width),
    )
    for render in renderers:
        check_layout(render, states=("archived",))
        for locale, word in (("en", "Archived"), ("zh-CN", "已归档")):
            flat = "\n".join(render(40, locale))
            assert f"⊘ {word}" in flat
            assert "cancelled" not in flat


def test_empty_list_keeps_the_legacy_no_jobs_copy() -> None:
    for locale in ("en", "zh-CN"):
        assert jobs_rows([], _theme(), locale, 40)[-1] == "  no Jobs"
        assert attention_rows([], _theme(), locale, 40)[-1] == "  no Jobs"


def test_detail_wraps_question_summary_and_error_and_keeps_full_id() -> None:
    ticket = _ticket(2, "waiting", "Needs a host with bash", "deepseek", "2h05m")
    ticket.update(
        worker_provider="deepseek",
        worker_model="flash",
        updated_at="2026-11-11T12:34:56Z",
        attention={"question": "Which host should run this Ticket? " + "请选择一个主机" * 5},
        summary="The registry was checked and every adapter is accounted for. " * 2,
        error="The first sandbox attempt failed.",
    )
    check_layout(
        lambda width, locale: job_detail_rows(ticket, _theme(), locale, width),
        states=("needs-you",),
        prose=(ticket["attention"]["question"], ticket["summary"], ticket["error"]),
        columns=(ticket["ticket_id"], "deepseek/flash", ticket["updated_at"]),
    )


def test_watch_header_is_shared_by_watch_and_attach_layouts() -> None:
    ticket = _ticket(4, "running", "Add the review queue screen", "minimax", "4m12s")
    check_layout(
        lambda width, locale: watch_header_rows(ticket, _theme(), locale, width),
        states=("running",),
        columns=(ticket["ticket_id"],),
    )
    english = "\n".join(watch_header_rows(ticket, _theme(), "en", 40))
    assert "watching Ticket" in english
    assert "Esc to stop watching" in english
    chinese = "\n".join(watch_header_rows(ticket, _theme(), "zh-CN", 40))
    assert "正在跟随 Ticket" in chinese
    assert "按 Esc 停止跟随" in chinese


def test_elapsed_seconds_and_injected_clock_format_age() -> None:
    elapsed = _ticket(1, "running", "Elapsed", "kimi", "")
    elapsed["elapsed_seconds"] = 7532.0
    assert "2h05m" in "\n".join(jobs_rows([elapsed], _theme(), "en", 120, now=0.0))
    dated = _ticket(1, "running", "Dated", "kimi", "")
    dated.pop("age")
    dated["updated_at"] = "1970-01-01T00:00:00Z"
    assert "12.0s" in "\n".join(jobs_rows([dated], _theme(), "en", 120, now=12.0))


@pytest.mark.parametrize("width", [40, 80, 120])
def test_no_rendered_row_exceeds_the_terminal(width: int) -> None:
    for locale in ("en", "zh-CN"):
        rows = jobs_rows(ALL_STATES, _theme(), locale, width)
        assert all(cell_len(row) <= width for row in rows)
