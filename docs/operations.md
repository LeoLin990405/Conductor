# Operations reference

This file collects the operational detail kept out of the root README: the `conductor` CLI, native
CLI adapters, local daemons, live Worker output, the Cloud Worker, sandbox and isolation rules,
deploy/rollback, configuration, verification and the module documentation index. It describes the
tree as committed and says where a statement was read from code and not run; where a path referenced
by CI or Compose is absent, that is called out rather than assumed.

## CLI reference

`conductor` (installed by `uv sync`, `conductor --version` prints `conductor 0.1.0`) is a REST client
for a running Conductor server. It defaults to `http://127.0.0.1:8000`; override with `--url` or
`CONDUCTOR_URL`. A `--url` outside loopback must be HTTPS. `conductor auth set` asks for a Bearer
token with hidden input (an interactive terminal is required), writes it to
`~/.config/conductor/token` with mode `0600` and refuses to overwrite an existing file;
`conductor auth status` only says whether one is configured. Global `--json` goes before the
subcommand, for example `conductor --json tickets list`. Bare `conductor` opens an interactive
Ticket REPL.

| Command | What it does |
|---|---|
| `health`, `status` | Read `/health` and `/api/v1/system/readiness` |
| `tickets list\|show\|create\|confirm\|reply\|cancel\|switch\|lines` | List (with `--status`, `--attention`, `--failed`, `--limit`), inspect, create or act on a Ticket; `lines` groups Tickets by branch or worktree |
| `sources list\|add\|sync\|enable\|disable` | Manage GitHub and Feishu sources; `add --kind github\|feishu --config '<json>'`. Cron schedules are not a `sources` subcommand; they are created on the web Sources page |
| `daemons list\|register\|start\|stop\|status` | Manage local worker daemons; `register --provider ... --max-concurrency N`; `status` reads the local provider breaker and does not contact the server |
| `agents list`, `agents fleet` | List agents; scan the local agent fleet read-only (`--watch`, `--filter KEY=VALUE`, `--sort`) |
| `models list\|quota`, `deliberation show <id>` | List configured models; show live CodexBar quota; show one deliberation by its own ID (there is no Ticket-ID lookup) |
| `dispatch start "<goal>"`, `dispatch active\|status\|approve\|reject\|reassign` | Run a goal as parallel auto-dispatched work lines (see below) |
| `lines list\|show\|logs` | Worktrees and branches under `~/conductor-wt`: model and state |
| `review run\|status\|verdict`, `merge <line> --into <branch>` | Cross-review queue (self-review refused) and the server-run rehearsal merge; see [`REVIEW-MERGE-API.md`](REVIEW-MERGE-API.md) |
| `memory export\|recall\|propose\|approve\|reject` | Export and search the caller's memory; stage, approve or reject a proposal |
| `skills`, `skills-hub list\|install` | Manage the central skill repository, per-agent skill directories and bundled skills |
| `github auth\|status` | Store a GitHub token in the Conductor vault; status reads the vault and does not call GitHub |
| `deploy`, `rollback` | Local checkout deploy and database rollback, outside the REST API |
| `logs tail`, `usage`, `impact`, `browser`, `web`, `install-shim`, `completion`, `version` | Local log tail, provider usage, change impact, isolated agent browser, shell shim and completion |

Commands that never open an API client are `auth`, `install-shim`, `logs`, `deploy`, `rollback`,
`usage`, `skills`, `skills-hub`, `browser`, `web`, `lines`, `impact`, `version`, `completion`,
`daemons status` and `models quota` (`_LOCAL_COMMANDS` and `_is_local` in
`src/conductor_cli/cli.py`). Local is not offline: `deploy` reads a running backend's dispatch state,
and `usage` calls `codexbar` and, when `DEEPSEEK_API_KEY` is in `~/.config/deepseek/env`, DeepSeek's
balance endpoint; only `usage --no-refresh` reads the saved snapshot without touching either.

Ticket creation accepts `--workspace`, `--prompt`, `--title`, `--provider`, `--model`, `--daemon`,
`--cwd` and `--worktree-root` (default `~/conductor-wt`). `tickets switch` keeps the thread and
worktree, never moves a Ticket onto its own reviewer and, for a daemon-bound Ticket, only switches
within its provider; it is manual, nothing detects a stuck Ticket and calls it.

