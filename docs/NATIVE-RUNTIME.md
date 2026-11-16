# Native runtime backend

Status: **dual-runtime lane live.** The registry exposes 11 native providers:
DeepSeek DSH, Codex, Claude Code, Cursor, Kimi, Grok,
StepFun, ZCode, Zhipu, MiniMax Code and MiMo Code. Some providers additionally support
the ACP transport for streamed tool events and per-command approvals; their CLI
must be installed and signed in on the backend host.

The native runtime is a second execution lane beside the PTC agent. A thread
created with `native_provider` is executed by the vendor agent CLI: the CLI owns
its prompt, tool loop, session history and prompt cache. Conductor carries the
thread, admission, SSE transport, checkpoint projection, replay and finalize.
It does not wrap the CLI in an OpenAI-compatible chat transport.

## What is implemented

### Thread binding

`conversation_threads.metadata.native_runtime` stores:

```json
{"provider": "codex", "session_id": "...", "cwd": null, "model": null, "version": 1}
```

The binding is created with the thread and is immutable. `POST /api/v1/threads`
accepts `native_provider`; `POST /api/v1/threads/{id}/messages` routes a bound
thread into the native lane. A PTC thread cannot be switched to native, and a
native thread cannot be sent to a different provider.

The feature is opt-in:

```text
Settings → Experiments → Native agent runtime
```

or:

```bash
curl -X PUT "$API/api/v1/features/native_runtime" \
  -H 'Content-Type: application/json' -d '{"enabled": true}'
```

### Runtime contract

```
src/server/services/native_runtime/
  contracts.py   NativeRuntimeSpec, NativeEvent, capabilities, protocol
  spec.py        thread metadata parsing/writing
  registry.py    provider inventory over src/llms/native_cli
  graph.py       one-node LangGraph wrapper
```

`GET /api/v1/native-agents` reports, per provider:

- whether the binary is installed;
- whether a live proof has been recorded;
- the explicit capability contract (streaming, tools, session, cache, approvals,
  MCP, workspace files, cancel).

An installed binary is not a claim of authentication. That is deliberately not
probed by this endpoint.

### Native graph

The graph has one node. It reads only the current turn's last human message and
calls the runtime. It never replays Conductor's transcript into the CLI. The final
`AIMessage` is Conductor's projection for checkpoint/replay/search; the canonical
conversation remains in the CLI session.

On the first turn only, a composed memory context block may be prepended as an
explicit handoff. On resumed sessions it is not re-injected; doing so would
change the native prompt and invalidate the provider cache.

### Chat lane

`src/server/handlers/chat/native_run.py` reuses the existing:

- run admission / START row;
- Redis SSE stream and `RunSSEProducer`;
- checkpoint and transcript projection;
- finalize and terminal CAS.

Native output is normalized into the existing `message_chunk` SSE contract. The
native CLI's session id is persisted after a successful turn.

### Live evidence

Codex has been exercised on this host through the real chat lane:

```text
POST /api/v1/threads                 native_provider=codex
POST /api/v1/threads/{id}/messages   "Reply with exactly: native ok"
  → message_chunk "native ok", run_end completed
second turn → same Codex session reused, answer returned
```

The session id persisted on the thread:

```json
{"native_runtime": {"provider": "codex", "session_id": "01a17de5-..."}}
```

`/messages/replay` shows the native assistant projection with
`agent: "main"` and `turn_index`.

A Codex `resume` bug was found and fixed by the live proof: `-C` is an `exec`
option, not a `resume` option; the process cwd now carries the stable directory.

### Cancellation

Codex, DSH, MiniMax and MiMo all advertise `cancel=true` and terminate their
process group on `/cancel`. All four have live proof: a native thread asked the CLI to sleep for 120 s, the API `/cancel`
returned 200, the SSE stream ended with `outcome: cancelled`, and no CLI
process remained. The native streaming adapter uses process-group termination,
so cancellation does not wait for the model call to finish.

