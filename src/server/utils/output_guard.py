"""Continuity guard for live Worker text, shared by the server and the daemon.

A live output channel concatenates many small text events into one document
that a person reads. A credential can be split across events, so scanning each
event alone proves nothing. This module is the single place that decides
whether an event may be appended to a stream whose earlier text was already
shown, and it is deliberately *not* a growing pile of patterns:

* The decision is a pure function of ``(state, text)``. The state is a small,
  bounded, JSON-serializable record, so it can live in the database row that
  also holds the events (see ``autonomy_daemon_output_sessions``) and a second
  process reaches the same decision as the first.
* Every text event must end in whitespace (it is *run aligned*): the stream
  never stops inside a word, so no token, key name or value is ever stored
  half-finished and then completed by a later event. The daemon appends a
  newline to each model message and splits long messages at whitespace.
* No unbroken run of non-whitespace characters may exceed ``RUN_MAX``. Every
  credential shape the shared sanitizer knows is built from at most three
  parts (``name``, a gap, ``value``) where the gap is only whitespace and the
  separator characters ``:``/``=``. The window holds the *gap-compressed* tail
  of stored text, in which any such gap is one character, so an arbitrarily
  long gap between a key and its value (blanks, newlines or ``: : :`` alike)
  collapses instead of pushing the key out of a fixed-size window. This premise
  is checked against the real sanitizer by a randomized soundness test. The
  one rule that is not of this shape is the URL userinfo one: it is a single run
  with no gap, and an authority still open at the end of an event (line breaks
  and tabs do not end an authority, so this holds for displayed text too) is
  capped at ``RUN_MAX``, so ``scheme://`` and the whole userinfo stay inside the
  scan tail.
* Anything that cannot be proven safe taints the stream *permanently*: the
  event that tripped it is not stored and no later text is accepted. There is
  no "recoverable later" state, no dropped middle (an event is stored whole or
  not at all) and no pending buffer that can overflow silently.

The checks run on ``window + text`` where ``window`` is text that already
passed the same checks, so a hit always involves the new event.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from src.server.utils.sanitize import ID_KEYWORDS, sanitize_log_text

# The longest unbroken non-whitespace run (a word, URL, hash or path) the stream
# will carry. A longer run cannot be evaluated against the credential rules in
# bounded context, so it taints the stream.
RUN_MAX = 512
# Compressed tail of stored text kept as scan context. Five maximal runs: the
# credential rules need at most three, two of which may come from the window.
WINDOW_MAX = 5 * RUN_MAX
# Hard cap on one event's text (the transport contract uses the same bound).
EVENT_TEXT_MAX = 4000

# 3: the joined (adjacency) windows are required state; 2 added the projection
# windows, 1 had a single window.
STATE_VERSION = 3

# Reasons recorded on a tainted stream. They are fixed identifiers, never a
# fragment of the text that tripped them.
REASON_CONTROL = "control_character"
REASON_UNALIGNED = "unaligned_event"
REASON_LONG_RUN = "long_run"
REASON_PEM = "pem_marker"
REASON_CREDENTIAL = "credential_shape"
REASON_KEY = "credential_key"
REASON_BEARER = "bearer_scheme"
REASON_URL_CREDENTIAL = "url_credential"
REASON_REDACTION = "redaction_candidate"
REASON_OVERSIZE = "oversized_event"
REASON_STATE = "guard_state_invalid"
REASON_TAINTED = "already_tainted"
REASON_BATCH_GAP = "batch_gap"
REASON_DISCONTINUITY = "discontinuity"
REASON_STORE = "store_unconfirmed"

# C0 controls other than tab/newline/carriage return, DEL and the C1 range. ESC
# starts every terminal sequence, so a sequence cannot be modelled in bounded
# state across events: seeing one taints rather than guessing where it ends.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# Characters that change what a reader sees without being visible text. The
# Unicode Default_Ignorable_Code_Point set plus the format characters (Cf) are
# invisible inside a word, so ``pass<U+200B>word:`` and ``sk-AB<U+034F>CD...``
# read as the key and the token they spell: those are folded out before any rule
# runs. Bidi overrides/isolates, the tag block (invisible ASCII look-alikes) and
# the reserved range around it change what is shown, and a lone surrogate cannot
# even be encoded: those taint outright.
_INVISIBLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_TAINT_RANGES: tuple[tuple[int, int], ...] = (
    (0x202A, 0x202E),  # bidi overrides
    (0x2066, 0x2069),  # bidi isolates
    (0xD800, 0xDFFF),  # lone surrogates
    (0xE0000, 0xE00FF),  # tag characters and the reserved range after them
    (0xE01F0, 0xE0FFF),
)


def _char_class(ranges: tuple[tuple[int, int], ...]) -> str:
    return "[" + "".join(
        f"\\U{low:08x}" if low == high else f"\\U{low:08x}-\\U{high:08x}"
        for low, high in ranges
    ) + "]"


_HIDDEN_TAINT_RE = re.compile(_char_class(_TAINT_RANGES))
_FOLD_OUT_RE = re.compile(_char_class(_INVISIBLE_RANGES))
# Folding may expand text (NFKC turns one character into up to eighteen), so the
# folded form is bounded as well as the original.
FOLDED_MAX = 2 * EVENT_TEXT_MAX
# The lengths below are checked by scanning words, never by a bounded repetition
# (``\\S{513,}`` retries from every offset of a long word: quadratic).
_WORD_RE = re.compile(r"\S+")
_GAP_WORD_RE = re.compile(r"[^\s:=]+")


def _longest(pattern: re.Pattern[str], text: str) -> int:
    return max((len(m.group()) for m in pattern.finditer(text)), default=0)


_WS_RUN_RE = re.compile(r"\s+")
_GAP_RUN_RE = re.compile(r"[\s:=]+")
_ENDS_WS_RE = re.compile(r"\s\Z")
_WS_RE = re.compile(r"\s")

# A credential's first characters are already secret material, so these fire on
# the prefix plus a short body, well before the sanitizer's full-length rules.
_CREDENTIAL_RE = re.compile(
    r"(?i)(?:eyJ[A-Za-z0-9_-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{8,}"
    r"|github_pat_[A-Za-z0-9_]{8,}"
    r"|glpat-[A-Za-z0-9_-]{8,}"
    r"|sk-[A-Za-z0-9_-]{8,}"
    r"|AKIA[0-9A-Z]{8,}|ASIA[0-9A-Z]{8,}"
    r"|AIza[0-9A-Za-z_-]{8,}"
    r"|xox[baprs]-[A-Za-z0-9-]{8,}"
    r"|npm_[A-Za-z0-9]{8,}"
    r"|dop_v1_[A-Za-z0-9]{8,}"
    r"|gx[sa]-[A-Za-z0-9]{8,})"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+\S")
# `Basic` is also an ordinary word, so the scheme is recognized by what follows
# it: a base64-looking word with a digit or one of ``+/=``, or with a capital
# after its first letter and a lower-case letter too (``YTpi`` is ``a:b``). The
# sanitizer only acts on eight characters, long after a short credential is out.
_BASIC_RE = re.compile(
    r"(?i:\bbasic)\s+"
    r"(?:(?=[A-Za-z0-9+/]*(?:[0-9+/]|=))"  # a digit, +, / or padding, in any case, or
    r"|(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/][A-Za-z0-9+/]*[A-Z]))"  # mixed case
    r"[A-Za-z0-9+/]{4,}"
)
# A credential-bearing name followed by a separator: the value that follows
# (however many events or blanks later) is withheld by tainting at the name.
_KEY_RE = re.compile(
    r"(?i)(?:cookie|set-cookie|authorization|x-api-key|api[_-]?key|"
    r"password|passwd|secret|access[_-]?token|refresh[_-]?token|"
    r"private[_-]?key)[\"']?\s*[:=]"
)
# The sanitizer redacts ``session <uuid>`` only once the id is complete, which is
# after 35 of its 36 characters could already be stored. The id's first two
# groups after one of its key words are secret material and refuse the event.
_ID_PREFIX_RE = re.compile(
    rf"(?i)\b(?:{ID_KEYWORDS})\b[\"']?[\s:=]+[\"']?[0-9a-f]{{8}}-[0-9a-f]{{4}}-"
)
_PEM_MARKERS = ("-----BEGIN", "-----END")

# The anchor is the ``://`` itself, not the scheme: a scheme glued to a longer
# word, or a second URL inside the first one's authority, would otherwise hide
# it. Extra and backward slashes are read the way a browser reads them, and so
# are the tab and line break such a parser drops from a URL, wherever they fall
# in the anchor. A scheme-relative ``//user:pw@host`` has no anchor and is not
# read: nearly every ``//`` in code and prose would be one.
#
# The authority is read as widely as any URL parser reads userinfo, because the
# guard has to refuse what *any* of them would take for a password. Only a
# plain blank and the delimiters ``/ ? #`` end it. A backslash is an ordinary
# userinfo character for every scheme but the web ones, so it does not end it;
# a tab or a line break is dropped from a URL by WHATWG parsers, urllib and
# yarl, so it does not end it either; and a Unicode space (see ``_fold``) is a
# userinfo character, not a separator.
_AUTHORITY_RE = re.compile(r":[\t\n]*(?:[/\\][\t\n]*){2,}(?=([^ /?#]*))")
# A user name with no password is a credential only when it is a long mixed
# token (a hex or base62 API key, base64 padding allowed); provider prefixes are
# refused by ``_CREDENTIAL_RE`` whatever follows them.
_TOKEN_NAME_RE = re.compile(r"[A-Za-z0-9_+-]{24,}={0,2}")
_HAS_DIGIT_RE = re.compile(r"[0-9]")
_HAS_LETTER_RE = re.compile(r"[A-Za-z]")
# A password that carries nothing: what pip, npm and SQLAlchemy print in its
# place, and the sanitizer's own marker.
_MASK_RE = re.compile(r"\*+|\[REDACTED\]")
# What may follow ``host:`` where a message stops inside an authority: a port,
# then at most three of the characters that close a sentence, a code span or
# markdown emphasis. Anything else (a letter, a symbol, another script) is
# password material. A password of up to five digits is indistinguishable from
# a port, so such a password, and what closes prose after it, can be stored.
_PORT_PROSE_RE = re.compile(r"[0-9]{1,5}[.,;:!?)\]}>'\"`*_~]{0,3}")
# An IPv6 host still being written (``[fe80::1%eth0``): hex digits, colons,
# dots, then a zone. Any other character after ``[`` is a user name.
_IPV6_OPEN_RE = re.compile(r"\[[0-9A-Fa-f:.]*(?:%[A-Za-z0-9_.~-]*)?")


def _token_name(name: str) -> bool:
    # A tab, line break or Unicode space is dropped or kept by the parser that
    # reads the name, never a boundary of it: judge the characters themselves.
    name = "".join(name.split())
    return bool(
        _TOKEN_NAME_RE.fullmatch(name)
        and _HAS_DIGIT_RE.search(name)
        and _HAS_LETTER_RE.search(name)
    )


def _secret_password(password: str) -> bool:
    return bool(password) and _MASK_RE.fullmatch(password) is None


def _split_userinfo(authority: str) -> tuple[str, str, str]:
    """``name``, ``:`` and the rest, split at the first colon outside ``[...]``."""
    start = authority.find("]") + 1 if authority.startswith("[") else 0
    cut = authority.find(":", start)
    if cut < 0:
        return authority, "", ""
    return authority[:cut], ":", authority[cut + 1 :]


def _open_authority_secret(authority: str) -> bool:
    """An authority cut off by the end of the text may still become userinfo.

    The next event can bring the ``@``, and by then the password would already
    be stored, so it is withheld at its first characters: ``host:port`` is told
    from ``user:password`` by the port being digits. The cap keeps ``scheme://``
    and the whole userinfo inside the scan tail however the rest arrives.
    """
    if len(authority) > RUN_MAX:
        return True
    if authority.endswith("\n"):
        return False  # a displayed event ends on a line break, as every one does
    authority = authority.rstrip()  # a trailing tab or Unicode space ends nothing
    if authority.startswith("[") and "]" not in authority:
        return _IPV6_OPEN_RE.fullmatch(authority) is None
    name, colon, rest = _split_userinfo(authority)
    if _token_name(name):
        return True
    return (
        bool(colon)
        and _secret_password(rest)
        and _PORT_PROSE_RE.fullmatch(rest) is None
    )


def _userinfo_secret(userinfo: str) -> bool:
    name, _, password = userinfo.partition(":")
    # A token as the user name is a credential whatever follows it:
    # ``curl -u TOKEN:`` style URLs carry an empty or masked password.
    return _token_name(name) or _secret_password(password)


_DRIVE_RE = re.compile(r"[A-Za-z]:")


def _name_and_non_port(authority: str) -> bool:
    """Whether ``authority`` is ``name:`` followed by anything that is no port.

    An empty port counts, so ``user:/ss@host`` is caught.
    """
    _, colon, rest = _split_userinfo(authority)
    return bool(colon) and _PORT_PROSE_RE.fullmatch(rest) is None


def _url_credential(probe: str) -> bool:
    """Whether ``probe`` holds, or is about to complete, a URL userinfo secret.

    The sanitizer only catches ``user:pw@host`` when the host looks like an
    e-mail domain, so ``localhost``, service names and IP addresses (where
    ``DATABASE_URL`` lives) pass it, and it fires after the password is out.
    Userinfo is whatever precedes an ``@`` of the authority; the first and the
    last one are both tried, since parsers disagree on which ends it.
    """
    pos = 0
    run_end = last_at = -1
    while (match := _AUTHORITY_RE.search(probe, pos)) is not None:
        authority = match.group(1)
        end = match.end(1)
        # An anchor inside the authority is part of it and was judged with it;
        # but the colon that ends it can open the next URL (``http://a://u:p@h``).
        pos = end - 1 if authority.endswith(":") else end
        if "@" in authority:
            last = authority.rfind("@")
            for cut in {authority.find("@"), last}:
                if _userinfo_secret(authority[:cut]):
                    return True
            # ``smtp://deploy@mail:hunter2pw``: an ``@`` in the user name does
            # not close the userinfo, and the real one can come next.
            if end == len(probe) and _open_authority_secret(authority[last + 1 :]):
                return True
        elif end == len(probe):
            # The text stops inside the authority: mid-word as the model wrote
            # it (see ``scan``'s ``join``), or on a tab or a Unicode space.
            if _open_authority_secret(authority):
                return True
        elif _name_and_non_port(authority) and not (
            # ``file://C:/dir/@scope``: a drive, not a user name.
            _DRIVE_RE.fullmatch(authority)
            and probe[max(0, match.start() - 4) : match.start()].lower() == "file"
        ):
            # ``user:pa/ss@host``: a parser that ends the authority at ``/``,
            # ``?`` or ``#`` reads a host and a port, but SQLAlchemy and most
            # DSN parsers take everything up to the ``@`` as the password. An
            # empty port (``user:/ss@host``) counts as no port: a path that
            # holds an ``@`` right after one is far rarer than such a password.
            # The run ends at a plain blank, like the authority itself: a tab,
            # a line break or a Unicode space is part of the password up to the
            # ``@``. One that ends the text closes it, as in ``_open_authority_secret``.
            if end > run_end:
                blank = probe.find(" ", end)
                run_end = blank if blank >= 0 else len(probe)
                last_at = probe.rfind("@", end, run_end)
            if last_at >= end or (run_end == len(probe) and not probe.endswith("\n")):
                return True
    return False


CHANNEL_TEXT = "text"
CHANNEL_REASONING = "reasoning"
CHANNEL_TOOL = "tool"
_CHANNELS = frozenset({CHANNEL_TEXT, CHANNEL_REASONING, CHANNEL_TOOL})
# Every way a watcher may render the stream: it can leave reasoning out (print
# mode, "hide reasoning"), leave tool names out, or both. Omitting an event
# creates adjacencies the full stream never had, so each projection keeps its
# own window and every one of them must be safe by itself.
_VIEWS: tuple[tuple[str, frozenset[str]], ...] = (
    ("window", frozenset({CHANNEL_TEXT, CHANNEL_REASONING, CHANNEL_TOOL})),
    ("no_reasoning", frozenset({CHANNEL_TEXT, CHANNEL_TOOL})),
    ("no_tool", frozenset({CHANNEL_TEXT, CHANNEL_REASONING})),
    ("text_only", frozenset({CHANNEL_TEXT})),
)


_PROJECTION_NAMES = tuple(name for name, _ in _VIEWS)


@dataclass(frozen=True)
class GuardState:
    """Bounded continuity state for one stream epoch.

    ``window`` is the whole stream as shown together; ``no_reasoning``,
    ``no_tool`` and ``text_only`` are the windows of the projections that omit
    those channels (see ``_VIEWS``). Those four describe the text *as displayed*.
    ``joined`` is the same four windows for the text *as the model wrote it*,
    where the synthetic newline of a ``join`` event is not there (a message that
    ended mid-word runs into the next); it is ``None`` while the two are
    identical, which is the common case.
    """

    window: str = ""
    no_reasoning: str = ""
    no_tool: str = ""
    text_only: str = ""
    joined: tuple[str, str, str, str] | None = None
    tainted: bool = False
    reason: str = ""

    @property
    def displayed(self) -> tuple[str, str, str, str]:
        return (self.window, self.no_reasoning, self.no_tool, self.text_only)

    def to_json(self) -> dict[str, Any]:
        return {
            "v": STATE_VERSION,
            "tainted": self.tainted,
            "reason": self.reason,
            "no_reasoning": self.no_reasoning,
            "no_tool": self.no_tool,
            "text_only": self.text_only,
            "joined": (
                None
                if self.joined is None
                else dict(zip(_PROJECTION_NAMES, self.joined))
            ),
        }

    @classmethod
    def from_columns(
        cls, window: object, tainted: object, guard_state: object
    ) -> GuardState:
        """Rebuild from the persisted columns; anything malformed is tainted."""
        if (
            not isinstance(window, str)
            or len(window) > WINDOW_MAX
            or not isinstance(guard_state, dict)
            or guard_state.get("v") != STATE_VERSION
            or "joined" not in guard_state
        ):
            return cls(tainted=True, reason=REASON_STATE)
        extras: dict[str, str] = {}
        for name in ("no_reasoning", "no_tool", "text_only"):
            # Required, never defaulted: a window that went missing from the
            # persisted state is lost context, not an empty one.
            value = guard_state.get(name)
            if not isinstance(value, str) or len(value) > WINDOW_MAX:
                return cls(tainted=True, reason=REASON_STATE)
            extras[name] = value
        joined: tuple[str, str, str, str] | None = None
        raw_joined = guard_state["joined"]
        if raw_joined is not None:
            if not isinstance(raw_joined, dict):
                return cls(tainted=True, reason=REASON_STATE)
            parts = [raw_joined.get(name) for name in _PROJECTION_NAMES]
            if any(not isinstance(v, str) or len(v) > WINDOW_MAX for v in parts):
                return cls(tainted=True, reason=REASON_STATE)
            joined = (parts[0], parts[1], parts[2], parts[3])
        reason = guard_state.get("reason")
        if bool(tainted) or bool(guard_state.get("tainted")):
            return cls(
                tainted=True,
                reason=reason if isinstance(reason, str) and reason else REASON_STATE,
            )
        return cls(window=window, joined=joined, **extras)


@dataclass(frozen=True)
class ScanResult:
    ok: bool
    text: str
    state: GuardState
    reason: str = ""


def has_control(text: str) -> bool:
    """Whether ``text`` holds a terminal control or a hidden-character taint.

    Cheap and linear: callers use it to refuse such text *before* any heavier
    per-character processing runs on it.
    """
    return bool(_CONTROL_RE.search(text) or _HIDDEN_TAINT_RE.search(text))


def normalize_text(text: str) -> str:
    """Line-ending normalization applied before any decision."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


