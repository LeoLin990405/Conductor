# SuperAgent prototypes

Small adapters for the Router and the watchdog, compared through one contract.
The product path still builds a full agent graph and a sandbox. These packages
do not. They answer a turn with the injected chat double, a Postgres-shaped
store, a credit gate, and a memory recall source.

Two reference packages always ship with the contract. Three more slots are
discovered when present and skipped when absent:

| Package | Module | Shape |
|---|---|---|
| `reference` | `prototypes.superagent.reference.adapter` | in-process |
| `reference_bridge` | `prototypes.superagent.reference_bridge.adapter` | Python child over the bridge |
| `p_python` | `prototypes.superagent.p_python.adapter` | P: langchain-core loop |
| `n_pi` | `prototypes.superagent.n_pi.adapter` | N: Node pi-agent-core service |
| `c_native` | `prototypes.superagent.c_native.adapter` | C: native CLI, tools from MCP |
| `production` | `prototypes.superagent.production.adapter` | glue over `superagent_runtime.loop.run_turn` |

Each module exposes `make_adapter`, and optionally `runtime_argv`. A
non-Python runtime still needs this Python entry point so the suite can
construct it. It must not run a shell, write outside its scratch directory,
or call a hosted model.

## What a prototype implements

```python
def runtime_argv(runtime: RuntimeContext) -> list[list[str]]:
    """Optional. Every command the adapter will launch, exactly."""

def make_adapter(
    *,
    model: ScriptedFakeChatModel,
    store: FakePostgresStore,
    credit_gate: FakeCreditGate,
    memory: FakeMemoryRecall,
    subscriber: EventSubscriber,
    runtime: RuntimeContext,
) -> SuperAgentAdapter:
    ...
```

All arguments are keyword-only and come from `prototypes.superagent.contract`.
The model reports `provider_name == "scripted"`. Refuse anything else.

`RuntimeContext` has two fields:

- `scratch_dir`: an empty directory owned by this adapter instance. It is the
  only place the adapter may write during `start` and `turn` (pid files, a
  generated config, an MCP manifest). The file guard allows exactly this
  directory: not a sibling adapter's scratch, and not the bridge socket's
  directory. A restarted adapter gets a new, empty one, so nothing in it
  survives a restart.
- `bridge_path`: a unix socket the harness serves for exactly the doubles
  passed to this `make_adapter` call. See "Out-of-process runtimes".

The test calls `runtime_argv(runtime)` before `make_adapter` and registers
those argv tuples, and only those, with its launch gate. Any other
`subprocess`, `os.system`, `os.exec*`, or `os.spawn*` call fails the test,
including `node` and `npm`. Launch through the `subprocess` module attribute
(`subprocess.Popen(argv)`), not a reference captured at import time.

## Adapter surface

- `provider_name`: the string `scripted`.
- `tool_names(role) -> list[str]`: exact names, in order, from
  `AUTONOMY_ROUTER_TOOLS` or `AUTONOMY_WATCHDOG_TOOLS` in
  `src/tools/autonomy/tools.py`. `router` and `supervisor` use the Router
  list. `watchdog` uses the watchdog list. Any other role raises `ValueError`.
  `autonomy_tool_names` returns that list. This is the adapter's claim only;
  the tool-set test checks what the model is offered (below).
- `await start(session_id, *, role, user_id)`: open a session. When the store
  already has the session, keep the stored `role` and `user_id`.
- `await turn(session_id, user_text) -> list[StreamEvent]`: one turn.
- `transcript(session_id) -> Transcript`: committed transcript rows.
- `events(session_id) -> list[StreamEvent]`: committed stream rows.
- `session(session_id) -> SessionRecord`: committed session identity.
- `cold_start_ms() -> float`: duration of the last `start`, in milliseconds.
- `close()`: stop every process and socket the adapter started. A plain
  method or a coroutine. It must be idempotent: the harness closes every
  adapter again at teardown.

`transcript` and `session` read the injected store. Restart is tested this
way: the first adapter is closed, the store is deep-copied, a row is added to
the copy as if another process wrote it, and a new adapter is built on the
copy. The new adapter must report that row and put it in the next model
request, and must write only to the copy. The test then reads the store's
session, transcript, and event rows directly, and checks that the turn was one
commit. The subscriber is process-local and is not replayed.

## Turn order

1. `credit_gate.authorize(user_id=..., session_id=...)`.
2. If `allowed` is false, publish one `stop` event whose text is
   `decision.reason` (the gate uses `Stopped by the credit gate.`), commit a
   transcript, and return. Do not call `memory.recall` or `model.stream`.
