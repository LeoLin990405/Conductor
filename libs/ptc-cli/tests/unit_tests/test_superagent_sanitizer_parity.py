r"""A text and every split of it print the same, and the rules are the documented ones.

``plain()`` (a whole text) and ``TokenSanitizer`` (a stream) used to be two
implementations that were never run against each other, and they disagreed:
``plain("A\x1b]title\x9cB")`` was ``"A"`` while the stream gave ``"AB"``. They
now share one scanner, and this differential test keeps it that way: every short
text over the characters that matter, then seeded random longer ones, is cut
into one, two and three chunks in every place, and each cut must print what the
whole text prints. A third, deliberately naive reader of the documented rules
checks that what they agree on is what ``text.py`` says it does.

The hold bounds (``_CSI_HOLD_MAX``, ``_STRING_HOLD_MAX``) are the one place
where chunking may change what is shown; the texts here are far shorter, and
``test_the_hold_bounds_are_where_chunking_may_show`` pins that exception.
"""

from __future__ import annotations

import itertools
import random
import time

import pytest

from ptc_cli.superagent import text as text_module
from ptc_cli.superagent.text import TokenSanitizer, plain

ESC = "\x1b"
# The characters whose order matters: ESC and the introducers and final bytes that
# follow it, BEL and the C1 controls (CSI, ST, and the string introducers), the
# line breaks, a NUL, a zero-width character, a letter and a digit.
ALPHABET = [
    ESC, "[", "]", "P", "X", "^", "_", "\\", "\x07",
    "\x90", "\x98", "\x9b", "\x9c", "\x9d", "\x9e", "\x9f",
    "\r", "\n", "\x00", "\u200b", "a", "0",
]  # fmt: skip
# The same with fewer characters (no variation of one that is already there), so
# that longer texts can be exhaustive too.
STRUCTURAL = [ESC, "[", "]", "P", "\\", "\x07", "\x9b", "\x9c", "\x9d", "\x90", "\n", "a"]
CORE = [ESC, "[", "]", "\x9c", "\x07", "a"]
# Every text of five characters over CORE and of six over five of them (BEL left out).
EXHAUSTIVE_LONGER = ((CORE, 5), (CORE[:4] + CORE[5:], 6))
# The C0 controls the display drops (tab, line feed and carriage return stay) and DEL.
DROPPED_CONTROLS = {chr(code) for code in range(0x20)} - {"\t", "\n", "\r"} | {"\x7f"}


def streamed(chunks: list[str]) -> str:
    sanitizer = TokenSanitizer()
    return "".join(sanitizer.feed(chunk) for chunk in chunks) + sanitizer.flush()


def splits(value: str):
    """The text whole, and cut in two and in three at every place (empty chunks too)."""
    yield [value]
    for first in range(len(value) + 1):
        yield [value[:first], value[first:]]
        for second in range(first, len(value) + 1):
            yield [value[:first], value[first:second], value[second:]]


def assert_every_split_agrees(value: str) -> None:
    expected = plain(value)
    for chunks in splits(value):
        shown = streamed(chunks)
        assert shown == expected, (value, chunks, expected, shown)


def reference(value: str) -> str:
    """The rules in the module docstring, read as plainly as possible.

    Slow and shares nothing with the scanner but the hidden-character table. The
    hold bounds are not modelled; the texts it is run on are shorter.
    """
    body = text_module._HIDDEN.sub("", value)
    ends = {
        "]": ("\x07", ESC + "\\"),
        "P": ("\x9c", ESC + "\\"),
        "X": ("\x9c", ESC + "\\"),
        "^": ("\x9c", ESC + "\\"),
        "_": ("\x9c", ESC + "\\"),
        "\x9d": ("\x07", "\x9c", ESC + "\\"),
        "\x90": ("\x9c", ESC + "\\"),
        "\x98": ("\x9c", ESC + "\\"),
        "\x9e": ("\x9c", ESC + "\\"),
        "\x9f": ("\x9c", ESC + "\\"),
    }
    kept: list[str] = []
    at = 0
    while at < len(body):
        char = body[at]
        if char not in {ESC, *map(chr, range(0x80, 0xA0))}:
            kept.append(char)
            at += 1
            continue
        key = char
        start = at + 1
        if char == ESC:
            if at + 1 == len(body):
                break  # a lone ESC at the end of the text
            key = body[at + 1]
            start = at + 2
        if key in ends:
            found = [body.index(end, start) + len(end) for end in ends[key] if body.find(end, start) != -1]
            if not found:
                break  # an open string control: the rest is payload
            at = min(found)
        elif (key == "[" and char == ESC) or char == "\x9b":
            cursor = start
            while cursor < len(body) and "0" <= body[cursor] <= "?":
                cursor += 1
            while cursor < len(body) and " " <= body[cursor] <= "/":
                cursor += 1
            if cursor == len(body):
                break  # an unfinished CSI sequence
            if "@" <= body[cursor] <= "~":
                at = cursor + 1
            elif body[cursor] == ESC or "\x80" <= body[cursor] <= "\x9f":
                at = cursor  # cut off by another control: none of it is drawn
            else:
                at += 1  # broken: only the introducer goes, the rest is text
        else:
            at += 1  # a lone ESC (and the character after it stays), or a C1 control
    shown = "".join(char for char in kept if char not in DROPPED_CONTROLS)
    return shown.replace("\r\n", "\n").replace("\r", "\n")


