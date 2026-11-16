"""Native agent CLI adapter contract, parser and session reuse."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from src.llms.native_cli.base import (
    CliInvocation,
    NativeCliError,
    NativeCliResult,
    NativeCliUsage,
)
from src.llms.native_cli.deepseek import DeepSeekDshAdapter
from src.llms.native_cli.events import parse_ndjson
from src.llms.native_cli.process import run_process
from src.llms.native_cli.sessions import NativeCliSessionStore, run_with_session

SAMPLE = "\n".join(
    [
        json.dumps({"type": "session", "sessionId": "session-1", "cwd": "/tmp"}),
        json.dumps({"type": "status", "phase": "step_start", "turn": 1, "step": 1}),
        json.dumps({"type": "text", "text": "ok"}),
        json.dumps(
            {
                "type": "status",
                "phase": "step_end",
                "turn": 1,
                "step": 1,
                "usage": {
                    "inputTokens": 224,
                    "outputTokens": 2,
                    "cacheReadTokens": 9344,
                    "cacheWriteTokens": 0,
                    "totalTokens": 9570,
                },
            }
        ),
        json.dumps({"type": "final", "text": "ok"}),
    ]
)


class TestParser:
    def test_reads_session_final_and_cache_usage(self):
        parsed = parse_ndjson(SAMPLE + "\nnot-json\n")
        assert parsed.session_id == "session-1"
        assert parsed.text == "ok"
        assert parsed.usage.cache_read_tokens == 9344
        assert parsed.usage.input_tokens == 224
        assert len(parsed.events) == 5

    def test_falls_back_to_text_chunks_without_final(self):
        parsed = parse_ndjson(
            "\n".join(
                [
                    json.dumps({"type": "session", "sessionId": "s"}),
                    json.dumps({"type": "text", "text": "hel"}),
                    json.dumps({"type": "text", "text": "lo"}),
                ]
            )
        )
        assert parsed.text == "hello"

    def test_no_events_is_a_loud_failure(self):
        with pytest.raises(NativeCliError, match="no machine-readable"):
            parse_ndjson("")


class TestUsage:
    def test_cache_ratio_separates_cached_input(self):
        result = NativeCliResult(
            provider="deepseek-dsh",
            text="ok",
            usage=NativeCliUsage(cache_read_tokens=900, input_tokens=100),
        )
        assert result.cache_read_ratio == pytest.approx(0.9)


class TestDeepSeekAdapter:
    def test_builds_headless_json_invocation_with_session(self):
        adapter = DeepSeekDshAdapter(
            binary="/opt/dsh", harness_home="/tmp/dsh-home", cwd="/tmp"
        )
        invocation = adapter.build_invocation(
            "hello", session_id="session-1", cwd="/workspace"
        )
        assert invocation.argv[:7] == (
            "/bin/sh",
            "-c",
            invocation.argv[2],
            "conductor-dsh",
            "/opt/dsh",
            "headless",
            "--json",
        )
        assert invocation.argv[7:] == (
            "--session-id", "session-1", "--", "hello"
        )
        assert '"$HOME/.config/deepseek/env"' in invocation.argv[2]
        assert "set +x" in invocation.argv[2]
        assert invocation.stdin == ""
        assert invocation.cwd == "/workspace"
        assert invocation.env["DSH_HOME"] == "/tmp/dsh-home"

    def test_missing_binary_is_a_typed_error(self, monkeypatch):
        monkeypatch.delenv("NATIVE_CLI_DSH_BIN", raising=False)
        monkeypatch.setattr("shutil.which", lambda _: None)
        from src.llms.native_cli.base import NativeCliUnavailable

        with pytest.raises(NativeCliUnavailable, match="not found"):
            DeepSeekDshAdapter().binary

    @pytest.mark.asyncio
    async def test_run_parses_process_output(self, monkeypatch):
        async def fake_run(invocation: CliInvocation):
            assert invocation.argv[-1] == "hello"
            assert invocation.stdin == ""
            from src.llms.native_cli.process import ProcessOutput

            return ProcessOutput(stdout=SAMPLE, stderr="", returncode=0)

        monkeypatch.setattr(
            "src.llms.native_cli.deepseek.run_process", fake_run
        )
        adapter = DeepSeekDshAdapter(binary="/opt/dsh")
        result = await adapter.run("hello")
        assert result.text == "ok"
        assert result.session_id == "session-1"
        assert result.usage.cache_read_tokens == 9344


class TestDoubaoArkCliAdapter:
    def test_builds_stateless_arkcli_invocation_without_prompt_storage(self):
        from src.llms.native_cli.doubao import DoubaoArkCliAdapter

        invocation = DoubaoArkCliAdapter(
            binary="/opt/arkcli", model="doubao-test", cwd="/workspace"
        ).build_invocation("hello")
        assert invocation.argv == (
            "/opt/arkcli", "+chat", "--format", "json", "--no-progress",
            "--model", "doubao-test", "--", "hello",
        )
        assert invocation.cwd == "/workspace"
        assert invocation.env == {
            "ARKCLI_NO_UPDATE_NOTIFIER": "1",
            "ARKCLI_CALLER_TYPE": "ai_agent",
            "ARKCLI_CALLER_NAME": "codex",
            "ARKCLI_SKILL_NAME": "arkcli-chat",
        }
        assert "--store" not in invocation.argv

    def test_stored_response_can_be_resumed(self):
        from src.llms.native_cli.doubao import DoubaoArkCliAdapter

        invocation = DoubaoArkCliAdapter(binary="/opt/arkcli", store=True).build_invocation(
            "again", session_id="resp-1"
        )
        assert "--store" in invocation.argv
        assert invocation.argv[-4:] == (
            "--previous-response-id", "resp-1", "--", "again"
        )

    def test_resume_is_rejected_when_storage_is_disabled(self):
        from src.llms.native_cli.doubao import DoubaoArkCliAdapter

        with pytest.raises(NativeCliError, match="requires response storage"):
            DoubaoArkCliAdapter(binary="/opt/arkcli").build_invocation(
                "again", session_id="resp-1"
            )

    def test_parser_uses_flat_content_and_normalizes_usage(self):
        from src.llms.native_cli.doubao import parse_doubao_result

        text, response_id, usage, _payload = parse_doubao_result(
            json.dumps(
                {
                    "id": "resp-1",
                    "content": "answer",
                    "reasoning_content": "private reasoning",
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 3,
                        "total_tokens": 15,
                    },
                }
            )
        )
        assert (text, response_id) == ("answer", "resp-1")
        assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (12, 3, 15)

    def test_incomplete_status_is_not_reported_as_a_complete_answer(self):
        from src.llms.native_cli.doubao import parse_doubao_result

        with pytest.raises(NativeCliError, match="did not complete"):
            parse_doubao_result(json.dumps({"status": "incomplete", "content": "partial"}))

    def test_argv_like_prompt_stays_a_positional_value(self):
        from src.llms.native_cli.doubao import DoubaoArkCliAdapter

        invocation = DoubaoArkCliAdapter(binary="/opt/arkcli").build_invocation("--help")
        assert invocation.argv[-2:] == ("--", "--help")

    def test_missing_binary_is_typed_unavailable(self, monkeypatch):
        from src.llms.native_cli.doubao import DoubaoArkCliAdapter
        from src.llms.native_cli.base import NativeCliUnavailable

        monkeypatch.delenv("NATIVE_CLI_DOUBAO_BIN", raising=False)
        monkeypatch.setattr("shutil.which", lambda _name: None)
        with pytest.raises(NativeCliUnavailable, match="arkcli CLI not found"):
            DoubaoArkCliAdapter().binary

    def test_auth_error_code_is_clean_but_403_access_error_is_not_auth(self):
        from src.llms.native_cli.doubao import parse_doubao_result
        from src.llms.native_cli.base import NativeCliUnavailable

        with pytest.raises(NativeCliUnavailable, match="not logged in"):
            parse_doubao_result(json.dumps({"error": {"code": "invalid_api_key"}}))
        with pytest.raises(NativeCliError, match="arkcli returned an error"):
            parse_doubao_result(json.dumps({"error": {"code": "access_denied", "status": 403}}))
        with pytest.raises(NativeCliError, match="arkcli returned an error"):
            parse_doubao_result(
                json.dumps({"error": "HTTP 403 Unauthorized: model access denied"})
            )

    def test_malformed_usage_is_a_typed_cli_error(self):
        from src.llms.native_cli.doubao import parse_doubao_result

        with pytest.raises(NativeCliError, match="invalid usage metadata"):
            parse_doubao_result(
                json.dumps({"content": "ok", "usage": {"prompt_tokens": "bad"}})
            )

    @pytest.mark.asyncio
    async def test_auth_failure_is_classified_without_echoing_diagnostic(self, monkeypatch):
        from src.llms.native_cli.doubao import DoubaoArkCliAdapter
        from src.llms.native_cli.base import NativeCliUnavailable

        async def fail(_invocation):
            raise NativeCliError("unauthorized: token=do-not-echo")

        monkeypatch.setattr("src.llms.native_cli.doubao.run_process", fail)
        with pytest.raises(NativeCliUnavailable, match="not logged in") as exc:
            await DoubaoArkCliAdapter(binary="/opt/arkcli").run("hello")
        assert "do-not-echo" not in str(exc.value)


class TestProcess:
    @pytest.mark.asyncio
    async def test_runs_argv_and_returns_stdout(self):
        output = await run_process(
            CliInvocation(
                argv=("/bin/sh", "-c", "cat; printf '\\nsecond'"),
                stdin="first",
            )
        )
        assert output.stdout == "first\nsecond"
        assert output.returncode == 0

    @pytest.mark.asyncio
    async def test_nonzero_exit_carries_stderr(self):
        with pytest.raises(NativeCliError, match="exited 7"):
            await run_process(
                CliInvocation(
                    argv=("/bin/sh", "-c", "echo nope >&2; exit 7"),
                )
            )

    @pytest.mark.asyncio
    async def test_zcode_bounded_stderr_preserves_start_and_end_without_exceeding_cap(
        self, tmp_path
    ):
        import sys

        from src.llms.native_cli.base import CliInvocation
        from src.llms.native_cli.process import _MAX_STDERR, run_process
        from src.llms.native_cli.zcode import _stderr_diagnostic

        small = await run_process(
            CliInvocation(
                argv=("/bin/sh", "-c", "printf 'first diagnostic\\n' >&2"),
            ),
            check=False,
            stderr_keep_ends=True,
        )
        assert small.stderr == "first diagnostic\n"

        script = tmp_path / "zcode-stderr"
        script.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "sys.stderr.write(\"!function(){var e='Unauthorized';\" + 'x' * 70000 + \"}\\n\")\n"
            "sys.stderr.write('Error: Select a model before sending a message\\n')\n"
            "raise SystemExit(1)\n"
        )
        script.chmod(0o755)
        output = await run_process(
            CliInvocation(argv=(str(script),)),
            check=False,
            stderr_keep_ends=True,
        )

        diagnostic = _stderr_diagnostic(output.stderr)
        assert output.returncode == 1
        assert len(output.stderr.encode()) <= _MAX_STDERR
        assert "[stderr middle omitted; tail-mid-line=1]" in output.stderr
        assert "Select a model before sending a message" in diagnostic
        assert "Unauthorized" not in diagnostic

    @pytest.mark.asyncio
    async def test_zcode_bounded_stderr_keeps_error_after_tail_cut_at_newline(self):
        import asyncio

        from src.llms.native_cli.process import _read_bounded_ends
        from src.llms.native_cli.zcode import _stderr_diagnostic

        suffix = b"\nError: Select a model before sending a message\n"
        marker = b"\n[stderr middle omitted; tail-mid-line=0]\n"
        cap = 2 * len(suffix) + len(marker)
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * 1000 + suffix)
        stream.feed_eof()

        captured = await _read_bounded_ends(stream, cap)
        diagnostic = _stderr_diagnostic(captured.decode())

        assert len(captured) <= cap
        assert b"tail-mid-line=1" in captured
        assert "Select a model before sending a message" in diagnostic

    @pytest.mark.asyncio
    async def test_missing_binary_is_typed_unavailable(self):
        from src.llms.native_cli.base import NativeCliUnavailable

        with pytest.raises(NativeCliUnavailable, match="native CLI not found"):
            await run_process(CliInvocation(argv=("/definitely/missing/native-cli",)))


class TestSessionReuse:
    @pytest.mark.asyncio
    async def test_second_run_resumes_the_same_cli_session(self, tmp_path):
        store = NativeCliSessionStore(tmp_path / "sessions.json")
        calls = []

        class FakeAdapter:
            async def run(self, prompt, *, session_id=None, cwd=None):
                calls.append(session_id)
                return NativeCliResult(
                    provider="fake", text=prompt, session_id="session-1"
                )

        await run_with_session(FakeAdapter(), "one", session_key="thread:t", store=store)
        await run_with_session(FakeAdapter(), "two", session_key="thread:t", store=store)
        assert calls == [None, "session-1"]
        assert store.get("thread:t") == "session-1"
        assert json.loads((tmp_path / "sessions.json").read_text()) == {
            "thread:t": "session-1"
        }


class TestNativeAgentTool:
    @pytest.mark.asyncio
    async def test_tool_returns_native_answer_and_reuses_thread_key(
        self, monkeypatch, tmp_path
    ):
        from ptc_agent.agent.tools import native_agent as module

        calls = []

        class FakeAdapter:
            pass

        async def fake_run(adapter, prompt, **kwargs):
            calls.append(kwargs)
            return NativeCliResult(
                provider="deepseek-dsh",
                text="native answer",
                session_id="session-tool",
                usage=NativeCliUsage(cache_read_tokens=100, input_tokens=10),
            )

        monkeypatch.setenv("NATIVE_CLI_ENABLED", "1")
        monkeypatch.setenv("NATIVE_CLI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setattr(module, "get_adapter", lambda provider: FakeAdapter())
        monkeypatch.setattr(module, "run_with_session", fake_run)
        monkeypatch.setattr(module, "NativeCliSessionStore", lambda: "store")

        tool = module.create_native_agent_tool(thread_id="thread-42")
        result = await tool.ainvoke({"prompt": "do research", "provider": "codex"})
        assert result == "native answer"
        assert calls[0]["session_key"] == "thread:thread-42:codex"
        assert calls[0]["store"] == "store"
        assert calls[0]["cwd"].endswith("thread-42")

    @pytest.mark.asyncio
    async def test_tool_turns_cli_failure_into_a_visible_result(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NATIVE_CLI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        from ptc_agent.agent.tools import native_agent as module

        async def fail(*args, **kwargs):
            raise NativeCliError("dsh CLI not found")

        monkeypatch.setenv("NATIVE_CLI_ENABLED", "1")
        monkeypatch.setattr(module, "get_adapter", lambda provider: object())
        monkeypatch.setattr(module, "run_with_session", fail)
        monkeypatch.setattr(module, "NativeCliSessionStore", lambda: "store")

        tool = module.create_native_agent_tool(thread_id="t")
        result = await tool.ainvoke({"prompt": "x"})
        assert "NativeAgent (deepseek-dsh) failed" in result
        assert "not found" in result

    def test_flag_is_off_by_default(self, monkeypatch):
        from ptc_agent.agent.tools.native_agent import native_cli_enabled

        monkeypatch.delenv("NATIVE_CLI_ENABLED", raising=False)
        assert native_cli_enabled() is False


CODEX_SAMPLE = "\n".join(
    [
        json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "i1", "type": "agent_message", "text": "ok"},
            }
        ),
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 47232,
                    "cached_input_tokens": 20352,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 10,
                },
            }
        ),
    ]
)


class TestCodex:
    def test_parses_messages_thread_and_cached_usage(self):
        from src.llms.native_cli.codex import parse_codex_events

        text, session_id, usage, events = parse_codex_events(CODEX_SAMPLE)
        assert text == "ok"
        assert session_id == "thread-1"
        assert usage.input_tokens == 47232
        assert usage.cache_read_tokens == 20352
        assert usage.output_tokens == 10
        assert len(events) == 4

    def test_new_invocation_is_read_only_and_uses_stdin(self):
        from src.llms.native_cli.codex import CodexExecAdapter

        adapter = CodexExecAdapter(binary="/opt/codex", model="gpt-test")
        invocation = adapter.build_invocation("hello", cwd="/tmp/work")
        assert invocation.argv[:3] == ("/opt/codex", "exec", "--json")
        assert "--sandbox" in invocation.argv
        assert invocation.argv[invocation.argv.index("--sandbox") + 1] == "read-only"
        assert invocation.argv[-1] == "-"
        assert invocation.stdin == "hello"
        assert invocation.cwd == "/tmp/work"

    def test_approve_for_me_is_added_without_conflicting_sandbox_flag(self):
        from src.llms.native_cli.codex import CodexExecAdapter

        adapter = CodexExecAdapter(
            binary="/opt/codex", read_only=False, approve_for_me=True
        )
        invocation = adapter.build_invocation("edit", cwd="/tmp/w")
        assert "--approve-for-me" in invocation.argv
        assert "--sandbox" not in invocation.argv

    def test_outer_sandboxed_disables_nested_codex_sandbox(self):
        from src.llms.native_cli.codex import CodexExecAdapter

        for kwargs in ({"read_only": True}, {"read_only": False, "approve_for_me": True}):
            adapter = CodexExecAdapter(binary="/opt/codex", outer_sandboxed=True, **kwargs)
            argv = adapter.build_invocation("x", cwd="/tmp/w").argv
            assert argv.count("--sandbox") == 1
            assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
            assert "--approve-for-me" not in argv

    def test_resume_invocation_uses_thread_id(self):
        from src.llms.native_cli.codex import CodexExecAdapter

        adapter = CodexExecAdapter(binary="/opt/codex")
        invocation = adapter.build_invocation(
            "again", session_id="thread-1", cwd="/tmp/w"
        )
        assert invocation.argv[:4] == ("/opt/codex", "exec", "resume", "thread-1")
        # ``resume`` has no ``-C`` option; the process cwd is the stable dir.
        assert "-C" not in invocation.argv
        assert invocation.cwd == "/tmp/w"
        assert invocation.argv[-1] == "-"

    @pytest.mark.asyncio
    async def test_run_parses_output(self, monkeypatch):
        from src.llms.native_cli.codex import CodexExecAdapter
        from src.llms.native_cli.process import ProcessOutput

        async def fake_run(invocation):
            return ProcessOutput(stdout=CODEX_SAMPLE, stderr="", returncode=0)

        monkeypatch.setattr("src.llms.native_cli.codex.run_process", fake_run)
        result = await CodexExecAdapter(binary="/opt/codex").run("hello")
        assert result.text == "ok"
        assert result.session_id == "thread-1"
        assert result.usage.cache_read_tokens == 20352


CLAUDE_SAMPLE = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "claude-session-1",
        "result": "ok",
        "usage": {
            "input_tokens": 120,
            "cache_read_input_tokens": 4096,
            "cache_creation_input_tokens": 0,
            "output_tokens": 2,
        },
    }
)


class TestClaude:
    def test_parses_success_and_cache_usage(self):
        from src.llms.native_cli.claude import parse_claude_result

        text, session_id, usage, _ = parse_claude_result(CLAUDE_SAMPLE)
        assert text == "ok"
        assert session_id == "claude-session-1"
        assert usage.cache_read_tokens == 4096
        assert usage.input_tokens == 120

    def test_error_result_is_a_loud_failure(self):
        from src.llms.native_cli.claude import parse_claude_result

        payload = json.loads(CLAUDE_SAMPLE)
        payload.update({"is_error": True, "result": "OAuth expired"})
        with pytest.raises(NativeCliError, match="reported an error") as exc_info:
            parse_claude_result(json.dumps(payload))
        assert "OAuth expired" not in str(exc_info.value)

    def test_invocation_uses_print_mode_prompt_default_model_and_resume(self):
        from src.llms.native_cli.claude import ClaudeCodeAdapter

        prompt = "--help\n$(touch /tmp/not-run)"
        adapter = ClaudeCodeAdapter(binary="/opt/claude")
        invocation = adapter.build_invocation(prompt, session_id="s-1", cwd="/tmp")
        assert invocation.argv[0] == "/opt/claude"
        assert invocation.argv[:9] == (
            "/opt/claude", "-p", "--model", "haiku", "--output-format", "json",
            "--exclude-dynamic-system-prompt-sections", "--permission-mode", "plan",
        )
        assert invocation.argv[-4:] == ("--resume", "s-1", "--", prompt)
        assert invocation.stdin == ""
        assert invocation.cwd == "/tmp"

    def test_invocation_model_override_and_isolated_mode(self):
        from src.llms.native_cli.claude import ClaudeCodeAdapter

        assert ClaudeCodeAdapter.routing_priority == "lowest"
        assert ClaudeCodeAdapter.fallback_only is True
        invocation = ClaudeCodeAdapter(
            binary="/opt/claude", model="sonnet", isolated=True
        ).build_invocation("hello")
        assert invocation.argv[3] == "sonnet"
        assert "--restricted" in invocation.argv
        assert "--strict-mcp-config" in invocation.argv
        assert "--disable-slash-commands" in invocation.argv
        assert invocation.argv[invocation.argv.index("--tools") + 1] == ""

    @pytest.mark.asyncio
    async def test_run_suppresses_raw_provider_diagnostics(self, monkeypatch):
        from src.llms.native_cli.claude import ClaudeCodeAdapter

        async def fail(_invocation):
            raise NativeCliError("private-token=secret-looking-value")

        monkeypatch.setattr("src.llms.native_cli.claude.run_process", fail)
        with pytest.raises(NativeCliError, match="check local CLI diagnostics") as exc_info:
            await ClaudeCodeAdapter(binary="/opt/claude").run("hello")
        assert "secret-looking-value" not in str(exc_info.value)


@pytest.mark.skipif(
    os.environ.get("CONDUCTOR_LIVE_CLAUDE") != "1",
    reason="set CONDUCTOR_LIVE_CLAUDE=1 to run the one-call Claude Code smoke check",
)
@pytest.mark.asyncio
async def test_claude_live_smoke_once(tmp_path):
    from src.llms.native_cli.claude import ClaudeCodeAdapter

    try:
        result = await ClaudeCodeAdapter(timeout_seconds=120, isolated=True).run(
            "Reply with exactly: ok", cwd=str(tmp_path)
        )
    except Exception:
        pytest.fail("Claude live smoke call failed; details suppressed", pytrace=False)
    if result.text.strip() != "ok":
        pytest.fail("Claude did not reply with exactly ok", pytrace=False)

@pytest.mark.asyncio
async def test_claude_run_parses_json_output(monkeypatch):
    from src.llms.native_cli.claude import ClaudeCodeAdapter
    from src.llms.native_cli.process import ProcessOutput

    async def fake_run(invocation):
        return ProcessOutput(stdout=CLAUDE_SAMPLE, stderr="", returncode=0)

    monkeypatch.setattr("src.llms.native_cli.claude.run_process", fake_run)
    result = await ClaudeCodeAdapter(binary="/opt/claude").run("hello")
    assert result.text == "ok"
    assert result.session_id == "claude-session-1"
    assert result.usage.cache_read_tokens == 4096


GEMINI_SAMPLE = json.dumps(
    {
        "response": "ok",
        "stats": {
            "models": {
                "gemini-test": {
                    "tokens": {
                        "promptTokenCount": 120,
                        "candidatesTokenCount": 2,
                        "cachedContentTokenCount": 4096,
                        "totalTokenCount": 4218,
                    }
                }
            }
        },
    }
)


class TestGemini:
    def test_parses_response_and_cached_usage(self):
        from src.llms.native_cli.gemini import parse_gemini_result

        text, usage, _ = parse_gemini_result(GEMINI_SAMPLE)
        assert text == "ok"
        assert usage.cache_read_tokens == 4096
        assert usage.input_tokens == 120
        assert usage.output_tokens == 2

    def test_error_payload_is_a_loud_failure(self):
        from src.llms.native_cli.gemini import parse_gemini_result

        with pytest.raises(NativeCliError, match="Auth method"):
            parse_gemini_result(
                json.dumps({"error": {"type": "Error", "message": "Auth method missing"}})
            )

    def test_first_turn_mints_and_later_turn_resumes_session(self):
        from src.llms.native_cli.gemini import GeminiCliAdapter

        adapter = GeminiCliAdapter(binary="/opt/gemini")
        first = adapter.build_invocation("hello")
        assert "--session-id" in first.argv
        session = first.argv[first.argv.index("--session-id") + 1]
        assert session
        second = adapter.build_invocation("again", session_id=session)
        assert "--resume" in second.argv
        assert second.argv[second.argv.index("--resume") + 1] == session

    @pytest.mark.asyncio
    async def test_run_parses_output(self, monkeypatch):
        from src.llms.native_cli.gemini import GeminiCliAdapter
        from src.llms.native_cli.process import ProcessOutput

        async def fake_run(invocation):
            return ProcessOutput(stdout=GEMINI_SAMPLE, stderr="", returncode=0)

        monkeypatch.setattr("src.llms.native_cli.gemini.run_process", fake_run)
        result = await GeminiCliAdapter(binary="/opt/gemini").run("hello")
        assert result.text == "ok"
        assert result.usage.cache_read_tokens == 4096
        assert result.session_id


QWEN_SAMPLE = json.dumps(
    [
        {"type": "system", "subtype": "session_start", "session_id": "qwen-session-1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "ok"}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "qwen-session-1",
            "result": "ok",
            "usage": {"input_tokens": 100, "cached_tokens": 2048, "output_tokens": 2},
        },
    ]
)


class TestQwen:
    def test_parses_result_array_and_usage(self):
        from src.llms.native_cli.qwen import parse_qwen_result

        text, session_id, usage, _ = parse_qwen_result(QWEN_SAMPLE)
        assert text == "ok"
        assert session_id == "qwen-session-1"
        assert usage.cache_read_tokens == 2048
        assert usage.input_tokens == 100

    def test_error_result_is_visible(self):
        from src.llms.native_cli.qwen import parse_qwen_result

        payload = json.loads(QWEN_SAMPLE)
        payload[-1].update({"is_error": True, "result": "no credentials"})
        with pytest.raises(NativeCliError, match="no credentials"):
            parse_qwen_result(json.dumps(payload))

    def test_session_is_minted_then_resumed(self):
        from src.llms.native_cli.qwen import QwenCliAdapter

        adapter = QwenCliAdapter(binary="/opt/qwen")
        first = adapter.build_invocation("hello")
        session = first.argv[first.argv.index("--session-id") + 1]
        assert session
        second = adapter.build_invocation("again", session_id=session)
        assert second.argv[second.argv.index("--resume") + 1] == session
        assert "--approval-mode" in second.argv


KIMI_SAMPLE = "\n".join(
    [
        json.dumps({"role": "assistant", "content": [{"type": "text", "text": "old"}]}),
        json.dumps({"role": "assistant", "content": [{"type": "text", "text": "ok"}]}),
    ]
)


class TestKimi:
    def test_takes_latest_assistant_message(self):
        from src.llms.native_cli.kimi import parse_kimi_stream

        text, events = parse_kimi_stream(KIMI_SAMPLE)
        assert text == "ok"
        assert len(events) == 2

    def test_only_a_kimi_created_id_is_resumed_and_never_continue(self):
        from src.llms.native_cli.kimi import KimiCliAdapter

        adapter = KimiCliAdapter(binary="/opt/kimi")
        first = adapter.build_invocation("hello")
        assert "--continue" not in first.argv
        assert "--session" not in first.argv
        i = list(first.argv).index("--prompt")
        assert first.argv[i + 1] == "hello"
        # A legacy stored "continue" names no session: the turn runs fresh.
        legacy = adapter.build_invocation("again", session_id="continue")
        assert "--continue" not in legacy.argv
        assert "--session" not in legacy.argv
        resumed = adapter.build_invocation("again", session_id="kimi-made-1")
        assert resumed.argv[resumed.argv.index("--session") + 1] == "kimi-made-1"
        assert "--continue" not in resumed.argv

    @pytest.mark.asyncio
    async def test_run_parses_stream(self, monkeypatch):
        from src.llms.native_cli.kimi import KimiCliAdapter
        from src.llms.native_cli.process import ProcessOutput

        async def fake_run(invocation):
            return ProcessOutput(stdout=KIMI_SAMPLE, stderr="", returncode=0)

        monkeypatch.setattr("src.llms.native_cli.kimi.run_process", fake_run)
        result = await KimiCliAdapter(binary="/opt/kimi").run("hello")
        assert result.text == "ok"
        # Kimi named no session in this stream: nothing is invented.
        assert result.session_id is None

    @pytest.mark.asyncio
    async def test_run_returns_the_session_kimi_created(self, monkeypatch):
        from src.llms.native_cli.kimi import KimiCliAdapter
        from src.llms.native_cli.process import ProcessOutput

        seen = []
        stream = KIMI_SAMPLE + "\n" + json.dumps(
            {"role": "meta", "type": "session.resume_hint", "session_id": "kimi-made-1"}
        )

        async def fake_run(invocation):
            seen.append(invocation.argv)
            return ProcessOutput(stdout=stream, stderr="", returncode=0)

        monkeypatch.setattr("src.llms.native_cli.kimi.run_process", fake_run)
        adapter = KimiCliAdapter(binary="/opt/kimi")
        first = await adapter.run("hello", session_id=None)
        assert first.session_id == "kimi-made-1"
        second = await adapter.run("again", session_id=first.session_id)
        assert second.session_id == "kimi-made-1"
        assert "--session" not in seen[0]
        assert seen[1][seen[1].index("--session") + 1] == "kimi-made-1"


MINIMAX_SAMPLE = json.dumps(
    {
        "schemaVersion": 1,
        "type": "exec.result",
        "runId": "run-1",
        "sessionId": "minimax-session-1",
        "status": "succeeded",
        "output": "ok",
        "usage": {
            "inputTokens": 120,
            "outputTokens": 2,
            "totalTokens": 122,
        },
    }
)


class TestMiniMax:
    def test_parses_exec_result_and_session(self):
        from src.llms.native_cli.minimax import parse_minimax_result

        text, session_id, usage, payload = parse_minimax_result(MINIMAX_SAMPLE)
        assert text == "ok"
        assert session_id == "minimax-session-1"
        assert usage.input_tokens == 120
        assert usage.output_tokens == 2
        assert payload["type"] == "exec.result"

    def test_failed_status_is_a_loud_failure(self):
        from src.llms.native_cli.minimax import parse_minimax_result
        from src.llms.native_cli.base import NativeCliUnavailable

        payload = json.loads(MINIMAX_SAMPLE)
        payload.update({"status": "failed", "error": {"message": "not signed in"}})
        with pytest.raises(NativeCliUnavailable, match="no usable credentials"):
            parse_minimax_result(json.dumps(payload))

    def test_invocation_uses_stdin_and_resumes_session(self):
        from src.llms.native_cli.minimax import MiniMaxCodeAdapter

        adapter = MiniMaxCodeAdapter(binary="/opt/mcode")
        first = adapter.build_invocation("hello", cwd="/tmp/work")
        assert first.argv[:3] == ("/opt/mcode", "exec", "--input")
        assert "--session" not in first.argv
        assert first.stdin == "hello"
        assert first.cwd == "/tmp/work"
        second = adapter.build_invocation("again", session_id="minimax-session-1")
        assert second.argv[second.argv.index("--session") + 1] == "minimax-session-1"

    @pytest.mark.asyncio
    async def test_run_parses_output(self, monkeypatch):
        from src.llms.native_cli.minimax import MiniMaxCodeAdapter
        from src.llms.native_cli.process import ProcessOutput

        async def fake_run(invocation):
            assert invocation.stdin == "hello"
            return ProcessOutput(stdout=MINIMAX_SAMPLE, stderr="", returncode=0)

        monkeypatch.setattr("src.llms.native_cli.minimax.run_process", fake_run)
        result = await MiniMaxCodeAdapter(binary="/opt/mcode").run("hello")
        assert result.text == "ok"
        assert result.session_id == "minimax-session-1"

    @pytest.mark.asyncio
    async def test_dyld_runtime_failure_is_redacted_and_classified(self, monkeypatch):
        from src.llms.native_cli.base import NativeCliError
        from src.llms.native_cli.failure import FailureClass, classify_failure
        from src.llms.native_cli.minimax import MiniMaxCodeAdapter

        async def fail(_invocation):
            raise NativeCliError(
                "native CLI exited -6: dyld[42]: Library not loaded: "
                "/private/runtime/secret/libsimdjson.34.dylib"
            )

        monkeypatch.setattr("src.llms.native_cli.minimax.run_process", fail)
        with pytest.raises(NativeCliError) as raised:
            await MiniMaxCodeAdapter(binary="/opt/mcode").run("hello")

        classification = classify_failure("minimax", str(raised.value))
        assert classification.failure_class is FailureClass.CRASH
        assert classification.reason_code == "minimax_runtime_dependency_missing"
        assert str(raised.value) == "dyld: library not loaded"
        assert "secret" not in str(raised.value)


MIMO_SAMPLE = "\n".join(
    [
        json.dumps(
            {
                "type": "step_start",
                "timestamp": 1,
                "sessionID": "mimo-session-1",
                "part": {"type": "step-start"},
            }
        ),
        json.dumps(
            {
                "type": "text",
                "timestamp": 2,
                "sessionID": "mimo-session-1",
                "part": {"type": "text", "text": "intermediate"},
            }
        ),
        json.dumps(
            {
                "type": "text",
                "timestamp": 3,
                "sessionID": "mimo-session-1",
                "part": {"type": "text", "text": "ok"},
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "timestamp": 4,
                "sessionID": "mimo-session-1",
                "part": {
                    "type": "step-finish",
                    "reason": "stop",
                    "tokens": {
                        "input": 100,
                        "output": 2,
                        "total": 102,
                        "cache": {"read": 2048, "write": 0},
                    },
                },
            }
        ),
    ]
)


class TestMiMo:
    def test_takes_latest_text_and_cache_tokens(self):
        from src.llms.native_cli.mimo import parse_mimo_stream

        text, session_id, usage, events = parse_mimo_stream(MIMO_SAMPLE)
        assert text == "ok"
        assert session_id == "mimo-session-1"
        assert usage.cache_read_tokens == 2048
        assert usage.input_tokens == 100
        assert len(events) == 4

    def test_error_event_is_visible(self):
        from src.llms.native_cli.mimo import parse_mimo_stream
        from src.llms.native_cli.base import NativeCliUnavailable

        payload = {
            "type": "error",
            "sessionID": "mimo-session-1",
            "error": {"data": {"message": "Invalid API Key"}},
        }
        with pytest.raises(NativeCliUnavailable, match="no usable credentials"):
            parse_mimo_stream(json.dumps(payload))

    def test_invocation_keeps_prompt_after_separator(self):
        from src.llms.native_cli.mimo import MiMoCodeAdapter

        adapter = MiMoCodeAdapter(binary="/opt/mimo")
        invocation = adapter.build_invocation("hello", cwd="/tmp/work")
        assert invocation.argv[:4] == ("/opt/mimo", "run", "--format", "json")
        assert invocation.argv[-2:] == ("--", "hello")
        assert invocation.cwd == "/tmp/work"

    @pytest.mark.asyncio
    async def test_run_parses_stream(self, monkeypatch):
        from src.llms.native_cli.mimo import MiMoCodeAdapter
        from src.llms.native_cli.process import ProcessOutput

        async def fake_run(invocation):
            return ProcessOutput(stdout=MIMO_SAMPLE, stderr="", returncode=0)

        monkeypatch.setattr("src.llms.native_cli.mimo.run_process", fake_run)
        result = await MiMoCodeAdapter(binary="/opt/mimo").run("hello")
        assert result.text == "ok"
        assert result.session_id == "mimo-session-1"
        assert result.usage.cache_read_tokens == 2048


ZCODE_SAMPLE = json.dumps(
    {
        "type": "result",
        "sessionId": "zcode-session-1",
        "traceId": "trace-1",
        "response": "ok",
        "usage": {
            "inputTokens": 100,
            "outputTokens": 2,
            "cacheReadTokens": 4096,
            "totalTokens": 4198,
        },
        "eventCount": 4,
    }
)


class TestZCode:
    def test_parses_result_and_cache_usage(self):
        from src.llms.native_cli.zcode import parse_zcode_result

        text, session_id, usage, payload = parse_zcode_result(ZCODE_SAMPLE)
        assert text == "ok"
        assert session_id == "zcode-session-1"
        assert usage.cache_read_tokens == 4096
        assert payload["type"] == "result"

    def test_error_is_visible(self):
        from src.llms.native_cli.zcode import parse_zcode_result

        payload = {"type": "result", "error": {"message": "Select a model"}}
        with pytest.raises(NativeCliError, match="Select a model"):
            parse_zcode_result(json.dumps(payload))

    def test_finds_the_app_bundled_provider_config(self, tmp_path, monkeypatch):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        for name in (
            "NATIVE_CLI_ZCODE_API_KEY",
            "ZAI_CN_CODING_API_KEY",
            "ZAI_API_KEY",
        ):
            monkeypatch.delenv(name, raising=False)
        binary = tmp_path / "glm" / "zcode.cjs"
        binary.parent.mkdir(parents=True)
        binary.write_text("")
        config = tmp_path / "config" / "provider" / "zcode-builtin.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}")
        invocation = ZCodeCliAdapter(binary=str(binary)).build_invocation("hello")
        assert invocation.env == {"ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": str(config)}

    @pytest.mark.skipif(
        not (__import__("shutil").which("sandbox-exec") or __import__("shutil").which("bwrap")),
        reason="no supported worker isolation runtime",
    )
    @pytest.mark.asyncio
    async def test_isolated_child_receives_only_validated_config_paths(
        self, tmp_path
    ):
        import shutil
        from src.llms.native_cli.isolation import (
            current_worker_isolation,
            worker_isolation,
        )
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        runtime = tmp_path / "zcode-runtime"
        binary = runtime / "glm" / "zcode.cjs"
        builtin = runtime / "config" / "provider" / "zcode-builtin.json"
        binary.parent.mkdir(parents=True)
        builtin.parent.mkdir(parents=True)
        builtin.write_text("{}", encoding="utf-8")
        expected = json.dumps(
            {
                "type": "result",
                "sessionId": "isolated-zcode",
                "response": "ok",
                "eventCount": 1,
            }
        )
        import shlex

        binary.write_text(
            "#!/bin/sh\n"
            '[ -r "$ZCODE_BUILTIN_PROVIDER_CONFIG_FILE" ] || exit 31\n'
            '[ -r "$ZCODE_PERSONAL_PROVIDER_CONFIG_FILE" ] || exit 32\n'
            'case "$ZCODE_PERSONAL_PROVIDER_CONFIG_FILE" in "$HOME"/*) ;; *) exit 33 ;; esac\n'
            f"printf '%s\\n' {shlex.quote(expected)}\n",
            encoding="utf-8",
        )
        binary.chmod(0o755)
        key = "zcode-config-path-secret-sentinel"

        with worker_isolation(
            workspace_id="zcode-config-paths",
            root=tmp_path / "state",
            workdir=workspace,
            env_names=set(),
            runtime_read_paths=(str(runtime),),
            read_only=True,
            workspace_root=workspace,
        ):
            if not shutil.which("sandbox-exec") and not shutil.which("bwrap"):
                pytest.skip("no supported worker isolation runtime")
            adapter = ZCodeCliAdapter(
                binary=str(binary), api_key=key, model="GLM-5.3", read_only=True
            )
            invocation = adapter.build_invocation("hello", cwd=str(workspace))
            isolation = current_worker_isolation()
            assert isolation is not None
            assert "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE" not in isolation.env_names
            assert "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE" not in isolation.env_names
            assert key not in invocation.argv
            assert invocation.env is not None
            personal = Path(invocation.env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"])
            assert personal.is_relative_to(isolation.home)
            assert personal.stat().st_mode & 0o777 == 0o600
            result = await adapter.run("hello", cwd=str(workspace))

        assert result.text == "ok"
        assert key not in result.text

    def test_personal_provider_file_is_private_and_selects_model(
        self, tmp_path, monkeypatch
    ):
        import json
        import os

        from src.llms.native_cli.zcode import ZCodeCliAdapter

        monkeypatch.setenv("NATIVE_CLI_ZCODE_PROVIDER_DIR", str(tmp_path / "provider"))
        invocation = ZCodeCliAdapter(
            binary="/opt/zcode",
            api_key="test-key",
            base_url="https://example.invalid/anthropic",
            model="GLM-5.3-Flash",
        ).build_invocation("hello", cwd="/tmp/work")
        assert invocation.env is not None
        path = invocation.env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"]
        payload = json.loads(open(path, encoding="utf-8").read())
        provider = payload["config"]["providerConfigRules"]["providerRules"][0]
        assert provider["config"]["access"]["apiKey"] == "test-key"
        assert payload["config"]["defaultModelSelection"] == {
            "providerId": "alpha-zai",
            "modelId": "GLM-5.3-Flash",
        }
        assert os.stat(path).st_mode & 0o777 == 0o600

    def test_invocation_is_read_only_and_resumes(self):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        adapter = ZCodeCliAdapter(binary="/opt/zcode")
        first = adapter.build_invocation("hello", cwd="/tmp/work")
        assert first.argv[0] == "/opt/zcode"
        assert first.argv[first.argv.index("--prompt") + 1] == "hello"
        assert "--output-format" in first.argv
        assert first.argv[first.argv.index("--mode") + 1] == "plan"
        second = adapter.build_invocation("again", session_id="zcode-session-1")
        assert second.argv[second.argv.index("--resume") + 1] == "zcode-session-1"

    @pytest.mark.asyncio
    async def test_run_parses_output(self, monkeypatch, tmp_path):
        from src.llms.native_cli.zcode import ZCodeCliAdapter
        from src.llms.native_cli.process import ProcessOutput

        # The provider lock is a shared file; keep the test off it.
        monkeypatch.setattr(
            "src.llms.native_cli.zcode._LOCK_PATH", tmp_path / "zcode.lock"
        )

        async def fake_run(invocation, **_kwargs):
            return ProcessOutput(stdout=ZCODE_SAMPLE, stderr="", returncode=0)

        monkeypatch.setattr("src.llms.native_cli.zcode.run_process", fake_run)
        result = await ZCodeCliAdapter(binary="/opt/zcode").run("hello")
        assert result.text == "ok"
        assert result.session_id == "zcode-session-1"
        assert result.usage.cache_read_tokens == 4096

    @pytest.mark.asyncio
    async def test_bundled_auth_words_do_not_override_model_selection_error(
        self, monkeypatch, tmp_path
    ):
        from src.llms.native_cli.process import ProcessOutput
        from src.llms.native_cli.zcode import ZCodeCliAdapter
        from src.llms.native_cli.failure import classify_failure

        monkeypatch.setattr(
            "src.llms.native_cli.zcode._LOCK_PATH", tmp_path / "zcode.lock"
        )

        async def fake_run(invocation, **_kwargs):
            return ProcessOutput(
                stdout="",
                stderr=(
                    "ZCode startup complete\n"
                    "Error: Select a model before sending a message\n"
                    "    at c (/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs:2:17)\n"
                    "!function(){var e='Unauthorized';throw e}()\n"
                ),
                returncode=1,
            )

        monkeypatch.setattr("src.llms.native_cli.zcode.run_process", fake_run)
        with pytest.raises(NativeCliError, match="no selected model") as caught:
            await ZCodeCliAdapter(binary="/opt/zcode").run("hello")

        assert "unauthorized" not in str(caught.value).lower()
        classification = classify_failure("zcode", str(caught.value))
        assert classification.reason_code == "zcode_model_selection_missing"

    @pytest.mark.asyncio
    async def test_diagnostic_after_bundle_stack_is_preserved_without_bundle_auth_words(
        self, monkeypatch, tmp_path
    ):
        from src.llms.native_cli.failure import classify_failure
        from src.llms.native_cli.process import ProcessOutput
        from src.llms.native_cli.zcode import ZCodeCliAdapter, _stderr_diagnostic

        monkeypatch.setattr(
            "src.llms.native_cli.zcode._LOCK_PATH", tmp_path / "zcode.lock"
        )
        source = "!function(){var e='Unauthorized';" + "x" * 1200 + "}"
        stderr = (
            "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs:2217\n"
            f"{source}\n"
            "    at c (/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs:2:17)\n"
            "Error: Select a model before sending a message\n"
        )

        async def fake_run(invocation, **_kwargs):
            return ProcessOutput(stdout="", stderr=stderr, returncode=1)

        monkeypatch.setattr("src.llms.native_cli.zcode.run_process", fake_run)
        diagnostic = _stderr_diagnostic(stderr)
        assert "Select a model before sending a message" in diagnostic
        assert source not in diagnostic
        assert "zcode.cjs" not in diagnostic

        with pytest.raises(NativeCliError, match="no selected model") as caught:
            await ZCodeCliAdapter(binary="/opt/zcode").run("hello")
        assert classify_failure("zcode", str(caught.value)).reason_code == (
            "zcode_model_selection_missing"
        )

    @pytest.mark.asyncio
    async def test_plan_quota_1310_is_safe_and_not_retried(self, monkeypatch, tmp_path):
        from src.llms.native_cli.failure import FailureClass, classify_failure
        from src.llms.native_cli.process import ProcessOutput
        from src.llms.native_cli.zcode import ZCodeCliAdapter, _stderr_diagnostic

        monkeypatch.setattr(
            "src.llms.native_cli.zcode._LOCK_PATH", tmp_path / "zcode.lock"
        )
        stderr = (
            "ZCode Built-in unchanged revision=43 source=CDN\n"
            "ProviderBusinessError: [1310][您已达到每周/每月使用上限，限额将在2026-11-13 10:27:40重置。] "
            "[provider-request-secret-sentinel]\n"
        )
        calls = 0

        async def fake_run(invocation, **_kwargs):
            nonlocal calls
            calls += 1
            return ProcessOutput(stdout="", stderr=stderr, returncode=1)

        monkeypatch.setattr("src.llms.native_cli.zcode.run_process", fake_run)
        with pytest.raises(NativeCliError, match="plan quota exhausted") as caught:
            await ZCodeCliAdapter(binary="/opt/zcode").run("Reply OK")

        assert calls == 1
        assert "provider-request-secret-sentinel" not in str(caught.value)
        assert "2026-11-13" not in str(caught.value)
        classification = classify_failure("zcode", str(caught.value))
        assert classification.failure_class is FailureClass.QUOTA_EXHAUSTED
        assert classification.reason_code == "zcode_plan_quota_exhausted"
        assert classify_failure(
            "zcode", "ProviderBusinessError: [1310] generic failure"
        ).reason_code != "zcode_plan_quota_exhausted"
        assert classify_failure(
            "zcode", "ordinary usage limit reached"
        ).reason_code != "zcode_plan_quota_exhausted"

        bundle_noise = (
            "!function(){var e='ProviderBusinessError: [1310]"
            "您已达到每周/每月使用上限';throw e}()\nError: startup failed"
        )
        selected = _stderr_diagnostic(bundle_noise)
        assert "[1310]" not in selected
        assert classify_failure("zcode", selected).reason_code != (
            "zcode_plan_quota_exhausted"
        )

    @pytest.mark.asyncio
    async def test_real_auth_line_still_classifies_without_echoing_credential(
        self, monkeypatch, tmp_path
    ):
        from src.llms.native_cli.failure import classify_failure
        from src.llms.native_cli.process import ProcessOutput
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        monkeypatch.setattr(
            "src.llms.native_cli.zcode._LOCK_PATH", tmp_path / "zcode.lock"
        )

        async def fake_run(invocation, **_kwargs):
            return ProcessOutput(
                stdout="",
                stderr=(
                    "ZCode startup complete\n"
                    "Error: Unauthorized: Invalid API key sentinel-secret-value\n"
                ),
                returncode=1,
            )

        monkeypatch.setattr("src.llms.native_cli.zcode.run_process", fake_run)
        with pytest.raises(NativeCliError) as caught:
            await ZCodeCliAdapter(binary="/opt/zcode").run("hello")

        assert "sentinel-secret-value" not in str(caught.value)
        classification = classify_failure("zcode", str(caught.value))
        assert classification.reason_code == "zcode_credentials_unavailable"


ZCODE_STREAM_SAMPLE = "\n".join(
    [
        json.dumps(
            {
                "type": "session.titleUpdated",
                "sessionId": "zcode-session-1",
                "payload": {"title": "run"},
            }
        ),
        json.dumps(
            {
                "type": "model.streaming",
                "sessionId": "zcode-session-1",
                "payload": {"kind": "text_delta", "delta": "ok"},
            }
        ),
        json.dumps(
            {
                "type": "turn.completed",
                "sessionId": "zcode-session-1",
                "payload": {
                    "resultType": "success",
                    "usage": {
                        "inputTokens": 100,
                        "outputTokens": 2,
                        "cacheReadTokens": 4096,
                        "totalTokens": 4198,
                    },
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "sessionId": "zcode-session-1",
                "response": "ok",
                "usage": {
                    "modelRequestCount": 3,
                    "inputTokens": 100,
                    "outputTokens": 2,
                    "cacheReadTokens": 4096,
                    "totalTokens": 4198,
                },
                "eventCount": 4,
            }
        ),
    ]
)

# The CLI stops at its turn budget, reports the exhausted budget as the turn
# resultType, and still exits non-zero with an empty final answer.
ZCODE_MAX_TURNS_SAMPLE = "\n".join(
    [
        json.dumps(
            {
                "type": "turn.completed",
                "sessionId": "zcode-session-1",
                "payload": {
                    "resultType": "error_max_turns",
                    "response": "",
                    "usage": {"modelRequestCount": 40, "inputTokens": 12000},
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "sessionId": "zcode-session-1",
                "response": "",
                "usage": {"modelRequestCount": 40, "inputTokens": 12000},
                "eventCount": 1200,
            }
        ),
    ]
)

# A provider failure keeps the generic shape: isError, no answer, no usage.
ZCODE_FAILED_SAMPLE = "\n".join(
    [
        json.dumps(
            {
                "type": "turn.failed",
                "sessionId": "zcode-session-1",
                "payload": {
                    "error": {"name": "Error", "message": "provider refused"},
                    "turnPhase": "model",
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "sessionId": "zcode-session-1",
                "response": "",
                "usage": {"modelRequestCount": 0, "inputTokens": 0},
                "isError": True,
                "eventCount": 1200,
            }
        ),
    ]
)


class TestZCodeTurnBudget:
    """The turn budget, mode and model are always explicit."""

    def test_review_mode_is_plan(self):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        argv = ZCodeCliAdapter(binary="/opt/zcode").build_invocation(
            "hello", cwd="/tmp/work"
        ).argv
        assert argv[argv.index("--mode") + 1] == "plan"

    def test_write_mode_without_a_dedicated_directory_fails_closed(self):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        adapter = ZCodeCliAdapter(binary="/opt/zcode", read_only=False)
        with pytest.raises(NativeCliError, match="dedicated working directory"):
            adapter.build_invocation("hello")

    def test_write_mode_with_a_dedicated_directory_uses_yolo(self):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        argv = ZCodeCliAdapter(binary="/opt/zcode", read_only=False).build_invocation(
            "hello", cwd="/tmp/work"
        ).argv
        assert argv[argv.index("--mode") + 1] == "yolo"

    def test_an_explicit_mode_wins_for_the_approval_lane(self):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        argv = ZCodeCliAdapter(
            binary="/opt/zcode", read_only=False, mode="build"
        ).build_invocation("hello").argv
        assert argv[argv.index("--mode") + 1] == "build"

    def test_clean_room_cli_receives_model_and_turn_budget(self, monkeypatch):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        monkeypatch.delenv("NATIVE_CLI_ZCODE_MODEL", raising=False)
        monkeypatch.delenv("NATIVE_CLI_ZCODE_MAX_TURNS", raising=False)
        adapter = ZCodeCliAdapter(binary="/opt/zcode-cli")
        argv = adapter.build_invocation("hello").argv
        assert argv[argv.index("--model") + 1] == "GLM-5.3"
        assert argv[argv.index("--max-turns") + 1] == "200"
        assert adapter.model == "GLM-5.3"
        assert adapter.max_turns == 200
        # The clean-room grammar has no --surface.
        assert "--surface" not in argv

    def test_model_and_turn_budget_are_configurable(self, monkeypatch):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        monkeypatch.setenv("NATIVE_CLI_ZCODE_MODEL", "GLM-5.2")
        monkeypatch.setenv("NATIVE_CLI_ZCODE_MAX_TURNS", "64")
        argv = ZCodeCliAdapter(binary="/opt/zcode-cli").build_invocation(
            "hello"
        ).argv
        assert argv[argv.index("--model") + 1] == "GLM-5.2"
        assert argv[argv.index("--max-turns") + 1] == "64"

        # An unusable value falls back to the default instead of failing.
        monkeypatch.setenv("NATIVE_CLI_ZCODE_MAX_TURNS", "not-a-number")
        assert ZCodeCliAdapter(binary="/opt/zcode-cli").max_turns == 200

        explicit = ZCodeCliAdapter(
            binary="/opt/zcode-cli", max_turns=12
        ).build_invocation("hello").argv
        assert explicit[explicit.index("--max-turns") + 1] == "12"

    def test_official_cli_keeps_its_own_grammar(self):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        argv = ZCodeCliAdapter(binary="/opt/zcode").build_invocation(
            "hello", cwd="/tmp/work"
        ).argv
        assert argv[argv.index("--surface") + 1] == "terminal"
        # The official CLI has no --model or --max-turns option.
        assert "--max-turns" not in argv
        assert "--model" not in argv

    def test_both_grammars_stream_the_result(self):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        for binary in ("/opt/zcode", "/opt/zcode-cli"):
            argv = ZCodeCliAdapter(binary=binary).build_invocation("hello").argv
            assert argv[argv.index("--output-format") + 1] == "stream-json"

    def test_the_api_key_never_reaches_the_argument_list(self, monkeypatch, tmp_path):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        monkeypatch.setenv("NATIVE_CLI_ZCODE_PROVIDER_DIR", str(tmp_path / "provider"))
        invocation = ZCodeCliAdapter(
            binary="/opt/zcode-cli", api_key="test-key"
        ).build_invocation("hello")
        assert "test-key" not in invocation.argv
        assert invocation.env is not None
        assert invocation.env["ZCODE_API_KEY"] == "test-key"


class TestZCodeTranscript:
    """The transcript, not the exit code, decides what happened."""

    def test_stream_transcript_yields_the_final_result(self):
        from src.llms.native_cli.zcode import parse_zcode_result

        text, session_id, usage, payload = parse_zcode_result(ZCODE_STREAM_SAMPLE)
        assert text == "ok"
        assert session_id == "zcode-session-1"
        assert usage.cache_read_tokens == 4096
        assert usage.input_tokens == 100
        assert payload["type"] == "result"

    def test_turn_exhaustion_is_a_distinct_max_turns_error(self):
        from src.llms.native_cli.zcode import parse_zcode_result
        from src.llms.native_cli.zcode_transcript import ZCodeMaxTurnsError

        with pytest.raises(ZCodeMaxTurnsError) as excinfo:
            parse_zcode_result(ZCODE_MAX_TURNS_SAMPLE)
        assert "max_turns" in str(excinfo.value)
        assert excinfo.value.code == "max_turns"

    def test_a_failed_turn_stays_a_generic_failure(self):
        from src.llms.native_cli.zcode import parse_zcode_result
        from src.llms.native_cli.zcode_transcript import ZCodeMaxTurnsError

        with pytest.raises(NativeCliError) as excinfo:
            parse_zcode_result(ZCODE_FAILED_SAMPLE)
        assert not isinstance(excinfo.value, ZCodeMaxTurnsError)

    def test_a_structured_rate_limit_is_reported_as_a_status(self):
        from src.llms.native_cli.zcode import parse_zcode_result

        with pytest.raises(NativeCliError, match="HTTP 429"):
            parse_zcode_result(
                json.dumps({"type": "result", "status": 429, "response": ""})
            )


class TestZCodeEndpoint:
    """The endpoint the adapter was configured with is the one that is used."""

    def test_the_clean_room_cli_keeps_the_configured_endpoint(self, monkeypatch):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        monkeypatch.setenv(
            "NATIVE_CLI_ZCODE_BASE_URL", "https://example.invalid/anthropic"
        )
        invocation = ZCodeCliAdapter(binary="/opt/zcode-cli").build_invocation("hello")
        assert invocation.env is not None
        assert invocation.env["ZCODE_BASE_URL"] == "https://example.invalid/anthropic"

        # An explicit argument wins over the environment.
        explicit = ZCodeCliAdapter(
            binary="/opt/zcode-cli", base_url="https://other.invalid/v1"
        ).build_invocation("hello")
        assert explicit.env is not None
        assert explicit.env["ZCODE_BASE_URL"] == "https://other.invalid/v1"

    def test_an_unset_endpoint_does_not_replace_the_cli_endpoint(self, monkeypatch):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        monkeypatch.delenv("NATIVE_CLI_ZCODE_BASE_URL", raising=False)
        invocation = ZCodeCliAdapter(binary="/opt/zcode-cli").build_invocation("hello")
        assert "ZCODE_BASE_URL" not in (invocation.env or {})


def _big_transcript_cli(tmp_path: Path) -> Path:
    """A fake ZCode CLI whose transcript is larger than the stdout cap."""
    from src.llms.native_cli.process import _MAX_STDOUT

    chunks = _MAX_STDOUT // (1024 * 1024) + 1
    script = tmp_path / "zcode-cli"
    script.write_text(
        f"#!{sys.executable}\n"
        + "import json, sys\n"
        + "blob = 'x' * (1024 * 1024)\n"
        + f"for _ in range({chunks}):\n"
        + '    sys.stdout.write(json.dumps({"type": "tool.updated", "payload": blob})'
        + ' + "\\n")\n'
        + "sys.stdout.write(json.dumps({'type': 'result', 'sessionId': "
        + "'zcode-session-1', 'response': 'ok', 'usage': {'inputTokens': 7, "
        + "'cacheReadTokens': 4096}}) + \"\\n\")\n"
    )
    script.chmod(0o755)
    return script


class TestZCodeLargeTranscript:
    """A run bigger than the stdout cap still delivers its terminal result."""

    @pytest.mark.asyncio
    async def test_the_cap_drops_the_head_of_the_stream(self, tmp_path):
        from src.llms.native_cli.process import run_process

        script = _big_transcript_cli(tmp_path)
        invocation = CliInvocation(argv=(str(script),), timeout_seconds=120)

        head = await run_process(invocation, check=False)

        # The events fill more than the cap, so the head-truncated form is
        # exactly the shape that loses the answer.
        assert '"response": "ok"' not in head.stdout

    @pytest.mark.asyncio
    async def test_the_terminal_result_survives_the_cap(self, tmp_path):
        from src.llms.native_cli.process import run_process

        script = _big_transcript_cli(tmp_path)
        invocation = CliInvocation(argv=(str(script),), timeout_seconds=120)

        tail = await run_process(invocation, check=False, keep_tail=True)

        assert '"response": "ok"' in tail.stdout

    @pytest.mark.asyncio
    async def test_the_adapter_answers_from_a_transcript_bigger_than_the_cap(
        self, monkeypatch, tmp_path
    ):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        monkeypatch.setattr(
            "src.llms.native_cli.zcode._LOCK_PATH", tmp_path / "zcode.lock"
        )
        script = _big_transcript_cli(tmp_path)

        result = await ZCodeCliAdapter(binary=str(script)).run("hello")

        assert result.text == "ok"
        assert result.session_id == "zcode-session-1"
        assert result.usage.input_tokens == 7
        assert result.usage.cache_read_tokens == 4096


class TestZCodeRun:
    """The adapter's run loop: bounded retries and serialized calls."""

    @pytest.mark.asyncio
    async def test_queued_calls_each_run_with_their_own_mcp_config(
        self, monkeypatch, tmp_path
    ):
        import asyncio
        import json

        from src.llms.native_cli.process import ProcessOutput
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(
            "src.llms.native_cli.zcode._LOCK_PATH", tmp_path / "zcode.lock"
        )
        user_config = home / ".zcode" / "cli" / "config.json"
        seen: list[tuple[str, ...]] = []

        async def fake_run(_invocation, **_kwargs):
            data = json.loads(user_config.read_text()) if user_config.is_file() else {}
            seen.append(tuple(sorted(data.get("mcp", {}).get("servers", {}))))
            await asyncio.sleep(0.05)
            return ProcessOutput(stdout=ZCODE_STREAM_SAMPLE, stderr="", returncode=0)

        monkeypatch.setattr("src.llms.native_cli.zcode.run_process", fake_run)
        servers = [{"name": "alpha", "url": "https://mcp.example.test/mcp"}]
        adapters = [
            ZCodeCliAdapter(binary="/opt/zcode", mcp_servers=servers) for _ in range(2)
        ]
        cwd = str(tmp_path / "w")
        await asyncio.gather(*(a.run("hi", cwd=cwd) for a in adapters))

        assert seen == [("alpha",), ("alpha",)]
        # Nothing of either call's transient config is left behind.
        assert not user_config.exists()
        assert not (tmp_path / "w" / ".zcode" / "config.json").exists()

    @pytest.mark.asyncio
    async def test_mcp_config_is_restored_while_the_lock_is_still_held(
        self, monkeypatch, tmp_path
    ):
        import contextlib

        from src.llms.native_cli import zcode
        from src.llms.native_cli.process import ProcessOutput

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        held = False
        restored_under_lock: list[bool] = []
        real_lock = zcode._provider_lock

        @contextlib.asynccontextmanager
        async def tracking_lock(**kwargs):
            nonlocal held
            async with real_lock(**kwargs):
                held = True
                try:
                    yield
                finally:
                    held = False

        monkeypatch.setattr(zcode, "_LOCK_PATH", tmp_path / "zcode.lock")
        monkeypatch.setattr(zcode, "_provider_lock", tracking_lock)

        async def fake_run(_invocation, **_kwargs):
            return ProcessOutput(stdout=ZCODE_STREAM_SAMPLE, stderr="", returncode=0)

        monkeypatch.setattr(zcode, "run_process", fake_run)
        adapter = zcode.ZCodeCliAdapter(
            binary="/opt/zcode",
            mcp_servers=[{"name": "alpha", "url": "https://mcp.example.test/mcp"}],
        )
        real_restore = adapter._restore_mcp

        def restore():
            restored_under_lock.append(held)
            real_restore()

        monkeypatch.setattr(adapter, "_restore_mcp", restore)
        await adapter.run("hi", cwd=str(tmp_path / "w"))

        assert restored_under_lock == [True]

    def test_clean_room_cli_is_detected_and_skips_mcp_config(
        self, monkeypatch, tmp_path
    ):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        monkeypatch.setenv("HOME", str(tmp_path))
        servers = [{"name": "alpha", "url": "https://mcp.example.test/mcp"}]
        clean = ZCodeCliAdapter(binary="/opt/zcode-cli", mcp_servers=servers)
        desktop = ZCodeCliAdapter(binary="/opt/zcode", mcp_servers=servers)
        assert clean.clean_room is True
        assert desktop.clean_room is False
        clean._materialize_mcp(str(tmp_path / "w"))
        assert not (tmp_path / ".zcode").exists()

    @pytest.mark.asyncio
    async def test_run_reports_max_turns_instead_of_a_generic_failure(
        self, monkeypatch, tmp_path
    ):
        from src.llms.native_cli.zcode import ZCodeCliAdapter
        from src.llms.native_cli.zcode_transcript import ZCodeMaxTurnsError
        from src.llms.native_cli.process import ProcessOutput

        monkeypatch.setattr(
            "src.llms.native_cli.zcode._LOCK_PATH", tmp_path / "zcode.lock"
        )
        calls = 0

        async def fake_run(_invocation, **_kwargs):
            nonlocal calls
            calls += 1
            return ProcessOutput(
                stdout=ZCODE_MAX_TURNS_SAMPLE, stderr="", returncode=1
            )

        monkeypatch.setattr("src.llms.native_cli.zcode.run_process", fake_run)
        with pytest.raises(ZCodeMaxTurnsError, match="max_turns"):
            await ZCodeCliAdapter(binary="/opt/zcode-cli").run("hello")
        # A spent turn budget is not a provider failure, so it is not retried.
        assert calls == 1

    @pytest.mark.asyncio
    async def test_run_retries_a_rate_limit_with_bounded_backoff(
        self, monkeypatch, tmp_path
    ):
        from src.llms.native_cli.zcode import ZCodeCliAdapter
        from src.llms.native_cli.process import ProcessOutput

        monkeypatch.setattr(
            "src.llms.native_cli.zcode._LOCK_PATH", tmp_path / "zcode.lock"
        )
        calls = 0
        delays = []

        async def fake_run(_invocation, **_kwargs):
            nonlocal calls
            calls += 1
            if calls < 3:
                return ProcessOutput(
                    stdout="", stderr="HTTP 429 Too Many Requests", returncode=1
                )
            return ProcessOutput(stdout=ZCODE_STREAM_SAMPLE, stderr="", returncode=0)

        async def record_sleep(seconds):
            delays.append(seconds)

        monkeypatch.setattr("src.llms.native_cli.zcode.run_process", fake_run)
        monkeypatch.setattr("src.llms.native_cli.zcode.asyncio.sleep", record_sleep)
        result = await ZCodeCliAdapter(binary="/opt/zcode-cli").run("hello")

        assert result.text == "ok"
        assert calls == 3
        assert delays == [1.0, 2.0]

    @pytest.mark.asyncio
    async def test_the_retry_backoff_does_not_outlive_the_call_deadline(
        self, monkeypatch, tmp_path
    ):
        """A retry never waits longer than the call is allowed to take.

        The lock wait, the process run and the backoff share one budget, so a
        queued call that is already close to its timeout cannot be pushed
        past it by the backoff.
        """
        from src.llms.native_cli.zcode import ZCodeCliAdapter
        from src.llms.native_cli.process import ProcessOutput

        monkeypatch.setattr(
            "src.llms.native_cli.zcode._LOCK_PATH", tmp_path / "zcode.lock"
        )
        delays = []

        async def fake_run(_invocation, **_kwargs):
            return ProcessOutput(
                stdout="", stderr="HTTP 429 Too Many Requests", returncode=1
            )

        async def record_sleep(seconds):
            delays.append(seconds)

        monkeypatch.setattr("src.llms.native_cli.zcode.run_process", fake_run)
        monkeypatch.setattr("src.llms.native_cli.zcode.asyncio.sleep", record_sleep)

        with pytest.raises(NativeCliError, match="bounded retries"):
            await ZCodeCliAdapter(
                binary="/opt/zcode-cli", timeout_seconds=0.05
            ).run("hello")

        # The nominal backoff is 1s and 2s; both are clamped to what is
        # left of the call's own budget.
        assert delays
        assert all(delay <= 0.05 for delay in delays), delays

    @pytest.mark.asyncio
    async def test_an_answer_mentioning_a_status_is_not_retried(
        self, monkeypatch, tmp_path
    ):
        from src.llms.native_cli.zcode import ZCodeCliAdapter
        from src.llms.native_cli.process import ProcessOutput

        monkeypatch.setattr(
            "src.llms.native_cli.zcode._LOCK_PATH", tmp_path / "zcode.lock"
        )
        calls = 0
        answer = json.dumps(
            {
                "type": "result",
                "sessionId": "zcode-session-1",
                "response": "The service returned 429 yesterday.",
                "usage": {"inputTokens": 3},
            }
        )

        async def fake_run(_invocation, **_kwargs):
            nonlocal calls
            calls += 1
            return ProcessOutput(stdout=answer, stderr="", returncode=0)

        monkeypatch.setattr("src.llms.native_cli.zcode.run_process", fake_run)
        result = await ZCodeCliAdapter(binary="/opt/zcode-cli").run("hello")

        assert result.text == "The service returned 429 yesterday."
        assert calls == 1

    @pytest.mark.asyncio
    async def test_calls_are_serialized(self, monkeypatch, tmp_path):
        from src.llms.native_cli.zcode import ZCodeCliAdapter
        from src.llms.native_cli.process import ProcessOutput

        monkeypatch.setattr(
            "src.llms.native_cli.zcode._LOCK_PATH", tmp_path / "zcode.lock"
        )
        active = 0
        maximum_active = 0
        release = asyncio.Event()

        async def fake_run(_invocation, **_kwargs):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                await release.wait()
            finally:
                active -= 1
            return ProcessOutput(stdout=ZCODE_STREAM_SAMPLE, stderr="", returncode=0)

        monkeypatch.setattr("src.llms.native_cli.zcode.run_process", fake_run)
        first = asyncio.create_task(
            ZCodeCliAdapter(binary="/opt/zcode-cli").run("one")
        )
        await asyncio.sleep(0.05)
        second = asyncio.create_task(
            ZCodeCliAdapter(binary="/opt/zcode-cli").run("two")
        )
        await asyncio.sleep(0.05)
        assert maximum_active == 1
        release.set()
        assert (await first).text == "ok"
        assert (await second).text == "ok"
        assert maximum_active == 1

    @pytest.mark.asyncio
    async def test_a_queued_call_gives_up_instead_of_waiting_forever(
        self, monkeypatch, tmp_path
    ):
        """A call that cannot get the provider lock fails with a timeout.

        The lock is shared with every other ZCode call on the host, so a
        queued call must not wait behind another turn indefinitely.
        """
        import fcntl

        from src.llms.native_cli.zcode import ZCodeCliAdapter
        from src.llms.native_cli.process import ProcessOutput

        lock_path = tmp_path / "zcode.lock"
        monkeypatch.setattr("src.llms.native_cli.zcode._LOCK_PATH", lock_path)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        launched = []

        async def fake_run(_invocation, **_kwargs):
            launched.append(True)
            return ProcessOutput(stdout=ZCODE_STREAM_SAMPLE, stderr="", returncode=0)

        monkeypatch.setattr("src.llms.native_cli.zcode.run_process", fake_run)
        began = time.monotonic()
        try:
            with pytest.raises(NativeCliError, match="timed out"):
                # The outer wait keeps an unbounded lock from hanging the
                # suite instead of failing it.
                await asyncio.wait_for(
                    ZCodeCliAdapter(
                        binary="/opt/zcode-cli", timeout_seconds=0.2
                    ).run("hello"),
                    timeout=10,
                )
            assert time.monotonic() - began < 5
            # The CLI was never launched: the call never got the lock.
            assert not launched
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @pytest.mark.asyncio
    async def test_a_failed_process_without_a_transcript_is_not_a_success(
        self, monkeypatch, tmp_path
    ):
        from src.llms.native_cli.zcode import ZCodeCliAdapter
        from src.llms.native_cli.process import ProcessOutput

        monkeypatch.setattr(
            "src.llms.native_cli.zcode._LOCK_PATH", tmp_path / "zcode.lock"
        )

        async def fake_run(_invocation, **_kwargs):
            return ProcessOutput(stdout="", stderr="", returncode=1)

        monkeypatch.setattr("src.llms.native_cli.zcode.run_process", fake_run)
        with pytest.raises(NativeCliError):
            await ZCodeCliAdapter(binary="/opt/zcode-cli").run("hello")

    @pytest.mark.asyncio
    async def test_a_long_transcript_still_yields_the_final_answer(
        self, monkeypatch, tmp_path
    ):
        """A run with thousands of events must not come back empty.

        This is the reported failure: the CLI streamed the whole run and then
        ended with an empty, failed result, so the answer was lost.
        """
        from src.llms.native_cli.zcode import ZCodeCliAdapter
        from src.llms.native_cli.process import ProcessOutput

        monkeypatch.setattr(
            "src.llms.native_cli.zcode._LOCK_PATH", tmp_path / "zcode.lock"
        )
        noise = "\n".join(
            json.dumps(
                {
                    "type": "tool.updated",
                    "sessionId": "zcode-session-1",
                    "payload": {"kind": "result", "result": {"content": "x" * 200}},
                }
            )
            for _ in range(2000)
        )

        async def fake_run(_invocation, **_kwargs):
            return ProcessOutput(
                stdout=f"{noise}\n{ZCODE_STREAM_SAMPLE}", stderr="", returncode=0
            )

        monkeypatch.setattr("src.llms.native_cli.zcode.run_process", fake_run)
        result = await ZCodeCliAdapter(binary="/opt/zcode-cli").run("hello")

        assert result.text == "ok"
        assert result.session_id == "zcode-session-1"
        assert result.usage.cache_read_tokens == 4096

    @pytest.mark.asyncio
    async def test_a_credential_failure_on_stderr_is_classified(
        self, monkeypatch, tmp_path
    ):
        from src.llms.native_cli.zcode import ZCodeCliAdapter
        from src.llms.native_cli.base import NativeCliUnavailable
        from src.llms.native_cli.process import ProcessOutput

        monkeypatch.setattr(
            "src.llms.native_cli.zcode._LOCK_PATH", tmp_path / "zcode.lock"
        )

        async def fake_run(_invocation, **_kwargs):
            return ProcessOutput(
                stdout="", stderr="Error: unauthorized (401)", returncode=1
            )

        monkeypatch.setattr("src.llms.native_cli.zcode.run_process", fake_run)
        with pytest.raises(NativeCliUnavailable, match="not logged in"):
            await ZCodeCliAdapter(binary="/opt/zcode-cli").run("hello")


