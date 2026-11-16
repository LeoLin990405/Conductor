#!/usr/bin/env python3
"""Read-only report for N73 assignment rollout.

The script never writes a dispatch and never opens a database. Rows are
either injected (``load_snapshot`` / ``build_report``) or read from a JSON
file. It prints the metrics from the plan: pass rate, review rounds, cost,
duration, feasibility, fallback, and call overhead, split by the group that
actually ran, and again by provider and area. Cells with fewer than 10
samples are marked ``insufficient`` and are not used as a conclusion.
Pending, assigned, running, and reviewing lines are not pass-rate samples.
Neither is a line whose G5 phase is still pending, merging, or running.

``spec["modes.rollback_due"]`` is the slice ``modes.rollback_due`` reads.
``cost_per_line`` is each group's wall-clock median. Token medians stay on
``cost.tokens`` and are not a rollback arm: the consumer has no token arm,
and a key named ``cost`` would hide the wall-clock cells.

Stage C adds the automatic-rollback rule: if the Super-agent group's pass
rate is more than 10 points below the selector group, or its wall-clock
cost per line is more than 50% higher, the report says to return to shadow
and notifies the Planner. It does not change ``CONDUCTOR_DISPATCH_ASSIGN_MODE``.

Missing token or CodexBar numbers stay missing. They are not counted as zero.
Paid-credit spend is summed by day for the lines that reported it.

Examples:
    python3 scripts/assign_report.py snapshot.json
    python3 scripts/assign_report.py snapshot.json --stage B
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

SCHEMA = "conductor.assign_report/v1"
MIN_CELL = 10
# Q8: stage B promotes only after 40 lines actually adopted the Super-agent.
ADOPTED_MIN = 40

# B → C. Pass-rate gaps are fractions (0.05 is five percentage points).
PASS_GAP_LIMIT = 0.05
REVIEW_MEAN_LIMIT = 0.3
COST_RATIO_LIMIT = 1.15
DURATION_RATIO_LIMIT = 1.15
PASS_GAIN_WAIVES_COST = 0.10
FEASIBILITY_MIN = 0.90
FALLBACK_MAX = 0.05
LATENCY_P95_MS = 30_000

# C → shadow.
ROLLBACK_PASS_GAP = 0.10
ROLLBACK_COST_RATIO = 1.5
# modes.rollback_due reads ``rate`` back with Decimal(str(float)) and ignores
# hits and n. Ten decimal places keep an exact 10-point gap from comparing
# as slightly over 10, and they still show 149/1498 versus 299/1499
# (10.0000356 points). Six places hide that gap.
_SPEC_RATE_PLACES = Decimal("1e-10")

# These vocabularies are the package's (superagent_assign/types.py). The
# script does not import ``src``: loading that package logs to stdout and
# would corrupt the JSON report. tests/unit/scripts/test_assign_report.py
# checks that the copies still equal Source and ErrorKind.
_SUPERAGENT_SOURCES = frozenset({"superagent", "superagent_reask"})
# LineStatus values that have not finished. They are not pass-rate samples.
_OPEN_LINE_STATUSES = frozenset({"pending", "assigned", "running", "reviewing"})
# Integration phases that have not finished. A failed G5 is a finished miss.
_OPEN_G5_PHASES = frozenset({"pending", "merging", "running"})
# ``ErrorKind`` values. record.py stores an ERROR fallback as its kind.
_ERROR_REASONS = frozenset({"call_error", "timeout", "bad_format"})
# Same folding as superagent_assign/context.py ``normalize_untrusted``.
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Mn", "Me"})
_INVISIBLE_CHARS = frozenset("\u2800\u3164\u115f\u1160\uffa0")
# Same list as superagent_assign/context.py ``_TOP_LEVEL_AREAS``.
_TOP_LEVEL_AREAS = frozenset(
    {
        "web",
        "desktop",
        "scripts",
        "migrations",
        "docs",
        "tests",
        "deploy",
        "plugins",
        "extensions",
        "config",
        "contrib",
        "workflows",
        "skills",
        "prototypes",
        "reverse-engineering",
        "mcp_servers",
        "libs",
    }
)
# The assignment writer stores ``result``; older rows stored ``ok``.
_FEASIBLE_RESULTS = frozenset({"ok", "feasible"})
_INFEASIBLE_RESULTS = frozenset({"infeasible"})
# Ledger keys that are not an attempt. N30 stores a list; the assignment
# writer stores a dict and may hang ``assignment_ref`` on the line.
_LEDGER_META = frozenset({"assignment_ref", "truncated", "v", "attempts"})
# N30: our own environment is not the model's cost. A rate limit is the
# provider's, and it stays in the sum. A serial-lock wait exits as a rate
# limit, so it stays too: the ledger has no separate code for it.
_SELF_ENV_CLASSES = frozenset({"auth_failed"})
_SELF_ENV_REASONS = frozenset(
    {
        "worker_auth_failed",
        "missing_credential",
        "review_unparseable",
    }
)


def area_bucket(path: str) -> str:
    """The area of one write path: ``web/``, ``src/tools/auto_dispatch``, ``docs/``.

    The same rule as ``superagent_assign.context.area_bucket``, so a
    provider × area cell here names the bucket ``success_rate_by_area``
    used: a known top-level tree is that tree with a trailing ``/``,
    anything else is its first three directories. A dotted last segment
    is a file and is dropped; globs are not directories; a ``..`` segment
    is not an area. A path with no area is ``unspecified``. The context
    folds the path the same way first (NFKC, invisible characters
    removed), so ``ｗｅｂ/app.ts`` is ``web/`` in both. The context also
    redacts credentials before splitting; this report only reads stored
    scopes, so it drops a segment that carries URL userinfo (``@``)
    instead of printing it.
    """
    folded = unicodedata.normalize("NFKC", str(path or ""))
    folded = "".join(
        char
        for char in folded
        if unicodedata.category(char) not in _INVISIBLE_CATEGORIES
        and char not in _INVISIBLE_CHARS
    )
    cleaned = folded.strip().replace("\\", "/").replace("**", "").replace("*", "")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    parts = [part for part in cleaned.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return "unspecified"
    if "." in parts[-1]:
        parts = parts[:-1]
    parts = [part for part in parts if "@" not in part]
    if not parts:
        return "unspecified"
    if parts[0] in _TOP_LEVEL_AREAS:
        return parts[0] + "/"
    return "/".join(parts[:3])


def line_area(write_scope: Sequence[str]) -> str:
    """The bucket most of the scope sits in. Ties break on the sorted name."""
    if not write_scope:
        return "unspecified"
    counts: dict[str, int] = {}
    for raw in write_scope:
        bucket = area_bucket(str(raw))
        counts[bucket] = counts.get(bucket, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ranked[0][0]


@dataclass
class LineStat:
    """One line, already normalised from a flat row or a gate snapshot."""

    line_id: str
    provider: str
    area: str
    source: str
    adopted: str
    skipped: bool
    fallback: bool
    passed: bool
    human_reassign: bool
    human_unblock: bool
    review_blocks: int | None
    inconclusive: int
    review_errors: int
    assignment_error: bool
    wall_clock_secs: float | None
    tokens: int | None
    token_basis: str | None
    duration_secs: float | None
    self_inflicted: bool
    first_feasible: bool | None
    latency_ms: int | float | None
    asked: bool
    paid_credits: float | None
    codexbar_delta: float | None
    day: str | None
    # None means this snapshot has no G5 record. False means G5 did not complete.
    g5_passed: bool | None = None
    # Tokens spent on the assignment call itself, not the author run.
    call_tokens: int | float | None = None
    # Every selection call. The first launch still picks the cohort.
    call_latencies: tuple[int | float, ...] = ()
    call_token_samples: tuple[int | float, ...] = ()
    # One record per selection call on this line: identity, latency, tokens.
    # A shared batch keeps a record on every member line. Global overhead
    # collapses identities; provider and area cells collapse only inside
    # their own members, so each participant still shows the call.
    call_records: tuple[tuple[tuple[Any, ...], int | float | None, int | float | None], ...] = ()
    # True when the first launch itself fell back. A later fallback
    # stays in this cohort and is counted on ``fallback_events``.
    launch_fallback: bool = False
    fallback_events: int = 0
    assignment_error_events: int = 0
    # True when some assignment entry was a real decision. An opening
    # ``off`` launch is not one, and it does not erase a later decision.
    decided: bool = False
    # Feasibility of that first decision. Unknown stays unknown.
    decision_feasible: bool | None = None
    # False while the line is still pending, assigned, running, or reviewing,
    # or while G5 is pending, merging, or running. A missing status and a
    # missing integration record stay resolved so a gate-only row keeps its
    # pass bit.
    outcome_resolved: bool = True

    def clean_pass(self) -> bool:
        """G2/MERGE/G5 finished, and nobody reassigned or unblocked the line.

        ``passed`` is the line outcome. A failed integration phase has not
        cleared G5, so the outcome alone is not enough. An in-progress phase
        is not a failure: those lines leave the pass-rate sample. Flat
        fixtures that never recorded G5 keep the outcome.
        """
        if self.skipped or self.human_reassign or self.human_unblock:
            return False
        if self.g5_passed is False:
            return False
        return self.passed

    def counts_toward_cost(self) -> bool:
        """Author spend of a finished line, including a human reassign or unblock.

        Pass rate still uses ``clean_pass`` and leaves those interventions
        out. Cost does not: the author already spent the wall clock and the
        tokens. A self-inflicted failure stays out, a failed G5 is not a
        finished line, and an unfinished line or G5 (``outcome_resolved``
        false) is not a sample yet. Missing usage is still missing, not zero.
        """
        if self.skipped or self.self_inflicted or self.group() is None:
            return False
        # A line whose G5 is still pending, merging, or running has not
        # merged. Its spend is not yet a merged line's cost, and counting it
        # would let an unfinished cohort trigger the cost rollback.
        if not self.outcome_resolved:
            return False
        if self.g5_passed is False:
            return False
        return self.passed

    def counts_toward_duration(self) -> bool:
        """First launch until this line PASSED.

        PRD 6.2 stops at line PASSED, not at the dispatch G5 gate. A later
        integration failure does not erase a duration the line already
        recorded. Human reassign and unblock do not either. Skips and
        self-inflicted failures stay out.
        """
        if self.skipped or self.self_inflicted or self.group() is None:
            return False
        return self.passed

    def group(self) -> str | None:
        """Who actually ran the first launch. Later fallbacks stay here."""
        if self.skipped or self.launch_fallback or self.source == "selector_fallback":
            return None
        if self.adopted == "superagent":
            return "superagent"
        if self.adopted == "selector":
            return "selector"
        if self.source in _SUPERAGENT_SOURCES:
            return "superagent"
        if self.source == "selector":
            return "selector"
        return None


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    return bool(value)


def _area_of(raw: Mapping[str, Any]) -> str:
    """Same buckets as the assignment context. ``src`` is not one bucket."""
    if raw.get("area"):
        return area_bucket(str(raw["area"]))
    scope = raw.get("write_scope")
    if isinstance(scope, str) and scope:
        return line_area((scope,))
    if isinstance(scope, list) and scope:
        return line_area(tuple(str(item) for item in scope))
    return "unspecified"


def _finite_number(value: object) -> int | float | None:
    """A recorded count. Integral floats stay ints. Missing stays missing."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        if value.is_integer():
            return int(value)
        return value
    return None


