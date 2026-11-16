# Cloud autonomous Workers: Docker OS-isolated runtime

Status: implemented. The explicit switch now selects a real Docker-backed
OS-isolated runtime; cloud Workers stay refused by default and can only launch
after the switch is deliberately enabled and the runtime verifies. Local
daemons and ordinary threads are unchanged. The runtime has been run against a
real Docker engine with a stub Worker CLI and, for verification and a
credential-less launch, against the real `codex-cli` image; **no real vendor
(provider) call has been made through it** (see "What this does not prove").

See [`ports/worker-isolation.md`](ports/worker-isolation.md) for the local
daemon boundary this runtime mirrors, and `src/server/services/autonomy/`
[`docker_worker_runtime.py`](../src/server/services/autonomy/docker_worker_runtime.py)
/ [`cloud_runtime.py`](../src/server/services/autonomy/cloud_runtime.py) for
the implementation.

## Contract

An autonomous Ticket with no `worker_daemon_id` is a **cloud Worker**. It
normally would run through the shared PTC/native runtime, which has no
per-workspace OS boundary comparable to the daemon's profile-enforced sandbox.
The switch in [`cloud_runtime.py`](../src/server/services/autonomy/cloud_runtime.py)
maps a configured runtime id to a registered **verifier** and **launcher**
pair:

- **Default off.** Unset, empty, `none`, `off`, or `disabled` requests no
  isolated runtime. The existing default refusal is unchanged.
- **Verified, not merely enabled.** Any other value names a runtime id. A
  runtime is usable only when a provider registered a verifier and a launcher
  and the verifier proves the boundary is available. This build registers
  `docker` with both.
- **Fail closed.** A missing/invalid runtime id, a failed verifier, an
  unavailable engine/image/CLI, a launch exception, a timeout, or a non-zero
  worker exit settles the Ticket failed. There is no fallback to the shared
  PTC runtime.

No secret handling lives in the switch: it names a runtime, never a credential.

## The Docker runtime

Each launch runs one vendor-native Worker CLI one-shot in a **fresh disposable
container** against a private host staging copy of the Ticket's workspace:

- `docker run --rm --name conductor-cloud-worker-<id>` — a new container per
  attempt (`<id>` is 16 hex characters). `--rm` plus a `docker rm -f` that runs
  after the client returns, times out, fails or is cancelled covers a killed
  client or timeout. That removal is retried (three attempts) and confirmed; if the
  container still exists afterwards the failure is logged at `ERROR` and the
  container's expiry label (below) hands it to the sweep. **When the run
  fails, the Ticket error says so, whichever way it failed** (a timeout, a
  non-zero exit, a result that could not be read or published, an unexpected
  error): `...; container conductor-cloud-worker-<id> could not be removed and
  is left for the expiry sweep`, in fixed words, once. A run that succeeded
  has no error to carry it, so there the survivor is only in the `ERROR` log.