The lanes that keep one process across an approval pause (the ACP client, Codex
`app-server`, ZCode approvals and StepFun RPC) start it in its own session
through a start a cancel cannot interrupt halfway: a task cancelled while the
process is being started kills its group before the cancel propagates. Each of
them lives for one turn, its approval pauses included (the handler builds the
runtime per turn request, and a later turn starts a new process), so after that
each lane stops the agent when the turn completes, when the turn's task is
cancelled, when an error ends the turn and when the consumer closes the stream
(the graph does so on an error event). Codex and ACP do it by `close()`: SIGTERM
to the agent, 3 s, then SIGKILL to its group, so what the agent left running
dies with it at the end of every turn; an agent that had already exited before
`close()` is not signalled. ZCode and StepFun give the agent 5 s to leave by
itself and kill its group if it did not or if that wait is cancelled; an agent
that leaves inside the 5 s keeps its group, so a tool it left running survives.
A ZCode turn is over when the CLI's output closes; its one-shot CLI is expected
to exit then, and the 5 s is for one that does not. A StepFun turn is over at
`agent_end`, and `step --mode rpc` is a server that waits on its stdin for the
next command (checked with the installed `step` while idle: it stays up with its
stdin open and exits at once, status 0, when the stdin closes; no model turn was
run), so StepFun closes that stdin first, which is how the server is told to
exit, and then gives it the 5 s. The CLI's own documentation says an `agent_end`
can be followed by a retry or a compaction and marks the final settle with
`agent_settled`; the runtime stops reading at the first `agent_end`, so what
follows it is not delivered, and the stop after it may end it (not checked
against a live CLI). A turn parked for an approval keeps its agent until it
resumes, and the resume ends it the same way; nothing closes one that is never
resumed (the broker has no sweep and the server no shutdown hook). Only the
Codex delegation has an explicit stop (`cancel_worker` and the timeout of a
command-approval delegation), and it also reaches an app-server that is still
starting or answering `initialize`; the ACP and Codex clients refuse a `close()`
that lands while their process starts. These lanes carry no process tracker, so
unlike a daemon-run CLI nothing attests that a descendant that left the group
died, and a tool left behind by an agent that had already exited is not found
when the lane is closed later ([SUPER-AGENT.md](SUPER-AGENT.md), "Other children
the server holds"). The Codex `app-server` exits when its stdin closes (checked
with the installed binary); the ACP agents and the ZCode CLI were not checked
for that. These lanes are covered by fake-spawn tests and by real shell children
standing in for the agent, a normal completion of each lane included; no live
CLI turn was run for them.

### Workspace boundary

Live file-write probes now cover every live runtime:

- Codex runs in its own review/workspace-write sandbox.
- DSH's `dsh-proof.txt` landed only in the native mirror and synced to
  `workspace_files`; no stray path was created.
- MiniMax's `minimax-proof.txt` did the same.
- MiMo is the exception that needed a fix: under `--yolo` it accepted a
  model-chosen absolute path outside the thread cwd. Its native config now
  sets `permission.external_directory=deny`; the re-run wrote into the mirror
  and synced, with no stray file.
- The post-turn walk skips `.mimocode`, `.codex`, `.claude` and `.zcode` so
  CLI runtime state cannot become a user deliverable.

## Not implemented yet

These are intentional gaps, not hidden fallbacks.

### Workspace files

The private default cwd is:

```text
~/.cache/conductor/native-cli/workspaces/<thread_id>
```

The bridge is now implemented:

- first native turn: the workspace manifest is materialized into that cwd;
- after every native turn: the cwd is walked, changed/new regular files are
  upserted into `workspace_files`, and rows for files the CLI deleted are
  removed;
- runtime directories (`.agents`, `.system`, `_internal`, `mcp_servers`,
  `node_modules`, VCS dirs) are never copied or synced;
- symlinks and files over 20 MB are skipped;
- an explicit `native_cwd` (inside home, `/tmp`, or
  `NATIVE_CLI_WORKSPACE_ROOT`) is treated as the user's own tree and is never
  overwritten from a stale manifest.

