# `cond` — local Conductor supervisor

`scripts/cond` is the single entry point for the backend, the web console, and
every daemon listed in the fleet file. It is a bash wrapper around
`python -m src.conductor_cli.ops_cli`. The checkout is the repository that
contains the script (`CONDUCTOR_REPO_ROOT` overrides it). It does not default
to a particular worktree.

## Install

```bash
ln -sfn /path/to/conductor/scripts/cond ~/.local/bin/cond
```

`~/.local/bin` must be on `PATH`. Use the checkout you actually run, not a
retired worktree.

## Fleet file

`~/.conductor/daemons.toml` replaces the hand-written `v1-daemon.sh` and
`v1-daemons.txt` pair:

```toml
[backend]
host = "127.0.0.1"
port = 8000

[web]
host = "127.0.0.1"
port = 5170
mode = "preview"   # or "static" after `pnpm build`

[[daemon]]
provider = "codex"
id = "daemon-id"
env = ["CODEX_HOME"]

[[daemon]]
provider = "cursor"
id = "cursor-id"
env = ["CURSOR_API_KEY"]
keychain = { CURSOR_API_KEY = "service-name:account" }

[[daemon]]
provider = "deepseek-dsh"
id = "dsh-id"
env = ["DEEPSEEK_API_KEY", "DSH_HOME"]
env_files = ["~/.config/deepseek/env"]
```

Import an existing pair without executing the shell script:

```bash
cond daemons migrate \
  --script /path/to/v1-daemon.sh \
  --list /path/to/v1-daemons.txt \
  --output ~/.conductor/daemons.toml
```

Migration keeps provider names and env **names**. It does not copy secret
values and it does not read `~/.zshenv`.

Codex is one provider in that file, not the only one. `cond up` starts every
`[[daemon]]` block. A daemon other than Codex is the same block with its own
`provider` and `id` (`cursor`, `deepseek-dsh`, `deepseek`, `kimi`, `grok`,
`zcode`, `stepfun`, `minimax`, `claude`, `mimo`, or `doubao`). Names in `env`
have to be on that provider's allow-list (`ops_fleet.PROVIDER_ENV`).

One daemon in the foreground, once the backend is already running. This does
not start the rest of the fleet:

```bash
cond foreground daemon --provider cursor --id cursor-id
```

The process working directory and `CONDUCTOR_REPO_ROOT` are this checkout
(the repository that contains `scripts/cond`, unless `CONDUCTOR_REPO_ROOT`
is set).

## Secrets

Provider keys come from `~/.conductor/secrets.env` (mode `600`) or from a
keychain reference in the fleet file. `cond` refuses a secrets file with any
other mode, refuses symlinks, and never prints values. A line that names
`~/.zshenv` is rejected. Only names on the provider allow-list are passed to
that daemon (`CURSOR_API_KEY`, `CODEX_HOME`, `KIMI_CODE_HOME`, `DSH_HOME`,
`DEEPSEEK_API_KEY`, and the other names in `ops_fleet.PROVIDER_ENV`).

Home defaults, when the allow-list includes them and the variable is unset:
`CODEX_HOME=~/.codex`, `KIMI_CODE_HOME=~/.kimi-code`, `DSH_HOME=~/.dsh`.

Daemon tokens stay in `~/.config/conductor/daemons/<id>.token`. The workspace
policy is `~/.conductor-daemon/workspaces-cli.json`. The sandbox policy files
`~/.config/conductor/daemon_policy.yaml` and
`~/.config/conductor/daemon_trusted_keys.json` are checked by `cond doctor`,
not loaded from the shell.

## Commands

