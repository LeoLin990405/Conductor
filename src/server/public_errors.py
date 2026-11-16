"""Public error text and correlation ids, without importing the web app.

The local daemon and the HTTP 500 handler share these helpers. This module
lives outside ``src.server.app`` on purpose: importing anything under that
package builds the whole FastAPI app (its ``__init__`` imports ``setup``),
which a daemon formatting a failure report must not do.

The traceback never goes into public text; ``log_failure`` writes it to
the log next to the same correlation id.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Long enough to say what failed, short enough that a cut cannot be the
# way a secret is shortened into something that still looks like itself.
PUBLIC_MESSAGE_LIMIT = 500

_TRACEBACK_MARK = "Traceback (most recent call last)"
# Request trace ids are UUID text. Daemon ids are hex. Either may be
# echoed in a header, so anything else is rejected rather than trusted.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
# A same-line credential value in a sentence a client can see. The
# diagnostic redactor stops at a comma, a semicolon, or an ampersand so
# the next clause stays. A secret may contain those characters, and the
# clause that stays is the rest of the secret. This value runs through
# them and still stops at a space or a brace, so the words and the JSON
# around it stay.
_PUBLIC_VALUE_RE = re.compile(
    r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\s\"'})\]]+"
)


# --- Hardening passes ------------------------------------------------------
# The diagnostic redactors below are blocklists tuned for vendor CLI output.
# Text that leaves this host (a daemon report) also goes through these
# passes first. Each one errs on the side of removing too much.

# Vendor token shapes whose prefix alone says "secret".
_TOKEN_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"github_pat_[A-Za-z0-9_]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|glpat-[A-Za-z0-9_\-]{16,}"
    r"|hf_[A-Za-z0-9]{16,}"
    r"|npm_[A-Za-z0-9]{16,}"
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}"
    r"|sk-[A-Za-z0-9_\-]{8,}"
    r")"
)

# A URL with userinfo. ``\S*`` is greedy, so the userinfo runs up to the
# last ``@`` of the URL: a password may contain ``/``, ``:`` or ``@``.
_DSN_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]{0,31}://)\S*@")

# One query parameter: ``?name=value`` or ``&name=value`` (``;`` too).
_QUERY_PARAM_RE = re.compile(r"(?P<lead>[?&;])(?P<name>[A-Za-z0-9_.\-]{1,64})=(?P<value>[^&;#\s\"'<>]*)")
_SECRET_QUERY_NAME_RE = re.compile(
    r"(?i)^(?:x-amz-[a-z\-]+|x-goog-[a-z\-]+|sig|signature|se|sp|sv|skoid|sktid"
    r"|token|access_token|refresh_token|id_token|auth|authorization|code|state"
    r"|key|apikey|api_key|api-key|secret|client_secret|password|passwd|pass|pwd"
    r"|credential|credentials|session|sessionid|session_id|jwt|hmac|policy)$"
)

# A key=value credential: ``pass=``, ``"password": "…"``, ``db_secret : x``.
_SECRET_KEY_VALUE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?P<q>[\"']?)(?P<key>(?:[A-Za-z0-9]+[_.\-])*"
    r"(?:passwd|password|pass|pwd|secret|token|api[_\-]?key|access[_\-]?key))"
    r"(?P=q)(?![A-Za-z0-9])(?P<sep>[ \t]*[:=][ \t]*)(?!\[redacted\])"
    r"(?P<value>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\s\"'})\]]+)"
)

_PARAMETERS_MARK_RE = re.compile(r"\[parameters:", re.IGNORECASE)

# ``connection to server at "10.0.0.1", port 5432 failed: … for user "app"``
_DB_SERVER_RE = re.compile(r"(?i)(\bserver at )\"[^\"]*\"(?: \([^)]*\))?")
_DB_USER_RE = re.compile(r"(?i)(\b(?:for user|role|user)\s+)\"[^\"]*\"")

_HOME_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?:/(?:Users|home)/[^/\s\"':;,)\]]+|[A-Za-z]:\\Users\\[^\\\s\"':;,)\]]+)"
)
_IPV4_RE = re.compile(
    r"(?<![0-9.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?![0-9.])"
)


def _high_entropy(value: str) -> bool:
    """A query value long and varied enough to be a signature or token."""
    if len(value) < 16:
        return False
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    total = len(value)
    entropy = -sum(n / total * math.log2(n / total) for n in counts.values())
    return entropy >= 3.0


def _scrub_query_params(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        value = match.group("value")
        if not value:
            return match.group(0)
        if _SECRET_QUERY_NAME_RE.match(name) or _high_entropy(value):
            return f"{match.group('lead')}{name}=[redacted]"
        return match.group(0)

    return _QUERY_PARAM_RE.sub(replace, text)


def _drop_sql_parameters(text: str) -> str:
    """Remove each ``[parameters: …]`` block, bracket- and quote-aware.

    An unterminated block runs to the end of the text: the bound values
    are what a statement failed on, and none of them is safe to show.
    """
    out: list[str] = []
    pos = 0
    while True:
        match = _PARAMETERS_MARK_RE.search(text, pos)
        if match is None:
            out.append(text[pos:])
            return "".join(out)
        out.append(text[pos : match.start()])
        out.append("[parameters: [redacted]]")
        depth = 1
        quote = ""
        i = match.end()
        while i < len(text) and depth:
            ch = text[i]
            if quote:
                if ch == "\\":
                    i += 1
                elif ch == quote:
                    quote = ""
            elif ch in "\"'":
                quote = ch
            elif ch in "[(":
                depth += 1
            elif ch in "])":
                depth -= 1
            i += 1
        pos = i


def _replace_home(text: str) -> str:
    try:
        home = str(Path.home())
    except (RuntimeError, OSError, KeyError):
        home = ""
    if len(home) > 1:
        text = text.replace(home.rstrip("/\\"), "~")
    return _HOME_PREFIX_RE.sub("~", text)


def harden_public_text(text: str) -> str:
    """The extra passes for text that leaves the host. See module notes."""
    text = _drop_sql_parameters(text)
    text = _DSN_USERINFO_RE.sub(r"\1[redacted]@", text)
    text = _TOKEN_PREFIX_RE.sub("[redacted]", text)
    text = _scrub_query_params(text)
    text = _SECRET_KEY_VALUE_RE.sub(
        lambda m: f"{m.group('q')}{m.group('key')}{m.group('q')}{m.group('sep')}[redacted]",
        text,
    )
    text = _DB_SERVER_RE.sub(r'\1"<host>"', text)
    text = _DB_USER_RE.sub(r'\1"<user>"', text)
    text = _replace_home(text)
    return _IPV4_RE.sub("<ip>", text)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def usable_correlation_id(value: object) -> str | None:
    """An id safe to put in a header and a log line, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _SAFE_ID_RE.fullmatch(text):
        return text
    return None