class TestZCodeTurnScope:
    """One turn owns the provider lock, the shared config and the budget."""

    @pytest.mark.asyncio
    async def test_a_failed_turn_releases_the_lock_and_restores_the_config(
        self, monkeypatch, tmp_path
    ):
        """The scope is the only owner of the lock and the shared config.

        A turn that fails has to give both back, or the next queued call
        waits for a lock nobody will free and reads a config that is not
        its own.
        """
        import fcntl

        from src.llms.native_cli.zcode import ZCodeCliAdapter

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        lock_path = tmp_path / "zcode.lock"
        monkeypatch.setattr("src.llms.native_cli.zcode._LOCK_PATH", lock_path)
        user_config = home / ".zcode" / "cli" / "config.json"

        def lock_is_free() -> bool:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except BlockingIOError:
                    return False
            finally:
                os.close(descriptor)

        adapter = ZCodeCliAdapter(
            binary="/opt/zcode",
            mcp_servers=[{"name": "alpha", "url": "https://mcp.example.test/mcp"}],
        )
        with pytest.raises(RuntimeError, match="boom"):
            async with adapter.turn_scope("hi", cwd=str(tmp_path / "w")):
                assert not lock_is_free()
                assert user_config.is_file()
                raise RuntimeError("boom")

        assert lock_is_free()
        assert not user_config.exists()

    def test_the_scope_refuses_to_hand_out_a_spent_budget(self):
        """A turn has one budget, so the scope cannot mint a second one."""
        from src.llms.native_cli.zcode import ZCodeTurnScope

        scope = ZCodeTurnScope(
            invocation=CliInvocation(argv=("zcode",)),
            deadline=time.monotonic() - 1.0,
            timeout_seconds=1.0,
        )
        with pytest.raises(NativeCliError, match="timed out"):
            scope.remaining()

    @pytest.mark.asyncio
    async def test_the_run_gets_only_the_budget_the_lock_wait_left(
        self, monkeypatch, tmp_path
    ):
        """A queued run does not get a second full timeout either.

        The wait for the provider lock is part of the turn, so the process
        run gets only what is left of the turn's single budget.
        """
        import fcntl

        from src.llms.native_cli.process import ProcessOutput
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        lock_path = tmp_path / "zcode.lock"
        monkeypatch.setattr("src.llms.native_cli.zcode._LOCK_PATH", lock_path)
        seen: list[float] = []

        async def fake_run(invocation, **_kwargs):
            seen.append(invocation.timeout_seconds)
            return ProcessOutput(stdout=ZCODE_STREAM_SAMPLE, stderr="", returncode=0)

        monkeypatch.setattr("src.llms.native_cli.zcode.run_process", fake_run)
        adapter = ZCodeCliAdapter(binary="/opt/zcode-cli", timeout_seconds=1.0)

        # Another turn holds the provider lock, so this one has to wait.
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        async def release_soon():
            await asyncio.sleep(0.2)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

        releaser = asyncio.ensure_future(release_soon())
        try:
            await adapter.run("hello")
        finally:
            await releaser

        assert seen, "the CLI was never launched"
        assert 0 < seen[0] < 0.9, seen[0]