```bash
cond up                 # backend --host 127.0.0.1, web, every daemon
cond status             # backend / web / daemons; exit 1 if something is down
cond restart            # rolling restart: backend, web, then one daemon at a time
cond restart daemons    # only the daemons, rolling; no backend stop, no launch wait
cond down               # stop what this checkout started
cond stop               # alias of down
cond down|stop|upgrade [--wait-timeout SECONDS] [--force | --no-wait]
cond restart [all] [--wait-timeout SECONDS] [--force | --no-wait]
cond start              # alias of up
cond logs [-n 80] [name]
cond doctor             # port 5170 (or CONDUCTOR_WEB_PORT), daemons, policy files
cond upgrade [--ref REF] [--restore-db] [--wait-timeout SECONDS] [--force | --no-wait]
cond smoke [--timeout SECONDS] [--json]   # one read-only Ticket per daemon
cond drain [--reason TEXT]   # dispatch starts no new launch or fix round
cond undrain                 # clear the drain; held launches continue
cond restart --drain [--wait-timeout SECONDS] [--force]   # drain, wait, restart, undrain
cond backup
cond clean              # list only
cond clean --apply      # remove merged clean worktrees; never dirty or unmerged
cond launchd install|enable|disable|uninstall
cond setup [--write] [--force] [--json]   # detect CLIs, optionally write agents.toml
cond agents list|set|login                # configure worker CLIs in agents.toml
cond                    # up, then the Super-agent CLI
```

`scripts/cond` forwards every verb that `ops_cli.build_parser()` defines
(`start` and `stop` are mapped to `up` and `down`). Anything else, such as
`-p PROMPT`, runs `cond up` and hands the arguments to the Super-agent CLI.
`tests/unit/test_cond_wrapper_verbs.py` enumerates the ops CLI verbs and fails
when the wrapper does not forward one.

The backend command always includes `--host 127.0.0.1`, even if the server's
own default changes. The web console defaults to port **5170**. `mode = preview`
runs `vite preview`. `mode = static` serves `web/dist`: build it with
`make web` (`pnpm build` in `web/`) or `cond upgrade`. Ports: `CONDUCTOR_BACKEND_PORT`,
`CONDUCTOR_WEB_PORT`.

Logs go to `~/.conductor/logs/` (`CONDUCTOR_LOG_DIR`). Files rotate by size
before each open (`CONDUCTOR_LOG_MAX_BYTES`, default 10 MiB,
`CONDUCTOR_LOG_BACKUPS`, default 5). `launchd install` also writes a newsyslog
snippet under that directory. It does not write `/etc/newsyslog.d` unless
`CONDUCTOR_NEWSYSLOG_DIR` is set.

`cond down` only signals processes whose command and cwd belong to this
checkout. It does not call `launchctl` unless `cond launchd install` recorded
labels for this `CONDUCTOR_HOME`.

`make dev` runs the backend with hot reload on `DEV_HOST`, default
`127.0.0.1`. Binding another interface has to be explicit:
`make dev DEV_HOST=0.0.0.0`. `make web` builds `web/dist`.

## Dispatch launches on down and restart

Dispatch Workers are started by the backend through
`scripts/dispatch_launch.sh`, each launch leading a process group of its own.
They do not die with the backend: after a plain stop they are reparented to
init and keep working in the line worktree, and a resumed dispatch can start
the same line a second time. So `cond down`, `cond stop`, `cond restart` and
`cond upgrade` first list the dispatch launches of this deployment.

A process is a launch of this deployment only when all of these hold:

- its command is the launcher script: `CONDUCTOR_DISPATCH_LAUNCH_SCRIPT`, or
  `scripts/dispatch_launch.sh` under `CONDUCTOR_DISPATCH_REPO` or this checkout;
- it leads its own process group (the dispatcher starts each launch that way);
- its parent is this deployment's backend (the pid in the backend pid file,
  still running `server.py` from this checkout), or its parent is pid 1 and
  its cwd is under this deployment's worktree roots (`CONDUCTOR_WORKTREE_ROOT`,
  and `dispatch-worktrees` next to `CONDUCTOR_DISPATCH_RUNS_DIR`): a launch a
  previous backend left behind.

