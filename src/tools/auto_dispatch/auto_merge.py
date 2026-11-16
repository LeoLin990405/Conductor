"""High-risk paths that keep a dispatch on the human G6 gate (N73 §3.3).

Second-reviewer coverage stays on ``DEFAULT_RISK_GLOBS``. This list decides
whether G6 may auto-approve. The integrate branch lands every line
together, so one high-risk line blocks auto-approval of the whole
dispatch. Push and merge to the default branch stay outside this decision.

``CONDUCTOR_DISPATCH_HIGH_RISK_GLOBS`` may only add patterns. A deployment
that shortens the list would auto-approve a dispatch this set is here to
hold.

:func:`auto_approve_decision` is the pure S7 / §5.3 predicate L4's
``_open_g6`` calls: low risk, G2 passed, every required non-author review
MERGE, G5 passed, and no INCONCLUSIVE, ERROR, unparseable reply, park or
human unblock. ``CONDUCTOR_DISPATCH_AUTO_APPROVE=0`` keeps G6 human.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

HIGH_RISK_ENV = "CONDUCTOR_DISPATCH_HIGH_RISK_GLOBS"

# PRD 3.3, in table order. ``*`` does not cross ``/``. Each ``**/name*``
# category glob also has a ``**/name*/**`` twin: fullmatch alone would let
# ``src/server/auth/jwt_bearer.py`` or ``src/ptc_agent/core/sandbox/*.py``
# (the auth and isolation code the category names) auto-approve.
HIGH_RISK_GLOBS: tuple[str, ...] = (
    # Dispatch state machine.
    "src/tools/auto_dispatch/machine.py",
    # Git and sandbox launch.
    "scripts/dispatch_launch.sh",
    "src/tools/auto_dispatch/execution.py",
    "src/tools/auto_dispatch/delivery.py",
    "src/server/services/autonomy/git_delivery.py",
    "scripts/conductor_local_daemon.py",
    # Isolation.
    "src/llms/native_cli/isolation.py",
    "src/conductor_cli/daemon_policy.py",
    "src/conductor_cli/daemon_capability.py",
    "**/sandbox*",
    "**/sandbox*/**",
    # The defence itself.
    "src/tools/auto_dispatch/gates.py",
    "src/server/services/autonomy/stage_gate.py",
    "src/tools/auto_dispatch/approval.py",
    "src/tools/auto_dispatch/auto_merge.py",
    "src/tools/auto_dispatch/superagent_assign/feasibility.py",
    # Migrations and database.
    "migrations/**",
    "src/server/database/**",
    "src/conductor_cli/db_guard.py",
    # Auth and secrets. failure.py is the worker-output redaction path.
    "**/auth*",
    "**/auth*/**",
    "**/credential*",
    "**/credential*/**",
    "**/secret*",
    "**/secret*/**",
    "**/keychain*",
    "**/keychain*/**",
    "src/llms/native_cli/failure.py",
    # Deploy config.
    "config.yaml",
    "**/.env*",
    "deploy/**",
    ".github/workflows/**",
)

__all__ = [
    "AUTO_APPROVER",
    "AUTO_APPROVE_ENV",
    "HIGH_RISK_ENV",
    "HIGH_RISK_GLOBS",
    "AutoApproval",
    "LineApprovalFacts",
    "auto_approve_decision",
    "auto_approve_enabled",
    "high_risk_globs",
    "is_high_risk",
]

#: S7: auto-approval is on unless this is ``0`` / ``false`` / ``no`` / ``off``.
AUTO_APPROVE_ENV = "CONDUCTOR_DISPATCH_AUTO_APPROVE"
#: The approver the G6 audit records for an automatic approval.
AUTO_APPROVER = "auto:n73"
_FALSY = frozenset({"0", "false", "no", "off"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Review results that stop auto-approval (PRD 5.3). Only MERGE approves.
_MERGE = "merge"
_INCONCLUSIVE = frozenset({"inconclusive"})
_REVIEW_ERRORS = frozenset({"error", "review_unavailable"})
_UNPARSEABLE = frozenset({"review_unparseable", "unparseable"})

# Reason codes, in the order the checks run.
REASON_DISABLED = "auto_approve_disabled"
REASON_NO_LINES = "no_lines"
REASON_G5 = "g5_not_passed"
REASON_HUMAN_UNBLOCK = "review_unavailable_unblocked"
REASON_HIGH_RISK = "high_risk"
REASON_PARKED = "parked"
REASON_G2 = "g2_not_passed"
REASON_INCONCLUSIVE = "review_inconclusive"
REASON_REVIEW_ERROR = "review_error"
REASON_UNPARSEABLE = "review_unparseable"
REASON_NOT_MERGE = "review_not_merge"
REASON_SECOND_REVIEW = "second_review_missing"


def high_risk_globs(*, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Built-in globs plus every pattern the operator appended.

    The value is comma-separated, the same spelling as
    ``CONDUCTOR_DISPATCH_RISK_GLOBS``. A missing, blank, or partial value
    leaves every built-in pattern in place. Patterns already present are
    not repeated. ``environ`` defaults to the process environment; a
    mapping passed in is the only source that is read.
    """
    source = os.environ if environ is None else environ
    added: list[str] = []
    seen = set(HIGH_RISK_GLOBS)
    for part in source.get(HIGH_RISK_ENV, "").split(","):
        pattern = part.strip()
        if not pattern or pattern in seen:
            continue
        seen.add(pattern)
        added.append(pattern)
    if not added:
        return HIGH_RISK_GLOBS
    return HIGH_RISK_GLOBS + tuple(added)