class TestRegistry:
    def test_remaining_domestic_agents_are_registered(self):
        from src.llms.native_cli.registry import list_adapters

        providers = {row["provider"] for row in list_adapters()}
        assert {
            "zcode", "zhipu", "glm", "minimax", "mmx", "minimax-mmx",
            "mimo", "stepfun", "step", "doubao", "arkcli",
            "claude-code", "cursor",
        } <= providers
        assert "agy" not in providers
        assert "gemini" not in providers and "qwen" not in providers

    def test_domestic_aliases_resolve_to_distinct_cli_protocols(self):
        from src.llms.native_cli.registry import get_adapter
        from src.llms.native_cli.doubao import DoubaoArkCliAdapter
        from src.llms.native_cli.minimax import MiniMaxCodeAdapter
        from src.llms.native_cli.minimax_mmx import MiniMaxMmxAdapter
        from src.llms.native_cli.stepfun import StepFunAdapter
        from src.llms.native_cli.zcode import ZCodeCliAdapter
        from src.llms.native_cli.zhipu import ZhipuCliAdapter

        assert isinstance(get_adapter("zhipu", binary="/opt/zcode"), ZhipuCliAdapter)
        assert isinstance(get_adapter("glm", binary="/opt/zcode"), ZCodeCliAdapter)
        assert isinstance(get_adapter("zcode", binary="/opt/zcode"), ZCodeCliAdapter)
        assert isinstance(get_adapter("step", binary="/opt/step"), StepFunAdapter)
        assert isinstance(get_adapter("doubao", binary="/opt/arkcli"), DoubaoArkCliAdapter)
        assert isinstance(get_adapter("minimax", binary="/opt/mcode"), MiniMaxCodeAdapter)
        assert isinstance(get_adapter("mmx", binary="/opt/mmx"), MiniMaxMmxAdapter)
        assert isinstance(
            get_adapter("minimax-mmx", binary="/opt/mmx"), MiniMaxMmxAdapter
        )


