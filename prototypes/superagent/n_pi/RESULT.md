# Prototype N: Node service on pi-agent-core

Status: passes every check the reference passes. Nothing is wired into the
product yet.

## What it is

- `service.mjs` is a Node service. It runs the Super-agent loop on
  `@earendil-works/pi-agent-core` `Agent`, with `@earendil-works/pi-ai` as the
  model layer. Both packages are pinned to exactly `1.5.2`.
- The model layer is a pi-ai provider (`createModels` + `createProvider`, id
  `scripted`, api `scripted-bridge`). Its stream function turns bridge
  `model.open` / `model.next` into pi-ai's `AssistantMessageEventStream`
  protocol (`start`, `text_*`, `toolcall_*`, `done`). A hosted provider sits in
  this same slot in production.
- `prepareRequest` runs before every model round. It calls
  `credit.authorize`, and before the first round it also calls
  `memory.recall`, records the memory row, and puts the memory text in front
  of the current user message. When the gate denies, the service publishes
  `stop`, aborts the run, and the stream function returns `aborted` without
  calling the model.
- The service registers only the role's tools as `AgentTool`s (Router:
  `AUTONOMY_ROUTER_TOOLS`, watchdog: `AUTONOMY_WATCHDOG_TOOLS`, with names
  and descriptions sent from Python in `hello`). Each `execute` makes a single
  bridge `tools.call` with `session_id`. pi has no built-in shell or file
  tools. Any other name the model asks for is answered `Tool X not found`
  before it can run, and the service publishes `tool_refused`. A
  `beforeToolCall` guard refuses non-role names a second time.
- `Agent.subscribe` publishes `token` on `text_end`, and `tool_call` /
  `tool_result` (or `tool_refused`) on `tool_execution_end`. Streaming is
  lockstep: the stream function waits until a token has been published before
  it asks `model.next` for the next chunk.
- Every seq comes from `seq.allocate`. The turn is one `store.commit` with a
  `next_seq` precondition. History is rebuilt from `store.transcript_rows` on
  every turn, and nothing is cached across turns.
- `bridge-client.mjs` is the JSON-lines client (protocol 2) over
  `net.connect({path})`. It sends `hello` first.
- `adapter.py` declares one `runtime_argv`:
  `[<abs node>, service.mjs, <bridge_path>, <scratch_dir>]`. The node binary
  is `SUPERAGENT_NODE` if set, otherwise `node` from `PATH`, resolved to an
  absolute path. The adapter writes `boot.json` in scratch, and the service
  writes `runtime.pid` there. Control runs over stdin/stdout. `close()` sends
  `close`, waits up to 5 s, then kills the process. Calling `close()` again
  does nothing.

## Suite

`.venv/bin/python -m pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket -W ignore tests/prototypes/superagent`
gives `55 passed, 24 skipped`. The 24 skips are the absent `p_python` and
`c_native` slots. `uvx --offline ruff@0.15.14 check prototypes/superagent/n_pi`
reports `All checks passed!`.

| Check | reference | reference_bridge | n_pi |
|---|---|---|---|
| test_role_tool_set_matches_autonomy_lists | pass | pass | pass |
| test_restart_survival | pass | pass | pass |
| test_offered_tools_are_exactly_the_role_list | pass | pass | pass |
| test_transcript_records_the_turn | pass | pass | pass |
| test_ordered_streaming | pass | pass | pass |
| test_credit_gate_stops_before_the_model | pass | pass | pass |
| test_credit_gate_stops_mid_turn | pass | pass | pass |
| test_memory_is_injected_before_the_model_call | pass | pass | pass |
| test_scripted_role_tools_run_and_continue | pass | pass | pass |
| test_no_shell_or_file_actions | pass | pass | pass |
| test_no_real_provider | pass | pass | pass |
| test_cold_start_and_loc_json | pass | pass | pass |

The adapter-independent checks also pass: cheater detection, bridge
protocol, discovery, LOC exclusions, launch gate, and file guard.

## Cold start (suite JSON, `cold-start.json`)

| adapter | cold_start_ms | import_ms | start_ms |
|---|---|---|---|
| n_pi | 2647 | 2580 | 67 |
| reference_bridge | 2761 | 2572 | 190 |
| reference | 3162 | 3162 | 0.3 |

