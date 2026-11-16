from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.llms.native_cli.base import NativeCliError, NativeCliUnavailable
from src.llms.native_cli.isolation import prepare_process, worker_isolation
from src.llms.native_cli.opencode import OpenCodeAdapter


def _fake_opencode(runtime: Path, workspace: Path, *, fail: bool) -> Path:
    runtime.mkdir()
    fake = runtime / "opencode"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, stat\n"
        "from pathlib import Path\n"
        f"fail = {fail!r}\n"
        "config = Path(os.environ.get('OPENCODE_CONFIG_DIR', ''))\n"
        "write_ok = False\n"
        "if config.is_dir():\n"
        "    (config / 'child-write').write_text('allowed')\n"
        "    write_ok = True\n"
        "proof = {\n"
        "    'config': str(config),\n"
        "    'home': os.environ.get('HOME', ''),\n"
        "    'mode': stat.S_IMODE(config.stat().st_mode) if config.is_dir() else None,\n"
        "    'write_ok': write_ok,\n"
        "}\n"
        f"Path({str(workspace / 'proof.json')!r}).write_text(json.dumps(proof))\n"
        "if fail:\n"
        "    print(json.dumps({'type': 'error', 'error': {'message': 'quota exceeded: private-detail'}}))\n"
        "else:\n"
        "    print(json.dumps({'type': 'text', 'part': {'text': 'ok'}, 'sessionID': 'fixture-session'}))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True], ids=["success", "failure"])
async def test_opencode_config_is_private_writable_and_always_cleaned(
    tmp_path: Path, fail: bool
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = tmp_path / "runtime"
    fake = _fake_opencode(runtime, workspace, fail=fail)

    with worker_isolation(
        workspace_id="opencode-fixture",
        root=tmp_path / "state",
        workdir=workspace,
        env_names=set(),
        runtime_read_paths=(str(runtime), "/Library/Developer/CommandLineTools"),
    ) as isolated:
        adapter = OpenCodeAdapter(binary=str(fake), timeout_seconds=10)
        if fail:
            with pytest.raises(NativeCliError) as caught:
                await adapter.run("Run the fixture")
            assert str(caught.value) == "opencode failure: opencode_quota_exceeded"
            assert "private-detail" not in str(caught.value)
        else:
            result = await adapter.run("Run the fixture")
            assert result.text == "ok"

        proof = json.loads((workspace / "proof.json").read_text(encoding="utf-8"))
        config = Path(proof["config"])
        assert proof["home"] == str(isolated.home)
        assert proof["write_ok"] is True
        assert proof["mode"] == 0o700
        assert config.is_relative_to(isolated.home)
        assert not config.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["outside-home", "group-readable"])
async def test_opencode_config_rejects_unsafe_directories(
    tmp_path: Path, problem: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with worker_isolation(
        workspace_id=f"opencode-{problem}",
        root=tmp_path / "state",
        workdir=workspace,
        env_names=set(),
    ) as isolated:
        candidate = (
            tmp_path / "outside"
            if problem == "outside-home"
            else isolated.home / "group-readable"
        )
        candidate.mkdir()
        candidate.chmod(0o700 if problem == "outside-home" else 0o755)

        with pytest.raises(NativeCliUnavailable) as caught:
            prepare_process(
                ("/usr/bin/true",),
                str(workspace),
                {"OPENCODE_CONFIG_DIR": str(candidate)},
            )

        assert str(candidate) not in str(caught.value)


@pytest.mark.asyncio
async def test_policy_cannot_inject_opencode_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "attacker-controlled"))

    with worker_isolation(
        workspace_id="opencode-reserved-env",
        root=tmp_path / "state",
        workdir=workspace,
        env_names={"OPENCODE_CONFIG_DIR"},
    ) as isolated:
        assert "OPENCODE_CONFIG_DIR" not in isolated.env_names
        _argv, env, _cwd = prepare_process(("/usr/bin/true",), str(workspace), {})

    assert env is not None
    assert "OPENCODE_CONFIG_DIR" not in env
