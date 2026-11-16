"""N29: ``dispatch_launch.sh --context-budget`` inlines the budget block and
hands the Worker the context-budget environment.

The script is driven with fake CLIs on PATH (as in ``test_dispatch_launch.py``)
that record their argv, stdin and whole environment. No real model runs.
Without ``--context-budget`` the prompt and the environment must be exactly
what they were before N29.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "dispatch_launch.sh"

sys.path.insert(0, str(ROOT))
from src.tools.auto_dispatch import context_budget as cb  # noqa: E402

CONTEXT_VARS = (
    cb.ENV_BUDGET_FILE,
    cb.ENV_HANDOFF_REQUEST_FILE,
    cb.ENV_HISTORY_MANIFEST,
    cb.ENV_RUN_LOG,
    cb.ENV_HISTORY_CMD,
)
# Frozen names (fix round); context_budget.py may carry them as constants too.
ENV_PYTHON = "CONDUCTOR_HISTORY_PYTHON"
ENV_SCRIPT = "CONDUCTOR_HISTORY_SCRIPT"
ENV_RUNS_ROOT = "CONDUCTOR_HISTORY_RUNS_ROOT"
NEW_VARS = (ENV_PYTHON, ENV_SCRIPT, ENV_RUNS_ROOT)
ALL_VARS = CONTEXT_VARS + NEW_VARS
FORBIDDEN_PREFIXES = ("CONDUCTOR_CONTEXT_", "CONDUCTOR_HANDOFF_REQUEST", "CONDUCTOR_HISTORY_")

FAKE_CLI = """#!/bin/bash
STDIN_COPY="$(mktemp "${TMPDIR:-/tmp}/fake-stdin.XXXXXX")"
cat > "$STDIN_COPY"
python3 - "$FAKE_RECORD" "$STDIN_COPY" "$@" <<'PYEOF'
import json, os, sys
rec, stdin_path = sys.argv[1:3]
with open(stdin_path) as fh:
    stdin = fh.read()
os.unlink(stdin_path)
with open(rec, "w") as fh:
    json.dump({"argv": sys.argv[3:], "stdin": stdin, "env": dict(os.environ)}, fh)
