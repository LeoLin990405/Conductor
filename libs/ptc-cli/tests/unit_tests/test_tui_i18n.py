"""Locale resolution, string parity, and the layout contract's own self-test.

``tui_i18n`` reads a renderer's ``STRINGS`` table; ``tui_layout_contract`` is the
gate every screen test imports. Both are checked here against the rules of
``docs/design/terminal-native``: the locale comes from the environment, a table
that is not bilingual is a bug, and a row may never be wider than the terminal —
measured in cells, so a CJK character counts as the two it takes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.tui_i18n import (  # noqa: E402
    DEFAULT_LOCALE,
    LOCALES,
    MissingTranslationError,
    check_parity,
    normalize_locale,
    parity_enforced,
    parity_problems,
    resolve_locale,
    strings,
    t,
)
from tests.unit_tests import tui_layout_contract  # noqa: E402
from tests.unit_tests.tui_layout_contract import LayoutError, check_layout  # noqa: E402

STRINGS = {
    "en": {"greeting": "Describe a task", "hint": "/ for commands"},
    "zh-CN": {"greeting": "描述一个任务", "hint": "/ 打开命令"},
}


@pytest.fixture(autouse=True)
def _clean_locale_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No inherited locale, and parity on the way pytest leaves it."""
    for name in ("CONDUCTOR_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG", "PTC_I18N_PARITY"):
        monkeypatch.delenv(name, raising=False)


def test_normalize_locale() -> None:
    assert normalize_locale("zh_CN.UTF-8") == "zh-CN"
    assert normalize_locale("zh-Hans") == "zh-CN"
    assert normalize_locale("ZH") == "zh-CN"
    assert normalize_locale("en_US.UTF-8") == "en"
    assert normalize_locale("C") == "en"
    assert normalize_locale("") == DEFAULT_LOCALE
    assert normalize_locale(None) == "en"


def test_conductor_locale_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONDUCTOR_LOCALE", "zh-CN")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert resolve_locale() == "zh-CN"


