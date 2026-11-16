"""Terminal-control-safe text for the core CLI and the TUI.

The scanner below is the display scanner, moved here unchanged.
This module imports only the standard library.
"""

from __future__ import annotations

import re

# Characters that change what a reader sees, or carry text a reader cannot see.
# The bidirectional controls (marks, embeddings, overrides, isolates) reorder
# what is drawn, so a path or a verdict can read differently from what it is;
# the tag block and the variation selectors are invisible ASCII/byte carriers;
# the zero-width joiners, fillers and format characters hide text inside a word.
# This is the server's fold table plus its lone-surrogate taint (output_guard's
# ``_INVISIBLE_RANGES`` and ``_TAINT_RANGES``), so the terminal never shows a
# character the guard had to look through; a test pins the two together.
#
# ZWJ, ZWNJ and the variation selectors go too: prompt_toolkit counts them as
# zero cells while a shaping terminal draws a joined glyph, so they only skew
# the layout and give the text a hidden one-bit channel. The price is that emoji
# ZWJ sequences and flag tag sequences fall apart into their parts and Persian
# and Indic ZWNJ spelling is lost on screen; the server record keeps them.
_HIDDEN_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),  # soft hyphen
    (0x034F, 0x034F),  # combining grapheme joiner
    (0x0600, 0x0605),  # Arabic number signs
    (0x061C, 0x061C),  # Arabic letter mark
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x115F, 0x1160),  # Hangul fillers
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),  # Mongolian variation selectors
    (0x200B, 0x200F),  # zero-width space/joiners, LRM, RLM
    (0x202A, 0x202E),  # bidi embeddings and overrides
    (0x2060, 0x206F),  # word joiner, invisible operators, bidi isolates
    (0x3164, 0x3164),  # Hangul filler
    (0xFE00, 0xFE0F),  # variation selectors
    (0xFEFF, 0xFEFF),  # byte order mark / zero-width no-break space
    (0xFFA0, 0xFFA0),  # halfwidth Hangul filler
    (0xFFF0, 0xFFFB),  # specials, interlinear annotation
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xD800, 0xDFFF),  # lone surrogates: cannot be encoded, abort the write
    (0xE0000, 0xE0FFF),  # tag characters, variation selectors supplement, reserved
)
_HIDDEN = re.compile(
    "["
    + "".join(f"\\U{low:08x}-\\U{high:08x}" for low, high in _HIDDEN_RANGES)
    + "]"
)

# Where a string control ends: the first of these after its introducer. BEL ends
# only an OSC, and U+009C ends every string control but the 7-bit ``ESC ]`` one
# (see the module docstring for why that one stays open); ``ESC \`` (ST) ends all.
# The scanner looks for them with ``str.find``, the older filter below with the
# regexes built from the same strings.
_OSC_ENDS = ("\x07", "\x1b\\")
_C1_OSC_ENDS = ("\x07", "\x9c", "\x1b\\")
_STRING_ENDS = ("\x9c", "\x1b\\")
_END_OSC, _END_C1_OSC, _END_STRING = (re.compile("|".join(map(re.escape, ends))) for ends in (_OSC_ENDS, _C1_OSC_ENDS, _STRING_ENDS))

# The scanner behind ``plain`` and ``TokenSanitizer``. A control starts at ESC or
# at a C1 control; which terminator ends the string control it introduces is
# looked up by the character after ESC (OSC, DCS, SOS, PM, APC) or by the C1
# control itself.
_INTRODUCER = re.compile(r"[\x1b\x80-\x9f]")
_ESC_STRING_ENDS = {"]": _OSC_ENDS, "P": _STRING_ENDS, "X": _STRING_ENDS, "^": _STRING_ENDS, "_": _STRING_ENDS}
_C1_STRING_ENDS = {
    "\x9d": _C1_OSC_ENDS,
    "\x90": _STRING_ENDS,
    "\x98": _STRING_ENDS,
    "\x9e": _STRING_ENDS,
    "\x9f": _STRING_ENDS,
}
# A CSI sequence of either form up to its final byte, and what one looks like
# before the final byte arrives. Any other byte breaks it (a terminal does the
# same), so a malformed one cannot hold the rest of the stream.
_CSI_ANY = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
_CSI_ANY_PREFIX = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*")
# How much of a stream one unfinished control sequence may hold back. A real
# CSI is a few bytes and a real OSC (title, hyperlink) a few KiB; beyond these
# it is not a sequence a terminal would be sent, so it is dropped and the text
# after it shown, instead of swallowing the rest of the run and growing without
# bound (every ``feed`` searches what is held again).
_CSI_HOLD_MAX = 256
_STRING_HOLD_MAX = 64 * 1024
# What the scanner leaves in the text for the line-break step: the C0 controls it
# does not keep (tab, line feed and carriage return stay) and DEL. ESC and the C1
# controls never reach it, the scanner consumes them.
_C0_DROPPED = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# ``strip_controls`` (text that is sent on, not shown) is the older filter: one
# pass per kind of sequence. String controls go by ``_drop_strings`` rather than
# by one lazy ``.*?`` pattern, which retries every introducer of an unterminated
# run against the whole rest of the text. Both forms of a sequence are removed,
# since a terminal honours the 8-bit ones too.
_OSC = (re.compile(r"\x1b\]"), _END_OSC)
_C1_OSC = (re.compile(r"\x9d"), _END_C1_OSC)
_C1_STRING_CONTROL = (re.compile(r"[\x90\x98\x9e\x9f]"), _END_STRING)
_ESC_STRING_CONTROL = (re.compile(r"\x1b[PX^_]"), _END_STRING)
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_C1_CSI = re.compile(r"\x9b[0-?]*[ -/]*[@-~]")
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# Unterminated forms at the end of a value: from the introducer on there is
# only payload, never visible text, so drop it rather than print the payload.
_OSC_OPEN = re.compile(r"\x1b\].*$", re.DOTALL)
_ESC_STRING_OPEN = re.compile(r"\x1b[PX^_].*$", re.DOTALL)
_C1_OSC_OPEN = re.compile(r"\x9d.*$", re.DOTALL)
_C1_STRING_OPEN = re.compile(r"[\x90\x98\x9e\x9f].*$", re.DOTALL)
_CSI_OPEN = re.compile(r"\x1b\[[0-?]*[ -/]*$")
_C1_CSI_OPEN = re.compile(r"\x9b[0-?]*[ -/]*$")