def is_high_risk(
    diff_files: Sequence[str] | Sequence[Sequence[str]],
    write_scope: Sequence[str] | Sequence[Sequence[str]],
    risk_bearing: bool | Sequence[bool],
    *,
    globs: Sequence[str] | None = None,
) -> bool:
    """True when auto-approval of the whole dispatch must stay closed.

    The planner flag, the declared write scope, and the files the diff
    actually touched are one union. Pass every line, nested or already
    flattened. Any hit returns True: the integrate branch is one unit.

    A diff entry matches as a path. A write-scope entry matches as a path
    and, when it is a directory or a glob, also covers each concrete
    high-risk path inside it. ``src/tools/auto_dispatch/**`` therefore
    blocks on its own, because ``machine.py`` sits in that scope. A scope
    of ``docs/**`` covers none of the concrete paths.

    Globs fullmatch the whole path. ``*`` and ``?`` stay inside one
    component, and a leading dot that is part of the name is kept, so
    ``.env`` and ``.github`` still match. ``globs`` defaults to
    :data:`HIGH_RISK_GLOBS` and the check does not read the environment.
    Pass the tuple :func:`high_risk_globs` returns when appended patterns
    should count.
    """
    patterns = HIGH_RISK_GLOBS if globs is None else globs
    compiled = _compiled(patterns)
    if _any_flag(risk_bearing):
        return True
    for path in _iter_paths(diff_files, "diff_files"):
        if _path_matches(path, compiled):
            return True
    for scope in _iter_paths(write_scope, "write_scope"):
        if _scope_matches(scope, patterns, compiled):
            return True
    return False


def _any_flag(risk_bearing: bool | Sequence[bool]) -> bool:
    if isinstance(risk_bearing, bool):
        return risk_bearing
    if isinstance(risk_bearing, str) or not isinstance(risk_bearing, Sequence):
        raise TypeError("risk_bearing must be a bool or a sequence of bools")
    return any(risk_bearing)


def _iter_paths(
    values: Sequence[str] | Sequence[Sequence[str]],
    label: str,
) -> Iterable[str]:
    if isinstance(values, str):
        raise TypeError(f"{label} must be a sequence of paths, not a string")
    for item in values:
        if isinstance(item, str):
            yield item
            continue
        if isinstance(item, Sequence):
            for part in item:
                if not isinstance(part, str):
                    raise TypeError(f"{label} entries must be path strings")
                yield part
            continue
        raise TypeError(f"{label} entries must be path strings")


