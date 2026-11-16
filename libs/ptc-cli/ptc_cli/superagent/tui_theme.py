"""Conductor TUI colour tokens, glyph set and motion switches.

This module is the TUI's copy of ``docs/design/terminal-native/system/tokens.json``.
The values are written out instead of read from that file at import time because the
TUI ships as an installed package and the design tree does not travel with it, so
``test_tui_theme.py`` loads the JSON and fails when this copy drifts from it.

``baton`` and ``accent`` are the token accent, which the token set defines as the
same hex as ``ink.baton``; both are filled from that one entry so the brand colour
cannot split in two the way the web accent and the TUI baton once did. Light ``hold``
is ``#9A4A06`` and light ``ok`` is ``#1A7F37`` rather than the dark pair: the token
set picks the darker candidate so body text clears 4.5:1 on white.

The public names are a contract. ``tui.py`` reads the theme through
``resolve_theme``, ``style_for``, ``text_tokens`` and ``grounds_for``, and the
snapshot goldens record role letters rather than hex, so a token change must not
move a frame. New readers (the renderers of the terminal-native screens) take the
state vocabulary from :data:`STATUS` and the locale-independent copy from
:mod:`ptc_cli.superagent.tui_i18n`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace

_SRGB_THRESHOLD = 0.04045
_HEX_CHANNELS = 6

# From tokens.json, the themes.dark.surfaces and themes.light.surfaces tables.
_DARK_SURFACES = {
    "page": "#0F1115",
    "canvas": "#1E1E1E",
    "elevated": "#282C34",
    "selection": "#1A2140",
    "accent": "#8B9CFF",
}
_LIGHT_SURFACES = {
    "page": "#FFFFFF",
    "canvas": "#F5F4F1",
    "elevated": "#FDF6E3",
    "selection": "#DCE3FF",
    "accent": "#3F4FC8",
}

# From tokens.json, the themes.dark.ink and themes.light.ink tables.
_DARK_INK = {
    "fg": "#E6E8EE",
    "muted": "#9AA3B5",
    "baton": "#8B9CFF",
    "tempo": "#56C7D9",
    "ok": "#5BD38A",
    "hold": "#F2B544",
    "error": "#FF7A7A",
    "on_accent": "#0F1115",
    "focus": "#8B9CFF",
    "faint": "#2A2F3A",
    "rule": "#3A4150",
}
_LIGHT_INK = {
    "fg": "#1B1E26",
    "muted": "#5B6272",
    "baton": "#3F4FC8",
    "tempo": "#0E7A8C",
    "ok": "#1A7F37",
    "hold": "#9A4A06",
    "error": "#C93838",
    "on_accent": "#FFFFFF",
    "focus": "#3F4FC8",
    "faint": "#C9CCD4",
    "rule": "#C9CCD4",
}

_DARK_GROUNDS = (
    _DARK_SURFACES["page"],
    _DARK_SURFACES["canvas"],
    _DARK_SURFACES["elevated"],
)
_LIGHT_GROUNDS = (
    _LIGHT_SURFACES["page"],
    _LIGHT_SURFACES["canvas"],
    _LIGHT_SURFACES["elevated"],
)

# From tokens.json, terminal.xterm256.themes for each theme. The keys are the token
# names without their ``ink.`` / ``surface.`` prefix; the palette hex an index draws
# is computed by xterm256_hex, never copied, because a hand-copied hex is exactly
# the drift this module exists to prevent.
_XTERM256_DARK = {
    "page": 233,
    "canvas": 234,
    "elevated": 236,
    "selection": 235,
    "fg": 255,
    "muted": 248,
    "baton": 111,
    "tempo": 80,
    "ok": 78,
    "hold": 215,
    "error": 210,
    "on_accent": 233,
    "focus": 111,
    "faint": 236,
    "rule": 238,
}
_XTERM256_LIGHT = {
    "page": 15,
    "canvas": 255,
    "elevated": 230,
    "selection": 189,
    "fg": 234,
    "muted": 59,
    "baton": 25,
    "tempo": 24,
    "ok": 23,
    "hold": 58,
    "error": 125,
    "on_accent": 15,
    "focus": 62,
    "faint": 252,
    "rule": 252,
}
XTERM256 = {"dark": _XTERM256_DARK, "light": _XTERM256_LIGHT}

# From tokens.json, terminal.ansi16. These are prompt_toolkit colour names; the
# selected row is painted by reversing the cell, so it has no colour to name.
ANSI16 = {
    "fg": "default",
    "muted": "ansibrightblack",
    "faint": "ansibrightblack",
    "rule": "ansibrightblack",
    "baton": "ansicyan",
    "tempo": "ansicyan",
    "focus": "ansicyan",
    "ok": "ansigreen",
    "hold": "ansiyellow",
    "error": "ansired",
    "selection": "reverse",
    "tool": "ansimagenta",
}

# From tokens.json, motion.durationMs, motion.ease and motion.spinner.
MOTION_DURATIONS_MS = {
    "quick": 160,
    "fold": 240,
    "enter": 280,
    "exit": 140,
    "reveal": 360,
    "spinner": 100,
}
MOTION_EASE = {
    "standard": (0.16, 1.0, 0.3, 1.0),
    "reveal": (0.33, 1.0, 0.68, 1.0),
}
SPINNER_FRAMES = ("♪", "♫", "♬", "♫")
SPINNER_ASCII = ("*",)
_REDUCED_MOTION_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class Glyphs:
    """State marks. Colour is never the only signal."""

    prompt: str = "\u276f"
    conductor: str = "♩"
    voice: str = "▸"
    collapsed: str = "╰"
    done: str = "✔"
    needs_you: str = "◆"
    failed: str = "✖"
    queued: str = "○"
    online: str = "●"
    blocked: str = "▣"
    archived: str = "⊘"
    spinner: tuple[str, ...] = SPINNER_FRAMES


_ASCII = Glyphs(
    prompt=">",
    conductor="@",
    voice=">",
    collapsed="`",
    done="+",
    needs_you="?",
    failed="x",
    queued="o",
    online="*",
    blocked="#",
    archived="-",
    spinner=SPINNER_ASCII,
)


@dataclass(frozen=True, slots=True)
class Status:
    """One Ticket state: an ink, a glyph, its ASCII twin and a word per locale."""

    id: str
    ink: str
    glyph: str
    ascii: str
    label: str
    label_zh: str


# From tokens.json, the status table. The order is the order the screens list
# the states in.
STATUS = (
    Status("queued", "muted", "○", "o", "Queued", "排队中"),
    Status("running", "tempo", "●", "*", "Running", "运行中"),
    Status("blocked", "hold", "▣", "#", "Blocked", "已阻塞"),
    Status("needs-you", "hold", "◆", "?", "Needs you", "需要你"),
    Status("failed", "error", "✖", "x", "Failed", "失败"),
    Status("completed", "ok", "✔", "+", "Completed", "已完成"),
    Status("archived", "muted", "⊘", "-", "Archived", "已归档"),
)
STATUS_IDS = tuple(item.id for item in STATUS)
_STATUS_BY_ID = {item.id: item for item in STATUS}
# From tokens.json, status[].alias, so a caller may spell the state the old way.
_STATUS_ALIASES = {"needs_you": "needs-you"}


@dataclass(frozen=True, slots=True)
class Theme:
    """One resolved palette. Empty strings mean "do not paint"."""

    name: str
    fg: str
    muted: str
    faint: str
    baton: str
    tempo: str
    ok: str
    hold: str
    error: str
    rule: str
    selection_bg: str
    glyphs: Glyphs
    accent: str = ""
    user: str = ""
    tool: str = ""
    warn: str = ""
    danger: str = ""
    focus: str = ""
    on_accent: str = ""
    depth: str = "truecolor"

    def __post_init__(self) -> None:
        """Fill the derived roles. ``accent`` is the token accent, never a second brand."""
        if not self.accent:
            object.__setattr__(self, "accent", self.baton)
        if not self.user:
            object.__setattr__(self, "user", self.baton)
        if not self.tool:
            object.__setattr__(self, "tool", self.baton)
        if not self.warn:
            object.__setattr__(self, "warn", self.hold)
        if not self.danger:
            object.__setattr__(self, "danger", self.error)
        if not self.focus:
            object.__setattr__(self, "focus", self.baton)
        if not self.on_accent:
            object.__setattr__(self, "on_accent", self.fg)


def _utf8(environ: Mapping[str, str]) -> bool:
    if environ.get("PTC_ASCII", "").strip() == "1":
        return False
    for key in ("LC_ALL", "LC_CTYPE", "LANG"):
        raw = environ.get(key, "")
        if not raw:
            continue
        lowered = raw.lower()
        return "utf-8" in lowered or "utf8" in lowered
    return True


def _glyphs(environ: Mapping[str, str]) -> Glyphs:
    return Glyphs() if _utf8(environ) else _ASCII


def _theme_kwargs(ink: Mapping[str, str], surfaces: Mapping[str, str]) -> dict[str, str]:
    """Theme fields from one theme's ink and surface tables."""
    return {
        "fg": ink["fg"],
        "muted": ink["muted"],
        "faint": ink["faint"],
        "baton": ink["baton"],
        "tempo": ink["tempo"],
        "ok": ink["ok"],
        "hold": ink["hold"],
        "error": ink["error"],
        "rule": ink["rule"],
        "selection_bg": surfaces["selection"],
        "accent": surfaces["accent"],
        "focus": ink["focus"],
        "on_accent": ink["on_accent"],
    }


