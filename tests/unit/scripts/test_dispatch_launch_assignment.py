"""N73 L2: per-line model, effort and runtime flags in the launch script.

``scripts/dispatch_launch.sh`` receives the dispatcher's choice through
``CONDUCTOR_LINE_MODEL_ID``, ``CONDUCTOR_LINE_CLI_EFFORT`` and
``CONDUCTOR_LINE_RUNTIME`` (PRD 4.1/4.2).  These tests run the real script
against fake CLIs that write their argv as NUL-separated bytes, so no model is
started and no quota is spent.

The golden lists below are copied from main (``e702aad00``): when none of the
three variables is set, the script must hand each worker exactly that argv,
byte for byte, for a fresh launch and for a resumed session.  Every other
test is the golden argv plus the one provider flag the assignment adds, in
the position the provider's parser needs it.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "dispatch_launch.sh"

EXIT_RESERVED = 65
EXIT_BAD_ASSIGNMENT = 66

# Roster names this script can start. claude and codex are reserved roles and
# are covered separately.
WORKERS = (
    "kimi",
    "glm",
    "grok",
    "cursor",
    "stepfun",
    "deepseek",
    "minimax",
    "doubao",
)

# Every executable the roster can select, including both GLM grammars.
BINARY_NAMES = (
    "kimi",
    "zcode",
    "zcode-cli",
    "grok",
    "cursor-agent",
    "step",
    "dsh",
    "mcode",
    "arkcli",
)

# The argv main builds for a fresh run with no assignment.  <PROMPT>,
# <PROMPT_FILE> and <PWD> replace the run-specific values.
GOLDEN = {
    "kimi": ["-p", "<PROMPT>", "--output-format", "stream-json"],
    "glm": [
        "--prompt",
        "<PROMPT>",
        "--output-format",
        "stream-json",
        "--mode",
        "yolo",
        "--surface",
        "terminal",
        "--no-color",
    ],
    "glm-clean-room": [
        "--prompt",
        "<PROMPT>",
        "--output-format",
        "stream-json",
        "--mode",
        "yolo",
        "--model",
        "GLM-5.3",
        "--max-turns",
        "200",
        "--no-color",
    ],
    "grok": [
        "--prompt-file",
        "<PROMPT_FILE>",
        "--always-approve",
        "--reasoning-effort",
        "high",
        "--max-turns",
        "150",
        "--output-format",
        "streaming-json",
    ],
    "cursor": [
        "-p",
        "--force",
        "--trust",
        "--workspace",
        "<PWD>",
        "--output-format",
        "stream-json",
        "<PROMPT>",
    ],
    "stepfun": [
        "-p",
        "--no-session",
        "--approval-mode",
        "auto",
        "--non-interactive-approval",
        "allow",
        "--mode",
        "json",
        "<PROMPT>",
    ],
    "deepseek": ["--profile", "headless", "-"],
    "minimax": [
        "exec",
        "--cwd",
        "<PWD>",
        "--prompt-mode",
        "coding",
        "--permission",
        "full",
        "--output-format",
        "stream-json",
        "--input",
        "-",
    ],
    "doubao": ["+chat", "--format", "json", "--no-progress", "--", "<PROMPT>"],
}

# The argv main builds for --session-id=sess-1 --session-mode=resume with no
# assignment. Written out in full so a dropped or reordered resume argument
# cannot hide behind the fresh-launch lists. stepfun, deepseek and doubao
# accept the session options and add no flag of their own.
GOLDEN_RESUME = {
    "kimi": [
        "-p",
        "<PROMPT>",
        "--output-format",
        "stream-json",
        "--session",
        "<SESSION>",
    ],
    "glm": [
        "--prompt",
        "<PROMPT>",
        "--output-format",
        "stream-json",
        "--mode",
        "yolo",
        "--surface",
        "terminal",
        "--no-color",
        "--resume",
        "<SESSION>",
    ],
    "glm-clean-room": [
        "--prompt",
        "<PROMPT>",
        "--output-format",
        "stream-json",
        "--mode",
        "yolo",
        "--model",
        "GLM-5.3",
        "--max-turns",
        "200",
        "--no-color",
        "--resume",
        "<SESSION>",
    ],
    "grok": [
        "--prompt-file",
        "<PROMPT_FILE>",
        "--always-approve",
        "--reasoning-effort",
        "high",
        "--max-turns",
        "150",
        "--output-format",
        "streaming-json",
        "--resume",
        "<SESSION>",
    ],
    "cursor": [
        "-p",
        "--force",
        "--trust",
        "--workspace",
        "<PWD>",
        "--output-format",
        "stream-json",
        "--resume",
        "<SESSION>",
        "<PROMPT>",
    ],
    "stepfun": [
        "-p",
        "--no-session",
        "--approval-mode",
        "auto",
        "--non-interactive-approval",
        "allow",
        "--mode",
        "json",
        "<PROMPT>",
    ],
    "deepseek": ["--profile", "headless", "-"],
    "minimax": [
        "exec",
        "--cwd",
        "<PWD>",
        "--prompt-mode",
        "coding",
        "--permission",
        "full",
        "--output-format",
        "stream-json",
        "--session",
        "<SESSION>",
        "--input",
        "-",
    ],
    "doubao": ["+chat", "--format", "json", "--no-progress", "--", "<PROMPT>"],
}

# A fake CLI writes exactly the argv it was handed, NUL-separated, so a value
# containing spaces or an empty argument is still visible to the assertion.
FAKE_CLI = """#!/bin/bash
: "${FAKE_ARGV:?}"
printf '%s\\0' "$@" > "$FAKE_ARGV"
printf '%s' "${MINIMAX_DATA_DIR-<unset>}" > "$FAKE_MINIMAX_DIR"
printf '%s' "${MCODE_PROVIDER_API_KEY-<unset>}" > "$FAKE_MINIMAX_KEY"
exit "${FAKE_EXIT:-0}"
"""


def _write_fake(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_text(FAKE_CLI, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _normalize(argv: list[str]) -> list[str]:
    """Replace the run-specific prompt, prompt-file, cwd and session id."""
    normalized = list(argv)
    for flag, placeholder in (
        ("--prompt-file", "<PROMPT_FILE>"),
        ("--workspace", "<PWD>"),
        ("--cwd", "<PWD>"),
        ("--resume", "<SESSION>"),
        ("--session", "<SESSION>"),
    ):
        if flag in normalized:
            normalized[normalized.index(flag) + 1] = placeholder
    normalized = ["<PROMPT>" if item == "the task" else item for item in normalized]
    return normalized


def _environment(bin_dir: Path, home: Path, **extra: str) -> dict[str, str]:
    environment = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(home / "logs"),
        "FAKE_ARGV": str(home / "argv.bin"),
        "FAKE_MINIMAX_DIR": str(home / "minimax-dir.txt"),
        "FAKE_MINIMAX_KEY": str(home / "minimax-key.txt"),
        # A caller's own assignment must not leak into a test that does not
        # set one; the environment is built explicitly below.
        "CONDUCTOR_LINE_MODEL_ID": "",
        "CONDUCTOR_LINE_CLI_EFFORT": "",
        "CONDUCTOR_LINE_RUNTIME": "",
    }
    environment.update(extra)
    return environment


@pytest.fixture
def run(tmp_path: Path):
    """Run the launcher with fresh fake CLIs for every roster binary."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in BINARY_NAMES:
        _write_fake(bin_dir, name)

    def _run(
        *args: str,
        env: dict[str, str] | None = None,
        binaries: bool = True,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        (tmp_path / "argv.bin").unlink(missing_ok=True)
        path_dir = bin_dir if binaries else tmp_path / "no-bin"
        path_dir.mkdir(exist_ok=True)
        environment = _environment(path_dir, tmp_path)
        if env:
            environment.update(env)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=str(cwd or tmp_path),
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    return _run


def _argv(tmp_path: Path) -> list[str]:
    raw = (tmp_path / "argv.bin").read_bytes()
    assert raw.endswith(b"\0"), "the fake CLI records a trailing NUL"
    return [part.decode() for part in raw.split(b"\0")[:-1]]


def _assert_no_argv(tmp_path: Path) -> None:
    assert not (tmp_path / "argv.bin").exists(), "a refused launch started a Worker"


def _without_pair(argv: list[str], flag: str, value: str) -> list[str]:
    """Exactly one ``flag value`` pair removed; the rest is unchanged."""
    assert argv.count(flag) == 1, (flag, argv)
    index = argv.index(flag)
    assert argv[index + 1] == value, (flag, value, argv)
    return argv[:index] + argv[index + 2 :]


def _assert_pair_added(
    argv: list[str], golden: list[str], flag: str, value: str
) -> None:
    assert _without_pair(_normalize(argv), flag, value) == golden


# ------------------------------------------------------------------ baselines


@pytest.mark.parametrize("roster", WORKERS)
def test_unset_assignment_matches_main_byte_for_byte(run, tmp_path, roster):
    result = run(roster, "the task")
    assert result.returncode == 0, result.stderr
    assert _normalize(_argv(tmp_path)) == GOLDEN[roster]


def test_unset_assignment_matches_main_for_a_clean_room_glm(run, tmp_path):
    """A clean-room binary keeps main's clean-room argv, model flag included."""
    clean = tmp_path / "clean"
    clean.mkdir()
    clean_room = _write_fake(clean, "zcode-cli")
    result = run("glm", "the task", env={"NATIVE_CLI_ZCODE_BIN": str(clean_room)})
    assert result.returncode == 0, result.stderr
    assert _normalize(_argv(tmp_path)) == GOLDEN["glm-clean-room"]


@pytest.mark.parametrize("roster", (*WORKERS, "glm-clean-room"))
@pytest.mark.parametrize("assignment", ("unset", "default"))
def test_unset_assignment_resume_matches_main_byte_for_byte(
    run, tmp_path, roster, assignment
):
    """Resumed argv with no assignment is main's full list, not a flag check."""
    launch = roster
    env: dict[str, str] = {}
    if roster == "glm-clean-room":
        launch = "glm"
        clean = tmp_path / f"clean-{assignment}"
        clean.mkdir()
        env["NATIVE_CLI_ZCODE_BIN"] = str(_write_fake(clean, "zcode-cli"))
    if assignment == "default":
        env["CONDUCTOR_LINE_MODEL_ID"] = "default"
        env["CONDUCTOR_LINE_CLI_EFFORT"] = "default"
        env["CONDUCTOR_LINE_RUNTIME"] = "default"
    result = run(
        launch,
        "the task",
        "--session-id=sess-1",
        "--session-mode=resume",
        env=env or None,
    )
    assert result.returncode == 0, result.stderr
    assert _normalize(_argv(tmp_path)) == GOLDEN_RESUME[roster]


@pytest.mark.parametrize("roster", ["kimi", "grok", "glm", "minimax"])
def test_empty_and_default_values_match_main(run, tmp_path, roster):
    for value in ("", "default"):
        result = run(
            roster,
            "the task",
            env={
                "CONDUCTOR_LINE_MODEL_ID": value,
                "CONDUCTOR_LINE_CLI_EFFORT": value,
                "CONDUCTOR_LINE_RUNTIME": value,
            },
        )
        assert result.returncode == 0, result.stderr
        assert _normalize(_argv(tmp_path)) == GOLDEN[roster]


def test_native_cli_runtime_is_todays_behavior(run, tmp_path):
    result = run("kimi", "the task", env={"CONDUCTOR_LINE_RUNTIME": "native_cli"})
    assert result.returncode == 0, result.stderr
    assert _normalize(_argv(tmp_path)) == GOLDEN["kimi"]


# ------------------------------------------------------- one case per provider


@pytest.mark.parametrize(
    ("roster", "flag", "model"),
    [
        # Kimi's CLI accepts -m or --model; the launch script uses -m.
        ("kimi", "-m", "kimi-k2.5"),
        ("grok", "-m", "grok-4.1"),
        ("cursor", "--model", "gpt-5"),
        ("stepfun", "--model", "step-3.7-flash"),
        ("minimax", "--model", "minimax/minimax-m2"),
        ("doubao", "--model", "doubao-seed-2"),
    ],
)
def test_each_provider_adds_its_own_model_flag(run, tmp_path, roster, flag, model):
    result = run(roster, "the task", env={"CONDUCTOR_LINE_MODEL_ID": model})
    assert result.returncode == 0, result.stderr
    _assert_pair_added(_argv(tmp_path), GOLDEN[roster], flag, model)


def test_glm_clean_room_takes_the_line_model_before_the_prompt(run, tmp_path):
    result = run(
        "glm",
        "the task",
        env={
            "CONDUCTOR_LINE_RUNTIME": "clean-room",
            "CONDUCTOR_LINE_MODEL_ID": "GLM-5.3-Flash",
        },
    )
    assert result.returncode == 0, result.stderr
    argv = _argv(tmp_path)
    assert argv[0] == "--model"
    assert argv[1] == "GLM-5.3-Flash"
    assert argv[2] == "--prompt"
    assert argv[3] == "the task"


def test_glm_official_has_no_model_flag(run, tmp_path):
    result = run("glm", "the task", env={"CONDUCTOR_LINE_MODEL_ID": "GLM-5.3"})
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    assert "no model flag" in result.stderr
    _assert_no_argv(tmp_path)


def test_deepseek_takes_its_model_from_the_profile(run, tmp_path):
    result = run("deepseek", "the task", env={"CONDUCTOR_LINE_MODEL_ID": "deepseek-v4"})
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    assert "profile" in result.stderr
    _assert_no_argv(tmp_path)


@pytest.mark.parametrize(
    ("roster", "model"),
    [
        ("kimi", "definitely-not-a-model"),
        ("glm", "GLM-9.9"),
        ("grok", "grok-99"),
        ("cursor", "no-such-cursor-model"),
        ("stepfun", "step-99"),
        ("minimax", "MiniMax-99"),
        ("doubao", "doubao-99"),
    ],
)
def test_an_unknown_model_id_is_refused(run, tmp_path, roster, model):
    result = run(roster, "the task", env={"CONDUCTOR_LINE_MODEL_ID": model})
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    assert f"unknown {roster} model id" in result.stderr
    _assert_no_argv(tmp_path)


def test_a_model_id_that_could_read_as_a_flag_is_refused(run, tmp_path):
    result = run("kimi", "the task", env={"CONDUCTOR_LINE_MODEL_ID": "-rf"})
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    _assert_no_argv(tmp_path)


@pytest.mark.parametrize(
    "endpoint",
    ["ep-20260101-abcdef", "ep-", "ep-not-declared"],
)
def test_an_undeclared_doubao_endpoint_is_refused(run, tmp_path, endpoint):
    """An account endpoint is not a wildcard. Only the provider env can name it."""
    result = run("doubao", "the task", env={"CONDUCTOR_LINE_MODEL_ID": endpoint})
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    assert "unknown doubao model id" in result.stderr
    _assert_no_argv(tmp_path)


def test_a_declared_doubao_endpoint_is_accepted(run, tmp_path):
    endpoint = "ep-20260101-abcdef"
    result = run(
        "doubao",
        "the task",
        env={
            "NATIVE_CLI_DOUBAO_MODEL": endpoint,
            "CONDUCTOR_LINE_MODEL_ID": endpoint,
        },
    )
    assert result.returncode == 0, result.stderr
    _assert_pair_added(_argv(tmp_path), GOLDEN["doubao"], "--model", endpoint)


def test_the_line_model_wins_over_the_doubao_provider_override(run, tmp_path):
    result = run(
        "doubao",
        "the task",
        env={
            "NATIVE_CLI_DOUBAO_MODEL": "provider-default",
            "CONDUCTOR_LINE_MODEL_ID": "doubao-seed-2-1-pro",
        },
    )
    assert result.returncode == 0, result.stderr
    argv = _normalize(_argv(tmp_path))
    assert argv.count("--model") == 1
    _assert_pair_added(argv, GOLDEN["doubao"], "--model", "doubao-seed-2-1-pro")


def test_the_doubao_provider_override_still_applies_without_a_line_model(run, tmp_path):
    """Unset line model keeps main's override flag, in main's position."""
    result = run(
        "doubao",
        "the task",
        env={"NATIVE_CLI_DOUBAO_MODEL": "provider-default"},
    )
    assert result.returncode == 0, result.stderr
    _assert_pair_added(_argv(tmp_path), GOLDEN["doubao"], "--model", "provider-default")


def test_a_declared_provider_model_id_is_accepted(run, tmp_path):
    result = run(
        "doubao",
        "the task",
        env={
            "NATIVE_CLI_DOUBAO_MODEL": "my-doubao-model",
            "CONDUCTOR_LINE_MODEL_ID": "my-doubao-model",
        },
    )
    assert result.returncode == 0, result.stderr
    _assert_pair_added(_argv(tmp_path), GOLDEN["doubao"], "--model", "my-doubao-model")


def test_a_qualified_minimax_model_is_forwarded_unchanged(run, tmp_path):
    result = run(
        "minimax",
        "the task",
        env={"CONDUCTOR_LINE_MODEL_ID": "minimax/MiniMax-M3"},
    )
    assert result.returncode == 0, result.stderr
    _assert_pair_added(
        _argv(tmp_path),
        GOLDEN["minimax"],
        "--model",
        "minimax/MiniMax-M3",
    )


def test_a_bare_minimax_model_is_qualified(run, tmp_path):
    result = run("minimax", "the task", env={"CONDUCTOR_LINE_MODEL_ID": "MiniMax-M3"})
    assert result.returncode == 0, result.stderr
    _assert_pair_added(
        _argv(tmp_path), GOLDEN["minimax"], "--model", "minimax/MiniMax-M3"
    )


def test_a_configured_minimax_variant_launches(run, tmp_path):
    """mcode accepts provider/model#variant. A hash is not an unknown id."""
    model = "minimax/MiniMax-M3#xhigh"
    result = run(
        "minimax",
        "the task",
        env={
            "NATIVE_CLI_MINIMAX_MODEL": model,
            "CONDUCTOR_LINE_MODEL_ID": model,
        },
    )
    assert result.returncode == 0, result.stderr
    _assert_pair_added(_argv(tmp_path), GOLDEN["minimax"], "--model", model)


def test_a_catalog_minimax_model_keeps_one_variant(run, tmp_path):
    result = run(
        "minimax",
        "the task",
        env={"CONDUCTOR_LINE_MODEL_ID": "MiniMax-M3#x-high"},
    )
    assert result.returncode == 0, result.stderr
    _assert_pair_added(
        _argv(tmp_path),
        GOLDEN["minimax"],
        "--model",
        "minimax/MiniMax-M3#x-high",
    )


def test_a_minimax_mmx_base_keeps_one_variant(run, tmp_path):
    result = run(
        "minimax",
        "the task",
        env={
            "NATIVE_CLI_MINIMAX_MMX_MODEL": "custom/mmx",
            "CONDUCTOR_LINE_MODEL_ID": "custom/mmx#xhigh",
        },
    )
    assert result.returncode == 0, result.stderr
    _assert_pair_added(
        _argv(tmp_path), GOLDEN["minimax"], "--model", "custom/mmx#xhigh"
    )


def test_a_different_variant_of_an_unknown_minimax_base_is_refused(run, tmp_path):
    result = run(
        "minimax",
        "the task",
        env={
            "NATIVE_CLI_MINIMAX_MODEL": "custom/thing#xhigh",
            "CONDUCTOR_LINE_MODEL_ID": "custom/thing#low",
        },
    )
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    assert "unknown minimax model id" in result.stderr
    _assert_no_argv(tmp_path)


@pytest.mark.parametrize(
    "model",
    ["MiniMax-M3#", "#xhigh", "MiniMax-M3#xhigh#low", "MiniMax-M3#-rf"],
)
def test_a_malformed_minimax_variant_is_refused(run, tmp_path, model):
    result = run("minimax", "the task", env={"CONDUCTOR_LINE_MODEL_ID": model})
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    assert "unsupported model id" in result.stderr
    _assert_no_argv(tmp_path)


def test_a_hash_in_a_non_minimax_model_id_is_refused(run, tmp_path):
    result = run("kimi", "the task", env={"CONDUCTOR_LINE_MODEL_ID": "kimi-k2.5#xhigh"})
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    assert "unsupported model id" in result.stderr
    _assert_no_argv(tmp_path)


# ------------------------------------------------------------------- effort


def test_grok_default_effort_stays_high(run, tmp_path):
    result = run("grok", "the task")
    assert result.returncode == 0, result.stderr
    assert _argv(tmp_path)[_argv(tmp_path).index("--reasoning-effort") + 1] == "high"


@pytest.mark.parametrize(
    "effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
)
def test_grok_effort_replaces_the_fixed_high(run, tmp_path, effort):
    result = run("grok", "the task", env={"CONDUCTOR_LINE_CLI_EFFORT": effort})
    assert result.returncode == 0, result.stderr
    argv = _argv(tmp_path)
    index = argv.index("--reasoning-effort")
    assert argv[index + 1] == effort


@pytest.mark.parametrize("effort", ["low", "medium", "high", "max"])
def test_stepfun_effort_uses_thinking(run, tmp_path, effort):
    """PRD 4.2 maps stepfun low/medium/high/max onto ``--thinking``."""
    result = run("stepfun", "the task", env={"CONDUCTOR_LINE_CLI_EFFORT": effort})
    assert result.returncode == 0, result.stderr
    _assert_pair_added(_argv(tmp_path), GOLDEN["stepfun"], "--thinking", effort)


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high"])
def test_doubao_effort_uses_reasoning_effort(run, tmp_path, effort):
    """PRD 4.2 maps doubao low/medium/high/high; the CLI also accepts minimal."""
    result = run("doubao", "the task", env={"CONDUCTOR_LINE_CLI_EFFORT": effort})
    assert result.returncode == 0, result.stderr
    _assert_pair_added(_argv(tmp_path), GOLDEN["doubao"], "--reasoning-effort", effort)


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_cursor_effort_rides_in_the_model_string(run, tmp_path, effort):
    """cursor-agent has no effort flag; max maps to high and is not a token."""
    result = run(
        "cursor",
        "the task",
        env={
            "CONDUCTOR_LINE_MODEL_ID": "gpt-5",
            "CONDUCTOR_LINE_CLI_EFFORT": effort,
        },
    )
    assert result.returncode == 0, result.stderr
    _assert_pair_added(
        _argv(tmp_path),
        GOLDEN["cursor"],
        "--model",
        f"gpt-5[effort={effort}]",
    )


def test_cursor_accepts_an_already_suffixed_model_id(run, tmp_path):
    """The dispatcher sends cursor's effort inside the model id (N73 L1)."""
    result = run(
        "cursor",
        "the task",
        env={"CONDUCTOR_LINE_MODEL_ID": "gpt-5[effort=high]"},
    )
    assert result.returncode == 0, result.stderr
    _assert_pair_added(
        _argv(tmp_path), GOLDEN["cursor"], "--model", "gpt-5[effort=high]"
    )


def test_cursor_rejects_an_unknown_model_suffix_before_the_binary_lookup(run, tmp_path):
    """The suffix is checked like the variable; no CLI on PATH gives 66, not 127."""
    result = run(
        "cursor",
        "the task",
        env={
            "CONDUCTOR_LINE_MODEL_ID": "gpt-5[effort=bogus]",
            "NATIVE_CLI_CURSOR_BIN": str(tmp_path / "nonexistent-cursor"),
        },
    )
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    assert "unsupported cursor model suffix" in result.stderr
    _assert_no_argv(tmp_path)


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-5[effort=bogus]",
        "gpt-5[effort=]",
        "gpt-5[effort=high",
        "gpt-5[effort=high]junk",
        "gpt-5[reasoning=high]",
        "gpt-5[effort=high][effort=low]",
        # A bare or unbalanced bracket is not a valid suffix either; it must
        # not reach an installed CLI (the "gpt-5[" regression).
        "gpt-5[",
        "gpt-5]",
        "gpt-5[]",
        "gpt-5[[effort=high]",
        "gpt-5][effort=high]",
        "gpt-5[effort=high]]",
        "gpt-5[effort=high][",
    ],
)
def test_cursor_rejects_a_malformed_or_unknown_model_suffix(run, tmp_path, model_id):
    result = run("cursor", "the task", env={"CONDUCTOR_LINE_MODEL_ID": model_id})
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    assert "unsupported cursor model suffix" in result.stderr
    _assert_no_argv(tmp_path)


