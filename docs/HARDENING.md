# Hardening baseline

The local attack surface this repository starts with, and the operator-facing
reasons it gives when a boundary refuses something. Five things are pinned
here: where the backend listens, which server keys a device trusts, what a
BYOM daemon may run on its device, how much of that device a run may reach
once it starts, and (section 8) how the host runs Git against a workspace a
Worker has written.

Nothing here changes what a *deployment* chooses to do; it changes what the
defaults are and how loudly a refusal explains itself.

Every section below names the file and the symbol that implements the
behaviour, in backticks. Section 6 explains how those names are checked, so a
claim that has rotted cannot stay quiet here. That check proves only that the
named files, symbols, options and environment names exist; it does not prove
that the code behaves the way the sentence around them says. The behaviour
claims were checked by reading the code, and the tests listed in sections 7
and 8 are what hold them.

## 1. The backend and the web console listen on loopback

`scripts/cond` is the operator's wrapper; it runs `src.conductor_cli.ops_cli`,
and every start path the CLI owns binds `127.0.0.1`, with the host passed
explicitly rather than inherited from a tool's default:

| Path | What it starts |
|---|---|
| `src/conductor_cli/ops_support.py` | `load_settings` sets `Settings.backend_host` and `Settings.web_host` to `127.0.0.1`; `Settings.base_url` builds the probe URL from them, and every `RealSystem.port_open` and `RealSystem.http_get` call site in the CLI probes a loopback address |
| `src/conductor_cli/ops_runtime.py` | `ops_runtime.backend_argv` starts `server.py --host 127.0.0.1`, `ops_runtime.web_argv` starts Vite preview or the static console on `127.0.0.1`, and `ops_runtime.daemon_argv` points the daemons at `Settings.base_url` |
| `src/conductor_cli/operations.py` | `operations.HEALTH_URL` is the health probe, and the deploy and rollback plans start `server.py --host 127.0.0.1` |
| `src/conductor_cli/ops_static.py` | `ops_static.main` refuses any host but `127.0.0.1` (see the note below on what it serves) |
| `src/conductor_cli/ops_upgrade.py` | `ops_upgrade.upgrade` restarts through `ops_runtime.restart_all` rather than spawning its own server |
| `src/conductor_cli/ops_fleet.py` | `ops_fleet.render_fleet_toml` records `host = "127.0.0.1"` for both components, and `ops_fleet.load_fleet` reads only ports and the web mode back from that file, never a host |
| `deploy/launchd/conductor.plist.in` | the launchd unit runs `src.conductor_cli.ops_cli up --foreground`, which takes the loopback path above |

The one deliberate wildcard is a container: `deploy/Dockerfile.backend` and
`deploy/Dockerfile.dev` pass `--host 0.0.0.0` because the process is reached
through Docker's port mapping, not from the host network. That is a stated
choice at the call site, not a default. `server.py` itself binds `127.0.0.1`
by default; `--host` or `CONDUCTOR_BIND_HOST` changes it, and a non-loopback
host logs a warning at startup. Every path the CLI owns passes the loopback
host explicitly, so it does not depend on that default.

CORS starts with only the local console origins (`http://127.0.0.1:5170` and
`http://localhost:5170`) in `config.yaml`. A deployment whose console lives
elsewhere lists its origins there, or sets `CONDUCTOR_CORS_ORIGINS`
(comma-separated) to replace the list; `src/config/settings.py:get_allowed_origins`
is where that override is read. CORS allows credentials, so `*` is refused
there with a startup warning: an override containing it is ignored, and a
configured `*` is dropped (falling back to the local console origins).

`CONDUCTOR_BACKEND_PORT` and `CONDUCTOR_WEB_PORT` (or the ports in the fleet
file) still move the ports; there is no environment variable or fleet-file
field that changes the host. `src/conductor_cli/ops_support.py:load_settings`
is where that is decided.

