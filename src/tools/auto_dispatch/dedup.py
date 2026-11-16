"""De-duplicate dispatch and review requests by a normalised identity key.

The same work used to be sent out again and again: one Ticket went to a worker
seven times, one issue was dispatched four times, one commit was reviewed up to
twenty-two times. This module gives every request an identity key and keeps a
small ledger of the keys already claimed:

* a dispatch is ``(ticket or issue ref, line, base commit)``;
* a review is ``(line, commit)``.

A new request whose key is already ``in_flight`` or ``done`` is a duplicate. In
``enforce`` mode it reuses the existing entry unless the operator forces it; in
``shadow`` mode (the default) it records that it *would* reuse and proceeds as
before; ``off`` neither records nor reuses. A ``failed`` entry is never reused,
so a retry after a failure always runs.

Everything here is pure: the ledger is a plain JSON-safe ``dict`` of key to
entry, the caller owns its persistence, and the clock is passed in.

Empty or missing key parts make :func:`dispatch_key` / :func:`review_key`
return ``None`` (never raise); a ``None`` key means "skip dedup", and
:func:`decide` answers ``new`` for it.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.tools.auto_dispatch.liveness.modes import ENFORCE, OFF, SHADOW, normalize_mode
from src.utils.shadow_log import shadow_decision

logger = logging.getLogger(__name__)

MODE_ENV = "CONDUCTOR_DISPATCH_DEDUP_MODE"

IN_FLIGHT = "in_flight"
DONE = "done"
FAILED = "failed"
STATES = frozenset({IN_FLIGHT, DONE, FAILED})
REUSABLE_STATES = frozenset({IN_FLIGHT, DONE})
SETTLED_STATES = frozenset({DONE, FAILED})

#: The ledger keeps at most this many entries (newest kept).
MAX_ENTRIES = 512

_ISSUE_RE = re.compile(r"^(?:issue\s*[:#]?\s*|#)#?(\d+)$")
_REPO_ISSUE_RE = re.compile(r"^([\w.-]+/[\w.-]+)\s*(?:#|:|/issues/|/pull/)\s*(\d+)$")
_TICKET_RE = re.compile(r"^(?:ticket|t)\s*:\s*(\S+)$")
_SPACE_RE = re.compile(r"\s+")


def dedup_mode(environ: Mapping[str, str] | None = None) -> str:
    """The configured mode (``off``/``shadow``/``enforce``); ``shadow`` by default."""
    env = os.environ if environ is None else environ
    return normalize_mode(env.get(MODE_ENV, ""), source=MODE_ENV)


def _part(value: object) -> str:
    return _SPACE_RE.sub(" ", str(value or "").strip()).casefold()


def normalize_ref(source_ref: object) -> str | None:
    """A ticket/issue reference in canonical form; ``None`` when empty.

    ``#59``, ``issue:59``, ``Issue #59`` -> ``issue:59``;
    ``Owner/Repo#59``, ``owner/repo/issues/59`` -> ``owner/repo#59``;
    ``ticket:<id>`` / ``T:<id>`` -> ``t:<id>``. Anything else is stripped,
    whitespace-collapsed and case-folded.
    """
    text = _part(source_ref)
    if not text:
        return None
    match = _ISSUE_RE.fullmatch(text)
    if match:
        return f"issue:{int(match.group(1))}"
    match = _REPO_ISSUE_RE.fullmatch(text)
    if match:
        return f"{match.group(1)}#{int(match.group(2))}"
    match = _TICKET_RE.fullmatch(text)
    if match:
        return f"t:{match.group(1)}"
    return text


def _commit(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def dispatch_key(source_ref: str, line: str, base_commit: str) -> str | None:
    """``dispatch|<ref>|<line>|<commit>``, or ``None`` if any part is empty."""
    ref = normalize_ref(source_ref)
    line_part = _part(line)
    commit = _commit(base_commit)
    if not (ref and line_part and commit):
        return None
    return f"dispatch|{ref}|{line_part}|{commit}"


def review_key(line: str, commit: str) -> str | None:
    """``review|<line>|<commit>``, or ``None`` if either part is empty."""
    line_part = _part(line)
    commit_part = _commit(commit)
    if not (line_part and commit_part):
        return None
    return f"review|{line_part}|{commit_part}"


@dataclass(frozen=True)
class DedupDecision:
    """What to do with one request.

    ``action`` is ``"reuse"`` only in enforce mode. ``would_reuse`` says an
    in-flight or done entry exists and was not forced past (recorded in shadow
    and enforce, never in off). ``forced`` says the operator overrode a
    reusable entry.
    """

    action: str
    key: str | None
    existing: dict[str, Any] | None
    would_reuse: bool
    forced: bool
    mode: str

    @property
    def is_duplicate(self) -> bool:
        return self.would_reuse or self.forced


def decide(
    ledger: Mapping[str, Any],
    key: str | None,
    *,
    force: bool = False,
    mode: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> DedupDecision:
    """Decide whether ``key`` is new or reuses an existing ledger entry."""
    active = dedup_mode(environ) if mode is None else normalize_mode(mode, source=MODE_ENV)
    entry = ledger.get(key) if key else None
    existing = dict(entry) if isinstance(entry, Mapping) else None
    if active == OFF or existing is None:
        return DedupDecision("new", key, existing, False, False, active)
    reusable = existing.get("state") in REUSABLE_STATES
    forced = bool(force) and reusable
    would_reuse = reusable and not forced
    action = "reuse" if would_reuse and active == ENFORCE else "new"
    if active == SHADOW:
        # One line per shadow judgement, including the ones that change
        # nothing: a ledger entry exists but is a ``failed`` state (never
        # reused) or the operator forced past it. Those are the denominator
        # T4 divides by, so only "no entry for the key" is left out (see
        # ``shadow_log``'s table). The decision itself is unchanged.
        shadow_decision(
            logger,
            "dedup",
            would_change=would_reuse,
            subject=key,
            current="new",
            enforced="reuse" if would_reuse else "new",
            reason=_shadow_reason(existing, forced=forced, reusable=reusable),
            existing_ref=existing.get("ref"),
            existing_key=existing.get("key"),
            attempts=existing.get("attempts"),
            duplicates=existing.get("duplicates"),
        )
    return DedupDecision(action, key, existing, would_reuse, forced, active)


def _shadow_reason(existing: Mapping[str, Any], *, forced: bool, reusable: bool) -> str:
    """Why enforce would (or would not) reuse this ledger entry."""
    state = _part(existing.get("state")) or "unknown"
    if forced:
        return "operator_forced"
    if not reusable:
        return f"not_reusable_{state}"
    return f"duplicate_{state}"


def _iso(now: datetime) -> str:
    return now.isoformat()


def _json_safe(value: Mapping[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, item in dict(value or {}).items():
        if item is None or isinstance(item, (str, int, float, bool)):
            out[str(name)] = item
        elif isinstance(item, datetime):
            out[str(name)] = item.isoformat()
        elif isinstance(item, Mapping):
            out[str(name)] = _json_safe(item)
        elif isinstance(item, (list, tuple)):
            out[str(name)] = [
                x if x is None or isinstance(x, (str, int, float, bool)) else str(x)
                for x in item
            ]
        else:
            out[str(name)] = str(item)
    return out


def _evict(ledger: dict[str, Any], keep: str) -> None:
    """Drop the oldest entries beyond :data:`MAX_ENTRIES`, settled ones first."""
    excess = len(ledger) - MAX_ENTRIES
    if excess <= 0:
        return

    def age(item: tuple[str, Any]) -> tuple[int, str]:
        entry = item[1] if isinstance(item[1], Mapping) else {}
        settled = 0 if entry.get("state") in SETTLED_STATES else 1
        stamp = str(entry.get("settled_at") or entry.get("claimed_at") or "")
        return settled, stamp

    candidates = sorted((kv for kv in ledger.items() if kv[0] != keep), key=age)
    for name, _entry in candidates[:excess]:
        del ledger[name]


def claim(
    ledger: dict[str, Any],
    key: str,
    ref: str,
    *,
    now: datetime,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark ``key`` in flight for ``ref`` (a dispatch/review id); returns a copy."""
    if not key:
        raise ValueError("claim needs a non-empty key")
    previous = ledger.get(key)
    entry: dict[str, Any] = dict(previous) if isinstance(previous, Mapping) else {
        "key": key,
        "first_claimed_at": _iso(now),
        "attempts": 0,
        "duplicates": 0,
        "forced": 0,
    }
    entry.update(
        {
            "ref": str(ref),
            "state": IN_FLIGHT,
            "attempts": int(entry.get("attempts") or 0) + 1,
            "claimed_at": _iso(now),
            "settled_at": None,
            "result": None,
        }
    )
    if meta is not None:
        entry["meta"] = _json_safe(meta)
    ledger[key] = entry
    _evict(ledger, keep=key)
    return dict(entry)


