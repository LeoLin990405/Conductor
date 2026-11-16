from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.llms.native_cli.base import (
    NativeCliError,
    NativeCliUnavailable,
    NativeCliUsage,
)
from src.llms.native_cli.failure import FailureClass, classify_failure
from src.llms.native_cli.opencode import OpenCodeAdapter, parse_opencode_stream
from src.llms.native_cli.process import ProcessOutput
from src.llms.native_cli.registry import get_adapter
from src.server.services.native_runtime.registry import capabilities_for


def test_invocation_is_standalone_isolated_and_read_only(tmp_path):
    adapter = OpenCodeAdapter(
        binary="/opt/opencode",
        model="openai/test-model",
        config_dir=str(tmp_path / "config"),
    )

    invocation = adapter.build_invocation(
        "Continue safely", session_id="session-1", cwd=str(tmp_path / "work")
    )

    assert invocation.argv[:5] == (
        "/opt/opencode",
        "run",
        "--format",
        "json",
        "--standalone",
    )
    assert "--auto" not in invocation.argv
    assert "--dangerously-skip-permissions" not in invocation.argv
    assert invocation.argv[invocation.argv.index("--model") + 1] == "openai/test-model"
    assert invocation.argv[invocation.argv.index("--session") + 1] == "session-1"
    assert invocation.argv[invocation.argv.index("--dir") + 1] == str(tmp_path / "work")
    assert invocation.argv[-2:] == ("--", "Continue safely")
    assert invocation.env == {"OPENCODE_CONFIG_DIR": str(tmp_path / "config")}


def test_auto_is_an_explicit_write_opt_in(tmp_path):
    invocation = OpenCodeAdapter(
        binary="/opt/opencode", auto=True, config_dir=str(tmp_path)
    ).build_invocation("Write in the workspace")

    assert invocation.argv.count("--auto") == 1
    assert "--dangerously-skip-permissions" not in invocation.argv


def test_stream_preserves_the_final_answer_session_and_usage():
    stdout = "\n".join(
        [
            json.dumps({"type": "text", "part": {"text": "intermediate"}}),
            json.dumps({"type": "text", "part": {"text": "final"}, "sessionID": "s-1"}),
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {
                        "tokens": {
                            "input": 10,
                            "output": 4,
                            "total": 14,
                            "cache": {"read": 3, "write": 2},
                        }
                    },
                }
            ),
        ]
    )

    text, session, usage, events = parse_opencode_stream(stdout)

    assert (text, session) == ("final", "s-1")
    assert usage == NativeCliUsage(
        input_tokens=10,
        output_tokens=4,
        cache_read_tokens=3,
        cache_write_tokens=2,
        total_tokens=14,
    )
    assert len(events) == 3


@pytest.mark.asyncio
async def test_run_uses_and_removes_only_its_owned_config_dir(monkeypatch):
    seen = {}

    async def succeed(invocation):
        seen["config"] = invocation.env["OPENCODE_CONFIG_DIR"]
        assert Path(seen["config"]).is_dir()
        return ProcessOutput(
            stdout='{"type":"text","part":{"text":"ok"},"sessionID":"s-1"}\n',
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("src.llms.native_cli.opencode.run_process", succeed)
    adapter = OpenCodeAdapter(binary="/opt/opencode")
    result = await adapter.run("hello")

    assert result.text == "ok"
    assert result.session_id == "s-1"
    assert not Path(seen["config"]).exists()


@pytest.mark.asyncio
async def test_run_preserves_a_caller_supplied_config_dir(monkeypatch, tmp_path):
    config = tmp_path / "provided"
    config.mkdir()

    async def succeed(invocation):
        return ProcessOutput(
            stdout='{"type":"text","part":{"text":"ok"}}\n', stderr="", returncode=0
        )

    monkeypatch.setattr("src.llms.native_cli.opencode.run_process", succeed)
    adapter = OpenCodeAdapter(binary="/opt/opencode", config_dir=str(config))
    await adapter.run("hello")

    assert config.is_dir()


@pytest.mark.asyncio
async def test_timeout_classification_is_preserved(monkeypatch):
    async def timeout(_invocation):
        raise NativeCliError("native CLI timed out after 90s")

    monkeypatch.setattr("src.llms.native_cli.opencode.run_process", timeout)

    with pytest.raises(NativeCliError, match="timed out after 90s"):
        await OpenCodeAdapter(binary="/opt/opencode", timeout_seconds=90).run("hello")


def test_stream_auth_error_does_not_echo_diagnostics():
    stdout = json.dumps(
        {
            "type": "error",
            "error": {
                "name": "Unauthorized",
                "message": "unauthorized: sensitive-detail",
            },
        }
    )

    with pytest.raises(Exception, match="no usable credentials") as exc:
        parse_opencode_stream(stdout)

    assert "sensitive-detail" not in str(exc.value)


def test_opencode_is_registered_in_both_registries():
    assert isinstance(get_adapter("opencode"), OpenCodeAdapter)
    capabilities = capabilities_for("opencode")
    assert capabilities.session_resume is True
    assert capabilities.cache_usage is True
    assert capabilities.workspace_files is True
    assert capabilities.cancel is True
    assert capabilities.mcp is False
    assert capabilities.memory is False
    assert capabilities.skills is False


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["stream", "process"])
@pytest.mark.parametrize(
    ("diagnostic", "failure_class", "reason_code"),
    [
        ("authentication failed", FailureClass.AUTH_FAILED, "opencode_login_required"),
        ("quota exceeded", FailureClass.QUOTA_EXHAUSTED, "opencode_quota_exceeded"),
        ("context window exceeded", FailureClass.CONTEXT_EXHAUSTED, "opencode_context_overflow"),
    ],
)
async def test_failures_keep_their_class_without_leaking_diagnostics(
    monkeypatch, path, diagnostic, failure_class, reason_code
):
    secret = "raw-private-diagnostic"
    raw = f"{diagnostic}: {secret}"
    if path == "stream":
        stdout = json.dumps({"type": "error", "error": {"message": raw}})
        with pytest.raises(NativeCliError) as caught:
            parse_opencode_stream(stdout)
    else:

        async def fail(_invocation):
            raise NativeCliError(f"native CLI exited 1: {raw}")

        monkeypatch.setattr("src.llms.native_cli.opencode.run_process", fail)
        with pytest.raises(NativeCliError) as caught:
            await OpenCodeAdapter(binary="/opt/opencode").run("hello")

    message = str(caught.value)
    assert secret not in message
    if failure_class is FailureClass.AUTH_FAILED:
        assert isinstance(caught.value, NativeCliUnavailable)
        assert "no usable credentials" in message
    else:
        assert message == f"opencode failure: {reason_code}"
    downstream = classify_failure("opencode", message, exit_code=1)
    assert downstream.failure_class is failure_class
    assert downstream.reason_code == reason_code


