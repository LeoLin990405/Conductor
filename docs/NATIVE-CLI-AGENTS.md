# Native agent CLI providers

Status: **13 native providers are registered** — DeepSeek/DSH, Doubao/ArkCLI,
Codex, Claude Code, Cursor, Kimi, Grok, StepFun, ZCode, Zhipu, MiniMax Code,
MiMo Code and OpenCode. Each has an adapter, a host binary probe, a session
contract and explicit capabilities. Gemini remains PTC-only by design. Qwen
remains PTC-only unless the user explicitly approves the documented compat
Worker activation; no such activation is made here. Vendors without an
installed official CLI remain planned.

## Why an agent backend, not a chat-completions transport

A native CLI agent (Codex, Kimi CLI, DSH, Grok, StepFun)
owns three things a plain chat completion does not:

1. its own system prompt and tool harness;
2. a persistent session identity;
3. its provider-side prompt/KV cache accounting.

Calling it with `-p` / `exec` / `headless` as a one-shot **agent** preserves
those, while a fake OpenAI-compatible wrapper around the CLI would lose the
cache and the tool semantics. The integration point is therefore an external
delegation backend, matching DSH's `subagent` provider pattern: a task goes out,
one final answer comes back.

## Architecture

```
main agent
  └─ NativeAgent tool (flag-gated, main-only)
       └─ adapter registry
            └─ DeepSeekDshAdapter (first)
                 └─ async subprocess
                      └─ dsh --profile headless --json [--session-id] -
                           └─ NDJSON events: text/thinking/final/usage
```

- `src/llms/native_cli/base.py` — invocation/result/usage contract.
- `events.py` — NDJSON event parser (text, final, session, cache usage).
- `process.py` — async subprocess, timeout/cancel, bounded output.
- `deepseek.py` — DSH argument grammar and adapter.
- `claude.py` — Claude Code `--print --output-format json` adapter.
- `cursor.py` — Cursor `cursor-agent --print --output-format stream-json`.
- `sessions.py` — atomic, private session-id store keyed by Conductor thread.
- `registry.py` — verified adapters only. `get_adapter` takes the registered names. `get_read_only_adapter` is the only way a read-only turn such as a cross-review is built, and it alone also takes the dispatch pool's spellings (`claude`, `deepseek`; see "Read-only mode").
- `src/ptc_agent/agent/tools/native_agent.py` — main-agent `NativeAgent` tool.
- `scripts/native_cli_smoke.py` — live smoke probe.

`NativeAgents` runs a small dependency DAG inside one ticket worker invocation.
`depends_on` uses earlier task indexes; independent ready tasks run together up
to `NATIVE_CLI_MAX_PARALLEL` (capped at four), and a newly ready task starts as
soon as its own prerequisites finish. Results stay in that invocation and are
returned in input order. A failed task is reported in its result; dependent
tasks still run and receive that result as context. Cancellation of the parent
tool cancels its active children. This is not a persistent ticket scheduler:
there is no task-row claim, cross-process resume, or distributed DAG state.

## DeepSeek/DSH adapter (iteration 1)

Install (already done on this host):

```bash
npm install -g @deepseek-ai/dsh@0.2.1-rc.2
dsh --version   # 0.2.1-rc.2
```

The CLI is installed by the operator, not downloaded at request time. A
deployment either ships it in the image, mounts it, or sets
`NATIVE_CLI_DSH_BIN`. First use initializes the headless profile under
`NATIVE_CLI_DSH_HOME` (default `~/.cache/conductor/dsh`).

Live invocation used by the adapter:

```bash
dsh --profile headless --json --session-id <id> -   # prompt on stdin
```

Long prompts use stdin (`-`), never argv. The adapter parses the NDJSON stream
and exposes `cacheReadTokens` separately from input/output. `--session-id`
resumption is the cache lever; a stable per-thread cwd is the second lever.

### Live proof (2026-10-27, this host)

First call:

```json
{"input_tokens": 224, "cache_read_tokens": 9344, "cache_write_tokens": 0,
 "total_tokens": 9570, "cache_read_ratio": 0.9766}
```

Second call, same session id via the session store:

```json
{"input_tokens": 238, "cache_read_tokens": 9344, "cache_write_tokens": 0,
 "total_tokens": 9584, "cache_read_ratio": 0.9752}
```

The session id was returned unchanged and the cached prefix stayed at 9,344
tokens. Raw evidence is in
internal research evidence (not published).

### Enable in Conductor

```dotenv
NATIVE_CLI_ENABLED=1
NATIVE_CLI_PROVIDER=deepseek-dsh  # or codex / claude-code / cursor / kimi / ...
# optional:
# NATIVE_CLI_DSH_BIN=/path/to/dsh
# NATIVE_CLI_DSH_HOME=/path/to/dsh-home
# NATIVE_CLI_WORKSPACE_ROOT=/path/to/private/workspaces
```

With the flag off (default), the tool is not added to the agent at all. It is
main-agent only; subagents never see it. Errors are returned as tool text, not
raised into the graph.

### Auto provider selection (Phase 2 partial)

`NativeAgent(provider="auto")` uses the same capability-aware router as
`native_provider="auto"` on a thread, but with `allow_ptc=False`: the tool must
return from a native runtime or report a visible failure. It selects only
`installed && live_verified` providers, prefers coding tasks to Codex and
Chinese research to DSH, and returns one final answer in a private per-thread
cwd.