@pytest.mark.parametrize(
    ("diagnostic", "is_auth"),
    [
        ("authentication_error: invalid_api_key", True),
        ("Please login to continue", True),
        ("HTTP 403: model access denied", False),
        ("HTTP 403 Unauthorized: model access denied", False),
        ("quota exhausted", False),
    ],
)
def test_auth_failure_classifier_is_narrow(diagnostic, is_auth):
    from src.llms.native_cli.base import raise_if_auth_failure, NativeCliUnavailable

    if is_auth:
        with pytest.raises(NativeCliUnavailable, match="no usable credentials"):
            raise_if_auth_failure("fake", diagnostic)
    else:
        assert raise_if_auth_failure("fake", diagnostic) is None


def test_codex_native_thread_can_opt_into_workspace_write():
    from src.llms.native_cli.codex import CodexExecAdapter

    adapter = CodexExecAdapter(binary="/opt/codex", read_only=False)
    invocation = adapter.build_invocation("edit", cwd="/tmp/w")
    assert invocation.argv[invocation.argv.index("--sandbox") + 1] == "workspace-write"


def test_codex_passes_mcp_overrides_before_prompt():
    from src.llms.native_cli.codex import CodexExecAdapter

    adapter = CodexExecAdapter(
        binary="/opt/codex",
        read_only=False,
        mcp_servers=[
            {"name": "cn_price", "command": "uv", "args": ["run", "python", "/x.py"]}
        ],
    )
    invocation = adapter.build_invocation("hello", cwd="/tmp/w")
    flags = [invocation.argv[i + 1] for i, part in enumerate(invocation.argv) if part == "-c"]
    assert any("mcp_servers.cn_price.command" in flag for flag in flags)
    assert any("mcp_servers.cn_price.args" in flag for flag in flags)
    assert invocation.argv[-1] == "-"