def _attempt_feasible(item: object) -> bool | None:
    """One feasibility attempt. ``result`` is the writer; ``ok`` is older rows."""
    if not isinstance(item, Mapping):
        return None
    if "result" in item and item.get("result") is not None:
        raw = item.get("result")
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            name = raw.strip().casefold()
            if name in _FEASIBLE_RESULTS:
                return True
            if name in _INFEASIBLE_RESULTS:
                return False
        return None
    if "ok" in item:
        return bool(item.get("ok"))
    return None


def _first_feasible(feasibility: object) -> bool | None:
    if isinstance(feasibility, Mapping):
        return _attempt_feasible(feasibility)
    if isinstance(feasibility, list) and feasibility:
        return _attempt_feasible(feasibility[0])
    return None


def _usage_total(usage: object) -> int | float | None:
    """Assignment-call tokens. A number, or ``{"total_tokens": ...}``."""
    if isinstance(usage, Mapping):
        if "total_tokens" in usage:
            return _finite_number(usage.get("total_tokens"))
        if "total" in usage:
            return _finite_number(usage.get("total"))
        return None
    return _finite_number(usage)


def _infer_adopted(adopted: str, mode: str, source: str) -> str:
    """Who ran when the assignment row left ``adopted`` blank.

    Shadow and off run the selector. An enforce line that the sample skipped
    records ``source=selector`` and also ran the selector; ``choice`` on that
    row is the proposal, and ``selector_choice`` is the model that launched.
    An explicit adopted value still wins. A fallback is not a selector cohort.
    """
    if adopted or source == "selector_fallback":
        return adopted
    if mode in {"shadow", "off"} or source == "selector":
        return "selector"
    return adopted


def _assignment_asked(entry: Mapping[str, Any]) -> bool:
    if "asked" in entry:
        return bool(entry.get("asked"))
    mode = str(entry.get("mode") or "").strip().casefold()
    if mode == "off":
        return False
    source = str(entry.get("source") or "")
    if _first_feasible(entry.get("feasibility")) is not None:
        return True
    return mode in {"shadow", "enforce"} or source in _SUPERAGENT_SOURCES | {"selector_fallback"}


