"""A human reassign sticks: the line is pinned to the worker a person chose (N86).

On 2026-11-13 ``dispatch reassign <id> line-0 kimi`` answered ok and status
showed kimi ASSIGNED; ten minutes later the line was RUNNING on deepseek
(attempt 2). The override path was the drive's automatic move off a limited
provider: kimi's run failed ``quota_exhausted``/``rate_limited`` and
``DispatchMachine._move_off_limited_provider`` (machine.py) asked the selector
for another developer and reassigned the line there through the same
Stage-Gate path a human reassign uses, without checking that a person had just
picked kimi. ``_liveness_reassign`` (R-EXIT/R-REPLAN) and the generic
``_reassign`` reselect the same way.

This module is the pin and its rules:

- ``record_pin`` writes ``gates.human_pins[line_id]`` when the service accepts
  a human reassign. The key is service-owned (``_SERVICE_GATES``), so a drive
  save does not drop it. A queued reassign (a drive owns the row) is recorded
  as ``queued`` and only counts once the drive applied it (the scheduler audit
  then has an applied ``human_reassign`` for that model).
- ``pinned_model`` is the model the line is pinned to, or None.
- The work selector (``selection.QuotaAwareModelSelector.select``) consults
  ``work_pick``: a pinned line gets its pinned model back, or nobody when the
  pinned model is excluded (a skipped provider, the model a move is leaving).
  No automatic path can then seat another worker, so the drive keeps its
  own retry policy on the pinned model or parks the line.
- ``reassign_headroom_refusal`` refuses the reassign itself when the chosen
  worker is skipped on this dispatch for quota/rate limits, or its headroom
  reads exhausted: the person hears it at reassign time instead of watching
  the line move elsewhere later.
- ``park_pinned_unusable`` is the park a launch-time hook writes when the
  pinned worker cannot run (``pinned_worker_unusable``, a HUMAN reason).

``CONDUCTOR_DISPATCH_HUMAN_PIN_MODE`` is ``off|shadow|enforce`` (default
``shadow``): ``off`` records nothing and checks nothing; ``shadow`` records
the pin and logs (and audits) what enforcement would have refused or kept,
without changing any pick; ``enforce`` applies the rules above.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .check_modes import ENFORCE, OFF, check_mode
from .types import Dispatch, Line, LineStatus

logger = logging.getLogger(__name__)

HUMAN_PIN_MODE_ENV = "CONDUCTOR_DISPATCH_HUMAN_PIN_MODE"
#: The service-owned gates key that holds the pins.
HUMAN_PINS = "human_pins"
#: The park reason when the pinned worker cannot run (a HUMAN reason).
PINNED_WORKER_UNUSABLE = "pinned_worker_unusable"
#: The refusal code when the chosen worker has no headroom at reassign time.
WORKER_NO_HEADROOM = "worker_no_headroom"
#: The refusal code when the chosen worker is skipped on this dispatch.
WORKER_SKIPPED = "worker_skipped"
_PROVIDER_SKIPS = "provider_skips"
#: Headroom at or below this reads as exhausted for a human pick. Lower than
#: the selector's ranking floor: a person may knowingly pick a low worker.
EXHAUSTED_HEADROOM = 0.0


def pin_mode(environ: Mapping[str, str] | None = None) -> str:
    return check_mode(HUMAN_PIN_MODE_ENV, environ)


def _canon(model: object) -> str:
    try:
        from src.server.services.autonomy.roles import canonical_provider

        return canonical_provider(str(model or "")).strip().lower()
    except Exception:  # noqa: BLE001 - a pin read never fails a pick
        return str(model or "").strip().lower()


def _pins(dispatch: Dispatch) -> dict[str, Any]:
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else None
    pins = gates.get(HUMAN_PINS) if isinstance(gates, dict) else None
    return pins if isinstance(pins, dict) else {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_pin(
    dispatch: Dispatch,
    line: Line,
    model: str,
    *,
    queued: bool = False,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Pin ``line`` to ``model`` after a human reassign. False in ``off`` mode."""
    if pin_mode(environ) == OFF:
        return False
    if not isinstance(dispatch.gates, dict):
        dispatch.gates = {}
    pins = dispatch.gates.get(HUMAN_PINS)
    if not isinstance(pins, dict):
        pins = {}
        dispatch.gates[HUMAN_PINS] = pins
    pins[line.id] = {
        "model": _canon(model),
        "at": _now(),
        "queued": bool(queued),
        "attempts_at_request": line.attempts,
    }
    return True


