"""Home screen rows: the layout contract, the state vocabulary and the fixtures.

``tui_home`` is the pure renderer of ``docs/design/terminal-native/tui/SPEC.md``
§7.1. The tests below call it at the three contract widths in both languages,
compare the body of the design fixtures with the rendered rows, and pin the
rules a narrow or long row must not break: the stack at 40 columns, the glyph
and word of every state, the ASCII twins and the cell-boundary ellipsis.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from rich.cells import cell_len

_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.tui_home import STRINGS, home_rows  # noqa: E402
from ptc_cli.superagent.tui_i18n import check_parity  # noqa: E402
from ptc_cli.superagent.tui_theme import resolve_theme, status_glyph, status_word  # noqa: E402
from tests.unit_tests.tui_layout_contract import check_layout  # noqa: E402

# A test that only reads the design tree still has to find it from libs/ptc-cli.
_ROOT = Path(__file__).resolve().parents[4]
_FIXTURES = _ROOT / "docs/design/terminal-native/tui"
_WIDTHS = (40, 80, 120)
_LOCALES = ("en", "zh-CN")

TICKETS = [
    {
        "ticket_id": "7f782add",
        "status": "completed",
        "title": "Read the adapter registry",
        "worker_model": "kimi",
        "elapsed_seconds": 12,
        "updated_at": "2026-05-20T09:41:00Z",
    },
    {
        "ticket_id": "6bfa630f",
        "status": "waiting",
        "title": "Needs a host with bash",
        "worker_model": "deepseek",
        "elapsed_seconds": 7500,
        "updated_at": "2026-05-20T09:12:00Z",
    },
    {
        "ticket_id": "4b2f7803",
        "status": "failed",
        "title": "Sandbox denied",
        "worker_model": "cursor",
        "elapsed_seconds": 28620,
        "updated_at": "2026-05-20T01:35:00Z",
    },
]
TITLES_ZH = (
    "读取适配器注册表",
    "需要一台带 bash 的主机",
    "沙箱拒绝了这次执行",
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No inherited locale, ASCII switch or theme changes the expected output."""
    for name in (
        "CONDUCTOR_LOCALE",
        "LC_ALL",
        "LC_MESSAGES",
        "LANG",
        "PTC_ASCII",
        "PTC_I18N_PARITY",
        "CONDUCTOR_THEME",
        "NO_COLOR",
        "COLORFGBG",
    ):
        monkeypatch.delenv(name, raising=False)


def _theme():
    return resolve_theme({"CONDUCTOR_THEME": "dark", "LANG": "en_US.UTF-8"})


def _ticket(status: str, title: str, index: int = 1) -> dict[str, object]:
    return {
        "ticket_id": f"{index:08x}",
        "status": status,
        "title": title,
        "worker_model": "kimi",
        "elapsed_seconds": 12,
        "updated_at": "2026-05-20T09:41:00Z",
    }


def _fixture_body(locale: str, width: int) -> list[str]:
    """The transcript body of one design fixture, between the chrome regions."""
    path = _FIXTURES / f"tui-home-{locale}-w{width}.txt"
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == "Conductor")
    end = next(index for index in range(start + 1, len(lines)) if not lines[index].strip())
    return lines[start:end]


def test_the_strings_are_bilingual() -> None:
    check_parity(STRINGS)


@pytest.mark.parametrize("locale", _LOCALES)
@pytest.mark.parametrize("width", _WIDTHS)
def test_the_body_matches_the_design_fixture(locale: str, width: int) -> None:
    """The fixture is the contract: same columns, same stack, same greeting."""
    tickets = _localized_tickets(locale)
    expected = _fixture_body(locale, width)
    assert home_rows(tickets, 2, _theme(), locale, width) == expected


def test_empty_home_is_the_greeting_and_the_keys() -> None:
    rows = home_rows([], 0, _theme(), "en", 80)
    assert rows == [
        "  Conductor",
        "  Describe a task and press Enter. It runs as a Ticket on Home.",
        "  / commands    @ attach    ? keys",
    ]
    assert not any("need you" in row for row in rows)