def settle(
    ledger: dict[str, Any],
    key: str,
    state: str,
    *,
    now: datetime,
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Mark ``key`` done or failed; ``None`` when the key was never claimed."""
    if state not in SETTLED_STATES:
        raise ValueError(f"settle state must be one of {sorted(SETTLED_STATES)}")
    previous = ledger.get(key) if key else None
    if not isinstance(previous, Mapping):
        return None
    entry = dict(previous)
    entry["state"] = state
    entry["settled_at"] = _iso(now)
    if result is not None:
        entry["result"] = _json_safe(result)
    ledger[key] = entry
    return dict(entry)


def note_duplicate(
    ledger: dict[str, Any],
    key: str | None,
    *,
    decision: DedupDecision,
    now: datetime,
) -> dict[str, Any] | None:
    """Count a duplicate (and a forced override) on the existing entry.

    Records only in shadow and enforce, and only when ``decision`` found an
    in-flight or done entry; returns the updated copy or ``None``.
    """
    if decision.mode == OFF or not decision.is_duplicate or not key:
        return None
    previous = ledger.get(key)
    if not isinstance(previous, Mapping):
        return None
    entry = dict(previous)
    entry["duplicates"] = int(entry.get("duplicates") or 0) + 1
    if decision.forced:
        entry["forced"] = int(entry.get("forced") or 0) + 1
    if decision.action == "reuse":
        entry["reused"] = int(entry.get("reused") or 0) + 1
    entry["last_duplicate_at"] = _iso(now)
    entry["last_duplicate_mode"] = decision.mode
    ledger[key] = entry
    return dict(entry)


__all__ = [
    "DONE",
    "DedupDecision",
    "FAILED",
    "IN_FLIGHT",
    "MAX_ENTRIES",
    "MODE_ENV",
    "claim",
    "decide",
    "dedup_mode",
    "dispatch_key",
    "normalize_ref",
    "note_duplicate",
    "review_key",
    "settle",
]