Other launcher processes are printed as `not ours (left alone)` and are never
waited for or signalled; neither is `cond`'s own group.

The dispatch variables (`CONDUCTOR_DISPATCH_LAUNCH_SCRIPT`,
`CONDUCTOR_DISPATCH_REPO`, `CONDUCTOR_DISPATCH_RUNS_DIR`,
`CONDUCTOR_DISPATCH_ADAPTERS`) are read from the checkout `.env`, because that
is what the backend started by `cond up` sees: it gets cond's minimal
environment plus `load_dotenv`, which follows a symlinked `.env`. A relative
path is relative to the checkout. When the operator's shell has a different
value, `cond` prints a warning and uses the `.env` value (or the backend
default when the `.env` has none). When `CONDUCTOR_DISPATCH_ADAPTERS=live` and
neither the launch script nor the dispatch repo is set, `cond` warns that
launches cannot be detected: the backend's built-in default launcher
(`~/conductor-wt/launch.sh`) is not tied to this checkout and is not matched
on purpose.

| Option | Behaviour |
|---|---|
| default | wait up to `--wait-timeout` seconds (default `CONDUCTOR_DISPATCH_WAIT_SECONDS`, else **600**) for every launch to finish, printing the pid, group, age and model it waits for. If one still runs then, exit 1 and leave the backend running. `--wait-timeout 0` checks once and refuses at once |
| `--force` | send `SIGTERM` to each launch's process group, wait 10 s, then `SIGKILL` the groups whose leader is still a launch of this deployment, and stop the backend. The leader is checked again before each signal, so a reused pid is not signalled; a group whose leader exited while other members remain is reported, not killed |
| `--no-wait` | today's behaviour: stop the backend at once and print a warning that lists the launches left running |

`--force` and `--no-wait` cannot be combined. `--wait-timeout` must be a
finite number of seconds `>= 0`; `inf`, `nan` and negative values are refused. A launch started between the
check and the backend stop is not seen; run `cond down` again or check
`cond doctor`. `cond upgrade` runs the check before step 1 (the backup), so a
refusal leaves the code and the database untouched; a launch started during
the upgrade's build steps is not seen either.

## Drain for deploys

Once lines run in parallel there is no quiet window to restart in, so a
deploy asks dispatch to stop *starting* work instead:

- `cond drain [--reason TEXT]` writes `$CONDUCTOR_HOME/drain.json`
  (`{"set_at", "reason"}`; default `~/.conductor/drain.json`). `cond undrain`
  removes it. Draining twice keeps the first flag. Readers fail closed: a flag
  file that cannot be parsed or read (not UTF-8, a directory, a dangling
  symlink) still counts as drained. `cond undrain` on a path that is not a
  file exits `1` and asks you to remove it by hand.
- While the flag is set, dispatch starts **no new Worker process**: no new
  launch, no fix round, no proactive self-report turn. Runs already going keep
  running, and monitoring, G2, review and merge continue. The backend reads the
  flag before each start; nothing needs restarting to apply or clear it.
- A held launch is not a failure. It waits before its Worker is spawned
  (checked before the worktree is prepared and again right before the spawn),
  spends no attempt, and continues by itself within ~5 seconds of
  `cond undrain`. If the backend is restarted while a launch is held, the
  resumed drive finds a launch that never spawned, as after a restart during
  worktree preparation.
- `cond status` prints `drain: off` or
  `drain: on (no new dispatch launches) since=<time> reason=<text>`; `--json`
  has `"drain": {"drained": bool, "set_at", "reason"}`. It also asks the
  running backend (`GET /api/v1/dispatch/status/ops`) and prints
  `drain (backend): on|off` (`unknown` when the backend is down or does not
  answer), the backend's busy markers, and
  `warning: CONDUCTOR_HOME mismatch: cli=<path> backend=<path>` when the
  backend reads a different flag file than the CLI writes; `--json` has it as
  `"drain_backend"`. Drain does not change the status exit code.

