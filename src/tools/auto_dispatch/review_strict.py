"""Strict reviewer-output handling: one schema-led retry, then INCONCLUSIVE.

Reviewer root sessions often reply with prose or nothing at all. Neither is a
review of the author's work, so neither may become PASS or FAIL. This module
holds the pieces the drive wires around ``CrossReviewer.review_line``:

* :func:`classify_reply` -- ``empty``, ``prose`` or ``valid``, decided by the
  same parsers ``execution.py`` uses (nothing is re-implemented here).
* :func:`strict_retry_note` -- the one correction sent after an empty or prose
  reply, built from ``verdict.schema.json`` so it cannot drift from the schema.
* :func:`inconclusive_verdict` -- what a reply that is still unusable after the
  retry becomes: ``passed`` false with N59 ``status: "inconclusive"``.
* :func:`record_attempt` -- a bounded, JSON-safe record on the line snapshot.

Changing the outcome ships behind ``CONDUCTOR_DISPATCH_REVIEW_STRICT_MODE``
(``off|shadow|enforce``, default ``shadow``). In shadow the caller keeps
today's behaviour and only records what enforce would have done; every recorded
attempt also writes one ``shadow_decision`` log line (``would_change`` only for
the reply enforce would park), so a rollout review has both the changed and
the unchanged judgements to count.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime

from src.utils.shadow_log import shadow_decision

from .liveness.modes import SHADOW, normalize_mode
from .review_schema import load_verdict_schema
from .review_triage import INCONCLUSIVE, with_status
from .types import Verdict

logger = logging.getLogger(__name__)

__all__ = [
    "EMPTY",
    "INCONCLUSIVE_PREFIX",
    "KINDS",
    "MODE_ENV",
    "PROSE",
    "RECORD_KEY",
    "VALID",
    "WOULD_INCONCLUSIVE",
    "classify_reply",
    "inconclusive_verdict",
    "is_strict_inconclusive",
    "record_attempt",
    "strict_mode",
    "strict_retry_note",
]

MODE_ENV = "CONDUCTOR_DISPATCH_REVIEW_STRICT_MODE"

EMPTY = "empty"
PROSE = "prose"
VALID = "valid"
KINDS = (EMPTY, PROSE, VALID)

#: Line-snapshot key of the strict-review attempt log.
RECORD_KEY = "review_strict"
#: The one blocker an INCONCLUSIVE strict verdict carries starts with this.
INCONCLUSIVE_PREFIX = "Reviewer output was not a valid verdict"
#: The attempt outcome the drive records in shadow: enforce would have parked
#: the line ``review_inconclusive`` instead of asking the author for a fix round.
WOULD_INCONCLUSIVE = "would_inconclusive"

_MAX_ATTEMPTS = 32
_MAX_CHARS = 200
_NEEDED_EVIDENCE = "a reviewer reply that is one JSON object matching verdict.schema.json"

# A reply that is only a code fence with nothing inside it.
_EMPTY_FENCE = re.compile(r"^```[\w+-]*\s*(?:```)?$")


def strict_mode(environ: Mapping[str, str] | None = None) -> str:
    """``off``, ``shadow`` or ``enforce``; ``shadow`` when unset or misspelt."""
    env = os.environ if environ is None else environ
    return normalize_mode(env.get(MODE_ENV, ""), source=MODE_ENV)


def _is_empty(raw: str | Mapping[str, object] | None) -> bool:
    if raw is None:
        return True
    if isinstance(raw, Mapping):
        return not raw
    text = str(raw).strip()
    return not text or bool(_EMPTY_FENCE.match(text))


def classify_reply(raw: str | Mapping[str, object] | None) -> str:
    """``empty``, ``prose`` or ``valid`` for one raw reviewer reply.

    ``valid`` is exactly what ``CrossReviewer.review_line`` would accept: a
    schema-valid structured object, or a text reply whose verdict object
    parses (``verdict_unparseable`` false).
    """
    from .execution import _parse_verdict, _structured_verdict, verdict_unparseable

    if _is_empty(raw):
        return EMPTY
    if _structured_verdict(raw) is not None:
        return VALID
    verdict = _parse_verdict(str(raw))
    if verdict.passed or not verdict_unparseable(verdict):
        return VALID
    return PROSE


def _type_name(spec: object) -> str:
    if not isinstance(spec, Mapping):
        return "value"
    kind = spec.get("type")
    if kind == "array":
        return f"array of {_type_name(spec.get('items'))}s"
    return str(kind or "value")


def _example_value(spec: object) -> object:
    """The smallest value of ``spec``'s type; INCONCLUSIVE never reads as a pass."""
    if not isinstance(spec, Mapping):
        return None
    kind = spec.get("type")
    allowed = spec.get("enum")
    if kind == "string" and isinstance(allowed, list) and allowed:
        return INCONCLUSIVE if INCONCLUSIVE in allowed else allowed[0]
    if kind == "string":
        return "one line"
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object":
        return {}
    return None


def _schema_parts() -> tuple[list[tuple[str, str]], list[str], dict[str, object]]:
    schema = load_verdict_schema()
    properties = schema.get("properties")
    required = schema.get("required")
    props = properties if isinstance(properties, Mapping) else {}
    names = [str(name) for name in required] if isinstance(required, list) else []
    keys = [(name, _type_name(props.get(name))) for name in names]
    status = props.get("status")
    enum = status.get("enum") if isinstance(status, Mapping) else None
    statuses = [str(item) for item in enum] if isinstance(enum, list) else []
    example = {name: _example_value(props.get(name)) for name in names}
    return keys, statuses, example


