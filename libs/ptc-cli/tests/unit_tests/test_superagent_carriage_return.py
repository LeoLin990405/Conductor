"""A carriage return never reaches the terminal as one.

It moves the cursor back, so a later part of a line would overwrite an earlier
one (``tests passed 12/12<CR>tests FAILED 0/12``). Print mode writes straight to
stdout, so every frame type is checked there; the line shell's Rich console and
the TUI's prompt_toolkit draw a carriage return harmlessly.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# tui imports conductor_cli, which lives in the repo src tree; the ptc-cli
# lockfile's conductor-core is a different PyPI project.
_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.client import ActivityStreamEvent  # noqa: E402
from ptc_cli.superagent.text import TokenSanitizer, plain, strip_controls, text  # noqa: E402
from ptc_cli.superagent.tui import run_print_mode  # noqa: E402
from tests.unit_tests.test_superagent_tui_app import FakeClient as TuiClient  # noqa: E402

CR = "tests passed 12/12\rtests FAILED 0/12"


def test_a_carriage_return_becomes_a_line_break_in_every_display_path():
    assert plain(CR) == "tests passed 12/12\ntests FAILED 0/12"
    assert plain("a\r\nb\r\n\r\nc\rd") == "a\nb\n\nc\nd"
    assert text("x\ry") == "x\ny"
    assert plain("tab\tkept\nline") == "tab\tkept\nline"
    # Text that is sent on rather than shown keeps what the file held.
    assert strip_controls("a\r\nb") == "a\r\nb"


@pytest.mark.parametrize("pieces", [["a\r\nb"], ["a\r", "\nb"], ["a", "\r", "", "\n", "b"]])
def test_a_crlf_split_over_chunks_is_one_line_break(pieces):
    sanitizer = TokenSanitizer()
    out = "".join(sanitizer.feed(piece) for piece in pieces) + sanitizer.flush()
    assert out == "a\nb"


def test_a_bare_cr_then_a_line_feed_after_a_sequence_is_still_one_line_break():
    sanitizer = TokenSanitizer()
    out = sanitizer.feed("a\r") + sanitizer.feed("\x1b[0m") + sanitizer.feed("\nb")
    assert out == "a\nb"
    assert plain("a\r\x1b[0m\nb") == "a\nb"


def _frames() -> list[ActivityStreamEvent]:
    def frame(name: str, data: dict[str, Any], event_id: str) -> ActivityStreamEvent:
        return ActivityStreamEvent(event=name, data=data, event_id=event_id)

    return [
        frame("activity", {"event_id": "e", "at": CR, "kind": CR, "summary": CR, "detail": CR}, "1"),
        frame("output", {"kind": "text", "text": CR, "run": "r", "epoch": 1}, "2"),
        frame("output", {"kind": "reasoning", "text": CR, "run": "r", "epoch": 1}, "3"),
        frame("output", {"kind": "tool", "tools": [CR]}, "4"),
        frame("output", {"kind": "error", "text": CR}, "5"),
        frame("output", {"kind": "gap", "text": CR}, "6"),
        frame("output", {"kind": "output_reset", "reason": CR, "epoch": 2}, "7"),
        frame("end", {"status": "completed", "summary": CR, "question": CR}, "8"),
    ]


@pytest.mark.asyncio
async def test_print_mode_writes_no_carriage_return():
    writes: list[str] = []
    code = await run_print_mode(
        server_url="http://test",
        user_id="u1",
        prompt="x",
        client=TuiClient(stream=_frames()),
        write=writes.append,
    )
    out = "".join(writes)
    assert code == 0
    assert "\r" not in out
    assert "tests passed 12/12\ntests FAILED 0/12" in out


@pytest.mark.parametrize("pieces", [["\r", "\x00", "\n"], ["a\r", "\x07", "\x00", "\nb"]])
def test_a_chunk_of_only_deleted_controls_does_not_forget_a_carriage_return(pieces):
    whole = "".join(pieces)
    sanitizer = TokenSanitizer()
    out = "".join(sanitizer.feed(piece) for piece in pieces) + sanitizer.flush()
    assert out == plain(whole)
    assert "\n\n" not in out


def test_every_split_of_a_cr_lf_text_filters_like_the_whole():
    """CR, LF, a NUL, a BEL and a colour sequence in every order and split."""
    import itertools

    alphabet = ["\r", "\n", "\x00", "\x07", "\x1b[0m", "a"]
    for length in range(1, 6):
        for combo in itertools.product(alphabet, repeat=length):
            whole = "".join(combo)
            expected = plain(whole)
            for cut in range(len(combo) + 1):
                sanitizer = TokenSanitizer()
                first = sanitizer.feed("".join(combo[:cut]))
                second = sanitizer.feed("".join(combo[cut:]))
                assert first + second + sanitizer.flush() == expected, (combo, cut)
