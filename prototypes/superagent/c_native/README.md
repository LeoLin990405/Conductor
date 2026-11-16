# `c_native` — the SuperAgent inside a native CLI

This is prototype **C**: the Router and the Watchdog run inside a native agent
CLI (Claude Code), their tools come from an MCP server, and the CLI's model
traffic is served by a shim that speaks **Claude Code's own model API**. It
answers the same contract as `reference` and `reference_bridge`, so the three
can be compared on one set of tests.

```
   ANTHROPIC_BASE_URL=unix://…/model.sock         MCP over stdio
  ┌────────────┐   POST /v1/messages   ┌────────────┐   ┌────────────┐
  │  Claude    │◄─────────────────────►│ model shim │   │    MCP     │
  │  Code CLI  │   Anthropic Messages  │            │   │  server    │
  │ (per turn) │        API + SSE      └─────┬──────┘   └─────┬──────┘
  └────────────┘                             │ bridge socket     │ bridge socket
                                    ┌────────┴──────────────────┴────────┐
                                    │  contract bridge (model, store, gate,│
                                    │  memory, subscriber, tool handlers)  │
                                    └──────────────────────────────────────┘
```

| File | Role |
|---|---|
| `adapter.py` | `runtime_argv` and `make_adapter`. Launches the three processes, owns the session row. |
| `model_shim.py` | **Claude Code's model API** on a unix socket. Owns the turn: credit, memory, transcript, events, commit. |
| `mcp_server.py` | **MCP on stdio**. Publishes *only* the role's autonomy tools; dispatches through `tools.call`. |
| `fake_claude_code.py` | The fake Claude Code CLI the tests drive. A client, and nothing more. |

Everything is offline: no TCP socket is opened anywhere, and the children
inherit the contract's offline guard through `sitecustomize`.

## The turn is not in the CLI

The CLI is deliberately thin. It owns **no** turn state:

- the **credit gate**, the **memory recall**, the **transcript**, the
  **events** and the **store commit** all live in the model shim, behind the
  model API;
- the **MCP server** only dispatches: it publishes the role's tools and runs
  the call the bridge hands it;
- the **CLI** reads the prompt, asks the model endpoint for the next message,
  lists the MCP server's tools, runs every tool the model asked for in that
  message, in order, sends the answers back in one user message — together
  with the text the model wrote before the tool calls, which belongs to the
  same assistant message — and repeats until the model stops asking. The
  shim matches each `tool_result` to its `tool_use` by id.

That is what makes the swap possible: the real `claude` binary is a client
too. Give it the same `ANTHROPIC_BASE_URL`, the same `--mcp-config` and the
same flags, and the turn still has its credit gate, its memory, its
transcript and its commit — they were never in the fake.

## The model API

The shim serves **Anthropic's Messages API**, the one Claude Code calls. The
endpoint is `POST /v1/messages` and the request is Anthropic's:

```json
{"model": "scripted", "max_tokens": 4096,
 "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
 "tools": [{"name": "mcp__autonomy__autonomy_route",
            "description": "…", "input_schema": {"type": "object", "…": "…"}}],
 "stream": true}
```

With `stream` the reply is the server-sent event stream Claude Code expects:

```
event: message_start        data: {"type": "message_start", "message": {…}}
event: content_block_start  data: {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
event: content_block_delta  data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}}
event: content_block_stop   data: {"type": "content_block_stop", "index": 0}
event: message_delta        data: {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": null}, "usage": {"output_tokens": 1}}
event: message_stop         data: {"type": "message_stop"}
```

Every text chunk of a turn is a delta into **one** text block. A tool request
is a **second** block, at the next index, and the text block is closed before
it starts:

```
event: content_block_stop   data: {"type": "content_block_stop", "index": 0}
event: content_block_start  data: {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_…", "name": "mcp__autonomy__autonomy_route", "input": {}}}
event: content_block_delta  data: {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"ticket_id\": \"t1\"}"}}
event: content_block_stop   data: {"type": "content_block_stop", "index": 1}
```

A block is opened once and closed once. Reusing an index for a second block
is a protocol error, and the fake CLI's parser refuses it — it is strict
about the indices, so a broken stream fails instead of being folded together
and passed off as a good one. A model round is always streamed: Claude Code
always asks for the stream, and a non-streaming model round is not
implemented (only the credit-stop reply honours `stream: false`).

