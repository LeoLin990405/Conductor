"""Redact credential-shaped text out of agent log tails before they are shown.

The fleet dashboard reads logs written by third-party CLIs, which dump whole
environments, request headers and stack traces. Everything crossing this module
is treated as untrusted: the API returns a redacted string or nothing, never a
partially masked one, and the scanner never writes back to the worktrees.
"""

from __future__ import annotations

import json
import re
import uuid
from array import array
from bisect import bisect_left, bisect_right

REDACTED = "[REDACTED]"

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")
# A PEM body line and nothing else: the whole-line half of the rule that
# decides whether a line carries material.
_BASE64_LINE = re.compile(r"[A-Za-z0-9+/=]+")
# The shortest run of key characters that is read as material rather than as
# a word. Six leaves the ordinary words of a log line alone — `agent`, `keys`
# and `done` are all shorter — and the log levels (INFO, WARN, ERROR, DEBUG,
# TRACE, FATAL) are shorter still.
_BODY_RUN_MIN = 6
# A run of key characters, looked for anywhere in the line: a CLI writes a body
# line behind its own log prefix — a timestamp, a level — so the material sits
# inside the line rather than making up all of it, and a word may also trail
# behind it. The padding character is left out of the run on purpose: a `=`
# inside a line is the separator of a `key=value` pair rather than part of a
# key, so it ends one run instead of joining two.
_BODY_RUN = re.compile(rf"[A-Za-z0-9+/]{{{_BODY_RUN_MIN},}}")
# A run of 20 or more key characters is key material whatever it is made of:
# base64 data that happens to spell a lower-case word is still data.
_BODY_RUN_LONG = 20
# The characters that tell a run of key characters apart from a word. A word
# is written in letters, so a run that carries a digit — or the `+` of the
# base64 alphabet — is data rather than a word.
#
# A capital letter does not tell the two apart: English words are capitalized,
# and so are the identifiers a log line carries (`MCPConfig`, `TimeoutError`).
# Reading a capital as key material swallows the log around a key — every line
# above the body that names anything goes with it. The `/` of the base64
# alphabet is left out for the same reason: it is the path separator, and a run
# that carries one may be a path a log line names.
_BODY_DATA_CHARS = frozenset("0123456789+")
# A body line a CLI wrote behind its own log prefix: the line ends in one
# opaque run of key characters and nothing in front of it is one. A word is
# written in letters and an ordinary log line *says* something — `Connection
# established`, `rotating keys` — so it carries a second long token, while the
# prefix a CLI puts in front of a body (a timestamp, a level) is shorter than a
# body run. This is what tells a short body line from a word: a body of ten
# characters carries no digit and no `+`, so the rules above read it as a word
# and the walk back from a marker finds no material at all. The price is that a
# log line whose whole payload is one long word goes with the key above it.
_BODY_PAYLOAD_MIN = 8
# One token of the prefix a CLI writes in front of a line. A date and the clock
# behind it are two tokens, not one — Python's own logging writes `12:00:01,123`
# — syslog writes a month, a day and a clock, and a prefix is as often bracketed
# as not: `[INFO]`, `[2026-11-02 12:00:01]`, with the level in either case. A
# lower-case level is held to the names a level actually has rather than to any
# word, so a line whose whole payload is one word behind an ordinary one
# (`worker finished`) stays a log line.
_PREFIX_TOKEN = (
    r"(?:"
    r"\[[^\]\n]*\]"  # a bracketed prefix
    r"|[\d][\dT:.Z+\-,]*"  # a date, a clock or a pid
    r"|[A-Z][a-z]{2}"  # the month of a syslog stamp
    r"|[A-Z]{2,9}"  # a level
    r"|trace|debug|info|notice|warn|warning|error|err|crit|critical|fatal|severe|panic"
    r"):?"
)
# The prefix is what makes a short run of letters data rather than a word: a
# body sits behind one, and a bare line of letters is a log token the
# whole-line rule above judges on its own. A rule that also reads a bare line
# as a prefixed body swallowed `Traceback` and every other lone word a CLI
# writes on its own line.
_LOG_PREFIXED_BODY = re.compile(
    rf"^\s*(?:{_PREFIX_TOKEN}\s+){{1,5}}"
    rf"([A-Za-z0-9+/]{{{_BODY_PAYLOAD_MIN},}})\s*$"
)
# The characters a word never carries, read for a line that is nothing but one
# run of key characters. `=` pads a base64 body and `/` is in its alphabet, so
# on such a line either one is data — the two are left out of the in-line rule
# because there they are separators (a `key=value` pair, a path a log names).
_BODY_LINE_DATA_CHARS = frozenset("0123456789+/=")
# The shortest bare run of key characters that is read as material rather than
# as a log word. Ordinary log words and the failure markers the status reader
# needs (`FAILED`, `ERROR`, `WARNING`, `Traceback`, `done`) are nine characters
# or fewer; a bare run of ten or more has the shape of a short key body a
# window cut free of its block.
_BARE_BODY_MIN = 10
# The exact standalone markers are useful to the status reader and are too
# short to distinguish from base64 by shape alone.
_BARE_STATUS_MARKERS = frozenset({"FAILED", "ERROR"})
# How many consecutive lines of log noise the walk back from an END marker may
# cross before it gives up. A key body and its marker are contiguous in a PEM
# dump, but a CLI interleaves its own log lines between them and a window cut
# leaves a fragment behind; the walk crosses that noise so the body stays
# linked to the marker. The count is bounded because the walk is what keeps a
# stray marker from swallowing the log: past this many lines the body is no
# longer assumed to belong to the marker.
_BODY_GAP = 8