def _compiled(globs: Sequence[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(_compile_glob(pattern) for pattern in globs if pattern)


def _compile_glob(pattern: str) -> re.Pattern[str]:
    """Translate one glob into a whole-path match.

    ``\\A`` and ``\\Z`` are part of the pattern, and callers use
    ``fullmatch``. ``re.search`` of a pattern without those anchors
    accepts a component in the middle of a longer path: ``**/sandbox*``
    would then hit ``.../sandbox/_defaults.py``. ``*`` is ``[^/]*`` and
    ``?`` is ``[^/]``, so neither crosses ``/``. ``**`` crosses components.
    A leading slash on the pattern is not part of a repo-relative path.
    """
    text = pattern.replace("\\", "/").lstrip("/")
    parts = ["\\A"]
    index = 0
    while index < len(text):
        if text.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif text.startswith("/**", index) and index + 3 == len(text):
            parts.append("(?:/.*)?")
            index += 3
        elif text.startswith("**", index):
            parts.append(".*")
            index += 2
        elif text[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif text[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(text[index]))
            index += 1
    parts.append("\\Z")
    return re.compile("".join(parts))


def _normalize(path: str) -> str:
    """Repo-relative form. Only a ``./`` prefix is removed.

    A leading dot that belongs to the name stays, so ``.env`` and
    ``.github/...`` still match their globs.
    """
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _path_matches(path: str, compiled: Sequence[re.Pattern[str]]) -> bool:
    normalized = _normalize(path)
    if not normalized:
        return False
    return any(regex.fullmatch(normalized) is not None for regex in compiled)


def _is_concrete(pattern: str) -> bool:
    return "*" not in pattern and "?" not in pattern


def _scope_covers_concrete(scope: str, concrete: str) -> bool:
    """Whether a write-scope entry contains one concrete high-risk path."""
    if not scope or not concrete:
        return False
    if "*" in scope or "?" in scope:
        return _compile_glob(scope).fullmatch(concrete) is not None
    trimmed = scope.rstrip("/")
    return concrete == trimmed or concrete.startswith(trimmed + "/")


def _scope_matches(
    scope: str,
    patterns: Sequence[str],
    compiled: Sequence[re.Pattern[str]],
) -> bool:
    if _path_matches(scope, compiled):
        return True
    normalized = _normalize(scope)
    if not normalized:
        return False
    for pattern in patterns:
        if pattern and _normalize(pattern) == normalized:
            return True
    for pattern in patterns:
        if (
            pattern
            and _is_concrete(pattern)
            and _scope_covers_concrete(normalized, _normalize(pattern))
        ):
            return True
    return False


def auto_approve_enabled(*, environ: Mapping[str, str] | None = None) -> bool:
    """S7 switch. Default OFF (review ruling 2026-11-14): only
    ``CONDUCTOR_DISPATCH_AUTO_APPROVE=1`` (true/yes/on) turns it on, and even
    then auto-approval only marks the integrate branch ``auto_ready``; push
    and PR stay a human step."""
    source = os.environ if environ is None else environ
    return source.get(AUTO_APPROVE_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class LineApprovalFacts:
    """What G6 needs to know about one line. Gathered by the caller (L4).

    ``reviews`` is the final verdict of each required non-author reviewer
    (``merge`` / ``block`` / ``inconclusive`` / ``error``), primary first.
    ``review_history`` is every verdict this line received, so an earlier
    INCONCLUSIVE, ERROR or unparseable reply is still seen after a later
    MERGE. ``parked`` is any park or block the line went through.
    """

    line_id: str
    g2_passed: bool
    reviews: Sequence[str]
    second_review_required: bool = False
    review_history: Sequence[str] = ()
    parked: bool = False
    diff_files: Sequence[str] = ()
    write_scope: Sequence[str] = ()
    risk_bearing: bool = False


@dataclass(frozen=True)
class AutoApproval:
    """``approve`` with ``approver`` set, or a human G6 with the first reason."""

    approve: bool
    reason: str = ""
    approver: str = ""


def _verdict(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().casefold()


def auto_approve_decision(
    lines: Sequence[LineApprovalFacts],
    *,
    g5_passed: bool,
    review_unavailable_unblocked: bool = False,
    environ: Mapping[str, str] | None = None,
    globs: Sequence[str] | None = None,
) -> AutoApproval:
    """PRD S7 and 5.3: auto-approve G6 only for a low-risk, all-MERGE dispatch.

    Every line needs G2 passed and a MERGE from each required non-author
    reviewer (both, when a second review is required), and G5 must have
    passed. Any high-risk path (actual diff, declared scope, planner flag)
    on any line keeps the whole dispatch human, because the integrate
    branch is one unit. INCONCLUSIVE, ERROR, an unparseable reply, a park,
    or a human unblock after ``review_unavailable`` also keep it human.
    ``CONDUCTOR_DISPATCH_AUTO_APPROVE=0`` keeps every dispatch human.
    ``globs`` defaults to :func:`high_risk_globs` of ``environ``, so
    appended patterns count. The function reads nothing else.
    """
    if not auto_approve_enabled(environ=environ):
        return AutoApproval(False, REASON_DISABLED)
    if not lines:
        return AutoApproval(False, REASON_NO_LINES)
    if g5_passed is not True:
        return AutoApproval(False, REASON_G5)
    if review_unavailable_unblocked:
        return AutoApproval(False, REASON_HUMAN_UNBLOCK)
    patterns = high_risk_globs(environ=environ) if globs is None else tuple(globs)
    if is_high_risk(
        [tuple(line.diff_files) for line in lines],
        [tuple(line.write_scope) for line in lines],
        [bool(line.risk_bearing) for line in lines],
        globs=patterns,
    ):
        return AutoApproval(False, REASON_HIGH_RISK)
    for line in lines:
        reason = _line_reason(line)
        if reason:
            return AutoApproval(False, reason)
    return AutoApproval(True, approver=AUTO_APPROVER)


def _line_reason(line: LineApprovalFacts) -> str:
    if line.parked:
        return REASON_PARKED
    if line.g2_passed is not True:
        return REASON_G2
    seen = [_verdict(item) for item in (*line.review_history, *line.reviews)]
    if any(item in _INCONCLUSIVE for item in seen):
        return REASON_INCONCLUSIVE
    if any(item in _REVIEW_ERRORS for item in seen):
        return REASON_REVIEW_ERROR
    if any(item in _UNPARSEABLE for item in seen):
        return REASON_UNPARSEABLE
    finals = [_verdict(item) for item in line.reviews]
    needed = 2 if line.second_review_required else 1
    if len(finals) < needed:
        return REASON_SECOND_REVIEW if finals else REASON_NOT_MERGE
    if any(item != _MERGE for item in finals):
        return REASON_NOT_MERGE
    return ""


def review_history_from_snapshot(snapshot: Mapping[str, object] | None) -> tuple[str, ...]:
    """Every review verdict a line snapshot records, for ``review_history`` (N59).

    Stage-Gate history keeps ``merge``/``approve``/``block``; a reviewer's
    ``inconclusive`` lives in ``review_status_history`` (``review_triage``),
    because in shadow the drive still acted on it as a BLOCK. A park the
    line went through for evidence (``review_inconclusive``) counts too.
    """
    if not isinstance(snapshot, Mapping):
        return ()
    seen: list[str] = []
    history = snapshot.get("history")
    for entry in history if isinstance(history, list) else []:
        if isinstance(entry, Mapping) and str(entry.get("gate") or "").endswith("review"):
            result = _verdict(entry.get("result"))
            if result:
                seen.append(result)
    statuses = snapshot.get("review_status_history")
    for status in statuses if isinstance(statuses, list) else []:
        if _verdict(status) in _INCONCLUSIVE:
            seen.append("inconclusive")
    if _verdict(snapshot.get("reason")) == "review_inconclusive":
        seen.append("inconclusive")
    return tuple(seen)