# The characters NFKC would turn into a plain blank, or into text that holds
# one. A plain blank ends a URL authority, but a URL parser reads none of these
# as a blank, so folding one inside a password would end the authority there
# and hide the rest of it. They are whitespace to every other rule here (the
# Unicode spaces match ``\s``), so they are kept as they are and only the
# sanitizer is shown their folded form.
_KEPT_SPACES: tuple[tuple[int, int], ...] = (
    (0x00A0, 0x00A0),
    (0x2000, 0x200A),
    (0x202F, 0x202F),
    (0x205F, 0x205F),
    (0x3000, 0x3000),
)
# Spacing accents and Arabic ligatures: their NFKC form is a blank followed by a
# combining mark (U+FDFA and U+FDFB spell a phrase with blanks in it). Listed
# rather than computed so the server and every daemon decide alike whatever
# Unicode tables their Python carries; a test checks the list against the
# tables of the interpreter that runs it.
_KEPT_MARKS: tuple[tuple[int, int], ...] = (
    (0x00A8, 0x00A8),
    (0x00AF, 0x00AF),
    (0x00B4, 0x00B4),
    (0x00B8, 0x00B8),
    (0x02D8, 0x02DD),
    (0x037A, 0x037A),
    (0x0384, 0x0385),
    (0x1FBD, 0x1FBD),
    (0x1FBF, 0x1FC1),
    (0x1FCD, 0x1FCF),
    (0x1FDD, 0x1FDF),
    (0x1FED, 0x1FEE),
    (0x1FFD, 0x1FFE),
    (0x2017, 0x2017),
    (0x203E, 0x203E),
    (0x309B, 0x309C),
    (0xFC5E, 0xFC63),
    (0xFDFA, 0xFDFB),
    (0xFE49, 0xFE4C),
    (0xFE70, 0xFE70),
    (0xFE72, 0xFE72),
    (0xFE74, 0xFE74),
    (0xFE76, 0xFE76),
    (0xFE78, 0xFE78),
    (0xFE7A, 0xFE7A),
    (0xFE7C, 0xFE7C),
    (0xFE7E, 0xFE7E),
    (0xFFE3, 0xFFE3),
)
_KEPT_RANGES = _KEPT_SPACES + _KEPT_MARKS
_KEEP_RE = re.compile("(" + _char_class(_KEPT_RANGES) + ")")
_BLANKED_RE = re.compile(_char_class(_KEPT_RANGES))
# What the sanitizer is shown in their place: the form it was written against.
_BLANKED_FORM = {
    chr(code): unicodedata.normalize("NFKC", chr(code))
    for low, high in _KEPT_RANGES
    for code in range(low, high + 1)
}