def test_codex_native_thread_can_use_automatic_review():
    from src.llms.native_cli.codex import CodexExecAdapter

    adapter = CodexExecAdapter(
        binary="/opt/codex", read_only=False, approve_for_me=True
    )
    invocation = adapter.build_invocation("edit", cwd="/tmp/w")
    assert "--approve-for-me" in invocation.argv
    assert "--sandbox" not in invocation.argv
    resumed = adapter.build_invocation("again", session_id="s-1", cwd="/tmp/w")
    assert "--approve-for-me" not in resumed.argv
    assert "-C" not in resumed.argv


class TestStreamProcess:
    @pytest.mark.asyncio
    async def test_streams_lines_before_process_exit(self):
        from src.llms.native_cli.process import stream_process

        invocation = CliInvocation(
            argv=("/bin/sh", "-c", "printf 'one\\ntwo\\n'"),
        )
        lines = [line async for line in stream_process(invocation)]
        assert lines == ["one", "two"]

    @pytest.mark.asyncio
    async def test_stream_nonzero_exit_raises_with_stderr(self):
        from src.llms.native_cli.process import stream_process

        invocation = CliInvocation(
            argv=("/bin/sh", "-c", "echo nope >&2; exit 7"),
        )
        with pytest.raises(NativeCliError, match="exited 7"):
            async for _line in stream_process(invocation):
                pass