PYEOF
exit 0
"""

# roster -> (binary, how to find the prompt in the recorded argv)
PROVIDERS = {"glm": ("zcode", "--prompt"), "kimi": ("kimi", "-p")}


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for binary, _ in PROVIDERS.values():
        path = bin_dir / binary
        path.write_text(FAKE_CLI, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return bin_dir


@pytest.fixture
def run(tmp_path: Path, fake_bin: Path):
    def _run(model: str, *args: str, record_name: str = "record.json") -> dict:
        record = tmp_path / record_name
        environment = {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(tmp_path / "logs"),
            "FAKE_RECORD": str(record),
        }
        result = subprocess.run(
            ["bash", str(SCRIPT), model, "the task", *args],
            cwd=str(tmp_path),
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(record.read_text(encoding="utf-8"))
        flag = PROVIDERS[model][1]
        data["prompt"] = data["argv"][data["argv"].index(flag) + 1]
        data["env"].pop("FAKE_RECORD", None)
        return data

    return _run


def _paths(tmp_path: Path, runs: str = "runs") -> cb.RunPaths:
    runs_dir = (tmp_path / runs).resolve()
    return cb.prepare_run_paths(runs_dir, "run-1", "dispatch-7:line-1")


def _block() -> str:
    est = cb.ContextEstimate(
        provider="glm",
        window_tokens=190_000,
        window_assumed=False,
        used_tokens=1_000,
        left_tokens=189_000,
        ratio=1_000 / 190_000,
        source="log_bytes",
        drives_rotation=True,
    )
    lineage = type("L", (), {"first": "ab" * 16, "previous": None, "current": "cd" * 16, "generation": 1})()
    return cb.render_budget_block(est, lineage, soft_ratio=0.7)


def _options(paths: cb.RunPaths) -> list[str]:
    return [
        f"{cb.ARG_CONTEXT_BUDGET}={paths.budget_file}",
        f"{cb.ARG_HANDOFF_REQUEST}={paths.request_file}",
        f"{cb.ARG_HISTORY_MANIFEST}={paths.manifest}",
        f"{cb.ARG_RUN_LOG}={paths.run_log}",
    ]


def test_the_budget_block_is_inlined_into_the_glm_prompt(run, tmp_path) -> None:
    paths = _paths(tmp_path)
    cb.write_budget_file(paths.budget_file, _block())
    record = run("glm", *_options(paths))
    assert "<conductor_context_budget>" in record["prompt"]
    assert "</conductor_context_budget>" in record["prompt"]
    assert record["prompt"].startswith("the task\n")


def test_the_block_comes_right_after_the_handoff_note(run, tmp_path) -> None:
    paths = _paths(tmp_path)
    cb.write_budget_file(paths.budget_file, _block())
    note = tmp_path / "note.md"
    note.write_text("HANDOFF NOTE BODY\n", encoding="utf-8")
    record = run("glm", f"--handoff={note}", *_options(paths))
    prompt = record["prompt"]
    assert prompt.index("HANDOFF NOTE BODY") < prompt.index("<conductor_context_budget>")


def test_a_symlinked_budget_file_is_not_inlined_and_the_launch_goes_on(run, tmp_path) -> None:
    paths = _paths(tmp_path)
    real = tmp_path / "real-budget.txt"
    real.write_text(_block(), encoding="utf-8")
    paths.budget_file.symlink_to(real)
    record = run("glm", *_options(paths))
    assert "conductor_context_budget" not in record["prompt"]


def test_a_missing_budget_file_inlines_nothing_and_the_launch_goes_on(run, tmp_path) -> None:
    paths = _paths(tmp_path)
    record = run("glm", *_options(paths))
    assert "conductor_context_budget" not in record["prompt"]
    assert record["env"][cb.ENV_BUDGET_FILE] == str(paths.budget_file)


def test_only_the_first_2048_bytes_are_inlined(run, tmp_path) -> None:
    paths = _paths(tmp_path)
    body = "A" * 2048 + "TAILMARKER" * 50
    paths.budget_file.write_text(body, encoding="utf-8")
    record = run("glm", *_options(paths))
    assert "A" * 2048 in record["prompt"]
    assert "A" * 2049 not in record["prompt"]
    assert "TAILMARKER" not in record["prompt"]


def test_the_variables_reach_the_worker(run, tmp_path) -> None:
    paths = _paths(tmp_path)
    cb.write_budget_file(paths.budget_file, _block())
    env = run("glm", *_options(paths))["env"]
    assert env[cb.ENV_BUDGET_FILE] == str(paths.budget_file)
    assert env[cb.ENV_HANDOFF_REQUEST_FILE] == str(paths.request_file)
    assert env[cb.ENV_HISTORY_MANIFEST] == str(paths.manifest)
    assert env[cb.ENV_RUN_LOG] == str(paths.run_log)
    assert env[ENV_RUNS_ROOT] == str(cb.runs_dir_for_manifest(paths.manifest))
    interpreter, script = env[ENV_PYTHON], env[ENV_SCRIPT]
    assert Path(interpreter).is_absolute()
    assert os.access(interpreter, os.X_OK)
    assert Path(script).is_absolute()
    assert Path(script) == ROOT / "scripts" / "worker_history.py"
    assert Path(script).is_file()
    expected_python = ROOT / ".venv" / "bin" / "python"
    if os.access(expected_python, os.X_OK):
        assert interpreter == str(expected_python)
    # The compatibility command names the same two words.
    assert shlex.split(env[cb.ENV_HISTORY_CMD]) == [interpreter, script]


def test_without_a_venv_the_interpreter_is_still_absolute(tmp_path, fake_bin) -> None:
    """A copy of the launcher with no .venv beside it falls back to python3 on PATH."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "dispatch_launch.sh").write_bytes(SCRIPT.read_bytes())
    paths = _paths(tmp_path)
    record = tmp_path / "record.json"
    result = subprocess.run(
        ["bash", str(repo / "scripts" / "dispatch_launch.sh"), "glm", "the task", *_options(paths)],
        cwd=str(tmp_path),
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(tmp_path / "logs"),
            "FAKE_RECORD": str(record),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    env = json.loads(record.read_text(encoding="utf-8"))["env"]
    assert Path(env[ENV_PYTHON]).is_absolute()
    assert os.access(env[ENV_PYTHON], os.X_OK)
    assert Path(env[ENV_PYTHON]).name.startswith("python3")


def _helper(env: dict, tmp_path: Path, argv: list[str], **changes: str):
    child_env = {k: env[k] for k in ALL_VARS}
    child_env.update(PATH="/usr/bin:/bin", HOME=str(tmp_path), **changes)
    return subprocess.run(
        argv,
        cwd=str(tmp_path),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("runs", ["runs", "runs with space/and more"])
def test_the_history_command_reads_the_budget(run, tmp_path, runs) -> None:
    paths = _paths(tmp_path, runs)
    cb.write_budget_file(paths.budget_file, _block())
    paths.run_log.write_bytes(b"x" * 400)
    env = run("glm", *_options(paths))["env"]
    # Exactly the form the budget block tells the Worker to run.
    result = _helper(
        env, tmp_path,
        ["bash", "-c", '"$CONDUCTOR_HISTORY_PYTHON" "$CONDUCTOR_HISTORY_SCRIPT" budget'],
    )
    assert result.returncode == 0, result.stderr
    assert "tokens_left: 188900" in result.stdout
    assert "run_log_bytes: 400" in result.stdout


def test_the_helper_refuses_another_lines_manifest(run, tmp_path) -> None:
    paths = _paths(tmp_path)
    other = _paths(tmp_path, "other runs")
    cb.write_budget_file(paths.budget_file, _block())
    env = run("glm", *_options(paths))["env"]
    result = _helper(
        env, tmp_path, [env[ENV_PYTHON], env[ENV_SCRIPT], "budget"],
        **{cb.ENV_HISTORY_MANIFEST: str(other.manifest)},
    )
    assert result.returncode != 0
    assert result.stderr.strip() == "error: manifest_outside_runs_root"


def test_only_the_given_options_are_exported(run, tmp_path) -> None:
    paths = _paths(tmp_path)
    cb.write_budget_file(paths.budget_file, _block())
    env = run("glm", f"{cb.ARG_CONTEXT_BUDGET}={paths.budget_file}")["env"]
    assert cb.ENV_BUDGET_FILE in env
    assert cb.ENV_HISTORY_CMD in env
    assert ENV_PYTHON in env
    assert ENV_SCRIPT in env
    for name in (cb.ENV_HANDOFF_REQUEST_FILE, cb.ENV_HISTORY_MANIFEST, cb.ENV_RUN_LOG, ENV_RUNS_ROOT):
        assert name not in env


@pytest.mark.parametrize("model", ["glm", "kimi"])
def test_without_context_budget_nothing_changes(run, tmp_path, model) -> None:
    paths = _paths(tmp_path)
    cb.write_budget_file(paths.budget_file, _block())
    baseline = run(model, record_name="baseline.json")
    # The other three options alone are inert: only --context-budget turns N29 on.
    others = run(model, *_options(paths)[1:], record_name="others.json")
    assert baseline["prompt"] == "the task"
    for record in (baseline, others):
        assert not [k for k in record["env"] if k.startswith(FORBIDDEN_PREFIXES)]
    assert others["argv"] == baseline["argv"]
    assert others["stdin"] == baseline["stdin"]
    assert others["env"] == baseline["env"]


def test_kimi_gets_the_block_and_the_environment_too(run, tmp_path) -> None:
    paths = _paths(tmp_path)
    cb.write_budget_file(paths.budget_file, _block())
    record = run("kimi", *_options(paths))
    assert "<conductor_context_budget>" in record["prompt"]
    for name in ALL_VARS:
        assert name in record["env"]
    assert record["argv"][-2:] == ["--output-format", "stream-json"]