**Paths.** The path has to be exactly `/v1/messages`; a query string such as
Claude Code's `?beta=true` is ignored. `POST /v1/messages/count_tokens` is a
side request: it is answered with a rough estimate (`{"input_tokens": n}`)
and never reaches the turn — no gate, no memory, no model round, no rows.
Any other path is a `404`, any other method on `/v1/messages` a `405`.

**Status codes.** The shim answers the way Anthropic's API answers, and the
fake CLI honours the status the way the real CLI does:

| What happened | The shim answers | The fake CLI |
|---|---|---|
| a normal round | `200`, the event stream | carries on |
| the **credit gate stopped** the turn | `200`, an ordinary `end_turn` message with one text block (the stop reason) and **no tool use**. The `stop` event and the `stopped` commit are already recorded through the bridge. | ends the turn, exits `0` |
| a **real failure** (the bridge refused a call, a double raised) before anything was streamed | `502`, Anthropic's error body (`{"type": "error", "error": {"type": "api_error", …}}`) | retries **once** (a 5xx), then exits `1` |
| a real failure after the `200` was sent | an `event: error` frame in the stream | exits `1` (retries only `overloaded_error`) |
| any other non-2xx | — | exits `1`, no retry |

A credit stop is not an API error: answering it with a `400`, as the shim
used to, would make the real CLI report an API failure and exit non-zero
for a turn that ended exactly as designed. A real failure drops the turn's
uncommitted rows, so a retried request starts the turn over instead of
continuing a broken one; nothing reached the store, because the turn
commits once, at the end.

**Two languages, one boundary.** On the wire the shim speaks the **CLI's**
language: the tools are advertised as `mcp__autonomy__autonomy_route`, and
the model's reply has to name one of them exactly — the shim maps the bare
product name back onto the advertised one, so the tool call on the wire
always names a tool the client offered. Across the bridge it speaks the
**product's** language: the tool names are the bare ones the transcript rows
and `tools.call` use. The translation happens in the shim, at the boundary,
and nowhere else. A call that comes back with either spelling is
normalized before the role's tool list is consulted, so a correctly
prefixed reply is authorized and a bare name that was never offered is not.