class TestCodexStream:
    def test_parse_line_maps_agent_message_and_tool(self):
        from src.server.services.native_runtime.codex_stream import parse_codex_line

        events = parse_codex_line(
            json.dumps({"type": "thread.started", "thread_id": "t-1"})
        )
        assert events[0].kind == "session_started"
        assert events[0].session_id == "t-1"
        events = parse_codex_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "i1", "type": "agent_message", "text": "hello"},
                }
            )
        )
        assert events[0].kind == "text_delta"
        assert events[0].text == "hello"
        events = parse_codex_line(
            json.dumps(
                {
                    "type": "item.started",
                    "item": {"id": "t1", "type": "mcp_tool_call", "tool": "cn_price"},
                }
            )
        )
        assert events[0].kind == "tool_started"
        assert events[0].tool_name == "cn_price"
        assert events[0].tool_call_id == "t1"

    def test_turn_usage_maps_cache(self):
        from src.server.services.native_runtime.codex_stream import parse_codex_line

        events = parse_codex_line(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 900,
                        "output_tokens": 2,
                    },
                }
            )
        )
        assert events[0].kind == "usage"
        assert events[0].data["cache_read_tokens"] == 900


class TestDshAccountAuth:
    def test_account_patch_is_added_only_when_explicitly_enabled(self, tmp_path):
        from src.llms.native_cli.deepseek import DeepSeekDshAdapter

        adapter = DeepSeekDshAdapter(
            binary="/opt/dsh", harness_home=str(tmp_path), account_auth=True
        )
        invocation = adapter.build_invocation("hello", cwd="/tmp")
        assert "--patch" in invocation.argv
        patch = invocation.argv[invocation.argv.index("--patch") + 1]
        assert patch.endswith(".yml")
        assert "alpha-native-patch-" in patch
        assert "provider: deepseek-account" in Path(patch).read_text()
        assert invocation.env["DSH_HOME"] == str(tmp_path)

    def test_api_key_disables_account_patch_by_default(self, tmp_path, monkeypatch):
        from src.llms.native_cli.deepseek import DeepSeekDshAdapter

        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        adapter = DeepSeekDshAdapter(binary="/opt/dsh", harness_home=str(tmp_path))
        invocation = adapter.build_invocation("hello")
        assert "--patch" not in invocation.argv


def test_dsh_patch_can_include_native_mcp_servers(tmp_path):
    from src.llms.native_cli.deepseek import DeepSeekDshAdapter

    (tmp_path / ".credentials.yaml").write_text("version: 1\n")
    adapter = DeepSeekDshAdapter(
        binary="/opt/dsh",
        harness_home=str(tmp_path),
        account_auth=True,
        mcp_servers=[
            {
                "name": "cn_price",
                "command": "uv",
                "args": ["run", "--directory", "/repo", "python", "/repo/x.py"],
            }
        ],
    )
    invocation = adapter.build_invocation("hello")
    patch = Path(invocation.argv[invocation.argv.index("--patch") + 1]).read_text()
    assert "name: '@deepseek-ai/dsh-mcp-client'" in patch
    assert "serverName: cn_price" in patch
    assert "python" in patch


class TestDshStream:
    def test_parse_line_maps_session_text_tools_and_usage(self):
        from src.server.services.native_runtime.dsh_stream import parse_dsh_line

        assert parse_dsh_line(
            json.dumps({"type": "session", "sessionId": "s-1"})
        )[0].kind == "session_started"
        assert parse_dsh_line(
            json.dumps({"type": "text", "text": "hello"})
        )[0].text == "hello"
        tool = parse_dsh_line(
            json.dumps(
                {
                    "type": "tool_call",
                    "callId": "c-1",
                    "tool": "mcp__cn_price__get_index_history",
                    "input": {"symbol": "000001.SH"},
                }
            )
        )[0]
        assert tool.kind == "tool_started"
        assert tool.tool_name == "mcp__cn_price__get_index_history"
        result = parse_dsh_line(
            json.dumps(
                {
                    "type": "tool_result",
                    "callId": "c-1",
                    "status": "completed",
                    "result": "3648.30",
                }
            )
        )[0]
        assert result.kind == "tool_result"
        assert result.data["content"] == "3648.30"
        usage = parse_dsh_line(
            json.dumps(
                {
                    "type": "status",
                    "phase": "step_end",
                    "usage": {
                        "inputTokens": 100,
                        "outputTokens": 2,
                        "cacheReadTokens": 900,
                    },
                }
            )
        )[0]
        assert usage.kind == "usage"
        assert usage.data["cache_read_tokens"] == 900


class TestMiniMaxApiKey:
    @pytest.mark.asyncio
    async def test_provider_setup_uses_env_key_and_marks_ready(self, tmp_path, monkeypatch):
        from src.llms.native_cli.minimax import MiniMaxCodeAdapter
        from src.llms.native_cli.process import ProcessOutput

        calls = []

        async def fake_run(invocation):
            calls.append(invocation)
            return ProcessOutput(stdout="", stderr="", returncode=0)

        monkeypatch.setattr("src.llms.native_cli.process.run_process", fake_run)
        adapter = MiniMaxCodeAdapter(
            binary="/opt/mcode",
            api_key="sk-test",
            data_dir=str(tmp_path),
        )
        await adapter._ensure_api_provider()
        assert calls[0].argv[1:3] == ("provider", "add")
        assert calls[0].env["MCODE_PROVIDER_API_KEY"] == "sk-test"
        assert calls[0].env["MINIMAX_DATA_DIR"] == str(tmp_path)
        assert (tmp_path / ".alpha-provider-ready").is_file()
        invocation = adapter.build_invocation("hello")
        assert "--model" not in invocation.argv
        assert invocation.env["MINIMAX_DATA_DIR"] == str(tmp_path)

    def test_without_key_keeps_plain_exec(self, monkeypatch):
        from src.llms.native_cli.minimax import MiniMaxCodeAdapter

        adapter = MiniMaxCodeAdapter(binary="/opt/mcode")
        invocation = adapter.build_invocation("hello")
        assert invocation.env is None


class TestMiniMaxStream:
    def test_parse_items_and_tool(self):
        from src.server.services.native_runtime.minimax_stream import parse_minimax_line

        assert parse_minimax_line(
            json.dumps({"type": "session.started", "sessionId": "s-1"})
        )[0].kind == "session_started"
        assert parse_minimax_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "content": "hello"},
                }
            )
        )[0].text == "hello"
        tool = parse_minimax_line(
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "type": "tool_call",
                        "toolCall": {
                            "id": "c-1",
                            "name": "get_index_spot",
                            "status": "running",
                            "input": {"symbol": "000001.SH"},
                        },
                    },
                }
            )
        )[0]
        assert tool.kind == "tool_started"
        assert tool.tool_name == "get_index_spot"
        result = parse_minimax_line(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "tool_call",
                        "toolCall": {
                            "id": "c-1",
                            "name": "get_index_spot",
                            "status": "completed",
                            "output": "3648.30",
                        },
                    },
                }
            )
        )[0]
        assert result.kind == "tool_result"
        assert result.data["content"] == "3648.30"

    def test_stream_format_invocation(self):
        from src.llms.native_cli.minimax import MiniMaxCodeAdapter

        invocation = MiniMaxCodeAdapter(binary="/opt/mcode").build_invocation(
            "hello", stream=True
        )
        assert invocation.argv[invocation.argv.index("--output-format") + 1] == "stream-json"


def test_mimo_token_plan_env_is_not_argv():
    from src.llms.native_cli.mimo import MiMoCodeAdapter

    adapter = MiMoCodeAdapter(
        binary="/opt/mimo",
        api_key="tp-test",
        base_url="https://token-plan-sgp.xiaomimimo.com/v1",
        model="mimo-v2.6-pro",
    )
    invocation = adapter.build_invocation("hello")
    assert invocation.env["MIMOCODE_MIMO_API_KEY"] == "tp-test"
    assert "tp-test" not in invocation.argv
    assert '"model": "xiaomi/mimo-v2.6-pro"' in invocation.env["MIMOCODE_CONFIG_CONTENT"]


def test_native_mcp_http_is_bridged_without_headers():
    from src.server.services.native_runtime.mcp import NativeMCPServer, codex_mcp_overrides

    server = NativeMCPServer(name="remote_public", url="https://mcp.example.test/mcp")
    assert server.as_dict()["transport"] == "http"
    assert server.as_dict()["url"] == "https://mcp.example.test/mcp"
    overrides = codex_mcp_overrides([server])
    assert overrides == ['mcp_servers.remote_public.url="https://mcp.example.test/mcp"']

    from src.llms.native_cli.deepseek import DeepSeekDshAdapter

    adapter = DeepSeekDshAdapter(
        binary="/opt/dsh",
        harness_home=str(__import__("tempfile").mkdtemp()),
        mcp_servers=[server.as_dict()],
    )
    invocation = adapter.build_invocation("hello")
    patch = Path(invocation.argv[invocation.argv.index("--patch") + 1]).read_text()
    assert "transport: streamable-http" in patch
    assert "https://mcp.example.test/mcp" in patch


def test_mimo_mcp_config_and_yolo():
    import json as _json
    from src.llms.native_cli.mimo import MiMoCodeAdapter

    adapter = MiMoCodeAdapter(
        binary="/opt/mimo",
        api_key="tp-test",
        mcp_servers=[
            {"name": "cn_price", "command": "uv", "args": ["run", "python", "/x.py"]},
            {"name": "remote_public", "url": "https://mcp.example.test/mcp"},
        ],
        yolo=True,
    )
    invocation = adapter.build_invocation("hello")
    assert "--yolo" in invocation.argv
    config = _json.loads(invocation.env["MIMOCODE_CONFIG_CONTENT"])
    assert config["mcp"]["cn_price"]["type"] == "local"
    assert config["mcp"]["cn_price"]["command"][0] == "uv"
    assert config["mcp"]["remote_public"] == {
        "type": "remote",
        "url": "https://mcp.example.test/mcp",
        "enabled": True,
    }
    assert config["permission"]["external_directory"] == "deny"


def test_minimax_project_mcp_merges_and_restores(tmp_path):
    import json as _json
    from src.llms.native_cli.minimax import MiniMaxCodeAdapter

    original = {"mcpServers": {"existing": {"type": "stdio", "command": "x"}}}
    (tmp_path / ".mcp.json").write_text(_json.dumps(original))
    adapter = MiniMaxCodeAdapter(
        binary="/opt/mcode",
        mcp_servers=[
            {"name": "cn_price", "command": "uv", "args": ["run", "python", "/x.py"]},
            {"name": "remote_public", "url": "https://mcp.example.test/mcp"},
        ],
    )
    adapter._write_project_mcp(str(tmp_path))
    written = _json.loads((tmp_path / ".mcp.json").read_text())
    assert "existing" in written["mcpServers"]
    assert written["mcpServers"]["cn_price"]["type"] == "stdio"
    assert written["mcpServers"]["remote_public"] == {
        "type": "http",
        "url": "https://mcp.example.test/mcp",
        "enabled": True,
    }
    adapter._restore_project_mcp()
    assert _json.loads((tmp_path / ".mcp.json").read_text()) == original


def test_minimax_project_mcp_removes_when_absent(tmp_path):
    from src.llms.native_cli.minimax import MiniMaxCodeAdapter

    adapter = MiniMaxCodeAdapter(
        binary="/opt/mcode",
        mcp_servers=[{"name": "cn_price", "command": "uv", "args": []}],
    )
    adapter._write_project_mcp(str(tmp_path))
    assert (tmp_path / ".mcp.json").is_file()
    adapter._restore_project_mcp()
    assert not (tmp_path / ".mcp.json").exists()


def test_mcp_env_reaches_dsh_patch_and_minimax_config(tmp_path):
    import json as _json
    from src.llms.native_cli.deepseek import DeepSeekDshAdapter
    from src.llms.native_cli.minimax import MiniMaxCodeAdapter
    from src.server.services.native_runtime.mcp import NativeMCPServer

    server = NativeMCPServer(
        name="conductor_memory",
        command="uv",
        args=("run", "python", "/x.py"),
        env=(("TOKEN", "abc"),),
    )
    assert server.as_dict()["env"] == {"TOKEN": "abc"}

    dsh = DeepSeekDshAdapter(
        binary="/opt/dsh",
        harness_home=str(tmp_path / "dsh-test-home"),
        mcp_servers=[server.as_dict()],
    )
    invocation = dsh.build_invocation("hi")
    patch = Path(invocation.argv[invocation.argv.index("--patch") + 1]).read_text()
    assert '"TOKEN": "abc"' in patch

    minimax = MiniMaxCodeAdapter(binary="/opt/mcode", mcp_servers=[server.as_dict()])
    import tempfile
    from pathlib import Path as _Path
    with tempfile.TemporaryDirectory() as tmp:
        minimax._write_project_mcp(tmp)
        data = _json.loads((_Path(tmp) / ".mcp.json").read_text())
        assert data["mcpServers"]["conductor_memory"]["env"]["TOKEN"] == "abc"
        minimax._restore_project_mcp()


def test_codex_mcp_env_uses_env_vars_not_argv():
    from src.llms.native_cli.codex import CodexExecAdapter

    adapter = CodexExecAdapter(
        binary="/opt/codex",
        read_only=False,
        mcp_servers=[
            {
                "name": "conductor_memory",
                "command": "uv",
                "args": ["run", "python", "/x.py"],
                "env": {"CONDUCTOR_MEMORY_TOKEN": "secret-token"},
            }
        ],
    )
    invocation = adapter.build_invocation("hi", cwd="/tmp")
    assert "secret-token" not in invocation.argv
    assert "mcp_servers.conductor_memory.env_vars" in " ".join(invocation.argv)
    assert invocation.env["CONDUCTOR_MEMORY_TOKEN"] == "secret-token"


class TestNativeAgentRouting:
    @pytest.mark.asyncio
    async def test_auto_provider_uses_router_choice(self, monkeypatch, tmp_path):
        from ptc_agent.agent.tools import native_agent as module

        calls = []

        class FakeAdapter:
            pass

        async def fake_run(adapter, prompt, **kwargs):
            calls.append(kwargs)
            return NativeCliResult(
                provider="codex", text="routed answer", session_id="s-1"
            )

        async def fake_auto_candidates(prompt):
            return ["codex"], "coding affinity"

        monkeypatch.setenv("NATIVE_CLI_ENABLED", "1")
        monkeypatch.setenv("NATIVE_CLI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setattr(
            module, "_auto_candidates_async", fake_auto_candidates
        )
        monkeypatch.setattr(module, "get_adapter", lambda provider: FakeAdapter())
        monkeypatch.setattr(module, "run_with_session", fake_run)
        monkeypatch.setattr(module, "NativeCliSessionStore", lambda: "store")

        tool = module.create_native_agent_tool(thread_id="thread-auto")
        result = await tool.ainvoke({"prompt": "refactor code", "provider": "auto"})
        assert result == "routed answer"
        assert calls[0]["session_key"] == "thread:thread-auto:codex"

    @pytest.mark.asyncio
    async def test_auto_without_native_runtime_returns_visible_error(self, monkeypatch):
        from ptc_agent.agent.tools import native_agent as module

        monkeypatch.setenv("NATIVE_CLI_ENABLED", "1")

        async def no_candidates(prompt):
            return [], "no eligible native runtime"

        monkeypatch.setattr(
            module, "_auto_candidates_async", no_candidates
        )
        tool = module.create_native_agent_tool(thread_id="thread-auto")
        result = await tool.ainvoke({"prompt": "chat", "provider": "auto"})
        assert "NativeAgent (auto) failed" in result
        assert "no eligible native runtime" in result


