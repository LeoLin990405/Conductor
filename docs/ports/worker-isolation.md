# Per-workspace isolation for autonomous Workers

Status: implemented and validated. Code commit: `fcd5334cecec36a6069744106e643cbef48bf255` (tree `b348ef76feac10b63fe182cf4c32c116c94d0f10`). This port adapts the isolation requirement identified by §2 of the Super-agent comparison. The legacy survey is used as architectural context only; no source code was copied from the legacy repositories.

## Contract

Each local daemon Worker must resolve an explicit policy profile for its workspace. The profile supplies the workspace root, environment variable names, and code-only runtime read paths. The policy file is owned by the daemon user, mode `0600` or stricter, and stored outside workspace roots and isolation state; the mode/owner check is tied to the descriptor that is parsed. Missing/invalid profiles, unsafe root overlap, unsafe host credential setup, or unsupported sandboxing fail closed. Neither a workspace root nor a runtime read path may be the daemon user's home, a credential directory (`~/.ssh`, `~/.aws`, `~/.gnupg`, keychains) beneath it, or an ancestor of one (`~/Library` carries `Library/Keychains`), or a system temp tree (`/tmp`, `/var/tmp` — resolved, so `/private/tmp` on macOS is covered); overlap between roots, the policy, the isolation state, and runtime paths is decided by inode identity where the paths exist plus case-folded containment on volumes that probe case-insensitive (a case-swapped-spelling probe on the nearest existing ancestor, failing closed as case-insensitive), so case-variant spellings of one directory on a case-insensitive volume — including not-yet-created paths — are still detected. The launched process receives a workspace-specific `HOME` and temporary directory, and only the profile-approved environment values. It does not inherit the broad parent environment or ambient authentication state. Public network access remains enabled for providers. Loopback TCP is denied on macOS: Postgres (5432), Redis (6379), the Conductor backend (8000), and the Docker API (2375/2376) stay closed unless the workspace policy lists that port in `localhost_tcp_ports`. The Seatbelt filter is `localhost`, which matches 127.0.0.1 and ::1; the profile language rejects a numeric host. The Docker unix socket stays denied with every other host unix socket. Linux does not enforce that deny. `bwrap` is started without `--unshare-net`, so the worker shares the host network namespace: every loopback port is reachable, including Redis on 127.0.0.1, Postgres, and the backend, and `localhost_tcp_ports` is ignored. Abstract-namespace unix sockets are reachable too (X11 `@/tmp/.X11-unix/X0`, and some D-Bus and systemd endpoints) because they live in that shared namespace. Filesystem unix sockets outside the sandbox mounts, such as `docker.sock` and ssh-agent, are not mounted. Tracked follow-up: a network namespace plus an egress proxy that forwards only public traffic and the policy's loopback ports, failing closed when the proxy is unavailable. Do not describe loopback or abstract-socket denial as a Linux guarantee until that lands.

Native process and ACP launches pass through [`isolation.py`](../../src/llms/native_cli/isolation.py). Linux uses a `bwrap` namespace and macOS uses `sandbox-exec`. The Linux namespace read-only binds whichever host trust store exists — Debian (`/etc/ssl/certs`, `/etc/ca-certificates`) or RHEL/Fedora (`/etc/pki/ca-trust`, `/etc/pki/tls/certs`, `/etc/pki/tls/cert.pem`) — and fails closed when none of those paths exist. `prepare_process` mounts each existing store with `--ro-bind` through `collect_trust_store_binds()`, so `/etc/pki/ca-trust` is mounted on a RHEL or Fedora host where that directory exists. Symlink entries that leave the store, including the chain from `/etc/ssl/certs` through `/etc/pki/...` to `ca-trust/extracted` or a resolved `/usr/share/pki/...` copy, are mounted as well. A top-level symlink is followed to the end of the chain, not only its first hop. Provider adapters use the shared launch boundary in [`process.py`](../../src/llms/native_cli/process.py), [`acp.py`](../../src/llms/native_cli/acp.py), and native adapters (`deepseek.py`, `grok.py`, `minimax.py`, `zcode.py`, `zhipu.py`). The local policy is explicit and workspace keyed. Runtime read paths must identify trusted CLI/runtime code directories only; do not include credential stores or user home directories. The child PATH is minimal and derived by the isolation wrapper.