Auto delegation tries the router's ordered live candidates until one succeeds.
A failed primary (for example an expired Codex session) falls through to the
next live provider; explicit `provider=...` calls never fall through. The
final error names the attempted chain and each provider's message.

When the caller supplies `user_id`/`workspace_id` (the PTC factory does), the
tool forwards Conductor's explicit bridges to the CLI: secret-less builtin MCP
servers plus `conductor_memory` and `conductor_skills` with a short-lived capability
token. The token is an environment value, never argv. Codex runs the delegated call
with `--approve-for-me` (automatic review, workspace-write) when the bridge is
present, so its one-shot exec can complete MCP writes; this is Codex's
automatic reviewer, not a user-facing nested HITL card. DSH/MiniMax/MiMo use
their own permission modes.

Live on this host, all four MCP-capable providers have written through the
same `conductor_memory` bridge:

| Provider | Delegation | Memory entry |
|---|---|---|
| Codex | auto | `native-agent-approve-ok` |
| DSH | deepseek-dsh | `native-agent-dsh-bridge-ok` |
| MiniMax | minimax | `native-agent-minimax-bridge-ok` |
| MiMo | mimo | `native-agent-mimo-bridge-ok` |

The memory read API showed each new user entry. This proves the bridge is not
Codex-specific.

Delegation also resolves the user's MiniMax/MiMo credentials from Conductor's
provider store and hands them to the adapter (child environment, never argv);
without that, a host whose `mcode`/`mimo` CLI is not separately signed in would
fail before reaching the bridge. True user-approved nested HITL remains future
work.

### Parallel delegation

The main agent also gets `NativeAgents(tasks=[...])` when the flag is on. It
runs up to four tasks in dependency waves, each with its own native session and
private cwd, and returns labelled sections in input order. A `depends_on`
list references earlier task indexes; dependents wait for those results while
independent tasks still run concurrently. Each dependent's prompt is extended
with the labelled results of its prerequisites, so a later native task can
consume earlier native work as text rather than only as ordering. A failed task returns its own error
instead of failing the batch. The shared MCP bridge list is collected once per
batch.

Live on this host:

```text
NativeAgents([
  {"prompt": "Reply with exactly: parallel-one", "provider": "auto"},
  {"prompt": "Reply with exactly: parallel-two", "provider": "auto",
   "depends_on": [0]},
])
→ ### Native task 1 [auto]
  parallel-one

  ### Native task 2 [auto]
  parallel-two
```

With `NATIVE_CLI_STREAM_DELEGATION=1` (or `all`), Codex/DSH/MiniMax
delegations use the native runtime's streaming parser instead of the one-shot
adapter. The value may also be a comma-separated allowlist (for example
`codex,deepseek-dsh`) so a host can gray-launch one CLI at a time. Text is
still collected into one tool result, but the CLI's tool_started/tool_result
events become `native_agent {phase:"tool", tool_name,...}` progress notices; the
final cache/input/output usage comes from the stream's usage event. MiMo and
non-streaming paths keep the one-shot adapter.

Native delegations also emit `native_agent` custom-stream events at the tool
boundary: `started` (provider/lane/prompt size), `completed` (provider plus
cache/input/output tokens) and `failed` (provider/error). The SSE producer maps
them to a `native_agent` frame, and the chat stream folds each frame into a
transcript notification (info/success/warning; batch lane appended when not
`main`). This is delegation progress, not a claim that the CLI's internal tool
stream is forwarded.

Native delegations also emit provenance: `NativeAgent` records one
`native_agent` source keyed by provider, and `NativeAgents` records one batch
source with the task count. The existing provenance card shows the provider,
redacted arguments, result hash/snippet and tool-call id; the CLI's own internal
tools remain outside that record.

It is still a **delegation tool**, not yet the full orchestrator: nested HITL
and streamed subagent progress remain future work.

Live on this host:

```text
NativeAgent(provider="auto", prompt="Reply with exactly: native-agent-auto-ok")
→ router: codex
→ codex one-shot: native-agent-auto-ok
```

## Official CLI sign-in

Settings → **原生 CLI / Native CLI** exposes official sign-in for the
providers in the table below. The backend runs the vendor's own login command
in a PTY; it does not implement OAuth itself and never stores a token.

| Provider | Command | Flow |
|---|---|---|
| Claude Code | `claude auth login --claudeai` | URL + pasted authorization code |
| Cursor | `cursor-agent login` (`NO_OPEN_BROWSER=1`) | browser URL; CLI completes itself |
| Codex | `codex login` | browser URL; CLI completes itself |
| Kimi | `kimi login --region mainland-cn` | device-code flow in the browser |
| GLM / ZCode | `zcode login bigmodel --no-browser` | browser URL; CLI completes itself |

DeepSeek (DSH) has no standalone CLI login command. Its account token is
configured through **API & models → Connected accounts** (or
`DEEPSEEK_API_KEY` for the API-key route).

`GET /api/v1/native-agents/auth` asks each CLI for its signed-in state; the
login session endpoints are under `/api/v1/native-agents/{provider}/login` and
`/api/v1/native-agents/login/{session_id}`.

## Provider matrix

