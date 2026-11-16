"""Dispatch lines an interactive app may claim (N80 L4, machine side).

A line the Planner marks ``app_claimable`` can be taken by a registered app
(Claude Code, Codex, ...) instead of a Worker (n80-collab-protocol.md §5.1).
The machine then starts no Worker for it. It waits for the app's report and
treats a ``done`` report as the completion claim that enters G2, exactly as
a Worker's finished run does; review, G5 and G6 are unchanged (§5.3).

Everything here is pure: it reads and writes one service-owned gates key,
``gates.app_line_claims.<line_id>``, and the line fields the caller passes.
The service side (``src/server/services/collab/lines.py``) writes claims,
heartbeats and reports into the same cell; the machine adopts reports,
lapses unclaimed lines and expires leases (N76 R-EXIT).

Gated by ``CONDUCTOR_COLLAB_MODE`` (default ``off``): only ``full`` holds
lines for apps. In off / shadow / docs, a dispatch that has no claim cell
behaves exactly as before (:func:`engaged` is false and no hook does
anything). A dispatch that still carries a live cell from an earlier
``full`` stage has it expired, as §9 asks of a downgrade.

Cell states::

    open      marked line, ready, waiting for an app (claim window running)
    active    an app holds the lease (``expires_at``, ``max_lease_until``);
              the machine prepares the line worktree and names it (``worktree``)
    reported  the app reported; the machine has not adopted it yet
    adopted   the report entered G2 (terminal for the cell)
    lapsed    nobody claimed within the window; a Worker takes the line
    expired   the lease ran out (or the mode left ``full``): R-EXIT
    released  the app gave the line back (or reported anything but done): R-EXIT
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .types import Dispatch, Line, LineStatus

#: The service-owned gates key (listed in the service's ``_SERVICE_GATES``).
GATE_KEY = "app_line_claims"

OPEN = "open"
ACTIVE = "active"
REPORTED = "reported"
ADOPTED = "adopted"
LAPSED = "lapsed"
EXPIRED = "expired"
RELEASED = "released"
STATES = (OPEN, ACTIVE, REPORTED, ADOPTED, LAPSED, EXPIRED, RELEASED)
#: Cells that keep the machine from launching a Worker.
HOLDING = frozenset({OPEN, ACTIVE, REPORTED})
#: Cells whose app still holds the line (a lease is live or a report waits).
LIVE = frozenset({ACTIVE, REPORTED})

#: Park reasons (both R-EXIT in the N76 policy; RUNBOOK rows).
REASON_EXPIRED = "app_lease_expired"
REASON_RELEASED = "app_lease_released"

APP_PREFIX = "app:"

#: §7: 15 minute lease, heartbeat every 60 s, at most 4 h without a report.
LEASE_SECONDS = 15 * 60
MAX_LEASE_SECONDS = 4 * 60 * 60
#: How long a marked, ready line waits for a claim before a Worker takes it.
WINDOW_ENV = "CONDUCTOR_COLLAB_LINE_CLAIM_WINDOW_SECONDS"
DEFAULT_WINDOW_SECONDS = 15 * 60
#: How long the drive sleeps when only app-held lines are left.
POLL_SECONDS = 15.0

#: Statuses of a line that has not started (§5.1).
_UNSTARTED = (LineStatus.ASSIGNED, LineStatus.PENDING)

# Refusal codes of a claim (the service maps them to envelope codes).
R_MODE = "mode_denied"
R_NOT_CLAIMABLE = "not_app_claimable"
R_NOT_READY = "line_not_ready"
R_DEPS = "dependencies_not_passed"
R_TAKEN = "already_claimed"
R_WINDOW = "claim_window_closed"
R_NO_LINE = "no_such_line"
R_NOT_HOLDER = "lease_lost"


def lines_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True only when ``CONDUCTOR_COLLAB_MODE`` is ``full`` *and* line claims are on.

    ``src.collab.modes.LINE_CLAIMS_ENABLED`` is the switch that lets an app
    actually reach a line claim (the collab tool routing). While it is off,
    no app can claim, so no line is held for one and the Planner is not
    told it may mark lines.
    """
    from src.collab import modes

    return modes.LINE_CLAIMS_ENABLED is True and modes.current_mode(environ=env) == modes.FULL