def test_cursor_malformed_bracket_is_refused_before_the_binary_lookup(run, tmp_path):
    """The "gpt-5[" regression: no installed CLI may receive it (66, not 127)."""
    result = run(
        "cursor",
        "the task",
        env={
            "CONDUCTOR_LINE_MODEL_ID": "gpt-5[",
            "CONDUCTOR_LINE_CLI_EFFORT": "",
            "CONDUCTOR_LINE_RUNTIME": "",
            "NATIVE_CLI_CURSOR_BIN": str(tmp_path / "nonexistent-cursor"),
        },
    )
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    assert "unsupported cursor model suffix" in result.stderr
    _assert_no_argv(tmp_path)


@pytest.mark.parametrize(
    ("model_id", "effort"),
    [
        ("gpt-5[effort=low]", "high"),
        ("gpt-5[effort=high]", "low"),
        ("gpt-5[effort=medium]", "high"),
    ],
)
def test_cursor_rejects_a_suffix_that_conflicts_with_the_assigned_effort(
    run, tmp_path, model_id, effort
):
    """Neither value may silently win: the assignment would be a different one."""
    result = run(
        "cursor",
        "the task",
        env={
            "CONDUCTOR_LINE_MODEL_ID": model_id,
            "CONDUCTOR_LINE_CLI_EFFORT": effort,
        },
    )
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    assert "conflicting cursor effort" in result.stderr
    _assert_no_argv(tmp_path)