| Provider | Official CLI | Machine mode | Session/cache lever | Status |
|---|---|---|---|---|
| DeepSeek | `dsh` | `--profile headless --json` | `--session-id`, stable cwd | ✅ adapter + tool + live call |
| OpenAI | `codex` | `exec --json`, `exec resume` | thread id resume | ✅ adapter + parser; CLI cache observed |
| Anthropic | `claude` | `-p --output-format json` | `--resume`; plan mode | ✅ adapter + tests; native registered |
| Cursor | `cursor-agent` | `--print --output-format stream-json`; isolated daemon workers add `--trust --sandbox disabled` | `--resume <chat id>` | ✅ adapter + tests; native registered |
| Google Gemini | `gemini` | `-p --output-format json` | minted `--session-id` / `--resume` | ❌ PTC-only；不注册 native provider |
| Qwen | - | - | - | ❌ PTC-only by default; compat Worker activation requires explicit user approval and is not activated here |
| Moonshot/Kimi | `kimi` | `--prompt ... --output-format stream-json` / `kimi acp` | `--session <id>` only for an id Kimi created and named in its stream (`session.resume_hint`); never `--continue`, never a caller-owned id (N22) | ✅ one-shot adapter + ACP streaming live |
| xAI Grok | `grok` | `grok -p ... --output-format streaming-json --permission-mode auto` | `--resume <sessionId>` only for the id Grok reported; no `--session-id` (N22) | ✅ streaming/tool events；approval 未声明 |
| StepFun | `step` | `step --mode json -p ... --approval-mode auto` | `--session-id` | ✅ Step Plan 登录后 live；thinking/text/tool events |
| Z.ai / GLM | `zcode` (official, default); `zcode-cli` (clean-room, override only) | `--prompt ... --output-format stream-json --mode yolo` (native, needs a dedicated working directory) / `--mode plan` (delegation); official `zcode` (including its shell wrapper and the bundled app CLI) gets `--surface terminal` and no `--model`/`--max-turns`; the clean-room `zcode-cli`, selected only through `NATIVE_CLI_ZCODE_BIN`/`NATIVE_CLI_ZHIPU_BIN`, also gets `--model GLM-5.3 --max-turns 200` | `--resume <sessionId>` | ✅ native stream/tool events + write + MCP/memory/skills; approvals with `NATIVE_CLI_ZCODE_APPROVALS=1`; a spent turn budget is reported as `max_turns` |
| MiniMax | `mcode` | `exec --input - --output-format json` | `--session <sessionId>` | ✅ adapter + fake-CLI tests; live sign-in required |
| Xiaomi MiMo | `mimo` | `run --format json` | `--session <sessionID>` | ✅ adapter + fake-CLI tests; live auth missing |
| OpenCode | `opencode` | `run --format json --standalone` (write only with explicit `--auto`) | `--session <sessionId>` | ✅ `OpenCodeAdapter`; registered in `DEVELOPMENT_WORKERS`; not yet in the dispatch role pool or `dispatch_launch.sh` |
| ByteDance / Doubao | `arkcli` | `+chat --format json --no-progress` | `--store` + `--previous-response-id <id>` | ✅ `DoubaoArkCliAdapter`; native registered + Worker; text-only |
| Others | official CLIs | per CLI | per CLI | after evidence |

Daemon worker Tickets use the outer worker isolation for filesystem policy.
Cursor adds `--trust --sandbox disabled` only when that outer isolation is
active. It omits `--mode plan` for read-only Tickets under a read-only outer
sandbox; without isolation, `--trust` and `--sandbox` are absent and a
read-only request keeps plan mode.
DSH's installed 0.2.1-rc.2 headless profile uses `sandbox-policy` and `approval`
entries. Under worker isolation, its private profile patch selects the
`danger-full-access` preset and `approval: never`; the outer OS sandbox remains
the boundary and no prompt can block the daemon. Outside isolation, no patch is
added.
StepFun read-only Tickets use JSON/tool mode only inside an actual read-only
worker isolation. They pass `--approval-mode auto --non-interactive-approval allow`
so read tools can run; the outer sandbox denies writes. The installed Step CLI
help lists `confirm|auto` and `allow|deny`, not `strict`. Outside that isolation,
read-only StepFun stays on its text-only, no-tools path.

Daemon failures include a provider failure class and reason code plus a fixed,
redacted message. Vendor stderr and stdout are not copied into the Ticket or log.

Each adapter must have: argument builder, NDJSON/text parser, session reuse,
cache-usage mapping, fake-CLI tests, and one live proof recorded in
internal research evidence (not published).

## Development Workers

### Daemon credential policy

For a daemon worker that uses DeepSeek API-key authentication, add
`DEEPSEEK_API_KEY` to the policy `env` names. The daemon's own environment must
provide the value. Keep `~/.config/deepseek/env` outside `read_paths`.
When `DEEPSEEK_API_KEY` is already present in the child environment, DSH skips
the entire env file, including any non-key settings it might contain.

Nine names are registered in `DEVELOPMENT_WORKERS`. Eight of them are launched
by `dispatch_launch.sh` for implementation tasks; `opencode` is registered but
not yet in the dispatch role pool or the launcher. Doubao's adapter is
text-only. Claude (the planner) and Codex (the auditor) are **reserved roles**
— they decide and judge, and are never started as a Worker.

| Worker (roster name) | CLI | Native runtime key | Adapter |
|---|---|---|---|
| `kimi` | `kimi` | `kimi` | `KimiCliAdapter` |
| `glm` | `zcode` (official default; `zcode-cli` only via override) | `zcode` | `ZCodeCliAdapter` |
| `grok` | `grok` | `grok` | `GrokCliAdapter` |
| `cursor` | `cursor-agent` | `cursor` | `CursorAgentAdapter` |
| `stepfun` | `step` | `stepfun` | `StepFunAdapter` |
| `deepseek` | `dsh` | `deepseek-dsh` | `DeepSeekDshAdapter` |
| `doubao` | `arkcli` | `doubao` | `DoubaoArkCliAdapter` |
| `minimax` | `mcode` | `minimax` | `MiniMaxCodeAdapter` |
| `opencode` | `opencode` | `opencode` | `OpenCodeAdapter` (registered; not in launcher/role pool yet) |