def _fold(text: str) -> str:
    """The form every rule reads: no invisible joiners, compatibility-folded.

    NFKC maps full-width and other compatibility look-alikes (``\uff1d`` for
    ``=``, ``\uff50``..) onto the ASCII the credential rules are written for,
    except the characters that fold into a blank (see ``_KEPT_RANGES``).
    The stored text is the original; only the decision uses this.
    """
    pieces = _KEEP_RE.split(_FOLD_OUT_RE.sub("", text))
    return "".join(
        piece if index % 2 else unicodedata.normalize("NFKC", piece)
        for index, piece in enumerate(pieces)
    )


# Gap runs are normalized (each blank run becomes one blank, each contiguous run
# of ``:``/``=`` is capped at eight) and, if still longer than ``_GAP_KEEP``,
# reduced to their start and their end. The start is where a rule finds its
# structure (blank, the one separator, blank, the first stretch of value
# characters, at most 1+8+1+8 characters); the end is where a value or token
# that continues in the next event begins. Nothing in between can matter: a rule
# whose keyword precedes the run uses the start of the run, and one that lies
# wholly inside the run was judged when that text arrived.
_GAP_HEAD = 20
_GAP_TAIL = 8
_GAP_KEEP = 28
_BLANK_RUN_RE = re.compile(r"\s+")
_SEP_RUN_RE = re.compile(r"[:=]{9,}")