def _without_traceback(text: str) -> str:
    """Drop a pasted traceback and keep the line breaks of what remains.

    A credential value is allowed to occupy the next line (``password=``
    then the secret). Joining those lines with a space before redaction
    ends the value at the first comma, and the rest of the secret stays
    in the sentence. Frame lines go; the breaks between the others stay.
    """
    if _TRACEBACK_MARK in text:
        text = text.split(_TRACEBACK_MARK, 1)[0]
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('File "') and ", line " in stripped:
            continue
        kept.append(line)
    return "\n".join(kept)


def _scrub_complete_credential_values(text: str) -> str:
    """Replace a same-line credential value, including what follows `,`, `;`, or `&`.

    Those three characters end a pair in the diagnostic redactor. Publishing
    that cut leaves the tail of the secret in the sentence
    (``password=[redacted],the-rest``). The value here is the whole run.
    A value that already crossed onto the next line is left for the
    diagnostic pass, which takes that line whole.
    """
    from src.llms.native_cli.failure import (
        _AUTHORIZATION_KEY_RE,
        _CREDENTIAL_PAIR_RE,
        _REDACTED,
        _is_count_key,
        _is_credential_key,
        _looks_like_a_count,
    )

    pieces: list[str] = []
    pos = 0
    for match in _CREDENTIAL_PAIR_RE.finditer(text):
        if match.start() < pos:
            continue
        key = match.group("key")
        if not _is_credential_key(key) or _AUTHORIZATION_KEY_RE.search(key):
            continue
        separator = match.group("sep")
        if "\n" in separator or "\r" in separator:
            continue
        value_start = match.end()
        value_match = _PUBLIC_VALUE_RE.match(text, value_start)
        if value_match is None:
            continue
        value = value_match.group(0)
        if text[value_start : value_start + len(_REDACTED)].lower() == _REDACTED:
            # Already taken out by the hardening pass.
            continue
        if _is_count_key(key) and _looks_like_a_count(value):
            continue
        pieces.append(text[pos : match.start()])
        pieces.append(text[match.start() : value_start])
        pieces.append(_REDACTED)
        pos = value_match.end()
    pieces.append(text[pos:])
    return "".join(pieces)


def scrub_public_message(
    text: str,
    *,
    limit: int = PUBLIC_MESSAGE_LIMIT,
    fallback: str = "Internal server error",
) -> str:
    """Credential-shaped text removed first, then cut, so a cut keeps no secret."""
    from src.llms.native_cli.failure import redact_credentials
    from src.server.utils.error_sanitization import sanitize_error_text, single_line

    # Newlines stay through both scrubbers. single_line is what flattens,
    # and it runs only after a value that crossed a line has been removed.
    # The complete-value pass runs first: the diagnostic redactor would
    # otherwise keep the tail after a comma, a semicolon, or an ampersand.
    prepared = _scrub_complete_credential_values(
        harden_public_text(_without_traceback(text).replace("\x00", ""))
    )
    redacted = redact_credentials(prepared)
    if not isinstance(redacted, str):
        redacted = ""
    # The hardening runs again after the other passes: neither of them
    # adds a home path or an address, but a second pass is cheap and keeps
    # this independent of how those passes rewrite the text.
    cleaned = single_line(harden_public_text(sanitize_error_text(redacted))).strip()
    if not cleaned:
        return fallback
    if len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned


def public_exception_message(
    exc: BaseException,
    *,
    limit: int = PUBLIC_MESSAGE_LIMIT,
    fallback: str = "Internal server error",
) -> str:
    return scrub_public_message(str(exc), limit=limit, fallback=fallback)


def log_failure(
    correlation_id: str,
    exc: BaseException,
    *,
    log: logging.Logger | None = None,
    where: str = "unhandled exception",
    detail: str = "",
) -> None:
    """Write the traceback with ``correlation_id``. ``detail`` is already safe."""
    target = logger if log is None else log
    exc_info = (type(exc), exc, exc.__traceback__)
    if detail:
        target.error(
            "%s correlation_id=%s type=%s detail=%s",
            where,
            correlation_id,
            type(exc).__name__,
            detail,
            exc_info=exc_info,
        )
        return
    target.error(
        "%s correlation_id=%s type=%s",
        where,
        correlation_id,
        type(exc).__name__,
        exc_info=exc_info,
    )