def test_fresh_interpreter_classifies_opencode_reason_codes():
    source_root = Path(__file__).resolve().parents[3]
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(source_root)!r})\n"
        "from src.llms.native_cli.failure import classify_failure\n"
        "result = classify_failure('opencode', 'opencode_quota_exceeded', exit_code=1)\n"
        "print(json.dumps({'class': result.failure_class.value, 'reason': result.reason_code}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "class": "quota_exhausted",
        "reason": "opencode_quota_exceeded",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["stream", "process"])
@pytest.mark.parametrize(
    ("diagnostic", "safe_code", "failure_class", "normalized_code"),
    [
        (
            "insufficient_quota",
            "worker_quota_exhausted",
            FailureClass.QUOTA_EXHAUSTED,
            "opencode_quota_exceeded",
        ),
        (
            "prompt is too long",
            "worker_context_exhausted",
            FailureClass.CONTEXT_EXHAUSTED,
            "opencode_context_overflow",
        ),
        (
            "context_length_exceeded",
            "worker_context_exhausted",
            FailureClass.CONTEXT_EXHAUSTED,
            "opencode_context_overflow",
        ),
    ],
)
async def test_generic_failure_reason_codes_keep_their_class_downstream(
    monkeypatch, path, diagnostic, safe_code, failure_class, normalized_code
):
    secret = "raw-generic-diagnostic"
    raw = f"{diagnostic}: {secret}"
    if path == "stream":
        stdout = json.dumps({"type": "error", "error": {"message": raw}})
        with pytest.raises(NativeCliError) as caught:
            parse_opencode_stream(stdout)
    else:

        async def fail(_invocation):
            raise NativeCliError(f"native CLI exited 1: {raw}")

        monkeypatch.setattr("src.llms.native_cli.opencode.run_process", fail)
        with pytest.raises(NativeCliError) as caught:
            await OpenCodeAdapter(binary="/opt/opencode").run("hello")

    message = str(caught.value)
    assert message == f"opencode failure: {safe_code}"
    assert diagnostic not in message
    assert secret not in message
    downstream = classify_failure("opencode", message, exit_code=1)
    assert downstream.failure_class is failure_class
    assert downstream.reason_code == normalized_code


@pytest.mark.parametrize(
    ("generic_code", "failure_class", "normalized_code"),
    [
        ("worker_quota_exhausted", "quota_exhausted", "opencode_quota_exceeded"),
        ("worker_context_exhausted", "context_exhausted", "opencode_context_overflow"),
    ],
)
def test_fresh_interpreter_retains_generic_reason_code_classes(
    generic_code, failure_class, normalized_code
):
    source_root = Path(__file__).resolve().parents[3]
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(source_root)!r})\n"
        "from src.llms.native_cli.failure import classify_failure\n"
        f"result = classify_failure('opencode', {generic_code!r}, exit_code=1)\n"
        "print(json.dumps({'class': result.failure_class.value, 'reason': result.reason_code}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "class": failure_class,
        "reason": normalized_code,
    }