class TestNativeAgentMcpBridge:
    @pytest.mark.asyncio
    async def test_mcp_servers_are_passed_to_supported_adapter(
        self, monkeypatch, tmp_path
    ):
        from ptc_agent.agent.tools import native_agent as module

        captured = {}
        adapter_seen = {}

        async def fake_mcp(**kwargs):
            captured.update(kwargs)
            return [{"name": "cn_price", "command": "uv"}]

        class FakeAdapter:
            pass

        def fake_get_adapter(provider, **kwargs):
            adapter_seen["provider"] = provider
            adapter_seen["kwargs"] = kwargs
            return FakeAdapter()

        async def fake_run(adapter, prompt, **kwargs):
            return NativeCliResult(provider="codex", text="ok", session_id="s")

        monkeypatch.setenv("NATIVE_CLI_ENABLED", "1")
        monkeypatch.setenv("NATIVE_CLI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setattr(module, "_native_mcp_servers", fake_mcp)
        monkeypatch.setattr(module, "get_adapter", fake_get_adapter)
        monkeypatch.setattr(module, "run_with_session", fake_run)
        monkeypatch.setattr(module, "NativeCliSessionStore", lambda: "store")

        tool = module.create_native_agent_tool(
            thread_id="t-mcp", user_id="u-1", workspace_id="ws-1"
        )
        await tool.ainvoke({"prompt": "use market tool", "provider": "codex"})
        assert captured == {"thread_id": "t-mcp", "user_id": "u-1", "workspace_id": "ws-1"}
        assert adapter_seen["provider"] == "codex"
        assert adapter_seen["kwargs"]["mcp_servers"] == [{"name": "cn_price", "command": "uv"}]
        assert adapter_seen["kwargs"]["read_only"] is False
        assert adapter_seen["kwargs"]["approve_for_me"] is True

    @pytest.mark.asyncio
    async def test_bridge_contains_memory_and_skills_servers(self, monkeypatch):
        from ptc_agent.agent.tools import native_agent as module

        async def fake_builtin(**kwargs):
            return []

        monkeypatch.setattr(
            "src.server.services.native_runtime.mcp.collect_builtin_mcp_servers",
            fake_builtin,
        )
        monkeypatch.setattr(
            "src.server.services.native_runtime.memory_token.issue_memory_token",
            lambda *args, **kwargs: "token",
        )
        servers = await module._native_mcp_servers(
            thread_id="t-1", user_id="u-1", workspace_id="ws-1"
        )
        names = [server["name"] for server in servers]
        assert names[:2] == ["conductor_skills", "conductor_memory"]
        assert servers[0]["env"]["CONDUCTOR_API_BASE"] == "http://127.0.0.1:8000"


    @pytest.mark.asyncio
    async def test_auto_falls_through_to_next_live_provider(self, monkeypatch, tmp_path):
        from ptc_agent.agent.tools import native_agent as module

        calls = []

        class FakeAdapter:
            pass

        def fake_get_adapter(provider, **kwargs):
            calls.append(provider)
            return FakeAdapter()

        async def fake_run(adapter, prompt, **kwargs):
            if calls[-1] == "codex":
                raise NativeCliError("codex auth expired")
            return NativeCliResult(provider="deepseek-dsh", text="fallback answer", session_id="s2")

        async def fake_auto_candidates(prompt):
            return ["codex", "deepseek-dsh"], "coding affinity"

        monkeypatch.setenv("NATIVE_CLI_ENABLED", "1")
        monkeypatch.setenv("NATIVE_CLI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setattr(
            module,
            "_auto_candidates_async",
            fake_auto_candidates,
        )
        monkeypatch.setattr(module, "get_adapter", fake_get_adapter)
        monkeypatch.setattr(module, "run_with_session", fake_run)
        monkeypatch.setattr(module, "NativeCliSessionStore", lambda: "store")

        tool = module.create_native_agent_tool(thread_id="thread-fallback")
        result = await tool.ainvoke({"prompt": "refactor", "provider": "auto"})
        assert result == "fallback answer"
        assert calls == ["codex", "deepseek-dsh"]


class TestProcessCancellation:
    @pytest.mark.asyncio
    async def test_cancelling_run_process_kills_the_cli(self):
        import asyncio
        import sys
        import time

        invocation = CliInvocation(
            argv=(sys.executable, "-c", "import time; time.sleep(30)"),
            timeout_seconds=30,
        )
        started = time.monotonic()
        task = asyncio.create_task(run_process(invocation))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 5


class TestNativeAgentsParallel:
    @pytest.mark.asyncio
    async def test_parallel_tool_returns_results_in_input_order(self, monkeypatch):
        from ptc_agent.agent.tools import native_agent as module

        calls = []

        async def fake_delegate(**kwargs):
            calls.append(kwargs)
            return f"answer for {kwargs['prompt']}"

        monkeypatch.setattr(module, "_delegate", fake_delegate)
        _single, batch = module.create_native_agent_tools(thread_id="t-batch")
        result = await batch.ainvoke({
            "tasks": [
                {"prompt": "research one", "provider": "auto"},
                {"prompt": "research two", "provider": "deepseek-dsh"},
            ]
        })
        assert "### Native task 1 [auto]" in result
        assert "answer for research one" in result
        assert "### Native task 2 [deepseek-dsh]" in result
        assert "answer for research two" in result
        assert [call["prompt"] for call in calls] == ["research one", "research two"]
        assert calls[0]["lane"] == "batch0"
        assert calls[1]["lane"] == "batch1"

    @pytest.mark.asyncio
    async def test_parallel_tool_rejects_an_empty_task_list(self):
        from ptc_agent.agent.tools import native_agent as module

        _single, batch = module.create_native_agent_tools(thread_id="t-batch")
        result = await batch.ainvoke({"tasks": []})
        assert "NativeAgents failed" in result


class TestNativeAgentTimeout:
    @pytest.mark.asyncio
    async def test_timeout_is_forwarded_to_the_adapter(self, monkeypatch, tmp_path):
        from ptc_agent.agent.tools import native_agent as module

        seen = {}

        def fake_get_adapter(provider, **kwargs):
            seen.update(kwargs)
            return object()

        async def fake_run(adapter, prompt, **kwargs):
            return NativeCliResult(provider="deepseek-dsh", text="ok", session_id="s")

        monkeypatch.setenv("NATIVE_CLI_ENABLED", "1")
        monkeypatch.setenv("NATIVE_CLI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setattr(module, "get_adapter", fake_get_adapter)
        monkeypatch.setattr(module, "run_with_session", fake_run)
        monkeypatch.setattr(module, "NativeCliSessionStore", lambda: "store")
        tool = module.create_native_agent_tool(thread_id="t-timeout")
        result = await tool.ainvoke({
            "prompt": "x", "provider": "deepseek-dsh", "timeout_seconds": 30
        })
        assert result == "ok"
        assert seen["timeout_seconds"] == 30.0


class TestNativeAgentProgress:
    @pytest.mark.asyncio
    async def test_progress_events_are_emitted_on_the_custom_stream(
        self, monkeypatch, tmp_path
    ):
        from ptc_agent.agent.tools import native_agent as module

        emitted: list[dict] = []

        def fake_run(adapter, prompt, **kwargs):
            async def result():
                return NativeCliResult(
                    provider="deepseek-dsh",
                    text="ok",
                    session_id="s",
                    usage=NativeCliUsage(cache_read_tokens=12, input_tokens=3),
                )
            return result()

        monkeypatch.setenv("NATIVE_CLI_ENABLED", "1")
        monkeypatch.setenv("NATIVE_CLI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setattr(module, "get_adapter", lambda provider: object())
        monkeypatch.setattr(module, "run_with_session", fake_run)
        monkeypatch.setattr(module, "NativeCliSessionStore", lambda: "store")
        monkeypatch.setattr("langgraph.config.get_stream_writer", lambda: emitted.append)

        tool = module.create_native_agent_tool(thread_id="t-progress")
        await tool.ainvoke({"prompt": "x", "provider": "deepseek-dsh"})
        assert [event["phase"] for event in emitted] == ["started", "completed"]
        assert emitted[0]["provider"] == "deepseek-dsh"
        assert emitted[1]["cache_read_tokens"] == 12


class TestNativeAgentCredentials:
    @pytest.mark.asyncio
    async def test_minimax_user_key_is_passed_to_adapter(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NATIVE_CLI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        from ptc_agent.agent.tools import native_agent as module

        async def key(_user):
            return "mm-key"

        seen = {}

        def fake_get_adapter(provider, **kwargs):
            seen.update(kwargs)
            return object()

        async def fake_run(adapter, prompt, **kwargs):
            return NativeCliResult(provider="minimax", text="ok", session_id="s")

        monkeypatch.setenv("NATIVE_CLI_ENABLED", "1")
        monkeypatch.setattr(
            "src.server.services.native_runtime.credentials.minimax_api_key", key
        )
        monkeypatch.setattr(module, "get_adapter", fake_get_adapter)
        monkeypatch.setattr(module, "run_with_session", fake_run)
        monkeypatch.setattr(module, "NativeCliSessionStore", lambda: "store")

        tool = module.create_native_agent_tool(thread_id="t-mm", user_id="u-1")
        result = await tool.ainvoke({"prompt": "x", "provider": "minimax"})
        assert result == "ok"
        assert seen["api_key"] == "mm-key"


class TestNativeAgentApproval:
    @pytest.mark.asyncio
    async def test_rejected_approval_never_starts_the_cli(self, monkeypatch, tmp_path):
        from ptc_agent.agent.tools import native_agent as module

        monkeypatch.setenv("NATIVE_CLI_ENABLED", "1")
        monkeypatch.setenv("NATIVE_CLI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setattr(
            "langgraph.types.interrupt",
            lambda payload: {"decisions": [{"type": "reject"}]},
        )
        called = []
        monkeypatch.setattr(module, "get_adapter", lambda provider: called.append(provider))

        tool = module.create_native_agent_tool(thread_id="t-approval")
        result = await tool.ainvoke({
            "prompt": "x", "provider": "codex", "require_approval": True
        })
        assert result == "NativeAgent rejected by user"
        assert called == []

    @pytest.mark.asyncio
    async def test_approved_delegation_runs(self, monkeypatch, tmp_path):
        from ptc_agent.agent.tools import native_agent as module

        async def fake_run(adapter, prompt, **kwargs):
            return NativeCliResult(provider="codex", text="approved-answer", session_id="s")

        monkeypatch.setenv("NATIVE_CLI_ENABLED", "1")
        monkeypatch.setenv("NATIVE_CLI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setattr(
            "langgraph.types.interrupt",
            lambda payload: {"decisions": [{"type": "approve"}]},
        )
        monkeypatch.setattr(module, "get_adapter", lambda provider: object())
        monkeypatch.setattr(module, "run_with_session", fake_run)
        monkeypatch.setattr(module, "NativeCliSessionStore", lambda: "store")

        tool = module.create_native_agent_tool(thread_id="t-approval")
        result = await tool.ainvoke({
            "prompt": "x", "provider": "codex", "require_approval": True
        })
        assert result == "approved-answer"


class TestNativeAgentsDependencies:
    @pytest.mark.asyncio
    async def test_fan_in_waits_for_every_prerequisite(self, monkeypatch):
        from ptc_agent.agent.tools import native_agent as module
        import asyncio

        first_started = asyncio.Event()
        second_started = asyncio.Event()
        join_started = asyncio.Event()
        first_finished = asyncio.Event()
        release_first = asyncio.Event()
        release_second = asyncio.Event()

        async def fake_delegate(**kwargs):
            lane = kwargs["lane"]
            if lane == "batch0":
                first_started.set()
                await release_first.wait()
                first_finished.set()
                return "first-result"
            if lane == "batch1":
                second_started.set()
                await release_second.wait()
                return "second-result"
            join_started.set()
            assert "first-result" in kwargs["prompt"]
            assert "second-result" in kwargs["prompt"]
            return "joined"

        monkeypatch.setattr(module, "_delegate", fake_delegate)
        _single, batch = module.create_native_agent_tools(thread_id="t-dag-fanin")
        invocation = asyncio.create_task(batch.ainvoke({
            "tasks": [
                {"prompt": "first"},
                {"prompt": "second"},
                {"prompt": "join", "depends_on": [0, 1]},
            ]
        }))
        try:
            await asyncio.wait_for(
                asyncio.gather(first_started.wait(), second_started.wait()), timeout=1
            )
            release_first.set()
            await asyncio.wait_for(first_finished.wait(), timeout=1)
            assert not join_started.is_set()
            release_second.set()
            await asyncio.wait_for(join_started.wait(), timeout=1)
        finally:
            release_first.set()
            release_second.set()
        assert "joined" in await invocation

    @pytest.mark.asyncio
    async def test_invalid_concurrency_setting_uses_default(self, monkeypatch):
        from ptc_agent.agent.tools import native_agent as module
        import asyncio

        monkeypatch.setenv("NATIVE_CLI_MAX_PARALLEL", "bad")
        started = asyncio.Event()
        release = asyncio.Event()
        count = 0

        async def fake_delegate(**kwargs):
            nonlocal count
            count += 1
            if count == 4:
                started.set()
            await release.wait()
            return kwargs["lane"]

        monkeypatch.setattr(module, "_delegate", fake_delegate)
        _single, batch = module.create_native_agent_tools(thread_id="t-dag-config")
        invocation = asyncio.create_task(batch.ainvoke({
            "tasks": [{"prompt": str(i)} for i in range(4)]
        }))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            assert count == 4
        finally:
            release.set()
        await invocation

    @pytest.mark.asyncio
    async def test_concurrency_limit_bounds_active_tasks(self, monkeypatch):
        from ptc_agent.agent.tools import native_agent as module
        import asyncio

        monkeypatch.setenv("NATIVE_CLI_MAX_PARALLEL", "2")
        release = asyncio.Event()
        two_started = asyncio.Event()
        started = []

        async def fake_delegate(**kwargs):
            started.append(kwargs["lane"])
            if len(started) == 2:
                two_started.set()
            await release.wait()
            return kwargs["lane"]

        monkeypatch.setattr(module, "_delegate", fake_delegate)
        _single, batch = module.create_native_agent_tools(thread_id="t-dag-limit")
        invocation = asyncio.create_task(batch.ainvoke({
            "tasks": [{"prompt": str(i)} for i in range(3)]
        }))
        try:
            await asyncio.wait_for(two_started.wait(), timeout=1)
            assert len(started) == 2
            assert len(set(started)) == 2
        finally:
            release.set()
        result = await invocation
        assert "### Native task 3" in result

    @pytest.mark.asyncio
    async def test_parent_cancellation_cancels_active_children(self, monkeypatch):
        from ptc_agent.agent.tools import native_agent as module
        import asyncio

        cancelled = asyncio.Event()
        both_started = asyncio.Event()
        start_count = 0

        async def fake_delegate(**kwargs):
            nonlocal start_count
            start_count += 1
            if start_count == 2:
                both_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(module, "_delegate", fake_delegate)
        _single, batch = module.create_native_agent_tools(thread_id="t-dag-cancel")
        invocation = asyncio.create_task(batch.ainvoke({
            "tasks": [{"prompt": "one"}, {"prompt": "two"}]
        }))
        await asyncio.wait_for(both_started.wait(), timeout=1)
        invocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invocation
        assert cancelled.is_set()

    @pytest.mark.asyncio
    async def test_task_exception_is_reported_and_dependent_receives_it(self, monkeypatch):
        from ptc_agent.agent.tools import native_agent as module

        prompts = {}

        async def fake_delegate(**kwargs):
            prompts[kwargs["lane"]] = kwargs["prompt"]
            if kwargs["lane"] == "batch0":
                raise RuntimeError("upstream unavailable")
            return "recovered"

        monkeypatch.setattr(module, "_delegate", fake_delegate)
        _single, batch = module.create_native_agent_tools(thread_id="t-dag-error")
        result = await batch.ainvoke({
            "tasks": [{"prompt": "first"}, {"prompt": "second", "depends_on": [0]}]
        })
        assert "failed: upstream unavailable" in result
        assert "[task 0 result]\nfailed: upstream unavailable" in prompts["batch1"]
        assert "recovered" in result

    @pytest.mark.asyncio
    async def test_ready_dependency_starts_while_an_independent_branch_is_running(
        self, monkeypatch
    ):
        from ptc_agent.agent.tools import native_agent as module
        import asyncio

        started = {name: asyncio.Event() for name in ("slow", "root", "child")}
        release_slow = asyncio.Event()
        release_root = asyncio.Event()
        calls = []

        async def fake_delegate(**kwargs):
            prompt = kwargs["prompt"]
            name = "child" if prompt.startswith("child") else prompt
            calls.append(name)
            started[name].set()
            if name == "slow":
                await release_slow.wait()
            elif name == "root":
                await release_root.wait()
            return f"result-{name}"

        monkeypatch.setattr(module, "_delegate", fake_delegate)
        _single, batch = module.create_native_agent_tools(thread_id="t-dag-ready")
        invocation = asyncio.create_task(batch.ainvoke({
            "tasks": [
                {"prompt": "slow"},
                {"prompt": "root"},
                {"prompt": "child", "depends_on": [1]},
            ]
        }))
        try:
            await asyncio.wait_for(
                asyncio.gather(started["slow"].wait(), started["root"].wait()),
                timeout=1,
            )
            release_root.set()
            await asyncio.wait_for(started["child"].wait(), timeout=1)
            assert not invocation.done()
            assert calls.count("slow") == calls.count("root") == calls.count("child") == 1
        finally:
            release_slow.set()
            release_root.set()
        result = await invocation
        assert "result-root" in result and "result-child" in result

    @pytest.mark.asyncio
    async def test_dependent_runs_after_prerequisite(self, monkeypatch):
        from ptc_agent.agent.tools import native_agent as module
        import asyncio

        order = []
        prompts = {}

        async def fake_delegate(**kwargs):
            prompts[kwargs["lane"]] = kwargs["prompt"]
            order.append(("start", kwargs["prompt"]))
            if kwargs["prompt"] == "one":
                await asyncio.sleep(0.05)
            order.append(("done", kwargs["prompt"]))
            return f"result-{kwargs['prompt']}"

        monkeypatch.setattr(module, "_delegate", fake_delegate)
        _single, batch = module.create_native_agent_tools(thread_id="t-deps")
        result = await batch.ainvoke({
            "tasks": [
                {"prompt": "one", "provider": "auto"},
                {"prompt": "two", "provider": "auto", "depends_on": [0]},
            ]
        })
        done_one = order.index(("done", "one"))
        start_two = next(
            i for i, (kind, prompt) in enumerate(order)
            if kind == "start" and prompt.startswith("two")
        )
        assert done_one < start_two
        assert "result-one" in result and "result-two" in result
        assert prompts["batch0"] == "one"
        assert prompts["batch1"].startswith("two\n\n[task 0 result]\nresult-one")

    @pytest.mark.asyncio
    async def test_dependency_must_reference_an_earlier_task(self):
        from ptc_agent.agent.tools import native_agent as module

        _single, batch = module.create_native_agent_tools(thread_id="t-deps")
        result = await batch.ainvoke({
            "tasks": [
                {"prompt": "one", "depends_on": [1]},
                {"prompt": "two"},
            ]
        })
        assert "NativeAgents failed" in result
        assert "earlier task index" in result


class TestNativeAgentStreamedDelegation:
    @pytest.mark.asyncio
    async def test_streamed_turn_forwards_tool_events_and_text(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NATIVE_CLI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        from ptc_agent.agent.tools import native_agent as module
        from src.server.services.native_runtime.contracts import NativeEvent

        emitted = []

        async def fake_stream(adapter, prompt, *, session_id, cwd):
            yield NativeEvent(kind="session_started", session_id="s-1")
            yield NativeEvent(kind="tool_started", tool_name="memory",
                              tool_call_id="t-1", data={"name": "memory"})
            yield NativeEvent(kind="text_delta", text="hello")
            yield NativeEvent(kind="tool_result", tool_name="memory",
                              tool_call_id="t-1", data={"content": "ok"})
            yield NativeEvent(kind="usage", data={"cache_read_tokens": 9})
            yield NativeEvent(kind="turn_completed", session_id="s-1")

        class Store:
            def get(self, key):
                return None

            def set(self, key, value):
                self.value = value

        monkeypatch.setenv("NATIVE_CLI_ENABLED", "1")
        monkeypatch.setenv("NATIVE_CLI_STREAM_DELEGATION", "1")
        monkeypatch.setattr(
            "src.server.services.native_runtime.dsh_stream.stream_dsh_turn",
            fake_stream,
        )
        monkeypatch.setattr(module, "get_adapter", lambda provider, **kwargs: object())
        monkeypatch.setattr(module, "NativeCliSessionStore", Store)
        monkeypatch.setattr("langgraph.config.get_stream_writer", lambda: emitted.append)

        tool = module.create_native_agent_tool(thread_id="t-stream")
        result = await tool.ainvoke({"prompt": "x", "provider": "deepseek-dsh"})
        assert result == "hello"
        phases = [(event["phase"], event.get("tool_name")) for event in emitted]
        assert ("tool", "memory") in phases
        assert emitted[-1]["phase"] == "completed"
        assert emitted[-1]["cache_read_tokens"] == 9


def test_stream_delegation_can_be_scoped_to_one_provider(monkeypatch):
    from ptc_agent.agent.tools import native_agent as module

    monkeypatch.setenv("NATIVE_CLI_STREAM_DELEGATION", "codex")
    assert module._stream_delegation_providers() == {"codex"}
    monkeypatch.setenv("NATIVE_CLI_STREAM_DELEGATION", "1")
    assert module._stream_delegation_providers() == {"codex", "deepseek-dsh", "minimax"}
    monkeypatch.setenv("NATIVE_CLI_STREAM_DELEGATION", "unknown")
    assert module._stream_delegation_providers() == set()


class TestNativeAgentCommandHITL:
    @pytest.mark.asyncio
    async def test_codex_app_server_pauses_and_resumes_same_turn(self, monkeypatch, tmp_path):
        from ptc_agent.agent.tools import native_agent as module
        from src.server.services.native_runtime.approvals import (
            PendingNativeApproval,
            get_approval_broker,
        )
        from src.server.services.native_runtime.contracts import NativeEvent

        class FakeRuntime:
            provider = "codex"

            def __init__(self, **kwargs):
                self.key = kwargs["broker_thread_id"]

            async def stream_turn(self, prompt, *, session_id=None, cwd=None):
                yield NativeEvent(kind="session_started", session_id="codex-session")
                pending = PendingNativeApproval(
                    thread_id=self.key,
                    runtime=self,
                    request_id=7,
                    approval_id="a-7",
                    kind="command",
                    interrupt_payload={"action_requests": [{"name": "mcp__codex__command", "args": {"command": "echo ok"}}]},
                )
                get_approval_broker().park(pending)
                yield NativeEvent(kind="approval_requested", data={"approval_id": "a-7"})

            async def resume_turn(self, response):
                yield NativeEvent(kind="text_delta", text="approved")
                yield NativeEvent(kind="turn_completed", session_id="codex-session")

        class Store:
            values = {}

            def get(self, key):
                return self.values.get(key)

            def set(self, key, value):
                self.values[key] = value

        def fake_interrupt(payload):
            return {"decisions": [{"type": "approve"}]}

        monkeypatch.setenv("NATIVE_CLI_ENABLED", "1")
        monkeypatch.setenv("NATIVE_CLI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setattr(
            "src.server.services.native_runtime.codex_app_server.CodexAppServerRuntime",
            FakeRuntime,
        )
        monkeypatch.setattr("langgraph.types.interrupt", fake_interrupt)
        store = Store()
        text = await module._delegate_codex_hitl(
            prompt="do the thing",
            thread="t-hitl",
            sessions=store,
            mcp_servers=[],
            lane="main",
        )
        assert text == "approved"
        assert store.values["thread:t-hitl:codex"] == "codex-session"


def test_delegation_approval_can_be_forced_by_env(monkeypatch):
    from ptc_agent.agent.tools.native_agent import _approval_required

    monkeypatch.delenv("NATIVE_CLI_REQUIRE_APPROVAL", raising=False)
    assert _approval_required(False) is False
    monkeypatch.setenv("NATIVE_CLI_REQUIRE_APPROVAL", "1")
    assert _approval_required(False) is True
    assert _approval_required(True) is True




class TestZCodeNativeMode:
    def test_edit_mode_is_used_for_native_threads(self):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        invocation = ZCodeCliAdapter(binary="/opt/zcode", read_only=False).build_invocation(
            "hello", cwd="/tmp/w"
        )
        assert invocation.argv[invocation.argv.index("--mode") + 1] == "yolo"

    def test_mcp_config_is_written_and_restored(self, tmp_path, monkeypatch):
        from src.llms.native_cli.zcode import ZCodeCliAdapter

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            "src.llms.native_cli.zcode.shutil.which",
            lambda command: "/usr/bin/uv" if command == "uv" else None,
        )
        adapter = ZCodeCliAdapter(
            binary="/opt/zcode",
            read_only=False,
            mcp_servers=[
                {"name": "cn_price", "command": "uv", "args": ["run", "python", "/x.py"]}
            ],
        )
        cfg = tmp_path / ".zcode" / "config.json"
        user_cfg = tmp_path / ".zcode" / "cli" / "config.json"
        adapter._materialize_mcp(str(tmp_path))
        assert cfg.is_file()
        assert user_cfg.is_file()
        assert "cn_price" in cfg.read_text()
        assert "cn_price" in user_cfg.read_text()
        adapter._restore_mcp()
        assert not cfg.exists()
        assert not user_cfg.exists()


CURSOR_SAMPLE = "\n".join(
    [
        json.dumps({"type": "system", "subtype": "init", "session_id": "cursor-session-1"}),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "ok"}]},
            }
        ),
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "cursor-session-1",
                "result": "ok",
                "usage": {
                    "inputTokens": 120,
                    "outputTokens": 2,
                    "cacheReadTokens": 4096,
                    "totalTokens": 4218,
                },
            }
        ),
    ]
)