`cond restart --drain` is the deploy restart:

1. set the drain (unless one is already set),
2. wait for running dispatch launches (Worker processes) to finish, up to
   `--wait-timeout` (default `CONDUCTOR_DISPATCH_WAIT_SECONDS`, 600) — the same
   wait as a plain `cond restart`; `--force` stops them instead. The scan runs
   twice, before and after a 2 s grace period, so a launch that passed its
   drain check just before the flag was set is caught too,
3. wait, in the same budget, until no busy marker is live under
   `$CONDUCTOR_HOME/busy/`: in-flight G2 and G5 runs, reviewer CLIs, merges and
   launch calls past the drain hold each hold one (`src/conductor_cli/activity.py`;
   a marker of a dead process is ignored),
4. the rolling restart,
5. clear the drain.

The drain is cleared on every exit path, including SIGTERM and SIGHUP (a
dropped SSH session) during the wait, which exit `1`. When either wait times
out nothing is restarted, the drain is cleared, and the command exits `1`
(naming the busy kinds, if any); run it again or use `--force`. A drain that was set before the command (`cond drain`) belongs
to the operator and stays on. `--drain` refuses `--no-wait` and
`restart daemons` (that restart has no launch wait at all).

The guard lives in `src/tools/auto_dispatch/drain.py` (`DrainAwareLauncher`,
wrapped around `ScriptLauncher` in `DispatchService`); the flag path and
reader are in `src/conductor_cli/drain.py`, which both sides import.

## Restarting only the daemons

`cond restart daemons` reloads daemon configuration without rebuilding the
whole stack. It stops and starts each configured daemon **one at a time**, in
fleet-file order, and leaves the backend and web console running with their
existing pids. It does not wait for dispatch launches and refuses
`--wait-timeout` / `--force` / `--no-wait`, because no launch wait happens; it
also does not update worker CLIs (that belongs to `cond upgrade` or
`cond cli update`). A full `cond restart` keeps its dispatch-launch wait and
rolling backend-first behaviour.

The command prints the same status block as `cond status`. It exits `0` only
when the backend is healthy, the web console is up, and every configured daemon
is up. A daemon that fails to stop or start aborts the rolling pass with exit
`1`; daemons already restarted stay up.

## Post-deploy smoke

`cond smoke` sends one manual Ticket to every configured daemon and checks that
the daemon claims it and a worker CLI completes it:

- The Ticket is **read-only by prompt**: its prompt forbids reading or writing
  files, running tools or shell commands, and committing. It asks the worker to
  reply with `SMOKE-READY`. `POST /api/v1/autonomy/tasks` has no read-only or
  speculative flag, so the prompt is not enforced by the daemon; as a fence the
  Ticket runs in a fresh scratch cwd, `<root>/.cond-smoke/run-*`, where
  `<root>` is the deeper of the worktree root and the Home workspace's
  workspace-policy root (a daemon accepts only a cwd under both). It is never
  the repo or the workspace root, and it is removed when the run ends. When no
  such cwd can be made (no policy entry, roots that do not overlap) every
  daemon fails with `no smoke cwd: <reason>` and no Ticket is created.
  `CONDUCTOR_SMOKE_CWD` names the directory to make it in instead, for a host
  whose daemons run with a worktree root this shell does not see: it must be
  an absolute, existing directory (not a symlink) inside the Home workspace
  root, and the daemons still check it against their own worktree root.
- `READY` needs the whole trimmed reply to be exactly `SMOKE-READY`
  (case-sensitive): `NOT SMOKE-READY`, `SMOKE-READY.` or `smoke-ready` fail.
- Each Ticket names that daemon (`worker_daemon_id`) and its provider
  (`worker_provider`), so a queue shared by several daemons cannot mask one
  broken daemon with another daemon's success.
- All Tickets are created first, then polled until every Ticket is terminal or
  `--timeout` (default `CONDUCTOR_SMOKE_TIMEOUT`, else **180** seconds)
  expires. A broken daemon therefore does not delay the others.