The static console is a loopback bind, not a file-serving boundary.
`ops_static.main` hands the resolved build directory to the standard
library's `SimpleHTTPRequestHandler` and adds no containment check of its own,
so a symlink inside that directory is followed to wherever it points. Keep the
build directory free of links you would not want served on `127.0.0.1`.

## 2. Which server keys a device trusts: origin verification

A lease is only as good as the key that signed it, so the device keeps its
own list of the server public keys it accepts
(`src/conductor_cli/daemon_grant.py`):

| Symbol | What it holds |
|---|---|
| `daemon_grant.DEFAULT_TRUSTED_KEYS` | the device's trusted-keys file, `~/.config/conductor/daemon_trusted_keys.json` |
| `daemon_grant.TrustedKeys` | the loaded key set; `TrustedKeys.load` resolves the path once, then reads it through `daemon_grant.read_owned_text`, which refuses anything but a regular file owned by the daemon user and not writable by group or others, and `TrustedKeys.public_key` picks one key out of it |
| `daemon_grant.DEFAULT_NONCE_FILE` | the replay record, `~/.local/state/conductor/daemon-grant-nonces.json` |
| `daemon_grant.NonceStore` | the replay record itself; `NonceStore.seen` asks, `NonceStore.consume` records |
| `daemon_grant.OriginCheck` | the pair the daemon actually holds: the keys and the nonce store |

When trusted keys are loaded, before a claimed Ticket runs,
`daemon_grant.verify_grant` checks the signature against those keys and
`daemon_grant.bind_grant` checks that the grant was minted for this ticket,
this lease and this policy version. Every failure is a
`daemon_grant.GrantError`, and `daemon_grant.refusal_reason` turns it into a
distinguishable refusal: a grant that was not signed, one that is malformed,
one whose signature does not verify, one that has expired, one bound to
another ticket, lease or policy version, one that has already been seen, and
a replay record that cannot be confirmed.

Rotation is additive, and it happens in the trusted-keys file the device
actually loads — `daemon_grant.DEFAULT_TRUSTED_KEYS`, or the path given with
`--trusted-keys`: list the new key beside the old one there and remove the old
one once no lease names it. `config/daemon_trusted_keys.example.json` only
shows the file's shape; editing it changes nothing a daemon reads.

The daemons started by `src.conductor_cli.ops_cli up` do **not** name the
trusted-keys file or the daemon policy on their command line —
`ops_runtime.daemon_argv` passes only the backend URL, the provider, the
daemon id, the workspace policy, the isolation root and the working
directory — so on that path the daemon falls back to
`daemon_grant.DEFAULT_TRUSTED_KEYS` and `daemon_policy.DEFAULT_POLICY_PATH`.
`Settings.trusted_keys` and `Settings.daemon_policy` (from
`CONDUCTOR_DAEMON_TRUSTED_KEYS` and `CONDUCTOR_DAEMON_POLICY`) only move what
`cond doctor` inspects, not what those daemons load. `conductor daemons start`
(`src/conductor_cli/daemon.py`) accepts explicit `--trusted-keys` and
`--policy` paths instead.

## 3. What a BYOM daemon may run: the device's daemon policy

A local daemon (`scripts/conductor_local_daemon.py`) keeps its own authority
boundary in `~/.config/conductor/daemon_policy.yaml`
(`src/conductor_cli/daemon_policy.py`): the operations it recognises, which of
them each provider may run, which of them only read, and a version a lease is
bound to. The cloud never relaxes the file — it only learns the version, so a
lease granted under a policy the device has since replaced is refused before
the native CLI runs. The example is `config/daemon_policy.example.yaml`.

