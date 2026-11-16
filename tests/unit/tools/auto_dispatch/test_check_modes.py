"""N58: the R2d checks (N46, N47, N52) roll out as off | shadow | enforce.

Default is shadow: a check records what it would have refused on the line's G2
cell and on the scheduler audit (both shown by ``dispatch status``) and changes
nothing. Enforce behaves as the line implemented it; off skips the check.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.conductor_cli.dispatch import render_status
from src.tools.auto_dispatch.check_modes import (
    DEFAULT_MODE,
    ENFORCE,
    OFF,
    REQUIRE_TESTS_CHECK,
    REQUIRE_TESTS_ENV,
    SCOPE_CHECK,
    SCOPE_CHECK_ENV,
    SHADOW,
    SHADOW_REFUSAL,
    WORKTREE_CHECK,
    WORKTREE_CHECK_ENV,
    check_mode,
)
from src.tools.auto_dispatch.gates import G2_SCOPE_CONFLICT, G2_WORKTREE_DIRTY
from src.tools.auto_dispatch.planning import (
    ACCEPTANCE_MISSING_TEST,
    PlanRejection,
    PlanResult,
    validate_plan,
)
from src.tools.auto_dispatch.types import Line

from .test_gates_structural import _dispatch, _gate, _line, _Repo, _ScriptedCommands
from .test_planning_requires_tests import POLICY, _agents, _payload
from .test_planning_requires_tests import _line as _plan_line

ALL_ENVS = (REQUIRE_TESTS_ENV, WORKTREE_CHECK_ENV, SCOPE_CHECK_ENV)


@pytest.fixture(autouse=True)
def _clean_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ALL_ENVS:
        monkeypatch.delenv(name, raising=False)


def _audit(dispatch) -> list[dict]:
    scheduler = dispatch.gates.get("scheduler") or {}
    return [
        item
        for item in scheduler.get("audit") or []
        if item.get("kind") == SHADOW_REFUSAL
    ]


# --------------------------------------------------------------------------
# The switch itself
# --------------------------------------------------------------------------


def test_every_check_defaults_to_shadow() -> None:
    assert DEFAULT_MODE == SHADOW
    for name in ALL_ENVS:
        assert check_mode(name, {}) == SHADOW
        assert check_mode(name, {name: "  "}) == SHADOW


@pytest.mark.parametrize("raw,mode", [("off", OFF), ("Shadow", SHADOW), (" ENFORCE ", ENFORCE)])
def test_a_mode_is_read_case_insensitively(raw: str, mode: str) -> None:
    assert check_mode(SCOPE_CHECK_ENV, {SCOPE_CHECK_ENV: raw}) == mode


def test_a_value_that_is_not_a_mode_falls_back_to_shadow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert check_mode(WORKTREE_CHECK_ENV, {WORKTREE_CHECK_ENV: "enforcing"}) == SHADOW
    assert WORKTREE_CHECK_ENV in caplog.text


# --------------------------------------------------------------------------
# N46: plan validation
# --------------------------------------------------------------------------


def _untested_plan() -> dict:
    return _payload(_plan_line("one", acceptance=["cmd: ruff check src/one.py"]))


def _validate(payload: dict, **kwargs):
    return validate_plan(payload, policy=POLICY, agents=_agents("kimi"), **kwargs)


def test_enforce_refuses_a_plan_whose_code_line_runs_no_test() -> None:
    outcome = _validate(_untested_plan(), require_tests_mode=ENFORCE)
    assert isinstance(outcome, PlanRejection)
    assert outcome.reason == ACCEPTANCE_MISSING_TEST


def test_shadow_lets_the_plan_through_and_logs_the_refusal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        outcome = _validate(_untested_plan(), require_tests_mode=SHADOW)
    assert isinstance(outcome, PlanResult)
    assert ACCEPTANCE_MISSING_TEST in caplog.text
    assert "shadow" in caplog.text


def test_off_lets_the_plan_through_silently(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        outcome = _validate(_untested_plan(), require_tests_mode=OFF)
    assert isinstance(outcome, PlanResult)
    assert ACCEPTANCE_MISSING_TEST not in caplog.text


# N73 S4: the env switch is record-only, so ``enforce`` no longer refuses.
@pytest.mark.parametrize("raw,refused", [(None, False), ("shadow", False), ("off", False), ("enforce", False)])
def test_plan_validation_reads_its_mode_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, refused: bool
) -> None:
    if raw is not None:
        monkeypatch.setenv(REQUIRE_TESTS_ENV, raw)
    outcome = _validate(_untested_plan())
    assert isinstance(outcome, PlanRejection) is refused


def test_other_rejections_do_not_depend_on_the_mode() -> None:
    payload = _payload(_plan_line("one", acceptance=["cmd: pytest -q tests/unit"]))
    for mode in (OFF, SHADOW, ENFORCE):
        outcome = _validate(payload, require_tests_mode=mode)
        assert isinstance(outcome, PlanRejection)
        assert outcome.reason != ACCEPTANCE_MISSING_TEST


# --------------------------------------------------------------------------
# N46 at G2: shadow records the refusal on the line it let through
# --------------------------------------------------------------------------


def _untested_line(repo: _Repo) -> Line:
    return Line(
        id="line-0",
        name="test line",
        task="Implement the line",
        acceptance=("cmd: ruff check src/one.py",),
        branch=repo.branch,
        base_commit=repo.base,
        write_scope=("src/one.py",),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, SHADOW, ENFORCE, OFF])
async def test_g2_records_the_require_tests_refusal_only_in_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str | None
) -> None:
    if mode is not None:
        monkeypatch.setenv(REQUIRE_TESTS_ENV, mode)
    repo = _Repo(tmp_path, ("src/one.py",))
    gate = _gate(tmp_path, repo, _ScriptedCommands())
    line = _untested_line(repo)
    dispatch = _dispatch(line)

    step = await gate.run_g2(dispatch, line)

    # The gate's verdict never changes: the rule is a plan rule.
    assert step.passed
    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    # N73 S4: an env ``enforce`` is record-only, so it records like shadow.
    shadowed = mode in (None, SHADOW, ENFORCE)
    entries = [e for e in g2.get("shadow", []) if e["check"] == REQUIRE_TESTS_CHECK]
    assert bool(entries) is shadowed
    audit = [e for e in _audit(dispatch) if e["check"] == REQUIRE_TESTS_CHECK]
    assert bool(audit) is shadowed
    if shadowed:
        assert entries[0]["mode"] == SHADOW
        assert entries[0]["would_refuse"].startswith(ACCEPTANCE_MISSING_TEST)
        assert audit[0]["line_id"] == "line-0"


# --------------------------------------------------------------------------
# N47: the worktree check
# --------------------------------------------------------------------------


async def _run_dirty(tmp_path: Path):
    repo = _Repo(tmp_path, ("in_scope.txt",))
    gate = _gate(tmp_path, repo, _ScriptedCommands([("src/stub_module.py", "stub\n")]))
    line = _line(repo, ("in_scope.txt",))
    dispatch = _dispatch(line)
    step = await gate.run_g2(dispatch, line)
    return step, gate._line(dispatch, line.id)["gates"]["G2"], dispatch


@pytest.mark.asyncio
async def test_n73_worktree_check_enforce_is_record_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(WORKTREE_CHECK_ENV, ENFORCE)
    step, g2, dispatch = await _run_dirty(tmp_path)
    assert step.passed
    assert "dirty_worktree" not in g2
    (entry,) = [e for e in g2["shadow"] if e["check"] == WORKTREE_CHECK]
    assert entry["would_refuse"] == G2_WORKTREE_DIRTY


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, SHADOW])
async def test_worktree_check_shadow_records_and_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str | None
) -> None:
    if mode is not None:
        monkeypatch.setenv(WORKTREE_CHECK_ENV, mode)
    step, g2, dispatch = await _run_dirty(tmp_path)
    assert step.passed
    assert step.reason == "focused_tests_passed"
    assert g2["result"] == "pass"
    assert "dirty_worktree" not in g2
    (entry,) = [e for e in g2["shadow"] if e["check"] == WORKTREE_CHECK]
    assert entry["would_refuse"] == G2_WORKTREE_DIRTY
    assert entry["record"]["paths"] == ["src/stub_module.py"]
    assert "src/stub_module.py" in entry["finding"]
    (audited,) = [e for e in _audit(dispatch) if e["check"] == WORKTREE_CHECK]
    assert audited == {
        "kind": SHADOW_REFUSAL,
        "check": WORKTREE_CHECK,
        "line_id": "line-0",
        "attempt": 0,
        "would_refuse": G2_WORKTREE_DIRTY,
    }


@pytest.mark.asyncio
async def test_worktree_check_off_skips_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(WORKTREE_CHECK_ENV, OFF)
    step, g2, dispatch = await _run_dirty(tmp_path)
    assert step.passed
    assert "dirty_worktree" not in g2
    assert not [e for e in g2.get("shadow", []) if e["check"] == WORKTREE_CHECK]
    assert not [e for e in _audit(dispatch) if e["check"] == WORKTREE_CHECK]


@pytest.mark.asyncio
async def test_worktree_check_error_follows_the_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that raises is only recorded: N73 makes enforce record-only."""
    repo = _Repo(tmp_path, ("in_scope.txt",))
    line = _line(repo, ("in_scope.txt",))

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    for mode, passed in ((ENFORCE, True), (SHADOW, True), (OFF, True)):
        monkeypatch.setenv(WORKTREE_CHECK_ENV, mode)
        gate = _gate(tmp_path, repo, _ScriptedCommands())
        monkeypatch.setattr(gate, "_structural_findings", broken)
        dispatch = _dispatch(line)
        step = await gate.run_g2(dispatch, line)
        assert step.passed is passed, mode