def clear_pin(dispatch: Dispatch, line_id: str) -> None:
    pins = _pins(dispatch)
    pins.pop(line_id, None)


def _applied_by_drive(dispatch: Dispatch, line_id: str, model: str) -> bool:
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    scheduler = gates.get("scheduler") if isinstance(gates, dict) else None
    audit = scheduler.get("audit") if isinstance(scheduler, dict) else None
    for entry in reversed(audit if isinstance(audit, list) else []):
        if not isinstance(entry, dict) or entry.get("kind") != "human_reassign":
            continue
        if entry.get("line_id") != line_id or _canon(entry.get("model")) != model:
            continue
        return entry.get("result") == "applied"
    return False


def pinned_model(dispatch: Dispatch | None, line_id: str) -> str | None:
    """The model a person pinned this line to, or None.

    A pin queued for a running drive counts only after that drive applied it;
    a queued request the drive dropped never pins the line.
    """
    if dispatch is None:
        return None
    entry = _pins(dispatch).get(line_id)
    if not isinstance(entry, dict):
        return None
    model = _canon(entry.get("model"))
    if not model:
        return None
    if entry.get("queued") and not _applied_by_drive(dispatch, line_id, model):
        return None
    return model


def _audit(dispatch: Dispatch, entry: dict[str, object]) -> None:
    if not isinstance(dispatch.gates, dict):
        return
    scheduler = dispatch.gates.setdefault("scheduler", {})
    if not isinstance(scheduler, dict):
        return
    audit = scheduler.setdefault("audit", [])
    if isinstance(audit, list):
        audit.append({**entry, "at": _now()})
        del audit[:-200]


class PinPick:
    """What the selector does for a pinned line: ``handled`` and the model."""

    __slots__ = ("handled", "model")

    def __init__(self, handled: bool, model: str | None) -> None:
        self.handled = handled
        self.model = model


