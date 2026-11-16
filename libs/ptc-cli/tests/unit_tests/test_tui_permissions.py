"""Terminal-native permission prompt rendering (PLAN S09 / tui SPEC §7.8)."""

# ruff: noqa: RUF001

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from rich.cells import cell_len

_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.tui_i18n import check_parity, t  # noqa: E402
from ptc_cli.superagent.tui_permissions import (  # noqa: E402
    LOCAL_MUTATING_ACTIONS,
    STRINGS,
    PermissionManager,
    PermissionRequest,
    decision_from_key,
    render_permission_prompt,
)
from ptc_cli.superagent.tui_theme import status_glyph  # noqa: E402
from tests.unit_tests.tui_layout_contract import WIDTHS, check_layout  # noqa: E402

_FRAMES = (
    Path(__file__).resolve().parents[4]
    / "docs"
    / "design"
    / "terminal-native"
    / "tui"
)

_SHORT = PermissionRequest(
    action="local-merge",
    label="merge feat/tui",
    detail="into integrate/r2",
)
_SHORT_ZH = PermissionRequest(
    action="local-merge",
    label="merge feat/tui",
    detail="合入 integrate/r2",
)
_LONG_DETAIL = (
    "into integrate/r2 and also stage the very long command argument "
    "path/to/a/deeply/nested/worktree/and/another/segment/that/must/wrap "
    + ("x" * 120)
)
_LONG = PermissionRequest(
    action="local-shell",
    label="run " + ("npm test -- --runInBand " * 8),
    detail=_LONG_DETAIL,
)


def _request_for(locale: str, *, long: bool = False) -> PermissionRequest:
    if long:
        return _LONG
    return _SHORT_ZH if locale == "zh-CN" else _SHORT


def _kept(locale: str) -> tuple[str, ...]:
    """Localized answer keys and Escape hint that must survive every width."""
    return (
        t(STRINGS, "allow", locale),
        t(STRINGS, "session", locale),
        t(STRINGS, "deny", locale),
        *t(STRINGS, "keys", locale).split("  "),
        t(STRINGS, "escape", locale),
    )


def _frame_card_and_status(locale: str, width: int) -> tuple[list[str], str]:
    """Card rows and status-bar row from a §7.8 golden frame."""
    path = _FRAMES / f"tui-permission-{locale}-w{width}.txt"
    lines = path.read_text(encoding="utf-8").splitlines()
    mark = "权限：" if locale == "zh-CN" else "permission:"
    region = "命令" if locale == "zh-CN" else "commands"
    start = next(index for index, line in enumerate(lines) if mark in line)
    end = next(
        index
        for index in range(start + 1, len(lines))
        if lines[index].lstrip().startswith(region)
    )
    card = lines[start:end]
    status = next(
        line
        for line in reversed(lines)
        if t(STRINGS, "allow", locale) in line and t(STRINGS, "deny", locale) in line
    )
    return card, status


def test_string_table_is_bilingual() -> None:
    check_parity(STRINGS)
    for locale in ("en", "zh-CN"):
        for key in ("keys", "escape", "allow", "session", "deny", "mark", "action"):
            assert t(STRINGS, key, locale)


def test_short_prompt_matches_golden_card_and_status() -> None:
    english = render_permission_prompt(_SHORT, 80, "en")
    assert english[0] == "  ◆ permission: merge feat/tui"
    assert english[1] == "    action: local-merge"
    assert english[2] == "    into integrate/r2"
    assert english[3] == "    [a]llow once  [s]ession allow  [d]eny"
    assert english[4] == "    esc deny"
    assert english[5] == (
        " ◆ merge feat/tui (local-merge) — into…  a allow · s session · d deny"
    )

    chinese = render_permission_prompt(_SHORT_ZH, 80, "zh-CN")
    assert chinese[0] == "  ◆ 权限：merge feat/tui"
    assert chinese[1] == "    动作：local-merge"
    assert chinese[2] == "    合入 integrate/r2"
    assert chinese[3] == "    [a]允许一次  [s]本会话允许  [d]拒绝"
    assert chinese[4] == "    esc 拒绝"
    assert chinese[5] == (
        " ◆ merge feat/tui（local-merge）— 合入 integrate/r2"
        "  a 允许 · s 会话 · d 拒绝"
    )


def test_english_keys_stack_at_40_columns() -> None:
    rows = render_permission_prompt(_SHORT, 40, "en")
    assert rows[0] == "  ◆ permission: merge feat/tui"
    assert rows[1] == "    action: local-merge"
    assert rows[2] == "    into integrate/r2"
    assert rows[3] == "    [a]llow once  [s]ession allow"
    assert rows[4] == "    [d]eny"
    assert rows[5] == "    esc deny"
    assert rows[6] == " ◆ merge…  a allow · s session · d deny"


@pytest.mark.parametrize("width", list(WIDTHS))
@pytest.mark.parametrize("locale", ["en", "zh-CN"])
def test_row_structure_matches_golden_frames(width: int, locale: str) -> None:
    request = _request_for(locale)
    rows = render_permission_prompt(request, width, locale)
    frame_card, frame_status = _frame_card_and_status(locale, width)
    escape = f"    {t(STRINGS, 'escape', locale)}"
    assert escape in rows
    card = [row for row in rows[:-1] if row != escape]
    assert card == frame_card
    assert rows[-1] == frame_status
    for needle in _kept(locale):
        assert needle in "\n".join(rows)


