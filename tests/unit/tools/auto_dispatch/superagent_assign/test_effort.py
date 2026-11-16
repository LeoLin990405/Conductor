"""PRD 4.2: one effort row per CLI, and default is today's launch flags.

Grok is the only provider whose current command carries an effort flag
(``--reasoning-effort high``). Everyone else's ``default`` adds nothing.
Providers with no parameter add nothing at every level.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.tools.auto_dispatch.superagent_assign.effort import (
    EFFORT_TABLE,
    NA,
    UNIFIED_EFFORTS,
    EffortNotLaunchable,
    apply_model_id,
    effort_in_table,
    resolve_effort,
)
from src.tools.auto_dispatch.superagent_assign.types import Effort

LAUNCHER = Path(__file__).resolve().parents[5] / "scripts" / "dispatch_launch.sh"

# PRD 4.2 providers, in table order. n/a rows are the last four.
PRD_PROVIDERS = (
    "claude",
    "codex",
    "grok",
    "cursor",
    "stepfun",
    "doubao",
    "kimi",
    "glm",
    "deepseek",
    "minimax",
)
NA_PROVIDERS = ("kimi", "glm", "deepseek", "minimax")

# (provider, low, medium, high, max, argv builder or None for "no argv").
# Cursor's builder is unused: its token is a model-string suffix.
FLAGGED = (
    ("claude", "low", "medium", "high", "max", ("--effort",)),
    ("codex", "low", "medium", "high", "ultra", ("-c", "model_reasoning_effort=")),
    ("grok", "low", "medium", "high", "high", ("--reasoning-effort",)),
    ("stepfun", "low", "medium", "high", "max", ("--thinking",)),
    ("doubao", "low", "medium", "high", "high", ("--reasoning-effort",)),
)


def test_table_is_exactly_the_prd_providers() -> None:
    assert tuple(EFFORT_TABLE) == PRD_PROVIDERS


def test_every_row_covers_the_unified_scale() -> None:
    scale = set(UNIFIED_EFFORTS)
    assert scale == {"default", "low", "medium", "high", "max"}
    for provider, row in EFFORT_TABLE.items():
        assert set(row.levels) == scale, provider


@pytest.mark.parametrize("provider", PRD_PROVIDERS)
def test_default_is_todays_flags(provider: str) -> None:
    resolved = resolve_effort(provider, "default")
    assert resolved.supported is True
    if provider == "grok":
        assert resolved.cli_effort == "high"
        assert resolved.argv == ("--reasoning-effort", "high")
        assert resolved.adds_flag is True
        assert resolved.argv == resolve_effort("grok", "high").argv
    else:
        assert resolved.argv == ()
        assert resolved.model_suffix == ""
        assert resolved.adds_flag is False


@pytest.mark.parametrize(
    ("provider", "low", "medium", "high", "maximum", "prefix"),
    FLAGGED,
)
def test_flagged_provider_maps_each_level(
    provider: str,
    low: str,
    medium: str,
    high: str,
    maximum: str,
    prefix: tuple[str, ...],
) -> None:
    expected = {"low": low, "medium": medium, "high": high, "max": maximum}
    for level, token in expected.items():
        resolved = resolve_effort(provider, level)
        assert resolved.supported is True
        assert resolved.cli_effort == token
        assert resolved.adds_flag is True
        if provider == "codex":
            assert resolved.argv == ("-c", f"model_reasoning_effort={token}")
            assert resolved.model_suffix == ""
        else:
            assert resolved.argv == (*prefix, token)
            assert resolved.model_suffix == ""


def test_cursor_writes_effort_into_the_model_string() -> None:
    default = resolve_effort("cursor", "default")
    assert default.argv == ()
    assert default.model_suffix == ""
    assert default.cli_effort is None

    high = resolve_effort("cursor", "high")
    assert high.argv == ()
    assert high.cli_effort == "high"
    assert high.model_suffix == "[effort=high]"
    assert resolve_effort("cursor", "max").model_suffix == "[effort=high]"
    assert resolve_effort("cursor", "low").model_suffix == "[effort=low]"

    # A default model id must not swallow the suffix and look like success.
    for blank in ("default", "", None):
        with pytest.raises(EffortNotLaunchable):
            apply_model_id(blank, high)
    assert apply_model_id("default", default) is None
    assert apply_model_id("gpt-5", high) == "gpt-5[effort=high]"
    assert apply_model_id("gpt-5", default) == "gpt-5"
    # A non-cursor provider does not decorate a concrete model id.
    assert apply_model_id("GLM-5.3", resolve_effort("glm", "max")) == "GLM-5.3"


@pytest.mark.parametrize("provider", NA_PROVIDERS)
@pytest.mark.parametrize("level", UNIFIED_EFFORTS)
def test_na_providers_add_no_flag(provider: str, level: str) -> None:
    row = EFFORT_TABLE[provider]
    assert row.style == "none"
    resolved = resolve_effort(provider, level)
    assert resolved.supported is True
    assert resolved.cli_effort == NA
    assert resolved.argv == ()
    assert resolved.model_suffix == ""
    assert resolved.adds_flag is False
    assert effort_in_table(provider, level) is True


def test_cli_only_words_are_not_inputs() -> None:
    # ultra / xhigh / minimal are tokens the table emits, not levels a choice names.
    for provider, word in (
        ("codex", "ultra"),
        ("claude", "xhigh"),
        ("stepfun", "xhigh"),
        ("doubao", "minimal"),
        ("grok", "maxx"),
    ):
        assert effort_in_table(provider, word) is False
        resolved = resolve_effort(provider, word)
        assert resolved.supported is False
        assert resolved.argv == ()
        assert resolved.adds_flag is False


def test_unknown_provider_is_not_in_the_table() -> None:
    assert effort_in_table("manus", "high") is False
    resolved = resolve_effort("manus", "default")
    assert resolved.supported is False
    assert resolved.argv == ()
    assert resolved.cli_effort is None


def test_blank_and_case_fold_onto_the_scale() -> None:
    assert resolve_effort("grok", "  HIGH ").argv == ("--reasoning-effort", "high")
    assert resolve_effort("claude", "").argv == ()
    assert resolve_effort("claude", None).cli_effort is None
    assert effort_in_table("kimi", " Max ") is True


@pytest.mark.parametrize(
    ("alias", "canonical"),
    (
        ("zcode", "glm"),
        ("zcode-cli", "glm"),
        ("arkcli", "doubao"),
        ("dsh", "deepseek"),
        ("mcode", "minimax"),
        ("step", "stepfun"),
        ("cursor-agent", "cursor"),
        ("claude-code", "claude"),
    ),
)
def test_aliases_use_the_canonical_row(alias: str, canonical: str) -> None:
    alias_resolved = resolve_effort(alias, "max")
    canonical_resolved = resolve_effort(canonical, "max")
    assert alias_resolved.provider == canonical
    assert alias_resolved.argv == canonical_resolved.argv
    assert alias_resolved.cli_effort == canonical_resolved.cli_effort
    assert alias_resolved.model_suffix == canonical_resolved.model_suffix


def test_grok_default_matches_the_hardcoded_launch_flag() -> None:
    """The single source of grok's today-flag, not a second translation."""
    row = EFFORT_TABLE["grok"]
    assert row.levels["default"] == "high"
    assert row.levels["max"] == "high"
    assert row.flag == ("--reasoning-effort",)
    assert resolve_effort("grok").argv == ("--reasoning-effort", "high")


