"""The N80 rollout mode table (L5): off, shadow, docs, full; default off.

HOME isolation and the real-HOME tripwire come from this directory's
``conftest.py``. Every check is pure: no database, no network, no clock.
"""

from __future__ import annotations

import os

import pytest

from src.collab import modes
from src.collab.modes import (
    MODE_ENV,
    MODES,
    TaskFacts,
    allowed_tools,
    current_mode,
    decide,
    is_docs_task,
    lease_allowed,
    mode_allows,
    mode_rank,
    mode_scopes,
    require_mode,
    resolve_mode,
)
from src.collab.scopes import SCOPES, TOOL_SCOPES
from src.collab.types import ModeDeniedError, NotClaimableError

ALL_TOOLS = tuple(sorted(TOOL_SCOPES))
DOCS_TASK = TaskFacts(app_claimable=True, write_scope=("docs/ports/x.md", "tests/unit/x/"))
CODE_TASK = TaskFacts(app_claimable=True, write_scope=("src/collab/modes.py",))
NO_GLOBS: tuple[str, ...] = ()


# --- resolution ------------------------------------------------------------


def test_home_is_isolated(tmp_path) -> None:
    assert os.environ["HOME"].startswith(str(tmp_path))


def test_mode_order_and_default() -> None:
    assert MODES == ("off", "shadow", "docs", "full")
    assert modes.DEFAULT_MODE == "off"
    assert MODE_ENV == "CONDUCTOR_COLLAB_MODE"
    assert [mode_rank(m) for m in MODES] == [0, 1, 2, 3]


@pytest.mark.parametrize("value", [None, "", "   ", "bogus", "ON", "true", "1", "fulll"])
def test_unset_or_unknown_resolves_to_off(value) -> None:
    environ = {} if value is None else {MODE_ENV: value}
    assert current_mode(environ=environ) == "off"
    assert resolve_mode(environ=environ) == "off"


def test_unset_process_env_is_off(monkeypatch) -> None:
    monkeypatch.delenv(MODE_ENV, raising=False)
    assert current_mode() == "off"
    assert decide("whoami").allowed is False


@pytest.mark.parametrize("value", ["shadow", " Docs ", "FULL"])
def test_known_modes_resolve_case_insensitively(value) -> None:
    assert current_mode(environ={MODE_ENV: value}) == value.strip().lower()


def test_explicit_mode_beats_environment() -> None:
    assert resolve_mode("full", environ={MODE_ENV: "off"}) == "full"
    assert resolve_mode("nonsense", environ={MODE_ENV: "full"}) == "off"


# --- off ---------------------------------------------------------------------


@pytest.mark.parametrize("tool", ALL_TOOLS)
def test_off_denies_every_verb(tool) -> None:
    decision = decide(tool, mode="off", task=DOCS_TASK, globs=NO_GLOBS)
    assert not decision.allowed
    assert decision.code == "mode_denied"
    assert decision.reason == modes.REASON_MODE_OFF
    with pytest.raises(ModeDeniedError) as exc:
        require_mode(tool, mode="off", task=DOCS_TASK)
    assert exc.value.code == "mode_denied"
    assert exc.value.details["mode"] == "off"


@pytest.mark.parametrize("tool", ALL_TOOLS)
def test_unknown_mode_value_denies_every_verb(tool) -> None:
    environ = {MODE_ENV: "typo"}
    assert decide(tool, environ=environ, task=DOCS_TASK, globs=NO_GLOBS).allowed is False


def test_off_allows_no_scope_and_no_tool() -> None:
    assert mode_scopes("off") == frozenset()
    assert mode_scopes("garbage") == frozenset()
    assert allowed_tools("off") == ()


# --- shadow ----------------------------------------------------------------


def test_shadow_allows_only_read_and_propose() -> None:
    assert mode_scopes("shadow") == {"read", "propose"}
    for tool, scope in TOOL_SCOPES.items():
        decision = decide(tool, mode="shadow", task=DOCS_TASK, globs=NO_GLOBS)
        assert decision.allowed is (scope in {"read", "propose"}), tool
        if not decision.allowed:
            assert decision.code == "mode_denied"
            assert decision.reason == modes.REASON_SCOPE_NOT_IN_MODE
    assert set(allowed_tools("shadow")) == {
        t for t, s in TOOL_SCOPES.items() if s in {"read", "propose"}
    }


def test_shadow_denies_claim_even_for_an_app_claimable_docs_task() -> None:
    decision = decide("task_claim", mode="shadow", task=DOCS_TASK, globs=NO_GLOBS)
    assert (decision.allowed, decision.code) == (False, "mode_denied")


