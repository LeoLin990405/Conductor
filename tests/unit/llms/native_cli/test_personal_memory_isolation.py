"""Reviewers and Ticket workers run without the operator's personal agent memory.

The operator's CODEX_HOME holds ``memories/memory_summary.md``, a global
``AGENTS.md`` and MCP servers in ``config.toml``. A reviewer's Codex keeps the
login and the transcript store but sees none of the rest, and Claude Code's
reviewer runs in safe mode with auto-memory off. Every home here is synthetic.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from src.llms.native_cli import get_read_only_adapter
from src.llms.native_cli.codex import CodexExecAdapter
from src.llms.native_cli.personal_memory import (
    CODEX_MEMORY_OFF_ARGS,
    prepare_isolated_codex_home,
)

MEMORY_OFF = list(CODEX_MEMORY_OFF_ARGS)


def _contains_run(argv, run) -> bool:
    argv = list(argv)
    return any(argv[i : i + len(run)] == run for i in range(len(argv) - len(run) + 1))


def _fake_codex_home(root: Path, *, store: str | None = None) -> Path:
    home = root / "operator-codex"
    (home / "memories").mkdir(parents=True)
    (home / "memories" / "memory_summary.md").write_text("private topics\n")
    (home / "AGENTS.md").write_text("Always do Fleet Memory lookups.\n")
    (home / "skills").mkdir()
    (home / "sessions").mkdir()
    (home / "sessions" / "keep.jsonl").write_text("{}\n")
    (home / "auth.json").write_text('{"synthetic": "not-a-credential"}\n')
    lines = ['model = "gpt-test"', 'model_reasoning_effort = "high"']
    if store is not None:
        lines.append(f'cli_auth_credentials_store = "{store}"')
    lines += [
        'developer_instructions = "personal"',
        "",
        "[features]",
        "memories = true",
        "",
        "[memories]",
        "use_memories = true",
        "generate_memories = true",
        "",
        "[mcp_servers.fleet-memory]",
        'command = "fleet-memory-mcp"',
        "",
        '[projects."/Users/someone/private"]',
        'trust_level = "trusted"',
        "",
    ]
    (home / "config.toml").write_text("\n".join(lines))
    return home


FAKE_CODEX = """#!{python}
import json, os, sys
home = os.environ.get("CODEX_HOME", "")
record = {{"argv": sys.argv[1:], "codex_home": home}}
if home:
    record["entries"] = sorted(os.listdir(home))
    record["memories_exists"] = os.path.exists(os.path.join(home, "memories"))
    record["agents_exists"] = os.path.exists(os.path.join(home, "AGENTS.md"))
    auth = os.path.join(home, "auth.json")
    record["auth_is_link"] = os.path.islink(auth)
    record["auth_target"] = os.readlink(auth) if os.path.islink(auth) else None
    record["sessions_target"] = (
        os.readlink(os.path.join(home, "sessions"))
        if os.path.islink(os.path.join(home, "sessions")) else None
    )
    record["mode"] = os.stat(home).st_mode & 0o777
    with open(os.path.join(home, "config.toml"), encoding="utf-8") as handle:
        record["config"] = handle.read()
sys.stdin.read()
with open(os.environ["FAKE_CODEX_RECORD"], "w", encoding="utf-8") as handle:
    json.dump(record, handle)
print(json.dumps({{"type": "thread.started", "thread_id": "t-1"}}))
print(json.dumps({{"type": "item.completed",
                  "item": {{"type": "agent_message", "text": "LGTM"}}}}))