# ---------------------------------------------------------------------------
# the differential: whole text against every split, and against the reference
# ---------------------------------------------------------------------------


def test_every_text_of_up_to_three_characters_and_every_split_agree():
    for length in range(4):
        for combo in itertools.product(ALPHABET, repeat=length):
            assert_every_split_agrees("".join(combo))


def test_every_text_of_four_structural_characters_and_every_split_agree():
    for combo in itertools.product(STRUCTURAL, repeat=4):
        assert_every_split_agrees("".join(combo))


def test_every_longer_text_of_a_few_core_characters_and_every_split_agree():
    for alphabet, length in EXHAUSTIVE_LONGER:
        for combo in itertools.product(alphabet, repeat=length):
            assert_every_split_agrees("".join(combo))


def test_seeded_random_longer_texts_and_splits_agree():
    rng = random.Random(20261105)
    for _ in range(12_000):
        value = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(5, 24)))
        expected = plain(value)
        # One character at a time, then three random cuts.
        assert streamed(list(value)) == expected, value
        for _ in range(3):
            cuts = sorted(rng.randint(0, len(value)) for _ in range(rng.randint(1, 4)))
            pieces = [value[a:b] for a, b in zip([0, *cuts], [*cuts, len(value)], strict=True)]
            assert streamed(pieces) == expected, (value, pieces)


def sample_texts():
    """The texts the differential test cuts (all of them, exhaustively), and seeded random ones."""
    rng = random.Random(7)
    for length in range(4):
        for combo in itertools.product(ALPHABET, repeat=length):
            yield "".join(combo)
    for combo in itertools.product(STRUCTURAL, repeat=4):
        yield "".join(combo)
    for alphabet, length in EXHAUSTIVE_LONGER:
        for combo in itertools.product(alphabet, repeat=length):
            yield "".join(combo)
    for _ in range(12_000):
        yield "".join(rng.choice(ALPHABET) for _ in range(rng.randint(5, 24)))


def test_the_documented_rules_are_what_the_scanner_does():
    for value in sample_texts():
        assert plain(value) == reference(value), value


def test_no_control_or_hidden_character_is_ever_printed():
    forbidden = {ESC, "\r", "\u200b", *map(chr, range(0x7F, 0xA0)), *DROPPED_CONTROLS}
    for value in sample_texts():
        leaked = forbidden.intersection(plain(value))
        assert not leaked, (value, sorted(map(hex, map(ord, leaked))))


# ---------------------------------------------------------------------------
# the choices the differential forced, each in both directions
# ---------------------------------------------------------------------------


def both(value: str) -> str:
    """What the whole text and the stream print, which must be the same."""
    shown = plain(value)
    assert streamed([value]) == shown
    assert streamed(list(value)) == shown
    return shown


def test_an_osc_opened_by_esc_is_not_ended_by_u009c():
    # The case that started this: a terminal that reads ESC ] strictly has not seen
    # the end of the string, so what follows stays hidden until BEL or ESC \.
    assert both("A\x1b]title\x9cB") == "A"
    assert both("A\x1b]title\x9cB\x07C") == "AC"
    assert both("A\x1b]title\x9cB\x1b\\C") == "AC"
    # Every other terminator still ends the string it ends.
    assert both("a\x1b]t\x07b") == "ab"
    assert both("a\x1b]t\x1b\\b") == "ab"
    assert both("a\x9dt\x07b") == "ab"
    assert both("a\x9dt\x9cb") == "ab"
    assert both("a\x9dt\x1b\\b") == "ab"
    for introducer in ("\x1bP", "\x1bX", "\x1b^", "\x1b_", "\x90", "\x98", "\x9e", "\x9f"):
        assert both(f"a{introducer}t\x9cb") == "ab"
        assert both(f"a{introducer}t\x1b\\b") == "ab"
        # BEL is data inside everything but an OSC.
        assert both(f"a{introducer}t\x07b\x9cc") == "ac"