class TestCursor:
    def test_parses_stream_and_usage(self):
        from src.llms.native_cli.cursor import parse_cursor_stream

        text, session_id, usage, events = parse_cursor_stream(CURSOR_SAMPLE)
        assert text == "ok"
        assert session_id == "cursor-session-1"
        assert usage.cache_read_tokens == 4096
        assert len(events) == 3

    def test_error_result_is_a_loud_failure(self):
        from src.llms.native_cli.cursor import parse_cursor_stream

        payload = json.loads(CURSOR_SAMPLE.splitlines()[-1])
        payload.update({"is_error": True, "result": "login required"})
        with pytest.raises(NativeCliError, match="login required"):
            parse_cursor_stream(json.dumps(payload))

    def test_unisolated_invocation_keeps_plan_mode_without_trusting_workspace(self):
        from src.llms.native_cli.cursor import CursorAgentAdapter

        adapter = CursorAgentAdapter(binary="/opt/cursor-agent", model="auto")
        invocation = adapter.build_invocation("hello", session_id="c-1", cwd="/tmp")
        assert invocation.argv[0] == "/opt/cursor-agent"
        assert invocation.argv[invocation.argv.index("--mode") + 1] == "plan"
        assert "--trust" not in invocation.argv
        assert "--sandbox" not in invocation.argv
        assert "--force" not in invocation.argv
        assert invocation.argv[invocation.argv.index("--resume") + 1] == "c-1"
        assert invocation.argv[-1] == "hello"
        assert invocation.cwd == "/tmp"

    def test_unisolated_writable_invocation_uses_default_mode(self):
        from src.llms.native_cli.cursor import CursorAgentAdapter

        invocation = CursorAgentAdapter(
            binary="/opt/cursor-agent", read_only=False
        ).build_invocation("hello")
        assert "--trust" not in invocation.argv
        assert "--sandbox" not in invocation.argv
        assert "--force" not in invocation.argv
        assert "--mode" not in invocation.argv

    def test_read_only_request_keeps_plan_when_outer_isolation_is_writable(self, monkeypatch):
        from types import SimpleNamespace

        from src.llms.native_cli import cursor

        monkeypatch.setattr(
            cursor, "current_worker_isolation", lambda: SimpleNamespace(read_only=False)
        )
        invocation = cursor.CursorAgentAdapter(
            binary="/opt/cursor-agent", read_only=True
        ).build_invocation("hello")
        assert invocation.argv[invocation.argv.index("--mode") + 1] == "plan"
        assert invocation.argv[invocation.argv.index("--sandbox") + 1] == "disabled"

    def test_worker_isolation_trusts_workspace_and_disables_cursor_sandbox(self, monkeypatch):
        from types import SimpleNamespace

        from src.llms.native_cli import cursor

        monkeypatch.setattr(
            cursor, "current_worker_isolation", lambda: SimpleNamespace(read_only=False)
        )
        invocation = cursor.CursorAgentAdapter(
            binary="/opt/cursor-agent", read_only=False
        ).build_invocation("hello")
        assert "--trust" in invocation.argv
        assert invocation.argv[invocation.argv.index("--sandbox") + 1] == "disabled"
        assert "--force" in invocation.argv

    @pytest.mark.skipif(
        not __import__("shutil").which("sandbox-exec"),
        reason="macOS Seatbelt unavailable",
    )
    @pytest.mark.asyncio
    async def test_isolated_read_only_worker_runs_git_log_and_cannot_write(self, tmp_path):
        import shutil
        import subprocess

        from src.llms.native_cli.cursor import CursorAgentAdapter
        from src.llms.native_cli.isolation import worker_isolation

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        (workspace / "tracked.txt").write_text("read-only evidence\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "-c",
                "user.name=Cursor Test",
                "-c",
                "user.email=cursor-test@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            check=True,
        )
        fake_cli = workspace / "fake-cursor-agent"
        system_event = json.dumps(
            {"type": "system", "subtype": "init", "session_id": "fake-session"}
        )
        result_event = json.dumps(
            {"type": "result", "result": "git log ran; writes denied"}
        )
        fake_cli.write_text(
            "#!/bin/sh\n"
            'case " $* " in *" --trust "*) ;; *) exit 40 ;; esac\n'
            'case " $* " in *" --sandbox disabled "*) ;; *) exit 44 ;; esac\n'
            'case " $* " in *" --force "*) ;; *) exit 45 ;; esac\n'
            'case " $* " in *" --mode "*) exit 41 ;; esac\n'
            "git log -1 >/dev/null || exit 42\n"
            "if printf blocked > should-not-write 2>/dev/null; then exit 43; fi\n"
            f"printf '%s\n' '{system_event}'\n"
            f"printf '%s\n' '{result_event}'\n",
            encoding="utf-8",
        )
        fake_cli.chmod(0o755)

        with worker_isolation(
            workspace_id="cursor-read-only",
            root=tmp_path / "state",
            workdir=workspace,
            env_names=set(),
            runtime_read_paths=(
                ("/Library/Developer/CommandLineTools",)
                if Path("/Library/Developer/CommandLineTools").is_dir()
                else ()
            ),
            read_only=True,
        ):
            if not shutil.which("sandbox-exec") and not shutil.which("bwrap"):
                pytest.skip("no supported worker isolation runtime")
            adapter = CursorAgentAdapter(
                binary=str(fake_cli), cwd=str(workspace), read_only=True
            )
            invocation = adapter.build_invocation("read git history")
            assert "--trust" in invocation.argv
            assert invocation.argv[invocation.argv.index("--sandbox") + 1] == "disabled"
            assert "--mode" not in invocation.argv
            result = await adapter.run("read git history")

        assert result.text == "git log ran; writes denied"
        assert not (workspace / "should-not-write").exists()

    @pytest.mark.asyncio
    async def test_run_parses_output(self, monkeypatch):
        from src.llms.native_cli.cursor import CursorAgentAdapter
        from src.llms.native_cli.process import ProcessOutput

        async def fake_run(invocation):
            return ProcessOutput(stdout=CURSOR_SAMPLE, stderr="", returncode=0)

        monkeypatch.setattr("src.llms.native_cli.cursor.run_process", fake_run)
        result = await CursorAgentAdapter(binary="/opt/cursor-agent").run("hello")
        assert result.text == "ok"
        assert result.session_id == "cursor-session-1"
        assert result.events == 3


def test_login_url_joins_wrapped_lines():
    from src.server.services.native_runtime.login import _extract_url

    lines = [
        " Your browser should open automatically. If not:",
        " https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=x",
        " .apps.googleusercontent.com&code_challenge=y&state=z",
        " If you aren't automatically redirected, paste the authorization code below:",
    ]
    assert _extract_url("\n".join(lines)) == (
        "https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=x"
        ".apps.googleusercontent.com&code_challenge=y&state=z"
    )


@pytest.mark.asyncio
async def test_run_process_does_not_deadlock_on_output_before_a_large_prompt(tmp_path):
    """A child that prints before it reads stdin must not wedge the parent."""
    import sys

    from src.llms.native_cli.base import CliInvocation
    from src.llms.native_cli.process import run_process

    script = tmp_path / "chatty"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "sys.stdout.write('x' * 400000)\n"
        "sys.stdout.flush()\n"
        "data = sys.stdin.read()\n"
        "sys.stdout.write('\\nlen=%d' % len(data))\n"
    )
    script.chmod(0o755)
    output = await run_process(
        CliInvocation(argv=(str(script),), stdin="p" * 2_000_000, timeout_seconds=20),
        keep_tail=True,
    )
    assert output.stdout.endswith("len=2000000")