def window_seconds(env: Mapping[str, str] | None = None) -> float:
    source = os.environ if env is None else env
    try:
        value = float(str(source.get(WINDOW_ENV) or DEFAULT_WINDOW_SECONDS))
    except ValueError:
        return float(DEFAULT_WINDOW_SECONDS)
    return value if value >= 0 else float(DEFAULT_WINDOW_SECONDS)


def app_author(app_id: str) -> str:
    """``app:<app_id>``: the author of record of an app-claimed line (shared normaliser)."""
    from src.server.services.autonomy.stage_gate import canonical_app

    return canonical_app(app_id) or APP_PREFIX + str(app_id).strip().lower()


def cells(dispatch: Dispatch) -> dict[str, dict[str, Any]]:
    """The claim cells, read-only view ({} when there are none)."""
    root = dispatch.gates if isinstance(dispatch.gates, dict) else None
    found = root.get(GATE_KEY) if isinstance(root, dict) else None
    return found if isinstance(found, dict) else {}


def cell(dispatch: Dispatch, line_id: str) -> dict[str, Any] | None:
    value = cells(dispatch).get(line_id)
    return value if isinstance(value, dict) else None


def _cells_for_write(dispatch: Dispatch) -> dict[str, dict[str, Any]]:
    if not isinstance(dispatch.gates, dict):
        dispatch.gates = {}
    root = dispatch.gates.get(GATE_KEY)
    if not isinstance(root, dict):
        root = {}
        dispatch.gates[GATE_KEY] = root
    return root


def engaged(dispatch: Dispatch, env: Mapping[str, str] | None = None) -> bool:
    """Whether any L4 hook has work: mode ``full``, or a cell is still live.

    False for every dispatch in off / shadow / docs that never had a claim:
    those drive exactly as before.
    """
    if lines_enabled(env):
        return any(line.app_claimable for line in dispatch.lines) or bool(cells(dispatch))
    return any(
        isinstance(item, dict) and item.get("state") in HOLDING
        for item in cells(dispatch).values()
    )


def _deps_passed(dispatch: Dispatch, line: Line) -> bool:
    for dep in line.depends_on:
        other = dispatch.line(dep)
        if other is None or other.status is not LineStatus.PASSED:
            return False
    return True


def _unstarted(line: Line) -> bool:
    return line.status in _UNSTARTED and not line.current_run


def held(
    dispatch: Dispatch, line: Line, *, now: float, env: Mapping[str, str] | None = None
) -> bool:
    """True when the machine must not launch a Worker for ``line`` now."""
    item = cell(dispatch, line.id)
    state = item.get("state") if item else None
    if state in LIVE:
        return True
    if not lines_enabled(env) or not line.app_claimable or not _unstarted(line):
        return False
    # A line that cannot run yet is never held: the drive must still reach
    # its blocked / exhausted parks instead of polling for an app.
    if not _deps_passed(dispatch, line):
        return False
    if state is None:
        return True  # the window opens on the drive's next step
    if state == OPEN:
        since = item.get("open_since") if item else None
        if isinstance(since, (int, float)) and not isinstance(since, bool):
            return now < float(since) + window_seconds(env)
        return True
    return False


def held_ids(
    dispatch: Dispatch, *, now: float, env: Mapping[str, str] | None = None
) -> frozenset[str]:
    if not engaged(dispatch, env):
        return frozenset()
    return frozenset(line.id for line in dispatch.lines if held(dispatch, line, now=now, env=env))


