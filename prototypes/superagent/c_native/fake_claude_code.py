"""Fake Claude Code CLI: the stand-in the native-CLI prototype drives.

This is a small stand-in for the real ``claude`` binary. It speaks the real
CLI's flags, so the argv the adapter builds for it is the argv production
would use, and it refuses to run when the tool restriction is not in place.

It also refuses to run without ``--bare``, ``--no-session-persistence`` and an
empty ``--setting-sources``: those are what keep the real binary from loading
the user's settings, hooks, plugins and CLAUDE.md, from offering its
``DeferredToolPlaceholder`` tool, and from writing a session to disk. Pinning
them here pins them in the adapter's argv.

It is deliberately **thin**. It owns no turn state: the credit gate, the
memory, the transcript, the events and the store live in the model shim, so
the real binary can take this stand-in's place without the turn losing them.
What the stand-in does is what the real binary does:

- it reads the prompt,
- it asks its model endpoint for the next message, over Anthropic's Messages
  API, configured the way Claude Code configures it (``ANTHROPIC_BASE_URL``,
  ``ANTHROPIC_API_KEY``),
- it lists the tools the MCP server publishes and offers them to the model
  under the names the server's name gives them (``mcp__<server>__<tool>``),
- it runs the tool the model asked for, over MCP on the server's standard
  input and output, and refuses a call for any name it did not offer,
- it sends the tool's answer back to the model, with the text the model
  wrote before the tool call, and repeats until the model stops asking for
  tools. A message with several ``tool_use`` blocks runs every one of them,
  in order, and answers them all in one user message;
- it honours the HTTP status the way the real CLI does: a non-2xx reply is an
  API error and the CLI exits non-zero, a 5xx is retried once first, and an
  ``event: error`` frame inside the stream is an error too.

The streaming parser is strict about Anthropic's content block indices: a
block that is opened twice, a delta for a block that was never opened, and a
block that is still open when the message stops are all refused. A lenient
parser would hide a broken stream instead of failing on it.

Flags this stand-in understands (the ones the adapter passes, plus the
neighbours that change the same behaviour):

    -p, --print                     run one turn and exit
    --verbose                       required with --output-format stream-json
    --output-format stream-json     the stream the adapter reads
    --model <name>                  the model the turn runs on
    --tools ""                      disable every built-in tool
    --disallowed-tools <list>       deny list, belt and braces
    --allowed-tools <list>          pre-approve the MCP tools
    --strict-mcp-config             only the servers in --mcp-config
    --mcp-config <path>             the MCP server this turn may use
    --permission-mode <mode>        dontAsk in production; plan blocks MCP calls
    --disable-slash-commands        no skills, no slash commands
    --bare                          required: no hooks, plugins, CLAUDE.md, keychain
    --no-session-persistence        required: no session written to disk
    --setting-sources ""            required: no user, project or local settings
    --system-prompt <text>          replaces the default system prompt
    --append-system-prompt <text>   appended to the default system prompt
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

MCP_PROTOCOL_VERSION = "2024-11-05"
ANTHROPIC_VERSION = "2023-06-01"
MESSAGES_PATH = "/v1/messages"

# ``mcp__<server>__<tool>`` is how an MCP-backed CLI names a server's tools.
_MCP_PREFIX = re.compile(r"^mcp__[^_].*?__(?=[^_])")

_VALUE_FLAGS = {
    "--model",
    "--output-format",
    "--input-format",
    "--permission-mode",
    "--permission-prompts",
    "--mcp-config",
    "--tools",
    "--disallowed-tools",
    "--disallowedTools",
    "--allowed-tools",
    "--allowedTools",
    "--system-prompt",
    "--append-system-prompt",
    "--settings",
    "--setting-sources",
    "--session-id",
    "--resume",
    "--add-dir",
}
_BOOL_FLAGS = {
    "-p",
    "--print",
    "--verbose",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--bare",
    "--restricted",
    "--safe-mode",
    "--no-session-persistence",
}


class CliError(RuntimeError):
    """The stand-in refuses to run."""


class ApiError(CliError):
    """The model endpoint answered with a non-2xx status or an error frame."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"API Error: {status} {message}")
        self.status = status

    @property
    def retryable(self) -> bool:
        return 500 <= self.status < 600


