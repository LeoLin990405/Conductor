"""N19: isolated Workers must not run binaries they put in scratch areas."""

import logging
import shutil
from pathlib import Path

import pytest

from src.llms.native_cli import exec_guard
from src.llms.native_cli.base import CliInvocation
from src.llms.native_cli.isolation import _mac_profile, worker_isolation
from src.llms.native_cli.process import run_process

_SCRIPT = "#!/bin/sh\necho ran-$1\n"


def _exe(path: Path, body: str = _SCRIPT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _workspace(tmp_path: Path) -> Path:
    work = tmp_path / "workspace"
    work.mkdir()
    return work


def test_mode_defaults_to_shadow_and_rejects_unknown_values():
    assert exec_guard.exec_guard_mode({}) == "shadow"
    assert exec_guard.exec_guard_mode({exec_guard.EXEC_GUARD_ENV: " ENFORCE "}) == "enforce"
    assert exec_guard.exec_guard_mode({exec_guard.EXEC_GUARD_ENV: "off"}) == "off"
    assert exec_guard.exec_guard_mode({exec_guard.EXEC_GUARD_ENV: "block"}) == "shadow"
    with pytest.raises(ValueError):
        exec_guard.normalize_mode("block")


def test_worker_isolation_reads_the_mode_from_the_environment(tmp_path, monkeypatch):
    work = _workspace(tmp_path)
    monkeypatch.setenv(exec_guard.EXEC_GUARD_ENV, "enforce")
    with worker_isolation(
        workspace_id="w", root=tmp_path / "state", workdir=work, env_names=set()
    ) as isolation:
        assert isolation.exec_guard == "enforce"
    monkeypatch.delenv(exec_guard.EXEC_GUARD_ENV)
    with worker_isolation(
        workspace_id="w", root=tmp_path / "state", workdir=work, env_names=set()
    ) as isolation:
        assert isolation.exec_guard == "shadow"


def test_scratch_roots_cover_workspace_and_home_scratch_only(tmp_path):
    work = _workspace(tmp_path)
    with worker_isolation(
        workspace_id="w",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        exec_guard="enforce",
    ) as isolation:
        roots = isolation.exec_guard_roots
    resolved = work.resolve()
    assert resolved / ".orchestrator" in roots
    assert isolation.home / "autonomy" in roots
    assert isolation.home / "Downloads" in roots
    assert all(root != resolved for root in roots)
    assert not any(".venv" in str(root) or "node_modules" in str(root) for root in roots)


def test_enforce_profile_denies_exec_after_the_exec_allow(tmp_path):
    work = _workspace(tmp_path)
    with worker_isolation(
        workspace_id="w",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        exec_guard="enforce",
    ) as isolation:
        profile = _mac_profile(isolation.workdir, isolation.home, isolation.root / "tmp")
    scratch = str(work.resolve() / ".orchestrator")
    deny = f'(deny process-exec* (subpath "{scratch}")'
    assert deny in profile
    assert profile.index("(allow process-exec)") < profile.index(deny)


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_off_and_shadow_profiles_do_not_deny_exec(tmp_path, mode):
    work = _workspace(tmp_path)
    with worker_isolation(
        workspace_id="w",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        exec_guard=mode,
    ) as isolation:
        profile = _mac_profile(isolation.workdir, isolation.home, isolation.root / "tmp")
    assert "deny process-exec" not in profile


def test_shadow_reports_new_scratch_executables_and_ignores_the_rest(tmp_path, caplog):
    work = _workspace(tmp_path)
    old = _exe(work / ".orchestrator" / "autonomy" / "t1" / "old-tool")
    caplog.set_level(logging.WARNING, logger=exec_guard.__name__)
    with worker_isolation(
        workspace_id="w",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        exec_guard="shadow",
    ) as isolation:
        vm = _exe(work / ".orchestrator" / "autonomy" / "t1" / "lima" / "bin" / "limactl")
        ticket = _exe(isolation.home / "autonomy" / "abcd1234" / "qemu")
        (work / ".orchestrator" / "notes.txt").write_text("not executable")
        _exe(work / ".venv" / "bin" / "python")
        _exe(work / "node_modules" / ".bin" / "vite")
    paths = {item.path for item in isolation.exec_guard_findings}
    assert paths == {str(work.resolve() / vm.relative_to(work)), str(ticket)}
    assert str(old) not in paths
    assert "worker_exec_guard_scratch_executables mode=shadow" in caplog.text
    assert "blocked=no" in caplog.text


def test_shadow_reports_a_rewritten_scratch_executable(tmp_path):
    work = _workspace(tmp_path)
    tool = _exe(work / ".orchestrator" / "tool")
    with worker_isolation(
        workspace_id="w",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        exec_guard="shadow",
    ) as isolation:
        _exe(tool, "#!/bin/sh\necho replaced with a longer body\n")
    assert [item.path for item in isolation.exec_guard_findings] == [
        str(work.resolve() / ".orchestrator" / "tool")
    ]


def test_off_mode_does_not_scan(tmp_path, monkeypatch):
    work = _workspace(tmp_path)
    calls = []
    monkeypatch.setattr(
        exec_guard, "snapshot_executables", lambda roots, **_: calls.append(roots) or {}
    )
    with worker_isolation(
        workspace_id="w",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        exec_guard="off",
    ) as isolation:
        _exe(work / ".orchestrator" / "vm")
    assert calls == []
    assert isolation.exec_guard_findings == []


def test_scan_failure_does_not_fail_the_ticket(tmp_path, monkeypatch):
    work = _workspace(tmp_path)
    calls = {"n": 0}

    def flaky(roots, **_):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("boom")
        return {}

    monkeypatch.setattr(exec_guard, "snapshot_executables", flaky)
    with worker_isolation(
        workspace_id="w",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        exec_guard="shadow",
    ):
        pass
    assert calls["n"] == 2


def test_snapshot_is_bounded_and_skips_symlinks(tmp_path):
    root = tmp_path / "scratch"
    for index in range(5):
        _exe(root / f"tool{index}")
    (root / "link").symlink_to(root / "tool0")
    assert len(exec_guard.snapshot_executables([root], limit=3)) <= 3
    full = exec_guard.snapshot_executables([root])
    assert str(root / "link") not in full
    assert len(full) == 5
    assert exec_guard.snapshot_executables([tmp_path / "missing"]) == {}


@pytest.mark.skipif(not shutil.which("sandbox-exec"), reason="macOS Seatbelt unavailable")
@pytest.mark.asyncio
async def test_macos_enforce_blocks_scratch_exec_but_keeps_venv_and_node_bins(tmp_path):
    work = _workspace(tmp_path)
    _exe(work / ".venv" / "bin" / "tool")
    _exe(work / "node_modules" / ".bin" / "tool")
    _exe(work / "scripts" / "tool")
    _exe(work / ".orchestrator" / "autonomy" / "t1" / "vm")
    script = (
        ".venv/bin/tool venv || exit 10; "
        "node_modules/.bin/tool node || exit 11; "
        "scripts/tool repo || exit 12; "
        ".orchestrator/autonomy/t1/vm scratch 2>/dev/null && exit 13; "
        'mkdir -p "$HOME/autonomy/x" && cp scripts/tool "$HOME/autonomy/x/dl" '
        '&& chmod +x "$HOME/autonomy/x/dl" || exit 14; '
        '"$HOME/autonomy/x/dl" home 2>/dev/null && exit 15; '
        "printf done"
    )
    with worker_isolation(
        workspace_id="w",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        exec_guard="enforce",
    ) as isolation:
        result = await run_process(
            CliInvocation(argv=("/bin/sh", "-c", script), cwd=str(work))
        )
    assert result.stdout.splitlines()[-1].endswith("done"), result
    assert "ran-venv" in result.stdout and "ran-node" in result.stdout
    assert "ran-scratch" not in result.stdout and "ran-home" not in result.stdout
    assert {Path(item.path).name for item in isolation.exec_guard_findings} == {"dl"}


@pytest.mark.skipif(not shutil.which("sandbox-exec"), reason="macOS Seatbelt unavailable")
@pytest.mark.asyncio
async def test_macos_shadow_lets_scratch_exec_run(tmp_path):
    work = _workspace(tmp_path)
    _exe(work / ".orchestrator" / "vm")
    with worker_isolation(
        workspace_id="w",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        exec_guard="shadow",
    ):
        result = await run_process(
            CliInvocation(argv=(".orchestrator/vm", "scratch"), cwd=str(work))
        )
    assert "ran-scratch" in result.stdout