def _blank(match: re.Match[str]) -> str:
    run = match.group()
    if "\n" in run:
        return "\n"
    return " " if " " in run else run[0]


def _gap(match: re.Match[str]) -> str:
    run = match.group()
    if len(run) <= _GAP_KEEP:
        return run
    # Separators are value characters too (a token may be ``==ab``) and the
    # sanitizer's thresholds count them (a value of 6, a bearer token of 8), so
    # their runs are capped at eight rather than collapsed to one; that keeps
    # every threshold decidable. A blank run keeps the *kind* of blank it held:
    # a plain blank is the one character that ends a URL authority, so turning a
    # run of tabs or Unicode spaces into one would end an authority the single
    # event left open, and the password after it would no longer be recognized.
    run = _BLANK_RUN_RE.sub(_blank, run)
    run = _SEP_RUN_RE.sub(lambda r: r.group()[:8], run)
    if len(run) <= _GAP_KEEP:
        return run
    return run[:_GAP_HEAD] + run[-_GAP_TAIL:]


def _compress(text: str) -> str:
    """Bound every gap, keeping what the credential rules look at.

    Every credential rule joins its parts with a gap drawn from whitespace and
    the two separator characters (``key``, ``: =``, ``value``; a keyword and a
    UUID with ``[\\s:=]+`` between them). A maximal run of those characters is
    kept as is up to ``_GAP_KEEP`` characters; a longer one is normalized and, if
    still long, reduced to its head and tail (see ``_gap``). The pass is one
    linear scan (no nested quantifiers).
    """
    return _GAP_RUN_RE.sub(_gap, text)