**Auto-dispatch.** `CONDUCTOR_DISPATCH_ADAPTERS` defaults to `dev`: fixed lines, no Worker started,
review always passes, no merge. `live` needs the `claude` CLI on the server host (the
default planner is `CONDUCTOR_DISPATCH_PLANNER=claude-code`; set it to `llm` to opt out)
and `CONDUCTOR_DISPATCH_REPO`, which defaults to the server's working directory. It also
needs the launch script (`CONDUCTOR_DISPATCH_LAUNCH_SCRIPT`, default `~/conductor-wt/launch.sh`).
It merges only into
`integrate/auto-dispatch-<id>` after `conductor dispatch approve <id>` and never pushes. The pending
summary lands in `$CONDUCTOR_HOME/proposed/auto-dispatch/`, or
`${XDG_STATE_HOME:-~/.local/state}/conductor/proposed/auto-dispatch/` when `CONDUCTOR_HOME`
is unset (override: `CONDUCTOR_DISPATCH_PROPOSED_DIR`). The live selector is quota- and success-aware:
`dispatch.default_headroom()` supplies headroom: `MostConstrainedHeadroom` over `CodexBarHeadroom`
(the CodexBar snapshot file) and `SnapshotOrUnlimitedHeadroom` (through `UsageSnapshotHeadroom`, which
reads `src/server/services/native_runtime/usage_headroom.load_usage_headroom`), the more exhausted reading
winning. The planner's DAG assignment and the selector share that port, and a large task ranks by
remaining headroom. `MemoryP0OutcomeStore(DispatchRowMemoryPort(...))` supplies success rates recorded in
existing dispatch rows; a missing or stale snapshot makes every model unlimited. Which models are eligible comes from the
`RolePolicy` roster: planner `claude`, primary reviewer `codex`, developer pool
`kimi, glm, grok, cursor, stepfun, deepseek`, work tail `minimax, doubao`; `CONDUCTOR_ROLE_POLICY_FILE`
replaces the built-in roster and a file that cannot be read or is invalid fails when a `DispatchService` is
constructed, instead of falling back to the built-in roster. Work never takes a reserved role
unless the policy sets `allow_reserved_code`, and that choice is audited on the line. A live review
builds its reviewer only through `get_read_only_adapter`, which refuses a CLI without a mode it enforces
itself; review tries the primary reviewer `codex` first, then a read-only-capable secondary — `stepfun`
in the shipped roster, because `grok` and `cursor` have no read-only mode yet. `kimi`, `deepseek`, `glm`
and `minimax` are never picked for the review role (`REVIEW_INELIGIBLE` in
`src/tools/auto_dispatch/pool.py` gives each reason), and when no secondary remains the refusal reason
is `no_secondary_reviewer`. The reviewer's prompt is the goal, the line and the
branch diff, at most 96 KiB in UTF-8 bytes; it does not include the worker's output, because the output tail
(`Line.log_tail`) is raw and unredacted, and it tells the reviewer so in one line. A reviewer that cannot be built or run, or a diff that cannot be
read, ends the dispatch in `ERROR` with a fixed sentence and no other model is tried. The planner refuses
a task over 8,000 UTF-8 bytes (a name over 256, a lone surrogate or a NUL byte) as a plan failure, and the
launcher refuses an argument no process can start before it spawns anything; the details are in
[`ports/auto-dispatch.md`](ports/auto-dispatch.md) and [`NATIVE-CLI-AGENTS.md`](NATIVE-CLI-AGENTS.md).
`approve`, `reject` and `reassign` have no local fallback; start, `status <id>` and `active` fall
back to a `fake: true` record under `~/.config/conductor/dispatches/` (`CONDUCTOR_DISPATCH_HOME`)
when the server answers 404, 405 or 501.

GitHub access expects a fine-grained personal access token scoped to the selected repositories, with
**Issues: Read and write**, **Pull requests: Read and write**, **Contents: Read-only** and
**Metadata: Read-only**. That list was not checked against GitHub's permission matrix: the Source
adapter only calls `GET /user`, `GET /repos/{repo}`, the repository's issue-comment and
pull-request-review-comment lists, and issue-comment `GET` and `POST`
(`src/server/services/autonomy/github.py`), and nothing in it reads repository contents. Each
credential is bound to its host, method and path prefix and checked before the vault is read.

**Running on your own token.** By default a GitHub Source ignores comments written by the token's own login, so it needs a separate bot account. To run it on your own token instead (for example `gh auth token`, stored with `conductor github auth`), set `trigger_phrase` in the Source config, for example `"/conductor"`. A comment containing that phrase as a whole token addresses the agent. The token's own comments are read, but the replies Conductor posted (they carry a `<!-- conductor-autonomy:… -->` marker) are skipped. The phrase must not start with `@`, because a mention would notify whoever owns that GitHub name. Add `"require_approval": true` to hold every GitHub reply in Conductor for a person, instead of posting it.