# --- docs ----------------------------------------------------------------------


def test_docs_adds_claim_report_ask_review() -> None:
    assert mode_scopes("docs") == {"read", "propose", "claim", "report", "ask", "review"}
    for tool in ALL_TOOLS:
        assert decide(tool, mode="docs", task=DOCS_TASK, globs=NO_GLOBS).allowed, tool


@pytest.mark.parametrize(
    "tool", sorted(t for t, s in TOOL_SCOPES.items() if s in {"claim", "report", "review"})
)
def test_docs_limits_task_verbs_to_docs_tasks(tool) -> None:
    decision = decide(tool, mode="docs", task=CODE_TASK, globs=NO_GLOBS)
    if tool == "task_release":
        assert decision.allowed  # giving work back is never blocked
        return
    assert not decision.allowed
    assert (decision.code, decision.reason) == ("mode_denied", modes.REASON_NOT_DOCS_TASK)
    with pytest.raises(ModeDeniedError):
        decision.raise_for_refusal()


@pytest.mark.parametrize(
    "tool",
    sorted(
        t
        for t, s in TOOL_SCOPES.items()
        if s in {"claim", "report", "review"} and t != "task_release"
    ),
)
def test_docs_task_verbs_fail_closed_without_task_facts(tool) -> None:
    decision = decide(tool, mode="docs", globs=NO_GLOBS)
    assert (decision.allowed, decision.reason) == (False, modes.REASON_TASK_REQUIRED)


def test_docs_ask_is_checked_only_when_it_names_a_task() -> None:
    assert decide("agent_ask", mode="docs").allowed
    assert decide("agent_ask", mode="docs", task=DOCS_TASK, globs=NO_GLOBS).allowed
    assert not decide("agent_ask", mode="docs", task=CODE_TASK, globs=NO_GLOBS).allowed


def test_docs_read_and_propose_need_no_task() -> None:
    for tool in ("whoami", "task_list", "task_get", "task_create", "review_list"):
        assert decide(tool, mode="docs").allowed


@pytest.mark.parametrize(
    "scope",
    [
        ("docs/plans/next/n80.md",),
        ("docs/**",),
        ("tests/unit/collab/test_modes.py",),
        ("tests/unit/scripts/",),
        ("README.md",),
        ("src/collab/README.md",),
        ("src/**/*.md",),
        ("./docs/x.md",),
        ("docs/a.md", "tests/b.py", "CHANGELOG.md"),
    ],
)
def test_docs_task_accepts_docs_tests_and_markdown(scope) -> None:
    assert is_docs_task(TaskFacts(app_claimable=True, write_scope=scope), globs=NO_GLOBS)


@pytest.mark.parametrize(
    "scope",
    [
        (),
        ("src/collab/modes.py",),
        ("docs/x.md", "src/collab/modes.py"),
        ("**",),
        ("*",),
        ("docs",),
        ("docs/../src/x.py",),
        ("tests/../src/x.py",),
        ("/etc/passwd",),
        ("~/notes.md",),
        ("docs\\x.md",),
        ("src/x.md*",),
        ("src/.md",),
        (".md",),
        ("scripts/collab_report.py",),
    ],
)
def test_docs_task_refuses_everything_else(scope) -> None:
    assert not is_docs_task(TaskFacts(app_claimable=True, write_scope=scope), globs=NO_GLOBS)


def test_docs_task_refuses_risk_bearing() -> None:
    task = TaskFacts(app_claimable=True, write_scope=("docs/x.md",), risk_bearing=True)
    assert not is_docs_task(task, globs=NO_GLOBS)
    assert not decide("task_claim", mode="docs", task=task, globs=NO_GLOBS).allowed


@pytest.mark.parametrize(
    "path",
    [
        "tests/unit/auth/test_login.py",  # **/auth*/**
        "docs/secrets.md",  # **/secret*
        "tests/sandbox_probe/test_x.py",  # **/sandbox*/**
        "docs/.env.md",  # **/.env*
    ],
)
def test_docs_task_refuses_built_in_high_risk_globs(path) -> None:
    task = TaskFacts(app_claimable=True, write_scope=(path,))
    assert not is_docs_task(task, environ={})
    decision = decide("task_claim", mode="docs", task=task, environ={})
    assert (decision.allowed, decision.reason) == (False, modes.REASON_NOT_DOCS_TASK)