The roster name is the dispatch concept; the runtime key is the registry
concept. `DEVELOPMENT_WORKERS` in
`src/server/services/native_runtime/registry.py` is the one mapping between
them, and `worker_provider_id(name)` resolves it — refusing an unknown name and
refusing a reserved role.

GLM maps to `zcode`, not `zhipu`: both are the same CLI, but the `zhipu`
adapter only offers `cancel`, while a Worker needs the full capability set.

GLM uses the official `zcode` CLI by default. Binary selection is
`NATIVE_CLI_ZCODE_BIN` first, then `NATIVE_CLI_ZHIPU_BIN`, then `zcode`; the
clean-room `zcode-cli` is used only when one of those overrides names it. This
is an operator installation preference only; no CLI was installed for this
documentation update.

The `zhipu` key stays in `src/server/services/native_runtime/registry.py`, and
it stays there on purpose. It is **not** a Worker: nothing in the dispatch
roster resolves to it. It is kept for the two things that still name it — the
status query (`status_for`) and the disabled-provider policy
(`_disabled_providers`, `disabled_provider_reason`) — so a lane that asks for
`zhipu` keeps working. A Worker that needs the full GLM capability set has to
ask for `zcode`, which is what `DEVELOPMENT_WORKERS` does.

`minimax` maps to `minimax` (`MiniMaxCodeAdapter`), **not** to `mmx`
(`MiniMaxMmxAdapter`), which is a text-only channel.

### The launch script

`scripts/dispatch_launch.sh` is the seam `ScriptLauncher` calls:

```sh
dispatch_launch.sh <model> <task> [--skills=<s>] [--impact-map=<p>]
```

`<model>` is the roster name. CLI spellings (`zcode`, `zcode-cli`, `step`,
`dsh`, `arkcli`, `mcode`, `cursor-agent`) fold onto the roster.

| Worker | argv the CLI receives |
|---|---|
| `kimi` | `kimi -p "<prompt>"` |
| `glm` | Grammar by the two-step rule below. Clean-room `zcode-cli`: `--prompt "<prompt>" --output-format stream-json --mode yolo --model <model> --max-turns 200 --no-color`. Official `zcode`, including its shell wrapper and the bundled app CLI: the same argv without `--model` and `--max-turns`, plus `--surface terminal`. Neither grammar receives `--provider`. |
| `grok` | `grok --prompt-file <prompt-file> --always-approve --reasoning-effort high --max-turns 150 --output-format streaming-json` |
| `cursor` | `cursor-agent -p --force --trust --workspace "$PWD" --output-format stream-json "<prompt>"` |
| `stepfun` | `step -p --no-session --approval-mode auto --non-interactive-approval allow --mode json "<prompt>"` |
| `deepseek` | `dsh --profile headless -` (prompt on stdin) |
| `doubao` | `arkcli +chat --format json --no-progress -- <prompt>` |
| `minimax` | `mcode exec --cwd "$PWD" --prompt-mode coding --permission full --output-format stream-json --input -` (prompt on stdin) |

For `glm`, the grammar is decided in two steps, the same rule in
`dispatch_launch.sh` and `ZCodeCliAdapter._is_clean_room_cli`. First, take the
invoked name after `command -v` and before resolving links: `zcode-cli` is
clean-room. Otherwise resolve the target (`realpath` / `Path.resolve()`) and
compare its basename: `zcode-cli` is clean-room too. Anything else — the
official `zcode`, its shell wrapper and the bundled app CLI — takes the official
grammar: `--surface terminal`, no `--model` and no `--max-turns`. Step one means
a wrapper named `zcode-cli` stays clean-room even when it points at another
file; step two means an override that is a symlink such as
`glm-custom -> zcode-cli` is clean-room as well.

The prompt is the task, plus a line for `--skills` and one for `--impact-map`
when they are given. It is written to `<log-dir>/<model>-<timestamp>-<pid>.prompt`
before the CLI starts. The working directory is the caller's own: the script
never changes directory.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | the Worker finished successfully |
| the CLI's own code | the Worker failed; the code is passed through unchanged |
| `64` | the arguments are wrong: a missing argument, an unknown flag, an extra argument, or an unknown model |
| `65` | the model is a reserved role (`codex`, `claude`, `claude-code`, `codexrev`, `claudedev`), not a WORK worker — the binary is never started |
| `124` | the run overran `CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT`, or the log stopped growing for `CONDUCTOR_DISPATCH_LAUNCH_STALL` |
| `1` | the launch group could not be reclaimed: live members survived the sweep. Their pids are named on stderr. The sweep result outranks the Worker's own exit code, so a launch that left something behind is never reported as a success |
| `127` | the CLI binary is not installed |

### Environment