def _drop_strings(text: str, kind: tuple[re.Pattern[str], re.Pattern[str]]) -> str:
    """Remove every terminated string control; same result as ``introducer.*?end``.

    The first introducer with no terminator after it means no later one has
    one either, so the scan ends there (the open forms drop the rest) and each
    character is looked at once.
    """
    start, end = kind
    kept: list[str] = []
    pos = 0
    while (opened := start.search(text, pos)) is not None:
        closed = end.search(text, opened.end())
        if closed is None:
            break
        kept.append(text[pos : opened.start()])
        pos = closed.end()
    kept.append(text[pos:])
    return "".join(kept)


def strip_controls(value: object) -> str:
    """Strip terminal control sequences only; every printable character is kept.

    For text that is sent on rather than shown (an attached file), where
    dropping a joiner would change what the model reads. This is a separate,
    older filter (one pass per kind of sequence) from the scanner behind
    :func:`plain`: a sequence nested in or cut off by another can leave different
    text behind, so it is not what the display shows.
    """
    text = str(value or "")
    text = _drop_strings(text, _OSC)
    text = _drop_strings(text, _C1_OSC)
    text = _drop_strings(text, _ESC_STRING_CONTROL)
    text = _drop_strings(text, _C1_STRING_CONTROL)
    text = _CSI.sub("", text)
    text = _C1_CSI.sub("", text)
    text = _OSC_OPEN.sub("", text)
    text = _C1_OSC_OPEN.sub("", text)
    text = _ESC_STRING_OPEN.sub("", text)
    text = _C1_STRING_OPEN.sub("", text)
    text = _CSI_OPEN.sub("", text)
    text = _C1_CSI_OPEN.sub("", text)
    return _CONTROLS.sub("", text)


def _string_end(text: str, at: int, start: int, terminators: tuple[str, ...], seen: dict[str, int]) -> int | None:
    r"""Index just past the string control at ``text[at]``, or None if unseen.

    The first terminator after ``start`` ends the string, so every kind is looked
    for, and one that never comes (BEL in a run of strings that ``ESC \`` ends)
    would be looked for to the end of the text by each string in the run:
    quadratic in their number. ``seen`` is the scan's memory of where each kind was
    last found (-1: nowhere after the place it looked from). The scan only moves
    forward, so a terminator is searched for again only once the scan has passed
    where it was found, and the text is read once per terminator in all. A stream
    still searches its whole held text again on every chunk, and ``str.find`` does
    that an order of magnitude faster than one regex over the alternatives, which
    a stream fed a string that never closes, a few characters at a time, would pay for.

    A string control that outgrows ``_STRING_HOLD_MAX`` without a terminator is
    taken to end where the text does: what is held is dropped.
    """
    ends = []
    for end in terminators:
        found = seen.get(end)
        if found is None or 0 <= found < start:
            found = seen[end] = text.find(end, start)
        if found != -1:
            ends.append(found + len(end))
    if ends:
        return min(ends)
    return len(text) if len(text) - at > _STRING_HOLD_MAX else None


def _csi_end(text: str, at: int, *, final: bool) -> int | None:
    """Index just past the CSI sequence at ``text[at]``; None while it can still grow.

    A broken sequence ends at its introducer: the rest is text. One that ESC or a
    C1 control cuts off ends at the cut instead, because the standard terminal
    state machine cancels it there and draws none of its parameters. The hold
    bound is for a stream, which cannot tell a long sequence from one that never
    ends; a text that is ``final`` has nothing more to wait for, so a sequence
    unfinished at its end is dropped whatever its length.
    """
    complete = _CSI_ANY.match(text, at)
    if complete is not None:
        return complete.end()
    prefix = _CSI_ANY_PREFIX.match(text, at)
    stop = prefix.end() if prefix is not None else at + 1
    if stop == len(text):
        return None if final or stop - at <= _CSI_HOLD_MAX else at + 1
    return stop if _INTRODUCER.match(text, stop) else at + 1