def _trim_window(compressed: str) -> str:
    """Bound the window, cutting only at a gap.

    A cut inside a word could turn a word-boundary rule (``\\b``) into a false
    match on the word's remainder, so the partial leading word is dropped. The
    cut lands on the first *gap* character (blank, ``:`` or ``=``), not the
    first blank: compressing gaps can glue many short words into one long
    stretch with no blank in it (``a:b:c:...``), and a blank-only search would
    then discard everything up to the final newline, key included.
    """
    if len(compressed) <= WINDOW_MAX:
        return compressed
    cut = compressed[-WINDOW_MAX:]
    boundary = _GAP_RUN_RE.search(cut)
    return cut[boundary.start() :] if boundary else ""


def taint(state: GuardState, reason: str) -> GuardState:
    """The terminal state: nothing is accepted afterwards, window is dropped."""
    return GuardState(tainted=True, reason=state.reason or reason)


def _violation(probe: str) -> str:
    """The first rule ``probe`` breaks, or an empty string."""
    if any(marker in probe for marker in _PEM_MARKERS):
        return REASON_PEM
    if _CREDENTIAL_RE.search(probe):
        return REASON_CREDENTIAL
    if _BEARER_RE.search(probe) or _BASIC_RE.search(probe):
        return REASON_BEARER
    if _KEY_RE.search(probe):
        return REASON_KEY
    if _ID_PREFIX_RE.search(probe):
        return REASON_REDACTION
    if _url_credential(probe):
        return REASON_URL_CREDENTIAL
    # The authoritative sanitizer has the last word on everything the rules
    # above do not name (cookies, key=value pairs, ids next to key words,
    # emails, full-length provider tokens). The window already passed, so a
    # difference means the new text completes a redaction candidate.
    blanked = _BLANKED_RE.sub(lambda m: _BLANKED_FORM[m.group()], probe)
    if sanitize_log_text(blanked, max_length=0, strip=False) != blanked:
        return REASON_REDACTION
    return ""


