"""Fold reviewer text before the security/scope matchers run (N58, N51).

A blocker is matched against regexes that decide whether a finding fails
closed (a ``security:`` / ``scope:`` tag parks the line for adjudication,
N58/N41), or is only a format problem the N51 repro rule may downgrade.
Unicode look-alikes defeat those regexes while reading exactly like the tag:
a full-width ``ｓｅｃｕｒｉｔｙ:``, a zero-width space inside ``injection``, or a
Cyrillic ``і`` in ``іnjection`` are the same word to a reader and a different
string to ``re``.

:func:`fold` normalizes in this order, before any tag or keyword regex runs:

1. NFKD, so a compatibility form and a letter that carries a combining mark
   both decompose;
2. the characters that render as nothing (zero-width space and friends, soft
   hyphen, BOM) and the nonspacing marks that sit on a Latin letter are
   dropped (``se\u0301curity`` is ``security``, ``s\u0335ecurity`` too);
3. NFKC, so what is left has one canonical composed form;
4. the Cyrillic, Greek and Armenian letters that read as Latin ones, plus the
   dotless ``ı`` and long ``ſ`` that Unicode case folding already equates with
   ``i`` / ``s`` but ``re.IGNORECASE`` then forgets in the captured text.

Folding is for matching only. The blocker a reviewer wrote is what the
machine records and shows, so nothing here rewrites a blocker.

The tag regexes live here too, because every caller must match them on the
folded text: :func:`conflict_tag` routes the N41 conflict (``machine.py`` is
the thin hook), :func:`security_tag` / :func:`scope_tag` are the N51
exemption, and both read the same compiled pattern so the two cannot
disagree about what a tag is.

Routing is tag-only. A finding that names a security flaw without the tag is
*exempt* from the N51 repro downgrade (:func:`mentions_security`), but it is
not an adjudicable conflict: the reviewer never said it was one, and words
such as "token" or "secret" are common enough in this code base that routing
on them parks ordinary fix rounds for a human (R2 review item 1).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

__all__ = [
    "SCOPE_TAG",
    "SECURITY_KEYWORDS",
    "SECURITY_TAG",
    "conflict_of",
    "conflict_tag",
    "fold",
    "mentions_security",
    "scope_tag",
    "security_tag",
]

#: Code points that render as nothing. A zero-width space inside a word makes
#: it a different string for ``re``; a soft hyphen or a BOM does the same.
#: NBSP is deliberately absent: NFKC turns it into a plain space, which is a
#: word boundary the matchers can see.
_INVISIBLE = re.compile(
    "[\u00ad\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f"
    "\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f"
    "\ufe00-\ufe0f\ufeff\uffa0\U000e0000-\U000e007f]"
)

#: Letters from another script that read as Latin ones. Lower case covers a
#: word such as ``іnjection``; upper case covers a tag or a keyword written in
#: capitals. Only look-alikes are listed, so a real Cyrillic word stays as it
#: is for every other matcher that reads the same folded text. The dotless
#: ``ı``, the long ``ſ``, the Armenian ``օ`` and the Cyrillic ``п`` are here
#: because a reader sees ``i``, ``s``, ``o`` and ``n``.
_CONFUSABLES = str.maketrans(
    {
        # Cyrillic lower case: а е о р с у х і ј ѕ һ
        "\u0430": "a",
        "\u0435": "e",
        "\u043e": "o",
        "\u0440": "p",
        "\u0441": "c",
        "\u0443": "y",
        "\u0445": "x",
        "\u0456": "i",
        "\u0458": "j",
        "\u0455": "s",
        "\u04bb": "h",
        "\u043f": "n",
        # Cyrillic upper case: А В Е К М Н О Р С Т У Х І Ј Ѕ
        "\u0410": "A",
        "\u0412": "B",
        "\u0415": "E",
        "\u041a": "K",
        "\u041c": "M",
        "\u041d": "H",
        "\u041e": "O",
        "\u0420": "P",
        "\u0421": "C",
        "\u0422": "T",
        "\u0423": "Y",
        "\u0425": "X",
        "\u0406": "I",
        "\u0408": "J",
        "\u0405": "S",
        # Greek upper case: Α Β Ε Ζ Η Ι Κ Μ Ν Ο Ρ Τ Υ Χ
        "\u0391": "A",
        "\u0392": "B",
        "\u0395": "E",
        "\u0396": "Z",
        "\u0397": "H",
        "\u0399": "I",
        "\u039a": "K",
        "\u039c": "M",
        "\u039d": "N",
        "\u039f": "O",
        "\u03a1": "P",
        "\u03a4": "T",
        "\u03a5": "Y",
        "\u03a7": "X",
        # Greek lower case: α ε ι κ ν ο ρ τ χ
        "\u03b1": "a",
        "\u03b5": "e",
        "\u03b9": "i",
        "\u03ba": "k",
        "\u03bd": "v",
        "\u03bf": "o",
        "\u03c1": "p",
        "\u03c4": "t",
        "\u03c7": "x",
        # Look-alikes that are not another script's letter: dotless i and long
        # s (case folding already equates them with i/s, which is why ``re.I``
        # matched the tag and then kept the look-alike in the captured text)
        # and the Armenian small o.
        "\u0131": "i",
        "\u017f": "s",
        "\u0585": "o",
    }
)

#: Whitespace a reviewer puts inside a Chinese phrase (``注 入``) is not a word
#: boundary in Chinese. NFKC turns a full-width space into a plain one first.
_CJK = r"[\u3400-\u4dbf\u4e00-\u9fff]"
_CJK_GAP = re.compile(rf"(?<={_CJK})\s+(?={_CJK})")


def _is_latin(char: str) -> bool:
    return unicodedata.name(char, "").startswith("LATIN")


def _strip_latin_marks(text: str) -> str:
    """Drop the nonspacing marks that sit on a Latin letter.

    Only marks whose base is a Latin letter are dropped: ``é`` and ``s̵`` are
    the same word to a reader as ``e`` / ``s``, but a combining mark that
    belongs to another script is part of that script's word and stays.
    """
    kept: list[str] = []
    for char in text:
        if (
            unicodedata.category(char) == "Mn"
            and kept
            and _is_latin(kept[-1])
        ):
            continue
        kept.append(char)
    return "".join(kept)


def fold(text: object) -> str:
    """NFKD, drop invisibles and Latin marks, NFKC, fold look-alikes."""
    raw = str(text or "")
    if not raw:
        return ""
    # NFKD first: a compatibility character and a letter+mark pair both
    # decompose, so the next steps see plain characters.
    folded = unicodedata.normalize("NFKD", raw)
    folded = _INVISIBLE.sub("", folded)
    folded = _strip_latin_marks(folded)
    folded = _CJK_GAP.sub("", folded)
    # NFKC recomposes what is left, then the look-alike map is applied to the
    # composed text (it maps one code point to one Latin letter).
    return unicodedata.normalize("NFKC", folded).translate(_CONFUSABLES)


#: A finding that names one of these flaws is security even without the
#: ``security:`` tag (N58). English words match on word boundaries; the
#: Chinese forms have no word boundaries to rely on and match literally.
SECURITY_KEYWORDS = re.compile(
    r"\b(?:injection|injections|"
    r"auth(?:entication|orization)?[-\s]+bypass|"
    r"bypass(?:es|ing)?[-\s]+(?:auth|login|authentication|authorization)|"
    r"secrets?|credentials?|tokens?|traversal|xss|csrf|rce|ssrf|sqli|"
    r"deseriali[sz]ation|privilege[-\s]+escalation)\b"
    r"|注入|认证绕过|鉴权绕过|密钥|凭证|令牌|穿越|跨站|远程代码执行",
    re.I,
)


def mentions_security(text: object) -> bool:
    """True when ``text`` names a security flaw, with or without its tag.

    This is the N51 exemption only. It keeps such a finding out of the repro
    downgrade; it never routes a conflict (see the module docstring).
    """
    return bool(SECURITY_KEYWORDS.search(fold(text)))


# A reviewer tags a conflict finding ``scope: ...`` / ``security: ...`` (or
# ``[scope] ...`` / ``[security] ...``): at the start, right after a leading
# ``path:LINE -`` / ``path:LINE:`` prefix, or anywhere before the first " - ".
# The bare word ("a module-scoped fixture", "no security review") is not a
# conflict.
_TAG_PREFIX = r"^\s*(?:[^\s:]+:\d+(?::\d+)?\s*(?:-|:)\s*)?(?:(?! - ).)*?(?<![\w-])"

#: The N41 scope tag: ``scope:``, ``out-of-scope:``, ``[scope]`` or
#: ``[out-of-scope]``, in the tag position above. The negated spellings say the
#: opposite ("not out-of-scope", "non-out-of-scope", "not out of scope"), so
#: fixed-width lookbehinds keep the common ones from becoming a tag. A wider
#: gap ("not  out of scope:") still reads as a tag, which errs toward
#: adjudication and is the safer side.
_NEGATED_SCOPE = (
    r"(?<!not\s)(?<!not-)(?<!non-)"
    r"(?<!not\sout\sof\s)(?<!non\sout\sof\s)"
)

#: The security tag in the tag position; the named group is the tag text, so
#: two matches can be ordered by where the tag itself starts.
SECURITY_TAG = re.compile(
    _TAG_PREFIX + r"(?P<tag>\[\s*security\s*\]|security\s*:)", re.I
)
#: The scope tag in the tag position, or the shorthand ``scope - ...`` at the
#: start. This is the one regex both the N41 router and the N51 exemption read
#: (R2 review item 4): ``out-of-scope:`` used to be a scope tag only for the
#: exemption, and "not out of scope:" only for the router, so the two
#: disagreed about the same blocker.
SCOPE_TAG = re.compile(
    r"(?:"
    + _TAG_PREFIX
    + _NEGATED_SCOPE
    + r"(?P<tag>(?:out-of-)?scope\s*:|[\[(]\s*(?:out-of-)?scope\s*[\])])"
    + r"|^\s*(?P<dash>(?:out-of-)?scope\s+[-\u2013\u2014]\s))",
    re.I,
)


def security_tag(blocker: object) -> bool:
    """True when ``blocker`` carries a ``security:`` / ``[security]`` tag."""
    return bool(SECURITY_TAG.match(fold(blocker)))


def scope_tag(blocker: object) -> bool:
    """True when ``blocker`` carries a ``scope:`` / ``out-of-scope:`` tag."""
    return bool(SCOPE_TAG.match(fold(blocker)))


def _tag_start(match: re.Match[str]) -> int:
    """Where the tag itself starts in a conflict-tag match (prefix excluded)."""
    if match.groupdict().get("tag") is not None:
        return match.start("tag")
    return match.start("dash")


def conflict_tag(blocker: object) -> str:
    """``scope`` / ``security`` when the blocker carries the conflict tag, else "".

    Tag-only: a keyword in the blocker is not a conflict tag (see the module
    docstring). When both tags fit in the tag position, the one written first
    is the tag.
    """
    text = fold(blocker)
    security = SECURITY_TAG.match(text)
    scope = SCOPE_TAG.match(text)
    if security is not None and scope is not None:
        return "security" if _tag_start(security) < _tag_start(scope) else "scope"
    if security is not None:
        return "security"
    if scope is not None:
        return "scope"
    return ""


def conflict_of(blockers: Iterable[object]) -> str:
    """The conflict a verdict raises: tag-only, security wins over scope, else ""."""
    tags = {conflict_tag(item) for item in blockers}
    if "security" in tags:
        return "security"
    if "scope" in tags:
        return "scope"
    return ""
