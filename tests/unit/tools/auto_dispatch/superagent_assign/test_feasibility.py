"""PRD 4.4: a pure check over a choice and an injected snapshot.

The snapshot carries roster, quota and launcher facts. The check does not
read the clock, the paid-credits environment variable, or the live roster.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from src.tools.auto_dispatch.pool import MIN_HEADROOM
from src.tools.auto_dispatch.selection import NO_HEADROOM as SELECTOR_NO_HEADROOM
from src.tools.auto_dispatch.selection import PLAN_EXHAUSTED as SELECTOR_PLAN_EXHAUSTED
from src.tools.auto_dispatch.superagent_assign.types import FeasibilityResult
from src.tools.auto_dispatch.superagent_assign.feasibility import (
    BASE_RUNTIMES,
    BREAKER_OPEN,
    CANNOT_TAKE_LINE,
    CLI_NOT_FOUND,
    DISABLED,
    GLM_RUNTIMES,
    IS_LINE_REVIEWER,
    MINIMAX_RUNTIMES,
    NO_HEADROOM,
    NOT_ELIGIBLE,
    PLAN_EXHAUSTED,
    PROVIDER_SKIPPED,
    RESERVED_ROLE,
    EFFORT_NEEDS_MODEL_ID,
    MODEL_NOT_LAUNCHABLE,
    UNKNOWN_EFFORT,
    UNKNOWN_MODEL_ID,
    UNSUPPORTED_RUNTIME,
    model_id_launchable,
    Choice,
    Snapshot,
    check_feasibility,
    launcher_runtimes,
)

WORK = (
    "kimi",
    "glm",
    "grok",
    "cursor",
    "stepfun",
    "deepseek",
    "minimax",
    "doubao",
)


def snapshot(**overrides: object) -> Snapshot:
    base: dict[str, object] = {
        "eligible_models": WORK,
        "can_take_line": WORK,
        "cli_found": WORK,
        "headroom": {name: 1.0 for name in WORK},
        "now": 1_000.0,
        "paid_credits_allowed": False,
    }
    base.update(overrides)
    return Snapshot(**base)  # type: ignore[arg-type]


def choice(**overrides: object) -> Choice:
    base: dict[str, object] = {
        "model": "kimi",
        "model_id": "default",
        "effort": "default",
        "runtime": "default",
    }
    base.update(overrides)
    return Choice(**base)  # type: ignore[arg-type]


def test_reason_codes_match_the_stable_spellings() -> None:
    assert RESERVED_ROLE == "reserved_role"
    assert NOT_ELIGIBLE == "not_eligible"
    assert CANNOT_TAKE_LINE == "cannot_take_line"
    assert UNKNOWN_MODEL_ID == "unknown_model_id"
    assert UNKNOWN_EFFORT == "unknown_effort"
    assert EFFORT_NEEDS_MODEL_ID == "effort_needs_model_id"
    assert MODEL_NOT_LAUNCHABLE == "model_not_launchable"
    assert UNSUPPORTED_RUNTIME == "unsupported_runtime"
    assert CLI_NOT_FOUND == "cli_not_found"
    assert DISABLED == "disabled"
    assert PROVIDER_SKIPPED == "provider_skipped"
    assert BREAKER_OPEN == "breaker_open"
    assert NO_HEADROOM == "no_headroom"
    assert PLAN_EXHAUSTED == "plan_exhausted"
    assert IS_LINE_REVIEWER == "is_line_reviewer"
    assert NO_HEADROOM == SELECTOR_NO_HEADROOM
    assert PLAN_EXHAUSTED == SELECTOR_PLAN_EXHAUSTED


def test_healthy_choice_is_ok() -> None:
    result = check_feasibility(choice(), snapshot())
    assert result == FeasibilityResult.ok()
    assert result.is_ok is True
    assert result.reason_code == ""


def test_mapping_and_attribute_choices_are_accepted() -> None:
    assert check_feasibility({"model": "grok", "effort": "low"}, snapshot()).is_ok
    other = SimpleNamespace(
        model="stepfun", model_id="", effort=None, runtime="native_cli"
    )
    assert check_feasibility(other, snapshot()).is_ok


def test_snapshot_must_be_injected() -> None:
    with pytest.raises(TypeError):
        check_feasibility(choice(), {"eligible_models": WORK})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "model", ("claude", "codex", "claude-code", "codexrev", "openai")
)
def test_claude_and_codex_are_never_authors(model: str) -> None:
    # Listed, capable, funded and present: Q1 still refuses them.
    roster = (*WORK, "claude", "codex")
    result = check_feasibility(
        choice(model=model),
        snapshot(eligible_models=roster, can_take_line=roster, cli_found=roster),
    )
    assert result.is_ok is False
    assert result.reason_code == RESERVED_ROLE


def test_reserved_wins_over_later_failures() -> None:
    result = check_feasibility(
        choice(model="claude", effort="ultra", runtime="docker", model_id="nope"),
        snapshot(cli_found=(), line_reviewers=("claude",), breakers={"claude": "open"}),
    )
    assert result.reason_code == RESERVED_ROLE


@pytest.mark.parametrize("model", ("manus", "agy", "", "unknown"))
def test_model_outside_the_work_roster_is_ineligible(model: str) -> None:
    result = check_feasibility(choice(model=model), snapshot())
    assert result.reason_code == NOT_ELIGIBLE


def test_can_take_line_is_required() -> None:
    capable = tuple(name for name in WORK if name != "kimi")
    result = check_feasibility(choice(), snapshot(can_take_line=capable))
    assert result.reason_code == CANNOT_TAKE_LINE


def test_default_model_id_is_accepted_without_a_known_list() -> None:
    assert check_feasibility(choice(model_id=""), snapshot()).is_ok
    assert check_feasibility(choice(model_id="Default"), snapshot()).is_ok
    refused = check_feasibility(choice(model_id="moonshot-v1"), snapshot())
    assert refused.reason_code == UNKNOWN_MODEL_ID


def test_known_model_id_or_default() -> None:
    known = {"glm": ("GLM-5.3",), "stepfun": ["step-3.7-flash"]}
    snap = snapshot(
        known_model_ids=known,
        runtime_cli_found={"glm:clean-room": ("zcode-cli",)},
    )
    # A concrete GLM id on the clean-room runtime, whose probe found zcode-cli.
    assert check_feasibility(
        choice(model="glm", model_id="GLM-5.3", runtime="clean-room"), snap
    ).is_ok
    assert check_feasibility(choice(model="glm", model_id="default"), snap).is_ok
    assert check_feasibility(
        choice(model="stepfun", model_id="step-3.7-flash"), snap
    ).is_ok
    wrong = check_feasibility(choice(model="glm", model_id="glm-5.3"), snap)
    assert wrong.reason_code == UNKNOWN_MODEL_ID
    # An empty list is "no list": only default survives.
    empty = snapshot(known_model_ids={"kimi": ()})
    assert check_feasibility(
        choice(model="kimi", model_id="anything"), empty
    ).reason_code == (UNKNOWN_MODEL_ID)


def test_launcher_rejects_inexpressible_combinations() -> None:
    """Cursor effort, official glm, and deepseek model ids are not argv flags."""
    providers = ("cursor", "glm", "deepseek")
    snap = Snapshot(
        providers,
        providers,
        providers,
        known_model_ids={
            "glm": ["GLM-5.3"],
            "deepseek": ["deepseek-v4-pro"],
            "cursor": ["composer-2"],
        },
        headroom=dict.fromkeys(providers, 1),
        runtime_cli_found={"glm:clean-room": ("zcode-cli",)},
    )
    cases = (
        Choice("cursor", effort="high"),
        Choice("glm", "GLM-5.3", runtime="official"),
        Choice("deepseek", "deepseek-v4-pro"),
    )
    assert all(not check_feasibility(case, snap).is_ok for case in cases)
    assert check_feasibility(cases[0], snap).reason_code == EFFORT_NEEDS_MODEL_ID
    assert check_feasibility(cases[1], snap).reason_code == MODEL_NOT_LAUNCHABLE
    assert check_feasibility(cases[2], snap).reason_code == MODEL_NOT_LAUNCHABLE

    # The same fields are launchable when the invocation can carry them.
    assert check_feasibility(Choice("cursor", "composer-2", effort="high"), snap).is_ok
    assert check_feasibility(Choice("cursor"), snap).is_ok
    assert check_feasibility(Choice("glm", "GLM-5.3", runtime="clean-room"), snap).is_ok
    assert check_feasibility(Choice("glm", runtime="official"), snap).is_ok
    assert check_feasibility(Choice("glm", runtime="default"), snap).is_ok
    assert check_feasibility(Choice("glm", runtime="native_cli"), snap).is_ok
    assert check_feasibility(Choice("deepseek"), snap).is_ok
    assert model_id_launchable("dsh", "deepseek-v4-pro", "default") is False
    assert check_feasibility(
        Choice("zcode", "GLM-5.3", runtime="official"), snap
    ).reason_code == (MODEL_NOT_LAUNCHABLE)


@pytest.mark.parametrize("runtime", ("default", "native_cli", "official", ""))
def test_concrete_glm_model_id_requires_clean_room_grammar(runtime: str) -> None:
    """With no injected binary, default and native_cli are official zcode.

    That grammar has no ``--model``. An injected ``zcode-cli`` default
    binary is the other case and is covered separately.
    """
    snap = Snapshot(
        ("glm",),
        ("glm",),
        ("glm",),
        known_model_ids={"glm": ("GLM-5.3",)},
        headroom={"glm": 1},
        runtime_cli_found={"glm:clean-room": ("zcode-cli",)},
    )
    result = check_feasibility(Choice("glm", "GLM-5.3", runtime=runtime), snap)
    assert result.is_ok is False
    assert result.reason_code == MODEL_NOT_LAUNCHABLE
    assert check_feasibility(Choice("glm", "GLM-5.3", runtime="clean-room"), snap).is_ok
    assert model_id_launchable("zcode", "GLM-5.3", runtime or "default") is False
    assert model_id_launchable("glm", "GLM-5.3", "clean-room") is True


def test_effort_must_be_in_the_table() -> None:
    assert check_feasibility(choice(model="kimi", effort="max"), snapshot()).is_ok
    assert check_feasibility(choice(model="grok", effort="low"), snapshot()).is_ok
    refused = check_feasibility(choice(model="grok", effort="ultra"), snapshot())
    assert refused.reason_code == UNKNOWN_EFFORT
    assert (
        check_feasibility(choice(effort="xhigh"), snapshot()).reason_code
        == UNKNOWN_EFFORT
    )


def test_launcher_runtimes_are_native_cli_plus_two_variants() -> None:
    assert launcher_runtimes("kimi") == BASE_RUNTIMES
    assert "docker" not in launcher_runtimes("kimi")
    assert "cloud" not in launcher_runtimes("glm")
    assert launcher_runtimes("zcode") == BASE_RUNTIMES | GLM_RUNTIMES
    assert launcher_runtimes("mcode") == BASE_RUNTIMES | MINIMAX_RUNTIMES
    assert {"clean-room", "official"} <= launcher_runtimes("glm")
    assert {"api-key", "login"} <= launcher_runtimes("minimax")


@pytest.mark.parametrize(
    ("model", "runtime"),
    (
        ("kimi", "default"),
        ("kimi", "native_cli"),
        ("kimi", ""),
        ("glm", "clean-room"),
        ("glm", "official"),
        ("minimax", "api-key"),
        ("minimax", "login"),
        ("doubao", "NATIVE_CLI"),
    ),
)
def test_supported_runtimes_pass(model: str, runtime: str) -> None:
    extra: dict[str, object] = {}
    if model == "glm" and runtime == "clean-room":
        # The roster name in cli_found is the official probe, not zcode-cli.
        extra["runtime_cli_found"] = {"glm:clean-room": ("zcode-cli",)}
    assert check_feasibility(choice(model=model, runtime=runtime), snapshot(**extra)).is_ok


@pytest.mark.parametrize(
    ("model", "runtime"),
    (
        ("kimi", "clean-room"),
        ("kimi", "api-key"),
        ("glm", "login"),
        ("minimax", "official"),
        ("grok", "docker"),
        ("cursor", "cloud"),
    ),
)
def test_invented_runtimes_are_refused(model: str, runtime: str) -> None:
    result = check_feasibility(choice(model=model, runtime=runtime), snapshot())
    assert result.reason_code == UNSUPPORTED_RUNTIME


def test_cli_must_be_found_and_provider_not_disabled() -> None:
    missing = tuple(name for name in WORK if name != "kimi")
    assert (
        check_feasibility(choice(), snapshot(cli_found=missing)).reason_code
        == CLI_NOT_FOUND
    )
    disabled = check_feasibility(choice(), snapshot(disabled_providers=("kimi",)))
    assert disabled.reason_code == DISABLED
    # Found under the official binary. That spelling is not clean-room.
    assert check_feasibility(choice(model="glm"), snapshot(cli_found=("zcode",))).is_ok


def test_official_zcode_does_not_prove_clean_room() -> None:
    """clean-room launches zcode-cli. Official zcode is a different file."""
    snap = Snapshot(("glm",), ("glm",), ("zcode",), headroom={"glm": 1})
    result = check_feasibility(Choice("glm", runtime="clean-room"), snap)
    assert result.is_ok is False
    assert result.reason_code == CLI_NOT_FOUND
    # The roster name is the default probe, not the clean-room binary either.
    roster = Snapshot(("glm",), ("glm",), ("glm",), headroom={"glm": 1})
    assert check_feasibility(Choice("glm", runtime="clean-room"), roster).reason_code == (
        CLI_NOT_FOUND
    )
    assert check_feasibility(Choice("glm", runtime="official"), snap).is_ok
    assert check_feasibility(Choice("glm"), snap).is_ok
    assert check_feasibility(Choice("glm", runtime="native_cli"), snap).is_ok
    # The other direction: zcode-cli does not satisfy command -v zcode.
    only_clean = Snapshot(("glm",), ("glm",), ("zcode-cli",), headroom={"glm": 1})
    assert check_feasibility(Choice("glm", runtime="clean-room"), only_clean).is_ok
    assert check_feasibility(Choice("glm"), only_clean).reason_code == CLI_NOT_FOUND
    assert check_feasibility(Choice("glm", runtime="official"), only_clean).reason_code == (
        CLI_NOT_FOUND
    )
    probed = Snapshot(
        ("glm",),
        ("glm",),
        ("zcode",),
        headroom={"glm": 1},
        runtime_cli_found={"glm:clean-room": ("zcode-cli",)},
    )
    assert check_feasibility(Choice("glm", runtime="clean-room"), probed).is_ok


def test_default_runtime_uses_injected_zcode_cli() -> None:
    """NATIVE_CLI_ZCODE_BIN=zcode-cli is a real default launch.

    The probe names the effective binary. Official zcode is not required,
    and that binary's clean-room grammar can carry a known model id.
    ``native_cli`` is the same launcher, so a probe under either key covers
    both. An explicit official runtime still needs the official binary.
    """
    snap = Snapshot(
        ("glm",),
        ("glm",),
        ("zcode-cli",),
        runtime_cli_found={"glm:default": ("zcode-cli",)},
        headroom={"glm": 1},
    )
    assert check_feasibility(Choice("glm"), snap).is_ok
    assert check_feasibility(Choice("glm", runtime="native_cli"), snap).is_ok
    assert check_feasibility(Choice("glm", runtime="clean-room"), snap).is_ok
    assert (
        check_feasibility(Choice("glm", runtime="official"), snap).reason_code
        == CLI_NOT_FOUND
    )

    via_native = Snapshot(
        ("glm",),
        ("glm",),
        ("zcode-cli",),
        runtime_cli_found={"glm:native_cli": ("/usr/local/bin/zcode-cli",)},
        known_model_ids={"zcode": ("GLM-5.3",)},
        headroom={"glm": 1},
    )
    assert check_feasibility(Choice("glm", "GLM-5.3"), via_native).is_ok
    assert check_feasibility(
        Choice("glm", "GLM-5.3", runtime="native_cli"), via_native
    ).is_ok
    assert (
        check_feasibility(
            Choice("glm", "GLM-5.3", runtime="official"), via_native
        ).reason_code
        == MODEL_NOT_LAUNCHABLE
    )
    # No probe: zcode-cli on PATH is not the binary the launcher selects.
    bare = Snapshot(
        ("glm",),
        ("glm",),
        ("zcode-cli",),
        known_model_ids={"glm": ("GLM-5.3",)},
        headroom={"glm": 1},
    )
    assert check_feasibility(Choice("glm"), bare).reason_code == CLI_NOT_FOUND
    assert (
        check_feasibility(Choice("glm", "GLM-5.3"), bare).reason_code
        == MODEL_NOT_LAUNCHABLE
    )
    assert model_id_launchable("glm", "GLM-5.3", "default", grammar="clean-room")
    assert model_id_launchable("glm", "GLM-5.3", "native_cli", grammar="clean-room")
    assert not model_id_launchable("glm", "GLM-5.3", "default")
    assert not model_id_launchable(
        "glm", "GLM-5.3", "official", grammar="clean-room"
    )
    # A mixed probe is not proof of the clean-room grammar.
    mixed = Snapshot(
        ("glm",),
        ("glm",),
        ("zcode-cli", "zcode"),
        runtime_cli_found={"glm:default": ("zcode-cli", "zcode")},
        known_model_ids={"glm": ("GLM-5.3",)},
        headroom={"glm": 1},
    )
    assert check_feasibility(Choice("glm"), mixed).is_ok
    assert (
        check_feasibility(Choice("glm", "GLM-5.3"), mixed).reason_code
        == MODEL_NOT_LAUNCHABLE
    )


def test_skip_blocks_only_before_the_injected_reset() -> None:
    # until=100 is long past on the wall clock. The injected now is 50, so
    # the skip has not reset. This fails if the check reads time.time().
    blocked = check_feasibility(
        choice(),
        snapshot(provider_skips={"kimi": 100}, now=50),
    )
    assert blocked.reason_code == PROVIDER_SKIPPED
    reset = check_feasibility(
        choice(),
        snapshot(
            provider_skips={"kimi": {"until": 100, "reason": "rate_limited"}}, now=100
        ),
    )
    assert reset.is_ok
    past = check_feasibility(choice(), snapshot(provider_skips={"kimi": 100}, now=101))
    assert past.is_ok
    # A bool until is not a timestamp (same rule as the machine's skip reader).
    ignored = check_feasibility(choice(), snapshot(provider_skips={"kimi": True}))
    assert ignored.is_ok


@pytest.mark.parametrize("state", ("open", "half_open", "OPEN"))
def test_breaker_must_be_closed(state: str) -> None:
    result = check_feasibility(choice(), snapshot(breakers={"kimi": state}))
    assert result.reason_code == BREAKER_OPEN


def test_absent_or_closed_breaker_passes() -> None:
    assert check_feasibility(choice(), snapshot()).is_ok
    assert check_feasibility(choice(), snapshot(breakers={"kimi": "closed"})).is_ok


def test_headroom_must_exceed_the_floor() -> None:
    assert MIN_HEADROOM == 0.0
    at_floor = check_feasibility(choice(), snapshot(headroom={"kimi": MIN_HEADROOM}))
    assert at_floor.reason_code == NO_HEADROOM
    missing = check_feasibility(choice(), snapshot(headroom={}))
    assert missing.reason_code == NO_HEADROOM
    # True must not count as 1.0 of quota.
    flagged = check_feasibility(choice(), snapshot(headroom={"kimi": True}))
    assert flagged.reason_code == NO_HEADROOM
    assert check_feasibility(
        choice(), snapshot(headroom={"kimi": MIN_HEADROOM + 0.01})
    ).is_ok


def test_paid_credits_cover_a_full_plan_only_when_allowed() -> None:
    full = {
        "plan_full": {"kimi": True},
        "paid_credits": {"kimi": True},
        "headroom": {"kimi": 0.0},
    }
    blocked = check_feasibility(choice(), snapshot(**full, paid_credits_allowed=False))
    assert blocked.reason_code == PLAN_EXHAUSTED
    allowed = check_feasibility(choice(), snapshot(**full, paid_credits_allowed=True))
    assert allowed.is_ok
    no_credits = check_feasibility(
        choice(),
        snapshot(
            plan_full={"kimi": True},
            paid_credits={"kimi": 0},
            headroom={"kimi": 0.0},
            paid_credits_allowed=True,
        ),
    )
    assert no_credits.reason_code == PLAN_EXHAUSTED
    numeric = check_feasibility(
        choice(),
        snapshot(
            plan_full={"kimi": True},
            paid_credits={"kimi": 3},
            headroom={"kimi": 0.0},
            paid_credits_allowed=True,
        ),
    )
    assert numeric.is_ok
    # Headroom alone is enough; a full plan does not cancel it.
    both = check_feasibility(
        choice(),
        snapshot(
            headroom={"kimi": 0.4}, plan_full={"kimi": True}, paid_credits_allowed=False
        ),
    )
    assert both.is_ok


def test_paid_credits_flag_comes_from_the_snapshot_not_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONDUCTOR_DISPATCH_ALLOW_PAID_CREDITS", "1")
    assert os.environ["CONDUCTOR_DISPATCH_ALLOW_PAID_CREDITS"] == "1"
    result = check_feasibility(
        choice(),
        snapshot(
            headroom={"kimi": 0.0},
            plan_full={"kimi": True},
            paid_credits={"kimi": True},
            paid_credits_allowed=False,
        ),
    )
    assert result.reason_code == PLAN_EXHAUSTED


def test_choice_cannot_be_the_lines_reviewer() -> None:
    result = check_feasibility(choice(model="kimi"), snapshot(line_reviewers=("kimi",)))
    assert result.reason_code == IS_LINE_REVIEWER
    other = check_feasibility(choice(model="glm"), snapshot(line_reviewers=("kimi",)))
    assert other.is_ok
    # The author and the reviewer are the same CLI under two spellings.
    aliased = check_feasibility(
        choice(model="glm"), snapshot(line_reviewers=("zcode",))
    )
    assert aliased.reason_code == IS_LINE_REVIEWER


def test_aliases_fold_before_every_fact() -> None:
    snap = snapshot(
        known_model_ids={"zcode": ("GLM-5.3",)},
        disabled_providers=(),
        headroom={"zai": 0.5},
        breakers={"zhipu": "closed"},
        provider_skips={"zcode": 10},
        now=50,
        cli_found=("zcode",),
        runtime_cli_found={"zcode:clean-room": ("/opt/bin/zcode-cli",)},
    )
    assert check_feasibility(
        choice(model="glm", model_id="GLM-5.3", runtime="clean-room"), snap
    ).is_ok
    skipped = check_feasibility(
        choice(model="zcode"),
        snapshot(provider_skips={"glm": {"until": 5_000}}, now=50),
    )
    assert skipped.reason_code == PROVIDER_SKIPPED
    # disabled_providers binds a runtime: zhipu is the cancel-only adapter,
    # not the zcode worker glm launches.
    assert check_feasibility(
        choice(model="glm"), snapshot(disabled_providers=("zhipu",))
    ).is_ok
    for spelling in ("glm", "zcode", "zcode-cli", "ZAI"):
        disabled = check_feasibility(
            choice(model="glm"), snapshot(disabled_providers=(spelling,))
        )
        assert disabled.reason_code == DISABLED, spelling
    open_breaker = check_feasibility(
        choice(model="zcode"), snapshot(breakers={"zai": "open"})
    )
    assert open_breaker.reason_code == BREAKER_OPEN


def test_disagreeing_alias_spellings_fail_closed() -> None:
    """Two spellings of one CLI are one plan. The tighter spelling wins."""
    exhausted = snapshot(headroom={"glm": 1.0, "zai": 0.0})
    assert (
        check_feasibility(choice(model="glm"), exhausted).reason_code == NO_HEADROOM
    )
    # The canonical key has reset; the zcode spelling has not.
    skipped = snapshot(
        provider_skips={"glm": {"until": 10}, "zcode": 5_000},
        now=50,
    )
    assert check_feasibility(choice(model="glm"), skipped).reason_code == (
        PROVIDER_SKIPPED
    )
    # NaN compares false with every timestamp, so it must not hide zcode.
    nan_skip = snapshot(
        provider_skips={"glm": float("nan"), "zcode": {"until": 5_000}},
        now=50,
    )
    assert check_feasibility(choice(model="glm"), nan_skip).reason_code == (
        PROVIDER_SKIPPED
    )
    opened = snapshot(breakers={"glm": "closed", "zhipu": "open"})
    assert check_feasibility(choice(model="zcode"), opened).reason_code == (
        BREAKER_OPEN
    )
    # Credits apply only when every spelling agrees the plan is full.
    split = snapshot(
        headroom={"kimi": 0.0, "moonshot": 0.0},
        plan_full={"kimi": True, "moonshot": False},
        paid_credits={"kimi": True, "moonshot": True},
        paid_credits_allowed=True,
    )
    assert check_feasibility(choice(), split).reason_code == PLAN_EXHAUSTED
    agreed = snapshot(
        headroom={"kimi": 0.0, "moonshot": 0.0},
        plan_full={"kimi": True, "moonshot": True},
        paid_credits={"kimi": 2, "moonshot": True},
        paid_credits_allowed=True,
    )
    assert check_feasibility(choice(), agreed).is_ok


def test_first_failing_condition_wins() -> None:
    # In the roster, but the model id is unknown, and so are effort, runtime,
    # the binary, the quota and the reviewer. The model id is the earliest.
    result = check_feasibility(
        choice(model_id="no-such-model", effort="ultra", runtime="docker"),
        snapshot(cli_found=(), headroom={"kimi": 0.0}, line_reviewers=("kimi",)),
    )
    assert result.reason_code == UNKNOWN_MODEL_ID


def test_ok_and_infeasible_results_disagree_on_a_reason() -> None:
    with pytest.raises(ValueError):
        FeasibilityResult(status="ok", reason_code=NO_HEADROOM)
    with pytest.raises(ValueError):
        FeasibilityResult(status="infeasible", reason_code="")


def test_disabling_the_cancel_only_zhipu_lane_keeps_glm_launchable() -> None:
    """Reviewer repro: registry.disabled_provider_reason keys on runtime id."""
    snap = Snapshot(
        ("glm",), ("glm",), ("zcode",), disabled_providers=("zhipu",), headroom={"glm": 1}
    )
    assert check_feasibility(Choice("glm"), snap).is_ok
    deepseek = snapshot(disabled_providers=("deepseek-dsh",))
    assert check_feasibility(choice(model="deepseek"), deepseek).reason_code == DISABLED
    assert check_feasibility(choice(model="dsh"), deepseek).reason_code == DISABLED


def test_official_runtime_accepts_an_injected_official_wrapper() -> None:
    """Reviewer repro: an override that is not zcode-cli speaks official."""
    snap = Snapshot(
        ("glm",),
        ("glm",),
        (),
        runtime_cli_found={"glm:official": ("/opt/bin/zcode-wrapper",)},
        headroom={"glm": 1},
    )
    assert check_feasibility(Choice("glm", runtime="official"), snap).is_ok
    # A zcode-cli probe under the official key is still not official.
    clean = Snapshot(
        ("glm",),
        ("glm",),
        (),
        runtime_cli_found={"glm:official": ("/opt/bin/zcode-cli",)},
        headroom={"glm": 1},
    )
    assert (
        check_feasibility(Choice("glm", runtime="official"), clean).reason_code
        == CLI_NOT_FOUND
    )


def test_cursor_model_id_with_a_bracket_block_is_not_launchable() -> None:
    """effort.apply_model_id refuses it, so the check must too."""
    snap = snapshot(known_model_ids={"cursor": ("gpt-5[effort=low]", "gpt-5")})
    bracketed = check_feasibility(
        choice(model="cursor", model_id="gpt-5[effort=low]", effort="high"), snap
    )
    assert bracketed.reason_code == MODEL_NOT_LAUNCHABLE
    assert check_feasibility(
        choice(model="cursor", model_id="gpt-5", effort="high"), snap
    ).is_ok


def test_result_is_the_package_feasibility_type() -> None:
    """record.py stores this type; a second result class would not round-trip."""
    from src.tools.auto_dispatch.superagent_assign import types

    result = check_feasibility(choice(), snapshot(cli_found=()))
    assert type(result) is types.FeasibilityResult
    assert result.to_dict() == {"status": "infeasible", "reason_code": CLI_NOT_FOUND}


def test_accepts_a_line_choice() -> None:
    from src.tools.auto_dispatch.superagent_assign.types import LineChoice

    line = LineChoice(
        line_id="line-1",
        model="stepfun",
        effort="high",
        runtime="native_cli",
        reason="stepfun passed 9/10 on src/web",
        confidence="medium",
    )
    assert check_feasibility(line, snapshot()).is_ok
