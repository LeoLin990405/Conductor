# Daily operations runbook

This is the page to open when you run Conductor on one Mac and need to do
something today: start or stop it, deploy a new commit, roll back, take or
restore a backup, find a log, or move a parked dispatch line. It names the
command and the file. The full behaviour of each command is in the reference
docs linked from each section:

- [`COND.md`](COND.md): the `cond` supervisor, fleet file, drain, smoke,
  LaunchAgents, upgrade.
- the internal document `OPERATIONS-LOCAL` (not published): what should be running, the
  one-time files, backup format, cleanup.
- [`operations.md`](operations.md): the `conductor` API client.
- [`ports/auto-dispatch.md`](ports/auto-dispatch.md): the dispatch state
  machine and every operator verb in detail.
- [`HARDENING.md`](HARDENING.md): loopback binding, daemon policy, workspace
  isolation.

`tests/unit/docs/test_runbook_sync.py` checks this page against the code:
every `conductor ...` and `cond ...` command in a code span must exist in the
CLI parser or the `scripts/cond` dispatch table, and every park reason the
dispatch code defines must have a row below.

Two command families:

- `cond` (`scripts/cond`, symlinked onto `PATH`) runs the local stack:
  backend, web console, daemons, backups, upgrades. It works with the backend
  down.
- `conductor` is the API client. It talks to the running backend
  (`--url`, then `CONDUCTOR_URL`, then `http://127.0.0.1:8000`).

> **A dispatch starts only with `conductor dispatch start "<goal>"`.** Any
> other first word that is not a verb listed below is refused with a "did you
> mean" hint (`dispatch show <id>` suggests `status <id>`, `dispatch list`
> suggests `active`), and nothing starts. `start` also refuses a one-word goal
> and a goal that begins with a verb-like word (`start list`). The old bare
> form `conductor dispatch "<goal>"` is refused too (N85, 2026-11-14).

## What runs

| Piece | Started by | Port | Log |
|---|---|---|---|
| Backend (`server.py`, bound to 127.0.0.1) | `cond up` | 8000 (`CONDUCTOR_BACKEND_PORT`) | `~/.conductor/logs/backend.log` |
| Web console | `cond up` | 5170 (`CONDUCTOR_WEB_PORT`) | `~/.conductor/logs/web.log` |
| Daemons, one per `[[daemon]]` in `~/.conductor/daemons.toml` | `cond up` | none | `~/.conductor/logs/daemon-<provider>-<id>.log` |
| Postgres 16 | Homebrew, not `cond` | 5432 | outside the tree |
| Redis | Homebrew, not `cond` | 6379 | outside the tree |

Postgres and Redis must already be running. `cond` never starts or stops them.

Where keys live:

- Provider keys: `~/.conductor/secrets.env` (mode `600`).
- Daemon tokens: `~/.config/conductor/daemons/<id>.token`.
- Policy files: `~/.config/conductor/daemon_policy.yaml`,
  `~/.config/conductor/daemon_trusted_keys.json` and
  `~/.conductor-daemon/workspaces-cli.json`.