def test_cursor_matching_suffix_and_effort_are_not_applied_twice(run, tmp_path):
    result = run(
        "cursor",
        "the task",
        env={
            "CONDUCTOR_LINE_MODEL_ID": "gpt-5[effort=high]",
            "CONDUCTOR_LINE_CLI_EFFORT": "high",
        },
    )
    assert result.returncode == 0, result.stderr
    argv = _argv(tmp_path)
    _assert_pair_added(argv, GOLDEN["cursor"], "--model", "gpt-5[effort=high]")
    assert argv.count("--model") == 1
    assert argv.count("gpt-5[effort=high]") == 1


def test_cursor_effort_without_a_model_id_is_refused(run, tmp_path):
    result = run("cursor", "the task", env={"CONDUCTOR_LINE_CLI_EFFORT": "high"})
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    _assert_no_argv(tmp_path)


@pytest.mark.parametrize(
    ("roster", "effort"),
    [
        # PRD 4.2: these providers have no effort flag at all.
        ("kimi", "high"),
        ("glm", "high"),
        ("deepseek", "high"),
        ("minimax", "high"),
        # A value the provider's parser does not accept.
        ("grok", "ultra"),
        ("stepfun", "none"),
        ("doubao", "xhigh"),
        ("cursor", "max"),
    ],
)
def test_an_unexpressible_effort_is_refused(run, tmp_path, roster, effort):
    result = run(roster, "the task", env={"CONDUCTOR_LINE_CLI_EFFORT": effort})
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    _assert_no_argv(tmp_path)