| Symbol | What it does |
|---|---|
| `daemon_policy.DEFAULT_POLICY_PATH` | the device's policy file, `~/.config/conductor/daemon_policy.yaml` |
| `daemon_policy.load_policy` and `daemon_policy.parse_policy` | read the file, and validate the parsed YAML into a `daemon_policy.DaemonPolicy` whose version is a hash of the normalised rules |
| `daemon_policy.PolicyStore` | the store the daemon holds; `PolicyStore.load` reads the file in one snapshot, and `PolicyStore.current` re-reads it only when the file has changed |
| `daemon_policy.DaemonPolicy` | the loaded policy; `DaemonPolicy.evaluate` judges a request, `DaemonPolicy.evaluate_operations` judges an already-classified set, and `DaemonPolicy.is_read_only` decides whether the run may change the device |
| `daemon_policy.unclassified_executable_segments` | the executable text in a request the rules do not classify |
| `daemon_policy.executable_request_rejection` | the refusal that text produces |

The policy is read like authority state, not like configuration, but it is
not a symlink-free path. `PolicyStore.__init__` resolves the configured path
once and pins the result, so a configured path that is itself a symlink, or
sits under a symlinked directory, is followed to its target and accepted;
from then on the daemon reads, re-stats and authorises only that pinned
location, so re-pointing the link later does not move it. What is checked is
the pinned file: `daemon_policy._read_policy_snapshot` opens it with
`O_NOFOLLOW` (so a symlink swapped in at the pinned location is refused),
takes mode and owner from the descriptor it then reads — mode `0600` or
stricter, owned by the daemon user — and retries if the path stops naming the
file it opened, so the bytes that are parsed are the bytes that were checked.
`daemon_policy.load_policy`, called with a path directly, additionally refuses
a final path component that is a symlink. A `daemon_policy.PolicyError` says
which check failed.

Per Ticket, the daemon also refuses to use a policy, trusted-keys file or
replay record that sits inside anything the Worker can write — the
workspace, the isolation root, or an extra write path the workspace policy
names (`conductor_local_daemon._policy_location_rejection` and
`conductor_local_daemon._origin_location_rejection`).

`conductor_local_daemon._open_policy_store` loads the policy *before* the
daemon spawns anything, so a file the device cannot use is a start failure
with the reason, not a daemon that quietly runs without its boundary:

```
Local daemon policy is unusable: the daemon policy at
/Users/me/.config/conductor/daemon_policy.yaml must have mode 0600 or stricter
```

A device with no policy file at the default path still runs, without that
boundary: the daemon says so on stderr at startup, and `--require-policy` (or
an explicit `--policy` path that does not exist) turns that into a hard
refusal.

## 4. How much of the device a run may reach: the workspace isolation policy

The daemon policy says which operations may run. The workspace policy says
how much of the device a run may touch once it starts. It lives at
`~/.conductor-daemon/workspaces-cli.json` and is read by
`src/llms/native_cli/isolation.py:read_workspace_policy`.

Each workspace entry names the root the run works in, the environment names
it may see, and the extra paths it may read or write. The file is authority
state too, and is checked like it:

- it may not be a symlink, and no parent of it may be a symlink either;
- it must have mode `0600` or stricter, and be owned by the daemon user;
- the descriptor that is parsed is the descriptor that was checked, so a
  swap between the check and the read cannot pass an unchecked file;
- a workspace root may not follow a symlink, may not be an unsafe mount
  root, and must exist;
- two workspace roots may not overlap;
- the policy file and the isolation state root must both sit outside every
  workspace root.

`isolation.worker_isolation` then builds the boundary for one Ticket: a
private home and a private temporary directory, a workspace the run may read,
an explicit environment allowlist, and a state root that has to be disjoint
from the workspace. A run the policy declares read-only may not hold a write
grant to the workspace by any route, including an extra write path the
workspace policy names.

One of these boundaries is macOS-only, and the module says so rather than
implying otherwise. On macOS the Seatbelt profile denies loopback TCP
unless the workspace policy lists that port, so the Conductor backend on
`8000`, Redis on `6379`, Postgres on `5432` and the Docker API on `2375` and
`2376` stay closed by default. On Linux there is no such deny: the worker
shares the host network namespace and `localhost_tcp_ports` is ignored. The
path checks above, including the overlap check (`isolation._paths_overlap`),
apply on every platform.