def claimable_refusal(
    dispatch: Dispatch, line: Line | None, *, now: float, env: Mapping[str, str] | None = None
) -> str | None:
    """Why ``line`` cannot be claimed now (§5.1 / §5.2 step 2), or None."""
    if not lines_enabled(env):
        return R_MODE
    if line is None:
        return R_NO_LINE
    if not line.app_claimable:
        return R_NOT_CLAIMABLE
    if not _unstarted(line):
        return R_NOT_READY
    if not _deps_passed(dispatch, line):
        return R_DEPS
    item = cell(dispatch, line.id)
    state = item.get("state") if item else None
    if state in LIVE:
        return R_TAKEN
    if state not in (None, OPEN):
        return R_WINDOW
    if state == OPEN and not held(dispatch, line, now=now, env=env):
        return R_WINDOW
    return None


def claim(
    dispatch: Dispatch,
    line: Line,
    *,
    app_id: str,
    agent: str = "",
    now: float,
    lease_seconds: float = LEASE_SECONDS,
    max_lease_seconds: float = MAX_LEASE_SECONDS,
) -> dict[str, Any]:
    """Write an ``active`` cell for ``app_id``. The caller checked the refusals."""
    previous = cell(dispatch, line.id) or {}
    revision = int(previous.get("revision") or 0) + 1
    item: dict[str, Any] = {
        "state": ACTIVE,
        "app_id": app_author(app_id)[len(APP_PREFIX) :],
        "agent": str(agent or "")[:64],
        "author": app_author(app_id),
        "claimed_at": float(now),
        "heartbeat_at": float(now),
        "expires_at": float(now) + float(lease_seconds),
        "max_lease_until": float(now) + float(max_lease_seconds),
        "base_commit": line.base_commit or "",
        "branch": line.branch or "",
        "attempt": int(line.attempts),
        "revision": revision,
    }
    if previous.get("open_since") is not None:
        item["open_since"] = previous.get("open_since")
    _cells_for_write(dispatch)[line.id] = item
    return item


def holder_refusal(item: Mapping[str, Any] | None, app_id: str) -> str | None:
    if not isinstance(item, Mapping) or item.get("state") != ACTIVE:
        return R_NOT_HOLDER
    if item.get("app_id") != app_author(app_id)[len(APP_PREFIX) :]:
        return R_NOT_HOLDER
    return None


def heartbeat(item: dict[str, Any], *, now: float, lease_seconds: float = LEASE_SECONDS) -> None:
    """Extend the lease, never past ``max_lease_until``."""
    cap = float(item.get("max_lease_until") or now)
    item["heartbeat_at"] = float(now)
    item["expires_at"] = min(float(now) + float(lease_seconds), cap)


def report(
    item: dict[str, Any],
    *,
    status: str,
    head: str,
    summary: str = "",
    files: tuple[str, ...] = (),
    now: float,
) -> None:
    """Record the app's report; the machine adopts it on its next step."""
    item["state"] = REPORTED
    item["report"] = {
        "status": str(status),
        "head": str(head or ""),
        "summary": str(summary or "")[:4000],
        "files": [str(path) for path in files][:200],
        "at": float(now),
    }


def release(item: dict[str, Any], *, now: float, reason: str = "released") -> None:
    item["state"] = RELEASED
    item["ended_at"] = float(now)
    item["end_reason"] = str(reason)[:64]


def lease_expired(item: Mapping[str, Any], now: float) -> bool:
    expires = item.get("expires_at")
    cap = item.get("max_lease_until")
    for value in (expires, cap):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and now >= float(value):
            return True
    return False


@dataclass(frozen=True, slots=True)
class Step:
    """One thing the machine does for a cell on this drive step."""

    kind: str  # open | prepare | adopt | expire | release | lapse
    line_id: str