# ------------------------------------------------------------------ runtime


def test_glm_clean_room_runtime_selects_the_clean_room_binary(run, tmp_path):
    result = run("glm", "the task", env={"CONDUCTOR_LINE_RUNTIME": "clean-room"})
    assert result.returncode == 0, result.stderr
    assert _normalize(_argv(tmp_path)) == GOLDEN["glm-clean-room"]


def test_glm_official_runtime_selects_the_official_binary(run, tmp_path):
    result = run("glm", "the task", env={"CONDUCTOR_LINE_RUNTIME": "official"})
    assert result.returncode == 0, result.stderr
    assert _normalize(_argv(tmp_path)) == GOLDEN["glm"]


def test_glm_clean_room_runtime_takes_a_matching_override(run, tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()
    clean_room = _write_fake(clean, "zcode-cli")
    result = run(
        "glm",
        "the task",
        env={
            "NATIVE_CLI_ZCODE_BIN": str(clean_room),
            "CONDUCTOR_LINE_RUNTIME": "native_cli:glm-clean-room",
        },
    )
    assert result.returncode == 0, result.stderr
    assert _normalize(_argv(tmp_path)) == GOLDEN["glm-clean-room"]


@pytest.mark.parametrize(
    "runtime",
    ["clean-room", "native_cli:clean-room", "native_cli:glm-clean-room"],
)
def test_glm_clean_room_spellings_behave_the_same(run, tmp_path, runtime):
    result = run("glm", "the task", env={"CONDUCTOR_LINE_RUNTIME": runtime})
    assert result.returncode == 0, result.stderr
    assert _normalize(_argv(tmp_path)) == GOLDEN["glm-clean-room"]


@pytest.mark.parametrize(
    "runtime",
    ["official", "native_cli:official", "native_cli:glm-official"],
)
def test_glm_official_spellings_behave_the_same(run, tmp_path, runtime):
    result = run("glm", "the task", env={"CONDUCTOR_LINE_RUNTIME": runtime})
    assert result.returncode == 0, result.stderr
    assert _normalize(_argv(tmp_path)) == GOLDEN["glm"]


@pytest.mark.parametrize(
    ("runtime", "binary_name"),
    [("clean-room", "zcode"), ("official", "zcode-cli")],
)
def test_a_glm_runtime_must_match_glm_grammar(run, tmp_path, runtime, binary_name):
    """The variant and the selected binary must agree, both directions."""
    other = tmp_path / "other"
    other.mkdir()
    selected = _write_fake(other, binary_name)
    result = run(
        "glm",
        "the task",
        env={
            "NATIVE_CLI_ZCODE_BIN": str(selected),
            "CONDUCTOR_LINE_RUNTIME": runtime,
        },
    )
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    assert "GLM binary" in result.stderr
    _assert_no_argv(tmp_path)


@pytest.mark.parametrize(
    ("runtime", "message"),
    [
        ("native_cli:docker", "unsupported runtime"),
        ("docker", "unsupported runtime"),
        ("clean-room", "only supported for glm"),
        ("official", "only supported for glm"),
        ("login", "only supported for minimax"),
        ("api-key", "only supported for minimax"),
    ],
)
def test_a_runtime_variant_for_another_provider_is_refused(
    run, tmp_path, runtime, message
):
    result = run("kimi", "the task", env={"CONDUCTOR_LINE_RUNTIME": runtime})
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    assert message in result.stderr
    _assert_no_argv(tmp_path)


@pytest.mark.parametrize(
    "runtime",
    ["login", "native_cli:login", "native_cli:minimax-login"],
)
def test_minimax_login_runtime_leaves_the_environment_alone(run, tmp_path, runtime):
    result = run("minimax", "the task", env={"CONDUCTOR_LINE_RUNTIME": runtime})
    assert result.returncode == 0, result.stderr
    assert _normalize(_argv(tmp_path)) == GOLDEN["minimax"]
    assert (tmp_path / "minimax-dir.txt").read_text() == "<unset>"
    assert (tmp_path / "minimax-key.txt").read_text() == "<unset>"


def test_minimax_login_runtime_can_name_a_login_profile(run, tmp_path):
    login_dir = tmp_path / "login-profile"
    result = run(
        "minimax",
        "the task",
        env={
            "CONDUCTOR_LINE_RUNTIME": "login",
            "NATIVE_CLI_MINIMAX_LOGIN_DATA_DIR": str(login_dir),
        },
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "minimax-dir.txt").read_text() == str(login_dir)


@pytest.mark.parametrize(
    "runtime",
    ["api-key", "native_cli:api-key", "native_cli:minimax-api-key"],
)
def test_minimax_api_key_runtime_selects_its_own_profile(run, tmp_path, runtime):
    """The API-key runtime selects a data dir; no credential is read or set."""
    _provision_minimax_key_profile(tmp_path / ".cache" / "conductor" / "minimax")
    result = run(
        "minimax",
        "the task",
        env={
            "CONDUCTOR_LINE_RUNTIME": runtime,
            "MINIMAX_API_KEY": "sk-" "test-line",
        },
    )
    assert result.returncode == 0, result.stderr
    data_dir = Path((tmp_path / "minimax-dir.txt").read_text())
    # The adapter's API-key profile (src/llms/native_cli/minimax.py).
    assert data_dir == tmp_path / ".cache" / "conductor" / "minimax"
    # A caller-supplied key is not copied into mcode's credential variable
    # and never appears on the command line.
    assert (tmp_path / "minimax-key.txt").read_text() == "<unset>"
    assert "sk-" "test-line" not in _argv(tmp_path)
    assert "MINIMAX_API_KEY" not in _argv(tmp_path)
    assert _normalize(_argv(tmp_path)) == GOLDEN["minimax"]


def _provision_minimax_key_profile(data_dir: Path) -> None:
    """What the adapter's setup leaves behind: the ready marker."""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / ".alpha-provider-ready").write_text("", encoding="utf-8")