@pytest.mark.parametrize(
    ("name", "value"),
    [("LC_ALL", "zh_CN.UTF-8"), ("LC_MESSAGES", "zh_CN.UTF-8"), ("LANG", "zh_CN.UTF-8")],
)
def test_lc_and_lang_pick_zh_cn(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    assert resolve_locale() == "zh-CN"


def test_an_earlier_variable_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    assert resolve_locale() == "en"


def test_no_environment_is_english(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "C")
    assert resolve_locale() == "en"


def test_t_reads_the_resolved_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    assert t(STRINGS, "greeting") == "Describe a task"
    monkeypatch.setenv("CONDUCTOR_LOCALE", "zh-CN")
    assert t(STRINGS, "greeting") == "描述一个任务"
    assert t(STRINGS, "hint") == "/ 打开命令"


def test_t_takes_an_explicit_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONDUCTOR_LOCALE", "zh-CN")
    assert t(STRINGS, "greeting", "en") == "Describe a task"
    assert strings(STRINGS, "en")["hint"] == "/ for commands"
    assert strings(STRINGS)["hint"] == "/ 打开命令"


def test_a_key_no_locale_has_always_raises() -> None:
    with pytest.raises(MissingTranslationError):
        t(STRINGS, "not-a-key")


def test_a_flat_table_is_its_own_english_section() -> None:
    flat = {"only": "one language"}
    assert t(flat, "only") == "one language"
    assert parity_problems(flat) == []


def test_a_missing_zh_cn_key_raises_while_parity_is_enforced() -> None:
    """Pytest is the parity switch, so a half-translated table fails the suite."""
    assert parity_enforced() is True
    table = {"en": {"greeting": "Describe a task"}, "zh-CN": {}}
    with pytest.raises(MissingTranslationError) as excinfo:
        t(table, "greeting", "zh-CN")
    assert "zh-CN has no 'greeting'" in str(excinfo.value)


def test_a_missing_zh_cn_key_falls_back_to_english_at_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PTC_I18N_PARITY", "0")
    table = {"en": {"greeting": "Describe a task"}, "zh-CN": {}}
    assert t(table, "greeting", "zh-CN") == "Describe a task"


def test_parity_can_be_forced_back_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTC_I18N_PARITY", "1")
    assert parity_enforced() is True


def test_parity_reports_missing_and_unknown_keys() -> None:
    table = {
        "en": {"a": "A", "b": "B"},
        "zh-CN": {"a": "甲", "c": "丙"},
    }
    assert parity_problems(table) == [
        "zh-CN: missing 'b'",
        "zh-CN: unknown 'c'",
    ]
    with pytest.raises(MissingTranslationError):
        check_parity(table)
    assert check_parity(STRINGS) is None


def test_a_table_without_a_section_is_a_problem() -> None:
    problems = parity_problems({"en": {"a": "A"}})
    assert problems == ["zh-CN: the table has no 'zh-CN' section"]


# --- the layout contract's own self-test --------------------------------------


def _wide(width: int, locale: str) -> list[str]:
    del locale
    return ["\u276f", "x" * 41, "status bar"]


def test_a_row_wider_than_the_terminal_fails() -> None:
    with pytest.raises(LayoutError) as excinfo:
        check_layout(_wide, widths=(40,), locales=("en",), keep=("status bar",))
    assert "row 1 is 41 cells wide" in str(excinfo.value)


def test_a_row_of_exactly_the_width_passes() -> None:
    check_layout(
        lambda width, locale: ["\u276f", "x" * width, "status bar"],
        widths=(40, 80),
        locales=("en",),
        keep=("\u276f", "status bar"),
    )


def test_a_kept_row_that_is_lost_fails() -> None:
    with pytest.raises(LayoutError) as excinfo:
        check_layout(
            lambda width, locale: ["only one row"],
            widths=(40,),
            locales=("en",),
            keep=("\u276f",),
        )
    assert "lost the row it must keep: '\u276f'" in str(excinfo.value)


def test_a_state_keeps_its_glyph_and_its_word() -> None:
    def render(width: int, locale: str) -> list[str]:
        del width
        if locale == "zh-CN":
            return ["● 运行中", "\u276f", "status bar"]
        return ["● Running", "\u276f", "status bar"]

    check_layout(render, widths=(40,), locales=tui_layout_contract.LOCALES, states=("running",))
    with pytest.raises(LayoutError) as excinfo:
        check_layout(
            lambda width, locale: ["● only the glyph", "\u276f", "status bar"],
            widths=(40,),
            locales=("en",),
            states=("running",),
        )
    assert "state running lost its word 'Running'" in str(excinfo.value)


def test_ascii_glyphs_are_checked_too() -> None:
    check_layout(
        lambda width, locale: ["* Running", "\u276f", "status bar"],
        widths=(40,),
        locales=("en",),
        states=("running",),
        ascii_glyphs=True,
    )
    with pytest.raises(LayoutError):
        check_layout(
            lambda width, locale: ["● Running", "\u276f", "status bar"],
            widths=(40,),
            locales=("en",),
            states=("running",),
            ascii_glyphs=True,
        )


def test_a_prose_row_keeps_every_character() -> None:
    wrapped = ["the first half of a", "sentence that wraps", "\u276f", "status bar"]
    check_layout(
        lambda width, locale: wrapped,
        widths=(40,),
        locales=("en",),
        prose=("the first half of a sentence that wraps",),
    )
    with pytest.raises(LayoutError) as excinfo:
        check_layout(
            lambda width, locale: ["the first half of a", "sentence that wrap…", "\u276f", "status bar"],
            widths=(40,),
            locales=("en",),
            prose=("the first half of a sentence that wraps",),
        )
    assert "a prose row lost a character" in str(excinfo.value)


def test_a_narrow_row_stacks_instead_of_dropping_a_column() -> None:
    stacked = ["▸ ● Deploy the line", "  worker · 12s · /jobs 1", "\u276f", "status bar"]
    check_layout(
        lambda width, locale: stacked,
        widths=(40,),
        locales=("en",),
        columns=("worker", "12s", "/jobs 1"),
    )
    with pytest.raises(LayoutError) as excinfo:
        check_layout(
            lambda width, locale: ["▸ ● Deploy the line", "\u276f", "status bar"],
            widths=(40,),
            locales=("en",),
            columns=("worker", "12s"),
        )
    assert "a narrow row dropped the column 'worker'" in str(excinfo.value)


def test_a_cjk_row_counts_two_cells() -> None:
    """Twenty Chinese characters are 40 cells; twenty-one are 42."""
    check_layout(
        lambda width, locale: ["排" * 20, "\u276f", "status bar"],
        widths=(40,),
        locales=("zh-CN",),
    )
    with pytest.raises(LayoutError) as excinfo:
        check_layout(
            lambda width, locale: ["排" * 21, "\u276f", "status bar"],
            widths=(40,),
            locales=("zh-CN",),
        )
    assert "row 0 is 42 cells wide" in str(excinfo.value)


def test_the_defaults_are_the_contract_widths_and_locales() -> None:
    assert tui_layout_contract.WIDTHS == (40, 80, 120)
    assert tui_layout_contract.LOCALES == ("en", "zh-CN")
    assert LOCALES == ("en", "zh-CN")


def test_lossless_prose_ignores_continuation_rails() -> None:
    text = "排" * 19
    rows = ["  │ " + "排" * 18, "  │ 排"]
    check_layout(
        lambda width, locale: rows,
        widths=(40,),
        locales=("zh-CN",),
        prose=(text,),
    )


def test_state_glyph_and_word_must_be_on_the_same_row() -> None:
    def render(width: int, locale: str) -> list[str]:
        del width, locale
        return ["○ Queued", "● Running"]

    check_layout(render, widths=(40,), locales=("en",), states=("queued", "running"))
    with pytest.raises(LayoutError) as excinfo:
        check_layout(
            lambda width, locale: ["○ Running", "● Queued"],
            widths=(40,),
            locales=("en",),
            states=("queued", "running"),
        )
    message = str(excinfo.value)
    assert "state queued glyph and word are not on the same row" in message
    assert "state running glyph and word are not on the same row" in message
