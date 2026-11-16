"""P0 privacy: a WORK Worker must not fold the operator's personal memory into
a prompt sent to another vendor.

The launcher runs Workers under the operator's real HOME. Where a CLI
documents a switch that turns off only its personal memory / global
instruction loading (and leaves the login where it is), dispatch_launch.sh
must set it. The fake CLIs below load the same files the real ones do, unless
the documented switch says not to, and print what they loaded. The HOME is a
tmp_path seeded with synthetic personal files.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "dispatch_launch.sh"

PERSONAL = "PERSONAL-MEMORY-ANT0000"


def _seed_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    files = {
        ".grok/memory-v2/global/MEMORY.md": f"{PERSONAL} grok memory\n",
        ".claude/CLAUDE.md": f"{PERSONAL} claude global\n",
        ".claude/rules/personal.md": f"{PERSONAL} claude rule\n",
        ".cursor/rules/personal.mdc": f"{PERSONAL} cursor rule\n",
        ".agents/AGENTS.md": f"{PERSONAL} shared agents\n",
        ".agents/skills/personal/SKILL.md": f"{PERSONAL} shared skill\n",
    }
    for rel, text in files.items():
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return home


def _fake(tmp_path: Path, name: str, body: str) -> Path:
    binary = tmp_path / "bin" / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    binary.chmod(0o755)
    return binary


# Grok's documented behaviour: GROK_MEMORY=0 force-disables memory; each
# GROK_<VENDOR>_<CELL>_ENABLED false turns that compat scan off (default on).
FAKE_GROK = r"""
env | grep -E '^(GROK_|HOME=)' | sort > "$FAKE_ENV_OUT"
off() { case "${1:-}" in 0|false|off|no) return 0 ;; esac; return 1; }
off "${GROK_MEMORY:-}" || cat "$HOME/.grok/memory-v2/global/MEMORY.md"
off "${GROK_CLAUDE_AGENTS_ENABLED:-}" || cat "$HOME/.claude/CLAUDE.md"
off "${GROK_CLAUDE_RULES_ENABLED:-}" || cat "$HOME"/.claude/rules/*.md
off "${GROK_CURSOR_RULES_ENABLED:-}" || cat "$HOME"/.cursor/rules/*.mdc
echo grok-done
"""

# dsh-agent-instructions: the user-global chain is $DSH_HOME/AGENTS.md, then
# $DSH_AGENTS_HOME/AGENTS.md (default ~/.agents).
FAKE_DSH = r"""
env | grep -E '^(DSH_|HOME=)' | sort > "$FAKE_ENV_OUT"
agents_home="${DSH_AGENTS_HOME:-$HOME/.agents}"
[ -f "$agents_home/AGENTS.md" ] && cat "$agents_home/AGENTS.md"
ls -A "$agents_home" > "$FAKE_ENV_OUT.agents-home"
cat > /dev/null
echo dsh-done
"""

FAKE_KIMI = r"""
env | grep -E '^(GROK_|DSH_)' | sort > "$FAKE_ENV_OUT"
echo kimi-done
"""


def _run(tmp_path: Path, provider: str, bin_var: str, binary: Path):
    home = _seed_home(tmp_path)
    env_out = tmp_path / "env.out"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GROK_", "DSH_", "NATIVE_CLI_", "CONDUCTOR_"))
    }
    env.update(
        {
            "HOME": str(home),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(tmp_path / "log"),
            "CONDUCTOR_DISPATCH_SERIAL_PROVIDERS": "",
            bin_var: str(binary),
            "FAKE_ENV_OUT": str(env_out),
            "LANG": "en_US.UTF-8",
        }
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    result = subprocess.run(
        ["bash", str(SCRIPT), provider, "do the work"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, env_out


def _env_lines(path: Path) -> set[str]:
    return set(path.read_text(encoding="utf-8").splitlines())


def test_grok_worker_gets_no_personal_memory_or_compat_instructions(tmp_path):
    binary = _fake(tmp_path, "grok", FAKE_GROK)

    result, env_out = _run(tmp_path, "grok", "NATIVE_CLI_GROK_BIN", binary)

    assert result.returncode == 0, result.stderr
    assert "grok-done" in result.stdout
    assert PERSONAL not in result.stdout
    lines = _env_lines(env_out)
    assert "GROK_MEMORY=0" in lines
    for name in (
        "GROK_CLAUDE_AGENTS_ENABLED",
        "GROK_CLAUDE_RULES_ENABLED",
        "GROK_CURSOR_AGENTS_ENABLED",
        "GROK_CURSOR_RULES_ENABLED",
    ):
        assert f"{name}=false" in lines
    # The login lives in GROK_HOME (default ~/.grok): the launcher leaves it.
    assert not any(line.startswith("GROK_HOME=") for line in lines)
    assert f"HOME={tmp_path / 'home'}" in lines


def test_deepseek_worker_gets_an_empty_shared_agents_home(tmp_path):
    binary = _fake(tmp_path, "dsh", FAKE_DSH)

    result, env_out = _run(tmp_path, "deepseek", "NATIVE_CLI_DSH_BIN", binary)

    assert result.returncode == 0, result.stderr
    assert "dsh-done" in result.stdout
    assert PERSONAL not in result.stdout
    lines = _env_lines(env_out)
    agents = [line for line in lines if line.startswith("DSH_AGENTS_HOME=")]
    assert len(agents) == 1
    agents_home = Path(agents[0].split("=", 1)[1])
    assert agents_home.is_dir()
    assert agents_home.resolve().is_relative_to((tmp_path / "log").resolve())
    assert (env_out.parent / "env.out.agents-home").read_text(encoding="utf-8") == ""
    # DSH_HOME holds the profiles and the login: the launcher leaves it.
    assert not any(line.startswith("DSH_HOME=") for line in lines)


def test_other_workers_do_not_get_the_grok_or_dsh_switches(tmp_path):
    binary = _fake(tmp_path, "kimi", FAKE_KIMI)

    result, env_out = _run(tmp_path, "kimi", "NATIVE_CLI_KIMI_BIN", binary)

    assert result.returncode == 0, result.stderr
    assert env_out.read_text(encoding="utf-8") == ""
