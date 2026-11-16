"""String controls (OSC, DCS, SOS, PM, APC) are removed in linear time.

An unterminated introducer used to be retried against the whole rest of the
text by a lazy pattern, so a few tens of kilobytes of ``ESC ]`` hung the client
for seconds. The removal is a scan now; it must give the old pattern's result.
"""

from __future__ import annotations

import random
import re
import time

import pytest

from ptc_cli.superagent.text import TokenSanitizer, plain, strip_controls

# The patterns the scan replaced, kept here as the specification.
_OLD_STRINGS = [
    re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL),
    re.compile(r"\x9d.*?(?:\x07|\x9c|\x1b\\)", re.DOTALL),
    re.compile(r"\x1b[PX^_].*?(?:\x9c|\x1b\\)", re.DOTALL),
    re.compile(r"[\x90\x98\x9e\x9f].*?(?:\x9c|\x1b\\)", re.DOTALL),
]
_REST = [
    re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]"),
    re.compile(r"\x9b[0-?]*[ -/]*[@-~]"),
    re.compile(r"\x1b\].*$", re.DOTALL),
    re.compile(r"\x9d.*$", re.DOTALL),
    re.compile(r"\x1b[PX^_].*$", re.DOTALL),
    re.compile(r"[\x90\x98\x9e\x9f].*$", re.DOTALL),
    re.compile(r"\x1b\[[0-?]*[ -/]*$"),
    re.compile(r"\x9b[0-?]*[ -/]*$"),
]
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _specification(value: str) -> str:
    for pattern in _OLD_STRINGS + _REST:
        value = pattern.sub("", value)
    return _CONTROLS.sub("", value)


ALPHABET = list("\x1b\x9b\x9c\x9d\x90\x98\x9e\x9f\x07]P[X^_\\a0 ;m\n")


def test_the_scan_gives_the_result_of_the_patterns_it_replaced():
    rng = random.Random(20261105)
    for _ in range(20_000):
        value = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 24)))
        assert strip_controls(value) == _specification(value), repr(value)


@pytest.mark.parametrize(
    "unit",
    ["\x1b]", "\x9d", "\x1bP", "\x1bX", "\x1b^", "\x1b_", "\x90", "\x98", "\x1b[", "\x1bc"],
    ids=["osc", "c1-osc", "dcs", "sos", "pm", "apc", "c1-dcs", "c1-sos", "csi", "esc-c"],
)
def test_unterminated_introducers_are_removed_in_linear_time(unit):
    hostile = unit * 80_000
    started = time.perf_counter()
    plain(hostile)
    plain("\x07" + hostile)  # a terminator in front of them does not help the scan
    sanitizer = TokenSanitizer()
    sanitizer.feed(hostile)
    sanitizer.flush()
    assert time.perf_counter() - started < 2.0


def test_terminated_strings_are_still_removed_among_unterminated_ones():
    assert plain("a\x1b]0;title\x07b") == "ab"
    assert plain("a\x1bPq\x9cb\x90x\x1b\\c") == "abc"
    assert plain("a\x9d52;c;QQ==\x07b") == "ab"
    # An unterminated one drops the rest, which is payload as far as a terminal knows.
    assert plain("a\x1b]0;title") == "a"


def test_an_unterminated_string_control_cannot_swallow_the_rest_of_a_run():
    sanitizer = TokenSanitizer()
    assert sanitizer.feed("before \x1b]52;c;") == "before "
    started = time.perf_counter()
    shown = ""
    for _ in range(4000):
        shown += sanitizer.feed("x" * 100)
    assert time.perf_counter() - started < 1.5
    # What the sequence held back is dropped once it outgrows the bound and the
    # text after it is shown again; nothing is held without limit.
    assert 400_000 - 64 * 1024 - 100 <= len(shown) <= 400_000 - 64 * 1024
    assert sanitizer._pending == ""


@pytest.mark.parametrize("introducer", ["\x1b]", "\x1bP", "\x9d", "\x90"])
def test_a_terminator_that_arrives_in_time_still_ends_the_sequence(introducer):
    sanitizer = TokenSanitizer()
    out = sanitizer.feed(f"a{introducer}payload")
    out += sanitizer.feed(" more")
    out += sanitizer.feed("\x07" if introducer in ("\x1b]", "\x9d") else "\x9c")
    out += sanitizer.feed("b")
    assert out == "ab"


def test_a_malformed_csi_ends_at_its_introducer_like_the_whole_text_does():
    sanitizer = TokenSanitizer()
    assert sanitizer.feed("a\x1b[3") == "a"  # may still grow into a sequence
    assert sanitizer.feed("\nrest\n") == "[3\nrest\n"
    assert plain("a\x1b[3\nrest\n") == "a[3\nrest\n"
    held = TokenSanitizer()
    assert held.feed("\x1b[" + "1" * 400) != ""  # not a CSI a terminal would take
    assert held._pending == ""


def test_a_csi_split_over_chunks_is_still_removed():
    sanitizer = TokenSanitizer()
    out = sanitizer.feed("a\x1b[3") + sanitizer.feed("1;1") + sanitizer.feed("mb\x9b3") + sanitizer.feed("1mc")
    assert out == "abc"


def test_protocol_trailers_are_whole_lines_and_controls_still_go():
    from ptc_cli.superagent.text import strip_protocol_trailers

    assert strip_protocol_trailers("hello\nAUTONOMY_STATE: secret\nthere") == "hello\nthere"
    assert strip_protocol_trailers("keep AUTONOMY_STATE in a sentence") == (
        "keep AUTONOMY_STATE in a sentence"
    )
    assert strip_protocol_trailers("a\x1b[31m\nAUTONOMY_QUESTION: q\nb") == "a\nb"
    assert "\x1b" not in strip_protocol_trailers("a\x1b[2Jsafe")