| Variable | Default | Meaning |
|---|---|---|
| `CONDUCTOR_DISPATCH_LAUNCH_SCRIPT` | `~/conductor-wt/launch.sh` | the script the dispatch launcher calls. Point it at `scripts/dispatch_launch.sh` to use this one. |
| `CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT` | `5400` | seconds for the whole run; on expiry the process group is stopped and the run ends `124` |
| `CONDUCTOR_DISPATCH_LAUNCH_STALL` | `900` | seconds without **log growth**; on expiry the process group is stopped and the run ends `124` |
| `CONDUCTOR_DISPATCH_LAUNCH_STOP_GRACE` | `10` | seconds a stopped Worker is given to exit on `SIGTERM` before it is killed with `SIGKILL`. It is also the number of sweep rounds, and each round waits a second after the `SIGTERM` and a second after the `SIGKILL`, so the sweep phase can take up to about twice this long |
| `CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR` | `${XDG_STATE_HOME:-$HOME/.local/state}/conductor/dispatch-launch` | where the prompt, the run log and the watchdog line are written (mode `0700`; files `0600`) |
| `NATIVE_CLI_<X>_BIN` | per provider | overrides the binary for one provider (`NATIVE_CLI_KIMI_BIN`, `NATIVE_CLI_ZCODE_BIN`, `NATIVE_CLI_MINIMAX_BIN`, …) — the same names the runtime registry uses. GLM is the `zcode` key, so `NATIVE_CLI_ZCODE_BIN` is its name; `NATIVE_CLI_ZHIPU_BIN` is accepted as well, because the same CLI is registered under both |
| `CONDUCTOR_GLM_MODEL` | `GLM-5.3` | the model the GLM Worker is launched with |
| `CONDUCTOR_GLM_PROVIDER` | `bigmodel-individual-coding-plan` | the provider the GLM Worker is launched with |

To use the repository's script instead of the operator's own:

```sh
export CONDUCTOR_DISPATCH_LAUNCH_SCRIPT=/path/to/this/repo/scripts/dispatch_launch.sh
```

The script holds no credential: it reads no key file and passes no `--api-key`.
Each CLI runs with the environment it already has, and the per-workspace
isolation policy supplies the provider's own values.

A stopped Worker is asked to exit with `SIGTERM` and killed with `SIGKILL`
after `CONDUCTOR_DISPATCH_LAUNCH_STOP_GRACE` seconds, so a CLI that traps or
ignores `SIGTERM` cannot keep the run, and the worktree, open.

The group is listed again after every round of the sweep, because a member
can fork a replacement while the group is being stopped, and that
replacement has to be found and stopped too. Each round waits a second
after the `SIGTERM` and a second after the `SIGKILL`, so the sweep phase can
take up to about `2 × CONDUCTOR_DISPATCH_LAUNCH_STOP_GRACE` seconds. A group
that is still not empty when that bound is reached fails the launch with
exit code `1` and the surviving pids on stderr, rather than being reported
as a clean run.

The Worker runs in the caller's own process group, on purpose: the dispatcher
cancels a launch by killing that group, and a Worker in a group of its own
would survive the cancellation and keep working in the worktree after the line
was reassigned. One cancellation boundary therefore covers the Worker, the
children the CLI forked, the tee and the helpers. A stop of the script's own
(the timeout, the watchdog) walks the Worker's process tree instead of
signalling a group, because the caller's group also holds the dispatcher.

### Adding a new Worker

1. Register the adapter in `src/llms/native_cli/registry.py` and the runtime
   entry in `src/server/services/native_runtime/registry.py`.
2. Add one line to `DEVELOPMENT_WORKERS` in the runtime registry.
3. Add a `case` to `run_worker` in `scripts/dispatch_launch.sh`, and add the
   binary to its model table.
4. Add a fake-CLI test in `tests/unit/scripts/test_dispatch_launch.py` and a
   roster test in `tests/unit/server/services/native_runtime/test_worker_roster.py`.
5. Add the Worker's policy row to `docs/ports/worker-isolation.md`.
6. Tell the role-policy owner and the Planner, so the Worker joins the
   developer pool and the roster stays in one place.

## Cache strategy

1. **Stable prefix** — fixed system/profile, fixed workspace path, no mutable
   timestamp or random id in the prompt.
2. **Session reuse** — persist the CLI's own session id per Conductor thread and
   provider; append-only turns reuse the provider cache.
3. **Stable cwd** — the native CLI's context discovery must not invalidate the
   prefix because a random temp directory changed.
4. **Measure, don't assume** — every result carries `cacheReadTokens`,
   `cacheWriteTokens`, and `cache_read_ratio`; a provider without these fields
   is not accepted as cache-verified.
5. **One session per conversation** — no cross-thread reuse; the session store
   key includes the Conductor thread.

## Security and failure rules

- Flag off by default; no automatic CLI download at request time.
- No shell: argv tuple + stdin only.
- Per-call timeout; kill process on timeout/cancel; bounded stdout/stderr.
- Provider credentials stay in the CLI's own environment; Conductor does not
  copy them into prompts or logs.
- No permission-bypass flags. The CLI runs in a private per-thread workspace.
- Tool failure is a visible `NativeAgent (...) failed: ...` result, never a
  silent empty answer.

### Read-only mode

A cross-review must not change the workspace it reads. `get_read_only_adapter(provider,
cwd=, timeout_seconds=)` builds an adapter in a mode its CLI itself enforces, or raises
`ReadOnlyUnavailable`; it takes no other arguments, so a caller cannot widen the mode. An
adapter that is not listed in `_READ_ONLY_MODES` (`registry.py`) is refused. The dispatch
pool's review role skips the refused models before a review starts
(`REVIEW_INELIGIBLE`, `src/tools/auto_dispatch/pool.py`), and a test keeps that list equal
to what the registry refuses. The factory resolves the pool's names (`claude` is
`claude-code`, `deepseek` is `deepseek-dsh`); `get_adapter` does not, because NativeAgent
keys its MCP bridge, credentials and streaming on the registered name.