Autonomy cloud Worker launch, including legacy resume, is refused in [`worker.py`](../../src/server/services/autonomy/worker.py); legacy inputs are saved for manual review in [`inputs.py`](../../src/server/services/autonomy/inputs.py). The same refusal (`_CLOUD_WORKER_REJECTION_ERROR`) covers the dispatch paths added with line switch, staged handoff, resume, and thread reassign: an unisolated cloud worker is not started, and a daemon-bound ticket stays on the pull path. Ticket reply and follow-up paths in [`autonomy.py`](../../src/server/app/autonomy.py) and `inputs.py` keep daemon-bound work pull-only, including legacy rows with a backend thread ID. A compare-and-swap guard in [`tickets.py`](../../src/server/database/autonomy/tickets.py) blocks eligible unisolated cloud launches. Daemon-bound Tickets continue through the local policy-enforced path, and orchestration Tickets remain non-worker orchestration records. Ordinary interactive chat retains its existing shared trust behavior.

An explicit OS-isolation runtime switch (`AUTONOMY_CLOUD_WORKER_RUNTIME` or
`autonomy_cloud_worker_runtime` in `config.yaml`) lives in
[`cloud_runtime.py`](../../src/server/services/autonomy/cloud_runtime.py) and
is off by default. This build ships a Docker-backed runtime
([`docker_worker_runtime.py`](../../src/server/services/autonomy/docker_worker_runtime.py)):
when the switch is enabled it verifies the engine, worker image, isolation
flags and worker CLI, then runs the Worker in a disposable container with only
the private workspace staging tree mounted, no host environment/credentials or
host network by default, a read-only rootfs, dropped capabilities, a non-root
user and resource/time limits. Any verification or launch failure settles the
Ticket failed; there is no shared-runtime fallback. Enablement, the refusal
matrix and security caveats are documented in
[`../CLOUD-WORKERS.md`](../CLOUD-WORKERS.md).

### Scratch exec guard and the throwaway test database (N19)

An isolated Worker once downloaded a VM tarball into
`.orchestrator/autonomy/<ticket>/` and ran it. Two controls now cover that case.

**Scratch exec guard.** `CONDUCTOR_WORKER_EXEC_GUARD` (`off`, `shadow`,
`enforce`; default `shadow`) is read when a Ticket's isolation context opens
([`exec_guard.py`](../../src/llms/native_cli/exec_guard.py)). The scratch and
download areas are `<workspace>/.orchestrator` (also under the policy root when
the run uses a subdirectory) and `<isolated HOME>/autonomy` and
`<isolated HOME>/Downloads`.

| Mode | macOS | Linux |
|---|---|---|
| `off` | nothing checked | nothing checked |
| `shadow` | when the context closes, every executable file that appeared or changed in the scratch areas is logged (`worker_exec_guard_scratch_executables`) and listed in `WorkerIsolation.exec_guard_findings`; nothing is blocked | same |
| `enforce` | the Seatbelt profile adds `(deny process-exec* …)` for those directories, so direct exec and `#!` use of a file there fail with EPERM; the shadow report still runs | report only: `bwrap` has no per-mount `noexec`, so Linux does not block |

Seatbelt can log a denial but cannot report what a run would have done, so
shadow is a scan at the end of the run, not a log-only Seatbelt rule.
`.venv/bin`, `node_modules/.bin` and every other workspace path outside the
scratch areas keep running. Limits: the deny checks paths. A Worker can copy
or move a binary out of the scratch areas, download it straight into another
workspace directory, or pass it to an allowed interpreter (`sh file`), and in
those cases it still runs. A scan stops after 20,000 entries.
Network egress is not restricted. **Still open:** the Linux network namespace
plus egress proxy (and a macOS egress policy that denies binary downloads).