- One line per daemon reports `READY` or `FAILED: <reason>`; the summary line
  is `smoke: N/M ready`. The exit code is `0` only when every daemon is READY.
  A timeout or a Ticket waiting for input is a failure.
- Cleanup runs however the run ends (verdicts, timeout, Ctrl-C, an unexpected
  error): every Ticket still working is asked to stop, and every created
  Ticket is archived (`POST /tickets/{id}/archive`), so smoke Tickets leave the
  active views (Ticket Lines hide archived Tickets by default; a raw
  `GET /tickets` with no status filter still returns them). All are titled `[smoke] <provider>/<id>` in case an
  archive fails. Both calls are best effort. Ctrl-C exits `130`.
- Like every `cond` HTTP call, smoke only contacts `http(s)` on `localhost`,
  `127.0.0.0/8` or `::1`; any other host is refused before a connection.
- With no configured daemons the command prints `daemon: none configured` and
  exits `0`.
- `--json` prints `{"ok": bool, "daemons": [{"provider", "id", "state",
  "reason", "ticket_id"}]}` for automation.

Run it as the last deploy step, after `cond up` or `cond restart`:

```bash
cond restart --drain    # or: cond restart daemons
cond smoke
```

It needs the same prerequisites as any daemon Ticket: a saved daemon token
(`~/.config/conductor/daemons/<id>.token`), a workspace-policy entry for the
Home workspace, and a live backend. A missing token, policy refusal, provider
quota error, or worker timeout appears as that daemon's failure reason.

## LaunchAgents

Templates live in `deploy/launchd/conductor.plist.in`. Login starts one
job, `com.conductor.supervisor`, whose program is `cond up --foreground`.
That process is the supervisor: it runs the same startup as a manual `up`
and does not exec a single role. An old personal `com.example.conductor.*.plist` in
the same directory is renamed to `*.plist.migrated` and is not loaded.

```bash
cond launchd install      # write plists, RunAtLoad false, no launchctl
cond launchd enable       # rewrite enabled and bootstrap the configured domain
cond launchd disable      # bootout, leave plists with auto-start off
cond launchd uninstall    # bootout and delete plists
```

`install` never calls `launchctl`. `enable` calls `launchctl` from `PATH`.
The directory is `CONDUCTOR_LAUNCHD_DIR` (default `~/Library/LaunchAgents`).
The domain is `CONDUCTOR_LAUNCHD_DOMAIN` (default `gui/<uid>`). Plists do not
contain secret values; the foreground command reads `secrets.env` itself.
Systemd units render only when `CONDUCTOR_SYSTEMD_DIR` is set
(`deploy/systemd/conductor.service.in`).

Do not point a LaunchAgent at a second checkout that shares the production
database.

## Upgrade

`cond upgrade --ref <tag|sha|HEAD>` first settles running dispatch launches
(see above, same options and defaults), then:

1. `pg_dump --format=custom` into `~/.conductor/backups` (`CONDUCTOR_BACKUP_DIR`), mode `600`, then `pg_restore --list`.
2. `git fetch` and `git checkout --detach <ref>` (detached HEAD is supported).
3. `uv sync --frozen --extra cli`.
4. `alembic upgrade head` (current revision is printed before and after).
5. `pnpm install --frozen-lockfile` and `pnpm build` in `web/`.
6. Rolling restart of the backend and daemons.
7. `GET /ready`.

On failure the code is checked out at the previous ref. The database backup is
restored only when `--restore-db` was passed. Redis is not part of the backup;
treat it as ephemeral.

## Worktrees