def test_a_csi_cut_off_by_another_control_is_dropped_as_far_as_the_cut():
    # A terminal cancels it there and draws none of its parameters.
    assert both("a\x1b[31\x1b]52;c;x\x07b") == "ab"
    assert both("a\x1b[3\x9d0;t\x07b") == "ab"
    assert both("a\x9b0\x90payload") == "a"
    assert both("a\x1b[\x90payload") == "a"
    assert both("x\x1b[3\x1b[0my") == "xy"
    assert both("x\x9b3\x9b0my") == "xy"
    assert both("x\x1b[3\x85y") == "xy"
    # Broken by an ordinary character it loses only its introducer, and the rest is
    # text, wherever the break is: at the end of the text too.
    assert both("a\x1b[3\nrest\n") == "a[3\nrest\n"
    assert both("a\x1b[3\n") == "a[3\n"
    assert both("a\x9b0\n") == "a0\n"
    assert both("a\x1b[3x") == "a"  # x is a final byte: a complete sequence
    assert both("a\x1b[3\u00e9b") == "a[3\u00e9b"


@pytest.mark.parametrize("introducer", ["\x1b[", "\x9b"], ids=["7-bit", "8-bit"])
def test_a_csi_sequence_is_parameters_intermediates_and_one_final_byte(introducer):
    # Every byte from @ to ~ is a final byte (the alphabet above has few of them).
    for code in range(0x40, 0x7F):
        assert both(f"a{introducer}1;2{chr(code)}b") == "ab", hex(code)
    for body in ("31m", "38:2::1:2:3m", "?25l", ">0c", "<1u", "1 q", "0!p", "2$|", "m"):
        assert both(f"a{introducer}{body}b") == "ab", body
    # Parameters never follow an intermediate, and DEL and non-ASCII break it.
    rest = introducer[1:]  # what stays of an introducer that only loses its ESC
    assert both(f"a{introducer} 1mb") == f"a{rest} 1mb"
    assert both(f"a{introducer}1\x7fb") == f"a{rest}1b"
    assert both(f"a{introducer}1\u00e9b") == f"a{rest}1\u00e9b"


STRING_CONTROLS = {
    "OSC": ("\x1b]", ("\x07", "\x1b\\")),
    "C1 OSC": ("\x9d", ("\x07", "\x9c", "\x1b\\")),
    "DCS": ("\x1bP", ("\x9c", "\x1b\\")),
    "SOS": ("\x1bX", ("\x9c", "\x1b\\")),
    "PM": ("\x1b^", ("\x9c", "\x1b\\")),
    "APC": ("\x1b_", ("\x9c", "\x1b\\")),
    "C1 DCS": ("\x90", ("\x9c", "\x1b\\")),
    "C1 SOS": ("\x98", ("\x9c", "\x1b\\")),
    "C1 PM": ("\x9e", ("\x9c", "\x1b\\")),
    "C1 APC": ("\x9f", ("\x9c", "\x1b\\")),
}  # fmt: skip


@pytest.mark.parametrize("name", STRING_CONTROLS)
def test_a_string_controls_payload_is_never_printed_however_the_text_is_cut(name):
    introducer, terminators = STRING_CONTROLS[name]
    for terminator in terminators:
        value = f"a{introducer}SECRET;{terminator}b"
        assert plain(value) == "ab"
        for chunks in splits(value):
            assert streamed(chunks) == "ab", (name, terminator, chunks)
    # With no terminator everything after the introducer is payload, wherever the
    # text is cut, and the flush drops what was held.
    unfinished = f"a{introducer}SECRET"
    assert plain(unfinished) == "a"
    for chunks in splits(unfinished):
        assert streamed(chunks) == "a", (name, chunks)


@pytest.mark.parametrize(
    "tail",
    [
        "\x1b]52;c;PAYLOAD",
        "\x1bPq;PAYLOAD",
        "\x1bXPAYLOAD",
        "\x1b^PAYLOAD",
        "\x1b_PAYLOAD",
        "\x9d52;c;PAYLOAD",
        "\x90q;PAYLOAD",
        "\x98PAYLOAD",
        "\x9ePAYLOAD",
        "\x9fPAYLOAD",
        "\x1b]52;c;PAYLOAD\x9c",  # a 7-bit OSC is not ended by U+009C
        "\x1bPq;PAYLOAD\x07",  # nor a DCS by BEL
        "\x1b[",
        "\x1b[31",
        "\x1b[31;",
        "\x9b",
        "\x9b3;1",
        "\x1b",
    ],
)
def test_nothing_unfinished_is_printed_at_the_end_of_the_text_or_the_stream(tail):
    assert plain("a" + tail) == "a"
    sanitizer = TokenSanitizer()
    assert sanitizer.feed("a" + tail) == "a"
    assert sanitizer.flush() == ""
    # Cut anywhere, and flushed, it is the same: the held payload is dropped.
    for cut in range(len(tail) + 1):
        pieces = TokenSanitizer()
        shown = pieces.feed("a" + tail[:cut]) + pieces.feed(tail[cut:]) + pieces.flush()
        assert shown == "a", (tail, cut)
    # After the flush the sanitizer starts clean; nothing held leaks into what follows.
    assert sanitizer.feed("b") == "b"