_DARK = _theme_kwargs(_DARK_INK, _DARK_SURFACES)
_LIGHT = _theme_kwargs(_LIGHT_INK, _LIGHT_SURFACES)

_ANSI16_RGB = (
    (0, 0, 0),
    (128, 0, 0),
    (0, 128, 0),
    (128, 128, 0),
    (0, 0, 128),
    (128, 0, 128),
    (0, 128, 128),
    (192, 192, 192),
    (128, 128, 128),
    (255, 0, 0),
    (0, 255, 0),
    (255, 255, 0),
    (0, 0, 255),
    (255, 0, 255),
    (0, 255, 255),
    (255, 255, 255),
)
_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)
_XTERM_ANSI_LAST = len(_ANSI16_RGB) - 1
_XTERM_CUBE_FIRST = 16
_XTERM_CUBE_LAST = 231
_XTERM_GRAY_FIRST = 232
_XTERM_GRAY_LAST = 255
# Public roles that inherit a canonical token's fallback at shallow depths.
_TOKEN_KEYS = {
    "selection_bg": "selection",
    "accent": "baton",
    "user": "baton",
    "tool": "baton",
    "warn": "hold",
    "danger": "error",
}


def _named(name: str, environ: Mapping[str, str]) -> Theme:
    glyphs = _glyphs(environ)
    depth = color_depth(environ)
    if name == "dark":
        theme = Theme(name="dark", glyphs=glyphs, depth=depth, **_DARK)
    elif name == "light":
        theme = Theme(name="light", glyphs=glyphs, depth=depth, **_LIGHT)
    elif name == "ansi16":
        theme = Theme(
            name="ansi16",
            fg="",
            muted="ansibrightblack",
            faint="ansibrightblack",
            baton="ansicyan",
            tempo="ansicyan",
            ok="ansigreen",
            hold="ansiyellow",
            error="ansired",
            rule="ansibrightblack",
            selection_bg="",
            glyphs=glyphs,
            depth="ansi16",
            tool="ansimagenta",
            focus="ansicyan",
        )
    else:
        return Theme(
            name="none",
            fg="",
            muted="",
            faint="",
            baton="",
            tempo="",
            ok="",
            hold="",
            error="",
            rule="",
            selection_bg="",
            glyphs=glyphs,
            depth="none",
        )
    return _at_depth(theme)