**Interactive CLI.** `uv sync --extra cli` installs the TUI from `libs/ptc-cli`. `ptc-agent --server
<url> --user-id <id> --super-agent` sends only an `X-User-Id` header, so it is meant for the default
`HOST_MODE=oss`; plain `ptc-agent` is the older chat loop. `cond start|stop|status` starts
the backend, the web console, and every daemon in `~/.conductor/daemons.toml`, and bare `cond`
then opens the TUI. The checkout is the repository that contains `scripts/cond`; set
`CONDUCTOR_REPO_ROOT` to run a different one. A daemon other than Codex is another `[[daemon]]`
block in that file (`provider` and `id`). With the backend already running,
`cond foreground daemon --provider <provider> --id <id>` runs one of them in
the foreground. See [`COND.md`](COND.md) and [`CLI-SUPERAGENT.md`](CLI-SUPERAGENT.md).

## Native CLI adapters

Thirteen adapter classes are registered under seventeen provider keys in
[`../src/llms/native_cli/registry.py`](../src/llms/native_cli/registry.py). Eleven of them are also
registered as native Workers in
[`../src/server/services/native_runtime/registry.py`](../src/server/services/native_runtime/registry.py),
which holds the capability table (session resume and cache-usage reporting differ per CLI); the
Doubao/ARK and MiniMax mmx adapters are text-only and are not native Workers. Adapters that support ACP
can be enabled through `NATIVE_CLI_ACP` (a comma-separated list of provider names); the others run
the vendor CLI in its JSON/exec mode.

| Provider key | Adapter class | Native Worker | Binary override |
|---|---|---|---|
| `deepseek-dsh` | `DeepSeekDshAdapter` | yes | `NATIVE_CLI_DSH_BIN` |
| `codex` | `CodexExecAdapter` | yes | `NATIVE_CLI_CODEX_BIN` |
| `claude-code` | `ClaudeCodeAdapter` | yes | `NATIVE_CLI_CLAUDE_BIN` |
| `cursor` | `CursorAgentAdapter` | yes | `NATIVE_CLI_CURSOR_BIN` |
| `kimi` | `KimiCliAdapter` | yes | `NATIVE_CLI_KIMI_BIN` |
| `grok` | `GrokCliAdapter` | yes | `NATIVE_CLI_GROK_BIN` |
| `stepfun` (adapter key `step` is an alias with no runtime of its own) | `StepFunAdapter` | yes (as `stepfun`) | `NATIVE_CLI_STEPFUN_BIN` |
| `minimax` | `MiniMaxCodeAdapter` | yes | `NATIVE_CLI_MINIMAX_BIN` |
| `minimax-mmx`, `mmx` | `MiniMaxMmxAdapter` | no (text) | `NATIVE_CLI_MMX_BIN` |
| `mimo` | `MiMoCodeAdapter` | yes | `NATIVE_CLI_MIMO_BIN` |
| `zcode` | `ZCodeCliAdapter` | yes | `NATIVE_CLI_ZCODE_BIN` |
| `glm`, `zhipu` | `ZhipuCliAdapter` | yes | `NATIVE_CLI_ZHIPU_BIN` |
| `doubao`, `arkcli` | `DoubaoArkCliAdapter` | no (text) | `NATIVE_CLI_DOUBAO_BIN` |

`NATIVE_CLI_ENABLED=1` turns on the `NativeAgents` delegation tool for PTC threads; no other code in
`src/` or `scripts/` reads it. Each adapter finds its
CLI on `PATH` or through the override above; the CLI keeps its own login state and session cache.
Gemini and Qwen remain PTC-only and are not registered as native runtimes. The per-adapter details
are in [`../src/llms/native_cli/`](../src/llms/native_cli/) and [`NATIVE-RUNTIME.md`](NATIVE-RUNTIME.md).

Worker concurrency has three independent limits:

- `AUTONOMY_MAX_RUNNING_WORKERS` (default 4; a non-integer falls back to 4 and a value below 1
  counts as 1) caps the Tickets in `running` across all Workers, daemon-bound and cloud together. A
  local daemon's claim and, when the Cloud Worker runtime is on, a Cloud Worker launch are counted under
  one Postgres advisory lock in the step that makes the Ticket running. A launch that finds no free
  slot leaves its Ticket queued and the newest live launch effect of that Ticket goes back to pending, due in
  10 seconds with its attempt given back, instead of failing (an older duplicate launch effect of the same
  Ticket returns and ends). A follow-up delivered to a Ticket sets it running without
  looking at the cap, so the running count can stand above it
  ([`CLOUD-WORKERS.md`](CLOUD-WORKERS.md), "Concurrency").
  `GET /api/v1/autonomy/metrics` returns the current value as `max_workers`.
- A daemon's `max_concurrency` is 1–32 and defaults to 1, set at registration (and updatable
  through the API); the daemon process's own `--max-concurrency` has the same range and default, and
  `conductor daemons start` passes it the registered value. A daemon claims no more than the smaller
  of its registered limit and the global cap.