def scan(
    state: GuardState,
    text: str,
    channel: str = CHANNEL_TEXT,
    *,
    join: bool = False,
) -> ScanResult:
    """Decide whether ``text`` may be appended to the stream.

    ``join`` marks an event whose final newline is *synthetic*: the daemon added
    it so a model message ends on a display line break, but the message itself
    ended mid-word. For continuity such an event runs straight into the next
    one (the newline is not a boundary), so an open word is carried in the
    window and a credential split across two messages is still one token.

    ``channel`` says what the event is. The event is judged against the window
    of every projection that shows it (``text`` is in all of them, ``reasoning``
    and ``tool`` only in the ones that keep them), so no way of rendering or
    hiding parts of the stream can join a key to its value. ``ok`` results carry the normalized text to store verbatim
    and the state after it. A refusal carries a tainted state; the caller must
    persist that state, store nothing for the refused event and drop all later
    text.
    """
    if state.tainted:
        return ScanResult(False, "", state, state.reason or REASON_TAINTED)
    body = normalize_text(text)
    if not body:
        return ScanResult(True, "", state)

    def refuse(reason: str) -> ScanResult:
        return ScanResult(False, "", taint(state, reason), reason)

    if channel not in _CHANNELS:
        return refuse(REASON_STATE)
    if len(body) > EVENT_TEXT_MAX:
        return refuse(REASON_OVERSIZE)
    if _CONTROL_RE.search(body) or _HIDDEN_TAINT_RE.search(body):
        return refuse(REASON_CONTROL)
    if join:
        # The one synthetic character is display only; everything before it is
        # the raw message end and is scanned as if the next event followed it.
        if not body.endswith("\n") or len(body) < 2:
            return refuse(REASON_UNALIGNED)
        scanned = body[:-1]
    else:
        if not _ENDS_WS_RE.search(body):
            return refuse(REASON_UNALIGNED)
        scanned = body
    folded = _fold(scanned)
    if len(folded) > FOLDED_MAX:
        # NFKC can expand one character into many; bound the folded form too.
        return refuse(REASON_OVERSIZE)
    if not folded or (not join and not _ENDS_WS_RE.search(folded)):
        # Nothing readable, or the visible text no longer ends on a boundary
        # (a zero-width tail after the last word): unprovable, not skippable.
        return refuse(REASON_UNALIGNED)
    if _longest(_WORD_RE, folded) > RUN_MAX:
        return refuse(REASON_LONG_RUN)
    # The text as displayed carries the synthetic newline of a joined event; the
    # text as the model wrote it does not. Both are judged, in every projection.
    shown, reason = _advance(state.displayed, folded + ("\n" if join else ""), channel)
    if reason:
        return refuse(reason)
    if join or state.joined is not None:
        written, reason = _advance(
            state.joined if state.joined is not None else state.displayed,
            folded,
            channel,
        )
        if reason:
            return refuse(reason)
    else:
        written = shown
    return ScanResult(
        True,
        body,
        GuardState(
            window=shown[0],
            no_reasoning=shown[1],
            no_tool=shown[2],
            text_only=shown[3],
            joined=None if written == shown else written,
        ),
    )