# --------------------------------------------------------------------------
# N52: the scope check
# --------------------------------------------------------------------------


async def _run_out_of_scope(tmp_path: Path):
    repo = _Repo(tmp_path, ("in_scope.txt", "out_of_scope.txt"))
    gate = _gate(tmp_path, repo, _ScriptedCommands())
    line = _line(repo, ("in_scope.txt",))
    dispatch = _dispatch(line)
    step = await gate.run_g2(dispatch, line)
    return step, gate._line(dispatch, line.id)["gates"]["G2"], dispatch


@pytest.mark.asyncio
async def test_n73_scope_check_enforce_is_record_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SCOPE_CHECK_ENV, ENFORCE)
    step, g2, _ = await _run_out_of_scope(tmp_path)
    assert step.passed
    (entry,) = [e for e in g2["shadow"] if e["check"] == SCOPE_CHECK]
    assert entry["record"]["files"] == ["out_of_scope.txt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, SHADOW])
async def test_scope_check_shadow_records_and_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str | None
) -> None:
    if mode is not None:
        monkeypatch.setenv(SCOPE_CHECK_ENV, mode)
    step, g2, dispatch = await _run_out_of_scope(tmp_path)
    assert step.passed
    assert "scope_conflict" not in g2
    (entry,) = [e for e in g2["shadow"] if e["check"] == SCOPE_CHECK]
    assert entry["would_refuse"] == G2_SCOPE_CONFLICT
    assert entry["record"]["files"] == ["out_of_scope.txt"]
    assert entry["finding"].startswith("scope: ")
    assert [e["check"] for e in _audit(dispatch)] == [SCOPE_CHECK]