@pytest.mark.parametrize("locale", _LOCALES)
def test_empty_home_passes_the_layout_contract(locale: str) -> None:
    check_layout(
        lambda width, language: home_rows([], 0, _theme(), language, width),
        keep=("Conductor",),
        locales=(locale,),
    )


@pytest.mark.parametrize("locale", _LOCALES)
def test_three_tickets_pass_the_layout_contract(locale: str) -> None:
    tickets = _localized_tickets(locale)
    check_layout(
        lambda width, language: home_rows(tickets, 2, _theme(), language, width),
        widths=_WIDE,
        states=("completed", "needs-you", "failed"),
        locales=(locale,),
    )
    _check_stacked_states(home_rows(tickets, 2, _theme(), locale, 40), locale)
    check_layout(
        lambda width, language: home_rows(tickets, 2, _theme(), language, width),
        widths=(40, 80, 100, 120),
        keep=("Conductor", "/jobs --attention"),
        prose=(STRINGS[locale]["greeting"],),
        columns=(
            status_word("completed", locale),
            "/jobs 1",
            "/reply 2",
            "/retry 3",
            "kimi",
            "deepseek",
            "12s",
            "09:41",
        ),
        locales=(locale,),
    )


_STATES = ("queued", "running", "blocked", "needs-you", "failed", "completed", "archived")
# Above the stack width a row is a table: glyph and word sit on one row.
_WIDE = (80, 100, 120)
_CANONICAL = {"waiting": "needs-you"}


def _check_stacked_states(rows: list[str], locale: str, *, ascii_glyphs: bool = False) -> None:
    """At 40 the glyph leads the identity line and the word leads the meta line.

    This is the stack of §3.3 and of ``tui-home-*-w40.txt``: the word moves
    under its glyph instead of being dropped.
    """
    environ = {"PTC_ASCII": "1"} if ascii_glyphs else {}
    glyphs = {status_glyph(state, environ): state for state in _STATES}
    identities = [index for index, row in enumerate(rows) if row[2:3] in glyphs and row[3:5] == "  "]
    assert identities, rows
    for start in identities:
        state = glyphs[rows[start][2]]
        meta = next(row for row in rows[start + 1 :] if row.startswith("    ") and row[4] != " ")
        assert meta.strip().startswith(status_word(state, locale)), (rows[start], meta)


# Nine Tickets, two of them waiting on the operator.
NINE_STATES = (
    "queued",
    "running",
    "needs-you",
    "blocked",
    "failed",
    "completed",
    "waiting",
    "archived",
    "completed",
)


def _nine(locale: str) -> list[dict[str, object]]:
    return [_ticket(state, _localized_title(locale, index), index + 1) for index, state in enumerate(NINE_STATES)]


@pytest.mark.parametrize("locale", _LOCALES)
def test_nine_tickets_with_two_needing_you_pass_the_layout_contract(locale: str) -> None:
    tickets = _nine(locale)
    check_layout(
        lambda width, language: home_rows(tickets, 2, _theme(), language, width),
        widths=_WIDE,
        states=_STATES,
        locales=(locale,),
    )
    _check_stacked_states(home_rows(tickets, 2, _theme(), locale, 40), locale)
    check_layout(
        lambda width, language: home_rows(tickets, 2, _theme(), language, width),
        keep=("Conductor", "/jobs --attention"),
        prose=(STRINGS[locale]["greeting"],),
        columns=(
            status_word("needs-you", locale),
            "/reply 3",
            "/reply 4",
            "/retry 5",
            "/reply 7",
            "/jobs 9",
        ),
        locales=(locale,),
    )


@pytest.mark.parametrize("locale", _LOCALES)
@pytest.mark.parametrize("width", _WIDTHS)
def test_nine_tickets_count_two_and_list_every_row(locale: str, width: int) -> None:
    tickets = _nine(locale)
    for attention in (2, None):
        rows = home_rows(tickets, attention, _theme(), locale, width)
        attention_row = STRINGS[locale]["attention"].format(glyph="◆", count=2)
        assert f"  {attention_row}" in rows
        flat = "\n".join(rows)
        for index in range(1, 10):
            assert f" {index}" in flat
        marks = {status_glyph(_CANONICAL.get(state, state), {}) for state in NINE_STATES}
        identity = [row for row in rows if row[2:3] in marks and row[3:5] == "  "]
        assert len(identity) == len(tickets)
        assert sum(1 for row in identity if row[2] == "◆") == 2