# How much of a window a new event can interact with. A rule's match spans a
# name or open word (<= RUN_MAX), a bounded gap and the start of a value (<=
# RUN_MAX); everything older was judged when it arrived.
_TAIL_MAX = 2 * RUN_MAX + 2 * _GAP_KEEP + 64


def _interacting_tail(window: str) -> str:
    if len(window) <= _TAIL_MAX:
        return window
    cut = window[-_TAIL_MAX:]
    boundary = _GAP_RUN_RE.search(cut)
    return cut[boundary.start() :] if boundary else ""


def _advance(
    windows: tuple[str, str, str, str], piece: str, channel: str
) -> tuple[tuple[str, str, str, str], str]:
    """Judge ``piece`` against each projection window that shows ``channel``.

    Projections whose windows are identical (the usual case: no reasoning or
    tool names yet) are judged once.
    """
    judged: dict[str, tuple[str, str]] = {}
    updated: list[str] = []
    for current, (_, channels) in zip(windows, _VIEWS):
        if channel not in channels:
            updated.append(current)  # this projection never shows the event
            continue
        outcome = judged.get(current)
        if outcome is None:
            probe = _interacting_tail(current) + piece
            if _longest(_GAP_WORD_RE, probe) > RUN_MAX:
                reason = REASON_LONG_RUN
            else:
                reason = _violation(probe)
            outcome = (reason, "" if reason else _trim_window(_compress(current + piece)))
            judged[current] = outcome
        reason, window = outcome
        if reason:
            return windows, reason
        updated.append(window)
    return (updated[0], updated[1], updated[2], updated[3]), ""


__all__ = [
    "EVENT_TEXT_MAX",
    "GuardState",
    "has_control",
    "REASON_BATCH_GAP",
    "REASON_CONTROL",
    "REASON_DISCONTINUITY",
    "REASON_REDACTION",
    "REASON_STATE",
    "REASON_STORE",
    "REASON_TAINTED",
    "REASON_URL_CREDENTIAL",
    "RUN_MAX",
    "CHANNEL_REASONING",
    "CHANNEL_TEXT",
    "CHANNEL_TOOL",
    "ScanResult",
    "WINDOW_MAX",
    "normalize_text",
    "scan",
    "taint",
]