def test_the_hold_bounds_are_where_chunking_may_show():
    """Below the bounds any split agrees; beyond them a stream releases what it holds.

    A whole text with no terminator has none to wait for, so the rest of it is
    payload; a stream cannot know that, and gives up on a string control after
    64 KiB so that one that never closes does not swallow a run (the existing
    string tests pin the stream's side; this pins that the whole text is stricter).
    """
    value = "\x1b]" + "x" * (64 * 1024 + 5000) + "tail"
    assert plain(value) == ""
    assert streamed([value]) == ""
    sanitizer = TokenSanitizer()
    shown = "".join(sanitizer.feed(value[i : i + 1000]) for i in range(0, len(value), 1000))
    assert shown.endswith("tail")
    assert 0 < len(shown) <= 5000


# The numbers below are the documented bounds, written out on purpose: a change to
# either constant has to change them here and in docs/ports/cli-tui.md.
CSI_HOLD = 256  # characters of an unfinished CSI sequence a stream holds, introducer included
STRING_HOLD = 64 * 1024  # characters of an unterminated string control it holds


@pytest.mark.parametrize("introducer", ["\x1b[", "\x9b"], ids=["7-bit", "8-bit"])
def test_a_whole_text_drops_an_unfinished_csi_sequence_however_long(introducer):
    """The hold bound is for a stream, which cannot know the text ends; a text does.

    Past the bound the stream shows what it released (its introducer is dropped),
    but a text that ends inside a CSI sequence has only parameters left, and must
    never print them, however many there are.
    """
    for parameters in (0, 1, CSI_HOLD - 3, CSI_HOLD - 2, CSI_HOLD - 1, CSI_HOLD, CSI_HOLD + 1, 5_000, 1_000_000):
        assert plain("a" + introducer + "1" * parameters) == "a", parameters
    assert plain("a" + introducer + "1;" * 300 + " !") == "a"
    # It is the end of the text that drops it: a final byte completes it however long it is,
    # and a character that breaks it ends it there, so the rest is text.
    assert plain("a" + introducer + "1" * 5_000 + "mb") == "ab"
    assert plain("a" + introducer + "1" * 5_000 + "\u00e9b") == f"a{introducer[1:]}" + "1" * 5_000 + "\u00e9b"


@pytest.mark.parametrize(("introducer", "length"), [("\x1b[", 2), ("\x9b", 1)], ids=["7-bit", "8-bit"])
def test_a_stream_holds_an_unfinished_csi_sequence_up_to_256_characters(introducer, length):
    # Exactly CSI_HOLD characters, introducer included, are still held: the final byte
    # that comes next completes the sequence and nothing of it is shown.
    held = TokenSanitizer()
    assert held.feed("a" + introducer + "1" * (CSI_HOLD - length)) == "a"
    assert held.feed("m") == ""
    # One character more and it is released: the introducer goes, the rest is text.
    released = TokenSanitizer()
    assert released.feed("a" + introducer + "1" * (CSI_HOLD - length + 1)) == ("a" + introducer[1:] + "1" * (CSI_HOLD - length + 1))
    assert released.feed("m") == "m"


@pytest.mark.parametrize("name", STRING_CONTROLS)
def test_a_stream_holds_an_unterminated_string_control_up_to_64_kib(name):
    introducer = STRING_CONTROLS[name][0]
    # Exactly STRING_HOLD characters, introducer included, are still held: what comes
    # next is payload until its terminator, and only what follows that is shown.
    held = TokenSanitizer()
    assert held.feed("a" + introducer + "x" * (STRING_HOLD - len(introducer))) == "a"
    assert held.feed("tail\x1b\\z") == "z"
    # One character more and the held text is dropped and the stream stops waiting, so
    # the next text is shown (an ESC \ outside a string loses its ESC and keeps its backslash).
    released = TokenSanitizer()
    assert released.feed("a" + introducer + "x" * (STRING_HOLD - len(introducer) + 1)) == "a"
    assert released.feed("tail\x1b\\z") == "tail\\z"