def test_the_ascii_glyphs_pass_the_layout_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTC_ASCII", "1")
    tickets = _nine("en")
    check_layout(
        lambda width, language: home_rows(tickets, 2, _theme(), language, width),
        keep=("Conductor", "/jobs --attention"),
        locales=("en",),
    )
    check_layout(
        lambda width, language: home_rows(tickets, 2, _theme(), language, width),
        widths=_WIDE,
        states=_STATES,
        ascii_glyphs=True,
        locales=("en",),
    )
    _check_stacked_states(home_rows(tickets, 2, _theme(), "en", 40), "en", ascii_glyphs=True)


def test_the_attention_count_comes_before_the_roster() -> None:
    rows = home_rows(TICKETS, 2, _theme(), "en", 80)
    attention = rows.index("  ◆ 2 need you — /jobs --attention")
    first_ticket = next(index for index, row in enumerate(rows) if "Completed" in row)
    assert attention < first_ticket
    # The nine rows are all rendered, and the last one keeps its digit index.
    many = [_ticket("completed", f"Ticket {index}", index) for index in range(1, 10)]
    many_rows = home_rows(many, 2, _theme(), "en", 80)
    assert many_rows[-1].endswith("/jobs 9")


def test_attention_none_counts_the_tickets_that_need_you() -> None:
    tickets = [
        _ticket("needs-you", "First", 1),
        _ticket("waiting", "Second", 2),
        _ticket("completed", "Third", 3),
    ]
    rows = home_rows(tickets, None, _theme(), "en", 80)
    assert "  ◆ 2 need you — /jobs --attention" in rows


def test_explicit_zero_attention_hides_the_attention_row() -> None:
    rows = home_rows([_ticket("needs-you", "Needs you", 1)], 0, _theme(), "en", 80)
    assert not any("need you" in row for row in rows)


@pytest.mark.parametrize(
    ("state", "glyph", "word"),
    [
        (state, status_glyph(state, {}), status_word(state, "en"))
        for state in (
            "queued",
            "running",
            "blocked",
            "needs-you",
            "failed",
            "completed",
            "archived",
        )
    ],
)
def test_every_state_shows_its_glyph_and_word(state: str, glyph: str, word: str) -> None:
    rows = home_rows([_ticket(state, f"Ticket {state}", 1)], 0, _theme(), "en", 80)
    flat = "\n".join(rows)
    assert glyph in flat
    assert word in flat
    assert any(glyph in row and word in row for row in rows)


def test_ascii_glyphs_when_ptc_ascii_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTC_ASCII", "1")
    states = ("queued", "running", "blocked", "needs-you", "failed", "completed", "archived")
    tickets = [_ticket(state, f"Ticket {state}", index + 1) for index, state in enumerate(states)]
    rows = home_rows(tickets, 2, _theme(), "en", 80)
    flat = "\n".join(rows)
    for state in states:
        assert status_glyph(state, {"PTC_ASCII": "1"}) in flat
        assert status_glyph(state, {}) not in flat
    assert "? 2 need you" in flat


def test_an_ascii_theme_also_uses_the_ascii_glyphs() -> None:
    theme = resolve_theme({"PTC_ASCII": "1", "CONDUCTOR_THEME": "dark", "LANG": "en_US.UTF-8"})
    rows = home_rows([_ticket("completed", "Only ASCII", 1)], 0, theme, "en", 80)
    assert any("+  Completed" in row for row in rows)
    assert not any("✔" in row for row in rows)


def _clipped_title(row: str, locale: str) -> str:
    prefix = f"  ✔  {status_word('completed', locale)}"
    assert row.startswith(prefix), row
    return row[len(prefix) :].strip().split("  ", 1)[0]