def test_minimax_api_key_runtime_can_name_its_data_dir(run, tmp_path):
    data_dir = tmp_path / "key-profile"
    _provision_minimax_key_profile(data_dir)
    result = run(
        "minimax",
        "the task",
        env={
            "CONDUCTOR_LINE_RUNTIME": "native_cli:minimax-api-key",
            "NATIVE_CLI_MINIMAX_DATA_DIR": str(data_dir),
        },
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "minimax-dir.txt").read_text() == str(data_dir)
    assert (tmp_path / "minimax-key.txt").read_text() == "<unset>"
    assert _normalize(_argv(tmp_path)) == GOLDEN["minimax"]


def test_an_unprovisioned_minimax_api_key_profile_is_refused(run, tmp_path):
    """No ready marker: refuse before a Worker starts, create nothing."""
    data_dir = tmp_path / "key-profile"
    result = run(
        "minimax",
        "the task",
        env={
            "CONDUCTOR_LINE_RUNTIME": "api-key",
            "NATIVE_CLI_MINIMAX_DATA_DIR": str(data_dir),
        },
    )
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    assert "not provisioned" in result.stderr
    assert not data_dir.exists()
    _assert_no_argv(tmp_path)


def test_native_cli_runtime_leaves_a_clean_room_glm_binary_alone(run, tmp_path):
    """``native_cli`` adds no argument; the selected binary still picks the grammar."""
    clean = tmp_path / "clean"
    clean.mkdir()
    clean_room = _write_fake(clean, "zcode-cli")
    result = run(
        "glm",
        "the task",
        env={
            "NATIVE_CLI_ZCODE_BIN": str(clean_room),
            "CONDUCTOR_LINE_RUNTIME": "native_cli",
        },
    )
    assert result.returncode == 0, result.stderr
    assert _normalize(_argv(tmp_path)) == GOLDEN["glm-clean-room"]