def _auto_name(environ: Mapping[str, str]) -> str:
    raw = environ.get("COLORFGBG", "").strip()
    if not raw:
        return "dark"
    parts = [part.strip() for part in raw.split(";") if part.strip()]
    background = parts[-1] if parts else ""
    try:
        value = int(background)
    except ValueError:
        return "dark"
    if value in {7, 15}:
        return "light"
    return "dark"


def color_depth(environ: Mapping[str, str] | None = None) -> str:
    """The depth the terminal can draw: truecolor, xterm256, ansi16 or none.

    ``COLORTERM`` wins, then a ``256color`` ``TERM``, then any other real terminal
    falls back to the 16 ANSI colours and ``TERM=dumb`` paints nothing. An unset
    ``TERM`` is not a probe — there is no terminal to ask — so the theme keeps its
    truecolour hex and the caller decides what to do with it.
    """
    environment = os.environ if environ is None else environ
    if environment.get("NO_COLOR", "").strip():
        return "none"
    colorterm = environment.get("COLORTERM", "").strip().lower()
    if colorterm in {"truecolor", "24bit"}:
        return "truecolor"
    term = environment.get("TERM", "").strip().lower()
    if "256color" in term:
        return "xterm256"
    if term and term != "dumb":
        return "ansi16"
    return "truecolor" if not term else "none"


