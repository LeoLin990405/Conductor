"""Regenerate the Stage-Gate parts of ``tests/fixtures/dispatch_gates_v1.json``.

The fixture's ``gates.lines["line-0"].machine`` must be exactly what
``StageGateState.to_dict()`` produces (with ``handoff`` set to ``null``, per the
``dispatch_gates_v1`` contract) and ``events`` must be the ``GateEvent.to_dict()``
records of the same transitions. This script drives the real, pure module
``src/server/services/autonomy/stage_gate.py`` through one legal event sequence
for the risk-bearing line and writes those two fields back. Everything else in
the fixture (the dispatch-level outer fields) is left alone.

Usage (from the repository root)::

    .venv/bin/python tests/fixtures/gen_dispatch_gates_v1.py          # rewrite
    .venv/bin/python tests/fixtures/gen_dispatch_gates_v1.py --check  # exit 1 on drift

The regression test ``tests/unit/tools/auto_dispatch/test_dispatch_gates_fixture.py``
imports :func:`build_line_snapshot` and compares it with the fixture.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.server.services.autonomy import stage_gate as sg  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "dispatch_gates_v1.json"

#: Stage-Gate revision the fixture was generated from (feat/stage-gate-machine,
#: merged into integrate/r2 at 8de76397d).
STAGE_GATE_SOURCE_COMMIT = "aee455e00"

LINE_ID = "line-0"
AUTHOR = "kimi"
PRIMARY = "codex"
SECONDARY = "glm"
SPEC_REF = "specs/9f3a1c7e5b2d4086"
HANDOFF_REF = "sha256:5b2d1bc84a74b93e708ef95a7b48224c5b514e7db2c7d1333a7d4d9c0a51ddb4"

# The handoff the author publishes with the fix. Only its reference reaches the
# snapshot; the contract nulls ``machine.handoff``.
_FIX_HANDOFF = sg.Handoff(
    branch="feature/auto-dispatch-d3adbeef/line-0",
    commit="0f3c9b2a7d4e5f60718293a4b5c6d7e8f9012345",
    files=("src/server/app/health.py", "tests/unit/server/app/test_health.py"),
    test_command="pytest -q tests/unit/server/app/test_health.py",
    results="exit 0",
    known_limitations="none",
    ref=HANDOFF_REF,
)


def _ts(clock: str) -> str:
    return f"2026-11-20T{clock}+00:00"


def _events() -> list[sg.Event]:
    """planning -> G0 -> D1 -> assign -> implement -> G2 -> primary BLOCK ->
    one fix round -> primary APPROVE -> secondary APPROVE -> G5 -> G6 pending."""
    kind = sg.EventKind
    passed = sg.GateResult(sg.PASS)
    return [
        sg.Event(kind=kind.SPEC_SUBMITTED.value, timestamp=_ts("10:00:00")),
        sg.Event(kind=kind.GATE_RESULT.value, gate="G0", result=passed, timestamp=_ts("10:00:05")),
        sg.Event(kind=kind.ASSIGN.value, provider=AUTHOR, timestamp=_ts("10:00:10")),
        sg.Event(kind=kind.START_IMPLEMENTATION.value, timestamp=_ts("10:00:15")),
        sg.Event(kind=kind.SUBMIT_WORK.value, timestamp=_ts("10:12:00")),
        sg.Event(kind=kind.GATE_RESULT.value, gate="G2", result=passed, timestamp=_ts("10:14:30")),
        sg.Event(
            kind=kind.PRIMARY_REVIEW.value,
            provider=PRIMARY,
            result=sg.GateResult(sg.FAIL),
            verdict=sg.Verdict.BLOCK.value,
            timestamp=_ts("10:20:00"),
        ),
        sg.Event(kind=kind.SUBMIT_WORK.value, handoff=_FIX_HANDOFF, timestamp=_ts("10:31:10")),
        sg.Event(
            kind=kind.PRIMARY_REVIEW.value,
            provider=PRIMARY,
            result=passed,
            verdict=sg.Verdict.APPROVE.value,
            timestamp=_ts("10:35:00"),
        ),
        sg.Event(
            kind=kind.SECONDARY_REVIEW.value,
            provider=SECONDARY,
            result=passed,
            verdict=sg.Verdict.APPROVE.value,
            timestamp=_ts("10:38:00"),
        ),
        sg.Event(kind=kind.GATE_RESULT.value, gate="G5", result=passed, timestamp=_ts("10:42:00")),
    ]


def build_line_snapshot() -> dict[str, Any]:
    """Drive Stage-Gate and return ``{"machine": ..., "events": [...]}``.

    Raises ``AssertionError`` if the module refuses any step, so a rule change
    in ``stage_gate.py`` surfaces here instead of silently drifting.
    """
    state = sg.initial_state(LINE_ID, risk_bearing=True)
    events: list[dict[str, Any]] = []
    for event in _events():
        transition = sg.advance(state, event)
        assert transition.accepted, (event.kind, transition.reason, transition.detail)
        assert transition.event is not None
        events.append(transition.event.to_dict())
        state = transition.state
    assert state.stage is sg.Stage.AWAITING_HUMAN, state.stage
    machine = state.to_dict()
    machine["handoff"] = None  # contract: only handoff_ref is kept
    return {"machine": machine, "events": events}


def render(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def regenerate(data: dict[str, Any]) -> dict[str, Any]:
    """Return ``data`` with the Stage-Gate parts and ``spec_ref`` refreshed."""
    snapshot = build_line_snapshot()
    line = data["gates"]["lines"][LINE_ID]
    line["events"] = snapshot["events"]
    line["machine"] = snapshot["machine"]
    if "spec_ref" not in data:
        ordered: dict[str, Any] = {}
        for key, value in data.items():
            ordered[key] = value
            if key == "message":
                ordered["spec_ref"] = SPEC_REF
        data = ordered
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the fixture is stale")
    args = parser.parse_args(argv)
    current = FIXTURE.read_text(encoding="utf-8")
    fresh = render(regenerate(json.loads(current)))
    if args.check:
        if fresh != current:
            print(f"{FIXTURE} is stale; rerun {Path(__file__).name}", file=sys.stderr)
            return 1
        return 0
    FIXTURE.write_text(fresh, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