# ------------------------------------------------- flags and session branches


@pytest.mark.parametrize("session", [False, True])
@pytest.mark.parametrize(
    ("roster", "env", "new_flags", "marker"),
    [
        (
            "kimi",
            {"CONDUCTOR_LINE_MODEL_ID": "kimi-k2.5"},
            {"-m": "kimi-k2.5"},
            "-p",
        ),
        (
            "grok",
            {"CONDUCTOR_LINE_MODEL_ID": "grok-4.1"},
            {"-m": "grok-4.1"},
            "--prompt-file",
        ),
        (
            "cursor",
            {
                "CONDUCTOR_LINE_MODEL_ID": "gpt-5",
                "CONDUCTOR_LINE_CLI_EFFORT": "high",
            },
            {"--model": "gpt-5[effort=high]"},
            "<PROMPT>",
        ),
        (
            "stepfun",
            {
                "CONDUCTOR_LINE_MODEL_ID": "step-3.7-flash",
                "CONDUCTOR_LINE_CLI_EFFORT": "high",
            },
            {"--model": "step-3.7-flash", "--thinking": "high"},
            "<PROMPT>",
        ),
        (
            "minimax",
            {"CONDUCTOR_LINE_MODEL_ID": "MiniMax-M3"},
            {"--model": "minimax/MiniMax-M3"},
            "--input",
        ),
        (
            "doubao",
            {
                "CONDUCTOR_LINE_MODEL_ID": "doubao-seed-2",
                "CONDUCTOR_LINE_CLI_EFFORT": "medium",
            },
            {"--model": "doubao-seed-2", "--reasoning-effort": "medium"},
            "--",
        ),
    ],
)
def test_new_flags_precede_the_prompt_or_stdin_marker(
    run, tmp_path, session, roster, env, new_flags, marker
):
    args = [roster, "the task"]
    if session:
        args += ["--session-id=sess-1", "--session-mode=resume"]
    result = run(*args, env=env)
    assert result.returncode == 0, result.stderr
    argv = _normalize(_argv(tmp_path))
    marker_index = argv.index(marker)
    for flag, value in new_flags.items():
        assert flag in argv, (flag, argv)
        index = argv.index(flag)
        assert argv[index + 1] == value, (flag, value, argv)
        assert index < marker_index, (flag, marker, argv)


