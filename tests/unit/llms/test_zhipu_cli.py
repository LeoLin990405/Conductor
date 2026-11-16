"""Zhipu CLI adapter contracts; live usage is explicitly opt-in."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.llms.native_cli.base import NativeCliError, NativeCliResult, NativeCliUnavailable
from src.llms.native_cli.process import ProcessOutput
from src.llms.native_cli.zhipu import ZhipuCliAdapter, parse_zhipu_text

# The CLI stops at its turn budget, reports the exhausted budget as the turn
# resultType and still exits non-zero with an empty final answer.
SPENT_TURN_BUDGET_TRANSCRIPT = "\n".join(
    [
        json.dumps(
            {
                "type": "turn.completed",
                "sessionId": "sess-1",
                "payload": {"resultType": "error_max_turns"},
            }
        ),
        json.dumps(
            {
                "type": "result",
                "sessionId": "sess-1",
                "response": "",
                "isError": True,
            }
        ),
    ]
)


def test_invocation_uses_supported_read_only_wrapper_shape(tmp_path, monkeypatch):
    fake_binary = tmp_path / ".local" / "bin" / "zcode-cli"
    fake_binary.parent.mkdir(parents=True)
    fake_binary.touch()
    (fake_binary.parent / "zcode").touch()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    adapter = ZhipuCliAdapter()
    invocation = adapter.build_invocation("hello")

    assert invocation.argv == (
        str(fake_binary),
        "-p",
        "hello",
        "--output-format",
        "stream-json",
        "--model",
        "GLM-5.3",
        "--max-turns",
        "200",
        "--provider",
        "bigmodel-individual-coding-plan",
        "--permission-mode",
        "plan",
    )
    assert invocation.cwd == "/tmp"
    assert adapter.model == "GLM-5.3"
    assert adapter.max_turns == 200
    assert invocation.env is None

    monkeypatch.setenv("NATIVE_CLI_ZHIPU_MODEL", "GLM-5.3-Flash")
    monkeypatch.setenv("NATIVE_CLI_ZHIPU_MAX_TURNS", "64")
    assert ZhipuCliAdapter().build_invocation("model override").argv[-8:] == (
        "--model",
        "GLM-5.3-Flash",
        "--max-turns",
        "64",
        "--provider",
        "bigmodel-individual-coding-plan",
        "--permission-mode",
        "plan",
    )

    explicit = ZhipuCliAdapter(binary="/opt/zcode", cwd=str(tmp_path)).build_invocation(
        "next", cwd="/tmp/isolated"
    )
    assert explicit.argv == ("/opt/zcode", "-p", "next", "--mode", "plan")
    assert explicit.cwd == "/tmp"
    with pytest.raises(NativeCliError, match="does not support session"):
        adapter.build_invocation("next", session_id="session-1")


def test_legacy_zcode_is_used_when_clean_room_cli_is_unavailable(tmp_path, monkeypatch):
    fake_binary = tmp_path / ".local" / "bin" / "zcode"
    fake_binary.parent.mkdir(parents=True)
    fake_binary.touch()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "src.llms.native_cli.zhipu.shutil.which", lambda _name: None
    )

    invocation = ZhipuCliAdapter().build_invocation("hello")

    assert invocation.argv == (str(fake_binary), "-p", "hello", "--mode", "plan")


def test_binary_override_uses_single_provider_setting(monkeypatch):
    monkeypatch.setenv("NATIVE_CLI_ZHIPU_BIN", "/configured/zcode")
    assert ZhipuCliAdapter().binary == "/configured/zcode"


def test_new_cli_requires_json_result_but_legacy_keeps_text_compatibility():
    answer, usage = parse_zhipu_text(
        '{"type":"result","response":"answer","usage":{"inputTokens":3}}',
        require_json=True,
    )
    assert answer == "answer"
    assert usage.input_tokens == 3
    with pytest.raises(NativeCliError, match="non-JSON"):
        parse_zhipu_text("plain response", require_json=True)
    with pytest.raises(NativeCliError, match="request error"):
        parse_zhipu_text('{"isError":true,"response":"private detail"}', require_json=True)
    with pytest.raises(NativeCliError, match="unexpected JSON result"):
        parse_zhipu_text('{"sessionId":"session-only"}', require_json=True)
    with pytest.raises(NativeCliError, match="invalid usage"):
        parse_zhipu_text(
            '{"response":"answer","usage":{"inputTokens":"not-a-number"}}',
            require_json=True,
        )
    assert parse_zhipu_text("plain response")[0] == "plain response"


def test_clean_room_transcript_reads_the_final_result():
    from src.llms.native_cli.zhipu import parse_zhipu_transcript

    transcript = "\n".join(
        [
            json.dumps({"type": "session", "sessionId": "sess-1"}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "sessionId": "sess-1",
                    "payload": {"resultType": "success"},
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "sessionId": "sess-1",
                    "response": "answer",
                    "usage": {"inputTokens": 3},
                }
            ),
        ]
    )

    answer, usage = parse_zhipu_transcript(transcript)

    assert answer == "answer"
    assert usage.input_tokens == 3


def test_clean_room_transcript_reports_a_spent_turn_budget():
    from src.llms.native_cli.zhipu import parse_zhipu_transcript
    from src.llms.native_cli.zcode_transcript import ZCodeMaxTurnsError

    with pytest.raises(ZCodeMaxTurnsError, match="max_turns"):
        parse_zhipu_transcript(SPENT_TURN_BUDGET_TRANSCRIPT)


@pytest.mark.asyncio
async def test_a_spent_turn_budget_survives_a_nonzero_exit(monkeypatch, tmp_path):
    """The transcript decides the outcome, not the exit code.

    The CLI exits non-zero when it runs out of turns, so a check on the exit
    code before the transcript is read would report a generic failure and
    make the spent-budget error unreachable.
    """
    from src.llms.native_cli.zcode_transcript import ZCodeMaxTurnsError
    from src.llms.native_cli.zhipu import ZhipuCliAdapter

    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "max-turns.lock")
    runs = tmp_path / "runs.log"
    script = tmp_path / "zcode-cli"
    script.write_text(
        f"#!{sys.executable}\n"
        + "import json, sys\n"
        + f"open({str(runs)!r}, 'a').write('run\\n')\n"
        + "sys.stdout.write(json.dumps({'type': 'turn.completed', 'sessionId': 'sess-1',"
        + " 'payload': {'resultType': 'error_max_turns'}}) + \"\\n\")\n"
        + "sys.stdout.write(json.dumps({'type': 'result', 'sessionId': 'sess-1',"
        + " 'response': '', 'isError': True}) + \"\\n\")\n"
        + "sys.exit(1)\n"
    )
    script.chmod(0o755)

    with pytest.raises(ZCodeMaxTurnsError, match="max_turns"):
        await ZhipuCliAdapter(binary=str(script)).run("prompt")

    # A spent turn budget is not a provider failure, so it is not retried.
    assert runs.read_text().splitlines() == ["run"]


@pytest.mark.asyncio
async def test_a_nonzero_exit_without_an_answer_is_a_generic_failure(
    monkeypatch, tmp_path
):
    from src.llms.native_cli.zcode_transcript import ZCodeMaxTurnsError
    from src.llms.native_cli.zhipu import ZhipuCliAdapter

    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "failed.lock")
    script = tmp_path / "zcode-cli"
    script.write_text(
        f"#!{sys.executable}\n"
        + "import sys\n"
        + "sys.stderr.write('provider detail that must not leak\\n')\n"
        + "sys.exit(1)\n"
    )
    script.chmod(0o755)

    with pytest.raises(NativeCliError, match="details omitted") as error:
        await ZhipuCliAdapter(binary=str(script)).run("prompt")
    assert not isinstance(error.value, ZCodeMaxTurnsError)
    assert "provider detail" not in str(error.value)


@pytest.mark.asyncio
async def test_a_spent_turn_budget_is_not_read_as_a_credential_failure(
    monkeypatch, tmp_path
):
    """A spent budget stays a spent budget even with a noisy stderr.

    The transcript is the source of truth. A stderr line that happens to
    mention a credential must not turn a spent turn budget into a
    "not logged in" report.
    """
    from src.llms.native_cli.zcode_transcript import ZCodeMaxTurnsError
    from src.llms.native_cli.zhipu import ZhipuCliAdapter

    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "noisy.lock")
    script = tmp_path / "zcode-cli"
    script.write_text(
        f"#!{sys.executable}\n"
        + "import json, sys\n"
        + "sys.stderr.write('the saved credentials were rejected\\n')\n"
        + "sys.stdout.write(json.dumps({'type': 'turn.completed', 'sessionId': 'sess-1',"
        + " 'payload': {'resultType': 'error_max_turns'}}) + \"\\n\")\n"
        + "sys.stdout.write(json.dumps({'type': 'result', 'sessionId': 'sess-1',"
        + " 'response': '', 'isError': True}) + \"\\n\")\n"
        + "sys.exit(1)\n"
    )
    script.chmod(0o755)

    with pytest.raises(ZCodeMaxTurnsError, match="max_turns"):
        await ZhipuCliAdapter(binary=str(script)).run("prompt")


@pytest.mark.asyncio
async def test_a_credential_failure_is_still_classified_as_one(monkeypatch, tmp_path):
    """Reading the transcript first must not lose the credential diagnosis.

    A run that fails to authenticate produces no usable transcript, so the
    process diagnostic is what classifies it.
    """
    from src.llms.native_cli.zhipu import ZhipuCliAdapter

    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "auth.lock")
    script = tmp_path / "zcode-cli"
    script.write_text(
        f"#!{sys.executable}\n"
        + "import sys\n"
        + "sys.stderr.write('Error: not logged in\\n')\n"
        + "sys.exit(1)\n"
    )
    script.chmod(0o755)

    with pytest.raises(NativeCliUnavailable, match="not logged in"):
        await ZhipuCliAdapter(binary=str(script)).run("prompt")


def _make_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    main = tmp_path / "main"
    linked = tmp_path / "linked"
    main.mkdir()
    subprocess.run(["git", "init", str(main)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-b", "write-test", str(linked)],
        check=True,
        capture_output=True,
    )
    return main, linked


def test_write_capable_modes_require_a_real_linked_worktree(tmp_path, monkeypatch):
    main, linked = _make_linked_worktree(tmp_path)
    adapter = ZhipuCliAdapter(binary="/opt/zcode-cli", permission_mode="edit")

    invocation = adapter.build_invocation("edit file", cwd=str(linked))

    assert invocation.cwd == str(linked.resolve())
    assert invocation.argv[-2:] == ("--cwd", str(linked.resolve()))
    assert "--permission-mode" in invocation.argv
    assert invocation.argv[invocation.argv.index("--permission-mode") + 1] == "edit"

    alias = tmp_path / "linked-alias"
    alias.symlink_to(main, target_is_directory=True)
    for invalid_path in (main, alias, tmp_path / "missing"):
        with pytest.raises(NativeCliError, match="linked Git worktree"):
            adapter.build_invocation("edit file", cwd=str(invalid_path))

    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_DIR", str(main / ".git"))
    with pytest.raises(NativeCliError, match="linked Git worktree"):
        adapter.build_invocation("edit file", cwd=str(plain))

    monkeypatch.delenv("GIT_DIR")
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    for invalid_path in (linked / "subdirectory", bare):
        if invalid_path == linked / "subdirectory":
            invalid_path.mkdir()
        with pytest.raises(NativeCliError, match="linked Git worktree"):
            adapter.build_invocation("edit file", cwd=str(invalid_path))


def test_write_capable_legacy_cli_is_rejected(tmp_path):
    with pytest.raises(NativeCliError, match="require the zcode-cli"):
        ZhipuCliAdapter(binary="/opt/zcode", permission_mode="edit").build_invocation(
            "edit file", cwd=str(tmp_path)
        )


@pytest.mark.parametrize("mode", ["build", "edit", "yolo", "guarded"])
def test_write_capable_modes_require_explicit_worktree_at_invocation(tmp_path, mode):
    binary = tmp_path / "zcode-cli"
    binary.touch()
    adapter = ZhipuCliAdapter(binary=str(binary), permission_mode=mode)

    with pytest.raises(NativeCliError, match="explicit linked Git worktree cwd"):
        adapter.build_invocation("please edit my files")


@pytest.mark.asyncio
async def test_new_cli_run_parses_json_and_explicitly_uses_plan_mode(monkeypatch, tmp_path):
    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "json.lock")
    seen = []

    async def fake_run_process(invocation, **_kwargs):
        seen.append(invocation)
        return ProcessOutput(
            stdout='{"response":"answer","usage":{"inputTokens":2}}',
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", fake_run_process)
    result = await ZhipuCliAdapter(binary="/opt/zcode-cli", model="GLM-5.2").run("prompt")

    assert result.text == "answer"
    assert result.usage.input_tokens == 2
    assert seen[0].argv[1:3] == ("-p", "prompt")
    assert seen[0].argv[seen[0].argv.index("--permission-mode") + 1] == "plan"
    assert seen[0].argv[seen[0].argv.index("--model") + 1] == "GLM-5.2"


@pytest.mark.asyncio
async def test_write_run_revalidates_linked_worktree_before_process(monkeypatch, tmp_path):
    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "write.lock")
    _main, linked = _make_linked_worktree(tmp_path)
    seen = []

    async def fake_run_process(invocation, **_kwargs):
        seen.append(invocation)
        return ProcessOutput(stdout='{"response":"done"}', stderr="", returncode=0)

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", fake_run_process)
    result = await ZhipuCliAdapter(
        binary="/opt/zcode-cli", permission_mode="edit", cwd=str(linked)
    ).run("edit the file")

    assert result.text == "done"
    assert len(seen) == 1
    assert seen[0].cwd == str(linked.resolve())
    assert seen[0].argv[seen[0].argv.index("--permission-mode") + 1] == "edit"


def test_parser_keeps_plain_text_and_known_json_envelopes_safe():
    assert parse_zhipu_text("  hello  ")[0] == "hello"
    assert parse_zhipu_text("Example: the service returned 429 yesterday.")[0] == (
        "Example: the service returned 429 yesterday."
    )
    assert parse_zhipu_text('{"response":"answer","usage":{"input_tokens":3}}')[0] == "answer"
    with pytest.raises(NativeCliError, match="request error") as error:
        parse_zhipu_text('{"error":{"code":"bad_request","message":"private detail"}}')
    assert "private detail" not in str(error.value)
    with pytest.raises(NativeCliError, match="HTTP 429"):
        parse_zhipu_text('{"error":{"code":"429","message":"private detail"}}')
    with pytest.raises(NativeCliError, match="HTTP 429"):
        parse_zhipu_text('{"status":429,"message":"private detail"}')
    with pytest.raises(NativeCliError, match="request error"):
        parse_zhipu_text('{"status_code":503,"message":"private detail"}')
    with pytest.raises(NativeCliError, match="request error"):
        parse_zhipu_text('{"status":"ERROR","message":"private detail"}')
    with pytest.raises(NativeCliError, match="request error"):
        parse_zhipu_text('{"type":"error","message":"private detail"}')
    with pytest.raises(NativeCliUnavailable, match="not logged in"):
        parse_zhipu_text('{"error":{"code":"unauthorized","message":"private detail"}}')


@pytest.mark.asyncio
async def test_run_returns_text_and_does_not_leak_environment_or_diagnostics(monkeypatch, tmp_path):
    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "zhipu.lock")
    seen = []

    async def fake_run_process(invocation, **_kwargs):
        seen.append(invocation)
        return ProcessOutput(stdout=" answer\n", stderr="", returncode=0)

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", fake_run_process)
    result = await ZhipuCliAdapter(binary="/opt/zcode").run("prompt")

    assert isinstance(result, NativeCliResult)
    assert result.provider == "zhipu"
    assert result.text == "answer"
    assert result.session_id is None
    assert result.usage.input_tokens == 0
    assert seen[0].argv == ("/opt/zcode", "-p", "prompt", "--mode", "plan")
    assert seen[0].cwd == "/tmp"
    assert seen[0].env is None


@pytest.mark.asyncio
async def test_retries_only_actual_429_or_502_with_bounded_exponential_backoff(monkeypatch, tmp_path):
    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "retry.lock")
    calls = 0
    delays = []

    async def limited_twice(_invocation, **_kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise NativeCliError("native CLI exited 1: HTTP 429 Too Many Requests: private")
        return ProcessOutput(stdout="ok", stderr="", returncode=0)

    async def record_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", limited_twice)
    monkeypatch.setattr("src.llms.native_cli.zhipu.asyncio.sleep", record_sleep)
    result = await ZhipuCliAdapter(binary="/opt/zcode").run("prompt")

    assert result.text == "ok"
    assert calls == 3
    assert delays == [1.0, 2.0]


@pytest.mark.asyncio
async def test_retries_http_502_but_not_successful_answer_mentioning_502(monkeypatch, tmp_path):
    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "502.lock")
    calls = 0
    delays = []

    async def bad_gateway_once(_invocation, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ProcessOutput(stdout='{"status":502,"message":"private"}', stderr="", returncode=0)
        return ProcessOutput(stdout="The report mentions 502 briefly.", stderr="", returncode=0)

    async def record_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", bad_gateway_once)
    monkeypatch.setattr("src.llms.native_cli.zhipu.asyncio.sleep", record_sleep)
    result = await ZhipuCliAdapter(binary="/opt/zcode").run("prompt")

    assert calls == 2
    assert delays == [1.0]
    assert result.text == "The report mentions 502 briefly."


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic", ["API Error: 429", "Error: 429"])
async def test_common_cli_429_diagnostics_retry(monkeypatch, tmp_path, diagnostic):
    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "common-429.lock")
    calls = 0

    async def limit_once(_invocation, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise NativeCliError(f"{diagnostic}; private detail")
        return ProcessOutput(stdout="ok", stderr="", returncode=0)

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", limit_once)
    monkeypatch.setattr("src.llms.native_cli.zhipu.asyncio.sleep", no_wait)
    result = await ZhipuCliAdapter(binary="/opt/zcode").run("prompt")

    assert calls == 2
    assert result.text == "ok"


@pytest.mark.asyncio
async def test_retries_zero_exit_json_429_but_not_answer_text_mentioning_429(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "json-429.lock")
    calls = 0

    async def json_limit_once(_invocation, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ProcessOutput(
                stdout='{"error":{"code":"429","message":"private"}}',
                stderr="",
                returncode=0,
            )
        return ProcessOutput(
            stdout="The service returned 429 yesterday.", stderr="", returncode=0
        )

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", json_limit_once)
    monkeypatch.setattr("src.llms.native_cli.zhipu.asyncio.sleep", no_wait)
    result = await ZhipuCliAdapter(binary="/opt/zcode").run("prompt")

    assert calls == 2
    assert result.text == "The service returned 429 yesterday."


@pytest.mark.asyncio
async def test_429_exhaustion_is_safe_and_non_429_is_not_retried(monkeypatch, tmp_path):
    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "errors.lock")
    calls = 0

    async def rate_limit(_invocation, **_kwargs):
        nonlocal calls
        calls += 1
        raise NativeCliError("Error code: 429; private diagnostic")

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", rate_limit)
    monkeypatch.setattr("src.llms.native_cli.zhipu.asyncio.sleep", no_wait)
    with pytest.raises(NativeCliError, match="bounded retries") as error:
        await ZhipuCliAdapter(binary="/opt/zcode").run("prompt")
    assert calls == 3
    assert "private diagnostic" not in str(error.value)

    calls = 0

    async def bad_request(_invocation, **_kwargs):
        nonlocal calls
        calls += 1
        raise NativeCliError("400 Bad Request; secret-bearing provider detail")

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", bad_request)
    with pytest.raises(NativeCliError, match="details omitted") as error:
        await ZhipuCliAdapter(binary="/opt/zcode").run("prompt")
    assert calls == 1
    assert "secret-bearing" not in str(error.value)


@pytest.mark.asyncio
async def test_lock_serializes_adapter_instances_and_releases_after_cancellation(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "shared.lock")
    entered = asyncio.Event()
    second_entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum_active = 0
    calls = 0

    async def held_call(_invocation, **_kwargs):
        nonlocal active, maximum_active, calls
        calls += 1
        active += 1
        maximum_active = max(maximum_active, active)
        if calls == 1:
            entered.set()
        else:
            second_entered.set()
        try:
            await release.wait()
            return ProcessOutput(stdout="ok", stderr="", returncode=0)
        finally:
            active -= 1

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", held_call)
    first = asyncio.create_task(ZhipuCliAdapter(binary="/opt/zcode").run("one"))
    await entered.wait()
    second = asyncio.create_task(ZhipuCliAdapter(binary="/opt/zcode").run("two"))
    await asyncio.sleep(0.08)
    assert maximum_active == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.wait_for(second_entered.wait(), timeout=1)
    release.set()
    assert (await second).text == "ok"
    assert maximum_active == 1


@pytest.mark.asyncio
async def test_timeout_while_waiting_or_running_releases_provider_lock(monkeypatch, tmp_path):
    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "timeout.lock")
    entered = asyncio.Event()

    async def hold_forever(_invocation, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", hold_forever)
    adapter = ZhipuCliAdapter(binary="/opt/zcode", timeout_seconds=0.05)
    with pytest.raises(NativeCliError, match="timed out after 0.05s"):
        await adapter.run("prompt")
    assert entered.is_set()

    async def succeed(_invocation, **_kwargs):
        return ProcessOutput(stdout="ok", stderr="", returncode=0)

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", succeed)
    assert (await ZhipuCliAdapter(binary="/opt/zcode").run("next")).text == "ok"


@pytest.mark.asyncio
async def test_cancelling_while_waiting_closes_unlocked_descriptor(monkeypatch, tmp_path):
    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "wait.lock")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold(_invocation, **_kwargs):
        entered.set()
        await release.wait()
        return ProcessOutput(stdout="ok", stderr="", returncode=0)

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", hold)
    owner = asyncio.create_task(ZhipuCliAdapter(binary="/opt/zcode").run("owner"))
    await entered.wait()
    waiting = asyncio.create_task(ZhipuCliAdapter(binary="/opt/zcode").run("waiting"))
    await asyncio.sleep(0.08)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release.set()
    assert (await owner).text == "ok"

    async def succeed(_invocation, **_kwargs):
        return ProcessOutput(stdout="available", stderr="", returncode=0)

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", succeed)
    assert (await ZhipuCliAdapter(binary="/opt/zcode").run("after")).text == "available"


@pytest.mark.asyncio
async def test_live_zhipu_text_smoke_once_when_explicitly_enabled(monkeypatch):
    if os.environ.get("CONDUCTOR_LIVE_ZHIPU") != "1":
        pytest.skip("set CONDUCTOR_LIVE_ZHIPU=1 to run one live Zhipu call")

    # Keep the opt-in smoke to one launch even if it is rate limited.
    monkeypatch.setattr("src.llms.native_cli.zhipu._MAX_ATTEMPTS", 1)
    try:
        result = await ZhipuCliAdapter(timeout_seconds=90).run(
            "Reply with exactly: ok"
        )
    except Exception:
        pytest.fail("Zhipu live smoke failed; details suppressed", pytrace=False)
    if result.text.strip() != "ok":
        pytest.fail("Zhipu live smoke returned an unexpected response", pytrace=False)


def _failed_turn_transcript(error: dict) -> str:
    return "\n".join(
        [
            json.dumps({"type": "turn.failed", "payload": {"error": error}}),
            json.dumps({"type": "result", "response": "", "isError": True}),
        ]
    )


@pytest.mark.asyncio
async def test_a_failed_turn_with_http_502_is_retried(monkeypatch, tmp_path):
    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "t502.lock")
    calls = 0
    delays = []

    async def flaky(_invocation, **_kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            return ProcessOutput(
                stdout=_failed_turn_transcript(
                    {"name": "APIError", "message": "HTTP 502 Bad Gateway"}
                ),
                stderr="",
                returncode=1,
            )
        return ProcessOutput(
            stdout=json.dumps({"type": "result", "response": "done"}),
            stderr="",
            returncode=0,
        )

    async def record_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", flaky)
    monkeypatch.setattr("src.llms.native_cli.zhipu.asyncio.sleep", record_sleep)
    monkeypatch.setattr("src.llms.native_cli.zhipu._is_clean_room_cli", lambda _b: True)
    result = await ZhipuCliAdapter(binary="/opt/zcode-cli").run("prompt")

    assert result.text == "done"
    assert calls == 3
    assert delays == [1.0, 2.0]


@pytest.mark.asyncio
async def test_a_failed_turn_with_an_invalid_key_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "tkey.lock")

    async def rejected(_invocation, **_kwargs):
        return ProcessOutput(
            stdout=_failed_turn_transcript(
                {"name": "AuthError", "message": "invalid api key"}
            ),
            stderr="",
            returncode=1,
        )

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", rejected)
    monkeypatch.setattr("src.llms.native_cli.zhipu._is_clean_room_cli", lambda _b: True)
    with pytest.raises(NativeCliUnavailable):
        await ZhipuCliAdapter(binary="/opt/zcode-cli").run("prompt")