| Provider | What a review runs | What enforces it | Evidence |
|---|---|---|---|
| Codex | `codex exec --sandbox read-only` | an OS sandbox | `codex exec --help` lists the policy; argv pinned by test |
| Claude Code | `claude -p ... --permission-mode plan --restricted --strict-mcp-config --disable-slash-commands --tools ""` | no tool exists to call; user, project and local settings (so their hooks) are ignored; no MCP servers | observed on Claude Code 2.1.309: a project hook ran under the old plan-mode-only argv and not under this one, which also left no plan file; `claude --help` for each flag; argv pinned by test |
| StepFun | `step -p --no-session --no-tools --no-extensions --no-approve` | no tools; no extension discovery; project-local files ignored | observed on step 0.1.6: a project extension ran under `--no-tools --approve` (which stands in for a folder the user trusted) and not under these flags, and the CLI still answered; argv pinned by test |
| Doubao | `arkcli +chat` | the adapter passes no `--tools`, so the call is a text reply | `arkcli +chat --help` lists `--tools` as opt-in |
| Z.ai / GLM | none | the only candidate is `--permission-mode plan` from `/tmp`, the same setting that did not hold on Claude Code; `zcode-cli` is not installed on the host, so no write was tried | `zcode` 0.17.1 is installed, `zcode-cli` is not |
| Kimi | none | `kimi --prompt` cannot be combined with `--plan` and runs its tool calls under the auto permission policy; the vendor page does not say how that policy treats a call that would need approval | `kimi --help`, the vendor's published documentation |
| DeepSeek | none | the `dsh` headless profile has no read-only switch; the profile decides its tools | `dsh headless --help` |
| MiniMax | none | `mcode exec --permission` is `smart`, `full` or `off`; none is documented as read-only, and what `smart` allows in a headless run is not documented | `mcode exec --help`, its README and changelog |

Only Codex's mode is an OS sandbox. The others work by withholding what could write:
Claude Code and StepFun load no tool, and Doubao's call has none. Claude Code's plan mode
is not what makes its reviewer read-only (the adapter still passes it because it always
has). Under plan mode alone a project hook ran and the model wrote a plan file under
`~/.claude/plans`, both observed here. A shell write under it was reported by one reviewer
and not reproduced in two attempts here: the model declined and the CLI recorded no denial
(`permission_denials` stayed empty), so that refusal was the model's, not the CLI's. The
other adapters (Grok, Cursor, MiMo, ZCode, the `mmx` text transport) are not declared
read-only, so they are refused too. A refusal means "no read-only mode was found that
holds", not "proven to write": the Kimi, MiniMax and GLM rows say what is left open.

A reviewer in a tool-less or text-only mode cannot open the repository, so the live
evidence carries it: `branch_evidence` (`src/tools/auto_dispatch/execution.py`) adds what
the line's branch changes against the repository's HEAD (`git diff HEAD...<branch>`, git's
external diff and textconv programs off). The whole prompt is held to 98,304 UTF-8 bytes
(`REVIEW_PROMPT_BUDGET_BYTES`): StepFun, Claude and Doubao take it as one argv string, which
Linux will not start at 131,072 bytes or more, and Codex's stdin gets the same bound. The
diff takes what the goal, task and branch leave, cut on a character boundary with the cut
stated; those parts have caps of their own (`docs/ports/auto-dispatch.md` lists them). The
reviewer is not shown the worker's output: the builder has a slot for it that the live
service does not fill, because the worker's log tail is raw and unredacted, and the prompt
says so in one line (worker output and test results are not part of the evidence, and their
absence is not success and not silence) instead of an empty `Output tail:` heading. If git cannot produce the diff, or the prompt would go over the budget, the
review fails and the dispatch ends in ERROR; no reviewer is asked about code nobody showed
it. A CLI the operator lists in
`native_runtime_routing.disabled_providers` is not started as a reviewer either: the live
selector skips it and the backend refuses it.

## One-at-a-time replication protocol

For each provider:

1. Record CLI `--help`/version and, where applicable, a REA analysis evidence id.
2. Implement the adapter + parser behind the registry (no UI/docs claims yet).
3. Add fake-CLI tests covering argv, stdin, JSON parse, session reuse, timeout,
   nonzero exit and missing binary.
4. Run one live task; record cache metrics and session id behavior.
5. Merge the adapter and update the matrix; only then start the next provider.

## Live cache evidence

DSH (main tool): cacheReadTokens 9,344 on both turns, 97.66% / 97.52%.

Codex (CLI-level, same thread id resumed):

```json
{"input_tokens":47232,"cached_input_tokens":20352,
 "cache_write_input_tokens":0,"output_tokens":10}
```

The adapter parses `thread.started` and `turn.completed.usage`, and reuses the
thread id through the session store. Codex's non-TTY run is slower under a
Python pipe than in an interactive shell; the tool keeps a 300s timeout and
surfaces a visible failure rather than hanging the agent turn.

## Credential state on this host (2026-10-27)

- `dsh` works with `DEEPSEEK_API_KEY` and a reachable local proxy.
- `codex login status` → logged in using ChatGPT; direct `codex exec` works.
- `claude` / `cursor-agent` binaries are installed; each needs its own
  one-time account sign-in before a live turn can run. The adapters parse their
  JSON/NDJSON result formats and register as native providers.
