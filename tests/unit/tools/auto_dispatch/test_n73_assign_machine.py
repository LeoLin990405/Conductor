"""N73 L4: the machine asks the Super-agent, records both picks, adopts only when sampled."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.server.services.autonomy import role_types
from src.tools.auto_dispatch.machine import DispatchMachine
from src.tools.auto_dispatch.superagent_assign import launch_env
from src.tools.auto_dispatch.superagent_assign.record import assignment_entries
from src.tools.auto_dispatch.superagent_assign.runner import LineDecision
from src.tools.auto_dispatch.superagent_assign.types import LineChoice
from src.tools.auto_dispatch.types import Dispatch, Line


def _choice(model: str, effort: str = "high") -> LineChoice:
    return LineChoice(
        line_id="l1", model=model, effort=effort, runtime="default",
        reason="area rate", confidence="high",
    )


class _Assigner:
    def __init__(self, decisions, sample=1.0):
        self.decisions = decisions
        self._sample = sample
        self.calls = []

    def sample(self):
        return self._sample

    async def decide(self, dispatch, lines):
        self.calls.append([line.id for line in lines])
        return self.decisions


class _Selector:
    def __init__(self):
        self.released = []
        self.taken = []

    def release(self, model):
        self.released.append(model)

    def _take(self, model):
        self.taken.append(model)


def _machine(assigner, snapshot):
    class _Gates:
        def line_snapshot(self, dispatch, line):
            return snapshot

    machine = object.__new__(DispatchMachine)
    machine._c = SimpleNamespace(assigner=assigner, gates=_Gates(), selector=_Selector())

    async def leave(coro):
        return await coro

    machine._leave = leave
    return machine


@pytest.fixture(autouse=True)
def _reset():
    role_types.set_registry_provider(None)
    launch_env._registry.clear()
    yield
    launch_env._registry.clear()
    role_types.set_registry_provider(None)


def _case():
    line = Line(id="l1", name="l1", task="t")
    return Dispatch(id="d1", goal="g", user_id="u", created_at="now", lines=[line]), line


def test_shadow_launches_the_selector_pick_and_records_both() -> None:
    dispatch, line = _case()
    snap: dict = {}
    decision = LineDecision("l1", "shadow", "superagent", choice=_choice("stepfun"))
    machine = _machine(_Assigner({}), snap)
    assert machine._apply_superagent(dispatch, line, "kimi", decision) == "kimi"
    (entry,) = assignment_entries(snap["assignment"])
    assert entry["mode"] == "shadow" and entry["source"] == "superagent"
    assert entry["choice"]["model"] == "stepfun"
    assert entry["selector_choice"]["model"] == "kimi"
    assert launch_env.for_line("d1", "l1") == {}


def test_enforce_sampled_adopts_and_passes_the_effort_to_the_launch() -> None:
    dispatch, line = _case()
    snap: dict = {}
    machine = _machine(_Assigner({}, sample=1.0), snap)
    decision = LineDecision("l1", "enforce", "superagent", choice=_choice("stepfun"))
    assert machine._apply_superagent(dispatch, line, "kimi", decision) == "stepfun"
    assert machine._c.selector.released == ["kimi"]
    assert launch_env.for_line("d1", "l1") == {launch_env.CLI_EFFORT_ENV: "high"}
    (entry,) = assignment_entries(snap["assignment"])
    assert entry["source"] == "superagent" and entry["mode"] == "enforce"


def test_enforce_unsampled_runs_the_selector_on_purpose() -> None:
    dispatch, line = _case()
    snap: dict = {}
    machine = _machine(_Assigner({}, sample=0.0), snap)
    decision = LineDecision("l1", "enforce", "superagent", choice=_choice("stepfun"))
    assert machine._apply_superagent(dispatch, line, "kimi", decision) == "kimi"
    (entry,) = assignment_entries(snap["assignment"])
    assert entry["source"] == "selector"


def test_registry_constraints_still_bind_the_super_agent() -> None:
    from src.server.services.autonomy.role_types import (
        SOURCE_STORE,
        ModelRef,
        RoleConstraints,
        RoleEntry,
        RoleRegistry,
        fallback_registry,
    )

    base = fallback_registry({}, router_model="r")
    worker = RoleEntry("worker", (ModelRef.parse("kimi"),), ModelRef.parse("kimi"), RoleConstraints())
    reg = RoleRegistry(entries=base.with_entry(worker).entries, source=SOURCE_STORE, revision=1)
    role_types.set_registry_provider(lambda: reg)
    dispatch, line = _case()
    snap: dict = {}
    machine = _machine(_Assigner({}, sample=1.0), snap)
    decision = LineDecision("l1", "enforce", "superagent", choice=_choice("stepfun"))
    assert machine._apply_superagent(dispatch, line, "kimi", decision) == "kimi"
    (entry,) = assignment_entries(snap["assignment"])
    assert entry["fallback_reason"] == "registry_excluded"


def test_no_decision_or_no_assigner_changes_nothing() -> None:
    dispatch, line = _case()
    snap: dict = {}
    machine = _machine(None, snap)
    assert machine._apply_superagent(dispatch, line, "kimi", None) == "kimi"
    assert snap == {}


@pytest.mark.asyncio
async def test_decisions_are_asked_once_for_unassigned_lines_only() -> None:
    line = Line(id="l1", name="l1", task="t")
    placed = Line(id="l2", name="l2", task="t", assigned_model="glm")
    dispatch = Dispatch(id="d1", goal="g", user_id="u", created_at="now", lines=[line, placed])
    assigner = _Assigner({"l1": "x"})
    machine = _machine(assigner, {})
    assert await machine._superagent_decisions(dispatch) == {"l1": "x"}
    assert assigner.calls == [["l1"]]
    assert await _machine(None, {})._superagent_decisions(dispatch) == {}


@pytest.mark.asyncio
async def test_an_assigner_fault_is_never_a_choice() -> None:
    class _Boom(_Assigner):
        async def decide(self, dispatch, lines):
            raise RuntimeError("model down")

    dispatch, _line = _case()
    assert await _machine(_Boom({}), {})._superagent_decisions(dispatch) == {}


def test_script_launcher_env_carries_only_a_registered_choice(tmp_path) -> None:
    from src.tools.auto_dispatch.execution import ScriptLauncher

    launcher = ScriptLauncher(
        script=tmp_path / "x.sh", cwd=tmp_path, runs_dir=tmp_path / "runs", dispatch_id="d1"
    )
    base = launcher._launch_env("stepfun", "l1")
    assert launch_env.CLI_EFFORT_ENV not in base
    launch_env.register("d1", "l1", {launch_env.CLI_EFFORT_ENV: "high"})
    assert launcher._launch_env("stepfun", "l1")[launch_env.CLI_EFFORT_ENV] == "high"
    assert launch_env.CLI_EFFORT_ENV not in launcher._launch_env("stepfun", "l2")


def test_the_adopted_model_is_reserved_and_its_env_persisted() -> None:
    """Review fixes 5 and 6 (fail on 3025c108e)."""
    dispatch, line = _case()
    snap: dict = {}
    machine = _machine(_Assigner({}, sample=1.0), snap)
    decision = LineDecision("l1", "enforce", "superagent", choice=_choice("stepfun"))
    machine._apply_superagent(dispatch, line, "kimi", decision)
    assert machine._c.selector.taken == ["stepfun"]
    assert snap[launch_env.SNAPSHOT_KEY] == {launch_env.CLI_EFFORT_ENV: "high"}
    # A restart loses the process map; restore() rebuilds it from the row.
    launch_env._registry.clear()
    dispatch.gates = {"lines": {"l1": snap}}
    assert launch_env.restore(dispatch) == 1
    assert launch_env.for_line("d1", "l1") == {launch_env.CLI_EFFORT_ENV: "high"}
    launch_env.clear_dispatch("d1")
    assert launch_env.for_line("d1", "l1") == {}


def test_restore_ignores_unknown_keys() -> None:
    dispatch, _line = _case()
    dispatch.gates = {"lines": {"l1": {launch_env.SNAPSHOT_KEY: {"PATH": "/evil", launch_env.RUNTIME_ENV: "official"}}}}
    launch_env.restore(dispatch)
    assert launch_env.for_line("d1", "l1") == {launch_env.RUNTIME_ENV: "official"}