**Throwaway test PostgreSQL.** This is opt-in: set `CONDUCTOR_WORKER_TEST_PG=on`
(default `off`). [`worker_test_pg.py`](../../src/server/services/autonomy/worker_test_pg.py)
runs `initdb` and `pg_ctl` on the host (binaries from `PATH` or
`CONDUCTOR_WORKER_TEST_PG_BIN`) and creates `conductor_worker_<ticket8>` with
a random per-run password. The server listens on `127.0.0.1` only, on a free
port, with `scram-sha-256` auth and no unix socket. The module returns
`TEST_DB_HOST/PORT/NAME/USER/PASSWORD`, then stops the cluster (`-m immediate`)
and removes its directory when the run ends, including when the run fails.
It never creates or uses a database or role named `postgres`, and it ignores
`PG*` variables from the daemon environment. Pass the values to
`worker_isolation(test_db_env=pg.env())`. That call accepts only the
`TEST_DB_*` names, sets them in every child environment, and adds the port to
the run's `localhost_tcp_ports`. The local daemon does not call this yet.

## Development Worker policy notes

Each Worker in the dispatch roster runs under one workspace policy. This
section records what each provider needs so a policy can be written for it:
the binary, the shape of its runtime read paths, the environment variable
**names** it takes, and whether the adapter knows about the isolated `HOME`.