- `gemini` → no auth method in `~/.gemini/settings.json`.
- `qwen` → no provider credentials configured for headless mode.
- `kimi` → CLI installed, no configured session/provider for a live proof.

The adapters stay in the registry with fake-CLI tests; they are not advertised
as live-verified until the missing credential is supplied.

## Domestic second wave (2026-10-27)

Three additional domestic agent CLIs were replicated without bundling their
binaries or model data:

### ZCode (Z.ai / GLM)

- CLI: `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs`
  (`zcode 0.17.0`), launched through the app's own resource file.
- Headless surface: `--prompt`, `--output-format stream-json`, `--mode plan`,
  `--resume <sessionId>`, `--cwd`.
- Result object: `{type: "result", sessionId, response, usage, eventCount}`.
- Adapter: `src/llms/native_cli/zcode.py`; the adapter always names the mode —
  a review run stays `--mode plan`, and a write-capable run only gets
  `--mode yolo` when the caller designated a dedicated working directory for
  it (native threads use their private workspace mirror; MCP tools need a
  permission client in headless mode), otherwise it fails closed. It also
  always asks for `--output-format stream-json` and parses the transcript's
  terminal result, so a long run keeps its answer, session id and usage.
- Turn budget: the standalone `zcode-cli` flavor stops after 40 turns by
  default and reports the spent budget as an empty failed result, so the
  adapter passes `--model` and `--max-turns` (200 by default,
  `NATIVE_CLI_ZCODE_MODEL` / `NATIVE_CLI_ZCODE_MAX_TURNS` to override) when it
  drives that binary. A spent budget is reported as `ZCodeMaxTurnsError`
  (code `max_turns`), not as a generic failure. The desktop flavor exposes
  neither option, so it keeps the app's own budget; point
  `NATIVE_CLI_ZCODE_BIN` at `zcode-cli` to use the standalone flavor.
- MCP bridge: writes a transient project `.zcode/config.json` with absolute
  stdio/HTTP servers, including `conductor_memory`/`conductor_skills`, then restores
  the file after the turn; the mirror walk skips `.zcode`.
- Native streaming/tool events are live through `--output-format stream-json`
  (`model.streaming` text/reasoning/tool input, `tool.updated` lifecycle);
  approvals use the opt-in REA-patched permission broker
  (`NATIVE_CLI_ZCODE_APPROVALS=1`); the headless default broker denies without
  that bridge. A live HITL interrupt/approve then created the requested file. Live proof: logged-in BigModel account
  `guojhy25`/GLM-5.3 wrote a workspace file, injected 7 MCP servers (user +
  workspace config), and created a new `native-...md` memory entry through
  `conductor_memory`. A personal API key or a logged-in ZCode account is required;
  `zcode login bigmodel` is the China-site flow.
- Status: **live** through a generated personal API-key provider
  (`alpha-zai`, `GLM-5.3`); the bundled desktop account providers still
  cannot be materialized by the standalone CLI.

### MiniMax Code

- CLI: `mcode 0.7.1` (`npm i -g @minimax-ai/code`; run `mcode login` once).
- Headless surface: `mcode exec --input - --input-format text --output-format
  json --cwd <dir> --session <id> --permission smart`.
- Result object: `{type: "exec.result", sessionId, status, output, usage}`.
- Adapter: `src/llms/native_cli/minimax.py`; prompt through stdin, no argv
  length limit.
- Status: fake-CLI tests pass. Live call requires a one-time
  `mcode login`; ExecResult exposes input/output/total only,
  so this adapter does not claim cache counters.

### Xiaomi MiMo Code

- CLI: `mimo 0.1.16` (`curl -fsSL https://mimo.xiaomi.com/install | bash`; binary at `~/.mimocode/bin/mimo`).
- Headless surface: `mimo run --format json --dir <dir> --session <id> -- <prompt>`.
- Event stream: `text` parts, `step_finish` with
  `tokens.{input,output,total,cache.read,cache.write}`, `error`.
- Adapter: `src/llms/native_cli/mimo.py`; takes the last completed text part so
  intermediate turns are not concatenated into the final answer.
- Status: fake-CLI tests pass; live call currently returns `401 Invalid API Key`
  because the CLI has no credentials. Cache fields are parsed but not claimed
  live until authentication exists.

### Doubao / ArkCLI

- CLI: `arkcli`, installed and authenticated by the operator.
- Native mode: `arkcli +chat --format json --no-progress -- <prompt>`.
- Adapter: `src/llms/native_cli/doubao.py` (`DoubaoArkCliAdapter`) parses the
  JSON response, its final content and usage metadata.
- Session continuity is opt-in through `--store` and
  `--previous-response-id <id>`.
- Status: native registered and in the development-Worker roster. The adapter
  is text-only and passes no `--tools`; this supports read-only review, but no
  workspace-write capability is claimed.

Evidence for this pass is recorded in:

- internal research evidence (not published)

### Replication boundary

These are adapters + fake-CLI tests only; their binaries and provider
credentials are operator-installed and never committed. A provider moves to
“live verified” only after a real call with its own evidence line.

## Remaining providers

Vendors without an installed official CLI or without a stable machine-readable
mode on this host (Baidu Comate, Tencent CodeBuddy, StepFun desktop, etc.)
remain planned. The replication protocol is unchanged: install the CLI, record
its machine mode and cache/session fields, add a fake-CLI test, run one live
task, then merge. Do not advertise a provider as cache-verified when its CLI
exposes no cache counters (Kimi is such a case; it is still supported for
native session continuity, but not claimed as cache-measured).