`src/conductor_cli/ops_support.py` resolves the two paths the daemon needs —
`Settings.workspace_policy` from `CONDUCTOR_WORKSPACE_POLICY` and
`Settings.isolation_root` from `CONDUCTOR_ISOLATION_ROOT` — and
`ops_runtime.daemon_argv` passes both to the daemons it starts;
`ops_runtime.start_daemon` refuses to start one when the workspace policy file
is missing. A run whose
workspace has no usable isolation policy is refused before the native CLI
starts:

```
Local daemon has no valid OS isolation policy for this workspace.
```

## 5. Where the operator sees a refusal

| Situation | Where the reason appears |
|---|---|
| Policy file missing at daemon startup | the daemon's stderr, or a hard stop with `--require-policy` or an explicit `--policy` |
| Policy file present but unusable | the daemon's stderr: `Local daemon policy is unusable: ...` |
| Trusted-keys file missing | the daemon's stderr, or a hard stop with `--require-origin` or an explicit `--trusted-keys` |
| Trusted-keys file present but unusable | the daemon's stderr: `Trusted server keys are unusable: ...` |
| A lease bound to a policy version the device has replaced | the Ticket's failed report summary, naming both versions, before the native CLI runs |
| A policy file that became unusable while the daemon runs | the Ticket's failed report summary |
| A Ticket the daemon policy refuses | the Ticket's failed report summary |
| An operation capability bound to a policy version that no longer matches | the Ticket's failed report summary |
| A policy, trusted-keys file or replay record inside a Worker-writable path | the Ticket's failed report summary |
| A run with no usable workspace isolation policy | the Ticket's failed report summary |
| A live stream whose policy gate blocks the text | the stream stops with `policy_refused` |
| A policy, trusted-keys or workspace-policy file that is absent, a symlink, not a regular file, or group/other accessible | `cond doctor` reports it as `missing`, `not a regular file` or `mode is too open` |
| A daemon that dies during startup | `conductor daemons start` reports it and names the log |

`cond doctor` (`src.conductor_cli.ops_cli doctor`, implemented in
`src/conductor_cli/doctor.py`) checks the daemon policy, the trusted keys and
the workspace policy at the `Settings` paths with
`src/conductor_cli/doctor.py:_file_check`. That check is limited to whether
the file exists, whether it is a regular file rather than a symlink, and
whether its mode leaves group and other bits clear. It does not check the
owner and does not parse the contents, so a file the daemon would refuse —
owned by someone else, or not a valid policy — can still report `ok`. The
daemon's own startup message is the authority on whether it can use the file.

## 6. How this document is kept honest

A hardening doc that names a file or a symbol is a promise to the reader:
"the loopback bind lives in `src/conductor_cli/ops_runtime.py`". The promise
rots silently — a module gets renamed, a helper moves, a policy file is
dropped — and the document keeps reading as if it were still true.

`scripts/check_hardening.py` walks the backticked references in this file and
resolves each one against the working tree:

- a directory-qualified path must exist;
- a file-anchored symbol must be defined at module level in that file;
- a dotted symbol must resolve to a module attribute or a class member;
- a bare name written as a call (`backend_argv()`) must be a function or class some
  module or class in the tree defines;
- a line anchor (a file followed by `:12`, `:12-20` or `#L12`) must name a file
  that has those lines;
- an environment name such as `CONDUCTOR_WORKSPACE_POLICY` or
  `GIT_CONFIG_NOSYSTEM`, and an option such as `--trusted-keys`, must be
  spelled as a string literal in first-party, non-test Python code;
- a single word that reads as code — snake_case such as `load_settings`, or
  CamelCase — must be a name some non-test module or class defines, assigns
  or imports, a module of that name, or a string constant the code spells
  (a status such as `policy_refused`); an ordinary English word stays prose;
- a Markdown link to a repository file must point, relative to this document,
  at a file that exists, and a line anchor on it must be a line that file has.