# How many times a 5xx is retried before the turn fails.
API_RETRIES = 1


def strip_mcp_prefix(name: str) -> str:
    """``mcp__<server>__<tool>`` -> ``<tool>``."""

    return _MCP_PREFIX.sub("", name, count=1)


def parse_flags(argv: list[str]) -> dict[str, Any]:
    """Parse the CLI flags. An unknown flag is an error, not a shrug."""

    flags: dict[str, Any] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            break
        if token.startswith("--") and "=" in token:
            name, _, value = token.partition("=")
            if name not in _VALUE_FLAGS:
                raise CliError(f"unknown flag: {name}")
            flags[name] = value
            index += 1
            continue
        if token in _VALUE_FLAGS:
            if index + 1 >= len(argv):
                raise CliError(f"flag {token} needs a value")
            flags[token] = argv[index + 1]
            index += 2
            continue
        if token in _BOOL_FLAGS:
            flags[token] = True
            index += 1
            continue
        raise CliError(f"unknown flag: {token}")
    return flags


def check_tool_restriction(flags: dict[str, Any]) -> None:
    """Refuse to run unless the built-in tools are off and MCP is pinned.

    These are the flags that make the model see the MCP tools and nothing
    else. Without them the CLI's own shell, file and web tools would be
    offered next to the autonomy tools.
    """

    if "--tools" not in flags:
        raise CliError("--tools is required; use --tools \"\" to disable the built-ins")
    if flags["--tools"] not in ("", " "):
        raise CliError(
            f"--tools must be empty for this runtime, got {flags['--tools']!r}"
        )
    if not flags.get("--strict-mcp-config"):
        raise CliError("--strict-mcp-config is required")
    if not flags.get("--mcp-config"):
        raise CliError("--mcp-config is required")
    mode = str(flags.get("--permission-mode") or "default")
    if mode == "plan":
        # Verified locally: plan mode refuses every MCP tool call.
        raise CliError("--permission-mode plan blocks MCP tool calls")
    allowed = str(flags.get("--allowed-tools") or flags.get("--allowedTools") or "")
    if "mcp__" not in allowed:
        raise CliError("--allowed-tools must pre-approve the MCP tools")
    check_isolation(flags)


def check_isolation(flags: dict[str, Any]) -> None:
    """Refuse to run unless the CLI loads nothing of the user's.

    Observed on the real 2.1.311 binary: without ``--bare`` the model is
    also offered ``DeferredToolPlaceholder``. That ``--bare`` skips hooks,
    plugins, CLAUDE.md and the keychain, and what ``--no-session-persistence``
    and ``--setting-sources`` do, is read off ``claude --help``; the full
    argv with those two flags has not been run against the real binary.
    """

    if not flags.get("--bare"):
        raise CliError("--bare is required")
    if not flags.get("--no-session-persistence"):
        raise CliError("--no-session-persistence is required")
    if "--setting-sources" not in flags:
        raise CliError('--setting-sources is required; use --setting-sources ""')
    if str(flags["--setting-sources"]).strip():
        raise CliError(
            "--setting-sources must be empty for this runtime, "
            f"got {flags['--setting-sources']!r}"
        )


