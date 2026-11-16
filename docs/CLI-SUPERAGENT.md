# Super-agent interactive CLI

The interactive surface is `ptc-agent --super-agent` from `libs/ptc-cli`,
backed by the backend `cond` starts from this checkout. `CONDUCTOR_REPO_ROOT`
selects that checkout; unset, `cond` uses the repository that contains
`scripts/cond`. On an interactive terminal at least 80 columns wide it opens
the full-screen TUI; a pipe, a narrower terminal, `--no-tui` or
`CONDUCTOR_NO_TUI=1` selects the Rich line shell instead, `--exec COMMAND` runs
one command through the line shell, and `-p/--print PROMPT` runs one prompt
non-interactively. Plain text is a signal: it creates a manual Ticket on the
Home workspace. The TUI and `-p` then follow it by themselves (`-p` prints the
result and exits 0 only when the Ticket ends `completed`); the line shell
prints the Ticket id and `/jobs watch <id>` follows the event record. The
legacy chat loop remains available as plain `ptc-agent`. Keys, screen, sessions
and the permission prompt of the TUI are in
[`ports/cli-tui.md`](ports/cli-tui.md).

## Start on M5

The one-key entry point is `cond` (see `docs/COND.md`):

```bash
# <repo> is this checkout. CONDUCTOR_REPO_ROOT overrides it when cond runs.
ln -sfn /path/to/conductor/scripts/cond ~/.local/bin/cond
export CONDUCTOR_REPO_ROOT=/path/to/conductor
cond
```

It starts the backend, the web console and every daemon in the fleet file
when needed, then opens this CLI; it stops with an error when a daemon's
workspace policy file or token is missing
(see "Local execution" below). `cond status` and `cond stop` manage that
fleet, and
options after `cond` go to `ptc-agent --super-agent`: `cond -p "prompt"`,
`cond --continue` (reopen the last TUI session), `cond --no-tui`,
`cond --exec '/jobs'`. The manual steps below remain the fallback.

```bash
# backend; needs Postgres and Redis (make setup-db)
# <repo> is this checkout. CONDUCTOR_REPO_ROOT is the same path.
cd /path/to/conductor
export CONDUCTOR_REPO_ROOT="$(pwd)"
cp .env.example .env                    # then fill in credentials (or: make config); never commit it
.venv/bin/alembic upgrade head          # current head: ../MIGRATIONS.md
.venv/bin/python server.py --port 8000  # separate terminal

# CLI (same checkout; conductor-cli extra)
uv sync --frozen --extra cli
.venv/bin/ptc-agent --server http://127.0.0.1:8000 \
  --user-id local-dev-user --super-agent
```

`--server` defaults to `$PTC_SERVER_URL`, else `http://localhost:8000`. The
client sends only an `X-User-Id` header (the `--user-id` value) and no Bearer
token, so it is meant for `HOST_MODE=oss`, the default: there the server
ignores the header and serves the single local user, `AUTH_USER_ID` (default
`local-dev-user`). In `HOST_MODE=platform` the server answers 401 (read from
`get_current_user_id`; not run).

`conductor status` (`GET /api/v1/system/readiness`) reports the overall status
`ready` once every required check passes. Its `primary_model` check is
`missing` (overall `needs_action`) until the user's effective default model has
a usable credential; set the model with `PUT /api/v1/users/me/preferences`
(the `model_preference` object). The Super-agent Router's own model is pinned
separately below, so the account default continues to drive ordinary threads.

### Router model: DeepSeek-V4.1-Flash (account route)

The committed [`config.yaml`](../config.yaml) sets the deployment-wide Router
model override:

```yaml
autonomy_router_model: deepseek-flash-account
```

That id is the manifest's DeepSeek account route: display name
**DeepSeek-V4.1-Flash（账号登录）**, `model_id` `deepseek-flash`, provider
`deepseek-account`. It is the main Router model for candidate and
human-facing report turns; it does not replace the user's account default for
ordinary threads. Do not configure `step-3.7-flash` for the Router.