`CONDUCTOR_WORKTREE_ROOT` (default `~/.conductor/worktrees`) is the directory
for line and ticket worktrees. `CONDUCTOR_WT_ROOT` is still accepted as a
legacy alias. `cond clean` classifies worktrees under that root as merged and
clean, unmerged, uncommitted, or broken. The default is a list.
`--apply` runs `git worktree remove` only for merged worktrees with a clean
status. It also drops stale rows from `~/.conductor/run/registrations.json`
and deletes pid files whose process is already gone. It never removes a
worktree with unmerged commits or uncommitted files, and it never deletes the
checkout `cond` itself is running from.

## Readiness and doctor

`GET /ready` reports database, Redis, the MCP registry
(`CONDUCTOR_MCP_REGISTRY`, default `~/.conductor/mcp/servers.toml`), dispatch
config (`CONDUCTOR_DISPATCH_REPO` must be a git checkout), and daemon
heartbeat files under `~/.conductor/run/heartbeats`. A failed check returns
503. `/health` stays the liveness probe.

`cond doctor` checks that the web port accepts connections (5170 unless
`CONDUCTOR_WEB_PORT` or the fleet file says otherwise), that each configured
daemon process is the one this checkout started, and that the sandbox policy
files exist as regular files and are not group- or world-readable.

The `orphan_processes` check is a warning (`WARN`), never a failure, and it
never signals anything. It lists processes whose parent is pid 1 and that have
run longer than `CONDUCTOR_ORPHAN_SECONDS` (default 21600, 6 h), when the
command is a dispatch launch of this deployment, or a `claude -p` /
`claude --print` or `codex exec` whose working directory is under a worktree
root (`CONDUCTOR_WORKTREE_ROOT`, and `dispatch-worktrees` next to
`CONDUCTOR_DISPATCH_RUNS_DIR`, default `~/.conductor/dispatch/runs`). Each row
shows the pid, age, kind and cwd, not the command line. Stop them yourself
after checking what they are. The dispatch configuration warnings above
(shell value differs from `.env`, live dispatch without a launch path) are
also reported here, under `config_warnings`.

## Environment

| Variable | Role |
|---|---|
| `CONDUCTOR_REPO_ROOT` | checkout to run |
| `CONDUCTOR_HOME` | state directory, default `~/.conductor` |
| `CONDUCTOR_WORKTREE_ROOT` | worktree root, default `~/.conductor/worktrees` |
| `CONDUCTOR_BACKEND_PORT` | backend port, default 8000. Host is always 127.0.0.1 |
| `CONDUCTOR_WEB_PORT` | web port, default 5170 |
| `CONDUCTOR_WEB_MODE` | `preview` or `static` |
| `CONDUCTOR_SECRETS_FILE` | mode-600 env file |
| `CONDUCTOR_DAEMONS_FILE` | fleet file |
| `CONDUCTOR_BACKUP_DIR` | database backups |
| `CONDUCTOR_LAUNCHD_DIR` | where plists are written |
| `CONDUCTOR_LAUNCHD_DOMAIN` | launchctl domain |
| `CONDUCTOR_DISPATCH_LAUNCH_SCRIPT` | dispatch launcher that down/restart/upgrade waits for (read from the checkout `.env`) |
| `CONDUCTOR_DISPATCH_REPO` | dispatch checkout; its `scripts/dispatch_launch.sh` counts too (read from the checkout `.env`) |
| `CONDUCTOR_DISPATCH_WAIT_SECONDS` | default `--wait-timeout`, 600 |
| `CONDUCTOR_DISPATCH_RUNS_DIR` | dispatch runs; `../dispatch-worktrees` is a worktree root (read from the checkout `.env`) |
| `CONDUCTOR_ORPHAN_SECONDS` | doctor orphan age threshold, default 21600 (6 h) |
| `CONDUCTOR_SMOKE_TIMEOUT` | default `cond smoke --timeout`, 180 seconds |
| `CONDUCTOR_SMOKE_CWD` | directory `cond smoke` makes its scratch cwd in (inside the Home workspace root) |

Switching a machine that is already running onto this `cond` is a separate
deploy step. Do not bootstrap LaunchAgents, dump the production database, or
stop the live stack from a development checkout.