def strict_example() -> dict[str, object]:
    """The minimal example object the retry note shows (it validates)."""
    return _schema_parts()[2]


def strict_retry_note(kind: str) -> str:
    """The correction sent once after an ``empty`` or ``prose`` reply.

    Built from ``verdict.schema.json``. It carries no reviewed material; the
    caller appends it to the original prompt (with a blank line before it).
    """
    if kind == EMPTY:
        wrong = "Your previous reply was empty."
    elif kind == PROSE:
        wrong = "Your previous reply was prose with no valid verdict object."
    else:
        wrong = "Your previous reply had no valid verdict object."
    keys, statuses, example = _schema_parts()
    fields = "; ".join(f'"{name}" ({kind_name})' for name, kind_name in keys)
    status_text = " | ".join(f'"{item}"' for item in statuses)
    return (
        f"STRICT FORMAT. {wrong} Reply again: no prose before or after, "
        "one JSON object only, and no keys beyond these. Required keys: "
        f"{fields}. \"status\" must be one of {status_text}. Example of a valid "
        f"reply: {json.dumps(example, separators=(', ', ': '))}"
    )


def inconclusive_verdict(kind: str, reviewers: Sequence[str]) -> Verdict:
    """The verdict for a reply still unusable after the strict retry.

    ``passed`` is false, ``status`` is ``inconclusive``; it is neither a MERGE
    nor a fix round for the author.
    """
    names: list[str] = []
    for name in reviewers or ():
        text = str(name or "").strip()[:_MAX_CHARS]
        if text and text not in names:
            names.append(text)
    who = ", ".join(names) if names else "unknown"
    line = (
        f"{INCONCLUSIVE_PREFIX} ({str(kind or 'unknown')[:_MAX_CHARS]}) after a strict "
        f"retry; reviewers: {who}"
    )
    base = Verdict(passed=False, blockers=[line], verdict=INCONCLUSIVE)
    return with_status(base, {"status": INCONCLUSIVE, "needed_evidence": [_NEEDED_EVIDENCE]})


def is_strict_inconclusive(verdict: object) -> bool:
    """True for a verdict made by :func:`inconclusive_verdict`."""
    if verdict is None or getattr(verdict, "passed", True):
        return False
    if getattr(verdict, "status", "") != INCONCLUSIVE:
        return False
    blockers = list(getattr(verdict, "blockers", []) or [])
    return len(blockers) == 1 and str(blockers[0]).startswith(INCONCLUSIVE_PREFIX)


def _safe(value: object) -> str:
    return str(value if value is not None else "")[:_MAX_CHARS]


def _stamp(now: object) -> object:
    if isinstance(now, datetime):
        return now.isoformat()
    if isinstance(now, bool):
        return str(now)
    if isinstance(now, (int, float)):
        return float(now)
    return _safe(now)


def record_attempt(
    snapshot: dict,
    *,
    reviewer: str,
    kind: str,
    retried: bool,
    outcome: str,
    mode: str,
    now: object,
) -> None:
    """Append one attempt to ``snapshot[RECORD_KEY]``; newest 32 are kept."""
    if not isinstance(snapshot, dict):
        return
    cell = snapshot.get(RECORD_KEY)
    if not isinstance(cell, dict):
        cell = {}
    attempts = cell.get("attempts")
    if not isinstance(attempts, list):
        attempts = []
    entry = {
        "reviewer": _safe(reviewer),
        "kind": _safe(kind),
        "retried": bool(retried),
        "outcome": _safe(outcome),
        "mode": _safe(mode),
        "at": _stamp(now),
    }
    attempts.append(entry)
    del attempts[:-_MAX_ATTEMPTS]
    total = cell.get("total")
    snapshot[RECORD_KEY] = {
        "mode": entry["mode"],
        "attempts": attempts,
        "total": (total if isinstance(total, int) and not isinstance(total, bool) else 0) + 1,
    }
    if entry["mode"] == SHADOW:
        # One line per shadow judgement: every attempt, not only the one
        # enforce would change. A usable reply and an unusable first reply are
        # the denominator T4 divides by (``shadow_log``'s table); only the
        # ``would_inconclusive`` outcome flips the result -- enforce parks
        # review_inconclusive there, shadow parks review_unparseable (a fix
        # round). The record above is unchanged; only the log says what
        # enforce would have done.
        current, enforced, reason = _SHADOW_OUTCOMES.get(
            str(entry["outcome"]),
            ("record_attempt", "record_attempt", f"outcome_{entry['outcome']}"),
        )
        shadow_decision(
            logger,
            "review_strict",
            would_change=entry["outcome"] == WOULD_INCONCLUSIVE,
            subject=snapshot.get("line_id"),
            current=current,
            enforced=enforced,
            reason=reason,
            reviewer=entry["reviewer"],
            kind=entry["kind"],
            retried=str(entry["retried"]).lower(),
        )


#: What the outcome means for the line: (current, enforced, reason). Enforce
#: parks only when the retried reply is still unusable.
_SHADOW_OUTCOMES = {
    VALID: ("accept_reply", "accept_reply", "usable_reviewer_reply"),
    "unparseable": ("retry_reviewer", "retry_reviewer", "reviewer_reply_unusable"),
    WOULD_INCONCLUSIVE: (
        "park_review_unparseable",
        "park_review_inconclusive",
        "reviewer_reply_unusable_after_retry",
    ),
    "inconclusive": (
        "park_review_inconclusive",
        "park_review_inconclusive",
        "review_inconclusive",
    ),
}