def work_pick(
    dispatch: Dispatch | None,
    line: Line,
    excluded: set[str] | frozenset[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> PinPick:
    """The selector's answer for a pinned work line.

    Not pinned, or mode ``off``/``shadow``: not handled (the ordinary ranking
    runs; ``shadow`` logs what enforcement would have returned). Pinned in
    ``enforce``: the pinned model, or None when it is excluded -- the line
    must not move to anyone a person did not choose.
    """
    if dispatch is None:
        return PinPick(False, None)
    mode = pin_mode(environ)
    if mode == OFF:
        return PinPick(False, None)
    pinned = pinned_model(dispatch, line.id)
    if not pinned:
        return PinPick(False, None)
    answer = None if pinned in {_canon(name) for name in excluded} else pinned
    if mode != ENFORCE:
        logger.info(
            "human pin (shadow): line %s pinned to %s; enforce would pick %s",
            line.id,
            pinned,
            answer or "nobody",
        )
        return PinPick(False, None)
    if answer is None:
        logger.warning(
            "Line %s is pinned to %s by a human reassign and %s is excluded; "
            "no other worker is picked",
            line.id,
            pinned,
            pinned,
        )
    return PinPick(True, answer)


def blocks_automatic_move(
    dispatch: Dispatch, line: Line, *, environ: Mapping[str, str] | None = None
) -> bool:
    """True when an automatic author swap must not run on this line (enforce only)."""
    return pin_mode(environ) == ENFORCE and pinned_model(dispatch, line.id) is not None


def park_pinned_unusable(
    dispatch: Dispatch, line: Line, why: str, *, environ: Mapping[str, str] | None = None
) -> bool:
    """Park a pinned line whose worker cannot run, instead of switching worker.

    Returns False (and writes nothing) unless the line is pinned and the mode
    is ``enforce``; the caller then keeps its old behaviour.
    """
    pinned = pinned_model(dispatch, line.id)
    if not pinned or pin_mode(environ) != ENFORCE:
        return False
    line.status = LineStatus.BLOCKED
    line.failure_reason = PINNED_WORKER_UNUSABLE
    line.current_run = None
    line.blockers = [
        f"{pinned} was chosen by a human reassign and cannot run now ({why}); "
        "the line was not moved to another worker. Reassign it to another "
        "worker, or wait for headroom and recover it."
    ]
    _audit(
        dispatch,
        {
            "kind": "human_pin_parked",
            "line_id": line.id,
            "model": pinned,
            "reason": str(why)[:200],
        },
    )
    return True


def _skip_entry(dispatch: Dispatch, model: str, now: float) -> dict[str, Any] | None:
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    skips = gates.get(_PROVIDER_SKIPS) if isinstance(gates, dict) else None
    if not isinstance(skips, dict):
        return None
    for name, entry in skips.items():
        if _canon(name) != model or not isinstance(entry, dict):
            continue
        until = entry.get("until")
        if isinstance(until, (int, float)) and not isinstance(until, bool) and until > now:
            return entry
    return None


async def reassign_headroom_refusal(
    dispatch: Dispatch,
    model: str,
    headroom: Any,
    *,
    now: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Why a human reassign to ``model`` is refused now, or None.

    ``worker_skipped`` when this dispatch skips the provider for new launches
    (it hit a quota or rate limit and its reset is not reached);
    ``worker_no_headroom`` when the quota reader (a ``HeadroomPort`` or a
    factory for one) says nothing is left. A read that fails is not a
    refusal. In ``shadow`` the refusal is logged and
    audited, and None is returned.
    """
    mode = pin_mode(environ)
    if mode == OFF:
        return None
    chosen = _canon(model)
    moment = datetime.now(timezone.utc).timestamp() if now is None else now
    reason: str | None = None
    entry = _skip_entry(dispatch, chosen, moment)
    if entry is not None:
        until = entry.get("until")
        when = (
            datetime.fromtimestamp(float(until), timezone.utc).isoformat()
            if isinstance(until, (int, float))
            else "unknown"
        )
        reason = (
            f"{WORKER_SKIPPED}: {chosen} is skipped on this dispatch "
            f"({entry.get('reason') or 'limited'}) until {when}; pick another worker"
        )
    elif headroom is not None:
        try:
            # A factory is called only here, so ``off`` and a skipped
            # provider never build (or read) the quota reader.
            port = headroom if hasattr(headroom, "remaining") else headroom()
            remaining = float(await port.remaining(chosen))
        except Exception:  # noqa: BLE001 - an unreadable quota never refuses
            logger.warning("headroom read failed for %s at reassign", chosen, exc_info=True)
            remaining = 1.0
        if remaining <= EXHAUSTED_HEADROOM:
            reason = (
                f"{WORKER_NO_HEADROOM}: {chosen} has no quota left; "
                "pick another worker or wait for its reset"
            )
    if reason and mode != ENFORCE:
        logger.info("human pin (shadow): reassign would be refused: %s", reason)
        _audit(
            dispatch,
            {"kind": "human_pin_shadow_refusal", "model": chosen, "reason": reason},
        )
        return None
    return reason


__all__ = [
    "EXHAUSTED_HEADROOM",
    "HUMAN_PINS",
    "HUMAN_PIN_MODE_ENV",
    "PINNED_WORKER_UNUSABLE",
    "WORKER_NO_HEADROOM",
    "WORKER_SKIPPED",
    "PinPick",
    "blocks_automatic_move",
    "clear_pin",
    "park_pinned_unusable",
    "pin_mode",
    "pinned_model",
    "reassign_headroom_refusal",
    "record_pin",
    "work_pick",
]