print(json.dumps({{"type": "turn.completed", "usage": {{}}}}))
sys.exit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
"""


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir()
    binary.write_text(FAKE_CODEX.format(python=sys.executable))
    binary.chmod(0o755)
    record = tmp_path / "record.json"
    monkeypatch.setenv("NATIVE_CLI_CODEX_BIN", str(binary))
    monkeypatch.setenv("FAKE_CODEX_RECORD", str(record))
    monkeypatch.delenv("CONDUCTOR_CODEX_OUTPUT_SCHEMA", raising=False)
    return record


def test_reviewer_codex_home_keeps_login_but_drops_personal_memory(
    tmp_path, monkeypatch, fake_codex
):
    source = _fake_codex_home(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(source))
    adapter = get_read_only_adapter("codex", cwd=str(tmp_path))

    result = asyncio.run(adapter.run("review this diff"))

    assert result.text == "LGTM"
    seen = json.loads(fake_codex.read_text())
    home = Path(seen["codex_home"])
    assert home != source and seen["codex_home"]
    assert seen["memories_exists"] is False
    assert seen["agents_exists"] is False
    assert set(seen["entries"]) == {"auth.json", "config.toml", "sessions"}
    assert seen["auth_is_link"] is True
    assert Path(seen["auth_target"]) == source / "auth.json"
    assert Path(seen["sessions_target"]) == source / "sessions"
    assert seen["mode"] == 0o700
    config = tomllib.loads(seen["config"])
    assert "mcp_servers" not in config
    assert "projects" not in config
    assert "developer_instructions" not in config
    assert config["features"]["memories"] is False
    assert config["memories"] == {"use_memories": False, "generate_memories": False}
    assert config["model"] == "gpt-test"
    assert _contains_run(seen["argv"], MEMORY_OFF)
    # The temporary home is gone; the operator's store is untouched.
    assert not home.exists()
    assert (source / "sessions" / "keep.jsonl").read_text() == "{}\n"
    assert (source / "memories" / "memory_summary.md").exists()
    assert (source / "auth.json").read_text().startswith("{")


def test_isolated_home_is_removed_when_the_run_fails(
    tmp_path, monkeypatch, fake_codex
):
    source = _fake_codex_home(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(source))
    monkeypatch.setenv("FAKE_CODEX_EXIT", "3")
    adapter = get_read_only_adapter("codex", cwd=str(tmp_path))

    with pytest.raises(Exception):
        asyncio.run(adapter.run("review"))

    seen = json.loads(fake_codex.read_text())
    assert seen["codex_home"] and not Path(seen["codex_home"]).exists()


def test_keyring_store_keeps_the_real_home_but_still_turns_memory_off(
    tmp_path, monkeypatch, fake_codex
):
    source = _fake_codex_home(tmp_path, store="keyring")
    monkeypatch.setenv("CODEX_HOME", str(source))
    adapter = get_read_only_adapter("codex", cwd=str(tmp_path))

    asyncio.run(adapter.run("review"))

    seen = json.loads(fake_codex.read_text())
    assert seen["codex_home"] == str(source)
    assert _contains_run(seen["argv"], MEMORY_OFF)
    assert prepare_isolated_codex_home(source, tmp_path / "out") is None


def test_reviewer_argv_turns_memory_off_and_default_argv_is_unchanged(monkeypatch):
    monkeypatch.setenv("NATIVE_CLI_CODEX_BIN", "/opt/fake/codex")
    reviewer = get_read_only_adapter("codex", cwd="/tmp/work")
    assert _contains_run(reviewer.build_invocation("p").argv, MEMORY_OFF)

    default = CodexExecAdapter(binary="codex").build_invocation("chat").argv
    assert default == (
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-",
    )
    assert not any("memories" in item for item in default)


def test_ticket_worker_argv_turns_memory_off(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    argv = CodexExecAdapter(
        binary="codex", isolate_user_instructions=True
    ).build_invocation("ticket", session_id="t-1").argv
    assert _contains_run(argv, MEMORY_OFF)
    # Exec-level flags precede the ``resume`` subcommand.
    assert argv.index("--disable") < argv.index("resume")


def test_prepare_isolated_home_without_auth_or_sessions(tmp_path):
    source = tmp_path / "empty-home"
    source.mkdir()
    home = prepare_isolated_codex_home(source, tmp_path / "parent")
    assert home is not None
    assert sorted(os.listdir(home)) == ["config.toml"]
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(home / "config.toml").st_mode) == 0o600


@pytest.mark.parametrize(
    "config",
    ['model_provider = "my-proxy"\n', "not = [valid toml\n", 'cli_auth_credentials_store = "auto"\n'],
)
def test_unsafe_source_configs_fall_back_to_flags_only(tmp_path, config):
    source = tmp_path / "home"
    source.mkdir()
    (source / "config.toml").write_text(config)
    assert prepare_isolated_codex_home(source, tmp_path / "parent") is None
    assert not (tmp_path / "parent").exists()


def test_claude_reviewer_runs_in_safe_mode_without_auto_memory(monkeypatch):
    monkeypatch.setenv("NATIVE_CLI_CLAUDE_BIN", "/opt/fake/claude")
    invocation = get_read_only_adapter("claude", cwd="/tmp/work").build_invocation(
        "review"
    )
    assert "--safe-mode" in invocation.argv
    assert "--restricted" in invocation.argv
    assert invocation.argv.count("--safe-mode") == 1
    assert dict(invocation.env or {}).get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1"


def test_default_claude_invocation_is_unchanged(monkeypatch):
    from src.llms.native_cli.claude import ClaudeCodeAdapter

    invocation = ClaudeCodeAdapter(binary="claude").build_invocation("chat")
    assert "--safe-mode" not in invocation.argv
    assert invocation.env is None


def _replace_auth_like_codex(home: Path, body: str) -> None:
    """Codex (or anything that can write the home) swaps the auth.json link for a file."""
    temporary = home / ".auth.json.tmp"
    temporary.write_text(body)
    os.replace(temporary, home / "auth.json")


def test_a_planted_auth_json_never_reaches_the_real_login(tmp_path):
    """Credential substitution: nothing found in the home is ever written back."""
    from src.llms.native_cli.personal_memory import (
        remove_isolated_codex_home,
        sweep_stale_isolated_codex_homes,
    )

    source = _fake_codex_home(tmp_path)
    real = (source / "auth.json").read_text()
    parent = tmp_path / "parent"
    for sweep in (False, True):
        home = prepare_isolated_codex_home(source, parent)
        _replace_auth_like_codex(home, '{"synthetic": "planted"}\n')
        future = os.stat(source / "auth.json").st_mtime + 3600
        os.utime(home / "auth.json", (future, future))
        if sweep:
            os.utime(home, (0, 0))
            assert sweep_stale_isolated_codex_homes(parent) >= 1
        else:
            remove_isolated_codex_home(home)
        assert not home.exists()
        assert (source / "auth.json").read_text() == real
        assert not (source / "auth.json").is_symlink()


def test_the_home_records_no_source_path_on_disk(tmp_path):
    source = _fake_codex_home(tmp_path)
    home = prepare_isolated_codex_home(source, tmp_path / "parent")
    assert sorted(os.listdir(home)) == ["auth.json", "config.toml", "sessions"]
    assert str(source) not in (home / "config.toml").read_text()


def test_isolated_homes_live_outside_tmpdir_and_are_private(monkeypatch, tmp_path):
    import tempfile

    from src.llms.native_cli.personal_memory import isolated_codex_home_parent

    monkeypatch.delenv("CONDUCTOR_CODEX_HOMES_DIR", raising=False)
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "conductor"))
    parent = isolated_codex_home_parent()
    base = tmp_path / "conductor" / "codex-homes"
    # Each process gets its own folder under the shared base.
    assert parent.parent == base and parent.name.startswith(f"proc-{os.getpid()}-")
    assert stat.S_IMODE(os.stat(base).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(parent).st_mode) == 0o700
    assert not str(parent).startswith(tempfile.gettempdir())
    # A shared (group/world-writable) base is refused: flags only.
    os.chmod(base, 0o777)
    assert isolated_codex_home_parent() is None


def test_the_startup_sweep_removes_only_stale_isolated_homes(tmp_path):
    import time

    from src.llms.native_cli.personal_memory import (
        ISOLATED_CODEX_HOME_PREFIX,
        sweep_stale_isolated_codex_homes,
    )

    source = _fake_codex_home(tmp_path)
    parent = tmp_path / "parent"
    stale = prepare_isolated_codex_home(source, parent)
    fresh = prepare_isolated_codex_home(source, parent)
    foreign = parent / "someone-elses-dir"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("not ours\n")
    linked = parent / f"{ISOLATED_CODEX_HOME_PREFIX}link"
    linked.symlink_to(foreign, target_is_directory=True)
    past = time.time() - 7 * 3600
    for path in (stale, foreign):
        os.utime(path, (past, past))
    os.utime(linked, (past, past), follow_symlinks=False)

    assert sweep_stale_isolated_codex_homes(parent) == 1

    assert not stale.exists()
    assert fresh.exists() and foreign.exists() and linked.is_symlink()
    assert (foreign / "notes.txt").exists()
    # The links were removed, never followed.
    assert (source / "auth.json").exists() and (source / "sessions" / "keep.jsonl").exists()


LOCK_HOLDER = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.read()
"""


