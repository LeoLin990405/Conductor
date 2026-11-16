"""N76 acceptance 1: LIVENESS_MODE=off reproduces main's action sequence.

Each scenario drives a dispatch through the existing W3a/N38 harnesses and
records an *action trace*: every row the machine saves (state, message, lines,
gates JSON), every launch, review, merge and gate command, in order. The
golden file ``n76_main_traces.json`` was recorded by running this same module
against ``core/main`` (a19dca578), before any N76 L2 code existed::

    N76_TRACE_RECORD=<file> pytest tests/unit/tools/auto_dispatch/test_n76_off_mode_equivalence.py

The test then requires that the branch produces the identical trace with the
sweep ``off``, and with ``shadow`` once the ``liveness`` records (the only
thing shadow may add) are removed. Only helpers that exist on main are used,
so the module runs unchanged on both trees.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from src.tools.auto_dispatch import Dispatch, DispatchState
from src.tools.auto_dispatch.gates import CommandResult, GateSettings
from src.tools.auto_dispatch.selection import QuotaAwareModelSelector
from src.tools.auto_dispatch.types import Line
from tests.unit.tools.auto_dispatch import test_machine as ungated
from tests.unit.tools.auto_dispatch.test_machine_gates import (
    FakeWorktrees,
    NoopMerger,
    ScriptedCommands,
    _BlockOne,
    _bundle,
    _line,
    _repo_with_lines,
    _two_heads,
)
from tests.unit.tools.auto_dispatch.test_n34_committed_retry import (
    BASE,
    H1,
    ScriptedWorkers,
    _dispatch,
    _handoff,
    _seed_runs,
)
from tests.unit.tools.auto_dispatch.test_n38_unreviewed_head import _seed_ledger

GOLDEN = Path(__file__).with_name("n76_main_traces.json")
RECORD_ENV = "N76_TRACE_RECORD"
MODE_ENV = "CONDUCTOR_DISPATCH_LIVENESS_MODE"

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_RUN_SUFFIX = re.compile(r":[0-9a-f]{12}(?!\w)")
_HEX_ID = re.compile(r"\b(?:[0-9a-f]{32,40}|[0-9a-f]{12})\b")
_RANDOM_KEYS = frozenset({"approval_token"})


def _scrub(value: Any, root: str) -> Any:
    """Make a trace comparable across runs: tmp paths, ids, shas, wall clocks."""
    if isinstance(value, str):
        text = value.replace(root, "<tmp>")
        text = _RUN_SUFFIX.sub(":<id>", text)
        text = _HEX_ID.sub("<hex>", text)
        return _ISO.sub("<ts>", text)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key = str(key)
            if key in _RANDOM_KEYS and item:
                out[key] = "<random>"
            elif key.endswith("_at") and isinstance(item, float):
                out[key] = "<clock>"
            elif key.endswith(("_secs", "_seconds", "_ms")) and isinstance(item, (int, float)):
                # Wall-clock durations (outcome duration_secs) vary with load.
                out[key] = "<duration>"
            else:
                out[key] = _scrub(item, root)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(item, root) for item in value]
    return value


def _without_liveness(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_liveness(item) for key, item in value.items() if key != "liveness"
        }
    if isinstance(value, list):
        return [_without_liveness(item) for item in value]
    return value


def _has_liveness(value: Any) -> bool:
    if isinstance(value, dict):
        return "liveness" in value or any(_has_liveness(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_liveness(item) for item in value)
    return False


# Additive top-level records newer than the golden: liveness (N76), the N62
# fresh-base record and the N45 schema revision. Neither drives an action.
_ADDED_GATE_KEYS = frozenset({"liveness", "base_check", "schema_revision", "cost"})
# Per-line shadow records: liveness (N76), N60 completion evidence and the
# N51/N59 review triage record.
_ADDED_CELL_KEYS = frozenset({"liveness", "completion_evidence", "review_triage"})


def _projection(dispatch: Dispatch) -> dict[str, Any]:
    """What one save changed that the machine acts on (the final row is kept whole)."""
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    cells = gates.get("lines") if isinstance(gates.get("lines"), dict) else {}
    return {
        "state": dispatch.state.value,
        "message": dispatch.message,
        "approval": bool(dispatch.approval_token),
        "gate_keys": sorted(key for key in gates if key not in _ADDED_GATE_KEYS),
        "lines": [
            {
                "id": line.id,
                "status": line.status.value,
                "reason": line.failure_reason,
                "model": line.assigned_model,
                "attempts": line.attempts,
                "run": bool(line.current_run),
                "blockers": list(line.blockers),
                "cell": sorted(
                    key
                    for key in (cells.get(line.id) or {})
                    if key not in _ADDED_CELL_KEYS
                ),
                "stage": (cells.get(line.id) or {}).get("stage"),
                "gates": {
                    name: (gate or {}).get("result")
                    for name, gate in ((cells.get(line.id) or {}).get("gates") or {}).items()
                    if isinstance(gate, dict)
                },
            }
            for line in dispatch.lines
        ],
    }


class Trace:
    def __init__(self, root: Path) -> None:
        self.root = str(root)
        self.events: list[Any] = []

    def add(self, kind: str, payload: Any) -> None:
        if kind == "final" and isinstance(payload, dict) and isinstance(payload.get("gates"), dict):
            gates = {
                key: item
                for key, item in payload["gates"].items()
                if key == "liveness" or key not in _ADDED_GATE_KEYS
            }
            if isinstance(gates.get("lines"), dict):
                gates["lines"] = {
                    line_id: {
                        key: item
                        for key, item in cell.items()
                        if key == "liveness" or key not in _ADDED_CELL_KEYS
                    }
                    if isinstance(cell, dict)
                    else cell
                    for line_id, cell in gates["lines"].items()
                }
            heads = gates.get("reviewed_heads")
            if isinstance(heads, dict):
                # N60 shadow: verified evidence per reviewed head.
                gates["reviewed_heads"] = {
                    line_id: {
                        branch: {k: v for k, v in ledger.items() if k != "evidence"}
                        if isinstance(ledger, dict)
                        else ledger
                        for branch, ledger in branches.items()
                    }
                    if isinstance(branches, dict)
                    else branches
                    for line_id, branches in heads.items()
                }
            payload = {**payload, "gates": gates}
        self.events.append([kind, _scrub(json.loads(json.dumps(payload, default=str)), self.root)])


def _digest(events: list[Any]) -> list[Any]:
    """Each save as a short hash of its projection; the rest stays readable.

    The golden would be megabytes with every saved row in full. A mismatch is
    still located exactly: the failing index is the first differing save.
    """
    out: list[Any] = []
    for kind, payload in events:
        if kind == "save":
            blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            payload = hashlib.sha256(blob.encode()).hexdigest()[:16]
        out.append([kind, payload])
    return out


def _collapse(events: list[Any]) -> list[Any]:
    out: list[Any] = []
    for event in events:
        if out and event[0] == "save" and out[-1] == event:
            continue
        out.append(event)
    return out


def _first_divergence(name: str, got: list[Any], want: list[Any]) -> str:
    for index, (left, right) in enumerate(zip(got, want)):
        if left != right:
            return f"{name}: event {index} differs: got {left!r:.600} want {right!r:.600}"
    return f"{name}: {len(got)} events, main had {len(want)}"


def _instrument(collaborators, trace: Trace) -> None:
    store = collaborators.store
    original_save = store.save

    async def save(dispatch: Dispatch) -> Dispatch:
        trace.add("save", _projection(dispatch))
        return await original_save(dispatch)

    store.save = save
    launcher = collaborators.launcher
    if hasattr(launcher, "launch"):
        original_launch = launcher.launch

        # ``wraps`` keeps the signature: the machine passes optional keywords
        # only to callables that accept them.
        @functools.wraps(original_launch)
        async def launch(line, model, skills, *args, **kwargs):
            trace.add("launch", [line.id, model, list(skills), sorted(kwargs)])
            return await original_launch(line, model, skills, *args, **kwargs)

        launcher.launch = launch
    reviewer = collaborators.reviewer
    if hasattr(reviewer, "review_line"):
        original_review = reviewer.review_line

        @functools.wraps(original_review)
        async def review_line(line_id, model, *args, **kwargs):
            trace.add("review", [line_id, model, sorted(kwargs)])
            return await original_review(line_id, model, *args, **kwargs)

        reviewer.review_line = review_line
    merger = collaborators.merger
    original_merge = merger.merge_line

    @functools.wraps(original_merge)
    async def merge_line(line_id):
        trace.add("merge", line_id)
        return await original_merge(line_id)

    merger.merge_line = merge_line


def _commands_of(collaborators) -> list:
    gates = collaborators.gates
    commands = getattr(gates, "_commands", None) if gates is not None else None
    return list(getattr(commands, "calls", []) or [])


async def _gated(root: Path, lines: list[Line], *, redrive: int = 0, approve=None, **overrides):
    collaborators, machine, _commands, _trees = _bundle(root, lines, **overrides)
    trace = Trace(root)
    _instrument(collaborators, trace)
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    result = await machine.run(dispatch)
    trace.add("result", [result.state.value, result.message])
    for _ in range(redrive):
        result = await machine.run(result)
        trace.add("redrive", [result.state.value, result.message])
    if approve is not None and result.state is DispatchState.APPROVAL:
        await collaborators.gate.resolve_dispatch(result.id, approve)
        result = await machine.run(result)
        trace.add("approved", [result.state.value, result.message])
    trace.add("commands", _commands_of(collaborators))
    trace.add("final", result.to_dict())
    return trace.events


def _repo_settings(root: Path, *line_ids: str) -> GateSettings:
    repo = _repo_with_lines(root / "repo", *line_ids)
    return GateSettings(runs_dir=root / "runs", repo_root=repo, g5_command="pytest -q")


async def s_happy_dag(root: Path):
    lines = [
        _line("line-c", depends_on=("line-a",)),
        _line("line-b", depends_on=("line-a",)),
        _line("line-a"),
    ]
    return await _gated(root, lines, approve=True)


async def s_review_block_parks_replan_exhausted(root: Path):
    lines = [_line("line-a"), _line("line-c"), _line("line-b", depends_on=("line-a",))]
    return await _gated(
        root,
        lines,
        redrive=2,
        reviewer=_BlockOne("line-c"),
        settings=_repo_settings(root, "line-a", "line-c", "line-b"),
    )


async def s_human_reassign_after_escalation(root: Path):
    """Two blocked fix rounds write the R5 escalation; a person reassigns."""
    from src.tools.auto_dispatch.machine import apply_human_reassign

    lines = [_line("line-a"), _line("line-c")]
    collaborators, machine, _commands, _trees = _bundle(
        root,
        lines,
        reviewer=_BlockOne("line-c"),
        settings=_repo_settings(root, "line-a", "line-c"),
    )
    trace = Trace(root)
    _instrument(collaborators, trace)
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    result = await machine.run(dispatch)
    trace.add("result", [result.state.value, result.message])
    line = result.line("line-c")
    refusal = await apply_human_reassign(result, line, "kimi", None, collaborators.gates)
    trace.add("reassign", [refusal, result.gates.get("reassign_briefs")])
    await collaborators.store.save(result)
    result = await machine.run(result)
    trace.add("redrive", [result.state.value, result.message])
    trace.add("final", result.to_dict())
    return trace.events


async def s_focused_tests_exhausted(root: Path):
    commands = ScriptedCommands([CommandResult(exit_code=1, duration_ms=12)] * 3)
    return await _gated(
        root,
        [_line("line-0")],
        redrive=2,
        commands=commands,
        settings=_repo_settings(root, "line-0"),
    )


async def s_risk_line_without_commands(root: Path):
    risky = _line("line-0", acceptance=("returns 200",), risk_bearing=True)
    return await _gated(root, [risky], redrive=1)


async def s_g5_full_suite_failure(root: Path):
    commands = ScriptedCommands([CommandResult(0, 5), CommandResult(exit_code=2, duration_ms=9)])
    return await _gated(root, [_line("line-0")], redrive=1, commands=commands)


async def s_g5_not_configured(root: Path):
    settings = GateSettings(runs_dir=root / "runs", repo_root=root / "repo", g5_command=None)
    return await _gated(root, [_line("line-0")], redrive=1, settings=settings)


async def s_two_alembic_heads(root: Path):
    return await _gated(root, [_line("line-0")], trees=FakeWorktrees(seed=_two_heads))


async def s_no_secondary_reviewer(root: Path):
    selector = QuotaAwareModelSelector(review_refused=lambda model: model in {"codex", "stepfun"})
    return await _gated(root, [_line("line-0")], redrive=1, selector=selector)


async def s_merge_conflict(root: Path):
    return await _gated(root, [_line("line-0")], merger=NoopMerger(conflict_on="line-0"))


async def s_dependency_cycle(root: Path):
    lines = [_line("line-a", depends_on=("line-b",)), _line("line-b", depends_on=("line-a",))]
    return await _gated(root, lines)


async def s_unreviewed_head_probe(root: Path):
    from src.tools.auto_dispatch.selection import ModelRole

    class Selector:
        def __init__(self) -> None:
            self.review_refusal: str | None = None

        async def select(self, line, used, *, role=None):
            if role is not ModelRole.REVIEW:
                return "kimi"
            self.review_refusal = "no_secondary_reviewer" if "deepseek" in used else "pool_empty"
            return None

    async def sleep(_seconds: float) -> None:
        return None

    collaborators, machine, _commands, _trees = _bundle(
        root, [], launcher=ScriptedWorkers([("empty", H1, True)]), selector=Selector()
    )
    machine._sleep = sleep
    collaborators.gates.handoff_facts = _handoff
    trace = Trace(root)
    _instrument(collaborators, trace)
    dispatch = _dispatch("d-n38-probe")
    _seed_runs(
        collaborators.gates,
        dispatch,
        [{"author": "deepseek", "start": BASE, "head": H1, "submitted": False}],
    )
    _seed_ledger(dispatch, [])
    result = await machine.run(dispatch)
    trace.add("result", [result.state.value, result.message])
    trace.add("final", result.to_dict())
    return trace.events


async def _ungated(root: Path, **overrides):
    collaborators = ungated._collaborators(root, **overrides)
    machine, recorder = ungated._machine(collaborators)
    trace = Trace(root)
    _instrument(collaborators, trace)
    dispatch = await collaborators.store.create(ungated._dispatch())
    result = await machine.run(dispatch)
    trace.add("result", [result.state.value, result.message])
    if result.state is DispatchState.APPROVAL:
        await collaborators.gate.resolve_dispatch(result.id, True)
        result = await machine.run(result)
        trace.add("approved", [result.state.value, result.message])
    trace.add("final", result.to_dict())
    return trace.events


async def s_ungated_happy(root: Path):
    return await _ungated(root)


async def s_ungated_text_only_pool(root: Path):
    from src.tools.auto_dispatch.pool import eligible_models

    roster = eligible_models("work")

    class Headroom:
        async def remaining(self, model: str) -> float:
            return 0.0 if model in roster and model != "doubao" else 1.0

    line = Line(id="line-0", name="edit", task="edit the file", write_scope=("src/a.py",))

    class Planner:
        async def plan(self, goal: str) -> list[Line]:
            return [line]

    return await _ungated(root, planner=Planner(), selector=QuotaAwareModelSelector(Headroom()))


@pytest.fixture(autouse=True)
def _no_preexisting_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    # N27 shadow re-runs a failing G2 command at the base; the scripted fake
    # runner here answers by call order, so the extra call would shift it.
    monkeypatch.setenv("CONDUCTOR_DISPATCH_G2_PREEXISTING_MODE", "off")


SCENARIOS = {
    name.removeprefix("s_"): fn for name, fn in sorted(globals().items()) if name.startswith("s_")
}


def _run_all(tmp_path: Path) -> dict[str, Any]:
    raw = {name: asyncio.run(fn(tmp_path / name)) for name, fn in SCENARIOS.items()}
    return json.loads(json.dumps(raw, sort_keys=True))


def test_record_or_compare_main_traces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = os.environ.get(RECORD_ENV)
    if target:
        # Recording runs on core/main, which has no liveness mode at all.
        traces = {name: _digest(events) for name, events in _run_all(tmp_path).items()}
        Path(target).write_text(
            "{\n"
            + ",\n".join(
                f"{json.dumps(name)}: {json.dumps(events, sort_keys=True, separators=(',', ':'))}"
                for name, events in sorted(traces.items())
            )
            + "\n}\n",
            encoding="utf-8",
        )
        return
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert sorted(golden) == sorted(SCENARIOS)

    monkeypatch.setenv(MODE_ENV, "off")
    off = _run_all(tmp_path / "off")
    assert not _has_liveness(off)
    for name in SCENARIOS:
        got = _digest(off[name])
        assert got == golden[name], _first_divergence(name, got, golden[name])


def test_shadow_only_adds_liveness_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get(RECORD_ENV):
        pytest.skip("recording run")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    monkeypatch.setenv(MODE_ENV, "shadow")
    monkeypatch.setenv("CONDUCTOR_DISPATCH_FOLLOWUPS_DIR", str(tmp_path / "followups"))
    shadow = _run_all(tmp_path / "shadow")
    # Shadow may save extra rows that differ only by a liveness record. Once
    # that record is removed, consecutive identical saves are collapsed on
    # both sides; every launch, review, merge, command and distinct row
    # state must still match main in order.
    for name in SCENARIOS:
        got = _collapse(_digest(_without_liveness(shadow[name])))
        want = _collapse(golden[name])
        assert got == want, "shadow added an action: " + _first_divergence(name, got, want)
    # Shadow did record something for the parked scenarios.
    assert _has_liveness(shadow["review_block_parks_replan_exhausted"])


def test_the_golden_was_recorded_on_main() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert not _has_liveness(golden)
    # Main's ERROR stops are in the golden; off must keep them.
    finals = {name: [e for e in events if e[0] == "final"][-1][1]["state"] for name, events in golden.items()}
    assert finals["g5_full_suite_failure"] == "ERROR"
    assert finals["g5_not_configured"] == "ERROR"
    assert finals["no_secondary_reviewer"] == "ERROR"
    assert finals["ungated_text_only_pool"] == "ERROR"
    assert finals["happy_dag"] == "COMPLETE"