3. `memory.recall(user_id=..., query=user_text)`.
4. Call `model.stream(messages, tools=...)`. The copied messages include every
   hit's text before the model yields. Put the current user text in that
   request. Each message is `{"role": str, "content": ...}`. `content` is a
   string or a list of Anthropic or OpenAI blocks (`text`, `tool_use`,
   `tool_result`); OpenAI `tool_calls` on a message are read too.
   `contract.message_texts` turns a message into text pieces (a `tool_use`
   becomes `scripted_tool_call_text(name, input)`), and the tests compare
   those pieces: each transcript entry the request carries (user, assistant,
   memory, tool call, tool result) must come out as one piece equal to that
   entry's text. Roles are not checked. `tools` is every tool definition the runtime offers the model on
   that request: a name, `{"name": ...}`, `{"function": {"name": ...}}`, or an
   object with `.name`. An `mcp__<server>__` prefix is stripped before the
   comparison. Every request must offer exactly the role's list: no CLI
   built-ins, no other role's tools, no missing tools, and no request without
   a tool list.
5. Each `model.stream` call is one round: the scripted model yields chunks
   until it yields a tool request, then it stops. Publish each text chunk as
   kind `token` before asking for the next chunk. The streaming check is
   per-chunk lockstep: before handing out chunk *n* the model records how many
   events the subscriber has, and the test requires that count to include
   every event for chunks before *n*. Buffering a whole round, or even one
   chunk, fails.
6. On a tool request, either refuse it or run the shared fake handler.
   - Shell, file, web, and subagent names (`is_shell_or_file_tool`, matched
     case-insensitively and after stripping an MCP prefix), and names outside
     the role's tool list, publish kind `tool_refused` with the tool name as
     the text. Do not execute them.
   - A Router or watchdog tool dispatches through `FAKE_TOOL_HANDLERS` (over
     the bridge: `tools.call`). Publish kind `tool_call` with
     `scripted_tool_call_text(name, arguments)` and kind `tool_result` with
     the handler's return value. Those two strings are the durable arguments
     and result.
7. Before every round after the first, call `credit_gate.authorize` again. If
   it denies, publish `stop` with its reason, commit with status `stopped`,
   and do not call the model again. This is how a turn stops after a tool
   call.
8. When a round yields no tool request, publish `done`.
9. Commit the session, transcript, and events in one store transaction.
   Session `status` is `ready` after a normal turn and `stopped` after a
   credit stop. `next_seq` advances.

`FAKE_TOOL_HANDLERS` echoes the model's arguments as JSON (`{"ok": true, ...}`).
The handlers do not call the product host, open a shell, or write a file.
A text-only script is one model round. A script with tool requests continues
once per request, so the tokens that follow a tool are produced only after
the result is in the next request.

`SHELL_OR_FILE_TOOLS` in the contract module lists the forbidden names: the
generic ones (`bash`, `write_file`, `execute_code`, ...) and native CLI
built-ins (`Bash`, `Read`, `Write`, `Edit`, `MultiEdit`, `Glob`, `Grep`, `LS`,
`NotebookEdit`, `WebFetch`, `WebSearch`, `Task`). The no-shell test scripts
every one of them plus `BASH`, `Write_File`, and `mcp__fs__write_file`.
`autonomy_read` stays on the Router list: it reads a Trigger, Ticket, or
Thread, not a file.

On the next turn, prior user, assistant, tool-call, and tool-result transcript
text is part of the model request. That is how a restarted adapter still has
the conversation and the tool work.

## Out-of-process runtimes

The harness serves the doubles on `runtime.bridge_path`, a unix socket, from
a background thread, so a child process can call back while the test awaits
`turn`. No TCP is opened, and the offline guard stays closed. The protocol is
JSON lines (one request, one response, in order per connection):

```text
-> {"id": 1, "method": "model.open", "params": {"messages": [...], "tools": [{"name": "autonomy_route"}]}}
<- {"id": 1, "result": null}
-> {"id": 2, "method": "model.next", "params": {}}
<- {"id": 2, "result": {"text": "hi", "tool_name": "", "tool_args": {}}}
```

Methods: `model.open`, `model.next` (returns `null` at the end of a round),
`subscriber.publish`, `tools.call`, `credit.authorize`, `memory.recall`,
`store.get_session`, `store.transcript_rows`, `store.event_rows`, and
`store.commit` (one atomic transaction of `upsert_session`,
`append_transcript`, and `append_event` ops, with an optional `next_seq`
precondition). The full parameter list is in the docstring of
`prototypes/superagent/bridge.py`, which imports only the standard library.
Errors come back as `{"id": n, "error": {"type": ..., "message": ...}}`.