The account route authenticates with the user's DeepSeek platform sign-in
instead of an API key, so the Router needs that connection. The step-by-step
login, check and fallback procedure, with its failure table, is in
the internal document `DEEPSEEK-ADAPTATION` (not published) ("DeepSeek account: login,
check and fallback"); this section keeps the Router-specific points and the
curl flow.

```bash
# status probe: {"connected": true, ...} only says a token row is stored,
# not that the Platform still accepts it
curl -s http://127.0.0.1:8000/api/v1/oauth/deepseek/status \
  -H 'X-User-Id: local-dev-user'

# if not connected: POST /api/v1/oauth/deepseek/initiate -> open authorize_url
# -> POST /api/v1/oauth/deepseek/callback {code, state}
```

(The `X-User-Id` header in these examples is ignored in `HOST_MODE=oss`.)

Without a stored account token the Router's turn is refused with HTTP 400
`oauth_required` ("Model 'deepseek-flash-account' requires a connected
deepseek-account account."); the dispatcher retries that effect with backoff
and marks it dead after the sixth failure, so connect the account promptly or
use the API-key route below. A token the Platform rejects at call time (401 or
403) is handled differently: `ModelResilienceMiddleware` does not retry it and
moves to the next model of the `llm.fallback` list; only when the whole chain
fails does the turn end with `sign_in_required: "deepseek-account"`, and the
server then deletes the stored token, so `status` flips to `connected: false`
and the next Router turn is refused with `oauth_required` as above. All of this
is read from the code (the refusal, the retry limit, the fallback step, the
error field and the token deletion were checked here; the Router path itself is
traced in DEEPSEEK-ADAPTATION.md, "Super-agent Router 固定在 DeepSeek-V4.1-Flash")
and none of it was run end to end; whether the fallback step rescues a Router
thread is inferred from the code path, not tested.
`GET /api/v1/oauth/deepseek/models` lists the account's model catalog.

Account token reads clamp the legacy year-9999 expiry sentinel on the read path
(`src/server/database/oauth_tokens.py`) and store new sentinels at 00:00 UTC,
because the deployment's database session timezone (Asia/Shanghai) would
decode the old 23:59:59 UTC value as year 10000 and fail the turn.

**Token no longer accepted.** The account token is long-lived and has **no
refresh grant** (see `src/server/services/deepseek_oauth.py`): a token the
Platform stops accepting cannot be refreshed in the background, so the account
must be connected again. `status` reads only the local row, so it stays
`connected: true` until a rejected call makes the server drop that row (see
above). `models` answers 404 both when no token row is stored and when the
Platform gives no catalog (unreachable, an HTTP error of 400 or above, or an
unreadable payload), with the same `detail` ("No DeepSeek account is
connected") either way; a rejected token is one possible cause, not proven. The
text `invalid dsh token`, recorded here earlier as a 401 from the
M5 session, appears nowhere in the code or the tests: not verified, and not a
contract.

The normal path is the Conductor web UI (Settings → Connected accounts, or the
Setup wizard). The page calls `/api/v1/oauth/deepseek/initiate` with its own
origin plus `/oauth/callback` as `redirect_uri` (`127.0.0.1` is rewritten to
`localhost`), opens the returned `authorize_url`, and its `/oauth/callback` page
POSTs the browser-supplied `code` and `state` to
`/api/v1/oauth/deepseek/callback`. The backend accepts a `redirect_uri` that is
an HTTP(S) URL on `localhost`, `127.0.0.1` or `::1` without credentials, query
or fragment; the path is not checked.

Without the web UI, drive the same flow with curl. The initiate response
contains only `authorize_url`; the OAuth `state` is kept server-side and comes
back in the browser redirect's query string:

```bash
# 1. start the flow (use the web UI origin as redirect_uri for a served
#    callback page, e.g. http://localhost:5173/oauth/callback; the backend on
#    :8000 does not serve that page)
curl -s -X POST http://127.0.0.1:8000/api/v1/oauth/deepseek/initiate \
  -H 'Content-Type: application/json' -H 'X-User-Id: local-dev-user' \
  -d '{"redirect_uri":"http://localhost:5173/oauth/callback"}'
# -> {"authorize_url": "..."}

# 2. open authorize_url, approve. The browser returns to
#    <redirect_uri>?code=<code>&state=<state>; if that page is not served,
#    copy both values from the address bar and exchange them here.
curl -s -X POST http://127.0.0.1:8000/api/v1/oauth/deepseek/callback \
  -H 'Content-Type: application/json' -H 'X-User-Id: local-dev-user' \
  -d '{"code":"<code>","state":"<state>"}'
```

The state must match the initiate call; the pending flow is cached for the
provider's advertised `expires_in` (600 seconds is only the fallback), and a
second `initiate` replaces the first. If the browser round-trip is not
available, use the API-key route (`autonomy_router_model: deepseek-flash`) or
`null` for the user's own preference. The account client authenticates only
through the provider-declared `x-dsh-auth-token` header; the Anthropic SDK
still requires a standard auth header to build a request, so the client keeps a
non-secret `EMPTY` placeholder in `X-Api-Key` (the account endpoint ignores it)
and the real token only in `x-dsh-auth-token`.

Two deployment alternatives, both without committing credentials:

- **API-key route:** set `autonomy_router_model: deepseek-flash` when the
  deployment uses a DeepSeek API key rather than account sign-in.
- **Follow each user:** comment the key out (or set it to `null`) and the
  Router names no model, so its turn is resolved like any unnamed turn: the
  model its thread already holds, else the account default, which is the
  user's `model_preference.preferred_model` when one is saved:
  ```bash
  curl -s -X PUT http://127.0.0.1:8000/api/v1/users/me/preferences \
    -H 'Content-Type: application/json' -H 'X-User-Id: local-dev-user' \
    -d '{"model_preference": {"preferred_model": "deepseek-flash-account"}}'
  ```
  With none saved the turn runs the deployment default (`llm.name` in
  `agent_config.yaml`, `auto` in the committed file), which need not be
  DeepSeek, so a saved preference matters only for this alternative; the
  committed `autonomy_router_model` is the deployment-wide setting. (Read from
  `effective_default_model` and `saved_default_model`; the Router with no
  override was not run.)

Restart the backend after editing `config.yaml`. `tests/unit/config/test_router_model_config.py`
pins the committed selection and its manifest row.

### Local execution (BYOM)

By default this deployment refuses cloud Workers and runs Tickets on local
daemons. The Cloud Worker runtime is opt-in and default OFF:
`AUTONOMY_CLOUD_WORKER_RUNTIME=docker` (or `autonomy_cloud_worker_runtime:
docker` in `config.yaml`; the environment variable wins) enables the shipped
Docker OS-isolated runtime, but only after it verifies the configured worker
image, isolation flags and worker CLI; verification or launch failures settle
the Ticket failed with no shared-runtime fallback. See
[`CLOUD-WORKERS.md`](CLOUD-WORKERS.md) for enablement, the refusal matrix and
security caveats. The daemon path must run with a policy entry for the Home
workspace:

```bash
# the policy entry is keyed by the Home workspace id; this call creates the Home
# row if needed and is idempotent -> {"workspace_id": "...", ...}
curl -s -X POST http://127.0.0.1:8000/api/v1/workspaces/home \
  -H 'X-User-Id: local-dev-user'

# one-time policy: write the JSON file yourself (entry format:
# docs/ports/worker-isolation.md, "Local setup"). It must sit outside the
# workspace and isolation roots, be owned by the daemon user and be mode 0600
# or stricter. ~/.conductor-daemon/workspaces-cli.json is the path `cond` reads
# (CONDUCTOR_WORKSPACE_POLICY overrides it).
mkdir -p ~/.conductor-daemon
# ... create ~/.conductor-daemon/workspaces-cli.json, then:
chmod 600 ~/.conductor-daemon/workspaces-cli.json

# registers the daemon and saves its one-time token (not printed) to
# ~/.config/conductor/daemons/<daemon-id>.token; cond reads it from there
conductor daemons register --name my-laptop --provider codex

# A prefix assignment does not change this shell's expansion of --cwd.
# Export first, or an existing CONDUCTOR_REPO_ROOT selects another checkout.
export CONDUCTOR_REPO_ROOT=/path/to/conductor

CODEX_HOME=$HOME/.codex \
CONDUCTOR_DAEMON_TOKEN=<token from that file> \
.venv/bin/python scripts/conductor_local_daemon.py \
  --base-url http://127.0.0.1:8000 \
  --provider codex \
  --workspace-policy ~/.conductor-daemon/workspaces-cli.json \
  --isolation-root ~/.conductor-daemon/cli-state \
  --cwd "$CONDUCTOR_REPO_ROOT"
```

Codex is the example, not the only provider. Register another the same way
(`--provider cursor`, `kimi`, `grok`, `deepseek-dsh`, `zcode`, `stepfun`,
`minimax`, `claude`, `mimo`, `doubao`, or `deepseek`) and pass that
`--provider` to the daemon script. `cond up` starts every `[[daemon]]` block
in `~/.conductor/daemons.toml`. With the backend already running, one
provider in the foreground:

```bash
cond foreground daemon --provider cursor --id cursor-id
```

`docs/COND.md` lists the fleet file and the env names each provider may receive.

The isolation root needs no manual step: the daemon creates it with mode 0700
(`--help`; `cond` also runs `mkdir -p` and `chmod 700` on it before it starts the
daemon), and refuses to start when neither `--isolation-root` nor
`CONDUCTOR_ISOLATION_ROOT` is set. A Ticket's `cwd`, when set, must already
exist under `--worktree-root` (default `~/conductor-wt`) and inside that
workspace's policy root. The policy file's shape and what it allows are in
`docs/ports/worker-isolation.md`. The commands above were checked against
`--help`, `scripts/cond` and the code, not executed: no daemon was started for
this audit.

The shell and the TUI select the most recently seen registered daemon
automatically (one seen in the last 180 seconds if there is any, else the
newest). Only the line shell reads `CONDUCTOR_DAEMON_ID` to pin one; the TUI and
`-p` ignore it (`cond` uses the variable for a different job, choosing which
saved token starts the daemon). The TUI and `-p` always name a daemon and
refuse to send a Ticket when none is registered, so neither can reach the
Docker Cloud Worker runtime. Only the line shell sends a Ticket without
`worker_daemon_id` (it warns at start when no daemon is registered), and so does
any API or web client that omits the field: such a Ticket is a cloud Ticket
(CLOUD-WORKERS.md, "Contract").

## Commands

Line shell (`--no-tui`, a pipe, a narrow terminal, `--exec`):

```
/jobs [--status S] [--attention] [--failed] [--limit N]
/jobs <id|index>          detail + event record
/jobs new <prompt>        create a manual Ticket
/jobs watch <id|index>    follow activity until terminal (15 minutes at most)
/jobs retry <id>
/jobs reply <id> <text>
/jobs requeue <eval-id>
/stop <id|index>
/agents [logs <id> [--lines N]]   local fleet + native runtimes + daemons
/signals                  Sources and counts
/review run <line> --by <line> [--provider codex]
/review status [line]
/review verdict <id> --verdict MERGE|BLOCK [--note TEXT]
/merge <line> --into integrate/r2 [--test-cmd CMD]
!<command>                local shell command, after confirmation
/new /help /exit          (/new only clears the job list here)
```

Line-shell aliases: `/j`, `/s`, `/a`, `/r`, `/m`; `/quit` and `/q` for `/exit`.
The TUI knows only `/j`, `/quit` and `/q`; `/s`, `/a`, `/r` and `/m` answer
`unknown command` there.

`<id>` is the full Ticket UUID, or the number from the last `/jobs` listing.
The line shell prints the full UUID when it creates or acts on a Ticket
(`dispatched <id>`, `stop requested <id>`, `retry queued <id>`,
`reply sent <id>`), and `-p` prints `ticket <id>` in full. The line shell's
`/jobs` table and detail title, and the TUI's notices and `/jobs` output, print
only the first 8 characters, which the server does not accept: its Ticket
routes are typed `UUID`, so a short id (or any other word) gets
`422 ... Input should be a valid UUID`. For these two inputs, `retry` and
`3b7bc6e0`, sent through the app's real 422 handler on a one-route test app,
the body is

```json
{"detail":[{"type":"uuid_parsing","loc":["path","ticket_id"],"msg":"Input should be a valid UUID"}]}
```

which names the field and the rule, not the rejected value. That is not a
general guarantee: `docs/SUPER-AGENT.md` ("Request validation errors") says the
scrub is not value-free by construction. Both the line shell and the TUI accept
the list number instead of an id (read from `target` and `_resolve_target`), so
in the TUI use the number shown by `/jobs`.

The TUI defaults to the Supervisor view: Worker token/tool output is hidden and `/details` toggles it for debugging. It has a smaller set, plus session commands (`docs/ports/cli-tui.md`,
"Commands"): `/jobs` (the ten newest), `/jobs <id|index>`, `/jobs watch
<id|index>`, `/stop`, `/agents` (fleet and daemons), `/signals`, `/model`,
`/review`, `/merge`, `!<command>`, `/clear`, `/compact`, `/expand`,
`/collapse`, `/resume [id]`, `/new` (a fresh saved session), `/help`, `/exit`;
`@path` attaches a local file. It has no `/jobs retry|reply|requeue|new`, no
`/jobs` filters and no `/agents logs`.

`/review` and `/merge` run `python -m conductor_cli --url <server> review|merge
…` as a local subprocess, in `CONDUCTOR_WT_ROOT` (default `~/conductor-wt`),
where the `<line>` arguments name worktree directories. That CLI only resolves
the lines locally and calls the server's review/merge API (see
[REVIEW-MERGE-API.md](REVIEW-MERGE-API.md)); the git, test and Codex work is
done by the server, not by the subprocess. Both the CLI and the server refuse a
line reviewing itself, and the server pins each queued review to the line head;
the queue is the durable review record. Merge is gated by a Codex review the
server runs itself: the merge is rehearsed in a temporary git worktree, tested
against the merged tree, and Codex must return `VERDICT: MERGE` on that exact
rehearsal before the target branch is fast-forwarded onto it. Merge is
local-only: loopback peer, a configured local Host name, and no forwarding
headers, so it runs on the machine that hosts the backend and is not reachable
remotely. Landing is a ref-only compare-and-set: the server moves the target
branch onto the rehearsed merge commit and reports
`worktree_sync_required: true`; run `git -C <target> reset --hard` to sync the
checkout (this discards uncommitted changes in it).

Local actions ask first. The TUI asks Allow once / Session allow / Deny for
`/review` (every form), `/merge` and `!<command>`. The line shell asks only for
`/merge` and `!<command>` (on stdin, and it refuses both when stdin is not a
terminal); its `/review` runs without a prompt.

## Smoke evidence (M5, 2026-11-04/05)

Entries marked *recorded* come from the earlier M5 sessions (a live backend,
DeepSeek account and Codex daemon on `integrate/r2`, after the TUI, review/merge
API, DeepSeek and Docker-isolation merges). No backend was running when this
page was audited, so they were not repeated; the one that left a checkable
trace was checked as noted. Entries marked *re-run* were run on this checkout
on 2026-11-05.

- *Recorded.* `cond` starts the backend + local daemon; full-screen TUI opens
  with the conversation, composer and status bar (`model deepseek-flash-account`,
  jobs, daemon, token estimate, session, transport).
- *Recorded.* Plain prompt → Ticket `3b7bc6e0…` created and followed to
  `completed`; TUI showed activity rows and the final report.
- *Recorded.* `!echo hello` showed the in-TUI permission row; Allow once ran it
  and printed `hello`. `/help`, `/jobs`, `/model` render as scrollable blocks.
- *Recorded.* Server review API: `conductor review run api-smoke-line --by
  api-smoke-target` queued a durable review; `review status` reads it back.
  `/review` still refuses self-review locally and on the server (both checks
  are in the code: `src/conductor_cli/review.py` and the server's
  `self_review_refused`).
- *Recorded, trace checked.* Server merge API: `conductor merge api-smoke-line
  --into api-smoke-target --test-cmd true` rehearsed in a temporary worktree,
  ran the server-side Codex gate, and landed a real merge commit (`ec3ce84…`),
  returning `codex_verdict: merge` and `worktree_sync_required: true`. That
  commit exists in the local smoke repository ("Merge line api-smoke-line into
  api-smoke-target"). A red test command returns a structured `tests_failed`
  refusal without touching the target.
- *Recorded.* DeepSeek account route restored from the DSH platform credential:
  `/oauth/deepseek/status` connected true; `/oauth/deepseek/models` returns
  `deepseek-flash`/`deepseek-v4-pro`; the header-auth SDK bug is fixed (pinned
  by `tests/unit/llms/test_anthropic_oauth_header.py`, passing here) and
  DeepSeek-V4.1-Flash inference succeeded.
- *Re-run.* The super-agent unit tests of `libs/ptc-cli` (the six
  `test_superagent_*.py` files: shell, TUI app and units, hidden text, text
  strings, carriage return): 288 passed, with `PYTHONPATH=libs/ptc-cli:.` and
  `--rootdir=libs/ptc-cli`. A direct probe of the TUI controller against a fake
  client reproduced the differences listed under "Known gaps" (`/jobs retry
  abc` is looked up as the Ticket id `retry`, and a plain prompt with no daemon
  sends nothing). That 2026-11-05 re-run printed a single Alembic head of `140`.
  The current head is in [`../MIGRATIONS.md`](../MIGRATIONS.md).
- *Re-run.* Cloud Worker Docker runtime, live smoke
  (`scripts/cloud_worker_smoke.py`; colima on macOS Virtualization.Framework,
  arm64, Docker Engine 29.5.2; no credential and no vendor call):
  - The stub run passed 113 checks (0 failed, 1 informational) in 45 s. The run
    against the real image `conductor-cloud-worker:latest` (already on the
    engine; `codex-cli 0.166.0`) passed 13 checks in 33 s. Both exited 0 with
    their own verified cleanup; `docker ps -a` held four containers before the
    first run and four after the second (counted, not compared by id), and
    `docker images` the same five tags. The check counts match those first
    recorded on the same day in CLOUD-WORKERS.md ("Live smoke"), which also
    lists what its nine review rounds of the runtime and the smoke fixed.
  - They cover verification (the probe binds a staging directory and reads the
    user, cgroup limits and mounts the engine really applied), a launch through
    the switch, the `/workspace` bind mount (the Worker's file reached the host
    staging copy), and no container or staging directory left after success,
    failure, timeout, cancel, a Worker that `chmod 000`s its tree and a failing
    `docker rm -f`. The smoke removes only containers carrying its own label
    and confines the runtime's expiry sweep to that label
    (`AUTONOMY_CLOUD_WORKER_SWEEP_LABEL`, checked with a decoy expired
    container the run makes itself). On this engine `--network bridge` lets a
    Worker reach services on the host's loopback (reported by the smoke).
  - What the reviews left in the runtime, as CLOUD-WORKERS.md records it: the
    isolation probe reads the kernel's files with shell builtins instead of
    trusting the image's tools, and verification needs the CLI's `--version`
    output to name the CLI; every staging directory and env file carries an
    ownership marker (`<entry>.owner`) and its container the label
    `conductor.cloud-worker.stage=<entry name>`, and the staging sweep deletes
    only marked entries (unmarked entries from older versions are left for the
    operator) whose ctime is older than 1,800 s (twice the 900 s reap grace),
    a staging directory only when the engine lists no container for it (if the
    engine cannot be asked, no directory is touched), an env file on age and
    marker alone, with no container check; a failed Worker's output reaches only
    the Ticket error, built as one screened string where the failure is raised,
    while the exception, its traceback and the logs carry the exit code and
    fixed words, and exceptions raised while handling a Worker that exited 0
    carry a fixed message naming only the stage; container teardown is
    protected from repeated cancellation and bounded at 60 s. Verification
    checks a trusted image's configuration and cannot detect an image that
    lies.
  - **No vendor call was made in any of these runs.** Default remains local
    daemon; Docker must be enabled explicitly.

## Known gaps

- **Docker cloud Workers have no live output stream.** The CLI/TUI stream run
  event logs for Workers that publish them (legacy/orchestration Worker runs)
  and fall back to activity polling. The Docker-isolated runtime buffers
  subprocess output and parses the final report; it has no run-event or daemon
  output publisher, so a Ticket on it shows activity rows and the final report
  only. Native providers other than Codex (no incremental transcript) keep the
  activity/final-report path too.
- **Local daemon/native live text: Codex only, best effort.** Local
  daemon/native output streams live for the Codex provider only
  (line-incremental: Codex reports completed items rather than token deltas)
  when the server has migration 140 and the daemon runs with the default
  `CONDUCTOR_DAEMON_TOKEN_STREAM=1`; set it to `0` to keep only the
  report/activity summary. Streaming is fail-closed: a credential key followed
  by `:`/`=`, PEM block, email, terminal control or hidden-text character (bidi
  override or isolate, tag character, lone surrogate), URL password
  (`scheme://user:password@host`, a placeholder such as
  `postgres://user:password@localhost` included), over-512-character token, a
  skipped or altered stdout record, a queue overflow, a lost batch, a stale
  epoch, an acknowledgement that is not exactly `stored: true`,
  `tainted: false`, or any store failure ends live *text* for that run (tool
  names stop with it) and the report path is unaffected. After a retry or a
  stream reset the shell prints `live output restarted (...)` and never joins
  text across it. The guard (`src/server/utils/output_guard.py`) is a heuristic
  over known credential shapes, not a guarantee. See `docs/SUPER-AGENT.md`
  ("Output stream protocol") for the rules, their false positives and what they
  do not catch.
- **Migration 140 is a precondition for live text.** `docs/SUPER-AGENT.md` says
  a daemon whose live channel is unavailable, a missing migration included,
  keeps the report/activity path (not run here); run `alembic upgrade head` to
  get live text. The revision the M5 database is at was not checked (no
  backend was running); a fresh database needs no repair.
- **The TUI is not a superset of the line shell.** Checked against the code and
  a fake client on this checkout:
  - It has no `/jobs retry|reply|requeue|new`, no `/jobs` filters and no
    `/agents logs`. `/jobs retry <id>` is read as a lookup of the Ticket id
    `retry` and the server answers 422 (not a UUID); `/agents logs a1` is
    accepted and the argument ignored. Use `cond --no-tui` (or the web UI or
    API) for those.
  - The TUI and `-p` send nothing when no daemon is registered (the TUI's
    message says the deployment cannot run cloud Workers, which is not true
    when the opt-in Docker runtime is enabled), and they ignore
    `CONDUCTOR_DAEMON_ID`.
  - A TUI `/merge` subprocess is cut at 11,100 s whatever
    `CONDUCTOR_MERGE_TIMEOUT_SECONDS` says; the line shell follows that
    variable (up to 86,400 s for the CLI's own wait).
  - The line shell's `/review` is not gated (see "Commands"); the TUI's is.
- **Hidden and joining characters are not drawn.** The shell and the TUI remove
  bidi controls, the tag block, zero-width and joiner characters (ZWJ and ZWNJ
  included), variation selectors, fillers and lone surrogates from everything
  they draw. Emoji ZWJ sequences and flag tag sequences are therefore drawn as
  their parts, and Persian and Indic ZWNJ/ZWJ spelling is not shown joined; the
  server record keeps the original text (checked with `plain()` on a family
  emoji, a tag-sequence flag and a Persian ZWNJ word). See
  `docs/ports/cli-tui.md` ("Terminal safety") for the paths covered and what is
  not.
- **Cloud Worker OS isolation requires an explicit image and switch.** The
  Docker runtime ships default-off and refuses unless the image verifies
  (engine, hardened isolation probe, worker CLI). Enable with
  `AUTONOMY_CLOUD_WORKER_RUNTIME=docker` after building
  `conductor-cloud-worker:latest`; see [`CLOUD-WORKERS.md`](CLOUD-WORKERS.md).
  No live vendor CLI/provider call has been made: the real CLI image builds and
  verifies, but no Codex credential was available on 2026-11-05 (what is needed
  is in `CLOUD-WORKERS.md`), so a real Worker turn is unverified. Without
  credentials the real CLI keeps retrying until the wall-clock limit rather
  than failing fast (the real-image smoke run ended at its 30 s limit). Known
  runtime limits: a container orphaned by a backend crash is force-removed
  (with its staging copy and env file) only by the next launch's expiry sweep,
  by default 900 s past the Worker's wall-clock limit (3600 s unless
  `AUTONOMY_CLOUD_WORKER_TIMEOUT` says otherwise); stop/switch cancellation
  latency is poll interval + DB + up to three `docker rm -f` attempts; a staging
  copy whose container could not be confirmed removed is left on disk until the
  sweep finds the container gone; the expiry sweep removes every expired
  container carrying the runtime's deadline label on the engine, another
  backend's included (`AUTONOMY_CLOUD_WORKER_SWEEP_LABEL=key=value` narrows it
  to containers that also carry that label, but the runtime never adds the
  label to its own containers: it is for a test harness such as the smoke, and
  a production backend leaves it unset, because with it set its own expired
  Workers are never reaped); `AUTONOMY_MAX_RUNNING_WORKERS` bounds daemon
  claims and cloud launches together (a cloud launch that finds no free slot
  leaves its Ticket queued and looks again every 10 seconds);
  `--network bridge` is unfiltered egress and reaches the host's loopback
  services on colima, where the smoke showed it (Docker Desktop was not
  tested); nothing caps what a Worker writes to its bind mount while it runs;
  workspace publication never deletes files from a lossy staging snapshot; a
  Worker that exits 0 reports in its own words, stored unscreened (screening
  of forwarded environment values covers only a failed Worker's Ticket error).
- **Merge API is ref-only.** A successful merge moves the target branch ref and
  returns `worktree_sync_required: true`; the operator must sync the target
  checkout (`git -C <target> reset --hard`), and until then that checkout reads
  as dirty and the next merge into it is refused `target_dirty`. The Codex gate
  runs on the exact rehearsal tree; the queue verdict is bookkeeping, not the
  gate. A Codex `BLOCK` comes back without its findings
  (`review_output_omitted`); run the review yourself to read them.
- **TUI tool blocks show names only.** The activity stream intentionally sends
  tool names only; tool arguments and file diffs are not projected because
  free-text arguments/file bodies cannot be made credential-safe by a bounded
  scrubber. The client does not read `args`/`diff` either, so a payload or an
  old session file that carries them still shows the name alone. (A unified diff
  the assistant writes in its own text is colored; that is model output, not a
  tool argument.) Showing arguments would need a per-tool allow-list of
  structured, non-free-text fields on the server first. A
  daemon/native Worker that does not stream (non-Codex provider, streaming
  disabled or tainted) still provides only its activity/report rows.
- **DeepSeek account token has no refresh grant.** If the platform stops
  accepting it, one browser PKCE re-login is required (web UI → Connected
  accounts, or the curl initiate/callback sequence documented above; see
  `DEEPSEEK-ADAPTATION.md`). Fallbacks: `deepseek-flash` API-key route (needs a
  key) or null Router model override. How the M5 token was first obtained (the
  earlier note said it was restored from the DSH platform credential) was not
  re-checked.