See [NATIVE-HITL-HANDOFF](NATIVE-HITL-HANDOFF.md) for the Codex app-server per-command HITL evidence and blockers.


MiniMax needs `mcode login` before ACP; its `permissionMode=default` maps tool permission requests to the native HITL interrupt.


DSH permission live proof (2026-10-29): outside-workspace write -> ACP `session/request_permission` -> approve -> file created -> `turn_completed`; ACP memory/skills bridge injected for DSH/MiMo/MiniMax/Kimi/Grok (MiMo `conductor_memory_remember` live).


### StepFun Step Code

- CLI: `stepfun 0.3.6` (`npm i -g @stepfun-ai/cli`; the package exposes
  `stepfun`, so create a `step` symlink or set `NATIVE_CLI_STEPFUN_BIN` to the
  `stepfun` binary).
- Native mode: `step` RPC transport with approvals; set
  `NATIVE_CLI_STEPFUN_APPROVALS=1`.
- Authentication: StepFun account/API key; without it the CLI starts but turns
  fail.


### Ticket instruction isolation

Native Ticket launches add a fixed delivery contract before the Ticket prompt.
For writable Git work it requires the Ticket's named branch, or the Ticket ID's
default branch, and a commit. It also requires one complete final report. The contract says operator-global
memory and notification instructions do not apply. The Worker is asked to read
repository `AGENTS.md` files explicitly.

| CLI observed on this machine | Ticket launch flags | Limits |
|---|---|---|
| Codex CLI 0.162.1, `exec` | `codex exec --ignore-user-config --config project_doc_max_bytes=0` | `exec --help` says authentication still uses `CODEX_HOME`. Ticket execution reads only `cli_auth_credentials_store` from the effective child config path and passes back a supported enum (`file`, `keyring`, `auto`, or `ephemeral`) as a CLI override. Fake-config tests cover all four values without reading credential contents. This host reports ChatGPT login and has no explicit store setting. No real isolated login was attempted. `project_doc_max_bytes=0` disables automatic AGENTS.md loading, including repository instructions; there is no selective global-only setting in the inspected config reference. The prompt therefore asks the Worker to read repository `AGENTS.md` explicitly. |
| Codex CLI 0.162.1, `app-server` | `codex -c project_doc_max_bytes=0 app-server --listen stdio://` | `app-server --help` accepts `-c`; it rejects `--ignore-user-config`. The config override leaves the user's authentication config and approval transport in place while disabling automatic AGENTS.md loading. Ticket launches also pass a fixed developer-instruction override in `thread/start` or `thread/resume`. The regular app-server approval flow remains enabled. The prompt asks the Worker to read repository instructions explicitly. The effect of a user-configured `model_instructions_file` was not established. |
| Claude Code 2.1.309 | `claude --safe-mode` | Help describes safe mode as disabling customizations while retaining authentication. This suppresses project customizations too, so the prompt asks the Worker to read repository `AGENTS.md` explicitly. |

The installed CLIs' `--version` and `--help` output were read locally. The exact
Codex `exec`, Codex `app-server`, and Claude flag combinations parsed in those
versions. `codex login status` reported the existing ChatGPT login without
reading credentials. No model call was made. This verifies argument support,
not live model behavior. The Docker image's pinned Codex CLI 0.166.0 has not
been checked for `--ignore-user-config` support.

The other installed CLI help did not establish a global-instruction isolation
switch. Kimi 2.1.1 exposes `--skills-dir` and `--agent-file`, Grok 1.0.77 has
system-prompt and rules options, Cursor 2026.10.22-27dc5f5 and DSH 0.2.1-rc.2
showed no verified isolation option, and mcode 0.7.0 can replace its system
prompt but did not document disabling AGENTS discovery. StepFun 0.1.7 and
ArkCLI help could not be verified because each attempted a local state write
that the sandbox denied. Their isolation support remains unknown. No real
model/API calls or login changes were made. Do not claim these CLIs ignore
global instructions until their installed help or primary documentation
proves a supported flag and its authentication effect.

### Git delivery completion guard

A writable Ticket completes only when the Git workspace has no new or changed
uncommitted files since its launch baseline. Uncommitted changes move a ready
report to `needs_action` with reason `uncommitted_changes`; an unsafe or failed
Git check uses `git_status_unavailable`. Read-only and speculative runs skip
the guard. The report body contains redacted `git status --short` and
`git diff --stat` summaries. Pre-existing dirt on unrelated files does not
block a new Ticket. Native and Docker Worker retries retain that Ticket's
first baseline in its `mandate` record. The local daemon keeps its baseline for
retries while its process remains alive; a daemon restart loses that in-memory
baseline. Legacy runs without a baseline keep their prior completion behavior.

Baseline and completion Git probes run inside the native Worker isolation
boundary with read-only access and network disabled. Docker probes use
`docker exec` in the same Worker container, before CLI launch and before
container teardown. Both paths use the hardened Git environment and have no
host fallback. The native server holds a cross-process lock for the canonical
workspace from the baseline probe until the Worker child has stopped and the
completion result is saved. Read-only and speculative runs skip the lock. If a
native lock times out, the run still launches, but its Git result is unknown.

The local daemon takes the same workspace lock only around its baseline and
completion probes. It releases the lock while the CLI runs, so Workers sharing
a workspace may execute concurrently. Probe contention records unknown Git
status and does not count as a Worker or circuit-breaker failure. Docker uses
its private stage and checks Git only inside that container; it does not hold a
workspace-wide lock.