- `NATIVE_CLI_MAX_PARALLEL` (default 4) caps parallel native tasks inside one `NativeAgents` call; it
  can only lower the built-in limit of four, never raise it.

## Local daemons

`conductor daemons register --provider codex` creates the daemon on the server and saves its token at
`~/.config/conductor/daemons/<daemon_id>.token`. `conductor daemons start <daemon_id>` (or
`scripts/conductor_local_daemon.py` directly) pulls and runs Tickets on this host; a Ticket can be
bound to one daemon.

- The daemon refuses to start without an isolation root (`--isolation-root` or
  `CONDUCTOR_ISOLATION_ROOT`), and a Ticket whose workspace has no entry in the workspace policy
  (`--workspace-policy` or `CONDUCTOR_WORKSPACE_POLICY`) fails closed. `conductor daemons start` has
  neither flag; it passes its environment to the script, so set both variables first. The policy
  format is in [`ports/worker-isolation.md`](ports/worker-isolation.md).
- `conductor daemons start` also takes `--policy` (device policy, default
  `~/.config/conductor/daemon_policy.yaml` when present) and `--trusted-keys` (server public keys the
  device trusts, default `~/.config/conductor/daemon_trusted_keys.json` when present; with it set, a
  signed grant must name this daemon). Without them the daemon logs a warning and, respectively,
  runs every operation it is sent and does not verify that a lease was signed by the server. The
  script itself adds `--require-policy`, `--require-origin` (refuse to start when the file is
  missing) and `--nonce-file` (replay record for signed grants). Examples:
  [`../config/daemon_policy.example.yaml`](../config/daemon_policy.example.yaml) and
  [`../config/daemon_trusted_keys.example.json`](../config/daemon_trusted_keys.example.json); see also
  [`SUPER-AGENT-BYOM.md`](SUPER-AGENT-BYOM.md). This was read from `--help` and the code; no daemon
  was started for this document.

## Live Worker output

While a local daemon's Codex CLI runs, the daemon posts its visible text and tool names in batches
to `POST /api/v1/autonomy/daemon/tickets/{id}/output` (daemon token and a live lease are checked).
`GET /api/v1/autonomy/tickets/{id}/activity/stream` is an SSE stream of the activity rows plus that
output, with resumable ids for `Last-Event-ID`. The web task page polls `.../activity` and does not
read the stream; the CLI and TUI do.

- Needs migration 140 or later on the server (the current head is in [`../MIGRATIONS.md`](../MIGRATIONS.md)). `CONDUCTOR_DAEMON_TOKEN_STREAM=0` on the daemon turns it off;
  an older server without the endpoint makes the daemon stop streaming at its first HTTP error.
  Streaming is best effort and never fails the Worker.
- Only the Codex provider streams, and only the daemon's Codex loop is guarded. Every other provider
  keeps the report and activity path. Tool arguments, tool results and prompts stay local.
- `src/server/utils/output_guard.py` is one scanner used by the daemon and the server; a hit ends the
  live text of that run (the Worker and its final report continue). It is a heuristic over known
  credential shapes and cannot recognise a secret of no known shape.

The protocol, limits, retention and the "does not catch" list are in [`SUPER-AGENT.md`](SUPER-AGENT.md).

## Cloud Worker

An autonomous Ticket with no `worker_daemon_id` is a cloud Worker. By default it fails with
`_CLOUD_WORKER_REJECTION_ERROR` (`src/server/services/autonomy/worker.py`): "Cloud Worker execution is
disabled until this workspace has an OS-isolated worker runtime. Assign the Ticket to a configured
local daemon." The switch is `AUTONOMY_CLOUD_WORKER_RUNTIME` (or `autonomy_cloud_worker_runtime` in
`config.yaml`); unset, empty, `none`, `off` and `disabled` mean off, `docker` enables the Docker
runtime, and a failed verification, an unavailable engine or image, a launch error or a timeout
settles the Ticket failed with no fallback to the shared runtime.

Each launch runs one Codex CLI one-shot in a fresh disposable container over a private staging copy
of the workspace. Defaults: image `conductor-cloud-worker:latest`, provider `codex` (the only value),
network `none` (`bridge` only through `AUTONOMY_CLOUD_WORKER_NETWORK`), timeout 3600 s, memory 2g,
1.5 CPUs, 512 pids. It was run against a real Docker engine with a stub Worker and, for verification
and a credential-less launch, a real Codex image; no real vendor call has been made through it, and
verification checks a trusted image's configuration, so an image that lies on purpose passes. Setup,
the full settings table, the refusal matrix and the limits are in [`CLOUD-WORKERS.md`](CLOUD-WORKERS.md).