@pytest.mark.parametrize("width", range(41, 121))
def test_a_long_zh_cn_title_is_cut_on_a_cell_boundary(width: int) -> None:
    title = TITLES_ZH[2] * 6
    rows = home_rows([_ticket("completed", title, 1)], 0, _theme(), "zh-CN", width)
    assert all(cell_len(row) <= width for row in rows)
    line = next(row for row in rows if "…" in row)
    clipped = _clipped_title(line, "zh-CN")
    assert clipped.endswith("…")
    body = clipped[:-1]
    # Whole wide characters only: the body is a character prefix of the title.
    assert body
    assert title.startswith(body)
    assert cell_len(body) % 2 == 0
    # The next character would not have fitted, so nothing was cut early.
    assert cell_len(body) + 2 + 1 > cell_len(_pad_room(line, clipped, width))


def _pad_room(line: str, clipped: str, width: int) -> str:
    """The cut title plus the padding the column gave it."""
    start = line.index(clipped)
    rest = line[start + len(clipped) :]
    padding = len(rest) - len(rest.lstrip(" "))
    if not rest.strip():
        padding = width - cell_len(line[: start + len(clipped)])
    else:
        padding -= 2  # the column gap
    return clipped + " " * max(0, padding)


@pytest.mark.parametrize("width", [80, 81, 84, 89])
@pytest.mark.parametrize(
    "title",
    [
        "Ship the terminal-native review queue screen with a worker report and gates ok!!",
        "x" * 80,
    ],
)
def test_an_80_character_en_title_is_cut_with_an_ellipsis(title: str, width: int) -> None:
    assert len(title) == 80
    rows = home_rows([_ticket("completed", title, 1)], 0, _theme(), "en", width)
    assert all(cell_len(row) <= width for row in rows)
    line = next(row for row in rows if "…" in row)
    clipped = _clipped_title(line, "en")
    assert clipped.endswith("…")
    assert title.startswith(clipped[:-1])
    # §3.2: the glyph and the word are pinned; the convenience columns went first.
    assert line.startswith("  ✔  Completed  ")


def test_an_80_character_en_title_is_whole_once_the_columns_make_room() -> None:
    """§3.2: fixed columns drop before the title is cut, so at 120 it is whole."""
    title = "y" * 80
    rows = home_rows([_ticket("completed", title, 1)], 0, _theme(), "en", 120)
    assert any(title in row for row in rows)
    assert not any("…" in row for row in rows)


def test_a_long_title_wraps_at_40_and_keeps_the_marker() -> None:
    title = "把终端原生的评审队列屏幕一起带到这一行里"
    rows = home_rows([_ticket("needs-you", title, 1)], 0, _theme(), "zh-CN", 40)
    assert all(cell_len(row) <= 40 for row in rows)
    assert any(row.startswith("  ◆  ") for row in rows)
    assert not any("…" in row for row in rows)
    compact = "".join("".join(rows).split())
    assert "".join(title.split()) in compact


def test_objects_as_well_as_mappings_are_accepted() -> None:
    class Ticket:
        status = "completed"
        title = "Object ticket"
        worker_model = "kimi"
        elapsed_seconds = 12
        updated_at = "2026-05-20T09:41:00Z"

    rows = home_rows([Ticket()], 0, _theme(), "en", 80)
    assert any("Object ticket" in row for row in rows)


def test_unknown_states_do_not_take_the_screen_down() -> None:
    rows = home_rows([_ticket("something-new", "Unknown state", 1)], 0, _theme(), "en", 80)
    assert any("Queued" in row for row in rows)


def _localized_tickets(locale: str) -> list[dict[str, object]]:
    tickets = [dict(ticket) for ticket in TICKETS]
    if locale == "zh-CN":
        for ticket, title in zip(tickets, TITLES_ZH, strict=True):
            ticket["title"] = title
    return tickets


def _localized_title(locale: str, index: int) -> str:
    if locale == "zh-CN":
        return f"第 {index + 1} 个需要审查的 Ticket 标题"
    return f"Review ticket number {index + 1}"