Live proof: Codex wrote `native-proof.txt` with content `hello`; the workspace
file API returned it as a normal text file after the turn.

Still missing: an explicit file-panel push event (the panel polls), executable
bit preservation beyond the mode string, and conflict arbitration with a
simultaneously running sandbox turn.

### Plugins and MCP

Codex now receives Conductor's **secret-less stdio MCP servers** as native
`mcp_servers.<name>.*` config overrides:

- builtin stdio servers are bridged; their entrypoints are rewritten to absolute
  paths with `uv run --directory <repo>` so they run from the native cwd;
- user stdio servers are bridged with their env passed through the child
  environment, never argv;
- HTTP MCP servers with a single bearer token are bridged through
  `bearer_token_env_var`; servers with arbitrary headers are skipped rather
  than sent unauthenticated;
- native Codex uses `app-server` with `approvalPolicy=on-request` and
  `sandbox=workspace-write`; no `--approve-for-me` bypass. Commands that need
  escalation go to the HITL card instead of being auto-approved.

DSH gets the same servers through a generated profile overlay:
`@deepseek-ai/dsh-mcp-client` entries plus the existing account patch. The
DSH patch uses Cordis `insert` entries; MCP servers are not top-level id
patches (which only modify existing entries).

Live proof: a native Codex thread reached an MCP tool through the bridge and
reported the value it returned; a native DSH thread did the same and named the
tools it used.

HTTP MCP servers with headers are now bridged too:

- `${vault:NAME}` header refs are resolved host-side from the user's vault;
  a missing ref drops that server rather than sending a literal placeholder;
- Codex supports a single `Authorization: Bearer ...` via
  `bearer_token_env_var` (token in the child environment, never argv);
- DSH/MiniMax/MiMo receive headers in their generated profile/.mcp.json/MCP
  config. Those files are permission-restricted and transient per turn.
- OAuth-managed HTTP servers call `ensure_fresh_access_token` before the turn
  and inject the resulting Authorization header; a connection that cannot
  refresh is dropped rather than sent stale.
- The equivalent configuration for ZCode remains outside (ZCode itself is not
  native-live).

### Memory

Only an explicitly composed first-turn context block can be handed to the CLI.
Memory writes by the native CLI are not projected back into Conductor memory, and
the semantic memory index is still off by default.

### Automations

Automations now have typed `native_provider` / `native_cwd` columns (migration
099), exposed on create/update/response. The executor stamps the same
`native_runtime` thread metadata that `POST /threads` writes. A pinned native
thread can still be reused. The legacy `metadata.native_provider` path remains
for existing rows.

The binding is validated before save: provider must be known and installed, the
user must have the `native_runtime` feature enabled, and `native_cwd` must pass
the host-directory allowlist. There is no UI field yet; the API is the current
surface.

### Product accounting and interaction

- Native token usage is recorded in `PerCallTokenTracker` as a `native`
  record, so token totals and `context_window` frames are populated. It is not
  priced against Conductor's model cards (the vendor account owns that spend).
- HITL approvals are live for the Codex native lane: the app-server parks a
  command/file/permission request, LangGraph raises an interrupt, and the
  ordinary HITL card + resume path answers it. The ACP transport
  (`NATIVE_CLI_ACP=deepseek-dsh,mimo,minimax`) gives DSH/MiMo/MiniMax the same
  `session/request_permission` -> approval broker -> HITL path; MiniMax needs
  `mcode login` first. ZCode and the non-ACP surfaces still report
  ``approvals=false`` rather than pretending to support it. Checkpoint
  replay/regenerate and steering remain rejected for native threads with a 409.
- Native tool SSE frames are also projected into the checkpoint as synthetic
  AIMessage(tool_calls) + ToolMessage rows. They replay through the normal
  history pipeline; the CLI never receives those projection messages on a
  later turn.
- Native Codex writes use `workspace-write`; the standalone `NativeAgent` tool
  remains read-only.
- Codex native threads run through ``app-server`` with
  ``approvalPolicy=on-request`` and ``sandbox=workspace-write``; a command that
  needs escalation becomes a real HITL card instead of being auto-approved.