A span with spaces in it is checked word by word, so a renamed module inside
a command line still fails. Fenced blocks are skipped, and a fence that never
closes is itself an error, because it would hide every claim after it.

A reference proves that a name exists, not that the sentence around it is
true; the claims themselves were checked by reading the code they name.

Resolution is purely static: the target files are parsed with `ast` and never
imported, so the check cannot run project code, cannot start a server, and
cannot be defeated by an import side effect. A reference resolves only through
a *binding* — a name the module actually binds, an import it actually
performs, a base class it actually inherits. A sibling file that happens to
share a name is not a binding, so the check cannot be satisfied by a file
that was never wired in.

Run it with:

```
uv run python scripts/check_hardening.py
```

It exits `0` when every reference resolves, `1` when one does not — printing
the line, the reference and the reason — and `2` when the document itself
cannot be read. `tests/unit/scripts/test_check_hardening.py` covers the
resolver, and also checks this document against this tree.

## 7. Tests

- `tests/unit/conductor_cli/test_daemon_policy.py` — the policy grammar, the
  authority checks on the file, the executable-text rules, and the shipped
  example.
- `tests/unit/conductor_cli/test_daemon_grant.py` — grant signing, expiry,
  binding and replay.
- `tests/unit/conductor_cli/test_daemon_capability.py` — the operation
  capability and the decision it authorises.
- `tests/unit/server/test_autonomy_daemon_grants.py` and
  `tests/unit/server/test_autonomy_daemon_lease_policy.py` — the server
  side of a grant and of a lease's policy binding.
- `tests/unit/test_cond_ops.py` — the `cond` lifecycle, and `cond doctor`'s
  policy-file checks.
- `tests/unit/scripts/test_check_hardening.py` — the reference resolver and
  this document.

## 8. Git delivery checks: how the host runs Git on a Worker's workspace

A delivery verdict is read from Git, and the workspace Git reads was written
by the Worker, so the Worker must not be able to configure what Git does
during the check. `src/server/services/autonomy/git_delivery.py` builds every
Git command in one place:

- `git_delivery.hardened_git_invocation` returns the argv and environment of
  every Git command a check, a snapshot or a mutation runs. The argv carries a
  fixed set of `-c` pins (`git_delivery.git_pinned_config_args`) ahead of the
  command; the command scope outranks the repository's own config, so hooks,
  fsmonitor, signing programs, exclude and attribute files, and the
  worktree-vs-index comparison settings cannot be chosen by the workspace. The
  environment sets `GIT_CONFIG_NOSYSTEM` and points `GIT_CONFIG_SYSTEM` and
  `GIT_CONFIG_GLOBAL` at `/dev/null`, so no system or global config is read,
  and disables terminal prompts and lazy fetches.
- `git_delivery.is_hardened_git_invocation` is how a runner that executes Git
  elsewhere confirms an invocation came from that builder: the pinned prefix
  must be intact, no later `-c`, `--config-env` or `--exec-path` may follow
  it, and the environment must hold exactly the builder's keys and values plus
  neutral driver overrides, so `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`,
  an external diff program or any other Git variable is refused. A runner
  refuses anything else rather than patching it.
- The include gate: Git has no switch that turns include.path and
  includeIf off for an ordinary command, so before the first real Git
  command every public entry point (`git_delivery.run_isolated_git_readonly`,
  `git_delivery.capture_git_snapshot`, `git_delivery.git_workspace_summary`,
  `git_delivery.run_isolated_git_mutation`) lists the local and worktree
  config keys, unexpanded, and refuses a workspace whose config has any
  include directive. The check then reports its status as unknown.
- Drivers: every filter, merge and diff driver the repository's config names
  (`git_delivery.GitDrivers`) gets its programs emptied (filter clean, smudge
  and process with required off, merge driver, diff textconv and command),
  through command-scope config in the environment, so no driver program runs
  during a check or a mutation; `git_delivery.is_neutral_driver_override` is
  the only extra config slot a hardened invocation may carry. Replace refs are
  off (`GIT_NO_REPLACE_OBJECTS`).