def _process_folder(base: Path, name: str) -> Path:
    folder = base / name
    folder.mkdir(parents=True)
    os.chmod(folder, 0o700)
    (folder / "conductor-codex-home-abc").mkdir()
    (folder / "conductor-codex-home-abc" / "config.toml").write_text("")
    return folder


def test_the_sweep_never_removes_a_live_process_folder_however_old(tmp_path):
    """The base is shared by every Conductor process; a live owner keeps its homes."""
    import subprocess
    import time

    from src.llms.native_cli.personal_memory import OWNER_LOCK, sweep_dead_process_dirs

    base = tmp_path / "codex-homes"
    live = _process_folder(base, "proc-111-live")
    dead = _process_folder(base, "proc-222-dead")
    (dead / OWNER_LOCK).write_text("")
    holder = subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER, str(live / OWNER_LOCK)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        past = time.time() - 30 * 24 * 3600
        for folder in (live, dead):
            os.utime(folder, (past, past))

        assert sweep_dead_process_dirs(base) == 1
        assert live.exists() and (live / "conductor-codex-home-abc").exists()
        assert not dead.exists()
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)

    # Its owner is gone now, so its lock is free: the next sweep removes it.
    assert sweep_dead_process_dirs(base) == 1
    assert not live.exists()


def test_a_process_keeps_its_own_folder_locked_and_sweeps_once(tmp_path, monkeypatch):
    from src.llms.native_cli import personal_memory

    monkeypatch.setattr(personal_memory, "_PROCESS_DIRS", {})
    monkeypatch.setattr(personal_memory, "_SWEPT", set())
    base = tmp_path / "codex-homes"
    base.mkdir(mode=0o700)
    calls = []
    real = personal_memory.sweep_dead_process_dirs
    monkeypatch.setattr(
        personal_memory, "sweep_dead_process_dirs", lambda b, **kw: calls.append(b) or real(b, **kw)
    )
    monkeypatch.setenv("CONDUCTOR_CODEX_HOMES_DIR", str(base))
    first = personal_memory.isolated_codex_home_parent()
    second = personal_memory.isolated_codex_home_parent()
    assert first == second and calls == [base]
    # Our own lock is held: another sweep (as from a second process) leaves it.
    monkeypatch.setattr(personal_memory, "_PROCESS_DIRS", {})
    assert real(base) == 0
    assert first.exists()
