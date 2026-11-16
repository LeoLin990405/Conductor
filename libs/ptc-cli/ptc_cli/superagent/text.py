"""Display text for the TUI.

The scanner lives in :mod:`conductor_cli.terminal_text`. ``text`` stays here
because it escapes Rich markup; every other name is the core scanner.
"""

from __future__ import annotations

import re

from conductor_cli.terminal_text import (
    _C0_DROPPED,
    _C1_CSI,
    _C1_CSI_OPEN,
    _C1_OSC,
    _C1_OSC_ENDS,
    _C1_OSC_OPEN,
    _C1_STRING_CONTROL,
    _C1_STRING_ENDS,
    _C1_STRING_OPEN,
    _CONTROLS,
    _CSI,
    _CSI_ANY,
    _CSI_ANY_PREFIX,
    _CSI_HOLD_MAX,
    _CSI_OPEN,
    _END_C1_OSC,
    _END_OSC,
    _END_STRING,
    _ESC_STRING_CONTROL,
    _ESC_STRING_ENDS,
    _ESC_STRING_OPEN,
    _HIDDEN,
    _HIDDEN_RANGES,
    _INTRODUCER,
    _OSC,
    _OSC_ENDS,
    _OSC_OPEN,
    _STRING_ENDS,
    _STRING_HOLD_MAX,
    TokenSanitizer,
    _construct_end,
    _csi_end,
    _drop_strings,
    _line_breaks,
    _scan,
    _string_end,
    plain,
    strip_controls,
)
from rich.markup import escape

__all__ = [
    "_C0_DROPPED",
    "_C1_CSI",
    "_C1_CSI_OPEN",
    "_C1_OSC",
    "_C1_OSC_ENDS",
    "_C1_OSC_OPEN",
    "_C1_STRING_CONTROL",
    "_C1_STRING_ENDS",
    "_C1_STRING_OPEN",
    "_CONTROLS",
    "_CSI",
    "_CSI_ANY",
    "_CSI_ANY_PREFIX",
    "_CSI_HOLD_MAX",
    "_CSI_OPEN",
    "_END_C1_OSC",
    "_END_OSC",
    "_END_STRING",
    "_ESC_STRING_CONTROL",
    "_ESC_STRING_ENDS",
    "_ESC_STRING_OPEN",
    "_HIDDEN",
    "_HIDDEN_RANGES",
    "_INTRODUCER",
    "_OSC",
    "_OSC_ENDS",
    "_OSC_OPEN",
    "_STRING_ENDS",
    "_STRING_HOLD_MAX",
    "TokenSanitizer",
    "_construct_end",
    "_csi_end",
    "_drop_strings",
    "_line_breaks",
    "_scan",
    "_string_end",
    "plain",
    "strip_controls",
    "strip_protocol_trailers",
    "text",
]

# Server bookkeeping the model copies onto a result. A whole line, not a mention.
_AUTONOMY_TRAILER = re.compile(r"^AUTONOMY_[A-Z_]+:.*$")


def text(value: object, width: int | None = None) -> str:
    """Strip terminal controls, then escape Rich markup, for server text."""
    body = escape(plain(value))
    return body if width is None else body[:width]


def strip_protocol_trailers(value: object) -> str:
    """Drop ``AUTONOMY_*`` trailer lines after terminal controls are removed.

    The line has to be the whole trailer. A sentence that merely mentions the
    token stays, and the control strip runs first so a trailer hidden inside
    an escape sequence cannot come back as text.
    """
    body = plain(value)
    kept = [
        line for line in body.splitlines() if not _AUTONOMY_TRAILER.match(line.strip())
    ]
    rendered = "\n".join(kept)
    if body.endswith("\n") and rendered:
        rendered += "\n"
    return rendered