- Streaming is live for Codex and DSH at the **event level**: completed agent
  messages become `message_chunk`, and native tool items become `tool_calls` /
  `tool_call_result` SSE frames. It is not token-by-token (Codex's JSONL and
  DSH's status stream do not expose token deltas).
- Cancellation is live-verified: `/cancel` produced `run_end: cancelled`, the
  Codex process disappeared, and the run settled through the normal finalize
  path.
- Live Codex MCP proof: an MCP tool answered through the native tool path; the
  SSE stream carried `tool_calls`, `tool_call_result`, commentary and final
  message chunks.
- Live DSH MCP proof: two MCP tools answered; DSH's stream emitted two live tool
  cards plus the final answer.

## Codex app-server transport

Native Codex threads use ``codex app-server --stdio`` rather than
``codex exec``. The transport is bidirectional JSON-RPC:

- ``initialize`` once per process;
- ``thread/start`` or ``thread/resume`` with
  ``approvalPolicy=on-request`` and ``sandbox=workspace-write``;
- ``turn/start`` with a text input;
- notifications: ``item/agentMessage/delta`` (true token deltas),
  ``item/started`` / ``item/completed`` (tools), ``thread/tokenUsage/updated``,
  ``turn/completed``;
- reverse requests: ``item/commandExecution/requestApproval`` (also file
  change/permissions), answered with ``{"decision": "accept|decline|cancel"}``.

When an approval request arrives, the process is parked in
``native_runtime.approvals`` and the graph calls LangGraph ``interrupt``. The
existing SSE interrupt and HITL resume pipeline handles the card; the resume
run calls ``interrupt`` again (which returns the user's decision), responds to
the parked request, and continues the same process/turn.

Capabilities for the Codex native lane now include ``approvals=true``,
``streaming=true`` and token-level deltas. The one-shot ``CodexExecAdapter``
remains for the standalone ``NativeAgent`` tool.

## ZCode app-server notes

The ZCode CLI's `app-server` is the only headless path that accepts a model
selection. Its NDJSON protocol has been probed against the installed app:

```json
{"id":"1","method":"session/create","params":{
  "workspace":{"workspacePath":"/path","workspaceKey":"/path"},
  "mode":"yolo","persistence":"immediate",
  "model":{"providerId":"account:bigmodel-individual-coding-plan",
           "modelId":"GLM-5.3-Flash",
           "options":{"reasoningLevel":"max"}}}}
```

The server answers with a reverse request
`session/requestRuntimePreferences`; the client must reply with
`nativeSearchEnhancementsEnabled` (required boolean), plus optional
`memoryEnabled`, `askUserQuestionAutoResolutionEnabled`, `toolSearchMode` and
`modelContextBudgetStrategy`. Until that reply arrives, `session/create`
times out after 15 s.

The remaining blocker is standalone provider configuration: `zcode app-server`
does not load the desktop account provider, so an `account:*` model fails with
`Provider Registry 中不存在 Model`. `--prompt` has the same problem earlier at
`CliDefaultSelection.prepareInput` (`Select a model`). A live ZCode lane needs
the account provider snapshot injected, or a built-in provider configured with
an API key, before the protocol can create a session.

### ACP transport (DSH / MiMo / MiniMax)

`src/server/services/native_runtime/acp_runtime.py` implements the shared ACP
stdio client/runtime:

```text
initialize
session/new | session/resume
session/set_config_option (model / MiniMax permissionMode=default)
session/prompt
  session/update* (text, thinking, tool, usage)
  session/request_permission -> native approval broker + HITL
session/cancel
```

Enable per provider:

```dotenv
NATIVE_CLI_ACP=deepseek-dsh,mimo,minimax
NATIVE_CLI_ACP_DSH_MODEL='["deepseek-account","deepseek-flash"]'
```

Live proof on this host: DSH and MiMo native threads stream and complete;
MiniMax native thread streams and completes after `mcode login`; MiMo and
MiniMax tool permission requests interrupt, approve, execute, and finish.
MCP servers are passed through `session/new` with absolute stdio commands.
The memory/skills bridge is injected for DSH, MiMo, MiniMax, Kimi and Grok;
MiMo live proof: `conductor_memory_remember` returned a new `native-*.md` entry
through the ACP MCP bridge. ACP capabilities now advertise memory/skills as
well as MCP.

DSH permission live proof: a write outside the session workspace first failed
under `workspace-write`, then DSH requested `danger-full-access` through ACP
`session/request_permission`; approve resumed the same turn and created the
file, followed by `turn_completed`.

### MiniMax project MCP

MiniMax loads project-level `<cwd>/.mcp.json`. The native lane merges the
secret-less MCP set into that file before the turn and restores the user's file
afterwards. The set is capped (default 4 servers`NATIVE_CLI_MINIMAX_MCP_LIMIT`)
so cold-start discovery stays inside the turn budget. A live native MiniMax
turn called an MCP tool and answered with the value it returned.

#### ZCode credentials probe

Copying the desktop `~/.zcode/v2/credentials.json` to the CLI path
`~/.zcode/cli/credentials.json` did not materialize the account provider:
`session/create` still reported the `account:bigmodel-*` model missing. The
standalone app-server does not read the same account snapshot location as the
desktop app. The copied file was removed after the probe.

#### ZCode model-rule probe

Adding a matching `builtinProviderModelRules` entry
(`{providerId: xiaomi-mimo-token-plan, modelId: mimo-v2.6-pro, config: {enabled: true}}`)
alongside the provider rule still produced
`Provider Registry 中不存在 Model` under `zcode app-server`. The standalone
runtime does not consume the injected built-in config's provider/model records;
it needs the desktop account snapshot or a real standalone model catalog.

### ZCode stream-json live path (2026-10-29)

`--output-format stream-json` exposes the headless session event stream without
the app-server model-catalog blocker. The native adapter now uses it:

```text
model.streaming: text_delta / reasoning_delta / tool_input_start / tool_call
tool.updated:    scheduled / started / result / batch
turn.completed:  usage + terminal
```

Live native proof: a ZCode thread created `/tmp/zcode-native-stream.txt`, emitted
`message_chunk` text/thinking, `tool_calls` and `tool_call_result`, and finished
with `run_end completed`. Streaming and tool_events are now advertised for
ZCode; approvals remain false because the headless stream has no
`permission.requested` reply channel. `--mode yolo` is used for native turns in
the private workspace mirror; detached delegation stays `--mode plan`.

### Grok and StepFun approvals (2026-10-29)

Grok exposes ACP over `grok agent stdio`; with an isolated Grok home whose
config says `permission_mode = "default"`, ACP `session/request_permission`
becomes a real HITL interrupt. Enable with:

```dotenv
NATIVE_CLI_ACP=...,grok
```

StepFun exposes a JSON RPC mode (`step --mode rpc`) whose tool approval is an
`extension_ui_request` with method `confirm`; the runtime answers
`extension_ui_response`. Enable with:

```dotenv
NATIVE_CLI_STEPFUN_APPROVALS=1
```

Both were live-tested through the real native-thread API: tool_approval
interrupt -> approve -> file created -> final message -> run_end completed.

### ZCode approvals via REA permission-broker patch (2026-10-29)

ZCode's headless CLI emits `permission.requested` but its default
`DenyPermissionBroker` immediately resolves with
"No permission client configured". REA located that one minified class. The
runtime now generates a cached **copy** of the vendor CLI with the class
replaced by a file-backed broker; the vendor app is never modified in place.
The copy is opt-in:

```dotenv
NATIVE_CLI_ZCODE_APPROVALS=1
```

Flow:

```text
stream-json permission.requested
  -> <tmp>/<requestId>.request.json
  -> native approval broker -> HITL interrupt
  -> approve writes <requestId>.response.json
  -> patched broker resolves allow -> tool executes -> turn completes
```

Live native proof: a ZCode thread emitted `tool_calls` for Write, raised a
`tool_approval` interrupt, resumed with approve, created
`/Users/you/zcode-native-approval.txt`, and finished with
`zcode-approval-done` / `run_end completed`.

Capabilities for ZCode become streaming/tool_events/approvals/mcp/memory/
skills/cancel while the flag is set. The patched copy lives under
`~/.cache/conductor/native-cli/zcode/` and is only used by this opt-in runtime.

### ZCode app-server REA probe (2026-10-29)

After `zcode login bigmodel` (guojhy25 / GLM-5.3), the app-server protocol was
driven directly:

- stdio is newline-delimited JSON;
- `session/create` triggers the reverse request
  `session/requestRuntimePreferences`; the client must answer the same request
  id with `result` containing `nativeSearchEnhancementsEnabled` plus the other
  preference fields, or the create call times out after 15 s;
- a successful create returns `session`, `settings`, `projection`, and
  `runtime` state;
- `session/send` returns `{accepted:true,...}` and emits
  `state.updated`, `computer-use/operation-event` (`turn-started` /
  `turn-failed`) and `v4/telemetry/event` (`turn.started` / `turn.terminal`).

The remaining blocker is still model materialization. The logged-in account
model `account:bigmodel-individual-coding-plan/GLM-5.3` works through
`zcode --prompt`, but app-server `settings.model.available` is empty and
`session/create` with that model fails with
`Provider Registry 中不存在 Model`. Loading the builtin provider plus the
logged-in personal provider rules, and calling
`provider/updateAccountConfig` + `provider/refreshRegistry`, did not materialize
the catalog. Therefore app-server streaming/tool events/approvals cannot be
live-claimed for ZCode yet; the one-shot native path remains the supported
ZCode lane.

### ZCode live resolution (2026-10-28)

The official CLI's open-source docs identify the standalone model
configuration file: `~/.zcode/v2/provider_config.json` (overridable with
`ZCODE_PERSONAL_PROVIDER_CONFIG_FILE`). The older probes tried to inject
desktop `account:*` providers and built-in model rules; those still do not
materialize a catalog. A **personal API-key provider** does.

The working shape, generated by `ZCodeCliAdapter` with mode `0600`:

```json
{
  "schemaVersion": 1,
  "config": {
    "providerConfigRules": {
      "providerRules": [{
        "providerId": "alpha-zai",
        "providerName": "Conductor Z.ai",
        "config": {
          "group": "standard-personal",
          "access": {"type": "api-key", "apiKey": "<redacted>"},
          "api": {
            "type": "anthropic-messages",
            "baseUrl": "https://open.bigmodel.cn/api/anthropic"
          },
          "personalModelIds": ["GLM-5.3-Flash"]
        }
      }]
    },
    "modelConfigRules": {"providerModelRules": [], "manualProviderModelRules": []},
    "defaultModelSelection": {"providerId": "alpha-zai", "modelId": "GLM-5.3-Flash"}
  }
}
```

Live result: `zcode-native-ok`, session `sess_32058...`, `cacheReadTokens=142976`,
`totalTokens=152291`. The native runtime one-shot path returned
`zcode-runtime-ok` with `cache_read_tokens=143488`.

### ZCode MiMo-template probe

A follow-up probe tried the shipped `xiaomi-mimo-token-plan` template as a
custom built-in provider (`providerId=xiaomi-mimo-token-plan`, builtin model
ids `mimo-v2.6-pro`/`mimo-v2.6-flash`, dummy API key). `session/create` still
reported `Provider Registry 中不存在 Model`, so injecting provider rules into
the built-in file alone does not materialize model entries. The standalone
runtime needs the model registry entry as well as the provider overlay.

#### ZCode active-builtin model probe

The app-server ignores `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` for provider/model
materialization and reads the active built-in file. Adding
`builtinProviderModelRules` for `account:bigmodel-individual-coding-plan`
directly to that active file, then injecting the account config and refreshing
the registry, still leaves `settings.model.available=[]`. The active file was
restored afterwards. The remaining blocker is therefore not the path used to
load providers; the standalone runtime does not build a model catalog from
these rules alone.

#### ZCode account-config multi-provider probe

Sending all 10 account providers from the built-in release with
`availability=available, entitled=true, current=true` is accepted by
`provider/updateAccountConfig`:

```text
receivedRevision=alpha-account-v3
providerCount=10
status=received
```

`provider/refreshRegistry` also returns refreshed, but a model-less
`session/create` still reports `settings.model.available=[]`. A further probe
that added `builtinProviderModelRules` for `GLM-5.3`/`GLM-5.3-Flash` did not
change that. The account snapshot reaches the process, but the standalone
model catalog is still empty.

#### ZCode account-config protocol probe

`provider/updateAccountConfig` is accepted by the standalone app-server when
the account provider is reduced to the schema it actually accepts:

```json
{
  "access": {"type": "zhipu-account", "entitled": true},
  "builtinModelIds": ["GLM-5.3", "GLM-5.3-Flash"]
}
```

The response is `receivedRevision=... providerCount=1 status=received`, and
`provider/refreshRegistry` returns refreshed. `session/create` with the
account model still reports the model missing, so injecting the account
snapshot alone does not populate the standalone model catalog. The next
required piece is the account/registry model materialization.

#### ZCode model-less session probe

`session/create` succeeds when no model is supplied, proving the NDJSON
session lifecycle is healthy, but the returned settings show
`model.available=[]`. A subsequent `session/send` is accepted and then fails
with `prompt_failed`; the standalone runtime still has no model catalog entry
to run a turn. This rules out the protocol as the blocker.

### ZCode standalone provider probe

A second probe tried a custom built-in provider (`alpha-ollama` over an
OpenAI-compatible local endpoint) by appending a `providerRules` entry to a
copy of `zcode-builtin.json` and pointing `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE`
at it. `session/create` still rejected the model with
`Provider Registry 中不存在 Model`, so a custom provider needs a matching
registry/model rule beyond the provider overlay. ZCode remains blocked on
standalone provider materialization, not on the NDJSON protocol.

### End-to-end verification (2026-10-28)

Two backend SSE runs were exercised against the local host backend:

- **ZCode live**: `POST /threads/{id}/messages` on a native ZCode thread returned
  `event: message_chunk` with `content: "zcode-native-backend-ok"`, then
  `context_window` (`input_tokens=152309`, `output_tokens=42`) and
  `run_end outcome=completed`;
- **Codex HITL**: a fake `codex app-server` emitted a command approval request.
  The backend stream emitted `event: interrupt` with
  `action_requests=[{"name":"mcp__codex__command",...}]` and
  `run_end outcome=interrupted`. A follow-up `POST` with `hitl_response`
  (`approve`) resumed the same thread and returned
  `tool_call_result "hitl-ok"`, `message_chunk "approved-accept"`, and
  `run_end outcome=completed`.

The frontend composer in a real browser loaded a workspace chat and rendered
exactly `PTC / Codex / DeepSeek DSH / ZCode`, proving the new-thread selector is
driven by `GET /api/v1/native-agents` and only includes installed +

### Native skills bridge

The same capability token also opens `/api/v1/native/skills` and
`/api/v1/native/skills/<name>`. A generated `conductor_skills` MCP server exposes
`list_skills` and `load_skill`, and is injected alongside `conductor_memory` for
Codex/DSH/MiniMax/MiMo. Live proof covers all four live runtimes (Codex, DSH, MiniMax, MiMo): each
listed the effective skills, loaded `chart-annotation`, and reported its
`# Chart Annotation Skill` heading.

### Native memory write (explicit)

`POST /api/v1/memory/remember` writes one caller-supplied text into the user's
LangGraph memory store: a detail Markdown file plus a pointer in `memory.md`,
then best-effort projection. It is idempotent by text hash and rejects empty
or >1000-character text. The write half is bridged to DSH/MiniMax/MiMo through a generated
`conductor_memory` MCP server. It receives a short-lived HMAC capability scoped to
one user/thread; auto-extraction from native turns is deliberately not enabled.
The capability row now advertises `memory=true` for Codex, DSH, MiniMax and MiMo.
Live proof covers DSH, MiniMax and MiMo: each called the MCP `remember`
tool, `GET /api/v1/memory/user` listed the returned `native-*.md` entry, and
`/memory/forget` removed it afterwards. Codex is injected through its config `env_vars` allowlist: the capability token
is passed in the child environment, not argv. A live Codex turn called
`remember` and the entry appeared in `/memory/user`.

## Remaining provider status

Adapters exist for:

```text
deepseek-dsh, codex, kimi, zcode, minimax, mimo, grok, stepfun
```

`deepseek-dsh`, `codex`, `minimax`, and `mimo` now have recorded live proofs:

- Codex: two successful turns, MCP tool call, tool replay, cancellation.
- MiMo: two successful turns through a native thread using the user's MiMo
  Token Plan credential, plus a live MCP turn and a live file-write turn. The adapter injects secret-less
  stdio/HTTP servers through `MIMOCODE_CONFIG_CONTENT.mcp`, runs native threads
  with `--yolo`, and sets `permission.external_directory=deny` so a file tool
  cannot write outside the thread's workspace mirror. The adapter supplies `MIMOCODE_CONFIG_CONTENT` and
  `MIMOCODE_MIMO_API_KEY` through the child environment (never argv), with the
  token-plan SGP base URL and `xiaomi/mimo-v2.6-pro`; the second turn reused the
  MiMo session `ses_...`.
- MiniMax: two successful turns through a native thread using the user's
  Conductor `minimax-coding` key. `mcode --output-format stream-json` is parsed
  into native events; the live greeting turn completed through the normal SSE
  lane. The credential is passed through the child
  environment to `mcode provider add` (never argv), with a private data dir and
  a ready marker; the second turn reused the mcode session.
- DSH: two successful turns through a native thread using the installed DSH
  account grant; the headless profile is patched to enable the local
  `deepseek-account` provider instead of requiring an API key. The adapter
  writes a stable hashed `alpha-native-patch-*.yml` under the DSH home and
  preserves the session id; the second turn reused the same DSH session.
  A later DSH live turn also called MCP tools through the inserted
  `dsh-mcp-client` entries; DSH's NDJSON stream was normalized into live
  `message_chunk`, `tool_calls` and `tool_call_result` frames.

MiMo remains contract/fake-CLI tested and marked
`live_verified: false`. They must not be offered as native-ready until one real
turn passes per provider.

## Next phases

1. MCP/plugin bridge: generate each CLI's native MCP config from Conductor's
   installed servers; add explicitly supported tools only.
3. Memory bridge: first-turn context + post-turn proposal extraction; no hidden
   per-turn reinjection.
2. Streaming transport per provider (ACP where possible).
3. Native MCP/plugin bridge.
4. Live parity matrix: stream, tool loop, session, cache, approval, workspace,
   model override, images/PDF, MCP, cancel.


## Native MCP and search

Native threads run the vendor CLI's own prompt/tool loop and MCP configuration;
Conductor forwards its secret-less builtin MCP servers plus memory/skills bridges,
and does not force the Conductor WebSearch provider. There is therefore no need to
configure Tavily/Serper/Bocha for a native CLI: whatever search/tools the CLI
itself exposes are what the model can call. The Conductor `web_search` path remains
separate and still needs a search-provider key for non-native turns.

Useful env for this host:

```dotenv
NATIVE_CLI_ENABLED=1
NATIVE_CLI_ACP=deepseek-dsh,mimo,minimax
NATIVE_CLI_STEPFUN_APPROVALS=1
NATIVE_CLI_MIMO_BIN=/Users/you/.mimocode/bin/mimo
NATIVE_CLI_MINIMAX_BIN=/Users/you/.npm-global/bin/mcode
NATIVE_CLI_STEPFUN_BIN=/Users/you/.npm-global/bin/step
```