`cond` refuses a secrets file with a looser mode and never prints values. See
[`COND.md#secrets`](COND.md#secrets).

## Start, stop, status

```bash
cond up                 # backend, web console, every daemon
cond status             # exit 1 if anything is down; --json for scripts
cond doctor             # web port, daemon pids, policy-file modes, orphans
cond down               # stop what this checkout started (cond stop is the same)
cond restart            # rolling: backend, web, then one daemon at a time
cond restart daemons    # daemons only, one at a time; backend keeps running
```

`cond start` is `cond up`. With no subcommand, `cond` runs `cond up` and
then opens the Super-agent TUI.

`cond down`, `cond restart` and `cond upgrade` first wait for running dispatch
Workers, up to 600 s by default (`--wait-timeout`). If a Worker still runs
after that, they exit 1 and leave the backend up. `--force` kills those
Workers' process groups and `--no-wait` stops the backend at once and leaves
them running. Details: [`COND.md`](COND.md), section "Dispatch launches on
down and restart".

Checks from the API side:

```bash
conductor health        # liveness (GET /health)
conductor status        # server status
conductor daemons list  # registered daemons
conductor daemons status
```

Which code is running:

```bash
cond --version          # version, commit SHA of this checkout, dirty flag
conductor --version     # the same line for the CLI's own checkout
cond doctor             # build_identity: checkout SHA next to the server's
```

`/health` also reports the commit the backend was started from (`sha`). When
`cond doctor` shows a different server SHA, the backend still runs older code:
`cond restart`.

The backend binds 127.0.0.1 unless `--host` or `CONDUCTOR_BIND_HOST` says
otherwise, and logs a warning for a non-loopback address. CORS allows only the
local console (`http://127.0.0.1:5170`, `http://localhost:5170`);
`CONDUCTOR_CORS_ORIGINS` (comma-separated) replaces that list. See
[`HARDENING.md`](HARDENING.md).

`/health` means the process is up. `/ready` also checks the database, Redis,
the MCP registry, `CONDUCTOR_DISPATCH_REPO` (default: the backend's working
directory, which must be a git checkout) and the daemon heartbeats. It
returns 503 when one of them fails.

Start at login (macOS):

```bash
cond launchd install    # writes the plists only, no launchctl
cond launchd enable     # turns auto-start on and bootstraps the job
cond launchd disable    # boots it out and leaves the plists
cond launchd uninstall  # boots it out and deletes the plists
```

After a reboot with LaunchAgents enabled, run `cond status` and check for
exit 0. Without them, run `cond up`.

## Drain

Draining stops dispatch from starting new Worker processes. Running Workers
keep going, and review, G2 and merge continue.

```bash
cond drain --reason "deploy"   # writes ~/.conductor/drain.json
cond status                    # shows drain: on ... and the backend's own view
cond undrain                   # held launches continue within ~5 s
cond restart --drain           # drain, wait for Workers and busy markers, restart, undrain
```

A launch held by the drain is not a failure and spends no attempt. If you set
the drain yourself, `cond restart --drain` leaves it on when it finishes. Run
`cond undrain` afterwards. Details: [`COND.md`](COND.md), section "Drain for
deploys".

## Deploy

For the stack that `cond` runs, deploy with `cond upgrade`:

```bash
cond status                          # start from a healthy stack
cond upgrade --ref <tag-or-sha>      # backup, checkout, sync, migrate, build, restart, /ready
cond smoke                           # one read-only Ticket per daemon; exit 0 only if all READY
```

`cond upgrade` performs these steps in order:

1. Settles running dispatch launches, with the same wait and options as
   `cond down`.
2. Writes a `pg_dump` backup.
3. Runs `git checkout --detach <ref>`.
4. Runs `uv sync --frozen --extra cli`.
5. Runs `alembic upgrade head`.
6. Builds `web/`.
7. Restarts the backend, then each daemon.
8. Requires `/ready`.

On success it prints the `previous` commit, the new `ref`, the `backup` file and
the alembic revision before and after. Keep that output: it is what you roll
back to. If a step fails, the rollback notes also go to
`~/.conductor/logs/cond.log`.

To deploy with parallel lines running, drain first:

```bash
cond drain --reason "upgrade"
cond upgrade --ref <tag-or-sha>
cond undrain
cond smoke
```

`conductor deploy` (with `--dry-run` and `--repo`, and `conductor deploy status`
for the last record) is an older, separate path that pulls the current branch.
Its stop step matches the process command line `server.py --port 8000`. That
pattern matches neither the backend `cond up` starts nor the one
`conductor deploy` itself starts: both run `server.py --host 127.0.0.1 --port
8000`. On a `cond`-managed stack, use `cond upgrade`. See
[`operations.md`](operations.md), section "Deploy and rollback".

## Rollback

There are three ways to roll back:

- **An upgrade step failed.** `cond upgrade` checks the previous ref back out
  by itself. It restores the database only when you passed `--restore-db`:
  `pg_restore --clean --if-exists --single-transaction` from the backup it took
  at the start. Without that flag, the database stays as it was when the step
  failed.
- **The upgrade succeeded but the code is bad, and no migration ran.** Run
  `cond upgrade --ref <previous-sha>`. The previous commit is the `previous`
  value the upgrade printed.
- **A migration must be undone.** Restore the backup taken before the
  upgrade (its path is the `backup` value the upgrade printed). On a `cond`
  stack, do it by hand: `cond down`, then `git checkout --detach <sha>`, then
  the live restore in the next section, then `cond up`.

`conductor rollback --commit <sha> --backup <file>.dump` does the same steps
for a backend that `conductor deploy` started. It validates the dump, asks you
to type `ROLLBACK`, stops the backend, runs `git reset --hard <sha>`, restores
the dump and starts its own backend. Run it with `--dry-run` first. It has the
same stop-pattern limit as `conductor deploy`, so it does not stop a backend
that `cond up` started. Neither path runs `alembic downgrade`. Read
[`../MIGRATIONS.md`](../MIGRATIONS.md) before downgrading by hand.

## Backup and restore

```bash
cond backup
```

The dump is a custom-format file, `conductor-<timestamp>.dump`, in
`~/.conductor/backups` (`CONDUCTOR_BACKUP_DIR`), mode `600`. Each dump is
checked with `pg_restore --list`. Only the newest `CONDUCTOR_BACKUP_KEEP` dumps
are kept (default 10). Redis is not backed up and should be treated as
ephemeral.

Restoring is manual: neither `cond` nor `conductor` has a restore command. **Rehearse on a
scratch database first:**

```bash
createdb conductor_restore_drill
pg_restore --clean --if-exists --no-owner --exit-on-error \
  --dbname conductor_restore_drill ~/.conductor/backups/conductor-<timestamp>.dump
```

To restore the live database (named `conductor` here; use your `DB_NAME`):

```bash
cond down
pg_restore --clean --if-exists --single-transaction \
  --dbname conductor ~/.conductor/backups/conductor-<timestamp>.dump
# check out the commit the dump was taken on, so alembic current matches the code
cond up
cond status
```

These are the same `pg_restore` flags that `cond upgrade --restore-db` uses.
Before `cond up`, `alembic current` must match the checkout.

## Logs

| What | Where | Read with |
|---|---|---|
| Backend | `~/.conductor/logs/backend.log` | `cond logs backend`, `conductor logs tail` |
| Web console | `~/.conductor/logs/web.log` | `cond logs web` |
| One daemon | `~/.conductor/logs/daemon-<provider>-<id>.log` | `cond logs <provider>` |
| All of the above | `~/.conductor/logs/*.log` | `cond logs -n 200` |
| Upgrade rollback notes | `~/.conductor/logs/cond.log` | `cond logs cond` |
| Worker CLI updates | `~/.conductor/logs/cli-update.jsonl` | `cond cli status` |
| One work line | `<worktree root>/logs/` | `conductor lines logs <line>` |
| A dispatch, as a timeline | the database | `conductor dispatch status <id>` |

`cond logs` filters on the file name, so `cond logs codex` shows every codex
daemon. The directory is `CONDUCTOR_LOG_DIR` (default `~/.conductor/logs`).
Files rotate by size, both when they are opened and while the process runs
(a small pump process owns each log file). `CONDUCTOR_LOG_MAX_BYTES` sets the
size (default 10 MiB) and `CONDUCTOR_LOG_BACKUP_COUNT` the number of archives
kept (default 5; the older name `CONDUCTOR_LOG_BACKUPS` still works when the
new one is unset). `backend.log.1` is the newest archive. Output from
`cond logs` is redacted.

An unhandled server error answers HTTP 500 with the fixed `detail`
`Internal server error` and a `correlation_id` (also in the
`x-correlation-id` header); no exception text is in the response. A failed
Ticket's report names the exception type, a scrubbed one-line message
(tokens, secret query parameters, DSN userinfo, SQL bound values, home
paths, and IP addresses removed), and ends with `(correlation_id=...)`.
Search the backend or daemon log for that id to find the full message and
traceback:
`grep <correlation_id> ~/.conductor/logs/*.log*`.

## Dispatch operator verbs

`conductor dispatch` has twelve verbs. Any other first word is refused, never
taken as a goal.

| Command | Writes? | Use it when |
|---|---|---|
| `conductor dispatch start "<goal>" [--workspace ID\|NAME] [--workers W,W]` | yes | You want a new dispatch for that goal. The goal needs more than one word and must not begin with a verb |
| `conductor dispatch active` | no | You want the dispatches in progress. A bare `conductor dispatch status` lists the same |
| `conductor dispatch status <id>` | no | You want lines, gates, park reasons, each line's owner and next step, and who needs a person |
| `conductor dispatch approve <id> [--note T]` | yes | The dispatch waits at `APPROVAL` and the lines should merge into `integrate/auto-dispatch-<id>` |
| `conductor dispatch reject <id> [--note T] [--force]` | yes | The dispatch waits at `APPROVAL` and should not merge. On a dispatch in `REVIEW`, `LAUNCHING`, `MONITORING` or `ASSIGNED`, it cancels |
| `conductor dispatch cancel <id> [--note T] [--force]` | yes | The dispatch should stop. Unfinished lines become `FAILED dispatch_cancelled`. `--force` stops running Workers first |
| `conductor dispatch reassign <id> <line-id> <model>` | yes | A parked line should continue with another model on the same branch. The pick sticks (N86): no automatic fallback moves the line to another worker; if that worker cannot run, the line parks `pinned_worker_unusable`. A worker this dispatch skips for quota, or whose quota reads empty, is refused at once (`worker_skipped`, `worker_no_headroom`). Both rules follow `CONDUCTOR_DISPATCH_HUMAN_PIN_MODE` (`off`, `shadow` default: log and audit only, `enforce`) |
| `conductor dispatch unblock <id> <line-id> [--note T]` | yes | An old parked line answers `reassignment_not_at_checkpoint`. Unblock moves it to a checkpoint so `reassign` works |
| `conductor dispatch adjudicate <id> <line-id> in-scope\|out-of-scope [--note T]` | yes | Reviewers raised a scope or security conflict. A security conflict needs `--note` to be ruled out of scope |
| `conductor dispatch recover <id> [<line-id>] [--split\|--human\|--rereview] [--note T]` | yes | A parked line should take its next recovery step now (`--split` takes it out of the round, `--human` hands it to a person). `--rereview` (N87) means you fixed the line's branch yourself: G2 and review run again on its current head, with no Worker launched; use it for `replan_exhausted` and other replan parks. Without a line id, it asks for the next step of every blocked line |
| `conductor dispatch abort <id> [--note T] [--force]` | yes | The dispatch should end whatever live state it is in. At `APPROVAL` it is a reject; anywhere else, `PLANNING` and `MERGE` included, it cancels and keeps the branches. A finished dispatch answers 409 |
| `conductor dispatch deliver <id>` | yes | An approved, `COMPLETE` dispatch should be pushed and get its pull request. The outcome is in `gates.delivery`; a missing GitHub token is recorded as `needs_token`, not an error |
| `conductor dispatch cost [<id>] [--limit N]` | no | Read-only cost report (N30): with an id, cost per line and model for that dispatch; without one, the by-model and waste-by-class tables over the latest N dispatches (default 50, at most 500) |
| `conductor dispatch assignments <id>` | no | Read-only (N73 L5): every recorded Super-agent assignment entry per line, oldest first (model, effort, runtime, reason, feasibility, re-ask, fallback); the same data as `GET /api/v1/dispatch/{id}/assignments` |

A refusal is an HTTP 409 whose text is the reason, for example
`line_not_parked`, `run_out`, `fix_pending`, `drive_active` or `needs_human`.
A refused request writes nothing. While a drive runs, `reassign`, `unblock`,
`adjudicate` and `recover` are queued and applied at its next step. Refusal
codes and queue behaviour are listed in
[`ports/auto-dispatch.md`](ports/auto-dispatch.md), section "Operator ways out
of a parked dispatch".

## Collab apps

Apps that use the collaboration protocol (N80,
[`ports/collab-protocol.md`](ports/collab-protocol.md)) are managed with
`conductor apps`; `cond apps` forwards to it unchanged.

| Command | Use it when |
|---|---|
| `conductor apps register <app> --scopes read,claim` | A new app should get a token. It goes to the keychain entry `conductor-collab/<app>` and is never printed |
| `conductor apps list` | You want the apps, their scopes and token digest prefixes |
| `conductor apps scopes <app> --add review` | An app needs more (or, with `--remove`, fewer) scopes |
| `conductor apps rotate <app>` | An app token may have leaked; the old one stops working at once |
| `conductor apps revoke <app>` | An app should stop; every lease it holds is released |
| `conductor apps audit <app>` | You want the app's audit rows |

Every `conductor apps` call needs the operator credential: the internal
service token, read from `INTERNAL_SERVICE_TOKEN` in the environment or the
checkout `.env` and sent as `X-Service-Token`. Without it the CLI stops with
`unauthorized` before any request, and the server answers 401 to an admin
call that lacks it, carries a wrong one, or carries an app token. An app's
own calls use `conductor collab` (for example `conductor collab whoami`) with
the app's token.

## Park reasons

A parked line is `BLOCKED` with a `failure_reason`, and `conductor dispatch
status <id>` shows it. The liveness sweep (N76) puts each reason in a recovery
group (`src/tools/auto_dispatch/liveness/policy.py`):

| Group | Automatic steps |
|---|---|
| R-EXIT | Reassign to another developer, reassign again, then split the line off |
| R-REVIEW | Retry the review after 60 s, 300 s and 900 s |
| R-QUOTA | Wait for headroom (at most 24 h), then resume the review |
| R-REPLAN | Reassign once, then split |
| R-SPLIT | Remove the line and its dependants from this round |
| HUMAN | None. A person must act |

Automatic steps run only when `CONDUCTOR_DISPATCH_LIVENESS_MODE=enforce`. The
default, `shadow`, only records what the sweep would do. In every mode,
`conductor dispatch recover <id> <line>` runs the next step on request. A
reason the policy does not list falls to HUMAN. Each line may take at most
`CONDUCTOR_DISPATCH_LIVENESS_MAX_STEPS` automatic steps (default 4) before it
needs a person. `~/.conductor/liveness_policy.yaml` may only tighten the table.

In the table below, "recover" means
`conductor dispatch recover <id> <line>` and "reassign" means
`conductor dispatch reassign <id> <line> <model>`.

### Worker exited, watchdog, focused tests

| Reason | Group | Meaning | What to do |
|---|---|---|---|
| `no_new_commit` | R-EXIT | The Worker exited 0 without a new commit | reassign, or recover |
| `uncommitted_changes` | R-EXIT | The Worker exited with a dirty worktree. The tree is kept and shown to the next model | reassign, or recover |
| `watchdog_timeout` | R-EXIT | The run passed its watchdog deadline (default 900 s) | Check the line log, then reassign or recover |
| `g2_no_commands_risk_bearing` | R-EXIT | A risk-bearing line has no focused-test command, so G2 cannot pass it | reassign, or cancel and replan with a test command |
| `g2_revision_moved` | R-EXIT | The branch moved while G2 was testing it | recover |
| `focused_tests_exhausted` | R-EXIT | Focused tests still fail after the allowed fix rounds | Read the G2 output, then reassign or recover `--split` |
| `fix_launch_failed` | R-EXIT | A fix round's Worker crashed again after its one fresh retry | Check the daemon log, then reassign |
| `worker_no_output` | R-EXIT | The Worker produced no output within `CONDUCTOR_DISPATCH_SILENT_WORKER_MINUTES` (default 15), or exited with an empty final answer and no new commit; recorded in `shadow` (default), reassigned only in `enforce` | Check the run log; reassign, or recover once the provider works again |
| `app_lease_expired` | R-EXIT | An app's lease on the line ran out (expiry or max lease) before it reported | reassign; the brief names the previous app and what it left |
| `app_lease_released` | R-EXIT | The app that held the line released its lease without finishing | reassign; the brief names the previous app and what it left |
| `handoff_incomplete` | HUMAN | The Worker's handoff is missing a branch, commit, files, test command, results or limits | Read the line log. Reassign, or recover `--split` |
| `git_evidence_unavailable` | R-SPLIT | The host could not read git evidence for the submitted worktree | Check the worktree with `git status`, then recover `--split` or cancel |

### Worker failures (native CLI classifier)

| Reason | Group | Meaning | What to do |
|---|---|---|---|
| `worker_auth_failed` | HUMAN | The Worker CLI is not logged in, or its key was refused | Log the CLI in again (`cond agents login`), then reassign |
| `worker_quota_exhausted` | HUMAN | The Worker's plan quota is used up | Reassign to another provider, or wait for the reset |
| `preexisting_failure` | HUMAN | G2 failed, but the same command fails the same way on the line's base commit (N27, only with `CONDUCTOR_DISPATCH_G2_PREEXISTING_MODE=enforce`) | Fix main first, or drop that check from the line's acceptance, then recover |
| `review_inconclusive` | HUMAN | The reviewer could not decide and named the evidence it needs (N59, only with `CONDUCTOR_DISPATCH_REVIEW_INCONCLUSIVE_MODE=enforce`); no fix round was counted | Provide the evidence on the branch, then `conductor dispatch unblock <id> <line>` reviews again |
| `pinned_worker_unusable` | HUMAN | A person reassigned the line to this Worker and it cannot run now (quota, rate limit); the line was not moved to anyone else | Reassign to another worker, or wait for headroom and recover |
| `context_rotation_exhausted` | HUMAN | The Worker asked for a fresh session (N29 context handoff) more times than the line allows; its work so far is on the branch | Relaunch with `conductor dispatch recover <id> <line-id>` (the rotation count restarts at 0), or reassign to another worker |
| `author_quota_exhausted` | HUMAN | The author ran out of quota during a fix round | Wait for the reset, or reassign |
| `worker_context_exhausted` | HUMAN | The Worker ran out of context | reassign |
| `worker_turn_budget_exhausted` | HUMAN | The Worker CLI stopped at its own `--max-turns`. Every request was answered; this is not a login or quota problem. It is normally continued in the same session automatically | Nothing while it retries. If it parks here, reassign or recover |
| `worker_confirmation_required` | HUMAN | The CLI asked for a confirmation nobody could give, twice | Reassign, or loosen that CLI's approval policy |
| `worker_tool_blocked` | HUMAN | The CLI's own safety gate blocked a tool call it will not run unattended (stepfun). The line is normally reassigned to another worker automatically; it parks here in a fix round or when nobody else is eligible | Reassign to another worker |
| `worker_rate_limited` | HUMAN | The provider rate-limited the Worker | recover after a while, or reassign |
| `worker_overloaded` | HUMAN | The provider reported it was overloaded | recover after a while, or reassign |
| `worker_timeout` | HUMAN | The Worker call hit its local deadline | Check the daemon log, then reassign |
| `worker_network` | HUMAN | A network error reached the Worker | Check connectivity, then recover |
| `worker_crash` | HUMAN | The Worker CLI crashed | Check the daemon log, then reassign |
| `worker_unknown` | HUMAN | The Worker failed in a way the classifier does not know | Read the daemon log, then reassign |
| `network_retries_exhausted` | HUMAN | Three same-session retries after network errors failed | Check connectivity, then reassign |
| `rate_limited_retries_exhausted` | HUMAN | Three retries after rate limits failed | Reassign to another provider |
| `overloaded_retries_exhausted` | HUMAN | Three retries after overload failed | Reassign to another provider |
| `timeout_retries_exhausted` | HUMAN | Three retries after timeouts failed | Reassign, or split the task |
| `context_exhausted_retries_exhausted` | HUMAN | Three fresh-session rotations ran out of context | Recover `--split` or cancel. The line is too large |
| `turn_budget_exhausted_retries_exhausted` | R-SPLIT | Same-session continuations after a spent turn budget ran out: the line is too big for one run | Recover `--split`, or cancel and re-plan smaller |
| `no_eligible_worker` | R-QUOTA | No Worker in the pool can take the line | Check `conductor workers` and `cond status`, then recover |

### Review

| Reason | Group | Meaning | What to do |
|---|---|---|---|
| `review_unavailable` | R-REVIEW | The reviewer CLI failed (timeout, launch failure, non-zero exit) | recover, or wait for the retries |
| `review_unparseable` | R-REVIEW | The reviewer's reply had no usable verdict | recover |
| `no_eligible_reviewer` | R-REVIEW | Every available reviewer already held the line | Add a reviewer to the pool, then recover |
| `reviewers_quota_exhausted` | R-QUOTA | Every eligible reviewer is out of quota | Wait, or recover to skip the wait |
| `no_secondary_reviewer` | R-QUOTA | A risk-bearing line found no second reviewer | Wait, or add a reviewer and recover |
| `review_backlog` | HUMAN | The line is held before launch because the review queue is full (`CONDUCTOR_DISPATCH_REVIEW_BACKLOG_MAX`) | Nothing. The line launches when the backlog drains |
| `review_blocked` | HUMAN | The reviewers blocked the change | Read the findings in `status`, then reassign or cancel |
| `reviewer_conflict_scope` | HUMAN | A reviewer tagged a blocker `scope:` | `conductor dispatch adjudicate <id> <line> in-scope` or `out-of-scope` |
| `reviewer_conflict_security` | HUMAN | A reviewer tagged a blocker `security:` | adjudicate. `out-of-scope` needs `--note` |
| `two_failed_rounds` | HUMAN | The second fix round after an in-scope ruling failed too | reassign (replaces the developer) |

### Review-result refusals (Stage-Gate)

These come from the Stage-Gate when it refuses a review result. They mean
the review record and the gate disagree. They are rare, and a person should
read the line before moving it.

| Reason | Group | Meaning | What to do |
|---|---|---|---|
| `unknown_event_kind` | HUMAN | The gate got an event it does not know | Read the line, then reassign or cancel |
| `already_terminal` | HUMAN | The gate task had already finished | Read the line, then reassign or cancel |
| `timestamp_missing` | HUMAN | A review event had no timestamp | recover, or reassign |
| `stage_gate_order` | HUMAN | The event came in the wrong stage order | recover, or reassign |
| `provider_required` | HUMAN | The review event named no provider | recover, or reassign |
| `self_review_forbidden` | HUMAN | The reviewer was the author | recover so another reviewer is picked |
| `result_required` | HUMAN | The review event had no result | recover |
| `invalid_result` | HUMAN | The review result was not a valid value | recover |
| `invalid_verdict` | HUMAN | The verdict was not a valid value | recover |
| `verdict_result_mismatch` | HUMAN | The verdict and the result disagree | recover |
| `secondary_review_required` | HUMAN | A risk-bearing line needs a secondary review first | recover |
| `reviewer_is_primary` | HUMAN | The secondary reviewer was the primary reviewer | recover so another reviewer is picked |

### Replan and planning

| Reason | Group | Meaning | What to do |
|---|---|---|---|
| `replan_parked` | R-REPLAN | The line used its one replan | reassign, or cancel |
| `replan_uncombined` | R-REPLAN | Passed dependencies sit on branches with no shared commit | reassign, or cancel |
| `replan_base_unresolved` | R-REPLAN | git cannot name a passed dependency's branch | reassign, or cancel |
| `replan_exhausted` | R-REPLAN | A reassign was refused because the replan budget is spent | Fix the branch yourself and `recover <id> <line> --rereview`, or recover `--split`, or cancel |
| `planner_failed` | HUMAN | The planner failed. The dispatch carries `planner_failed:<reason>` (table below) | cancel, then start again with a clearer goal |
| `resume_spec_missing` | HUMAN | A dispatch resumed in planning has no archived spec | cancel, then start again |

### Integration (G5)

| Reason | Group | Meaning | What to do |
|---|---|---|---|
| `g5_not_configured` | HUMAN | No full-suite command is configured for the integration gate | Configure it, then recover |
| `g5_full_suite_failed` | HUMAN | The full suite failed on the integration branch | Read the output. Recover `--split` the guilty line, or cancel |
| `g5_timeout` | HUMAN | The full suite passed its deadline (default 3600 s) | recover, or cancel |
| `g5_worktree_dirty` | HUMAN | The integration worktree had changes before G5 | Clean the worktree by hand, then recover |

### Liveness

| Reason | Group | Meaning | What to do |
|---|---|---|---|
| `liveness_inconsistent` | HUMAN | The line's status and its Stage-Gate stage disagree and cannot be repaired safely | Read `status`, then reassign, recover `--split` or cancel |

### Failed-line reasons (not parks)

These end a line for the round. They are listed so you recognise them in
`status`.

| Reason | Meaning |
|---|---|
| `split_off` | The line and its dependants were taken out of this round (`recover --split` or R-SPLIT) |
| `replanned` | The line was replaced by its replan children |

### Planner failure reasons

A dispatch whose planner fails parks in `REVIEW` as
`planner_failed:<reason>`, with no lines run. Cancel it with
`conductor dispatch cancel <id>`, then start a new dispatch.

| Reason | Meaning |
|---|---|
| `invalid_json` | The planner's reply did not parse, or did not match the schema |
| `no_json` | The reply held no plan |
| `unassigned` | The role policy could not place every line |
| `exceeds_developer_pool` | The plan had more lines than the developer pool can staff |
| `acceptance_test_directory` | An acceptance command ran pytest on a whole test directory |
| `goal_too_large` | The goal was over the byte limit and was refused before the model ran |
| `planner_timeout` | The planner run timed out |
| `planner_error` | Any other planner failure |

## Clean-up

```bash
cond clean              # list stale worktrees, registrations, pid files
cond clean --apply      # remove only merged, clean worktrees and dead entries
```

See the internal document `OPERATIONS-LOCAL` (not published), section "Cleanup".