@pytest.mark.parametrize("name", STRING_CONTROLS)
def test_a_whole_text_drops_a_string_control_at_any_distance_from_its_terminator(name):
    introducer, terminators = STRING_CONTROLS[name]
    for size in (STRING_HOLD - 1, STRING_HOLD, STRING_HOLD + 1, 3 * STRING_HOLD):
        assert plain("a" + introducer + "x" * size) == "a", size
        assert plain("a" + introducer + "x" * size + terminators[0] + "b") == "ab", size


def test_a_string_that_never_closes_costs_a_stream_no_more_than_searching_it_does():
    """Every chunk searches the whole held text again, so the search has to be cheap.

    A server can send an unterminated string control a character at a time and the
    client then does 64 Ki searches of up to 64 KiB. Done with a regex over the
    terminators that was about thirteen times the cost of ``str.find`` for each of
    them; timed against that baseline in the same process, so that a slow machine
    slows both, the stream stays within a small multiple of it (about 1.2 measured).
    """
    chunks = 20_000

    def drip() -> float:
        sanitizer = TokenSanitizer()
        sanitizer.feed("\x1b]0;")
        start = time.perf_counter()
        for _ in range(chunks):
            sanitizer.feed("z")
        return time.perf_counter() - start

    def baseline() -> float:
        held = "\x1b]0;"
        start = time.perf_counter()
        for _ in range(chunks):
            held += "z"
            held.find("\x07", 2)
            held.find("\x1b\\", 2)
        return time.perf_counter() - start

    assert min(drip() for _ in range(3)) < 4 * min(baseline() for _ in range(3))


class Counting(str):  # noqa: SLOT000 - a str cannot have non-empty __slots__
    """A text that counts the characters its ``find`` calls look at.

    ``str.find`` stops at the first match, so the count is the work it did, the
    same on every machine; a timing cannot say that and this can.
    """

    examined = 0

    def find(self, sub, start=0, end=None):
        stop = len(self) if end is None else min(end, len(self))
        found = super().find(sub, start, end)
        self.examined += max((found + len(sub) if found != -1 else stop) - start, 0)
        return found


REPEATED = 1_000


@pytest.mark.parametrize("name", STRING_CONTROLS)
def test_repeated_terminated_string_controls_are_searched_forward_only(name):
    r"""Looking for a terminator never reads the same text again, whatever the others do.

    The first terminator after an introducer ends the string, so every kind is looked
    for; one that never occurs, or lies far off, used to be searched for to the end of
    the text by every string control before it, which made ``plain("\x1b]x\x07" * n)``
    quadratic. A kind may be read once over the whole text; the budget allows twice
    that, for the character a two-character terminator's searches can share.
    """
    introducer, terminators = STRING_CONTROLS[name]
    budget = 2 * len(terminators)
    for ended_by in terminators:
        unit = f"{introducer}x{ended_by}"
        # Another terminator is either absent or comes once, after every string (as far off
        # as it can be); alone, it prints what a terminator prints outside a string
        # (ESC \ loses its ESC and keeps the backslash, the others print nothing).
        other = next(end for end in terminators if end != ended_by)
        for value, shown in (
            (unit * REPEATED, ""),
            ("a" + (unit + "b") * REPEATED, "a" + "b" * REPEATED),
            (unit * REPEATED + other, other[1:]),
        ):
            counted = Counting(value)
            _, held = text_module._scan(counted, final=True)
            examined, size = counted.examined, len(value)
            assert held == "", (name, ended_by)
            assert examined <= budget * size, (name, ended_by, examined, size)
            assert plain(value) == shown
            assert streamed([value]) == shown


def test_repeated_terminated_strings_cost_plain_and_a_stream_time_linear_in_the_text():
    """The same through the public entry points: four times the text, about four times the time.

    A quadratic scan takes sixteen times as long (the 160 KB text of the review that
    found it took 3.9 s against 0.01 s). Best of three, a generous bound, so that a
    busy machine does not fail it; the work count above is the exact check.
    """

    def best(run, value) -> float:
        times = []
        for _ in range(3):
            start = time.perf_counter()
            run(value)
            times.append(time.perf_counter() - start)
        return min(times)

    for unit in ("\x1b]x\x07", "\x1b]x\x1b\\", "\x1bPx\x9c", "\x9dx\x07", "\x9fx\x9c"):
        for run in (plain, lambda value: streamed([value])):
            small = best(run, unit * 4_000)
            large = best(run, unit * 16_000)
            assert large < 8 * small, (unit, small, large)
