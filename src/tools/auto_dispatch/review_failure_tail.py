"""The text a failed review is classified from (N63, N63-1).

A review that fails is classified with the native-CLI classifier, but only
from the trailing lines of the error: stderr can echo the code under review
or the prompt, and a quota phrase in it must not read as the CLI's own quota
message.

Limit: only the last :data:`REVIEW_FAILURE_TAIL_LINES` non-empty lines (and
at most :data:`REVIEW_FAILURE_TAIL_CHARS` characters of them) are read. A CLI
that prints a footer of three or more lines after its own error line (a usage
hint, a log path, a banner) pushes that error line out of the window, and the
failure is then unclassified: a generic review failure, not a quota skip.

The adapters put their own prefix in front of the CLI's output. Only those
known prefixes are taken off, and only from the message's first line: a
message such as ``insufficient_quota: ...`` keeps its first word, which a
"cut at the first colon" rule would drop.
"""

from __future__ import annotations

import re

REVIEW_FAILURE_TAIL_LINES = 3
REVIEW_FAILURE_TAIL_CHARS = 500

# The prefixes the native-CLI layer itself writes in front of a CLI's output:
# ``native CLI exited <code>: <stderr>`` (``process.py``) and
# ``<name> produced no final answer: <stderr>`` (``codex.py``; the other
# adapters print the sentence with no detail after it).
_ADAPTER_PREFIXES = (
    re.compile(r"^native cli exited -?\d+:\s*", re.IGNORECASE),
    re.compile(r"^[a-z0-9][a-z0-9_.-]{0,40} produced no final answer:\s*", re.IGNORECASE),
)


def strip_adapter_prefix(line: str) -> str:
    """``line`` without a known adapter prefix; the line itself when nothing is left."""
    for pattern in _ADAPTER_PREFIXES:
        match = pattern.match(line)
        if match is not None:
            rest = line[match.end() :].strip()
            return rest or line
    return line


def review_failure_text(message: str) -> str:
    """The trailing error lines of a failed review, ready for the classifier."""
    lines = [part.strip() for part in str(message or "").splitlines() if part.strip()]
    tail = lines[-REVIEW_FAILURE_TAIL_LINES:]
    if tail and len(lines) <= REVIEW_FAILURE_TAIL_LINES:
        # The message's first line is in the window: that is where the
        # adapter's prefix sits.
        tail[0] = strip_adapter_prefix(tail[0])
    return "\n".join(tail)[-REVIEW_FAILURE_TAIL_CHARS:]


__all__ = [
    "REVIEW_FAILURE_TAIL_CHARS",
    "REVIEW_FAILURE_TAIL_LINES",
    "review_failure_text",
    "strip_adapter_prefix",
]