**How the CLI reaches it.** The way Claude Code reaches any endpoint:
`ANTHROPIC_BASE_URL` plus `ANTHROPIC_API_KEY`. The prototype's base URL is
`unix://<scratch>/model.sock`, because a Python child cannot open a TCP
socket under the contract's offline guard and the superagent README offers
exactly this alternative ("have the CLI reach it over a unix socket, or
write the shim in another language"). The client understands
`http://host:port` as well — that is the form the real binary uses, and the
form a production shim would serve on loopback.

**What the shim does with a request.** It owns the turn:

- the first request of a turn asks the credit gate, recalls memory, and
  takes the conversation out of the store;
- every later request records the tool exchange the client reported, asks
  the gate again, and opens the next model round;
- a turn that the gate stops ends there: the flag that says "a turn is in
  progress" is cleared, so the next turn starts from the top — its own user
  row, its own memory recall, its own round — instead of running as a
  continuation of the stopped one;
- the chunks come from the bridge's `model.open` and `model.next`, one at a
  time, and each chunk's event is published before the next chunk is asked
  for. The shim reads the subscriber's event count and waits for it to
  reach the value it published: that is the per-chunk lockstep the contract
  requires, and `subscriber.count` is what it waits on.

The request the shim forwards to the bridge carries the store's history, the
memory, and the tool definitions the CLI offered — so "what the model saw"
is what the shim forwarded.

## The MCP transport

The MCP server speaks MCP on **standard input and standard output**, the
transport the real Claude Code uses for a server named in `--mcp-config`. It
binds no socket and opens no TCP connection. The `--mcp-config` file the
adapter writes is the one the real CLI reads:

```json
{"mcpServers": {"autonomy": {"command": "…/python", "args": ["…/mcp_server.py", "…", "…"], "env": {}}}}
```

The session is the **server's own configuration**, read from `session.json`
when it starts. A client speaks plain MCP to it — no session field is needed
in `initialize`, and none is invented:

```
-> {"jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
               "clientInfo": {"name": "claude-code", "version": "2.1.311"}}}
<- {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", …}}
-> {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
<- {"jsonrpc": "2.0", "id": 2, "result": {"tools": [...]}}
-> {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
    "params": {"name": "autonomy_route", "arguments": {…}}}
<- {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "…"}]}}
```

The server publishes **only** the role's autonomy tools. The role comes from
the session row in the store, so a Router tool is not even listed for a
Watchdog session, and the bridge refuses such a call a second time. The tool
schemas come from `roles.json`, which the adapter writes from the product
tool registry when the session starts.

## The real Claude Code flags

These are the flags that make the model see the MCP tools **and nothing
else**. They are the flags production would use; the prototype passes exactly
them to its fake binary.

| Flag | What it does | Why it is here |
|---|---|---|
| `--tools ""` | Disables every **built-in** tool (shell, file, web, subagent). | This is what removes `Bash`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `WebFetch`, `WebSearch`, `Task`. |
| `--disallowed-tools "Bash Read Write Edit Glob Grep WebFetch WebSearch Task NotebookEdit LS MultiEdit"` | Deny list. | Second lock on the same door: if `--tools ""` ever changed meaning, the deny list still holds. |
| `--strict-mcp-config` | Only the MCP servers named in `--mcp-config` are loaded. | Without it the model is offered **every** MCP server on the machine. |
| `--mcp-config <file>` | The one MCP server this turn may use. | The autonomy tools arrive through it. |
| `--allowed-tools "mcp__autonomy__*"` | Pre-approves the MCP tools. | `dontAsk` denies anything that would otherwise prompt, so the tools must be pre-approved. |
| `--permission-mode dontAsk` | Nothing may wait for a human. | The Router's only tools are MCP calls, and they must not be denied for want of a prompt. |
| `--disable-slash-commands` | No skills, no slash commands. | A skill is another way to run code. |
| `--bare` | Skips hooks, plugins, LSP, auto-memory, CLAUDE.md discovery and the keychain; auth is strictly `ANTHROPIC_API_KEY`. | **Required.** Without it the real CLI also offers `DeferredToolPlaceholder` (observed, see below) and, per `--help`, loads the user's hooks, plugins and CLAUDE.md. |
| `--setting-sources ""` | Loads none of the `user`, `project`, `local` setting sources. | **Required.** Belt and braces with `--bare`: no settings file of the user's or the repository's reaches the turn. |
| `--no-session-persistence` | No session is written to disk (`--print` only). | **Required.** The session lives in the store, not in `~/.claude`. |
| `-p --verbose --output-format stream-json` | One turn, streamed as JSON lines. | The shape the adapter reads. `--verbose` is required with `stream-json`. |

> **`--permission-mode plan` does not work here.** Plan mode refuses *every*
> MCP tool call ("Cannot call mcp__autonomy__autonomy_route while in plan
> mode."). `src/llms/native_cli/claude.py` uses plan mode for the read-only
> **Worker** lane, which is right there and wrong for a Router that has to
> dispatch tools.

The fake CLI **refuses to run** without `--bare`, `--no-session-persistence`
and an empty `--setting-sources`, exactly as it refuses a non-empty `--tools`
or a missing `--strict-mcp-config`, so the suite pins the command line the
real binary would get. The adapter also gives every child a **minimal
environment**: `PATH`, `TMPDIR`, `LANG`/`LC_ALL`/`LC_CTYPE`, `PYTHONPATH` and
`SUPERAGENT_OFFLINE` from the parent, `HOME` set to the scratch directory,
and the prototype's own `C_NATIVE_*` and `ANTHROPIC_*` plumbing — nothing
else of the parent's environment reaches the CLI.

### What was and was not verified against the real binary

| Claim | Status |
|---|---|
| `--tools ""` empties the built-ins; `--strict-mcp-config` + `--mcp-config` pins the MCP server; `dontAsk` + `--allowed-tools "mcp__autonomy__*"` lets MCP calls run; `plan` refuses them | **Run** against 2.1.311 with the harness below |
| without `--bare` the model is also offered `DeferredToolPlaceholder`; with it, exactly the MCP tool | **Run** (contrast table below) |
| the real CLI parses and acts on the SSE shape the shim writes (a single `tool_use` block, `input_json_delta`, `end_turn`) | **Run**, but against the harness's server, not against the shim itself |
| what `--bare` skips (hooks, plugins, CLAUDE.md, keychain), what `--setting-sources` and `--no-session-persistence` do | **Read off `claude --help`** (2.1.311). Not observed. |
| the full current argv, with `--no-session-persistence --setting-sources ""`, is accepted by the real binary | **Not run.** In particular, whether the real CLI accepts an *empty* `--setting-sources` is unverified. |
| the real CLI's behaviour on a non-2xx (error exit, 5xx retry count and backoff), on `event: error`, on several `tool_use` blocks in one message, on the `count_tokens` side request | **Not run.** The fake models it and retries a 5xx once; the real CLI's retry count is expected to be higher but was not observed. |
| the real CLI against the shim over a unix-socket `ANTHROPIC_BASE_URL` | **Not run**, and not expected to work: the real CLI speaks HTTP over TCP (see `RESULT.md`, risk 2). |
| the minimal environment is enough for the real CLI to start | **Not run.** |

### How this was verified locally

Verified against the real binary on this machine: **Claude Code 2.1.311**
(`/opt/homebrew/bin/claude` → `claude-code` 2.1.311, `claude --version`).

The method: point the real CLI at a local Anthropic-compatible HTTP server
and a local stdio MCP server, then read the request body the CLI sends to the
model. The tool list in that body is exactly what the model would see. The
server answers with a `tool_use` block so the MCP call really happens, and
the MCP server logs the `tools/call` it receives.

This is also the evidence that the shim's API shape is right: the harness
below is an Anthropic-compatible server whose SSE the real Claude Code
parsed and acted on, and the shim writes the same event sequence.

<details>
<summary>The harness (two files, standard library only)</summary>

`fake_api.py` — logs the request body and answers with Anthropic's streaming
shape:

```python
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = sys.argv[2]
TOOL = {"type": "tool_use", "id": "toolu_1",
        "name": "mcp__autonomy__autonomy_route", "input": {"trigger_id": "t1"}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.loads(raw or b"{}")
        open(LOG, "a").write(json.dumps({"path": self.path, "body": body}) + "\n")
        first = not any(
            "tool_result" in json.dumps(m.get("content"))
            for m in body.get("messages") or []
        )
        content = [TOOL] if first else [{"type": "text", "text": "done"}]
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = [
                ("message_start", {"type": "message_start", "message": {
                    "id": "m", "type": "message", "role": "assistant", "model": "m",
                    "content": [], "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0,
                    "content_block": {"type": "tool_use", "id": "toolu_1",
                                      "name": "mcp__autonomy__autonomy_route",
                                      "input": {}}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "input_json_delta",
                              "partial_json": '{"trigger_id": "t1"}'}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta",
                    "delta": {"stop_reason": "tool_use" if first else "end_turn",
                              "stop_sequence": None},
                    "usage": {"output_tokens": 1}}),
                ("message_stop", {"type": "message_stop"}),
            ]
            for name, data in events:
                self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()
            return
        payload = json.dumps({"id": "m", "type": "message", "role": "assistant",
                              "model": "m", "content": content,
                              "stop_reason": "tool_use" if first else "end_turn",
                              "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
```

`fake_mcp.py` — a stdio MCP server with one tool that logs its calls:

```python
import json, sys

TOOLS = [{"name": "autonomy_route", "description": "Route a trigger.",
          "inputSchema": {"type": "object",
                          "properties": {"trigger_id": {"type": "string"}},
                          "required": ["trigger_id"]}}]


def send(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request["id"], "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "autonomy", "version": "0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": request["id"], "result": {"tools": TOOLS}})
    elif method == "tools/call":
        open("/tmp/cnverify/mcp_calls.log", "a").write(
            json.dumps(request.get("params")) + "\n")
        send({"jsonrpc": "2.0", "id": request["id"], "result": {
            "content": [{"type": "text", "text": "routed"}], "isError": False}})
    elif method and method.startswith("notifications/"):
        continue
    elif "id" in request:
        send({"jsonrpc": "2.0", "id": request["id"], "result": {}})
```

</details>

The run (made before `--no-session-persistence` and `--setting-sources ""`
were added to the adapter's argv; those two flags have not been run against
the real binary):

```bash
python3 fake_api.py 8732 /tmp/cnverify/requests.log &

cat > /tmp/cnverify/mcp.json <<'EOF'
{"mcpServers": {"autonomy": {
  "command": "/usr/bin/python3", "args": ["/tmp/cnverify/fake_mcp.py"]}}}
EOF

ANTHROPIC_API_KEY=test-key ANTHROPIC_BASE_URL=http://127.0.0.1:8732 \
claude -p "call the route tool" \
  --bare --verbose --output-format stream-json \
  --tools "" \
  --disallowed-tools "Bash Read Write Edit Glob Grep WebFetch WebSearch Task NotebookEdit LS MultiEdit" \
  --strict-mcp-config --mcp-config /tmp/cnverify/mcp.json \
  --allowed-tools "mcp__autonomy__*" \
  --permission-mode dontAsk \
  --disable-slash-commands
```

What came back:

```
# the tools the model was offered, from the request body
["mcp__autonomy__autonomy_route"]
["mcp__autonomy__autonomy_route"]

# the MCP server's own log: the call really happened, arguments intact
{"name": "autonomy_route", "arguments": {"trigger_id": "t1"}, ...}

# the tool_result that went back to the model
{"tool_use_id": "toolu_1", "type": "tool_result",
 "content": [{"type": "text", "text": "routed"}]}

# Claude Code's own init event
{"type": "system", "subtype": "init", "session_id": "...",
 "tools": ["mcp__autonomy__autonomy_route"],
 "mcp_servers": [{"name": "autonomy", "status": "connected", "source": "dynamic"}],
 "permissionMode": "dontAsk", "claude_code_version": "2.1.311"}
```

The same run with the flags removed, for contrast:

| Flags | Tools the model was offered |
|---|---|
| `--tools default` | `Bash`, `Edit`, `Read`, `mcp__autonomy__autonomy_route` |
| `--restricted --strict-mcp-config` | `Edit`, `Read`, `mcp__autonomy__autonomy_route` — restricted mode drops the code-running tools but **keeps the file tools** |
| `--tools ""` | `mcp__autonomy__autonomy_route` — every built-in gone |
| `--tools "" --strict-mcp-config`, no `--bare` | `mcp__autonomy__autonomy_route`, `DeferredToolPlaceholder` |
| `--tools "" --strict-mcp-config --bare` | `mcp__autonomy__autonomy_route` — and nothing else |

And the flag that decides whether MCP tools may run at all:

| `--permission-mode` | MCP call |
|---|---|
| `plan` | refused: "Cannot call … while in plan mode." |
| `dontAsk` | refused: "… denied because Claude Code is running in don't ask mode." |
| `auto` | refused: "Auto mode could not evaluate this action…" |
| `acceptEdits` | refused: "Claude requested permissions … but you haven't granted it yet." |
| `bypassPermissions` | allowed — but it also removes the sandbox, so it is not the answer |
| `dontAsk` **+ `--allowed-tools "mcp__autonomy__*"`** | **allowed** ✅ |

Without `--strict-mcp-config`, the same machine offered the model **200+
tools** from every MCP server configured in the user and project scopes.
That is the reason the flag is not optional.

## Running it

```bash
uv run pytest -p no:cacheprovider --disable-socket --allow-unix-socket \
  tests/prototypes/superagent -v -k c_native

SUPERAGENT_RESULT_DIR=/tmp/superagent-results \
  uv run pytest --disable-socket --allow-unix-socket tests/prototypes/superagent -v
```

`tests/prototypes/superagent/test_c_native_protocol.py` holds the checks that
look at the **wire**: the content block indices of the stream the shim
writes, the name the tool call carries, the text that precedes a tool call,
the turn that follows a stopped one, the isolation flags the fake refuses to
run without, the HTTP status of a credit stop and of a real failure (and the
fake's retry of a 5xx), the side requests that must not touch the turn,
several tool calls in one message, and the children's environment. The
shared contract cannot see any of that — it checks what an adapter does, not
what goes over the socket — which is how those defects passed it.

`RESULT.md` has the per-check results, the cold-start and LOC numbers, the
production providers, the failure and restart behaviour, the wiring that would
replace `astream_ptc_workflow`, and the risks.

## Where this differs from production

| Prototype | Production |
|---|---|
| `fake_claude_code.py` | the real `claude` binary, same flags |
| the shim's Anthropic API on a unix socket | the shim on loopback TCP, `ANTHROPIC_BASE_URL=http://…` |
| the bridge's scripted model | Anthropic, or whatever `--model` names |
| the bridge's `tools.call` and `FAKE_TOOL_HANDLERS` | `AutonomyToolHost`, reached over MCP |
| the adapter starts the MCP server and hands the CLI its stdio channel | the CLI spawns the server from `--mcp-config` |
| the bridge socket | the product's own transport |
| `session.json` in the scratch directory | the thread's own row, `--resume` |