def test_layout_contract_for_short_and_long_arguments() -> None:
    assert len(_LONG.detail) >= 200
    for long in (False, True):
        for locale in ("en", "zh-CN"):
            request = _request_for(locale, long=long)

            def render(
                width: int,
                render_locale: str,
                *,
                bound: PermissionRequest = request,
                fixed_locale: str = locale,
            ) -> list[str]:
                del render_locale
                return render_permission_prompt(bound, width, fixed_locale)

            check_layout(
                render,
                locales=(locale,),
                keep=_kept(locale),
                prose=(request.label, request.detail),
            )


@pytest.mark.parametrize("width", list(WIDTHS))
@pytest.mark.parametrize("locale", ["en", "zh-CN"])
def test_answer_keys_and_escape_never_drop(width: int, locale: str) -> None:
    short = "\n".join(render_permission_prompt(_request_for(locale), width, locale))
    long_rows = render_permission_prompt(_request_for(locale, long=True), width, locale)
    for needle in _kept(locale):
        assert needle in short
        assert needle in "\n".join(long_rows)
    assert all(cell_len(row) <= width for row in long_rows)
    status = long_rows[-1]
    for key in (
        t(STRINGS, "allow", locale),
        t(STRINGS, "session", locale),
        t(STRINGS, "deny", locale),
    ):
        assert key in status


def test_status_bar_cuts_label_not_keys() -> None:
    rows = render_permission_prompt(_SHORT, 40, "en")
    assert rows[-1] == " ◆ merge…  a allow · s session · d deny"
    assert "…" in rows[-1]
    assert "a allow · s session · d deny" in rows[-1]


def test_ascii_glyph_follows_ptc_ascii(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTC_ASCII", "1")
    rows = render_permission_prompt(_SHORT, 80, "en")
    assert rows[0].lstrip().startswith(f"{status_glyph('needs-you')} permission:")


def test_existing_helpers_keep_their_behaviour() -> None:
    manager = PermissionManager()
    assert manager.is_mutating("local-merge") is True
    assert manager.needs_confirmation("local-merge") is True
    manager.allow_session("local-merge")
    assert manager.needs_confirmation("local-merge") is False
    assert manager.session_allowed("local-merge") is True
    manager.allow_once("local-review")
    manager.deny("local-shell")
    auto = PermissionManager(auto_approve=True)
    assert auto.needs_confirmation("local-merge") is False
    assert decision_from_key("a") == "allow"
    assert decision_from_key("S") == "session"
    assert decision_from_key("d") == "deny"
    assert decision_from_key("x") == "pending"
    assert "into integrate/r2" in _SHORT.render()
    assert "local-merge" in LOCAL_MUTATING_ACTIONS


def test_multiline_detail_keeps_hang_and_flat_status() -> None:
    """Newlines in a shell argument become hung physical rows, not embedded newlines."""
    detail = "echo one\necho two"
    request = PermissionRequest("local-shell", "run shell command", detail)
    rows = render_permission_prompt(request, 120, "en")
    assert all("\n" not in row for row in rows)
    assert "    echo one" in rows
    assert "    echo two" in rows
    status = rows[-1]
    assert status.count("\n") == 0
    assert "echo one echo two" in status or "echo one" in status
    assert t(STRINGS, "allow", "en") in status
    assert t(STRINGS, "deny", "en") in status
    assert cell_len(status) <= 120


def test_cjk_wrap_never_opens_with_closing_mark() -> None:
    """SPEC §3.4: CJK closing marks move down with the character above them."""
    detail = ("中" * 18) + "，" + ("文" * 20)
    request = PermissionRequest("local-shell", "run shell command", detail)
    rows = render_permission_prompt(request, 40, "zh-CN")
    assert not any(row.lstrip().startswith("，") for row in rows)
    assert "，" in "".join(rows)
    assert all(cell_len(row) <= 40 for row in rows)


def test_cjk_wrap_moves_paired_closing_punctuation() -> None:
    """A paired CJK closing run never opens a continuation alone."""
    closing = "，。、；：？！"
    detail = ("中" * 17) + "？！" + ("文" * 20)
    request = PermissionRequest("local-shell", "run command", detail)
    rows = render_permission_prompt(request, 40, "zh-CN")
    assert not any(row.lstrip().startswith(tuple(closing)) for row in rows)
    assert "？！" in "".join(rows)
    assert any("中？！" in row for row in rows)
    assert all(cell_len(row) <= 40 for row in rows)


def test_cjk_wrap_fills_line_instead_of_stranding_short_head() -> None:
    """SPEC §3.4: a short head such as ``共 2`` never sits alone above a CJK run."""
    detail = "共 2 " + ("中" * 30)
    request = PermissionRequest("local-shell", "run shell command", detail)
    rows = render_permission_prompt(request, 40, "zh-CN")
    assert rows[2] != "    共 2"
    assert rows[2].startswith("    共 2 ")
    assert "中" in rows[2]
    assert all(cell_len(row) <= 40 for row in rows)
