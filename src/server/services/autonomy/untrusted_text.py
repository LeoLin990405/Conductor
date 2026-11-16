"""Untrusted text that cannot spell a prompt block marker.

Prompts wrap untrusted data (issue text, judgment material) in
``<<<BEGIN ... nonce>>>`` / ``<<<END ... nonce>>>`` markers. This module
makes sure the data itself cannot spell such a marker, whatever Unicode
tricks it uses. The per-prompt nonce in the real markers is the second line
of defence.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["sanitize_untrusted_text"]

# Three or more angle brackets in a row. After NFKC the fullwidth and small
# forms are plain ``<`` / ``>``, so this covers them too.
_BRACKET_RUN = re.compile(r"<{3,}|>{3,}")
# Characters dropped because they render as nothing (or as a blank) and could
# sit between brackets: format controls (Cf), combining and enclosing marks
# (Mn, Me) such as U+034F and variation selectors, and the blank fillers below
# (braille blank, Hangul fillers, halfwidth Hangul filler).
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Mn", "Me"})
_INVISIBLE_CHARS = frozenset("⠀ㅤᅟᅠﾠ")


def sanitize_untrusted_text(text: str) -> str:
    """``text`` that cannot spell a ``<<<...>>>`` block marker.

    NFKC folds fullwidth and compatibility forms onto ASCII, invisible format
    characters (category Cf, such as zero-width spaces and joiners) are
    dropped, and every run of three or more angle brackets is broken up
    whatever the case or spacing of the words around it.
    """
    folded = unicodedata.normalize("NFKC", text)
    visible = "".join(
        ch
        for ch in folded
        if unicodedata.category(ch) not in _INVISIBLE_CATEGORIES
        and ch not in _INVISIBLE_CHARS
    )
    return _BRACKET_RUN.sub(lambda m: " ".join(m.group(0)), visible)