def _carries_data(run: str) -> bool:
    """Whether ``run`` carries a character a word does not.

    A word is written in letters. Key data carries a digit, or the `+` of the
    base64 alphabet, and a capital letter is not enough: English words are
    capitalized too.
    """
    return not _BODY_DATA_CHARS.isdisjoint(run)


def _is_body_line(line: str) -> bool:
    """Whether ``line`` carries a base64 body run, log prefix and all.

    The whole line goes when it does, rather than the run alone: a span that
    opens mid-line cuts whatever the run follows — an address, a token — in
    half, and the rules that would have redacted it whole can no longer see it.

    A line that is nothing but one run of key characters is only material when
    it is long or carries a character a word does not. A short line of letters
    is a log token — `FAILED`, `ERROR`, `Traceback` — and the dashboard reads
    the redacted tail for the failure markers that decide an agent's status, so
    a stray marker must not be able to swallow one. Ten characters, or a
    digit, `+`, `=` or `/`, is data whatever it spells; the exact failure
    markers are kept visible ahead of everything else.
    """
    stripped = line.strip()
    if stripped in _BARE_STATUS_MARKERS:
        return False
    if _BASE64_LINE.fullmatch(stripped) and (
        len(stripped) >= _BARE_BODY_MIN
        or not _BODY_LINE_DATA_CHARS.isdisjoint(stripped)
    ):
        return True
    for match in _BODY_RUN.finditer(line):
        run = match.group()
        if len(run) >= _BODY_RUN_LONG or _carries_data(run):
            return True
    return _LOG_PREFIXED_BODY.match(line) is not None


