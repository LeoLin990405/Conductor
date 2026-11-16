"""DeepSeek DSH invocation and opt-in live smoke coverage."""

from __future__ import annotations

import os
import stat
from dataclasses import replace

import pytest

from src.llms.native_cli.base import NativeCliError
from src.llms.native_cli.deepseek import DeepSeekDshAdapter
from src.llms.native_cli.failure import FailureClass, classify_failure
from src.llms.native_cli.isolation import prepare_process, worker_isolation


@pytest.mark.asyncio
async def test_dsh_receives_env_file_values_only_in_child_process(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    home = tmp_path / "home"
    config = home / ".config" / "deepseek"
    config.mkdir(parents=True)
    prompt = '--help "quoted" $(printf injected)'
    (config / "env").write_text(
        "printf 'env-file-stdout-must-stay-hidden\\n'\n"
        "printf 'env-file-stderr-must-stay-hidden\\n' >&2\n"
        "set -x\n"
        "DSH_TEST_SENTINEL=loaded-in-child-only\n"
        "DSH_TEST_PROMPT='--help \"quoted\" $(printf injected)'\n"
    )
    fake_dsh = tmp_path / "fake-dsh"
    fake_dsh.write_text(
        "#!/bin/sh\n"
        'if [ "$DSH_TEST_SENTINEL" = "loaded-in-child-only" ] && '
        '[ "$DSH_TEST_PROMPT" = "$4" ] && [ "$1" = "headless" ] && '
        '[ "$2" = "--json" ] && [ "$3" = "--" ] && [ "$#" -eq 4 ]; then\n'
        '  printf \'%s\\n\' \'{"type":"session","sessionId":"s-1"}\'\n'
        '  printf \'%s\\n\' \'{"type":"final","text":"ok"}\'\n'
        "else\n  exit 4\nfi\n"
    )
    fake_dsh.chmod(fake_dsh.stat().st_mode | stat.S_IXUSR)

    adapter = DeepSeekDshAdapter(
        binary=str(fake_dsh), harness_home=str(tmp_path / "dsh")
    )
    invocation = adapter.build_invocation(prompt)
    invocation = replace(invocation, env={**invocation.env, "HOME": str(home)})

    from src.llms.native_cli.process import run_process

    result = await run_process(invocation)
    assert '"text":"ok"' in result.stdout
    assert "DSH_TEST_SENTINEL" not in result.stdout
    assert "env-file-stdout-must-stay-hidden" not in result.stdout
    assert "env-file-stderr-must-stay-hidden" not in result.stderr
    assert prompt not in result.stderr
    assert "DSH_TEST_SENTINEL" not in os.environ
    assert "DSH_TEST_PROMPT" not in os.environ


def test_dsh_outer_pass_through_leaves_one_headless_task_delimiter():
    prompt = '--help "quoted" $(printf injected)'
    invocation = DeepSeekDshAdapter(binary="/opt/dsh").build_invocation(prompt)

    # The launcher passes through arguments after --json. The headless parser
    # consumes its single `--`, leaving the original prompt as the task.
    profile_args = invocation.argv[5:]
    assert profile_args == ("headless", "--json", "--", prompt)
    headless_args = profile_args[1:]
    task = headless_args[headless_args.index("--") + 1 :]
    assert task == (prompt,)


def test_dsh_unisolated_invocation_does_not_patch_sandbox(tmp_path):
    invocation = DeepSeekDshAdapter(
        binary="/opt/dsh", harness_home=str(tmp_path / "dsh-home")
    ).build_invocation("hello")
    assert "--patch" not in invocation.argv
    assert "DSH_PERMISSION_MODE" not in invocation.env


@pytest.mark.asyncio
async def test_dsh_fake_cli_uses_outer_sandbox_patch_noninteractively(tmp_path):
    import shutil

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_dsh = workspace / "fake-dsh"
    fake_dsh.write_text(
        "#!/bin/sh\n"
        '[ "$1" = "headless" ] || exit 8\n'
        '[ "$2" = "--patch" ] || exit 9\n'
        'patch="$3"\n'
        "grep -q 'mode: danger-full-access' \"$patch\" || exit 10\n"
        "grep -q 'policy: never' \"$patch\" || exit 11\n"
        "printf '%s\\n' '{\"type\":\"final\",\"text\":\"outer sandbox active\"}'\n"
    )
    fake_dsh.chmod(fake_dsh.stat().st_mode | stat.S_IXUSR)

    with worker_isolation(
        workspace_id="dsh-nested-sandbox",
        root=tmp_path / "state",
        workdir=workspace,
        env_names=set(),
        runtime_read_paths=(),
        read_only=True,
    ) as isolation:
        if not shutil.which("sandbox-exec") and not shutil.which("bwrap"):
            pytest.skip("no supported worker isolation runtime")
        env_file = isolation.home / ".config" / "deepseek" / "env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text("DEEPSEEK_API_KEY=fake-key-for-test\n", encoding="utf-8")
        result = await DeepSeekDshAdapter(binary=str(fake_dsh)).run("say ok")

    assert result.text == "outer sandbox active"


@pytest.mark.asyncio
async def test_dsh_does_not_run_when_child_env_file_is_missing(tmp_path, monkeypatch):
    home = tmp_path / "empty-home"
    home.mkdir()
    marker = tmp_path / "dsh-was-launched"
    fake_dsh = tmp_path / "fake-dsh"
    fake_dsh.write_text(
        f"#!/bin/sh\ntouch '{marker}'\nprintf '%s\\n' "
        "'{\"type\":\"final\",\"text\":\"ok\"}'\n"
    )
    fake_dsh.chmod(fake_dsh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(NativeCliError, match="DeepSeek API key unavailable"):
        await DeepSeekDshAdapter(binary=str(fake_dsh)).run("say ok")
    assert not marker.exists()


@pytest.mark.asyncio
async def test_dsh_uses_process_api_key_without_private_home_file(tmp_path, monkeypatch):
    home = tmp_path / "private-home"
    home.mkdir()
    fake_dsh = tmp_path / "fake-dsh"
    fake_dsh.write_text(
        "#!/bin/sh\n"
        '[ "$DEEPSEEK_API_KEY" = "test-api-key" ] || exit 9\n'
        'printf \'%s\\n\' \'{"type":"text","text":"ok"}\'\n'
        'printf \'%s\\n\' \'{"type":"final","text":"ok"}\'\n'
    )
    fake_dsh.chmod(fake_dsh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-api-key")

    adapter = DeepSeekDshAdapter(binary=str(fake_dsh))
    invocation = adapter.build_invocation("say ok")
    assert "test-api-key" not in repr(invocation.argv)

    from src.llms.native_cli.process import run_process

    output = await run_process(invocation)
    result = await adapter.run("say ok")
    assert result.text == "ok"
    assert "test-api-key" not in output.stderr
    assert "test-api-key" not in output.stdout
    assert "test-api-key" not in repr(result)

    from src.server.services.native_runtime.dsh_stream import stream_dsh_turn

    events = [
        event
        async for event in stream_dsh_turn(
            adapter, "say ok", session_id=None, cwd=None
        )
    ]
    assert "test-api-key" not in repr(events)


@pytest.mark.asyncio
async def test_dsh_missing_key_is_classified_as_auth_failure(tmp_path, monkeypatch):
    home = tmp_path / "private-home"
    home.mkdir()
    fake_dsh = tmp_path / "fake-dsh"
    marker = tmp_path / "dsh-was-launched"
    fake_dsh.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    fake_dsh.chmod(fake_dsh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(NativeCliError) as raised:
        await DeepSeekDshAdapter(binary=str(fake_dsh)).run("say ok")
    classification = classify_failure("deepseek-dsh", str(raised.value))
    assert classification.failure_class is FailureClass.AUTH_FAILED
    assert classification.reason_code == "deepseek_api_key_unavailable"
    assert not marker.exists()


@pytest.mark.asyncio
async def test_dsh_missing_key_marker_must_be_the_entire_stderr(tmp_path, monkeypatch):
    home = tmp_path / "private-home"
    home.mkdir()
    fake_dsh = tmp_path / "fake-dsh"
    fake_dsh.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'prefix deepseek_api_key_unavailable suffix' >&2\n"
        "exit 78\n"
    )
    fake_dsh.chmod(fake_dsh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-api-key")

    with pytest.raises(NativeCliError, match="dsh CLI failed") as raised:
        await DeepSeekDshAdapter(binary=str(fake_dsh)).run("say ok")
    classification = classify_failure("deepseek-dsh", str(raised.value))
    assert classification.reason_code != "deepseek_api_key_unavailable"


@pytest.mark.asyncio
async def test_dsh_account_mode_keeps_missing_env_file_generic(tmp_path, monkeypatch):
    home = tmp_path / "private-home"
    home.mkdir()
    fake_dsh = tmp_path / "fake-dsh"
    fake_dsh.write_text("#!/bin/sh\nexit 0\n")
    fake_dsh.chmod(fake_dsh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(NativeCliError, match="dsh CLI failed"):
        await DeepSeekDshAdapter(binary=str(fake_dsh), account_auth=True).run("say ok")


@pytest.mark.asyncio
async def test_dsh_account_mode_keeps_missing_env_file_generic_under_isolation(
    tmp_path, monkeypatch
):
    work = tmp_path / "workspace"
    work.mkdir()
    fake_dsh = work / "fake-dsh"
    marker = work / "dsh-was-launched"
    fake_dsh.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n")
    fake_dsh.chmod(fake_dsh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    adapter = DeepSeekDshAdapter(binary=str(fake_dsh), account_auth=True)

    with worker_isolation(
        workspace_id="deepseek-account-auth",
        root=tmp_path / "isolation",
        workdir=work,
        env_names={"DEEPSEEK_API_KEY", "CONDUCTOR_DSH_ACCOUNT_AUTH"},
    ):
        invocation = adapter.build_invocation("say ok", cwd=str(work))
        _argv, env, _cwd = prepare_process(
            invocation.argv, invocation.cwd, invocation.env
        )
        assert env is not None
        assert "CONDUCTOR_DSH_ACCOUNT_AUTH" not in env
        assert "CONDUCTOR_DSH_ACCOUNT_AUTH" not in invocation.env
        with pytest.raises(NativeCliError, match="dsh CLI failed") as raised:
            await adapter.run("say ok", cwd=str(work))

    classification = classify_failure("deepseek-dsh", str(raised.value))
    assert classification.reason_code != "deepseek_api_key_unavailable"
    assert not marker.exists()


@pytest.mark.asyncio
async def test_dsh_process_diagnostics_are_not_forwarded(monkeypatch):
    async def fail(_invocation):
        raise NativeCliError("provider leaked secret-value in diagnostics")

    monkeypatch.setattr("src.llms.native_cli.deepseek.run_process", fail)
    with pytest.raises(NativeCliError, match="dsh CLI failed") as raised:
        await DeepSeekDshAdapter(binary="/opt/dsh").run("hello")
    assert "secret-value" not in str(raised.value)


@pytest.mark.asyncio
async def test_dsh_dyld_failure_keeps_safe_runtime_dependency_classification(
    monkeypatch,
):
    async def fail(_invocation):
        raise NativeCliError(
            "native CLI exited -6: dyld[42]: Library not loaded: "
            "/private/runtime/secret/libsimdjson.34.dylib; "
            "Reason: tried /private/secret/path"
        )

    monkeypatch.setattr("src.llms.native_cli.deepseek.run_process", fail)
    with pytest.raises(NativeCliError) as raised:
        await DeepSeekDshAdapter(binary="/opt/dsh").run("hello")

    classification = classify_failure("deepseek-dsh", str(raised.value))
    assert classification.failure_class is FailureClass.CRASH
    assert classification.reason_code == "deepseek_runtime_dependency_missing"
    assert str(raised.value) == "dyld: library not loaded"
    assert "secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_dsh_stream_process_diagnostics_are_not_forwarded(monkeypatch):
    from src.server.services.native_runtime.dsh_stream import stream_dsh_turn

    async def fail(_invocation):
        if False:
            yield ""
        raise NativeCliError("provider leaked secret-value in diagnostics")

    monkeypatch.setattr(
        "src.server.services.native_runtime.dsh_stream.stream_process", fail
    )
    with pytest.raises(NativeCliError, match="dsh CLI failed") as raised:
        _ = [
            event
            async for event in stream_dsh_turn(
                DeepSeekDshAdapter(binary="/opt/dsh"),
                "hello",
                session_id=None,
                cwd=None,
            )
        ]
    assert "secret-value" not in str(raised.value)


@pytest.mark.asyncio
async def test_live_deepseek_smoke_once_when_explicitly_enabled(capsys):
    if os.environ.get("CONDUCTOR_LIVE_DEEPSEEK") != "1":
        pytest.skip("set CONDUCTOR_LIVE_DEEPSEEK=1 to make one DeepSeek call")

    try:
        result = await DeepSeekDshAdapter(timeout_seconds=90).run(
            "Reply with exactly: deepseek live smoke ok"
        )
    except Exception:
        pytest.fail("DeepSeek live smoke failed; details suppressed", pytrace=False)
    if result.text.strip() != "deepseek live smoke ok":
        pytest.fail(
            "DeepSeek live smoke returned an unexpected response", pytrace=False
        )
    print("DEEPSEEK_LIVE_SMOKE_RESULT=ok")
    assert capsys.readouterr().out.strip() == "DEEPSEEK_LIVE_SMOKE_RESULT=ok"