`import_ms` is almost all the shared import of `src.tools.autonomy.tools`
through the contract. It moves by about ±400 ms between runs, and it is the
same for every adapter. The number that tells the adapters apart is
`start_ms`. For n_pi that is Node boot, the pi import, the bridge `hello`,
and session `start`: 63 to 67 ms over three runs. A Python child takes about
185 ms. A restarted service measured 57 ms.

## Lines of code (suite `loc.json`, non-blank, excluding node_modules and lockfile)

- Node: 612. `service.mjs` 532, `bridge-client.mjs` 80.
- Python: 166. `adapter.py` 165, `__init__.py` 1.
- Total: 778. For comparison, reference_bridge is 377.

## Providers pi-ai can reach in production

pi-ai 1.5.2 ships 43 built-in providers (`providers/all.js`) over 10 wire APIs
(`openai-completions`, `openai-responses`, `azure-openai-responses`,
`openai-codex-responses`, `anthropic-messages`, `bedrock-converse-stream`,
`google-generative-ai`, `google-vertex`, `mistral-conversations`,
`pi-messages`).

The providers are anthropic, openai, openai-codex, azure, amazon-bedrock,
google, google-vertex, mistral, deepseek, xai, groq, cerebras, fireworks,
together, openrouter, vercel-ai-gateway, cloudflare-ai-gateway,
cloudflare-workers-ai, github-copilot, huggingface, nvidia, baseten, meta,
moonshotai, moonshotai-cn, kimi-coding, minimax, minimax-cn, zai,
zai-coding-cn, qwen-token-plan (4 variants), xiaomi (4 variants), opencode,
opencode-go, ant-ling, radius, and typesafe.

That covers every vendor the fleet uses today (Anthropic, OpenAI/Codex,
Kimi/Moonshot, GLM/Z.ai, DeepSeek, MiniMax, xAI/Grok, Qwen). OpenAI-compatible
endpoints such as StepFun can be added as custom providers. None of these
providers was exercised here. Every request in this prototype went to the
scripted provider.

## Failure and restart behaviour (probed with a script outside the suite)

- **Service killed between turns** (SIGKILL): the next `turn` raises
  `RuntimeError: n_pi service exited (rc=-9)` and the store is unchanged.
  There is no automatic respawn. A new adapter on the same store boots in
  about 57 ms, reads the session back, and its next turn goes through
  (`token`, `done`).
- **Service dies mid-turn**: this case was not probed, so it is inferred from
  the design. Rows are written only by the final `store.commit`, so a crash
  mid-turn leaves the store as it was before the turn. Events already
  published to the live subscriber are not rolled back. That matches the
  contract, which treats the subscriber as process-local.
- **Bridge closes under a live service**: the service exits with code 3.
- **stdin EOF or `close`**: the service exits with code 0.
- **Bridge or hook errors during a turn**: pi does not throw these out of
  `prompt()`. It turns stream and hook failures into an `error` assistant
  message. The service records the first failure, raises it after
  `prompt()`, and does not commit. The adapter then gets
  `{"ok": false, "error": ...}`.
- **Node too old** (below 22.19, the pi `engines` floor): the service exits
  with code 1 at boot.

## Production wiring: replacing `astream_ptc_workflow` for supervisor and watchdog

Today `src/server/app/threads/messaging.py` runs both roles through
`astream_ptc_workflow`. The supervisor gets `extra_tools=AUTONOMY_ROUTER_TOOLS`.
The watchdog dispatch (`platform == "superagent_watchdog"`) gets
`AUTONOMY_WATCHDOG_TOOLS`. Both get an `AutonomyToolHost(user_id, thread_id)`.
To swap in N:

1. **Process lifecycle.** Run one long-lived Node service per server process,
   started and supervised by the backend lifespan with respawn and backoff.
   Spawning one per turn would add about 60 ms per turn and is not needed,
   because the service holds no state between turns. Turns are serialized per
   connection today. Production needs either a turn id on every control
   message and on every bridge call, or a small pool of services. Add a health
   ping, and kill on overrun with the same budget the PTC run uses.
2. **Transport.** Keep both channels on unix sockets. Replace the harness
   bridge with a server-side bridge in the backend:
   - model events stay inside Node (a real pi-ai provider), so `model.open` /
     `model.next` go away;
   - `tools.call` dispatches to `AutonomyToolHost` with the stored role check;
   - `credit.authorize` goes to the real credit gate;
   - `memory.recall` goes to the memory selector that feeds
     `memory_context_block`;
   - `subscriber.publish` goes to the Redis stream
     `workflow:stream:{tid}:{rid}` and the SSE event shapes;
   - `store.*` goes to thread and turn persistence (the BTM / persistence
     service).

   Tool schemas should be the real JSON schemas of the autonomy tools.
   This prototype uses a permissive object schema because the scripted
   arguments are probes.