def resolve_theme(environ: Mapping[str, str] | None = None) -> Theme:
    """Pick a theme. ``CONDUCTOR_THEME`` wins, then ``NO_COLOR``, then auto.

    An unknown ``CONDUCTOR_THEME`` value falls through to auto detection
    instead of raising. The theme carries the colour depth the terminal can
    draw, so ``style_for`` can pick the 256-colour or 16-colour fallback.
    """
    environment = os.environ if environ is None else environ
    requested = environment.get("CONDUCTOR_THEME", "").strip().lower()
    aliases = {
        "no-color": "none",
        "nocolor": "none",
        "plain": "none",
        "16": "ansi16",
        "ansi": "ansi16",
    }
    requested = aliases.get(requested, requested)
    if requested in {"dark", "light", "ansi16", "none"}:
        return _named(requested, environment)
    if environment.get("NO_COLOR", "").strip():
        return _named("none", environment)
    return _named(_auto_name(environment), environment)


def _channel(hex_color: str) -> tuple[float, float, float]:
    text = hex_color.lstrip("#")
    if len(text) != _HEX_CHANNELS:
        message = f"not a hex colour: {hex_color!r}"
        raise ValueError(message)
    red = int(text[0:2], 16) / 255
    green = int(text[2:4], 16) / 255
    blue = int(text[4:6], 16) / 255

    def linear(channel: float) -> float:
        if channel <= _SRGB_THRESHOLD:
            return channel / 12.92
        return ((channel + 0.055) / 1.055) ** 2.4

    return linear(red), linear(green), linear(blue)


def _luminance(hex_color: str) -> float:
    red, green, blue = _channel(hex_color)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    """WCAG contrast ratio for two ``#RRGGBB`` colours."""
    left = _luminance(fg_hex)
    right = _luminance(bg_hex)
    lighter = max(left, right)
    darker = min(left, right)
    return (lighter + 0.05) / (darker + 0.05)