The first request on every connection is `hello` with
`{"protocol": PROTOCOL_VERSION}` (currently 2); anything else first, or a
different version, is an error. `tools.call` takes the `session_id` and the
bridge refuses, without dispatching, any name outside that session's stored
role. `subscriber.count` returns how many events have been published, so a
shim can wait for another process (an MCP server publishing `tool_call` and
`tool_result`) before asking `model.next` for the next chunk. `seq.allocate`
hands out transcript and event seqs from one counter per session (starting
at the store's `next_seq`), so two processes writing rows for one turn never
collide.

Shutdown: `close()` must stop the runtime, and the runtime must exit when its
control channel reaches EOF. The harness closes an adapter's bridge after
that adapter is closed; the restart test does so before it snapshots the old
store, so a runtime that outlives `close` cannot write to it.

`reference_bridge` is the worked example. Its Python adapter declares one
`runtime_argv`, launches it, writes `boot.json` into scratch, and forwards
`start`, `turn`, and reads to the child over stdin and stdout. The child
(`runtime.py`) reaches the model, store, gate, memory, subscriber, and tool
handlers only over the bridge, takes every seq from `seq.allocate`, and sends
Anthropic-shaped content blocks. It passes the whole suite.

For N, the Node service connects to the socket with `net.connect({path})`.
For C, the CLI's own model traffic needs a shim that speaks the CLI's model
API and forwards each request's messages and tool definitions over
`model.open`; that shim is part of the prototype and is launched through
`runtime_argv` like any other process. It may forward the CLI's content-block
messages as they are. For C, "what the model saw" is what the shim reports.
The MCP server that provides the autonomy tools calls `tools.call`. A shim
written in Python runs under the offline guard and gets no TCP, neither
listening nor dialing: have the CLI reach it over a unix socket, or write the
shim in another language.

Limits: the launch gate and file guard patch the test interpreter. A child's
own launches and writes are not intercepted. They are caught only by the
sentinel and tree checks, by the declared argv, and by what reaches the
bridge. Python children inherit the offline guard through `sitecustomize`;
non-Python children do not.

## Store rows

`FakePostgresStore` keeps three tables until process exit. `transaction()`
commits or rolls back as a whole.

Session row: `session_id`, `user_id`, `role` (`router` or `watchdog`),
`status` (`ready` or `stopped`), `next_seq` (int, starting at 1).

Transcript and event rows: `session_id`, `seq`, `kind`, `text`.

Committed reads are `get_session`, `transcript_rows`, and `event_rows`.

## Results

The contract tests stay offline for the whole lifecycle: importing the
adapter, constructing it, `start`, `turn`, and any Python child. New TCP
sockets are blocked. `psycopg` (including libpq), pools, and a Postgres
unix socket are rejected. The injected store is the only database.

Discovery probes for a missing or broken adapter use a temporary package.
They do not create or delete `prototypes/superagent/n_pi` (or any other
real adapter directory).

The contract test writes two JSON objects when it runs. `SUPERAGENT_RESULT_DIR`
is the directory. When that variable is unset, the test uses a temporary
directory and still checks the files. Line counts read the real adapter
directory and do not write into it. Exclusion rules (`dist`, `generated`,
`node_modules`, and the rest of that list) are checked on a temporary tree.

- `cold-start.json`: one key per adapter. `cold_start_ms` is a fresh
  interpreter importing the adapter, constructing it, and finishing `start`
  for a Router session, including any runtime boot. The harness's own bridge
  setup is subtracted. `import_ms`, `construct_ms`, and `start_ms` are the
  pieces. `provider_name` is `scripted`.
- `loc.json`: one key per adapter. `loc` is the non-blank line count of the
  implementation package only (`prototypes/superagent/<name>/`). The shared
  contract is not included. `files` lists each source file. Dependency and
  generated trees are not the implementation: `node_modules`,
  `bower_components`, `vendor`, `third_party`, `site-packages`, virtualenvs,
  `__pycache__`, `dist`, `build`, `out`, `generated`, `coverage`, `.next`,
  `.turbo`, and `target`. An `npm install` inside the package must not change
  `loc`.

```bash
uv run pytest -p no:cacheprovider --disable-socket --allow-unix-socket tests/prototypes/superagent -v
SUPERAGENT_RESULT_DIR=/tmp/superagent-results uv run pytest --disable-socket --allow-unix-socket tests/prototypes/superagent -v
```

`tests/prototypes/superagent/cheaters.py` holds deliberately dishonest
adapters (store bypass, cached history, a stale long-lived store, extra or
missing offered tools, natively run CLI built-ins, a credit check only before
the first round). The restart, offered-tools, no-shell, and mid-turn credit
checks are run against them and must fail.