def _from_assignment(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Fields a gate assignment entry can answer without an outcome row."""
    first_ok = _first_feasible(entry.get("feasibility"))
    source = str(entry.get("source") or "")
    reason = entry.get("fallback_reason")
    assignment_error = source == "selector_fallback" and str(reason or "") in _ERROR_REASONS
    choice = entry.get("choice") if isinstance(entry.get("choice"), Mapping) else {}
    selector = entry.get("selector_choice") if isinstance(entry.get("selector_choice"), Mapping) else {}
    mode = str(entry.get("mode") or "").strip().casefold()
    adopted = _infer_adopted(str(entry.get("adopted") or ""), mode, source)
    provider = ""
    if adopted == "selector":
        provider = str(selector.get("model") or choice.get("model") or "")
    else:
        provider = str(choice.get("model") or selector.get("model") or "")
    tokens = _usage_total(entry.get("llm_tokens"))
    return {
        "source": source,
        "adopted": adopted,
        "provider": provider,
        "first_feasible": first_ok,
        "fallback": source == "selector_fallback",
        "assignment_error": assignment_error,
        "latency_ms": entry.get("latency_ms"),
        "mode": entry.get("mode"),
        "llm_total_tokens": tokens,
    }


def _assignment_entries(store: object) -> list[Mapping[str, Any]]:
    """Assignment calls in launch order. Cohort uses the first; overhead uses all."""
    if not isinstance(store, Mapping):
        return []
    items = [item for item in store.values() if isinstance(item, Mapping)]
    items.sort(key=lambda item: int(item.get("seq") or 0))
    return items


def _first_assignment(store: object) -> Mapping[str, Any] | None:
    """The first author launch. A later reassignment does not move the cohort.

    Section 6.2 scores the run that first wrote the line. The newest entry
    is often a human reassign onto the selector, and folding that into the
    selector group hides the Super-agent failure that caused it. Later
    calls still count toward selection latency and tokens.
    """
    items = _assignment_entries(store)
    return items[0] if items else None


def _call_samples(
    entries: Sequence[Mapping[str, Any]],
) -> tuple[list[int | float], list[int | float]]:
    """Latency and assignment-token totals for every selection call."""
    latencies: list[int | float] = []
    tokens: list[int | float] = []
    for entry in entries:
        if not _assignment_asked(entry):
            continue
        latency = _finite_number(entry.get("latency_ms"))
        if latency is not None:
            latencies.append(latency)
        total = _usage_total(entry.get("llm_tokens"))
        if total is not None:
            tokens.append(total)
    return latencies, tokens


def _event_counts(entries: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    """Fallback and assignment-error events. The first launch still owns the cohort."""
    fallbacks = 0
    errors = 0
    for entry in entries:
        source = str(entry.get("source") or "")
        if source == "selector_fallback" or entry.get("fallback") is True:
            fallbacks += 1
        reason = str(entry.get("fallback_reason") or "")
        if entry.get("assignment_error") is True or (
            source == "selector_fallback" and reason in _ERROR_REASONS
        ):
            errors += 1
    return fallbacks, errors


def _digest_key(digest: object) -> str | None:
    """The shared call id inside ``inputs_digest``. Measurements are not an id."""
    if isinstance(digest, str) and digest.strip():
        return digest.strip()
    if not isinstance(digest, Mapping) or not digest:
        return None
    for name in ("hash", "sha256", "digest", "id"):
        value = digest.get(name)
        if isinstance(value, bool) or value is None or value == "":
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    try:
        return json.dumps(digest, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def _seq_key(entry: Mapping[str, Any]) -> str:
    """Launch sequence, when the row recorded one. A bool is not a sequence."""
    seq = entry.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, (int, str)):
        return ""
    return str(seq)


def _call_identity(
    entry: Mapping[str, Any],
    *,
    scope: str,
    line_id: str,
    ordinal: int,
) -> tuple[Any, ...]:
    """Identity of one selection call.

    Lines that share an ``inputs_digest`` joined one invocation. ``seq`` is
    only this line's assignment ordinal, so a line with an older launch
    stores that same call at a higher seq. The shared id does not include
    ``seq`` or ``run_id``. A relaunch with a different digest is a new call
    even when latency and tokens match. Missing digests stay on their own
    line; equal measurements do not merge them.
    """
    digest = _digest_key(entry.get("inputs_digest"))
    if digest is not None:
        return ("digest", scope, digest)
    run_id = str(entry.get("run_id") or "")
    seq_key = _seq_key(entry) or str(ordinal)
    return ("entry", scope, line_id, ordinal, run_id, seq_key)


def _call_records(
    entries: Sequence[Mapping[str, Any]],
    *,
    scope: str,
    line_id: str,
) -> tuple[tuple[tuple[Any, ...], int | float | None, int | float | None], ...]:
    """Asked calls on one line, still attached to that line.

    A shared batch writes one entry on every member line, each with that
    line's own ``run_id`` and ``seq``, so neither can name the call. The
    k-th time this line sees a digest is the k-th invocation with those
    inputs, and every member line that joined it sees it the k-th time
    too. The identity is the digest plus that occurrence number: the first
    and second shared invocations stay two calls, and each is counted
    once however many lines joined it. A repeated copy of the same run
    (same ``run_id`` and ``seq``) is the same occurrence and merges.
    """
    records: list[tuple[tuple[Any, ...], int | float | None, int | float | None]] = []
    occurrences: dict[tuple[Any, ...], list[tuple[str, str]]] = {}
    for ordinal, entry in enumerate(entries):
        if not _assignment_asked(entry):
            continue
        identity = _call_identity(entry, scope=scope, line_id=line_id, ordinal=ordinal)
        if identity[0] == "digest":
            launch = (str(entry.get("run_id") or ""), _seq_key(entry))
            seen = occurrences.setdefault(identity, [])
            if launch in seen:
                occurrence = seen.index(launch)
            else:
                occurrence = len(seen)
                seen.append(launch)
            identity = (*identity, occurrence)
        records.append(
            (
                identity,
                _finite_number(entry.get("latency_ms")),
                _usage_total(entry.get("llm_tokens")),
            )
        )
    return tuple(records)


def _stamp_decisions(merged: dict[str, Any], entries: Sequence[Mapping[str, Any]]) -> None:
    """Count the first real decision, not an earlier ``off`` launch.

    The opening entry still picks the cohort. Feasibility and fallback
    denominators follow the first entry that actually decided.
    """
    decisions = [entry for entry in entries if _assignment_asked(entry)]
    if not decisions:
        return
    merged["decided"] = True
    feasible = _first_feasible(decisions[0].get("feasibility"))
    if feasible is not None:
        merged["decision_feasible"] = feasible


def _attach_calls(
    merged: dict[str, Any],
    entries: Sequence[Mapping[str, Any]],
    *,
    scope: str,
    line_id: str,
    keep_samples: bool,
    keep_events: bool,
) -> None:
    if not entries:
        return
    if keep_samples:
        latencies, token_samples = _call_samples(entries)
        merged["call_latencies"] = latencies
        merged["call_token_samples"] = token_samples
        merged["call_records"] = _call_records(entries, scope=scope, line_id=line_id)
    if keep_events:
        fallbacks, errors = _event_counts(entries)
        merged["fallback_events"] = fallbacks
        merged["assignment_error_events"] = errors
    _stamp_decisions(merged, entries)


def _number_list(value: object) -> tuple[int | float, ...]:
    if not isinstance(value, list):
        return ()
    found: list[int | float] = []
    for item in value:
        number = _finite_number(item)
        if number is not None:
            found.append(number)
    return tuple(found)


_BLOCK_RESULTS = frozenset({"block", "blocked"})
_INCONCLUSIVE_RESULTS = frozenset({"inconclusive"})
_REVIEW_ERROR_RESULTS = frozenset({"error", "review_unavailable"})


def _history_review_counts(history: object) -> tuple[int, int, int] | None:
    """BLOCK, INCONCLUSIVE, and ERROR counts from ``gates.lines[id].history``.

    A missing history is unknown, not zero. A present list with no BLOCK is
    zero blocks. MERGE, approve, and pass are not blocks. The stored verdict
    is lowercase; snapshots also say ``BLOCK``.
    """
    if not isinstance(history, list):
        return None
    blocks = 0
    inconclusive = 0
    errors = 0
    for entry in history:
        if not isinstance(entry, Mapping):
            continue
        result = str(entry.get("result") or "").strip().casefold()
        if result in _BLOCK_RESULTS:
            blocks += 1
        elif result in _INCONCLUSIVE_RESULTS:
            inconclusive += 1
        elif result in _REVIEW_ERROR_RESULTS:
            errors += 1
    return blocks, inconclusive, errors


def line_from_mapping(raw: Mapping[str, Any], *, day: str | None, dispatch_id: str) -> LineStat:
    """One flat line object. Unknown numbers stay unknown."""
    source = str(raw.get("source") or "")
    adopted = str(raw.get("adopted") or "")
    mode = raw.get("mode")
    mode_text = str(mode or "").strip().casefold()
    adopted = _infer_adopted(adopted, mode_text, source)
    if "asked" in raw:
        asked = bool(raw.get("asked"))
    elif mode == "off":
        asked = False
    elif raw.get("first_feasible") is not None or source in _SUPERAGENT_SOURCES | {"selector_fallback"}:
        asked = True
    elif mode in {"shadow", "enforce"}:
        asked = True
    else:
        asked = False
    history_counts = _history_review_counts(raw.get("history"))
    review_blocks = _optional_int(raw.get("review_blocks"))
    if review_blocks is None and history_counts is not None:
        review_blocks = history_counts[0]
    if raw.get("inconclusive") is None:
        inconclusive = history_counts[1] if history_counts is not None else 0
    else:
        inconclusive = _optional_int(raw.get("inconclusive")) or 0
    review_errors = raw.get("review_errors")
    retry = raw.get("review_retry") if isinstance(raw.get("review_retry"), Mapping) else {}
    retry_errors = _optional_int(retry.get("attempt"))
    if review_errors is None:
        if raw.get("review_attempts") is not None:
            review_errors = raw.get("review_attempts")
        elif retry_errors is not None:
            review_errors = retry_errors
        # ``errors`` on a flat row is the review-error count only when the row
        # did not also record an assignment error. Assignment rows always carry
        # that flag, and it must not hide ERROR verdicts in review history.
        elif raw.get("errors") is not None and "assignment_error" not in raw:
            review_errors = raw.get("errors")
        elif history_counts is not None:
            review_errors = history_counts[2]
    tokens = _optional_int(raw.get("tokens"))
    basis = raw.get("token_basis")
    basis_text = str(basis) if isinstance(basis, str) and basis.strip() else None
    if tokens is not None and basis_text is None:
        # A token count with no basis cannot be compared, and it is not zero.
        tokens = None
        basis_text = None
    # ``duration_to_passed_secs`` is the span from the first launch to PASSED.
    # ``duration_secs`` counts only when this row recorded that same span.
    # The machine's outcome field is the last native run: a relaunch replaces
    # ``_Run.started_at``, and the join below does not copy it.
    duration = _optional_float(raw.get("duration_to_passed_secs"))
    if duration is None:
        duration = _optional_float(raw.get("duration_secs"))
    provider = str(raw.get("provider") or raw.get("model") or raw.get("assigned_model") or "")
    if not provider and adopted == "selector":
        selector = raw.get("selector_choice") if isinstance(raw.get("selector_choice"), Mapping) else {}
        choice = raw.get("choice") if isinstance(raw.get("choice"), Mapping) else {}
        provider = str(selector.get("model") or choice.get("model") or "")
    if not provider:
        provider = "unknown"
    call_latencies = _number_list(raw.get("call_latencies"))
    if not call_latencies and asked:
        one_latency = _finite_number(raw.get("latency_ms"))
        call_latencies = () if one_latency is None else (one_latency,)
    call_token_samples = _number_list(raw.get("call_token_samples"))
    if not call_token_samples and asked:
        one_tokens = _finite_number(raw.get("llm_total_tokens"))
        call_token_samples = () if one_tokens is None else (one_tokens,)
    stored_calls = raw.get("call_records")
    call_records = stored_calls if isinstance(stored_calls, tuple) else ()
    if "decided" in raw:
        decided = bool(raw.get("decided"))
    else:
        decided = asked
    if "decision_feasible" in raw:
        marked = raw.get("decision_feasible")
        decision_feasible = None if marked is None else bool(marked)
    elif decided:
        marked_feasible = raw.get("first_feasible") if "first_feasible" in raw else None
        if "first_feasible" not in raw or marked_feasible is None:
            decision_feasible = None
        else:
            decision_feasible = bool(marked_feasible)
    else:
        decision_feasible = None
    event_fallbacks = _optional_int(raw.get("fallback_events"))
    if event_fallbacks is None:
        launch_fallback = _bool(raw.get("fallback")) or source == "selector_fallback"
        event_fallbacks = 1 if launch_fallback else 0
    else:
        # Events were counted across calls. Only the first source leaves the cohort.
        launch_fallback = source == "selector_fallback"
    fallback = event_fallbacks > 0 or launch_fallback
    event_errors = _optional_int(raw.get("assignment_error_events"))
    if event_errors is None:
        assignment_error = _bool(raw.get("assignment_error"))
        event_errors = 1 if assignment_error else 0
    else:
        assignment_error = event_errors > 0 or _bool(raw.get("assignment_error"))
    if "passed" in raw:
        passed = _bool(raw.get("passed"))
    else:
        passed = str(raw.get("status") or "").strip().lower() == "passed"
    status = str(raw.get("status") or "").strip().casefold()
    outcome_resolved = status not in _OPEN_LINE_STATUSES
    return LineStat(
        line_id=str(raw.get("line_id") or raw.get("id") or dispatch_id),
        provider=provider,
        area=_area_of(raw),
        source=source,
        adopted=adopted,
        skipped=_bool(raw.get("skipped")),
        fallback=fallback,
        passed=passed,
        human_reassign=_bool(raw.get("human_reassign")),
        human_unblock=_bool(raw.get("human_unblock")),
        review_blocks=review_blocks,
        inconclusive=inconclusive,
        review_errors=_optional_int(review_errors) or 0,
        assignment_error=assignment_error,
        wall_clock_secs=_optional_float(raw.get("wall_clock_secs")),
        tokens=tokens,
        token_basis=basis_text if tokens is not None else None,
        duration_secs=duration,
        self_inflicted=_bool(raw.get("self_inflicted_failure") or raw.get("self_inflicted")),
        first_feasible=None if "first_feasible" not in raw else (
            None if raw.get("first_feasible") is None else bool(raw.get("first_feasible"))
        ),
        latency_ms=_finite_number(raw.get("latency_ms")),
        asked=asked,
        paid_credits=_optional_float(raw.get("paid_credits")),
        codexbar_delta=_optional_float(raw.get("codexbar_delta")),
        day=str(raw.get("day") or day) if (raw.get("day") or day) else None,
        g5_passed=None if "g5_passed" not in raw else (
            None if raw.get("g5_passed") is None else _bool(raw.get("g5_passed"))
        ),
        call_tokens=sum(call_token_samples) if call_token_samples else None,
        call_latencies=call_latencies,
        call_token_samples=call_token_samples,
        call_records=call_records,
        launch_fallback=launch_fallback,
        fallback_events=event_fallbacks,
        assignment_error_events=event_errors,
        decided=decided,
        decision_feasible=decision_feasible,
        outcome_resolved=outcome_resolved,
    )


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _self_environment(item: Mapping[str, Any]) -> bool:
    """True when this attempt failed in our environment, not the model's.

    A missing reading on one of these must not erase the attempts that did
    record usage, and a recorded zero on one of them is not a cheap line.
    """
    if item.get("self_inflicted") or item.get("self_inflicted_failure"):
        return True
    failure = str(item.get("failure_class") or "").strip().casefold()
    if failure in _SELF_ENV_CLASSES:
        return True
    reason = str(item.get("reason_code") or "").strip().casefold()
    return reason in _SELF_ENV_REASONS


def _ledger_attempts(bucket: object) -> list[Mapping[str, Any]]:
    """Author attempts on one cost-ledger line. Reviewer rows stay out.

    A list is the N30 shape. A dict may nest them under ``attempts`` or key
    them by ``run_id``. Metadata hung on the line is not an attempt.
    """
    if isinstance(bucket, list):
        raw = bucket
    elif isinstance(bucket, Mapping):
        nested = bucket.get("attempts")
        if isinstance(nested, list):
            raw = nested
        elif isinstance(nested, Mapping):
            raw = list(nested.values())
        else:
            raw = [item for key, item in bucket.items() if key not in _LEDGER_META]
    else:
        return []
    marks = {
        "wall_secs",
        "token_basis",
        "tokens_in",
        "tokens_out",
        "tokens_est",
        "tokens",
        "run_id",
        "role",
        "kind",
        "result",
        "paid_credits",
        "quota",
    }
    found: list[Mapping[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping) or not any(key in item for key in marks):
            continue
        role = str(item.get("role") or "author").strip().lower()
        if role != "author":
            continue
        if _self_environment(item):
            continue
        found.append(item)
    return found


def _attempt_tokens(item: Mapping[str, Any]) -> tuple[str, float] | None:
    """One attempt's tokens, or None when the usage was not recorded.

    ``cli`` is input plus output. ``estimate`` is ``tokens_est``. A missing
    field is not zero, and an estimate is not a stand-in for a cli reading.
    """
    basis = item.get("token_basis")
    if not isinstance(basis, str) or not basis.strip():
        return None
    basis = basis.strip()
    if basis == "cli":
        incoming = _number(item.get("tokens_in"))
        outgoing = _number(item.get("tokens_out"))
        if incoming is None or outgoing is None:
            return None
        return basis, incoming + outgoing
    if basis == "estimate":
        estimate = _number(item.get("tokens_est"))
        if estimate is None:
            return None
        return basis, estimate
    for key in ("tokens", "tokens_est"):
        total = _number(item.get(key))
        if total is not None:
            return basis, total
    return None


def _summarize_ledger(bucket: object) -> dict[str, Any] | None:
    """Line totals from the cost ledger.

    Wall and tokens are unknown when any counted attempt omitted them, so a
    partial sum cannot look like a cheap line. CodexBar and paid credits sum
    the readings that exist; a missing reading is left out rather than added
    as zero. Mixed token bases are not added together.
    """
    attempts = _ledger_attempts(bucket)
    if not attempts:
        return None
    walls: list[float] = []
    wall_known = True
    parts: list[tuple[str, float]] = []
    tokens_known = True
    codexbar: list[float] = []
    paid: list[float] = []
    for item in attempts:
        wall = _number(item.get("wall_secs")) if "wall_secs" in item else None
        if wall is None:
            wall_known = False
        else:
            walls.append(wall)
        part = _attempt_tokens(item)
        if part is None:
            tokens_known = False
        else:
            parts.append(part)
        quota = item.get("quota")
        if isinstance(quota, Mapping):
            delta = _number(quota.get("delta_pct"))
            if delta is not None:
                codexbar.append(delta)
        spent = _number(item.get("paid_credits"))
        if spent is not None:
            paid.append(spent)
    bases = {basis for basis, _value in parts}
    tokens = None
    basis_name = None
    if tokens_known and parts and len(bases) == 1:
        basis_name = next(iter(bases))
        tokens = sum(value for _basis, value in parts)
    return {
        "wall": sum(walls) if wall_known and walls else None,
        "tokens": tokens,
        "basis": basis_name,
        "codexbar": sum(codexbar) if codexbar else None,
        "paid": sum(paid) if paid else None,
    }


def _apply_ledger(merged: dict[str, Any], bucket: object) -> None:
    """Fill cost fields the row left blank. A number already on the row wins.

    Tokens without a ``token_basis`` are not a measurement, so the ledger
    may replace them. It never writes zero in their place.
    """
    summary = _summarize_ledger(bucket)
    if summary is None:
        return
    if _blank(merged.get("wall_clock_secs")) and summary["wall"] is not None:
        merged["wall_clock_secs"] = summary["wall"]
    has_basis = isinstance(merged.get("token_basis"), str) and bool(merged["token_basis"].strip())
    if not has_basis and summary["tokens"] is not None:
        merged["tokens"] = summary["tokens"]
        merged["token_basis"] = summary["basis"]
    if _blank(merged.get("codexbar_delta")) and summary["codexbar"] is not None:
        merged["codexbar_delta"] = summary["codexbar"]
    if _blank(merged.get("paid_credits")) and summary["paid"] is not None:
        merged["paid_credits"] = summary["paid"]


def _line_from_gates(
    line_id: str,
    shell: Mapping[str, Any],
    outcome: Mapping[str, Any] | None,
    *,
    day: str | None,
    cost_bucket: object = None,
    scope: str = "",
) -> LineStat:
    entries = _assignment_entries(shell.get("assignment"))
    derived = _from_assignment(entries[0] if entries else {})
    merged = dict(derived)
    merged["line_id"] = line_id
    _attach_calls(
        merged,
        entries,
        scope=scope,
        line_id=line_id,
        keep_samples=True,
        keep_events=True,
    )
    if isinstance(outcome, Mapping):
        for key, value in outcome.items():
            # Same last-run clock as the line/outcome join. See that copy.
            if key == "duration_secs":
                continue
            if key not in merged or merged.get(key) in (None, ""):
                merged[key] = value
        if "passed" in outcome:
            merged["passed"] = outcome.get("passed")
        if outcome.get("model") and merged.get("provider") in (None, ""):
            merged["provider"] = outcome.get("model")
        if "tokens" in outcome:
            merged["tokens"] = outcome.get("tokens")
    for key, value in shell.items():
        if key == "assignment":
            continue
        if key not in merged or merged.get(key) in (None, ""):
            merged[key] = value
    if shell.get("write_scope") and not merged.get("area"):
        merged["write_scope"] = shell.get("write_scope")
    _apply_ledger(merged, cost_bucket)
    return line_from_mapping(merged, day=day, dispatch_id=line_id)


def _blank(value: object) -> bool:
    return value is None or value == ""


def _fill(merged: dict[str, Any], key: str, value: object) -> None:
    if key not in merged or _blank(merged.get(key)):
        merged[key] = value


def _line_from_record(
    item: Mapping[str, Any],
    *,
    outcomes: Mapping[str, Any],
    gate_lines: Mapping[str, Any],
    cost_lines: Mapping[str, Any],
    day: str | None,
    dispatch_id: str,
    scope: str = "",
) -> LineStat:
    """One serialized line, joined to its outcome and its first assignment.

    A dispatch stores the line, the outcome, and the gate snapshot apart.
    Reading the line list alone drops the id, the model, and the pass bit.
    A row that already names its cohort (the report fixtures) keeps that
    cohort; the gate is only the source when the line did not.
    """
    line_id = str(item.get("line_id") or item.get("id") or "")
    shell = gate_lines.get(line_id) if line_id else None
    outcome = outcomes.get(line_id) if line_id else None
    outcome_map = outcome if isinstance(outcome, Mapping) else None
    explicit_cohort = "adopted" in item or "source" in item
    explicit_pass = "passed" in item
    merged: dict[str, Any] = {}
    shell_map = shell if isinstance(shell, Mapping) else None
    entries = _assignment_entries(shell_map.get("assignment")) if shell_map is not None else []
    if shell_map is not None and not explicit_cohort:
        if entries:
            merged.update(_from_assignment(entries[0]))
        for key, value in shell_map.items():
            if key == "assignment":
                continue
            _fill(merged, key, value)
        if shell_map.get("write_scope") and not merged.get("area"):
            _fill(merged, "write_scope", shell_map.get("write_scope"))
    for key, value in item.items():
        if key == "id":
            continue
        _fill(merged, key, value)
    if line_id:
        merged["line_id"] = line_id
    if outcome_map is not None:
        if not explicit_pass and "passed" in outcome_map:
            merged["passed"] = outcome_map.get("passed")
        for key, value in outcome_map.items():
            # Outcome.duration_secs is clock minus the current run's start.
            # A native relaunch stores a new ``_Run`` and drops the first
            # start, so 100 then 180 then pass at 200 becomes 20. The
            # snapshot has no earlier start to add back. Leave it unknown
            # unless the line row recorded the complete span.
            if key in {"passed", "duration_secs"}:
                continue
            _fill(merged, key, value)
        if outcome_map.get("model"):
            _fill(merged, "provider", outcome_map.get("model"))
    if shell_map is not None:
        # A row that already names its cohort still keeps later selection
        # calls, and review history that the line row did not repeat.
        _attach_calls(
            merged,
            entries,
            scope=scope,
            line_id=line_id or dispatch_id,
            keep_samples="call_latencies" not in item,
            keep_events="fallback_events" not in item,
        )
        for key in ("history", "review_attempts", "review_retry"):
            if key not in item:
                _fill(merged, key, shell_map.get(key))
    if line_id:
        _apply_ledger(merged, cost_lines.get(line_id))
    return line_from_mapping(merged, day=day, dispatch_id=dispatch_id or line_id)


def _audit_flags(gates: Mapping[str, Any]) -> dict[str, set[str]]:
    """Applied scheduler interventions, keyed by line id.

    A refused request did not happen. ``human_reassign`` and ``human_unblock``
    are the kinds ``note_human_reassign_audit`` writes.
    """
    scheduler = gates.get("scheduler")
    audit = scheduler.get("audit") if isinstance(scheduler, Mapping) else None
    found: dict[str, set[str]] = {}
    if not isinstance(audit, list):
        return found
    for entry in audit:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("result") or "").strip().lower() != "applied":
            continue
        line_id = str(entry.get("line_id") or "")
        if not line_id:
            continue
        kind = str(entry.get("kind") or "").strip().lower()
        flag = ""
        if kind in {"human_reassign", "reassign"}:
            flag = "human_reassign"
        elif kind in {"human_unblock", "unblock"}:
            flag = "human_unblock"
        if flag:
            found.setdefault(line_id, set()).add(flag)
    return found


def _g5_complete(gates: Mapping[str, Any]) -> tuple[bool | None, bool]:
    """Finished G5, and whether that reading is a pass-rate sample.

    Missing integration stays unknown and the line outcome still counts.
    ``pending``, ``merging`` and ``running`` are still in progress: they are
    not failures, and they are not pass-rate samples. ``failed`` is a
    finished miss. Only ``passed`` is a finished pass.
    """
    integration = gates.get("integration")
    if not isinstance(integration, Mapping) or "phase" not in integration:
        return None, True
    phase = str(integration.get("phase") or "").strip().casefold()
    if phase in _OPEN_G5_PHASES:
        return None, False
    return phase == "passed", True


def _stamp(
    line: LineStat,
    flags: Mapping[str, set[str]],
    g5: bool | None,
    *,
    g5_resolved: bool = True,
) -> LineStat:
    marked = flags.get(line.line_id, set())
    if "human_reassign" in marked:
        line.human_reassign = True
    if "human_unblock" in marked:
        line.human_unblock = True
    if g5 is not None:
        line.g5_passed = g5
    if not g5_resolved:
        line.outcome_resolved = False
    return line


def _dispatch_day(dispatch: Mapping[str, Any]) -> str | None:
    if dispatch.get("day"):
        return str(dispatch["day"])
    created = str(dispatch.get("created_at") or "")
    if len(created) >= 10 and created[4] == "-" and created[7] == "-":
        return created[:10]
    return None


def _cost_lines(gates: Mapping[str, Any]) -> Mapping[str, Any]:
    cost = gates.get("cost")
    if not isinstance(cost, Mapping):
        return {}
    lines = cost.get("lines")
    return lines if isinstance(lines, Mapping) else {}


def iter_lines(dispatch: Mapping[str, Any], *, scope: str = "") -> list[LineStat]:
    """Lines joined with their outcomes and the first assignment on the gate."""
    day = _dispatch_day(dispatch)
    dispatch_id = str(dispatch.get("id") or "")
    outcomes = dispatch.get("outcomes") if isinstance(dispatch.get("outcomes"), Mapping) else {}
    gates = dispatch.get("gates") if isinstance(dispatch.get("gates"), Mapping) else {}
    stored = gates.get("lines") if isinstance(gates, Mapping) else None
    gate_lines = stored if isinstance(stored, Mapping) else {}
    cost_lines = _cost_lines(gates)
    flags = _audit_flags(gates)
    g5, g5_resolved = _g5_complete(gates)
    flat = dispatch.get("lines")
    if isinstance(flat, list):
        return [
            _stamp(
                _line_from_record(
                    item,
                    outcomes=outcomes,
                    gate_lines=gate_lines,
                    cost_lines=cost_lines,
                    day=day,
                    dispatch_id=dispatch_id,
                    scope=scope,
                ),
                flags,
                g5,
                g5_resolved=g5_resolved,
            )
            for item in flat
            if isinstance(item, Mapping)
        ]
    if isinstance(flat, Mapping) and flat and all(isinstance(value, Mapping) for value in flat.values()):
        rows: list[LineStat] = []
        for line_id, value in flat.items():
            item = dict(value)
            item.setdefault("line_id", line_id)
            rows.append(
                _stamp(
                    _line_from_record(
                        item,
                        outcomes=outcomes,
                        gate_lines=gate_lines,
                        cost_lines=cost_lines,
                        day=day,
                        dispatch_id=str(line_id),
                        scope=scope,
                    ),
                    flags,
                    g5,
                    g5_resolved=g5_resolved,
                )
            )
        return rows
    if not isinstance(stored, Mapping):
        return []
    lines: list[LineStat] = []
    for line_id, shell in stored.items():
        if not isinstance(shell, Mapping):
            continue
        outcome = outcomes.get(line_id) if isinstance(outcomes, Mapping) else None
        outcome_map = outcome if isinstance(outcome, Mapping) else None
        lines.append(
            _stamp(
                _line_from_gates(
                    str(line_id),
                    shell,
                    outcome_map,
                    day=day,
                    cost_bucket=cost_lines.get(str(line_id)),
                    scope=scope,
                ),
                flags,
                g5,
                g5_resolved=g5_resolved,
            )
        )
    return lines


def load_snapshot(payload: object) -> list[LineStat]:
    """A dispatch, a list of dispatches, or ``{"dispatches": [...]}``.

    Each dispatch gets its own call-identity scope. A shared digest collapses
    inside that dispatch only.
    """
    if isinstance(payload, list):
        dispatches = payload
    elif isinstance(payload, Mapping) and isinstance(payload.get("dispatches"), list):
        dispatches = payload["dispatches"]
    elif isinstance(payload, Mapping):
        dispatches = [payload]
    else:
        raise ValueError("snapshot must be a dispatch, a list, or an object with dispatches")
    lines: list[LineStat] = []
    for index, dispatch in enumerate(dispatches):
        if isinstance(dispatch, Mapping):
            scope = f"{index}:{dispatch.get('id') or ''}"
            lines.extend(iter_lines(dispatch, scope=scope))
    return lines


def median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2


def percentile_nearest(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile. P95 of 20 values is the 19th ordered value."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return float(ordered[rank - 1])


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _cell_status(count: int) -> str:
    return "ok" if count >= MIN_CELL else "insufficient"


def _rate_cell(hits: int, total: int, *, precise: bool = False) -> dict[str, Any]:
    """A rate cell. ``precise`` keeps ``hits / n`` unrounded.

    This report's rollback compares ``hits`` and ``n``. ``modes.rollback_due``
    does not, so the SPEC slice copies the cell and replaces ``rate``.
    """
    status = _cell_status(total)
    rate = None if total == 0 else hits / total
    if not precise:
        rate = _round(rate)
    return {"rate": rate, "n": total, "hits": hits, "status": status}


def _mean_cell(values: Sequence[float]) -> dict[str, Any]:
    status = _cell_status(len(values))
    if not values:
        return {"mean": None, "n": 0, "status": "insufficient"}
    return {"mean": _round(sum(values) / len(values)), "n": len(values), "status": status}


def _median_cell(values: Sequence[float], *, precise: bool = False) -> dict[str, Any]:
    value = median(values)
    return {
        "median": value if precise else _round(value),
        "n": len(values),
        "status": _cell_status(len(values)),
    }


def _group_lines(lines: Sequence[LineStat], name: str) -> list[LineStat]:
    return [line for line in lines if line.group() == name]


def _cost_values(lines: Sequence[LineStat], kind: str) -> list[float]:
    found: list[float] = []
    for line in lines:
        if not line.counts_toward_cost():
            continue
        if kind == "wall" and line.wall_clock_secs is not None:
            found.append(line.wall_clock_secs)
    return found


def _tokens_by_basis(lines: Sequence[LineStat]) -> dict[str, list[float]]:
    """Author tokens split by ``token_basis``. cli and estimate are not added."""
    found: dict[str, list[float]] = {}
    for line in lines:
        if not line.counts_toward_cost():
            continue
        if line.tokens is None or not line.token_basis:
            continue
        found.setdefault(line.token_basis, []).append(float(line.tokens))
    return found


def _token_basis_cells(
    groups: Mapping[str, Sequence[LineStat]],
) -> dict[str, dict[str, dict[str, Any]]]:
    per = {name: _tokens_by_basis(grouped) for name, grouped in groups.items()}
    names = sorted({basis for values in per.values() for basis in values})
    return {
        basis: {
            name: _median_cell(per[name].get(basis, []))
            for name in ("superagent", "selector")
        }
        for basis in names
    }


def _public_token_cells(
    by_basis: Mapping[str, Mapping[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Top-level cells only when every token shares one basis.

    Two bases stay under ``by_basis`` so a reader cannot add them.
    """
    if len(by_basis) == 1:
        public: dict[str, Any] = dict(next(iter(by_basis.values())))
    else:
        public = {
            "superagent": _median_cell([]),
            "selector": _median_cell([]),
        }
    public["by_basis"] = by_basis
    return public


def _token_cost_reasons(
    by_basis: Mapping[str, Mapping[str, Mapping[str, Any]]],
    limit: float,
    *,
    promotion: bool,
) -> tuple[list[str], bool]:
    """Compare each basis on its own.

    Returns reason strings and whether a shared basis was conclusive.
    A side with no tokens is insufficient for promotion. cli versus estimate
    is not a comparison and does not roll back.
    """
    reasons: list[str] = []
    present = {
        name: [basis for basis, cells in by_basis.items() if int(cells[name].get("n") or 0) > 0]
        for name in ("superagent", "selector")
    }
    if not by_basis or not present["superagent"] or not present["selector"]:
        if promotion:
            if not present["superagent"]:
                reasons.append("insufficient:cost_tokens_superagent")
            if not present["selector"]:
                reasons.append("insufficient:cost_tokens_selector")
        return reasons, False
    shared = set(present["superagent"]) & set(present["selector"])
    if not shared:
        # Both sides recorded tokens, but not on one basis. That is not a
        # comparison. Promotion must not treat the silence as a pass.
        return reasons, False
    conclusive = False
    over = False
    small_left = False
    small_right = False
    for basis in sorted(shared):
        left = by_basis[basis]["superagent"]
        right = by_basis[basis]["selector"]
        if left.get("status") != "ok" or right.get("status") != "ok":
            small_left = small_left or left.get("status") != "ok"
            small_right = small_right or right.get("status") != "ok"
            continue
        conclusive = True
        if _ratio_high(left.get("median"), right.get("median"), limit):
            over = True
    if promotion and small_left:
        reasons.append("insufficient:cost_tokens_superagent")
    if promotion and small_right:
        reasons.append("insufficient:cost_tokens_selector")
    if over:
        reasons.append("cost_tokens")
    return reasons, conclusive


def _duration_values(lines: Sequence[LineStat]) -> list[float]:
    """First launch to PASSED. A failed dispatch G5 does not drop the sample."""
    found: list[float] = []
    for line in lines:
        if not line.counts_toward_duration():
            continue
        if line.duration_secs is not None:
            found.append(line.duration_secs)
    return found


def _usage_missing(lines: Sequence[LineStat]) -> int:
    missing = 0
    for line in lines:
        if line.skipped or line.self_inflicted:
            continue
        if line.group() is None:
            continue
        if line.tokens is None or line.wall_clock_secs is None:
            missing += 1
    return missing


def _selection_samples(
    lines: Sequence[LineStat],
) -> tuple[list[float], list[int | float]]:
    """Latency and tokens for the selection calls represented by ``lines``.

    Records that share a call identity count once. Passing one provider's
    members, rather than the whole dispatch, keeps that call in every
    provider and area that joined it. Flat rows have no call identity and
    each stored sample counts.
    """
    latencies: list[float] = []
    tokens: list[int | float] = []
    chosen: dict[tuple[Any, ...], list[int | float | None]] = {}
    order: list[tuple[Any, ...]] = []
    for line in sorted((line for line in lines if not line.skipped), key=lambda item: item.line_id):
        if not line.call_records:
            latencies.extend(float(value) for value in line.call_latencies)
            tokens.extend(line.call_token_samples)
            continue
        for identity, latency, total in line.call_records:
            if identity not in chosen:
                chosen[identity] = [latency, total]
                order.append(identity)
                continue
            saved = chosen[identity]
            if saved[0] is None:
                saved[0] = latency
            if saved[1] is None:
                saved[1] = total
    for identity in order:
        latency, total = chosen[identity]
        if latency is not None:
            latencies.append(float(latency))
        if total is not None:
            tokens.append(total)
    return latencies, tokens


def _side_token_cell(lines: Sequence[LineStat]) -> dict[str, Any]:
    """One group's tokens. Mixed bases stay under ``by_basis`` and are not added."""
    by_basis = {
        basis: _median_cell(values)
        for basis, values in sorted(_tokens_by_basis(lines).items())
    }
    if len(by_basis) == 1:
        public: dict[str, Any] = dict(next(iter(by_basis.values())))
    else:
        public = _median_cell([])
    public["by_basis"] = by_basis
    return public


def _decision_rates(lines: Sequence[LineStat]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Feasibility and fallback over lines that had an assignment decision.

    The first launch can be ``off`` and still define the cohort. A later
    shadow or enforce entry is the decision those rates use.
    """
    eligible = [line for line in lines if line.decided and not line.skipped]
    known = [line for line in eligible if line.decision_feasible is not None]
    feasibility = _rate_cell(
        sum(1 for line in known if line.decision_feasible),
        len(known),
    )
    fallback = _rate_cell(sum(1 for line in eligible if line.fallback), len(eligible))
    return feasibility, fallback


def _group_cell(side: Sequence[LineStat]) -> dict[str, Any]:
    """Pass rate plus the other §6.2 metrics for one group inside a bucket.

    ``rate``, ``n`` and ``status`` stay on the cell. A bucket under 10
    samples is ``insufficient`` and is not a conclusion. The pass sample
    drops unfinished outcomes; the other metrics keep the whole bucket.
    """
    conclusive = [line for line in side if line.outcome_resolved]
    hits = sum(1 for line in conclusive if line.clean_pass())
    cell = _rate_cell(hits, len(conclusive))
    passed = [line for line in side if line.passed and line.review_blocks is not None]
    cell["review_rounds"] = _mean_cell([float(line.review_blocks or 0) for line in passed])
    cell["wall_clock"] = _median_cell(_cost_values(side, "wall"))
    cell["tokens"] = _side_token_cell(side)
    cell["duration"] = _median_cell(_duration_values(side))
    feasibility, fallback = _decision_rates(side)
    cell["feasibility_rate"] = feasibility
    cell["fallback_rate"] = fallback
    latencies, _member_tokens = _selection_samples(side)
    cell["call_overhead"] = {
        "p50_ms": _round(median(latencies)),
        "p95_ms": _round(percentile_nearest(latencies, 95)),
        "n": len(latencies),
        "status": _cell_status(len(latencies)),
    }
    return cell


def _provider_area(lines: Sequence[LineStat]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], list[LineStat]] = {}
    for line in lines:
        if line.group() is None:
            continue
        buckets.setdefault((line.provider, line.area), []).append(line)
    cells: list[dict[str, Any]] = []
    for provider, area in sorted(buckets):
        grouped = buckets[(provider, area)]
        cell: dict[str, Any] = {"provider": provider, "area": area}
        for name in ("superagent", "selector"):
            side = [line for line in grouped if line.group() == name]
            cell[name] = _group_cell(side)
        cells.append(cell)
    return cells


def _paid_credits(lines: Sequence[LineStat]) -> dict[str, Any]:
    by_day: dict[str, float] = {}
    known = 0
    missing = 0
    for line in lines:
        if line.paid_credits is None:
            missing += 1
            continue
        known += 1
        day = line.day or "unknown"
        by_day[day] = by_day.get(day, 0.0) + line.paid_credits
    rows = [{"day": day, "amount": _round(by_day[day])} for day in sorted(by_day)]
    return {
        "known": known > 0,
        "by_day": rows,
        "lines_with_spend": known,
        "lines_missing": missing,
    }


def _codexbar(lines: Sequence[LineStat]) -> dict[str, Any]:
    known = [line.codexbar_delta for line in lines if line.codexbar_delta is not None]
    if not known:
        return {"known": False, "delta": None, "n": 0}
    return {"known": True, "delta": _round(sum(known)), "n": len(known)}


def _ratio_high(treatment: float | None, control: float | None, limit: float) -> bool | None:
    """True when treatment is higher than ``limit`` times control.

    ``None`` when either side is missing. A control of 0 is higher only when
    the treatment is above 0; two zeros are not an increase. The ratio is
    rounded to six decimals first, so a float product cannot turn an exact
    15% or 50% increase into a miss.
    """
    if treatment is None or control is None:
        return None
    if control == 0:
        return treatment > 0
    return round(treatment / control, 6) > limit


def _promotion(
    pass_cells: Mapping[str, Mapping[str, Any]],
    review_cells: Mapping[str, Mapping[str, Any]],
    wall: Mapping[str, Mapping[str, Any]],
    token_by_basis: Mapping[str, Mapping[str, Mapping[str, Any]]],
    duration: Mapping[str, Mapping[str, Any]],
    feasibility: Mapping[str, Any],
    fallback: Mapping[str, Any],
    overhead: Mapping[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []

    def _need(cell: Mapping[str, Any], name: str) -> bool:
        if cell.get("status") != "ok":
            reasons.append(f"insufficient:{name}")
            return False
        return True

    sa_pass = pass_cells["superagent"]
    sel_pass = pass_cells["selector"]
    # Adopted lines are the Super-agent group. Ten conclusive lines are not
    # the forty Q8 requires before stage B can move to C.
    if int(sa_pass.get("n") or 0) < ADOPTED_MIN:
        reasons.append("insufficient:adopted_lines")
    pass_ready = _need(sa_pass, "pass_rate_superagent") and _need(sel_pass, "pass_rate_selector")
    gain: float | None = None
    if pass_ready and sa_pass["rate"] is not None and sel_pass["rate"] is not None:
        # Round before the limit check. 0.95 - 1.0 is a hair under -0.05 in
        # binary floats, and an exact five-point gap must not fail the gate.
        gain = round(sa_pass["rate"] - sel_pass["rate"], 6)
        if gain < -PASS_GAP_LIMIT:
            reasons.append("pass_rate_gap")
    sa_review = review_cells["superagent"]
    sel_review = review_cells["selector"]
    if _need(sa_review, "review_rounds_superagent") and _need(sel_review, "review_rounds_selector"):
        if (
            sa_review["mean"] is not None
            and sel_review["mean"] is not None
            and round(sa_review["mean"] - sel_review["mean"], 6) > REVIEW_MEAN_LIMIT
        ):
            reasons.append("review_rounds")
    cost_waived = gain is not None and gain >= PASS_GAIN_WAIVES_COST
    if not cost_waived:
        left = wall["superagent"]
        right = wall["selector"]
        if _need(left, "cost_wall_clock_superagent") and _need(right, "cost_wall_clock_selector"):
            if _ratio_high(left.get("median"), right.get("median"), COST_RATIO_LIMIT):
                reasons.append("cost_wall_clock")
        token_reasons, conclusive = _token_cost_reasons(
            token_by_basis, COST_RATIO_LIMIT, promotion=True
        )
        reasons.extend(token_reasons)
        if not conclusive and not any(
            reason.startswith("insufficient:cost_tokens") for reason in token_reasons
        ):
            reasons.append("insufficient:cost_tokens")
    left_duration = duration["superagent"]
    right_duration = duration["selector"]
    if _need(left_duration, "duration_superagent") and _need(right_duration, "duration_selector"):
        if _ratio_high(left_duration.get("median"), right_duration.get("median"), DURATION_RATIO_LIMIT):
            reasons.append("duration")
    if _need(feasibility, "feasibility"):
        if feasibility["rate"] is None or feasibility["rate"] < FEASIBILITY_MIN:
            reasons.append("feasibility")
    if _need(fallback, "fallback"):
        if fallback["rate"] is None or fallback["rate"] > FALLBACK_MAX:
            reasons.append("fallback")
    if _need(overhead, "latency"):
        p95 = overhead.get("p95_ms")
        if p95 is None or p95 > LATENCY_P95_MS:
            reasons.append("latency_p95")
    return {"ready": not reasons, "reasons": reasons, "pass_rate_gain": _round(gain)}


def _consumer_rate(hits: int, total: int) -> float | None:
    """Pass fraction that survives ``Decimal(str(float(rate)))``.

    Quantizing the exact hits/n fraction, then formatting it, is what keeps
    an exact 10-point gap on the consumer's side of the comparison.
    """
    if total <= 0:
        return None
    exact = (Decimal(hits) / Decimal(total)).quantize(
        _SPEC_RATE_PLACES, rounding=ROUND_HALF_EVEN
    )
    return float(format(exact, "f"))


def _spec_pass_rate(
    pass_cells: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Copy pass cells and replace ``rate`` with the consumer reading.

    The public cells stay raw. Sharing the dicts would quantize promotion
    and the report's own displayed rate as well.
    """
    exported: dict[str, dict[str, Any]] = {}
    for name, cell in pass_cells.items():
        copied = dict(cell)
        total = copied.get("n")
        hits = copied.get("hits")
        if (
            isinstance(total, int)
            and not isinstance(total, bool)
            and isinstance(hits, int)
            and not isinstance(hits, bool)
            and total > 0
        ):
            copied["rate"] = _consumer_rate(hits, total)
        exported[name] = copied
    return exported


def _pass_drop_fires(
    selector_hits: int,
    selector_total: int,
    super_hits: int,
    super_total: int,
) -> bool:
    """Compare the exact hit fractions; exactly 10 points does not fire."""
    if selector_total <= 0 or super_total <= 0:
        return False
    drop_numerator = selector_hits * super_total - super_hits * selector_total
    threshold = Decimal(str(ROLLBACK_PASS_GAP)) * Decimal(100)
    return drop_numerator * 100 > threshold * selector_total * super_total


def _wall_cost_rose(super_cost: float, selector_cost: float) -> bool:
    """More than 50% higher wall-clock cost. A zero selector cost does not fire.

    The rise is a decimal ratio, the same comparison ``rollback_due`` uses.
    An exact 50% rise stays.
    """
    if (
        not math.isfinite(super_cost)
        or not math.isfinite(selector_cost)
        or selector_cost <= 0
        or super_cost < 0
    ):
        return False
    rise = (Decimal(str(super_cost)) - Decimal(str(selector_cost))) / Decimal(str(selector_cost))
    return rise > Decimal(str(ROLLBACK_COST_RATIO)) - Decimal(1)


def _rollback(
    pass_cells: Mapping[str, Mapping[str, Any]],
    wall: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    reasons: list[str] = []
    conclusive = False
    sa_pass = pass_cells["superagent"]
    sel_pass = pass_cells["selector"]
    if sa_pass.get("status") == "ok" and sel_pass.get("status") == "ok":
        conclusive = True
        if _pass_drop_fires(
            int(sel_pass["hits"]),
            int(sel_pass["n"]),
            int(sa_pass["hits"]),
            int(sa_pass["n"]),
        ):
            reasons.append("pass_rate")
    left_wall = wall["superagent"]
    right_wall = wall["selector"]
    if left_wall.get("status") == "ok" and right_wall.get("status") == "ok":
        conclusive = True
        left_median = left_wall.get("median")
        right_median = right_wall.get("median")
        if (
            isinstance(left_median, (int, float))
            and not isinstance(left_median, bool)
            and isinstance(right_median, (int, float))
            and not isinstance(right_median, bool)
            and _wall_cost_rose(float(left_median), float(right_median))
        ):
            reasons.append("cost_per_line")
    if reasons:
        notice = (
            "N73 assignment would return to shadow: "
            + ", ".join(reasons)
            + ". Notify the Planner. This report does not change the mode."
        )
        return {"to_shadow": True, "status": "rollback", "reasons": reasons, "notice": notice}
    if not conclusive:
        return {
            "to_shadow": False,
            "status": "insufficient",
            "reasons": [],
            "notice": None,
        }
    return {"to_shadow": False, "status": "ok", "reasons": [], "notice": None}


def build_report(lines: Sequence[LineStat], *, stage: str = "C") -> dict[str, Any]:
    """Metrics for one snapshot. ``stage`` selects which decision applies."""
    selected = stage.upper()
    groups = {name: _group_lines(lines, name) for name in ("superagent", "selector")}
    # Unfinished outcomes are not failures. Leave them out of the sample or a
    # still-running cohort looks like a pass-rate collapse.
    pass_cells = {
        name: _rate_cell(
            sum(1 for line in grouped if line.outcome_resolved and line.clean_pass()),
            sum(1 for line in grouped if line.outcome_resolved),
            precise=True,
        )
        for name, grouped in groups.items()
    }
    review_cells = {}
    for name, grouped in groups.items():
        passed = [line for line in grouped if line.passed and line.review_blocks is not None]
        review_cells[name] = _mean_cell([float(line.review_blocks or 0) for line in passed])
    # Review ERROR and INCONCLUSIVE are counted on their own, including lines
    # that fell back, and they are not added into the review-round mean.
    inconclusive = sum(line.inconclusive for line in lines)
    review_errors = sum(line.review_errors for line in lines)
    wall = {
        name: _median_cell(_cost_values(grouped, "wall"), precise=True)
        for name, grouped in groups.items()
    }
    token_by_basis = _token_basis_cells(groups)
    token_cells = _public_token_cells(token_by_basis)
    duration = {name: _median_cell(_duration_values(grouped)) for name, grouped in groups.items()}
    feasibility, fallback = _decision_rates(lines)
    latencies, call_tokens = _selection_samples(lines)
    overhead = {
        "p50_ms": _round(median(latencies)),
        "p95_ms": _round(percentile_nearest(latencies, 95)),
        "n": len(latencies),
        "status": _cell_status(len(latencies)),
        "tokens": {
            "total": sum(call_tokens) if call_tokens else None,
            "median": _round(median([float(value) for value in call_tokens])),
            "n": len(call_tokens),
        },
    }
    bases = sorted({line.token_basis for line in lines if line.token_basis})
    promotion = _promotion(
        pass_cells, review_cells, wall, token_by_basis, duration, feasibility, fallback, overhead
    )
    rollback = _rollback(pass_cells, wall)
    promotion["applies"] = selected == "B"
    rollback["applies"] = selected == "C"
    if selected == "A":
        promotion["applies"] = False
        rollback["applies"] = False
    # The slice ``modes.rollback_due`` reads. ``cost_per_line`` is the
    # wall-clock median, flat under each cohort. A nested ``cost`` object
    # is an alias of that metric and would hide these cells. Token medians
    # stay on the report; that consumer has no token arm.
    rollback_mapping = {
        "pass_rate": _spec_pass_rate(pass_cells),
        "review_rounds": review_cells,
        "cost_per_line": wall,
        "time_per_line": duration,
        "feasibility_rate": feasibility,
        "fallback_rate": fallback,
        "call_overhead": overhead,
    }
    return {
        "schema": SCHEMA,
        "stage": selected,
        "counts": {
            "lines": len(lines),
            "skipped": sum(1 for line in lines if line.skipped),
            "fallbacks": sum(line.fallback_events for line in lines),
            "assignment_errors": sum(line.assignment_error_events for line in lines),
            "review_errors": review_errors,
            "inconclusive": inconclusive,
            "superagent": len(groups["superagent"]),
            "selector": len(groups["selector"]),
        },
        "pass_rate": pass_cells,
        "review_rounds": review_cells,
        "cost": {
            "wall_clock": wall,
            "tokens": token_cells,
            "usage_missing": _usage_missing(lines),
            "token_basis": bases,
        },
        "duration": duration,
        "feasibility_rate": feasibility,
        "fallback_rate": fallback,
        "call_overhead": overhead,
        "by_provider_area": _provider_area(lines),
        "paid_credits": _paid_credits(lines),
        "codexbar": _codexbar(lines),
        "promotion": promotion,
        "rollback": rollback,
        "spec": {"modes.rollback_due": rollback_mapping},
    }


def load_path(path: Path) -> list[LineStat]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return load_snapshot(payload)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only N73 assignment report")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--stage", default="C", choices=["A", "B", "C", "a", "b", "c"])
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.snapshot.is_file():
        print(f"snapshot not found: {args.snapshot}", file=sys.stderr)
        return 2
    try:
        lines = load_path(args.snapshot)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"snapshot unreadable: {exc.__class__.__name__}", file=sys.stderr)
        return 2
    report = build_report(lines, stage=str(args.stage))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