def steps(
    dispatch: Dispatch, *, now: float, env: Mapping[str, str] | None = None
) -> list[Step]:
    """What the drive must do with the claim cells now, in line order."""
    if not engaged(dispatch, env):
        return []
    enabled = lines_enabled(env)
    out: list[Step] = []
    for line in dispatch.lines:
        item = cell(dispatch, line.id)
        state = item.get("state") if item else None
        if state is None:
            if enabled and line.app_claimable and _unstarted(line) and _deps_passed(dispatch, line):
                out.append(Step("open", line.id))
            continue
        assert item is not None
        if state == OPEN:
            if not enabled or not line.app_claimable or not _unstarted(line):
                out.append(Step("lapse", line.id))
            elif not held(dispatch, line, now=now, env=env):
                out.append(Step("lapse", line.id))
        elif state == ACTIVE:
            if not enabled or lease_expired(item, now):
                out.append(Step("expire", line.id))
            elif not item.get("worktree") and not item.get("worktree_error"):
                out.append(Step("prepare", line.id))
        elif state == REPORTED:
            done = isinstance(item.get("report"), Mapping) and item["report"].get("status") == "done"
            if not enabled:
                out.append(Step("expire", line.id))
            elif done:
                out.append(Step("adopt", line.id))
            else:
                out.append(Step("release", line.id))
    return out


def mark(dispatch: Dispatch, line_id: str, state: str, *, now: float, **fields: Any) -> dict[str, Any]:
    """Set a cell's state (the machine's side of a step)."""
    root = _cells_for_write(dispatch)
    item = root.get(line_id)
    if not isinstance(item, dict):
        item = {}
        root[line_id] = item
    item["state"] = state
    if state == OPEN:
        item.setdefault("open_since", float(now))
    elif state in (ADOPTED, LAPSED, EXPIRED, RELEASED):
        item["ended_at"] = float(now)
    item.update(fields)
    return item


def liveness_overlay(dispatch: Dispatch, line: Line) -> dict[str, Any] | None:
    """The ``app_lease`` owner view of a live cell, for N76's owner decision."""
    item = cell(dispatch, line.id)
    if not item or item.get("state") not in LIVE:
        return None
    return {
        "app_id": item.get("app_id"),
        "state": item.get("state"),
        "expires_at": item.get("expires_at"),
        "max_lease_until": item.get("max_lease_until"),
    }


def line_pack_facts(dispatch: Dispatch, line: Line, item: Mapping[str, Any] | None):
    """The §6.2 ``system`` facts of a line an app leaves (shared M2c generator)."""
    from . import handoff_pack

    report_cell = item.get("report") if isinstance(item, Mapping) else None
    report_cell = report_cell if isinstance(report_cell, Mapping) else {}
    snap = {}
    lines = dispatch.gates.get("lines") if isinstance(dispatch.gates, dict) else None
    if isinstance(lines, dict) and isinstance(lines.get(line.id), dict):
        snap = lines[line.id]
    g2 = snap.get("gates", {}).get("G2") if isinstance(snap.get("gates"), dict) else None
    app_id = str(item.get("app_id") or "") if isinstance(item, Mapping) else ""
    return handoff_pack.LineFacts(
        line_id=line.id,
        task=line.task,
        predecessor=app_author(app_id) if app_id else (line.assigned_model or ""),
        predecessor_kind="app" if app_id else "worker",
        session_id="",
        branch=line.branch or "",
        base_commit=line.base_commit or "",
        head=str(report_cell.get("head") or ""),
        files=tuple(str(path) for path in report_cell.get("files") or ()),
        last_report=str(report_cell.get("summary") or ""),
        g2_result=str(g2.get("result") or "") if isinstance(g2, Mapping) else "",
        open_review=tuple(str(item) for item in line.blockers),
        park_reason=str(line.failure_reason or ""),
        reason=str(item.get("state") or "") if isinstance(item, Mapping) else "",
        extra={"conversation_ref": {"app": app_id, "session_id": None}} if app_id else {},
    )


def line_pack(dispatch: Dispatch, line: Line, item: Mapping[str, Any] | None) -> dict[str, Any]:
    """The line-side handoff pack (``memfw.handoff/v1`` shape, Conductor-built)."""
    from . import handoff_pack

    return handoff_pack.build_line_pack(line_pack_facts(dispatch, line, item))