class AnthropicClient:
    """The Messages API, the way Claude Code calls it."""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.socket_path = ""
        self.host = ""
        self.port = 0
        if base_url.startswith("unix://"):
            # The offline shape: the endpoint is a unix socket, not a port.
            self.socket_path = base_url[len("unix://") :]
            self.host = "localhost"
        else:
            without_scheme = base_url.split("://", 1)[-1].rstrip("/")
            host, _, port = without_scheme.partition(":")
            self.host = host or "localhost"
            self.port = int(port or 80)

    def messages(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """One model round. A 5xx is retried; any other failure is raised."""

        attempt = 0
        while True:
            try:
                return self._request(messages, tools)
            except ApiError as exc:
                if not exc.retryable or attempt >= API_RETRIES:
                    raise
                attempt += 1

    def _request(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """One request. Returns the assistant's text and tool requests."""

        payload = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": messages,
            "tools": tools,
            "stream": True,
        }
        body = json.dumps(payload).encode("utf-8")
        head = (
            f"POST {MESSAGES_PATH} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Content-Type: application/json\r\n"
            "Accept: text/event-stream\r\n"
            f"x-api-key: {self.api_key}\r\n"
            f"anthropic-version: {ANTHROPIC_VERSION}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode("latin-1")
        with self._connect() as connection:
            connection.sendall(head + body)
            stream = connection.makefile("rb")
            status_line = stream.readline()
            if not status_line:
                raise CliError("the model endpoint closed the connection")
            status = _status_code(status_line)
            headers: dict[str, str] = {}
            while True:
                line = stream.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("latin-1").partition(":")
                headers[key.strip().lower()] = value.strip()
            if not 200 <= status < 300:
                length = int(headers.get("content-length") or 0)
                raw = stream.read(length) if length else stream.read()
                raise ApiError(status, _error_message(raw))
            if headers.get("content-type", "").startswith("text/event-stream"):
                return _read_stream(stream)
            length = int(headers.get("content-length") or 0)
            raw = stream.read(length) if length else b""
            return _read_message(json.loads(raw or b"{}"))

    def _connect(self) -> socket.socket:
        if self.socket_path:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.connect(self.socket_path)
            return connection
        return socket.create_connection((self.host, self.port), timeout=300)


def _status_code(line: bytes) -> int:
    """``HTTP/1.1 200 OK`` -> 200. A line that is not a status is an error."""

    parts = line.decode("latin-1").split()
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        raise CliError(f"the model endpoint sent no HTTP status: {line!r}")
    return int(parts[1])


def _error_message(raw: bytes) -> str:
    """The message of Anthropic's error body, or the raw body."""

    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return raw.decode("utf-8", "replace").strip()
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return f"{error.get('type')}: {error.get('message')}"
    return json.dumps(body)


def _read_stream(stream: Any) -> dict[str, Any]:
    """Anthropic's server-sent event stream, folded into one message.

    The content block indices are checked the way the API checks them: a
    block is opened once, deltas only reach an open block, and every block
    is closed before the message stops. A stream that breaks those rules is
    refused here, the way the real Claude Code refuses it, instead of being
    folded together and passed off as a good one.
    """

    blocks: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    closed: set[int] = set()
    stop_reason = ""
    started = False
    for raw in stream:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        if line.startswith("event:"):
            continue
        if not line.startswith("data:"):
            continue
        data = json.loads(line[5:].strip())
        kind = data.get("type")
        if kind == "message_start":
            started = True
        elif kind == "content_block_start":
            index = int(data.get("index", -1))
            if index in blocks or index in closed:
                raise CliError(f"content block {index} was opened twice")
            blocks[index] = dict(data.get("content_block") or {})
            order.append(index)
        elif kind == "content_block_delta":
            index = int(data.get("index", -1))
            if index not in blocks:
                raise CliError(f"content block {index} got a delta before it opened")
            if index in closed:
                raise CliError(f"content block {index} got a delta after it closed")
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta":
                blocks[index]["text"] = str(blocks[index].get("text") or "") + str(
                    delta.get("text") or ""
                )
            elif delta.get("type") == "input_json_delta":
                blocks[index]["_partial"] = str(
                    blocks[index].get("_partial") or ""
                ) + str(delta.get("partial_json") or "")
        elif kind == "content_block_stop":
            index = int(data.get("index", -1))
            if index not in blocks:
                raise CliError(f"content block {index} closed without opening")
            closed.add(index)
        elif kind == "message_delta":
            stop_reason = str((data.get("delta") or {}).get("stop_reason") or "")
        elif kind == "message_stop":
            break
        elif kind == "error":
            # Anthropic's in-stream error: the 200 was sent, the round failed.
            # An overloaded endpoint says so; that one is worth a retry.
            error = data.get("error") or {}
            status = 529 if error.get("type") == "overloaded_error" else 400
            raise ApiError(status, f"{error.get('type')}: {error.get('message')}")
    if not started:
        raise CliError("the model stream ended without a message_start")
    still_open = [index for index in order if index not in closed]
    if still_open:
        raise CliError(f"content blocks {still_open} were never closed")
    for index in order:
        block = blocks[index]
        partial = block.pop("_partial", None)
        if partial is not None:
            block["input"] = json.loads(partial or "{}")
    text = "".join(
        str(blocks[index].get("text") or "")
        for index in order
        if blocks[index].get("type") == "text"
    )
    tool_uses = [
        blocks[index] for index in order if blocks[index].get("type") == "tool_use"
    ]
    return {"text": text, "tool_uses": tool_uses, "stop_reason": stop_reason}


def _read_message(payload: dict[str, Any]) -> dict[str, Any]:
    """One JSON message object, the non-streaming shape."""

    text = ""
    tool_uses: list[dict[str, Any]] = []
    for block in payload.get("content") or []:
        if block.get("type") == "text":
            text += str(block.get("text") or "")
        elif block.get("type") == "tool_use":
            tool_uses.append(dict(block))
    return {
        "text": text,
        "tool_uses": tool_uses,
        "stop_reason": str(payload.get("stop_reason") or ""),
    }


class McpClient:
    """MCP over a server's standard input and output."""

    def __init__(self, reader: Any, writer: Any) -> None:
        self._reader = reader
        self._writer = writer
        self._next_id = 0

    def call(self, method: str, **params: Any) -> Any:
        self._next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        self._writer.write(json.dumps(request).encode("utf-8") + b"\n")
        self._writer.flush()
        line = self._reader.readline()
        if not line:
            raise CliError("the MCP server closed the connection")
        reply = json.loads(line)
        if "error" in reply:
            error = reply["error"]
            raise CliError(f"MCP {method} failed: {error.get('message')}")
        return reply.get("result")

    def initialize(self) -> Any:
        return self.call(
            "initialize",
            protocolVersion=MCP_PROTOCOL_VERSION,
            capabilities={},
            clientInfo={"name": "claude-code", "version": "2.1.311"},
        )

    def list_tools(self) -> list[dict[str, Any]]:
        return list(self.call("tools/list").get("tools") or [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        result = self.call("tools/call", name=name, arguments=arguments)
        pieces = [
            str(block.get("text") or "")
            for block in result.get("content") or []
            if block.get("type") == "text"
        ]
        return "".join(pieces)


def read_mcp_config(config_path: str) -> tuple[str, dict[str, Any]]:
    """The one MCP server the config names, and the name it gives it.

    Claude Code prefixes a server's tools with the server's name, so the
    names the model sees are ``mcp__<server>__<tool>``.
    """

    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    servers = dict(config.get("mcpServers") or {})
    if len(servers) != 1:
        raise CliError(f"expected exactly one MCP server, found {len(servers)}")
    name, spec = next(iter(servers.items()))
    return name, dict(spec)


def open_mcp_channel(spec: dict[str, Any], env: dict[str, str]) -> tuple[Any, Any, Any]:
    """The MCP server's standard input and output.

    When the adapter started the server itself, the channel is the file
    descriptor it handed over. Otherwise the server is spawned from the
    ``--mcp-config`` file, the way the real Claude Code spawns it.
    """

    fd = env.get("C_NATIVE_MCP_FD")
    if fd:
        channel = os.fdopen(int(fd), "r+b", buffering=0)
        return channel, channel, None
    command = [str(spec["command"]), *[str(part) for part in spec.get("args") or []]]
    child_env = {**os.environ, **{str(k): str(v) for k, v in (spec.get("env") or {}).items()}}
    process = subprocess.Popen(
        command,
        env=child_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return process.stdout, process.stdin, process


def run_turn(
    model_client: Any, mcp: Any, prompt: str, tools: list[dict[str, Any]]
) -> dict[str, Any]:
    """The agent loop: ask the model, run its tools, repeat. Returns the end.

    Every ``tool_use`` block of a message runs, in order. The assistant
    message goes back whole — its text and all its tool calls — and the
    results go back together in one user message, each pointing at the call
    it answers.
    """

    # The names the model may ask for. A call for any other name is refused,
    # the way the real CLI refuses a tool it never offered.
    advertised = {str(tool["name"]) for tool in tools}
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": prompt}]}
    ]
    while True:
        answer = model_client.messages(messages, tools)
        tool_uses = list(answer.get("tool_uses") or [])
        if not tool_uses:
            return answer
        content: list[dict[str, Any]] = []
        text = str(answer.get("text") or "")
        if text:
            # The text the model wrote before the tool calls belongs to the
            # same assistant message, and it goes back with it.
            content.append({"type": "text", "text": text})
        results: list[dict[str, Any]] = []
        for tool_use in tool_uses:
            name = str(tool_use.get("name") or "")
            use_id = str(tool_use.get("id") or "")
            arguments = dict(tool_use.get("input") or {})
            if name in advertised:
                result = mcp.call_tool(strip_mcp_prefix(name), arguments)
            else:
                # A tool this CLI does not have. The real CLI refuses it the
                # same way; the model endpoint records the refusal.
                result = f"refused:{name}"
            content.append(
                {"type": "tool_use", "id": use_id, "name": name, "input": arguments}
            )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": use_id,
                    "content": [{"type": "text", "text": result}],
                }
            )
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": results})


def emit(stream: Any, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload) + "\n")
    stream.flush()


def main(argv: list[str]) -> int:
    try:
        flags = parse_flags(argv[1:])
        check_tool_restriction(flags)
    except CliError as exc:
        sys.stderr.write(f"claude: {exc}\n")
        return 2

    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not base_url:
        sys.stderr.write("claude: ANTHROPIC_BASE_URL is required\n")
        return 2
    model = str(flags.get("--model") or "scripted")

    try:
        server_name, spec = read_mcp_config(str(flags["--mcp-config"]))
        reader, writer, child = open_mcp_channel(spec, dict(os.environ))
    except (OSError, json.JSONDecodeError, KeyError, CliError) as exc:
        sys.stderr.write(f"claude: {exc}\n")
        return 2

    prompt = sys.stdin.read()
    mcp = McpClient(reader, writer)
    model_client = AnthropicClient(base_url, api_key, model)
    try:
        mcp.initialize()
        published = mcp.list_tools()
        tools = [
            {
                "name": f"mcp__{server_name}__{tool['name']}",
                "description": str(tool.get("description") or ""),
                "input_schema": dict(tool.get("inputSchema") or {}),
            }
            for tool in published
        ]
        answer = run_turn(model_client, mcp, prompt, tools)
        emit(
            sys.stdout,
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": answer.get("text") or ""},
            },
        )
        emit(
            sys.stdout,
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "tools": [tool["name"] for tool in tools],
            },
        )
    except (CliError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"claude: {exc}\n")
        return 1
    finally:
        try:
            writer.close()
        except OSError:
            pass
        if child is not None:
            try:
                child.terminate()
                child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
