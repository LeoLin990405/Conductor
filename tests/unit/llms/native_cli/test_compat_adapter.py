"""Secret handling, invocation and cleanup for the compat DSH adapter."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from src.config.compat import CompatWorkerConfig
from src.llms.native_cli.base import NativeCliError
from src.llms.native_cli.compat import CompatDshAdapter
from src.llms.native_cli.process import ProcessOutput

ENV_NAME = "W9A_TEST_API_KEY"
SENTINEL = "sk-" "w9a-test-only-sentinel"
SAMPLE = "\n".join(
    [
        json.dumps({"type": "session", "sessionId": "compat-session-1"}),
        json.dumps({"type": "text", "text": "compat answer"}),
        json.dumps(
            {
                "type": "status",
                "phase": "step_end",
                "usage": {
                    "inputTokens": 82,
                    "outputTokens": 17,
                    "cacheReadTokens": 41,
                    "totalTokens": 140,
                },
            }
        ),
        json.dumps({"type": "final", "text": "compat answer"}),
    ]
)


def _config(api_key_env: str = ENV_NAME) -> CompatWorkerConfig:
    return CompatWorkerConfig(
        name="qwen-compat",
        protocol="openai-completions",
        base_url="https://example.invalid/v1",
        model="w9a-fake-model",
        api_key_env=api_key_env,
        context_window_tokens=131072,
        review_capable=False,
    )


def _adapter(home: Path, work: Path) -> CompatDshAdapter:
    return CompatDshAdapter(
        _config(),
        binary="/opt/dsh",
        harness_home=str(home / ".dsh"),
        timeout_seconds=12,
        cwd=str(work),
    )


def _overlay_path(argv: tuple[str, ...]) -> Path:
    assert "--patch" in argv
    return Path(argv[argv.index("--patch") + 1])


@pytest.mark.asyncio
async def test_overlay_names_env_without_secret_and_is_removed_after_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    overlay = None

    async def fake_run(invocation):
        nonlocal overlay
        overlay = _overlay_path(invocation.argv)
        assert overlay.is_file()
        assert overlay.resolve().is_relative_to(home.resolve())
        assert os.stat(overlay).st_mode & 0o777 == 0o600
        text = overlay.read_text(encoding="utf-8")
        assert f"apiKeyEnv: {json.dumps(ENV_NAME)}" in text
        assert SENTINEL not in text
        assert SENTINEL not in invocation.argv
        assert invocation.env[ENV_NAME] == SENTINEL
        assert invocation.argv[-2:] == ("--", "hello")
        assert invocation.cwd == str(work)
        return ProcessOutput(stdout=SAMPLE, stderr="", returncode=0)

    monkeypatch.setattr("src.llms.native_cli.compat.run_process", fake_run)
    with caplog.at_level(logging.DEBUG):
        result = await _adapter(home, work).run("hello")

    assert overlay is not None
    assert not overlay.exists()
    assert result.provider == "qwen-compat"
    assert result.text == "compat answer"
    assert result.usage.input_tokens == 82
    assert result.usage.cache_read_tokens == 41
    assert SENTINEL not in caplog.text
    assert SENTINEL not in repr(result.raw)


@pytest.mark.asyncio
async def test_errors_logs_and_failed_run_cleanup_never_expose_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    overlay = None

    async def fail_with_secret(invocation):
        nonlocal overlay
        overlay = _overlay_path(invocation.argv)
        assert overlay.is_file()
        raise NativeCliError(f"provider echoed {SENTINEL}")

    monkeypatch.setattr("src.llms.native_cli.compat.run_process", fail_with_secret)
    with caplog.at_level(logging.DEBUG), pytest.raises(NativeCliError) as caught:
        await _adapter(home, work).run("hello")

    assert SENTINEL not in str(caught.value)
    assert SENTINEL not in caplog.text
    assert overlay is not None
    assert not overlay.exists()


def test_review_capability_is_always_disabled(tmp_path: Path) -> None:
    config = _config()

    assert config.review_capable is False
    assert CompatDshAdapter(config, binary="/opt/dsh").review_capable is False


def test_adapter_defensively_rejects_an_adapter_controlled_env_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(NativeCliError):
        CompatDshAdapter(
            _config("HOME"),
            binary="/opt/dsh",
            harness_home=str(home / ".dsh"),
            cwd=str(tmp_path),
        ).build_invocation("hello")

    assert list(home.glob("**/.compat-*.yml")) == []


@pytest.mark.asyncio
async def test_stdout_secret_is_redacted_before_text_and_raw_events_are_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    overlay = None

    async def fake_run(invocation):
        nonlocal overlay
        overlay = _overlay_path(invocation.argv)
        events = [json.loads(line) for line in SAMPLE.splitlines()]
        events.insert(2, {"type": "thinking", "text": SENTINEL})
        stdout = "\n".join(json.dumps(event) for event in events).replace(
            "compat answer", f"compat answer {SENTINEL}"
        )
        return ProcessOutput(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr("src.llms.native_cli.compat.run_process", fake_run)
    result = await _adapter(home, work).run("hello")

    assert result.text == "compat answer [redacted]"
    _assert_raw_contract(result)
    original_final = next(event for event in result.raw if event["type"] == "final")
    assert original_final["text"] == "compat answer [redacted]"
    assert SENTINEL not in result.text
    assert SENTINEL not in repr(result.raw)
    assert overlay is not None
    assert not overlay.exists()


@pytest.mark.asyncio
async def test_malformed_secret_shaped_usage_is_a_sanitized_error_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    overlay = None

    async def fake_run(invocation):
        nonlocal overlay
        overlay = _overlay_path(invocation.argv)
        stdout = SAMPLE.replace('"inputTokens": 82', f'"inputTokens": "{SENTINEL}"')
        return ProcessOutput(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr("src.llms.native_cli.compat.run_process", fake_run)
    with caplog.at_level(logging.DEBUG), pytest.raises(NativeCliError) as caught:
        await _adapter(home, work).run("hello")

    assert SENTINEL not in str(caught.value)
    assert SENTINEL not in caplog.text
    assert overlay is not None
    assert not overlay.exists()


def _sample_with_text(text: str) -> str:
    events = [json.loads(line) for line in SAMPLE.splitlines()]
    for event in events:
        if event.get("type") in {"text", "final"}:
            event["text"] = text
    return "\n".join(json.dumps(event) for event in events)


async def _run_with_stdout(
    stdout: str,
    secret: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(ENV_NAME, secret)
    overlay = None

    async def fake_run(invocation):
        nonlocal overlay
        overlay = _overlay_path(invocation.argv)
        assert overlay.is_file()
        return ProcessOutput(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr("src.llms.native_cli.compat.run_process", fake_run)
    result = await _adapter(home, work).run("hello")
    assert overlay is not None
    assert not overlay.exists()
    return result


def _string_leaves(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _string_leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _string_leaves(item)


def _assert_raw_contract(result) -> None:
    assert result.raw[-1] == {"type": "compat_final_text", "text": result.text}
    assert sum(event.get("type") == "compat_final_text" for event in result.raw) == 1
    for event in result.raw[:-1]:
        if event.get("type") in {"text", "thinking"}:
            assert event == {"type": event["type"], "content": "[redacted]"}


@pytest.mark.asyncio
async def test_json_escaped_quote_and_backslash_key_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = 'sk-' 'w9a-quote-"-\\-slash'
    stdout = _sample_with_text(f"model output {secret}")
    assert secret not in stdout
    assert json.dumps(secret)[1:-1] in stdout

    result = await _run_with_stdout(stdout, secret, tmp_path, monkeypatch)

    assert result.text == "model output [redacted]"
    _assert_raw_contract(result)
    original_final = next(event for event in result.raw if event["type"] == "final")
    assert original_final["text"] == "model output [redacted]"
    assert secret not in repr(result.raw)


@pytest.mark.asyncio
async def test_json_unicode_escaped_key_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-" "w9a-unicode-escapes"
    placeholder = "W9A_UNICODE_PLACEHOLDER"
    escaped = "".join(f"\\u{ord(char):04x}" for char in secret)
    stdout = SAMPLE.replace("compat answer", f"compat answer {placeholder}").replace(
        placeholder, escaped
    )
    assert secret not in stdout
    assert "\\u0073\\u006b" in stdout

    result = await _run_with_stdout(stdout, secret, tmp_path, monkeypatch)

    assert result.text == "compat answer [redacted]"
    _assert_raw_contract(result)
    original_final = next(event for event in result.raw if event["type"] == "final")
    assert original_final["text"] == "compat answer [redacted]"
    assert secret not in repr(result.raw)


@pytest.mark.asyncio
async def test_split_key_is_redacted_after_text_chunks_are_concatenated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-" "w9a-split-secret"
    events = [json.loads(line) for line in SAMPLE.splitlines()]
    events[1]["text"] = f"before {secret[:12]}"
    events.insert(2, {"type": "text", "text": f"{secret[12:]} after"})
    events[-1]["text"] = ""
    stdout = "\n".join(json.dumps(event) for event in events)
    assert secret not in stdout

    result = await _run_with_stdout(stdout, secret, tmp_path, monkeypatch)

    assert result.text == "before [redacted] after"
    _assert_raw_contract(result)
    original_final = next(event for event in result.raw if event["type"] == "final")
    assert original_final["text"] == ""
    assert secret not in repr(result.raw)


@pytest.mark.asyncio
async def test_session_and_nested_raw_string_leaves_are_recursively_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-" "w9a-nested-secret"
    events = [json.loads(line) for line in SAMPLE.splitlines()]
    events[0]["sessionId"] = secret
    events[0]["nested"] = {"values": [secret]}
    events[-1]["text"] = "safe"
    events[-1]["nested"] = {"values": [secret, {"inner": secret}]}
    stdout = "\n".join(json.dumps(event) for event in events)

    result = await _run_with_stdout(stdout, secret, tmp_path, monkeypatch)

    assert result.text == "safe"
    assert result.session_id == "[redacted]"
    _assert_raw_contract(result)
    assert result.raw[0]["nested"] == {"values": ["[redacted]"]}
    original_final = next(event for event in result.raw if event["type"] == "final")
    assert original_final["nested"] == {
        "values": ["[redacted]", {"inner": "[redacted]"}]
    }
    leaves = list(_string_leaves(result.raw))
    assert leaves.count("[redacted]") == 5
    assert all(secret not in leaf for leaf in leaves)


@pytest.mark.asyncio
async def test_error_path_redacts_key_without_leaking_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "sk-" "w9a-error-secret"
    diagnostic = "private-provider-trace"
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(ENV_NAME, secret)
    overlay = None

    async def fail_with_secret(invocation):
        nonlocal overlay
        overlay = _overlay_path(invocation.argv)
        assert overlay.is_file()
        raise NativeCliError(f"{diagnostic}: {secret}")

    monkeypatch.setattr("src.llms.native_cli.compat.run_process", fail_with_secret)
    with caplog.at_level(logging.DEBUG), pytest.raises(NativeCliError) as caught:
        await _adapter(home, work).run("hello")

    assert "[redacted]" in str(caught.value)
    assert secret not in str(caught.value)
    assert diagnostic not in str(caught.value)
    assert secret not in caplog.text
    assert diagnostic not in caplog.text
    assert overlay is not None
    assert not overlay.exists()


@pytest.mark.asyncio
async def test_split_text_events_are_collapsed_and_raw_replay_cannot_rejoin_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = "sk-" "w9a-fix2-split-credential"
    events = [json.loads(line) for line in SAMPLE.splitlines()]
    events[1:2] = [
        {"type": "text", "text": f"before {credential[:8]}"},
        {"type": "text", "text": credential[8:17]},
        {"type": "text", "text": f"{credential[17:]} after"},
    ]
    events[-1]["text"] = ""
    stdout = "\n".join(json.dumps(event) for event in events)
    assert credential not in stdout

    result = await _run_with_stdout(stdout, credential, tmp_path, monkeypatch)

    assert result.text == "before [redacted] after"
    text_events = [event for event in result.raw if event.get("type") == "text"]
    assert text_events == [{"type": "text", "content": "[redacted]"}] * 3
    assert result.raw[-1] == {"type": "compat_final_text", "text": result.text}
    assert sum(event.get("type") == "compat_final_text" for event in result.raw) == 1

    replayed = [
        json.loads(line)
        for line in ("\n".join(json.dumps(event) for event in result.raw)).splitlines()
    ]
    ordered_fields = "".join(
        value
        for event in replayed
        for value in event.values()
        if isinstance(value, str)
    )
    nested_leaves = "".join(_string_leaves(replayed))
    assert credential not in ordered_fields
    assert credential not in nested_leaves


@pytest.mark.asyncio
async def test_incoming_compat_final_text_fragments_are_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = "sk-" "w9a-injected-final-fragment"
    events = [json.loads(line) for line in SAMPLE.splitlines()]
    events[1]["text"] = f"before {credential} after"
    events[2:2] = [
        {"type": "compat_final_text", "text": credential[:14]},
        {"type": "compat_final_text", "text": credential[14:]},
    ]
    events[-1]["text"] = ""
    stdout = "\n".join(json.dumps(event) for event in events)

    result = await _run_with_stdout(stdout, credential, tmp_path, monkeypatch)

    assert result.text == "before [redacted] after"
    _assert_raw_contract(result)
    synthetic = [
        event for event in result.raw if event.get("type") == "compat_final_text"
    ]
    assert synthetic == [{"type": "compat_final_text", "text": result.text}]
    original_final = next(event for event in result.raw if event["type"] == "final")
    assert original_final["text"] == ""

    replayed = [
        json.loads(line)
        for line in ("\n".join(json.dumps(event) for event in result.raw)).splitlines()
    ]
    ordered_fields = "".join(
        value
        for event in replayed
        for value in event.values()
        if isinstance(value, str)
    )
    nested_leaves = "".join(_string_leaves(replayed))
    assert credential not in ordered_fields
    assert credential not in nested_leaves