3. **Credentials.** `resolve_llm_config` decides the model and the credential
   (BYOK, OAuth, platform key). The backend passes the resolved provider, the
   model id, and a short-lived key per turn on the control channel, or Node
   pulls it per request through pi's `getApiKey`. Keys never sit in Node's
   environment or on disk. pi's own credential store and OAuth login flows
   stay unused.
4. **Deployment under `cond`.** `cond` (`src/conductor_cli/ops_cli.py`, the
   launchd and systemd units) starts only Python today. It would also need:
   - a Node ≥ 22.19 runtime on the host. The local v1 runs Node 26.13. The
     `python:3.13-slim` image in `deploy/Dockerfile.backend` has no Node and
     would need one added;
   - `npm ci --ignore-scripts` from the committed lockfile at install or
     upgrade time, ideally with a vendored or offline cache;
   - an `ops_cli` upgrade step that checks that both node and the lockfile
     are present. Rollback has to cover `node_modules` as well.
5. **Parity work.** These pieces of `astream_ptc_workflow` must be reproduced
   or moved behind the bridge:
   - run admission and slot release;
   - steering and 409 rules;
   - feedback and query persistence, run metadata, provenance;
   - observability labels and token/cost accounting (pi reports `usage` and
     `cost` per message);
   - retry provenance and HITL resumes.

## Risks

- **npm supply chain.** `package-lock.json` lists 88 packages (83 at the top
  of `node_modules`, 97 MB on disk), all for two direct dependencies. Most of
  that is provider SDKs that pi-ai declares as hard dependencies: `openai`,
  `@anthropic-ai/sdk`, `@google/genai`, `@aws-sdk/client-bedrock-runtime`,
  and the Smithy/AWS credential chain, plus `ws`, `protobufjs`, and the proxy
  agents. Licenses are MIT 39, Apache-2.0 36, BSD-3 11, Unlicense 1, 0BSD 1.
  Two packages have install scripts: `@google/genai` (a no-op preinstall)
  and `protobufjs` (postinstall). npm 11 did not run either one; production
  should keep `--ignore-scripts`. The lockfile pins exact versions and
  integrity hashes, but the pi packages come from a small publisher (four
  `@earendil-works/*` packages at 1.5.2), so each upgrade needs review. The
  SDKs are installed even when only the scripted provider is used. Importing
  both pi packages takes about 80 ms; whether they load the SDKs at import
  time was not measured.
- **Node version.** All four pi packages declare `engines.node >=22.19.0`.
  `openai` needs ≥ 22, and the AWS SDK needs ≥ 20. This host runs Node
  v26.13.0 with npm 11.20.0. Any deploy target needs Node 22.19 or newer, and
  the current backend image has no Node at all.
- **Second runtime.** A second runtime brings a second language, a second
  process to supervise, and an IPC boundary on the hot path. Each bridge call
  is a socket round trip, about 8 or more per turn. That is negligible next to
  model latency, but it is one more failure mode, covered above.
- **pi API churn.** The prototype uses `Agent`, `prepareRequest`,
  `beforeToolCall`, `createProvider`, and the system-message tool-declaration
  model (`getCurrentTools`). These are recent pi APIs. Upgrading past 1.5.2
  should rerun this suite.
- **Fidelity gaps.**
  - Tool parameters are permissive, not the real schemas.
  - A refused tool shows up in the live request as a `tool_use` plus a `Tool
    X not found` result. After a restart it is replayed as a user text row.
  - The service runs one turn at a time.

## Review notes (2026-11-10)

- Fixed after review: a failed `tools.call` now stops the turn. pi turns a thrown tool error into an error result and keeps looping, so `prepareRequest` aborts the run when the turn has already failed, before asking for credit or calling the model again.
- Known fidelity gap: the memory text is added to the first request's context only, not to agent state, so later rounds of the same turn are sent without it.
- Known gaps: no timeouts on bridge calls or the adapter's control-channel read; a malformed control line crashes the service (the `JSON.parse` sits outside the try).
- Production installs must use `npm ci --ignore-scripts` (`@google/genai` and `protobufjs` declare install scripts). The harness's offline guard covers Python only, not the Node child.
