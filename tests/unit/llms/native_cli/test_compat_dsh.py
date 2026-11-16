"""Real DSH against a loopback-only fake OpenAI-compatible SSE server."""

from __future__ import annotations

import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from src.config.compat import load_compat_workers
from src.llms.native_cli.base import NativeCliUsage
from src.llms.native_cli.compat import CompatDshAdapter
from src.llms.native_cli.events import parse_ndjson

ENV_NAME = "W9A_TEST_API_KEY"
SENTINEL = "sk-" "w9a-test-only-sentinel"
MODEL = "w9a-fake"
PROVIDER = "qwen-compat"
TOOL_CALL_ID = "call_w9a_compat"
TOOL_OUTPUT = "W9A_TOOL_RESULT"
FINAL_ANSWER = "W9A final answer: tool round trip completed."
PROMPT = "Call bash once, then report the tool result."
USAGE = {
    "prompt_tokens": 123,
    "completion_tokens": 17,
    "total_tokens": 140,
    "prompt_tokens_details": {"cached_tokens": 41},
}


def _sse(chunks: list[dict]) -> bytes:
    lines = [f"data: {json.dumps(chunk, separators=(',', ':'))}\n" for chunk in chunks]
    lines.append("data: [DONE]\n")
    return "\n".join(lines).encode()


def _chunk(
    response_id: str,
    delta: dict,
    finish_reason: str | None = None,
    *,
    empty_choices: bool = False,
    usage: dict | None = None,
) -> dict:
    choice = {"index": 0, "delta": delta, "finish_reason": finish_reason}
    return {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": 1760000000,
        "model": MODEL,
        "choices": [] if empty_choices else [choice],
        "usage": usage,
    }


def _first_response() -> bytes:
    return _sse(
        [
            _chunk("chatcmpl-w9a-first", {"role": "assistant", "content": ""}),
            _chunk(
                "chatcmpl-w9a-first",
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": TOOL_CALL_ID,
                            "type": "function",
                            "function": {"name": "bash", "arguments": ""},
                        }
                    ]
                },
            ),
            _chunk(
                "chatcmpl-w9a-first",
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "function": {
                                "arguments": (
                                    '{"command":"printf \'W9A_TOOL_RESULT\'",'
                                    '"description":"W9a compat loopback test"}'
                                )
                            },
                        }
                    ]
                },
            ),
            _chunk("chatcmpl-w9a-first", {}, "tool_calls"),
        ]
    )


def _second_response() -> bytes:
    return _sse(
        [
            _chunk(
                "chatcmpl-w9a-final",
                {"role": "assistant", "content": FINAL_ANSWER},
            ),
            _chunk("chatcmpl-w9a-final", {}, "stop"),
            _chunk("chatcmpl-w9a-final", {}, empty_choices=True, usage=USAGE),
        ]
    )


class _FakeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FakeHandler)
        self.requests: list[dict] = []
        self.errors: list[str] = []


class _FakeHandler(BaseHTTPRequestHandler):
    server: _FakeServer

    def log_message(self, _format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            messages = body.get("messages") or []
            record = {
                "method": "POST",
                "path": self.path,
                "authorization_matches_sentinel": self.headers.get("Authorization")
                == f"Bearer {SENTINEL}",
                "model": body.get("model"),
                "stream": body.get("stream"),
                "messages": messages,
            }
            self.server.requests.append(record)
            sequence = len(self.server.requests)
            if self.path != "/v1/chat/completions":
                payload = b"unexpected path"
                self.send_response(404)
            elif sequence == 1:
                payload = _first_response()
                self.send_response(200)
            elif sequence == 2:
                payload = _second_response()
                self.send_response(200)
            else:
                payload = b"unexpected extra request"
                self.server.errors.append(f"unexpected request {sequence}")
                self.send_response(500)
            self.send_header(
                "Content-Type", "text/event-stream" if sequence <= 2 else "text/plain"
            )
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (OSError, TypeError, ValueError) as exc:
            self.server.errors.append(f"{type(exc).__name__}: {exc}")
            self.send_response(500)
            self.send_header("Connection", "close")
            self.end_headers()


@pytest.fixture
def fake_server():
    server = _FakeServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _write_loopback_config(home: Path, port: int) -> Path:
    path = home / ".config" / "conductor" / "compat-workers.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "workers": [
                    {
                        "name": PROVIDER,
                        "protocol": "openai-completions",
                        "base_url": f"http://127.0.0.1:{port}/v1",
                        "model": MODEL,
                        "api_key_env": ENV_NAME,
                        "context_window_tokens": 32768,
                        "review_capable": False,
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


@pytest.mark.skipif(
    shutil.which("dsh") is None, reason="dsh binary is required for compat integration"
)
@pytest.mark.enable_socket
@pytest.mark.asyncio
async def test_real_dsh_completes_tool_round_trip_and_parses_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fake_server: _FakeServer,
) -> None:
    home = tmp_path / "home"
    harness_home = home / ".dsh"
    work = tmp_path / "work"
    work.mkdir()
    config_path = _write_loopback_config(home, fake_server.server_port)
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    for name, value in {
        "HOME": str(home),
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "TMPDIR": str(tmp_path),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "CI": "1",
        "NO_COLOR": "1",
        "DSH_TELEMETRY_MODE": "DISABLED",
        "DSH_PERMISSION_MODE": "danger-full-access",
        ENV_NAME: SENTINEL,
    }.items():
        monkeypatch.setenv(name, value)
    (worker,) = load_compat_workers(
        config_path,
        environ={ENV_NAME: SENTINEL},
        reserved_names={"kimi", "deepseek-dsh"},
        allow_loopback=True,
    )
    adapter = CompatDshAdapter(
        worker,
        binary=shutil.which("dsh"),
        harness_home=str(harness_home),
        timeout_seconds=90,
        cwd=str(work),
    )

    result = await adapter.run(PROMPT)

    assert len(fake_server.requests) == 2
    assert all(
        request["authorization_matches_sentinel"] for request in fake_server.requests
    )
    assert all(request["model"] == MODEL for request in fake_server.requests)
    assert all(request["stream"] is True for request in fake_server.requests)
    second_messages = fake_server.requests[1]["messages"]
    assert any(
        message.get("role") == "assistant"
        and any(
            call.get("id") == TOOL_CALL_ID
            and call.get("function", {}).get("name") == "bash"
            for call in message.get("tool_calls", [])
        )
        for message in second_messages
    )
    assert any(
        message.get("role") == "tool"
        and message.get("tool_call_id") == TOOL_CALL_ID
        and TOOL_OUTPUT in str(message.get("content"))
        for message in second_messages
    )
    assert result.provider == PROVIDER
    assert result.text == FINAL_ANSWER
    assert result.session_id
    assert result.events > 0
    assert result.usage == NativeCliUsage(
        input_tokens=82,
        output_tokens=17,
        cache_read_tokens=41,
        cache_write_tokens=0,
        total_tokens=140,
    )
    reparsed = parse_ndjson("\n".join(json.dumps(event) for event in result.raw))
    assert reparsed.text == FINAL_ANSWER
    assert reparsed.usage == result.usage
    event_types = [event.get("type") for event in result.raw]
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert "final" in event_types
    assert not fake_server.errors
    assert SENTINEL not in caplog.text
    assert SENTINEL not in repr(result.raw)