def test_scale_is_the_shared_effort_enum() -> None:
    assert UNIFIED_EFFORTS == tuple(member.value for member in Effort)
    for provider, row in EFFORT_TABLE.items():
        assert tuple(row.levels) == UNIFIED_EFFORTS, provider


@pytest.mark.parametrize("provider", PRD_PROVIDERS)
@pytest.mark.parametrize("member", list(Effort))
def test_effort_enum_members_resolve_like_their_values(
    provider: str, member: Effort
) -> None:
    by_member = resolve_effort(provider, member)
    by_value = resolve_effort(provider, member.value)
    assert by_member == by_value
    assert by_member.supported is True
    assert effort_in_table(provider, member) is True


@pytest.mark.parametrize("provider", PRD_PROVIDERS)
def test_default_never_hands_the_launcher_a_token(provider: str) -> None:
    """Unset env is today's argv byte for byte, grok included."""
    resolved = resolve_effort(provider, Effort.DEFAULT)
    assert resolved.launch_token is None
    assert resolved.changes_launch is False


@pytest.mark.parametrize("provider", NA_PROVIDERS)
@pytest.mark.parametrize("level", UNIFIED_EFFORTS)
def test_na_is_a_record_value_not_a_launch_token(provider: str, level: str) -> None:
    resolved = resolve_effort(provider, level)
    assert resolved.cli_effort == NA
    assert resolved.launch_token is None
    assert resolved.changes_launch is False