def test_glm_model_precedes_the_prompt_in_a_session(run, tmp_path):
    result = run(
        "glm",
        "the task",
        "--session-id=sess-1",
        "--session-mode=resume",
        env={
            "CONDUCTOR_LINE_RUNTIME": "clean-room",
            "CONDUCTOR_LINE_MODEL_ID": "GLM-5.3-Flash",
        },
    )
    assert result.returncode == 0, result.stderr
    argv = _normalize(_argv(tmp_path))
    assert argv.index("--model") < argv.index("--prompt")
    assert "--resume" in argv


# -------------------------------------------------------------- reserved roles


@pytest.mark.parametrize(
    "roster", ["claude", "codex", "claude-code", "codexrev", "claudedev"]
)
def test_reserved_roles_still_exit_65(run, tmp_path, roster):
    result = run(
        roster,
        "the task",
        env={
            "CONDUCTOR_LINE_MODEL_ID": "claude-sonnet",
            "CONDUCTOR_LINE_CLI_EFFORT": "high",
            "CONDUCTOR_LINE_RUNTIME": "native_cli",
        },
    )
    assert result.returncode == EXIT_RESERVED, result.stderr
    _assert_no_argv(tmp_path)


# ------------------------------------------------- the check runs before PATH


def test_a_bad_assignment_is_refused_before_the_binary_lookup(run, tmp_path):
    """No CLI on PATH: the assignment error wins over exit 127."""
    result = run(
        "kimi",
        "the task",
        env={"CONDUCTOR_LINE_MODEL_ID": "not-a-model"},
        binaries=False,
    )
    assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
    assert "unknown kimi model id" in result.stderr
    _assert_no_argv(tmp_path)


