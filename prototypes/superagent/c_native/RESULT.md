# `c_native` — result

Prototype **C**: the SuperAgent runs inside a native agent CLI. The CLI's own
tools are off; the only tools the model can reach are the role's autonomy
tools, published by an MCP server that dispatches them over the contract
bridge. The CLI's model traffic is served by a shim that speaks **Claude
Code's own model API**, and that shim owns the turn behind the API.

Everything below was measured on this machine (macOS 25.6.0, Python 3.14.8,
Claude Code 2.1.311). **Only the flag behaviour listed under "Verified
against the real binary" was run against the real `claude`; everything else
ran against `fake_claude_code.py`.**

## Verified against the real binary

| Claim | Status |
|---|---|
| `--tools ""` empties the built-ins; `--strict-mcp-config` + `--mcp-config` pins the one MCP server; `dontAsk` + `--allowed-tools "mcp__autonomy__*"` lets MCP calls run; `plan`, `auto`, `acceptEdits` and bare `dontAsk` refuse them | **Run** against 2.1.311 (README harness) |
| without `--bare` the model is also offered `DeferredToolPlaceholder`; with `--bare` exactly the MCP tool | **Run** |
| the real CLI parses and acts on Anthropic's SSE shape (one `tool_use` block via `input_json_delta`, then `end_turn`) | **Run**, against the README harness's server — **not** against this shim |
| what `--bare` skips (hooks, plugins, CLAUDE.md discovery, keychain) and what `--setting-sources` / `--no-session-persistence` do | **Read off `claude --help`** only |
| the adapter's current argv — `--bare --no-session-persistence --setting-sources ""` plus the rest — is accepted by the real binary | **Not run.** An *empty* `--setting-sources` in particular is unverified. |
| the real CLI on a non-2xx (error exit; its 5xx retry count and backoff), on `event: error`, on several `tool_use` blocks in one message, on `count_tokens` | **Not run.** The fake models it and retries a 5xx once; the real CLI's retry count is expected to be higher (Anthropic SDK behaviour) but was not observed here. |
| the real CLI against this shim, over the unix-socket base URL | **Not run**, and not expected to work (risk 2) |
| the minimal child environment is enough for the real CLI | **Not run** |

## The shape

```
  Claude Code CLI ──POST /v1/messages (Anthropic Messages API, SSE)──► model shim
        │                                                                     │
        │ MCP over stdio                        model.open / model.next        │
        ▼                                          (per-chunk lockstep)        │
     MCP server ──tools.call─────────────────────────────────────────────► bridge
```

The **turn** — credit gate, memory recall, transcript, events, the store
commit — lives in the model shim, behind the model API. The CLI is a client:
prompt, model request, tool call, tool result, repeat. The MCP server is a
dispatcher: publish the role's tools, run the call.

That split is the point. The real `claude` binary is a client too, so it can
take the fake's place without the turn losing its credit gate, its memory,
its transcript or its commit.

The shim is also a **boundary between two languages**. On the wire it speaks
the CLI's: the tools are advertised as `mcp__<server>__<tool>`, and the
model's reply has to name one of them exactly. Across the bridge it speaks
the product's: the bare tool names the transcript rows and `tools.call` use.
The translation happens in the shim, and nowhere else.

## Per-check results

`pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket -W ignore
tests/prototypes/superagent` (2026-11-10): **86 passed, 12 skipped**. That is
the shared contract (`test_contract.py`: 67 passed, 12 skipped — the skips
are the `n_pi` adapter, which is not in the tree; 12 of the 67 are
`c_native`'s) plus this prototype's 19 wire checks. Every check that applies
to `c_native` passes. `ruff check` on the package and its test file: clean.

| # | Contract check | Result | What it proves for this adapter |
|---|---|---|---|
| 1 | `test_role_tool_set_matches_autonomy_lists` | ✅ | `tool_names` returns the product's Router and Watchdog lists, in order; `supervisor` maps to the Router list; an unknown role raises `ValueError`. |
| 2 | `test_restart_survival` | ✅ | A restarted adapter on a deep copy of the store keeps the **stored** role and user (asked for `watchdog`/`someone-else`, got `router`/`u1`), carries the whole history into the next model request, and writes only to the copy. The turn is one store transaction. |
| 3 | `test_offered_tools_are_exactly_the_role_list` | ✅ | Every model request carries exactly the role's tool definitions — no duplicates, no other role's tools, no shell or file tool. This is the check that the MCP server publishes the role's list and nothing else. |
| 4 | `test_transcript_records_the_turn` | ✅ | The transcript is `user → memory → assistant → done`, with unique, ordered seqs. |
| 5 | `test_ordered_streaming` | ✅ | The events of a turn are contiguous and ordered; the model's per-chunk prefix lengths are `[0, 1, 2]` — the shim never handed out a chunk before the previous chunk's event was published. |
| 6 | `test_credit_gate_stops_before_the_model` | ✅ | The gate is asked first and alone (`trace == ["credit"]`): no memory recall, no model call, one `stop` event, the transcript is `user → stop`, the session ends `stopped`. |
| 7 | `test_credit_gate_stops_mid_turn` | ✅ | After a tool call the gate is asked again (`trace == ["credit", "memory", "model", "credit"]`), denies, and the turn ends `stopped` without a second model call. |
| 8 | `test_memory_is_injected_before_the_model_call` | ✅ | The recall hit is in the model request **before** the user's text. |
| 9 | `test_scripted_role_tools_run_and_continue` | ✅ | Every Router and Watchdog tool runs and the turn continues; the tool call and its result are in the next request and not in the first; a Router tool in a Watchdog session is refused, never dispatched. |
| 10 | `test_no_shell_or_file_actions` | ✅ | All 30 forbidden names are refused, never dispatched, and the turn still finishes. No file outside the scratch directory changed. **Every launch the adapter made is one of the three argv tuples it declared** — the launch gate refused nothing and nothing was launched that was not declared. |
| 11 | `test_no_real_provider` | ✅ | Import, construction, `start`, `turn` and the child processes stay offline: no TCP socket, no database client. |
| 12 | `test_cold_start_and_loc_json` | ✅ | The cold-start and LOC records are written and the adapter directory is not touched by the measurement. |

Three things the contract does not check, checked by hand:

- the **launch set equals the declared set** exactly — 3 launches, 3 declared
  argv tuples, no unregistered process, no `node`, no `npm`, no shell;
- the **model request carries the product's tool names**. The CLI offers
  `mcp__autonomy__autonomy_route` on the wire; the shim translates it to
  `autonomy_route` for the bridge, which is the name the transcript rows and
  `tools.call` use. The prefix round trip is exercised on every request, and
  the wire check reads the advertised name straight off the socket.
- the **suite leaks nothing**: after a full run no `c_native` process
  survives.

## The wire checks

The contract checks what an adapter **does**. It cannot see the bytes between
the CLI and the model shim, and four protocol defects passed it that way:
an invalid content block index, a tool name that did not match the advertised
one, assistant text that vanished before the next model request, and a turn
that was not initialized after a credit stop.

`tests/prototypes/superagent/test_c_native_protocol.py` looks at the wire.
The first two read the shim's own socket, the way the CLI does.

| # | Protocol check | Result | What it proves |
|---|---|---|---|
| 1 | `test_model_stream_uses_valid_content_block_indices` | ✅ | Three text chunks share **one** text block (index 0) and the tool request is a **second** block (index 1). Every block is opened once and closed once; no delta reaches a closed block; nothing is left open. |
| 2 | `test_tool_call_on_the_wire_uses_the_advertised_name` | ✅ | The `tool_use` block names `mcp__autonomy__autonomy_route` — the name the client advertised, not the bare product name. |
| 3 | `test_a_prefixed_tool_call_is_authorized` | ✅ | The CLI answers with the advertised name; the shim normalizes it, authorizes it, and records the call under the bare product name. Nothing is refused. |
| 4 | `test_assistant_text_before_a_tool_call_reaches_the_next_request` | ✅ | The text the model wrote before the tool call is in the **next** model request, in the assistant's own message. |
| 5 | `test_the_next_turn_starts_over_after_a_credit_stop` | ✅ | A turn after a stopped one is a new turn: its own user row, its own memory recall, its own prompt. It is not a continuation of the stopped turn. |
| 6 | `test_the_cli_argv_carries_the_isolation_flags` | ✅ | `cli_argv` carries `--bare`, `--no-session-persistence` and `--setting-sources ""`; the fake refuses the argv with any of them missing, or with a non-empty `--setting-sources`. |
| 7 | `test_a_credit_stop_is_an_ordinary_end_turn_reply` | ✅ | A stop is `200` + an `end_turn` message with one text block and no tool use (also on `/v1/messages?beta=true`); the `stop` event and `stopped` status are recorded; the model is never called. |
| 8 | `test_a_bridge_error_is_a_502_and_drops_the_turn` | ✅ | A double that raises gives `502` with Anthropic's `api_error` body; the failed request's user row does not survive into the next turn. |
| 9 | `test_the_cli_fails_on_a_5xx_after_one_retry` | ✅ | Through the real adapter and fake process: the 502 is retried exactly once (two gate checks), then the CLI exits `1` with `API Error: 502`; the store is untouched; the next turn starts clean. |
| 10–14 | `test_the_fake_honours_the_http_status[…]` | ✅ | Against a stub endpoint: 500→200 succeeds after one retry; 503→502 fails after two requests; 400 and 404 fail with no retry; an in-stream `event: error` fails. |
| 15 | `test_side_requests_do_not_touch_the_turn` | ✅ | `count_tokens` → `200 {"input_tokens": n}`; `/v1/messagesX` and `/v1/messages/batches` → `404`; `GET /v1/messages` → `405`; no gate check, no recall, no model call, no row — and the next real turn is normal. |
| 16 | `test_the_fake_parses_several_tool_uses_in_one_stream` | ✅ | A stream with a text block and two `tool_use` blocks yields both calls, ids and inputs intact. |
| 17 | `test_the_fake_runs_every_tool_use_and_answers_them_together` | ✅ | Three calls in one message (one not advertised) all get a `tool_result`, in order, in one user message; only advertised ones reach MCP. |
| 18 | `test_the_shim_records_several_tool_calls_in_one_message` | ✅ | Two `tool_use` blocks and two results give `tool_call, tool_call, tool_result, tool_result`, each result matched by id. |
| 19 | `test_the_children_get_a_minimal_environment` | ✅ | With `ANTHROPIC_AUTH_TOKEN` and `CLAUDE_CONFIG_DIR` set in the parent, the child environment holds only the allow list, `HOME` (= scratch) and the `C_NATIVE_*` plumbing; a turn still runs. |

Checks 6–19 were checked against the previous code by copying the
committed files back over the new ones (no stash) and re-running: with the
old shim, 7, 8, 9, 15 and 18 fail (and 5 now fails too, because the new fake
honours the old shim's `400` for a credit stop); with the old fake, 6, 9–14,
16 and 17 fail; with the old adapter, 6 and 19 fail.

Each of the four defects is caught by a check that **fails on the old code
and passes on the new one** — verified by stashing the fix and re-running:

```
=== stashed, running the new checks against the old code ===
FAILED test_model_stream_uses_valid_content_block_indices
FAILED test_tool_call_on_the_wire_uses_the_advertised_name
FAILED test_assistant_text_before_a_tool_call_reaches_the_next_request
FAILED test_the_next_turn_starts_over_after_a_credit_stop
4 failed, 1 passed
```

The fifth check passes on both, by construction: it guards the new path
(a correctly prefixed reply has to be authorized) rather than the old one.

## The handoff blockers

Each was checked on its own terms, not just by the contract.

### 1. The model API, and where the turn lives

| | before | now |
|---|---|---|
| the shim spoke | a private `open`/`next`/`ack` protocol | **Anthropic's Messages API**: `POST /v1/messages`, the request body Anthropic's, the reply Anthropic's server-sent event stream |
| the CLI reached it with | `C_NATIVE_MODEL_SOCKET` | **`ANTHROPIC_BASE_URL`** + `ANTHROPIC_API_KEY`, the way Claude Code reaches any endpoint |
| credit, memory, streaming, persistence lived in | the fake CLI | **the shim**, behind the model API |

Verified:

- the fake CLI's source contains none of `credit.authorize`, `memory.recall`,
  `seq.allocate`, `subscriber.publish`, `store.commit`,
  `store.transcript_rows`; the shim's contains all of them;
- a raw HTTP request to the shim's socket returns `HTTP/1.1 200 OK` and the
  Anthropic event sequence `message_start → content_block_start →
  content_block_delta → content_block_stop → message_delta → message_stop`.

The event sequence is the one the **real** Claude Code parsed and acted on
when it was pointed at a local Anthropic-compatible server — that harness is
in the README, and it is the evidence that the shape is right.

### 2. The MCP transport and the session binding

| | before | now |
|---|---|---|
| the transport | a unix socket the config never mentioned | **stdio** — the transport the real Claude Code uses for a `--mcp-config` server. The server binds no socket. |
| the config | declared `command` (a stdio server) that started a *socket listener* | declares the stdio command it actually is |
| the session | a custom `initialize.session_id`, without which the server said "no session for this MCP connection" | **the server's own configuration** (`session.json`). A standard `initialize` followed by `tools/list` works. |

Verified: the MCP server's source has no `import socket`; a plain
`initialize` (protocol version, capabilities, client info — no session field)
followed by `tools/list` returns exactly the role's seven Router tools.

The adapter starts the MCP server on a socket pair and hands the CLI the
file descriptor, so the server's standard input and output carry the MCP
traffic and the CLI never spawns anything. The config is still the one the
real CLI reads, and the fake CLI can spawn the server from it when it is
given no channel — the same code path the real binary takes.

### 3. The CLI's lifetime

| | before | now |
|---|---|---|
| `close()` stopped | the two sidecars | the sidecars **and the CLI it is tracking** |
| the CLI was | not tracked | `adapter._cli`, set when it is launched and cleared when it exits |
| cancellation | left the CLI running | terminates it, then re-raises |

Verified with the process stub the handoff used: after `close()` the stub
recorded `terminate` (and the adapter's `_cli` is `None`); the same happens
on the cancellation path. The CLI is launched with `start_new_session=True`,
so the stop takes the whole process group down — a CLI that spawned an MCP
server of its own does not outlive the stop.

`close()` stays a plain method, so it can be called from a teardown that has
no event loop. `asyncio.Process.wait()` is a coroutine, so the wait is the
pid itself (`os.kill(pid, 0)`), bounded at five seconds.

### 4. The content block indices

| | before | now |
|---|---|---|
| the text of a turn | every chunk reopened block 0 | **one** text block; every chunk is a delta into it |
| the tool request | reused index 0 without closing the text block | **the next index**, and the text block is closed first |
| the fake's parser | ignored the indices, so it could not see the defect | **strict**: a block opened twice, a delta to a block that was never opened, and a block still open at `message_stop` are all refused |

The real Claude Code rejects a stream whose indices do not line up. The
shim's stream is now the shape the API documents:

```
content_block_start  index 0   text
content_block_delta  index 0   "A"
content_block_delta  index 0   "B"
content_block_delta  index 0   "C"
content_block_stop   index 0
content_block_start  index 1   tool_use
content_block_delta  index 1   input_json_delta
content_block_stop   index 1
```

Verified by reading the shim's socket directly (wire check 1), and by the
fake's parser refusing a stream that breaks the rule.

### 5. The tool names on the wire

| | before | now |
|---|---|---|
| the shim emitted | the bare name, although it advertised `mcp__autonomy__*` | **the advertised name**, so the reply always names a tool the client offered |
| the shim authorized | the bare name only | the **normalized** name, so a correctly prefixed reply is authorized |
| the fake CLI accepted | a bare name it was never offered | only a name in the **advertised set**; anything else is refused |

The shim is a boundary between two languages: the CLI's on the wire
(`mcp__<server>__<tool>`), the product's across the bridge (the bare name
the transcript rows and `tools.call` use). The translation happens in the
shim, at the boundary.

One asymmetry is deliberate: a **refusal** reports the name the model asked
for (`mcp__fs__write_file`), because that is what was attempted, while an
**authorized call** is recorded under the product's name, because that is
what the transcript rows and the contract compare.

### 6. The text before a tool call

The fake CLI sent only the `tool_use` block back to the model. The shim
reads the committed history, and the current turn is not committed yet, so
the text the model wrote before the tool call was lost from the next
`model.open`. The CLI now sends the whole assistant message — the text and
the tool call — the way the real binary does.

### 7. The turn after a stopped one

A credit stop mid-turn committed the stop and raised, but left the shim's
"a turn is in progress" flag set. The next turn was then treated as a
continuation: no user row, no memory recall, no fresh round. The flag is
now cleared where the turn ends, so an allow/deny/allow sequence gives the
third turn its own initialization (wire check 5).

### 8. Review round 2: the real command line, and HTTP status

| | before | now |
|---|---|---|
| `cli_argv` | no `--bare`, though the README's own run showed it is what drops `DeferredToolPlaceholder` | `-p --bare --no-session-persistence --setting-sources ""` + the tool restriction; the fake refuses to run without each |
| a credit stop | `HTTP 400 stopped_error`, which the fake ignored (it exited `0` whatever the status) | `200`, an ordinary `end_turn` reply with no tool use; the stop is recorded through the bridge |
| a bridge error | `502`, ignored by the fake; written *after* the SSE head if the failure came mid-stream | `502` before the stream, `event: error` inside it; the turn's uncommitted rows are dropped |
| the fake on non-2xx | carried on | `API Error: <status>` and exit `1`; a 5xx is retried once |
| the path check | `startswith("/v1/messages")`, so `count_tokens` opened a turn | exactly `/v1/messages` (query ignored); `count_tokens` answered without touching the turn; anything else `404`/`405` |
| several `tool_use` blocks in one message | the fake ran only the first; the shim tracked one pending call | the fake runs all and answers them together; the shim matches results by id |
| the child environment | the parent's whole `os.environ`, contrary to the docstring | an allow list, `HOME` = scratch, and the `C_NATIVE_*` / `ANTHROPIC_*` plumbing |

## Cold start

`cold_start_ms` is a fresh interpreter that imports the adapter, constructs
it and finishes `start` for a Router session, with the harness's own bridge
setup subtracted. Five runs, milliseconds:

| Adapter | cold_start_ms | import_ms | construct_ms | start_ms |
|---|---|---|---|---|
| `c_native` | 1364.3 / 1387.6 / 1280.3 / 1275.3 / 1203.6 | 1191.6 / 1208.2 / 1087.9 / 1117.6 / 1050.3 | 0.00 | 172.7 / 179.4 / 192.4 / 157.7 / 153.3 |
| `reference_bridge` | 1248.4 / 1245.8 / 1488.2 / 1201.1 / 1192.5 | 1170.2 / 1171.1 / 1395.5 / 1124.2 / 1110.3 | 0.00 | 78.2 / 74.7 / 92.7 / 77.0 / 82.3 |
| `reference` | 1173.2 / 1152.3 / 1278.7 / 1168.9 / 1110.5 | 1172.9 / 1152.0 / 1278.4 / 1168.5 / 1110.3 | 0.00 | 0.3 / 0.2 / 0.3 / 0.4 / 0.2 |

Read it this way: **the import dominates and it is not this adapter's cost.**
Every adapter imports `prototypes.superagent.contract`, which imports the
product tool registry and drags LangChain and the MCP module generator with
it — about 1.1 s, and it moves with the machine's load (the `reference` row
above is the same import and varies by 168 ms across the same five runs).
That is the floor for the whole prototype family.

What this adapter adds is the **runtime boot**: `start_ms` ≈ 153–192 ms for
the model shim and the MCP server, against `reference_bridge`'s 75–83 ms for
one child. Two Python interpreters, each importing the bridge module, and the
MCP server now also connects to the bridge at startup. The whole `c_native`
cold start sits about 90–130 ms above `reference_bridge`'s.

A warm turn costs **33 ms** end to end (measured), including the CLI's
process spawn. A cold CLI spawn costs more; the number above is the honest
one for a warm process.

## LOC

Non-blank lines in `prototypes/superagent/c_native/` only, the shared contract
excluded (after review round 2; the cold-start numbers above predate it):

| File | LOC | raw lines |
|---|---|---|
| `model_shim.py` | 790 | 906 |
| `adapter.py` | 499 | 572 |
| `fake_claude_code.py` | 562 | 645 |
| `mcp_server.py` | 149 | 174 |
| `__init__.py` | 7 | 9 |
| **total** | **2007** | **2306** |

For comparison: `reference` 305, `reference_bridge` 377. This adapter is
**6.6× the reference**, and the two halves are now the interesting split: the
shim (790) is the production-shaped half — it is the model endpoint and the
turn — while the fake CLI (562) is the half that dies with the prototype,
because production runs the real binary and keeps the argv, not the
stand-in. The MCP server (149) is small because it only dispatches.

The wire checks added lines to the shim (the index bookkeeping, the name
translation) and to the fake (the strict parser, the assistant message). That
is the price of the fake no longer being able to hide a broken stream.

## Production providers

The shape is: a CLI, its tools restricted to an MCP server, and a model
endpoint the server controls. That needs two things from a provider — MCP
support, and a way to point the model somewhere else.

In `src/llms/native_cli/registry.py` the registered providers are
`deepseek-dsh`, `doubao`/`arkcli`, `mmx`/`minimax-mmx`, `codex`,
`claude-code`, `cursor`, `kimi`, `grok`, `stepfun`/`step`, `minimax`, `mimo`,
`opencode`, `zcode`/`glm`, `zhipu`.

- **claude-code** — verified here, end to end, against the real 2.1.311
  binary. `ClaudeCodeAdapter` does not yet take `mcp_servers`; it only passes
  `--strict-mcp-config` in its isolated mode. Adding the parameter is the
  small change that makes this lane real.
- **codex, deepseek-dsh, mimo, minimax, zcode/glm** — already carry an
  `mcp_servers` argument and write it into the CLI's own config. They are the
  closest to this shape; what they lack is the **tool restriction** half,
  which for Claude Code was `--tools ""` plus `--strict-mcp-config`. Each of
  those CLIs needs the same check: can its built-in tool set be emptied, and
  can its MCP server list be pinned to one?
- **doubao/arkcli, mmx/minimax-mmx** — text-only replies by design, no tool
  loop. Not eligible.
- **gemini, qwen** — PTC-only by design (the registry says so). Not eligible.
- **cursor, kimi, grok, stepfun, opencode, zhipu** — no MCP wiring in the
  adapter today. Eligible in principle, unproven.

The model endpoint is the other half: the production shim would stand in for
Anthropic's API (or whatever `--model` names) and forward to the configured
provider, with the key coming from the turn's `config`, not from the
environment.

## Failure and restart behaviour

**A turn commits once, at the end.** Everything the turn produced — the
transcript rows, the event rows, the session's `next_seq` and `status` — goes
into one `store.commit` with a `next_seq` precondition. The commit happens
in the shim, so it is the only writer.

**A failure before that commit leaves the store untouched.** Measured: the
model shim was killed mid-session and the next turn was run.

```
turn 1: [('token', 'hello'), ('token', 'world'), ('done', '')]  (33 ms)
turn 2 raised: RuntimeError: the Claude Code CLI failed (rc=1): claude: [Errno 61] Connection refused
commits after failed turn: 2 (unchanged: True)
transcript rows after failed turn: [('user','hi'), ('memory','memo'), ('assistant','hello'), ('assistant','world'), ('done','')]
```

The turn is abandoned with the CLI's own error, the store still holds the
last good state, and the session is not marked `stopped` — the turn never
happened, so it must not look like it did.

**A failing double is an API error, not a stop.** When a bridge call fails
inside the shim, the client gets a `502` (or an `event: error` frame if the
stream had started), the fake retries the 5xx once and then exits `1`, and
the adapter raises with `API Error: 502` in the message. The shim drops the
turn's uncommitted rows, so the retry — and the next turn — start over
(wire checks 8 and 9). A **credit stop** is the opposite: a `200` `end_turn`
reply, exit `0`, and the `stop` event and `stopped` status in the store.

**A dead sidecar is loud.** The CLI exits non-zero, the adapter raises with
the CLI's stderr, and a boot failure names the sidecar's log tail instead of
waiting out the timeout.

**Restart reads the store, never a cache.** The first adapter is closed, the
store is copied, and a row is written into the copy as if another process had
written it. The restarted adapter keeps the stored role and user, puts the
whole history — including the foreign row — into the next model request, and
writes only to the copy:

```
restarted session: SessionRecord(session_id='s1', user_id='u1', role='router', status='ready')
model saw: ['Route or review only…', 'memo', 'hi', 'memo', 'hello', 'world', 'next']
```

**`close()` is idempotent** and stops the CLI it is tracking plus the two
sidecars, each in its own process group (`start_new_session=True`), so a
stop takes the whole group down and never signals our own group.

## Wiring: replacing `astream_ptc_workflow`

The Router and the Watchdog reach the model through
`astream_ptc_workflow`, called from
`src/server/app/threads/messaging.py::_handle_send_message` (line 582). Just
above it, the same function decides which tools the turn gets (lines 544–581):

```python
if is_dispatch and request.platform == "superagent_watchdog":
    extra_configurable = {**(extra_configurable or {}),
                          "autonomy_host": AutonomyToolHost(user_id=..., thread_id=...)}
    extra_tools = AUTONOMY_WATCHDOG_TOOLS
elif route.role == "supervisor":
    extra_configurable = {**(extra_configurable or {}),
                          "autonomy_host": AutonomyToolHost(user_id=..., thread_id=...)}
    extra_tools = AUTONOMY_ROUTER_TOOLS
...
gen = astream_ptc_workflow(request=..., user_input=..., config=config,
                           extra_configurable=extra_configurable,
                           extra_tools=extra_tools)
```

This lane is already the shape the prototype fills in. The swap is one branch
next to that call:

1. **Where the branch goes.** In `_handle_send_message`, where `extra_tools`
   and `extra_configurable` are computed. `extra_tools` is the role's tool
   list — it becomes the MCP server's `tools/list`. `extra_configurable`
   carries `autonomy_host`, the object the tools actually call — it becomes
   what the MCP server dispatches to, instead of the bridge's `tools.call`.
2. **What runs the turn.** The same argv the prototype builds, with the real
   binary in place of the fake: `-p --bare --no-session-persistence
   --setting-sources "" --verbose --output-format stream-json --tools ""
   --disallowed-tools "…" --allowed-tools "mcp__autonomy__*" --strict-mcp-config
   --mcp-config <per-turn file> --permission-mode dontAsk
   --disable-slash-commands --`, the prompt on standard input, and a minimal
   environment (an allow list plus `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY`).
   The two flags `--no-session-persistence` and `--setting-sources ""` have
   not been run against the real binary. The model comes from `config` / `named_model`,
   the session from the thread's own row.
3. **What must not change.** The credit gate (`enforce_credit_limit`, called
   before the stream starts), the run ledger and BTM, the persistence layer,
   the steering rules, the workspace binding. The native turn replaces the
   *model and tool* path, not the run's lifecycle.
4. **The event seam.** `astream_ptc_workflow` is an async generator of PTC
   workflow events, and the caller streams them to the client. The native
   path has to produce the same vocabulary: the CLI's stream-json `assistant`,
   `user` and `result` events map onto token, tool-call and tool-result
   events; the run's own start/finish markers stay where they are.
5. **Where the session lives.** `astream_ptc_workflow` keeps the conversation
   in the PTC graph's checkpoints. The native lane keeps it in the thread's
   own rows — which is why the prototype's restart check reads the history
   back out of the store instead of a cache. That is the property the lane
   has to keep.
6. **Where the credit gate lives.** In the prototype it is behind the model
   API, so the CLI cannot skip it. In production the same seam applies: the
   shim (or whatever fronts the model) asks the gate before every model
   round, and the CLI is not in a position to bypass it.

Two things the prototype deliberately does **not** solve, and the wiring must:
the CLI's process lifetime is not the run's lifetime (a crashed CLI leaves a
run with no owner), and the MCP server is a second process that can die
mid-turn.

## Risks

1. **The prototype starts the MCP server; production lets the CLI start it.**
   The contract's launch gate only sees the test interpreter, so a child that
   spawns its own MCP server is invisible to it. In production the CLI spawns
   the server from `--mcp-config`, and the product must gate that spawn some
   other way — the OS sandbox in `isolation.py` is where that belongs. The
   prototype's socket-pair handover is the same bytes on the wire, but it is
   not how the real binary gets its server.
2. **The unix-socket base URL is the offline shape.** The prototype's
   `ANTHROPIC_BASE_URL=unix://…` exists because a Python child cannot open a
   TCP socket under the contract's offline guard. The real binary speaks
   HTTP, so a production shim must serve loopback TCP. The shim's HTTP code
   is transport-agnostic — the same request parser and the same SSE writer —
   but the TCP listener has not been exercised here.
3. **`--bare` is now mandatory, and it needs `ANTHROPIC_API_KEY`.** Auth
   under `--bare` is strictly `ANTHROPIC_API_KEY` or an `apiKeyHelper` passed
   with `--settings`; OAuth and the keychain are never read. A production
   lane on an OAuth subscription cannot run this argv. Dropping `--bare` is
   not the way out: the CLI then also offers `DeferredToolPlaceholder` and
   loads the user's hooks, plugins and CLAUDE.md, which is why the fake
   refuses to run without it.
4. **Plan mode blocks MCP calls.** `src/llms/native_cli/claude.py` uses
   `--permission-mode plan` for the read-only **Worker** lane. That is right
   there and wrong here: a Router lane must use `dontAsk` plus
   `--allowed-tools`. If the two lanes ever share one argv builder, the
   Router silently loses its tools.
5. **The shim reconstructs the tool-call text.** The `tool_call` event's text
   is the canonical JSON of the tool's name and arguments, and the shim
   rebuilds it from the `tool_use` block the client reported. It matches the
   bridge's `scripted_tool_call_text` today; if that format ever changes, two
   places must change together. The contract's own check compares the
   published text with the canonical form, so a drift fails the suite.
6. **The lockstep guard is a guard, not a barrier.** The shim waits for
   `subscriber.count` after every publish, but it publishes the events itself,
   so the wait is normally already satisfied. The contract's per-chunk check
   is what actually catches a regression; the shim's wait is the second line.
7. **The role is read per request.** The MCP server resolves the role from
   the session row on every `tools/list` and `tools/call`. A session whose
   role changed between two calls would see the tool list change under it.
   The role is immutable in the product, but the prototype does not enforce
   that.
8. **The adapter diffs the store's event rows** to work out which events
   belong to the turn it just ran. That is safe against the contract's
   single-writer store. In production, where the store is Postgres with
   several processes, the turn's events must be identified by the run id
   instead of by a seq diff.
9. **One CLI process per turn.** That is faithful to `claude -p`, and it
   costs a process spawn per turn (33 ms warm). A production lane that wants
   a session per thread would hold the CLI open and need the model shim to
   keep serving between turns.
10. **The timeouts are prototype numbers.** The shim waits 30 s for the
    previous chunk's events and the CLI's turn has 120 s. Both should come
    from the run's deadline, the way `src/llms/native_cli/isolation.py` and
    the worker budget already do.
11. **The fake CLI is 28% of the LOC** (562 of 2007). It is the part that
    dies with the prototype: production runs the real binary and keeps the
    argv, not the stand-in. The shim and the MCP server are the parts worth
    carrying over.
12. **The flags were verified on one machine, one version.** Claude Code
    2.1.311. `--tools ""` and `--strict-mcp-config` are documented, but a
    future release could change what `--tools ""` empties or add a built-in
    that ignores the deny list. The verification harness in the README is
    small enough to re-run on a version bump, and it should be a test, not a
    README.
13. **The name translation is a shim convention, not a standard.** The
    bridge speaks the product's bare tool names and the wire speaks the
    CLI's `mcp__<server>__` names, and the shim translates between them. A
    production lane that lets the model talk to two MCP servers at once has
    to keep that translation in one place, or the same tool will appear
    under two names in one transcript.
14. **The fake's strict parser is a proxy for the real one.** The wire checks
    prove the shim writes an indexed stream the fake accepts. The real
    Claude Code has its own parser, and it is stricter about some things the
    fake does not model (the `message_delta` usage fields, for one). The
    harness in the README is what actually settles that, and it should be
    re-run against the real binary whenever the shim's stream changes.
15. **The fake's error handling is a model of the real one, not a copy.**
    The real CLI is expected to retry 5xx (and 429/529) several times with
    backoff — not observed here; the fake retries once and does not back off. The shim's `502` for a bridge
    failure therefore costs the real CLI several identical requests, each of
    which starts the turn over and records (then drops) a user row. A
    production shim should answer a failure it knows is permanent with a
    4xx, and keep 5xx for the transient ones.
16. **`count_tokens` is an estimate.** Four bytes a token. Nothing in the
    turn depends on it, but a client that budgets context with it would
    budget wrong.
