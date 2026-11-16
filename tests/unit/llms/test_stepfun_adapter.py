import os
import subprocess
from pathlib import Path

import pytest

from src.llms.native_cli.base import (
    NativeCliError,
    NativeCliUnavailable,
    NativeCliUsage,
)
from src.llms.native_cli.process import ProcessOutput
from src.llms.native_cli.stepfun import (
    StepFunAdapter,
    parse_step_result,
    parse_step_text,
)


def test_text_invocation_is_stateless_tool_free_extension_free_and_bounded():
    invocation = StepFunAdapter(
        binary="/opt/step", timeout_seconds=37, model="step-3.7-flash"
    ).build_text_invocation("say hello", cwd="/tmp/isolated")

    assert invocation.argv == (
        "/opt/step",
        "-p",
        "--no-session",
        "--no-tools",
        # ``--no-tools`` does not stop a trusted folder's extension code from running.
        "--no-extensions",
        "--no-approve",
        "--model",
        "step-3.7-flash",
        "--",
        "say hello",
    )
    assert invocation.timeout_seconds == 37
    assert invocation.cwd == "/tmp/isolated"


def test_coding_invocation_keeps_json_session_contract():
    invocation = StepFunAdapter(binary="/opt/step").build_invocation(
        "inspect project", session_id="prior-session"
    )

    assert invocation.argv[:3] == ("/opt/step", "--mode", "json")
    assert invocation.argv[-2:] == ("--session-id", "prior-session")
    assert "--no-session" not in invocation.argv


def test_parse_text_output_and_explicit_auth_errors():
    assert parse_step_text("  concise answer\n") == "concise answer"
    with pytest.raises(NativeCliUnavailable, match="StepFun is not logged in"):
        parse_step_text('{"error":{"code":"unauthorized","message":"private"}}')
    with pytest.raises(NativeCliError, match="request error"):
        parse_step_text('{"error":{"code":"bad_request"}}')
    with pytest.raises(NativeCliError, match="no output"):
        parse_step_text("  \n")


def test_parse_coding_json_result_is_retained():
    answer, session, usage, _ = parse_step_result(
        '{"type":"session","id":"session-1"}\n'
        '{"type":"result","result":"ok","usage":{"input_tokens":2}}'
    )
    assert answer == "ok"
    assert session == "session-1"
    assert usage == NativeCliUsage(input_tokens=2)


@pytest.mark.asyncio
async def test_run_returns_text_without_session_or_usage(monkeypatch):
    seen = []

    async def fake_run_process(invocation):
        seen.append(invocation)
        return ProcessOutput(stdout="  ok\n", stderr="", returncode=0)

    monkeypatch.setattr("src.llms.native_cli.stepfun.run_process", fake_run_process)
    result = await StepFunAdapter(binary="/opt/step").run(
        "Reply with exactly: ok", cwd="/tmp/isolated"
    )

    assert result.text == "ok"
    assert result.session_id is None
    assert result.usage == NativeCliUsage()
    assert seen[0].argv[:4] == ("/opt/step", "-p", "--no-session", "--no-tools")
    assert seen[0].argv[-2:] == ("--", "Reply with exactly: ok")
    assert seen[0].cwd == "/tmp/isolated"