# ----------------------------------------------------------- documentation


def test_the_exit_code_and_probed_values_are_documented():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "EXIT_BAD_ASSIGNMENT=66" in text
    assert "66  the launch was refused" in text

    comments = " ".join(
        line.lstrip("#").strip()
        for line in text.splitlines()
        if line.lstrip().startswith("#")
    )
    # The PRD asks the launcher line to probe and record these two.
    assert "grok 1.0.77" in comments
    assert "none, minimal, low, medium, high, xhigh, max" in comments
    assert "codex 0.167.2" in comments
    assert "low, medium, high, xhigh, max, ultra" in comments


# ------------------------------------------------- L1 contract (main's tables)
#
# The launcher only accepts tokens that ``superagent_assign`` already mapped
# (PRD 4.2: "启动脚本只认已经映射好的 CLI 取值"). These tests feed it what the
# merged L1 package actually produces, so a drift between the effort table,
# the runtime set and this script fails here instead of as exit 66 at launch.

# A model id each provider's catalog in the launcher accepts. cursor needs a
# concrete id to carry an effort (effort.apply_model_id refuses ``default``).
_CONTRACT_MODEL_ID = {"cursor": "gpt-5"}


def _l1_launch_env(provider: str, level: str) -> dict[str, str]:
    from src.tools.auto_dispatch.superagent_assign import effort as l1_effort

    resolved = l1_effort.resolve_effort(provider, level)
    assert resolved.supported, (provider, level)
    model = l1_effort.apply_model_id(
        _CONTRACT_MODEL_ID.get(provider, "default"), resolved
    )
    return {
        "CONDUCTOR_LINE_MODEL_ID": model or "",
        "CONDUCTOR_LINE_CLI_EFFORT": resolved.launch_token or "",
    }


@pytest.mark.parametrize("roster", WORKERS)
@pytest.mark.parametrize("level", ("default", "low", "medium", "high", "max"))
def test_every_l1_effort_resolution_launches(run, tmp_path, roster, level):
    """Each (provider, unified level) L1 resolves is accepted by the launcher."""
    env = _l1_launch_env(roster, level)
    result = run(roster, "the task", env=env)
    assert result.returncode == 0, (env, result.stderr)
    argv = _argv(tmp_path)
    if not env["CONDUCTOR_LINE_MODEL_ID"] and not env["CONDUCTOR_LINE_CLI_EFFORT"]:
        # n/a providers and ``default``: today's argv, byte for byte.
        assert _normalize(argv) == GOLDEN[roster]


def test_l1_grok_default_is_todays_fixed_high(run, tmp_path):
    """L1 says grok ``default`` == ``high``; both launch today's argv."""
    for level in ("default", "high"):
        result = run("grok", "the task", env=_l1_launch_env("grok", level))
        assert result.returncode == 0, result.stderr
        assert _normalize(_argv(tmp_path)) == GOLDEN["grok"]


@pytest.mark.parametrize("roster", WORKERS)
def test_every_l1_runtime_is_accepted(run, tmp_path, roster):
    """``feasibility.launcher_runtimes`` names only spellings the script takes."""
    from src.tools.auto_dispatch.superagent_assign import feasibility

    # api-key needs the adapter's provisioned profile (refused with 66 else).
    _provision_minimax_key_profile(tmp_path / ".cache" / "conductor" / "minimax")
    for runtime in sorted(feasibility.launcher_runtimes(roster)):
        result = run(roster, "the task", env={"CONDUCTOR_LINE_RUNTIME": runtime})
        assert result.returncode == 0, (runtime, result.stderr)


def test_a_runtime_l1_does_not_name_is_refused(run, tmp_path):
    from src.tools.auto_dispatch.superagent_assign import feasibility

    for runtime in ("docker", "cloud"):
        assert runtime not in feasibility.launcher_runtimes("kimi")
        result = run("kimi", "the task", env={"CONDUCTOR_LINE_RUNTIME": runtime})
        assert result.returncode == EXIT_BAD_ASSIGNMENT, result.stderr
        _assert_no_argv(tmp_path)


@pytest.mark.parametrize("roster", WORKERS)
def test_truly_unset_assignment_matches_main(run, tmp_path, roster):
    """The variables absent from the environment, not merely empty."""
    environment = _environment(tmp_path / "bin", tmp_path)
    for name in (
        "CONDUCTOR_LINE_MODEL_ID",
        "CONDUCTOR_LINE_CLI_EFFORT",
        "CONDUCTOR_LINE_RUNTIME",
    ):
        environment.pop(name)
    result = subprocess.run(
        ["bash", str(SCRIPT), roster, "the task"],
        cwd=str(tmp_path),
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert _normalize(_argv(tmp_path)) == GOLDEN[roster]
    # The fake CLI records MINIMAX_DATA_DIR on every run: no launch sets it.
    assert (tmp_path / "minimax-dir.txt").read_text() == "<unset>"


def test_a_missing_glm_binary_resolves_no_grammar_first(run, tmp_path):
    """Unassigned glm with no binary: exit 127 before the grammar probe."""
    result = run("glm", "the task", binaries=False)
    assert result.returncode == 127, result.stderr
    assert not (tmp_path / "Library").exists()
    assert not (tmp_path / "logs").exists()