@pytest.mark.asyncio
async def test_scope_check_off_skips_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SCOPE_CHECK_ENV, OFF)
    step, g2, dispatch = await _run_out_of_scope(tmp_path)
    assert step.passed
    assert "scope_conflict" not in g2
    assert "shadow" not in g2
    assert _audit(dispatch) == []


@pytest.mark.asyncio
async def test_each_check_has_its_own_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope off, worktree in shadow: each check reads its own variable."""
    monkeypatch.setenv(SCOPE_CHECK_ENV, OFF)
    monkeypatch.setenv(WORKTREE_CHECK_ENV, SHADOW)
    repo = _Repo(tmp_path, ("in_scope.txt", "out_of_scope.txt"))
    gate = _gate(tmp_path, repo, _ScriptedCommands([("src/stub.py", "x\n")]))
    line = _line(repo, ("in_scope.txt",))
    dispatch = _dispatch(line)

    step = await gate.run_g2(dispatch, line)

    assert step.passed
    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    assert "dirty_worktree" not in g2
    assert [e["check"] for e in g2["shadow"]] == [WORKTREE_CHECK]


# --------------------------------------------------------------------------
# dispatch status shows the shadow refusals
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_status_shows_what_shadow_would_have_refused(
    tmp_path: Path,
) -> None:
    _, _, dispatch = await _run_out_of_scope(tmp_path)
    summary = {
        "id": dispatch.id,
        "state": "RUNNING",
        "lines": [line.to_dict() for line in dispatch.lines],
        "gates": dispatch.gates,
    }
    text = "\n".join(render_status(summary))
    assert f"shadow  {SCOPE_CHECK} would refuse: {G2_SCOPE_CONFLICT}" in text