@pytest.mark.asyncio
async def test_run_with_session_id_retains_json_resume_contract(monkeypatch):
    seen = []

    async def fake_run_process(invocation):
        seen.append(invocation)
        return ProcessOutput(
            stdout='[{"type":"result","result":"continued",'
            '"sessionId":"session-2"}]',
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("src.llms.native_cli.stepfun.run_process", fake_run_process)
    result = await StepFunAdapter(binary="/opt/step").run(
        "continue", session_id="session-1"
    )

    assert result.text == "continued"
    assert result.session_id == "session-2"
    assert seen[0].argv[:3] == ("/opt/step", "--mode", "json")
    assert seen[0].argv[-2:] == ("--session-id", "session-1")


@pytest.mark.asyncio
async def test_run_sanitizes_process_diagnostics_and_maps_auth(monkeypatch):
    async def generic_failure(_invocation):
        raise NativeCliError("private stderr and token-like text")

    monkeypatch.setattr("src.llms.native_cli.stepfun.run_process", generic_failure)
    with pytest.raises(NativeCliError, match="details omitted") as error:
        await StepFunAdapter(binary="/opt/step").run("hi")
    assert "private stderr" not in str(error.value)

    async def auth_failure(_invocation):
        raise NativeCliUnavailable("/opt/step is not logged in or has no usable credentials")

    monkeypatch.setattr("src.llms.native_cli.stepfun.run_process", auth_failure)
    with pytest.raises(NativeCliUnavailable, match="StepFun is not logged in"):
        await StepFunAdapter(binary="/opt/step").run("hi")


@pytest.mark.asyncio
async def test_run_preserves_safe_timeout_error(monkeypatch):
    async def timeout(_invocation):
        raise NativeCliError("native CLI timed out after 37s")

    monkeypatch.setattr("src.llms.native_cli.stepfun.run_process", timeout)
    with pytest.raises(NativeCliError, match="timed out after 37s"):
        await StepFunAdapter(binary="/opt/step", timeout_seconds=37).run("hi")


@pytest.mark.asyncio
async def test_run_read_write_mode_retains_coding_approval_flags(monkeypatch):
    seen = []

    async def fake_run_process(invocation):
        seen.append(invocation)
        return ProcessOutput(
            stdout='[{"type":"result","result":"edited"}]',
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("src.llms.native_cli.stepfun.run_process", fake_run_process)
    result = await StepFunAdapter(binary="/opt/step", read_only=False).run("edit")

    assert result.text == "edited"
    assert seen[0].argv[:4] == ("/opt/step", "--mode", "json", "-p")
    assert "--approval-mode" in seen[0].argv
    assert seen[0].argv[seen[0].argv.index("--approval-mode") + 1] == "auto"
    assert "--non-interactive-approval" in seen[0].argv
    assert seen[0].argv[seen[0].argv.index("--non-interactive-approval") + 1] == "allow"


@pytest.mark.asyncio
async def test_read_only_worker_runs_read_commands_and_denies_writes(tmp_path):
    import shutil

    from src.llms.native_cli.failure import classify_failure
    from src.llms.native_cli.isolation import worker_isolation

    if not shutil.which("sandbox-exec"):
        pytest.skip("macOS Seatbelt unavailable")
    work = tmp_path / "workspace"
    work.mkdir()
    (work / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "add", "tracked.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture"],
        check=True,
        capture_output=True,
    )
    fake = work / "step-fake"
    fake.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "case \" $* \" in *\" --mode json \"*) ;; *) exit 80 ;; esac\n"
        "case \" $* \" in *\" --approval-mode auto \"*) ;; *) exit 81 ;; esac\n"
        "case \" $* \" in *\" --non-interactive-approval allow \"*) ;; *) exit 83 ;; esac\n"
        "git log -1 >/dev/null\n"
        "wc -l tracked.txt >/dev/null\n"
        "if touch forbidden.txt 2>/dev/null; then exit 82; fi\n"
        "printf '%s\\n' '{\"type\":\"result\",\"result\":\"read commands ran\"}'\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)

    with worker_isolation(
        workspace_id="stepfun-read-only",
        root=tmp_path / "isolation",
        workdir=work,
        env_names=set(),
        runtime_read_paths=(
            ("/Library/Developer/CommandLineTools",)
            if Path("/Library/Developer/CommandLineTools").is_dir()
            else ()
        ),
        read_only=True,
    ):
        adapter = StepFunAdapter(binary=str(fake), read_only=True)
        invocation = adapter.build_invocation("run git log and wc", cwd=str(work))
        assert invocation.argv[invocation.argv.index("--approval-mode") + 1] == "auto"
        assert (
            invocation.argv[invocation.argv.index("--non-interactive-approval") + 1]
            == "allow"
        )
        from src.llms.native_cli.process import run_process

        probe = await run_process(invocation, check=False)
        assert probe.returncode == 0, (probe.stdout, probe.stderr)
        result = await adapter.run(
            "run git log and wc", cwd=str(work)
        )

    assert result.text == "read commands ran"
    assert not (work / "forbidden.txt").exists()
    assert (
        classify_failure("stepfun", "No API key found").failure_class.value
        == "auth_failed"
    )


@pytest.mark.asyncio
async def test_safe_no_api_key_message_is_not_hidden(monkeypatch):
    async def missing_key(_invocation):
        raise NativeCliUnavailable("No API key found")

    monkeypatch.setattr("src.llms.native_cli.stepfun.run_process", missing_key)
    with pytest.raises(NativeCliUnavailable, match="No API key found"):
        await StepFunAdapter(binary="/opt/step").run("hi")


@pytest.mark.asyncio
async def test_fake_cli_safe_auth_diagnostic_drops_vendor_secret(tmp_path):
    from src.llms.native_cli.failure import classify_failure

    fake = tmp_path / "step-fake-auth"
    fake.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' 'No API key found; SECRET_SENTINEL' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)

    with pytest.raises(NativeCliUnavailable, match="^No API key found$") as error:
        await StepFunAdapter(binary=str(fake)).run("hi")

    assert "SECRET_SENTINEL" not in str(error.value)
    assert classify_failure("stepfun", str(error.value)).failure_class.value == "auth_failed"


@pytest.mark.asyncio
async def test_live_stepfun_text_smoke():
    if os.environ.get("CONDUCTOR_LIVE_STEPFUN") != "1":
        pytest.skip("set CONDUCTOR_LIVE_STEPFUN=1 to run one live StepFun call")

    isolated_cwd = os.path.abspath(".pytest_cache/stepfun-live-cwd")
    os.makedirs(isolated_cwd, exist_ok=True)
    try:
        result = await StepFunAdapter(timeout_seconds=90, cwd=isolated_cwd).run(
            "Reply with exactly: ok"
        )
    except Exception:
        pytest.fail("StepFun live smoke failed; details suppressed", pytrace=False)
    if result.text.strip() != "ok":
        pytest.fail("StepFun live smoke returned an unexpected response", pytrace=False)