def test_docs_task_refuses_operator_appended_high_risk_globs() -> None:
    task = TaskFacts(app_claimable=True, write_scope=("docs/runbooks/prod.md",))
    assert is_docs_task(task, environ={})
    environ = {"CONDUCTOR_DISPATCH_HIGH_RISK_GLOBS": "docs/runbooks/**"}
    assert not is_docs_task(task, environ=environ)
    assert not decide("task_claim", mode="docs", task=task, environ=environ).allowed


def test_docs_claim_of_a_plain_docs_task_passes_with_real_globs() -> None:
    assert decide("task_claim", mode="docs", task=DOCS_TASK, environ={}).allowed


# --- full ----------------------------------------------------------------------


def test_full_allows_every_scope() -> None:
    assert mode_scopes("full") == frozenset(SCOPES)
    assert set(allowed_tools("full")) == set(TOOL_SCOPES)


def test_full_allows_any_app_claimable_ticket() -> None:
    risky = TaskFacts(
        app_claimable=True, write_scope=("src/tools/auto_dispatch/machine.py",), risk_bearing=True
    )
    for task in (DOCS_TASK, CODE_TASK, risky, TaskFacts(app_claimable=True)):
        assert decide("task_claim", mode="full", task=task).allowed
        assert require_mode("task_report", mode="full", task=task).allowed


@pytest.mark.parametrize("mode", ["docs", "full"])
def test_a_ticket_not_marked_app_claimable_is_not_claimable(mode) -> None:
    task = TaskFacts(app_claimable=False, write_scope=("docs/x.md",))
    decision = decide("task_claim", mode=mode, task=task, globs=NO_GLOBS)
    assert (decision.allowed, decision.code, decision.reason) == (
        False,
        "not_claimable",
        modes.REASON_NOT_APP_CLAIMABLE,
    )
    with pytest.raises(NotClaimableError):
        decision.raise_for_refusal()


def test_claim_needs_task_facts_in_full() -> None:
    decision = decide("task_claim", mode="full")
    assert (decision.allowed, decision.reason) == (False, modes.REASON_TASK_REQUIRED)


def test_dispatch_lines_wait_for_l4() -> None:
    line = TaskFacts(kind="line", app_claimable=True, write_scope=("docs/x.md",))
    assert modes.LINE_CLAIMS_ENABLED is False
    decision = decide("task_claim", mode="full", task=line)
    assert (decision.code, decision.reason) == ("not_claimable", modes.REASON_LINES_DISABLED)
    assert decide("task_claim", mode="full", task=line, lines_enabled=True).allowed


def test_unknown_tool_is_refused_not_defaulted() -> None:
    decision = decide("task_merge", mode="full")
    assert (decision.allowed, decision.reason) == (False, modes.REASON_UNKNOWN_TOOL)
    assert not mode_allows("push", mode="full")


# --- leases and facts ----------------------------------------------------------


def test_lease_allowed_follows_the_mode() -> None:
    assert not lease_allowed(DOCS_TASK, mode="off")
    assert not lease_allowed(DOCS_TASK, mode="shadow")
    assert lease_allowed(DOCS_TASK, mode="docs", globs=NO_GLOBS)
    assert not lease_allowed(CODE_TASK, mode="docs", globs=NO_GLOBS)
    assert lease_allowed(CODE_TASK, mode="full")
    assert not lease_allowed(CODE_TASK, environ={MODE_ENV: "unknown"})


def test_task_facts_from_mapping() -> None:
    facts = TaskFacts.from_mapping(
        {"app_claimable": True, "write_scope": "docs/x.md", "extra": 1}
    )
    assert facts == TaskFacts(app_claimable=True, write_scope=("docs/x.md",))
    # Only a real True marks a task claimable; a truthy string does not.
    assert TaskFacts.from_mapping({"app_claimable": "yes"}).app_claimable is False
    assert decide("task_claim", mode="full", task={"app_claimable": True}).allowed


def test_task_facts_validate_shape() -> None:
    with pytest.raises(ValueError):
        TaskFacts(kind="dispatch")
    with pytest.raises(TypeError):
        TaskFacts(write_scope="docs/x.md")  # type: ignore[arg-type]


def test_decisions_are_pure_and_repeatable() -> None:
    environ = {MODE_ENV: "docs"}
    first = decide("task_claim", environ=environ, task=DOCS_TASK, globs=NO_GLOBS)
    second = decide("task_claim", environ=environ, task=DOCS_TASK, globs=NO_GLOBS)
    assert first == second
    assert environ == {MODE_ENV: "docs"}