@pytest.mark.parametrize(
    ("provider", "level", "token"),
    (
        ("claude", "max", "max"),
        ("codex", "max", "ultra"),
        ("grok", "low", "low"),
        ("grok", "high", "high"),
        ("stepfun", "max", "max"),
        ("doubao", "max", "high"),
    ),
)
def test_named_levels_hand_the_launcher_the_mapped_token(
    provider: str, level: str, token: str
) -> None:
    resolved = resolve_effort(provider, level)
    assert resolved.launch_token == token
    assert resolved.changes_launch is True


def test_unsupported_resolution_has_no_launch_token() -> None:
    assert resolve_effort("manus", "high").launch_token is None
    assert resolve_effort("grok", "ultra").launch_token is None


def test_render_cannot_be_fed_a_token_outside_the_table() -> None:
    row = EFFORT_TABLE["grok"]
    assert not hasattr(row, "render")
    assert row._render("low").argv == ("--reasoning-effort", "low")
    with pytest.raises(KeyError):
        row._render("bogus")


@pytest.mark.parametrize("level", UNIFIED_EFFORTS)
def test_cursor_model_id_cannot_carry_its_own_effort(level: str) -> None:
    """Regression: ``gpt-5[effort=low]`` bypassed the table at default/high."""
    resolved = resolve_effort("cursor", level)
    with pytest.raises(EffortNotLaunchable):
        apply_model_id("gpt-5[effort=low]", resolved)
    with pytest.raises(EffortNotLaunchable):
        apply_model_id("gpt-5[x]", resolved)


def test_brackets_are_left_alone_off_cursor() -> None:
    assert apply_model_id("model[v2]", resolve_effort("stepfun", "high")) == "model[v2]"


def _launcher_effort_lines() -> list[str]:
    patterns = (
        "--reasoning-effort",
        "--effort",
        "--thinking",
        "model_reasoning_effort",
        "[effort=",
    )
    lines = []
    for raw in LAUNCHER.read_text(encoding="utf-8").splitlines():
        code = raw.split("#", 1)[0]
        if any(pattern in code for pattern in patterns):
            lines.append(code)
    return lines


def test_default_matches_the_real_launcher() -> None:
    """Acceptance 4 against dispatch_launch.sh, not against the table itself.

    With no assignment only grok carries an effort flag, and its fallback
    token is the table's grok ``default``. Since N73 L2 the launcher also
    passes ``CONDUCTOR_LINE_CLI_EFFORT`` (stepfun ``--thinking``, doubao
    ``--reasoning-effort``, cursor ``[effort=]``), but only from that
    variable: every other effort-bearing line must reference
    ``line_cli_effort``, never a literal level.
    If the launcher grows an unconditional effort flag, or grok's fallback
    drifts, the table's ``default`` column is stale and this fails. The
    byte-for-byte argv is pinned by tests/unit/scripts/
    test_dispatch_launch_assignment.py.
    """
    lines = _launcher_effort_lines()
    grok_default = EFFORT_TABLE["grok"].levels["default"]
    grok_lines = [line for line in lines if "--reasoning-effort" in line]
    fallback = f'--reasoning-effort "${{line_cli_effort:-{grok_default}}}"'
    assert any(fallback in line for line in grok_lines), grok_lines
    for line in lines:
        if fallback in line:
            continue
        assert "line_cli_effort" in line, line
        assert "model_reasoning_effort" not in line, line
    for provider in PRD_PROVIDERS:
        if provider != "grok":
            assert resolve_effort(provider, "default").argv == (), provider


def test_cursor_effort_has_one_carrier_the_model_string() -> None:
    resolved = resolve_effort("cursor", "medium")
    assert resolved.launch_token is None
    assert resolved.changes_launch is True
    assert apply_model_id("gpt-5", resolved) == "gpt-5[effort=medium]"
    assert resolve_effort("cursor", "default").changes_launch is False
