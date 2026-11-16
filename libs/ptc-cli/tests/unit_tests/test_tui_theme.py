"""Theme tokens, contrast, CONDUCTOR_THEME selection, and the tokens.json sync.

``tui_theme`` is the TUI's copy of ``docs/design/terminal-native/system/tokens.json``.
The tests below load that file and compare it with the module, so the copy cannot
drift: every ink hex, every xterm-256 index, every ansi16 name, the seven Ticket
states and the reduced-motion rule are checked against the token set, and the
module may not carry a hex value the token set does not define.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent import tui_theme  # noqa: E402
from ptc_cli.superagent.tui_theme import (  # noqa: E402
    ANSI16,
    STATUS,
    XTERM256,
    contrast_ratio,
    grounds_for,
    reduced_motion_enabled,
    resolve_theme,
    status_glyph,
    status_ink,
    status_word,
    style_for,
    text_tokens,
)

_THEMES = ("dark", "light")
# tokens.json ink name -> tui_theme Theme field.
_INK_FIELDS = {
    "fg": "fg",
    "muted": "muted",
    "baton": "baton",
    "tempo": "tempo",
    "ok": "ok",
    "hold": "hold",
    "error": "error",
    "onAccent": "on_accent",
    "focus": "focus",
    "faint": "faint",
    "rule": "rule",
}
# tokens.json surface name -> tui_theme Theme field, or "" for the body grounds.
_SURFACE_FIELDS = {"page": "", "canvas": "", "elevated": "", "selection": "selection_bg", "accent": "accent"}
# tokens.json spells the ink on the accent fill in camelCase; the module is Python.
_TABLE_KEYS = {"onAccent": "on_accent"}
_HEX_LITERAL = re.compile(r"#[0-9A-Fa-f]{3,8}")


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs/design/terminal-native/system/tokens.json").is_file():
            return parent
    pytest.fail("the design tree is not above this test file")


def _tokens() -> dict:
    path = _repo_root() / "docs/design/terminal-native/system/tokens.json"
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _theme(name: str):
    return resolve_theme({"CONDUCTOR_THEME": name, "LANG": "en_US.UTF-8"})


def test_the_token_file_is_the_repo_one() -> None:
    document = _tokens()
    assert document["id"] == "terminal-native"
    assert document["primaryTheme"] == "dark"
    assert set(document["themes"]) == {"dark", "light"}


@pytest.mark.parametrize("name", _THEMES)
def test_every_ink_matches_the_token_file(name: str) -> None:
    theme = _theme(name)
    for token, hex_value in _tokens()["themes"][name]["ink"].items():
        assert getattr(theme, _INK_FIELDS[token]) == hex_value, token


@pytest.mark.parametrize("name", _THEMES)
def test_every_surface_matches_the_token_file(name: str) -> None:
    theme = _theme(name)
    surfaces = _tokens()["themes"][name]["surfaces"]
    grounds = grounds_for(theme)
    for position, token in enumerate(("page", "canvas", "elevated")):
        assert grounds[position] == surfaces[token], token
        assert _SURFACE_FIELDS[token] == ""
    assert theme.selection_bg == surfaces["selection"]
    assert theme.accent == surfaces["accent"]


@pytest.mark.parametrize("name", _THEMES)
def test_xterm256_indexes_match_the_token_file(name: str) -> None:
    table = _tokens()["terminal"]["xterm256"]["themes"][name]
    for entry in table["ink"]:
        token = _TABLE_KEYS.get(entry["token"].removeprefix("ink."), entry["token"].removeprefix("ink."))
        assert XTERM256[name][token] == entry["index"], token
        assert tui_theme.xterm256_hex(entry["index"]) == entry["paletteHex"], token
    for entry in table["surfaces"]:
        token = entry["token"].removeprefix("surface.")
        assert XTERM256[name][token] == entry["index"], token


def test_ansi16_names_match_the_token_file() -> None:
    assert _tokens()["terminal"]["ansi16"] == ANSI16


def test_ansi16_theme_uses_the_ansi16_names() -> None:
    theme = resolve_theme({"CONDUCTOR_THEME": "ansi16"})
    for key, expected in _tokens()["terminal"]["ansi16"].items():
        if key == "selection":
            assert theme.selection_bg == f"bg:default {expected}"
            continue
        if key == "fg":
            # "default" and "" both mean "paint nothing" to prompt_toolkit.
            assert theme.fg == "default"
            continue
        if key == "tool":
            assert theme.tool == expected
            continue
        assert getattr(theme, _INK_FIELDS[key]) == expected, key


@pytest.mark.parametrize("name", _THEMES)
def test_baton_and_accent_are_the_token_accent(name: str) -> None:
    theme = _theme(name)
    token_set = _tokens()["themes"][name]
    assert theme.baton == token_set["ink"]["baton"]
    assert theme.accent == token_set["surfaces"]["accent"]
    assert theme.accent == theme.baton
    assert theme.focus == token_set["ink"]["focus"]


def test_the_module_carries_no_hex_outside_the_token_set() -> None:
    source = Path(tui_theme.__file__).read_text(encoding="utf-8")
    found = {value.upper() for value in _HEX_LITERAL.findall(source)}
    expected = set()
    for name in _THEMES:
        expected.update(_tokens()["themes"][name]["ink"].values())
        expected.update(_tokens()["themes"][name]["surfaces"].values())
    expected = {value.upper() for value in expected}
    assert len(expected) == 25
    assert found == expected


def test_the_seven_states_match_the_token_file() -> None:
    rows = _tokens()["status"]
    assert [row["id"] for row in rows] == [row.id for row in STATUS]
    for row, status in zip(rows, STATUS, strict=True):
        assert status.ink == row["ink"], status.id
        assert status.glyph == row["glyph"], status.id
        assert status.ascii == row["ascii"], status.id
        assert status.label == row["label"], status.id
        assert status.label_zh == row["labelZh"], status.id
        assert status_ink(status.id) == row["ink"]
        assert status_word(status.id) == row["label"]
        assert status_word(status.id, "zh-CN") == row["labelZh"]
        # Explicit UTF-8 locale: the default reads os.environ, and a runner
        # with LC_ALL=C gets the ASCII twin.
        assert status_glyph(status.id, {"LANG": "en_US.UTF-8"}) == row["glyph"]
        assert status_glyph(status.id, {"PTC_ASCII": "1"}) == row["ascii"]


def test_needs_you_alias_resolves() -> None:
    assert status_ink("needs_you") == "hold"
    assert status_word("needs_you", "zh-CN") == "需要你"


@pytest.mark.parametrize("name", _THEMES)
def test_text_tokens_clear_contrast(name: str) -> None:
    theme = _theme(name)
    for token, colour in text_tokens(theme).items():
        for ground in grounds_for(theme):
            ratio = contrast_ratio(colour, ground)
            assert ratio >= 4.5, f"{name} {token} {colour} on {ground} = {ratio:.2f}"


def test_published_contrast_pairs_are_reproduced() -> None:
    for pair in _tokens()["contrast"]["pairs"]:
        measured = contrast_ratio(pair["inkHex"], pair["surfaceHex"])
        assert measured == pytest.approx(pair["ratio"], abs=0.01), pair["ink"]
        assert (measured >= pair["minimum"]) is pair["legal"], pair["ink"]


@pytest.mark.parametrize("name", _THEMES)
def test_truecolour_and_fallback_styles(name: str) -> None:
    theme = _theme(name)
    table = _tokens()["terminal"]["xterm256"]["themes"][name]
    by_token = {}
    for entry in table["ink"]:
        token = entry["token"].removeprefix("ink.")
        by_token[_TABLE_KEYS.get(token, token)] = entry
    for token, entry in by_token.items():
        assert style_for(theme, token, depth="truecolor") == f"fg:{entry['truecolour']}"
        assert style_for(theme, token, depth="xterm256") == f"fg:{entry['paletteHex']}"
    assert style_for(theme, "baton", depth="ansi16") == "ansicyan"
    assert style_for(theme, "ok", depth="ansi16") == "ansigreen"
    assert style_for(theme, "fg", depth="ansi16") == ""


def test_reduced_motion_env_and_dumb_terminal() -> None:
    assert reduced_motion_enabled({}) is False
    assert reduced_motion_enabled({"TERM": "xterm-256color"}) is False
    for value in ("1", "true", "yes", "on", "ON"):
        assert reduced_motion_enabled({"PTC_REDUCED_MOTION": value}) is True
    assert reduced_motion_enabled({"PTC_REDUCED_MOTION": "0", "TERM": "dumb"}) is True
    assert reduced_motion_enabled({"TERM": "dumb"}) is True


def test_reduced_motion_freezes_the_spinner() -> None:
    frozen = tui_theme.spinner_frames({"TERM": "dumb"})
    assert frozen == ("●",)
    assert tui_theme.spinner_frames({}) == ("♪", "♫", "♬", "♫")
    assert tui_theme.spinner_frames({"PTC_ASCII": "1"}) == ("*",)


def test_conductor_theme_overrides_colorfgbg() -> None:
    theme = resolve_theme({"CONDUCTOR_THEME": "light", "COLORFGBG": "15;0", "NO_COLOR": "1"})
    assert theme.name == "light"
    assert theme.baton == "#3F4FC8"


def test_no_color_and_explicit_none_have_no_hex() -> None:
    for env in ({"NO_COLOR": "1"}, {"CONDUCTOR_THEME": "none"}):
        theme = resolve_theme(env)
        assert theme.name == "none"
        assert theme.fg == ""
        assert theme.baton == ""
        assert theme.glyphs.prompt == "\u276f"


def test_unknown_theme_falls_back_without_raising() -> None:
    theme = resolve_theme({"CONDUCTOR_THEME": "neon", "COLORFGBG": "0;15"})
    assert theme.name == "light"


def test_colorfgbg_background_selects_dark_or_light() -> None:
    assert resolve_theme({"COLORFGBG": "15;0"}).name == "dark"
    assert resolve_theme({"COLORFGBG": "0;15"}).name == "light"


def test_ascii_glyphs_when_requested() -> None:
    theme = resolve_theme({"CONDUCTOR_THEME": "dark", "PTC_ASCII": "1"})
    assert theme.glyphs.prompt == ">"
    assert theme.glyphs.spinner == ("*",)
    assert theme.glyphs.blocked == "#"
    assert theme.glyphs.archived == "-"


def test_derived_roles_follow_their_canonical_fallbacks() -> None:
    theme = _theme("dark")
    for role, canonical in (("accent", "baton"), ("user", "baton"), ("warn", "hold"), ("danger", "error")):
        assert style_for(theme, role, depth="xterm256") == style_for(theme, canonical, depth="xterm256")
        assert style_for(theme, role, depth="ansi16") == style_for(theme, canonical, depth="ansi16")
    assert style_for(theme, "tool", depth="xterm256") == style_for(theme, "baton", depth="xterm256")
    assert style_for(theme, "tool", depth="ansi16") == "ansimagenta"
    assert style_for(theme, "on_accent", depth="ansi16") == ""


def _style_rule(style, name: str) -> str:
    for selector, value in style.style_rules:
        if str(selector) == name:
            return str(value)
    return ""


def test_xterm256_resolution_reaches_public_roles_and_the_unchanged_consumer() -> None:
    from ptc_cli.superagent.tui import build_default_style

    environ = {"CONDUCTOR_THEME": "dark", "TERM": "xterm-256color"}
    theme = resolve_theme(environ)
    assert theme.accent == theme.user == theme.tool == theme.baton == "#87AFFF"
    assert theme.warn == "#FFAF5F"
    assert theme.danger == "#FF8787"
    assert theme.selection_bg == "#262626"

    style = build_default_style(environ)
    assert _style_rule(style, "conversation") == "fg:#EEEEEE"
    assert _style_rule(style, "tui-user") == "fg:#87AFFF"
    assert _style_rule(style, "tui-subagent") == "fg:#87AFFF"
    assert _style_rule(style, "tui-error") == "fg:#FF8787"
    assert _style_rule(style, "tui-warn") == "fg:#FFAF5F"
    assert _style_rule(style, "slash-menu-selected") == "bold fg:#EEEEEE bg:#262626"


def test_ansi16_resolution_reaches_public_roles_and_the_unchanged_consumer() -> None:
    from ptc_cli.superagent.tui import build_default_style

    environ = {"CONDUCTOR_THEME": "dark", "TERM": "xterm"}
    theme = resolve_theme(environ)
    assert theme.fg == theme.on_accent == "default"
    assert theme.selection_bg == "bg:default reverse"
    assert theme.accent == theme.user == theme.baton == "ansicyan"
    assert theme.tool == "ansimagenta"
    assert theme.warn == "ansiyellow"
    assert theme.danger == "ansired"

    style = build_default_style(environ)
    assert _style_rule(style, "conversation") == "default"
    assert _style_rule(style, "tui-user") == "fg:ansicyan"
    assert _style_rule(style, "tui-subagent") == "fg:ansimagenta"
    assert _style_rule(style, "tui-error") == "fg:ansired"
    assert _style_rule(style, "tui-warn") == "fg:ansiyellow"
    assert _style_rule(style, "slash-menu-selected") == "bold default bg:default reverse"
    assert all("#" not in str(value) for _selector, value in style.style_rules)