- Hidden changes: a workspace whose index marks entries assume-unchanged or
  skip-worktree, sets a sparse checkout or a non-empty info/attributes, or
  whose core.worktree names any tree other than the workspace itself (a
  submodule's gitdir naming its own checkout is normal), reports unknown, and
  so does one holding an ignored, untracked .gitattributes in a directory that
  has tracked files (`git_delivery._refuse_hidden_attribute_files`; the
  snapshot streams the index and runs the same check through
  `git_delivery._refuse_hidden_attribute_dirs`), because
  Git reads that file even when it is ignored. A .gitignore or .gitattributes
  that is untracked or modified and predates the run also makes the summary
  unknown. Files hidden only by info/exclude or by a self-ignoring directory,
  including files inside an ignored directory, are listed as uncommitted;
  more than 10,000 such files make that one check unknown.
- Tool caches: untracked or ignored files a tool cache really holds, however
  they are ignored, are not uncommitted work and do not count toward that
  limit (`git_delivery.TOOL_CACHE_DIRECTORY_NAMES`): `*.pyc` files directly
  inside a `__pycache__`; files that are not code inside a `.pytest_cache`,
  `.ruff_cache` or `.mypy_cache`; anything inside a `.venv`, `venv` (with its
  pyvenv.cfg), `.tox` or `node_modules` at the workspace top level or beside
  the manifest that owns it (a pyproject.toml, requirements.txt, tox.ini or
  package.json, for example), and a nested `.venv` that has its pyvenv.cfg.
  Anything else under a cache-named directory - a forged .py, .pth or .js file
  - is work, and a `.gitignore` inside a cache does not hide it. What a real
  cache may hold is still not inspected: a forged .pyc beside its source, or
  a .pth inside the top-level `.venv`, reads as a cache.
  `capture_git_snapshot` lists the caches in `GitSnapshot.tool_caches` and
  keeps only their hashes in a record; a summary that reports uncommitted work
  also lists the caches the run created, apart from the work.
- Submodules: the status reports a submodule whose checked-out commit moved
  (it ignores only dirty submodules), and the snapshot fingerprints the commit
  a checkout's HEAD names, read as files, so a second move is a change too.
  Edits inside a submodule's own worktree are not seen, since reading them
  would run Git under the submodule's own configuration.
- Mutations: `git_delivery.run_isolated_git_mutation` accepts only an
  allowlist of worktree, branch-rename and merge forms (never a push), runs
  them with the network disabled, and grants write access only to the
  repository's common Git directory, the one the configured repository root
  and the target worktree are both verified to share.

The Docker runtime (`src/server/services/autonomy/docker_worker_runtime.py`)
does not harden Git on its own. `docker_worker_runtime._docker_git_runner`
runs inside the Worker container only an invocation that passes
`git_delivery.is_hardened_git_invocation`, refuses any host path it cannot
map into the container mount and any driver override that is not inert, and
never falls back to the host. `docker_worker_runtime._container_git_available`
probes the image's Git with an invocation built by
`git_delivery.hardened_git_invocation`. Its outcome only chooses the reason
text: a Docker Worker never reports a delivery verdict, and its report
carries `git_check: unsupported_runtime`.

Tests: `tests/unit/server/services/autonomy/test_git_delivery.py`,
`tests/unit/server/services/autonomy/test_git_delivery_n13.py`,
`tests/unit/server/services/autonomy/test_git_delivery_rv12.py`,
`tests/unit/server/services/autonomy/test_git_delivery_bounded.py`,
`tests/unit/server/services/autonomy/test_git_delivery_cap_followup.py`,
`tests/unit/server/services/autonomy/test_git_delivery_r4_2_n56_merge.py`,
`tests/unit/server/services/autonomy/test_docker_worker_runtime.py` and
`tests/unit/server/services/autonomy/test_docker_worker_runtime_n13.py`.