def _construct_end(text: str, at: int, seen: dict[str, int], *, final: bool) -> int | None:
    """Index just past the control at ``text[at]`` (ESC or a C1 control); None to hold.

    Holding, rather than guessing, is what keeps the hidden payload of a
    sequence that is still arriving from being shown. ``seen`` is :func:`_string_end`'s.
    """
    char = text[at]
    if char == "\x1b":
        if at + 1 == len(text):
            return None  # a lone ESC: the next character decides what it starts
        follower = text[at + 1]
        ends = _ESC_STRING_ENDS.get(follower)
        if ends is not None:
            return _string_end(text, at, at + 2, ends, seen)
        # ESC [ opens a CSI sequence; any other single-character escape (ESC c,
        # ESC 7, ...) is consumed with its ESC and what follows is text.
        return _csi_end(text, at, final=final) if follower == "[" else at + 1
    ends = _C1_STRING_ENDS.get(char)
    if ends is not None:
        return _string_end(text, at, at + 1, ends, seen)
    # U+009B opens a CSI sequence; any other C1 control is a single character a
    # terminal drops.
    return _csi_end(text, at, final=final) if char == "\x9b" else at + 1


def _scan(text: str, *, final: bool) -> tuple[str, str]:
    """Split ``text`` into what to show and the unfinished tail to hold.

    Controls and whole sequences are removed; the text between them is returned
    as it is (C0 controls and carriage returns still in it). The tail starts at
    the control that cannot be decided yet: a stream holds it for the next chunk,
    :func:`plain` drops it. ``final`` says the text ends here, so no terminator is
    still to come and the stream's hold bounds, which only stop a stream from
    waiting forever, do not apply: an unfinished sequence is payload, however long.
    """
    shown: list[str] = []
    seen: dict[str, int] = {}
    pos = 0
    while True:
        found = _INTRODUCER.search(text, pos)
        if found is None:
            shown.append(text[pos:])
            return "".join(shown), ""
        at = found.start()
        shown.append(text[pos:at])
        end = _construct_end(text, at, seen, final=final)
        if end is None:
            return "".join(shown), text[at:]
        pos = end


def _line_breaks(kept: str) -> str:
    """Turn every carriage return into a line break (a CRLF into one)."""
    return kept.replace("\r\n", "\n").replace("\r", "\n") if "\r" in kept else kept


def plain(value: object) -> str:
    """Display-safe server text: no terminal controls, no hidden characters.

    Hidden characters go first, so a control sequence with one inside it
    (``ESC``, zero-width space, ``[``) is still seen and removed whole. A
    carriage return becomes a line break (as the server's live channel does):
    on a terminal it returns the cursor, so a later part of a line would
    overwrite an earlier one.
    """
    shown, _ = _scan(_HIDDEN.sub("", str(value or "")), final=True)
    return _line_breaks(_C0_DROPPED.sub("", shown))



class TokenSanitizer:
    """Incremental terminal-control filter for streamed token text.

    Whole-string filtering cannot work on a token stream: an escape sequence
    (or its payload) may be split across frames, or interrupted by a non-token
    frame. This keeps the incomplete tail between calls and preserves it across
    other events, so a sequence that resumes later is still removed. It runs the
    scanner :func:`plain` runs, on the held tail plus each new chunk, so any
    split of a text shows what the whole text shows (up to the hold bounds); an
    unterminated tail is dropped at flush time rather than risk printing its
    hidden payload.
    """

    def __init__(self) -> None:
        """Start with nothing held and no carriage return pending."""
        self._pending = ""
        # The last character handed on came from a carriage return, so a line
        # feed that opens the next chunk is the second half of a CRLF.
        self._after_cr = False

    def _show(self, shown: str) -> str:
        # What plain() keeps of the text before it turns CR into a line break.
        # Text that is nothing but controls it deletes (a NUL, a BEL) is
        # invisible, so it must not clear the memory of a CR before it: "\r",
        # "\x00", "\n" in three chunks is one break, as the whole text is.
        kept = _C0_DROPPED.sub("", shown)
        if not kept:
            return ""
        if self._after_cr and kept.startswith("\n"):
            kept = kept[1:]
        self._after_cr = kept.endswith("\r")
        return _line_breaks(kept)

    def feed(self, value: object) -> str:
        """Return the text of ``value`` that is safe to show now.

        Hidden characters go per code point, so any split of a stream filters
        like the whole text.
        """
        shown, self._pending = _scan(self._pending + _HIDDEN.sub("", str(value or "")), final=False)
        return self._show(shown)

    def flush(self) -> str:
        """Drop any held incomplete escape tail; return nothing to print."""
        self._pending = ""
        return ""