The policy itself is workspace-keyed and lives outside the workspace roots and
the isolation state, mode `0600`, owned by the daemon user — see
[Contract](#contract) and [Local setup](#local-setup). What follows is the
per-Worker detail those two sections do not carry.

| Worker | Binary | Typical `runtime_read_paths` (shape) | `env` names | Gets `HOME=<root>/home` |
|---|---|---|---|---|
| `kimi` | `kimi` | the CLI's own runtime directory (a Node install, or the package directory the CLI ships in) | — (CLI login) | yes |
| `glm` | `zcode` | the CLI's own runtime directory | `ZAI_API_KEY`, `ZAI_CN_CODING_API_KEY` | yes |
| `grok` | `grok` | the CLI's own runtime directory | — (CLI login) | yes |
| `cursor` | `cursor-agent` | the CLI's own runtime directory | `CURSOR_API_KEY` | yes |
| `stepfun` | `step` | the CLI's own runtime directory | `STEPFUN_API_KEY`, `STEPFUN_PLAN_API_KEY` | yes |
| `deepseek` | `dsh` | the CLI's own runtime directory; never `~/.config/deepseek/env` | `DEEPSEEK_API_KEY`, `DSH_HOME`; account mode `NATIVE_CLI_DSH_ACCOUNT` | yes |
| `minimax` | `mcode` | the CLI's own runtime directory | `MINIMAX_API_KEY` | yes |
| `doubao` | `arkcli` | the CLI's own runtime directory | `ARK_API_KEY` | yes |

Two cautions about the table.

**`runtime_read_paths` must name code, never state.** A read path is a
directory of trusted CLI/runtime code — a Node install directory, or a package
directory the CLI ships in. It is never a credential store, never a user home
directory, and never the place the CLI keeps its login. The contract already
fails closed on those; this table only gives the shape of a legitimate entry.

**`env` holds names, not values.** The policy lists the *name* of a variable
and the daemon reads the value from the host at launch. A policy must not
carry a value, and this table deliberately does not name any path where a
credential lives.

### Login state under an isolated `HOME`

Every Worker launch goes through `prepare_process` → `_minimal_env`, which
sets `HOME` to `<isolation-root>/home` (`WorkerIsolation.home` is
`root / "home"`). `kimi`, `cursor`, `stepfun` and `doubao` receive that
`HOME`. What they lack is adapter-side awareness: those adapters do not look
for configuration inside it. Whether a login created outside that `HOME` is
visible is unknown, so an operator who needs one of them signs it in under
the isolated `HOME` or checks the lane separately.

`grok`, `glm`/`zcode`, `deepseek` and `minimax` read the current isolation
and look for their configuration **inside** the isolated `HOME`. They do not
carry a login state across the boundary: the configuration has to be there
already, and one that is missing stops the run — `grok` refuses without
workspace-scoped credentials, and `minimax` refuses to provision an API key
for an isolated worker.

`glm` launches the official `zcode` binary by default (`DEVELOPMENT_WORKERS`
maps `glm` to `zcode`). The clean-room `zcode-cli` is used only when
`NATIVE_CLI_ZCODE_BIN` names it.

`doubao` is a development Worker (`DEVELOPMENT_WORKERS`). Its binary is
`arkcli`. The process gets the same isolated `HOME`. `DoubaoArkCliAdapter`
does not read that isolation itself. The policy `env` name is `ARK_API_KEY`.

DeepSeek:

- Keep `~/.config/deepseek/env` off `runtime_read_paths`. The child shell
  sources `$HOME/.config/deepseek/env` only when `DEEPSEEK_API_KEY` is unset,
  and that file has to live under the isolated `HOME`.
- `NATIVE_CLI_DSH_ACCOUNT` set to `1`, `true`, `yes` or `on` selects account
  mode. Account mode uses its own loader and writes a profile patch that
  enables the `deepseek-account` provider (`deepseek.py`).
- The child environment sets `DSH_HOME` to the harness home: an approved
  `NATIVE_CLI_DSH_HOME` that stays under the isolated home, or
  `<isolated HOME>/.dsh`.

None of this is a promise that a login is migrated. The isolation boundary is
the same for every adapter; what differs is whether the adapter looks for its
configuration in the isolated `HOME` at all, and whether that configuration is
already there when it starts.


## Legacy unisolated cloud lines

A ticket with no `worker_daemon_id` is an unisolated cloud worker. Switching it (`POST /api/v1/autonomy/tickets/{id}/switch`), delivering a staged handoff, resuming it, and reassigning a thread onto it now fail closed. Resume covers a follow-up on a ticket that already has a worker thread, and a launch effect for a queued ticket or a running ticket that has a thread and no run.

`refuse_unisolated_cloud_dispatch` in [`worker.py`](../../src/server/services/autonomy/worker.py) stores the current cloud-Worker refusal (the default `_CLOUD_WORKER_REJECTION_ERROR` while the OS-isolation switch is off) and sets `status` to `failed` when that snapshot is still an eligible cloud launch (integer `revision`, no daemon, not an orchestration record):

> Cloud Worker execution is disabled until this workspace has an OS-isolated worker runtime. Assign the Ticket to a configured local daemon.

That string is the ticket `error`. The call sites are line switch and the claim-path handoff in [`autonomy.py`](../../src/server/app/autonomy.py), input delivery in [`inputs.py`](../../src/server/services/autonomy/inputs.py), launch in [`worker.py`](../../src/server/services/autonomy/worker.py), and thread reassign in [`host.py`](../../src/server/services/autonomy/host.py). Switch, reply, and reassign re-read the ticket and retry the fence up to three times, because their own earlier write (or a concurrent one) bumps `revision` before `fail_unisolated_cloud_worker` runs. A switch that lands the fence returns the failed row. If every read stays eligible and the fence keeps losing, switch answers 503 and enqueues a launch effect that only refuses; reply stores the input for review and enqueues the same effect; reassign flags the moved inputs for review and enqueues it too. A daemon-bound ticket stays on the pull path: claim delivers a staged handoff, and follow-ups wait for the daemon. An open cloud ticket that is not an eligible launch (no integer revision, or a run already active) is not failed by the guard. The input is stored for manual review, and `deliver_input_effect` raises `PermanentEffectError` so the effect is not retried as a cloud resume. Reassign reports `input_delivery` so a caller can see that moved inputs will not be delivered.

### How to migrate

`update_ticket` does not accept `worker_daemon_id`, so an existing cloud row cannot be stamped onto a daemon. Continue the work on a ticket that is bound to a daemon when it is created:

1. Run `scripts/conductor_local_daemon.py` with a workspace policy and an isolation root, as in Local setup below. The daemon user owns the policy file (mode `0600`) outside the workspace and the isolation state.
2. Bind new work to that daemon. Set `worker_daemon_id` on the Source (`PATCH /api/v1/autonomy/sources/{id}`), on a schedule, or on a manual task (`POST /api/v1/autonomy/tasks`). The daemon claims those tickets and runs them inside the OS boundary.
3. Leave the legacy ticket failed with `_CLOUD_WORKER_REJECTION_ERROR`. Copy an input that was stored for review onto the new daemon ticket when that text should still be done. Switching, handing off, or resuming the old ticket calls the refusal again.
4. To move a thread, reassign it onto the daemon-bound ticket. The moved inputs stay pull-only. Reassigning onto another unisolated cloud ticket fails that target with the same error.

## Local setup

Create a trusted policy file outside workspace roots and isolation state, owned by the daemon user with mode `0600`. Store environment **names only**; the daemon reads the corresponding local values at launch. Use separate, non-overlapping workspace roots. The daemon creates per-workspace state and temporary directories with mode `0700`. Example with safe placeholders:

```json
{
  "<workspace-id>": {
    "root": "/absolute/path/to/workspace-a",
    "env": ["<PROVIDER_API_KEY_NAME>"],
    "runtime_read_paths": ["/absolute/path/to/trusted/provider-cli-runtime"]
  }
}
```

`runtime_read_paths` should contain only explicitly permitted CLI/runtime code directories, never credentials or a user home. Optional `localhost_tcp_ports` is a list of TCP ports on localhost that this workspace's worker may open. On macOS, leave it out to deny every loopback port, including Postgres, Redis, the backend, and the Docker API. On Linux the list is not enforced; the worker shares the host network namespace (see the contract above). The optional `user_info: true` policy flag is off when omitted and adds only the macOS `mach-lookup` permission for `com.apple.system.opendirectoryd.libinfo`, which ZCode needs to resolve the local username used for its account profile. Configure the local daemon with paths outside those workspaces:

```sh
.venv/bin/python scripts/conductor_local_daemon.py --provider <provider> \
  --workspace-policy /absolute/path/outside-workspaces/workspaces.json \
  --isolation-root /absolute/path/outside-workspaces/worker-state
```

The script defaults `--base-url` to `http://127.0.0.1:8000`; pass `--base-url <url>` when the server uses another address. If the repository has no `.venv`, invoke the script with the project's configured Python environment. The isolation root is taken from `--isolation-root` or `CONDUCTOR_ISOLATION_ROOT`; the daemon refuses to start when neither is set, and it refuses to start a workspace Worker when the policy fails validation. No database migration or web change is part of this implementation.

## Legacy reference and Conductor source mapping

The requirement was identified while comparing article §2 task-context isolation and the legacy survey's proposed worker boundaries. [`../SUPER-AGENT-ALIGNMENT.md`](../SUPER-AGENT-ALIGNMENT.md) records the article mapping; the read-only an internal survey of the legacy projects (not published) provided design context. The legacy work informed the requirement but contributed no copied implementation.

| Concern | Conductor implementation |
|---|---|
| Workspace to allowed filesystem/environment scope | Explicit local trusted JSON policy and profile resolution in [`isolation.py`](../../src/llms/native_cli/isolation.py) |
| Process and ACP launch containment | Shared wrappers in [`isolation.py`](../../src/llms/native_cli/isolation.py), [`process.py`](../../src/llms/native_cli/process.py), and [`acp.py`](../../src/llms/native_cli/acp.py); native provider adapters listed above |
| Unsupported or absent enforcement | Fail-closed policy/profile and OS sandbox checks in [`isolation.py`](../../src/llms/native_cli/isolation.py) |
| Cloud autonomy execution that cannot meet the contract | Explicit launch and resume refusal in [`worker.py`](../../src/server/services/autonomy/worker.py) |
| Preserve service/daemon task classes | Cloud autonomy Worker guard is scoped in `worker.py`; daemon-bound and orchestration tickets remain on their existing paths |

## Validation evidence

Focused validation passed: **1,921 passed, 20 skipped, 0 failed** (13.85 seconds; 101 warnings). The suite covered `tests/unit/llms`, `tests/unit/scripts/test_conductor_local_daemon.py`, `tests/unit/server/services/autonomy`, `tests/unit/server/services/native_runtime`, `tests/unit/server/database/test_autonomy*.py`, `tests/unit/server/test_autonomy_api.py`, `tests/unit/server/test_autonomy_daemon_followup.py`, `tests/unit/server/test_autonomy_ticket_replies.py`, and `tests/unit/server/test_import_layering.py`. The exact command was:

```sh
env -i PATH=/opt/homebrew/bin:/usr/bin:/bin PYTHON_DOTENV_DISABLED=1 HOME=/Users/you/conductor-wt/sa-isolation/.test-home TMPDIR=/Users/you/conductor-wt/sa-isolation/.test-tmp ../../Documents/deepseek-harness/default-workspace/AlphaCN/.venv/bin/python -m pytest tests/unit/llms tests/unit/scripts/test_conductor_local_daemon.py tests/unit/server/services/autonomy tests/unit/server/services/native_runtime tests/unit/server/database/test_autonomy*.py tests/unit/server/test_autonomy_api.py tests/unit/server/test_autonomy_daemon_followup.py tests/unit/server/test_autonomy_ticket_replies.py tests/unit/server/test_import_layering.py --basetemp=/Users/you/conductor-wt/sa-isolation/.test-tmp/root-final-verified -q --tb=short
```

Ruff passed for changed native CLI implementation, the daemon script, and the new isolation test. After the daemon docstring and this evidence record were updated, a supplementary docs/daemon check passed **61 tests** with `pytest tests/unit/scripts/test_conductor_local_daemon.py tests/unit/server/services/autonomy/test_alignment_docs.py -q --tb=short`. `git diff --check` passed. Linux `bwrap` argument construction is unit tested with a mock; a real macOS `sandbox-exec` smoke passed on a synthetic filesystem, including allowed read/write/temp access and denied sibling-workspace and symlink access. Network access remains enabled. This does not prove resistance to kernel exploits, host administrators, every provider CLI behavior, or production endpoint configuration.

No live provider, production host, remote service, GitHub, or database acceptance is claimed here. Cloud autonomy execution is deliberately unavailable until that runtime can provide an equivalent boundary. Ordinary interactive chat is unchanged.

## Review hardening (2026-11-03)

A follow-up review of the two isolation commits (`7ffc9b54e..HEAD`) reported four findings against [`isolation.py`](../../src/llms/native_cli/isolation.py) and the local daemon; all are fixed on this branch.

- **P1 — resolved temp paths escaped the denylist.** Runtime read paths (and workspace roots, which had no equivalent denylist at all) are `resolve()`d before checking, so on macOS `/tmp` became `/private/tmp` and passed; user homes and credential directories were never denied. A workspace at `/tmp` would also have shadowed the Linux `bwrap --tmpfs /tmp` with a read-write bind. `_unsafe_mount_root()` now rejects the resolved system temp trees, the daemon user home, credential directories beneath it, and the previous system roots, for both workspace roots and runtime read paths, and `worker_isolation()` applies the same checks to its `workdir` and runtime paths so a direct caller cannot bypass the policy gate.
- **P1 — overlap checks were case-sensitive string compares.** On a case-insensitive APFS volume two spellings of one directory (e.g. `sa-isolation` vs `SA-ISOLATION`, verified to be the same inode) were treated as disjoint and both would get a read-write mount. `_paths_overlap()` now decides by inode identity (`os.path.samefile`) when both paths exist and falls back to case-folded spelling only on volumes that report case-insensitive (`os.pathconf(..., PC_CASE_SENSITIVE)`).
- **P1 (re-review) — the case-insensitivity probe failed open.** `PC_CASE_SENSITIVE` is not in `os.pathconf_names` on macOS, so `os.pathconf` raised `ValueError` (and `OSError` for not-yet-created paths); the handler returned "case-sensitive", the case-fold branch never ran, and case-variant parent/child paths with distinct endpoints were treated as disjoint (`…/proj` vs `…/PROJ/nested`, state root `…/WS/state` vs workspace `…/Ws` before `mkdir`). The probe is now a direct volume test: walk to the nearest existing ancestor and check whether a case-swapped spelling names the same file (device, inode); unclassifiable volumes fail closed as case-insensitive, which only denies the launch. Case-variant spellings of the forbidden temp trees, system roots, and the daemon home are rejected the same way.
- **P1 (re-review) — credential check ignored ancestors.** The denylist matched a credential directory name only among the path's own components, so `~/Library` was accepted as a workspace root and runtime read path even though it carries `~/Library/Keychains`; `~/Library/keychains` and `~/.SSH` also slipped through on a case-insensitive volume. `_unsafe_mount_root()` now denies any ancestor of a known credential store (`_CREDENTIAL_TARGETS`, including `Library/Keychains`) and matches credential names case-folded on volumes that probe case-insensitive.
- **P2 (re-review) — case-variant tests only exercised what `samefile` already caught.** Same-inode endpoint equality and canonical `HOME`/`~/.ssh` spellings stayed green while both bypasses above passed. Six new tests cover distinct-inode nested case-variant roots, a case-variant state root checked before `mkdir`, case-variant system temp/system root spellings, credential ancestors (with and without the store present), case-variant credential and home names, and `worker_isolation` runtime paths.
- **P2 — the policy mode/owner check was not tied to the bytes parsed.** The file was `stat()`ed and then re-opened by path for reading. The policy is now opened once with `O_NOFOLLOW` and `fstat()`ed on that descriptor, so a swap between check and read cannot pass an unchecked file.
- **P2 — the daemon silently defaulted an empty `--isolation-root`.** An empty flag fell back to `~/.conductor/worker-isolation` and ignored `CONDUCTOR_ISOLATION_ROOT`. The daemon now resolves the root from the flag or the environment variable and refuses to start (exit 2) when neither is set.

Regression coverage: 7 new tests in [`test_native_cli_isolation.py`](../../tests/unit/llms/test_native_cli_isolation.py) (temp-tree runtime paths, home/credential mounts for roots and runtime paths and direct `worker_isolation`, case-variant root overlap and case-variant state-root overlap, foreign-owner policy rejection, Linux `bwrap` keeps `--tmpfs /tmp` unshadowed) and 2 new tests in [`test_conductor_local_daemon.py`](../../tests/unit/scripts/test_conductor_local_daemon.py) (startup refusal without an isolation root, flag-beats-env resolution). The focused suite below passed: **1,930 passed, 20 skipped, 0 failed**, and Ruff is clean on the changed files.

Re-review (2026-11-03, same day): the re-review of the round above returned BLOCK with 2×P1 + 1×P2 (log: `/Users/you/conductor-wt/logs/grok--sa-isolation--rereview-sa-isolation.log`); all three are fixed by the same-day commit noted in [`sa-isolation.md`](sa-isolation.md). The re-review confirmed the four earlier findings fixed. The re-run focused suite passed **1,936 passed, 20 skipped, 0 failed** (+6 tests); each new test fails when the fix in `isolation.py` is reverted. Ruff is clean and `git diff --check` passes.

Third review (2026-11-03, rr3): BLOCK with 1×P1 + 3×P2 (log: `/Users/you/conductor-wt/logs/codexrev--sa-isolation--rr3-sa-isolation.log`); all four are fixed and recorded in [`sa-isolation.md`](sa-isolation.md). In short: the case-insensitivity probe now walks past ancestor names that case-swap leaves unchanged (numeric directory names reported "case-sensitive" on APFS and admitted case-variant/nested workspace roots and isolation state); the macOS Seatbelt profile grants the private temp directory read so written temp files can be read back; output redaction applies only to credential-named env values (`NATIVE_CLI_DSH_ACCOUNT=1` no longer corrupts numeric JSON usage fields); and `launch_worker_effect()` re-reads and retries the CAS-guarded cloud-launch rejection when a concurrent input bumps the ticket revision, raising for a nack (backoff retry) instead of acknowledging an unsettled launch. Nine regression tests were added; the focused suite passed **1,945 passed, 20 skipped, 0 failed**, each new test fails on revert, and Ruff plus `git diff --check` are clean.
# Workspace sharing limits

Native Tickets that use the same workspace can see each other's uncommitted
changes while their Workers run. The per-Ticket delivery check prevents a
Worker from silently completing with newly introduced uncommitted changes; it
does not give each native Ticket a private checkout. Task 6 line worktrees
isolate dispatch lines within one dispatch only. They do not isolate separate
native Tickets that share a workspace.

- **N-a — `GitMerger.push` still uses host Git.** It must be hardened before
  production callers are connected. The current line-worktree isolation does
  not make this push path safe for production use.
- **N-b — operator Git invocations share the common config.** An operator who
  runs Git in `repo_root` can execute configuration that a Worker planted in
  the common Git config, such as an `include` directive that loads a
  `core.fsmonitor` command. The line-worktree boundary does not protect these
  operator invocations.