class _Bodies:
    """The base64 bodies that lead into the END markers of one text.

    Every line is read once, here, and every marker answers from the index
    that pass leaves behind. A marker that walked the lines above it on its
    own rescanned the same lines again for each marker in the text — the
    material lines of a tail that alternates body lines and markers lead all
    the way to the top, so each walk crossed the whole tail and the pass cost
    the square of its size — and locating the marker's own line with a
    backwards search from the start of the text cost the square again. Both
    are one lookup per marker now, so the pass scales with the tail.
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self._indexed: tuple[list[int], list[int], list[int]] | None = None

    def _index(self) -> tuple[list[int], list[int], list[int]]:
        """Index the line starts, the material lines and their block tops.

        A block is the run of material lines the walk back from a marker is
        allowed to cross: it starts at the first material line and at every
        line separated from the material line above it by more noise than
        ``_BODY_GAP`` allows. Tagging each material line with the top of its
        block is what turns the walk into a lookup. The noise between a
        marker and the material line above it is not part of the index — the
        marker's line is its block's bottom and only the lookup knows it — so
        ``start`` checks that one gap itself.
        """
        starts: list[int] = []
        material: list[int] = []
        blocks: list[int] = []
        number = 0
        start = 0
        top = 0
        previous: int | None = None
        for line in self._text.split("\n"):
            starts.append(start)
            if _is_body_line(line):
                if previous is None or number - previous - 1 > _BODY_GAP:
                    top = start
                material.append(number)
                blocks.append(top)
                previous = number
            number += 1
            start += len(line) + 1
        return starts, material, blocks

    def start(self, marker: int) -> int:
        """Start of the body that leads into the END marker at ``marker``.

        The body is the block of material lines that leads into the marker.
        It crosses runs of up to ``_BODY_GAP`` lines that carry no material on
        the way up — a blank line, the fragment a window cut short, an
        interleaved log line — because stopping at the first such line hands
        back the whole body: the lines above it are key material the
        dashboard is not supposed to show. The body starts at the topmost
        line of the block that carries material, so the noise below that top
        goes with it and the log above it is left alone.

        When nothing above the marker carries material the marker stands
        alone: a stray marker in an ordinary log redacts itself and nothing
        else.
        """
        if self._indexed is None:
            self._indexed = self._index()
        starts, material, blocks = self._indexed
        line = bisect_right(starts, marker) - 1
        index = bisect_left(material, line) - 1
        if index < 0:
            return starts[line]
        # The marker's own line is the bottom of the block, and the index
        # cannot know it: the noise between the marker and the material line
        # above it is checked here, once, exactly as the walk checked it.
        if line - material[index] - 1 > _BODY_GAP:
            return starts[line]
        return blocks[index]


def _pem_redaction_spans(text: str) -> list[tuple[int, int]]:
    """Return the private-key spans, including a block cut at either end."""
    spans: list[tuple[int, int]] = []
    begins = _PEM_BEGIN.finditer(text)
    ends = _PEM_END.finditer(text)
    begin = next(begins, None)
    end = next(ends, None)
    bodies = _Bodies(text)
    while begin is not None:
        while end is not None and end.start() < begin.start():
            spans.append((bodies.start(end.start()), end.end()))
            end = next(ends, None)
        if end is None:
            spans.append((begin.start(), len(text)))
        else:
            spans.append((begin.start(), end.end()))
            end = next(ends, None)
        begin = next(begins, None)
    while end is not None:
        spans.append((bodies.start(end.start()), end.end()))
        end = next(ends, None)
    if not spans:
        return []
    spans.sort()
    return sorted(spans)


def _redact_pem(text: str) -> str:
    """Redact every private-key block, whole or cut in half.

    A log tail is the last N bytes of a file, so a window can open inside a
    block: what is left is either the END marker with the base64 body leading
    into it, or the BEGIN marker with the body running past the window. Both
    halves are key material, so an unterminated BEGIN redacts the rest of the
    text and an orphan END redacts the body lines before it — including the
    body run on a line a CLI has put a log prefix in front of, and the log
    noise a CLI interleaved between the body and the marker. A pattern that
    requires both markers — which is what this replaces — hands back the body
    of every block the window cut in half.
    """
    spans = _pem_redaction_spans(text)
    if not spans:
        return text
    parts: list[str] = []
    cursor = 0
    for start, stop in spans:
        if stop <= cursor:
            continue
        parts.append(text[cursor : max(start, cursor)])
        parts.append(REDACTED)
        cursor = stop
    parts.append(text[cursor:])
    return "".join(parts)


# The words that make a UUID beside them a credential (a plain thread or ticket
# UUID is not one). The output guard refuses the same words followed by the
# start of a UUID, since the full-length rule below fires after the id is out.
ID_KEYWORDS = (
    "token|secret|key|session|auth|credential|password|passwd|signature|nonce|otp"
)

# Shared strict key/value rule. Report redaction consults this exact compiled
# rule before deciding whether a field label needs protection from the prose
# exception path, keeping its sensitive-label vocabulary aligned with logs.
_STRICT_KEY_VALUE_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9_.\-])"
    r"(?=[A-Za-z0-9_.\-]*(?:secret|token|password|passwd|session|cookie|credential|"
    r"signature|nonce|otp|api[-_]?key|access[-_]?key|private[-_]?key|"
    r"auth(?:orization)?|bearer))"
    r"[A-Za-z0-9_.\-]*+"
    r"\b([\"']?\s*[:=]\s*[\"']?)([^\s\"',;]{6,})"
)

# Longest-first so a specific provider pattern wins over a generic one.
_PROVIDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Provider credentials with a recognizable prefix.
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bdop_v1_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgx[sa]-[A-Za-z0-9]{16,}\b"),
    # JWTs: three base64url segments, header always starts with eyJ.
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    # Transport headers and scheme-prefixed tokens.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}"),
    re.compile(r"(?i)\bbasic\s+[A-Za-z0-9._\-+/=]{8,}"),
)

# A cookie header carries a session per `;`-separated pair, so the value is
# the rest of the line: stopping at the first `;` hands back later cookies.
_COOKIE_HEADER_PATTERN = re.compile(
    r"(?i)\b(?:set-)?cookie\b([\"']?\s*[:=]\s*[\"']?)[^\r\n]+"
)

# `key=value` where the name *contains* a credential word, not merely
# starts with one: AWS_SECRET_ACCESS_KEY and client_secret_key have
# qualifiers around the keyword that a keyword-anchored name skips. The
# name is anchored on its own character class rather than on `\b`, which
# also sits between a name character and the `-` or `.` inside one: a
# boundary-anchored scan retries the keyword at every character of a long
# dashed name, which is quadratic on a log full of them. The lookahead
# keeps that guarantee once the name is *full* of keywords: without it the
# name's leading `*` backtracks one character at a time and, at every position
# where the keyword then matches, the possessive suffix re-walks the rest of
# the name — `secret-` × 9,362 (a 64 KiB tail) cost 1.27 s that way. Asserting
# "this name run holds a keyword" once per run makes the pass linear again.
# Ids only look like credentials next to the words that carry them.
_ID_CONTEXT_PATTERN = re.compile(
    rf"(?i)\b({ID_KEYWORDS})\b[\"']?[\s:=]+[\"']?"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)

# Emails carry the operator's identity, which the fleet view has no use for.
_EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

_PATTERNS: tuple[re.Pattern[str], ...] = (
    *_PROVIDER_PATTERNS,
    _COOKIE_HEADER_PATTERN,
    _STRICT_KEY_VALUE_PATTERN,
    _ID_CONTEXT_PATTERN,
    _EMAIL_PATTERN,
)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Terminal escapes a CLI leaves in its log; stripped before the control sweep so
# the CSI/OSC introducer is still there to be matched.
_ANSI_ESCAPES = re.compile(
    r"\x1b(?:\[[0-9;:?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])"
)


# Terminal controls in a live model-output stream: the daemon is remote input
# and the CLI renders token text progressively, so this boundary drops every
# sequence form a terminal would honour (both 7-bit ESC and 8-bit C1) before
# the generic credential-shape scan and the length cap below.
_OUTPUT_OSC = re.compile(r"\x1b\].*?(?:\x07|\x9c|\x1b\\)", re.DOTALL)
_OUTPUT_ESC_STRING = re.compile(r"\x1b[PX^_].*?(?:\x9c|\x1b\\)", re.DOTALL)
_OUTPUT_C1_OSC = re.compile(r"\x9d.*?(?:\x07|\x9c|\x1b\\)", re.DOTALL)
_OUTPUT_C1_STRING = re.compile(r"[\x90\x98\x9e\x9f].*?(?:\x9c|\x1b\\)", re.DOTALL)
_OUTPUT_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OUTPUT_C1_CSI = re.compile(r"\x9b[0-?]*[ -/]*[@-~]")
_OUTPUT_OPEN_OSC = re.compile(r"\x1b\].*$", re.DOTALL)
_OUTPUT_OPEN_ESC_STRING = re.compile(r"\x1b[PX^_].*$", re.DOTALL)
_OUTPUT_OPEN_C1_OSC = re.compile(r"\x9d.*$", re.DOTALL)
_OUTPUT_OPEN_C1_STRING = re.compile(r"[\x90\x98\x9e\x9f].*$", re.DOTALL)
_OUTPUT_OPEN_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*$")
_OUTPUT_OPEN_C1_CSI = re.compile(r"\x9b[0-?]*[ -/]*$")
# Above this size sanitize_output_text takes the linear path for string controls.
_OUTPUT_LAZY_SCAN_MAX = 4096
_OUTPUT_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _strip_output_controls(text: str) -> str:
    """Remove terminal controls before any label inspection or shielding."""
    if len(text) > _OUTPUT_LAZY_SCAN_MAX:
        # The terminated-string patterns below scan to the end of the text from
        # every introducer, which is quadratic when introducers repeat without a
        # terminator. Past a modest size, drop from the first introducer on
        # (the "open" patterns are linear and are a superset of what the
        # terminated patterns remove), so no introducer is left to scan from.
        for pattern in (
            _OUTPUT_OPEN_OSC,
            _OUTPUT_OPEN_ESC_STRING,
            _OUTPUT_OPEN_C1_OSC,
            _OUTPUT_OPEN_C1_STRING,
        ):
            text = pattern.sub("", text)
    text = _OUTPUT_OSC.sub("", text)
    text = _OUTPUT_ESC_STRING.sub("", text)
    text = _OUTPUT_C1_OSC.sub("", text)
    text = _OUTPUT_C1_STRING.sub("", text)
    text = _OUTPUT_CSI.sub("", text)
    text = _OUTPUT_C1_CSI.sub("", text)
    text = _OUTPUT_OPEN_OSC.sub("", text)
    text = _OUTPUT_OPEN_ESC_STRING.sub("", text)
    text = _OUTPUT_OPEN_C1_OSC.sub("", text)
    text = _OUTPUT_OPEN_C1_STRING.sub("", text)
    text = _OUTPUT_OPEN_CSI.sub("", text)
    text = _OUTPUT_OPEN_C1_CSI.sub("", text)
    text = _OUTPUT_CONTROLS.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def sanitize_output_text(value: object, *, limit: int = 2000) -> str:
    """Strip terminal controls and credential-shaped spans from streamed text.

    Used on both sides of the daemon live-output channel: the daemon applies it
    to a complete vendor-CLI message *before* splitting it into transport
    chunks (so a credential or escape sequence cannot be split across two
    chunks and escape the scan), and the server applies it again to the stored
    event as the untrusted-input boundary.
    """
    text = _strip_output_controls(str(value or ""))
    # Streamed chunks are concatenated by the client: trimming each chunk's
    # edges would delete real whitespace at a transport boundary, and truncating
    # with the log-tail ellipsis would insert one mid-stream. Scan the complete
    # string, then bound it silently.
    cleaned = sanitize_log_text(text, max_length=0, strip=False)
    return cleaned[:limit] if limit > 0 else cleaned


_REPORT_AMBIGUOUS_JSON_KEY = re.compile(r"(?i)(?:^|[_.-])(?:session|token)(?:$|[_.-])")
_REPORT_AUTH_FAMILY = re.compile(
    r"(?i)(?:^|[_.-])(?:authorization|auth|bearer|cookie|set[-_]?cookie)(?:$|[_.-])"
)
_REPORT_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9._~+/=-]{16,}$")
_REPORT_AUTH_SCHEME = re.compile(r"(?i)(?:bearer|basic)(?=[ \t\r\n])")
_REPORT_SAFE_PROSE_VALUES = frozenset({"handling", "parsing", "processing"})
_REPORT_SMALL_INTEGER = re.compile(r"[0-9]{1,10}")
_REPORT_REDACTED_TAIL = re.compile(
    r"(?i)\[REDACTED\](?:[ \t]+(?:bearer|basic))?[ \t]+[^\s\"',;]+"
)
_REPORT_HEADER = re.compile(
    r"(?i)(?<![A-Za-z0-9_.@\-])(?P<name>[A-Za-z0-9_.\-]++)"
    r"(?P<separator>[\"']?[\s]*[:=][\s]*[\"']?)"
)


class _ReportJsonObject(list):
    """Retain JSON object pairs long enough to detect duplicate keys."""


class _ReportTextIndex:
    """Linear indexes for repeated field lookups in one report string."""

    def __init__(self, value: str) -> None:
        self.value = value
        self.terminators = [match.start() for match in re.finditer(r"[ \t\r\n\"',;]", value)]
        self.nonspace = [match.start() for match in re.finditer(r"\S", value)]
        self.non_token = [
            index
            for index, char in enumerate(value)
            if not (char.isascii() and (char.isalnum() or char in "._~+/=-"))
        ]
        newlines = [match.start() for match in re.finditer("\n", value)]
        self.line_starts = [0, *(position + 1 for position in newlines)]
        self.line_ends: list[int] = []
        for position in newlines:
            end = position - 1 if position > 0 and value[position - 1] == "\r" else position
            self.line_ends.append(end)
        self.line_ends.append(len(value))
        self.folded_ends = self.line_ends.copy()
        for index in range(len(newlines) - 1, -1, -1):
            next_start = self.line_starts[index + 1]
            if next_start < len(value) and value[next_start] in " \t":
                self.folded_ends[index] = self.folded_ends[index + 1]

    def value_start(self, position: int) -> int:
        index = bisect_left(self.nonspace, position)
        if index == len(self.nonspace):
            return len(self.value)
        start = self.nonspace[index]
        if self.value[start] in "\"'":
            index = bisect_left(self.nonspace, start + 1)
            return self.nonspace[index] if index < len(self.nonspace) else len(self.value)
        return start

    def value_end(self, start: int) -> int:
        index = bisect_left(self.terminators, start)
        return self.terminators[index] if index < len(self.terminators) else len(self.value)

    def line_index(self, position: int) -> int:
        return bisect_right(self.line_starts, position) - 1

    def line_end(self, position: int) -> int:
        return self.line_ends[self.line_index(position)]

    def folded_end(self, position: int) -> int:
        return self.folded_ends[self.line_index(position)]

    def has_nonspace(self, start: int, end: int) -> bool:
        index = bisect_left(self.nonspace, start)
        return index < len(self.nonspace) and self.nonspace[index] < end

    def token_shape(self, start: int, end: int) -> bool:
        length = end - start
        if length < 16:
            return False
        index = bisect_left(self.non_token, start)
        if index < len(self.non_token) and self.non_token[index] < end:
            return False
        if length >= 24:
            return True
        candidate = self.value[start:end]
        return (
            any(char.isdigit() for char in candidate)
            or any(char in "._~+/=-" for char in candidate)
            or any(char.isupper() for char in candidate)
        )


def _report_token_shape(value: str) -> bool:
    return bool(
        _REPORT_TOKEN_SHAPE.fullmatch(value)
        and (
            len(value) >= 24
            or any(char.isdigit() for char in value)
            or any(char in "._~+/=-" for char in value)
            or any(char.isupper() for char in value)
        )
    )


def _strict_report_label(name: str) -> bool:
    """Use the same key/value rule as the strict output sanitizer."""
    return name.casefold() in {"sid", "sessid", "sess_id"} or bool(
        _STRICT_KEY_VALUE_PATTERN.match(f"{name}=abcdef")
    )


def _report_plain_metadata_exception(
    name: str,
    value: str,
    index: _ReportTextIndex,
    start: int,
    end: int,
    *,
    explicit_scheme: bool = False,
) -> bool:
    """Keep only known benign report metadata cases; credentials still redact."""
    key = name.casefold()
    length = end - start
    if key in {"session", "token"}:
        return (
            not explicit_scheme
            and length <= 16
            and value[start:end].casefold() in _REPORT_SAFE_PROSE_VALUES
        )
    if key == "session_id" and end - start == 6 and value[start:end] == "abc123":
        return True
    if key == "token_count" and length <= 10:
        return bool(_REPORT_SMALL_INTEGER.fullmatch(value, start, end))
    if key == "auth_status" and length == len("failed"):
        return value[start:end].casefold() == "failed"
    # Short display names are useful report metadata. Keep the exception
    # value-bounded so a credential-shaped author value still reaches the
    # strict scanner.
    if key == "author" and length <= 5:
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", value[start:end]))
    return False


def _report_json_metadata_exception(name: str, value: object) -> bool:
    """Narrow value-aware exceptions for known benign JSON metadata."""
    key = name.casefold()
    if key in {"session", "token"} and isinstance(value, str):
        return value.casefold() in _REPORT_SAFE_PROSE_VALUES
    if key == "session_id" and value == "abc123":
        return True
    if key == "token_count":
        if type(value) is int:
            return 0 <= value <= 9_999_999_999
        if isinstance(value, str) and len(value) <= 10:
            return bool(_REPORT_SMALL_INTEGER.fullmatch(value))
    if key == "auth_status" and value == "failed":
        return True
    if key == "author" and isinstance(value, str) and len(value) <= 5:
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", value))
    return False


def _shield_report_allowlist(
    value: str,
    namespace: str,
    *,
    track_sources: bool,
    extra_shields: list[tuple[int, int]] | None = None,
) -> tuple[str, list[int] | None, list[int] | None, dict[str, str], list[tuple[int, int, str]]]:
    """Replace only approved false-positive labels, retaining their source spans."""
    index = _ReportTextIndex(value)
    shields: list[tuple[int, int, str]] = []
    replacements: dict[str, str] = {}
    for match in _REPORT_HEADER.finditer(value):
        if match.start("name") > 0 and value[match.start("name") - 1] in "+/=":
            continue
        name = match.group("name")
        if not _strict_report_label(name) and name.casefold() not in {
            "session", "token", "session_id", "token_count", "auth_status"
        }:
            continue
        start = index.value_start(match.end())
        end = index.value_end(start)
        scheme = _REPORT_AUTH_SCHEME.match(value, start)
        explicit_scheme = bool(
            scheme and index.has_nonspace(scheme.end(), index.folded_end(match.start()))
        )
        if not _report_plain_metadata_exception(
            name, value, index, start, end, explicit_scheme=explicit_scheme
        ):
            continue
        marker = f"\ue000{namespace}s{len(shields):x}\ue001"
        replacements[marker] = name
        shields.append((*match.span("name"), marker))

    for shield_start, shield_end in extra_shields or ():
        marker = f"\ue000{namespace}j{len(shields):x}\ue001"
        replacements[marker] = ""
        shields.append((shield_start, shield_end, marker))

    pieces: list[str] = []
    source_starts: list[int] | None = [] if track_sources else None
    source_ends: list[int] | None = [] if track_sources else None
    cursor = 0
    shields.sort(key=lambda item: item[0])
    for start, stop, marker in shields:
        if start < cursor:
            continue
        pieces.append(value[cursor:start])
        if track_sources:
            source_starts.extend(range(cursor, start))
            source_ends.extend(position + 1 for position in range(cursor, start))
        pieces.append(marker)
        if track_sources:
            source_starts.extend([start] * len(marker))
            source_ends.extend([stop] * len(marker))
        cursor = stop
    pieces.append(value[cursor:])
    if track_sources:
        source_starts.extend(range(cursor, len(value)))
        source_ends.extend(position + 1 for position in range(cursor, len(value)))
    return "".join(pieces), source_starts, source_ends, replacements, shields


def _strict_scan_with_source_spans(
    value: str,
    source_starts: list[int],
    source_ends: list[int],
    source_length: int,
) -> tuple[str, list[tuple[int, int]], list[tuple[int, int]]]:
    """Run the output scanner while mapping its redactions to original text."""
    value_diff = array("i", [0]) * (source_length + 1)
    key_diff = array("i", [0]) * (source_length + 1)
    text = value

    def map_interval(start: int, end: int) -> tuple[int, int] | None:
        if start >= end:
            return None
        # Source maps are monotone: copied characters advance by one, and a
        # shield/replacement maps every marker character to the same source
        # interval. Endpoints therefore cover the complete interval in O(1),
        # including nested PEM matches that start inside a prior replacement.
        return source_starts[start], source_ends[end - 1]

    def record(start: int, end: int, target: array) -> None:
        mapped = map_interval(start, end)
        if mapped is not None:
            left, right = mapped
            target[left] += 1
            target[right] -= 1

    def replace_spans(
        spans: list[tuple[int, int]],
        provenance_spans: list[tuple[int, int]] | None = None,
        key_provenance_spans: list[tuple[int, int]] | None = None,
    ) -> None:
        nonlocal text, source_starts, source_ends
        if not spans:
            return
        for start, end in provenance_spans if provenance_spans is not None else spans:
            record(start, end, value_diff)
        for start, end in key_provenance_spans or ():
            record(start, end, key_diff)
        pieces: list[str] = []
        new_starts: list[int] = []
        new_ends: list[int] = []
        cursor = 0
        for start, end in spans:
            if end <= cursor:
                continue
            visible_start = max(start, cursor)
            pieces.append(text[cursor:visible_start])
            new_starts.extend(source_starts[cursor:visible_start])
            new_ends.extend(source_ends[cursor:visible_start])
            pieces.append(REDACTED)
            mapped = map_interval(start, end)
            if mapped is None:
                marker_start = marker_end = 0
            else:
                marker_start, marker_end = mapped
            new_starts.extend([marker_start] * len(REDACTED))
            new_ends.extend([marker_end] * len(REDACTED))
            cursor = end
        pieces.append(text[cursor:])
        new_starts.extend(source_starts[cursor:])
        new_ends.extend(source_ends[cursor:])
        text = "".join(pieces)
        source_starts = new_starts
        source_ends = new_ends

    pem_spans = _pem_redaction_spans(text)
    replace_spans(pem_spans, pem_spans, pem_spans)
    for pattern in _PATTERNS:
        matches = list(pattern.finditer(text))
        spans: list[tuple[int, int]] = []
        value_provenance_spans: list[tuple[int, int]] = []
        key_provenance_spans: list[tuple[int, int]] = []
        for match in matches:
            if pattern is _STRICT_KEY_VALUE_PATTERN:
                value_provenance_spans.append(match.span(2))
            elif pattern is _COOKIE_HEADER_PATTERN:
                cookie_payload_span = (match.end(1), match.end())
                value_provenance_spans.append(match.span())
                key_provenance_spans.append(cookie_payload_span)
            else:
                value_provenance_spans.append(match.span())
                if pattern in _PROVIDER_PATTERNS or pattern is _EMAIL_PATTERN:
                    key_provenance_spans.append(match.span())
            spans.append(match.span())
        replace_spans(spans, value_provenance_spans, key_provenance_spans)

    def ranges_for(target: array) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        depth = 0
        start: int | None = None
        for position in range(source_length):
            depth += target[position]
            if depth and start is None:
                start = position
            elif not depth and start is not None:
                ranges.append((start, position))
                start = None
        if start is not None:
            ranges.append((start, source_length))
        return ranges

    return text, ranges_for(value_diff), ranges_for(key_diff)


def _json_value_source_spans(
    value: str,
) -> list[tuple[tuple[tuple[str, int], ...], int, int, bool]]:
    """Index JSON key and value tokens by source offsets and structural path."""
    decoder = json.JSONDecoder()
    tokens: list[tuple[tuple[tuple[str, int], ...], int, int, bool]] = []

    def skip_space(position: int) -> int:
        while position < len(value) and value[position].isspace():
            position += 1
        return position

    def parse(position: int, path: tuple[tuple[str, int], ...], depth: int) -> int:
        if depth > 64:
            raise RecursionError("report JSON exceeds source-span depth")
        position = skip_space(position)
        if position >= len(value):
            raise ValueError("unexpected end of report JSON")
        char = value[position]
        if char == "{":
            position = skip_space(position + 1)
            pair_index = 0
            if position < len(value) and value[position] == "}":
                return position + 1
            while True:
                key_start = position
                key, position = decoder.raw_decode(value, position)
                if not isinstance(key, str):
                    raise ValueError("report JSON object key is not a string")
                pair_path = path + (("object", pair_index),)
                tokens.append((pair_path, key_start, position, True))
                position = skip_space(position)
                if position >= len(value) or value[position] != ":":
                    raise ValueError("missing colon in report JSON")
                position = parse(position + 1, pair_path, depth + 1)
                pair_index += 1
                position = skip_space(position)
                if position < len(value) and value[position] == "}":
                    return position + 1
                if position >= len(value) or value[position] != ",":
                    raise ValueError("missing comma in report JSON object")
                position = skip_space(position + 1)
        if char == "[":
            position = skip_space(position + 1)
            item_index = 0
            if position < len(value) and value[position] == "]":
                return position + 1
            while True:
                position = parse(position, path + (("array", item_index),), depth + 1)
                item_index += 1
                position = skip_space(position)
                if position < len(value) and value[position] == "]":
                    return position + 1
                if position >= len(value) or value[position] != ",":
                    raise ValueError("missing comma in report JSON array")
                position = skip_space(position + 1)
        start = position
        _, end = decoder.raw_decode(value, position)
        tokens.append((path, start, end, False))
        return end

    end = parse(0, (), 0)
    if skip_space(end) != len(value):
        raise ValueError("trailing data after report JSON")
    return tokens


def _json_metadata_label_shields(
    value: object,
    tokens: list[tuple[tuple[tuple[str, int], ...], int, int, bool]],
) -> list[tuple[int, int]]:
    """Shield only parsed labels whose complete scalar is allow-listed metadata."""
    shields: list[tuple[int, int]] = []
    for path, start, end, is_key in tokens:
        if not is_key or not path or path[-1][0] != "object":
            continue
        parent = value
        for kind, index in path[:-1]:
            parent = parent[index][1] if kind == "object" else parent[index]
        name, scalar = parent[path[-1][1]]
        if name == "token_count" and _report_json_metadata_exception(name, scalar):
            shields.append((start + 1, end - 1))
    return shields


def _apply_json_source_redactions(
    value: object,
    tokens: list[tuple[tuple[tuple[str, int], ...], int, int, bool]],
    value_redactions: list[tuple[int, int]],
    key_redactions: list[tuple[int, int]],
) -> tuple[object, bool]:
    """Project strict raw-text spans onto the intersected JSON key/value tokens."""
    redacted_paths: set[tuple[tuple[tuple[str, int], ...], bool]] = set()
    value_span_index = 0
    key_span_index = 0
    for path, start, end, is_key in tokens:
        redactions = key_redactions if is_key else value_redactions
        span_index = key_span_index if is_key else value_span_index
        while span_index < len(redactions) and redactions[span_index][1] <= start:
            span_index += 1
        if is_key:
            key_span_index = span_index
        else:
            value_span_index = span_index
        if (
            span_index < len(redactions)
            and redactions[span_index][0] < end
            and redactions[span_index][1] > start
        ):
            redacted_paths.add((path, is_key))
    if not redacted_paths:
        return value, False
    changed = False
    for path, is_key in sorted(redacted_paths, key=lambda item: len(item[0]), reverse=True):
        if not path:
            value = REDACTED
            changed = True
            continue
        node = value
        for kind, index in path[:-1]:
            node = node[index][1] if kind == "object" else node[index]
        kind, index = path[-1]
        if kind == "object":
            key, old = node[index]
            new_key = REDACTED if is_key else key
            new_value = REDACTED if not is_key else old
            if (new_key, new_value) != (key, old):
                node[index] = (new_key, new_value)
        else:
            if node[index] != REDACTED:
                node[index] = REDACTED
        changed = True
    return value, changed


def _sanitize_report_plain(value: str) -> str:
    """Use the strict scanner while protecting non-credential field labels."""
    value = _strip_output_controls(value)
    namespace = uuid.uuid4().hex
    while namespace in value:
        namespace = uuid.uuid4().hex
    shielded, _, _, replacements, shields = _shield_report_allowlist(
        value, namespace, track_sources=False
    )
    cleaned = sanitize_output_text(shielded, limit=0)
    if replacements:
        restore = re.compile(r"\ue000" + re.escape(namespace) + r"[sl][0-9a-f]+\ue001")
        cleaned = restore.sub(lambda match: replacements[match.group()], cleaned)
    # Strict scanning can consume a short auth label while leaving a short
    # credential immediately after its marker. Remove that remainder in one
    # pass without changing the text that the strict scanner saw.
    cleaned = _REPORT_REDACTED_TAIL.sub(REDACTED, cleaned)
    # Keep the strict scanner's original anchors intact. Report-specific
    # folding and short-header handling runs only after strict redaction.
    cleaned = _redact_report_sensitive_fields(
        cleaned, _ReportTextIndex(cleaned), namespace, replacements
    )
    if replacements:
        restore = re.compile(r"\ue000" + re.escape(namespace) + r"[sl][0-9a-f]+\ue001")
        cleaned = restore.sub(lambda match: replacements[match.group()], cleaned)
    strict_field_spans: list[tuple[int, int]] = []
    for match in _REPORT_HEADER.finditer(cleaned):
        name = match.group("name")
        if not _strict_report_label(name) or name.casefold().replace("-", "").replace("_", "") in {
            "authorization", "auth", "bearer", "cookie", "setcookie"
        }:
            continue
        value_start = match.end()
        while value_start < len(cleaned) and cleaned[value_start].isspace():
            value_start += 1
        if cleaned.startswith(REDACTED, value_start):
            strict_field_spans.append((match.start("name"), value_start + len(REDACTED)))
    if strict_field_spans:
        parts = []
        cursor = 0
        for start, end in strict_field_spans:
            if start < cursor:
                continue
            parts.extend((cleaned[cursor:start], REDACTED))
            cursor = end
        parts.append(cleaned[cursor:])
        cleaned = "".join(parts)
    if re.fullmatch(rf"session\s*[:=]\s*{re.escape(REDACTED)}", cleaned, re.IGNORECASE):
        cleaned = REDACTED
    return cleaned


def _redact_report_sensitive_fields(
    value: str,
    index: _ReportTextIndex,
    namespace: str,
    replacements: dict[str, str],
) -> str:
    """Mask strict credential fields using precomputed value and line indexes."""
    spans: list[tuple[int, int]] = []
    labels: list[tuple[int, int, str]] = []
    safe_auth_labels: dict[str, str] = {}
    for match in _REPORT_HEADER.finditer(value):
        name = match.group("name")
        strict_label = _strict_report_label(name)
        bare_ambiguous = name.casefold() in {"session", "token"}
        if not strict_label and not bare_ambiguous:
            continue
        value_start = index.value_start(match.end())
        value_end = index.value_end(value_start)
        scheme = _REPORT_AUTH_SCHEME.match(value, value_start)
        folded_end = index.folded_end(match.start())
        explicit_scheme = bool(
            scheme and index.has_nonspace(scheme.end(), folded_end)
        )
        if _report_plain_metadata_exception(
            name, value, index, value_start, value_end, explicit_scheme=explicit_scheme
        ):
            continue
        if not strict_label and not explicit_scheme:
            continue
        if _REPORT_AUTH_FAMILY.search(name):
            marker = f"\ue000{namespace}l{len(labels):x}\ue001"
            safe_name = safe_auth_labels.get(name)
            if safe_name is None:
                safe_name = sanitize_output_text(name, limit=0)
                safe_auth_labels[name] = safe_name
            replacements[marker] = safe_name
            labels.append((*match.span("name"), marker))
        if value_start >= folded_end:
            continue
        spans.append((value_start, folded_end))
    if not spans and not labels:
        return value
    pieces: list[str] = []
    cursor = 0
    span_index = label_index = 0
    while span_index < len(spans) or label_index < len(labels):
        if label_index >= len(labels) or (
            span_index < len(spans) and spans[span_index][0] <= labels[label_index][0]
        ):
            start, end = spans[span_index]
            replacement = REDACTED
            span_index += 1
        else:
            start, end, replacement = labels[label_index]
            label_index += 1
        if start < cursor:
            continue
        pieces.extend((value[cursor:start], replacement))
        cursor = end
    pieces.append(value[cursor:])
    return "".join(pieces)


def _sanitize_report_json(
    value: object, *, depth: int = 0, force_secret: bool = False
) -> tuple[object, bool]:
    if depth > 64:
        raise RecursionError("report JSON exceeds sanitizer depth")
    if force_secret and isinstance(value, str):
        return (REDACTED, True) if value else (value, False)
    if force_secret and isinstance(value, (int, float)) and not isinstance(value, bool):
        return REDACTED, True
    changed = False
    if isinstance(value, (_ReportJsonObject, dict)):
        result: dict[object, object] = {}
        pairs = value if isinstance(value, _ReportJsonObject) else value.items()
        for key, item in pairs:
            new_key = _sanitize_report_plain(key) if isinstance(key, str) else key
            if new_key != key:
                new_key = REDACTED
                changed = True
            if isinstance(key, str):
                safe_key = _strip_output_controls(key)
                normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", safe_key)
                sensitive_label = _strict_report_label(safe_key)
                benign_metadata = _report_json_metadata_exception(safe_key, item)
                if sensitive_label and not benign_metadata and (
                    (isinstance(item, str) and item)
                    or (isinstance(item, (int, float)) and not isinstance(item, bool))
                ):
                    result[new_key] = REDACTED
                    changed = True
                    continue
                if sensitive_label and not benign_metadata and isinstance(item, (dict, list)):
                    new_item, _ = _sanitize_report_json(
                        item, depth=depth + 1, force_secret=True
                    )
                    result[new_key] = new_item
                    changed = True
                    continue
                if (
                    isinstance(item, str)
                    and _REPORT_AMBIGUOUS_JSON_KEY.search(normalized)
                    and _report_token_shape(item)
                    and not benign_metadata
                ):
                    result[new_key] = REDACTED
                    changed = True
                    continue
            new_item, item_changed = _sanitize_report_json(
                item, depth=depth + 1, force_secret=force_secret
            )
            if new_key in result:
                # JSON object semantics are last-wins. Mark duplicate inputs as
                # changed so the raw source is never returned with a shadowed value.
                changed = True
            result[new_key] = new_item
            changed = changed or item_changed
        return result, changed
    if isinstance(value, list):
        items = [
            _sanitize_report_json(item, depth=depth + 1, force_secret=force_secret)
            for item in value
        ]
        return [item for item, _ in items], any(item_changed for _, item_changed in items)
    if isinstance(value, str):
        cleaned = _sanitize_report_plain(value)
        return cleaned, cleaned != value
    return value, False


def sanitize_report_text(value: object, *, limit: int = 0) -> str:
    """Redact report text without changing live-stream/log sanitizer behavior.

    Valid JSON is scanned by value so credential redaction keeps its structure.
    Ordinary text uses the established provider, PEM, cookie and credential
    scanner, with a narrow exception for a known prose false positive.
    """
    text = str(value or "")
    text = _strip_output_controls(text)
    try:
        parsed = json.loads(text, object_pairs_hook=_ReportJsonObject)
        namespace = uuid.uuid4().hex
        while namespace in text:
            namespace = uuid.uuid4().hex
        json_tokens = _json_value_source_spans(text)
        shielded, source_starts, source_ends, _, _ = _shield_report_allowlist(
            text,
            namespace,
            track_sources=True,
            extra_shields=_json_metadata_label_shields(parsed, json_tokens),
        )
        _, value_redactions, key_redactions = _strict_scan_with_source_spans(
            shielded, source_starts, source_ends, len(text)
        )
        parsed, source_changed = _apply_json_source_redactions(
            parsed, json_tokens, value_redactions, key_redactions
        )
        cleaned, changed = _sanitize_report_json(parsed)
        changed = changed or source_changed
    except json.JSONDecodeError:
        cleaned_json = None
    except (ValueError, RecursionError):
        cleaned_json = json.dumps(REDACTED)
    else:
        cleaned_json = (
            json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
            if changed
            else text
        )
    result = cleaned_json if cleaned_json is not None else _sanitize_report_plain(text)
    return result[:limit] if limit > 0 else result


def sanitize_log_text(value: str, *, max_length: int = 2000, strip: bool = True) -> str:
    """Return ``value`` with credential-shaped spans replaced and length bounded.

    Control characters are dropped so a log tail cannot smuggle terminal escapes
    into a client that renders it, and the result is truncated on a character
    boundary with an ellipsis rather than cut mid-escape-sequence. ``strip`` and
    a zero ``max_length`` let a streamed-text caller scan a whole chunk without
    losing boundary whitespace or inserting an ellipsis mid-message.
    """
    if not isinstance(value, str) or not value:
        return ""
    text = _ANSI_ESCAPES.sub("", value)
    text = _CONTROL_CHARS.sub("", text)
    text = _redact_pem(text)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    if strip:
        text = text.strip()
    if max_length > 0 and len(text) > max_length:
        return text[: max_length - 1].rstrip() + "…"
    return text


def sanitize_log_tail(raw: bytes, *, max_length: int = 2000) -> str:
    """Sanitize the decoded tail of one log file, tolerating partial UTF-8."""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return ""
    return sanitize_log_text(
        bytes(raw).decode("utf-8", errors="replace"), max_length=max_length
    )