A launch counts against `AUTONOMY_MAX_RUNNING_WORKERS` (see Worker concurrency above) and runs inside
the autonomy effect runner, which has five places in the one process that holds the autonomy pass lock
and leases an effect only when it has a free place for it. A launch keeps its place for as long as it
runs: `AUTONOMY_CLOUD_WORKER_TIMEOUT` bounds only the container command, not preparation, cleanup,
publication and settlement, so a place has no end-to-end deadline. Five long Workers therefore hold
every place and every other effect (replies, report relays, input deliveries, Router turns, a waiting
Ticket's next look at the cap) waits pending until one ends, and a cap above 5 does not allow more than
5 cloud Workers in a process. This is read from the code; `CLOUD-WORKERS.md` records that the cap was
exercised on a throwaway PostgreSQL 16 with the container runtime faked, not with several real containers.

## Sandbox and isolation

PTC execution uses one of four providers:

| Provider | Selection | Notes |
|---|---|---|
| Docker | `SANDBOX_PROVIDER=docker` | Local containers; needs the Docker socket; image `conductor-sandbox:latest` (built by `make up PROVIDER=docker`, or by the backend on first workspace creation) |
| Daytona | `SANDBOX_PROVIDER=daytona` with `DAYTONA_API_KEY` | Cloud sandboxes; snapshots can be prebuilt with `make daytona-snapshots` |
| E2B | `SANDBOX_PROVIDER=e2b` with `E2B_API_KEY` | Cloud sandboxes; templates with `make e2b-templates` |
| SSH | Offered through `sandbox.offered_providers` in `agent_config.yaml` | A computer the user brings over SSH; the agent runs as the login user under the computer's `root_dir` |

`SANDBOX_PROVIDER` always wins. The committed `agent_config.yaml` sets `sandbox.provider: "docker"`, so
that is the default here whatever keys are set; only a config that names no provider falls back to
reading keys (Daytona if `DAYTONA_API_KEY` is set, else E2B if `E2B_API_KEY` is set, else Docker)
(`create_sandbox_config`, `detect_provider_kind`). `make up PROVIDER=...` sets `SANDBOX_PROVIDER` for
Compose.

Local autonomous native workers go through an OS boundary in
[`../src/llms/native_cli/isolation.py`](../src/llms/native_cli/isolation.py). Linux uses `bwrap`,
macOS uses `sandbox-exec`. The policy is per workspace, owned by the daemon user, mode `0600` or
stricter, and stored outside workspace roots. Missing or unsafe policy, unsafe root overlap or an
unsupported sandbox fails closed.

The boundary is not a tenant boundary:

- Sandboxes on one computer share the OS user, sibling files and MCP credentials.
- macOS denies host unix sockets and loopback TCP (Postgres, Redis, the backend and the Docker API
  among them) except the ports the workspace policy lists in `localhost_tcp_ports`; Linux does not
  pass `--unshare-net`, so loopback and abstract-namespace unix sockets stay reachable and
  `localhost_tcp_ports` has no effect there.
- A kernel exploit or the host administrator is outside the threat model.
- A cloud autonomous Worker has no such boundary on the shared PTC/native runtime, so it is refused
  unless the Docker Cloud Worker runtime above is enabled and verifies.

Details: [`ports/worker-isolation.md`](ports/worker-isolation.md), [`ports/sa-isolation.md`](ports/sa-isolation.md),
[`ports/agent-fleet.md`](ports/agent-fleet.md) and [`ports/agent-fleet-alt.md`](ports/agent-fleet-alt.md).
The agent fleet view (`GET /api/v1/agents/fleet`, `conductor agents fleet`) is read-only, describes
only the host running the backend and rejects non-loopback callers; its root defaults to
`~/conductor-wt` and `AGENT_FLEET_ROOT`, `AGENT_STALL_MINS` and `AGENT_FLEET_BASE_REF` override
the settings in `config.yaml`.

## Deploy and rollback

`conductor deploy` operates on a Git checkout, the configured PostgreSQL database and a backend on
port 8000; it does not go through the REST API.

```sh
conductor deploy --repo ~/conductor-wt/<checkout> --dry-run
conductor deploy --repo ~/conductor-wt/<checkout>

conductor rollback --repo ~/conductor-wt/<checkout> \
  --commit <sha> --backup ~/conductor-wt/backups/<backup>.dump --dry-run
conductor rollback --repo ~/conductor-wt/<checkout> \
  --commit <sha> --backup ~/conductor-wt/backups/<backup>.dump
```

`--repo` also comes from `CONDUCTOR_REPO`; the default is the current Git checkout (it must contain
`server.py` and `alembic.ini`). Deploy preflights a clean working tree on a named branch with an
upstream, `.venv/bin/python` and `.venv/bin/alembic`, `git`, `pg_dump`, `pg_restore`, `pkill`, `pgrep`
and `nohup` on `PATH`, the database settings, and that no auto-dispatch is waiting at `APPROVAL`; it
then writes a custom-format database backup under `~/conductor-wt/backups` (directory `0700`, file
`0600`, checked with `pg_restore --list`), runs `git pull --ff-only`, `.venv/bin/alembic upgrade
head`, restarts the backend and waits up to 30 s for `/health`. Dry-run creates no files and runs no
deploy command. `conductor deploy status` shows the last deploy
(`~/.config/conductor/last-deploy.json`, `CONDUCTOR_LAST_DEPLOY`), the live commit, health and the
latest backup.

Limits, from the code and not run against such a backend: the dispatch check is a plain HTTP `GET` of
`/api/v1/dispatch/status/active` on `CONDUCTOR_URL` or `http://127.0.0.1:8000` with no `Authorization`
header, and an unreadable answer blocks the deploy, so a backend with `HOST_MODE` other than `oss`
will block it. The restart stops the backend by matching the command line `server.py --port 8000`
(waiting up to 10 s) and starts `nohup .venv/bin/python server.py --port 8000 --log-level warning`
with output in `/tmp/conductor-backend.log`, so the backend must run in that form.

Rollback validates the backup (`pg_restore --list`) and the commit, requires the operator to type
`ROLLBACK`, stops the backend and waits for exit, runs `git reset --hard`, restores the database with
`pg_restore --clean --if-exists --single-transaction`, starts the backend and checks health. With
`--dry-run` and an existing backup file the preflight always lists "Backup archive contents are not
validated during dry-run", so `ready` stays false; that is expected, not a sign a real run would be
refused. Rollback restores the dump; it does not run `alembic downgrade`. A hand-run `alembic
downgrade` to a target of 115 or higher is one transaction for the range in
[`../MIGRATIONS.md`](../MIGRATIONS.md), so one that fails leaves the database at the revision it
started from; below that several downgrades commit part-way and a failure after one leaves the
revision of the last committed boundary. Stop the backend first and read that file before
downgrading.

The homelab deployment in the maintainer's homelab deployment (not published) is a separate rootless-Docker
setup: the backend has no TCP listener, web binds host loopback only, sandboxes have no host ports,
and an SSH tunnel is the intended access path. Its trust boundaries and first-run procedure are in
the maintainer's homelab deployment (not published).

## Configuration

| File | Purpose |
|---|---|
| `.env` | Credentials, URLs, provider keys and deployment switches |
| `config.yaml` | Infrastructure: Redis, logging, CORS, background execution, the Super-agent Router, native runtime routing, the agent fleet, the Cloud Worker switch and the memory-federation user bindings (`memory_federation.user_bindings`, empty by default with no implicit host path; see [`ports/memfw-v1-integration.md`](ports/memfw-v1-integration.md)) |
| `agent_config.yaml` | Agent behaviour: LLM defaults, sandbox provider, MCP servers, tools, prompts and subagents. `mcp.servers` there is the operator's own list; the committed file at the repository root also ships one default read-only `memfw-notes` stdio entry (`scripts/memfw_notes_mcp.py`, env `FLEET_MEMORY_HOST`, `MEMFW_CLI`, `MEMFW_RELATED_DB`, `MEMFW_READONLY_URL`, `MEMFW_READONLY_TOKEN_FILE`), so a fresh checkout declares that server in its MCP list until the entry is removed; an `APP_ENV` overlay (`agent_config.<env>.yaml`) merges into it |
| `config/llm.local.json` | Gitignored local provider/model/pricing overrides (copy the example) |
| `pyproject.toml` | Python floor, dependencies, extras, Ruff and pytest configuration |

`APP_ENV` layers a sibling overlay over each YAML file: `APP_ENV=production` reads
`agent_config.production.yaml` and `config.production.yaml`; nested maps merge and lists replace.
Overlays are gitignored, so the committed YAML stays the documented default.

### Retired direct chat

`POST /api/v1/threads/{id}/messages` serves the Super-agent only. A thread whose stored `platform`
is `superagent_router` (the Router), `superagent_worker` (an implementation Worker) or
`superagent_watchdog` (a Worker-class review lane), and whose turn arrives as an authenticated
`X-Dispatch: background` request, runs; every other thread — a person's chat, a legacy automation or
a channel thread — answers `410` with `detail.code = "chat_retired"` and a migration hint. Create a
Ticket (`conductor tickets create` or the Super-agent composer) instead of sending the turn directly.

The runtime's environment variables were renamed from `PTC_*` to `CONDUCTOR_*`. The new name wins
when both are set, and the old one is still read for one release: `PTC_CONFIG_FILE` →
`CONDUCTOR_CONFIG_FILE`, `PTC_TURN_CWD` → `CONDUCTOR_TURN_CWD`, `PTC_WORKSPACE_CONFIG` →
`CONDUCTOR_WORKSPACE_CONFIG`, `PTC_ALLOWED_SERVERS` → `CONDUCTOR_ALLOWED_SERVERS`,
`PTC_SERVER_URL` → `CONDUCTOR_SERVER_URL`, `PTC_CLI_LOG_FILE` → `CONDUCTOR_CLI_LOG_FILE`,
`PTC_PALETTE` → `CONDUCTOR_PALETTE` and `PTC_THEME` → `CONDUCTOR_THEME`.

`make config` is the interactive wizard (it creates `.env` from `.env.example` when missing and
configures `.env` and `agent_config.yaml`). `make config-check` validates `.env`, both YAML files and
the model manifests without starting services. `make doctor` checks Docker, PostgreSQL, Redis, ports,
backend health, LLM providers, native CLI providers and MCP servers; it is safe to run before
`make install` because it uses only the standard library.

The Super-agent Router is pinned in `config.yaml` to `autonomy_router_model: deepseek-flash-account`,
the DeepSeek account route: Router turns need a connected DeepSeek account, and `null` or an
API-key model such as `deepseek-flash` lifts that. Account login is `POST
/api/v1/oauth/deepseek/initiate` and `/callback`, with `GET /status`, `GET /models` and `DELETE`
under the same prefix; the steps, storage and failure modes are in
the internal document `DEEPSEEK-ADAPTATION` (not published). Router snapshots and metrics are off
(`autonomy_router_snapshot_enabled: false`, or `AUTONOMY_ROUTER_SNAPSHOT_ENABLED`).

Local model overrides belong in `config/llm.local.json` (gitignored). Copy
[`../config/llm.local.example.json`](../config/llm.local.example.json) and restart the backend after
editing.

## Verification

CI is defined in [`.github/workflows/test.yml`](../.github/workflows/test.yml):

| Job | Coverage |
|---|---|
| `backend-unit` | `pytest tests/unit/` with Python 3.13 |
| `backend-integration` | `tests/integration/` against PostgreSQL 18 and Redis 7 (`environment: integration`, with a Daytona provider key from secrets) |
| `hotpath-comment` | Posts a metrics comment after the integration job |
| `frontend-unit` | Typecheck, ESLint, Vitest, a production Vite build and a critical-path check (Node 24) |
| `desktop-unit` | Electron shell tests with Node 24 |
| `frontend-e2e` | Playwright against the official browser image |

Separate workflows build sandbox images, run sandbox integration tiers, and build desktop releases. A workflow file is a definition, not a result;
`tests/unit/deploy/test_build_paths.py` checks only that no build, Compose, workflow or script file
names the removed legacy `libs/` package and that the backend Dockerfiles' `COPY` lines are the listed
ones. The current pytest configuration (`pyproject.toml`)
collects `tests/` only, marks integration, slow, provider, regression and environment-dependent
tests, and deselects integration, slow and regression by default; `libs/ptc-cli` is its own project
with its own tests.

Local commands:

```sh
make test                          # backend unit tests
make test-web                      # Vitest
make test-desktop                  # Electron shell tests
make test-sandbox PROVIDER=memory  # sandbox integration without an external provider
make e2e-live                      # real stack end-to-end, opt-in
uv run alembic heads               # one head
```

What was run on this tree, on which commit and machine, what was not verified and the known limits
are in the internal document `RELEASE-READINESS` (not published); numbers are kept there, with their dates, and
not here. the internal document `FULL-TEST-RUN-REPORT` (not published) is an earlier host run (2026-11-02),
not a CI status.

## Module documentation index

| Document | Subject |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | layer map, orchestration, model plane, state and security boundaries |
| [`SUPER-AGENT.md`](SUPER-AGENT.md) | Super-agent flow, live Worker output stream and the output guard |
| [`CLI-SUPERAGENT.md`](CLI-SUPERAGENT.md), [`COND.md`](COND.md) | interactive CLI/TUI and the `cond` launcher |
| [`REVIEW-MERGE-API.md`](REVIEW-MERGE-API.md) | server-side review queue and rehearsal merge |
| [`CLOUD-WORKERS.md`](CLOUD-WORKERS.md) | opt-in Docker cloud Worker runtime |
| [`../MIGRATIONS.md`](../MIGRATIONS.md) | migration notes, schema head pins, downgrade limits |
| [`ports/auto-dispatch.md`](ports/auto-dispatch.md) | planner, model selection, cross-review and human merge gate |
| [`ports/ob-switch.md`](ports/ob-switch.md) | moving a stuck Ticket to another model |
| [`ports/ob-handoff.md`](ports/ob-handoff.md) | sha256-verified file handoffs between agents |
| [`ports/sa-verified-stop.md`](ports/sa-verified-stop.md) | verified process-tree stop |
| [`ports/sa-task-first.md`](ports/sa-task-first.md) | task-first Ticket closure |
| [`ports/beyond-cred-scope.md`](ports/beyond-cred-scope.md) | per-credential destination allowlist |
| [`ports/sa-router-shard.md`](ports/sa-router-shard.md) | router sharding and peer channel |
| [`ports/beyond-router-plan.md`](ports/beyond-router-plan.md) | router snapshot and decision metrics |
| [`ports/sa-lifecycle.md`](ports/sa-lifecycle.md) | subscription cancellation and reassignment |
| [`ports/skills-hub.md`](ports/skills-hub.md) | central skill repository, per-agent installs, rollback |
| [`ports/worker-isolation.md`](ports/worker-isolation.md) | per-workspace OS isolation for autonomous workers |
| [`ports/sa-isolation.md`](ports/sa-isolation.md) | isolation rollout notes and cloud-worker fence |
| [`ports/agent-fleet.md`](ports/agent-fleet.md) | read-only local agent fleet dashboard |
| [`ports/agent-fleet-alt.md`](ports/agent-fleet-alt.md) | alternate fleet scan design |
| [`CODEXBAR-BRIDGE.md`](CODEXBAR-BRIDGE.md) | provider headroom from CodexBar |
| [`CHANGELOG.md`](CHANGELOG.md) | dated behaviour changes and migration notes |
| [`ports/`](ports/) | full port and merge-note index |

## Snapshot notes

- No image was built for this document. The legacy `libs/` package the project dropped is no longer
  named by `docker-compose.yml`, `deploy/`, the workflows, the `Makefile` or `scripts/`
  (`tests/unit/deploy/test_build_paths.py` searches them for it), and the workflow that existed only for
  it is gone. The same test pins the `COPY` lines of `deploy/Dockerfile.dev` and
  `deploy/Dockerfile.backend` as an exact list whose sources exist. Both copy `skills/agent-browser/`
  before `uv sync`, because the root project's editable build force-includes it. Evidence and its limits are in
  the internal document `RELEASE-READINESS` (not published).
- `plugin-catalog/marketplace.json` lists `playwright-browser` and `github-mcp`, and both packages are
  present under `plugin-catalog/plugins/`. The bundled plugin directories under `plugins/` are
  `conductor_deliverables` and `conductor_service`, and they carry skills only.
- `docs/DESIGN.md` describes a research-desk visual language; root `DESIGN.md` and
  `web/src/styles/tokens.css` describe the current operations-terminal system. The localized
  `docs/README.*` overviews were brought up to the Super-agent shell and point back to the root README.
- `.env.example` names no data-source server and no token key for one: nothing in the tree reads
  them, and the two bundles under `plugins/` carry skills only. `OPENCLI_BIN` and
  `OPENCLI_BRIDGE_TOKEN` stay (`scripts/opencli_bridge.py` reads them). `FORK-CN.md` declares the
  inherited runtime out of scope, and the built-in subagents, the `interactive-dashboard` skill and the
  stub data of `scripts/utils/render_prompt.py` still name data servers this tree does not ship.
- Nothing pins the docstrings of agent-facing MCP tools: the snapshot lock that listed 61 tools that
  no code here defines was read by no test, so it and its writer script were deleted (see `AGENTS.md`).
- `AgentRole` has no Chief of Staff and `turn_route` always returns `analyst`, but two kinds of text
  still name one: the line in `src/ptc_agent/agent/middleware/runtime_context/epoch.py` that tells the
  analyst to ask a "Chief of Staff in All workspaces" (model-visible prompt text, left as it is), and the
  Chief of Staff strings in `web/src/locales/`. Three test files under
  `tests/unit/ptc_agent/agent/prompts/` also pass `chief_of_staff` as the role and get the analyst's
  prompt.
- `tests/unit/test_docs_name_real_tests.py` checks the test files named in `AGENTS.md`,
  `CONTRIBUTING.md`, this file, `mcp_servers/AGENT_CONTRACT.md`, `src/server/AGENTS.md`,
  `src/tools/AGENTS.md` and the prompt templates' `AGENTS.md`. `web/AGENTS.md` is not checked: it names a
  `web/src/pages/ChatAgent/` tree, `agentPaths.ts` and a sandbox-layout test, none of which this tree
  has.