def xterm256_hex(index: int) -> str:
    """The hex an xterm-256 index draws: the ANSI 16, the 6x6x6 cube, then gray."""
    if 0 <= index <= _XTERM_ANSI_LAST:
        red, green, blue = _ANSI16_RGB[index]
    elif _XTERM_CUBE_FIRST <= index <= _XTERM_CUBE_LAST:
        cell = index - _XTERM_CUBE_FIRST
        red = _CUBE_LEVELS[cell // 36]
        green = _CUBE_LEVELS[(cell // 6) % 6]
        blue = _CUBE_LEVELS[cell % 6]
    elif _XTERM_GRAY_FIRST <= index <= _XTERM_GRAY_LAST:
        level = 8 + 10 * (index - _XTERM_GRAY_FIRST)
        red = green = blue = level
    else:
        message = f"not an xterm-256 index: {index!r}"
        raise ValueError(message)
    return f"#{red:02X}{green:02X}{blue:02X}"


def _fallback_style(theme: Theme, token: str, depth: str) -> str | None:
    """The style string for a shallow terminal, including inherited roles.

    ``None`` means the token has no entry at that depth and the caller keeps the
    theme's own value; ``""`` means the entry is the terminal's default, so the
    row paints nothing rather than a colour the terminal has to approximate.
    """
    key = _TOKEN_KEYS.get(token, token)
    if depth == "xterm256":
        index = XTERM256.get(theme.name, {}).get(key)
        if index is None:
            return None
        return f"fg:{xterm256_hex(index)}"
    key = {"on_accent": "fg", "tool": "tool"}.get(token, key)
    name = ANSI16.get(key, "")
    if not name:
        return None
    if name == "reverse":
        return "bg:default reverse"
    return "" if name == "default" else name


def style_for(theme: Theme, token: str, *, depth: str | None = None) -> str:
    """A prompt_toolkit style string. Body tokens never set a background.

    ``depth`` overrides the theme's own colour depth: ``xterm256`` paints the
    palette entry of the token's index and ``ansi16`` its colour name, so a
    shallow terminal keeps the token instead of the truecolour hex. Derived
    roles such as accent, user, warn and danger follow their canonical token.
    """
    if theme.name == "none":
        return ""
    if depth is None:
        depth = theme.depth or "truecolor"
    if depth in {"xterm256", "ansi16"}:
        fallback = _fallback_style(theme, token, depth)
        if fallback is not None:
            return fallback
    value = getattr(theme, token, "") or ""
    if not value or value == "default":
        return ""
    if value.startswith(("ansi", "fg:")):
        return value
    if value.startswith("#"):
        return f"fg:{value}"
    return value


_STYLE_ROLES = (
    "fg",
    "muted",
    "faint",
    "baton",
    "tempo",
    "ok",
    "hold",
    "error",
    "rule",
    "selection_bg",
    "accent",
    "user",
    "tool",
    "warn",
    "danger",
    "focus",
    "on_accent",
)


def _at_depth(theme: Theme) -> Theme:
    """Apply the terminal depth to the public roles read by the shipped TUI."""
    if theme.depth not in {"xterm256", "ansi16"}:
        return theme
    updates: dict[str, str] = {}
    for token in _STYLE_ROLES:
        fallback = _fallback_style(theme, token, theme.depth)
        if fallback is not None:
            updates[token] = fallback.removeprefix("fg:") or "default"
    return replace(theme, **updates)


def text_tokens(theme: Theme) -> dict[str, str]:
    """Hex tokens that must clear 4.5:1. Decorative rule and faint are omitted."""
    return {
        "fg": theme.fg,
        "muted": theme.muted,
        "baton": theme.baton,
        "tempo": theme.tempo,
        "ok": theme.ok,
        "hold": theme.hold,
        "error": theme.error,
    }


def grounds_for(theme: Theme) -> tuple[str, ...]:
    """The three body grounds of the theme: page, canvas, elevated."""
    if theme.name == "light":
        return _LIGHT_GROUNDS
    return _DARK_GROUNDS


def status_row(status_id: str) -> Status:
    """The :class:`Status` row for an id, with the ``needs_you`` alias resolved."""
    key = _STATUS_ALIASES.get(status_id, status_id)
    row = _STATUS_BY_ID.get(key)
    if row is None:
        message = f"unknown ticket state: {status_id!r}"
        raise KeyError(message)
    return row


def status_ink(status_id: str) -> str:
    """The ink token name of a state, e.g. ``tempo`` for ``running``."""
    return status_row(status_id).ink


def status_style(theme: Theme, status_id: str) -> str:
    """The style string of a state's ink, for a row that already has a ground."""
    return style_for(theme, status_ink(status_id))


def status_glyph(status_id: str, environ: Mapping[str, str] | None = None) -> str:
    """The state mark: the Unicode glyph, or its ASCII twin when asked for one."""
    row = status_row(status_id)
    return row.ascii if not _utf8(os.environ if environ is None else environ) else row.glyph


def status_word(status_id: str, locale: str = "en") -> str:
    """The state word in ``en`` or ``zh-CN``. Never truncated by a caller."""
    row = status_row(status_id)
    return row.label_zh if locale == "zh-CN" else row.label


def reduced_motion_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True when no animation may run: ``PTC_REDUCED_MOTION`` or ``TERM=dumb``.

    The shipped full-screen TUI carries the same predicate in ``tui.py``; the
    renderers of the terminal-native screens read it from here so the token set
    stays the one place a motion rule is written down.
    """
    environment = os.environ if environ is None else environ
    flag = str(environment.get("PTC_REDUCED_MOTION", "")).strip().lower()
    if flag in _REDUCED_MOTION_VALUES:
        return True
    return str(environment.get("TERM", "")).strip().lower() == "dumb"


def spinner_frames(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The frames a queued or running row cycles, or one static frame when reduced."""
    environment = os.environ if environ is None else environ
    if reduced_motion_enabled(environment):
        return (status_glyph("running", environment),)
    return SPINNER_ASCII if not _utf8(environment) else SPINNER_FRAMES