def claim_handoff(line: Line, item: Mapping[str, Any]):
    """The Stage-Gate handoff of the swap to the app (all six fields filled).

    The app's own report is not trusted for these: the branch and base are
    the line's, the commit is the reported head, and G2 (run by Conductor)
    is still ahead.
    """
    from src.server.services.autonomy.stage_gate import Handoff

    report_cell = item.get("report") if isinstance(item.get("report"), Mapping) else {}
    assert isinstance(report_cell, Mapping)
    files = tuple(str(path) for path in report_cell.get("files") or ()) or ("none reported",)
    return Handoff(
        branch=line.branch or "unknown",
        commit=str(report_cell.get("head") or line.base_commit or "unknown"),
        files=files,
        test_command="none (G2 runs next, in Conductor)",
        results=f"app report: {report_cell.get('status') or 'unknown'}",
        known_limitations=(
            "interactive app: no sandbox; checked after the fact "
            "(branch, write scope, clean worktree)"
        ),
    )


def exit_note(item: Mapping[str, Any], pack: Mapping[str, Any], reason: str) -> str:
    """The brief item for the next author: which app held the line, what it left."""
    system = pack.get("system") if isinstance(pack.get("system"), Mapping) else {}
    conductor = system.get("conductor") if isinstance(system, Mapping) else {}
    conductor = conductor if isinstance(conductor, Mapping) else {}
    head = str(conductor.get("head") or "") or "no reported head"
    files = system.get("files_touched") if isinstance(system, Mapping) else None
    listed = ", ".join(str(path) for path in (files or [])[:10]) or "none reported"
    text = (
        f"Previous holder {app_author(str(item.get('app_id') or ''))} "
        f"({reason}): it left {head}; files {listed}. Its report is not verified; "
        "the branch and the task text are the record."
    )
    return text[:1000]


def scope_violations(paths: Sequence[str], write_scope: Sequence[str]) -> tuple[str, ...]:
    """Changed paths outside ``write_scope``; an empty scope permits nothing.

    A planner directory entry (``src/a/``) owns everything under it; any
    other entry is a glob (``gates.path_matches``) or an exact path.
    """
    from .gates import path_matches

    patterns = [pattern.strip() for pattern in write_scope if pattern and pattern.strip()]

    def inside(pattern: str, path: str) -> bool:
        clean = path.replace("\\", "/").lstrip("./")
        if pattern.endswith("/") and not any(ch in pattern for ch in "*?[{"):
            return clean.startswith(pattern.lstrip("./"))
        return clean == pattern.lstrip("./") or path_matches(pattern, clean)

    return tuple(path for path in paths if not any(inside(pattern, path) for pattern in patterns))


def adopt_refusal(line: Line, item: Mapping[str, Any], facts: Mapping[str, Any]) -> str:
    """Why a ``done`` report may not enter G2, from the branch as it is now.

    G2 and review run on the branch tip, so the tip must be the head the
    app reported (nothing slipped in after the report), the tree clean, and
    every path changed since the base inside the write scope. The facts are
    Conductor's own read (``launcher.app_branch_facts``); unknown facts
    refuse. ``""`` means adopt.
    """
    report_cell = item.get("report") if isinstance(item.get("report"), Mapping) else {}
    assert isinstance(report_cell, Mapping)
    head = str(report_cell.get("head") or "")
    tip = facts.get("tip")
    paths = facts.get("paths")
    dirty = facts.get("dirty")
    if not isinstance(tip, str) or not tip or not isinstance(paths, (list, tuple)) or not isinstance(dirty, bool):
        return "branch_facts_unknown"
    if tip != head:
        return "tip_moved_after_report"
    if dirty:
        return "worktree_dirty"
    if scope_violations([str(path) for path in paths], line.write_scope):
        return "out_of_scope"
    return ""