- **Expiry label and sweep.** Every container carries
  `conductor.cloud-worker.deadline=<epoch>`: the Worker's wall-clock limit plus
  900 s of slack. A launch that has verified the runtime then sweeps, at most
  once a minute per process: containers past their deadline are removed, and so
  are entries in the staging root that a killed backend left (workspace copies
  `<ticket>-<12 hex>`, env files `env-<32 hex>.env`, leftover `probe-*`
  directories), as described next.

  **The sweep deletes only what this runtime created.** A name of the right
  shape and an age do not show that: a staging root that is shared holds other
  things (`notes-0123456789ab/` is a name the sweep recognises). So every
  entry the runtime creates gets a marker beside it in the staging root,
  `<entry>.owner`: made first, with `O_EXCL`, never through a link, `0600`,
  holding a header, the entry's name and a random token. The marker is in the
  private root, not in the bind-mounted workspace, so a Worker cannot write,
  forge or delete one. The sweep considers an entry only if its marker is a
  regular file of the backend's user with no group or other permission bits,
  of at most 256 bytes and naming exactly that entry, and the entry is a directory (for a workspace copy or probe) or a
  file (for an env file) of that user, not a link. An entry without a valid
  marker is never touched, however old. The marker is removed with its entry; a
  marker whose entry is gone is removed once it is old enough that no launch
  can be about to create the entry. The unit tests cover the unmarked lookalike,
  forged, linked, group-readable, other-user and out-of-place markers, and a
  marker a Worker plants inside its workspace; the live smoke checks an old
  unmarked lookalike is left.

  **Migration.** Entries left by a version that wrote no markers are not swept
  and not deleted by age: this runtime cannot tell them from anyone else's. After
  upgrading, remove them by hand once you have checked that no Worker uses them
  (`docker ps -a --filter label=conductor.cloud-worker.stage --format
  '{{.Label "conductor.cloud-worker.stage"}}'` prints the names of the entries
  that still have a container): look in the staging root
  (`AUTONOMY_CLOUD_WORKER_ROOT`, default `~/.cache/conductor/cloud-workers`) for
  directories named `<ticket>-<12 hex>` and `probe-*` and files named
  `env-<32 hex>.env` that have no `.owner` file beside them, and delete the ones
  that are yours (a tree a Worker made unreadable needs `chmod -R u+rwx` first).

  **A tree is judged by its own container, not by the clock.** Every container
  also carries `conductor.cloud-worker.stage=<staging entry name>`, and after
  the expiry pass the sweep asks the engine which containers exist, in any
  state, for staging entries. A workspace copy goes only if no container for it
  exists; one whose container is still there stays where it is, with a warning,
  however old it is and whatever the timeout is configured to now (an earlier
  Worker's 3600 s limit does not stop being its own because the operator has
  since set 10 s). If the listing fails, or an expired container survived
  `rm -f`, nothing proves a Worker is gone and no tree is touched. The only
  clock left is a fixed age, twice the 900 s slack (1800 s, taken from the
  entry's `st_ctime`), and it is there for the
  launch that has created its staging directory but not yet its container
  (materializing a workspace takes minutes and has no container to ask about):
  an entry younger than that is never swept, whatever else is true. Env files
  have nothing to walk and are removed on age alone. Without a later launch
  nothing runs the sweep; see "Crash orphan" below.

  By default the sweep is engine-wide: it removes every container on the
  engine that carries the deadline label and has passed it, which is right for
  a backend that owns its engine. "Every" is bounded by two more tests in
  `_reap_expired`: the container's name must be one of the runtime's own shape
  (`conductor-cloud-worker-` followed by 1 to 81 lowercase letters, digits or
  hyphens, the first not a hyphen), and the label value must parse as a number;
  a labelled container that fails either is skipped, not removed.
  `AUTONOMY_CLOUD_WORKER_SWEEP_LABEL=key=value`
  (unset by default; it narrows the sweep and never widens it) restricts it to
  containers that also carry that label. The daemon is asked for them, and the
  label value in each row it returns is checked again before anything is
  removed. The runtime never adds that label to its own containers: the live
  smoke sets it to its per-run label and adds the label itself, through a
  `docker` wrapper, because it shares an engine with real Workers. A
  production backend should leave it unset: with it set, expired Workers
  without the label are never reaped. It narrows only the container pass; the
  staging pass is bounded by the ownership markers in the staging root, and its
  question "does a container still exist for this tree" is asked of every
  container on the engine, labelled or not.
- **Workspace staging bind mount.** The private staging tree for the Ticket is
  mounted at `/workspace` (read-write, or read-only for a speculative run).
  The mount option is CSV, so a staging path containing a comma or a quote is
  quoted rather than interpolated. Apart from explicit operator opt-in mounts,
  nothing from the host is mounted by the runtime: no home, no repo, no Docker
  socket. Operator mounts are validated for shape only (at most 8; a target
  under `/workspace`, `/proc`, `/sys`, `/dev`, `/etc` or `/run`, `/var` itself
  and `/var/run` are reserved): those targets and any path that *names*
  `docker.sock` are refused, but the
  validator does not look inside a directory, so a parent of the socket
  (`/var/run`, a home directory, `/`) is accepted and exposes whatever it
  holds. Mount only narrow, read-only credential directories.
- **No host environment or credentials by default.** The container gets only
  `HOME`/`TMPDIR`/`XDG_CONFIG_HOME` plus names the operator explicitly lists in
  `AUTONOMY_CLOUD_WORKER_ENV` (host values copied; a listed variable that is
  unset or empty in the backend's environment is skipped, not an error) or
  `AUTONOMY_CLOUD_WORKER_SET_ENV` (literal values). Each list takes at most 32
  names, and a value with a newline, carriage return or NUL fails the launch
  when the env file is written. Names beginning with
  `CONDUCTOR_` are refused so server credentials cannot be forwarded.
  Credentials are an explicit operator decision, never a default. Forwarded
  values travel in a private `--env-file` (mode `0600`) so they never appear in
  the host process argument list. The runtime sets
  `HOME`/`TMPDIR`/`XDG_CONFIG_HOME` itself, and `PATH` is the image's own, so
  those four names are refused in the
  allowlists, and operator mounts may not target `/workspace` or any reserved
  system path. (Variables the image bakes in with `ENV` stay in the container;
  the smoke fails if the image it is given has one whose name contains `KEY`,
  `TOKEN`, `SECRET`, `PASSWORD` or `CREDENTIAL`; the runtime itself does not
  look.) The Ticket's
  `worker_model` is validated and forwarded as the
  CLI's `--model` argument; the Ticket's provider must match the configured
  one.
- **A failed Worker's output reaches one place, and is screened there.** When
  the Worker exits non-zero, what it wrote to stderr (stdout if stderr is
  empty) goes into the **Ticket error**, as one line cut at 260 characters (plus
  a closing `…` when it was cut), and nowhere else. The runtime keeps only the
  last 256 KiB of stderr and the last 8 MiB of stdout of a Worker. Every other
  form of the failure holds only the exit code and
  fixed words (`OS-isolated cloud Worker runtime 'docker' failed: isolated cloud
  Worker exited 7`): the exception's `str`, `repr`, `args`, class-name-prefixed
  and module-qualified forms, the traceback header, the chained cause, and the
  log lines that quote it (`logger.exception` in `cloud_runtime` and
  `logger.error` in the Ticket worker). The cost is that the Worker's stderr is
  no longer in the logs; it is in the Ticket error. Nothing around a Worker-free
  string, a class name, a `repr(...)` wrapper, a log prefix, a traceback, can
  assemble a forwarded value out of it.

  **The guarantee:** no complete forwarded value, in any of three forms (as
  given, with control characters made spaces and whitespace runs folded, and
  with only its whitespace collapsed), is in the Ticket error that is handed to
  the Ticket store. The check is made on that *final* string, after the fixed
  prefix that `cloud_runtime` puts in front of the output has been added, by the
  runtime that knows the values: the runtime builds the whole Ticket error at
  the point where it raises, under the prefix `cloud_runtime` uses
  (`launch_failure_prefix`, one definition for both), and `cloud_runtime` takes
  the finished text only if it starts exactly as its own prefix does, adding
  nothing afterwards. If a
  value occurs anywhere in it beyond the fixed words, the output is replaced by
  a fixed sentence that holds no part of it (the exit code stays). An occurrence
  wholly inside the fixed words themselves (a literal `7` against `exited 7`) is
  not a leak, is judged by its position in the final string, and withholds
  nothing; a value that starts in the fixed words and runs into the output (for
  example `failed: isolated cloud Worker exited 7: ok`) is a leak. A
  whitespace-only value is not a secret and is ignored. The slices the Ticket
  worker applies afterwards (`[:1000]`, `[:2000]`) only cut, which cannot
  assemble a value.

  Under the guarantee the output is also *redacted*, so the detail stays useful:
  each forwarded value (host copies and literals, however short) becomes `***`,
  longest first, on the raw output and again after it is made one line with each
  value normalized the same way, and only then is it cut to length. A Worker
  that writes `alpha<TAB>beta` for a value `alpha beta` is redacted, and so is a
  value on two lines. The output is kept as its last bytes, so it can begin
  inside a value or end inside one when a capture stops early: a piece of **at
  least four characters** at the very start or very end of the text is redacted
  as well; a shorter piece, or a piece in the middle of the text, is not. When
  redaction itself assembles another value, or a cut's ellipsis completes one,
  the guard withholds the detail instead.

  Limits, stated plainly. The guard and the redaction are **exact matches of
  whole values**: a re-encoded value (base64, JSON escapes, reversed), a piece
  shorter than four characters at an end and any piece in the middle are not
  caught. **The guarantee ends at the Ticket store.** What the store, the Source
  relay, notifications or a UI add around the Ticket error (prefixes, markdown,
  a subject line) is outside this runtime and not screened, and a value that
  those would complete is not seen here. And **a Worker that exits 0 reports in
  its own words: its final message is stored as the Ticket report with no
  screening at all** (screening it would also mangle legitimate text that
  contains a short literal such as `1`). The Worker holds the credential, so
  none of this is a boundary against it; it stops the runtime's own diagnostics
  from repeating a value.
- **No exception raised while handling what a Worker produced carries it out.**
  The failed-exit path above screens the text it quotes; everything else the
  runtime does with a Worker's output and files can raise an exception that
  quotes them (`int('<value>')` for a usage field the Worker wrote as a string, a
  `UnicodeEncodeError` that holds a file name the database cannot encode, a
  database error naming a path), including on a run that exited 0. Every such
  stage runs inside a wrapper that turns any `Exception` into a fixed message
  naming the stage, and nothing the original carried survives:
  - `isolated cloud Worker result could not be read: parsing the Worker's output`
    (the CLI's JSONL and its usage) and `... parsing the Worker's report`;
  - `isolated cloud Worker result could not be published: reading and storing
    the Worker's file changes` (reading the changed files, the budget check, the
    database write);
  - `isolated cloud Worker workspace could not be prepared: reading the
    workspace manifest` and `... materializing the workspace`, which read file
    names an earlier Worker run published;
  - `isolated cloud Worker runtime failed unexpectedly`, for anything else that
    escapes `launch()`, the cleanup in its `finally` included.

  The replacement is raised after the `try` statement rather than inside a
  handler, so it has no `__cause__` and no `__context__`; only the stage and the
  exception's **type name** are logged (at ERROR, without a traceback), so a
  database error's detail is no longer in the log either. The runtime's own
  fixed errors (such as a Ticket that left the run) pass through with their
  message and with anything they chained to dropped. `CancelledError`,
  `KeyboardInterrupt` and `SystemExit` are not `Exception`s: the stage
  wrappers let them through unchanged, and `launch()` re-raises the same object
  after clearing the frames of its chain and, if a container could not be
  removed, adding a note that names it (see "Frames" below and "Stop during a
  run"). `cloud_runtime` chains its `CloudWorkerLaunchError` to that fixed
  error, which chains to nothing, and `ticket_error` is the same fixed text.
  What this does not cover: an exception raised by a launcher other than this
  runtime keeps the earlier behaviour (its text, bounded), since only this
  runtime knows what its Workers were given.

  **Frames.** An exception's traceback keeps the frames it passed through, and a
  frame keeps its locals, which in the runtime are the Worker's stdout and the
  forwarded environment: a log record's `exc_info`, or
  `TracebackException(capture_locals=True)`, reaches them through an error
  whatever its message says. So every error that leaves `launch()`, the fixed
  errors above, the failed-exit error and the `BaseException`s that still
  propagate as the same object, has the frames of its whole chain cleared
  (`traceback.clear_frames`) once they have finished, and the replaced
  foreign exceptions inside the stages are cleared too. What is left in a
  traceback is file and line information, and the frames that are still running
  and cannot be cleared: `launch()`'s own and its callers'. `launch()` holds the
  Ticket and its prompt, not the settings or the output; **frames of callers
  outside this runtime are outside the guarantee** (a caller that keeps the
  settings object, or the Worker's output, in a local of its own is not
  protected by this).

  **Nothing else on the exception.** The failed-exit error carries the finished
  Ticket error as a plain string (`operator_message`) and nothing more: no
  function, closure, bound method, default argument, `functools.partial` or note
  refers to the forwarded values or to the raw output (an earlier version kept a
  closure over both and could be asked for them through a log record's
  `exc_info`). So the exception object can be handed to a logger, a debugger or
  code that walks its attributes. The tests walk everything reachable from the
  error and the captured log records (attributes, `args`, notes, cause and
  context, closure cells, defaults, partials, the locals of the traceback's
  frames, log record fields), bounded and cycle-safe, for a failed exit, an
  unreadable result, a publication failure, a timeout, a refusal and a
  cancellation; the scanner is itself shown to find a value hidden in a
  closure, a default argument or a partial. The one place the Worker's own
  screened text is reachable is the Ticket error string. What this cannot cover
  is code outside the runtime that has the values from elsewhere (the settings,
  the environment).
- **No network by default.** `--network none`; a worker CLI needs public
  egress, so a real deployment opts in with
  `AUTONOMY_CLOUD_WORKER_NETWORK=bridge`. The host network namespace is never
  shared, **but `bridge` is unfiltered egress and, on a VM-backed engine, it
  can also reach the host.** On colima (Virtualization.Framework, Docker
  29.5.2, the only engine this was observed on) the names
  `host.docker.internal` and `host.lima.internal` reached a listener on the
  host's `127.0.0.1`; the smoke also tries the container's default gateway
  address, which the recorded result does not list among those reached. A
  prompt-injected Worker could therefore connect to Postgres, Redis or the
  backend if they listen on the host's loopback. The smoke shows this live (see
  "Live smoke"). **Docker Desktop was not tested**; do not assume it is safer or
  the same, run the smoke's bridge report on it. The runtime cannot prevent
  this: run the Workers on an engine that does not share a host with those
  services (a dedicated colima profile or a separate machine), or filter the
  engine's egress yourself. A Linux engine running directly on the host (no VM
  between the Worker and the host's loopback) was not tested, so whether
  services bound to `127.0.0.1` are reachable there is not known from this
  work.
- **Read-only rootfs.** `--read-only` with a bounded `tmpfs` at `/tmp`
  (`size=512m`, `noexec`); the
  workspace mount is the only persistent writable surface. The Worker's `HOME`
  is a second private `tmpfs` at `/home/worker` (`size=256m`), deliberately not under `/tmp`:
  codex-cli 0.166.0 refuses to create its PATH helper aliases beneath the temp
  directory and warns that it could not (seen with the earlier `/tmp/home`).
- **Least privilege and limits.** `--cap-drop ALL`,
  `--security-opt no-new-privileges`, a non-root `--user` (host uid:gid so the
  mounted staging tree is readable; a uid or gid of 0 fails verification),
  memory with swap equal to memory (`--memory-swap`), CPU, PID and wall-clock
  limits (defaults and ranges in the settings table below). The output caps
  are not settings but constants in the source: the last 8 MiB of stdout and
  the last 256 KiB of stderr of a Worker are kept (see "A failed Worker's
  output" above).
  `--init` is passed as well. Codex itself is run with `--sandbox
  danger-full-access`: its own Linux sandbox is switched off because the
  container is the outer boundary and Codex's sandbox does not nest reliably
  inside a read-only root (the reason given in the source; the local daemon
  passes the same intent as `outer_sandboxed`).
- **Report path.** The worker's final message is the JSON completion report
  (`src/server/services/autonomy/completion.py`, the same contract as the
  local daemon and the server executor); a missing or invalid report settles
  as `waiting`, never as success. The Ticket is settled through
  `apply_worker_report` under its published worker thread **and** run id, so a
  stale attempt cannot settle a resumed line that reused the same thread. The
  launch binds a per-effect run id in the same write that publishes the thread
  and keeps a mandate
  heartbeat while the container runs, so the watchdog treats a live Docker
  run as live without a `LocalRunExecutor` run.
- **Fenced, non-destructive publication.** Created/modified files are synced
  back only when the Ticket is still the same running thread/run; the commit
  is one transaction that locks the Ticket row, takes a `SHARE ROW EXCLUSIVE`
  lock on the whole `workspace_files` table (so every other writer of that
  table waits for the transaction) and locks the workspace's rows, re-reads
  both the Ticket ownership and the manifest, and refuses on any mismatch with
  the Worker's snapshot. The
  staging copy is deliberately lossy (symlinks, directory rows and
  unresolved/oversized blobs are not materialized), so deletions are **never**
  published — a missing staging path can only be a snapshot limitation, not
  proof the Worker deleted it. Speculative (read-only) runs never publish. The
  no-deletion rule is a documented limitation.
- **Bounded publication.** The container's memory limit does not cover its
  bind mount, so a Worker can write far more than the backend can safely read.
  Before the fenced transaction the changed files (not the unchanged ones, and
  not what publication skips anyway: symlinks, files over 20 MiB, `node_modules`
  and similar) are counted by hashing in blocks, and a Worker that changed more
  than 5000 files or 128 MiB has its changes refused as not published.
- **Staging cleanup.** The Worker runs as the host user, so it can `chmod 000`
  a directory it made, which leaves a plain `rmtree` unable to delete the tree
  and silent about it, and it can nest directories deeper than a recursive
  `rmtree` (Python 3.12 raised `RecursionError` at about 1500 levels; this
  project needs 3.13 or newer, and `shutil.rmtree` on the 3.14.8 interpreter
  of the development environment removed a 3000-level tree in a check made for
  this page, 3.13 not tried, so depth alone no longer defeats `rmtree` there
  and the `chmod 000` and link-swap cases are what the walk is for) or a walk
  that keeps a descriptor per level can follow. Once the container is
  confirmed removed, one iterative walk makes the directories accessible again
  and deletes the tree. It is relative to a single open directory descriptor:
  it goes down by opening a child with `O_NOFOLLOW|O_DIRECTORY` (an unreadable
  one is chmodded through its parent's descriptor with `follow_symlinks=False`)
  and back up by opening `..` and accepting it only if it is the very directory
  (device and inode) it left, so a symlink a Worker swaps in underneath the
  walk is unlinked, never followed, a walk whose way up leads elsewhere stops
  and leaves the tree reported as not removed, and no depth of tree exhausts
  the stack or the process's descriptor limit. Files, links and special files
  are unlinked, not followed. If the container could not be confirmed gone the
  tree is not touched at all, after a launch as well as in the sweep: it stays
  where it is, with a warning, until the sweep finds its container gone. A
  tree that cannot be removed is logged and left to the sweep. A Worker can
  still fill the disk *while it runs* (the bind mount has no quota): put
  `AUTONOMY_CLOUD_WORKER_ROOT` on a size-limited filesystem.

### Container flags

What one Worker launch passes to `docker run` (`build_container_argv` and
`_isolation_flags` in `docker_worker_runtime.py`), with the default values. The
last column says whether verification checks that the engine applied it.

| Flag | Value | Probed |
|---|---|---|
| `--rm`, `--init` | removed when its process exits; an init process at PID 1 | no |
| `--name` | `conductor-cloud-worker-<16 hex>` (verification containers: `conductor-cloud-worker-probe-<10 hex>`) | no |
| `--label` | `conductor.cloud-worker.deadline=<epoch>` always; `conductor.cloud-worker.stage=<staging entry name>` when the workspace is a staging entry | no |
| `--network` | `none`; `bridge` only by `AUTONOMY_CLOUD_WORKER_NETWORK` | no |
| `--read-only` | root filesystem | yes (`/proc/self/mountinfo`) |
| `--tmpfs /tmp` | `rw,nosuid,nodev,noexec,size=512m` | `/tmp` writable only |
| `--tmpfs /home/worker` | `rw,nosuid,nodev,size=256m` (the Worker's `HOME`) | no |
| `--cap-drop` | `ALL` | yes (empty bounding set) |
| `--security-opt` | `no-new-privileges` | yes |
| `--user` | the backend's `uid:gid` | yes (none of the eight ids is 0; the effective pair is the configured one) |
| `--pids-limit` | `512` | yes (no looser) |
| `--memory`, `--memory-swap` | `2g`, the same value | yes (no looser; swap off, see below) |
| `--cpus` | `1.5` | no |
| `--mount` | `type=bind,source=<staging dir>,target=/workspace`, plus `,readonly` for a speculative Ticket | yes (a mount point, writable, empty) |
| `-i` | the prompt goes in on stdin | no |
| `--env-file` | the `0600` file in the staging root; only when something is forwarded | no |
| `-w`, `-e` | `-w /workspace`, `HOME=/home/worker`, `TMPDIR=/tmp`, `XDG_CONFIG_HOME=/home/worker/.config` | no |
| `-v` | each operator mount, `host:container` with `:ro` when asked (at most 8) | no |
| image, argv | `codex exec --json --skip-git-repo-check --sandbox danger-full-access [--model <model>] -C /workspace -` | the CLI is checked with `--version` |

The verification containers get the same flags (the probe adds `--entrypoint
/bin/sh` and runs `-c <script>`; the `--version` check mounts no workspace) and
**the same operator `-v` mounts**, but no forwarded environment.

### Settings

Read from the backend's process environment on every verification and launch,
so a change needs a restart of the backend (the verdict itself is cached for
15 s). A
`.env` file is loaded into that environment when `src.config` is imported
(`load_dotenv()`; variables already set win). The switch can also come from
`config.yaml`; the environment wins, even when set empty. A value outside its
range makes verification fail with a message about that limit (for example
`worker timeout out of range`), and nothing launches.

| Name | Default | Accepted |
|---|---|---|
| `AUTONOMY_CLOUD_WORKER_RUNTIME` (or `autonomy_cloud_worker_runtime`, at most 64 characters) | unset: off | `docker`; unset, empty, `none`, `off`, `disabled` mean off |
| `AUTONOMY_CLOUD_WORKER_IMAGE` | `conductor-cloud-worker:latest` | an image reference of at most 255 characters, the first a letter or digit, then letters, digits and `. _ / : @ -` |
| `AUTONOMY_CLOUD_WORKER_PROVIDER` | `codex` | `codex` only; the Ticket's `worker_provider`, when it has one, must equal it |
| `AUTONOMY_CLOUD_WORKER_NETWORK` | `none` | `none` or `bridge` |
| `AUTONOMY_CLOUD_WORKER_TIMEOUT` | `3600` | 10 to 86400 seconds |
| `AUTONOMY_CLOUD_WORKER_MEMORY` | `2g` | up to five digits, optionally a point and up to three decimals, then an optional `b`, `k`, `m` or `g` in either case; also the swap limit |
| `AUTONOMY_CLOUD_WORKER_CPUS` | `1.5` | above 0, at most 8 |
| `AUTONOMY_CLOUD_WORKER_PIDS_LIMIT` | `512` | 16 to 65536 |
| `AUTONOMY_CLOUD_WORKER_USER` | the backend's `uid:gid` | two numbers joined by `:`; a 0 passes the setting but fails verification |
| `AUTONOMY_CLOUD_WORKER_ROOT` | `~/.cache/conductor/cloud-workers` | an absolute path (`~` is expanded); created `0700` if missing, an existing directory with any group or other permission bit is refused |
| `AUTONOMY_CLOUD_WORKER_DOCKER_BIN` | `docker` | a command name, or a path to an executable file; no whitespace |
| `AUTONOMY_CLOUD_WORKER_ENV` | none | comma-separated variable names (a letter or `_` first, then letters, digits and `_`; at most 64 characters each), at most 32 distinct; `CONDUCTOR_*`, `HOME`, `TMPDIR`, `XDG_CONFIG_HOME` and `PATH` are refused |
| `AUTONOMY_CLOUD_WORKER_SET_ENV` | none | comma-separated `NAME=value`, at most 32, the same name rules and refusals as above; a value cannot contain a comma, and whitespace around it is stripped |
| `AUTONOMY_CLOUD_WORKER_MOUNTS` | none | comma-separated `host:container[:ro]` (or `:rw`), at most 8; the host source must be absolute (after `~` is expanded) and the container target must start with `/` and not be `/` itself; a path cannot contain a comma or a colon; reserved targets and `docker.sock` are refused (see "Workspace staging bind mount") |
| `AUTONOMY_CLOUD_WORKER_SWEEP_LABEL` | unset | `key=value` of letters, digits, `.`, `_`, `-`, the key starting with a letter or digit (see "Expiry label and sweep") |

Fixed in the source, not settings: the slack of 900 s past the Worker's limit
before a container counts as leaked, the sweep interval of 60 s, the 15 s
verification cache, the `tmpfs` sizes (`/tmp` 512m, `/home/worker` 256m), the
output caps (last 8 MiB of stdout, last 256 KiB of stderr), the publication
budget (5000 files, 128 MiB) and the three removal attempts. Changing any of
them is a code change.

The Docker client the runtime starts gets only `PATH` and, when the backend has
them, `DOCKER_HOST`, `DOCKER_TLS_VERIFY` and `DOCKER_CERT_PATH`; nothing else of
the backend's environment (so not `DOCKER_CONTEXT`, `DOCKER_CONFIG` or `HOME`)
is passed on. Whether an engine chosen only through `DOCKER_CONTEXT`, or only
through the Docker CLI's own config file, is found with that environment was
not checked; set `DOCKER_HOST` if in doubt.

### Verification

Before every launch (cached for 15 s per settings and staging root) the runtime
checks, using bounded Docker commands (15 s each for the first two, 30 s for
each of the two containers, 10 s per removal attempt):

1. the Docker CLI exists and the engine answers (`docker version --format
   {{.Server.Version}}`);
2. the configured worker image is present (`docker image inspect`);
3. a probe container with the **same** isolation flags **and a bind mount of a
   fresh empty directory created under the staging root** starts and reports,
   through the kernel's own files, what the flags did:
   - `/proc/self/mountinfo`: every mount of `/` is read-only (and there is
     one), and `/workspace` is a separate mount whose *mount point* (field 5
     of the record) is exactly `/workspace`: a directory the image merely
     contains, which `docker run -w` creates, does not count, and neither does
     a record that only mentions the path as its root or its source;
   - `/proc/self/status`: the capability *bounding* set is empty and
     `no_new_privs` is set; the real, effective, saved and filesystem user ids
     and group ids are all numbers, none of them 0, and the effective ones are
     exactly the configured `uid:gid`;
   - the cgroup records a PID limit and a memory limit no looser than the
     configured ones (cgroup v2 `pids.max`, `memory.max`; v1 equivalents). A
     missing file or the word `max` fails the probe, because that is how an
     engine looks when it silently dropped the limit (no memory controller,
     rootless without delegation). There is no override: an engine that hides
     its cgroup files cannot run Workers;
   - swap is disabled, from the same cgroup the memory limit came from. On
     cgroup v1 the memory+swap file (`memory.memsw.limit_in_bytes`) must exist,
     be a number and not exceed the memory limit (`--memory-swap` equal to
     `--memory`); a missing, unreadable or unbounded one fails, since a bounded
     memory limit says nothing about swap there. On cgroup v2
     `memory.swap.max` must be `0` and a file that exists but cannot be read
     fails; **a cgroup v2 hierarchy that publishes no `memory.swap.max` at all
     is still accepted** (no swap accounting to ask), which is an assumption,
     not a proof;
   - `/tmp` is writable and `/workspace` is writable by the Worker's user,
     readable and searchable, and empty.

   The probe fails closed. Every answer it relies on is read with its own
   status: a missing `/proc` or cgroup file, an empty, unparsable or
   short line, an unreadable swap file or workspace all fail it, and none is a
   comparison an empty string can satisfy (an earlier version passed
   `test "$(id -u)" != 0` and `test -z "$(ls -A ...)"` when those commands
   failed). The unit tests run the real script under whichever of `sh`, `dash`
   and `bash` the machine has, against fixture files for each of those shapes;
   those tests are skipped when run as root or off POSIX.

   The probe reads `/proc` and the cgroup files with the shell's own `read`
   and tests them with shell builtins; it does not call the image's `id`,
   `grep`, `cat` or `ls`, which could be made to say anything. (Tests run the
   script with a `PATH` of liars and with a `PATH` holding only `rm`.) **The
   one external command left is `rm`**, which deletes the probe file because
   the shell cannot, and the shell itself is still the image's own
   `/bin/sh`. That narrows what an unusual image can fake; it is not a defence
   against one that lies on purpose (see below).

   The probe is run as `docker run ... --entrypoint /bin/sh <image> -c <probe>`
   and must print its success line (`isolation-ok`) last on stdout; exit status
   0 alone is not accepted. This keeps an image `ENTRYPOINT` that ignores its
   arguments from turning the probe into a command that exits 0 and prints
   nothing; it is not a test of the entrypoint (the real Worker launch passes
   no `--entrypoint`, so the image's own entrypoint and command behave as they
   always did, and the next check is what exercises them).

   A write test on `/` cannot show the first three: a non-root user is denied
   there with or without `--read-only`, so the earlier probe verified an
   unhardened container too. The bind mount means a staging root the engine
   cannot mount (under `/tmp` or `/private/tmp` on colima) or whose path its
   mount syntax cannot carry fails *verification* with the engine's message,
   instead of verifying and then failing every Ticket with exit 125. The live
   smoke removes each control in turn and requires the probe to fail for that
   reason. `--cpus` and the network mode are not probed. The probe container
   is removed with the same retry-and-confirm rule as a Worker container
   before its directory is deleted, and verification fails if it cannot be;
4. the configured worker CLI (`codex --version` by default) works inside the
   image. This goes through the image's entrypoint exactly as a launch does,
   and the output must contain a line that names the provider as a word and
   carries a dotted version number (`codex-cli 0.166.0`). An entrypoint that
   swallows the command (exit 0, nothing printed), one that echoes it back
   (`codex --version`), and an image that prints something else (the probe's
   own `isolation-ok`, say) are all refused.

Any failure is a closed result: the Ticket fails with the verifier reason and
no container is launched.

**What verification is, and is not.** It checks the configuration of a
*trusted* image: that the engine applied the flags, that the image's CLI is
there and answers as a CLI should, that the staging root can be mounted. It
cannot defend against an image that lies. The probe and the version check both
run the image's own `sh`, `rm` and entrypoint; an image built to print
`isolation-ok` and `codex-cli 9.9.9` passes without hardening anything, and
nothing in this runtime can tell. Choosing the image, and building it from
sources you trust, is the operator's job (see "Security caveats").

## Enabling it

The backend's host needs the Docker CLI and a reachable engine (the runtime
runs the `docker` command; see `AUTONOMY_CLOUD_WORKER_DOCKER_BIN`), and the
runtime stays off until step 2. The steps below are headings, not a list, so
every command block starts at the
left margin and can be pasted from the raw file as it stands (a heredoc inside
an indented list item is not terminated when the indentation is kept).

### 1. Build a worker image

The image must contain the Worker CLI and satisfy
`docker run --rm <image> codex --version`, with any `ENTRYPOINT` it defines
passing the command through (a launch appends the CLI's argv to it). The commands below were run from a
clean checkout on 2026-11-05 (the second with its Dockerfile fed from a file
instead of the heredoc; the heredoc form itself was extracted from this raw
text and run, against the layers already built, on the same day) and produced
`conductor-cloud-worker:latest` with `codex-cli 0.166.0`; no credential is
passed to or stored in either image.

```sh
# 1. Base: the sandbox image (Ubuntu 24.04, Node 24 under /usr/local).
docker build -f Dockerfile.sandbox -t conductor-cloud-worker-base:latest .

# 2. Add the Worker CLI. Pinned: 0.166.0 is the npm `latest` of
#    @openai/codex (Apache-2.0) on 2026-11-05, and the version whose
#    `codex exec --help` lists every flag the runtime passes (the smoke
#    checks this). Raise it deliberately and rerun the smoke.
docker build --build-arg CODEX_VERSION=0.166.0 \
  -t conductor-cloud-worker:latest - <<'EOF'
FROM conductor-cloud-worker-base:latest
ARG CODEX_VERSION
RUN npm install -g "@openai/codex@${CODEX_VERSION}" && npm cache clean --force
EOF

docker run --rm conductor-cloud-worker:latest codex --version   # codex-cli 0.166.0
```

What the first build showed on this host (colima, Docker 29.5.2, arm64):

- The base and the derived image use different tags on purpose. An earlier
  version of this page tagged both `conductor-cloud-worker:latest`, so the
  second `FROM` named the image it was about to replace.
- The base build took 188 s because 12 of its 21 layers were already cached
  from an earlier sandbox build on this machine (the Python layer was
  rebuilt); a fully cold build was not timed. Rebuilding it again takes
  about a second. The derived layer took 70 s, but the first attempt was
  abandoned after about eight minutes at roughly 40 KB/s from
  registry.npmjs.org: the install is network-bound and the speed varied
  widely, so budget for it.
- `npm install -g` works under the base image's default user (`workspace`)
  without `USER root`, but only by an accident of ids. Re-read on the existing
  base image on 2026-11-05: `workspace` is uid 1001; `/usr/local/bin` and
  `/usr/local/lib/node_modules` (and `node` and `npm` in them) are owned by
  1001:1001; `/usr/local` itself is `root:root`. `Dockerfile.sandbox` has no
  `chown` for `/usr/local`: it extracts the Node tarball as root with
  `tar -xJf ... -C /usr/local`, so the owner is whatever uid the tarball
  records (it reads as 1001 here, which is an inference from the ids, not
  something the build states), and it matches only because `useradd` later gave
  `workspace` the same uid. A different base image or Node tarball may not
  line up, in which case `npm install -g` would fail with a permission error
  (not tried); putting `USER root` before the `RUN` and `USER workspace` after
  it is the obvious remedy, also not tried. `git log` shows `Dockerfile.sandbox`
  last changed in 42d933e17 (2026-11-03), before the builds recorded here.
- `Dockerfile.sandbox` sets no `ENTRYPOINT`; it ends in `USER workspace`,
  `WORKDIR /home/workspace` and `CMD ["sleep", "infinity"]`, which the derived
  image inherits. A launch passes its own argv in place of the `CMD`, and the
  runtime's `--user` replaces `USER`.
- The result is about 7 GB (`docker images` listed 7.08GB for the derived
  image and 6.51GB for the base when read again on 2026-11-05; the image
  config there has no `ENTRYPOINT`, `CMD ["sleep","infinity"]` and user
  `workspace`). The sandbox base brings
  LibreOffice, Chromium and a Docker CLI that a one-shot Worker never uses;
  any image with the CLI on `PATH` for an arbitrary uid and a passing
  `codex --version` in the hardened container satisfies the contract, but
  a slimmer base was not built or verified.

The pin was read again from the npm registry (`npm view @openai/codex version
license`) when this page was checked and again at its last review
(2026-11-05): `latest` was still 0.166.0, licensed Apache-2.0. The builds
were not repeated then. On 2026-11-05, against the
images that build left, `docker run --rm conductor-cloud-worker:latest codex
--version` printed `codex-cli 0.166.0` again.

The Ticket's `worker_provider` must match `AUTONOMY_CLOUD_WORKER_PROVIDER`; a
mismatch settles the Ticket failed instead of running a different CLI than the
Ticket asked for. This build supports only `codex`.

### 2. Configure the backend

Environment, not committed secrets:

```sh
AUTONOMY_CLOUD_WORKER_RUNTIME=docker
AUTONOMY_CLOUD_WORKER_IMAGE=conductor-cloud-worker:latest
AUTONOMY_CLOUD_WORKER_PROVIDER=codex
AUTONOMY_CLOUD_WORKER_NETWORK=bridge      # required for provider egress; read "No network by default" first
AUTONOMY_CLOUD_WORKER_TIMEOUT=3600
# optional: explicit credentials, e.g. a read-only mounted CLI home
# AUTONOMY_CLOUD_WORKER_MOUNTS=~/.codex:/codex-home:ro
# AUTONOMY_CLOUD_WORKER_SET_ENV=CODEX_HOME=/codex-home
# optional, only for a harness sharing the engine with other Workers: confine
# the expiry sweep to containers carrying this label (leave unset in production)
# AUTONOMY_CLOUD_WORKER_SWEEP_LABEL=my-harness=run1
```

`autonomy_cloud_worker_runtime: docker` in [`config.yaml`](../config.yaml)
works too (env overrides it); the environment variables name the image, egress
and credential opt-ins. The block above lists only the common ones: every
variable, its default and its range are in the "Settings" table. `~` in a mount
source is expanded by the backend to its own user's home.

### 3. Staging filesystem

A VM-backed engine bind mounts only the paths shared into its VM. On colima
(Virtualization.Framework, `mountType: virtiofs`, the only engine tried) the
home directory is shared and `/tmp` is not: a `--mount type=bind` source under
`/private/tmp` fails with `bind source path does not exist` and one under
`~/.cache` mounts (both re-run on 2026-11-05). **Docker Desktop was not tried**,
so which paths it shares is not claimed here. Keep
`AUTONOMY_CLOUD_WORKER_ROOT` (default `~/.cache/conductor/cloud-workers`) on a
path the engine can mount; since verification binds a directory from the root,
an unmountable root fails verification with the engine's message instead of
every Ticket. Under the home directory on colima the container's files come
back owned by the host uid:gid. A path with commas or quotes works (verified
on colima).

The root should be dedicated to this runtime and must be private (`0700`,
created as such; anything looser is refused). If it is shared with other things
nothing of theirs is swept (see "Expiry label and sweep"), but a Worker's disk
use still lands on the whole filesystem. It should sit on a filesystem with a
quota or of its own: nothing bounds what a Worker writes there while it runs. Keep
the Worker's `--user` equal to the backend's uid:gid, or the backend cannot
delete what the Worker wrote.

### 4. Restart the backend

Restart so the cached YAML accessor rereads `config.yaml`, then confirm the
switch state. Run this from the repo root with the backend's Python, in a
separate process with the backend's environment
(the same `AUTONOMY_CLOUD_WORKER_*` variables): it is not the server's own
state, and with the switch on it runs the real verification (two short
containers, one directory under the staging root). With the switch unset it
prints the off state without touching Docker, which was checked for this page
(`requested=None, enabled=False`, reason `no OS-isolated cloud Worker runtime is
configured (off by default)`):

```python
from src.server.services.autonomy.cloud_runtime import (
    resolve_cloud_worker_isolation,
)
print(resolve_cloud_worker_isolation())
# CloudWorkerIsolation(requested='docker', enabled=True, verified=True,
#   launch_wired=True, reason="runtime 'docker' verified available (...)")
```

## Refusal matrix

| Switch | Requested runtime | Verified | Launcher | Cloud Worker |
|---|---|---|---|---|
| off / unset / `none` / `off` / `disabled` | — | — | — | Refused: default error |
| on | malformed id | — | — | Refused: `not a valid runtime identifier` |
| on | not registered | — | — | Refused: `not implemented by this build` |
| on | registered, verifier unavailable/raises/off-contract | no | — | Refused: names the verifier reason |
| on | registered, verifier says available, no launcher | yes | — | Refused: `no isolated cloud Worker launcher is registered for it` |
| on | `docker`, verification fails | no | yes | Refused: names engine/image/probe/CLI reason |
| on | `docker`, verification passes | yes | yes | Launches in the disposable container; launch failures settle the Ticket failed |

Every message keeps the `Cloud Worker execution is disabled` prefix so the
Ticket error family stays stable for operators and the UI.

## Launch coverage and limits

Covered cloud paths now:

- the `launch_worker` effect for a queued cloud Ticket (manual task, source or
  schedule trigger) — this is the main path;
- a line **switch** onto a cloud target when the runtime verifies (the switch
  enqueues the normal launch effect instead of refusing);
- a legacy resume that has a thread but no run.

Not covered by this runtime (unchanged or stored for review; no shared-runtime
fallback is introduced):

- **Mid-run follow-ups**, handoff delivery and reassignment onto a running
  cloud Worker: replies are stored for review (a one-shot isolated Worker
  cannot take mid-run input); reassignment moved inputs are stored for review.
  These paths return without enqueuing a second launch while the runtime
  verifies, and keep their refusal/safety behavior when it does not.
- **Stop during a run**: the launch heartbeat polls the Ticket every 5 s and
  renews the mandate heartbeat every 60 s; a stop or
  switch makes it cancel the container and await teardown before the launcher
  returns (three failed Ticket reads in a row cancel the run as well, and the
  effect is left to be retried). The delay is one poll interval plus the
  database read plus the
  container removal, which on a slow engine is up to three `docker rm -f`
  attempts of 10 s with 1 s and 2 s pauses (about 33 s); it is not an
  instantaneous guarantee. **Teardown is protected from further cancellation:**
  the removal, with its retries and the backoff between them, runs as its own
  task that is waited for through a second or third cancellation, for at most
  60 s in all. The cancellation that arrived meanwhile is raised afterwards,
  never lost. If the container is confirmed gone, the staging tree is removed
  as usual; if it is not (the retries failed, or the 60 s ran out), the
  container is named in fixed words (in the error on a failing run, as a note on
  the `CancelledError` that propagates), logged at `ERROR`, and the staging tree
  is left in place for the sweep. The other teardown steps run in threads, which
  a cancelled await does not interrupt: the staging purge, the verification
  containers' removal and the sweep. A launch cancelled while it is still materializing
  its staging copy waits for the materializer thread (which cannot be stopped)
  for up to 120 s before removing the copy, and a second or third cancellation
  during that wait only continues it; if the thread has not finished by then the
  copy is left in place, with a warning, for the sweep, and the cancellation
  still propagates. The Ticket's stop/switch path owns the settlement,
  so the cancelled run does not write a stale report. A replacement launch can
  overlap an old container only for that detection/teardown window.
- **Crash orphan**: if the backend dies mid-run the wall-clock deadline dies
  with it — `--rm` only removes the container after its process exits, while
  the 60-second effect lease expires. The retry derives the same run id from
  the effect, verifies the bound run belongs to its own earlier attempt (a
  different effect's live run is left alone), and settles the line closed
  without launching a second container. The one exception is an earlier
  attempt that bound its run but never started a container (its mandate marker
  has no `container_started`): that retry launches, with the same run id.
  Settling an orphan does not depend on the OS-isolated runtime still being
  enabled or Docker being available: the orphan check in `worker.py` runs
  before the switch is consulted. The stale mandate heartbeat lets the
  watchdog fail the line after its normal stall window.

  The orphan itself keeps running, with whatever credentials and network it
  was given, and a crash also leaves the staging copy of the workspace and the
  `0600` env file holding the forwarded credentials in the staging root. The
  expiry label and the sweep (see "The Docker runtime") are what end this: the
  next launch that verifies the runtime, on the same engine and staging root,
  force-removes containers past their deadline (the Worker limit plus 900 s)
  and then deletes the staging entries with an ownership marker that are older
  than 1800 s and whose container is gone (a copy whose container still exists
  stays). The first version of that
  was reproduced with a backend `kill -9` mid-run on 2026-11-05: the container
  survived the kill with a `0700` copy and a `0600` env file beside it, and the
  sweep removed all three (with the slack in the deadlines patched negative, so
  the hour was not waited out). That hand run predates the staging-entry label
  and the rule that a tree waits for its container; it was not repeated with
  them, and the live smoke covers the same steps with a created container
  instead of a killed backend. **If no launch follows, nothing does**; an operator
  can run the same sweep from the repo root, with the backend's Python and
  environment:

  ```sh
  python -c "from src.server.services.autonomy import docker_worker_runtime as r; r.sweep_leftovers(r.DockerWorkerSettings.from_env())"
  ```

  It is not rate-limited like the sweep at a launch and honours
  `AUTONOMY_CLOUD_WORKER_SWEEP_LABEL` if that is in the environment.
  `sweep_leftovers` itself does not raise: a pass that cannot run is a warning
  on stderr (checked with a non-existent `AUTONOMY_CLOUD_WORKER_DOCKER_BIN` and
  an empty staging root: one warning, `container sweep failed
  (DockerWorkerRuntimeUnavailable)`, exit status 0, no Docker involved), so
  read its output. The one-liner is not covered by that guarantee, though:
  `DockerWorkerSettings.from_env()` runs outside it and validates every
  `AUTONOMY_CLOUD_WORKER_*` value, so an invalid one (checked with
  `AUTONOMY_CLOUD_WORKER_SWEEP_LABEL='bad label'`) ends the command with a
  `DockerWorkerRuntimeUnavailable` traceback and exit status 1 before any sweep
  runs. Exit status 0 therefore means the settings were valid and the pass was
  attempted, not that anything was removed.
- **Lease loss**: ownership is claimed and renewed before any preparation; a
  refused extension means another runner may own the effect, so the launch is
  cancelled (the container is cleaned up) or never started, and the re-claimed
  owner settles the line. Parent-cancellation also cancels the container task.
- **Report persistence**: if the report write fails after a successful run,
  the effect is retried (not acknowledged with the Ticket still running); the
  retry settles the orphaned run closed rather than starting a second
  container.
- **Worker auth**: the container has no credentials unless the operator
  allowlists or mounts them, never from the host environment by default. The
  Ticket is settled failed whenever the CLI exits non-zero or hits the
  wall-clock limit, but a missing credential does not make codex-cli 0.166.0
  exit early: with no credentials and `--network none` it started a turn,
  retried its provider connection and was still running when the smoke's 30 s
  (and an earlier 90 s) limit killed it. So an unauthenticated Worker holds
  its container until `AUTONOMY_CLOUD_WORKER_TIMEOUT`, which defaults to
  3600 s. The same case with `AUTONOMY_CLOUD_WORKER_NETWORK=bridge` was not
  run, since it would contact the provider.
- **Concurrency**: `AUTONOMY_MAX_RUNNING_WORKERS` (default 4, minimum 1) bounds
  the Workers started through the two Worker paths, cloud containers and
  local-daemon Tickets together. Both are counted as every Ticket with status
  `running` that is not orchestration-only, under one Postgres advisory lock,
  in the same step that makes the Ticket running. A daemon claim does that in
  `claim_daemon_ticket`, holding the lock as a session lock; a cloud launch does
  it in the write that moves its queued Ticket to `running` and binds its Worker
  thread (`update_ticket(slot_limit=...)`, which takes the same lock for its own
  transaction), before the container starts. Neither holds the lock while a
  container or a Worker runs, so a long Worker blocks nothing but its own slot.
  - *The lock is waited for, but not for long.* A claim only tries it: a busy
    lock answers a claim that carries a claim id with a 503 and `Retry-After: 1`
    (a claim without one gets no Ticket this poll). A launch waits for it, as the
    first statement of its transaction, for at most one second
    (`_SLOT_LOCK_WAIT`), and the same second bounds its wait for its own
    Ticket's row once it holds the lock. Waiting rather than trying is what keeps
    a launch from starving behind a daemon that polls hard, since a try can lose
    every time while a queued waiter is served at the next release. The wait
    holds no row lock, the one lock a launch may already hold (its Ticket's
    instruction handoff lock) is never taken by a claim, and every wait is
    capped at that second, so it cannot close a cycle that outlives it. A stuck
    writer on one queued Ticket can therefore keep claims and launches out for a
    second at a time, not indefinitely. A launch that runs out of that second is
    refused and defers like a full pool.
  - *A full pool is not a failure.* The Ticket stays `queued` and the launch
    effect goes back to pending, due 10 seconds later, with its attempt given
    back (`DeferEffect`, `defer_effect` in `database/autonomy/effects.py`), so a
    Ticket can wait for a long Worker without dead-lettering its effect and
    without adding a row per wait. A Ticket usually has several launch effects
    (the one made with it, the recovery sweep's, one per owner requeue); at
    each deferral all but the newest end, and the newest keeps waiting. Each
    pass of the one that waits reads the Ticket and counts the running Tickets,
    which scans the Tickets table (there is no index on `status` alone, and the
    daemon claim pays the same count up to twice per poll), so many waiting
    Tickets cost a scan each every 10 seconds.
  - *Waiting for the runner is not a failure either.* The deferral above only
    happens once the launch effect has been leased and its handler is running,
    and a lease spends one of the effect's six attempts and starts a 60-second
    clock. So the runner leases an effect only when it has a free place to start
    it in: each pass takes at most as many due effects as it has free places (5
    in all, `AutonomyEffectRunner.run_once`), makes no claim query when none is
    free (the sweep that parks exhausted leases still runs each pass), and
    leaves the rest pending, unleased and with their attempts, for a later pass.
    A place that frees wakes the loop at once when rows were left due; otherwise
    it polls once a second, as before. A pass that fails (the database
    unreachable) waits that second before the next, whatever has woken the loop.
    An effect that waits for a place never holds a lease, so waiting cannot
    expire one and cannot dead-letter it; a lease reaches no handler only after a
    crash or a pass cancelled mid-claim, which recovers when the lease expires,
    as before. This holds for every effect kind the runner executes, not only
    launches; their order (due time, then creation) and their retry rules (a
    failure still nacks with backoff and parks at six attempts) are unchanged.
    The runner used to lease up to 20 effects against its 5 places: with five
    long launches running, a sixth effect leased behind them waited with its
    lease running, every expiry cost an attempt, and after six it was parked
    dead without its handler having run (reproduced, see "Validation").
  - *Places across a stop and a restart.* The places a pass may fill are the 5
    less the handlers still running, counted from the tasks themselves. When the
    scheduler's pass loop ends without a shutdown (the lock's connection drops,
    say) it stops the runner, and it starts the same runner again when it
    retakes the lock. `stop()` waits up to 10 seconds for the pass loop to end and
    then up to 10 seconds for running handlers, and leaves the ones still going: it does not cancel them, because
    cancelling a launch settles its Ticket failed, and a healthy launch keeps
    renewing its own lease. Those survivors keep their places and the restarted
    runner counts them, so it leases only as many effects as there are free
    places once they are counted; a place frees when its survivor ends. A second
    `stop()` (the scheduler stops its runner from two places on shutdown) does
    not wait another 10 seconds. A different process that takes the pass lock
    has a runner of its own with 5 places, so while the old process's survivors
    run, the deployment can run more than 5 effects at once; the Worker cap is
    not affected, since it is counted in the database. And a handler that
    outlives its 60-second lease without renewing it (only the launch renews its
    lease) can be leased again while it still runs, as it could before; counting
    places does not change that.
  - *What a waiting Ticket looks like.* A deferral writes nothing to the Ticket,
    so after 30 minutes without a start the stall watchdog flags it ("Queued for
    N minutes without a Worker start"), as it does a Ticket no daemon has
    claimed. Waiting never spends an attempt, whether it is for a Worker slot
    (the deferral) or for a free place in the runner (the effect is not leased
    yet), and the effect keeps its creation time, so
    `oldest_pending_effect_seconds` in the metrics counts a Ticket that has
    waited an hour as an hour-old pending effect. A launch effect that is
    dead-lettered for real failures leaves the Ticket queued, and
    the recovery sweep re-enqueues at most once per attempt count, so after that
    only the watchdog's flag shows it.
  - *Threads.* A cheap count before the Worker thread is created keeps a
    steadily full pool from creating a thread on every attempt. The write is
    what takes the slot, so launches that pass the count together, or one that
    meets a busy lock, can each create a Worker thread and lose the write; the
    thread is cancelled, but cancelling stops runs and does not delete it, so an
    empty "Worker" thread stays in the workspace for each lost write. The
    effect runner's 5 concurrent effects bound a burst. The cleanup
    is best effort: a cancel that fails is logged and the Ticket still waits.
  - *What it does not bound.* The cap applies to the Workers these two paths
    start. A Ticket already `running` (a legacy resume with a thread and no run,
    or a retry that resumes a published run) holds its slot and is not counted
    again, and `admit_followup_delivery`, which sets a Ticket running when a
    follow-up is delivered to it, does not look at the cap at all, so the running
    count can stand above it. A legacy helper, `_launch_queued`, publishes a
    running line without the cap too; nothing calls it.
  - *Fairness.* The count has no user in it: it is global and first come. Slots
    held by one user's Workers (a cloud Worker holds its slot while its launch
    runs: `AUTONOMY_CLOUD_WORKER_TIMEOUT`, 3600 s by default, bounds only the
    container command; a daemon Ticket until its lease ends) keep every user's claims and cloud launches waiting, and there is
    no quota or aging. A daemon claims a freed slot as soon as it polls, while a
    waiting cloud launch looks again every 10 seconds (plus up to a second for
    the runner's poll, and it needs a free place in the runner at that moment),
    so under steady daemon load a cloud Ticket can lose every race for a freed
    slot and wait a long time.

  With the runtime off, which is the default, a cloud Ticket is refused before
  any slot is looked at, as before. A launch runs inside the autonomy effect
  runner, which runs 5 effects at a time (a fixed default, shared with every
  other effect kind) in the one process that holds the autonomy pass lock, and
  the launch holds its place there for as long as the launch runs. The
  `AUTONOMY_CLOUD_WORKER_TIMEOUT` is a command timeout: it wraps only the
  container command, so preparation, cleanup, publication and settlement are
  outside it and a place has no end-to-end deadline. Five long Workers therefore
  hold every place, and then no other effect starts until one of them ends: a waiting Ticket's
  next look at the cap, replies, report relays, input deliveries and Router
  turns stay pending, unleased and with their attempts, and can wait as long as
  the Workers' launches last (the command timeout bounds only part of that). Waiting does not dead-letter them, but it delays them, and no
  sixth cloud Worker starts in that process before a place frees, whatever the
  cap: a cap above 5 does not allow more than 5 cloud Workers there (daemon
  Workers do not use the runner), apart from the handover between processes
  described above. The default cap of 4 leaves one place for everything else,
  and each waiting Ticket's deferral uses it for a moment every 10 seconds. The
  cap and the deferral were exercised on a throwaway PostgreSQL 16 with the
  container runtime faked (see "Validation"); not with several real containers,
  and the smoke runs one Worker at a time. Each container is bounded as in
  "Container flags"; size the engine for the number you allow.

## Security caveats

- The Docker daemon is root-equivalent on the host. This runtime assumes a
  trusted operator, a trusted engine and a trusted worker image; image
  provenance and daemon access are deployment responsibilities. Verification
  does not change that: it checks a trusted image's configuration and cannot
  detect an image that lies (see "Verification").
- The container is an OS/process boundary for the Worker's files, environment
  and network, not a kernel or host-administrator boundary.
- Staging copies workspace files to the host filesystem under the configured
  root before mounting them; that root must be private to the backend user
  (`0700` created) and dedicated to this runtime, on a filesystem sized for
  the worst case: a Worker can write to its bind mount until the wall-clock
  limit, and nothing caps it. The 0600 env file with forwarded credentials
  lives in the same root for the length of the run.
- `AUTONOMY_CLOUD_WORKER_NETWORK=bridge` gives a prompt-injected Worker
  unfiltered egress and, on colima, a route to services on the host's loopback
  (shown by the smoke on the colima engine it was run on; Docker Desktop and
  a native Linux engine were not tested). Do not run Workers with `bridge` on a
  host whose loopback services you would not hand to the Worker, or filter the
  engine's egress.
- Explicit credential mounts/allowlists weaken the default "no host
  credentials" property by the operator's choice; scope them narrowly and
  read-only. The mount validator checks shape only (see "The Docker runtime"),
  so choosing a safe directory is the operator's job. The verification
  containers (the isolation probe and the `--version` check) are started with
  the same operator mounts, so a mounted credential directory is visible to the
  image's `sh` and CLI during verification too, before any Ticket runs.
- Codex runs inside the container with `--sandbox danger-full-access`: its own
  sandbox is off and the container is the only boundary around the Worker's
  commands.
- The Worker slot cap is global and first come (see "Concurrency"): one user's
  long-held slots, cloud or daemon, keep every other user's cloud launches
  queued as well as their daemon claims. There is no per-user quota; a hostile
  or merely stalled Worker can hold its slot for as long as its launch runs
  (`AUTONOMY_CLOUD_WORKER_TIMEOUT` bounds only the container command; there is no
  end-to-end deadline).
- This is not whole-product tenant isolation: computers, ordinary chat and
  MCP/vault credentials keep their existing shared-trust behavior.

## Validation

Focused unit coverage (fakes only, no live container required), plus a test file
against a real PostgreSQL:

- [`test_docker_worker_runtime.py`](../tests/unit/server/services/autonomy/test_docker_worker_runtime.py):
  settings validation (unsafe image/network/memory/PID/mount/env/sweep-label
  rejected), hardened `docker run` argv (one workspace mount, CSV-quoted for
  commas and quotes, no host env, `--network none`, read-only root, caps
  dropped, non-root user, limits, swap, expiry label, the staging-entry label,
  the image's entrypoint kept unless asked otherwise), the verification
  success/failure matrix (an exit 0 without the probe's success line, a
  silent, echoing or non-CLI version check, and a probe or version-check
  container that cannot be removed, each refused; a retried or already-gone
  removal accepted), caching per staging root, and that the probe binds a
  fresh directory from the root and runs under its own `--entrypoint /bin/sh`;
  the isolation probe script run against fixture `/proc` and cgroup files
  under `sh`, `dash` and `bash` (read-only root, empty capability bounding set,
  `no_new_privs`, the configured user and a non-root group, any root id among
  the four, PID and memory limits recorded and no looser than configured,
  cgroup v1 memory+swap and cgroup v2 swap, a real bind mount that is
  writable, listable and empty), plus every missing, empty, short and
  unparsable record, a workspace path that is only a record's root or source,
  and a second, writable mount of `/`, each of which must fail it, and runs of
  the script with a `PATH` of utilities that lie and with a `PATH` holding only
  `rm`; launch lifecycle (report parse, bind mount staging/sync, timeout and
  non-zero exit fail closed, a mandate that is not a dict refused before any
  secret is written, the env file removed when staging fails); container
  removal retried, confirmed and reported, and a tree left exactly where it is
  when it was not; a tree the Worker made unreadable, or nested 300 and 400
  levels deep (under a lowered recursion limit and descriptor limit), removed,
  symlinks not followed (including one swapped in for a directory in the middle
  of the walk), and a way back up that leads to another directory stopping the
  walk; a sweep that judges each tree by its own container (one whose
  container still exists is kept whatever the current timeout, a young one is
  kept for the launch still materializing it, a failed listing or a surviving
  expired container leaves every tree); the expiry sweep (expired labelled
  containers only, stale staging entries only, foreign names untouched,
  throttled to once a minute) and its label scope (an expired container
  without the scope label stays while an owned one is removed, in the same
  listing); a failed launch never carrying a forwarded value in any emitted
  form: `str`, `repr`, the class-name and module-qualified forms, `args`, the
  chained cause, what the logger wrote at ERROR with its traceback, and the
  Ticket error with its `[:1000]` and `[:2000]` slices, for values built from the
  cloud prefix, the runtime's words, the class names, the Worker's text, the
  ellipsis, the withheld sentence and the redaction marker, and for a value that
  redaction assembles out of two others; every stage that handles a Worker's
  output or files raising an exception that quotes a forwarded value (a usage
  field that is a string, a file name psycopg cannot encode, each stage patched
  in turn to raise `Exception`, `ValueError`, `OSError` or `UnicodeError` with
  the value), through `launch_cloud_worker`, with the value absent from `str`,
  `repr`, `args`, the whole `__cause__`/`__context__` chain, the log at DEBUG with
  tracebacks and `ticket_error`, no traceback in the stage's own log record, and
  cancellation, `KeyboardInterrupt` and `SystemExit` still propagating as themselves; the
  Worker's output in the Ticket error
  and in no other form; the exemption judged by position in the final string; the
  redaction itself (split by a tab, newline, carriage return, control character
  or run of spaces, in stderr and stdout; half of one at the length cut with
  the cut swept across the value's neighbourhood; the ends of a capture and the
  four-character rule at its edge; the remainder of a value that contains a
  shorter one); the publication budget; report-grammar parity with the local
  daemon; registration.
  Also: a Worker's data in no frame that leaves the runtime (the error and its
  chain rendered with captured locals, and the `exc_info` of every captured log
  record, for a usage parse, a publication, a failed exit, a timeout, a refusal
  and a cancellation; with the clearing disabled they all fail); the sweep's
  ownership rule (an unmarked look-alike of every kind, and forged, linked,
  group-readable, other-user and out-of-place markers, a marker inside the
  workspace, a marker for the wrong kind of entry, stale and young markers,
  entries claimed and released by staging, the env file and verification); a
  container that could not be removed named once in the Ticket error on each
  failing path; and repeated cancellation against an Event-controlled
  materializer thread, with the wait bounded.
  Also: everything reachable from the error and the log records free of the
  forwarded values and (outside the Ticket error) of the Worker's text, for each
  failure path; and teardown driven by events and a fake clock: a second
  cancellation during the backoff retried to success, a survivor after the bound
  named with the tree left, a cancellation after a timeout not swallowed, an
  uninterrupted teardown unchanged, and a cancelled await not stopping the
  staging purge.
- [`test_cloud_worker_smoke.py`](../tests/unit/scripts/test_cloud_worker_smoke.py):
  the live smoke's exit status against a fake engine (a container listing that
  errors, a container, image or directory that is not confirmed gone, all
  non-zero; no directory touched while the containers are not confirmed gone;
  `--keep` not a failure and not reported as "gone"), that its tree removal is
  the runtime's walk and does not follow a link swapped in mid-walk, that a
  `ps` that failed, showed nothing, or showed only some other Worker fails the
  process-argument check, that a missing default staging parent is refused
  rather than created, and that `PYTHON_DOTENV_DISABLED` is forced to `1`
  whatever was inherited.
- [`test_cloud_worker_runtime.py`](../tests/unit/server/services/autonomy/test_cloud_worker_runtime.py):
  default-off refusal, off spellings, unknown/malformed runtimes, verifier
  failures, verifier-without-launcher refusal, the launch effect's
  isolated-launch path, and no shared-runtime fallback.
- [`test_cloud_worker_slots.py`](../tests/unit/server/services/autonomy/test_cloud_worker_slots.py),
  [`test_autonomy_ticket_slot_gate.py`](../tests/unit/server/database/test_autonomy_ticket_slot_gate.py)
  and the runner case in [`test_effects.py`](../tests/unit/server/services/autonomy/test_effects.py):
  a full pool leaves a queued cloud Ticket queued and defers the launch effect,
  one with a newer sibling ends instead, a free slot launches, a running line is
  not counted again, a default-off refusal never looks at the slots, a slot
  refused at the write defers instead of failing the Ticket (also when
  cancelling the unpublished thread fails), a lock that stays held past the
  timeout is a refusal, the write sets its timeout, takes the guard, counts, then
  updates, with the key and the count a daemon claim uses, and the runner puts a
  deferred effect back without acking, nacking or dead-lettering it.
- [`test_effect_runner_admission.py`](../tests/unit/server/services/autonomy/test_effect_runner_admission.py)
  (12 cases): the production runner over an in-memory store that keeps the
  queue's lease, attempt-ceiling and fence rules, on a clock the test moves.
  With five launch handlers blocked (the runner's 5 places, each renewing its own
  lease as the real launch does) and a sixth effect due, ten lease periods later
  the sixth is still pending with no attempt spent and its handler was never
  reached, for a launch and for a non-launch kind; on a runner that leased a
  full batch of 20 per pass, the same run left it dead with 6 attempts and the
  handler never reached (the failure an independent review reproduced). A pass
  leases no more than its free places and makes no claim query when there is
  none, a settled effect frees its place for the next pass, a freed place is
  refilled at once rather than after the poll interval (30 s in the test;
  without the wake the test times out), and an idle runner still polls once per
  interval. A pass that fails backs off for the interval instead of retrying on
  the wake a finished task left set (one test after a full batch, one after a
  stop and a restart; a loop that retried on that wake made 200 passes, the
  test's ceiling, in the time the correct one makes 2 or 1), and a stop requested while
  a pass runs, which clears the wake, still ends the loop on its own. A handler
  still running when `stop()` gave up keeps its place across a restart: the next
  effect stays pending with no attempt spent until the survivor ends, then is
  leased once and runs (a `stop()` that forgot its survivors let the next effect
  be leased at once behind the survivor, never reaching its handler); a second
  `stop()` does not wait out the grace again.
- [`test_cloud_worker_slots_postgres.py`](../tests/integration/test_cloud_worker_slots_postgres.py)
  (`pytest -m integration`, `DB_*` pointing at a migrated scratch database; the
  default suite deselects it; 15 cases): on a real PostgreSQL, a full pool keeps
  the launch queued and its effect pending again with its attempt given back,
  eight deferrals spend no attempt, five launch effects of one Ticket (the
  creation one, the recovery sweep's and three owner requeues) leave one waiting
  and add no rows, an older effect still running does not end the newest, only
  the holder of an effect can defer it, the deferred effect launches once a slot
  frees, a slot lost between the check and the write defers and cancels the
  thread, a launch is not turned away by four daemon-claim loops (the product's own
  `_launch_lock`) that each hold the guard for 15 ms and try it again at once, a guard held past the timeout refuses after
  waiting, a writer sitting on a Ticket's row refuses its launch after the
  timeout and leaves the guard free for the next, and six cloud launches racing
  six daemon claims against a cap three above what was already running never
  exceeded it (checked while they ran), and filled it once the deferred effects
  ran again. With the guard's lock removed that concurrency test saw 5 or 6
  Workers at once against a cap of 3. Through the runner's own claim pass (the
  other cases lease a row by hand and call `_execute`), with the runner's two
  places held by two long launches, a third launch effect is not leased and
  keeps its attempts however often its lease would have run out over eight
  passes, and once the Workers end it is leased once and done; on the runner
  before this change the third was leased with the others and ended dead.

## Live smoke

[`scripts/cloud_worker_smoke.py`](../scripts/cloud_worker_smoke.py) runs the
product code against the real local Docker engine. It needs only Docker and
the backend's Python environment; it never reads `.env` (it sets
`PYTHON_DOTENV_DISABLED=1` before importing anything from the backend, whatever
the shell had exported), opens no network socket of its own (apart from the
loopback listener of the bridge report below), never connects to Postgres or
Redis, prints only non-secret facts and exits non-zero on any failed check,
including a cleanup it could not verify.

```sh
# From the repo root, with the backend venv. Builds and removes a throwaway
# stub-codex image from a local base (default ubuntu:24.04; no vendor CLI).
python scripts/cloud_worker_smoke.py

# Against a real Worker image: verification, the CLI still accepts the flags
# the runtime passes, and a credential-less launch fails closed.
python scripts/cloud_worker_smoke.py --image conductor-cloud-worker:latest
```

Options (`--help` lists them): `--image IMAGE` (real-image mode instead of the
stub), `--stub-base IMAGE` (the stub's `FROM`, default `ubuntu:24.04`; the
script runs a plain `docker build`, so the engine fetches the base only if it
does not already have it), `--docker-bin PATH` (the Docker CLI, default
`docker`), `--workdir DIR` and `--stage-parent DIR` (below) and `--keep`. It
prints `PASS`/`FAIL`/`INFO` lines and ends with
`N passed, M failed, K informational, in Ts; cleanup problems: P`; the exit
status is 1 if any check failed. It builds images and runs containers, so it is
run by hand: no Makefile target or workflow in `.github/workflows` mentions it
(searched by name), and the unit suite covers only its helper logic against a
fake engine.

**What a run owns, and only that, is removed.** The runtime is pointed at a
short generated `sh` wrapper around `docker` (`write_docker_wrapper` in the
script, 17 lines as written; the script's own docstring calls it "one-line",
which it no longer is). It adds a per-run label to every `docker run` the
runtime issues (not to `docker create`, which the runtime does not use), so the
leak checks and the cleanup find exactly those containers by label. The same
wrapper is how the removal-failure scenario below is produced: while a counter
file says so, it fails the next N `docker rm -f conductor-cloud-worker-<hex>`
calls (a Worker container's name; the verification containers' `-probe-` names
are not matched) with `smoke: simulated rm failure` and exit status 1, and
passes every other command through to the real binary. A real Worker launched meanwhile by the backend, or by another smoke
run, is neither reported as a leak nor removed by the cleanup (shown on
2026-11-05 by starting a container named like a Worker mid-run: it was still
running afterwards and the run had no failures).

The runtime's own **expiry sweep**, which every launch runs, used to be
engine-wide and would have removed any other *expired* Worker container on the
engine. The run now sets `AUTONOMY_CLOUD_WORKER_SWEEP_LABEL` to its per-run
label (see "The Docker runtime"), so every sweep it triggers, at a launch or
called by a scenario, lists and removes only containers carrying that label. A
scenario shows it with a decoy: the run creates a container named and labelled
like a Worker whose deadline passed long ago, without its own label; the
deadline-label query alone lists it, the scoped sweep leaves it alone, and the
run removes it itself. The decoy carries its own label so a crash cannot strand
it. **No unscoped sweep is ever run live**, since on a shared engine that is
exactly the hazard, and that is enforced rather than assumed: the run replaces
the runtime's expiry pass with one that refuses, before listing anything, any
settings whose label is not the run's, and counts the refusal as a failed
check. The unscoped behaviour (and a scoped sweep next to a foreign expired
container in the same listing) is covered by the unit tests only.

The fixture, the stub build context and the staging
root are private directories created inside `--workdir` and `--stage-parent`
(which must already exist; defaults: the system temp directory and
`~/.cache/conductor`), and each is removed whole, so nothing pre-existing there
is touched. **No parent is created** (the run removes only what it made, so a
parent it made would stay behind): if `~/.cache/conductor` is missing the run
stops at once and says to create it or pass `--stage-parent`, leaving nothing
behind. The run ends by removing what it made and
then **looking again**: containers by label, each image it built through
`docker images -q`, each directory. Anything still there, and anything it could
not look at (a container listing that errors counts as unknown, not as empty),
is a failed check and a non-zero exit; the message names the container, image
or path and never an environment value. The directories go last and only if
the containers are verified gone: with a container that may still be alive (a
failed listing, or one that survived `rm -f`) every directory is **left in
place** and named, since a Worker still running can rewrite its tree under the
walk. The walk is the runtime's own (one implementation, imported, not a second
copy), so a Worker's `chmod 000` tree, or a link swapped in underneath, is
handled the same way in both places. SIGINT, SIGTERM and SIGHUP run the same
cleanup (checked: killed mid-launch with a Worker up, nothing was left but the
non-zero exit). **SIGKILL cannot be handled** and leaves the labelled
container (or the sweep scenario's decoy), the stub images and the
directories:

```sh
docker ps -aq --filter label=conductor.cloud-worker-smoke | xargs docker rm -f
docker ps -aq --filter label=conductor.cloud-worker-smoke-decoy | xargs docker rm -f
docker images --format '{{.Repository}}:{{.Tag}}' | grep conductor-cloud-worker-smoke
```

`--keep` leaves the stub images and directories for inspection (containers are
still removed). The run then says only that its containers are gone (verified)
and lists each retained image and directory, by name and path, on a line of its
own (`INFO kept by --keep: ...`); it never calls them gone.

Product code under test: `verify_docker_runtime`, the
`AUTONOMY_CLOUD_WORKER_RUNTIME` switch (`resolve_cloud_worker_isolation`,
`launch_cloud_worker`), `launch` with its staging, hardened `docker run`,
timeout and cancel teardown, the container removal and sweep, and
`collect_changed_files`. Only the three seams that need Postgres are replaced:
the workspace manifest read, the materializer that fills the staging
directory, and the fenced publication transaction (replaced by a recorder of
what that transaction would receive). The stub image's `codex` reports what the
container looks like from inside and writes a file into `/workspace`.

Stub mode asserts:

- verification passes, and refuses a missing image and an image without the
  CLI; the isolation probe passes with every hardening flag and a bound
  workspace, and fails, *for the named reason*, with `--read-only`,
  `--cap-drop`, `--security-opt`, `--user`, `--pids-limit`, `--memory`,
  `--memory-swap` or the bind mount removed, or with a workspace the Worker
  cannot write;
- **images whose `ENTRYPOINT` ignores or echoes its arguments** (variants of
  the stub with `ENTRYPOINT ["/bin/true"]` and `ENTRYPOINT ["echo",
  "isolation-ok"]`): the probe, run under its own `/bin/sh`, still runs and
  prints its success line, and verification refuses each image, one because its
  `--version` check printed nothing and one because the echoed arguments do not
  identify the CLI. (An image that lies on purpose is not something this
  detects.)
- a launch through the switch returns `ready`; the Worker CLI got the
  runtime's one-shot argv on stdin; it ran as the host uid:gid with an empty
  capability bounding set, `no_new_privs`, a read-only root, no Docker socket
  and only loopback; the only writable non-tmpfs mount was `/workspace` (apart
  from the engine's own `/etc/hosts`, `/etc/hostname`, `/etc/resolv.conf`) and
  the only writable tmpfs mounts besides the engine's own were `/tmp` and
  `/home/worker`; the
  allowlisted host value and literal arrived, an unlisted host variable did not;
  the file the Worker wrote is in the host staging copy owned by the host uid
  and is the only changed file; the staging directory was `0700` and the env
  file `0600`; both were gone afterwards;
- a speculative Ticket mounts `/workspace` read-only and publishes nothing; a
  non-zero exit, unparsable output, a missing image, a bad model and a provider
  mismatch all fail closed; a 10 s timeout and a task cancel both remove a
  container that was observed running; forwarded values never appear in the host
  process arguments, judged only from a `ps` that succeeded and showed *this*
  run's launch (its unique label value and its running container's name, not
  just the shared name prefix); the default-off switch refuses and starts
  nothing;
- **a Worker that `chmod 000`s its tree** leaves nothing in the staging root;
- **a failing `docker rm -f`**: one failure is retried and the container is
  gone; three in a row leave the container, the Ticket error names it, the
  failure is logged at `ERROR`, the staging copy stays where it is (one entry,
  untouched) while the container is not confirmed gone, and the expiry sweep
  then removes the container by label and the sweep after it the copy (the
  scenario patches the 900 s slack to a negative number so the container is
  already past its deadline; the real hour is not waited out);
- **stale entries** (an env file and a `chmod 000` tree) are swept once past the
  age limit and not before (the "past" half patches the 900 s slack to a
  negative number, since 1800 s is not waited out; the "not before" half uses
  the real value), and a foreign name beside them is kept. When the
  engine cannot be asked which containers exist the env file is removed while
  the `chmod 000` tree is left untouched. With a real container (created
  by the run, never started) carrying the tree's staging label, the sweep
  leaves the tree alone, removes the env file, and removes the tree only after
  the container is gone;
- **the sweep's label scope**: the decoy described above survives a scoped
  sweep and is removed by the run that made it;
- **a staging root with a comma and quotes in its path** verifies and launches,
  and a root the engine cannot bind (the system temp directory on colima) fails
  verification with the engine's message and makes a launch refuse before
  staging; on an engine that can bind it this is reported, not judged;
- after each scenario the engine is checked for containers carrying the run's
  label, and the staging root for leftovers; the run ends with the verified
  cleanup described above.

It also **reports, without judging**, whether `--network bridge` lets the
Worker reach a TCP listener on the host's loopback: a listener on `127.0.0.1`
stands in for Postgres, Redis or the backend, and a bridge-mode stub tries it
through `host.docker.internal`, `host.lima.internal` and the default gateway.
The check that is judged is that bridge mode really gives the Worker an
interface besides `lo`.

`--image` mode runs only these (the other scenarios need the stub's behaviours
and are not run against a real CLI): the image bakes no environment variable
whose name contains `KEY`, `TOKEN`, `SECRET`, `PASSWORD` or `CREDENTIAL` (stub
mode checks this too); verification accepts the image and refuses a missing
one; `codex exec --help`, run under the runtime's hardened flags, lists every
flag the runtime passes (`--json`, `--skip-git-repo-check`, `--sandbox`,
`--model`, `-C`); `docker run --rm <image> codex --version` exits 0 and prints
something; `codex --version` under the runtime's `HOME` and `TMPDIR` prints no
"PATH aliases" warning; a launch with no credentials, `--network none` and a
30 s limit fails closed and publishes nothing; and the check that every sweep
was scoped to the run's label, with the no-leftover and cleanup checks.

Result on 2026-11-05 (colima, macOS Virtualization.Framework, arm64, Docker
Engine 29.5.2), at the commit that last changed the runtime or the smoke:

| Mode | Checks | Result | Time |
|---|---|---|---|
| stub image | 113 (+1 informational) | all passed; exit 0; verified cleanup: no container, no stub image, no directory of the run left | 45 s |
| `--image conductor-cloud-worker:latest` (codex-cli 0.166.0) | 13 | all passed; exit 0; same verified cleanup; no credential and `--network none`, so no vendor call | 34 s |

`docker ps -a`, `docker images` and `~/.cache/conductor` were compared before
and after both runs: unchanged, apart from the order `docker images` listed
two tags of the same image.

The counts were checked again against the script when this page was reviewed,
by counting the `check` calls each mode reaches, not by running it (that
review used no Docker): 13 for `--image`, and 113 for the stub on an engine that
cannot bind the system temp directory, as colima cannot. Where the engine can
bind it, that scenario reports an informational line instead of a check, which
gives 112 checks and 2 informational lines. The runtime, `cloud_runtime.py`,
the smoke and their unit tests have not changed since the commit that last
recorded these timings, commit 055d61512 (a `git diff` of those files against
it is empty, repeated at the last review). The run itself was not repeated.

In the stub run the bridge report read: **yes**, the Worker reached the host's
loopback listener through both `host.docker.internal` and `host.lima.internal`
(two connections accepted). That is the colima/virtiofs engine behaving as
described under "No network by default", not a defect the runtime can fix.

**Recorded, or re-run at the last review.** Several figures and claims on this
page are manual observations from the builds and runs of 2026-11-05 that were
not repeated, and they should be read as recorded, not re-verified: the build
timings (188 s for the base with 12 of its 21 layers cached, 70 s for the
derived layer, the abandoned attempt of about eight minutes at roughly
40 KB/s), the two smoke results above, the `kill -9` reproduction, the
SIGINT/SIGTERM cleanup and the decoy checks made mid-run, the Python 3.12
`RecursionError` at about 1500 levels (no 3.12 interpreter was available at the
last review), and the walk run in a Linux container (glibc 2.39, Python 3.12).
(`Dockerfile.sandbox` has 21 instructions, which is consistent with "21
layers"; that is all that was confirmed about the count.) What the last review
did re-run, on the same colima engine and the images that remained: the
`bind source path does not exist` failure under `/private/tmp` and the success
under `~/.cache`; `codex --version` in `conductor-cloud-worker:latest`; the
image sizes, config and ownership under `/usr/local`; that the `codex` binary
in the image contains the strings `CODEX_API_KEY`, `OPENAI_API_KEY` and
`CODEX_HOME` (a string match, not a statement of which ones `codex exec`
reads); the `--help` of the smoke; the sweep one-liner with and without a
valid environment (no Docker involved); the 3000-level `shutil.rmtree` on
Python 3.14.8 (it removed the tree); and the npm registry pin. Nothing was
built, no smoke run was repeated, and no vendor call was made.

What the smoke and its reviews have found in the runtime, all fixed:

- the isolation probe passed whether or not `--read-only`, `--cap-drop` or
  `--security-opt no-new-privileges` was present; the Worker `HOME` under
  `/tmp` made the real CLI warn that it could not create its PATH helpers;
- a Worker could make its staging copy undeletable (`chmod 000`), and a single
  failed `docker rm -f` was swallowed at debug level, leaving a live
  container; neither was visible to the benign stub of the first run;
- the credential env file leaked when the Ticket's mandate was not a dict;
- verification never bound the staging root, so a root the engine could not
  mount, or whose path held a comma, verified and then failed every Ticket with
  exit 125;
- the probe passed without `--user`, `--pids-limit` and `--memory`, and swap was
  unlimited;
- nothing bounded what publication read into backend memory;
- the smoke itself removed any container with the runtime's name prefix that
  appeared during its run, and `--workdir` deleted directories it did not make;
- a second review of the smoke and the runtime found, and these are fixed:
  the run's launches executed the engine-wide expiry sweep, which would have
  removed another backend's expired Worker (now scoped by label, see above);
  the staging sweep repaired and deleted trees even when an expired container
  survived `rm -f` or the container listing failed, and the repair walk was a
  `lstat` followed by a path-based `chmod` that a Worker could race with a
  symlink (now confirmed-gone first, then descriptor-relative with
  `O_NOFOLLOW`); the isolation probe was satisfied by a failed or silent `id`,
  by a failed `ls`, by a `/workspace` path that only appeared as a record's
  root or source, and by a read-only mount of `/` next to a writable one (now
  failing closed); verification accepted exit 0 from a probe that never ran
  because the image's `ENTRYPOINT` swallowed its arguments; the smoke printed
  "0 left behind" and exited 0 when it could not list containers; and an
  inherited `PYTHON_DOTENV_DISABLED=false` let the smoke's imports read `.env`;
- a third review found, and these are fixed: the staging sweep aged every tree
  by the *current* timeout, so a Worker started under a long limit and swept
  under a shorter one lost its tree while its unexpired container still ran
  (trees are now judged by the container that mounts them, found through a
  label, with a fixed age only for a launch that has no container yet); the
  smoke's cleanup went on to delete directories after a failed container
  listing or removal, with its own check-then-chmod walk that a swapped symlink
  redirected (now confirmed-gone first, and the runtime's walk); verification
  deleted a probe directory after a single unchecked `rm` (now retried and
  confirmed, or left in place and failed); any non-empty `--version` output
  passed (now it must name the CLI and carry a version), and the probe called
  the image's own `id`, `grep`, `cat` and `ls` (now `read` and builtins, only
  `rm` is external); on cgroup v1 an unlimited memory+swap limit next to a
  bounded memory limit passed (now it must be recorded and no larger than the
  memory limit); the repair walk stopped at 128 levels, held a descriptor per
  level, and left such a tree for every later sweep to fail on (now one
  descriptor, no recursion, any depth); and a failed `ps` made the
  process-argument check pass;
- a fourth review found, and these are fixed: the failure detail put a forwarded
  value back together after redacting it, when folding a tab or newline into a
  space turned the Worker's split `alpha<TAB>beta` into the value `alpha beta`
  (redaction is now last, and covers the cut ends of a captured stream); the
  process-argument check accepted any Worker's launch as the witness (now this
  run's); `--keep` still claimed everything was gone (now retained resources
  are listed as kept); and the smoke created `~/.cache/conductor` and left it
  (it is no longer created);
- a fifth review found that the redaction passes, the cut's ellipsis and
  `cloud_runtime`'s second cut could each recombine with their neighbours into a
  forwarded value, and a sixth that the text added *after* the check (the outer
  `... failed:` prefix, the exception class name a traceback adds) completed one
  that passed it, and a seventh that exceptions raised while handling a Worker's
  output on a run that exited 0 (a usage field, a file name) bypassed the
  screening altogether and quoted the Worker in the logs and the Ticket error
  (now every such stage raises a fixed message). The Worker's output now lives in
  one string, the Ticket error,
  checked as that final string with its real prefix; the exception, its `repr`,
  its traceback and the log lines carry no Worker text at all (see "The Docker
  runtime");
- an eighth review found, and these are fixed: Worker data survived in the
  *frames* of a traceback (`LogRecord.exc_info`, `capture_locals`) though not in
  any message (now cleared, see "Frames"); the staging sweep chose entries by
  name shape and age alone and would have deleted an unrelated look-alike in a
  shared root (now ownership markers); a second cancellation during
  materialization removed the staging copy under the still-running writer, which
  recreated it (now a bounded, shielded wait); and the Ticket error named a
  container that could not be removed only on the timeout path (now on every
  failing path);
- a ninth review found, and these are fixed: the failed-exit error still held
  the forwarded environment in the closure of the function that screened the
  Ticket text, reachable through a log record's `exc_info` (the Ticket error is
  now finished at the raise point and only the string is kept); and a second
  cancellation during the removal's backoff left one attempt, a container that
  survived and no mention of it (now a protected, bounded teardown).

What this does not prove:

- **No live vendor call was made through the product launch path.** A one-off
  direct `docker run` of the image with the host's read-only Codex auth returned
  a real `CLOUD_REAL_OK` turn (documented under "What this does not prove");
  that verifies the image and vendor auth, not the runtime's credential
  forwarding. The product launch path was not used for a vendor call. The
  process environment of the verifying shell held no Codex
  credential (no `OPENAI_API_KEY`, `CODEX_API_KEY` or `CODEX_HOME`; names were
  checked, values and files were not read), and no other place was searched.
  To verify it, provide one credential the documented way and run one Ticket
  (or `launch` with a one-line prompt) with `AUTONOMY_CLOUD_WORKER_NETWORK=bridge`:
  either an API key whose environment name is listed in
  `AUTONOMY_CLOUD_WORKER_ENV`, or a Codex login directory mounted through
  `AUTONOMY_CLOUD_WORKER_MOUNTS` with `CODEX_HOME` set. The codex-cli 0.166.0
  binary references `CODEX_API_KEY`, `OPENAI_API_KEY` and `CODEX_HOME`, but
  which of them `codex exec` accepts from the environment, and whether a
  read-only mounted login directory survives a token refresh, were not tested.
  Whether the Worker can edit files with its helper aliases working is likewise
  untested.
- **Postgres-backed steps are not exercised**: materializing a real manifest
  and the fenced publication transaction (unit tests cover their logic with
  fakes).
- **A killed backend is not part of the smoke.** It was reproduced once by hand
  (a driver process killed with `kill -9` while its Worker ran; the container,
  a `0700` staging copy and a `0600` env file survived and the sweep removed
  all three), with the slack added to the deadlines patched to a negative value
  so the sweep did not have to wait out the real hour. What the smoke does show
  live is the same sweep removing a container that a failing `rm` left behind.
- **The unscoped sweep, and the symlink race itself, are not exercised live.**
  An unscoped sweep would touch other Workers on a shared engine, so only unit
  tests run it; the swap of a directory for a symlink in the middle of the
  walk is simulated by the unit tests (which fail against the earlier
  walks), not provoked against a real hostile container. The descriptor-relative
  walk was run on macOS (the smoke, and unit tests with trees 300 and 400 levels
  deep under a lowered recursion and descriptor limit) and, with its functions
  copied out, in a throwaway Linux container (glibc 2.39, Python 3.12: a
  1500-level tree with those limits lowered). Where `fchmodat` does not support
  `AT_SYMLINK_NOFOLLOW` at all (glibc before 2.32; not tried) the walk cannot
  chmod an unreadable directory and leaves it, so the tree is reported as not
  removable instead of anything being followed. The walk returns up through
  `..` and checks the identity of what it reaches; that check is exercised by
  a unit test that substitutes a different directory, not by a hostile
  filesystem. The staging-label rule is exercised live with a container the run
  creates for a stale tree, never with a Worker.
- **The image stays trusted, and verification cannot see a lying one.** The
  probe runs the image's own `/bin/sh` and `rm`, the version check runs the
  image's own entrypoint, and an image built to print `isolation-ok` and
  `codex-cli 9.9.9` would pass. Naming the probe's shell, requiring its success
  line, reading the kernel's files with builtins and requiring the CLI to name
  itself with a version narrow what a *broken or unusual* image can get past;
  none of that is a guarantee against a malicious one.
- **Swap on cgroup v2 where no `memory.swap.max` is published** is accepted
  (an assumption: no swap accounting to ask), and the cgroup v1 memory+swap
  rule was exercised against fixtures only, since the verifying engine uses
  cgroup v2.
- The probe reads the PID and memory cgroup limits the engine applied; `--cpus`
  and the network mode are not probed, and a Linux engine other than the
  colima VM was not tried.
- **More than one Worker at a time was not run against a real engine.** The
  smoke runs one Worker container at a time. The tests hold the effect runner's
  places with several blocked launch handlers, and the slot cap was exercised on
  PostgreSQL, both with the container runtime faked (see "Concurrency" under
  "Launch coverage and limits"); nothing put the engine under several long real
  Workers.
- Production hosts, image provenance and Docker daemon hardening, as before.
